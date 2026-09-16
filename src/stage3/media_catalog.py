from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
import subprocess
from typing import Any, Callable

from atomic_io import atomic_write_json


DEFAULT_MEDIA_ROOT = Path("/review-media")
CACHE_VERSION = 1
CACHE_FILENAME = "stage3_ffprobe_cache.json"
VIDEO_EXTENSIONS = frozenset(
    {
        ".3gp",
        ".avi",
        ".m2ts",
        ".m4v",
        ".mkv",
        ".mov",
        ".mp4",
        ".mpeg",
        ".mpg",
        ".mts",
        ".mxf",
        ".webm",
    }
)
AUDIO_EXTENSIONS = frozenset({".mp3", ".wav"})
MEDIA_EXTENSIONS = VIDEO_EXTENSIONS | AUDIO_EXTENSIONS


class MediaCatalogError(ValueError):
    """Raised when a media directory or media probe cannot be used safely."""


@dataclass(frozen=True)
class MediaSource:
    source_id: str
    fingerprint: str
    name: str
    relative_path: str
    container_path: str
    extension: str
    size_bytes: int
    mtime: float
    mtime_ns: int
    duration_sec: float
    fps: float | None
    width: int | None
    height: int | None
    media_kind: str
    has_audio: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def resolve_media_directory(
    directory: str,
    *,
    media_root: Path = DEFAULT_MEDIA_ROOT,
) -> Path:
    raw = str(directory or "").strip()
    if not raw:
        raise MediaCatalogError("directory is required")

    root = media_root.resolve()
    requested = Path(raw)
    if requested.is_absolute() and _is_relative_to(requested, media_root):
        candidate = requested
    else:
        raise MediaCatalogError("directory must be an absolute path inside the media root")

    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise MediaCatalogError("media directory does not exist") from exc
    if not _is_relative_to(resolved, root):
        raise MediaCatalogError("directory resolves outside the configured media root")
    if not resolved.is_dir():
        raise MediaCatalogError("media directory is not a directory")
    return resolved


