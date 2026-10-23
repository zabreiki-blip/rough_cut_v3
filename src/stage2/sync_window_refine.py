"""Локальное уточнение границ кандидата по узкому окну аудио.

Облачная расшифровка с `timestamp_precision="interpolated"` раскладывает слова
равномерно внутри сегмента: цитата находится верно, но её границы могут уходить на
10-20 с. Если общий локальный перерасчёт (`index_retiming`) не принят (например,
`rejected: word count ratio`), такие кандидаты остаются неточными. Здесь для КАЖДОГО
кандидата вырезается окно `[approx_in - pad, approx_out + pad]`, локальный
faster-whisper распознаёт его с word_timestamps, цитата ищется по словам тем же
`SyncMatcher`, что и в основном поиске, и возвращаются точные границы речи в
абсолютных секундах источника.

- `refine_candidate_window` — один кандидат, чистая функция плюс кэш слов окна;
- `refine_matches` — проход по матчам фазы сопоставления (метаданные, статус,
  лимиты, прогресс).

Кэш (`<work>/transcripts/window-refine/<sha1>.json`, атомарно) хранит слова окна по
ключу (аудио: размер+mtime, окно, модель, язык): повторная синхронизация с теми же
кандидатами не распознаёт заново, а найденная/ненайденная цитата пересчитывается из
слов за миллисекунды (в том числе для другой цитаты в том же окне).

Переменные окружения: `SYNC_WINDOW_REFINE=0` выключает, `SYNC_WINDOW_REFINE_MAX`
(по умолчанию 300) — лимит числа НОВЫХ распознаваний окон за прогон (попадания в кэш
не считаются), `SYNC_WINDOW_REFINE_MODEL` (по умолчанию `small`).
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import subprocess
import tempfile
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from atomic_io import atomic_write_json

from .sync_matcher import MatcherConfig, SyncMatcher, unreliable_timing_source_ids
from .sync_models import AlignmentRequest, SyncSource, WordIndex, WordTimestamp

logger = logging.getLogger(__name__)

ENABLE_ENV = "SYNC_WINDOW_REFINE"
MAX_ENV = "SYNC_WINDOW_REFINE_MAX"
MODEL_ENV = "SYNC_WINDOW_REFINE_MODEL"
DEFAULT_MAX_WINDOWS = 300
DEFAULT_MODEL = "small"
DEFAULT_PAD_SEC = 45.0
DEFAULT_MIN_SCORE = MatcherConfig.auto_threshold
PRECISION = "window_refined"
CACHE_DIRNAME = "window-refine"
CACHE_VERSION = 1
TIMING_UNRELIABLE = "timing_unreliable"
_FALSE_VALUES = {"0", "false", "no", "off"}
_FFMPEG_TIMEOUT_SEC = 120.0
CHECKPOINT_EVERY = 10

# (audio_path, start_sec, duration_sec, out_wav) -> None
ExtractFn = Callable[[Path, float, float, Path], None]
# (clip_wav, model) -> [{"word", "start", "end"}] with CLIP-relative seconds
TranscribeFn = Callable[[Path, str], list[dict[str, Any]]]


class WindowRefineUnavailable(RuntimeError):
    """The window could not be cut or recognised (no ffmpeg, no engine, ...)."""


def _extract_window(audio_path: Path, start: float, duration: float, out_path: Path) -> None:
    command = [
        "ffmpeg", "-nostdin", "-y", "-ss", f"{start:.3f}", "-t", f"{duration:.3f}",
        "-i", str(audio_path), "-vn", "-ac", "1", "-ar", "16000",
        "-c:a", "pcm_s16le", "-f", "wav", str(out_path),
    ]  # fmt: skip
    try:
        proc = subprocess.run(
            command, capture_output=True, text=True, timeout=_FFMPEG_TIMEOUT_SEC, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise WindowRefineUnavailable(f"ffmpeg failed: {exc}") from exc
    if proc.returncode != 0 or not out_path.is_file() or out_path.stat().st_size <= 44:
        tail = (proc.stderr or "").strip()[-200:]
        raise WindowRefineUnavailable(f"ffmpeg produced no audio (exit {proc.returncode}): {tail}")


def _default_transcribe(language: str) -> TranscribeFn:
    def transcribe(clip: Path, model: str) -> list[dict[str, Any]]:
        # The same reusable, killable Whisper worker (and model cache) as the retime pass.
        from .whisper_process import transcribe_audio_in_process
        from .whisper_runner import is_no_words_error

        try:
            result = transcribe_audio_in_process(
                clip, backend="faster-whisper", model=model, language=language
            )
        except Exception as exc:
            if is_no_words_error(exc):
                return []
            raise
        return list(result.get("words") or [])

    return transcribe


def audio_stat_signature(audio_path: Path) -> dict[str, Any]:
    stat = Path(audio_path).stat()
    return {"name": Path(audio_path).name, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def _cache_file(
    cache_dir: Path, signature: Mapping[str, Any], start: float, end: float, model: str, lang: str
) -> Path:
    key = json.dumps(
        [CACHE_VERSION, dict(signature), round(start, 2), round(end, 2), model, lang],
        sort_keys=True,
    )
    return Path(cache_dir) / f"{hashlib.sha1(key.encode('utf-8')).hexdigest()}.json"


def _clean_words(raw: Iterable[Any], offset: float) -> list[dict[str, Any]]:
    words: list[dict[str, Any]] = []
    for item in raw or []:
        try:
            text = str(item["word"]).strip()
            start, end = float(item["start"]) + offset, float(item["end"]) + offset
        except (KeyError, TypeError, ValueError):
            continue
        if text and math.isfinite(start) and math.isfinite(end) and start >= 0 and end >= start:
            words.append({"word": text, "start": round(start, 3), "end": round(end, 3)})
    words.sort(key=lambda w: (w["start"], w["end"]))
    return words


def _read_cache(path: Path) -> list[dict[str, Any]] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("version") != CACHE_VERSION:
        return None
    words = payload.get("words")
    return _clean_words(words, 0.0) if isinstance(words, list) else None


def find_quote(
    words: list[dict[str, Any]], quote: str, *, min_score: float = DEFAULT_MIN_SCORE
) -> tuple[float, float, float] | None:
    """Speech bounds of ``quote`` among window ``words`` (same matcher as the main search)."""
    if not words or not quote.strip():
        return None
    try:
        index = WordIndex(
            source_id="window",
            words=[WordTimestamp(w["word"], w["start"], w["end"]) for w in words],
            metadata={"timestamp_precision": "word"},
        )
        matcher = SyncMatcher(
            [SyncSource(id="window", speaker="", premiere_sequence="window")],
            [index],
            MatcherConfig(),
        )
        found = matcher.match(AlignmentRequest("window", quote, source_ids=("window",)))
    except ValueError:
        return None
    if found.status == "not_found" or found.confidence < min_score:
        return None
    if found.metadata.get("ambiguous"):
        return None
    start, end = found.metadata.get("speech_in_sec"), found.metadata.get("speech_out_sec")
    if not isinstance(start, (int, float)) or not isinstance(end, (int, float)) or end < start:
        return None
    return float(start), float(end), float(found.confidence)


@dataclass
class RefineOutcome:
    # found | not_found | limit | unavailable
    status: str
    bounds: tuple[float, float, float] | None = None
    transcribed: bool = False
    cached: bool = False
    reason: str = ""


def refine_candidate_window_detail(
    audio_path: str | Path,
    quote: str,
    approx_in: float,
    approx_out: float,
    *,
    window_pad_sec: float = DEFAULT_PAD_SEC,
    transcribe_fn: TranscribeFn | None = None,
    extract_fn: ExtractFn | None = None,
    model: str = DEFAULT_MODEL,
    language: str = "ru",
    min_score: float = DEFAULT_MIN_SCORE,
    cache_dir: Path | None = None,
    audio_signature: Mapping[str, Any] | None = None,
    allow_transcribe: bool = True,
) -> RefineOutcome:
    audio = Path(audio_path)
    start = max(0.0, float(approx_in) - window_pad_sec)
    end = max(start, float(approx_out) + window_pad_sec)
    cache_file = None
    words: list[dict[str, Any]] | None = None
    try:
        signature = audio_signature or audio_stat_signature(audio)
    except OSError as exc:
        return RefineOutcome("unavailable", reason=f"audio: {exc}")
    if cache_dir is not None:
        cache_file = _cache_file(Path(cache_dir), signature, start, end, model, language)
        words = _read_cache(cache_file)
    transcribed = False
    if words is None:
        if not allow_transcribe:
            return RefineOutcome("limit", reason="window limit reached")
        extract = extract_fn or _extract_window
        transcribe = transcribe_fn or _default_transcribe(language)
        try:
            with tempfile.TemporaryDirectory(prefix="window-refine-") as tmp:
                clip = Path(tmp) / "window.wav"
                extract(audio, start, end - start, clip)
                words = _clean_words(transcribe(clip, model), start)
        except WindowRefineUnavailable as exc:
            return RefineOutcome("unavailable", reason=str(exc))
        except Exception as exc:
            # A failed window only leaves this candidate unrefined; cancellation is
            # re-checked by the caller right after.
            logger.warning("window refine of %s failed", audio.name, exc_info=True)
            return RefineOutcome("unavailable", reason=f"{type(exc).__name__}: {exc}")
        transcribed = True
        if cache_file is not None:
            try:
                atomic_write_json(
                    cache_file,
                    {
                        "version": CACHE_VERSION,
                        "model": model,
                        "language": language,
                        "audio": dict(signature),
                        "window": [round(start, 3), round(end, 3)],
                        "words": words,
                    },
                )
            except OSError:
                logger.warning("window refine cache %s not saved", cache_file, exc_info=True)
    bounds = find_quote(words, quote, min_score=min_score)
    if bounds is None:
        return RefineOutcome("not_found", transcribed=transcribed, cached=not transcribed)
    return RefineOutcome("found", bounds, transcribed=transcribed, cached=not transcribed)


def refine_candidate_window(
    audio_path: str | Path,
    quote: str,
    approx_in: float,
    approx_out: float,
    *,
    window_pad_sec: float = DEFAULT_PAD_SEC,
    transcribe_fn: TranscribeFn | None = None,
    model: str = DEFAULT_MODEL,
    **options: Any,
) -> tuple[float, float, float] | None:
    """``(speech_in, speech_out, score)`` in absolute seconds, or ``None`` (not found)."""
    return refine_candidate_window_detail(
        audio_path,
        quote,
        approx_in,
        approx_out,
        window_pad_sec=window_pad_sec,
        transcribe_fn=transcribe_fn,
        model=model,
        **options,
    ).bounds


# ---------------------------------------------------------------------------
# Pass over the matches of the matching phase
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WindowRefineSettings:
    enabled: bool = True
    max_windows: int = DEFAULT_MAX_WINDOWS
    model: str = DEFAULT_MODEL
    pad_sec: float = DEFAULT_PAD_SEC

    @classmethod
    def from_env(cls, getenv: Callable[[str, str], str]) -> "WindowRefineSettings":
        enabled = str(getenv(ENABLE_ENV, "1")).strip().lower() not in _FALSE_VALUES
        try:
            limit = max(0, int(str(getenv(MAX_ENV, "")).strip() or DEFAULT_MAX_WINDOWS))
        except ValueError:
            limit = DEFAULT_MAX_WINDOWS
        model = str(getenv(MODEL_ENV, "")).strip() or DEFAULT_MODEL
        return cls(enabled=enabled, max_windows=limit, model=model)


@dataclass
class WindowRefineReport:
    candidates: int = 0
    refined: list[str] = field(default_factory=list)
    downgraded: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    transcribed: int = 0
    cache_hits: int = 0
    skipped_limit: int = 0

    @property
    def changed_ids(self) -> list[str]:
        return [*self.refined, *self.downgraded, *self.unchanged]


def _speech_range(match: Any) -> tuple[float, float] | None:
    metadata = getattr(match, "metadata", None) or {}
    start, end = metadata.get("speech_in_sec"), metadata.get("speech_out_sec")
    if isinstance(start, bool) or isinstance(end, bool):
        return None
    if not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
        return None
    if not (math.isfinite(start) and math.isfinite(end)) or end < start:
        return None
    return float(start), float(end)


def unrefined_source_ids(indexes: Iterable[Any]) -> set[str]:
    """Sources whose word times are still interpolated (no accepted retime)."""
    return unreliable_timing_source_ids(
        index for index in indexes if isinstance(getattr(index, "metadata", None), dict)
    )


def select_candidates(
    matches: Iterable[Any],
    unrefined: set[str],
    *,
    is_manual: Callable[[Any], bool],
) -> list[Any]:
    """Auto/review matches on interpolated sources, auto ones first (limit hits the weak)."""
    chosen = [
        match
        for match in matches
        if getattr(match, "status", "") in ("auto_matched", "needs_review")
        and getattr(match, "source_id", "") in unrefined
        and not is_manual(match)
        and not (getattr(match, "metadata", None) or {}).get("window_refined")
        and _speech_range(match) is not None
        and str(getattr(match, "query_text", "") or "").strip()
    ]
    chosen.sort(key=lambda m: 0 if m.status == "auto_matched" else 1)
    return chosen


def _apply_success(
    match: Any, bounds: tuple[float, float, float], source: Any | None, auto_threshold: float
) -> None:
    metadata = match.metadata
    metadata.pop("window_refine", None)
    old_in, old_out = _speech_range(match) or (0.0, 0.0)
    new_in, new_out, score = bounds
    head = metadata.get("head_padding_sec", MatcherConfig.head_padding_sec)
    tail = metadata.get("tail_padding_sec", MatcherConfig.tail_padding_sec)
    start = max(0.0, new_in - float(head))
    end = new_out + float(tail)
    duration = float(getattr(source, "duration_sec", 0.0) or 0.0)
    if duration > 0:
        start, end = min(duration, start), min(duration, end)
    end = max(start, end)
    metadata["window_refined"] = {
        "from": [round(old_in, 4), round(old_out, 4)],
        "to": [round(new_in, 4), round(new_out, 4)],
        "score": round(score, 4),
    }
    metadata["speech_in_sec"] = round(new_in, 4)
    metadata["speech_out_sec"] = round(new_out, 4)
    metadata["timestamp_precision_before_refine"] = metadata.get("timestamp_precision", "")
    metadata["timestamp_precision"] = PRECISION
    metadata["timing_flags_before_refine"] = list(metadata.get("timing_flags") or [])
    metadata["timing_flags"] = []
    metadata["timing_reliable"] = not metadata.get("plausibility_flags")
    match.source_in_sec = round(start, 4)
    match.source_out_sec = round(end, 4)
    # Interpolated times were the only blocker: two independent transcripts (cloud text,
    # local word times) now agree on the quote, so the text-based verdict stands.
    if (
        match.status == "needs_review"
        and metadata.get("review_reason") == "timing_unrefined"
        and not metadata.get("plausibility_flags")
        and float(match.confidence or 0.0) >= auto_threshold
    ):
        match.status = "auto_matched"
        metadata.pop("review_reason", None)
    elif metadata.get("review_reason") == "timing_unrefined":
        # Still needs review (plausibility flags or low score): say why, not the stale reason.
        flags = metadata.get("plausibility_flags")
        metadata["review_reason"] = str(flags[0]) if flags else "low_confidence"


def _apply_failure(match: Any, outcome: RefineOutcome) -> bool:
    """Record the failed attempt; a match that was auto becomes ``needs_review``."""
    metadata = match.metadata
    metadata["window_refine"] = {"status": outcome.status, "reason": outcome.reason}
    if match.status != "auto_matched":
        return False
    match.status = "needs_review"
    metadata["review_reason"] = TIMING_UNRELIABLE
    metadata["timing_reliable"] = False
    return True


def refine_matches(
    matches: Iterable[Any],
    sources: Iterable[Any],
    indexes: Iterable[Any],
    *,
    settings: WindowRefineSettings,
    resolve_audio_path: Callable[[Any], Path | None],
    is_manual: Callable[[Any], bool],
    cache_dir: Path | None = None,
    transcribe_fn: TranscribeFn | None = None,
    extract_fn: ExtractFn | None = None,
    auto_threshold: float = DEFAULT_MIN_SCORE,
    ensure_not_cancelled: Callable[[], None] | None = None,
    on_progress: Callable[[int, int], None] | None = None,
    on_checkpoint: Callable[[list[str]], None] | None = None,
    checkpoint_every: int = CHECKPOINT_EVERY,
) -> WindowRefineReport:
    """Refine boundaries of interpolated-source candidates in place (see module docstring)."""
    report = WindowRefineReport()
    if not settings.enabled:
        return report
    candidates = select_candidates(matches, unrefined_source_ids(indexes), is_manual=is_manual)
    report.candidates = len(candidates)
    source_by_id = {source.id: source for source in sources}
    budget = settings.max_windows
    for number, match in enumerate(candidates, 1):
        if ensure_not_cancelled is not None:
            ensure_not_cancelled()
        in_sec, out_sec = _speech_range(match) or (0.0, 0.0)
        source = source_by_id.get(match.source_id)
        audio = resolve_audio_path(source) if source is not None else None
        if audio is None or not Path(audio).is_file():
            outcome = RefineOutcome("unavailable", reason="guide audio not found")
        else:
            outcome = refine_candidate_window_detail(
                audio,
                match.query_text,
                in_sec,
                out_sec,
                window_pad_sec=settings.pad_sec,
                transcribe_fn=transcribe_fn,
                extract_fn=extract_fn,
                model=settings.model,
                cache_dir=cache_dir,
                allow_transcribe=budget > 0,
            )
        if ensure_not_cancelled is not None and outcome.status == "unavailable":
            ensure_not_cancelled()  # a cancelled worker surfaces as a failed window
        if outcome.transcribed:
            budget -= 1
            report.transcribed += 1
        elif outcome.cached:
            report.cache_hits += 1
        if outcome.status == "limit":
            report.skipped_limit += 1
        if outcome.status == "found" and outcome.bounds is not None:
            _apply_success(match, outcome.bounds, source, auto_threshold)
            report.refined.append(match.segment_id)
        elif _apply_failure(match, outcome):
            report.downgraded.append(match.segment_id)
        else:
            report.unchanged.append(match.segment_id)
        if on_progress is not None:
            on_progress(number, len(candidates))
        if on_checkpoint is not None and number % max(1, checkpoint_every) == 0:
            on_checkpoint(report.changed_ids)
    return report
