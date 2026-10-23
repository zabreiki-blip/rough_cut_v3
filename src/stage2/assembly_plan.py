"""Deterministic assembly plan: what every timeline segment becomes in the Stage 3 export.

The plan is derived from the timeline only (no clock, no uuid), so the same timeline and
media mapping always produce byte-identical JSON.  ``stage_common.placeholder_reason`` is
the only place that decides why a segment is a placeholder.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from stage_common import placeholder_reason

from .models import FullTimeline

PLAN_SCHEMA_VERSION = 1
_REQUIRED_TYPES = {"zk", "snh", "host", "life"}
_TASK_BY_REASON = {
    "life_unassigned": "assign_life_material",
    "needs_review": "review_match",
    "unmapped_source": "load_source",
    "no_file": "load_source",
}


def _num(value: object) -> float:
    try:
        return float(value or 0.0)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def _timeline_sha256(timeline: dict) -> str:
    canonical = json.dumps(timeline, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _item(index: int, segment: dict, media_mapping: set[str] | None) -> dict[str, Any]:
    seg_type = str(segment.get("type") or "")
    reason = placeholder_reason(segment, mapped=media_mapping)
    duration = max(_num(segment.get("duration_sec")), 0.0)
    start = _num(segment.get("start_sec"))
    if seg_type == "tech" or reason == "missing_wav" or (reason and duration <= 0):
        status = "skipped"
    elif reason:
        status = "placeholder"
    else:
        status = "ready"

    is_sync = seg_type in {"snh", "host"}
    source_id = str(segment.get("source_id") or "")
    if source_id and media_mapping is not None:
        media_status = "mapped" if source_id in media_mapping else "unmapped"
    else:
        media_status = "unchecked"

    source_in = _num(segment.get("source_in_sec"))
    source_out = _num(segment.get("source_out_sec"))
    speech_in = _num(segment.get("speech_in_sec"))
    speech_out = _num(segment.get("speech_out_sec"))
    has_speech = is_sync and speech_out > speech_in
    ranged = is_sync and source_out > source_in
    task = _TASK_BY_REASON.get(reason or "")
    trim = segment.get("trim") if isinstance(segment.get("trim"), dict) else None
    tasks: list[dict[str, Any]] = [{"kind": task, "open": True}] if task else []
    if trim and trim.get("speech_overlap") and not any(t["kind"] == "review_match" for t in tasks):
        tasks.append({"kind": "review_match", "open": True, "reason": "speech_overlap"})
    return {
        "uid": str(segment.get("block_uid") or segment.get("id") or ""),
        "parent_uid": str(segment.get("parent_block_uid") or ""),
        "segment_id": str(segment.get("id") or ""),
        "order": index,
        "type": seg_type,
        "required": seg_type in _REQUIRED_TYPES,
        "status": status,
        "reason": reason,
        "timeline": {
            "in_sec": round(start, 6),
            "out_sec": round(start + duration, 6),
            "duration_sec": round(duration, 6),
        },
        "source": {
            "source_id": source_id,
            "sequence_id": str(segment.get("sequence_id") or ""),
            "sequence_name": str(segment.get("sequence_name") or ""),
            "media_status": media_status,
        },
        "speech_range": {
            "in_sec": round(speech_in, 6) if has_speech else None,
            "out_sec": round(speech_out, 6) if has_speech else None,
        },
        "edit_range": {
            "in_sec": round(source_in, 6) if ranged else None,
            "out_sec": round(source_out, 6) if ranged else None,
        },
        "padding": {
            "head_sec": round(speech_in - source_in, 6) if has_speech and ranged else 0.0,
            "tail_sec": round(source_out - speech_out, 6) if has_speech and ranged else 0.0,
            "trimmed_head_sec": round(_num(trim.get("trimmed_head_sec")), 6) if trim else 0,
            "trimmed_tail_sec": round(_num(trim.get("trimmed_tail_sec")), 6) if trim else 0,
        },
        "wav": {
            "path": str(segment.get("wav_path") or ""),
            "duration_sec": round(duration, 6) if seg_type == "zk" else None,
        },
        "placeholder": {"path": str(segment.get("placeholder_path") or "")},
        "provenance": {
            "match_status": str(segment.get("match_status") or ""),
            "confidence": _num(segment.get("match_confidence")),
            "strategy": str(segment.get("match_strategy") or ""),
            "manual_range": bool(segment.get("manual_range")),
            "merged_from": list(segment.get("merged_from") or []),
            "trim": dict(trim) if trim else None,
            "revision_status": str(segment.get("revision_status") or ""),
        },
        "tasks": tasks,
    }


def build_assembly_plan(
    timeline: dict | FullTimeline, *, media_mapping: set[str] | None = None
) -> dict[str, Any]:
    """Build the assembly plan; ``media_mapping`` is the set of mapped source ids or None."""
    data = timeline.to_dict() if isinstance(timeline, FullTimeline) else timeline
    raw_segments = data.get("segments") if isinstance(data, dict) else None
    segments = [item for item in (raw_segments or []) if isinstance(item, dict)]
    items = [_item(index, segment, media_mapping) for index, segment in enumerate(segments)]

    by_reason: dict[str, dict[str, float]] = {}
    placeholders = 0
    placeholder_duration = 0.0
    for item in items:
        reason = item["reason"]
        duration = item["timeline"]["duration_sec"]
        if item["status"] == "placeholder":
            placeholders += 1
            placeholder_duration += duration
        if item["status"] == "placeholder" or reason == "missing_wav":
            entry = by_reason.setdefault(reason, {"segments": 0, "duration_sec": 0.0})
            entry["segments"] += 1
            entry["duration_sec"] = round(entry["duration_sec"] + duration, 6)
    fingerprint = data.get("sync_matches_fingerprint") if isinstance(data, dict) else None
    policy = data.get("edit_policy_version") if isinstance(data, dict) else None
    return {
        "schema_version": PLAN_SCHEMA_VERSION,
        "timeline_sha256": _timeline_sha256(data if isinstance(data, dict) else {}),
        "sync_matches_fingerprint": fingerprint,
        "edit_policy_version": policy,
        "items": items,
        "summary": {
            "items": len(items),
            "ready": sum(1 for item in items if item["status"] == "ready"),
            "placeholders": {
                "segments": placeholders,
                "duration_sec": round(placeholder_duration, 6),
                "by_reason": {key: by_reason[key] for key in sorted(by_reason)},
            },
            "open_tasks": sum(len(item["tasks"]) for item in items),
        },
    }


def plan_to_report(plan: dict) -> dict[str, Any]:
    """Placeholder report in the shape of the Stage 3 ``_placeholder_report`` response."""
    summary = plan["summary"]
    placeholders = summary["placeholders"]
    by_reason = placeholders["by_reason"]
    return {
        "placeholder_segments": placeholders["segments"],
        "placeholder_duration_sec": placeholders["duration_sec"],
        "placeholder_breakdown": {key: dict(value) for key, value in by_reason.items()},
        "unmapped_source_placeholders": int(
            by_reason.get("unmapped_source", {}).get("segments", 0)
        ),
        "plan_summary": summary,
    }
