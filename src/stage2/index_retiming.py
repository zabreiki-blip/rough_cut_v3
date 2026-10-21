"""Перерасчёт времени слов облачного индекса по локальному Whisper всего файла.

Облачный индекс (`backend=cloud-audio`, `timestamp_precision=interpolated`)
хранит слова, равномерно размазанные по репликам: ошибка времени ~7 с в медиане
(до 100 с). Один раз на файл здесь распознаётся ВЕСЬ файл локальным
faster-whisper (word_timestamps, VAD), и его время переносится на облачные слова
чистой `word_retiming.retime_words`. Облачных затрат нет.

- результат локального распознавания кэшируется рядом с индексом
  (`<source_id>.local_words.json`, с сигнатурой аудио, моделью и языком): новая
  версия алгоритма (`RETIME_VERSION`) пересчитывает время без повторного Whisper;
- итог пишется в `backend_metadata.retiming` (отпечаток: модель + версия +
  параметры `RetimeConfig`): с тем же отпечатком повторно не считаем;
- слова из `coverage.local_repaired_ranges` — вставки прежнего локального ремонта в
  облачных координатах: перед перерасчётом они удаляются, диапазоны переносятся
  через соседние слова и заполняются заново из локальных слов (вместе с
  `unrecovered_ranges`), иначе соседи сдвинулись бы, а вставки остались на месте;
- `silent_ranges` / `unrecovered_ranges` отчёта тоже переносятся через соседние слова;
  дыры заполняются из тех же локальных слов (без второго Whisper),
  отметка `coverage.local_repair.source = "retime-local-words"`;
- гейт качества (`word_retiming.quality_rejection`): результат применяется только
  при достаточной доле опорных слов и правдоподобных размерах/сдвигах, иначе слова
  не меняются (чужой текст, другой язык, тишина дали бы горстку ложных якорей);
- `timestamp_precision` становится `retimed` (исходное значение сохраняется в
  `backend_metadata.timestamp_precision_original`), только если слова менялись;
- карта «старое -> новое время» слов пишется рядом (`<source_id>.retime_map.json`, см.
  `match_retiming`) тоже вне индекса: по ней фаза сопоставления переносит границы
  подтверждённых матчей;
- отмена: индекс не меняется. Любая неудача (rejected / no_words / stopped /
  failed) пишет маркер попытки в ОТДЕЛЬНЫЙ файл `<source_id>.retime_attempt.json`,
  а не в индекс: индекс (а с ним `_sync_matching_fingerprint`, который хэширует
  `index.to_dict()` целиком вместе с метаданными) остаётся прежним и
  сопоставление не пересчитывается зря. Политика повторов — `_skip_attempt`.

Сохранение индекса на диск и сообщения пользователю остаются за вызывающей фазой.
"""

from __future__ import annotations

import dataclasses
import logging
import math
import os
import time
from bisect import bisect_left
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, ContextManager

from atomic_io import atomic_write_json

from . import local_hole_repair
from .match_retiming import RetimeMap, save_retime_map
from .cached_index_repair import (
    _merge,
    _ranges,
    _rounded,
    _sparse_holes,
    apply_hole_outcome,
    index_coverage,
)
from .sync_models import WordIndex, WordTimestamp
from .word_retiming import RetimeConfig, RetimeMetrics, quality_rejection, retime_words

logger = logging.getLogger(__name__)

# Версию повышают при любом изменении алгоритма/параметров перерасчёта: индексы
# с другим отпечатком пересчитываются (по кэшу локальных слов, без Whisper).
RETIME_VERSION = 2
ENABLE_ENV = "SYNC_CLOUD_ASR_RETIME"
MODEL_ENV = "SYNC_CLOUD_ASR_RETIME_MODEL"
MAX_SEC_ENV = "SYNC_CLOUD_ASR_RETIME_MAX_SEC"
RETRY_HOURS_ENV = "SYNC_CLOUD_ASR_RETIME_RETRY_HOURS"
DEFAULT_RETRY_HOURS = 24.0
DEFAULT_MODEL = "small"
MARKER_SOURCE = "retime-local-words"
LOCAL_WORDS_SUFFIX = ".local_words.json"
LOCAL_WORDS_VERSION = 1
ATTEMPT_SUFFIX = ".retime_attempt.json"
ATTEMPT_VERSION = 1
PRECISION = "retimed"
_FALSE_VALUES = {"0", "false", "no", "off"}
_CLOUD_BACKENDS = frozenset({"cloud", "cloud-audio", "openrouter"})
_MAX_GAPS = 20
# Дыра короче этого после переноса считается закрытой перерасчётом.
_MIN_HOLE_SEC = 0.5


