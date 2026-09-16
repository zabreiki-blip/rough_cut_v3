"""Russian-aware fuzzy alignment of scenario quotes to timed transcripts."""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
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
        return []

    min_length = max(1, len(query) - max(3, len(query) // 3))
    if len(tokens) < min_length or max_starts <= 0:
        return []
    max_length = min(len(tokens), len(query) + max(5, len(query) // 2))
    coarse_lengths, length_step = _length_samples(min_length, max_length, len(query))
    candidates: list[_Candidate] = []
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
    for start in _candidate_starts(
        query,
        tokens,
        max_starts=max_starts,
        eligible_starts=eligible_starts,
    ):
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
        speaker_tokens = normalize_text(request.speaker)
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
            query_name = set(speaker_tokens)
            fuzzy = [
                source
                for source in available
                if any(
                    query_name.intersection(tokens)
                    or SequenceMatcher(
                        None, " ".join(speaker_tokens), " ".join(tokens), autojunk=False
                    ).ratio()
                    >= 0.72
                    for tokens in source_speaker_tokens(source)
                )
            ]
            if fuzzy:
                selected = with_universal(prefer_rendered_viewings(fuzzy))
            else:
                # A source registered without speaker metadata remains
                # searchable, but a clearly different named speaker is not
                # searched in this interview.
                selected = with_universal(
                    prefer_rendered_viewings(
                        [source for source in available if not source_speaker_tokens(source)]
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
            return SyncMatch(
                segment_id=request.segment_id,
                query_text=request.text,
                speaker=request.speaker,
                approximate_timecode=str(request.approximate_timecode or ""),
                status="not_found",
                strategy="insufficient_input",
                metadata=_metadata({}),
            )

        local: list[_Candidate] = []
        local_counts: dict[str, int] = {}
        local_word_ranges: dict[str, tuple[float, float]] = {}
        has_approx = request.approximate_timecode not in (None, "")
        for source in sources:
            approx = _approx_for_source(request, source)
            window = self.config.search_window_sec if approx is not None else None
            source_candidates = _search_one(
                source,
                self.indexes[source.id],
                query,
                approx,
                window,
                "near_timecode" if approx is not None else "full_transcript",
                self.timed_tokens[source.id],
                max_starts=(
                    _MAX_LOCAL_CANDIDATE_STARTS if approx is not None else _MAX_CANDIDATE_STARTS
                ),
            )
            local.extend(source_candidates)
            local_counts[source.id] = len(source_candidates)
            if approx is not None and window is not None:
                guide_approx = approx - source.guide_offset_sec
                local_word_ranges[source.id] = (
                    guide_approx - window,
                    guide_approx + window,
                )
        candidates = [
            item
            for item in _deduplicate(local)
            if _has_usable_range(item, self.sources[item.source_id], self.config)
        ]

        if has_approx and (
            not candidates or candidates[0].confidence < self.config.fallback_trigger
        ):
            fallback: list[_Candidate] = []
            for source in sources:
                remaining_starts = max(
                    0,
                    _MAX_CANDIDATE_STARTS - local_counts.get(source.id, 0),
                )
                fallback.extend(
                    _search_one(
                        source,
                        self.indexes[source.id],
                        query,
                        None,
                        None,
                        "fallback_full",
                        self.timed_tokens[source.id],
                        max_starts=remaining_starts,
                        exclude_word_time_range=local_word_ranges.get(source.id),
                    )
                )
            candidates = [
                item
                for item in _deduplicate([*candidates, *fallback])
                if _has_usable_range(item, self.sources[item.source_id], self.config)
            ]

        if not candidates or candidates[0].confidence < self.config.review_threshold:
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
                metadata=_metadata({}),
            )

        best = candidates[0]
        other = candidates[1] if len(candidates) > 1 else None
        ambiguous = bool(
            other and best.confidence - other.confidence < self.config.ambiguity_margin
        )
        # Negations and changed numbers can flip the meaning of an otherwise
        # highly similar sentence; such matches must never be auto-assembled.
        semantic_flags = _semantic_gate_flags(request.text, best.text)
        # A strong global fallback is useful, but its disagreement with the
        # supplied timecode must remain visible to an editor.
        auto = (
            best.confidence >= self.config.auto_threshold
            and not ambiguous
            and best.strategy != "fallback_full"
            and not semantic_flags
        )
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
                    "speech_in_sec": round(best.start, 4),
                    "speech_out_sec": round(best.end, 4),
                    "head_padding_sec": self.config.head_padding_sec,
                    "tail_padding_sec": self.config.tail_padding_sec,
                }
            ),
        )
