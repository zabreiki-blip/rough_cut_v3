"""Pure word re-timing: transfer real word timestamps onto cloud transcript words.

Cloud ASR transcripts carry interpolated word times (words evenly share the
cloud utterance), so the error is several seconds (median ~7 s, up to 100 s).
A local ASR (e.g. Whisper with word timestamps) has accurate times but a worse
text.  ``retime_words`` aligns both token streams and gives every cloud word
the time of its local counterpart ("anchor"); the remaining words are
interpolated between anchors.  The module does no I/O and is deterministic.

Numbers are handled without extra dependencies: digit tokens and runs of
Russian numeral words ("тысяча девятьсот сорок пятого", "двадцать седьмого")
are collapsed into a single ``#num`` token on both sides, so "1945" aligns with
its spoken form wherever the neighbours align (a ``fuzzy`` match).
"""

from __future__ import annotations

import math
import re
from bisect import bisect_left, insort
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from functools import lru_cache
from itertools import groupby
from typing import Literal

from stage2.sync_models import WordTimestamp

RetimeKind = Literal["exact", "fuzzy", "interp", "unchanged"]

_NUM = "#num"


@dataclass(frozen=True)
class RetimeConfig:
    # An anchor is dropped when its shift differs from the local drift by more.
    drift_tolerance_sec: float = 6.0
    # Local drift = median shift of surviving anchors within +-N anchors.  An anchor
    # is kept when it agrees with the wide window (stable drift) or with the narrow
    # one (steep ramps, where cloud words are crammed into a short stretch).
    drift_window_anchors: int = 40
    drift_narrow_window_anchors: int = 10
    drift_iterations: int = 5
    # Safety net only: shifts larger than this are never trusted.
    absolute_max_shift_sec: float = 180.0
    min_word_duration_sec: float = 0.02
    fuzzy_threshold: float = 0.7
    # replace-blocks up to this size (per side) are refined with Needleman-Wunsch.
    max_fuzzy_block: int = 80
    # Report anchor-free stretches longer than this.
    gap_report_sec: float = 10.0
    # Quality gate (see ``quality_rejection``): a retime that does not pass it must not
    # be applied, because a few false anchors would rewrite the time of the whole file.
    min_anchor_ratio: float = 0.3
    # Absolute floor: at least min(min_anchor_count, min_anchor_ratio * N) anchors, but
    # never fewer than 3 (or N for a tiny file).
    min_anchor_count: int = 50
    # Share of raw anchors that the filters (cap/drift/monotone) may throw away.
    max_dropped_fraction: float = 0.5
    # local / cloud span and word-count ratios must lie in [1/x, x].
    max_size_ratio: float = 2.0
    # 90th percentile of |new - old| start of the words (real data: ~20 s, max 107 s).
    max_shift_p90_sec: float = 120.0


@dataclass(frozen=True)
class RetimeMetrics:
    total_words: int
    anchor_count: int
    exact_count: int
    fuzzy_count: int
    interp_count: int
    unchanged_count: int
    anchor_ratio: float
    shift_median_sec: float
    shift_p90_sec: float
    shift_max_sec: float
    # (start, end) in new time of stretches without anchors longer than gap_report_sec.
    anchor_gaps: list[tuple[float, float]] = field(default_factory=list)
    dropped_by_cap: int = 0
    dropped_by_drift: int = 0
    dropped_non_monotone: int = 0
    # local span / cloud span and local words / cloud words (1.0 = same size).
    duration_ratio: float = 1.0
    word_count_ratio: float = 1.0


@dataclass(frozen=True)
class RetimeResult:
    words: list[WordTimestamp]
    kinds: list[RetimeKind]
    metrics: RetimeMetrics


# --------------------------------------------------------------------------
# Tokenization
# --------------------------------------------------------------------------

_SPLIT_DASH = re.compile(r"[‐‑–—-]+")
_TOKEN = re.compile(r"[0-9a-zа-я]+")
_DIGIT_SUFFIXES = frozenset(
    "й я е го му м х ми ый ой ого ому ым ом ая ую ое ые их ыми ам ами ах ов ий ей".split()
)

