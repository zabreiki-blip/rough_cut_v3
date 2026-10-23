"""Provider-neutral ingestion for word-timestamp transcription output."""

from __future__ import annotations

import json
from math import isfinite
import re
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from atomic_io import atomic_write_json

from .sync_models import SyncSource, WordIndex, WordTimestamp

_TOKEN_RE = re.compile(r"[0-9A-Za-zА-Яа-яЁё]+(?:['’][A-Za-zА-Яа-яЁё]+)?")


@runtime_checkable
class WordIndexGenerator(Protocol):
    """Interface implemented by a local/remote Whisper adapter in a later layer.

    Core Stage 2 alignment depends only on this protocol and never uploads media
    or invokes a particular transcription engine itself.
    """

    def generate(self, source: SyncSource) -> WordIndex:
        """Generate word timestamps for ``source.guide_audio``."""
        ...


def _float_or_none(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if isfinite(result) else None


def _timestamps(item: dict[str, Any]) -> tuple[float | None, float | None]:
    start = _float_or_none(item.get("start"))
    end = _float_or_none(item.get("end"))
    timestamp = item.get("timestamp", item.get("timestamps"))
    if isinstance(timestamp, (list, tuple)) and timestamp:
        start = start if start is not None else _float_or_none(timestamp[0])
        if len(timestamp) > 1:
            end = end if end is not None else _float_or_none(timestamp[1])
    return start, end


def _explicit_words(values: Any) -> list[WordTimestamp]:
    if not isinstance(values, list):
        return []
    result: list[WordTimestamp] = []
    for item in values:
        if not isinstance(item, dict):
            continue
        text = str(item.get("word") or item.get("token") or item.get("text") or "").strip()
        start, end = _timestamps(item)
        if not text or start is None or start < 0:
            continue
        if end is None:
            end = start
        if end < start:
            continue
        confidence = item.get("confidence", item.get("probability", item.get("score")))
        confidence_value = _float_or_none(confidence)
        if confidence_value is not None:
            confidence_value = max(0.0, min(1.0, confidence_value))
        # Some engines put more than one lexical token in a timed "word".  Keep
        # time useful by interpolating the supplied interval over those tokens.
        tokens = _TOKEN_RE.findall(text)
        if not tokens:
            continue
        width = (end - start) / len(tokens)
        for index, token in enumerate(tokens):
            token_start = start + width * index
            token_end = end if index == len(tokens) - 1 else start + width * (index + 1)
            result.append(WordTimestamp(token, token_start, token_end, confidence_value))
    return result


def _segment_words(segments: Any) -> tuple[list[WordTimestamp], bool]:
    if not isinstance(segments, list):
        return [], False
    result: list[WordTimestamp] = []
    interpolated = False
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        words = _explicit_words(segment.get("words"))
        if words:
            result.extend(words)
            continue
        # Hugging Face pipelines often return chunks with a text span and one
        # timestamp pair.  Interpolation is marked in metadata so the reviewer
        # can distinguish it from genuine Whisper word timestamps.
        words = _explicit_words([segment])
        if words:
            result.extend(words)
            interpolated = interpolated or len(words) > 1
    return result, interpolated


def segment_words_interpolated(segments: Any) -> tuple[list[WordTimestamp], bool]:
    """Public wrapper around :func:`_segment_words` for callers outside this
    module (see ``stage2.whisper_runner._transcribe_cloud``) that need to turn
    reply-level ``{"text", "start", "end"}`` segments — with no per-word
    timestamps of their own, such as the cloud ASR backend's output — into
    per-word timestamps by linear interpolation over each segment's span.

    Returns the same ``(words, interpolated)`` pair as :func:`_segment_words`;
    ``interpolated`` is ``True`` whenever a segment contributed more than one
    token, i.e. whenever the returned timestamps are approximations rather
    than genuine per-word timing from the source.
    """
    return _segment_words(segments)


def ingest_word_index(
    payload: Any,
    source_id: str | None = None,
    *,
    model: str = "",
    language: str = "ru",
) -> WordIndex:
    """Convert common Whisper/WhisperX/HF JSON shapes into :class:`WordIndex`.

    Supported inputs include a bare word list, ``{"words": [...]}``,
    ``{"segments": [{"words": ...}]}``, and HF-style ``{"chunks": [...]}``.
    """

    inferred_source = ""
    inferred_model = model
    inferred_language = language
    generated_at = ""
    metadata: dict[str, Any] = {}
    interpolated = False

    if isinstance(payload, list):
        words = _explicit_words(payload)
    elif isinstance(payload, dict):
        inferred_source = str(payload.get("source_id") or "")
        inferred_model = str(payload.get("model") or inferred_model)
        inferred_language = str(payload.get("language") or inferred_language)
        generated_at = str(payload.get("generated_at") or payload.get("created_at") or "")
        metadata = dict(payload.get("metadata") or {})
        backend_metadata = payload.get("backend_metadata")
        if isinstance(backend_metadata, dict):
            metadata["backend_metadata"] = backend_metadata
            # Words were spread linearly over segment spans (cloud ASR): the
            # index is interpolated even though it carries explicit words.
            if str(backend_metadata.get("timestamp_precision") or "").lower() == "interpolated":
                interpolated = True
        # A persisted index keeps the marker in ``metadata`` (``backend_metadata`` is nested
        # there, not a payload key): honour it, or a reload of a cloud index whose retime
        # was rejected would silently look word-precise.
        persisted_backend = metadata.get("backend_metadata")
        if str(metadata.get("timestamp_precision") or "").lower() == "interpolated" or (
            isinstance(persisted_backend, dict)
            and str(persisted_backend.get("timestamp_precision") or "").lower() == "interpolated"
        ):
            interpolated = True
        for key in ("backend", "audio_path", "schema_version"):
            if key in payload:
                metadata[key] = payload[key]
        words = _explicit_words(payload.get("words"))
        if not words:
            for key in ("segments", "chunks", "results"):
                words, was_interpolated = _segment_words(payload.get(key))
                interpolated = interpolated or was_interpolated
                if words:
                    break
    else:
        raise TypeError("transcript payload must be a JSON object or list")

    actual_source = str(source_id or inferred_source).strip()
    if not actual_source:
        raise ValueError("source_id is required for a word index")
    if not words:
        raise ValueError("transcript contains no valid timed words")
    precision = "interpolated" if interpolated else "word"
    if metadata.get("timestamp_precision") == "retimed":
        # Cloud word times recomputed from a local Whisper pass (index_retiming).
        precision = "retimed"
    metadata = {**metadata, "timestamp_precision": precision}
    return WordIndex(
        source_id=actual_source,
        words=words,
        language=inferred_language,
        model=inferred_model,
        generated_at=generated_at,
        metadata=metadata,
    )


def load_word_index(
    path: Path | str,
    source_id: str | None = None,
    *,
    model: str = "",
    language: str = "ru",
) -> WordIndex:
    file_path = Path(path)
    try:
        payload = json.loads(file_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid transcript JSON in {file_path}: {exc}") from exc
    return ingest_word_index(payload, source_id, model=model, language=language)


_AUDIO_DURATION_TOLERANCE = 0.02


def _transcribed_audio_duration(index: WordIndex) -> float:
    """Duration of the audio the index was transcribed from (0 when unknown)."""
    backend = index.metadata.get("backend_metadata")
    if not isinstance(backend, dict):
        return 0.0
    try:
        value = float(backend.get("duration_sec") or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return value if value > 0 else 0.0


def reference_audio_mismatch(index: WordIndex, duration_sec: float) -> dict[str, float] | None:
    """Detect an index made from a reference audio that is not the media.

    Words that end where the transcribed audio ends (±2%) while the media is
    notably shorter are not a stretched time axis: the guide audio belongs to
    another (longer) version of the recording. Linear scaling would put every
    word in the wrong place, so such an index (also one that was already
    "repaired" by ``timestamp_scale_repair`` from such audio) must be
    re-transcribed from audio that matches the media.
    """
    duration = float(duration_sec or 0.0)
    audio_duration = _transcribed_audio_duration(index)
    if duration <= 0 or audio_duration <= duration * (1.0 + _AUDIO_DURATION_TOLERANCE):
        return None
    repair = index.metadata.get("timestamp_scale_repair")
    observed_end = index.duration_sec
    if isinstance(repair, dict):
        try:
            observed_end = float(repair.get("observed_end_sec") or 0.0)
        except (TypeError, ValueError):
            observed_end = 0.0
    if observed_end <= duration * (1.0 + _AUDIO_DURATION_TOLERANCE):
        return None
    if abs(audio_duration - observed_end) > observed_end * _AUDIO_DURATION_TOLERANCE:
        return None
    return {
        "audio_duration_sec": audio_duration,
        "media_duration_sec": duration,
        "observed_end_sec": observed_end,
    }


def rescale_stretched_word_index(index: WordIndex, duration_sec: float) -> float | None:
    """Repair cached interpolated timestamps that extend past real media.

    Older cloud responses occasionally described a uniformly stretched time
    axis. Reusing such a cache makes valid quotes near the end of an interview
    impossible to match because their ranges lie beyond ffprobe duration.
    Scaling is valid only for a stretched axis: when the transcribed audio
    itself is that long (``reference_audio_mismatch``) nothing is scaled.
    """
    duration = float(duration_sec or 0.0)
    observed_end = index.duration_sec
    if duration <= 0 or observed_end <= duration * 1.02:
        return None
    if reference_audio_mismatch(index, duration) is not None:
        return None
    scale = duration / observed_end
    index.words = [
        WordTimestamp(
            word=item.word,
            start=max(0.0, item.start * scale),
            end=max(0.0, item.end * scale),
            confidence=item.confidence,
        )
        for item in index.words
    ]
    index.metadata["timestamp_scale_repair"] = {
        "observed_end_sec": observed_end,
        "duration_sec": duration,
        "scale": scale,
    }
    return scale


def save_word_index(path: Path | str, index: WordIndex) -> None:
    """Atomically save an already-generated index beside its final path."""
    atomic_write_json(path, index.to_dict())
