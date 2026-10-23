"""Sequential alignment of the scenario's voiceover blocks to a recording.

Input is the ASR word index of one recording (or of its parts, in name order) and
the confirmed normalization. Only active ``voiceover`` blocks (not absorbed by
another block) take part; the spoken ``text_for_tts`` is matched, the original
text is only kept for display.

The alignment is strictly sequential: a cursor moves through the ASR tokens and
every block is searched from the cursor forward within a bounded window, never
over the whole recording independently. Text normalization, stop words and the
phrase score come from ``stage2.sync_matcher``; the padding handles are the
``MatcherConfig`` head/tail padding used for sync quotes.

Duplicates (see the design spec, section "Дубли"):
- consecutive full re-readings of a block become ``takes``; the cursor moves past
  the last reading, so following blocks are not shifted. The block-level range is
  the last full take (``selected_take`` stays ``None`` = auto);
- a repeated word/phrase inside a reading, or a false start before it, is a
  partial take (``partial=True``). A false start is excluded from the proposed
  range; a repeat in the middle is not cut out;
- the same phrase in two different blocks is not a duplicate: a reading that
  fits the next block at least as well (or equal neighbour texts) is never
  attributed to the current block as a duplicate.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass
from difflib import SequenceMatcher
import hashlib
import math
from typing import Any, Mapping, Sequence
import unicodedata

from stage2.sync_matcher import _STOPWORDS, MatcherConfig, normalize_text
from stage2.sync_matcher import _score as phrase_score
from stage2.sync_models import WordIndex
from stage2.timeline_parser import _spoken_text
from voiceover.models import Take, VoiceoverRange

FOUND_MIN_COVERAGE = 0.6
WIDE_MIN_COVERAGE = 0.8  # after several unread blocks the search window is widened
DUPLICATE_MIN_COVERAGE = 0.85
LOW_CONFIDENCE = 0.8
MAX_CANDIDATES = 25
MIN_SPEECH_SEC = 0.02


@dataclass(frozen=True)
class PartIndex:
    """ASR words of one recording part with the identity of that part."""

    recording_id: str
    index: WordIndex
    duration_sec: float


@dataclass(frozen=True)
class _Block:
    uid: str
    tokens: tuple[str, ...]
    sha256: str
    display_text: str


@dataclass
class _Stream:
    recording_id: str
    duration_sec: float
    canon: list[str]
    starts: list[float]
    ends: list[float]
    positions: dict[str, list[int]]


@dataclass
class _Reading:
    start: int  # first ASR token (inclusive)
    end: int  # last ASR token (inclusive)
    coverage: float
    partial: bool
    prefix: tuple[int, int] | None = None  # false start before the reading (inclusive)


@dataclass
class _Found:
    readings: list[_Reading]
    prefix: tuple[int, int] | None
    ambiguous: bool
    confidence: float


def _spoken_hash(text: str) -> str:
    value = " ".join(unicodedata.normalize("NFC", text).split())
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _active_voiceover_blocks(normalization: Mapping[str, Any]) -> list[_Block]:
    raw_blocks = [item for item in normalization.get("blocks") or [] if isinstance(item, dict)]
    raw_blocks.sort(key=lambda item: int(item.get("order", 0) or 0))
    result: list[_Block] = []
    for block in raw_blocks:
        if block.get("absorbed_by_uid") or str(block.get("type", "")).lower() != "voiceover":
            continue
        uid = str(block.get("uid") or "").strip()
        if not uid:
            continue
        display = str(block.get("normalized_text") or block.get("original_text") or "").strip()
        spoken = _spoken_text(str(block.get("text_for_tts") or display), block_type="voiceover")
        result.append(
            _Block(
                uid=uid,
                tokens=tuple(normalize_text(spoken)),
                sha256=_spoken_hash(spoken),
                display_text=display,
            )
        )
    return result


def _build_stream(part: PartIndex, vocabulary: dict[str, list[str]]) -> _Stream:
    """Tokenize ASR words; tokens close to a script word are snapped to it."""

    known = {token for bucket in vocabulary.values() for token in bucket}
    cache: dict[str, str] = {}

    def canonical(token: str) -> str:
        cached = cache.get(token)
        if cached is not None:
            return cached
        result = token
        if token not in known and len(token) >= 4:
            best = 0.8
            for candidate in vocabulary.get(token[:4], ()):
                if abs(len(candidate) - len(token)) > 3:
                    continue
                ratio = SequenceMatcher(None, token, candidate).ratio()
                if ratio >= best:
                    best, result = ratio, candidate
        cache[token] = result
        return result

    canon: list[str] = []
    starts: list[float] = []
    ends: list[float] = []
    positions: dict[str, list[int]] = {}
    for word in part.index.words:
        for token in normalize_text(word.word):
            token = canonical(token)
            positions.setdefault(token, []).append(len(canon))
            canon.append(token)
            starts.append(word.start)
            ends.append(word.end)
    return _Stream(part.recording_id, part.duration_sec, canon, starts, ends, positions)


def _key_tokens(tokens: Sequence[str]) -> list[tuple[int, str]]:
    keys = [
        (index, token)
        for index, token in enumerate(tokens)
        if len(token) >= 3 and token not in _STOPWORDS
    ][:3]
    return keys or list(enumerate(tokens))[:3]


def _is_weak(tokens: Sequence[str]) -> bool:
    return all(len(token) < 3 or token in _STOPWORDS for token in tokens)


def _evaluate(tokens: Sequence[str], stream: _Stream, start: int) -> _Reading | None:
    """Match ``tokens`` against the ASR window starting at ``start``."""

    size = len(tokens)
    window = stream.canon[start : start + math.ceil(size * 1.35) + 6]
    if not window:
        return None
    matcher = SequenceMatcher(None, tokens, window, autojunk=False)
    blocks = [block for block in matcher.get_matching_blocks() if block.size]
    # An isolated stop-word fragment far from the rest must not stretch the span.
    while len(blocks) > 1:
        first, second = blocks[0], blocks[1]
        gap = second.b - (first.b + first.size)
        if first.size <= 2 and _is_weak(tokens[first.a : first.a + first.size]) and gap > 3:
            blocks.pop(0)
            continue
        last, before = blocks[-1], blocks[-2]
        gap = last.b - (before.b + before.size)
        if last.size <= 2 and _is_weak(tokens[last.a : last.a + last.size]) and gap > 3:
            blocks.pop()
            continue
        break
    if not blocks:
        return None
    matched_positions = {block.b + offset for block in blocks for offset in range(block.size)}
    first_index, last_index = min(matched_positions), max(matched_positions)
    matched = sum(block.size for block in blocks)
    known = set(tokens)
    repeats = sum(
        1
        for position in range(first_index, last_index + 1)
        if position not in matched_positions
        and len(window[position]) >= 3
        and window[position] in known
    )
    return _Reading(
        start=start + first_index,
        end=start + last_index,
        coverage=matched / size,
        partial=repeats > 0,
    )


def _candidate_starts(tokens: Sequence[str], stream: _Stream, low: int, high: int) -> list[int]:
    starts: set[int] = set()
    for offset, token in _key_tokens(tokens):
        places = stream.positions.get(token)
        if not places:
            continue
        left = bisect.bisect_left(places, low + offset)
        right = bisect.bisect_left(places, high + offset)
        for position in places[left:right]:
            starts.add(max(low, position - offset))
    return sorted(starts)[:MAX_CANDIDATES]


def _find(
    tokens: Sequence[str],
    stream: _Stream,
    low: int,
    high: int,
    *,
    margin: float,
    minimum: float,
) -> tuple[_Reading | None, list[_Reading]]:
    """Earliest reading within ``margin`` of the best coverage (sequential preference)."""

    readings: list[_Reading] = []
    seen: set[tuple[int, int]] = set()
    for start in _candidate_starts(tokens, stream, low, high):
        reading = _evaluate(tokens, stream, start)
        if reading is None or (reading.start, reading.end) in seen:
            continue
        seen.add((reading.start, reading.end))
        readings.append(reading)
    if not readings:
        return None, []
    top = max(item.coverage for item in readings)
    if top < minimum:
        return None, readings
    readings.sort(key=lambda item: item.start)
    chosen = next(item for item in readings if item.coverage >= top - margin)
    return chosen, readings


def _restart_prefix(
    tokens: Sequence[str], stream: _Stream, reading: _Reading, floor: int
) -> tuple[int, int] | None:
    """A false start: tokens right before the reading repeat the block's beginning."""

    limit = min(reading.start - floor, len(tokens) - 1, 8)
    for length in range(limit, 0, -1):
        head = list(tokens[:length])
        if stream.canon[reading.start - length : reading.start] != head:
            continue
        if length == 1 and (len(head[0]) < 4 or head[0] in _STOPWORDS):
            continue
        return reading.start - length, reading.start - 1
    return None


