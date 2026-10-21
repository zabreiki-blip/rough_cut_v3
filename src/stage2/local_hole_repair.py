"""Локальный ремонт «дыр» облачного ASR: проверка речи (VAD) и распознавание
faster-whisper там, где облако пропустило речь и повтор по дыре не помог.

Облачная модель иногда молча пропускает минуты громкой речи без единой паузы
(см. `cloud_asr.find_coverage_holes`), а повторный запрос по дыре возвращает
то же самое. Для оставшихся «unrecovered» дыр:

1. фрагмент дыры декодируется и проверяется VAD из faster-whisper (Silero):
   речи меньше порога — дыра считается тишиной (ложная дыра), а не потерей;
2. иначе фрагмент с полем вокруг распознаётся локально через существующий
   `whisper_runner._transcribe_faster` (та же загрузка/кэш модели и настройки
   SYNC_WHISPER_*), слова сдвигаются на абсолютное время, оставляются только
   попавшие в дыру и вклеиваются в результат вместо разреженных облачных реплик.

Слова локального прохода имеют реальные таймкоды (в отличие от линейной
интерполяции облачных реплик); происхождение вставок помечается полем
`AsrSegment.timestamp_source` и диапазонами `coverage.local_repaired_ranges`.
Любая ошибка локального движка не валит распознавание: дыра остаётся
невосстановленной, причина попадает в `coverage.local_repair`.
"""

from __future__ import annotations

import logging
import math
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .asr_contract import AsrProgress, AsrSegment, AsrWord

logger = logging.getLogger(__name__)

ENABLE_ENV = "SYNC_CLOUD_ASR_LOCAL_REPAIR"
MODEL_ENV = "SYNC_CLOUD_ASR_LOCAL_REPAIR_MODEL"
MARGIN_ENV = "SYNC_CLOUD_ASR_LOCAL_REPAIR_MARGIN_SEC"
MIN_SPEECH_ENV = "SYNC_CLOUD_ASR_LOCAL_REPAIR_MIN_SPEECH_SEC"
MAX_SEC_ENV = "SYNC_CLOUD_ASR_LOCAL_REPAIR_MAX_SEC"
DEADLINE_RESERVE_ENV = "SYNC_CLOUD_ASR_LOCAL_REPAIR_DEADLINE_RESERVE_SEC"
DEFAULT_MODEL = "medium"
DEFAULT_MARGIN_SEC = 3.0
DEFAULT_MIN_SPEECH_SEC = 1.0
# Запас до общего дедлайна распознавания: локальная фаза необязательна и
# обязана закончиться раньше, чтобы облачный (оплаченный) результат не пропал.
DEFAULT_DEADLINE_RESERVE_SEC = 30.0
TEMPDIR_PREFIX = "rough_cut_local_repair_"
_STALE_TEMPDIR_AGE_SEC = 3600.0
TIMESTAMP_SOURCE = "local-whisper"
# Реплика из локальных слов закрывается на паузе длиннее этого, на конце
# предложения или после стольких слов.
_SEGMENT_GAP_SEC = 1.0
_SEGMENT_MAX_WORDS = 25
# Сколько слов по краям сравнивается с соседними облачными репликами при
# отбрасывании дублей на границе дыры (совпадение — не меньше двух слов).
_DEDUP_WINDOW = 8
_DEDUP_MIN_WORDS = 2
_EXTRACT_TIMEOUT_SEC = 300.0
_SAMPLE_RATE = 16000
# Столько подряд неудач движка — и остальные дыры не пробуем (загрузка модели
# без сети на каждую дыру только тратила бы время).
_MAX_CONSECUTIVE_FAILURES = 2
_MAX_REPORTED_ERRORS = 5
_FALSE_VALUES = {"0", "false", "no", "off"}
_WORD_RE = re.compile(r"\w+", re.UNICODE)


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if math.isfinite(value) and value >= 0 else default


