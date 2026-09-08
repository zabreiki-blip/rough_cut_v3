"""Compare a rendered Rough Cut sequence with its approved spoken timeline.

The reviewer is read-only. It classifies unavailable sources separately,
aligns eligible speech near its expected position, and never proposes a
source edit without enough evidence to identify that source.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import asdict, dataclass
from difflib import SequenceMatcher
import re
from typing import Any

from .models import FullTimeline, TimelineSegment
from .sync_models import WordIndex
from .sync_matcher import normalize_text

_MATCHED_STATUSES = {"auto_matched", "confirmed"}
_SPLICE_RE = re.compile(r"\b(?:СКЛ|СКЛЕЙКА)\b", re.IGNORECASE)
_TIMECODE_RE = re.compile(r"(?<!\d)\d{1,2}[:.]\d{2}(?:[:.]\d{2})?(?!\d)")
_SERVICE_PREFIX_RE = re.compile(r"^\s*(?:(?:СНХ|ЛАЙФ)\s*[,.:–—-]?\s*)+", re.IGNORECASE)
_FAMILY_PREFIX_RE = re.compile(
    r"^\s*(?:отец|мать|мама|папа|брат|сестра|ведущий|ведущая)\s*:\s*",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ReviewFinding:
    segment_id: str
    block_uid: str
    kind: str
    severity: str
    expected_text: str
    scenario_text: str
    actual_text: str
    expected_start_sec: float
    expected_end_sec: float
    actual_start_sec: float | None
    actual_end_sec: float | None
    score: float
    prefix_coverage: float
    suffix_coverage: float
    match_status: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def spoken_text_fragments(segment: TimelineSegment) -> list[str]:
    """Derive ASR text while preserving ``segment.text`` as source of truth."""
    source = segment.spoken_text.strip() or segment.text
    result: list[str] = []
    for raw in _SPLICE_RE.split(source):
        value = _TIMECODE_RE.sub(" ", raw)
        life_cue = bool(re.match(r"^\s*ЛАЙФ\b", value, flags=re.IGNORECASE))
        value = _SERVICE_PREFIX_RE.sub("", value)
        if life_cue:
            value = re.sub(r"^\s*«[^»]{1,80}»\s*", "", value)
        if segment.speaker.strip():
            value = re.sub(
                r"^\s*" + re.escape(segment.speaker.strip()) + r"\s*[,.:–—-]\s*",
                "",
                value,
                flags=re.IGNORECASE,
            )
        if segment.type in {"snh", "host"}:
            value = _FAMILY_PREFIX_RE.sub("", value)
        value = re.sub(r"\s+", " ", value).strip(" ,.:;–—-")
        if value:
            result.append(value)
    return result


def _best_span(
    expected: list[str], actual: list[str], start_at: int, stop_at: int
) -> tuple[int, int, float]:
    """Find the best bounded text window."""
    start_at = max(0, start_at)
    stop_at = min(len(actual), stop_at)
    if not expected or start_at >= stop_at:
        return start_at, start_at, 0.0
    minimum = max(1, len(expected) - max(3, len(expected) // 3))
    maximum = len(expected) + max(5, len(expected) // 2)
    best = (start_at, start_at, 0.0)
    for index in range(start_at, stop_at - minimum + 1):
        for length in range(minimum, min(maximum, stop_at - index) + 1):
            score = SequenceMatcher(
                None, expected, actual[index : index + length], autojunk=False
            ).ratio()
            if score > best[2]:
                best = (index, index + length, score)
    return best


def _global_best_span(expected: list[str], actual: list[str]) -> tuple[int, int, float]:
    """Run a separate global pass around SequenceMatcher anchors."""
    if not expected or not actual:
        return 0, 0, 0.0
    matcher = SequenceMatcher(None, expected, actual, autojunk=False)
    candidates: set[int] = {0}
    for block in matcher.get_matching_blocks():
        if block.size:
            estimate = block.b - block.a
            candidates.update(range(max(0, estimate - 4), min(len(actual), estimate + 5)))
    minimum = max(1, len(expected) - max(3, len(expected) // 3))
    maximum = len(expected) + max(5, len(expected) // 2)
    best = (0, 0, 0.0)
    for start in candidates:
        for length in range(minimum, min(maximum, len(actual) - start) + 1):
            score = SequenceMatcher(
                None, expected, actual[start : start + length], autojunk=False
            ).ratio()
            if score > best[2]:
                best = (start, start + length, score)
    return best


def _coverage(expected: list[str], actual: list[str]) -> tuple[float, float]:
    size = max(1, len(expected) // 4)
    prefix = SequenceMatcher(None, expected[:size], actual[:size], autojunk=False).ratio()
    suffix = SequenceMatcher(None, expected[-size:], actual[-size:], autojunk=False).ratio()
    return prefix, suffix


def _normalized_index(index: WordIndex) -> tuple[list[str], list[int]]:
    tokens: list[str] = []
    owners: list[int] = []
    for word_index, word in enumerate(index.words):
        normalized = normalize_text(word.word)
        tokens.extend(normalized)
        owners.extend([word_index] * len(normalized))
    return tokens, owners


def _uses_interpolated_timestamps(index: WordIndex) -> bool:
    """Cloud audio models often return coarse segment rather than word timing."""
    metadata = index.metadata if isinstance(index.metadata, dict) else {}
    backend_metadata = metadata.get("backend_metadata")
    if not isinstance(backend_metadata, dict):
        backend_metadata = {}
    return (
        str(metadata.get("backend") or "").strip().lower() == "cloud-audio"
        or str(backend_metadata.get("timestamp_precision") or "").strip().lower()
        == "interpolated"
    )


def _finding(
    segment: TimelineSegment,
    kind: str,
    severity: str,
    spoken_text: str,
    actual_text: str = "",
    actual_start: float | None = None,
    actual_end: float | None = None,
    score: float = 0.0,
    prefix: float = 0.0,
    suffix: float = 0.0,
    reason: str = "",
) -> ReviewFinding:
    return ReviewFinding(
        segment.id,
        segment.block_uid,
        kind,
        severity,
        spoken_text,
        segment.text,
        actual_text,
        segment.start_sec,
        segment.end_sec,
        actual_start,
        actual_end,
        round(score, 4),
        round(prefix, 4),
        round(suffix, 4),
        segment.match_status,
        reason,
    )


def review_rendered_timeline(timeline: FullTimeline, index: WordIndex) -> dict[str, Any]:
    """Return findings and evidence for eligible, actually assembled speech."""
    words = index.words
    actual, token_owners = _normalized_index(index)
    word_starts = [word.start for word in words]
    word_ends = [word.end for word in words]
    findings: list[ReviewFinding] = []
    checks: list[dict[str, Any]] = []
    cursor = 0
    interpolated_timestamps = _uses_interpolated_timestamps(index)

    speech_segments = [
        segment for segment in timeline.segments if segment.type in {"zk", "snh", "host"}
    ]
    reviewed: list[TimelineSegment] = []
    for segment in speech_segments:
        fragments = spoken_text_fragments(segment)
        spoken_text = " ".join(fragments)
        if segment.type in {"snh", "host"} and segment.match_status not in _MATCHED_STATUSES:
            findings.append(
                _finding(
                    segment,
                    "unavailable_source",
                    "info",
                    spoken_text,
                    reason=f"source match status is {segment.match_status or 'unset'}",
                )
            )
            continue
        if not spoken_text:
            findings.append(
                _finding(segment, "technical_cue", "info", "", reason="no spoken text")
            )
            continue
        reviewed.append(segment)
        expected = normalize_text(spoken_text)
        # OpenRouter audio models provide segment-level timestamps which this
        # adapter interpolates to words.  A wider *bounded* window absorbs that
        # known timing error without turning the primary pass into a global
        # text search.
        if interpolated_timestamps:
            margin = max(25.0, min(40.0, segment.duration_sec))
        else:
            margin = max(3.0, min(12.0, segment.duration_sec * 0.35))
        first_word = bisect_left(word_ends, max(0.0, segment.start_sec - margin))
        last_word = bisect_right(word_starts, segment.end_sec + margin)
        first_token = bisect_left(token_owners, first_word)
        last_token = bisect_left(token_owners, last_word)
        left, right, score = _best_span(expected, actual, first_token, last_token)

        minimum_evidence = max(2, (len(expected) * 3 + 4) // 5)
        local_match = score >= 0.5 and right - left >= minimum_evidence
        if not local_match:
            global_left, global_right, global_score = _global_best_span(expected, actual)
            if global_score >= 0.7 and global_right - global_left >= minimum_evidence:
                left, right, score = global_left, global_right, global_score
                kind = "out_of_order"
                reason = "speech found outside the expected timeline window"
            else:
                kind = "missing_from_sequence"
                reason = "speech not found near its expected timeline range"
        else:
            kind = ""
            reason = ""

        if left < right and token_owners:
            word_left = token_owners[left]
            word_right = token_owners[right - 1] + 1
            matched_words = words[word_left:word_right]
            actual_text = " ".join(word.word for word in matched_words)
            actual_start = matched_words[0].start
            actual_end = matched_words[-1].end
        else:
            actual_text = ""
            actual_start = None
            actual_end = None
        prefix, suffix = _coverage(expected, actual[left:right])

        if kind:
            findings.append(
                _finding(
                    segment, kind, "error", spoken_text, actual_text,
                    actual_start, actual_end, score, prefix, suffix, reason,
                )
            )
        elif prefix < 0.55 or suffix < 0.55:
            kind = "truncated_start" if prefix <= suffix else "truncated_end"
            findings.append(
                _finding(
                    segment, kind, "warning", spoken_text, actual_text,
                    actual_start, actual_end, score, prefix, suffix,
                    "expected prefix or suffix is not covered",
                )
            )
        elif left > cursor and words and token_owners:
            gap_left = token_owners[cursor] if cursor < len(token_owners) else len(words)
            gap_right = token_owners[left - 1] + 1
            if gap_right - gap_left >= 2:
                gap = words[gap_left:gap_right]
                findings.append(
                    ReviewFinding(
                        "", "", "extra_speech", "warning", "", "",
                        " ".join(word.word for word in gap), segment.start_sec,
                        segment.end_sec, gap[0].start, gap[-1].end, 0.0, 0.0,
                        0.0, "", "speech between approved blocks",
                    )
                )
        if local_match:
            cursor = max(cursor, right)

        checks.append(
            {
                "segment_id": segment.id,
                "block_uid": segment.block_uid,
                "status": kind or "ok",
                "spoken_text": spoken_text,
                "spoken_fragments": fragments,
                "timeline_range": [segment.start_sec, segment.end_sec],
                "actual_asr_range": [actual_start, actual_end],
                "score": round(score, 4),
                "prefix_coverage": round(prefix, 4),
                "suffix_coverage": round(suffix, 4),
                "source": {
                    "sequence_id": segment.sequence_id,
                    "sequence_name": segment.sequence_name,
                    "source_in_sec": segment.source_in_sec,
                    "source_out_sec": segment.source_out_sec,
                },
                "reason": reason or ("fully covered" if not kind else kind),
            }
        )

    return {
        "status": "completed",
        "summary": {
            "scenario_speech_segments": len(speech_segments),
            "expected_segments": len(reviewed),
            "unavailable_source": sum(item.kind == "unavailable_source" for item in findings),
            "findings": len(findings),
            "missing": sum(item.kind == "missing_from_sequence" for item in findings),
            "truncated": sum(item.kind.startswith("truncated_") for item in findings),
            "out_of_order": sum(item.kind == "out_of_order" for item in findings),
            "automatic_corrections": 0,
        },
        "checks": checks,
        "findings": [item.to_dict() for item in findings],
        # A rendered WAV cannot prove source identity or source-ASR boundaries.
        "corrections": [],
    }
