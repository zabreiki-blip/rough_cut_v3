"""Russian-aware fuzzy alignment of scenario quotes to timed transcripts."""

from __future__ import annotations

import heapq
import os
import re
import unicodedata
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from math import isfinite
from typing import Any, Iterable

from .sync_models import (
    AlignmentRequest,
    MatchStatus,
    SyncAlternative,
    SyncMatch,
    SyncSource,
    WordIndex,
    WordTimestamp,
)
from .text_normalization import fold_russian_text

_TOKEN_RE = re.compile(r"[0-9A-Za-zА-Яа-яЁё]+")
_RUSSIAN_ENDINGS = tuple(
    sorted(
        {
            "иями",
            "ями",
            "ами",
            "его",
            "ого",
            "ему",
            "ому",
            "ими",
            "ыми",
            "ая",
            "яя",
            "ою",
            "ею",
            "ий",
            "ый",
            "ой",
            "ие",
            "ые",
            "ое",
            "им",
            "ым",
            "ем",
            "ом",
            "их",
            "ых",
            "ую",
            "юю",
            "ах",
            "ях",
            "ам",
            "ям",
            "ами",
            "ями",
            "ов",
            "ев",
            "ей",
            "ою",
            "ею",
            "а",
            "я",
            "ы",
            "и",
            "ь",
            "й",
            "у",
            "ю",
            "о",
            "е",
        },
        key=len,
        reverse=True,
    )
)
_STOPWORDS = {
    "а",
    "без",
    "бы",
    "в",
    "во",
    "вот",
    "да",
    "для",
    "до",
    "же",
    "за",
    "и",
    "из",
    "или",
    "к",
    "как",
    "ли",
    "на",
    "не",
    "но",
    "ну",
    "о",
    "об",
    "от",
    "по",
    "под",
    "при",
    "с",
    "со",
    "то",
    "у",
    "что",
    "это",
    "я",
}
_MAX_CANDIDATE_STARTS = 240
_MAX_LOCAL_CANDIDATE_STARTS = 120

# Candidate-start heuristics. Exact anchors receive a small positional drift to
# tolerate inserted/missing Whisper words; fuzzy anchors use a stricter drift
# and may consume at most three quarters of the fixed search budget so that
# uniform transcript coverage is always retained.
_EXACT_ANCHOR_LIMIT = 12
_EXACT_ANCHOR_MAX_DRIFT = 2
_FUZZY_ANCHOR_MAX_DRIFT = 1
_FUZZY_MIN_TRIGRAM_SIMILARITY = 0.55
_FUZZY_BUDGET_NUMERATOR = 3
_FUZZY_BUDGET_DENOMINATOR = 4
_FUZZY_DRIFT_PENALTY = 0.05

# Candidate spans covering at least this share of the shorter span are treated
# as variants of one occurrence rather than distinct alternatives.
_DUPLICATE_MIN_OVERLAP_RATIO = 0.65


def normalize_token(token: str) -> str:
    """Normalize a token and apply a deliberately conservative Russian stem."""

    value = fold_russian_text(token)
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


_NEGATION_MARKERS = {
    "не",
    "ни",
    "нет",
    "без",
    "никогда",
    "никто",
    "ничего",
    "никак",
    "нельзя",
}
_NEGATED_WORD_PREFIXES = ("несчастлив",)


def _count_negations(text: str) -> int:
    tokens = _TOKEN_RE.findall(unicodedata.normalize("NFKC", text).replace("ё", "е").casefold())
    # Cloud ASR can stutter a token (``не не рассказывала``). Treat an
    # immediately repeated negation as one utterance, while preserving real
    # constructions such as ``не видел и не слышал``.
    return sum(
        1
        for index, token in enumerate(tokens)
        if (token in _NEGATION_MARKERS and (index == 0 or tokens[index - 1] != token))
        or any(token.startswith(prefix) for prefix in _NEGATED_WORD_PREFIXES)
    )


# Cardinal and (within reason) ordinal word forms for Russian numerals. Keys
# are normalized once through `normalize_token` below so that a form found in
# real text (which may carry a different case/gender ending) resolves to the
# same lookup key as the canonical spelling registered here.
_NUMBER_UNIT_FORMS = {
    "один": 1,
    "одна": 1,
    "одно": 1,
    "одного": 1,
    "одной": 1,
    "одним": 1,
    "одну": 1,
    "первый": 1,
    "первого": 1,
    "первая": 1,
    "первое": 1,
    "первым": 1,
    "первую": 1,
    "первой": 1,
    "два": 2,
    "две": 2,
    "второй": 2,
    "второго": 2,
    "вторая": 2,
    "второе": 2,
    "три": 3,
    "третий": 3,
    "третьего": 3,
    "третья": 3,
    "третье": 3,
    "четыре": 4,
    "четвертый": 4,
    "четвёртый": 4,
    "четвертого": 4,
    "четвёртого": 4,
    "пять": 5,
    "пятый": 5,
    "пятого": 5,
    "пятая": 5,
    "пятое": 5,
    "шесть": 6,
    "шестой": 6,
    "шестого": 6,
    "семь": 7,
    "седьмой": 7,
    "седьмого": 7,
    "восемь": 8,
    "восьмой": 8,
    "восьмого": 8,
    "девять": 9,
    "девятый": 9,
    "девятого": 9,
}
_NUMBER_TEEN_FORMS = {
    "десять": 10,
    "десятый": 10,
    "десятого": 10,
    "одиннадцать": 11,
    "одиннадцатый": 11,
    "двенадцать": 12,
    "двенадцатый": 12,
    "тринадцать": 13,
    "тринадцатый": 13,
    "четырнадцать": 14,
    "четырнадцатый": 14,
    "пятнадцать": 15,
    "пятнадцатый": 15,
    "шестнадцать": 16,
    "шестнадцатый": 16,
    "семнадцать": 17,
    "семнадцатый": 17,
    "восемнадцать": 18,
    "восемнадцатый": 18,
    "девятнадцать": 19,
    "девятнадцатый": 19,
}
_NUMBER_TEN_FORMS = {
    "двадцать": 20,
    "двадцатый": 20,
    "тридцать": 30,
    "тридцатый": 30,
    "сорок": 40,
    "сороковой": 40,
    "пятьдесят": 50,
    "пятидесятый": 50,
    "шестьдесят": 60,
    "шестидесятый": 60,
    "семьдесят": 70,
    "семидесятый": 70,
    "восемьдесят": 80,
    "восьмидесятый": 80,
    "девяносто": 90,
    "девяностый": 90,
}
_NUMBER_HUNDRED_FORMS = {
    "сто": 100,
    "сотый": 100,
    "двести": 200,
    "двухсотый": 200,
    "триста": 300,
    "трехсотый": 300,
    "трёхсотый": 300,
    "четыреста": 400,
    "четырехсотый": 400,
    "четырёхсотый": 400,
    "пятьсот": 500,
    "пятисотый": 500,
    "шестьсот": 600,
    "шестисотый": 600,
    "семьсот": 700,
    "семисотый": 700,
    "восемьсот": 800,
    "восьмисотый": 800,
    "девятьсот": 900,
    "девятисотый": 900,
}
_NUMBER_SCALE_FORMS = {
    "тысяча": 1_000,
    "тысячи": 1_000,
    "тысяч": 1_000,
    "тысячный": 1_000,
    "тысячного": 1_000,
    "миллион": 1_000_000,
    "миллиона": 1_000_000,
    "миллионов": 1_000_000,
    "миллионный": 1_000_000,
}


