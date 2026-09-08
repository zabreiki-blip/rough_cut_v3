"""Comparison of a rendered Rough Cut sequence with its approved timeline.

The module deliberately makes no editorial changes.  It produces a durable,
deterministic report from word timestamps; the CEP host owns creating a copy
of a Premiere sequence and applying the explicitly listed range corrections.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from difflib import SequenceMatcher
from typing import Any

from .models import FullTimeline
from .sync_models import WordIndex
from .sync_matcher import normalize_text


@dataclass(frozen=True)
class ReviewFinding:
    segment_id: str
    block_uid: str
    kind: str
    severity: str
    expected_text: str
    actual_text: str
    expected_start_sec: float
    actual_start_sec: float | None
    actual_end_sec: float | None
    score: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _best_span(
    expected: list[str], actual: list[str], start_at: int, stop_at: int | None = None
) -> tuple[int, int, float]:
    """Find the best forward text window without quadratic unbounded scans."""
    limit = min(len(actual), stop_at if stop_at is not None else start_at + 600)
    if not expected or start_at >= limit:
        return start_at, start_at, 0.0
    minimum = max(1, len(expected) - max(3, len(expected) // 3))
    maximum = min(len(actual), len(expected) + max(5, len(expected) // 2))
    best = (start_at, start_at, 0.0)
    for index in range(start_at, limit - minimum + 1):
        for length in range(minimum, min(maximum, limit - index) + 1):
            score = SequenceMatcher(None, expected, actual[index : index + length], autojunk=False).ratio()
            if score > best[2]:
                best = (index, index + length, score)
    return best


def review_rendered_timeline(timeline: FullTimeline, index: WordIndex) -> dict[str, Any]:
    """Return ordered findings and safe range-only corrections.

    A rendered mix cannot reliably identify a source sequence for speech that
    is wholly absent.  Therefore only a matched sync/host whose transcript
    starts or ends inside its spoken core receives an automatic correction.
    Missing blocks remain visible findings for the editor.
    """
    words = index.words
    actual = normalize_text(" ".join(word.word for word in words))
    cursor = 0
    findings: list[ReviewFinding] = []
    corrections: list[dict[str, Any]] = []
    reviewed = [segment for segment in timeline.segments if segment.type in {"zk", "snh", "host"}]
    for segment in reviewed:
        expected = normalize_text(segment.text)
        left, right, score = _best_span(expected, actual, cursor)
        actual_text = " ".join(word.word for word in words[left:right])
        actual_start = words[left].start if left < right else None
        actual_end = words[right - 1].end if left < right else None
        if score < 0.5:
            old_left, old_right, old_score = _best_span(expected, actual, 0, cursor)
            if old_score >= 0.7:
                findings.append(ReviewFinding(segment.id, segment.block_uid, "out_of_order", "error", segment.text, " ".join(word.word for word in words[old_left:old_right]), segment.start_sec, words[old_left].start, words[old_right - 1].end, round(old_score, 4)))
            else:
                findings.append(ReviewFinding(segment.id, segment.block_uid, "missing", "error", segment.text, actual_text, segment.start_sec, actual_start, actual_end, round(score, 4)))
            continue
        if left > cursor:
            findings.append(ReviewFinding("", "", "extra_speech", "warning", "", " ".join(word.word for word in words[cursor:left]), segment.start_sec, words[cursor].start, words[left - 1].end, 0.0))
        cursor = right
        expected_prefix = expected[: max(1, len(expected) // 4)]
        actual_prefix = actual[left : left + len(expected_prefix)]
        expected_suffix = expected[-max(1, len(expected) // 4) :]
        actual_suffix = actual[max(left, right - len(expected_suffix)) : right]
        prefix_score = SequenceMatcher(None, expected_prefix, actual_prefix, autojunk=False).ratio()
        suffix_score = SequenceMatcher(None, expected_suffix, actual_suffix, autojunk=False).ratio()
        if prefix_score < 0.55 or suffix_score < 0.55:
            kind = "truncated_start" if prefix_score <= suffix_score else "truncated_end"
            findings.append(ReviewFinding(segment.id, segment.block_uid, kind, "warning", segment.text, actual_text, segment.start_sec, actual_start, actual_end, round(score, 4)))
            if segment.type in {"snh", "host"} and segment.source_out_sec > segment.source_in_sec:
                corrections.append({
                    "segment_id": segment.id,
                    "block_uid": segment.block_uid,
                    "kind": "expand_source_range",
                    "source_in_sec": max(0.0, segment.source_in_sec - (2.0 if kind == "truncated_start" else 0.0)),
                    "source_out_sec": segment.source_out_sec + (1.5 if kind == "truncated_end" else 0.0),
                    "source_sequence_id": segment.sequence_id,
                    "source_sequence_name": segment.sequence_name,
                    "timeline_seconds": segment.start_sec,
                    "placeholder_identity": f"RC_BLOCK {segment.block_uid}",
                    "reason": kind,
                })
    # Extra speech is useful even when precise ownership cannot be inferred.
    if actual and not reviewed:
        findings.append(ReviewFinding("", "", "extra_speech", "warning", "", " ".join(word.word for word in words), 0.0, words[0].start, words[-1].end, 0.0))
    return {
        "status": "completed",
        "summary": {
            "expected_segments": len(reviewed),
            "findings": len(findings),
            "missing": sum(item.kind == "missing" for item in findings),
            "truncated": sum(item.kind.startswith("truncated_") for item in findings),
            "automatic_corrections": len(corrections),
        },
        "findings": [item.to_dict() for item in findings],
        "corrections": corrections,
    }
