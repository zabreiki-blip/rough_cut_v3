"""Immutable lineage snapshot for a rendered final-review WAV."""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import re
from typing import Any
import uuid

from .models import FullTimeline, TimelineSegment

ASSEMBLY_MANIFEST_SCHEMA_VERSION = 1
_MATCHED_STATUSES = {"auto_matched", "confirmed"}
_PREMIERE_SOURCE_PREFIX = "RC_SRC_"


def _normalized_source_name(value: object) -> str:
    name = str(value or "").strip()
    if name.upper().startswith(_PREMIERE_SOURCE_PREFIX):
        name = name[len(_PREMIERE_SOURCE_PREFIX) :].strip()
    return re.sub(r"[_\s]+", " ", name).strip().casefold()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _audio_clips(layout: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for track_index, track in enumerate(layout.get("audio") or []):
        if not isinstance(track, dict):
            continue
        for clip in track.get("clips") or []:
            if not isinstance(clip, dict):
                continue
            item = dict(clip)
            item["track"] = str(track.get("track") or f"A{track_index + 1}")
            item["track_index"] = track_index
            item["track_muted"] = track.get("muted")
            result.append(item)
    return result


def _overlap(segment: TimelineSegment, clip: dict[str, Any]) -> float:
    start = float(clip.get("startSeconds") or 0.0)
    end = float(clip.get("endSeconds") or 0.0)
    return max(0.0, min(segment.end_sec, end) - max(segment.start_sec, start))


def _block_snapshot(segment: TimelineSegment, clips: list[dict[str, Any]]) -> dict[str, Any]:
    overlaps = sorted(
        ((clip, _overlap(segment, clip)) for clip in clips),
        key=lambda item: item[1],
        reverse=True,
    )
    positive = [item for item in overlaps if item[1] > 0]
    expected_name = _normalized_source_name(segment.sequence_name)
    named = [
        item
        for item in positive
        if expected_name
        and expected_name
        in {
            _normalized_source_name(item[0].get("name")),
            _normalized_source_name(item[0].get("projectItemName")),
        }
    ]
    chosen = named[0] if named else (positive[0] if positive else None)
    actual = dict(chosen[0]) if chosen else None
    overlap_seconds = chosen[1] if chosen else 0.0
    eligible = segment.type == "zk" or (
        segment.type in {"snh", "host"} and segment.match_status in _MATCHED_STATUSES
    )
    return {
        "segment_id": segment.id,
        "block_uid": segment.block_uid,
        "type": segment.type,
        "eligible_for_review": eligible,
        "match_status": segment.match_status,
        "timeline_in_sec": segment.start_sec,
        "timeline_out_sec": segment.end_sec,
        "duration_sec": segment.duration_sec,
        "expected_source": {
            "source_id": segment.source_id,
            "sequence_id": segment.sequence_id,
            "sequence_name": segment.sequence_name,
            "source_in_sec": segment.source_in_sec,
            "source_out_sec": segment.source_out_sec,
        },
        "actual_audio_clip": actual,
        "actual_overlap_sec": round(overlap_seconds, 6),
        "overlapping_audio_clips": len(positive),
    }


def _manifest_findings(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []

    def add(block: dict[str, Any], kind: str, detail: str) -> None:
        findings.append(
            {
                "segment_id": block["segment_id"],
                "block_uid": block["block_uid"],
                "kind": kind,
                "detail": detail,
            }
        )

    for block in blocks:
        if not block["eligible_for_review"]:
            continue
        clip = block.get("actual_audio_clip")
        if not isinstance(clip, dict):
            add(block, "missing_from_sequence", "no audio clip overlaps the expected range")
            continue
        if clip.get("track_muted") is True:
            add(block, "muted_audio", f"{clip.get('track') or 'audio track'} is muted")
        if clip.get("enabled") is False:
            add(block, "disabled_audio", "the overlapping audio clip is disabled")
        if clip.get("hasAudioComponent") is False:
            add(block, "missing_audio_component", "the overlapping item has no audio component")
        expected_duration = float(block.get("duration_sec") or 0.0)
        actual_duration = float(clip.get("durationSeconds") or 0.0)
        if abs(expected_duration - actual_duration) > 0.08:
            add(
                block,
                "duration_mismatch",
                f"expected {expected_duration:.3f}s, found {actual_duration:.3f}s",
            )
        expected = dict(block.get("expected_source") or {})
        expected_display_name = str(expected.get("sequence_name") or "").strip()
        expected_name = _normalized_source_name(expected_display_name)
        actual_names = {
            _normalized_source_name(clip.get("name")),
            _normalized_source_name(clip.get("projectItemName")),
        }
        if expected_name and expected_name not in actual_names:
            add(block, "wrong_source", f"expected source sequence {expected_display_name}")
        if expected_name:
            source_in = float(clip.get("sourceInSeconds") or 0.0)
            source_out = float(clip.get("sourceOutSeconds") or 0.0)
            expected_in = float(expected.get("source_in_sec") or 0.0)
            expected_out = float(expected.get("source_out_sec") or 0.0)
            if abs(source_in - expected_in) > 0.08 or abs(source_out - expected_out) > 0.08:
                add(
                    block,
                    "wrong_source_range",
                    f"expected {expected_in:.3f}–{expected_out:.3f}, "
                    f"found {source_in:.3f}–{source_out:.3f}",
                )
    return findings


def build_assembly_manifest(
    *,
    project: str,
    timeline: FullTimeline,
    timeline_path: Path,
    scenario_path: Path,
    audio_path: Path,
    source_sequence: dict[str, Any],
    layout: dict[str, Any],
) -> dict[str, Any]:
    sequence_id = str(source_sequence.get("sequenceId") or "").strip()
    layout_sequence = dict(layout.get("sequence") or {})
    layout_sequence_id = str(layout_sequence.get("sequenceId") or "").strip()
    if not sequence_id:
        raise ValueError("active Premiere sequence ID is required for final review")
    if layout_sequence_id != sequence_id:
        raise ValueError("assembly layout does not belong to the inspected Premiere sequence")
    if str(source_sequence.get("name") or "") != str(layout_sequence.get("name") or ""):
        raise ValueError("assembly layout sequence name changed during final review snapshot")
    if not scenario_path.is_file():
        raise ValueError("scenario_normalized.json not found")
    clips = _audio_clips(layout)
    blocks = [_block_snapshot(segment, clips) for segment in timeline.segments]
    findings = _manifest_findings(blocks)
    return {
        "schema_version": ASSEMBLY_MANIFEST_SCHEMA_VERSION,
        "manifest_id": uuid.uuid4().hex,
        "created_at": datetime.now(UTC).isoformat(),
        "project": project,
        "scenario_revision_id": "",
        "scenario_sha256": sha256_file(scenario_path),
        "timeline_sha256": sha256_file(timeline_path),
        "wav": {
            "path": str(audio_path),
            "size": audio_path.stat().st_size,
            "sha256": sha256_file(audio_path),
        },
        "sequence": layout_sequence,
        "summary": {
            "blocks": len(blocks),
            "eligible_blocks": sum(bool(block["eligible_for_review"]) for block in blocks),
            "findings": len(findings),
        },
        "blocks": blocks,
        "findings": findings,
    }


def attach_scenario_revision(manifest: dict[str, Any], scenario_path: Path) -> None:
    try:
        value = json.loads(scenario_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        value = {}
    manifest["scenario_revision_id"] = str(value.get("scenario_revision_id") or "")


def validate_assembly_manifest(
    manifest: dict[str, Any], *, timeline_path: Path, scenario_path: Path, audio_path: Path
) -> None:
    if int(manifest.get("schema_version") or 0) != ASSEMBLY_MANIFEST_SCHEMA_VERSION:
        raise ValueError("unsupported assembly manifest schema")
    expected_wav = dict(manifest.get("wav") or {})
    checks = (
        ("scenario", str(manifest.get("scenario_sha256") or ""), scenario_path),
        ("timeline", str(manifest.get("timeline_sha256") or ""), timeline_path),
        ("WAV", str(expected_wav.get("sha256") or ""), audio_path),
    )
    for label, expected_hash, path in checks:
        if not path.is_file() or not expected_hash or sha256_file(path) != expected_hash:
            raise ValueError(f"stale_snapshot: {label} changed after assembly snapshot")
    if audio_path.stat().st_size != int(expected_wav.get("size") or -1):
        raise ValueError("stale_snapshot: WAV size changed after assembly snapshot")