def _build_number_lookup(forms: dict[str, int]) -> dict[str, int]:
    return {normalize_token(word): value for word, value in forms.items()}


_NUMBER_UNITS = _build_number_lookup(_NUMBER_UNIT_FORMS)
_NUMBER_TEENS = _build_number_lookup(_NUMBER_TEEN_FORMS)
_NUMBER_TENS = _build_number_lookup(_NUMBER_TEN_FORMS)
_NUMBER_HUNDREDS = _build_number_lookup(_NUMBER_HUNDRED_FORMS)
_NUMBER_SCALES = _build_number_lookup(_NUMBER_SCALE_FORMS)

# Place-value levels used to decide whether consecutive number words combine
# into a single value (classic descending-magnitude rule, e.g. "сто
# пятьдесят" = 150) or start a new number ("два три" = [2, 3]).
_LEVEL_UNIT = 1
_LEVEL_TEEN = 2
_LEVEL_TEN = 2
_LEVEL_HUNDRED = 3
_LEVEL_AFTER_SCALE = 4  # higher than any real level: accepts any next word


def _extract_numbers(text: str) -> list[int]:
    """Extract the multiset of significant numbers mentioned in ``text``.

    Digits are read directly; Russian number words (cardinal and, within
    reasonable bounds, ordinal forms) are resolved to the same integer value
    so that "сто пятьдесят" and "150" compare equal.
    """

    tokens = _TOKEN_RE.findall(unicodedata.normalize("NFKC", text).replace("ё", "е"))
    numbers: list[int] = []
    group = 0
    has_group = False
    last_level = 0

    def flush() -> None:
        nonlocal group, has_group, last_level
        if has_group:
            numbers.append(group)
        group = 0
        has_group = False
        last_level = 0

    def add(value: int, level: int) -> None:
        nonlocal group, has_group, last_level
        # Only combine with the current group when the place value strictly
        # decreases (hundreds -> tens/teens -> units). A level that repeats
        # or jumps back up starts a new number instead of being summed in.
        if has_group and level >= last_level:
            flush()
        group += value
        has_group = True
        last_level = level

    for raw in tokens:
        lowered = raw.casefold()
        if lowered.isdigit():
            flush()
            numbers.append(int(lowered))
            continue
        stem = normalize_token(raw)
        if stem in _NUMBER_UNITS:
            add(_NUMBER_UNITS[stem], _LEVEL_UNIT)
        elif stem in _NUMBER_TEENS:
            add(_NUMBER_TEENS[stem], _LEVEL_TEEN)
        elif stem in _NUMBER_TENS:
            add(_NUMBER_TENS[stem], _LEVEL_TEN)
        elif stem in _NUMBER_HUNDREDS:
            add(_NUMBER_HUNDREDS[stem], _LEVEL_HUNDRED)
        elif stem in _NUMBER_SCALES:
            group = (group if has_group else 1) * _NUMBER_SCALES[stem]
            has_group = True
            last_level = _LEVEL_AFTER_SCALE
        else:
            flush()
    flush()
    return numbers


TIMING_UNREFINED_FLAG = "timing_unrefined"
# Precisions whose word boundaries are NOT trustworthy for automatic assembly:
# "interpolated" = words spread evenly over a cloud segment span.  Reliable ones:
# "word" (genuine per-word engine times), "retimed" (cloud words re-aligned by the
# local Whisper pass, see stage2.index_retiming) and "" (hand-built index with no
# provenance, kept as before).
_UNRELIABLE_TIMESTAMP_PRECISIONS = frozenset({"interpolated"})


def _timing_gate_flags(index: WordIndex) -> list[str]:
    """``["timing_unrefined"]`` when the index word times are still interpolated.

    The top-level ``timestamp_precision`` is authoritative (a successful local
    retime sets it to ``retimed``); the backend value is only a fallback for
    indexes that carry no top-level marker.
    """
    metadata = index.metadata if isinstance(index.metadata, dict) else {}
    precision = str(metadata.get("timestamp_precision") or "").strip().lower()
    if not precision:
        backend = metadata.get("backend_metadata")
        if isinstance(backend, dict):
            precision = str(backend.get("timestamp_precision") or "").strip().lower()
    return [TIMING_UNREFINED_FLAG] if precision in _UNRELIABLE_TIMESTAMP_PRECISIONS else []


def _semantic_gate_flags(query_text: str, candidate_text: str) -> list[str]:
    """Detect meaning-inverting divergences that a purely textual similarity
    score cannot see (negation, changed numbers), so a strong fuzzy match can
    still be routed to manual review instead of being auto-assembled."""

    flags: list[str] = []
    if _count_negations(query_text) != _count_negations(candidate_text):
        flags.append("negation")
    if Counter(_extract_numbers(query_text)) != Counter(_extract_numbers(candidate_text)):
        flags.append("number")
    return flags


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


