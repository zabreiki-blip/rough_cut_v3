"""Minimal ASR result contract used by :mod:`src.stage2.cloud_asr`.

Trimmed down from the donor project's ``engine.asr.base`` module (see
TRANSCRIBE_v2/engine/asr/base.py): only the dataclasses that the cloud ASR
adapter actually produces are kept here. The donor's ``AsrAdapter`` ABC and
its domain-specific ``transcribe()`` contract (returning a
``TranscriptImportResult`` tied to the donor's own project model) were
intentionally NOT ported — that glue is out of scope for this module, see
the ``cloud_asr.py`` module docstring.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class AsrProgress:
    """Прогресс распознавания речи.

    `processed_sec`/`duration_sec`/`word_count` — необязательные поля для
    адаптеров, отдающих прогресс по позиции в аудио, а не по числу
    созданных сегментов.
    """

    done: int
    total: int
    message: str
    processed_sec: float | None = None
    duration_sec: float | None = None
    word_count: int | None = None


@dataclass(frozen=True)
class AsrWord:
    """Одно распознанное слово с временными метками (секунды от начала файла)."""

    word: str
    start: float
    end: float
    probability: float | None = None


@dataclass(frozen=True)
class AsrSegment:
    """Один сегмент распознавания (реплика) с входящими в него словами.

    `speaker` — необязательная метка говорящего (например, "ГОЛОС 1"),
    заполняется адаптерами, которые сами разделяют голоса за один проход
    (см. `cloud_asr.py`). Адаптеры без диаризации оставляют None.

    `has_explicit_end` — признак того, что `end` пришёл как собственная
    граница реплики от источника (например, новый формат ответа облачной
    модели `[начало — конец]`, см. `cloud_asr._resolve_end`), а не был
    вычислен как начало следующей реплики или иной fallback. По умолчанию
    `False` — безопасное значение для адаптеров, которые вообще не
    различают эти два случая.

    `timestamp_source` — происхождение реплики, если оно отличается от
    основного источника (например, "local-whisper" для вставок локального
    ремонта дыр, у которых `words` несут реальные пословные таймкоды).
    """

    text: str
    start: float
    end: float
    words: list[AsrWord]
    speaker: str | None = None
    has_explicit_end: bool = False
    timestamp_source: str | None = None


@dataclass
class AsrRunResult:
    """Результат низкоуровневого запуска ASR (слова + сегменты + fingerprint).

    `speakers` — сквозной список меток голосов, обнаруженных адаптером
    (пустой список, если адаптер не различает говорящих). `cost_usd` —
    фактическая стоимость запуска, если провайдер её отдаёт (иначе None).
    """

    words: list[AsrWord]
    segments: list[AsrSegment]
    language: str
    duration_sec: float
    backend: str
    model: str
    device: str
    compute_type: str
    params: dict
    word_count: int
    speakers: list[str] = field(default_factory=list)
    cost_usd: float | None = None
