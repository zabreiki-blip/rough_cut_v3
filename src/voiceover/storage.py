"""Storage of recorded voiceover sources and the per-block range map.

Layout (project-relative):
  ЗАГРУЗИТЬ/Закадр/            user drop folder (never modified)
  speaker/recordings/<sha256>/ immutable imported source (+ one shared PCM WAV for MP3)
  work/recorded_voiceover.json working map: recordings + ranges (+ generation)
  work/recorded_voiceover_asr/ ASR cache location (key = file hash + ASR config)

No ``zkN.wav`` files are produced and no ASR is run here.
"""

from __future__ import annotations

import hashlib
import json
import os
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
import re
import shutil
import subprocess
import threading
from typing import Any, Callable, Iterator, Mapping
import unicodedata
import uuid
import wave

from atomic_io import atomic_write_json
from inbox_service import discover_project_inbox
from project_service import read_json_safe
from voiceover.models import RecordingSource, VoiceoverRange

MAP_VERSION = 1
Converter = Callable[[Path, Path], None]


def _nfc(name: str) -> str:
    return unicodedata.normalize("NFC", name)


def _part_sort_key(name: str) -> tuple[tuple[Any, ...], str]:
    """Natural order: digit runs compare as numbers ("часть 2" < "часть 10")."""

    folded = _nfc(name).casefold()
    # re.split with a capture group alternates text (even) and digits (odd).
    chunks = tuple(
        int(chunk) if index % 2 else chunk for index, chunk in enumerate(re.split(r"(\d+)", folded))
    )
    return (chunks, _nfc(name))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def safe_project_file(root: Path, relative: str) -> Path | None:
    """Resolve a project-relative path; ``None`` if unsafe or not an existing file."""

    if not isinstance(relative, str) or not relative or "\\" in relative or "\x00" in relative:
        return None
    candidate = Path(relative)
    if candidate.is_absolute() or ".." in candidate.parts:
        return None
    safe_root = root.resolve()
    try:
        resolved = (safe_root / candidate).resolve(strict=True)
    except OSError:
        return None
    if not resolved.is_relative_to(safe_root) or not resolved.is_file():
        return None
    return resolved


def _empty_map() -> dict[str, Any]:
    return {"version": MAP_VERSION, "generation": 0, "recordings": [], "ranges": [], "rejected": []}


def _normalize_map(raw: Mapping[str, Any], root: Path) -> dict[str, Any]:
    result = _empty_map()
    generation = raw.get("generation")
    if isinstance(generation, int) and not isinstance(generation, bool) and generation >= 0:
        result["generation"] = generation
    for key in ("scenario_revision_id", "normalization_version"):
        if isinstance(raw.get(key), (str, int)) and not isinstance(raw.get(key), bool):
            result[key] = raw[key]

    recordings: dict[str, RecordingSource] = {}
    seen_hashes: set[str] = set()
    raw_recordings = raw.get("recordings")
    for item in raw_recordings if isinstance(raw_recordings, list) else []:
        try:
            recording = RecordingSource.from_dict(item)
        except (ValueError, TypeError, AttributeError):
            continue
        if recording.id in recordings or recording.sha256 in seen_hashes:
            continue
        if safe_project_file(root, recording.asset) is None:
            continue
        if safe_project_file(root, recording.original_asset) is None:
            continue
        recordings[recording.id] = recording
        seen_hashes.add(recording.sha256)

    ordered = sorted(
        recordings.values(),
        key=lambda item: (item.part_index, *_part_sort_key(item.part_name)),
    )
    result["recordings"] = [item.to_dict() for item in ordered]

    accepted: list[VoiceoverRange] = []
    seen_blocks: set[str] = set()
    raw_ranges = raw.get("ranges")
    for item in raw_ranges if isinstance(raw_ranges, list) else []:
        uid = str(item.get("block_uid", "")) if isinstance(item, dict) else ""

        def reject(reason: str, uid: str = uid) -> None:
            result["rejected"].append({"block_uid": uid, "reason": reason})

        if not isinstance(item, dict):
            reject("range is not an object")
            continue
        owner = recordings.get(str(item.get("recording_id", "")))
        is_missing = item.get("status") == "missing"
        if owner is None and not is_missing:
            reject("unknown recording")
            continue
        try:
            parsed = VoiceoverRange.from_dict(
                item, duration_sec=owner.duration_sec if owner is not None else 1.0
            )
        except (ValueError, TypeError) as exc:
            reject(f"invalid range: {exc}")
            continue
        if parsed.block_uid in seen_blocks:
            reject("duplicate block_uid")
            continue
        if parsed.status == "confirmed":
            clash = next(
                (
                    other
                    for other in accepted
                    if other.status == "confirmed"
                    and other.recording_id == parsed.recording_id
                    and parsed.source_in_sec < other.source_out_sec
                    and other.source_in_sec < parsed.source_out_sec
                ),
                None,
            )
            if clash is not None:
                reject(f"confirmed range overlaps block {clash.block_uid}")
                continue
        seen_blocks.add(parsed.block_uid)
        accepted.append(parsed)
    result["ranges"] = [item.to_dict() for item in accepted]
    return result