def _parse_rate(value: object) -> float | None:
    raw = str(value or "").strip()
    if not raw or raw in {"0/0", "N/A"}:
        return None
    try:
        if "/" in raw:
            numerator, denominator = raw.split("/", 1)
            denominator_value = float(denominator)
            return float(numerator) / denominator_value if denominator_value else None
        return float(raw)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def probe_media(
    path: Path,
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, float | int | str | bool | None]:
    try:
        result = run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration:stream=codec_type,avg_frame_rate,r_frame_rate,width,height",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise MediaCatalogError(f"ffprobe failed for {path.name}: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or "").strip()
        suffix = f": {detail[:300]}" if detail else ""
        raise MediaCatalogError(f"ffprobe failed for {path.name}{suffix}")
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise MediaCatalogError(f"ffprobe returned invalid JSON for {path.name}") from exc

    streams = payload.get("streams") if isinstance(payload, dict) else []
    streams = streams if isinstance(streams, list) else []
    video = next(
        (
            stream
            for stream in streams
            if isinstance(stream, dict) and stream.get("codec_type") == "video"
        ),
        {},
    )
    has_audio = any(
        isinstance(stream, dict) and stream.get("codec_type") == "audio" for stream in streams
    )
    format_payload = payload.get("format") if isinstance(payload, dict) else {}
    format_payload = format_payload if isinstance(format_payload, dict) else {}
    try:
        duration = float(format_payload.get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0.0
    fps = _parse_rate(video.get("avg_frame_rate")) or _parse_rate(video.get("r_frame_rate"))
    return {
        "duration_sec": max(0.0, duration),
        "fps": fps,
        "width": int(video["width"]) if video.get("width") is not None else None,
        "height": int(video["height"]) if video.get("height") is not None else None,
        "media_kind": "video" if video else "audio",
        "has_audio": has_audio,
    }


def _source_id(relative_path: str) -> str:
    digest = hashlib.sha256(relative_path.encode("utf-8")).hexdigest()[:20]
    return f"media_{digest}"


def _fingerprint(relative_path: str, size: int, mtime_ns: int) -> str:
    canonical = json.dumps(
        {"relative_path": relative_path, "size_bytes": size, "mtime_ns": mtime_ns},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _metadata_cache_path(media_root: Path) -> Path:
    root = media_root.resolve()
    work = root / "work"
    try:
        work.mkdir(parents=True, exist_ok=True)
        resolved_work = work.resolve(strict=True)
    except OSError as exc:
        raise MediaCatalogError("cannot create the Stage 3 metadata cache directory") from exc
    if not _is_relative_to(resolved_work, root):
        raise MediaCatalogError("Stage 3 metadata cache resolves outside the media root")
    cache_path = resolved_work / CACHE_FILENAME
    if cache_path.exists() and not _is_relative_to(cache_path.resolve(), root):
        raise MediaCatalogError("Stage 3 metadata cache resolves outside the media root")
    return cache_path


def _load_metadata_cache(path: Path) -> dict[str, dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict) or payload.get("version") != CACHE_VERSION:
        return {}
    entries = payload.get("entries")
    if not isinstance(entries, dict):
        return {}
    return {
        str(key): value
        for key, value in entries.items()
        if isinstance(key, str) and isinstance(value, dict)
    }


def _cached_probe_metadata(
    entry: dict[str, Any] | None,
    *,
    size_bytes: int,
    mtime_ns: int,
    extension: str,
) -> dict[str, Any] | None:
    if not entry:
        return None
    if (
        entry.get("size_bytes") != size_bytes
        or entry.get("mtime_ns") != mtime_ns
        or entry.get("extension") != extension
    ):
        return None
    metadata = entry.get("metadata")
    if not isinstance(metadata, dict):
        return None
    required = {"duration_sec", "fps", "width", "height", "media_kind", "has_audio"}
    if not required.issubset(metadata):
        return None
    try:
        normalized = {
            "duration_sec": max(0.0, float(metadata["duration_sec"])),
            "fps": float(metadata["fps"]) if metadata["fps"] is not None else None,
            "width": int(metadata["width"]) if metadata["width"] is not None else None,
            "height": int(metadata["height"]) if metadata["height"] is not None else None,
            "media_kind": str(metadata["media_kind"]),
            "has_audio": metadata["has_audio"],
        }
    except (TypeError, ValueError):
        return None
    if normalized["media_kind"] not in {"video", "audio"}:
        return None
    if not isinstance(normalized["has_audio"], bool):
        return None
    return normalized


def scan_media_directory(
    directory: str,
    *,
    recursive: bool = False,
    media_root: Path = DEFAULT_MEDIA_ROOT,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    root = media_root.resolve()
    resolved = resolve_media_directory(
        directory,
        media_root=media_root,
    )
    cache_path = _metadata_cache_path(root)
    cached_entries = _load_metadata_cache(cache_path)
    # One project cache is shared by both supported review folders. Preserve
    # entries outside this scan; switching folders must not make unchanged
    # media pay for ffprobe again.
    next_entries: dict[str, dict[str, Any]] = dict(cached_entries)
    scanned_paths: set[str] = set()
    iterator = resolved.rglob("*") if recursive else resolved.iterdir()
    sources: list[MediaSource] = []
    for candidate in sorted(iterator, key=lambda item: item.as_posix().casefold()):
        if candidate.suffix.lower() not in MEDIA_EXTENSIONS:
            continue
        try:
            safe_path = candidate.resolve(strict=True)
        except OSError:
            continue
        if not safe_path.is_file() or not _is_relative_to(safe_path, root):
            continue
        stat = safe_path.stat()
        relative_path = safe_path.relative_to(root).as_posix()
        scanned_paths.add(relative_path)
        extension = safe_path.suffix.lower()
        metadata = _cached_probe_metadata(
            cached_entries.get(relative_path),
            size_bytes=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
            extension=extension,
        )
        if metadata is None:
            metadata = probe_media(safe_path, run=run)
        cache_entry = {
            "size_bytes": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "extension": extension,
            "metadata": metadata,
        }
        next_entries[relative_path] = cache_entry
        sources.append(
            MediaSource(
                source_id=_source_id(relative_path),
                fingerprint=_fingerprint(relative_path, stat.st_size, stat.st_mtime_ns),
                name=safe_path.name,
                relative_path=relative_path,
                container_path=str(safe_path),
                extension=extension,
                size_bytes=stat.st_size,
                mtime=stat.st_mtime,
                mtime_ns=stat.st_mtime_ns,
                duration_sec=float(metadata["duration_sec"] or 0),
                fps=float(metadata["fps"]) if metadata["fps"] is not None else None,
                width=int(metadata["width"]) if metadata["width"] is not None else None,
                height=int(metadata["height"]) if metadata["height"] is not None else None,
                media_kind=str(metadata["media_kind"]),
                has_audio=bool(metadata["has_audio"]),
            )
        )
    scan_relative = resolved.relative_to(root).as_posix()
    for cached_path in tuple(next_entries):
        cached_candidate = Path(cached_path)
        in_scan_directory = cached_path == scan_relative or cached_path.startswith(
            f"{scan_relative}/"
        )
        is_direct_child = cached_candidate.parent.as_posix() == scan_relative
        if (
            in_scan_directory
            and (recursive or is_direct_child)
            and cached_path not in scanned_paths
        ):
            del next_entries[cached_path]
    if next_entries != cached_entries:
        atomic_write_json(cache_path, {"version": CACHE_VERSION, "entries": next_entries})
    return {
        "directory": str(resolved),
        "relative_directory": resolved.relative_to(root).as_posix(),
        "recursive": recursive,
        "sources": [source.to_dict() for source in sources],
    }