def _gap_belongs_to_next_blocks(
    blocks: Sequence[_Block],
    position: int,
    stream: _Stream,
    last: _Reading,
    again: _Reading,
) -> bool:
    """True when the filler between two readings is read text of a following block."""

    if again.start <= last.end + 1:
        return False
    for follower in blocks[position + 1 : position + 4]:
        if not follower.tokens:
            continue
        found, _ = _find(
            follower.tokens,
            stream,
            last.end + 1,
            again.start,
            margin=0.0,
            minimum=FOUND_MIN_COVERAGE,
        )
        if found is not None and found.start < again.start:
            return True
    return False


def _align_part(
    blocks: Sequence[_Block],
    first_block: int,
    stream: _Stream,
    config: MatcherConfig,
) -> dict[int, _Found | None]:
    results: dict[int, _Found | None] = {}
    cursor = 0
    misses = 0
    total = len(stream.canon)
    for position in range(first_block, len(blocks)):
        block = blocks[position]
        tokens = block.tokens
        if not tokens or total == 0:
            results[position] = None
            continue
        size = len(tokens)
        wide = misses >= 2
        high = total if wide else min(total, cursor + 6 * size + 300)
        best, candidates = _find(
            tokens,
            stream,
            cursor,
            high,
            margin=config.ambiguity_margin,
            minimum=WIDE_MIN_COVERAGE if wide else FOUND_MIN_COVERAGE,
        )
        if best is None:
            results[position] = None
            misses += 1
            continue
        misses = 0
        following = blocks[position + 1] if position + 1 < len(blocks) else None
        prefix = _restart_prefix(tokens, stream, best, cursor)
        readings = [best]
        # Identical neighbouring texts: every reading belongs to its own block.
        # A later (non-adjacent) block with the same text owns the next reading too.
        repeated_later = any(item.tokens == tokens for item in blocks[position + 1 :])
        if not repeated_later:
            slack = 3 + size // 10  # filler words allowed between two readings
            while True:
                last = readings[-1]
                again, _ = _find(
                    tokens,
                    stream,
                    last.end + 1,
                    last.end + 2 + slack,
                    margin=0.0,
                    minimum=DUPLICATE_MIN_COVERAGE,
                )
                if again is None or again.start > last.end + 1 + slack:
                    break
                if following is not None and following.tokens:
                    rival = _evaluate(following.tokens, stream, again.start)
                    if (
                        rival is not None
                        and rival.start <= again.start + 2
                        and rival.coverage >= again.coverage - 0.1
                    ):
                        break  # this reading fits the next block, not a duplicate
                if _gap_belongs_to_next_blocks(blocks, position, stream, last, again):
                    break  # the tokens in between are the following block(s)
                readings.append(again)
        end = readings[-1].end
        ambiguous = any(
            item.start > end
            and item.coverage
            >= max(DUPLICATE_MIN_COVERAGE, best.coverage - config.ambiguity_margin)
            for item in candidates
        )
        selected = _selected_reading(readings)
        span = stream.canon[selected.start : selected.end + 1]
        confidence = min(1.0, max(0.0, phrase_score(list(tokens), span)))
        if ambiguous:
            confidence *= 0.85
        results[position] = _Found(readings, prefix, ambiguous, confidence)
        cursor = end + 1
    return results


