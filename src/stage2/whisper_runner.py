from __future__ import annotations

from datetime import UTC, datetime
import importlib.util
from math import isfinite
import os
from pathlib import Path
import platform
import threading
from typing import Any, Callable

from env_utils import env_int_strict

from .cloud_asr import CloudAudioAsrError, transcribe_cloud
from .sync_transcript import segment_words_interpolated


class WhisperUnavailableError(RuntimeError):
    """Raised when none of the supported local Whisper backends is installed."""


class WhisperTranscriptionError(RuntimeError):
    """Raised when a backend did not return usable word timestamps."""


# "cloud-audio" is a paid, network-backed backend (see ``cloud_asr.py``) and is
# intentionally NOT part of the local-only auto-selection order below — it is
# only ever chosen by an explicit ``backend="cloud-audio"``/env request (see
# ``resolve_backend``).
SUPPORTED_BACKENDS = ("mlx-whisper", "faster-whisper", "openai-whisper", "cloud-audio")
_LOCAL_BACKENDS = ("mlx-whisper", "faster-whisper", "openai-whisper")
_CLOUD_BACKENDS = ("cloud-audio",)
_MODEL_CACHE: dict[tuple[str, ...], tuple[Any, threading.Lock]] = {}
_MODEL_CACHE_LOCK = threading.Lock()
ProgressCallback = Callable[[float, float, int], None]


def _env_int(name: str, default: int, *, minimum: int) -> int:
    return env_int_strict(name, default, minimum=minimum)


def _emit_progress(
    callback: ProgressCallback | None,
    processed_sec: float,
    duration_sec: float,
    word_count: int,
) -> None:
    if callback is None:
        return
    try:
        callback(
            max(0.0, float(processed_sec)),
            max(0.0, float(duration_sec)),
            max(0, int(word_count)),
        )
    except Exception:
        # A UI/status update must never abort an expensive transcription.
        pass


def _cached_model(key: tuple[str, ...], loader) -> tuple[Any, threading.Lock]:
    """Load each heavyweight model once per API process."""
    with _MODEL_CACHE_LOCK:
        cached = _MODEL_CACHE.get(key)
        if cached is None:
            cached = (loader(), threading.Lock())
            _MODEL_CACHE[key] = cached
        return cached


def _module_available(module: str) -> bool:
    return importlib.util.find_spec(module) is not None


def available_backends() -> list[str]:
    available: list[str] = []
    if _module_available("mlx_whisper"):
        available.append("mlx-whisper")
    if _module_available("faster_whisper"):
        available.append("faster-whisper")
    if _module_available("whisper"):
        available.append("openai-whisper")
    return available


def resolve_backend(requested: str | None = None) -> str:
    raw_backend = requested if requested else os.getenv("SYNC_WHISPER_BACKEND", "auto")
    value = raw_backend.strip().lower()
    aliases = {
        "mlx": "mlx-whisper",
        "faster": "faster-whisper",
        "openai": "openai-whisper",
        "whisper": "openai-whisper",
        "cloud": "cloud-audio",
        "openrouter": "cloud-audio",
    }
    value = aliases.get(value, value)
    installed = available_backends()

    if value != "auto":
        if value not in SUPPORTED_BACKENDS:
            raise ValueError(
                f"unsupported Whisper backend '{value}'; "
                f"expected auto or one of {', '.join(SUPPORTED_BACKENDS)}"
            )
        if value in _CLOUD_BACKENDS:
            # Cloud backends are not a locally installed package, so there is
            # nothing to check here. They are paid and require an API key;
            # that is validated lazily, only once a transcription actually
            # starts (see ``_transcribe_cloud``), so resolving the backend
            # name never touches the network or reveals key presence.
            return value
        if value not in installed:
            raise WhisperUnavailableError(
                f"Whisper backend '{value}' is not installed"
            )
        return value

    # Auto-selection only ever considers local backends: cloud-audio is paid
    # and must be opted into explicitly (see the module-level comment on
    # SUPPORTED_BACKENDS above).
    preferred = list(_LOCAL_BACKENDS)
    if platform.system() != "Darwin" or platform.machine() not in {"arm64", "aarch64"}:
        preferred = ["faster-whisper", "openai-whisper", "mlx-whisper"]
    for backend in preferred:
        if backend in installed:
            return backend
    raise WhisperUnavailableError(
        "No local Whisper backend is installed. Install mlx-whisper on Apple "
        "Silicon, faster-whisper, or openai-whisper."
    )


def _default_model(backend: str) -> str:
    configured = os.getenv("SYNC_WHISPER_MODEL", "").strip()
    if backend in _CLOUD_BACKENDS:
        # SYNC_WHISPER_MODEL is a local Whisper model name/size (e.g.
        # "medium") and is not a meaningful default for a cloud chat model
        # id, unless it already looks like one ("provider/model").
        if configured and "/" in configured:
            return configured
        from .cloud_asr import DEFAULT_MODEL as _CLOUD_DEFAULT_MODEL

        return _CLOUD_DEFAULT_MODEL
    if configured:
        if backend == "mlx-whisper" and "/" not in configured:
            return f"mlx-community/whisper-{configured}-mlx"
        return configured
    if backend == "mlx-whisper":
        return "mlx-community/whisper-medium-mlx"
    return "medium"