_ORD_ENDINGS = (
    "ый ий ой ого ому ым им ом ая яя ую юю ое ее ые ие ых их ыми ими ья ье ьего ьему ьим ьем "
    "ьей ью ьи ьих ьими"
).split()
_ORD_STEMS = "перв втор трет четверт пят шест седьм восьм девят десят сот сороков".split()
_NUM_EXACT = frozenset(
    "ноль нуль один одна одно одну одной одного одному одним одном одни одних два две двух двум "
    "двумя три трех трем тремя четыре четырех четырем четырьмя пять пяти пятью шесть шести "
    "шестью семь семи семью восемь восьми восемью девять девяти девятью десять десяти десятью "
    "сто ста стам сотни сотен полтора полторы".split()
) | {stem + end for stem in _ORD_STEMS for end in _ORD_ENDINGS}
_NUM_STEMS = (
    "одиннадцат двенадцат тринадцат четырнадцат пятнадцат шестнадцат семнадцат восемнадцат "
    "девятнадцат двадцат тридцат сорок пятидесят пятьдесят шестидесят шестьдесят семидесят "
    "семьдесят восьмидесят восемьдесят девяност двухсот трехсот четырехсот пятисот шестисот "
    "семисот восьмисот девятисот двести триста четыреста пятьсот шестьсот семьсот восемьсот "
    "девятьсот тысяч миллион миллиард двухтысяч"
).split()


@lru_cache(maxsize=65536)
def _is_numeral(token: str) -> bool:
    if token[0].isdigit():
        return True
    if token in _NUM_EXACT:
        return True
    return any(token.startswith(stem) and len(token) - len(stem) <= 5 for stem in _NUM_STEMS)


def _word_tokens(text: str) -> list[str]:
    cleaned = _SPLIT_DASH.sub(" ", text.lower().replace("ё", "е"))
    tokens = _TOKEN.findall(cleaned)
    result: list[str] = []
    for token in tokens:
        # "27-го", "1945-й": the ordinal suffix belongs to the number.
        if result and result[-1].isdigit() and token in _DIGIT_SUFFIXES:
            continue
        result.append(token)
    return result


@dataclass
class _Token:
    text: str
    first_word: int
    last_word: int
    t0: float = 0.0
    t1: float = 0.0


def _flatten(words: list[WordTimestamp], *, timed: bool) -> tuple[list[_Token], list[int]]:
    """Tokens with numeral runs collapsed, plus the number of tokens touching each word."""
    raw: list[_Token] = []
    for index, word in enumerate(words):
        tokens = _word_tokens(word.word)
        total = sum(len(token) for token in tokens)
        consumed = 0
        for token in tokens:
            t0 = t1 = 0.0
            if timed:
                span = word.end - word.start
                t0 = word.start + span * consumed / total
                t1 = word.start + span * (consumed + len(token)) / total
            consumed += len(token)
            raw.append(_Token(token, index, index, t0, t1))
    merged: list[_Token] = []
    for is_num, group in groupby(raw, key=lambda token: _is_numeral(token.text)):
        items = list(group)
        if is_num:
            merged.append(
                _Token(_NUM, items[0].first_word, items[-1].last_word, items[0].t0, items[-1].t1)
            )
        else:
            merged.extend(items)
    touching = [0] * len(words)
    for merged_token in merged:
        for word_index in range(merged_token.first_word, merged_token.last_word + 1):
            touching[word_index] += 1
    return merged, touching


# --------------------------------------------------------------------------
# Alignment
# --------------------------------------------------------------------------


@lru_cache(maxsize=65536)
def _similarity(a: str, b: str) -> float:
    if a == b:
        return 1.0
    if min(len(a), len(b)) < 3 or a == _NUM or b == _NUM:
        return 0.0
    if 2.0 * min(len(a), len(b)) / (len(a) + len(b)) < 0.5:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def _fuzzy_block(a: list[str], b: list[str], threshold: float) -> list[tuple[int, int]]:
    """Needleman-Wunsch over a replace block; returns matched (i, j) pairs."""
    n, m = len(a), len(b)
    gap = -0.4
    score = [[0.0] * (m + 1) for _ in range(n + 1)]
    back = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        score[i][0] = gap * i
        back[i][0] = 1
    for j in range(1, m + 1):
        score[0][j] = gap * j
        back[0][j] = 2
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            sim = _similarity(a[i - 1], b[j - 1])
            diag = score[i - 1][j - 1] + (sim if sim >= threshold else -0.6)
            up = score[i - 1][j] + gap
            left = score[i][j - 1] + gap
            best = max(diag, up, left)
            score[i][j] = best
            back[i][j] = 0 if best == diag else (1 if best == up else 2)
    i, j = n, m
    pairs: list[tuple[int, int]] = []
    while i > 0 or j > 0:
        step = back[i][j]
        if i > 0 and j > 0 and step == 0:
            if _similarity(a[i - 1], b[j - 1]) >= threshold:
                pairs.append((i - 1, j - 1))
            i -= 1
            j -= 1
        elif i > 0 and (step == 1 or j == 0):
            i -= 1
        else:
            j -= 1
    pairs.reverse()
    return pairs