def _selected_reading(readings: Sequence[_Reading]) -> _Reading:
    full = [item for item in readings if not item.partial]
    if full:
        return full[-1]
    return _Reading(
        start=readings[0].start,
        end=readings[-1].end,
        coverage=min(item.coverage for item in readings),
        partial=True,
    )


@dataclass
class _Item:
    """One padded interval of a recording (a reading, a false start or a block range)."""

    speech_in: float
    speech_out: float
    source_in: float = 0.0
    source_out: float = 0.0
    key: tuple[int, int] = (0, 0)  # (block position, item number)
    first_token: int = -1  # stream token span of the interval (-1: unknown)
    last_token: int = -1
    near_foreign: bool = False  # padding was cut by speech that belongs to no block


def _speech(stream: _Stream, first: int, last: int) -> tuple[float, float]:
    start = stream.starts[first]
    end = max(stream.ends[last], start + MIN_SPEECH_SEC)
    return start, end


def _pad(
    items: list[_Item], duration: float, config: MatcherConfig, stream: _Stream | None = None
) -> None:
    """Head/tail padding clamped to the middle of the pause towards the neighbours.

    Words of the recording that belong to no found interval (a skipped or misplaced
    block) also limit the padding, so unmarked speech never leaks into a range.
    """

    items.sort(key=lambda item: (item.speech_in, item.speech_out))
    covered = bytearray(len(stream.starts) if stream is not None else 0)
    for item in items:
        for token in range(max(item.first_token, 0), item.last_token + 1):
            if token < len(covered):
                covered[token] = 1
    for index, item in enumerate(items):
        low = 0.0
        if index > 0:
            low = (items[index - 1].speech_out + item.speech_in) / 2
        high = duration
        if index + 1 < len(items):
            high = (item.speech_out + items[index + 1].speech_in) / 2
        if stream is not None and item.first_token >= 0:
            before = next((t for t in range(item.first_token - 1, -1, -1) if not covered[t]), None)
            if before is not None and stream.ends[before] <= item.speech_in:
                cut = (stream.ends[before] + item.speech_in) / 2
                if cut > low:
                    low = cut
                if cut > item.speech_in - config.head_padding_sec:
                    item.near_foreign = True
            after = next(
                (t for t in range(item.last_token + 1, len(covered)) if not covered[t]), None
            )
            if after is not None and stream.starts[after] >= item.speech_out:
                cut = (item.speech_out + stream.starts[after]) / 2
                if cut < high:
                    high = cut
                if cut < item.speech_out + config.tail_padding_sec:
                    item.near_foreign = True
        item.source_in = min(
            max(item.speech_in - config.head_padding_sec, low, 0.0), item.speech_in
        )
        item.source_out = max(
            min(item.speech_out + config.tail_padding_sec, high, duration), item.speech_out
        )