@dataclass(frozen=True)
class LocalRepairSettings:
    enabled: bool = True
    model: str = DEFAULT_MODEL
    margin_sec: float = DEFAULT_MARGIN_SEC
    min_speech_sec: float = DEFAULT_MIN_SPEECH_SEC
    # Собственный лимит времени локальной фазы, секунд (0 — без лимита).
    max_sec: float = 0.0
    deadline_reserve_sec: float = DEFAULT_DEADLINE_RESERVE_SEC

    @classmethod
    def from_env(cls) -> "LocalRepairSettings":
        enabled = os.getenv(ENABLE_ENV, "").strip().lower() not in _FALSE_VALUES
        model = os.getenv(MODEL_ENV, "").strip()
        if not model:
            # SYNC_WHISPER_MODEL при облачном бэкенде может быть id облачной
            # модели или mlx-репозиторием ("provider/model"); как имя
            # faster-whisper это не годится (ср. whisper_runner._default_model,
            # где "/" тоже означает «не локальный размер Whisper»).
            configured = os.getenv("SYNC_WHISPER_MODEL", "").strip()
            model = configured if configured and "/" not in configured else DEFAULT_MODEL
        return cls(
            enabled=enabled,
            model=model,
            margin_sec=_env_float(MARGIN_ENV, DEFAULT_MARGIN_SEC),
            min_speech_sec=_env_float(MIN_SPEECH_ENV, DEFAULT_MIN_SPEECH_SEC),
            max_sec=_env_float(MAX_SEC_ENV, 0.0),
            deadline_reserve_sec=_env_float(DEADLINE_RESERVE_ENV, DEFAULT_DEADLINE_RESERVE_SEC),
        )


def unavailable_reason(settings: LocalRepairSettings) -> str | None:
    """Почему локальный ремонт не выполняется (None — можно выполнять)."""
    if not settings.enabled:
        return "disabled"
    from .whisper_runner import _module_available

    if not _module_available("faster_whisper"):
        return "faster-whisper не установлен"
    return None


# Текст ошибки «слов не больше, чем было» (не сбой движка); по нему отличают
# такие записи в `report["errors"]` от настоящих ошибок распознавания.
NO_NEW_WORDS_ERROR = "локальное распознавание не дало новых слов"