@dataclass(frozen=True)
class RetimeSettings:
    enabled: bool = True
    model: str = DEFAULT_MODEL
    # Лимит времени локального распознавания, секунд (0 — без лимита).
    max_sec: float = 0.0
    # Не чаще, чем раз в N часов повторять перерасчёт после сбоя (failed).
    retry_hours: float = DEFAULT_RETRY_HOURS

    @classmethod
    def from_env(cls) -> "RetimeSettings":
        enabled = os.getenv(ENABLE_ENV, "").strip().lower() not in _FALSE_VALUES
        model = os.getenv(MODEL_ENV, "").strip() or DEFAULT_MODEL
        try:
            max_sec = float(os.getenv(MAX_SEC_ENV, "").strip() or 0.0)
        except ValueError:
            max_sec = 0.0
        if not math.isfinite(max_sec) or max_sec < 0:
            max_sec = 0.0
        try:
            retry_hours = float(os.getenv(RETRY_HOURS_ENV, "").strip() or DEFAULT_RETRY_HOURS)
        except ValueError:
            retry_hours = DEFAULT_RETRY_HOURS
        if not math.isfinite(retry_hours) or retry_hours < 0:
            retry_hours = DEFAULT_RETRY_HOURS
        return cls(enabled=enabled, model=model, max_sec=max_sec, retry_hours=retry_hours)


@dataclass
class IndexRetimeResult:
    # not_needed | disabled | unavailable | already_retimed | attempt_skipped | no_audio |
    # retimed | rejected | no_words | failed | stopped | cancelled
    status: str
    reason: str = ""
    # Индекс в памяти изменён и его нужно сохранить.
    changed: bool = False
    # Дыры обработаны (или их нет): прежний ремонт дыр по клипам не нужен.
    holes_handled: bool = False
    words_added: int = 0


def retime_fingerprint(settings: RetimeSettings, config: RetimeConfig | None = None) -> str:
    cfg = config or RetimeConfig()
    params = ",".join(f"{k}={v:g}" for k, v in sorted(dataclasses.asdict(cfg).items()))
    return f"{settings.model}|v{RETIME_VERSION}|{params}"


def _faster_whisper_available() -> bool:
    from .whisper_runner import _module_available

    return bool(_module_available("faster_whisper"))


def local_words_path(index_path: Path, source_id: str) -> Path:
    return Path(index_path).with_name(f"{source_id}{LOCAL_WORDS_SUFFIX}")


