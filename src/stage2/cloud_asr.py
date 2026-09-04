"""Облачный ASR-адаптер с аудио-входом: распознавание и разделение говорящих
за один запрос к модели через OpenAI-совместимый endpoint.

Портировано из TRANSCRIBE_v2/engine/asr/cloud_audio.py (донор, только
чтение). Боевая логика разбора/сшивки перенесена дословно; обрублены
зависимости от доменной модели проекта-донора — см. примечания ниже и в
конце файла (публичная функция `transcribe_cloud`).

Один запрос к модели с поддержкой аудио-входа заменяет и распознавание речи,
и диаризацию: ответ уже содержит реплики с таймкодами и метками голоса.
Файлы длиннее `chunk_minutes` режутся на перекрывающиеся части — каждая
распознаётся отдельным последовательным запросом, результаты склеиваются
(дубли на стыке отбрасываются, метки голосов сшиваются по зоне перекрытия).

Модель нумерует голоса заново в каждой части, поэтому сквозная нумерация —
слабое место: на длинных записях (несколько частей) один и тот же человек
может получить несколько разных номеров, если на стыке частей не нашлось
дословного совпадения текста (пауза, служебная реплика, переформулировка).
От этого защищаемся тремя дополняющими друг друга уровнями:

1. Перекрытие между частями увеличено (см. `overlap_sec` /
   `SYNC_CLOUD_ASR_OVERLAP_SEC`) — больше материала для сопоставления
   текста на стыке.
2. Модели передаётся «легенда» голосов, накопленная по предыдущим частям
   (см. `_build_prompt`, `parse_speaker_legend`): модель просят использовать
   те же номера для тех же людей и заводить новый номер только для
   действительно нового голоса.
3. После сборки всех частей отдельный проход (`_reconcile_speakers`)
    склеивает метки, которые почти наверняка принадлежат одному человеку —
    по совпадению описания из легенды (личное имя в описаниях — самый сильный
    признак, иначе доля общих значимых слов, см. `_descriptions_match`).
     Итоговые метки перенумеровываются по убыванию суммарной длительности
    речи.

Обрубленные по сравнению с донором зависимости (этап 1 из 3 — только
портирование модуля, без интеграции в whisper_runner/UI):
- `engine.asr.base` → см. `src/stage2/asr_contract.py` (тот же набор
  dataclass-ов, без абстрактного `AsrAdapter.transcribe()`).
- `engine.models` (TcPrecision/TranscriptImportResult/TranscriptSegment) —
  не портировались: домен-специфичный метод `transcribe()` (конвертация в
  формат донора) сюда не переносился по заданию, здесь есть только
  низкоуровневый `transcribe_words()` + обёртка `transcribe_cloud()`.
- `engine.config.get_settings()` → параметры адаптера принимаются
  аргументами конструктора; ключ API и adres провайдера читаются напрямую
  из окружения (см. `_resolve_api_key`), как и в `src/stage1/config.py` —
  в rough_cut нет отдельного pydantic Settings.
- `engine.media.probe.probe_media` (длительность файла) → вызов `ffprobe`
  через `subprocess.run` с таймаутом и списком аргументов (без shell=True),
  тем же стилем, что и `_apply_tempo` в `src/stage1/gemini_tts.py`.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import difflib
import logging
import math
import os
import random
import re
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

import httpx

from .asr_contract import AsrProgress, AsrRunResult, AsrSegment

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "google/gemini-3.7-flash"
DEFAULT_CHUNK_MINUTES = 20.0
# Перекрытие по умолчанию (см. также SYNC_CLOUD_ASR_OVERLAP_SEC ниже) —
# значение, использующееся, если явный overlap_sec не передан и переменная
# окружения не задана.
DEFAULT_OVERLAP_SEC = 60.0
DEFAULT_TIMEOUT_SEC = 900.0
DEFAULT_MAX_RETRIES = 3

# Таймаут ffprobe при определении длительности файла (см. _probe_duration) —
# сама операция дешёвая, большой таймаут не нужен, но должен быть конечным
# (см. общий стиль вызовов ffmpeg/ffprobe в проекте, ../stage1/gemini_tts.py).
_PROBE_TIMEOUT_SEC = 60.0

# Таймаут нарезки одного фрагмента через ffmpeg (см. _extract_chunk_mp3) —
# должен быть конечным по тем же соображениям, что и _PROBE_TIMEOUT_SEC:
# зависший (например, из-за повреждённого входного файла) процесс ffmpeg без
# таймаута повесил бы весь прогон навсегда, потому что
# `process.communicate()` ждёт его завершения без ограничения по времени.
_EXTRACT_CHUNK_TIMEOUT_SEC = 300.0

# Порог схожести нормализованного текста, при котором реплики на стыке
# частей считаются дублями одной и той же фразы (см. _texts_match).
_DUPLICATE_TEXT_RATIO = 0.72

# Порог схожести нормализованных описаний голоса (из легенды) как строк
# целиком — запасной вариант _descriptions_match для вырожденных описаний,
# из которых после удаления стоп-слов не осталось значимых слов.
_DESCRIPTION_MATCH_RATIO = 0.78

# Порог меры Жаккара (доля общих значимых слов) между описаниями голоса, при
# котором они считаются одним и тем же человеком, если общее личное имя не
# найдено (см. _descriptions_match, пункт 2 задачи по сшивке голосов).
_DESCRIPTION_JACCARD_THRESHOLD = 0.5

# Роли и служебные слова, которые не считаем личными именами при извлечении
# имён из описания голоса (см. _extract_names) — по условию задачи «Голос,
# Оператор, Режиссер, Ассистент, Редактор, Интервьюер, Краевед и подобные».
_NAME_ROLE_STOPWORDS = frozenset(
    {
        "голос",
        "оператор",
        "режиссер",
        "режиссёр",
        "ассистент",
        "редактор",
        "интервьюер",
        "краевед",
        "экскурсовод",
        "сотрудник",
        "музея",
        "музей",
        "мужской",
        "женский",
        "научный",
    }
)

# Служебные слова, которые не учитываются при сравнении описаний по значимым
# словам (мера Жаккара, см. _significant_words) — слишком общие, чтобы что-то
# говорить о сходстве голосов (в т.ч. «голос» — он есть почти в каждом
# описании и без исключения искусственно завышал бы схожесть). Обозначения
# пола («мужской»/«женский») тоже исключены: пол уже сравнивается отдельным,
# более сильным шагом раньше (см. `_descriptions_match`, проверка по
# `_extract_gender`) — если оставить их значимыми словами, два РАЗНЫХ голоса
# одного пола с типовыми описаниями («Мужской голос, уверенный тон» /
# «Мужской голос, доброжелательный тон») набирают Жаккара только за счёт
# совпадения пола и ошибочно склеиваются.
_DESCRIPTION_STOPWORDS = frozenset(
    {"голос", "голоса", "и", "а", "но", "или", "с", "за", "кадром", "мужской", "женский"}
)

# Суффиксы русских падежных окончаний (от длинных к коротким), которые
# отбрасываются при сравнении личных имён по основе (см. _name_stem) — чтобы
# «Елена» и «Елены»/«Елене» считались одним и тем же именем.
_NAME_CASE_SUFFIXES = tuple(
    sorted(
        {
            "ого",
            "его",
            "ому",
            "ему",
            "ыми",
            "ими",
            "ах",
            "ях",
            "ой",
            "ей",
            "ем",
            "им",
            "ом",
            "ам",
            "ям",
            "у",
            "ю",
            "е",
            "ы",
            "и",
            "а",
            "я",
        },
        key=len,
        reverse=True,
    )
)

_BASE_PROMPT_RU = (
    "Сделай дословную расшифровку речи из приложенного аудио на русском языке. "
    "Раздели реплики по говорящим. Формат КАЖДОЙ строки строго такой:\n"
    "[ММ:СС.д — ММ:СС.д] ГОЛОС N: текст реплики\n"
    "Здесь N — порядковый номер говорящего. Первый таймкод в квадратных "
    "скобках — момент начала реплики, второй (после тире) — момент, когда "
    "говорящий ЗАКОНЧИЛ произносить именно эту фразу. Конец реплики — это НЕ "
    "момент начала следующей реплики другого говорящего: если после фразы "
    "есть пауза или её перебивает другой говорящий, конец всё равно должен "
    "указывать на реальное окончание речи этого голоса, без захвата паузы и "
    "чужих слов. Оба таймкода — от начала ЭТОГО аудиофрагмента (не исходного "
    "файла), в формате минуты:секунды с точностью до десятых долей секунды "
    "(например 01:23.4). Не пропускай реплики, не сокращай и не пересказывай "
    "— нужен только дословный текст. Не добавляй ничего, кроме строк в "
    "указанном формате и финального блока с описанием голосов (см. ниже): "
    "ни заголовков, ни пояснений, ни markdown-разметки."
)

_LEGEND_REQUEST_RU = (
    "\n\nВ САМОМ КОНЦЕ ответа, после всех реплик, добавь блок с кратким "
    "описанием каждого голоса, прозвучавшего в ЭТОМ фрагменте. Формат строго "
    "такой:\n"
    "ГОЛОСА:\n"
    "ГОЛОС N — краткое описание (пол, роль, манера речи)\n"
    "по одной строке на голос."
)

_LEGEND_REUSE_RU_TEMPLATE = (
    "\n\nВ предыдущих частях этой же записи уже встречались такие голоса:\n"
    "{legend_lines}\n"
    "Если в этом фрагменте звучит тот же человек — используй ТОТ ЖЕ номер "
    "голоса, что и выше, а не начинай нумерацию заново. Новый номер (следующий "
    "по порядку, начиная с {next_number}) заводи только для действительно "
    "нового голоса, которого не было в списке выше."
)


def _build_prompt(legend: dict[str, str] | None, *, next_number: int) -> str:
    """Собирает промпт для одной части: базовые инструкции + запрос легенды
    голосов + (для частей после первой) уже накопленная легенда предыдущих
    частей с требованием переиспользовать те же номера (см. пункт 2 защиты
    от расхождения нумерации голосов в докстринге модуля).
    """
    prompt = _BASE_PROMPT_RU + _LEGEND_REQUEST_RU
    if legend:
        legend_lines = "\n".join(
            f"{label} — {description}" for label, description in legend.items()
        )
        prompt += _LEGEND_REUSE_RU_TEMPLATE.format(
            legend_lines=legend_lines, next_number=next_number
        )
    return prompt


class CloudAudioAsrError(RuntimeError):
    """Ошибка облачного аудио-ASR (сеть, ответ провайдера, ffmpeg/ffprobe).

    `cost_usd` — стоимость уже успешно распознанных частей записи на момент
    ошибки (см. `CloudAudioAsrAdapter.transcribe_words`): длинная запись
    режется на несколько последовательных платных запросов, и если один из
    поздних чанков падает после исчерпания ретраев, деньги за предыдущие
    чанки уже потрачены — эта сумма не должна теряться молча вместе с
    исключением. `None`, если ошибка произошла до первого успешного платного
    запроса или стоимость не была известна ни для одного чанка.
    """

    def __init__(self, message: str, *, cost_usd: float | None = None) -> None:
        super().__init__(message)
        self.cost_usd = cost_usd


class TranscriptParseError(ValueError):
    """Ответ модели не удалось разобрать ни в одну реплику."""


# ---------------------------------------------------------------------------
# Разбор ответа модели (пункт 1.3 плана)
# ---------------------------------------------------------------------------

# Таймкод в произвольном месте строки (не только в начале) — облачная модель
# иногда возвращает сплошной абзац с несколькими инлайновыми таймкодами
# вместо построчного формата (см. TranscriptParseError докстринг и баг с
# 11-минутной записью, целиком попавшей в один сегмент).
#
# Понимает ОБА формата ответа модели:
# - старый `[ММ:СС]` / `[ЧЧ:ММ:СС]` — только начало реплики, целые секунды
#   (группы a1..a4, конец реплики не указан — вторая часть диапазона в
#   регулярном выражении необязательна);
# - новый `[начало — конец]` (см. _BASE_PROMPT_RU), где обе границы могут
#   быть с долями секунды после точки (группы b1..b4). Разделитель между
#   границами — дефис, тире или en/em-дэш, с произвольными пробелами вокруг:
#   модель не всегда следует инструкции буквально.
# Оба формата могут встретиться в одном и том же ответе вперемешку (модель
# сбивается в середине) — parse_transcript обрабатывает каждую строку
# независимо, без общих требований к остальным строкам ответа.
_INLINE_TIME_RE = re.compile(
    r"\[\s*(?P<a1>\d{1,2}):(?P<a2>\d{2})(?::(?P<a3>\d{2}))?(?P<a4>\.\d+)?"
    r"(?:\s*[\-–—]\s*"
    r"(?P<b1>\d{1,2}):(?P<b2>\d{2})(?::(?P<b3>\d{2}))?(?P<b4>\.\d+)?)?"
    r"\s*\]"
)

# Насколько «свой» конец реплики (новый формат) может превысить начало
# следующей реплики и всё ещё считаться правдоподобным (санитарная проверка
# в _resolve_end, защита от бреда модели): модель может слегка промахнуться
# на стыке реплик — это не повод откатываться к грубой аппроксимации «конец
# = начало следующей реплики», но большой промах уже похож на брак.
_END_OVERSHOOT_TOLERANCE_SEC = 2.0
# Группа текста — `.*`, а не `.+`: разметка вида `ГОЛОС 4:` без текста после
# разделителя должна распознать метку голоса с ПУСТЫМ content (а не провалить
# матч и утащить сам разделитель/метку в текст безымянной реплики) — иначе
# ниже по конвейеру пустая строка превращалась в фиктивный сегмент с текстом
# "ГОЛОС 4:" вместо того, чтобы быть тихо отброшенной (см. проверку
# `if not content.strip()` в parse_transcript).
_SPEAKER_RE = re.compile(
    r"^\s*([^\n:\-\u2013\u2014]{1,40}?)\s*[:\-\u2013\u2014]\s*(.*)$", re.DOTALL
)
_FENCE_RE = re.compile(r"^```\w*$")

# Заголовок секции легенды голосов (см. _LEGEND_REQUEST_RU) и её строки вида
# «ГОЛОС N — описание».
_LEGEND_HEADER_RE = re.compile(r"^голоса\s*:?\s*$", re.IGNORECASE)
_LEGEND_LINE_RE = re.compile(r"^\s*голос\s*(\d+)\s*[:\-\u2013\u2014]\s*(.+)$", re.IGNORECASE)


def _parse_timecode(a: str, b: str, c: str | None, frac: str | None = None) -> float:
    """Переводит компоненты таймкода в секунды.

    `c` — секунды в формате ЧЧ:ММ:СС (`None` для ММ:СС — тогда секунды в `b`).
    `frac` — необязательные доли секунды после точки (новый формат, см.
    _BASE_PROMPT_RU), вместе с самой точкой (например `".4"`); `None` или
    пустая строка — целые секунды, как в старом формате.
    """
    fraction = float(frac) if frac else 0.0
    if c is None:
        minutes, seconds = int(a), int(b)
        return float(minutes * 60 + seconds) + fraction
    hours, minutes, seconds = int(a), int(b), int(c)
    return float(hours * 3600 + minutes * 60 + seconds) + fraction


def _resolve_end(
    start: float, own_end: float | None, next_start: float | None
) -> tuple[float, bool]:
    """Определяет конец реплики и признак того, что это НАСТОЯЩАЯ граница,
    указанная моделью (новый формат `[начало — конец]`), а не приближение по
    началу следующей реплики (старая логика).

    Санитарная проверка (защита от бреда модели, см. задачу о смягчении
    «пре-ролла»/«хвоста» в репликах): свой конец принимается, только если он
    строго позже начала и не позже начала следующей реплики более чем на
    `_END_OVERSHOOT_TOLERANCE_SEC`. Отрицательные и нарушающие эти условия
    значения молча отбрасываются — тогда, как и раньше, конец = начало
    следующей реплики (или сам конец = начало для последней реплики ответа —
    её донастроит вызывающий код, знающий длительность фрагмента).
    """
    if own_end is not None and own_end > start:
        if next_start is None or own_end <= next_start + _END_OVERSHOOT_TOLERANCE_SEC:
            return own_end, True
    fallback = next_start if next_start is not None else start
    return fallback, False


def parse_transcript(text: str, *, offset_sec: float = 0.0) -> list[AsrSegment]:
    """Разбирает ответ модели в список сегментов.

    Понимает строки `[ММ:СС] ГОЛОС N: текст` и `[ЧЧ:ММ:СС] ГОЛОС N: текст`
    (старый формат, только начало реплики), а также `[начало — конец] ГОЛОС
    N: текст` (новый формат, см. _BASE_PROMPT_RU) — с концом реплики и,
    возможно, долями секунды у обеих границ. Оба формата могут встретиться в
    одном ответе вперемешку. Терпимо относится к отклонениям формата: лишние
    пробелы, тире вместо двоеточия, разные виды тире между границами
    диапазона, отсутствие говорящего, markdown-обёртки (```), пустые строки и
    произвольные преамбулы вроде «Расшифровка:». Строки без таймкода
    присоединяются к предыдущему сегменту.

    `end` сегмента — свой конец из ответа модели, если он указан и прошёл
    санитарную проверку (см. `_resolve_end`); иначе, как и раньше, начало
    следующей реплики. У последнего сегмента без своего конца `end == start`
    (вызывающий код, знающий длительность аудиофрагмента, обязан его
    подправить).

    Поднимает TranscriptParseError, если не удалось разобрать НИ ОДНОЙ
    строки — чтобы потеря материала не осталась незамеченной.
    """
    segments, _ = _parse_transcript_with_own_end(text, offset_sec=offset_sec)
    return segments


def _parse_transcript_with_own_end(
    text: str, *, offset_sec: float = 0.0
) -> tuple[list[AsrSegment], list[bool]]:
    """Как `parse_transcript`, но дополнительно возвращает параллельный
    список флагов «конец этого сегмента — настоящая граница от модели, не
    приближение по началу следующей реплики» (см. `_resolve_end`).

    Используется только внутри `transcribe_words`: там эта граница ещё раз
    проверяется относительно итогового соседа — уже ПОСЛЕ сшивки частей и
    отбрасывания дублей на стыке (см. `_stitch_chunk`), где «следующая
    реплика» может отличаться от той, что была видна на момент разбора
    одной части. `parse_transcript` — тонкая обёртка над этой функцией для
    вызывающего кода, которому флаги не нужны.
    """
    entries: list[tuple[float, str | None, str, float | None]] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or _FENCE_RE.match(line):
            continue
        timecodes = list(_INLINE_TIME_RE.finditer(line))
        if timecodes:
            # Текст до первого таймкода в строке (обычно пусто — таймкод в
            # начале строки, как в стандартном построчном формате). Если
            # модель вернула сплошной абзац, здесь может быть «хвост»
            # предыдущей реплики, которую нужно доклеить, а не потерять.
            prefix = line[: timecodes[0].start()].strip()
            if prefix:
                if entries:
                    prev_start, prev_speaker, prev_text, prev_own_end = entries[-1]
                    entries[-1] = (
                        prev_start,
                        prev_speaker,
                        f"{prev_text} {prefix}".strip(),
                        prev_own_end,
                    )
                # иначе — преамбула до первой распознанной реплики (например,
                # «Расшифровка:») молча отбрасывается, как и раньше.
            for i, match in enumerate(timecodes):
                start = _parse_timecode(
                    match.group("a1"), match.group("a2"), match.group("a3"), match.group("a4")
                )
                start += offset_sec
                own_end: float | None = None
                if match.group("b1") is not None:
                    own_end = _parse_timecode(
                        match.group("b1"), match.group("b2"), match.group("b3"), match.group("b4")
                    )
                    own_end += offset_sec
                remainder_end = timecodes[i + 1].start() if i + 1 < len(timecodes) else len(line)
                remainder = line[match.end() : remainder_end].strip()
                speaker: str | None = None
                content = remainder
                sm = _SPEAKER_RE.match(remainder)
                if sm:
                    label = sm.group(1).strip()
                    if label:
                        speaker = label
                        content = sm.group(2).strip()
                entries.append((start, speaker, content, own_end))
        elif entries:
            # Строка-продолжение (перенос реплики) — присоединяем к предыдущей.
            prev_start, prev_speaker, prev_text, prev_own_end = entries[-1]
            entries[-1] = (prev_start, prev_speaker, f"{prev_text} {line}".strip(), prev_own_end)
        # иначе — преамбула до первой распознанной строки (например,
        # «Расшифровка:») молча отбрасывается.

    if not entries:
        raise TranscriptParseError(
            "Не удалось разобрать ни одной строки расшифровки в ответе облачной "
            f"модели (ожидался формат '[ММ:СС — ММ:СС] ГОЛОС N: текст'): {text[:200]!r}"
        )

    segments: list[AsrSegment] = []
    has_own_end: list[bool] = []
    for i, (start, speaker, content, own_end) in enumerate(entries):
        next_start = entries[i + 1][0] if i + 1 < len(entries) else None
        end, used_own_end = _resolve_end(start, own_end, next_start)
        if not content.strip():
            # Пустая размеченная строка (например, `[00:12] ГОЛОС 4:` без
            # текста) не должна порождать сегмент — иначе downstream она
            # превращается в отдельного ложного участника "unknown"/призрак
            # голоса без единой реплики (раздел 0 плана). Соседние реальные
            # строки-продолжения уже были доклеены к ней ВЫШЕ (см. цикл по
            # `raw_line`) и потому не теряются: сюда попадают только
            # действительно пустые записи.
            continue
        segments.append(AsrSegment(text=content, start=start, end=end, words=[], speaker=speaker))
        has_own_end.append(used_own_end)

    if not segments:
        raise TranscriptParseError(
            "Ни одна строка расшифровки не содержит текста в ответе облачной "
            f"модели (только пустые метки голосов): {text[:200]!r}"
        )
    return segments, has_own_end


def split_transcript_and_legend(text: str) -> tuple[str, str | None]:
    """Отделяет секцию «ГОЛОСА:» (легенда голосов, см. `_LEGEND_REQUEST_RU`)
    от основной расшифровки.

    Ищет последнюю строку-заголовок «ГОЛОСА:» (без учёта регистра, markdown-
    обёртки ``` игнорируются). Всё, что находится ПОСЛЕ неё, возвращается
    отдельной строкой-легендой; из текста расшифровки эта секция вырезается,
    чтобы `parse_transcript` не приняла её строки за продолжение последней
    реплики. Если заголовок не найден (модель легенду не вернула), легенда —
    `None`, а текст возвращается как есть.
    """
    lines = text.splitlines()
    header_idx: int | None = None
    for i, raw_line in enumerate(lines):
        line = raw_line.strip()
        if _LEGEND_HEADER_RE.match(line):
            header_idx = i
    if header_idx is None:
        return text, None
    transcript_text = "\n".join(lines[:header_idx])
    legend_text = "\n".join(lines[header_idx + 1 :])
    return transcript_text, legend_text


def parse_speaker_legend(text: str | None) -> dict[str, str]:
    """Разбирает секцию легенды голосов в словарь `{"ГОЛОС N": описание}`.

    Понимает строки вида `ГОЛОС N — описание` (тире, дефис или двоеточие в
    качестве разделителя, регистр не важен). Нераспознанные строки молча
    пропускаются. Пустой текст или отсутствие легенды (`None`) — не ошибка:
    возвращается пустой словарь, а сшивка голосов продолжает работать на
    остальных признаках (совпадение текста на стыке, доминирующий голос).
    """
    result: dict[str, str] = {}
    if not text:
        return result
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or _FENCE_RE.match(line):
            continue
        match = _LEGEND_LINE_RE.match(line)
        if not match:
            continue
        label = f"ГОЛОС {match.group(1)}"
        description = match.group(2).strip()
        if description:
            result[label] = description
    return result


def _normalize_text(value: str) -> str:
    value = value.lower()
    value = re.sub(r"[^\w\s]", "", value, flags=re.UNICODE)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def _texts_match(a: str, b: str) -> bool:
    na, nb = _normalize_text(a), _normalize_text(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    ratio = difflib.SequenceMatcher(None, na, nb).ratio()
    return ratio >= _DUPLICATE_TEXT_RATIO


_NAME_TOKEN_RE = re.compile(r"^[А-ЯЁ][а-яё\-]+$")


def _name_stem(word: str) -> str:
    """Грубая основа слова для сравнения личных имён без учёта падежа: у слова
    длиной от 4 символов отбрасывается известное падежное окончание, если
    после этого остаётся не короче 3 символов (см. _NAME_CASE_SUFFIXES).
    «ё» приводится к «е» — одно и то же имя может встретиться в ответах
    модели написанным по-разному («Пётр» / «Петр»)."""
    low = word.lower().replace("ё", "е")
    for suffix in _NAME_CASE_SUFFIXES:
        if low.endswith(suffix) and len(low) - len(suffix) >= 3:
            return low[: -len(suffix)]
    return low


def _extract_names(text: str) -> set[str]:
    """Извлекает предполагаемые личные имена/фамилии из описания голоса.

    Признаки имени: слово с заглавной буквы (кириллица), не входящее в
    список ролей/стоп-слов (`_NAME_ROLE_STOPWORDS`). Слова внутри скобок —
    самый надёжный источник (в легенде так обычно и указывают имя, например
    «(Иван Сивопляс)») и учитываются все, включая первое в скобках. Слова вне
    скобок учитываются, кроме первого слова предложения — оно почти всегда
    просто начинается с заглавной буквы по правилам орфографии, а не потому,
    что это имя. Возвращает множество основ слов (без падежных окончаний,
    см. `_name_stem`) — для устойчивого сравнения по падежам.
    """
    names: set[str] = set()

    def add_candidate(word: str) -> None:
        cleaned = word.strip(" ,;:—-()")
        if not cleaned or not _NAME_TOKEN_RE.match(cleaned):
            return
        if cleaned.lower() in _NAME_ROLE_STOPWORDS:
            return
        names.add(_name_stem(cleaned))

    for fragment in re.findall(r"\(([^)]*)\)", text):
        for word in fragment.split():
            add_candidate(word)

    outside = re.sub(r"\([^)]*\)", " ", text)
    for sentence in re.split(r"[.!?;\n]+", outside):
        for idx, word in enumerate(sentence.split()):
            if idx == 0:
                continue
            add_candidate(word)

    return names


def _extract_gender(text: str) -> str | None:
    """Пол, упомянутый в описании голоса («женский»/«мужской»), если указан
    однозначно (не оба слова сразу). Иначе `None` — сравнение по полу тогда
    просто пропускается (см. `_descriptions_match`)."""
    low = text.lower()
    has_female = re.search(r"женск", low) is not None
    has_male = re.search(r"мужск", low) is not None
    if has_female and not has_male:
        return "женский"
    if has_male and not has_female:
        return "мужской"
    return None


def _significant_words(text: str) -> set[str]:
    """Множество значимых слов описания (нормализованных, без стоп-слов из
    `_DESCRIPTION_STOPWORDS`) — для сравнения описаний по мере Жаккара."""
    normalized = _normalize_text(text)
    return {word for word in normalized.split() if word and word not in _DESCRIPTION_STOPWORDS}


def _description_score(description: str) -> tuple[int, int]:
    """Оценка «информативности» описания для выбора представителя при
    слиянии меток (пункт 4 задачи): описание с личным именем предпочтительнее,
    при прочих равных — более длинное (подробное)."""
    has_name = 1 if _extract_names(description) else 0
    return has_name, len(description)


def _descriptions_match(a: str, b: str) -> bool:
    """Совпадают ли два описания голоса из легенды (см. пункт 3 защиты от
    расхождения нумерации голосов).

    Порядок проверок (от самого сильного и надёжного признака к самому
    слабому):
    1. Явно разный пол в обоих описаниях — никогда не считаем одним и тем же
       человеком, даже если остальное похоже (защита от переусердствования).
    2. Совпадение личного имени (`_extract_names`) — самый сильный признак,
       его одного достаточно (пол уже проверен на шаге 1). Если в обоих
       описаниях есть имена, но они разные — почти наверняка разные люди,
       дальше не сравниваем.
    3. Иначе — мера Жаккара по значимым словам описания (доля общих слов от
       объединения, без стоп-слов); если значимых слов не осталось —
       запасной вариант: схожесть нормализованных строк целиком.
    """
    na, nb = _normalize_text(a), _normalize_text(b)
    if not na or not nb:
        return False
    if na == nb:
        return True

    gender_a, gender_b = _extract_gender(a), _extract_gender(b)
    if gender_a and gender_b and gender_a != gender_b:
        return False

    names_a, names_b = _extract_names(a), _extract_names(b)
    if names_a and names_b:
        return bool(names_a & names_b)

    words_a, words_b = _significant_words(a), _significant_words(b)
    if not words_a or not words_b:
        ratio = difflib.SequenceMatcher(None, na, nb).ratio()
        return ratio >= _DESCRIPTION_MATCH_RATIO

    union = words_a | words_b
    if not union:
        return False
    jaccard = len(words_a & words_b) / len(union)
    return jaccard >= _DESCRIPTION_JACCARD_THRESHOLD


def _union_find_merge(labels: list[str], pairs: list[tuple[str, str]]) -> dict[str, str]:
    """Простое объединение меток в группы по списку пар «эти метки — один
    и тот же человек» (union-find). Возвращает `{метка: представитель группы}`.
    """
    parent = {label: label for label in labels}

    def find(label: str) -> str:
        while parent[label] != label:
            parent[label] = parent[parent[label]]
            label = parent[label]
        return label

    for a, b in pairs:
        if a not in parent or b not in parent:
            continue
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    return {label: find(label) for label in labels}


def _reconcile_speakers(
    segments: list[AsrSegment],
    *,
    speaker_descriptions: dict[str, str],
) -> tuple[list[AsrSegment], list[str], dict[str, str]]:
    """Пост-обработка после сборки всех частей (пункт 3 защиты): склеивает
    метки голосов, которые почти наверняка принадлежат одному человеку, и
    перенумеровывает итоговые метки по убыванию суммарной длительности речи
    (ГОЛОС 1 — тот, кто говорит дольше всех).

    Совпадение текста на стыке частей уже учтено раньше, в `_stitch_chunk`
    (самый надёжный признак — там же меткам присваивается общий номер).
    Единственный дополнительный признак здесь — совпадение описания голоса
    из легенды (`speaker_descriptions`).

    Слияние возможно только по описаниям из
    легенды (если описания совпадают — см. ``_descriptions_match``) или по
    результату `_stitch_chunk`, которому метки уже присвоены ранее.
    """
    labels_seen: list[str] = []
    for seg in segments:
        if seg.speaker is not None and seg.speaker not in labels_seen:
            labels_seen.append(seg.speaker)
    if not labels_seen:
        return segments, [], {}

    pairs: list[tuple[str, str]] = []

    desc_items = list(speaker_descriptions.items())
    for i in range(len(desc_items)):
        for j in range(i + 1, len(desc_items)):
            label_a, desc_a = desc_items[i]
            label_b, desc_b = desc_items[j]
            if label_a != label_b and _descriptions_match(desc_a, desc_b):
                pairs.append((label_a, label_b))

    group_of = _union_find_merge(labels_seen, pairs)

    duration_by_group: dict[str, float] = {}
    for seg in segments:
        if seg.speaker is None:
            continue
        group = group_of[seg.speaker]
        duration_by_group[group] = duration_by_group.get(group, 0.0) + max(0.0, seg.end - seg.start)

    group_order: list[str] = []
    for label in labels_seen:
        group = group_of[label]
        if group not in group_order:
            group_order.append(group)
    ranked_groups = sorted(
        group_order, key=lambda group: duration_by_group.get(group, 0.0), reverse=True
    )
    final_label_of_group = {group: f"ГОЛОС {i + 1}" for i, group in enumerate(ranked_groups)}

    renamed_segments = [
        replace(seg, speaker=final_label_of_group[group_of[seg.speaker]])
        if seg.speaker is not None
        else seg
        for seg in segments
    ]

    final_speakers: list[str] = []
    for seg in renamed_segments:
        if seg.speaker is not None and seg.speaker not in final_speakers:
            final_speakers.append(seg.speaker)

    # Для слитой метки берём самое информативное описание из объединённых
    # (с именем и подлиннее — см. _description_score), а не просто первое
    # попавшееся (пункт 4 задачи по сшивке голосов).
    final_descriptions: dict[str, str] = {}
    for old_label, description in speaker_descriptions.items():
        new_label = final_label_of_group.get(group_of.get(old_label, old_label))
        if not new_label:
            continue
        current = final_descriptions.get(new_label)
        if current is None or _description_score(description) > _description_score(current):
            final_descriptions[new_label] = description

    return renamed_segments, final_speakers, final_descriptions


def _stitch_chunk(
    accumulated: list[AsrSegment],
    new_segments: list[AsrSegment],
    own_end_flags: list[bool],
    *,
    part_start: float,
    boundary: float | None,
    next_speaker_number: int,
) -> tuple[list[AsrSegment], list[bool], int, dict[str, str]]:
    """Склеивает сегменты новой части с уже накопленными.

    `boundary` — абсолютное время, до которого предыдущая часть уже покрыла
    материал (её номинальный конец); `None` для самой первой части (тогда
    склейка не нужна — только сквозная нумерация голосов). `own_end_flags` —
    параллельный `new_segments` список флагов «свой конец от модели» (см.
    `_parse_transcript_with_own_end`) — фильтруется синхронно с сегментами
    (дубли из зоны перекрытия отбрасываются вместе с флагом). Возвращает
    сегменты новой части, которые нужно добавить, синхронный список их
    флагов, обновлённый счётчик для следующих голосов и `label_map`
    (локальная метка этой части → итоговая сквозная метка) — им пользуется
    вызывающий код, чтобы перевести легенду голосов части в сквозные метки.
    """
    label_map: dict[str, str] = {}
    out: list[AsrSegment] = []
    out_own_end: list[bool] = []

    prev_overlap: list[AsrSegment] = []
    if boundary is not None:
        prev_overlap = [s for s in accumulated if s.start >= part_start - 0.01]
    used_prev: set[int] = set()

    for seg, own_end_flag in zip(new_segments, own_end_flags):
        is_duplicate = False
        if boundary is not None and seg.start < boundary:
            best_idx: int | None = None
            best_ratio = 0.0
            for idx, prev in enumerate(prev_overlap):
                if idx in used_prev:
                    continue
                if not _texts_match(seg.text, prev.text):
                    continue
                ratio = difflib.SequenceMatcher(
                    None, _normalize_text(seg.text), _normalize_text(prev.text)
                ).ratio()
                if ratio > best_ratio:
                    best_ratio = ratio
                    best_idx = idx
            if best_idx is not None:
                is_duplicate = True
                used_prev.add(best_idx)
                matched_speaker = prev_overlap[best_idx].speaker
                if (
                    seg.speaker is not None
                    and seg.speaker not in label_map
                    and matched_speaker is not None
                ):
                    label_map[seg.speaker] = matched_speaker

        if seg.speaker is not None:
            if seg.speaker not in label_map:
                label_map[seg.speaker] = f"ГОЛОС {next_speaker_number}"
                next_speaker_number += 1
            seg = replace(seg, speaker=label_map[seg.speaker])

        if is_duplicate:
            continue
        out.append(seg)
        out_own_end.append(own_end_flag)

    return out, out_own_end, next_speaker_number, label_map


def _resolve_api_key() -> str | None:
    """Читает ключ облачного ASR из окружения: сперва `SYNC_CLOUD_ASR_API_KEY`
    (выделенный ключ для этого адаптера), затем `OPENROUTER_API_KEY` как
    fallback (общий ключ OpenRouter, если отдельный не задан) — прямое
    чтение `os.getenv`, как и в `src/stage1/config.py` (в rough_cut нет
    отдельного pydantic Settings)."""
    value = os.getenv("SYNC_CLOUD_ASR_API_KEY", "").strip()
    if value:
        return value
    value = os.getenv("OPENROUTER_API_KEY", "").strip()
    return value or None


def _probe_duration(
    media_path: Path, *, ffprobe: str = "ffprobe", timeout_sec: float = _PROBE_TIMEOUT_SEC
) -> float:
    """Определяет длительность медиафайла в секундах через `ffprobe`
    (замена `engine.media.probe.probe_media` донора): список аргументов без
    `shell=True` и с конечным таймаутом, тот же стиль вызова subprocess, что
    и `_apply_tempo` в `src/stage1/gemini_tts.py`."""
    args = [
        ffprobe,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(media_path),
    ]
    try:
        result = subprocess.run(args, capture_output=True, timeout=timeout_sec, check=False)
    except FileNotFoundError as exc:
        raise CloudAudioAsrError(
            f"Не найден исполняемый файл {ffprobe!r}. Установите ffmpeg/ffprobe и добавьте в PATH."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise CloudAudioAsrError(
            f"ffprobe не ответил при определении длительности {media_path}"
        ) from exc

    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()[-500:]
        raise CloudAudioAsrError(
            f"ffprobe завершился с ошибкой (код {result.returncode}) при определении длительности {media_path}"
            + (f": {detail}" if detail else "")
        )
    try:
        return float(result.stdout.decode("utf-8", errors="replace").strip())
    except ValueError as exc:
        raise CloudAudioAsrError(
            f"Не удалось разобрать длительность {media_path} из вывода ffprobe"
        ) from exc


class CloudAudioAsrAdapter:
    """ASR-адаптер поверх облачной модели с аудио-входом.

    Автономная версия донорского `engine.asr.cloud_audio.CloudAudioAsrAdapter`:
    не реализует доменный контракт `AsrAdapter.transcribe()` (нет смысла без
    `engine.models`) — только низкоуровневый `transcribe_words()`. Обёртка
    `transcribe_cloud()` в конце модуля — публичный синхронный вход для
    остального кода rough_cut.
    """

    name = "cloud-audio"

    def __init__(
        self,
        *,
        model: str = DEFAULT_MODEL,
        language: str = "ru",
        base_url: str | None = None,
        api_key: str | None = None,
        chunk_minutes: float = DEFAULT_CHUNK_MINUTES,
        overlap_sec: float | None = None,
        timeout_sec: float = DEFAULT_TIMEOUT_SEC,
        reasoning_effort: str | None = "minimal",
        http_client: httpx.AsyncClient | None = None,
        ffmpeg: str = "ffmpeg",
        ffprobe: str = "ffprobe",
        max_retries: int | None = None,
    ) -> None:
        self.model = model
        self.language = language
        resolved_base_url = (
            base_url
            if base_url
            else os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
        )
        self._base_url = resolved_base_url.rstrip("/")
        self._api_key = api_key if api_key is not None else _resolve_api_key()
        self.chunk_minutes = chunk_minutes
        # overlap_sec не задан явно — берём из переменной окружения
        # SYNC_CLOUD_ASR_OVERLAP_SEC, иначе DEFAULT_OVERLAP_SEC (60 c).
        if overlap_sec is not None:
            self.overlap_sec = overlap_sec
        else:
            raw_overlap = os.getenv("SYNC_CLOUD_ASR_OVERLAP_SEC", "").strip()
            try:
                self.overlap_sec = float(raw_overlap) if raw_overlap else DEFAULT_OVERLAP_SEC
            except ValueError:
                self.overlap_sec = DEFAULT_OVERLAP_SEC
        self.timeout_sec = timeout_sec
        self._reasoning_effort = reasoning_effort
        self._http_client = http_client
        self._ffmpeg = ffmpeg
        self._ffprobe = ffprobe
        # Число попыток одного запроса к модели при сетевой ошибке, HTTP 429
        # или 5xx (см. _call_model) — тот же паттерн ретраев с backoff, что и
        # в доноре, но без привязки к отдельному LLM-провайдеру.
        self._max_retries = max_retries if max_retries is not None else DEFAULT_MAX_RETRIES

    def is_available(self) -> bool:
        return bool(self._api_key) and bool(self._base_url)

    def _require_available(self) -> None:
        if not self.is_available():
            raise CloudAudioAsrError(
                "Облачный ASR недоступен: не задан ключ (SYNC_CLOUD_ASR_API_KEY / "
                "OPENROUTER_API_KEY) или адрес провайдера (OPENROUTER_BASE_URL)."
            )

    @property
    def _chat_url(self) -> str:
        return f"{self._base_url}/chat/completions"

    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        if "openrouter.ai" in self._base_url:
            headers["HTTP-Referer"] = "https://scenarist.local"
            headers["X-Title"] = "Scenarist"
        return headers

    # -- нарезка -----------------------------------------------------------

    def _plan_chunks(self, duration_sec: float) -> list[tuple[float, float]]:
        """Части файла (начало, конец) в абсолютных секундах с перекрытием."""
        chunk_len = self.chunk_minutes * 60
        if duration_sec <= 0:
            return [(0.0, 0.0)]
        if duration_sec <= chunk_len or chunk_len <= 0:
            return [(0.0, duration_sec)]

        parts: list[tuple[float, float]] = []
        count = math.ceil(duration_sec / chunk_len)
        for i in range(count):
            nominal_start = i * chunk_len
            nominal_end = min((i + 1) * chunk_len, duration_sec)
            start = 0.0 if i == 0 else max(0.0, nominal_start - self.overlap_sec)
            parts.append((start, nominal_end))
        return parts

    def _probe_duration(self, media_path: Path) -> float:
        try:
            return _probe_duration(media_path, ffprobe=self._ffprobe)
        except CloudAudioAsrError:
            raise
        except Exception as exc:
            raise CloudAudioAsrError(
                f"Не удалось определить длительность {media_path}: {exc}"
            ) from exc

    async def _extract_chunk_mp3(
        self, media_path: Path, start_sec: float, duration_sec: float
    ) -> bytes:
        """Вырезает и перекодирует фрагмент аудио в mp3 моно 16 кГц через ffmpeg."""
        args = [
            self._ffmpeg,
            "-nostdin",
            "-y",
            "-ss",
            f"{max(0.0, start_sec):.3f}",
            "-t",
            f"{max(0.01, duration_sec):.3f}",
            "-i",
            str(media_path),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-codec:a",
            "libmp3lame",
            "-f",
            "mp3",
            "-loglevel",
            "error",
            "pipe:1",
        ]
        try:
            process = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise CloudAudioAsrError(
                f"Не найден исполняемый файл {self._ffmpeg!r}. Установите ffmpeg и добавьте в PATH."
            ) from exc

        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=_EXTRACT_CHUNK_TIMEOUT_SEC
            )
        except asyncio.TimeoutError as exc:
            # Зависший ffmpeg (без таймаута process.communicate() ждал бы его
            # завершения бесконечно, см. комментарий у _EXTRACT_CHUNK_TIMEOUT_SEC)
            # — убиваем процесс и поднимаем понятную ошибку вместо того, чтобы
            # повесить весь прогон.
            process.kill()
            await process.wait()
            raise CloudAudioAsrError(
                f"ffmpeg не ответил за {_EXTRACT_CHUNK_TIMEOUT_SEC:.0f} с при нарезке "
                f"фрагмента {media_path} (start={start_sec:.1f}с, duration={duration_sec:.1f}с)"
            ) from exc
        if process.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace").strip()[-500:]
            raise CloudAudioAsrError(
                f"ffmpeg завершился с ошибкой (код {process.returncode}) при нарезке фрагмента"
                + (f": {detail}" if detail else "")
            )
        return stdout

    # -- запрос к модели -----------------------------------------------------

    @staticmethod
    def _extract_cost(usage: dict) -> float | None:
        for key in ("cost", "total_cost", "cost_usd", "total_usd"):
            value = usage.get(key)
            if value is not None:
                try:
                    return float(value)
                except (TypeError, ValueError):
                    continue
        return None

    async def _sleep_backoff(self, attempt: int) -> None:
        """Пауза перед повторной попыткой (см. `_call_model`).

        Экспоненциальный рост (0.5с, 1с, 2с, ...) с добавленным случайным
        джиттером (до +25% базовой паузы) — без него несколько параллельных
        чанков/запусков, упавших одновременно (общий rate limit провайдера),
        повторяли бы попытки синхронно и снова коллективно упирались в тот же
        лимит. В тестах джиттер не мешает детерминированности: тесты ретраев
        подменяют этот метод целиком.
        """
        base = (2 ** (attempt - 1)) * 0.5
        jitter = base * random.uniform(0.0, 0.25)
        await asyncio.sleep(base + jitter)

    @staticmethod
    async def _await_cancellable(aw: Any, cancel: asyncio.Event | None) -> Any:
        """Ждёт корутину/future `aw`, прерываясь раньше, если сработает
        `cancel` (см. `_call_model`): без этого отмена, нажатая пользователем
        во время долгого сетевого запроса к модели (до `timeout_sec`, по
        умолчанию 900с) или во время паузы backoff перед ретраем, не
        замечалась бы до самого конца ожидания — отмена могла зависать на
        минуты вместо мгновенного отклика (см. проверку `cancel.is_set()`
        перед стартом каждого чанка в `transcribe_words`, которая покрывает
        только границу между чанками, а не ожидание внутри одного).

        Если `cancel` не передан — просто ждёт `aw` как обычно (совместимо со
        старым поведением, использующимся, когда вызывающий код отмену не
        поддерживает).
        """
        task = aw if isinstance(aw, asyncio.Task) else asyncio.ensure_future(aw)
        if cancel is None:
            return await task
        cancel_task = asyncio.ensure_future(cancel.wait())
        try:
            done, _pending = await asyncio.wait(
                {task, cancel_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if task in done:
                return task.result()
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            raise asyncio.CancelledError("Распознавание отменено пользователем")
        finally:
            if not cancel_task.done():
                cancel_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await cancel_task

    async def _call_model(
        self,
        client: httpx.AsyncClient,
        audio_b64: str,
        *,
        prompt: str,
        cancel: asyncio.Event | None = None,
    ) -> tuple[str, float | None, dict]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "input_audio",
                            "input_audio": {"data": audio_b64, "format": "mp3"},
                        },
                    ],
                }
            ],
            "temperature": 0.0,
            # Без этого флага OpenRouter не включает в ответ `usage.cost` —
            # `_extract_cost` всегда получал бы None, и вся отчётность по
            # деньгам (см. CloudAudioAsrError.cost_usd, total_cost ниже)
            # была бы недоступна.
            "usage": {"include": True},
        }
        # Расшифровке не нужны развёрнутые размышления модели: на записи
        # 15 минут они раздували ответ до 46 тысяч выходных токенов и делали
        # запуск в семь раз дороже. Полностью отключить их провайдер не
        # позволяет, поэтому просим минимальный объём.
        if self._reasoning_effort:
            payload["reasoning"] = {"effort": self._reasoning_effort}

        # Ретраи с backoff при сетевой ошибке, HTTP 429 или 5xx: один платный
        # запрос на чанк длинной записи не должен ронять весь прогон
        # транзиентной ошибкой. 4xx (кроме 429) — не ретраится, это ошибка
        # запроса, а не временный сбой.
        attempt = 0
        while True:
            attempt += 1
            if cancel is not None and cancel.is_set():
                raise asyncio.CancelledError("Распознавание отменено пользователем")
            try:
                resp = await self._await_cancellable(
                    client.post(self._chat_url, headers=self._headers(), json=payload), cancel
                )
            except httpx.HTTPError as exc:
                if attempt >= self._max_retries:
                    raise CloudAudioAsrError(
                        f"Ошибка сети при запросе к облачной ASR-модели после {attempt} попыток: {exc}"
                    ) from exc
                logger.warning(
                    "Облачный ASR: сетевая ошибка (попытка %d/%d), повтор",
                    attempt,
                    self._max_retries,
                )
                await self._await_cancellable(self._sleep_backoff(attempt), cancel)
                continue

            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt >= self._max_retries:
                    raise CloudAudioAsrError(
                        f"Облачная ASR-модель вернула статус {resp.status_code} после "
                        f"{attempt} попыток: {resp.text[:300]}"
                    )
                logger.warning(
                    "Облачный ASR: статус %d (попытка %d/%d), повтор",
                    resp.status_code,
                    attempt,
                    self._max_retries,
                )
                await self._await_cancellable(self._sleep_backoff(attempt), cancel)
                continue

            if resp.status_code >= 400:
                raise CloudAudioAsrError(
                    f"Облачная ASR-модель вернула ошибку {resp.status_code}: {resp.text[:300]}"
                )

            try:
                data = resp.json()
            except ValueError as exc:
                raise CloudAudioAsrError(
                    f"Облачная ASR-модель вернула тело ответа, которое не удалось "
                    f"разобрать как JSON: {resp.text[:300]}"
                ) from exc
            break

        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise CloudAudioAsrError(
                f"Некорректная структура ответа облачной ASR-модели: {data}"
            ) from exc

        if isinstance(content, list):
            # Некоторые провайдеры возвращают content частями (list of parts).
            content = "".join(part.get("text", "") for part in content if isinstance(part, dict))

        usage = data.get("usage") or {}
        cost = self._extract_cost(usage)
        return str(content), cost, data

    # -- низкоуровневый прогон -------------------------------------------------

    async def transcribe_words(
        self,
        audio_path: Path,
        *,
        language: str = "ru",
        on_progress: Callable[[AsrProgress], None] | None = None,
        cancel: asyncio.Event | None = None,
    ) -> AsrRunResult:
        self._require_available()
        audio_path = Path(audio_path)
        # _probe_duration — синхронный subprocess.run (см. модульную функцию
        # _probe_duration выше) с таймаутом до _PROBE_TIMEOUT_SEC (60 с):
        # вызванный напрямую внутри этой корутины, он блокировал бы event
        # loop на всё это время. asyncio.to_thread выносит блокирующий вызов
        # в отдельный поток.
        duration_sec = await asyncio.to_thread(self._probe_duration, audio_path)
        parts = self._plan_chunks(duration_sec)
        chunk_len = self.chunk_minutes * 60

        client = self._http_client
        own_client = False
        if client is None:
            client = httpx.AsyncClient(timeout=self.timeout_sec)
            own_client = True

        accumulated: list[AsrSegment] = []
        # Параллельный accumulated список флагов «конец сегмента — своя
        # граница от модели, а не приближение по началу следующей реплики»
        # (см. _parse_transcript_with_own_end) — используется при финальной
        # сборке ниже, чтобы не потерять точную границу, которую иначе
        # безусловно перезаписывает старая логика «конец = начало соседа».
        accumulated_own_end: list[bool] = []
        raw_responses: list[dict] = []
        total_cost = 0.0
        cost_found = False
        next_speaker_number = 1
        # Легенда голосов, накопленная по предыдущим частям (сквозные метки →
        # описание) — передаётся в промпт следующей части (пункт 2 защиты) и
        # используется при склейке меток после сборки всех частей (пункт 3).
        speaker_descriptions: dict[str, str] = {}

        try:
            for idx, (part_start, part_end) in enumerate(parts):
                if cancel is not None and cancel.is_set():
                    raise asyncio.CancelledError("Распознавание отменено пользователем")

                mp3_bytes = await self._extract_chunk_mp3(
                    audio_path, part_start, part_end - part_start
                )
                audio_b64 = base64.b64encode(mp3_bytes).decode("ascii")

                prompt = _build_prompt(
                    speaker_descriptions if idx > 0 else None,
                    next_number=next_speaker_number,
                )
                try:
                    content, cost, raw = await self._call_model(
                        client, audio_b64, prompt=prompt, cancel=cancel
                    )
                except CloudAudioAsrError as exc:
                    # Ретраи внутри _call_model уже исчерпаны для ЭТОГО чанка —
                    # запрос окончательно не удался. Если предыдущие чанки уже
                    # были успешно оплачены (cost_found), эта стоимость не
                    # должна пропасть молча вместе с исключением: приложим её
                    # к ошибке (см. CloudAudioAsrError.cost_usd), чтобы
                    # вызывающий код и логи не потеряли информацию об уже
                    # потраченных деньгах.
                    if cost_found:
                        raise CloudAudioAsrError(
                            f"{exc} (уже потрачено ~{total_cost:.4f} USD на {idx} из "
                            f"{len(parts)} успешно распознанных частях записи — эта работа "
                            "оплачена и не потеряна, но текущий запуск придётся повторить)",
                            cost_usd=total_cost,
                        ) from exc
                    raise
                raw_responses.append(raw)
                if cost is not None:
                    total_cost += cost
                    cost_found = True

                try:
                    transcript_text, legend_text = split_transcript_and_legend(content)
                    part_segments, part_own_end = _parse_transcript_with_own_end(
                        transcript_text, offset_sec=part_start
                    )
                except Exception as exc:
                    # Запрос к модели уже оплачен (см. total_cost выше) —
                    # если разбор её ответа не удался (например, модель
                    # вернула текст без ожидаемой разметки таймкодов), эта
                    # уже потраченная сумма не должна пропасть молча вместе
                    # с TranscriptParseError/иным исключением парсера,
                    # которое вызывающий код (ловит только
                    # CloudAudioAsrError) не перехватит.
                    raise CloudAudioAsrError(
                        f"Не удалось разобрать ответ облачной ASR-модели для части "
                        f"{idx + 1} из {len(parts)}: {exc}"
                        + (
                            f" (уже потрачено ~{total_cost:.4f} USD на {idx} из "
                            f"{len(parts)} успешно распознанных частях записи — эта "
                            "работа оплачена и не потеряна, но текущий запуск придётся "
                            "повторить)"
                            if cost_found
                            else ""
                        ),
                        cost_usd=total_cost if cost_found else None,
                    ) from exc
                if part_segments:
                    part_segments[-1] = replace(part_segments[-1], end=part_end)
                    # Конец принудительно дотянут до технической границы
                    # части — это уже не «свой» конец от модели.
                    part_own_end[-1] = False

                boundary = None if idx == 0 else idx * chunk_len
                to_add, own_end_to_add, next_speaker_number, label_map = _stitch_chunk(
                    accumulated,
                    part_segments,
                    part_own_end,
                    part_start=part_start,
                    boundary=boundary,
                    next_speaker_number=next_speaker_number,
                )
                accumulated.extend(to_add)
                accumulated_own_end.extend(own_end_to_add)

                # Переводим легенду этой части (локальные метки) в сквозные
                # метки через label_map и запоминаем первое известное описание
                # каждого сквозного голоса.
                for local_label, description in parse_speaker_legend(legend_text).items():
                    global_label = label_map.get(local_label)
                    if global_label is not None:
                        speaker_descriptions.setdefault(global_label, description)

                if on_progress is not None:
                    on_progress(
                        AsrProgress(
                            done=idx + 1,
                            total=len(parts),
                            message=f"Распознана часть {idx + 1} из {len(parts)}",
                            processed_sec=part_end,
                            duration_sec=duration_sec or None,
                        )
                    )
        finally:
            if own_client:
                await client.aclose()

        if cancel is not None and cancel.is_set():
            raise asyncio.CancelledError("Распознавание отменено пользователем")

        # Сортируем сегменты и их флаги «свой конец» синхронно (zip
        # сохраняет пары, сортировка — только по времени начала сегмента).
        paired = sorted(zip(accumulated, accumulated_own_end), key=lambda item: item[0].start)
        final_segments: list[AsrSegment] = []
        for i, (seg, own_end_flag) in enumerate(paired):
            next_start = paired[i + 1][0].start if i + 1 < len(paired) else None
            # Итоговый сосед по времени (после сшивки частей и отбрасывания
            # дублей на стыке) может отличаться от того, что было видно на
            # момент разбора одной части — поэтому санитарная проверка
            # своего конца повторяется здесь же, ещё раз (см. _resolve_end):
            # если модель дала конец, но он всё же вылез за пределы
            # итогового соседа больше чем на допуск — тихо откатываемся к
            # прежней логике вместо того, чтобы выдать заведомо неверную
            # границу.
            is_own_end = (
                own_end_flag
                and seg.end > seg.start
                and (next_start is None or seg.end <= next_start + _END_OVERSHOOT_TOLERANCE_SEC)
            )
            if is_own_end:
                end = min(seg.end, next_start) if next_start is not None else seg.end
            elif next_start is not None:
                end = next_start
            else:
                end = max(seg.start, duration_sec or seg.end)
            # `has_explicit_end` доносит признак «это настоящая граница от
            # модели» до вызывающего кода — см. AsrSegment.has_explicit_end.
            final_segments.append(replace(seg, end=end, has_explicit_end=is_own_end))

        # Пункт 3 защиты: склеиваем метки одного и того же человека и
        # перенумеровываем итоговые метки по убыванию суммарной длительности
        # речи (ГОЛОС 1 — тот, кто говорит дольше всех).
        final_segments, speakers, speaker_descriptions = _reconcile_speakers(
            final_segments,
            speaker_descriptions=speaker_descriptions,
        )

        word_count = sum(len(seg.text.split()) for seg in final_segments)

        return AsrRunResult(
            words=[],
            segments=final_segments,
            language=language or self.language,
            duration_sec=duration_sec,
            backend=self.name,
            model=self.model,
            device="cloud",
            compute_type="api",
            params={
                "raw_response": raw_responses,
                "chunks": len(parts),
                "chunk_minutes": self.chunk_minutes,
                "overlap_sec": self.overlap_sec,
                "base_url": self._base_url,
                "speaker_descriptions": speaker_descriptions,
            },
            word_count=word_count,
            speakers=speakers,
            cost_usd=total_cost if cost_found else None,
        )


# ---------------------------------------------------------------------------
# Публичный интерфейс модуля
# ---------------------------------------------------------------------------


def transcribe_cloud(
    audio_path: Path,
    *,
    model: str | None = None,
    language: str = "ru",
    progress_callback: Callable[[AsrProgress], None] | None = None,
    api_key: str | None = None,
    chunk_minutes: float | None = None,
    overlap_sec: float | None = None,
) -> dict[str, Any]:
    """Синхронный публичный вход в облачный ASR-адаптер (этап 1: только сам
    адаптер, без интеграции в whisper_runner/UI — см. docstring модуля).

    Оборачивает асинхронный `CloudAudioAsrAdapter.transcribe_words()` через
    `asyncio.run`. Возвращает словарь с ключом `"segments"` в формате,
    который уже умеет разбирать `src/stage2/sync_transcript.ingest_word_index`
    (линейная интерполяция слов внутри сегмента по его границам).
    """
    adapter = CloudAudioAsrAdapter(
        model=model or DEFAULT_MODEL,
        language=language,
        api_key=api_key,
        chunk_minutes=chunk_minutes if chunk_minutes is not None else DEFAULT_CHUNK_MINUTES,
        overlap_sec=overlap_sec,
    )
    result = asyncio.run(
        adapter.transcribe_words(Path(audio_path), language=language, on_progress=progress_callback)
    )

    segments = [
        {
            "text": seg.text,
            "start": seg.start,
            "end": seg.end,
            "speaker": seg.speaker,
            "has_explicit_end": seg.has_explicit_end,
        }
        for seg in result.segments
    ]

    return {
        "segments": segments,
        "speakers": result.speakers,
        "language": result.language,
        "model": result.model,
        "backend": result.backend,
        "duration_sec": result.duration_sec,
        "cost_usd": result.cost_usd,
    }
