"""Локальный индекс слов (`<source_id>.local_words.json`) как резерв и проверка СНХ-матчей.

Файл пишет `index_retiming`: слова локального Whisper по всему файлу в той же
временной базе, что и основной индекс. Здесь он читается как обычный `WordIndex`
(`timestamp_precision="word"`, `index_kind="local_whisper"`) для `SyncMatcher`:
резервный поиск фраз, которых нет в основном индексе, и сверка границ найденного.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from .index_retiming import LOCAL_WORDS_VERSION, _normalize_words, local_words_path
from .sync_models import WordIndex, WordTimestamp

logger = logging.getLogger(__name__)

LOCAL_INDEX_KIND = "local_whisper"
_TRANSCRIPTION_CACHE_KEY = "transcription_cache"


def _audio_signatures_conflict(primary_index: Any, local_audio: Any) -> bool:
    """Основной индекс и local_words описывают разные аудиофайлы.

    Сравнивается только то, что есть у обоих: `size` и `sample_sha256`
    (`mtime_ns` не берётся: копирование файла его меняет). Если у любой стороны
    сигнатуры нет, файл не отвергается: это не доказательство расхождения.
    """
    metadata = getattr(primary_index, "metadata", None)
    cache = metadata.get(_TRANSCRIPTION_CACHE_KEY) if isinstance(metadata, dict) else None
    primary_audio = cache.get("audio") if isinstance(cache, dict) else None
    if not isinstance(primary_audio, dict) or not isinstance(local_audio, dict):
        return False
    for key in ("size", "sample_sha256"):
        left, right = primary_audio.get(key), local_audio.get(key)
        if left is not None and right is not None and left != right:
            return True
    return False


def load_local_word_index(
    index_path: Path, source_id: str, *, primary_index: Any = None
) -> WordIndex | None:
    """`WordIndex` из `local_words.json` рядом с основным индексом или `None`.

    Кэш-проверку модели/языка/аудио `_load_local_words` здесь не повторяем: аудио
    недоступно, а модель не важна для поиска. Версия файла обязана совпадать, слова —
    быть непустыми. Если передан `primary_index` с сигнатурой аудио
    (`metadata.transcription_cache.audio`) и в файле есть `audio`, они не должны
    противоречить друг другу (чужая шкала времени хуже отсутствия индекса).
    """
    path = local_words_path(Path(index_path), source_id)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("version") != LOCAL_WORDS_VERSION:
        return None
    if _audio_signatures_conflict(primary_index, payload.get("audio")):
        logger.warning("local_words %s belongs to another audio; ignored", path)
        return None
    raw_words = _normalize_words(payload.get("words"))
    if not raw_words:
        return None
    try:
        words = [WordTimestamp(item["word"], item["start"], item["end"]) for item in raw_words]
    except ValueError:
        return None
    return WordIndex(
        source_id=source_id,
        words=words,
        language=str(payload.get("language") or "ru"),
        model=str(payload.get("model") or ""),
        generated_at=str(payload.get("generated_at") or ""),
        metadata={
            "timestamp_precision": "word",
            "index_kind": LOCAL_INDEX_KIND,
        },
    )
