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


TIMELINE_VOICEOVER_CHANGED_ERROR = (
    "timeline_rebuild_required: опубликованный закадр изменился после сборки таймлайна"
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


def unit_text_sha256(text: object) -> str:
    """Hash of a unit text (whitespace-normalized): identifies what was confirmed by hand."""
    return hashlib.sha256(" ".join(str(text or "").split()).encode("utf-8")).hexdigest()


def life_text_sha256(text: object) -> str:
    """LIFE alias of ``unit_text_sha256`` (``metadata.life_text_sha256`` stays readable)."""
    return unit_text_sha256(text)


def is_life_match(match: Any) -> bool:
    return str((match.metadata or {}).get("block_type") or "") == "life"


def normalization_unit_texts(
    normalization: dict,
    preserve_whole_uids: set[str] | None = None,
    *,
    types: frozenset[str] = frozenset({"sync", "host", "life"}),
) -> dict[str, str]:
    """``{unit id: text}`` of the sync/host/LIFE units of the normalization.

    Built from the same fragments as the sync alignment units
    (``api._sync_alignment_units``), so the hash stored at PATCH time and the one
    compared at read time come from one source.  ``preserve_whole_uids`` are blocks kept
    as one unit because they carry a confirmed match.
    """
    from stage2.sync_quote import _sync_quote, life_unit_fragments
    from stage2.timeline_parser import (
        host_standup_remark,
        is_host_standup,
        is_label_only_sync,
        normalized_sync_fragments,
        sync_fragment_id,
    )

    preserve = preserve_whole_uids or set()
    texts: dict[str, str] = {}
    for block in normalization.get("blocks") or []:
        if not isinstance(block, dict):
            continue
        block_type = str(block.get("type") or "").lower()
        uid = str(block.get("uid") or "").strip()
        if block_type not in types or not uid:
            continue
        if is_host_standup(block):
            texts[uid] = host_standup_remark(block)
            continue
        if block_type == "sync":
            if is_label_only_sync(block):
                continue
            fragments = normalized_sync_fragments(
                block
                if block.get("sync_fragments")
                else {**block, "normalized_text": _sync_quote(block)},
                preserve_whole=uid in preserve,
            )
        else:
            fragments = life_unit_fragments(block, life_only=block_type == "life")
        for index, fragment in enumerate(fragments):
            texts[sync_fragment_id(uid, index, len(fragments))] = str(fragment["text"])
    return texts


def life_unit_texts(normalization: dict) -> dict[str, str]:
    """``{unit id: text}`` of every LIFE unit of the normalization."""
    return normalization_unit_texts(normalization, types=frozenset({"life"}))


def match_is_current_revision(match: Any, normalization: dict) -> bool:
    """True when the match was stored for this normalization/scenario revision (or is legacy)."""
    metadata = match.metadata or {}
    return metadata.get("normalization_revision") in (
        None,
        "",
        normalization.get("revision"),
    ) and _scenario_revision_matches(
        metadata.get("scenario_revision_id"), normalization.get("scenario_revision_id")
    )


def _text_key(text: object) -> str:
    return " ".join(str(text or "").split()).casefold()


def _confirmation_matches_unit(match: Any, text: str, source_ids: set[str] | None) -> bool:
    """A manual confirmation of an older revision still describes this unit text."""
    if source_ids is not None and str(getattr(match, "source_id", "") or "") not in source_ids:
        return False
    metadata = match.metadata or {}
    stored_hash = metadata.get("unit_text_sha256") or metadata.get("life_text_sha256")
    if stored_hash:
        return stored_hash == unit_text_sha256(text)
    if is_life_match(match):
        return False  # a LIFE text is never guessed: only the stored hash proves it
    # Legacy confirmation without a hash: the text it was matched against.
    wanted = _text_key(text)
    return wanted in {
        _text_key(getattr(match, "query_text", "")),
        _text_key(metadata.get("query_text")),
    } and bool(wanted)


def classify_sync_matches(
    matches: list, normalization: dict, source_ids: set[str] | None = None
) -> tuple[list, list[str], list[str]]:
    """Split stored matches into current ones, stale ids and stale confirmation ids.

    The single place that decides whether a match belongs to the current normalization.
    A manual confirmation (sync/host/LIFE) of an older revision survives when its unit
    still exists, its text is unchanged (``unit_text_sha256``; legacy ones without a hash
    are compared by ``query_text``; LIFE needs the hash) and its source exists (checked
    when ``source_ids`` is given).  A changed text makes it stale and reports the id in
    the third list so the UI can say why the confirmation disappeared.
    """
    texts: dict[str, str] | None = None
    block_uids: set[str] = set()
    older_confirmed = {
        match.segment_id
        for match in matches
        if match.status == "confirmed" and not match_is_current_revision(match, normalization)
    }
    current: list = []
    stale_ids: list[str] = []
    stale_confirmed: list[str] = []
    for match in matches:
        if match_is_current_revision(match, normalization):
            current.append(match)
            continue
        if match.status == "confirmed" or is_life_match(match):
            if texts is None:
                texts = normalization_unit_texts(normalization, older_confirmed)
                block_uids = {
                    str(block.get("uid") or "").strip()
                    for block in normalization.get("blocks") or []
                    if isinstance(block, dict)
                }
            text = texts.get(match.segment_id)
            if text is not None and _confirmation_matches_unit(match, text, source_ids):
                current.append(match)
                continue
            parent = (
                str((match.metadata or {}).get("parent_block_uid") or "")
                or str(match.segment_id).split("::fragment:")[0]
            )
            if text is not None or parent in block_uids:
                stale_confirmed.append(match.segment_id)
        stale_ids.append(match.segment_id)
    return current, stale_ids, stale_confirmed


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


def _timeline_voiceover_is_current(timeline: dict, normalization: dict, paths: Any) -> bool:
    """False when the published recorded-voiceover map changed after the timeline was built."""
    from voiceover.readiness import timeline_voiceover_is_current

    return timeline_voiceover_is_current(timeline, paths, normalization)


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

    Reasons: ``missing_wav``, ``missing_voiceover`` (no recording and no TTS), ``no_speaker``,
    ``no_file``, ``asr_failed``, ``not_found``, ``needs_review``,
    ``unmapped_source`` (only when ``mapped`` is given), ``life_unassigned`` and
    ``host_standup`` (a text-less ``ВЕДУЩИЙ № N`` stub without an assigned take).  ``tech``
    segments never produce a reason.
    """
    segment_type = segment.get("type")
    if segment_type == "zk":
        try:
            duration = _as_float(segment.get("duration_sec") or 0)
        except (TypeError, ValueError):
            duration = 0.0
        kind = str(segment.get("voiceover_kind") or "tts")
        if kind == "none":
            return "missing_voiceover"
        if kind == "recorded":
            # Recorded: a range of the shared recording. File existence and the range
            # against the recording duration are checked by validate_plan_complete.
            try:
                has_range = _as_float(segment.get("source_out_sec") or 0) > _as_float(
                    segment.get("source_in_sec") or 0
                ) >= 0
            except (TypeError, ValueError):
                has_range = False
            return (
                None
                if segment.get("wav_path") and has_range and duration > 0
                else "missing_voiceover"
            )
        return None if segment.get("wav_path") and duration > 0 else "missing_wav"
    if segment_type not in {"snh", "host", "life"}:
        return None

    status = str(segment.get("match_status") or "")
    standup = segment_type == "host" and bool(str(segment.get("host_standup") or "").strip())
    if segment_type == "life" or standup:
        # LIFE and a host stand-up stub are assigned by hand only: a confirmed match with a
        # range, or the placeholder.
        source_id = str(segment.get("source_id") or "")
        if status == "confirmed" and is_exportable_match(
            segment_type,
            status,
            source_id,
            segment.get("source_in_sec"),
            segment.get("source_out_sec"),
        ):
            return "unmapped_source" if mapped is not None and source_id not in mapped else None
        return "host_standup" if standup else "life_unassigned"
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