def _clean_words(words: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cleaned: list[dict[str, Any]] = []
    for item in words:
        text = str(item.get("word") or item.get("text") or "").strip()
        start_raw = item.get("start")
        end_raw = item.get("end")
        if start_raw is None or end_raw is None:
            continue
        try:
            start = float(start_raw)
            end = float(end_raw)
        except (TypeError, ValueError):
            continue
        if not text or not isfinite(start) or not isfinite(end) or start < 0 or end < start:
            continue
        cleaned.append({
            "word": text,
            "start": round(start, 3),
            "end": round(end, 3),
        })
    cleaned.sort(key=lambda item: (item["start"], item["end"]))
    return cleaned


def _words_from_mapping(result: dict[str, Any]) -> list[dict[str, Any]]:
    direct = result.get("words")
    if isinstance(direct, list):
        words = _clean_words([item for item in direct if isinstance(item, dict)])
        if words:
            return words

    flattened: list[dict[str, Any]] = []
    for segment in result.get("segments") or []:
        if not isinstance(segment, dict):
            continue
        for word in segment.get("words") or []:
            if isinstance(word, dict):
                flattened.append(word)
    return _clean_words(flattened)


def _transcribe_mlx(
    audio_path: Path, model: str, language: str
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    import mlx_whisper  # type: ignore[import-not-found]

    result = mlx_whisper.transcribe(
        str(audio_path),
        path_or_hf_repo=model,
        language=language,
        word_timestamps=True,
    )
    words = _words_from_mapping(result)
    return words, {"detected_language": result.get("language", language)}


def _transcribe_faster(
    audio_path: Path,
    model: str,
    language: str,
    progress_callback: ProgressCallback | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    from faster_whisper import (  # type: ignore[import-not-found]
        BatchedInferencePipeline,
        WhisperModel,
    )

    device = os.getenv("SYNC_WHISPER_DEVICE", "auto").strip() or "auto"
    default_compute_type = "int8" if device.lower() == "cpu" else "default"
    compute_type = (
        os.getenv("SYNC_WHISPER_COMPUTE_TYPE", default_compute_type).strip()
        or default_compute_type
    )
    default_beam_size = 1 if device.lower() == "cpu" else 5
    beam_size = _env_int(
        "SYNC_WHISPER_BEAM_SIZE",
        default_beam_size,
        minimum=1,
    )
    best_of = _env_int("SYNC_WHISPER_BEST_OF", 5, minimum=1)
    batch_size = _env_int("SYNC_WHISPER_BATCH_SIZE", 0, minimum=0)
    cpu_threads = _env_int("SYNC_WHISPER_CPU_THREADS", 0, minimum=0)
    model_kwargs: dict[str, Any] = {
        "device": device,
        "compute_type": compute_type,
    }
    if cpu_threads > 0:
        model_kwargs["cpu_threads"] = cpu_threads
    whisper_model, model_lock = _cached_model(
        ("faster-whisper", model, device, compute_type, str(cpu_threads)),
        lambda: WhisperModel(model, **model_kwargs),
    )
    # Backend model instances are not guaranteed to be thread-safe.
    raw_words: list[dict[str, Any]] = []
    with model_lock:
        transcriber = (
            BatchedInferencePipeline(whisper_model)
            if batch_size > 0
            else whisper_model
        )
        transcribe_kwargs: dict[str, Any] = {
            "language": language,
            "word_timestamps": True,
            "vad_filter": True,
            "beam_size": beam_size,
            "best_of": best_of,
        }
        if batch_size > 0:
            transcribe_kwargs["batch_size"] = batch_size
        segments, info = transcriber.transcribe(
            str(audio_path),
            **transcribe_kwargs,
        )
        duration_sec = float(getattr(info, "duration", 0.0) or 0.0)
        _emit_progress(progress_callback, 0.0, duration_sec, 0)
        for segment in segments:
            for word in segment.words or []:
                raw_words.append({
                    "word": word.word,
                    "start": word.start,
                    "end": word.end,
                })
            _emit_progress(
                progress_callback,
                float(getattr(segment, "end", 0.0) or 0.0),
                duration_sec,
                len(raw_words),
            )
    return _clean_words(raw_words), {
        "detected_language": getattr(info, "language", language),
        "language_probability": getattr(info, "language_probability", None),
        "duration_sec": duration_sec,
        "device": device,
        "compute_type": compute_type,
        "beam_size": beam_size,
        "best_of": best_of,
        "batch_size": batch_size,
        "cpu_threads": cpu_threads,
        "batched": batch_size > 0,
    }


def _transcribe_openai(
    audio_path: Path, model: str, language: str
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    import whisper  # type: ignore[import-not-found]

    whisper_model, model_lock = _cached_model(
        ("openai-whisper", model),
        lambda: whisper.load_model(model),
    )
    with model_lock:
        result = whisper_model.transcribe(
            str(audio_path),
            language=language,
            word_timestamps=True,
            verbose=False,
        )
    words = _words_from_mapping(result)
    return words, {"detected_language": result.get("language", language)}


def _transcribe_cloud(
    audio_path: Path,
    model: str,
    language: str,
    progress_callback: ProgressCallback | None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Transcribe via the cloud audio-ASR backend and interpolate word times.

    The cloud backend (see ``cloud_asr.transcribe_cloud``) only returns
    reply-level segments, not per-word timestamps. Word timestamps here are
    always a linear interpolation over each segment's span, reusing the same
    logic already used to ingest third-party segment-only transcripts (see
    ``sync_transcript.segment_words_interpolated``) rather than re-implementing
    interpolation a second time.
    """

    def _on_cloud_progress(progress: Any) -> None:
        # AsrProgress reports chunk-level position in the source audio, not
        # a running word count (unknown until the response is parsed); the
        # local-backend progress contract also does not require a live word
        # count, so 0 is reported until the final word list is ready below.
        duration = float(progress.duration_sec or 0.0)
        processed = float(
            progress.processed_sec if progress.processed_sec is not None else 0.0
        )
        _emit_progress(progress_callback, processed, duration, 0)

    try:
        result = transcribe_cloud(
            audio_path,
            model=model,
            language=language,
            progress_callback=_on_cloud_progress,
        )
    except CloudAudioAsrError as exc:
        # CloudAudioAsrError messages (see cloud_asr.py) are already
        # Russian-language, project-styled, and never include the API key
        # itself (only its presence/absence) — safe to surface as-is.
        raise WhisperTranscriptionError(
            f"Облачное распознавание речи не удалось: {exc}"
        ) from exc

    segments = result.get("segments") or []
    timestamps, _interpolated = segment_words_interpolated(segments)
    words = _clean_words([item.to_dict() for item in timestamps])

    explicit_end_count = sum(1 for seg in segments if seg.get("has_explicit_end"))
    backend_meta: dict[str, Any] = {
        "detected_language": result.get("language", language),
        "duration_sec": float(result.get("duration_sec") or 0.0),
        # Speaker labels from the cloud backend's single-pass diarization —
        # kept for later matching work, not consumed by this stage yet.
        "speakers": result.get("speakers") or [],
        "cost_usd": result.get("cost_usd"),
        "timestamp_precision": "interpolated",
        "segment_count": len(segments),
        "explicit_end_segment_count": explicit_end_count,
        # Lifted to the top-level result by transcribe_audio(); kept here
        # only transiently so this function's return shape matches every
        # other backend's (words, backend_metadata) contract.
        "segments": segments,
    }
    return words, backend_meta


def transcribe_audio(
    audio_path: str | Path,
    *,
    backend: str | None = None,
    model: str | None = None,
    language: str = "ru",
    progress_callback: ProgressCallback | None = None,
) -> dict[str, Any]:
    """Transcribe a sequence guide WAV into the canonical word-index format."""
    path = Path(audio_path).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"audio guide not found: {path}")

    selected = resolve_backend(backend)
    selected_model = (model or _default_model(selected)).strip()
    if not selected_model:
        raise ValueError("Whisper model must not be empty")

    if selected == "mlx-whisper":
        words, backend_meta = _transcribe_mlx(path, selected_model, language)
    elif selected == "faster-whisper":
        words, backend_meta = _transcribe_faster(
            path,
            selected_model,
            language,
            progress_callback,
        )
    elif selected == "cloud-audio":
        words, backend_meta = _transcribe_cloud(
            path,
            selected_model,
            language,
            progress_callback,
        )
    else:
        words, backend_meta = _transcribe_openai(path, selected_model, language)

    if not words:
        raise WhisperTranscriptionError(
            f"{selected} returned no usable word timestamps for {path.name}"
        )

    completed_duration = float(
        backend_meta.get("duration_sec") or words[-1]["end"]
    )
    _emit_progress(
        progress_callback,
        completed_duration,
        completed_duration,
        len(words),
    )

    # A backend may carry data that belongs at the top level of the result
    # rather than inside backend_metadata (currently only cloud-audio's
    # original reply segments, see _transcribe_cloud) — lift it out here so
    # every backend still shares one generic assembly path.
    top_level_extra: dict[str, Any] = {}
    segments_for_top_level = backend_meta.pop("segments", None)
    if segments_for_top_level is not None:
        top_level_extra["segments"] = segments_for_top_level

    return {
        "schema_version": 1,
        "audio_path": str(path),
        "backend": selected,
        "model": selected_model,
        "language": language,
        "created_at": datetime.now(UTC).isoformat(),
        "duration_sec": completed_duration,
        "word_count": len(words),
        "words": words,
        "backend_metadata": backend_meta,
        **top_level_extra,
    }
