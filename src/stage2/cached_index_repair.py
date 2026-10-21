"""Локальный ремонт «дыр» уже закэшированного облачного индекса слов.

`local_hole_repair` встроен в облачное распознавание и работает только при
новом прогоне. Индексы, сохранённые раньше (или когда ремонт был выключен),
хранят в `metadata.backend_metadata.coverage.unrecovered_ranges` дыры, которые
иначе лечатся лишь платным перераспознаванием всего файла. Здесь те же дыры
чинятся локально, по готовому индексу и без облачных затрат:

- слова индекса внутри дыры (разреженные облачные) заменяются локальными с
  реальными таймкодами, только если локальных больше; края дедуплицируются
  (общий код `local_hole_repair.repair_words_locally`);
- отчёт покрытия обновляется (`unrecovered_*`, `local_repaired_ranges`,
  `silent_ranges`, `holes_repaired_local`) и получает отметку
  `coverage.local_repair` с отпечатком настроек (модель, поле, порог речи):
  с теми же настройками повторно не чиним, при смене — чиним оставшиеся дыры;
- «плотные» дыры (слова индекса внутри уже дают >= SYNC_CLOUD_ASR_HOLE_MAX_WPM)
  не перераспознаются и из отчёта исключаются, как в облачном пути
  (`cloud_asr._range_is_dense`);
- если Whisper отработал без сбоев, но новых слов нет (`unchanged`), отметка
  всё равно пишется (`complete=True`, слова не меняются): иначе Whisper гонялся
  бы заново при каждом запуске. Сбой движка или остановка отметки не пишут;
- собственный лимит `SYNC_CLOUD_ASR_LOCAL_REPAIR_MAX_SEC` -> остановка
  `stopped="deadline"`, починенное сохраняется, `complete=False`;
- остальные ошибки оставляют индекс как был.

Сохранение на диск и сообщения пользователю остаются за вызывающей фазой.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import local_hole_repair
from .asr_contract import AsrProgress, AsrSegment
from .cloud_asr import DEFAULT_HOLE_MAX_WPM, _env_float, _range_is_dense
from .sync_models import WordIndex, WordTimestamp

logger = logging.getLogger(__name__)

MARKER_SOURCE = "cached-index"
_CLOUD_BACKENDS = frozenset({"cloud", "cloud-audio", "openrouter"})


@dataclass
class IndexRepairResult:
    # not_needed | disabled | unavailable | already_repaired | no_audio |
    # scale_repaired | repaired | unchanged | failed | cancelled
    status: str
    reason: str = ""
    # Индекс в памяти изменён и его нужно сохранить.
    changed: bool = False
    repaired: int = 0
    silent: int = 0
    remaining: int = 0
    # Причина остановки ремонта ("deadline"), если он остановился досрочно.
    stopped: str = ""


def settings_fingerprint(settings: local_hole_repair.LocalRepairSettings) -> str:
    """Отпечаток настроек, влияющих на результат ремонта."""
    return f"{settings.model}|margin={settings.margin_sec:g}|min_speech={settings.min_speech_sec:g}"


def _marker_fingerprint(marker: Any) -> str | None:
    """Отпечаток отметки; отчёт облачного прохода (без `fingerprint`) приводится
    к тому же виду, если ремонт там реально выполнялся."""
    if not isinstance(marker, dict):
        return None
    stored = marker.get("fingerprint")
    if isinstance(stored, str) and stored:
        return stored
    if marker.get("enabled") is not True or marker.get("skipped_reason"):
        return None
    try:
        return settings_fingerprint(
            local_hole_repair.LocalRepairSettings(
                model=str(marker["model"]),
                margin_sec=float(marker["margin_sec"]),
                min_speech_sec=float(marker["min_speech_sec"]),
            )
        )
    except (KeyError, TypeError, ValueError):
        return None


def _marker_is_complete(marker: dict[str, Any]) -> bool:
    if "complete" in marker:
        return bool(marker["complete"])
    if marker.get("stopped"):
        return False
    # Отчёт облачного прохода: «новых слов не дало» — завершённая попытка, а
    # сбой движка (временный: модель, ffmpeg) — нет, чиним снова.
    for item in marker.get("errors") or []:
        error = str(item.get("error") if isinstance(item, dict) else item)
        if error != local_hole_repair.NO_NEW_WORDS_ERROR:
            return False
    return True


def _ranges(value: Any) -> list[tuple[float, float]]:
    result: list[tuple[float, float]] = []
    for item in value or []:
        if isinstance(item, (list, tuple)) and len(item) == 2:
            try:
                start, end = float(item[0]), float(item[1])
            except (TypeError, ValueError):
                continue
            if end > start >= 0:
                result.append((start, end))
    return result


def _merge(ranges: list[tuple[float, float]]) -> list[tuple[float, float]]:
    merged: list[tuple[float, float]] = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _rounded(ranges: list[tuple[float, float]]) -> list[list[float]]:
    return [[round(a, 3), round(b, 3)] for a, b in ranges]


def _sparse_holes(
    words: list[WordTimestamp], holes: list[tuple[float, float]]
) -> list[tuple[float, float]]:
    """Дыры без «плотной» речи (тот же критерий, что в облачном отчёте)."""
    max_wpm = _env_float("SYNC_CLOUD_ASR_HOLE_MAX_WPM", DEFAULT_HOLE_MAX_WPM, allow_zero=True)
    pseudo = [AsrSegment(text=w.word, start=w.start, end=w.end, words=[]) for w in words]
    return [h for h in holes if not _range_is_dense(pseudo, h[0], h[1], max_wpm)]


def index_coverage(index: Any) -> dict[str, Any] | None:
    """`metadata.backend_metadata.coverage` облачного индекса (иначе None)."""
    metadata = getattr(index, "metadata", None)
    if not isinstance(metadata, dict):
        return None
    if str(metadata.get("backend") or "").strip().lower() not in _CLOUD_BACKENDS:
        return None
    backend_metadata = metadata.get("backend_metadata")
    coverage = backend_metadata.get("coverage") if isinstance(backend_metadata, dict) else None
    return coverage if isinstance(coverage, dict) else None


def has_unrecovered_holes(index: Any) -> bool:
    coverage = index_coverage(index)
    return coverage is not None and bool(_ranges(coverage.get("unrecovered_ranges")))


def apply_hole_outcome(
    index: WordIndex,
    coverage: dict[str, Any],
    *,
    new_words: list[WordTimestamp],
    still: list[tuple[float, float]],
    local_repaired: list[tuple[float, float]],
    silent: list[tuple[float, float]],
    marker: dict[str, Any],
) -> None:
    """Записывает итог ремонта дыр в слова индекса и отчёт покрытия (на месте).

    `still` / `local_repaired` / `silent` — итоговые (уже объединённые с прежними)
    диапазоны. Общий код локального ремонта по клипам и ремонта из готовых
    локальных слов (`index_retiming`).
    """
    new_coverage = dict(coverage)
    new_coverage.update(
        {
            "unrecovered_ranges": _rounded(still),
            "unrecovered_sec": round(sum(b - a for a, b in still), 3),
            "local_repaired_ranges": _rounded(local_repaired),
            "holes_repaired_local": len(local_repaired),
            "silent_ranges": _rounded(silent),
            "silent_sec": round(sum(b - a for a, b in silent), 3),
            "local_repair": marker,
        }
    )
    # Глобальный metadata.timestamp_precision не трогаем: большая часть слов
    # по-прежнему интерполирована; происхождение вставок — в coverage.
    index.words = new_words
    index.metadata["backend_metadata"] = {
        **index.metadata["backend_metadata"],
        "coverage": new_coverage,
    }


def repair_cached_index_holes(
    index: WordIndex,
    audio_path: Path | None,
    *,
    language: str,
    now_iso: Callable[[], str],
    transcribe: Callable[..., list[dict[str, Any]]] | None = None,
    settings: local_hole_repair.LocalRepairSettings | None = None,
    cancel: Any | None = None,
    on_progress: Callable[[AsrProgress], None] | None = None,
    duration_sec: float = 0.0,
    ffmpeg: str = "ffmpeg",
) -> IndexRepairResult:
    """Чинит unrecovered-дыры облачного индекса локально (синхронно).

    Меняет `index` на месте, если что-то починено/признано тишиной, а также
    пишет завершающую отметку при «новых слов нет» (`unchanged`, слова прежние);
    при сбое движка и остановке без результата индекс не трогается. Отмена
    (`cancel`) не прерывается исключением: статус "cancelled", индекс прежний.
    """
    coverage = index_coverage(index)
    holes = _merge(_ranges(coverage.get("unrecovered_ranges"))) if coverage else []
    if coverage is None or not holes:
        return IndexRepairResult("not_needed")
    remaining = len(holes)
    if settings is None:
        settings = local_hole_repair.LocalRepairSettings.from_env()
    if not settings.enabled:
        return IndexRepairResult("disabled", "disabled", remaining=remaining)
    try:
        unavailable = local_hole_repair.unavailable_reason(settings)
    except Exception as exc:  # проверка наличия движка не должна валить фазу
        unavailable = f"{type(exc).__name__}: {exc}"
    if unavailable is not None:
        return IndexRepairResult("unavailable", unavailable, remaining=remaining)
    fingerprint = settings_fingerprint(settings)
    previous = coverage.get("local_repair")
    if (
        isinstance(previous, dict)
        and _marker_fingerprint(previous) == fingerprint
        and _marker_is_complete(previous)
    ):
        return IndexRepairResult("already_repaired", remaining=remaining)
    if index.metadata.get("timestamp_scale_repair"):
        # Дыры измерены в шкале модели, а индекс уже сжат к длительности
        # файла: локальные (реальные) таймкоды легли бы не туда.
        return IndexRepairResult(
            "scale_repaired",
            "таймкоды индекса пересчитаны (timestamp_scale_repair): дыры измерены в другой шкале",
            remaining=remaining,
        )
    sparse = _sparse_holes(index.words, holes)
    outcome = local_hole_repair.LocalRepairOutcome(unrecovered=[])
    if sparse:
        if audio_path is None or not Path(audio_path).is_file():
            return IndexRepairResult("no_audio", "аудио источника не найдено", remaining=remaining)
        old_words = [
            {"word": w.word, "start": w.start, "end": w.end, "confidence": w.confidence}
            for w in index.words
        ]
        duration = float(duration_sec or 0.0) or index.duration_sec
        deadline = time.monotonic() + settings.max_sec if settings.max_sec > 0 else None
        try:
            outcome = local_hole_repair.repair_words_locally(
                Path(audio_path),
                sparse,
                old_words,
                duration_sec=duration,
                language=language,
                settings=settings,
                cancel=cancel,
                deadline_monotonic=deadline,
                on_progress=on_progress,
                ffmpeg=ffmpeg,
                transcribe=transcribe,
            )
        except Exception as exc:
            logger.warning(
                "Локальный ремонт кэшированного индекса не выполнен: %s", exc, exc_info=True
            )
            return IndexRepairResult("failed", f"{type(exc).__name__}: {exc}", remaining=remaining)
        if outcome.stopped == "cancelled" or (cancel is not None and cancel.is_set()):
            return IndexRepairResult("cancelled", remaining=remaining)
    errors = list(outcome.report.get("errors") or [])
    settled = not outcome.stopped and not outcome.engine_failures
    if sparse and not outcome.repaired and not outcome.silent and not settled:
        detail = "; ".join(str(e.get("error")) for e in errors[:3]) or str(
            outcome.stopped or "нечего вклеить"
        )
        return IndexRepairResult(
            "failed", detail, remaining=remaining, stopped=outcome.stopped or ""
        )

    changed_words = bool(outcome.repaired or outcome.silent)
    new_words = (
        [
            WordTimestamp.from_dict(w)
            for w in sorted(outcome.words or [], key=lambda w: (w["start"], w["end"]))
        ]
        if changed_words
        else index.words
    )
    # Плотные дыры — не дыры (как в облачном отчёте): в unrecovered не входят.
    still = _merge(outcome.unrecovered)
    local_repaired = _merge(_ranges(coverage.get("local_repaired_ranges")) + outcome.repaired)
    silent = _merge(_ranges(coverage.get("silent_ranges")) + outcome.silent)
    marker: dict[str, Any] = {
        "enabled": True,
        "model": settings.model,
        "margin_sec": settings.margin_sec,
        "min_speech_sec": settings.min_speech_sec,
        "fingerprint": fingerprint,
        "repaired_at": now_iso(),
        "source": MARKER_SOURCE,
        "words_added": int(outcome.report.get("words_added") or 0),
        "errors": errors,
        "stopped": outcome.stopped,
        "complete": settled,
    }
    apply_hole_outcome(
        index,
        coverage,
        new_words=new_words,
        still=still,
        local_repaired=local_repaired,
        silent=silent,
        marker=marker,
    )
    return IndexRepairResult(
        "repaired" if changed_words else "unchanged",
        changed=True,
        repaired=len(outcome.repaired),
        silent=len(outcome.silent),
        remaining=len(still),
        stopped=outcome.stopped or "",
    )