@dataclass
class _Anchor:
    word: int
    start: float
    end: float
    exact: bool


def _find_anchors(
    cloud: list[WordTimestamp], local: list[WordTimestamp], cfg: RetimeConfig
) -> list[_Anchor]:
    cloud_tokens, cloud_touching = _flatten(cloud, timed=False)
    local_tokens, _ = _flatten(local, timed=True)
    cloud_texts = [token.text for token in cloud_tokens]
    local_texts = [token.text for token in local_tokens]

    pairs: list[tuple[int, int, bool]] = []
    matcher = SequenceMatcher(None, cloud_texts, local_texts, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            pairs.extend((i1 + k, j1 + k, True) for k in range(i2 - i1))
        elif tag == "replace" and i2 - i1 <= cfg.max_fuzzy_block and j2 - j1 <= cfg.max_fuzzy_block:
            for a, b in _fuzzy_block(cloud_texts[i1:i2], local_texts[j1:j2], cfg.fuzzy_threshold):
                pairs.append((i1 + a, j1 + b, False))

    pieces: dict[int, list[tuple[float, float, bool]]] = {}
    for ci, lj, exact in pairs:
        ctoken, ltoken = cloud_tokens[ci], local_tokens[lj]
        exact = exact and ctoken.text != _NUM
        if ctoken.first_word == ctoken.last_word:
            pieces.setdefault(ctoken.first_word, []).append((ltoken.t0, ltoken.t1, exact))
            continue
        # A numeral run spread over several cloud words: split the local span by length.
        indices = range(ctoken.first_word, ctoken.last_word + 1)
        lengths = [max(1, len(cloud[k].word)) for k in indices]
        total, consumed = sum(lengths), 0
        span = ltoken.t1 - ltoken.t0
        for k, length in zip(indices, lengths):
            piece_start = ltoken.t0 + span * consumed / total
            consumed += length
            pieces.setdefault(k, []).append(
                (piece_start, ltoken.t0 + span * consumed / total, False)
            )

    anchors = []
    for word in sorted(pieces):
        items = pieces[word]
        if len(items) < cloud_touching[word]:
            continue
        anchors.append(
            _Anchor(
                word,
                min(item[0] for item in items),
                max(item[1] for item in items),
                all(item[2] for item in items),
            )
        )
    return anchors


# --------------------------------------------------------------------------
# Anchor filtering
# --------------------------------------------------------------------------


def _sliding_medians(values: list[float], radius: int) -> list[float]:
    window: list[float] = []
    result: list[float] = []
    lo = hi = 0  # window == values[lo:hi]
    for p in range(len(values)):
        new_lo, new_hi = max(0, p - radius), min(len(values), p + radius + 1)
        while hi < new_hi:
            insort(window, values[hi])
            hi += 1
        while lo < new_lo:
            del window[bisect_left(window, values[lo])]
            lo += 1
        mid = len(window) // 2
        result.append(window[mid] if len(window) % 2 else (window[mid - 1] + window[mid]) / 2)
    return result


def _filter_anchors(
    anchors: list[_Anchor], cloud: list[WordTimestamp], cfg: RetimeConfig
) -> tuple[list[_Anchor], int, int, int]:
    kept = [a for a in anchors if abs(a.start - cloud[a.word].start) <= cfg.absolute_max_shift_sec]
    dropped_cap = len(anchors) - len(kept)

    dropped_drift = 0
    for _ in range(max(1, cfg.drift_iterations)):
        if not kept:
            break
        offsets = [a.start - cloud[a.word].start for a in kept]
        wide = _sliding_medians(offsets, cfg.drift_window_anchors)
        narrow = _sliding_medians(offsets, cfg.drift_narrow_window_anchors)
        tolerance = cfg.drift_tolerance_sec
        survivors = [
            a
            for a, off, w, n in zip(kept, offsets, wide, narrow)
            if abs(off - w) <= tolerance or abs(off - n) <= tolerance
        ]
        if len(survivors) == len(kept):
            break
        dropped_drift += len(kept) - len(survivors)
        kept = survivors

    monotone: list[_Anchor] = []
    last_start = float("-inf")
    for anchor in kept:
        if anchor.start < last_start - 1e-9:
            continue
        monotone.append(anchor)
        last_start = anchor.start
    return monotone, dropped_cap, dropped_drift, len(kept) - len(monotone)


# --------------------------------------------------------------------------
# Timeline construction
# --------------------------------------------------------------------------


def _percentile(sorted_values: list[float], fraction: float) -> float:
    if not sorted_values:
        return 0.0
    return sorted_values[min(len(sorted_values) - 1, round(fraction * (len(sorted_values) - 1)))]


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    mid = len(ordered) // 2
    return ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2


def _span(words: list[WordTimestamp]) -> float:
    return words[-1].end - words[0].start if words else 0.0


def _size_ratios(cloud: list[WordTimestamp], local: list[WordTimestamp]) -> tuple[float, float]:
    """(local span / cloud span, local words / cloud words); 0.0 when a side is empty."""
    if not cloud or not local:
        return 0.0, 0.0
    cloud_span, local_span = _span(cloud), _span(local)
    duration = local_span / cloud_span if cloud_span > 0 and local_span > 0 else 1.0
    return duration, len(local) / len(cloud)


def quality_rejection(metrics: RetimeMetrics, cfg: RetimeConfig | None = None) -> str | None:
    """Why a retime must NOT be applied (None = good enough).

    Guards against local text that does not match the cloud text (another language,
    other speech, a mostly silent file): then only a few false anchors survive the
    drift filters and they would rewrite the time of the whole file.
    """
    cfg = cfg or RetimeConfig()
    total = metrics.total_words
    if total <= 0 or metrics.anchor_count <= 0:
        return "no anchors between the cloud and the local text"
    if metrics.anchor_ratio < cfg.min_anchor_ratio:
        return (
            f"anchor_ratio {metrics.anchor_ratio:.3f} < {cfg.min_anchor_ratio:g} "
            f"({metrics.anchor_count} of {total} words)"
        )
    needed = min(total, max(3, min(cfg.min_anchor_count, math.ceil(cfg.min_anchor_ratio * total))))
    if metrics.anchor_count < needed:
        return f"anchor_count {metrics.anchor_count} < {needed}"
    dropped = metrics.dropped_by_cap + metrics.dropped_by_drift + metrics.dropped_non_monotone
    raw = metrics.anchor_count + dropped
    if dropped / raw > cfg.max_dropped_fraction:
        return f"filters dropped {dropped} of {raw} raw anchors (> {cfg.max_dropped_fraction:g})"
    low, high = 1.0 / cfg.max_size_ratio, cfg.max_size_ratio
    if not low <= metrics.duration_ratio <= high:
        return (
            f"local/cloud duration ratio {metrics.duration_ratio:.2f} outside [{low:g}, {high:g}]"
        )
    if not low <= metrics.word_count_ratio <= high:
        return (
            f"local/cloud word count ratio {metrics.word_count_ratio:.2f} "
            f"outside [{low:g}, {high:g}]"
        )
    if metrics.shift_p90_sec > cfg.max_shift_p90_sec:
        return f"shift_p90 {metrics.shift_p90_sec:.1f} s > {cfg.max_shift_p90_sec:g} s"
    return None


def _unchanged(
    cloud: list[WordTimestamp],
    local: list[WordTimestamp],
    dropped: tuple[int, int, int] = (0, 0, 0),
) -> RetimeResult:
    total = len(cloud)
    duration_ratio, word_count_ratio = _size_ratios(cloud, local)
    metrics = RetimeMetrics(
        total_words=total,
        anchor_count=0,
        exact_count=0,
        fuzzy_count=0,
        interp_count=0,
        unchanged_count=total,
        anchor_ratio=0.0,
        shift_median_sec=0.0,
        shift_p90_sec=0.0,
        shift_max_sec=0.0,
        anchor_gaps=[],
        dropped_by_cap=dropped[0],
        dropped_by_drift=dropped[1],
        dropped_non_monotone=dropped[2],
        duration_ratio=duration_ratio,
        word_count_ratio=word_count_ratio,
    )
    return RetimeResult(list(cloud), ["unchanged"] * total, metrics)


def retime_words(
    cloud_words: list[WordTimestamp],
    local_words: list[WordTimestamp],
    *,
    config: RetimeConfig | None = None,
) -> RetimeResult:
    """Return cloud words (same text/order) with start/end taken from local ASR."""
    cfg = config or RetimeConfig()
    if not cloud_words or not local_words:
        return _unchanged(cloud_words, local_words)

    anchors, d_cap, d_drift, d_mono = _filter_anchors(
        _find_anchors(cloud_words, local_words, cfg), cloud_words, cfg
    )
    if not anchors:
        return _unchanged(cloud_words, local_words, (d_cap, d_drift, d_mono))

    total = len(cloud_words)
    starts = [0.0] * total
    ends = [0.0] * total
    kinds: list[RetimeKind] = ["interp"] * total
    for anchor in anchors:
        starts[anchor.word] = anchor.start
        ends[anchor.word] = max(anchor.end, anchor.start)
        kinds[anchor.word] = "exact" if anchor.exact else "fuzzy"

    # Words before the first / after the last anchor follow the nearest anchor's drift.
    first, last = anchors[0], anchors[-1]
    lead_shift = first.start - cloud_words[first.word].start
    for k in range(first.word):
        starts[k] = max(0.0, cloud_words[k].start + lead_shift)
        ends[k] = max(starts[k], cloud_words[k].end + lead_shift)
    tail_shift = last.start - cloud_words[last.word].start
    for k in range(last.word + 1, total):
        starts[k] = max(0.0, cloud_words[k].start + tail_shift)
        ends[k] = max(starts[k], cloud_words[k].end + tail_shift)

    # Words between anchors share the local gap proportionally to their length.
    for left, right in zip(anchors, anchors[1:]):
        if right.word == left.word + 1:
            continue
        indices = range(left.word + 1, right.word)
        t0 = ends[left.word]
        t1 = max(starts[right.word], t0)
        lengths = [max(1, len(cloud_words[k].word)) for k in indices]
        whole, consumed = sum(lengths), 0
        for k, length in zip(indices, lengths):
            starts[k] = t0 + (t1 - t0) * consumed / whole
            consumed += length
            ends[k] = t0 + (t1 - t0) * consumed / whole

    _make_monotone(starts, ends, cfg.min_word_duration_sec)

    words = [
        WordTimestamp(word.word, starts[k], ends[k], word.confidence)
        for k, word in enumerate(cloud_words)
    ]
    shifts = sorted(abs(starts[k] - cloud_words[k].start) for k in range(total))
    anchor_idx = [a.word for a in anchors]
    gaps: list[tuple[float, float]] = []

    def add_gap(t0: float, t1: float) -> None:
        if t1 - t0 > cfg.gap_report_sec:
            gaps.append((round(t0, 3), round(t1, 3)))

    add_gap(starts[0], starts[anchor_idx[0]])
    for a, b in zip(anchor_idx, anchor_idx[1:]):
        add_gap(ends[a], starts[b])
    add_gap(ends[anchor_idx[-1]], ends[-1])

    duration_ratio, word_count_ratio = _size_ratios(cloud_words, local_words)
    exact = sum(1 for kind in kinds if kind == "exact")
    fuzzy = sum(1 for kind in kinds if kind == "fuzzy")
    metrics = RetimeMetrics(
        total_words=total,
        anchor_count=len(anchors),
        exact_count=exact,
        fuzzy_count=fuzzy,
        interp_count=total - exact - fuzzy,
        unchanged_count=0,
        anchor_ratio=len(anchors) / total,
        shift_median_sec=_median(shifts),
        shift_p90_sec=_percentile(shifts, 0.9),
        shift_max_sec=shifts[-1],
        anchor_gaps=gaps,
        dropped_by_cap=d_cap,
        dropped_by_drift=d_drift,
        dropped_non_monotone=d_mono,
        duration_ratio=duration_ratio,
        word_count_ratio=word_count_ratio,
    )
    return RetimeResult(words, kinds, metrics)


def _make_monotone(starts: list[float], ends: list[float], min_duration: float) -> None:
    """In place: non-decreasing starts, no overlap, every word at least min_duration long."""
    total = len(starts)
    for k in range(1, total):
        starts[k] = max(starts[k], starts[k - 1])
    for k in range(total):
        ends[k] = max(ends[k], starts[k])
        if k + 1 < total:
            ends[k] = min(ends[k], starts[k + 1])
    previous_end = 0.0
    for k in range(total):
        starts[k] = max(starts[k], previous_end)
        ends[k] = max(ends[k], starts[k] + min_duration)
        previous_end = ends[k]