def load_recorded_voiceover(paths: Mapping[str, Path]) -> dict[str, Any]:
    """Read and validate the working map. Missing/old/corrupt map -> empty result."""

    raw = read_json_safe(paths["recorded_voiceover"])
    if not raw:
        return _empty_map()
    return _normalize_map(raw, Path(paths["root"]))


class StaleMapError(RuntimeError):
    """The map on disk changed after the caller read it (generation compare-and-swap)."""


_MAP_LOCKS: dict[str, threading.RLock] = {}
_MAP_LOCKS_GUARD = threading.Lock()


@contextmanager
def map_lock(paths: Mapping[str, Path]) -> Iterator[None]:
    """The one lock of the working map: registration and job commits are serialized by it."""

    key = str(paths["recorded_voiceover"])
    with _MAP_LOCKS_GUARD:
        lock = _MAP_LOCKS.setdefault(key, threading.RLock())
    with lock:
        yield


def save_recorded_voiceover(
    paths: Mapping[str, Path],
    data: Mapping[str, Any],
    *,
    expected_generation: int | None = None,
) -> dict[str, Any]:
    """Validate ``data``, bump the monotonic generation and write atomically.

    With ``expected_generation`` the write is a compare-and-swap: a map that changed
    on disk since it was read raises :class:`StaleMapError` instead of being overwritten.
    """

    root = Path(paths["root"])
    with map_lock(paths):
        normalized = _normalize_map(data, root)
        on_disk = read_json_safe(paths["recorded_voiceover"]).get("generation")
        disk_generation = (
            on_disk if isinstance(on_disk, int) and not isinstance(on_disk, bool) else 0
        )
        if expected_generation is not None and disk_generation != expected_generation:
            raise StaleMapError("recorded voiceover map changed; reload and retry")
        normalized["generation"] = max(normalized["generation"], disk_generation) + 1
        normalized.pop("rejected", None)
        atomic_write_json(paths["recorded_voiceover"], normalized)
    normalized["rejected"] = []
    return normalized


def update_recorded_voiceover(
    paths: Mapping[str, Path], mutate: Callable[[dict[str, Any]], Any]
) -> dict[str, Any]:
    """Re-read the map under the map lock, apply ``mutate`` and save (no lost updates)."""

    with map_lock(paths):
        data = load_recorded_voiceover(paths)
        mutate(data)
        return save_recorded_voiceover(paths, data)


def asr_cache_key(sha256: str, config: Mapping[str, Any]) -> str:
    """Cache key from the file hash and the effective ASR configuration only."""

    payload = json.dumps(
        {"sha256": sha256, "config": dict(config)},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def asr_cache_path(paths: Mapping[str, Path], sha256: str, config: Mapping[str, Any]) -> Path:
    return Path(paths["recorded_voiceover_asr"]) / f"{asr_cache_key(sha256, config)}.json"


def ffmpeg_to_pcm_wav(source: Path, destination: Path) -> None:
    """Decode any audio file to 16-bit PCM WAV keeping sample rate and channels."""

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise RuntimeError("ffmpeg is required to convert non-WAV recordings")
    result = subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-y",
            "-i",
            str(source),
            "-vn",
            "-c:a",
            "pcm_s16le",
            str(destination),
        ],
        capture_output=True,
        text=True,
        timeout=3600,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {result.stderr.strip()[:500]}")


