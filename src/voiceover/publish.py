"""Publication of the recorded-voiceover draft map into the active voiceover.

The draft (``work/recorded_voiceover.json``) holds recognition proposals and user
decisions. Publication copies the *decisions per block* into the published map
(``work/recorded_voiceover_published.json``) that timeline/build read:

  voice_source = recorded  confirmed range of a recording (text unchanged)
               = tts       manual "use TTS", or fallback when the TTS WAV exists
               = none      no confirmed range and no TTS WAV

Publication is per block, so a partial recording is fine. Nothing here writes
``zkN`` aliases: TTS keeps its own manifest, a recording is addressed by range.

The write is journaled (``recorded_voiceover_publish_intent.json``): a failure
restores the previous files, a crash is rolled forward by ``recover_publication``.
"""

from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path
from typing import Any, Mapping

from atomic_io import atomic_write_json
from project_service import read_json_safe
from stage4.revision_assets import (
    recorded_map_fingerprint,
    recorded_snapshot_path,
    safe_project_asset,
)
from voiceover.alignment import _active_voiceover_blocks

SCHEMA_VERSION = 1


def tts_available_uids(paths: Mapping[str, Path]) -> set[str]:
    """Block UIDs that have a TTS WAV in the active voiceover manifest."""

    manifest = read_json_safe(paths["active_voiceover_manifest"])
    result: set[str] = set()
    for item in manifest.get("items") or []:
        if not isinstance(item, dict) or not item.get("block_uid"):
            continue
        asset = safe_project_asset(paths, str(item.get("asset") or ""))
        if asset is not None and asset.is_file():
            result.add(str(item["block_uid"]))
    return result


def effective_partial(item: Mapping[str, Any]) -> bool:
    """Whether the range currently proposed for the block is a partial duplicate."""

    takes = item.get("takes") or []
    selected = item.get("selected_take")
    if isinstance(selected, int) and 0 <= selected < len(takes):
        return bool(takes[selected].get("partial"))
    for take in takes:
        if take.get("source_in_sec") == item.get("source_in_sec") and take.get(
            "source_out_sec"
        ) == item.get("source_out_sec"):
            return bool(take.get("partial"))
    return False


def decide_block(
    item: Mapping[str, Any] | None, block_sha256: str, has_tts: bool
) -> tuple[str, str, str]:
    """Return ``(voice_source, origin, reason)`` for one block."""

    fallback = "tts" if has_tts else "none"
    if item is None:
        return fallback, "auto", "no_range"
    origin = str(item.get("origin") or "auto")
    if item.get("text_sha256") != block_sha256:
        return fallback, "auto", "text_changed"
    wanted = str(item.get("voice_source") or "none")
    if wanted == "tts":
        return fallback, "manual", "manual_tts" if has_tts else "manual_tts_unavailable"
    if wanted == "none" and origin == "manual":
        return "none", "manual", "manual_none"
    if wanted == "recorded" and item.get("status") == "confirmed":
        return "recorded", origin, "confirmed"
    reason = "not_confirmed" if wanted == "recorded" else "no_range"
    return fallback, "auto", reason


