"""Russian-aware fuzzy alignment of scenario quotes to timed transcripts."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from math import isfinite
from typing import Iterable

from .sync_models import (
    AlignmentRequest,
    SyncAlternative,
    SyncMatch,
    SyncSource,
    WordIndex,
    WordTimestamp,
)

_TOKEN_RE = re.compile(r"[0-9A-Za-zА-Яа-яЁё]+")
_RUSSIAN_ENDINGS = tuple(
    sorted(
        {
            "иями", "ями", "ами", "его", "ого", "ему", "ому", "ими", "ыми",
            "ая", "яя", "ою", "ею", "ий", "ый", "ой", "ие", "ые", "ое",
            "им", "ым", "ем", "ом", "их", "ых", "ую", "юю", "ах", "ях",
            "ам", "ям", "ами", "ями", "ов", "ев", "ей", "ою", "ею",
            "а", "я", "ы", "и", "ь", "й", "у", "ю", "о", "е",
        },
        key=len,
        reverse=True,
    )
)
_STOPWORDS = {
    "а", "без", "бы", "в", "во", "вот", "да", "для", "до", "же", "за",
    "и", "из", "или", "к", "как", "ли", "на", "не", "но", "ну", "о", "об",
    "от", "по", "под", "при", "с", "со", "то", "у", "что", "это", "я",
}


def normalize_token(token: str) -> str:
    """Normalize a token and apply a deliberately conservative Russian stem."""

    value = unicodedata.normalize("NFKC", token).casefold().replace("ё", "е")
    value = "".join(char for char in value if char.isalnum())
    if len(value) <= 3 or not any("а" <= char <= "я" for char in value):
        return value
    for ending in _RUSSIAN_ENDINGS:
        if value.endswith(ending) and len(value) - len(ending) >= 3:
            return value[: -len(ending)]
    return value


def normalize_text(text: str) -> list[str]:
    return [
        normalized
        for token in _TOKEN_RE.findall(unicodedata.normalize("NFKC", text).replace("ё", "е"))
        if (normalized := normalize_token(token))
    ]


def parse_timecode(value: str | float | int | None, fps: float = 25.0) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        result = float(value)
        if not isfinite(result) or result < 0:
            raise ValueError("timecode seconds must be a non-negative finite number")
        return result
    cleaned = str(value).strip().replace(",", ".")
    if cleaned.replace(".", "", 1).isdigit() and ":" not in cleaned:
        # Production scripts use MM.SS as well as HH.MM.SS.  Treat exactly two
        # trailing digits as seconds; a value such as "12.5" remains decimal.
        if cleaned.count(".") == 1:
            left, right = cleaned.split(".")
            if len(right) != 2:
                return float(cleaned)
            if int(right) >= 60:
                raise ValueError(f"invalid timecode: {value!r}")
            return int(left) * 60 + int(right)
    parts = re.split(r"[:.]", cleaned)
    if not parts or any(not part.isdigit() for part in parts):
        raise ValueError(f"invalid timecode: {value!r}")
    numbers = [int(part) for part in parts]
    if len(numbers) == 2:
        minutes, seconds = numbers
        if seconds >= 60:
            raise ValueError(f"invalid timecode: {value!r}")
        return minutes * 60 + seconds
    if len(numbers) == 3:
        hours, minutes, seconds = numbers
        if minutes >= 60 or seconds >= 60:
            raise ValueError(f"invalid timecode: {value!r}")
        return hours * 3600 + minutes * 60 + seconds
    if len(numbers) == 4:
        hours, minutes, seconds, frames = numbers
        if minutes >= 60 or seconds >= 60 or frames >= fps:
            raise ValueError(f"invalid timecode: {value!r}")
        return hours * 3600 + minutes * 60 + seconds + frames / fps
    raise ValueError(f"invalid timecode: {value!r}")


@dataclass(frozen=True)
class MatcherConfig:
    search_window_sec: float = 180.0
    auto_threshold: float = 0.78
    review_threshold: float = 0.50
    ambiguity_margin: float = 0.07
    fallback_trigger: float = 0.64
    max_alternatives: int = 3
    head_padding_sec: float = 0.5
    tail_padding_sec: float = 1.0

    def __post_init__(self) -> None:
        if self.search_window_sec <= 0:
            raise ValueError("search_window_sec must be positive")
        for value in (
            self.auto_threshold,
            self.review_threshold,
            self.ambiguity_margin,
            self.fallback_trigger,
        ):
            if not 0 <= value <= 1:
                raise ValueError("matcher thresholds must be between 0 and 1")
        if self.review_threshold > self.auto_threshold:
            raise ValueError("review_threshold must not exceed auto_threshold")
        if self.max_alternatives < 0:
            raise ValueError("max_alternatives must not be negative")


@dataclass(frozen=True)
class _Token:
    normalized: str
    word: WordTimestamp


@dataclass(frozen=True)
class _Candidate:
    source_id: str
    start: float
    end: float
    confidence: float
    text: str
    strategy: str


def _source_start_tc(source: SyncSource) -> float:
    return parse_timecode(source.sequence_start_tc, source.fps) or 0.0


def _approx_for_source(request: AlignmentRequest, source: SyncSource) -> float | None:
    parsed = parse_timecode(request.approximate_timecode, source.fps)
    if parsed is None:
        return None
    if request.timecode_mode == "display":
        return max(0.0, parsed - _source_start_tc(source))
    return parsed


def _timed_tokens(index: WordIndex) -> list[_Token]:
    result: list[_Token] = []
    for word in index.words:
        tokens = normalize_text(word.word)
        result.extend(_Token(token, word) for token in tokens)
    return result


def _score(query: list[str], candidate: list[str]) -> float:
    if not query or not candidate:
        return 0.0
    matcher = SequenceMatcher(None, query, candidate, autojunk=False)
    ordered_coverage = sum(block.size for block in matcher.get_matching_blocks()) / len(query)
    ratio = matcher.ratio()
    query_content = [token for token in query if token not in _STOPWORDS and len(token) >= 3]
    candidate_content = set(
        token for token in candidate if token not in _STOPWORDS and len(token) >= 3
    )
    if query_content:
        content_coverage = sum(token in candidate_content for token in query_content) / len(
            query_content
        )
    else:
        content_coverage = ordered_coverage
    query_chars = " ".join(query)
    candidate_chars = " ".join(candidate)
    # Character-level SequenceMatcher with autojunk disabled is useful for
    # short spelling differences, but becomes quadratic on long paragraphs
    # with repeated Cyrillic characters.
    if max(len(query_chars), len(candidate_chars)) <= 480:
        char_ratio = SequenceMatcher(
            None, query_chars, candidate_chars, autojunk=False
        ).ratio()
    else:
        char_ratio = ratio
    return min(1.0, 0.38 * ratio + 0.34 * ordered_coverage + 0.20 * content_coverage + 0.08 * char_ratio)


def _candidate_starts(
    query: list[str],
    tokens: list[_Token],
    *,
    max_starts: int = 240,
) -> list[int]:
    positions: dict[str, list[int]] = {}
    for index, token in enumerate(tokens):
        positions.setdefault(token.normalized, []).append(index)
    anchors: list[tuple[int, int, str]] = []
    seen_tokens: set[str] = set()
    for query_index, token in enumerate(query):
        if token in _STOPWORDS or len(token) < 3 or token in seen_tokens:
            continue
        token_positions = positions.get(token, ())
        if token_positions:
            anchors.append((len(token_positions), query_index, token))
            seen_tokens.add(token)

    # Rare exact anchors are much more informative than common words. Limiting
    # them keeps long scenario paragraphs from producing hundreds of thousands
    # of expensive SequenceMatcher calls.
    anchors.sort(key=lambda item: (item[0], -len(item[2]), item[1]))
    start_scores: dict[int, int] = {0: 0}
    for occurrence_count, query_index, token in anchors[:12]:
        rarity_weight = max(1, 8 - min(7, occurrence_count))
        for position in positions[token]:
            estimate = position - query_index
            for drift in (-2, -1, 0, 1, 2):
                start = max(0, estimate + drift)
                start_scores[start] = start_scores.get(start, 0) + (
                    rarity_weight * (3 - abs(drift))
                )
    # When Whisper shares no exact token with the script, still sample all
    # plausible starts; this enables partial character-level matches.
    if len(start_scores) == 1:
        for start in range(0, len(tokens), max(1, len(query) // 3)):
            start_scores[start] = 1
    ranked = sorted(
        (
            (score, start)
            for start, score in start_scores.items()
            if start < len(tokens)
        ),
        key=lambda item: (-item[0], item[1]),
    )
    return sorted(start for _score_value, start in ranked[:max_starts])


def _length_samples(min_length: int, max_length: int, target: int) -> tuple[list[int], int]:
    width = max_length - min_length
    if width <= 24:
        return list(range(min_length, max_length + 1)), 1
    samples = 13
    step = max(1, (width + samples - 2) // (samples - 1))
    values = {
        min_length,
        max_length,
        max(min_length, min(max_length, target)),
    }
    values.update(range(min_length, max_length + 1, step))
    return sorted(values), step


def _search_one(
    source: SyncSource,
    index: WordIndex,
    query: list[str],
    approx: float | None,
    window: float | None,
    strategy: str,
    timed_tokens: list[_Token] | None = None,
) -> list[_Candidate]:
    all_tokens = timed_tokens if timed_tokens is not None else _timed_tokens(index)
    if approx is not None and window is not None:
        guide_approx = approx - source.guide_offset_sec
        lower, upper = guide_approx - window, guide_approx + window
        tokens = [
            item
            for item in all_tokens
            if item.word.end >= lower and item.word.start <= upper
        ]
    else:
        tokens = all_tokens
    if not tokens:
        return []

    min_length = max(1, len(query) - max(3, len(query) // 3))
    max_length = min(len(tokens), len(query) + max(5, len(query) // 2))
    coarse_lengths, length_step = _length_samples(min_length, max_length, len(query))
    candidates: list[_Candidate] = []
    for start in _candidate_starts(query, tokens):
        best: _Candidate | None = None

        def score_length(length: int) -> _Candidate | None:
            span = tokens[start : start + length]
            if len(span) < min_length:
                return None
            confidence = _score(query, [item.normalized for item in span])
            return _Candidate(
                source_id=source.id,
                start=max(0.0, span[0].word.start + source.guide_offset_sec),
                end=span[-1].word.end + source.guide_offset_sec,
                confidence=confidence,
                text=" ".join(item.word.word for item in span),
                strategy=strategy,
            )

        best_length = min_length
        for length in coarse_lengths:
            current = score_length(length)
            if current is None:
                continue
            if best is None or current.confidence > best.confidence:
                best = current
                best_length = length
        if best is not None and length_step > 1:
            refine_start = max(min_length, best_length - length_step)
            refine_end = min(max_length, best_length + length_step)
            for length in range(refine_start, refine_end + 1):
                if length in coarse_lengths:
                    continue
                current = score_length(length)
                if current is not None and current.confidence > best.confidence:
                    best = current
        if best is not None:
            candidates.append(best)
    return candidates


def _deduplicate(candidates: Iterable[_Candidate]) -> list[_Candidate]:
    ordered = sorted(
        candidates,
        key=lambda item: (-item.confidence, item.source_id, item.start, item.end),
    )
    result: list[_Candidate] = []
    for candidate in ordered:
        duplicate = False
        for prior in result:
            if candidate.source_id != prior.source_id:
                continue
            intersection = max(
                0.0, min(candidate.end, prior.end) - max(candidate.start, prior.start)
            )
            shorter = min(
                max(0.0, candidate.end - candidate.start),
                max(0.0, prior.end - prior.start),
            )
            same_occurrence = shorter > 0 and intersection / shorter >= 0.65
            if abs(candidate.start - prior.start) < 1.0 or same_occurrence:
                duplicate = True
                break
        if duplicate:
            continue
        result.append(candidate)
    return result


def _padded_range(
    candidate: _Candidate,
    source: SyncSource,
    config: MatcherConfig,
) -> tuple[float, float]:
    start = max(0.0, candidate.start - config.head_padding_sec)
    end = candidate.end + config.tail_padding_sec
    if source.duration_sec > 0:
        start = min(source.duration_sec, start)
        end = min(source.duration_sec, end)
    return start, max(start, end)


def _has_usable_range(
    candidate: _Candidate,
    source: SyncSource,
    config: MatcherConfig,
) -> bool:
    start, end = _padded_range(candidate, source, config)
    return end - start > 0.001


class SyncMatcher:
    def __init__(
        self,
        sources: Iterable[SyncSource],
        indexes: Iterable[WordIndex],
        config: MatcherConfig | None = None,
    ):
        self.sources = {item.id: item for item in sources}
        self.indexes = {item.source_id: item for item in indexes}
        self.timed_tokens = {
            source_id: _timed_tokens(index)
            for source_id, index in self.indexes.items()
        }
        self.speaker_tokens = {
            source_id: [
                tokens
                for name in source.speaker_names
                if (tokens := normalize_text(name))
            ]
            for source_id, source in self.sources.items()
        }
        self.config = config or MatcherConfig()

    def _eligible_sources(self, request: AlignmentRequest) -> list[SyncSource]:
        if request.source_ids:
            return [
                self.sources[source_id]
                for source_id in request.source_ids
                if source_id in self.sources and source_id in self.indexes
            ]
        available = [
            source for source in self.sources.values() if source.id in self.indexes
        ]
        speaker_tokens = normalize_text(request.speaker)
        if not speaker_tokens:
            return available
        universal = [source for source in available if source.all_speakers]

        def source_speaker_tokens(source: SyncSource) -> list[list[str]]:
            return self.speaker_tokens.get(source.id, [])

        def with_universal(values: list[SyncSource]) -> list[SyncSource]:
            by_id = {source.id: source for source in [*values, *universal]}
            return [by_id[source.id] for source in available if source.id in by_id]

        exact = [
            source
            for source in available
            if any(tokens == speaker_tokens for tokens in source_speaker_tokens(source))
        ]
        if exact:
            return with_universal(exact)
        query_name = set(speaker_tokens)
        fuzzy = [
            source
            for source in available
            if any(
                query_name.intersection(tokens)
                or SequenceMatcher(
                    None, " ".join(speaker_tokens), " ".join(tokens), autojunk=False
                ).ratio() >= 0.72
                for tokens in source_speaker_tokens(source)
            )
        ]
        if fuzzy:
            return with_universal(fuzzy)
        # A source registered without speaker metadata remains searchable, but
        # a clearly different named speaker is not searched in this interview.
        return with_universal([
            source for source in available if not source_speaker_tokens(source)
        ])

    def match(self, request: AlignmentRequest) -> SyncMatch:
        query = normalize_text(request.text)
        sources = self._eligible_sources(request)
        if len(query) < 2 or not sources:
            return SyncMatch(
                segment_id=request.segment_id,
                query_text=request.text,
                speaker=request.speaker,
                approximate_timecode=str(request.approximate_timecode or ""),
                status="not_found",
                strategy="insufficient_input",
            )

        local: list[_Candidate] = []
        has_approx = request.approximate_timecode not in (None, "")
        for source in sources:
            approx = _approx_for_source(request, source)
            local.extend(
                _search_one(
                    source,
                    self.indexes[source.id],
                    query,
                    approx,
                    self.config.search_window_sec if approx is not None else None,
                    "near_timecode" if approx is not None else "full_transcript",
                    self.timed_tokens[source.id],
                )
            )
        candidates = [
            item for item in _deduplicate(local)
            if _has_usable_range(item, self.sources[item.source_id], self.config)
        ]

        if has_approx and (not candidates or candidates[0].confidence < self.config.fallback_trigger):
            fallback: list[_Candidate] = []
            for source in sources:
                fallback.extend(
                    _search_one(
                        source,
                        self.indexes[source.id],
                        query,
                        None,
                        None,
                        "fallback_full",
                        self.timed_tokens[source.id],
                    )
                )
            candidates = [
                item for item in _deduplicate([*candidates, *fallback])
                if _has_usable_range(item, self.sources[item.source_id], self.config)
            ]

        if not candidates or candidates[0].confidence < self.config.review_threshold:
            alternatives: list[SyncAlternative] = []
            for item in candidates[: self.config.max_alternatives]:
                item_in, item_out = _padded_range(
                    item, self.sources[item.source_id], self.config
                )
                alternatives.append(
                    SyncAlternative(
                        item.source_id,
                        item_in,
                        item_out,
                        round(item.confidence, 4),
                        item.text,
                    )
                )
            return SyncMatch(
                segment_id=request.segment_id,
                query_text=request.text,
                speaker=request.speaker,
                approximate_timecode=str(request.approximate_timecode or ""),
                confidence=round(candidates[0].confidence, 4) if candidates else 0.0,
                status="not_found",
                strategy=candidates[0].strategy if candidates else "no_candidates",
                alternatives=alternatives,
            )

        best = candidates[0]
        other = candidates[1] if len(candidates) > 1 else None
        ambiguous = bool(
            other
            and best.confidence - other.confidence < self.config.ambiguity_margin
        )
        # A strong global fallback is useful, but its disagreement with the
        # supplied timecode must remain visible to an editor.
        auto = (
            best.confidence >= self.config.auto_threshold
            and not ambiguous
            and best.strategy != "fallback_full"
        )
        status = "auto_matched" if auto else "needs_review"
        alternatives: list[SyncAlternative] = []
        for item in candidates[1 : 1 + self.config.max_alternatives]:
            item_in, item_out = _padded_range(
                item, self.sources[item.source_id], self.config
            )
            alternatives.append(
                SyncAlternative(
                    item.source_id,
                    item_in,
                    item_out,
                    round(item.confidence, 4),
                    item.text,
                )
            )
        source_in, source_out = _padded_range(
            best, self.sources[best.source_id], self.config
        )
        return SyncMatch(
            segment_id=request.segment_id,
            query_text=request.text,
            speaker=request.speaker,
            approximate_timecode=str(request.approximate_timecode or ""),
            source_id=best.source_id,
            source_in_sec=source_in,
            source_out_sec=source_out,
            confidence=round(best.confidence, 4),
            status=status,
            matched_text=best.text,
            strategy=best.strategy,
            alternatives=alternatives,
            metadata={
                "ambiguous": ambiguous,
                "timecode_mode": request.timecode_mode,
            },
        )