def _probe_pcm_wav(path: Path) -> tuple[float, int, int]:
    with wave.open(str(path), "rb") as stream:
        rate = stream.getframerate()
        frames = stream.getnframes()
        channels = stream.getnchannels()
    if rate <= 0 or channels <= 0 or frames <= 0:
        raise ValueError(f"empty or invalid WAV: {path.name}")
    return frames / rate, rate, channels


def _copy_atomic(source: Path, destination: Path) -> None:
    temporary = destination.with_name(f".{destination.name}.part")
    try:
        shutil.copyfile(source, temporary)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _freeze(path: Path) -> None:
    try:
        path.chmod(0o444)
    except OSError:
        pass


def _reindex(recordings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered = sorted(recordings, key=lambda item: _part_sort_key(str(item.get("part_name", ""))))
    for index, item in enumerate(ordered):
        item["part_index"] = index
    return ordered


@dataclass
class PreparedRecording:
    """A recording already copied/converted into a private temporary store directory."""

    digest: str
    temp_dir: Path
    original_name: str
    montage_name: str
    duration_sec: float
    sample_rate: int
    channels: int
    part_name: str


def discard_prepared_recording(prepared: PreparedRecording) -> None:
    shutil.rmtree(prepared.temp_dir, ignore_errors=True)


def prepare_recording_file(
    paths: Mapping[str, Path],
    source: Path,
    *,
    converter: Converter | None = None,
    digest: str | None = None,
) -> PreparedRecording | RecordingSource:
    """Heavy part of the import (hash, copy, ffmpeg) into a temporary directory.

    Does not touch the map, so callers run it outside any project lock. Returns the
    existing record when the content hash is already registered.
    """

    root = Path(paths["root"]).resolve()
    source = Path(source)
    if source.is_symlink():
        raise ValueError("symbolic links are not accepted as recordings")
    resolved = source.resolve(strict=True)
    if not resolved.is_relative_to(root) or not resolved.is_file():
        raise ValueError("recording must be a file inside the project")

    digest = digest or _sha256_file(resolved)
    for existing in load_recorded_voiceover(paths)["recordings"]:
        if existing["sha256"] == digest:
            return RecordingSource.from_dict(existing)

    parent = Path(paths["voiceover_recordings"])
    parent.mkdir(parents=True, exist_ok=True)
    temp = parent / f".tmp-{uuid.uuid4().hex}"
    temp.mkdir()
    try:
        original = temp / f"original{resolved.suffix.lower()}"
        _copy_atomic(resolved, original)
        _freeze(original)
        montage = original
        probed: tuple[float, int, int] | None = None
        if original.suffix == ".wav":
            try:
                probed = _probe_pcm_wav(original)
            except (wave.Error, EOFError):
                probed = None  # not plain PCM: convert below
        if probed is None:
            montage = temp / "montage.wav"
            partial = temp / "montage.partial.wav"
            try:
                (converter or ffmpeg_to_pcm_wav)(original, partial)
                os.replace(partial, montage)
            finally:
                partial.unlink(missing_ok=True)
            _freeze(montage)
            probed = _probe_pcm_wav(montage)
    except BaseException:
        shutil.rmtree(temp, ignore_errors=True)
        raise
    duration, rate, channels = probed
    return PreparedRecording(
        digest=digest,
        temp_dir=temp,
        original_name=original.name,
        montage_name=montage.name,
        duration_sec=duration,
        sample_rate=rate,
        channels=channels,
        part_name=_nfc(resolved.name),
    )


def commit_prepared_recording(
    paths: Mapping[str, Path], prepared: PreparedRecording
) -> RecordingSource:
    """Atomic registration: rename the prepared directory and update the map."""

    root = Path(paths["root"]).resolve()
    with map_lock(paths):
        data = load_recorded_voiceover(paths)
        for existing in data["recordings"]:
            if existing["sha256"] == prepared.digest:
                discard_prepared_recording(prepared)
                return RecordingSource.from_dict(existing)
        store = Path(paths["voiceover_recordings"]) / prepared.digest
        if store.exists():  # leftover of an interrupted registration (not in the map)
            shutil.rmtree(store)
        os.replace(prepared.temp_dir, store)
        try:
            recording = RecordingSource(
                id=f"rec_{prepared.digest[:16]}",
                asset=(store.resolve() / prepared.montage_name).relative_to(root).as_posix(),
                sha256=prepared.digest,
                duration_sec=prepared.duration_sec,
                sample_rate=prepared.sample_rate,
                channels=prepared.channels,
                part_index=0,
                part_name=prepared.part_name,
                original_asset=(store.resolve() / prepared.original_name)
                .relative_to(root)
                .as_posix(),
            )
            data["recordings"] = _reindex([*data["recordings"], recording.to_dict()])
            saved = save_recorded_voiceover(paths, data)
        except BaseException:
            shutil.rmtree(store, ignore_errors=True)
            raise
    return next(
        RecordingSource.from_dict(item)
        for item in saved["recordings"]
        if item["id"] == recording.id
    )


def register_recording_file(
    paths: Mapping[str, Path],
    source: Path,
    *,
    converter: Converter | None = None,
) -> RecordingSource:
    """Import ``source`` into the immutable store and register it once (by content hash)."""

    prepared = prepare_recording_file(paths, source, converter=converter)
    if isinstance(prepared, RecordingSource):
        return prepared
    try:
        return commit_prepared_recording(paths, prepared)
    finally:
        discard_prepared_recording(prepared)  # no-op once renamed into the store


def prepare_voiceover_scan(
    paths: Mapping[str, Path],
    *,
    converter: Converter | None = None,
) -> list[PreparedRecording]:
    """Find new files in ``ЗАГРУЗИТЬ/Закадр/`` and prepare them (outside any project lock).

    Content hashes come from the inbox hash cache, so only new or changed files are
    read in full. Unreadable files are skipped.
    """

    root = Path(paths["root"])
    known = {item["sha256"] for item in load_recorded_voiceover(paths)["recordings"]}
    prepared: list[PreparedRecording] = []
    for item in discover_project_inbox(root)["voiceover_files"]:
        digest = str(item["fingerprint"]).removeprefix("sha256:")
        if digest in known:
            continue
        try:
            result = prepare_recording_file(
                paths, root / item["relative_path"], converter=converter, digest=digest
            )
        except (OSError, ValueError, RuntimeError, wave.Error, EOFError):
            continue
        known.add(digest)
        if isinstance(result, PreparedRecording):
            prepared.append(result)
    return prepared


def commit_prepared_scan(
    paths: Mapping[str, Path], prepared: list[PreparedRecording]
) -> list[RecordingSource]:
    """Register prepared recordings (short critical section); returns the new ones."""

    registered: list[RecordingSource] = []
    for item in prepared:
        try:
            registered.append(commit_prepared_recording(paths, item))
        except (OSError, ValueError, RuntimeError, wave.Error, EOFError):
            continue
        finally:
            discard_prepared_recording(item)
    return registered


def scan_recorded_voiceover(
    paths: Mapping[str, Path],
    *,
    converter: Converter | None = None,
) -> list[RecordingSource]:
    """Register new files from ``ЗАГРУЗИТЬ/Закадр/`` (as found by the inbox) once.

    Returns only the newly registered recordings. Parts are ordered by file name.
    An empty or missing folder registers nothing (voiceover stays on TTS). ASR is
    not started here.
    """

    prepared = prepare_voiceover_scan(paths, converter=converter)
    return commit_prepared_scan(paths, prepared)