DEFAULT_MAX_INTERNAL_GAP_SEC = 5.0


def _default_max_internal_gap_sec() -> float:
    """Gap gate default; ``SYNC_MATCH_MAX_INTERNAL_GAP_SEC`` overrides it."""
    raw = os.environ.get("SYNC_MATCH_MAX_INTERNAL_GAP_SEC", "").strip()
    if not raw:
        return DEFAULT_MAX_INTERNAL_GAP_SEC
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_MAX_INTERNAL_GAP_SEC
    return value if isfinite(value) and value > 0 else DEFAULT_MAX_INTERNAL_GAP_SEC


@dataclass(frozen=True)
class MatcherConfig:
    search_window_sec: float = 180.0
    auto_threshold: float = 0.78
    review_threshold: float = 0.50
    ambiguity_margin: float = 0.07
    fallback_trigger: float = 0.64
    max_alternatives: int = 3
    # ASR word timestamps regularly start a little after the audible onset.
    # These are editorial handles, not confidence margins.
    head_padding_sec: float = 2.0
    tail_padding_sec: float = 1.5
    # A candidate stitched across a longer silence between neighbouring words
    # is not one continuous phrase and must be reviewed by an editor.
    max_internal_gap_sec: float = field(default_factory=_default_max_internal_gap_sec)

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
        if not (isfinite(self.max_internal_gap_sec) and self.max_internal_gap_sec > 0):
            raise ValueError("max_internal_gap_sec must be positive")


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
    max_gap_sec: float = 0.0
    max_word_duration_sec: float = 0.0


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
        char_ratio = SequenceMatcher(None, query_chars, candidate_chars, autojunk=False).ratio()
    else:
        char_ratio = ratio
    return min(
        1.0, 0.38 * ratio + 0.34 * ordered_coverage + 0.20 * content_coverage + 0.08 * char_ratio
    )


def _token_positions(tokens: list[_Token]) -> dict[str, list[int]]:
    positions: dict[str, list[int]] = {}
    for index, token in enumerate(tokens):
        positions.setdefault(token.normalized, []).append(index)
    return positions


def _exact_anchor_scores(
    query: list[str],
    positions: dict[str, list[int]],
    eligible_starts: set[int],
) -> dict[int, int]:
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
    # them bounds the number of expensive SequenceMatcher calls for long text.
    anchors.sort(key=lambda item: (item[0], -len(item[2]), item[1]))
    start_scores: dict[int, int] = {}
    for occurrence_count, query_index, token in anchors[:_EXACT_ANCHOR_LIMIT]:
        rarity_weight = max(1, 8 - min(7, occurrence_count))
        for position in positions[token]:
            estimate = position - query_index
            for drift in range(
                -_EXACT_ANCHOR_MAX_DRIFT,
                _EXACT_ANCHOR_MAX_DRIFT + 1,
            ):
                start = max(0, estimate + drift)
                if start not in eligible_starts:
                    continue
                proximity_weight = _EXACT_ANCHOR_MAX_DRIFT + 1 - abs(drift)
                start_scores[start] = start_scores.get(start, 0) + rarity_weight * proximity_weight
    return start_scores


def _token_trigrams(token: str) -> set[str]:
    return {token[index : index + 3] for index in range(len(token) - 2)}


def _fuzzy_anchor_scores(
    query: list[str],
    tokens: list[_Token],
    eligible_starts: set[int],
) -> dict[int, float]:
    query_ngrams = [
        _token_trigrams(token) if token not in _STOPWORDS and len(token) >= 4 else set()
        for token in query
    ]
    query_positions_by_ngram: dict[str, list[int]] = {}
    for query_index, grams in enumerate(query_ngrams):
        for gram in grams:
            query_positions_by_ngram.setdefault(gram, []).append(query_index)

    fuzzy_scores: dict[int, float] = {}
    for position, item in enumerate(tokens):
        if position not in eligible_starts or len(item.normalized) < 4:
            continue
        token_ngrams = _token_trigrams(item.normalized)
        overlap_counts: dict[int, int] = {}
        for gram in token_ngrams:
            for query_index in query_positions_by_ngram.get(gram, ()):
                overlap_counts[query_index] = overlap_counts.get(query_index, 0) + 1
        for query_index, overlap_count in overlap_counts.items():
            grams = query_ngrams[query_index]
            similarity = 2 * overlap_count / (len(grams) + len(token_ngrams))
            if similarity < _FUZZY_MIN_TRIGRAM_SIMILARITY:
                continue
            estimate = position - query_index
            for drift in range(
                -_FUZZY_ANCHOR_MAX_DRIFT,
                _FUZZY_ANCHOR_MAX_DRIFT + 1,
            ):
                start = max(0, estimate + drift)
                if start in eligible_starts:
                    fuzzy_scores[start] = max(
                        fuzzy_scores.get(start, 0.0),
                        similarity - _FUZZY_DRIFT_PENALTY * abs(drift),
                    )
    return fuzzy_scores


