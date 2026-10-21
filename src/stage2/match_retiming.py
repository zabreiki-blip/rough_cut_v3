"""Перенос границ подтверждённых матчей после перерасчёта времени слов индекса.

`index_retiming` меняет `start/end` слов облачного индекса, а подтверждённые
(`status=confirmed`) матчи фаза сопоставления не пересчитывает: их
`source_in/out` остались бы в старом облачном времени. Здесь:

- `RetimeMap` — компактное соответствие «старое время слова -> новое» (по одному слову
  индекса), лежит рядом с индексом в `<source_id>.retime_map.json`, НЕ в метаданных
  индекса (они входят в `matching_fingerprint`);
- `apply_retime_to_match` переносит границы подтверждённого матча, если пользователь
  их не правил: `source_in/out` совпадают с `speech_in/out -+ padding` из метаданных
  матча (так записывает матчер, и так их шлёт UI при «Подтвердить» без правки).
  Ручная правка границ, выбранная альтернатива и неоднозначный span не трогаются, а
  помечаются `metadata.retime_pending_review = True`.
"""

from __future__ import annotations

import logging
import math
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Sequence

from atomic_io import atomic_write_json

from .sync_matcher import MatcherConfig
from .sync_models import SyncMatch, WordTimestamp

logger = logging.getLogger(__name__)

MAP_SUFFIX = ".retime_map.json"
MAP_VERSION = 1
# Слова в карте и метаданных матча округлены до 3-4 знаков; UI шлёт границы с 3 знаками.
_WORD_TOLERANCE_SEC = 0.002
_BOUNDARY_TOLERANCE_SEC = 0.01
_DIGITS = 4

Outcome = Literal["retimed", "review", "skip"]


def retime_map_path(index_path: Path, source_id: str) -> Path:
    return Path(index_path).with_name(f"{source_id}{MAP_SUFFIX}")


