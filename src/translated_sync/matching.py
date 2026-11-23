from __future__ import annotations

from collections import Counter
from difflib import SequenceMatcher
from typing import Sequence

from stage2.sync_matcher import normalize_text
from stage2.sync_models import AlignmentRequest, SyncAlternative, SyncMatch, SyncSource

from .transcript import TranscriptCue


def _time_seconds(value: str | float | None, fps: float = 25.0) -> float | None:
    if value is None:
        return None
    try:
        if isinstance(value, (float, int)):
            return float(value)
        parts = [float(part) for part in str(value).split(":")]
        if len(parts) == 3:
            if parts[1] >= 60 or parts[2] >= 60:
                return None
            return parts[0] * 3600 + parts[1] * 60 + parts[2]
        if len(parts) == 4:
            if parts[1] >= 60 or parts[2] >= 60 or parts[3] >= fps:
                return None
            return parts[0] * 3600 + parts[1] * 60 + parts[2] + parts[3] / fps
        if len(parts) == 2:
            return parts[0] * 60 + parts[1]
        return float(parts[0])
    except (ValueError, TypeError):
        return None


def _text_score(query: list[str], candidate: list[str]) -> float:
    if not query or not candidate:
        return 0.0
    best = SequenceMatcher(None, query, candidate, autojunk=False).ratio()
    q_counts = Counter(query)
    c_counts = Counter(candidate)
    common = sum((q_counts & c_counts).values())
    required_overlap = 1 if len(query) == 1 else max(2, (len(query) + 1) // 2)
    if common < required_overlap:
        return 0.0
    recall = common / len(query)
    precision = common / len(candidate)
    overlap = 2 * recall * precision / (recall + precision) if recall + precision else 0.0
    best = max(best, overlap)
    for width in range(max(1, len(query) - 2), min(len(candidate), len(query) + 3) + 1):
        for start in range(len(candidate) - width + 1):
            best = max(
                best,
                SequenceMatcher(
                    None, query, candidate[start : start + width], autojunk=False
                ).ratio(),
            )
    return best


def match_translated_quote(
    request: AlignmentRequest,
    cues: Sequence[TranscriptCue],
    sources: Sequence[SyncSource],
) -> SyncMatch:
    allowed = set(request.source_ids) if request.source_ids else {source.id for source in sources}
    source_by_id = {source.id: source for source in sources}
    query_tokens = normalize_text(request.text)
    ranked: list[tuple[float, TranscriptCue, float]] = []

    for cue in cues:
        source = source_by_id.get(cue.source_id)
        if source is None or cue.source_id not in allowed:
            continue
        requested_time = _time_seconds(request.approximate_timecode, source.fps)
        if requested_time is not None and request.timecode_mode == "display":
            source_start = _time_seconds(source.sequence_start_tc, source.fps) or 0.0
            requested_time = max(0.0, requested_time - source_start)
        elif requested_time is not None:
            requested_time += source.guide_offset_sec
        text_score = _text_score(query_tokens, normalize_text(cue.russian_text))
        if text_score <= 0:
            continue
        score = text_score
        if request.speaker and cue.speaker:
            expected = request.speaker.casefold()
            actual = cue.speaker.casefold()
            if expected in actual or actual in expected:
                score += 0.12
            else:
                score -= 0.15
        if request.source_hint:
            hint = request.source_hint.casefold()
            source_names = (
                source.premiere_sequence,
                source.id,
                source.sequence_id,
                source.media_path,
            )
            if any(
                hint == name.casefold()
                or (len(hint) >= 3 and hint in name.casefold())
                or (len(name) >= 3 and name.casefold() in hint)
                for name in source_names
                if name
            ):
                score += 0.12
        if requested_time is not None:
            distance = abs(cue.start_sec - requested_time)
            score += max(0.0, 0.12 * (1 - distance / 60.0))
        ranked.append((max(0.0, score), cue, text_score))

    ranked.sort(key=lambda item: (-item[0], item[1].start_sec, item[1].origin_index))
    plausible = [item for item in ranked if item[2] >= 0.42]
    if not plausible:
        return SyncMatch(
            request.segment_id,
            request.text,
            speaker=request.speaker,
            status="not_found",
            strategy="translated_docx",
            metadata={"reason": "No Russian transcript cue overlapped the quote"},
        )

    score, cue, _ = plausible[0]
    alternatives = [
        SyncAlternative(
            item.source_id,
            item.start_sec,
            item.end_sec,
            min(1.0, candidate_score),
            item.russian_text,
            item.id,
        )
        for candidate_score, item, _text_score_value in plausible[1:6]
    ]
    return SyncMatch(
        request.segment_id,
        request.text,
        speaker=request.speaker,
        source_id=cue.source_id,
        source_in_sec=cue.start_sec,
        source_out_sec=cue.end_sec,
        confidence=min(1.0, score),
        status="needs_review",
        matched_text=cue.russian_text,
        strategy="translated_docx",
        alternatives=alternatives,
        metadata={
            "document_cue_id": cue.id,
            "origin_index": cue.origin_index,
            "approximate": cue.approximate,
            "timing_precision": "coarse_window",
            "original_text": cue.original_text,
            "cue_speaker": cue.speaker,
            "raw_start_tc": cue.raw_start_tc,
            "raw_end_tc": cue.raw_end_tc,
            "explicit_end": cue.explicit_end,
        },
    )