def _as_parts(
    index: WordIndex | Sequence[PartIndex],
    recording_id: str | None,
    duration_sec: float | None,
    config: MatcherConfig,
) -> list[PartIndex]:
    if isinstance(index, WordIndex):
        last_end = max((word.end for word in index.words), default=0.0)
        duration = duration_sec if duration_sec is not None else last_end + config.tail_padding_sec
        return [PartIndex(recording_id or index.source_id, index, max(duration, last_end))]
    return [
        PartIndex(
            part.recording_id,
            part.index,
            max(part.duration_sec, max((word.end for word in part.index.words), default=0.0)),
        )
        for part in index
    ]


def align_voiceover(
    normalization: Mapping[str, Any],
    index: WordIndex | Sequence[PartIndex],
    *,
    recording_id: str | None = None,
    duration_sec: float | None = None,
    config: MatcherConfig | None = None,
) -> list[VoiceoverRange]:
    """Propose recording ranges for the active voiceover blocks, in scenario order.

    ``index`` is one :class:`WordIndex` (``recording_id``/``duration_sec`` identify
    the file) or the parts of a recording in name order. Parts are aligned in order:
    blocks not found before the end of a part are searched again only in the next
    part. The result has one entry per block: ``needs_review`` proposals and
    ``missing`` blocks (zero placeholder range, ``voice_source="none"``).
    """

    cfg = config or MatcherConfig()
    blocks = _active_voiceover_blocks(normalization)
    parts = _as_parts(index, recording_id, duration_sec, cfg)

    vocabulary: dict[str, list[str]] = {}
    for block in blocks:
        for token in set(block.tokens):
            if len(token) >= 4:
                vocabulary.setdefault(token[:4], []).append(token)

    found: dict[int, tuple[int, _Found]] = {}  # block position -> (part number, result)
    next_block = 0
    streams = [_build_stream(part, vocabulary) for part in parts]
    for part_number, stream in enumerate(streams):
        if next_block >= len(blocks):
            break
        results = _align_part(blocks, next_block, stream, cfg)
        hits = [position for position, result in results.items() if result is not None]
        if not hits:
            continue
        for position in hits:
            found[position] = (part_number, results[position])  # type: ignore[arg-type]
        # Blocks after the last hit were not read in this part: try them in the next.
        next_block = max(hits) + 1

    items_by_part: dict[int, list[_Item]] = {number: [] for number in range(len(streams))}
    block_items: dict[int, tuple[list[_Item], _Item | None, _Item]] = {}
    for position, (part_number, result) in found.items():
        stream = streams[part_number]
        reading_items = [
            _Item(
                *_speech(stream, item.start, item.end),
                key=(position, number),
                first_token=item.start,
                last_token=item.end,
            )
            for number, item in enumerate(result.readings)
        ]
        prefix_item = None
        if result.prefix is not None:
            prefix_item = _Item(
                *_speech(stream, *result.prefix),
                key=(position, -1),
                first_token=result.prefix[0],
                last_token=result.prefix[1],
            )
        full = [item for item in result.readings if not item.partial]
        if full:
            chosen = reading_items[result.readings.index(full[-1])]
        else:
            first_speech = reading_items[0].speech_in
            last_speech = reading_items[-1].speech_out
            chosen = _Item(first_speech, last_speech, key=(position, -2))
        flat = [*reading_items, *([prefix_item] if prefix_item else [])]
        items_by_part[part_number].extend(flat)
        block_items[position] = (reading_items, prefix_item, chosen)
    # Padding is computed over all intervals of a recording at once.
    for part_number, items in items_by_part.items():
        _pad(items, streams[part_number].duration_sec, cfg, streams[part_number])
    for position, (reading_items, prefix_item, chosen) in block_items.items():
        if chosen.key[1] == -2:  # merged span of partial readings only
            chosen.source_in = reading_items[0].source_in
            chosen.source_out = reading_items[-1].source_out

    ranges: list[VoiceoverRange] = []
    for position, block in enumerate(blocks):
        if position not in found:
            ranges.append(
                VoiceoverRange(
                    block_uid=block.uid,
                    text_sha256=block.sha256,
                    recording_id="",
                    source_in_sec=0.0,
                    source_out_sec=0.0,
                    speech_in_sec=0.0,
                    speech_out_sec=0.0,
                    status="missing",
                    confidence=0.0,
                    voice_source="none",
                    note="не найден в записи",
                    display_text=block.display_text,
                )
            )
            continue
        part_number, result = found[position]
        reading_items, prefix_item, chosen = block_items[position]
        readings = result.readings
        notes: list[str] = []
        if len(readings) >= 2:
            notes.append(f"дубли: {len(readings)}")
        if prefix_item is not None or any(item.partial for item in readings):
            notes.append("частичный дубль")
        if result.ambiguous:
            notes.append("неоднозначное совпадение")
        if any(item.near_foreign for item in reading_items):
            notes.append("рядом нераспознанная речь")
        if result.confidence < LOW_CONFIDENCE:
            notes.append("низкая уверенность")
        takes: list[Take] = []
        if len(readings) >= 2 or "частичный дубль" in notes:
            if prefix_item is not None:
                takes.append(_take(prefix_item, 0.5, partial=True))
            for reading, item in zip(readings, reading_items, strict=True):
                takes.append(_take(item, reading.coverage, partial=reading.partial))
        ranges.append(
            VoiceoverRange(
                block_uid=block.uid,
                text_sha256=block.sha256,
                recording_id=streams[part_number].recording_id,
                source_in_sec=chosen.source_in,
                source_out_sec=chosen.source_out,
                speech_in_sec=chosen.speech_in,
                speech_out_sec=chosen.speech_out,
                status="needs_review",
                confidence=result.confidence,
                takes=tuple(takes),
                selected_take=None,
                voice_source="recorded",
                note="; ".join(notes),
                display_text=block.display_text,
            )
        )
    return ranges


def _take(item: _Item, coverage: float, *, partial: bool) -> Take:
    return Take(
        source_in_sec=item.source_in,
        source_out_sec=item.source_out,
        speech_in_sec=item.speech_in,
        speech_out_sec=item.speech_out,
        confidence=min(1.0, max(0.0, coverage)),
        partial=partial,
    )


__all__ = ["PartIndex", "align_voiceover"]