def _parse_iso(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class RetimeMap:
    """Старые и новые start/end слов, изменённых перерасчётом (параллельные массивы)."""

    fingerprint: str
    retimed_at: str
    old_starts: list[float]
    old_ends: list[float]
    new_starts: list[float]
    new_ends: list[float]
    _ends_order: list[tuple[float, int]] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        sizes = {len(self.old_starts), len(self.old_ends), len(self.new_starts), len(self.new_ends)}
        if len(sizes) != 1:
            raise ValueError("retime map arrays must have equal length")
        object.__setattr__(
            self, "_ends_order", sorted((end, i) for i, end in enumerate(self.old_ends))
        )

    @classmethod
    def from_words(
        cls,
        old: Sequence[WordTimestamp],
        new: Sequence[WordTimestamp],
        *,
        fingerprint: str,
        retimed_at: str,
    ) -> "RetimeMap":
        if len(old) != len(new):
            raise ValueError("old and new words must be parallel")
        return cls(
            fingerprint=fingerprint,
            retimed_at=retimed_at,
            old_starts=[round(w.start, _DIGITS) for w in old],
            old_ends=[round(w.end, _DIGITS) for w in old],
            new_starts=[round(w.start, _DIGITS) for w in new],
            new_ends=[round(w.end, _DIGITS) for w in new],
        )

    def matches_index(self, index: Any) -> bool:
        """Карта относится к текущему индексу (маркер перерасчёта тот же)."""
        backend = (getattr(index, "metadata", None) or {}).get("backend_metadata") or {}
        marker = backend.get("retiming") if isinstance(backend, dict) else None
        return (
            isinstance(marker, dict)
            and marker.get("fingerprint") == self.fingerprint
            and marker.get("retimed_at") == self.retimed_at
        )

    def _start_word(self, start: float) -> int | None:
        lo = bisect_left(self.old_starts, start - _WORD_TOLERANCE_SEC)
        hi = bisect_right(self.old_starts, start + _WORD_TOLERANCE_SEC)
        return lo if hi - lo == 1 else None

    def _end_word(self, end: float) -> int | None:
        lo = bisect_left(self._ends_order, (end - _WORD_TOLERANCE_SEC, -1))
        hi = bisect_right(self._ends_order, (end + _WORD_TOLERANCE_SEC, len(self.old_ends)))
        return self._ends_order[lo][1] if hi - lo == 1 else None

    def new_speech(
        self, speech_in: float, speech_out: float, guide_offset_sec: float
    ) -> tuple[float, float] | None:
        """Новые speech_in/out слов старого span-а; None, если span неоднозначен."""
        first = self._start_word(speech_in - guide_offset_sec)
        last = self._end_word(speech_out - guide_offset_sec)
        if first is None or last is None or last < first:
            return None
        return (
            round(self.new_starts[first] + guide_offset_sec, _DIGITS),
            round(self.new_ends[last] + guide_offset_sec, _DIGITS),
        )


def save_retime_map(path: Path, rmap: RetimeMap) -> None:
    """Атомарно пишет карту; сбой записи не должен ронять перерасчёт."""
    try:
        atomic_write_json(
            path,
            {
                "version": MAP_VERSION,
                "fingerprint": rmap.fingerprint,
                "retimed_at": rmap.retimed_at,
                "old_starts": rmap.old_starts,
                "old_ends": rmap.old_ends,
                "new_starts": rmap.new_starts,
                "new_ends": rmap.new_ends,
            },
        )
    except OSError:
        logger.warning("Не удалось сохранить карту перерасчёта %s", path, exc_info=True)


def load_retime_map(path: Path) -> RetimeMap | None:
    import json

    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("version") != MAP_VERSION:
            return None
        arrays = [
            [float(x) for x in payload[key]]
            for key in ("old_starts", "old_ends", "new_starts", "new_ends")
        ]
        if not all(math.isfinite(x) for array in arrays for x in array):
            return None
        return RetimeMap(
            str(payload["fingerprint"]),
            str(payload["retimed_at"]),
            *arrays,  # type: ignore[arg-type]
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def _padded(
    speech_in: float, speech_out: float, head: float, tail: float, duration_sec: float
) -> tuple[float, float]:
    """Как `sync_matcher._padded_range`."""
    start = max(0.0, speech_in - head)
    end = speech_out + tail
    if duration_sec > 0:
        start, end = min(duration_sec, start), min(duration_sec, end)
    return start, max(start, end)


def _mark_review(match: SyncMatch, rmap: RetimeMap, now_iso: str) -> Outcome:
    match.metadata["retime_pending_review"] = True
    match.metadata["retime_review_for"] = rmap.fingerprint
    match.metadata["retime_review_at"] = now_iso
    return "review"


def apply_retime_to_match(
    match: SyncMatch,
    rmap: RetimeMap,
    *,
    now_iso: str,
    guide_offset_sec: float = 0.0,
    duration_sec: float = 0.0,
) -> Outcome:
    """Переносит границы подтверждённого матча на новое время слов (на месте).

    - `skip`: ничего не сделано (не confirmed; подтверждён после перерасчёта; уже
      пересчитан/помечен под этот перерасчёт; нечитаемая отметка ручной правки);
    - `retimed`: границы пересчитаны (статус confirmed, отступы прежние), старые значения
      в `metadata.retimed_from`;
    - `review`: границы правили руками/выбрана альтернатива/span неоднозначен — матч не
      тронут, помечен `metadata.retime_pending_review`.
    """
    metadata = match.metadata
    if match.status != "confirmed" or match.source_in_sec is None or match.source_out_sec is None:
        return "skip"
    previous = metadata.get("retimed_from")
    if isinstance(previous, dict) and previous.get("fingerprint") == rmap.fingerprint:
        return "skip"
    if metadata.get("retime_review_for") == rmap.fingerprint:
        return "skip"
    retimed_dt = _parse_iso(rmap.retimed_at)
    manual_raw = metadata.get("manual_updated_at")
    manual_dt = _parse_iso(manual_raw) if manual_raw else None
    if retimed_dt is None or (manual_raw and manual_dt is None):
        return "skip"  # безопаснее не трогать, чем сдвинуть чужую правку
    if manual_dt is not None and manual_dt >= retimed_dt:
        return "skip"  # подтверждено уже по новому времени

    speech_in = _number(metadata.get("speech_in_sec"))
    speech_out = _number(metadata.get("speech_out_sec"))
    head = _number(metadata.get("head_padding_sec"))
    tail = _number(metadata.get("tail_padding_sec"))
    head = MatcherConfig.head_padding_sec if head is None else head
    tail = MatcherConfig.tail_padding_sec if tail is None else tail
    if speech_in is None or speech_out is None:
        return _mark_review(match, rmap, now_iso)
    expected_in, expected_out = _padded(speech_in, speech_out, head, tail, duration_sec)
    if (
        abs(match.source_in_sec - expected_in) > _BOUNDARY_TOLERANCE_SEC
        or abs(match.source_out_sec - expected_out) > _BOUNDARY_TOLERANCE_SEC
    ):
        return _mark_review(match, rmap, now_iso)  # границы правили / выбрана альтернатива
    new_speech = rmap.new_speech(speech_in, speech_out, guide_offset_sec)
    if new_speech is None:
        return _mark_review(match, rmap, now_iso)

    new_in, new_out = _padded(new_speech[0], new_speech[1], head, tail, duration_sec)
    metadata["retimed_from"] = {
        "source_in_sec": match.source_in_sec,
        "source_out_sec": match.source_out_sec,
        "speech_in_sec": speech_in,
        "speech_out_sec": speech_out,
        "fingerprint": rmap.fingerprint,
    }
    metadata["retimed_at"] = now_iso
    metadata["speech_in_sec"], metadata["speech_out_sec"] = new_speech
    metadata.pop("retime_pending_review", None)
    metadata.pop("retime_review_for", None)
    metadata.pop("retime_review_at", None)
    match.source_in_sec, match.source_out_sec = round(new_in, 4), round(new_out, 4)
    return "retimed"
