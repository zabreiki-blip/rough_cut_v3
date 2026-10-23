"""Single voiceover readiness decision shared by timeline build, status, validate and export.

A voiceover block is ready when it has a usable recording range OR a TTS WAV.  The published
map (``work/recorded_voiceover_published.json``) names the per-block decision; this module
resolves it against what is actually on disk:

  recorded  the recording file exists, the block text still matches the published hash and
            ``0 <= in < out <= recording duration``
  tts       the block's own ``zkN.wav`` (legacy projects without a map are always ``tts``)
  none      neither (a manual "no voiceover" choice, or a failed recording without TTS)

Nothing here reads recording content: checks are by existence, size-free metadata and the
published hashes, so a status request stays cheap.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
from pathlib import Path
from typing import Any, Mapping
import unicodedata

from project_service import read_json_safe
from stage4.revision_assets import (
    published_map_path,
    recorded_map_fingerprint,
    safe_project_asset,
)
from stage_common import _scenario_revision_matches

RANGE_EPS = 1e-6


def spoken_text_sha256(text: str) -> str:
    """Hash of the spoken block text; identical to the hash stored in the published map."""
    value = " ".join(unicodedata.normalize("NFC", text).split())
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def active_published_map(published: object, normalization: Mapping[str, Any]) -> dict[str, Any]:
    """The published map when it belongs to the active scenario revision, else ``{}``."""
    if not isinstance(published, dict) or published.get("kind") != "recorded_voiceover":
        return {}
    if not _scenario_revision_matches(
        published.get("scenario_revision_id"), normalization.get("scenario_revision_id")
    ):
        return {}
    return published


def published_fingerprint(published: Mapping[str, Any]) -> str:
    """Fingerprint of an active published map (``""`` when there is none)."""
    return recorded_map_fingerprint(dict(published)) if published else ""


def load_active_published(
    paths: Mapping[str, Path], normalization: Mapping[str, Any]
) -> dict[str, Any]:
    return active_published_map(read_json_safe(published_map_path(dict(paths))), normalization)


def recording_files(paths: Mapping[str, Path], published: Mapping[str, Any]) -> dict[str, Path]:
    """``{recording id: absolute montage WAV path}`` of the published recordings."""
    result: dict[str, Path] = {}
    for item in published.get("recordings") or []:
        if not isinstance(item, dict):
            continue
        asset = safe_project_asset(dict(paths), str(item.get("asset") or ""))
        if asset is not None:
            result[str(item.get("id") or "")] = asset
    return result


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


@dataclass(frozen=True)
class VoiceoverChoice:
    kind: str  # recorded | tts | none
    reason: str = ""
    recording_id: str = ""
    path: Path | None = None
    source_in_sec: float = 0.0
    source_out_sec: float = 0.0
    speech_in_sec: float = 0.0
    speech_out_sec: float = 0.0
    audio_source_duration_sec: float = 0.0
    audio_source_sha256: str = ""


def resolve_voiceover(
    published: Mapping[str, Any],
    block_uid: str,
    spoken_sha256: str,
    *,
    files: Mapping[str, Path],
    has_tts: bool,
) -> VoiceoverChoice:
    """Effective voiceover of one block; ``published`` must be an active map or ``{}``."""
    if not published:
        return VoiceoverChoice("tts", "no_map")
    fallback = "tts" if has_tts else "none"
    entry = next(
        (
            item
            for item in published.get("blocks") or []
            if isinstance(item, dict) and str(item.get("block_uid") or "") == block_uid
        ),
        None,
    )
    if entry is None:
        return VoiceoverChoice(fallback, "not_published")
    if entry.get("text_sha256") != spoken_sha256:
        return VoiceoverChoice(fallback, "text_changed")
    source = str(entry.get("voice_source") or "none")
    if source == "none" and str(entry.get("reason") or "") == "manual_none":
        return VoiceoverChoice("none", "manual_none")
    if source != "recorded":
        return VoiceoverChoice(fallback, str(entry.get("reason") or source))
    recording_id = str(entry.get("recording_id") or "")
    recording = next(
        (
            item
            for item in published.get("recordings") or []
            if isinstance(item, dict) and str(item.get("id") or "") == recording_id
        ),
        None,
    )
    path = files.get(recording_id)
    if recording is None or path is None:
        return VoiceoverChoice(fallback, "recording_unknown")
    if not path.is_file():
        return VoiceoverChoice(fallback, "recording_missing")
    duration = _number(recording.get("duration_sec"))
    values = [
        _number(entry.get(key))
        for key in ("source_in_sec", "source_out_sec", "speech_in_sec", "speech_out_sec")
    ]
    if duration is None or any(value is None for value in values):
        return VoiceoverChoice(fallback, "range_invalid")
    source_in, source_out, speech_in, speech_out = (
        float(value) for value in values if value is not None
    )
    if not 0 <= source_in < source_out <= duration + RANGE_EPS:
        return VoiceoverChoice(fallback, "range_invalid")
    return VoiceoverChoice(
        "recorded",
        "confirmed",
        recording_id=recording_id,
        path=path,
        source_in_sec=source_in,
        source_out_sec=source_out,
        speech_in_sec=speech_in,
        speech_out_sec=speech_out,
        audio_source_duration_sec=duration,
        audio_source_sha256=str(recording.get("sha256") or ""),
    )


def timeline_voiceover_is_current(
    timeline: Mapping[str, Any], paths: Mapping[str, Path], normalization: Mapping[str, Any]
) -> bool:
    """False when the published recorded-voiceover map changed after the timeline was built."""
    stored = str(timeline.get("recorded_voiceover_sha256") or "")
    current = published_fingerprint(load_active_published(paths, normalization))
    return stored == current


def voiceover_readiness(
    paths: Mapping[str, Path], normalization: Mapping[str, Any], speaker_dir: Path
) -> dict[str, Any]:
    """Per-block voiceover readiness (recorded OR tts) for status/validate/build/export."""
    blocks = sorted(
        (
            item
            for item in normalization.get("blocks") or []
            if isinstance(item, dict)
            and not item.get("absorbed_by_uid")
            and str(item.get("type", "")).lower() == "voiceover"
        ),
        key=lambda item: int(item.get("order", 0) or 0),
    )
    published = load_active_published(paths, normalization)
    files = recording_files(paths, published) if published else {}
    from stage2.timeline_parser import _spoken_text

    rows: list[dict[str, Any]] = []
    for number, block in enumerate(blocks, start=1):
        segment_id = f"zk{number}"
        display = str(block.get("normalized_text") or block.get("original_text") or "").strip()
        spoken = _spoken_text(str(block.get("text_for_tts") or display), block_type="voiceover")
        has_tts = (speaker_dir / f"{segment_id}.wav").is_file()
        choice = resolve_voiceover(
            published,
            str(block.get("uid") or "").strip(),
            spoken_text_sha256(spoken),
            files=files,
            has_tts=has_tts,
        )
        rows.append(
            {
                "segment_id": segment_id,
                "block_uid": str(block.get("uid") or "").strip(),
                "voice_source": choice.kind,
                "reason": choice.reason,
                "ready": choice.kind in {"recorded", "tts"}
                and (choice.kind == "recorded" or has_tts),
                "has_tts": has_tts,
            }
        )
    return {
        "blocks": rows,
        "total": len(rows),
        "ready": sum(1 for row in rows if row["ready"]),
        "recorded": sum(1 for row in rows if row["voice_source"] == "recorded"),
        "missing": [row["segment_id"] for row in rows if not row["ready"]],
        "published_sha256": published_fingerprint(published),
    }
