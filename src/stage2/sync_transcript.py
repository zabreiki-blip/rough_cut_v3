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
    metadata = {**metadata, "timestamp_precision": "interpolated" if interpolated else "word"}
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


def save_word_index(path: Path | str, index: WordIndex) -> None:
    """Atomically save an already-generated index beside its final path."""
    atomic_write_json(path, index.to_dict())
