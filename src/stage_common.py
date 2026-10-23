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


def life_text_sha256(text: object) -> str:
    """Hash of a LIFE unit text (whitespace-normalized): identifies what was assigned."""
    return hashlib.sha256(" ".join(str(text or "").split()).encode("utf-8")).hexdigest()


def is_life_match(match: Any) -> bool:
    return str((match.metadata or {}).get("block_type") or "") == "life"


def life_unit_texts(normalization: dict) -> dict[str, str]:
    """``{unit id: text}`` of every LIFE unit of the normalization.

    Built from the same fragments as the sync alignment units, so the hash stored at
    PATCH time and the one compared at read time come from one source.
    """
    from stage2.sync_quote import life_unit_fragments
    from stage2.timeline_parser import sync_fragment_id

    texts: dict[str, str] = {}
    for block in normalization.get("blocks") or []:
        if not isinstance(block, dict) or str(block.get("type") or "").lower() != "life":
            continue
        uid = str(block.get("uid") or "").strip()
        if not uid:
            continue
        fragments = life_unit_fragments(block)
        for index, fragment in enumerate(fragments):
            texts[sync_fragment_id(uid, index, len(fragments))] = str(fragment["text"])
    return texts


def classify_sync_matches(
    matches: list, normalization: dict
) -> tuple[list, list[str], list[str]]:
    """Split stored matches into current ones, stale ids and stale LIFE assignment ids.

    The single place that decides whether a match belongs to the current normalization.
    A manual LIFE assignment survives a revision change when its unit still exists and
    the LIFE text is unchanged (``life_text_sha256``); a changed text makes it stale and
    reports the uid in the third list so the UI can say why the assignment disappeared.
    """
    revision = normalization.get("revision")
    scenario_revision_id = normalization.get("scenario_revision_id")
    life_texts: dict[str, str] | None = None
    current: list = []
    stale_ids: list[str] = []
    stale_life_ids: list[str] = []
    for match in matches:
        metadata = match.metadata
        if metadata.get("normalization_revision") in (
            None,
            "",
            revision,
        ) and _scenario_revision_matches(metadata.get("scenario_revision_id"), scenario_revision_id):
            current.append(match)
            continue
        if is_life_match(match):
            if life_texts is None:
                life_texts = life_unit_texts(normalization)
            text = life_texts.get(match.segment_id)
            if text is not None:
                if metadata.get("life_text_sha256") == life_text_sha256(text):
                    current.append(match)
                    continue
                stale_life_ids.append(match.segment_id)
        stale_ids.append(match.segment_id)
    return current, stale_ids, stale_life_ids


def current_sync_matches(matches: list, normalization: dict) -> tuple[list, list[str]]:
    """Split stored sync matches into those valid for the normalization and stale ids."""
    current, stale_ids, _stale_life = classify_sync_matches(matches, normalization)
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
    drift: an SNH/host/LIFE fragment with a matched status (legacy empty status included),
    a source id and a non-empty source range.
    """
    try:
        has_range = _as_float(source_out_sec or 0) > _as_float(source_in_sec or 0)
    except (TypeError, ValueError):
        has_range = False
    return bool(
        segment_type in {"snh", "host", "life"}
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


def _has_speaker(segment: dict) -> bool:
    """True when the segment names a speaker that can be matched to a source file."""
    # Imported lazily: stage2 modules import stage_common.
    from stage2.sync_matcher import speaker_name_tokens

    speaker = str(segment.get("speaker") or "").strip()
    # The timeline parser falls back to the segment prefix when the block has no speaker.
    if not speaker or speaker.lower() in {"snh", "host"}:
        return False
    return bool(speaker_name_tokens(speaker))


def placeholder_reason(segment: dict, *, mapped: set[str] | None = None) -> str | None:
    """Single classifier: why a timeline segment is not (fully) assembled, or None if ready.

    Reasons: ``missing_wav``, ``no_speaker``, ``no_file``, ``asr_failed``, ``not_found``,
    ``needs_review``, ``unmapped_source`` (only when ``mapped`` is given) and
    ``life_unassigned``.  ``tech`` segments never produce a reason.
    """
    segment_type = segment.get("type")
    if segment_type == "zk":
        try:
            duration = _as_float(segment.get("duration_sec") or 0)
        except (TypeError, ValueError):
            duration = 0.0
        return None if segment.get("wav_path") and duration > 0 else "missing_wav"
    if segment_type not in {"snh", "host", "life"}:
        return None

    status = str(segment.get("match_status") or "")
    if segment_type == "life":
        # LIFE is assigned by hand only: a confirmed match with a range, or the placeholder.
        source_id = str(segment.get("source_id") or "")
        if status == "confirmed" and is_exportable_match(
            segment_type,
            status,
            source_id,
            segment.get("source_in_sec"),
            segment.get("source_out_sec"),
        ):
            return "unmapped_source" if mapped is not None and source_id not in mapped else None
        return "life_unassigned"
    if status == "needs_review" or (status == "confirmed" and segment.get("retime_pending_review")):
        return "needs_review"
    source_id = str(segment.get("source_id") or "")
    if is_exportable_match(
        segment_type,
        status,
        source_id,
        segment.get("source_in_sec"),
        segment.get("source_out_sec"),
    ):
        if mapped is not None and source_id not in mapped:
            return "unmapped_source"
        return None
    unmatched = str(segment.get("unmatched_reason") or "")
    if unmatched == "asr_failed":
        return "asr_failed"
    if unmatched == "no_eligible_source":
        return "no_file" if _has_speaker(segment) else "no_speaker"
    return "not_found"