class _Stop(BaseException):
    """Остановка по отмене/дедлайну изнутри колбэка прогресса движка.

    BaseException: `whisper_runner._emit_progress` глушит обычные исключения
    колбэка, а отмену нужно пробросить до цикла ремонта.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass
class LocalRepairOutcome:
    segments: list[AsrSegment] = field(default_factory=list)
    own_end: list[bool] = field(default_factory=list)
    # Слова (абсолютное время) для word-level ремонта готового индекса
    # (`repair_words_locally`); None — ремонт шёл по репликам.
    words: list[dict[str, Any]] | None = None
    # Дыры, где упал сам движок (загрузка модели, ffmpeg, транскрипция), в
    # отличие от «слов не больше, чем было»: такие неудачи могут быть
    # временными, и вызывающий не должен считать попытку завершённой.
    engine_failures: int = 0
    repaired: list[tuple[float, float]] = field(default_factory=list)
    silent: list[tuple[float, float]] = field(default_factory=list)
    unrecovered: list[tuple[float, float]] = field(default_factory=list)
    report: dict[str, Any] = field(default_factory=dict)
    stopped: str | None = None


# -- работа с аудио и движком (точки подмены в тестах) ------------------------


class _Clip:
    """Фрагмент аудио: временный wav 16 кГц моно + декодированные отсчёты."""

    def __init__(self, directory: Path, path: Path, samples: Any) -> None:
        self._directory = directory
        self.path = path
        self.samples = samples

    def cleanup(self) -> None:
        shutil.rmtree(self._directory, ignore_errors=True)


def cleanup_stale_tempdirs(max_age_sec: float = _STALE_TEMPDIR_AGE_SEC) -> int:
    """Удаляет осиротевшие каталоги вырезок (прерванный/убитый процесс).

    Только каталоги с префиксом `TEMPDIR_PREFIX` прямо в `tempfile.gettempdir()`,
    старше `max_age_sec`; симлинки и файлы не трогаются. Возвращает число
    удалённых каталогов; ошибки файловой системы не пробрасываются.
    """
    removed = 0
    try:
        root = Path(tempfile.gettempdir())
        cutoff = time.time() - max_age_sec
        for entry in root.iterdir():
            try:
                if (
                    not entry.name.startswith(TEMPDIR_PREFIX)
                    or entry.is_symlink()
                    or not entry.is_dir()
                    or entry.stat().st_mtime >= cutoff
                ):
                    continue
                shutil.rmtree(entry, ignore_errors=True)
                removed += 1
            except OSError:
                continue
    except OSError:
        logger.debug("Не удалось просмотреть каталог временных файлов", exc_info=True)
    return removed


def _load_clip(audio_path: Path, start: float, end: float, *, ffmpeg: str = "ffmpeg") -> _Clip:
    """Вырезает [start, end] ffmpeg-ом в wav 16 кГц моно и декодирует отсчёты."""
    directory = Path(tempfile.mkdtemp(prefix=TEMPDIR_PREFIX))
    try:
        wav = directory / "clip.wav"
        args = [
            ffmpeg,
            "-nostdin",
            "-y",
            "-ss",
            f"{max(0.0, start):.3f}",
            "-t",
            f"{max(0.01, end - start):.3f}",
            "-i",
            str(audio_path),
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(_SAMPLE_RATE),
            "-codec:a",
            "pcm_s16le",
            "-loglevel",
            "error",
            str(wav),
        ]
        try:
            proc = subprocess.run(
                args, capture_output=True, timeout=_EXTRACT_TIMEOUT_SEC, check=False
            )
        except FileNotFoundError as exc:
            raise RuntimeError(f"Не найден исполняемый файл {ffmpeg!r}") from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("ffmpeg не ответил при нарезке фрагмента") from exc
        if proc.returncode != 0:
            detail = proc.stderr.decode("utf-8", errors="replace").strip()[-300:]
            raise RuntimeError(f"ffmpeg завершился с кодом {proc.returncode}: {detail}")
        from faster_whisper import decode_audio  # type: ignore[import-not-found]

        samples = decode_audio(str(wav), sampling_rate=_SAMPLE_RATE)
        return _Clip(directory, wav, samples)
    except BaseException:
        shutil.rmtree(directory, ignore_errors=True)
        raise


def _speech_seconds(samples: Any, lo_sec: float, hi_sec: float) -> float:
    """Суммарная длительность речи (Silero VAD из faster-whisper) на [lo, hi]."""
    from faster_whisper.vad import VadOptions, get_speech_timestamps  # type: ignore

    window = samples[int(max(0.0, lo_sec) * _SAMPLE_RATE) : int(max(0.0, hi_sec) * _SAMPLE_RATE)]
    if len(window) == 0:
        return 0.0
    options = VadOptions(min_speech_duration_ms=250, speech_pad_ms=0)
    chunks = get_speech_timestamps(window, options, sampling_rate=_SAMPLE_RATE)
    return sum(c["end"] - c["start"] for c in chunks) / _SAMPLE_RATE


def _transcribe_clip(
    clip_path: Path,
    model: str,
    language: str,
    progress_callback: Callable[[float, float, int], None] | None = None,
) -> list[dict[str, Any]]:
    """Слова фрагмента (время от начала фрагмента) через существующий путь
    faster-whisper (`whisper_runner._transcribe_faster`: кэш модели, SYNC_WHISPER_*,
    word_timestamps=True, vad_filter=True)."""
    from .whisper_runner import _transcribe_faster

    words, _meta = _transcribe_faster(clip_path, model, language, progress_callback)
    return words


# -- сборка результата ---------------------------------------------------------


def _tokens(text: str) -> list[str]:
    return [t.replace("ё", "е") for t in _WORD_RE.findall(text.lower())]


def _flat_tokens(words: list[dict[str, Any]]) -> list[str]:
    return [token for word in words for token in _tokens(word["word"])]


def _trim_edge_duplicates(
    words: list[dict[str, Any]],
    previous_tokens: list[str],
    following_tokens: list[str],
) -> list[dict[str, Any]]:
    """Отбрасывает слова на краях, повторяющие токены соседей (совпадение не
    короче `_DEDUP_MIN_WORDS` слов, берётся самое длинное)."""
    if previous_tokens:
        tail = previous_tokens[-_DEDUP_WINDOW:]
        for k in range(min(len(words), _DEDUP_WINDOW), 0, -1):
            head = _flat_tokens(words[:k])
            if _DEDUP_MIN_WORDS <= len(head) <= len(tail) and tail[-len(head) :] == head:
                words = words[k:]
                break
    if following_tokens and words:
        head_tokens = following_tokens[:_DEDUP_WINDOW]
        for k in range(min(len(words), _DEDUP_WINDOW), 0, -1):
            tail = _flat_tokens(words[-k:])
            if (
                _DEDUP_MIN_WORDS <= len(tail) <= len(head_tokens)
                and head_tokens[: len(tail)] == tail
            ):
                words = words[:-k]
                break
    return words


def _trim_boundary_duplicates(
    words: list[dict[str, Any]],
    previous: AsrSegment | None,
    following: AsrSegment | None,
) -> list[dict[str, Any]]:
    """То же для соседних облачных реплик."""
    return _trim_edge_duplicates(
        words,
        _tokens(previous.text) if previous is not None else [],
        _tokens(following.text) if following is not None else [],
    )


def _localize_words(
    raw_words: list[dict[str, Any]], hole: tuple[float, float], clip_start: float
) -> list[dict[str, Any]]:
    """Слова фрагмента в абсолютное время; остаются только попавшие в дыру
    (по середине слова), по порядку времени."""
    hole_start, hole_end = hole
    shifted = [
        {**w, "start": round(clip_start + w["start"], 3), "end": round(clip_start + w["end"], 3)}
        for w in raw_words
    ]
    inside = [w for w in shifted if hole_start <= (w["start"] + w["end"]) / 2.0 < hole_end]
    inside.sort(key=lambda w: (w["start"], w["end"]))
    return inside


def _build_segments(words: list[dict[str, Any]], speaker: str | None = None) -> list[AsrSegment]:
    groups: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for word in words:
        if current and (
            word["start"] - current[-1]["end"] > _SEGMENT_GAP_SEC
            or len(current) >= _SEGMENT_MAX_WORDS
            or current[-1]["word"].rstrip().endswith((".", "?", "!", "…"))
        ):
            groups.append(current)
            current = []
        current.append(word)
    if current:
        groups.append(current)
    segments: list[AsrSegment] = []
    for group in groups:
        segments.append(
            AsrSegment(
                text=" ".join(w["word"].strip() for w in group),
                start=group[0]["start"],
                end=group[-1]["end"],
                words=[
                    AsrWord(word=w["word"].strip(), start=w["start"], end=w["end"]) for w in group
                ],
                speaker=speaker,
                has_explicit_end=True,
                timestamp_source=TIMESTAMP_SOURCE,
            )
        )
    return segments


def _word_count(text: str) -> int:
    return len(text.split())


def _splice(
    segments: list[AsrSegment],
    own_end: list[bool],
    hole: tuple[float, float],
    raw_words: list[dict[str, Any]],
    clip_start: float,
) -> tuple[list[AsrSegment], list[bool], int] | None:
    """Вклеивает слова в дыру; None — если слов не больше, чем было."""
    hole_start, hole_end = hole
    inside = _localize_words(raw_words, hole, clip_start)
    in_hole = [s for s in segments if hole_start - 0.01 <= s.start < hole_end]
    outside = [s for s in segments if s not in in_hole]
    previous = max(
        (s for s in outside if s.start < hole_start), key=lambda s: s.start, default=None
    )
    following = min(
        (s for s in outside if s.start >= hole_end), key=lambda s: s.start, default=None
    )
    inside = _trim_boundary_duplicates(inside, previous, following)
    old_words = sum(_word_count(s.text) for s in in_hole)
    if len(inside) <= old_words:
        return None
    # Говорящего локальный Whisper не определяет: берём его у соседней
    # облачной реплики (предыдущей, иначе следующей), если он у неё есть.
    speaker = next((n.speaker for n in (previous, following) if n is not None and n.speaker), None)
    new_segments = _build_segments(inside, speaker)
    kept = [
        (s, f) for s, f in zip(segments, own_end) if not (hole_start - 0.01 <= s.start < hole_end)
    ]
    merged = sorted(kept + [(s, True) for s in new_segments], key=lambda item: item[0].start)
    return [m[0] for m in merged], [m[1] for m in merged], len(inside)


def _splice_words(
    words: list[dict[str, Any]],
    hole: tuple[float, float],
    raw_words: list[dict[str, Any]],
    clip_start: float,
) -> tuple[list[dict[str, Any]], int] | None:
    """Word-level аналог `_splice`: слова индекса, начинающиеся в дыре
    (разреженные облачные), заменяются локальными, только если тех больше;
    края дедуплицируются относительно соседних слов вне дыры."""
    hole_start, hole_end = hole
    inside = _localize_words(raw_words, hole, clip_start)
    in_hole = [w for w in words if hole_start - 0.01 <= w["start"] < hole_end]
    before = [w for w in words if w["start"] < hole_start - 0.01]
    after = [w for w in words if w["start"] >= hole_end]
    inside = _trim_edge_duplicates(
        inside,
        _flat_tokens(before[-_DEDUP_WINDOW:]),
        _flat_tokens(after[:_DEDUP_WINDOW]),
    )
    if len(inside) <= len(in_hole):
        return None
    merged = sorted(before + inside + after, key=lambda w: (w["start"], w["end"]))
    return merged, len(inside)


def _repair_loop(
    audio_path: Path,
    holes: list[tuple[float, float]],
    outcome: LocalRepairOutcome,
    apply: Callable[[tuple[float, float], list[dict[str, Any]], float], int | None],
    *,
    duration_sec: float,
    language: str,
    settings: LocalRepairSettings,
    cancel: Any | None = None,
    deadline_monotonic: float | None = None,
    on_progress: Callable[[AsrProgress], None] | None = None,
    ffmpeg: str = "ffmpeg",
    abort: threading.Event | None = None,
    transcribe: Callable[..., list[dict[str, Any]]] | None = None,
) -> LocalRepairOutcome:
    """Общий цикл по дырам: фрагмент -> VAD -> распознавание -> `apply`.

    `apply(hole, слова фрагмента, начало фрагмента)` вклеивает результат в
    состояние вызывающего (реплики или слова) и возвращает число добавленных
    слов либо None, если локальных слов не больше, чем было. `transcribe` —
    замена `_transcribe_clip` (например, процесс-воркер): вызывается как
    `transcribe(clip_path, model, language, progress_callback=...)`.

    `abort` — сигнал «результат больше не нужен» (дедлайн вызывающего истёк,
    а поток прервать нельзя, например во время загрузки модели): как только
    он взведён, функция останавливается на ближайшей проверке (колбэк
    прогресса движка, границы дыр) и больше не вызывает `on_progress`.
    """
    cleanup_stale_tempdirs()
    errors: list[dict[str, Any]] = []
    words_added = 0
    consecutive_failures = 0

    def check_stop() -> None:
        if cancel is not None and cancel.is_set():
            raise _Stop("cancelled")
        if abort is not None and abort.is_set():
            raise _Stop("deadline")
        if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
            raise _Stop("deadline")

    def engine_progress(_processed: float, _duration: float, _count: int) -> None:
        check_stop()

    def fail(hole: tuple[float, float], error: str) -> None:
        outcome.unrecovered.append(hole)
        if len(errors) < _MAX_REPORTED_ERRORS:
            errors.append({"range": [round(hole[0], 3), round(hole[1], 3)], "error": error})

    pending = list(holes)
    while pending:
        hole = pending.pop(0)
        index = len(holes) - len(pending)
        try:
            check_stop()
            if consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
                outcome.unrecovered.append(hole)
                continue
            hole_start, hole_end = hole
            if on_progress is not None:
                try:
                    on_progress(
                        AsrProgress(
                            done=index - 1,
                            total=len(holes),
                            message=(
                                f"Локальное распознавание дыры {index} из {len(holes)} "
                                f"({hole_start:.0f}–{hole_end:.0f} с)"
                            ),
                            processed_sec=hole_start,
                            duration_sec=duration_sec or None,
                        )
                    )
                except Exception:  # сбой UI-колбэка не должен терять починенные дыры
                    logger.warning("Колбэк прогресса локального ремонта упал", exc_info=True)
            clip_start = max(0.0, hole_start - settings.margin_sec)
            clip_end = (
                min(duration_sec, hole_end + settings.margin_sec)
                if duration_sec
                else (hole_end + settings.margin_sec)
            )
            clip = None
            try:
                clip = _load_clip(audio_path, clip_start, clip_end, ffmpeg=ffmpeg)
                speech = _speech_seconds(
                    clip.samples, hole_start - clip_start, hole_end - clip_start
                )
                if speech < settings.min_speech_sec:
                    outcome.silent.append(hole)
                    consecutive_failures = 0
                    continue
                raw_words = (transcribe or _transcribe_clip)(
                    clip.path, settings.model, language, progress_callback=engine_progress
                )
            except _Stop:
                raise
            except Exception as exc:  # движок не должен валить всё распознавание
                logger.warning(
                    "Локальный ремонт дыры %.1f–%.1f с не удался: %s",
                    hole_start,
                    hole_end,
                    exc,
                    exc_info=True,
                )
                consecutive_failures += 1
                outcome.engine_failures += 1
                fail(hole, f"{type(exc).__name__}: {exc}")
                continue
            finally:
                if clip is not None:
                    clip.cleanup()
            consecutive_failures = 0
            check_stop()  # результат брошенного потока не нужен
            added = apply(hole, list(raw_words), clip_start)
            if added is None:
                fail(hole, NO_NEW_WORDS_ERROR)
                continue
            words_added += added
            outcome.repaired.append(hole)
        except _Stop as stop:
            outcome.stopped = stop.reason
            outcome.unrecovered.append(hole)
            outcome.unrecovered.extend(pending)
            pending = []
    outcome.report = {
        "enabled": True,
        "model": settings.model,
        "margin_sec": settings.margin_sec,
        "min_speech_sec": settings.min_speech_sec,
        "words_added": words_added,
        "errors": errors,
    }
    if outcome.stopped:
        outcome.report["stopped"] = outcome.stopped
    return outcome


def repair_holes_locally(
    audio_path: Path,
    holes: list[tuple[float, float]],
    segments: list[AsrSegment],
    own_end: list[bool],
    *,
    duration_sec: float,
    language: str,
    settings: LocalRepairSettings,
    cancel: Any | None = None,
    deadline_monotonic: float | None = None,
    on_progress: Callable[[AsrProgress], None] | None = None,
    ffmpeg: str = "ffmpeg",
    abort: threading.Event | None = None,
) -> LocalRepairOutcome:
    """Проверяет и чинит дыры локально по репликам (синхронно: вызывать из
    потока). Все результаты накапливаются только в локальном
    `LocalRepairOutcome`, общих структур поток не меняет."""
    outcome = LocalRepairOutcome(segments=list(segments), own_end=list(own_end))

    def apply(hole: tuple[float, float], raw_words: list[dict[str, Any]], clip_start: float):
        spliced = _splice(outcome.segments, outcome.own_end, hole, raw_words, clip_start)
        if spliced is None:
            return None
        outcome.segments, outcome.own_end, added = spliced
        return added

    return _repair_loop(
        audio_path,
        holes,
        outcome,
        apply,
        duration_sec=duration_sec,
        language=language,
        settings=settings,
        cancel=cancel,
        deadline_monotonic=deadline_monotonic,
        on_progress=on_progress,
        ffmpeg=ffmpeg,
        abort=abort,
    )


def repair_words_locally(
    audio_path: Path,
    holes: list[tuple[float, float]],
    words: list[dict[str, Any]],
    *,
    duration_sec: float,
    language: str,
    settings: LocalRepairSettings,
    cancel: Any | None = None,
    deadline_monotonic: float | None = None,
    on_progress: Callable[[AsrProgress], None] | None = None,
    ffmpeg: str = "ffmpeg",
    transcribe: Callable[..., list[dict[str, Any]]] | None = None,
) -> LocalRepairOutcome:
    """Word-level ремонт дыр готового индекса (слова `{word, start, end[, ...]}`
    в абсолютном времени вместо реплик). Исходный список не меняется; итог в
    `outcome.words`. Логика цикла, VAD, дедупа и вклейки общая с
    `repair_holes_locally`; `transcribe` позволяет направить распознавание в
    процесс-воркер (отменяемый), а не в поток."""
    outcome = LocalRepairOutcome(words=[dict(w) for w in words])

    def apply(hole: tuple[float, float], raw_words: list[dict[str, Any]], clip_start: float):
        spliced = _splice_words(outcome.words or [], hole, raw_words, clip_start)
        if spliced is None:
            return None
        outcome.words, added = spliced
        return added

    return _repair_loop(
        audio_path,
        holes,
        outcome,
        apply,
        duration_sec=duration_sec,
        language=language,
        settings=settings,
        cancel=cancel,
        deadline_monotonic=deadline_monotonic,
        on_progress=on_progress,
        ffmpeg=ffmpeg,
        transcribe=transcribe,
    )