def _uniform_sample(values: list[int], sample_count: int) -> list[int]:
    if len(values) <= sample_count:
        return values
    if sample_count == 1:
        return [values[len(values) // 2]]
    if sample_count <= 0:
        return []
    indexes = {
        round(sample * (len(values) - 1) / (sample_count - 1)) for sample in range(sample_count)
    }
    return [values[index] for index in sorted(indexes)]


def _candidate_starts(
    query: list[str],
    tokens: list[_Token],
    *,
    max_starts: int = _MAX_CANDIDATE_STARTS,
    eligible_starts: Iterable[int] | None = None,
) -> list[int]:
    if max_starts <= 0:
        return []
    eligible = (
        list(range(len(tokens)))
        if eligible_starts is None
        else sorted({start for start in eligible_starts if 0 <= start < len(tokens)})
    )
    if not eligible:
        return []
    eligible_set = set(eligible)
    start_scores = _exact_anchor_scores(
        query,
        _token_positions(tokens),
        eligible_set,
    )
    # When Whisper shares no exact token with the script, retain likely starts
    # from cheap character-trigram anchors (common Whisper spelling errors),
    # then fill the remaining fixed budget evenly across the transcript.  The
    # previous query-length stride could evaluate every position for short
    # quotes.
    if not start_scores:
        fuzzy_scores = _fuzzy_anchor_scores(query, tokens, eligible_set)
        fuzzy_budget = max_starts * _FUZZY_BUDGET_NUMERATOR // _FUZZY_BUDGET_DENOMINATOR
        fuzzy_limit = min(len(fuzzy_scores), fuzzy_budget)
        fuzzy_starts = {
            start
            for _score_value, start in sorted(
                ((score, start) for start, score in fuzzy_scores.items()),
                key=lambda item: (-item[0], item[1]),
            )[:fuzzy_limit]
        }
        remaining = max_starts - len(fuzzy_starts)
        uniform_pool = [start for start in eligible if start not in fuzzy_starts]
        uniform_starts = _uniform_sample(uniform_pool, remaining)
        return sorted([*fuzzy_starts, *uniform_starts])
    ranked = sorted(
        ((score, start) for start, score in start_scores.items() if start < len(tokens)),
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


_CHAR_LEVEL_MAX_LENGTH = 480


class _QueryProfile:
    """Per-request constants of `_score`, computed once instead of per span."""

    __slots__ = (
        "tokens",
        "length",
        "chars",
        "counts",
        "content_counts",
        "content_total",
        "char_masks",
    )

    def __init__(self, query: list[str]):
        self.tokens = query
        self.length = len(query)
        self.chars = " ".join(query)
        self.counts = Counter(query)
        content = [token for token in query if token not in _STOPWORDS and len(token) >= 3]
        self.content_total = len(content)
        self.content_counts = Counter(content)
        masks: dict[str, int] = {}
        for position, char in enumerate(self.chars):
            masks[char] = masks.get(char, 0) | (1 << position)
        self.char_masks = masks


def _matching_total(first: list[str] | str, second: list[str] | str) -> int:
    matcher = SequenceMatcher(None, first, second, autojunk=False)
    return sum(block.size for block in matcher.get_matching_blocks())


def _lcs_length(profile: _QueryProfile, text: str) -> int:
    """Longest common subsequence length with the query characters.

    Bit-parallel (Hyyro) LCS. The matching blocks `difflib` finds form a
    common subsequence, so this is an upper bound of its matched characters.
    """
    size = len(profile.chars)
    full = (1 << size) - 1
    masks = profile.char_masks
    vector = full
    for char in text:
        mask = masks.get(char)
        if mask is not None:
            vector = ((vector + (vector & mask)) | (vector & ~mask)) & full
    return size - vector.bit_count()


def _combine_score(
    profile: _QueryProfile,
    span_length: int,
    matched: int,
    content_hits: int,
    char_ratio: float | None,
) -> float:
    """`_score` rebuilt from its parts with the identical float operations.

    ``char_ratio=None`` means "unknown, assume the maximum": the result is then
    an upper bound of the exact score, because every term is monotonic.
    """
    ratio = 2.0 * matched / (profile.length + span_length)
    ordered_coverage = matched / profile.length
    if profile.content_total:
        content_coverage = content_hits / profile.content_total
    else:
        content_coverage = ordered_coverage
    if char_ratio is None:
        char_ratio = 1.0
    return min(
        1.0,
        0.38 * ratio + 0.34 * ordered_coverage + 0.20 * content_coverage + 0.08 * char_ratio,
    )


class _LengthItem:
    """One candidate span length of a start, refined from cheap bound to exact."""

    __slots__ = ("order", "length", "span", "bound", "tier", "matched", "hits", "chars")

    def __init__(
        self,
        order: int,
        length: int,
        span: int,
        bound: float,
        matched: int,
        hits: int,
        chars: int,
    ):
        self.order = order
        self.length = length
        self.span = span
        self.bound = bound
        # 0: multiset bound, 1: exact token-level match, 2: character LCS
        # bound, 3: exact score.
        self.tier = 0
        self.matched = matched
        self.hits = hits
        self.chars = chars


class _SourceSearch:
    """Shared, read-only inputs of every start of one `_search_one` call."""

    __slots__ = (
        "source",
        "tokens",
        "norm",
        "profile",
        "strategy",
        "min_length",
        "max_length",
        "coarse_lengths",
        "length_step",
    )

    def __init__(
        self,
        source: SyncSource,
        tokens: list[_Token],
        profile: _QueryProfile,
        strategy: str,
        min_length: int,
        max_length: int,
    ):
        self.source = source
        self.tokens = tokens
        self.norm = [item.normalized for item in tokens]
        self.profile = profile
        self.strategy = strategy
        self.min_length = min_length
        self.max_length = max_length
        self.coarse_lengths, self.length_step = _length_samples(
            min_length, max_length, profile.length
        )


class _StartSearch:
    """Best span length of one candidate start, resolved lazily.

    The original search scored every sampled length exactly. Here each length
    first gets a cheap upper bound of its score; exact (expensive) scoring
    runs only for lengths whose bound can still beat the best exact score of
    this start, so the chosen length, its score and the tie-breaking (earliest
    length wins, as with the strict ``>`` of the original loop) are identical.
    """

    __slots__ = ("search", "start", "items", "best", "stage", "extra_bound", "seen_spans")

    def __init__(self, search: _SourceSearch, start: int):
        self.search = search
        self.start = start
        self.best: _LengthItem | None = None
        self.stage = 1
        self.seen_spans: set[int] = set()
        self.items: list[_LengthItem] = []
        self.extra_bound = -1.0
        self._init_stage_one()

    def _prefix_stats(self, longest: int) -> list[tuple[int, int, int]]:
        """Per prefix length: (multiset overlap, content hits, char length)."""
        search = self.search
        profile = search.profile
        counts = profile.counts
        content_counts = profile.content_counts
        norm = search.norm
        used: dict[str, int] = {}
        seen: set[str] = set()
        overlap = 0
        hits = 0
        chars = -1
        stats: list[tuple[int, int, int]] = [(0, 0, 0)]
        for position in range(self.start, self.start + longest):
            token = norm[position]
            chars += len(token) + 1
            taken = used.get(token, 0)
            if taken < counts.get(token, 0):
                used[token] = taken + 1
                overlap += 1
            if token not in seen:
                seen.add(token)
                hits += content_counts.get(token, 0)
            stats.append((overlap, hits, chars))
        return stats

    def _bound(self, span: int, overlap: int, hits: int, chars: int) -> float:
        profile = self.search.profile
        if max(len(profile.chars), chars) <= _CHAR_LEVEL_MAX_LENGTH:
            return _combine_score(profile, span, overlap, hits, None)
        # Long text: `_score` reuses the token ratio in place of the char ratio.
        ratio = 2.0 * overlap / (profile.length + span)
        return _combine_score(profile, span, overlap, hits, ratio)

    def _make_item(self, order: int, length: int, stats: list[tuple[int, int, int]]) -> _LengthItem:
        span = min(length, len(self.search.norm) - self.start)
        overlap, hits, chars = stats[span]
        return _LengthItem(
            order, length, span, self._bound(span, overlap, hits, chars), overlap, hits, chars
        )

    def _init_stage_one(self) -> None:
        search = self.search
        longest = min(search.max_length, len(search.norm) - self.start)
        stats = self._prefix_stats(longest)
        seen_spans: set[int] = set()
        for order, length in enumerate(search.coarse_lengths):
            span = min(length, len(search.norm) - self.start)
            if span in seen_spans:
                # Truncated at the transcript end: same span, same score,
                # and an equal score never replaces the earlier length.
                continue
            seen_spans.add(span)
            self.items.append(self._make_item(order, length, stats))
        self.seen_spans = seen_spans
        if search.length_step > 1:
            # Any non-sampled length may become a refinement candidate.
            coarse = set(search.coarse_lengths)
            for length in range(search.min_length, search.max_length + 1):
                if length in coarse:
                    continue
                span = min(length, len(search.norm) - self.start)
                if span in seen_spans:
                    continue
                overlap, hits, chars = stats[span]
                self.extra_bound = max(self.extra_bound, self._bound(span, overlap, hits, chars))

    @property
    def done(self) -> bool:
        return self.stage == 0

    @property
    def bound(self) -> float:
        """Upper bound of the final confidence of this start."""
        value = self.best.bound if self.best is not None else -1.0
        for item in self.items:
            if item.bound > value:
                value = item.bound
        if self.stage == 1 and self.extra_bound > value:
            value = self.extra_bound
        return value

    def _beaten(self, item: _LengthItem) -> bool:
        best = self.best
        if best is None:
            return False
        return item.bound < best.bound or (item.bound == best.bound and item.order > best.order)

    def _prune(self) -> None:
        if self.best is not None:
            self.items = [item for item in self.items if not self._beaten(item)]

    def _exact_item(self, item: _LengthItem) -> None:
        search = self.search
        profile = search.profile
        span_tokens = search.norm[self.start : self.start + item.span]
        if item.tier == 0:
            item.matched = _matching_total(profile.tokens, span_tokens)
            item.tier = 1
            item.bound = self._bound(item.span, item.matched, item.hits, item.chars)
            return
        ratio = 2.0 * item.matched / (profile.length + item.span)
        if max(len(profile.chars), item.chars) <= _CHAR_LEVEL_MAX_LENGTH:
            joined = " ".join(span_tokens)
            if item.tier == 1:
                lcs = _lcs_length(profile, joined)
                char_bound = 2.0 * lcs / (len(profile.chars) + item.chars)
                item.bound = _combine_score(profile, item.span, item.matched, item.hits, char_bound)
                item.tier = 2
                return
            char_ratio = SequenceMatcher(None, profile.chars, joined, autojunk=False).ratio()
        else:
            char_ratio = ratio
        item.bound = _combine_score(profile, item.span, item.matched, item.hits, char_ratio)
        item.tier = 3

    def refine(self) -> None:
        """Advance the most promising length of this start by one tier."""
        item = max(self.items, key=lambda value: (value.bound, -value.order), default=None)
        if item is not None:
            self._exact_item(item)
            if item.tier == 3:
                self.items.remove(item)
                best = self.best
                if best is None or item.bound > best.bound:
                    self.best = item
                elif item.bound == best.bound and item.order < best.order:
                    self.best = item
                self._prune()
        if self.items:
            return
        if self.stage == 1 and self.search.length_step > 1 and self.best is not None:
            self._start_refinement()
        else:
            self.stage = 0

    def _start_refinement(self) -> None:
        search = self.search
        assert self.best is not None
        coarse = set(search.coarse_lengths)
        refine_start = max(search.min_length, self.best.length - search.length_step)
        refine_end = min(search.max_length, self.best.length + search.length_step)
        self.stage = 2
        self.extra_bound = -1.0
        lengths = [
            length
            for length in range(refine_start, refine_end + 1)
            if length not in coarse
            and min(length, len(search.norm) - self.start) not in self.seen_spans
        ]
        if not lengths:
            self.stage = 0
            return
        longest = min(max(lengths), len(search.norm) - self.start)
        stats = self._prefix_stats(longest)
        # Refinement lengths follow every sampled one, and only a strictly
        # better score replaces the best, so they never win ties.
        base = len(search.coarse_lengths)
        seen: set[int] = set()
        for offset, length in enumerate(lengths):
            span = min(length, len(search.norm) - self.start)
            if span in seen:
                continue
            seen.add(span)
            self.items.append(self._make_item(base + offset, length, stats))
        self._prune()
        if not self.items:
            self.stage = 0

    def candidate(self) -> _Candidate:
        search = self.search
        best = self.best
        assert best is not None
        span = search.tokens[self.start : self.start + best.span]
        source = search.source
        return _Candidate(
            source_id=source.id,
            start=max(0.0, span[0].word.start + source.guide_offset_sec),
            end=span[-1].word.end + source.guide_offset_sec,
            confidence=best.bound,
            text=" ".join(item.word.word for item in span),
            strategy=search.strategy,
            max_gap_sec=max(
                (max(0.0, right.word.start - left.word.end) for left, right in zip(span, span[1:])),
                default=0.0,
            ),
            max_word_duration_sec=max(item.word.end - item.word.start for item in span),
        )


def _prepare_search(
    source: SyncSource,
    index: WordIndex,
    profile: _QueryProfile,
    approx: float | None,
    window: float | None,
    strategy: str,
    timed_tokens: list[_Token] | None = None,
    max_starts: int = _MAX_CANDIDATE_STARTS,
    exclude_word_time_range: tuple[float, float] | None = None,
) -> tuple[_SourceSearch, list[int]] | None:
    """Select the transcript window and candidate starts of one source."""
    query = profile.tokens
    all_tokens = timed_tokens if timed_tokens is not None else _timed_tokens(index)
    if approx is not None and window is not None:
        guide_approx = approx - source.guide_offset_sec
        lower, upper = guide_approx - window, guide_approx + window
        tokens = [
            item for item in all_tokens if item.word.end >= lower and item.word.start <= upper
        ]
    else:
        tokens = all_tokens
    if not tokens:
        return None

    min_length = max(1, len(query) - max(3, len(query) // 3))
    if len(tokens) < min_length or max_starts <= 0:
        return None
    max_length = min(len(tokens), len(query) + max(5, len(query) // 2))
    latest_start = len(tokens) - min_length
    eligible_starts: Iterable[int] = range(latest_start + 1)
    if exclude_word_time_range is not None:
        excluded_lower, excluded_upper = exclude_word_time_range
        eligible_starts = [
            start
            for start in eligible_starts
            if not (
                tokens[start].word.end >= excluded_lower
                and tokens[start].word.start <= excluded_upper
            )
        ]
    starts = _candidate_starts(
        query,
        tokens,
        max_starts=max_starts,
        eligible_starts=eligible_starts,
    )
    search = _SourceSearch(source, tokens, profile, strategy, min_length, max_length)
    return search, starts


def _search_one(
    source: SyncSource,
    index: WordIndex,
    query: list[str],
    approx: float | None,
    window: float | None,
    strategy: str,
    timed_tokens: list[_Token] | None = None,
    max_starts: int = _MAX_CANDIDATE_STARTS,
    exclude_word_time_range: tuple[float, float] | None = None,
) -> list[_Candidate]:
    """Best span of every candidate start of one source (exhaustive)."""
    prepared = _prepare_search(
        source,
        index,
        _QueryProfile(query),
        approx,
        window,
        strategy,
        timed_tokens,
        max_starts,
        exclude_word_time_range,
    )
    if prepared is None:
        return []
    search, starts = prepared
    candidates: list[_Candidate] = []
    for start in starts:
        state = _StartSearch(search, start)
        while not state.done:
            state.refine()
        candidates.append(state.candidate())
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
            same_occurrence = shorter > 0 and intersection / shorter >= _DUPLICATE_MIN_OVERLAP_RATIO
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
    # A long head handle must never manufacture a valid-looking range when
    # the recognised speech itself lies beyond the registered source.
    if source.duration_sec > 0 and candidate.end > source.duration_sec + 0.001:
        return False
    start, end = _padded_range(candidate, source, config)
    return end - start > 0.001


class _BestFirstSearch:
    """Scores candidate starts of all sources best-bound-first, stopping early.

    The matcher only reads the first ``needed`` entries of the ranked,
    de-duplicated candidate list (best, runner-up and alternatives). Ranking
    and de-duplication walk the list in descending confidence and each entry's
    fate depends only on entries above it, so the leading entries whose
    confidence is strictly above the upper bound of every start not yet scored
    are exactly the leading entries of the exhaustive result. Searching stops
    as soon as the ``needed``-th entry clears that bound. Entries below the
    cut are simply never scored; they cannot reach the output.
    """

    def __init__(
        self,
        sources: dict[str, SyncSource],
        config: MatcherConfig,
        needed: int,
    ):
        self._sources = sources
        self._config = config
        self._needed = needed
        self._heap: list[tuple[float, int, int, int, _StartSearch]] = []
        self._sequence = 0
        # (source order, start, candidate) per phase, in scoring-independent order.
        self._completed: dict[int, list[tuple[int, int, _Candidate]]] = {1: [], 2: []}
        self._ranked: list[_Candidate] | None = None
        self._ranked_phase = 1
        self._cut: float | None = None

    def add(self, phase: int, source_order: int, search: _SourceSearch, starts: list[int]) -> None:
        for start in starts:
            state = _StartSearch(search, start)
            self._push(phase, source_order, state)

    def _push(self, phase: int, source_order: int, state: _StartSearch) -> None:
        self._sequence += 1
        heapq.heappush(self._heap, (-state.bound, self._sequence, phase, source_order, state))

    @property
    def unscored_bound(self) -> float:
        return -self._heap[0][0] if self._heap else -1.0

    def _usable_ranked(self, candidates: Iterable[_Candidate]) -> list[_Candidate]:
        return [
            item
            for item in _deduplicate(candidates)
            if _has_usable_range(item, self._sources[item.source_id], self._config)
        ]

    def _phase_candidates(self, phase: int) -> list[_Candidate]:
        return [item for _order, _start, item in sorted(self._completed[phase], key=_by_position)]

    def ranked(self, phase: int) -> list[_Candidate]:
        """Ranked usable candidates scored so far (``phase`` 2 = merged with fallback)."""
        if self._ranked is None or self._ranked_phase != phase:
            local = self._usable_ranked(self._phase_candidates(1))
            if phase == 1:
                self._ranked = local
            else:
                self._ranked = self._usable_ranked([*local, *self._phase_candidates(2)])
            self._ranked_phase = phase
            self._cut = (
                self._ranked[self._needed - 1].confidence
                if len(self._ranked) >= self._needed
                else None
            )
        return self._ranked

    def run(self, phase: int, finished: Callable[[list[_Candidate], float], bool]) -> None:
        """Score starts until ``finished(ranked, unscored_bound)`` or none are left."""
        while self._heap:
            if finished(self.ranked(phase), self.unscored_bound):
                return
            _bound, _sequence, state_phase, source_order, state = heapq.heappop(self._heap)
            if state.done:
                candidate = state.candidate()
                self._completed[state_phase].append((source_order, state.start, candidate))
                # A candidate under the current cut cannot change the leading entries.
                if self._cut is None or candidate.confidence >= self._cut:
                    self._ranked = None
                continue
            state.refine()
            self._push(state_phase, source_order, state)

    def enough(self, ranked: list[_Candidate], unscored_bound: float) -> bool:
        return len(ranked) >= self._needed and ranked[self._needed - 1].confidence > unscored_bound


def _by_position(entry: tuple[int, int, _Candidate]) -> tuple[int, int]:
    return entry[0], entry[1]


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
            source_id: _timed_tokens(index) for source_id, index in self.indexes.items()
        }
        self.speaker_tokens = {
            source_id: [tokens for name in source.speaker_names if (tokens := normalize_text(name))]
            for source_id, source in self.sources.items()
        }
        self.config = config or MatcherConfig()

    def _eligible_sources(
        self, request: AlignmentRequest
    ) -> tuple[list[SyncSource], list[dict[str, Any]]]:
        if request.source_ids:
            return (
                [
                    self.sources[source_id]
                    for source_id in request.source_ids
                    if source_id in self.sources and source_id in self.indexes
                ],
                [],
            )
        available = [source for source in self.sources.values() if source.id in self.indexes]
        # Role descriptions after a comma are not part of a person's name.
        speaker_tokens = [
            token
            for token in normalize_text(request.speaker.split(",", 1)[0])
            if not token.isdigit()
        ]
        if not speaker_tokens:
            return available, []
        universal = [source for source in available if source.all_speakers]

        def source_speaker_tokens(source: SyncSource) -> list[list[str]]:
            return self.speaker_tokens.get(source.id, [])

        def with_universal(values: list[SyncSource]) -> list[SyncSource]:
            by_id = {source.id: source for source in [*values, *universal]}
            return [by_id[source.id] for source in available if source.id in by_id]

        def prefer_rendered_viewings(values: list[SyncSource]) -> list[SyncSource]:
            viewings = [source for source in values if source.source_kind == "review_media"]
            return viewings or values

        exact = [
            source
            for source in available
            if any(tokens == speaker_tokens for tokens in source_speaker_tokens(source))
        ]
        if exact:
            selected = with_universal(prefer_rendered_viewings(exact))
        else:
            surname = speaker_tokens[-1]

            def named_alias_match(tokens: list[str]) -> bool:
                if not tokens:
                    return False
                # Source labels may contain a date prefix or a setting after
                # the surname ("40726 Стычкин", "Сторожева дача").
                words = [token for token in tokens if not token.isdigit()]
                if not words:
                    return False
                if words[-1] == surname:
                    return (
                        len(speaker_tokens) == 1 or len(words) == 1 or words[0] == speaker_tokens[0]
                    )
                return (
                    words[0] == surname
                    and len(words) == 2
                    and (
                        words[1] in {"дач", "дом", "интервью"}
                        or surname in {"отец", "мать", "мам", "пап", "брат", "сестр"}
                    )
                )

            surname_matches = [
                source
                for source in available
                if any(named_alias_match(tokens) for tokens in source_speaker_tokens(source))
            ]
            if surname_matches:
                selected = with_universal(prefer_rendered_viewings(surname_matches))
            else:
                # An explicitly registered short alias can be a stage name
                # or familiar given name, e.g. Луэлла for Луэлла Краснощекова.
                registered_aliases = [
                    source
                    for source in available
                    if len(speaker_tokens) > 1
                    and any(
                        normalize_text(alias) == speaker_tokens[:1] for alias in source.speakers
                    )
                ]
                query_words = normalize_text(request.speaker)
                titled_sources = [
                    source
                    for source in available
                    if any(
                        len(alias_tokens := normalize_text(alias)) > 1
                        and any(
                            query_words[index : index + len(alias_tokens)] == alias_tokens
                            for index in range(len(query_words) - len(alias_tokens) + 1)
                        )
                        for alias in source.speaker_names
                    )
                ]
                if registered_aliases or titled_sources:
                    selected = with_universal(
                        prefer_rendered_viewings(registered_aliases or titled_sources)
                    )
                else:
                    # Inflected full Russian names may differ after the text
                    # stemmer (Иванов / Иванова). Require both name parts and a
                    # strong whole-name match; never fuzzy-match a lone word.
                    inflected_full = [
                        source
                        for source in available
                        if any(
                            len(tokens) > 1
                            and len(speaker_tokens) > 1
                            and SequenceMatcher(
                                None, " ".join(speaker_tokens), " ".join(tokens), autojunk=False
                            ).ratio()
                            >= 0.9
                            for tokens in source_speaker_tokens(source)
                        )
                    ]
                    if inflected_full:
                        selected = with_universal(prefer_rendered_viewings(inflected_full))
                    else:
                        # Sources without speaker metadata remain searchable.
                        selected = with_universal(
                            prefer_rendered_viewings(
                                [
                                    source
                                    for source in available
                                    if not source_speaker_tokens(source)
                                ]
                            )
                        )
        selected_ids = {source.id for source in selected}
        # Sources excluded purely because their registered speaker names did
        # not match the request are surfaced so the UI can explain a
        # not_found result instead of leaving the operator to guess.
        filtered = [
            {"source_id": source.id, "source_speakers": list(source.speaker_names)}
            for source in available
            if source.id not in selected_ids and source_speaker_tokens(source)
        ]
        return selected, filtered

    def match(self, request: AlignmentRequest) -> SyncMatch:
        query = normalize_text(request.text)
        sources, speaker_filtered = self._eligible_sources(request)

        def _metadata(base: dict[str, Any]) -> dict[str, Any]:
            if speaker_filtered:
                return {**base, "speaker_filtered_sources": speaker_filtered}
            return base

        if len(query) < 2 or not sources:
            reason = "query_too_short" if len(query) < 2 else "no_eligible_source"
            return SyncMatch(
                segment_id=request.segment_id,
                query_text=request.text,
                speaker=request.speaker,
                approximate_timecode=str(request.approximate_timecode or ""),
                status="not_found",
                strategy="insufficient_input",
                metadata=_metadata({"unmatched_reason": reason}),
            )

        profile = _QueryProfile(query)
        needed = max(2, self.config.max_alternatives + 1)
        searcher = _BestFirstSearch(self.sources, self.config, needed)
        local_counts: dict[str, int] = {}
        local_word_ranges: dict[str, tuple[float, float]] = {}
        has_approx = request.approximate_timecode not in (None, "")
        for order, source in enumerate(sources):
            approx = _approx_for_source(request, source)
            window = self.config.search_window_sec if approx is not None else None
            prepared = _prepare_search(
                source,
                self.indexes[source.id],
                profile,
                approx,
                window,
                "near_timecode" if approx is not None else "full_transcript",
                self.timed_tokens[source.id],
                max_starts=(
                    _MAX_LOCAL_CANDIDATE_STARTS if approx is not None else _MAX_CANDIDATE_STARTS
                ),
            )
            # Every start yields exactly one candidate.
            local_counts[source.id] = len(prepared[1]) if prepared else 0
            if prepared:
                searcher.add(1, order, *prepared)
            if approx is not None and window is not None:
                guide_approx = approx - source.guide_offset_sec
                local_word_ranges[source.id] = (
                    guide_approx - window,
                    guide_approx + window,
                )

        needs_fallback = False
        if has_approx:
            # Whether the global fallback runs depends only on the best local
            # candidate, so score locally until that is known.
            trigger = self.config.fallback_trigger

            def fallback_decided(ranked: list[_Candidate], unscored: float) -> bool:
                nonlocal needs_fallback
                if ranked and ranked[0].confidence > unscored:
                    needs_fallback = ranked[0].confidence < trigger
                    return True
                if unscored < trigger:
                    needs_fallback = not ranked or ranked[0].confidence < trigger
                    return True
                return False

            searcher.run(1, fallback_decided)
            if not searcher.unscored_bound >= 0:
                fallback_decided(searcher.ranked(1), -1.0)

        if needs_fallback:
            for order, source in enumerate(sources):
                remaining_starts = max(
                    0,
                    _MAX_CANDIDATE_STARTS - local_counts.get(source.id, 0),
                )
                prepared = _prepare_search(
                    source,
                    self.indexes[source.id],
                    profile,
                    None,
                    None,
                    "fallback_full",
                    self.timed_tokens[source.id],
                    max_starts=remaining_starts,
                    exclude_word_time_range=local_word_ranges.get(source.id),
                )
                if prepared:
                    searcher.add(2, order, *prepared)
        phase = 2 if needs_fallback else 1
        searcher.run(phase, searcher.enough)
        candidates = searcher.ranked(phase)

        if not candidates or candidates[0].confidence < self.config.review_threshold:
            reason = "no_candidate" if not candidates else "below_review_threshold"
            alternatives: list[SyncAlternative] = []
            for item in candidates[: self.config.max_alternatives]:
                item_in, item_out = _padded_range(item, self.sources[item.source_id], self.config)
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
                metadata=_metadata({"unmatched_reason": reason}),
            )

        best = candidates[0]
        other = candidates[1] if len(candidates) > 1 else None
        ambiguous = bool(
            other and best.confidence - other.confidence < self.config.ambiguity_margin
        )
        # Negations and changed numbers can flip the meaning of an otherwise
        # highly similar sentence; such matches must never be auto-assembled.
        semantic_flags = _semantic_gate_flags(request.text, best.text)
        # Words glued across a long silence (e.g. two halves of the phrase
        # minutes apart) score highly but are not a continuous quote.
        # A single word that lasts longer than the same threshold is just as
        # impossible and hides a pause: interpolated cloud timestamps spread a
        # segment evenly over words (gap is always ~0), and Whisper may stretch
        # a word's end over the silence after it.  Applied to every precision.
        gap_flags = (
            ["large_time_gap"]
            if best.max_gap_sec > self.config.max_internal_gap_sec
            or best.max_word_duration_sec > self.config.max_internal_gap_sec
            else []
        )
        # Text similarity (confidence) says nothing about how trustworthy the word
        # boundaries are: segment-spread (interpolated) cloud times that no local
        # retiming refined can be seconds off even for an exact quote.
        timestamp_precision = str(
            self.indexes[best.source_id].metadata.get("timestamp_precision") or ""
        )
        timing_flags = _timing_gate_flags(self.indexes[best.source_id])
        # A strong global fallback is useful, but its disagreement with the
        # supplied timecode must remain visible to an editor.
        text_auto = (
            best.confidence >= self.config.auto_threshold
            and not ambiguous
            and best.strategy != "fallback_full"
            and not semantic_flags
            and not gap_flags
        )
        auto = text_auto and not timing_flags
        status: MatchStatus = "auto_matched" if auto else "needs_review"
        alternatives = []
        for item in candidates[1 : 1 + self.config.max_alternatives]:
            item_in, item_out = _padded_range(item, self.sources[item.source_id], self.config)
            alternatives.append(
                SyncAlternative(
                    item.source_id,
                    item_in,
                    item_out,
                    round(item.confidence, 4),
                    item.text,
                )
            )
        source_in, source_out = _padded_range(best, self.sources[best.source_id], self.config)
        timing_metadata: dict[str, object] = {
            "timing_reliable": not timing_flags,
            "timing_flags": timing_flags,
        }
        if text_auto and timing_flags:
            timing_metadata["review_reason"] = timing_flags[0]
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
            metadata=_metadata(
                {
                    "ambiguous": ambiguous,
                    "timecode_mode": request.timecode_mode,
                    "semantic_flags": semantic_flags,
                    "gap_flags": gap_flags,
                    "max_internal_gap_sec": round(best.max_gap_sec, 4),
                    "max_word_duration_sec": round(best.max_word_duration_sec, 4),
                    "max_internal_gap_threshold_sec": self.config.max_internal_gap_sec,
                    "timestamp_precision": timestamp_precision,
                    **timing_metadata,
                    "speech_in_sec": round(best.start, 4),
                    "speech_out_sec": round(best.end, 4),
                    "head_padding_sec": self.config.head_padding_sec,
                    "tail_padding_sec": self.config.tail_padding_sec,
                }
            ),
        )
