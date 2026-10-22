"""Shared helpers for checking scenario/timeline freshness across stages.

Used by both stage2 and stage3 route modules to decide whether a stored
artifact (sync matches, timelines) is still valid against the current
scenario/normalization revision.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, SupportsFloat


TIMELINE_MATCHES_CHANGED_ERROR = (
    "timeline_rebuild_required: сопоставления СНХ изменились после сборки таймлайна"
)


def _scenario_revision_matches(stored: object, current: object) -> bool:
    """Compare scenario identities while accepting artifacts from legacy files.

    An empty ID on either side means that the artifact predates scenario-level
    versioning. Explicit, different IDs are always stale.
    """
    stored_id = str(stored or "").strip()
    current_id = str(current or "").strip()
    return not stored_id or not current_id or stored_id == current_id


def _timeline_is_current(timeline: dict, normalization: dict) -> bool:
    return timeline.get("normalization_revision") == normalization.get(
        "revision"
    ) and _scenario_revision_matches(
        timeline.get("scenario_revision_id"),
        normalization.get("scenario_revision_id"),
    )


def current_sync_matches(matches: list, normalization: dict) -> tuple[list, list[str]]:
    """Split stored sync matches into those valid for the normalization and stale ids."""
    revision = normalization.get("revision")
    scenario_revision_id = normalization.get("scenario_revision_id")
    current: list = []
    stale_ids: list[str] = []
    for match in matches:
        metadata = match.metadata
        match_revision = metadata.get("normalization_revision")
        if match_revision not in (None, "", revision) or not _scenario_revision_matches(
            metadata.get("scenario_revision_id"), scenario_revision_id
        ):
            stale_ids.append(match.segment_id)
            continue
        current.append(match)
    return current, stale_ids


def sync_matches_fingerprint(matches: list) -> str:
    """Fingerprint of the match fields that shape a built timeline.

    Stored in ``full_timeline.json`` at build time. Matches change independently of the
    normalization (a sync/retime pass, a manual PATCH, a ``retime_pending_review`` flag),
    so the revision alone cannot tell that the built timeline is outdated.
    """
    rows = []
    for match in sorted(matches, key=lambda item: item.segment_id):
        metadata = match.metadata
        rows.append(
            [
                match.segment_id,
                match.status,
                match.source_id,
                match.source_in_sec,
                match.source_out_sec,
                match.confidence,
                metadata.get("speech_in_sec"),
                metadata.get("speech_out_sec"),
                bool(metadata.get("retime_pending_review")),
            ]
        )
    return hashlib.sha256(json.dumps(rows, sort_keys=True).encode("utf-8")).hexdigest()


def _timeline_matches_are_current(timeline: dict, normalization: dict, match_store: Any) -> bool:
    """False when sync matches changed after the timeline was built.

    Timelines built before the fingerprint existed carry none and stay accepted.
    """
    stored = timeline.get("sync_matches_fingerprint")
    if not stored:
        return True
    try:
        matches, _stale = current_sync_matches(match_store.list(), normalization)
    except (OSError, ValueError):
        return False
    return stored == sync_matches_fingerprint(matches)


MATCHED_STATUSES = frozenset({"", "auto_matched", "confirmed"})


def _as_float(value: object) -> float:
    """float() restricted to the types float() accepts; anything else raises TypeError."""
    if isinstance(value, (str, bytes, bytearray, SupportsFloat)):
        return float(value)
    raise TypeError(f"unsupported type for float: {type(value).__name__}")


def is_exportable_match(
    segment_type: object,
    match_status: object,
    source_id: object,
    source_in_sec: object,
    source_out_sec: object,
) -> bool:
    """True when the Stage 3 export cuts this fragment from a source (not a placeholder).

    Single predicate shared by the exporter and the pre-export summary so they cannot
    drift: an SNH/host fragment with a matched status (legacy empty status included),
    a source id and a non-empty source range.
    """
    try:
        has_range = _as_float(source_out_sec or 0) > _as_float(source_in_sec or 0)
    except (TypeError, ValueError):
        has_range = False
    return bool(
        segment_type in {"snh", "host"}
        and str(match_status or "") in MATCHED_STATUSES
        and source_id
        and has_range
    )


def timeline_sync_summary(timeline: object) -> dict[str, int]:
    """Count SNH/host fragments of a built timeline by how they will be exported.

    ``needs_review`` and ``not_found`` fragments (and matched ones without a usable
    source range) are exported as placeholders: ``placeholders`` is their sum.
    A legacy empty status with a valid range is exported as a match
    (``matched_unlabeled``).
    """
    counts = {
        "total": 0,
        "auto_matched": 0,
        "confirmed": 0,
        "matched_unlabeled": 0,
        "needs_review": 0,
        "not_found": 0,
        "placeholders": 0,
    }
    segments = timeline.get("segments") if isinstance(timeline, dict) else None
    for segment in segments if isinstance(segments, list) else []:
        if not isinstance(segment, dict) or segment.get("type") not in {"snh", "host"}:
            continue
        counts["total"] += 1
        status = str(segment.get("match_status") or "")
        if is_exportable_match(
            segment.get("type"),
            status,
            segment.get("source_id"),
            segment.get("source_in_sec"),
            segment.get("source_out_sec"),
        ):
            counts[status or "matched_unlabeled"] += 1
        else:
            counts["needs_review" if status == "needs_review" else "not_found"] += 1
            counts["placeholders"] += 1
    return counts