def build_published_map(
    draft: Mapping[str, Any],
    normalization: Mapping[str, Any],
    tts_uids: set[str],
    *,
    previous: Mapping[str, Any] | None = None,
    published_at: str = "",
) -> dict[str, Any]:
    ranges = {str(item["block_uid"]): item for item in draft.get("ranges", [])}
    recordings = {str(item["id"]): item for item in draft.get("recordings", [])}
    blocks: list[dict[str, Any]] = []
    used: set[str] = set()
    for block in _active_voiceover_blocks(normalization):
        item = ranges.get(block.uid)
        source, origin, reason = decide_block(item, block.sha256, block.uid in tts_uids)
        entry: dict[str, Any] = {
            "block_uid": block.uid,
            "text_sha256": block.sha256,
            "voice_source": source,
            "origin": origin,
            "reason": reason,
        }
        if source == "recorded" and item is not None:
            if str(item["recording_id"]) not in recordings:
                entry.update(voice_source="tts" if block.uid in tts_uids else "none")
                entry["reason"] = "unknown_recording"
            else:
                used.add(str(item["recording_id"]))
                entry.update(
                    recording_id=item["recording_id"],
                    source_in_sec=item["source_in_sec"],
                    source_out_sec=item["source_out_sec"],
                    speech_in_sec=item["speech_in_sec"],
                    speech_out_sec=item["speech_out_sec"],
                    selected_take=item.get("selected_take"),
                    partial=effective_partial(item),
                    take_count=len(item.get("takes") or []),
                )
        blocks.append(entry)
    previous_generation = (previous or {}).get("published_generation")
    published: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": "recorded_voiceover",
        "scenario_revision_id": str(normalization.get("scenario_revision_id") or ""),
        "draft_generation": int(draft.get("generation", 0)),
        "published_generation": (previous_generation if isinstance(previous_generation, int) else 0)
        + 1,
        "published_at": published_at or datetime.now(UTC).isoformat(),
        "recordings": [recordings[key] for key in sorted(used)],
        "blocks": blocks,
        "summary": {
            "recorded": sum(item["voice_source"] == "recorded" for item in blocks),
            "tts": sum(item["voice_source"] == "tts" for item in blocks),
            "none": sum(item["voice_source"] == "none" for item in blocks),
        },
    }
    published["published_sha256"] = recorded_map_fingerprint(published)
    return published


def load_published(paths: Mapping[str, Path]) -> dict[str, Any]:
    return read_json_safe(paths["recorded_voiceover_published"])


def _read_bytes(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except OSError:
        return None


def _restore_bytes(path: Path, data: bytes | None) -> None:
    if data is None:
        path.unlink(missing_ok=True)
        return
    temporary = path.with_name(f".{path.name}.restore")
    temporary.write_bytes(data)
    temporary.replace(path)


def _write_all(paths: Mapping[str, Path], published: dict[str, Any]) -> None:
    revision_id = str(published.get("scenario_revision_id") or "")
    atomic_write_json(paths["recorded_voiceover_published"], published)
    if not revision_id:
        return
    snapshot = recorded_snapshot_path(dict(paths), revision_id)
    atomic_write_json(snapshot, published)
    revision_path = Path(paths["scenario_versions"]) / f"{revision_id}.json"
    revision = read_json_safe(revision_path)
    if revision:
        revision["recorded_voiceover_snapshot"] = snapshot.name
        atomic_write_json(revision_path, revision)


def publish_recorded_voiceover(
    paths: Mapping[str, Path], published: dict[str, Any]
) -> dict[str, Any]:
    """Write ``published`` (map, revision snapshot, pointer) atomically as a unit."""

    revision_id = str(published.get("scenario_revision_id") or "")
    targets = [Path(paths["recorded_voiceover_published"])]
    if revision_id:
        targets.append(recorded_snapshot_path(dict(paths), revision_id))
        targets.append(Path(paths["scenario_versions"]) / f"{revision_id}.json")
    backup = {path: _read_bytes(path) for path in targets}
    intent = Path(paths["recorded_voiceover_publish_intent"])
    atomic_write_json(intent, {"schema_version": 1, "published": published})
    try:
        _write_all(paths, published)
    except BaseException:
        for path, data in backup.items():
            _restore_bytes(path, data)
        intent.unlink(missing_ok=True)
        raise
    intent.unlink(missing_ok=True)
    return published


def recover_publication(paths: Mapping[str, Path]) -> bool:
    """Finish a publication interrupted by a crash (roll forward). True if one was found."""

    intent = Path(paths["recorded_voiceover_publish_intent"])
    if not intent.is_file():
        return False
    try:
        document = json.loads(intent.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        document = {}
    published = document.get("published") if isinstance(document, dict) else None
    if isinstance(published, dict):
        active = str(read_json_safe(paths["normalized"]).get("scenario_revision_id") or "")
        if str(published.get("scenario_revision_id") or "") == active:
            _write_all(paths, published)
    intent.unlink(missing_ok=True)
    return True