def _normalize_words(raw: Any) -> list[dict[str, Any]]:
    words: list[dict[str, Any]] = []
    for item in raw or []:
        try:
            text = str(item["word"]).strip()
            start, end = float(item["start"]), float(item["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if text and math.isfinite(start) and math.isfinite(end) and start >= 0 and end >= start:
            words.append({"word": text, "start": round(start, 3), "end": round(end, 3)})
    words.sort(key=lambda w: (w["start"], w["end"]))
    return words


def _load_local_words(
    path: Path, *, model: str, language: str, audio_signature: Any
) -> list[dict[str, Any]] | None:
    """Кэш локальных слов, если он от той же модели/языка/аудио (иначе None)."""
    import json

    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if (
        not isinstance(payload, dict)
        or payload.get("version") != LOCAL_WORDS_VERSION
        or payload.get("model") != model
        or payload.get("language") != language
        or audio_signature is None
        or payload.get("audio") != audio_signature
    ):
        return None
    words = _normalize_words(payload.get("words"))
    return words or None


def _save_local_words(
    path: Path,
    words: list[dict[str, Any]],
    *,
    model: str,
    language: str,
    audio_signature: Any,
    now_iso: str,
) -> None:
    try:
        atomic_write_json(
            path,
            {
                "version": LOCAL_WORDS_VERSION,
                "model": model,
                "language": language,
                "audio": audio_signature,
                "generated_at": now_iso,
                "words": words,
            },
        )
    except OSError:
        logger.warning("Не удалось сохранить локальные слова %s", path, exc_info=True)


def attempt_path(index_path: Path, source_id: str) -> Path:
    return Path(index_path).with_name(f"{source_id}{ATTEMPT_SUFFIX}")


def retime_enabled_and_available(settings: RetimeSettings | None = None) -> bool:
    """Перерасчёт включён и движок есть: облачный путь не чинит дыры по клипам сам."""
    settings = settings or RetimeSettings.from_env()
    if not settings.enabled:
        return False
    try:
        return _faster_whisper_available()
    except Exception:
        return False


def _load_attempt(path: Path | None) -> dict[str, Any] | None:
    import json

    if path is None:
        return None
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if isinstance(payload, dict) and payload.get("version") == ATTEMPT_VERSION:
        return payload
    return None


def _parse_iso(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _skip_attempt(
    attempt: dict[str, Any] | None,
    *,
    fingerprint: str,
    audio_signature: Any,
    identity: str,
    settings: RetimeSettings,
    now: str,
) -> bool:
    """Повторять ли попытку: прежняя неудача для тех же текста, аудио и параметров.

    - no_words / rejected: детерминированный итог, повтор бессмыслен;
    - stopped: повтор, только если лимит MAX_SEC вырос (или снят);
    - failed: повтор не чаще раза в `retry_hours`.
    Другой отпечаток (модель/версия/параметры) всегда даёт новую попытку; по кэшу
    локальных слов она не требует Whisper.
    """
    if (
        not attempt
        or attempt.get("identity") != identity
        or attempt.get("audio") != audio_signature
        or attempt.get("fingerprint") != fingerprint
    ):
        return False
    status = attempt.get("status")
    if status in {"no_words", "rejected"}:
        return True
    if status == "stopped":
        try:
            previous = float(attempt.get("max_sec") or 0.0)
        except (TypeError, ValueError):
            return False
        return not (settings.max_sec == 0 and previous != 0 or settings.max_sec > previous)
    if status == "failed":
        before, current = _parse_iso(attempt.get("attempted_at")), _parse_iso(now)
        if before is None or current is None:
            return False
        return (current - before).total_seconds() < settings.retry_hours * 3600
    return False


def _write_attempt(
    path: Path | None,
    status: str,
    *,
    reason: str = "",
    fingerprint: str,
    settings: RetimeSettings,
    audio_signature: Any,
    identity: str,
    now: str,
    metrics: dict[str, Any] | None = None,
) -> None:
    if path is None:
        return
    record: dict[str, Any] = {
        "version": ATTEMPT_VERSION,
        "status": status,
        "reason": reason,
        "error": reason if status == "failed" else "",
        "attempted_at": now,
        "fingerprint": fingerprint,
        "model": settings.model,
        "max_sec": settings.max_sec,
        "audio": audio_signature,
        "identity": identity,
    }
    if metrics is not None:
        record["metrics"] = metrics
    try:
        atomic_write_json(path, record)
    except OSError:
        logger.warning("Не удалось сохранить маркер попытки перерасчёта %s", path, exc_info=True)


def _clear_attempt(path: Path | None) -> None:
    if path is not None:
        try:
            Path(path).unlink(missing_ok=True)
        except OSError:
            logger.warning("Не удалось удалить маркер попытки %s", path, exc_info=True)


def _to_words(raw: list[dict[str, Any]]) -> list[WordTimestamp]:
    return [WordTimestamp(w["word"], w["start"], w["end"]) for w in raw]


def _remap_ranges(
    ranges: list[tuple[float, float]],
    old: list[WordTimestamp],
    new: list[WordTimestamp],
    *,
    min_sec: float = _MIN_HOLE_SEC,
) -> tuple[list[tuple[float, float]], int]:
    """Диапазоны облачного времени -> новое время по соседним словам.

    Возвращает `(диапазоны, число схлопнувшихся)`: диапазон, ставший не длиннее
    `min_sec` (закрыт перерасчётом) или нулевой/отрицательной длины, считается, а не
    отбрасывается молча. Для вставок прежнего ремонта передают `min_sec=0.0`.
    """
    if not old:
        return ranges, 0
    starts = [w.start for w in old]
    mapped: list[tuple[float, float]] = []
    collapsed = 0
    for a, b in ranges:
        prev = bisect_left(starts, a) - 1
        nxt = bisect_left(starts, b)
        new_a = new[prev].end if prev >= 0 else max(0.0, a + new[0].start - old[0].start)
        new_b = new[nxt].start if nxt < len(new) else b + new[-1].end - old[-1].end
        if new_b - new_a > min_sec:
            mapped.append((new_a, new_b))
        else:
            collapsed += 1
    return _merge(mapped), collapsed


def _marker_fingerprint(index: WordIndex) -> str | None:
    marker = (index.metadata.get("backend_metadata") or {}).get("retiming")
    if isinstance(marker, dict) and isinstance(marker.get("fingerprint"), str):
        return marker["fingerprint"]
    return None


def _fill_holes(
    index: WordIndex,
    coverage: dict[str, Any],
    local_words: list[dict[str, Any]],
    *,
    model: str,
    fingerprint: str,
    now_iso: str,
    holes: list[tuple[float, float]] | None = None,
    new_words: list[WordTimestamp] | None = None,
) -> int:
    """Заполняет unrecovered-дыры локальными словами; возвращает число слов."""
    words = new_words if new_words is not None else index.words
    holes = holes if holes is not None else _merge(_ranges(coverage.get("unrecovered_ranges")))
    sparse = _sparse_holes(words, holes)
    old_dicts = [
        {"word": w.word, "start": w.start, "end": w.end, "confidence": w.confidence} for w in words
    ]
    filled, repaired, silent, unrecovered, added = local_hole_repair.fill_holes_from_words(
        old_dicts, sparse, local_words
    )
    marker = {
        "enabled": True,
        "model": model,
        "fingerprint": f"{MARKER_SOURCE}|{fingerprint}",
        "repaired_at": now_iso,
        "source": MARKER_SOURCE,
        "words_added": added,
        "errors": [],
        "stopped": "",
        "complete": True,
    }
    apply_hole_outcome(
        index,
        coverage,
        new_words=[WordTimestamp.from_dict(w) for w in filled] if repaired else list(words),
        # Плотные дыры не дыры (как в облачном отчёте): в unrecovered не входят.
        still=_merge(unrecovered),
        local_repaired=_merge(_ranges(coverage.get("local_repaired_ranges")) + repaired),
        silent=_merge(_ranges(coverage.get("silent_ranges")) + silent),
        marker=marker,
    )
    return added


def _metrics_dict(metrics: RetimeMetrics) -> dict[str, Any]:
    return {
        "anchor_count": metrics.anchor_count,
        "anchor_ratio": round(metrics.anchor_ratio, 4),
        "exact_count": metrics.exact_count,
        "fuzzy_count": metrics.fuzzy_count,
        "interp_count": metrics.interp_count,
        "dropped_by_cap": metrics.dropped_by_cap,
        "dropped_by_drift": metrics.dropped_by_drift,
        "dropped_non_monotone": metrics.dropped_non_monotone,
        "duration_ratio": round(metrics.duration_ratio, 3),
        "word_count_ratio": round(metrics.word_count_ratio, 3),
        "shift_median_sec": round(metrics.shift_median_sec, 3),
        "shift_p90_sec": round(metrics.shift_p90_sec, 3),
        "shift_max_sec": round(metrics.shift_max_sec, 3),
        "anchor_gaps": [[round(a, 3), round(b, 3)] for a, b in metrics.anchor_gaps[:_MAX_GAPS]],
    }


def retime_cached_index(
    index: WordIndex,
    audio_path: Path | None,
    *,
    audio_signature: Any,
    local_words_file: Path,
    language: str,
    now_iso: Callable[[], str],
    transcribe: Callable[..., list[dict[str, Any]]],
    attempt_file: Path | None = None,
    retime_map_file: Path | None = None,
    index_identity: str = "",
    slot: Callable[[], ContextManager[Any]] | None = None,
    settings: RetimeSettings | None = None,
    config: RetimeConfig | None = None,
    cancel: Any | None = None,
    on_progress: Callable[[int], None] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    passthrough: tuple[type[BaseException], ...] = (),
) -> IndexRetimeResult:
    """Перерасчёт времени слов облачного индекса (синхронно, на месте).

    `transcribe(audio_path, model, language, progress_callback)` — локальное
    распознавание всего файла (слова `{word, start, end}`). Все проверки (нужен ли
    перерасчёт, маркеры, попытки, кэш локальных слов) идут до `slot`: слот
    распознавания (контекст-менеджер) берётся только вокруг `transcribe`.
    Исключения из `passthrough` (отмена) пробрасываются, остальные дают статус
    `failed`. При любом статусе кроме retimed (и дозаполнения дыр) индекс не меняется;
    итог неудачной попытки пишется в `attempt_file` (не в индекс). При успехе соответствие
    «старое -> новое время слов» пишется в `retime_map_file` (не в индекс, см. шапку
    модуля): по нему пересчитываются границы подтверждённых матчей.
    """
    metadata = index.metadata
    backend_metadata = metadata.get("backend_metadata")
    if (
        str(metadata.get("backend") or "").strip().lower() not in _CLOUD_BACKENDS
        or not isinstance(backend_metadata, dict)
        or not index.words
    ):
        return IndexRetimeResult("not_needed", holes_handled=True)
    settings = settings or RetimeSettings.from_env()
    if not settings.enabled:
        return IndexRetimeResult("disabled")
    try:
        available = _faster_whisper_available()
    except Exception:  # проверка наличия движка не должна валить фазу
        available = False
    if not available:
        return IndexRetimeResult("unavailable", "faster-whisper не установлен")
    config = config or RetimeConfig()
    fingerprint = retime_fingerprint(settings, config)
    coverage = index_coverage(index)
    holes = _merge(_ranges(coverage.get("unrecovered_ranges"))) if coverage else []
    repair_marker = coverage.get("local_repair") if coverage else None
    holes_done = isinstance(repair_marker, dict) and repair_marker.get("source") == MARKER_SOURCE

    if _marker_fingerprint(index) == fingerprint:
        if not holes or holes_done:
            return IndexRetimeResult("already_retimed", holes_handled=True)
        cached = _load_local_words(
            local_words_file,
            model=settings.model,
            language=language,
            audio_signature=audio_signature,
        )
        if cached is None:
            return IndexRetimeResult("already_retimed")
        assert coverage is not None
        added = _fill_holes(
            index,
            coverage,
            cached,
            model=settings.model,
            fingerprint=fingerprint,
            now_iso=now_iso(),
        )
        return IndexRetimeResult("retimed", changed=True, holes_handled=True, words_added=added)

    if audio_path is None or audio_signature is None or not Path(audio_path).is_file():
        # Без аудио ничего не делалось: маркера нет, предупреждение отражает
        # текущее состояние и пропадёт, когда аудио появится (как у ремонта дыр).
        return IndexRetimeResult("no_audio", "аудио источника не найдено")

    if _skip_attempt(
        _load_attempt(attempt_file),
        fingerprint=fingerprint,
        audio_signature=audio_signature,
        identity=index_identity,
        settings=settings,
        now=now_iso(),
    ):
        return IndexRetimeResult("attempt_skipped")

    def record(status: str, reason: str = "", metrics: dict[str, Any] | None = None) -> None:
        _write_attempt(
            attempt_file,
            status,
            reason=reason,
            fingerprint=fingerprint,
            settings=settings,
            audio_signature=audio_signature,
            identity=index_identity,
            now=now_iso(),
            metrics=metrics,
        )

    local_raw = _load_local_words(
        local_words_file, model=settings.model, language=language, audio_signature=audio_signature
    )
    fresh = False
    if local_raw is None:

        def progress(processed: float, duration: float, _words: int = 0) -> None:
            if on_progress is not None and duration > 0:
                on_progress(max(0, min(100, int(processed / duration * 100))))

        started = 0.0
        try:
            with slot() if slot is not None else nullcontext():
                # Лимит считается от начала распознавания, а не от очереди за слотом.
                started = monotonic()
                local_raw = _normalize_words(
                    transcribe(Path(audio_path), settings.model, language, progress)
                )
        except passthrough:
            raise
        except Exception as exc:
            if cancel is not None and cancel.is_set():
                return IndexRetimeResult("cancelled")
            if settings.max_sec > 0 and monotonic() - started >= settings.max_sec:
                reason = f"лимит времени {settings.max_sec:g} с ({type(exc).__name__})"
                record("stopped", reason)
                return IndexRetimeResult("stopped", reason)
            logger.warning("Перерасчёт времени слов не выполнен: %s", exc, exc_info=True)
            reason = f"{type(exc).__name__}: {exc}"
            record("failed", reason)
            return IndexRetimeResult("failed", reason)
        if cancel is not None and cancel.is_set():
            return IndexRetimeResult("cancelled")
        if not local_raw:
            reason = "локальное распознавание не нашло слов"
            record("no_words", reason)
            return IndexRetimeResult("no_words", reason)
        fresh = True
    if fresh:
        _save_local_words(
            local_words_file,
            local_raw,
            model=settings.model,
            language=language,
            audio_signature=audio_signature,
            now_iso=now_iso(),
        )

    # Вставки прежнего локального ремонта лежат в облачных координатах: убираем их
    # до перерасчёта и заполняем диапазоны заново из локальных слов.
    fixed_ranges = _ranges(coverage.get("local_repaired_ranges")) if coverage else []
    movable = [w for w in index.words if not any(a <= w.start < b for a, b in fixed_ranges)]
    outcome = retime_words(movable, _to_words(local_raw), config=config)
    metrics = outcome.metrics
    rejection = quality_rejection(metrics, config) if movable else "нет облачных слов"
    if rejection is not None:
        record("rejected", rejection, _metrics_dict(metrics))
        return IndexRetimeResult("rejected", rejection)

    new_movable = outcome.words
    marker: dict[str, Any] = {
        "fingerprint": fingerprint,
        "model": settings.model,
        "version": RETIME_VERSION,
        "applied": True,
        **_metrics_dict(metrics),
        "removed_local_inserts": len(index.words) - len(movable),
        "retimed_at": now_iso(),
    }
    new_backend_metadata = {**backend_metadata, "retiming": marker}
    new_backend_metadata.setdefault(
        "timestamp_precision_original", backend_metadata.get("timestamp_precision", "")
    )
    new_backend_metadata["timestamp_precision"] = PRECISION
    holes_to_fill: list[tuple[float, float]] = []
    if coverage is not None:
        unrecovered, lost_a = _remap_ranges(
            _ranges(coverage.get("unrecovered_ranges")), movable, new_movable
        )
        silent, lost_b = _remap_ranges(_ranges(coverage.get("silent_ranges")), movable, new_movable)
        reinserted, lost_c = _remap_ranges(fixed_ranges, movable, new_movable, min_sec=0.0)
        marker["holes_collapsed"] = lost_a + lost_b + lost_c
        holes_to_fill = _merge(unrecovered + reinserted)
        new_coverage = dict(coverage)
        new_coverage.update(
            {
                "unrecovered_ranges": _rounded(holes_to_fill),
                "unrecovered_sec": round(sum(b - a for a, b in holes_to_fill), 3),
                "silent_ranges": _rounded(silent),
                "silent_sec": round(sum(b - a for a, b in silent), 3),
                "local_repaired_ranges": [],
                "holes_repaired_local": 0,
            }
        )
        new_backend_metadata["coverage"] = new_coverage
    if retime_map_file is not None:
        save_retime_map(
            retime_map_file,
            RetimeMap.from_words(
                movable, new_movable, fingerprint=fingerprint, retimed_at=marker["retimed_at"]
            ),
        )
    index.words = new_movable
    index.metadata["backend_metadata"] = new_backend_metadata
    index.metadata["timestamp_precision"] = PRECISION
    _clear_attempt(attempt_file)
    added = 0
    new_coverage_now = index_coverage(index)
    if new_coverage_now is not None and holes_to_fill:
        added = _fill_holes(
            index,
            new_coverage_now,
            local_raw,
            model=settings.model,
            fingerprint=fingerprint,
            now_iso=now_iso(),
            holes=holes_to_fill,
        )
    return IndexRetimeResult("retimed", changed=True, holes_handled=True, words_added=added)
