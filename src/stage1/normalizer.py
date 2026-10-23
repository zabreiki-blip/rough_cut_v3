from __future__ import annotations

from datetime import datetime, timezone
from difflib import SequenceMatcher
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict, deque
from hashlib import sha256
import json
import logging
from pathlib import Path
import re
import threading
import time
from functools import lru_cache
from typing import Any, Callable
from uuid import uuid4

from atomic_io import atomic_write_json as _atomic_write_json
from atomic_io import atomic_write_text as _atomic_write_text
from cost_budget import CostBudget, CostBudgetExceeded
from openrouter_limits import raise_if_truncated, resolve_max_tokens
from openrouter_usage import report_json_usage

from .config import get_settings
from .io_utils import extract_paragraphs, read_json
from .normalization_memory import DEFAULT_MEMORY_PATH, NormalizationMemory, normalize_marker
from .normalization_models import (
    BlockType,
    DecisionSource,
    NormalizationStatus,
    NormalizedBlock,
    NormalizedScenario,
    SyncFragment,
    VALID_BLOCK_TYPES,
    VALID_DECISION_SOURCES,
)
from .scenario_context import (
    AMBIGUOUS_CAPS_WARNING,
    SCENE_CONTEXT_VERSION,
    SCENE_OPENER_CHANGED_WARNING,
    SCENE_TYPES,
    SECTION_HEADING_WARNING,
    SceneLink,
    classify_section_headings,
    is_dialogue_reply,
    is_plus_splice,
    is_section_heading,
    neighbour_context,
    plan_scene_context,
)
from .confirmation_checks import ConfirmationIssue, structural_checks
from .presenter_cues import parse_presenter_parts
from .scenario_structure import (
    LEAD_IN_WARNING,
    STANDUP_WARNING,
    STRUCTURE_PARSER_VERSION,
    SYNC_LABEL_ONLY_RE,
    ParagraphStructure,
    canonicalize_timecodes,
    find_repeated_author_numbers,
    parse_paragraph,
    parse_timecode,
    split_inline_sync_label,
    split_timed_parts,
    strip_leading_zk_label,
    timecode_warnings,
)

logger = logging.getLogger(__name__)


LLMClassifier = Callable[[list[dict[str, Any]], str], list[dict[str, Any]]]
LLM_PRESETS = {"standard", "fast"}
_REVIEWED_BLOCK_FIELDS = (
    "type",
    "original_label",
    "label_confirmed",
    "normalized_text",
    "text_for_tts",
    "speaker",
    "role",
    "source_description",
    "timecode_start",
    "timecode_end",
    "sync_fragments",
    "notes",
    "continuation_of_type",
    "absorbed_by_uid",
    "scene_uid",
)
_REVIEW_WARNING_PINNED_FIELDS = (
    ("Ambiguous timecode", ("timecode_start", "timecode_end", "sync_fragments")),
    (SECTION_HEADING_WARNING, ("type",)),
    ("Ambiguous label combination", ("type", "original_label", "label_confirmed")),
    (SCENE_OPENER_CHANGED_WARNING, ("type",)),
)


def normalize_llm_preset(value: str | None) -> str:
    """Return a supported, request-local OpenRouter routing preset."""
    preset = str(value or "standard").strip().lower()
    if preset not in LLM_PRESETS:
        raise ValueError("llm_preset must be 'standard' or 'fast'")
    return preset


class NormalizationError(RuntimeError):
    pass


class RevisionConflictError(NormalizationError):
    pass


class NormalizationValidationError(NormalizationError):
    def __init__(self, errors: list[str], issues: list[dict[str, Any]] | None = None):
        self.errors = errors
        # Structured blocking issues: {code, uid, order, message, severity}.
        self.issues = list(issues or [])
        super().__init__("; ".join(errors))


class SourceChangedError(NormalizationError):
    pass


_BUILTIN_MARKERS = {
    "vo": BlockType.VOICEOVER.value,
    "narrator": BlockType.VOICEOVER.value,
    "зк": BlockType.VOICEOVER.value,
    "zk": BlockType.VOICEOVER.value,
    "з к": BlockType.VOICEOVER.value,
    "з": BlockType.VOICEOVER.value,
    "закадр": BlockType.VOICEOVER.value,
    "закадровый текст": BlockType.VOICEOVER.value,
    "диктор": BlockType.VOICEOVER.value,
    "голос диктора": BlockType.VOICEOVER.value,
    "снх": BlockType.SYNC.value,
    "синхр": BlockType.SYNC.value,
    "синхрон": BlockType.SYNC.value,
    "интервью": BlockType.SYNC.value,
    "ведущий": BlockType.HOST.value,
    "ведущая": BlockType.HOST.value,
    "лайф": BlockType.LIFE.value,
    "лайфы": BlockType.LIFE.value,
    "life": BlockType.LIFE.value,
    # Stand-up: the presenter speaks on camera, but who exactly is not stated
    # by the label -- the block is flagged for a manual speaker check.
    "стендап": BlockType.SYNC.value,
    # Lead-in (capitals only, see ``scenario_structure``): same treatment.
    "подводка": BlockType.SYNC.value,
}
_TECH_MARKERS = {
    "видео",
    "титр",
    "титры",
    "плашка",
    "графика",
    "карта",
    "фото",
    "архив",
    "отбивка",
    "шапка",
    "склейка",
    "ремарка",
    "техническое",
    "тех",
    "техн",
}
_MARKER_TYPES = {**_BUILTIN_MARKERS, **dict.fromkeys(_TECH_MARKERS, BlockType.TECHNICAL.value)}


@lru_cache(maxsize=1024)
def _paragraph_structure(text: str) -> ParagraphStructure:
    """Shared structural parse (label, author number, speech) of one paragraph."""
    return parse_paragraph(text, _MARKER_TYPES)


def _structural_text(text: str) -> str:
    """Parsing view of a paragraph with its timecodes in the stored colon form.

    ``(00:42:32)`` -> ``00:42:32``, ``01.24.08`` -> ``01:24:08``, an unclosed
    ``(00:35:45`` loses its bracket.  Only the view changes: ``original_text``
    stays exactly as written, and narration is never rewritten.
    """
    return canonicalize_timecodes(text)


_LIFE_PREFIX = re.compile(
    r"^\s*(?P<label>ЛАЙФЫ?|LIFE)\b\s*(?:[,.:\-–—]\s*)?(?P<body>.*)$",
    re.IGNORECASE | re.DOTALL,
)
_EXPLICIT_SYNC_CREDIT = re.compile(
    r"^\s*(?P<marker>СНХ|СИНХРОН|SYNC)\s*(?P<sep>[,.])\s*"
    # The credit ends at a label colon (never the colon of a timecode, i.e. one
    # right after a digit) or right before a timecode: ``СНХ. Иванов 00:10:00 ...``.
    r"(?P<speaker>[^:\n]{1,80}?)"
    r"(?:(?<!\d)\s*:\s*[,;]?\s*|\s+(?=[(\[]?\d{1,2}[:.]\d{2}))"
    r"(?P<content>.+)$",
    re.IGNORECASE | re.DOTALL,
)
# ``СНХ Имя, роль — файл.wav 00:05:42 — реплика``: the marker is followed by
# whitespace (no comma), the credit by a dash and an optional media file name.
_SNH_DASH_CREDIT = re.compile(
    r"^\s*(?P<marker>СНХ|СИНХРОН|SYNC)\s+(?P<credit>[А-ЯЁA-Z][^—–\n]{1,160}?)\s+"
    # The dash is optional when the credit is directly followed by a timecode
    # (``СНХ ПОПОВ 00:12:06 — реплика``); such a credit carries no media file.
    r"(?:(?P<dash>[—–-])\s+|(?=\[?\d{1,2}[:.\-]))"
    r"(?P<rest>.+)$",
    re.IGNORECASE | re.DOTALL,
)
_MEDIA_EXTENSIONS = r"(?:wav|mp3|mp4|mov|m4a|aiff?|mxf|aac|flac|ogg|avi|mkv)"
# A media file name (may contain spaces and parentheses) ending the text right
# before a timecode, optionally followed by a dash.
_TRAILING_MEDIA_CREDIT = re.compile(
    rf"(?P<name>[^\n—–]{{1,118}}?\.{_MEDIA_EXTENSIONS})(?![\w.])\s*[—–-]?\s*$",
    re.IGNORECASE,
)
_SENTENCE_BOUNDARY = re.compile(r"[.!?…»\"”]\s+")
_MEDIA_FILE_NAME = re.compile(r"^(?:.{1,118}\.[A-Za-z0-9]{2,5}|\S{1,120})$")
_EXPLICIT_SYNC_LABEL = re.compile(r"^(?:СНХ|СИНХРОН|SYNC)\s*,\s*(.+)$", re.IGNORECASE)
_TIMECODE_TOKEN = (
    r"(?:\[?\d{1,2}[:.][0-5]\d(?:\s+|[:.])[0-5]\d\]?|"
    r"\[?\d{1,2}[:.][0-5]\d\]?|\[?\d{1,2}-[0-5]\d\]?)"
    r"(?!\w)"
)
_TIMECODE = re.compile(rf"({_TIMECODE_TOKEN})")
# ``T1 - T2`` as one range.  LIFE parts joined by ``+`` are separate inserts.
_TIMECODE_RANGE = re.compile(rf"{_TIMECODE_TOKEN}\s*[-–—]\s*{_TIMECODE_TOKEN}")
_SYNC_METADATA = re.compile(
    r"^([А-ЯЁA-Z][А-ЯЁа-яёA-Za-z\s\-]{1,60}),\s*"
    # The role fragment must stop at the first sentence-ending punctuation:
    # a genuine speaker credit's role is a short noun phrase ("режиссер
    # сериала", "поэт, издатель") and never spans a period or ellipsis. This
    # keeps the pattern from matching ordinary prose that happens to contain
    # an early comma and a timecode further along the same sentence (e.g.
    # "То, что получилось у нас, это живая история... 00:03:04 Мы делали...").
    rf"([^.…!?\n]{{2,160}}?)(?:,\s*|:\s*|\s+)({_TIMECODE_TOKEN})"
    r"[.\s–\-]*(.*)$",
    re.DOTALL,
)
_SYNC_LABEL_METADATA = re.compile(
    # The role, like in `_SYNC_METADATA` above, must not cross a
    # sentence-ending boundary: a real "Name, role" credit's role is always
    # a short noun phrase, never prose that happens to start with a comma
    # (e.g. "То, что получилось у нас, это живая история...").
    r"^([А-ЯЁA-Z][А-ЯЁа-яёA-Za-z\s\-]{1,60}),\s*([^.…!?\n]{2,160})$"
)
# Root words that make a bracket after a name a footage/source description.
_SOURCE_WORD = (
    r"(?i:фильм|сериал|кино|передач|архив|хроник|интервью|запис|видео|программ|эфир|фото)"
)
_SYNC_NAME_ONLY_LABEL = re.compile(
    # An optional bracketed *source* follows the name: ``Имя (фильм): 38:38 ...``.
    # A bracket without a source word (``Имя (эксперт)``) is a role, not a source.
    r"^[А-ЯЁA-Z][А-ЯЁа-яёA-Za-z\-]+(?:\s+[А-ЯЁA-Z][А-ЯЁа-яёA-Za-z\-]+)*"
    rf"(?:\s*\((?=[^()\n]*{_SOURCE_WORD})[^()\n]{{2,60}}\))?$"
)
_SYNC_NAME_SOURCE_SPLIT = re.compile(r"^(?P<name>[^()]+?)\s*\((?P<source>[^()]+)\)$")
# ``Голос «Маяковского»: текст`` -- a named voice reading text aloud.
_NAMED_VOICE_LABEL = re.compile(r"^Голос\s+[«\"“][^«»\"“”\n]{1,60}[»\"”]$", re.IGNORECASE)
# ``СНХ. Отец: речь`` -- the dot after the marker may be a separator, but then the
# credit must be a short name, never the first words of a sentence.
_DOT_CREDIT_MAX_WORDS = 4
# A paragraph that is *only* a sync label, with no colon and no quote --
# e.g. "Синх Сторожевой" -- names the interviewee in the genitive case ahead
# of a separate paragraph carrying the actual (often mid-sentence-timecoded)
# quote. Recognized independently of the generic marker machinery below,
# which requires an exact alias match and cannot absorb a trailing name.
_SYNC_LABEL_ONLY_MARKER = SYNC_LABEL_ONLY_RE
_SYNC_CONTINUATION_NOTE = re.compile(r"^\s*\((?:продолжение(?:\s+синхрона?)?)\)\s*", re.IGNORECASE)
_EDITORIAL_TIMECODE_PREFIX = re.compile(
    rf"^\s*\((?:[^)\n]{{1,240}}\)\s*:?\s*|[^:\n]{{1,240}}:\s*)"
    rf"{_TIMECODE_TOKEN}",
    re.DOTALL,
)
# ``ПОДВОДКА № 6`` / ``СТЕНДАП 3`` / ``ПОДВОДКА``: a numbered heading with no
# speech after it.
_PRESENTER_HEADING_ONLY = re.compile(
    r"^\s*(?:СНХ\s+)?(?:СТЕНДАП|(?-i:ПОДВОДКА))\s*(?:№|N|#|No\.?)?\s*\d*\s*[.:]?\s*$",
    re.IGNORECASE,
)
TIMECODE_NOTE_WARNING = "timecode note without label: check manually"
_TIMECODE_NOTE_MAX_WORDS = 4
_PLAYBACK_LIFE_CUE = re.compile(
    r"(?:\bзвучит\s+песня\b|\bпереход\s+на\s+клип\b|\bклип\b.{0,60}\bна\s+припев\b)",
    re.IGNORECASE | re.DOTALL,
)
_BARE_SPEAKER_PREFIX = re.compile(
    r"^\s*(?P<speaker>(?:[А-ЯЁ][а-яё]+\s+)?[А-ЯЁ]{3,})"
    r"(?:[.:\-–—]?\s+|[.:](?=[А-ЯЁ])|(?=[а-яё]))(?P<content>.+)$",
    re.DOTALL,
)
_CLEAR_SPEAKER_CREDIT = re.compile(
    r"^\s*(?P<speaker>[А-ЯЁ][а-яё]+(?:\s+[А-ЯЁ][а-яё]+)?)"
    r"(?:,\s*[а-яё][а-яё,\s-]{2,80})?"
    r"(?P<separator>[:.]\s*(?:[-–—]\s*)?)"
    r"(?P<content>.+)$",
    re.DOTALL,
)
_EDITORIAL_VOICEOVER_CUE = re.compile(
    r"^(?:ЗТМ\.?|НА КАДРАХ\b.+|ФРАГМЕНТ СЕРИАЛА\b.+|"
    # ``ФР СЕРИАЛА, ГДЕ ... – ЕСТЬ`` / ``ФРАГМЕНТ ИЗ ФИЛЬМА – ...``: a note about
    # footage to insert.  The prefix must be written in capitals (``(?-i:..)``);
    # narration such as ``Фрагмент фильма показывает ...`` stays speech.
    r"(?-i:ФР(?:АГМЕНТ)?)\b\.?\s+(?:(?-i:ИЗ)\s+)?"
    r"(?:сериал|фильм|кино|передач|ролик|интервью|эфир|видео|хроник|материал)\w*\b.*|"
    r"ЗДЕСЬ\s+(?:БЫ\s+НУЖЕН|НАДО(?:\s+БЫ)?\s+(?:ДАТЬ|ПОСТАВИТЬ|ВСТАВИТЬ))\s+ФРАГМЕНТ\b.+|"
    r"\([^)]*(?:фото|кадр|план)[^)]*\))$",
    re.IGNORECASE | re.DOTALL,
)
_TTS_VOICE_CREDIT = re.compile(
    r"^\s*(?:[«\"“]\s*)?Голос\s+(?:за\s+кадром\s+читает\s+воспоминания\s+[^:]+|"
    r"[«\"“]?[А-ЯЁа-яё]+[»\"”]?)\s*(?:[»\"”]\s*)?[:.\-–—]\s*",
    re.IGNORECASE,
)
_TTS_EDITORIAL_PARENTHESES = re.compile(
    r"\s*\((?:показываем|показать|вставить|на\s+кадрах)\b[^)]*\)",
    re.IGNORECASE,
)


ZK_LABEL_REMOVED_WARNING = "ЗК label removed from TTS text"
ABSORBED_LINK_LOST_WARNING = "Absorbed-by link dropped: target block was not found after re-parse"


def strip_zk_label(text: str) -> str:
    """Drop a leading ``ЗК`` voiceover label (``ЗК:``, ``ЗК14``, ``З.К.``); rest unchanged."""
    return strip_leading_zk_label(text)


def clean_voiceover_tts_text(text: str, *, strip_zk: bool = True) -> str:
    """Remove speech-direction labels and on-screen cues from spoken text.

    The leading ``ЗК`` label marks a voiceover block and is not speech.  It is
    removed at most once (``strip_zk``) and only from the very start, so the
    result is stable: ``ЗК: ЗК РФ запрещает`` -> ``ЗК РФ запрещает``.  Callers
    that receive text whose label was already split off (rule/LLM parsing,
    saved drafts) pass ``strip_zk=False``.
    """
    value = text.strip()
    if strip_zk:
        value = strip_zk_label(value).strip()
    value = _TTS_VOICE_CREDIT.sub("", value)
    value = _TTS_EDITORIAL_PARENTHESES.sub("", value)
    return value.replace("<…>", "…").strip()


_STRUCTURAL_HEADING = re.compile(
    r"^(?:часть\b|глава\b|заставка\b|эпизод\b|\d+\s*\.\s*$)",
    re.IGNORECASE,
)
# Seed lexicon for an unmarked "video cue" paragraph: a shot-list remark
# describing what footage should be shown on screen (portraits, busts,
# archival photos, drone/copter passes, etc.) rather than spoken narration.
# This is a hand-picked starting list; it is deliberately kept as a plain
# module constant so it can later be promoted into a learnable
# NormalizationMemory rule instead of a hardcoded regex.
_VIDEO_CUE_KEYWORDS = (
    "планы",
    "план",
    "портреты",
    "портрет",
    "бюст",
    "камень",
    "интерьер",
    "экстерьер",
    "детали",
    "деталь",
    "крупный план",
    "панорама",
    "проход",
    "коптер",
    "дрон",
    "архив",
    "фотографии",
    "фотография",
    "фото",
    "съёмка",
    "съемка",
    "кадры",
    "кадр",
    "виды",
    "вид",
)
_VIDEO_CUE_KEYWORD_RE = re.compile(
    r"\b(?:"
    + "|".join(re.escape(word) for word in sorted(_VIDEO_CUE_KEYWORDS, key=len, reverse=True))
    + r")\b",
    re.IGNORECASE,
)


# _seed_video_cue_rules is idempotent at the database level (record_manual's
# upsert never regresses a rule a user already disabled and is a no-op once
# a keyword is already active), but generate_normalization() runs it once
# per *document*, and every call re-issues all 24 upserts even though the
# rows never change after the first run. Skip the redundant writes with a
# process-local "already seeded" cache keyed by the memory database's
# resolved path -- not a single process-wide flag -- so unrelated memory
# databases (e.g. per-project or per-test tmp DBs opened in the same
# process) each still get seeded on their own first use. Guarded by a lock
# because generate_normalization can run concurrently from worker threads.
_video_cue_seeded_db_paths: set[str] = set()
_video_cue_seed_lock = threading.Lock()


def invalidate_video_cue_seed_cache(db_path: Path | str | None = None) -> None:
    """Forget that ``db_path`` (or, with no argument, every database) has
    already been seeded, so the next ``_seed_video_cue_rules`` call for it
    re-issues the upserts.

    Needed whenever something other than ``_seed_video_cue_rules`` itself
    can undo what seeding is responsible for -- most notably a user
    deleting a scope='video_cue' rule via the memory API: without
    invalidating the cache, the deleted rule would only come back after a
    process restart (when the in-memory "already seeded" set is naturally
    reset), making the API's behavior depend on process lifetime instead of
    being deterministic.
    """
    with _video_cue_seed_lock:
        if db_path is None:
            _video_cue_seeded_db_paths.clear()
            return
        _video_cue_seeded_db_paths.discard(str(Path(db_path).resolve()))


def _seed_video_cue_rules(memory: NormalizationMemory) -> None:
    """Seed the built-in video-cue lexicon into scope='video_cue' memory
    rules. Idempotent: ``record_manual``'s upsert never regresses a rule a
    user already disabled and is a no-op once a keyword is already active,
    so calling this once per normalization run never creates duplicates or
    overrides manual edits. Skipped entirely once this database has already
    been seeded in this process (see ``_video_cue_seeded_db_paths`` above).
    """
    cache_key = str(memory.path.resolve())
    with _video_cue_seed_lock:
        if cache_key in _video_cue_seeded_db_paths:
            return
        for keyword in _VIDEO_CUE_KEYWORDS:
            memory.record_manual(keyword, BlockType.TECHNICAL.value, scope="video_cue")
        _video_cue_seeded_db_paths.add(cache_key)


def _video_cue_keyword_pattern(memory: NormalizationMemory | None) -> re.Pattern[str]:
    """Build the effective video-cue keyword regex for a normalization run:
    the built-in seed plus any active learned keywords, minus anything a
    user disabled. Meant to be computed once per run (not per block) and
    passed down explicitly, since it requires a memory query.

    Falls back to the built-in-only pattern when memory is unavailable or
    the lookup fails, so normalization never breaks on a memory error.
    """
    if memory is None:
        return _VIDEO_CUE_KEYWORD_RE
    try:
        _seed_video_cue_rules(memory)
        rules = memory.list_rules(scope="video_cue")
    except Exception:
        logger.warning(
            "Failed to load learned video-cue keywords; using the built-in seed only",
            exc_info=True,
        )
        return _VIDEO_CUE_KEYWORD_RE
    active = {str(rule["marker"]).strip() for rule in rules if rule["status"] == "active"}
    disabled = {str(rule["marker"]).strip() for rule in rules if rule["status"] == "disabled"}
    keywords = ((set(_VIDEO_CUE_KEYWORDS) | active) - disabled) - {""}
    if keywords == set(_VIDEO_CUE_KEYWORDS):
        return _VIDEO_CUE_KEYWORD_RE
    return re.compile(
        r"\b(?:"
        + "|".join(re.escape(word) for word in sorted(keywords, key=len, reverse=True))
        + r")\b",
        re.IGNORECASE,
    )


# Coarse heuristic for a Russian personal (finite) verb form: past tense
# (-л/-ла/-ло/-ли, incl. reflexive -лся/-лась/-лись) and present/future
# 3rd-person and plural endings. It is intentionally approximate: a false
# positive here only keeps the safer voiceover classification.
_PERSONAL_VERB_RE = re.compile(
    r"\b[а-яё]{3,}(?:лся|лась|лись|ла|ло|ли|л|ете|ите|ешь|ишь|ют|ат|ят|ет|ит|ем|им)\b",
    re.IGNORECASE,
)
_VIDEO_CUE_MAX_WORDS = 12
# An unmarked, all-caps first paragraph of a scenario is almost always the
# film/scenario title (e.g. "БЕЛАРУСЬ. САРЬЯ. ХРАМ ВО ИМЯ ЛЮБВИ ИГНАТИЯ
# ЛОПАТИНСКОГО"), not spoken narration. It must never reach TTS or the
# interview search, so it belongs in "technical" like production credits.
# At least one Cyrillic/Latin letter is required so punctuation-only text
# does not match; the personal-verb check (shared with _looks_like_video_cue)
# keeps an all-caps opening line of narration from being misclassified.
_SCENARIO_TITLE_RE = re.compile(r"^[^а-яa-z]*[А-ЯЁA-Z][^а-яa-z]*$")
# A real scenario title is a short phrase (e.g. the 8-word/55-char example
# above); generous caps well above that keep long all-caps narration or
# a single very long run of caps (no spaces at all) from being mistaken
# for a title. Both bounds are needed: a single run-on "word" with no
# spaces would otherwise pass a word-count-only check.
_SCENARIO_TITLE_MAX_WORDS = 20
_SCENARIO_TITLE_MAX_CHARS = 120
_LLM_CONFIDENCE_THRESHOLD = 0.65
# Bumped whenever a change to the classification prompt/instructions should
# invalidate every previously cached LLM answer, even though the prompt text
# is already part of the cache fingerprint (see `_classifier_identity`): a
# version bump also drops stale entries from the on-disk cache file outright
# instead of merely leaving them unreachable under their old keys.
_LLM_CACHE_VERSION = 3
_LLM_CACHE_MAX_ENTRIES = 2000
# A single paragraph can be arbitrarily large (a pasted article without line
# breaks).  The classifier must answer per block uid, so a block cannot be
# split; instead the LLM sees its head and tail only, which bounds the cost of
# one request regardless of block size.
_LLM_BLOCK_TEXT_MAX_CHARS = 8000
_LLM_BLOCK_HEAD_CHARS = 6000
_LLM_BLOCK_TAIL_CHARS = 1500
_LLM_TRUNCATION_MARKER = "\n[... middle of an oversized block omitted ...]\n"
_OPENROUTER_SYSTEM_PROMPT = "You normalize TV production scripts."
_OPENROUTER_INSTRUCTIONS = (
    "Classify every scenario paragraph without deleting or merging any item. "
    "Allowed types: voiceover, sync, host, life, technical, other, unknown. "
    'Return JSON {"items":[{"uid":...,"type":...,"confidence":0..1,'
    '"label_is_marker":true|false,'
    '"text_for_tts":...,"speaker":...,"role":...,'
    '"timecode_start":...,"timecode_end":...,"notes":...}]}. '
    "Always choose the single most likely type, even when you are not fully "
    "certain -- use unknown only for text that is empty or unreadable. "
    "A line that is a URL/link to source footage, a bulleted or dashed list "
    "item (a song/film title, a name, a credit), a section heading (e.g. "
    '"Клипы", "Хроника"), or a production note telling the editor what '
    'to insert/show (e.g. "Вставить синхрон", "Показать фото") is '
    "type=technical, not unknown. Do not rewrite original content. "
    "A paragraph beginning with ЛАЙФ or LIFE is always type=life, even when "
    "it contains a title, colon, quote text, or timecode; it is source sound "
    "and must never become voiceover or sync. "
    "An item with continuation_of_type=voiceover is an unlabelled paragraph "
    "continuing the preceding voiceover unless its text clearly starts a new "
    "speaker or technical cue. "
    "An item may carry a context object with truncated neighbouring "
    "paragraphs (before/after). It is for reference only: classify the item "
    "itself, never the neighbours, and never copy their text. A neighbour "
    'with type "unresolved" has not been classified yet and says nothing '
    "about the item. An unlabelled plain sentence or line of verse that "
    "nothing marks as an interview, life sound or editorial cue is narration: "
    "type=voiceover. Leave speaker, role and timecodes empty when the text "
    "does not state them (never write the word unknown). "
)
_PATH_LOCKS: dict[str, threading.RLock] = {}
_PATH_LOCKS_GUARD = threading.Lock()


def generate_normalization(
    input_path: Path,
    output_path: Path,
    *,
    memory_path: Path | None = None,
    use_llm: bool = True,
    hint: str = "",
    classifier: LLMClassifier | None = None,
    llm_preset: str = "standard",
    usage_callback: Callable[[dict[str, Any]], None] | None = None,
    budget: CostBudget | None = None,
) -> dict[str, Any]:
    llm_preset = normalize_llm_preset(llm_preset)
    with _path_lock(output_path):
        return _generate_normalization_unlocked(
            input_path,
            output_path,
            memory_path=memory_path,
            use_llm=use_llm,
            hint=hint,
            classifier=classifier,
            llm_preset=llm_preset,
            usage_callback=usage_callback,
            budget=budget,
        )


def _generate_normalization_unlocked(
    input_path: Path,
    output_path: Path,
    *,
    memory_path: Path | None = None,
    use_llm: bool = True,
    hint: str = "",
    classifier: LLMClassifier | None = None,
    llm_preset: str = "standard",
    usage_callback: Callable[[dict[str, Any]], None] | None = None,
    budget: CostBudget | None = None,
) -> dict[str, Any]:
    """Create and persist a complete, ordered normalization draft."""
    input_path = Path(input_path)
    if not input_path.exists():
        raise FileNotFoundError(f"Input file not found: {input_path}")
    output_path = Path(output_path)
    previous: NormalizedScenario | None = None
    if output_path.is_file():
        try:
            previous = NormalizedScenario.from_dict(read_json(output_path))
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            previous = None
    logger.info("Normalization started source=%s llm=%s", input_path, use_llm)
    source_hash = _file_hash(input_path)
    if (
        previous is not None
        and previous.scenario_revision_id
        and previous.source_hash != source_hash
    ):
        raise SourceChangedError(
            "Active scenario revision cannot be replaced by another source; "
            "import it as a new scenario revision"
        )
    paragraphs = _merge_snh_credit_headers(_extract_paragraphs(input_path))
    now = _utcnow()
    blocks: list[NormalizedBlock] = []

    with NormalizationMemory(memory_path or DEFAULT_MEMORY_PATH) as memory:
        # Computed once per run (not per block): building it requires a
        # memory query, and the seed keyword list rarely changes mid-run.
        video_cue_re = _video_cue_keyword_pattern(memory)
        for order, original in enumerate(paragraphs, start=1):
            label, content = _split_label(original)
            block = NormalizedBlock(
                uid=f"block-{order}-{uuid4().hex[:8]}",
                order=order,
                original_text=original,
                normalized_text=original,
                text_for_tts="",
                original_label=label,
            )
            # Inside an open LIFE/SYNC scene a short timecoded line is a quote
            # of that scene, not an editor's note.
            in_scene = bool(blocks) and blocks[-1].type in (
                BlockType.LIFE.value,
                BlockType.SYNC.value,
            )
            _classify_by_rules(block, memory, video_cue_re, in_scene)
            for warning in (
                *_paragraph_structure(original).warnings,
                *timecode_warnings(original),
            ):
                # A numbered presenter heading has no speaker to identify.
                if (
                    warning in {STANDUP_WARNING, LEAD_IN_WARNING}
                    and block.type == BlockType.TECHNICAL.value
                    and _PRESENTER_HEADING_ONLY.match(original)
                ):
                    continue
                if warning not in block.warnings:
                    block.warnings.append(warning)
            if block.type == BlockType.SYNC.value and _is_rejected_dot_sync_credit(original):
                block.warnings.append(SYNC_CREDIT_REJECTED_WARNING)
            if block.type != BlockType.UNKNOWN.value and block.original_label:
                block.label_confirmed = True
                if content:
                    block.normalized_text = content
                elif block.type == BlockType.VOICEOVER.value:
                    # A marker-only paragraph (very common as ``З.к.`` on its
                    # own line) is a production cue for the following block,
                    # not text that should ever reach TTS.
                    _apply_type(
                        block,
                        BlockType.TECHNICAL.value,
                        DecisionSource.BUILTIN_RULE.value,
                        1.0,
                    )
                    block.notes = "section_marker_for=voiceover"
                    block.text_for_tts = ""
            _extract_block_metadata(block)
            if block.type == BlockType.VOICEOVER.value:
                block.text_for_tts = block.normalized_text
            blocks.append(block)

        _warn_repeated_author_numbers(blocks)
        _annotate_section_headings(blocks)
        _annotate_continuation_context(blocks, video_cue_re)
        # Continuation context can supply a speaker only after the first
        # metadata pass.  Re-extract its fragments now so a bare timecoded
        # continuation remains independently searchable in Stage 2.
        for block in blocks:
            if (
                block.type == BlockType.SYNC.value
                and block.continuation_of_type == BlockType.SYNC.value
                and not block.sync_fragments
            ):
                _extract_block_metadata(block)
        _annotate_scene_context(blocks)
        unresolved = [
            block
            for block in blocks
            if block.type == BlockType.UNKNOWN.value
            and AMBIGUOUS_CAPS_WARNING not in block.warnings
            and not _paragraph_structure(block.original_text).has_label_conflict
        ]
        if unresolved and use_llm:
            if classifier is not None:
                llm = classifier
            elif llm_preset == "standard" and usage_callback is None and budget is None:
                # Keep the legacy call shape: integrations and tests may patch
                # this classifier with a two-argument callable.
                llm = _openrouter_classifier
            else:
                llm_kwargs: dict[str, Any] = {"llm_preset": llm_preset}
                if usage_callback is not None:
                    llm_kwargs["usage_callback"] = usage_callback
                if budget is not None:
                    llm_kwargs["budget"] = budget
                llm = lambda batch, author_hint: _openrouter_classifier(
                    batch, author_hint, **llm_kwargs
                )
            block_index = {block.uid: position for position, block in enumerate(blocks)}
            llm_inputs = {
                block.uid: _llm_input(block, neighbour_context(blocks, block_index[block.uid]))
                for block in unresolved
            }
            batches = _llm_batches(unresolved, inputs=llm_inputs)
            payloads = [[llm_inputs[block.uid] for block in batch] for batch in batches]
            cache_path = _llm_cache_path(Path(output_path))
            cache = _load_llm_cache(cache_path)
            rules_signature = _llm_rules_signature(memory)
            cache_keys = [
                _llm_cache_fingerprint(
                    payload,
                    hint=hint,
                    classifier=classifier,
                    llm_preset=llm_preset,
                    rules_signature=rules_signature,
                )
                for payload in payloads
            ]
            batch_results: list[tuple[list[dict[str, Any]] | None, Exception | None]] = [
                (None, None) for _ in batches
            ]
            pending: list[int] = []
            for index, (payload, cache_key) in enumerate(zip(payloads, cache_keys)):
                cached = _llm_cached_results(cache, cache_key, payload)
                if cached is None:
                    pending.append(index)
                else:
                    batch_results[index] = (cached, None)

            # A large documentary script can contain hundreds of unresolved
            # paragraphs. OpenRouter calls are independent, so bounded
            # concurrency keeps the HTTP request practical without flooding the
            # provider. Injected classifiers remain sequential for deterministic
            # tests and custom integrations.
            if classifier is None and len(pending) > 1:
                with ThreadPoolExecutor(max_workers=min(4, len(pending))) as pool:
                    futures = {pool.submit(llm, payloads[index], hint): index for index in pending}
                    for future in as_completed(futures):
                        index = futures[future]
                        try:
                            results = future.result()
                            batch_results[index] = (results, None)
                            _cache_llm_results(
                                cache_path,
                                cache,
                                cache_keys[index],
                                payloads[index],
                                results,
                            )
                        except CostBudgetExceeded:
                            # A spent job budget is a hard stop, not a batch warning:
                            # drop queued batches so no new paid call starts.
                            pool.shutdown(wait=False, cancel_futures=True)
                            raise
                        except Exception as exc:
                            logger.exception("LLM batch %d classification failed", index)
                            batch_results[index] = (None, exc)
            else:
                for index in pending:
                    try:
                        results = llm(payloads[index], hint)
                        batch_results[index] = (results, None)
                        _cache_llm_results(
                            cache_path,
                            cache,
                            cache_keys[index],
                            payloads[index],
                            results,
                        )
                    except CostBudgetExceeded:
                        raise
                    except Exception as exc:
                        logger.exception("LLM batch %d classification failed", index)
                        batch_results[index] = (None, exc)

            for batch, (batch_result, error) in zip(batches, batch_results):
                if error is None:
                    _apply_llm_results(
                        batch,
                        batch_result or [],
                        video_cue_re=video_cue_re,
                        all_blocks=blocks,
                    )
                    continue
                message = f"LLM classification failed: {type(error).__name__}: {error}"
                for block in batch:
                    block.warnings.append(message)
            _recover_llm_unresolved_voiceover(blocks, video_cue_re)

    for block in blocks:
        if block.type == BlockType.VOICEOVER.value:
            # The label was split off once when the block was parsed
            # (``_split_label`` / LLM boundary); never strip a second time, or
            # speech such as ``ЗК РФ запрещает это.`` loses its first word.
            block.text_for_tts = clean_voiceover_tts_text(block.text_for_tts, strip_zk=False)
    _snapshot_proposals(blocks)
    scenario = NormalizedScenario(
        source=str(input_path),
        source_hash=source_hash,
        blocks=blocks,
        hint=hint.strip(),
        created_at=now,
        updated_at=now,
    )
    if previous is not None and previous.source_hash == source_hash:
        scenario.revision = previous.revision + 1
        scenario.scenario_revision_id = previous.scenario_revision_id
        scenario.parent_revision_id = previous.parent_revision_id
        uid_map: dict[str, str] = {}
        previous_by_text: dict[str, deque[NormalizedBlock]] = defaultdict(deque)
        for block in previous.blocks:
            previous_by_text[block.original_text].append(block)
        for block in scenario.blocks:
            candidates = previous_by_text.get(block.original_text)
            if not candidates:
                continue
            old = candidates.popleft()
            uid_map[block.uid] = old.uid
            block.uid = old.uid
            block.versioning = dict(old.versioning)
            reviewed = set(old.reviewed_warnings)
            block.reviewed_warnings = list(old.reviewed_warnings)
            block.manually_edited = old.manually_edited
            block.manual_fields = list(old.manual_fields)
            block.warnings = [warning for warning in block.warnings if warning not in reviewed]
            legacy_manual = old.source == DecisionSource.MANUAL.value and not old.manual_fields
            fields_to_restore = set(old.manual_fields)
            if legacy_manual:
                fields_to_restore = set(_REVIEWED_BLOCK_FIELDS)
            for field_name in fields_to_restore:
                if field_name in _REVIEWED_BLOCK_FIELDS:
                    setattr(block, field_name, getattr(old, field_name))
            classification_pinned = legacy_manual or bool(
                fields_to_restore & {"type", "original_label", "label_confirmed"}
            )
            if classification_pinned:
                block.proposed_type = old.proposed_type
                block.proposed_label = old.proposed_label
                block.proposed_label_confirmed = old.proposed_label_confirmed
                block.proposed_source = old.proposed_source
                block.proposed_memory_rule_id = old.proposed_memory_rule_id
                block.source = old.source
                block.confidence = old.confidence
                block.memory_rule_id = old.memory_rule_id
        for block in scenario.blocks:
            if block.scene_uid:
                block.scene_uid = uid_map.get(block.scene_uid, block.scene_uid)
        valid_uids = {block.uid for block in scenario.blocks}
        for block in scenario.blocks:
            if not block.absorbed_by_uid:
                continue
            target = uid_map.get(block.absorbed_by_uid, block.absorbed_by_uid)
            if target in valid_uids and target != block.uid:
                block.absorbed_by_uid = target
                continue
            block.absorbed_by_uid = ""
            if ABSORBED_LINK_LOST_WARNING not in block.warnings:
                block.warnings.append(ABSORBED_LINK_LOST_WARNING)
        _flag_changed_scene_openers(scenario.blocks)
    scenario_payload = scenario.to_dict()
    if previous is not None:
        # The re-parse is a comparable draft: the replaced revision (often a
        # confirmed one) stays on disk and can be diffed against / restored.
        _snapshot_normalization_file(output_path, previous)
    _atomic_write_json(Path(output_path), scenario_payload)
    logger.info("Normalization draft written source=%s blocks=%d", input_path, len(blocks))
    return scenario_payload


def _warn_repeated_author_numbers(blocks: list[NormalizedBlock]) -> None:
    """Flag each block that shares an author number (``ЗК7`` twice) with another.

    Block level (not scenario level): the reviewer sees the warning on the very
    block to renumber, and every block keeps its own UID -- the author's number
    is never an identifier and repeated numbers never merge blocks.
    """
    structures = [_paragraph_structure(block.original_text) for block in blocks]
    for (label, number), indexes in find_repeated_author_numbers(structures).items():
        for index in indexes:
            blocks[index].warnings.append(
                f"Repeated author number {label}{number} ({len(indexes)} occurrences, "
                f"first in block {blocks[indexes[0]].order}): check numbering"
            )


NORMALIZATION_HISTORY_DIRNAME = "normalization_history"
_HISTORY_NAME_RE = re.compile(r"\d{8}T\d{12}Z_rev\d+(?:-\d+)?\.json")
MAX_NORMALIZATION_HISTORY = 30


def normalization_history_dir(normalized_path: Path) -> Path:
    """Where replaced normalization revisions are kept (next to the draft)."""
    return Path(normalized_path).parent / NORMALIZATION_HISTORY_DIRNAME


def _snapshot_normalization_file(
    normalized_path: Path, scenario: NormalizedScenario
) -> Path | None:
    """Copy the on-disk normalization to ``normalization_history`` before it is replaced."""
    normalized_path = Path(normalized_path)
    try:
        data = normalized_path.read_bytes()
    except OSError:
        return None
    directory = normalization_history_dir(normalized_path)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    target = directory / f"{stamp}_rev{scenario.revision}.json"
    counter = 1
    while target.exists():
        target = directory / f"{stamp}_rev{scenario.revision}-{counter}.json"
        counter += 1
    temporary = target.with_name(f".{target.name}.tmp")
    temporary.write_bytes(data)
    temporary.replace(target)
    _prune_normalization_history(directory)
    return target


def _prune_normalization_history(directory: Path) -> None:
    """Keep the newest ``MAX_NORMALIZATION_HISTORY`` snapshots, drop the older ones."""
    names = sorted(
        path.name for path in directory.iterdir() if _HISTORY_NAME_RE.fullmatch(path.name)
    )
    for name in names[: max(0, len(names) - MAX_NORMALIZATION_HISTORY)]:
        try:
            (directory / name).unlink()
        except OSError:
            logger.warning("Cannot remove old normalization snapshot %s", name)


def list_normalization_history(normalized_path: Path) -> list[dict[str, Any]]:
    """Snapshots of replaced revisions, newest first.

    Each item: ``{name, path, revision, status, created_at, confirmed_at, blocks}``.
    ``name`` is the identifier accepted by :func:`diff_normalization_snapshot`
    and :func:`restore_normalization_snapshot`.
    """
    directory = normalization_history_dir(normalized_path)
    if not directory.is_dir():
        return []
    items: list[dict[str, Any]] = []
    for path in sorted(directory.iterdir(), reverse=True):
        if not _HISTORY_NAME_RE.fullmatch(path.name):
            continue
        try:
            payload = read_json(path)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        items.append(
            {
                "name": path.name,
                "path": str(path),
                "revision": payload.get("revision"),
                "status": payload.get("status", ""),
                "created_at": payload.get("created_at", ""),
                "confirmed_at": payload.get("confirmed_at", ""),
                "blocks": len(payload.get("blocks") or []),
            }
        )
    return items


def _load_history_snapshot(normalized_path: Path, name: str) -> dict[str, Any]:
    if not isinstance(name, str) or not _HISTORY_NAME_RE.fullmatch(name):
        raise ValueError(f"Invalid normalization snapshot name: {name!r}")
    path = normalization_history_dir(normalized_path) / name
    if not path.is_file():
        raise FileNotFoundError(f"Normalization snapshot not found: {name}")
    return NormalizedScenario.from_dict(read_json(path)).to_dict()


def diff_normalization_payloads(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """Pure block diff between two normalization payloads (stage4 ``compare_blocks``).

    Returns ``{summary, changes, snapshot, current}`` where ``snapshot`` and
    ``current`` describe the compared sides (``revision``, ``status``, ``blocks``).
    """
    from stage4.versioning import compare_blocks

    old_blocks = [b for b in old.get("blocks") or [] if isinstance(b, dict)]
    new_blocks = [b for b in new.get("blocks") or [] if isinstance(b, dict)]
    result = compare_blocks(old_blocks, new_blocks)
    result["snapshot"] = {
        "revision": old.get("revision"),
        "status": old.get("status", ""),
        "blocks": len(old_blocks),
    }
    result["current"] = {
        "revision": new.get("revision"),
        "status": new.get("status", ""),
        "blocks": len(new_blocks),
    }
    return result


def diff_normalization_snapshot(normalized_path: Path, snapshot: str) -> dict[str, Any]:
    """Diff a history snapshot (``name`` from the list) against the current draft."""
    with _path_lock(normalized_path):
        old = _load_history_snapshot(normalized_path, snapshot)
        current = NormalizedScenario.from_dict(read_json(Path(normalized_path))).to_dict()
    return diff_normalization_payloads(old, current)


def restore_normalization_snapshot(
    normalized_path: Path,
    snapshot: str,
    *,
    expected_revision: int | None = None,
) -> dict[str, Any]:
    """Roll the draft back to a history snapshot, as a new *draft* revision.

    The currently stored revision is snapshotted first, so a restore is itself
    reversible.  The restored scenario is never ``confirmed``: it must be
    confirmed again, and ``scenario_prepared.json`` is not touched here.
    """
    normalized_path = Path(normalized_path)
    with _path_lock(normalized_path):
        old = NormalizedScenario.from_dict(_load_history_snapshot(normalized_path, snapshot))
        current: NormalizedScenario | None = None
        if normalized_path.is_file():
            current = NormalizedScenario.from_dict(read_json(normalized_path))
            if expected_revision is not None and current.revision != expected_revision:
                raise RevisionConflictError(
                    f"Expected revision {expected_revision}, current revision is {current.revision}"
                )
        source_path = Path((current or old).source)
        if not source_path.exists() or _file_hash(source_path) != old.source_hash:
            raise SourceChangedError("Source scenario changed after this snapshot was taken")
        if current is not None:
            _snapshot_normalization_file(normalized_path, current)
            old.revision = current.revision + 1
            old.created_at = current.created_at or old.created_at
            old.source = current.source
        else:
            old.revision += 1
        old.status = NormalizationStatus.DRAFT.value
        old.confirmed_at = ""
        old.updated_at = _utcnow()
        result = old.to_dict()
        _atomic_write_json(normalized_path, result)
        return result


def load_normalization(path: Path) -> dict[str, Any]:
    return NormalizedScenario.from_dict(read_json(Path(path))).to_dict()


def reset_normalization(
    path: Path,
    *,
    expected_revision: int | None = None,
    source_root: Path | None = None,
) -> dict[str, Any]:
    """Clear all normalization blocks while preserving source identity and revision history."""
    with _path_lock(path):
        path = Path(path)
        scenario = NormalizedScenario.from_dict(read_json(path))
        if expected_revision is not None and scenario.revision != expected_revision:
            raise RevisionConflictError(
                f"Expected revision {expected_revision}, current revision is {scenario.revision}"
            )
        source_path = Path(scenario.source).resolve()
        if source_root is not None and not source_path.is_relative_to(Path(source_root).resolve()):
            raise SourceChangedError("Normalization source is outside the project")
        if not source_path.exists() or _file_hash(source_path) != scenario.source_hash:
            raise SourceChangedError("Source scenario changed after normalization")

        scenario.blocks = []
        scenario.status = NormalizationStatus.DRAFT.value
        scenario.revision += 1
        scenario.hint = ""
        scenario.warnings = []
        scenario.updated_at = _utcnow()
        scenario.confirmed_at = ""
        result = scenario.to_dict()
        _atomic_write_json(path, result)
        return result


def save_normalization(
    path: Path,
    payload: dict[str, Any],
    *,
    expected_revision: int | None = None,
) -> dict[str, Any]:
    with _path_lock(path):
        return _save_normalization_unlocked(path, payload, expected_revision=expected_revision)


def _save_normalization_unlocked(
    path: Path,
    payload: dict[str, Any],
    *,
    expected_revision: int | None = None,
) -> dict[str, Any]:
    """Save user edits as a new draft revision, preserving source identity."""
    path = Path(path)
    current = NormalizedScenario.from_dict(read_json(path)) if path.exists() else None
    if (
        current is not None
        and expected_revision is not None
        and current.revision != expected_revision
    ):
        raise RevisionConflictError(
            f"Expected revision {expected_revision}, current revision is {current.revision}"
        )
    incoming = NormalizedScenario.from_dict(payload)
    if current is not None:
        if incoming.source_hash != current.source_hash or incoming.source != current.source:
            raise SourceChangedError(
                "Source and source_hash cannot be changed while editing a draft"
            )
        previous = {block.uid: block for block in current.blocks}
        for block in incoming.blocks:
            old = previous.get(block.uid)
            _sanitize_saved_voiceover(block, old)
            if old is not None:
                block.proposed_type = old.proposed_type
                block.proposed_label = old.proposed_label
                block.proposed_label_confirmed = old.proposed_label_confirmed
                block.proposed_source = old.proposed_source
                block.proposed_memory_rule_id = old.proposed_memory_rule_id
            if old is not None and block.original_label and not old.label_confirmed:
                _label, content = _split_label(old.original_text)
                if block.original_label != old.original_label:
                    block.label_confirmed = True
                elif content and (
                    block.normalized_text == content or block.text_for_tts == content
                ):
                    block.label_confirmed = True
                    block.normalized_text = content
            if old is not None and (
                block.type != old.type or block.original_label != old.original_label
            ):
                block.source = DecisionSource.MANUAL.value
                block.confidence = 1.0
            if old is not None:
                changed_names = {
                    field_name
                    for field_name in _REVIEWED_BLOCK_FIELDS
                    if getattr(block, field_name) != getattr(old, field_name)
                }
                reviewed = set(old.reviewed_warnings)
                removed_warnings = [w for w in old.warnings if w not in block.warnings]
                reviewed.update(removed_warnings)
                reviewed.difference_update(block.warnings)
                block.reviewed_warnings = sorted(reviewed)
                # The automatic ZK-removal warning is not a reviewer decision.
                warnings_changed = set(block.warnings) - {ZK_LABEL_REMOVED_WARNING} != set(
                    old.warnings
                ) - {ZK_LABEL_REMOVED_WARNING}
                acknowledged_fields = {
                    field_name
                    for warning in removed_warnings
                    for prefix, pinned_fields in _REVIEW_WARNING_PINNED_FIELDS
                    if warning.startswith(prefix)
                    for field_name in pinned_fields
                }
                block.manual_fields = sorted(
                    (
                        set(_REVIEWED_BLOCK_FIELDS)
                        if old.source == DecisionSource.MANUAL.value and not old.manual_fields
                        else set(old.manual_fields)
                    )
                    | changed_names
                    | acknowledged_fields
                )
                block.manually_edited = (
                    old.manually_edited or bool(changed_names) or warnings_changed
                )
            else:
                block.reviewed_warnings = []
                block.manual_fields = []
                block.manually_edited = False
            if old is not None:
                block.original_text = old.original_text
            block.warnings = list(dict.fromkeys(block.warnings))
        incoming.created_at = current.created_at
        incoming.revision = current.revision + 1
    else:
        incoming.revision = max(1, incoming.revision)
        incoming.created_at = incoming.created_at or _utcnow()
        for block in incoming.blocks:
            _sanitize_saved_voiceover(block)
            block.reviewed_warnings = []
            block.manually_edited = False
            block.manual_fields = []
    _flag_changed_scene_openers(incoming.blocks)
    incoming.status = NormalizationStatus.DRAFT.value
    incoming.confirmed_at = ""
    incoming.updated_at = _utcnow()
    _validate_structure(incoming)
    result = incoming.to_dict()
    _atomic_write_json(path, result)
    return result


def _sanitize_saved_voiceover(block: NormalizedBlock, old: NormalizedBlock | None = None) -> None:
    """Remove a leading ``ЗК`` label a user typed into a voiced block; warn, never block.

    Only a field the user changed against the stored block can carry a typed
    label: stored text was already sanitized once when it was written, so an
    unchanged ``ЗК РФ запрещает это.`` is speech and must stay intact.
    """
    if block.type != BlockType.VOICEOVER.value or block.absorbed_by_uid:
        return
    tts_typed = old is None or block.text_for_tts != old.text_for_tts
    normalized_typed = old is None or block.normalized_text != old.normalized_text
    text = block.text_for_tts
    if tts_typed and text.strip():
        text = strip_zk_label(text)
    normalized = (
        strip_zk_label(block.normalized_text) if normalized_typed else block.normalized_text
    )
    if text == block.text_for_tts and normalized == block.normalized_text:
        return
    block.text_for_tts = text
    block.normalized_text = normalized
    if ZK_LABEL_REMOVED_WARNING not in block.warnings:
        block.warnings.append(ZK_LABEL_REMOVED_WARNING)


def _flag_changed_scene_openers(blocks: list[NormalizedBlock]) -> None:
    """Warn on replies whose scene opener is no longer a LIFE/SYNC/HOST block.

    The reply type is never changed here: the user decides.  The warning is
    removed again once the opener is a scene block.
    """
    by_uid = {block.uid: block for block in blocks}
    for block in blocks:
        if not block.scene_uid:
            continue
        opener = by_uid.get(block.scene_uid)
        if opener is None or opener.type not in SCENE_TYPES:
            if (
                SCENE_OPENER_CHANGED_WARNING not in block.reviewed_warnings
                and SCENE_OPENER_CHANGED_WARNING not in block.warnings
            ):
                block.warnings.append(SCENE_OPENER_CHANGED_WARNING)
        elif SCENE_OPENER_CHANGED_WARNING in block.warnings:
            block.warnings.remove(SCENE_OPENER_CHANGED_WARNING)


def confirm_normalization(
    normalized_path: Path,
    prepared_path: Path,
    raw_path: Path,
    *,
    memory_path: Path | None = None,
    expected_revision: int | None = None,
    learning_decisions: list[dict[str, Any]] | None = None,
    source_path_override: Path | None = None,
) -> dict[str, Any]:
    with _path_lock(normalized_path):
        return _confirm_normalization_unlocked(
            normalized_path,
            prepared_path,
            raw_path,
            memory_path=memory_path,
            expected_revision=expected_revision,
            learning_decisions=learning_decisions,
            source_path_override=source_path_override,
        )


def _confirm_normalization_unlocked(
    normalized_path: Path,
    prepared_path: Path,
    raw_path: Path,
    *,
    memory_path: Path | None = None,
    expected_revision: int | None = None,
    learning_decisions: list[dict[str, Any]] | None = None,
    source_path_override: Path | None = None,
) -> dict[str, Any]:
    """Validate, learn confirmed aliases, and atomically publish projections."""
    normalized_path = Path(normalized_path)
    scenario = NormalizedScenario.from_dict(read_json(normalized_path))
    if expected_revision is not None and scenario.revision != expected_revision:
        raise RevisionConflictError(
            f"Expected revision {expected_revision}, current revision is {scenario.revision}"
        )
    # A revision may have been authored in a container and later repaired on
    # the host. Keep its recorded source identity while verifying an explicit
    # local copy against the same source hash.
    source_path = Path(source_path_override) if source_path_override else Path(scenario.source)
    if not source_path.exists() or _file_hash(source_path) != scenario.source_hash:
        raise SourceChangedError("Source scenario changed after normalization")
    issues = [
        issue
        for issue in validate_for_confirmation_detailed(scenario)
        if issue["severity"] == "error"
    ]
    if issues:
        raise NormalizationValidationError(
            list(dict.fromkeys(issue["message"] for issue in issues)), issues
        )

    resolved_memory_path = memory_path or DEFAULT_MEMORY_PATH
    with NormalizationMemory(resolved_memory_path) as memory:
        learning_candidates = _infer_learning_candidates(scenario, memory)
    decisions = _validate_learning_decisions(learning_candidates, learning_decisions)

    scenario.status = NormalizationStatus.CONFIRMED.value
    scenario.revision += 1
    scenario.updated_at = _utcnow()
    scenario.confirmed_at = scenario.updated_at
    prepared = _prepared_projection(scenario)
    raw_text = _raw_projection(scenario)
    _snapshot_proposals(scenario.blocks)
    _atomic_write_json(Path(prepared_path), prepared)
    _atomic_write_text(Path(raw_path), raw_text)
    result = scenario.to_dict()
    _atomic_write_json(normalized_path, result)
    logger.info(
        "Normalization confirmed source=%s revision=%s blocks=%d",
        scenario.source,
        scenario.revision,
        len(scenario.blocks),
    )
    try:
        _learn_from_confirmation(scenario, resolved_memory_path, learning_candidates, decisions)
    except Exception as exc:
        # Publishing the confirmed scenario is authoritative. A memory failure
        # must not roll it back or make downstream projections inconsistent.
        result.setdefault("warnings", []).append(
            f"Normalization memory was not updated: {type(exc).__name__}: {exc}"
        )
    return result


def preview_learning_candidates(
    normalized_path: Path,
    *,
    memory_path: Path | None = None,
    expected_revision: int | None = None,
) -> list[dict[str, Any]]:
    with _path_lock(normalized_path):
        scenario = NormalizedScenario.from_dict(read_json(Path(normalized_path)))
        if expected_revision is not None and scenario.revision != expected_revision:
            raise RevisionConflictError(
                f"Expected revision {expected_revision}, current revision is {scenario.revision}"
            )
        with NormalizationMemory(memory_path or DEFAULT_MEMORY_PATH) as memory:
            return _infer_learning_candidates(scenario, memory)


_WARNING_CODES: tuple[tuple[str, str], ...] = (
    ("Repeated author number", "repeated_author_number"),
    (STANDUP_WARNING, "standup_label"),
    (LEAD_IN_WARNING, "lead_in_label"),
    (SECTION_HEADING_WARNING, "section_heading"),
    (AMBIGUOUS_CAPS_WARNING, "ambiguous_caps_heading"),
    (SCENE_OPENER_CHANGED_WARNING, "scene_opener_changed"),
    ("Ambiguous timecode", "ambiguous_timecode"),
    ("Ambiguous label combination", "ambiguous_label_combination"),
    ("Sync credit has no source media file", "sync_credit_no_media"),
    (ZK_LABEL_REMOVED_WARNING, "zk_label_removed"),
    ("LLM", "low_confidence_llm"),
    ("Possible learned alias", "learned_alias_suggestion"),
    ("leading '+' splice", "leading_splice"),
)


def _warning_code(message: str) -> str:
    for prefix, code in _WARNING_CODES:
        if message.startswith(prefix):
            return code
    return "block_warning"


def _structure_issue_uid(message: str, scenario: NormalizedScenario) -> tuple[str, int]:
    match = re.search(r"Block (\d+)", message)
    order = int(match.group(1)) if match else 0
    if match and 1 <= order <= len(scenario.blocks):
        return scenario.blocks[order - 1].uid, order
    return "", order


def validate_for_confirmation_detailed(
    scenario: NormalizedScenario | dict[str, Any],
) -> list[dict[str, Any]]:
    """Confirmation issues as ``{code, uid, order, message, severity}`` dicts.

    ``severity == "error"`` blocks confirmation; ``"warning"`` is informational
    (repeated author number, low LLM confidence, stand-up/lead-in label, section
    heading and similar).  ``validate_for_confirmation`` is the legacy view.
    """
    value = (
        scenario
        if isinstance(scenario, NormalizedScenario)
        else NormalizedScenario.from_dict(scenario)
    )
    issues: list[ConfirmationIssue] = []

    def add(code: str, block: NormalizedBlock, message: str, severity: str = "error") -> None:
        issues.append(ConfirmationIssue(code, block.uid, block.order, message, severity))

    try:
        _validate_structure(value)
    except NormalizationValidationError as exc:
        for message in exc.errors:
            uid, order = _structure_issue_uid(message, value)
            issues.append(ConfirmationIssue("invalid_structure", uid, order, message))
    blocks_by_uid = {block.uid: block for block in value.blocks}
    for block in value.blocks:
        if block.absorbed_by_uid:
            target = blocks_by_uid.get(block.absorbed_by_uid)
            if (
                block.type not in {BlockType.VOICEOVER.value, BlockType.TECHNICAL.value}
                or target is None
                or target.uid == block.uid
                or target.type != BlockType.VOICEOVER.value
                or target.absorbed_by_uid
            ):
                add(
                    "invalid_absorbed_link",
                    block,
                    f"Block {block.order} has an invalid absorbed-by link",
                )
            if block.text_for_tts.strip():
                add(
                    "absorbed_block_has_tts",
                    block,
                    f"Block {block.order} is absorbed but still has TTS text",
                )
        if block.type == BlockType.UNKNOWN.value:
            add("unknown_type", block, f"Block {block.order} has unknown type")
        if (
            block.type == BlockType.VOICEOVER.value
            and not block.absorbed_by_uid
            and not block.text_for_tts.strip()
        ):
            add("empty_tts", block, f"Voiceover block {block.order} has empty TTS text")
        if (
            block.type == BlockType.VOICEOVER.value
            and clean_voiceover_tts_text(block.text_for_tts, strip_zk=False)
            != block.text_for_tts.strip()
        ):
            add(
                "speech_direction_in_tts",
                block,
                f"Voiceover block {block.order} contains a speech-direction cue",
            )
        if block.type == BlockType.VOICEOVER.value and not block.absorbed_by_uid:
            label, content = _split_label(block.original_text)
            marked_cue = normalize_marker(label) in _BUILTIN_MARKERS and (
                _EDITORIAL_VOICEOVER_CUE.match(content)
                or (content.endswith(":") and re.search(r"\bголос\b", content, re.IGNORECASE))
            )
            if _EDITORIAL_VOICEOVER_CUE.match(block.original_text.strip()) or marked_cue:
                add(
                    "editorial_cue_in_voiceover",
                    block,
                    f"Voiceover block {block.order} contains an editorial cue",
                )
        if block.type == BlockType.SYNC.value:
            if not block.speaker.strip():
                # A sync typed only by scene context (splice) is not an
                # explicit label: the author may mean the same speaker.
                opener = blocks_by_uid.get(block.scene_uid)
                add(
                    "sync_no_speaker",
                    block,
                    f"Sync block {block.order} has no speaker",
                    "warning"
                    if opener is not None
                    and opener.speaker.strip()
                    and SYNC_CREDIT_REJECTED_WARNING not in block.warnings
                    else "error",
                )
            elif re.search(r"\b(?:читает|по[её]т)\s*$", block.speaker, re.IGNORECASE):
                add(
                    "sync_direction_as_speaker",
                    block,
                    f"Sync block {block.order} has a direction instead of a speaker",
                )
            if block.original_text.strip().endswith(":") and not _TIMECODE.search(
                block.original_text
            ):
                add(
                    "sync_only_credit",
                    block,
                    f"Sync block {block.order} contains only a speaker credit",
                )
            if (
                block.original_label
                and _SYNC_NAME_ONLY_LABEL.fullmatch(block.original_label.strip())
                and normalize_marker(block.original_label) not in _BUILTIN_MARKERS
                and block.speaker
                and normalize_marker(block.original_label) != normalize_marker(block.speaker)
            ):
                add(
                    "sync_speaker_conflict",
                    block,
                    f"Sync block {block.order} speaker conflicts with its credit",
                )
            for fragment in block.sync_fragments:
                if not re.search(r"\w", fragment.text):
                    add(
                        "sync_empty_fragment",
                        block,
                        f"Sync block {block.order} contains an empty fragment",
                    )
                for timecode in (fragment.timecode_start, fragment.timecode_end):
                    if timecode and not _TIMECODE.fullmatch(timecode):
                        add(
                            "invalid_timecode",
                            block,
                            f"Sync block {block.order} has invalid timecode {timecode}",
                        )
        for code, message, severity in structural_checks(block, _paragraph_structure):
            add(code, block, message, severity)
        for message in block.warnings:
            add(_warning_code(message), block, message, "warning")
        if (
            block.source == DecisionSource.LLM.value
            and 0 < block.confidence < _LLM_CONFIDENCE_THRESHOLD
        ):
            add(
                "low_confidence_llm",
                block,
                f"Block {block.order} LLM confidence {block.confidence:.0%} is low",
                "warning",
            )
    seen: set[tuple[str, str, str]] = set()
    unique: list[dict[str, Any]] = []
    for issue in issues:
        key = (issue.code, issue.uid, issue.message)
        if key not in seen:
            seen.add(key)
            unique.append(issue.to_dict())
    return unique


def validate_for_confirmation(scenario: NormalizedScenario | dict[str, Any]) -> list[str]:
    """Blocking confirmation errors as messages (legacy view of the detailed result)."""
    return list(
        dict.fromkeys(
            issue["message"]
            for issue in validate_for_confirmation_detailed(scenario)
            if issue["severity"] == "error"
        )
    )


def is_normalization_confirmed(path: Path, *, source_path: Path | None = None) -> bool:
    try:
        scenario = NormalizedScenario.from_dict(read_json(Path(path)))
    except (OSError, ValueError, TypeError, KeyError):
        return False
    if scenario.status != NormalizationStatus.CONFIRMED.value:
        return False
    actual_source = Path(source_path) if source_path else Path(scenario.source)
    return actual_source.exists() and _file_hash(actual_source) == scenario.source_hash


def import_legacy_normalization(
    prepared_path: Path,
    raw_path: Path,
    output_path: Path,
    *,
    source_path: Path | None = None,
) -> dict[str, Any]:
    """Import legacy prepared/raw files as an explicitly unconfirmed draft."""
    prepared = read_json(Path(prepared_path))
    raw_text = Path(raw_path).read_text(encoding="utf-8") if Path(raw_path).exists() else ""
    source = Path(source_path) if source_path else Path(str(prepared.get("source", raw_path)))
    if not source.exists() and raw_text:
        source = Path(output_path).parent / "scenario_legacy_source.txt"
        _atomic_write_text(source, raw_text)
    source_hash = (
        _file_hash(source) if source.exists() else sha256(raw_text.encode("utf-8")).hexdigest()
    )
    blocks: list[NormalizedBlock] = []
    prepared_items = list(prepared.get("items", []))
    if raw_text.strip():
        from stage2.timeline_parser import parse_timeline

        legacy_timeline = parse_timeline(
            raw_text,
            Path("/__normalization_import_no_audio__"),
            Path("/__normalization_import_no_placeholders__"),
            "legacy-import",
        )
        voiceover_index = 0
        for segment in legacy_timeline.segments:
            order = len(blocks) + 1
            if segment.type == "zk":
                item = (
                    prepared_items[voiceover_index] if voiceover_index < len(prepared_items) else {}
                )
                voiceover_index += 1
                original = str(item.get("text_original") or segment.text).strip()
                blocks.append(
                    NormalizedBlock(
                        uid=f"legacy-{order}-{uuid4().hex[:8]}",
                        order=order,
                        original_text=original,
                        normalized_text=original,
                        text_for_tts=str(item.get("text_for_tts") or original).strip(),
                        original_label=f"ЗК{voiceover_index}",
                        label_confirmed=True,
                        type=BlockType.VOICEOVER.value,
                        source=DecisionSource.MANUAL.value,
                        confidence=1.0,
                        notes="imported_from_legacy",
                    )
                )
            elif segment.type == "snh":
                blocks.append(
                    NormalizedBlock(
                        uid=f"legacy-{order}-{uuid4().hex[:8]}",
                        order=order,
                        original_text=segment.text,
                        normalized_text=segment.text,
                        original_label="СНХ",
                        label_confirmed=True,
                        type=BlockType.SYNC.value,
                        source=DecisionSource.MANUAL.value,
                        confidence=1.0,
                        speaker=segment.speaker,
                        role=segment.role,
                        timecode_start=segment.timecode,
                        notes="imported_from_legacy",
                    )
                )
            else:
                label, content = _split_label(segment.text)
                marker_type = _BUILTIN_MARKERS.get(normalize_marker(label))
                block_type = marker_type or BlockType.TECHNICAL.value
                blocks.append(
                    NormalizedBlock(
                        uid=f"legacy-{order}-{uuid4().hex[:8]}",
                        order=order,
                        original_text=segment.text,
                        normalized_text=content if marker_type and content else segment.text,
                        original_label=label,
                        label_confirmed=bool(label),
                        type=block_type,
                        source=DecisionSource.MANUAL.value,
                        confidence=1.0,
                        speaker=label if block_type == BlockType.HOST.value else "",
                        notes="imported_from_legacy",
                    )
                )
        for item in prepared_items[voiceover_index:]:
            order = len(blocks) + 1
            original = str(item.get("text_original", "")).strip()
            blocks.append(
                NormalizedBlock(
                    uid=f"legacy-{order}-{uuid4().hex[:8]}",
                    order=order,
                    original_text=original,
                    normalized_text=original,
                    text_for_tts=str(item.get("text_for_tts", original)).strip(),
                    original_label=f"ЗК{voiceover_index + 1}",
                    label_confirmed=True,
                    type=BlockType.VOICEOVER.value,
                    source=DecisionSource.MANUAL.value,
                    confidence=1.0,
                    notes="imported_from_legacy_missing_in_raw",
                )
            )
            voiceover_index += 1
    else:
        for item in prepared_items:
            order = len(blocks) + 1
            original = str(item.get("text_original", "")).strip()
            blocks.append(
                NormalizedBlock(
                    uid=f"legacy-{order}-{uuid4().hex[:8]}",
                    order=order,
                    original_text=original,
                    normalized_text=original,
                    text_for_tts=str(item.get("text_for_tts", original)).strip(),
                    original_label=f"ЗК{order}",
                    label_confirmed=True,
                    type=BlockType.VOICEOVER.value,
                    source=DecisionSource.MANUAL.value,
                    confidence=1.0,
                    notes="imported_from_legacy",
                )
            )
    now = _utcnow()
    _snapshot_proposals(blocks)
    scenario = NormalizedScenario(
        source=str(source),
        source_hash=source_hash,
        blocks=blocks,
        status=NormalizationStatus.DRAFT.value,
        created_at=now,
        updated_at=now,
        warnings=["Imported legacy data requires confirmation"],
    )
    result = scenario.to_dict()
    _atomic_write_json(Path(output_path), result)
    return result


def _extract_paragraphs(input_path: Path) -> list[str]:
    return extract_paragraphs(input_path)


def _snapshot_proposals(blocks: list[NormalizedBlock]) -> None:
    for block in blocks:
        block.proposed_type = block.type
        block.proposed_label = block.original_label
        block.proposed_label_confirmed = block.label_confirmed
        block.proposed_source = block.source
        block.proposed_memory_rule_id = block.memory_rule_id


CAPS_HEADING_AMBIGUOUS_NOTE = "caps_heading_ambiguous"


def _annotate_section_headings(blocks: list[NormalizedBlock]) -> None:
    """Unlabelled all-caps lines (``ПОСЛУШАНИЕ В САДУ``): heading or speech?

    A strong signal (repeated paragraph, or a complete block before and an
    explicit scene/technical block after; see
    ``scenario_context.classify_section_headings``) makes the line a
    ``technical`` heading: no TTS, no inheriting voiceover/life, closes the
    scene.  Otherwise the line stays ``unknown`` (suggested ``technical``), is
    kept away from voiceover inheritance and the LLM, and warns: only a human
    may decide that a slogan like ``СЛАВА РОССИИ`` is not spoken.
    """
    candidates = []
    for block in blocks:
        candidate = False
        if block.type == BlockType.UNKNOWN.value and not block.original_label:
            structure = _paragraph_structure(block.original_text)
            candidate = not (
                structure.has_label or structure.has_label_conflict
            ) and is_section_heading(block.original_text)
        candidates.append(candidate)
    for block, verdict in zip(blocks, classify_section_headings(blocks, candidates)):
        if verdict == "technical":
            _apply_type(block, BlockType.TECHNICAL.value, DecisionSource.BUILTIN_RULE.value, 0.85)
            block.notes = block.notes or "section_heading"
            block.text_for_tts = ""
            if SECTION_HEADING_WARNING not in block.warnings:
                block.warnings.append(SECTION_HEADING_WARNING)
        elif verdict == "ambiguous":
            block.notes = block.notes or CAPS_HEADING_AMBIGUOUS_NOTE
            block.text_for_tts = ""
            if not block.suggested_type:
                block.suggested_type = BlockType.TECHNICAL.value
            if AMBIGUOUS_CAPS_WARNING not in block.warnings:
                block.warnings.append(AMBIGUOUS_CAPS_WARNING)


def _annotate_scene_context(blocks: list[NormalizedBlock]) -> None:
    """Attach dialogue replies and ``+`` splices to the preceding scene.

    The speaker is never filled in here: a reply proves scene membership only.
    ``scene_uid`` points at the block that opened the scene.
    """
    plan: list[SceneLink | None] = plan_scene_context(blocks)
    for block, link in zip(blocks, plan):
        if link is None:
            continue
        if link.scene_index >= 0:
            block.scene_uid = blocks[link.scene_index].uid
        if link.scene_type and link.kind != "continuation":
            block.continuation_of_type = link.scene_type
            if not block.suggested_type:
                block.suggested_type = link.scene_type
            _apply_type(block, link.scene_type, DecisionSource.BUILTIN_RULE.value, 0.8)
            block.speaker = ""
            block.role = ""
            opener = blocks[link.scene_index] if link.scene_index >= 0 else None
            # A ``+`` splice is the same hero's own recording: it keeps the
            # opening block's speaker.  Dash replies stay unattributed.
            if (
                link.kind == "splice"
                and opener is not None
                and opener.type == BlockType.SYNC.value
                and link.scene_type == BlockType.SYNC.value
                and opener.speaker.strip()
            ):
                block.speaker = opener.speaker
                block.role = opener.role
            _extract_block_metadata(block)
        elif link.suggested_type and not block.suggested_type:
            block.suggested_type = link.suggested_type
        if link.warning and link.warning not in block.warnings:
            block.warnings.append(link.warning)


def _annotate_continuation_context(
    blocks: list[NormalizedBlock], video_cue_re: re.Pattern[str] = _VIDEO_CUE_KEYWORD_RE
) -> None:
    previous: NormalizedBlock | None = None
    # Last sync block, kept alive only across bare splice markers.
    splice_donor: NormalizedBlock | None = None
    for block in blocks:
        if (
            previous is not None
            and block.type == BlockType.UNKNOWN.value
            and not block.original_label
            and previous.type == BlockType.SYNC.value
            and (
                _looks_like_sync_continuation(block.original_text)
                # A label-only carrier ("Синх Сторожевой") is immediately
                # followed by the actual quote, whose timecode is often
                # buried mid-sentence rather than leading the paragraph.
                # Restricted to that specific carrier form (not just any
                # sync block with a speaker) so an ordinary voiceover/cue
                # paragraph that merely mentions a timecode somewhere in
                # its text, after an unrelated sync, is not swept in too.
                or (
                    previous.speaker
                    and _SYNC_LABEL_ONLY_MARKER.match(previous.original_text.strip())
                    and _TIMECODE.search(block.original_text)
                )
            )
        ):
            block.continuation_of_type = BlockType.SYNC.value
            if not block.suggested_type:
                block.suggested_type = BlockType.SYNC.value
            _apply_type(
                block,
                BlockType.SYNC.value,
                DecisionSource.BUILTIN_RULE.value,
                0.9,
            )
            block.speaker = previous.speaker
            block.role = previous.role
        elif (
            previous is not None
            and block.type == BlockType.UNKNOWN.value
            and AMBIGUOUS_CAPS_WARNING not in block.warnings
            and TIMECODE_NOTE_WARNING not in block.warnings
            and not block.original_label
            and (
                previous.type == BlockType.VOICEOVER.value
                or previous.notes == "section_marker_for=voiceover"
            )
            # A footage/shot-list remark must not inherit voiceover just
            # because it follows one without its own marker (see
            # _looks_like_video_cue).
            and not _looks_like_video_cue(block.original_text, video_cue_re)
            # A leading "+ 00.43.25" splice is not narration (see
            # scenario_context.plan_scene_context).
            and not is_plus_splice(block.original_text)
        ):
            block.continuation_of_type = BlockType.VOICEOVER.value
            if not block.suggested_type:
                block.suggested_type = BlockType.VOICEOVER.value
            # A plain paragraph after an explicitly recognized voiceover marker
            # is part of the same voiceover section. Classify this locally so a
            # paragraph boundary does not make the result depend on the LLM.
            _apply_type(
                block,
                BlockType.VOICEOVER.value,
                DecisionSource.BUILTIN_RULE.value,
                0.9,
            )
        # A bare timecoded quote can inherit the interviewee only from the
        # immediately preceding sync. A cue or narration in between makes
        # that attribution uncertain and requires an explicit speaker.
        # The one exception is a bare "СКЛЕЙКА" splice marker: it only joins
        # two cuts of the same interview, so a timecoded quote after one or
        # more of them still belongs to the sync block preceding the markers.
        if block.type == BlockType.SYNC.value:
            donor = previous
            if (
                not block.speaker
                and previous is not None
                and previous.type != BlockType.SYNC.value
                and splice_donor is not None
                and _is_splice_marker(previous.original_text)
                and _starts_with_unambiguous_timecode(block.original_text)
            ):
                donor = splice_donor
            if (
                not block.speaker
                and SYNC_CREDIT_REJECTED_WARNING not in block.warnings
                and donor is not None
                and donor.type == BlockType.SYNC.value
                and donor.speaker
            ):
                block.continuation_of_type = BlockType.SYNC.value
                block.speaker = donor.speaker
                block.role = donor.role
        if block.type == BlockType.SYNC.value:
            splice_donor = block
        elif not _is_splice_marker(block.original_text):
            splice_donor = None
        previous = block


_SPLICE_MARKER_RE = re.compile(r"^[\s:.]*склейка[\s:.]*$", re.IGNORECASE)


def _is_splice_marker(text: str) -> bool:
    return bool(_SPLICE_MARKER_RE.match(text))


def _looks_like_sync_continuation(text: str) -> bool:
    return bool(re.match(r"^(?:\.{3}|…|[a-zа-яё]|И(?:\s|$))", text.lstrip()))


def _looks_like_video_cue(text: str, video_cue_re: re.Pattern[str] = _VIDEO_CUE_KEYWORD_RE) -> bool:
    """Detect an unmarked "video cue": a shot-list remark describing footage
    to show on screen (e.g. "Мраморный бюст Марии"), not spoken narration.

    Paired with :func:`_looks_like_sync_continuation`: a short nominal
    phrase naming what to show, containing no personal (finite) verb form.
    The verb/length check is the primary signal; the keyword lexicon only
    narrows it to paragraphs that plausibly describe footage at all.
    ``video_cue_re`` defaults to the built-in seed pattern but callers with
    a :class:`NormalizationMemory` should pass the pattern produced by
    :func:`_video_cue_keyword_pattern`, computed once per normalization run.
    """
    stripped = text.strip()
    if not stripped or not video_cue_re.search(stripped):
        return False
    if _PERSONAL_VERB_RE.search(stripped):
        return False
    if len(stripped.split()) > _VIDEO_CUE_MAX_WORDS:
        return False
    return True


def _looks_like_scenario_title(text: str) -> bool:
    """Detect an unmarked scenario/film title paragraph: fully upper-case
    text (dots, dashes, spaces and quotes allowed) with no personal (finite)
    verb form. Intended to be applied only to the first block of a scenario;
    callers are responsible for that position check.
    """
    stripped = text.strip()
    if not stripped or not _SCENARIO_TITLE_RE.match(stripped):
        return False
    if len(stripped) > _SCENARIO_TITLE_MAX_CHARS:
        return False
    if len(stripped.split()) > _SCENARIO_TITLE_MAX_WORDS:
        return False
    return not _PERSONAL_VERB_RE.search(stripped)


# --- Additional "technical cue" detectors ----------------------------------
# Documentary scripts are full of editorial/production notes that must never
# reach TTS or the interview search: links to source footage, bulleted
# song/film lists, dash-prefixed credits, and section headings ("Клипы",
# "Хроника", "Вставить синхрон ..."). Each rule below is anchored to a shape
# (a URL, a bullet glyph, a short nominal phrase) that spoken narration
# essentially never takes, so it is safe as an unconditional builtin rule.

# A URL is always a pointer to source footage for the editor, never text a
# narrator/TTS voice would read aloud.
_URL_RE = re.compile(r"https?://", re.IGNORECASE)

# A bulleted list item (song/film title, credit, etc). "•" is the glyph
# editors actually use in this corpus; the others are accepted for the same
# shape even though no test line uses them.
_BULLET_PREFIX_RE = re.compile(r"^[•‣◦*]")

# A year credited in parentheses, e.g. "Ночной дозор (2004)" or "Белым
# снегом (Новогодний Огонёк 2001)": the shape of a song/film title, not
# spoken narration.
_YEAR_IN_PARENS_RE = re.compile(r"\((?:[^()\n]*\b(?:19|20)\d{2}\b[^()\n]*)\)")

# A dash-prefixed line is ambiguous: it has the shape both of a short credit
# ("-Жанна Фриске") and of a spoken sentence the editor set off with a dash
# ("- Я тогда совершенно не знала..."). It is only safe to call technical
# when an extra signal removes that ambiguity: a URL, a year credit, or (the
# general case) a short nominal phrase with no personal/finite verb -- the
# same signal `_looks_like_video_cue` uses for unmarked shot-list remarks.
_DASH_PREFIX_RE = re.compile(r"^[-–—]\s*(?P<rest>.*)$", re.DOTALL)
_DASH_TECHNICAL_MAX_WORDS = 8

# A real credit/title never contains a comma or ends the way a spoken
# sentence does (a full stop, exclamation, question mark or ellipsis);
# `_PERSONAL_VERB_RE` alone misses first-person singular verbs on -у/-ю
# ("хочу", "знаю"), so these punctuation shapes are the second, structural
# barrier keeping live speech ("- Не знаю, что сказать.") out of technical.
_SENTENCE_END_PUNCT_RE = re.compile(r"[.!?…]$")


def _looks_like_technical_dash_line(text: str) -> bool:
    """Detect a dash-prefixed technical credit, as opposed to a spoken
    sentence the editor happened to set off with a leading dash.
    """
    match = _DASH_PREFIX_RE.match(text)
    if not match:
        return False
    rest = match.group("rest").strip()
    if not rest:
        return False
    if _URL_RE.search(rest) or _YEAR_IN_PARENS_RE.search(rest):
        return True
    if "," in rest or _SENTENCE_END_PUNCT_RE.search(rest):
        return False
    if _PERSONAL_VERB_RE.search(rest):
        return False
    return len(rest.split()) <= _DASH_TECHNICAL_MAX_WORDS


# Section headings ("Сольные клипы", "Художественные фильмы") and production
# notes ("Вставить синхрон Жанны", "ФОТЫ ЖАННЫ") that group or introduce
# footage/interview material rather than narrate it. As with
# `_looks_like_video_cue`, the keyword lexicon only narrows the check to
# plausible production notes; the length cap and the absence of a personal
# (finite) verb are the real safety net, keeping an ordinary sentence that
# happens to mention e.g. "архив" or "фото" from being misclassified.
_SECTION_HEADING_KEYWORDS = (
    "клипы",
    "клип",
    "фильмы",
    "хроника",
    "фоты",
    "фото",
    "фрагмент",
    "вставить",
    "поставить",
    "показать",
    "дать",
    "синхрон",
    "портфолио",
    "портофолио",
    "архив",
    "последний герой",
)
_SECTION_HEADING_KEYWORD_RE = re.compile(
    r"\b(?:"
    + "|".join(re.escape(word) for word in sorted(_SECTION_HEADING_KEYWORDS, key=len, reverse=True))
    + r")\b",
    re.IGNORECASE,
)
_SECTION_HEADING_MAX_WORDS = 8


_SECTION_HEADING_SHORT_SENTENCE_MAX_WORDS = 4


def _looks_like_section_heading(text: str) -> bool:
    stripped = text.strip()
    if not stripped or not _SECTION_HEADING_KEYWORD_RE.search(stripped):
        return False
    if "," in stripped:
        return False
    if _PERSONAL_VERB_RE.search(stripped):
        return False
    word_count = len(stripped.split())
    if word_count > _SECTION_HEADING_MAX_WORDS:
        return False
    # A comma-free line ending like a spoken sentence (".", "!", "?", "…")
    # is only safe to call a heading/production note when it is very short
    # ("Фрагмент «Ледникового периода».") -- the same structural barrier as
    # `_looks_like_technical_dash_line`, needed because `_PERSONAL_VERB_RE`
    # misses first-person singular -у/-ю verbs ("Хочу показать всем...").
    if (
        _SENTENCE_END_PUNCT_RE.search(stripped)
        and word_count > _SECTION_HEADING_SHORT_SENTENCE_MAX_WORDS
    ):
        return False
    return True


# A scenario/treatment sometimes opens with a literal "Сценарий. <title>"
# line instead of (or alongside) the all-caps title `_looks_like_scenario_title`
# already covers. Restricted to the first block, like that heuristic.
_SCENARIO_LABEL_RE = re.compile(r"^сценарий\b", re.IGNORECASE)


def _looks_like_scenario_heading(text: str) -> bool:
    stripped = text.strip()
    if not stripped or not _SCENARIO_LABEL_RE.match(stripped):
        return False
    if len(stripped) > _SCENARIO_TITLE_MAX_CHARS:
        return False
    if len(stripped.split()) > _SCENARIO_TITLE_MAX_WORDS:
        return False
    return not _PERSONAL_VERB_RE.search(stripped)


def _technical_heuristic_confidence(
    text: str, video_cue_re: re.Pattern[str] = _VIDEO_CUE_KEYWORD_RE
) -> float | None:
    """Return the confidence a plain (unlabelled) line is a technical cue --
    a URL, a bulleted list item, a dash-prefixed credit, a section heading/
    production note, or a footage/shot-list remark -- or None when none of
    these safe heuristics apply.

    Used by the LLM low-confidence/`unknown` fallback in
    `_apply_llm_results`: a block only reaches the LLM after
    `_classify_by_rules` already failed to classify it, so this is a second,
    unconditional pass over the same signals (`_classify_by_rules` gates
    some of them by label/position to avoid ever preempting an
    already-recognized sync/voiceover marker).
    """
    stripped = text.strip()
    if not stripped:
        return None
    if _URL_RE.search(stripped):
        return 1.0
    if _BULLET_PREFIX_RE.match(stripped):
        return 1.0
    if _looks_like_technical_dash_line(stripped):
        return 0.9
    if _looks_like_section_heading(stripped):
        return 0.9
    if _looks_like_video_cue(stripped, video_cue_re):
        return 1.0
    return None


_CLOCK_PREPOSITIONS = frozenset(
    {"в", "к", "до", "с", "около", "после", "перед", "ровно", "примерно", "часов", "где-то"}
)


def _is_clock_time_in_speech(text: str, start: int, end: int, inner: str) -> bool:
    """``служба в 6:30`` is a clock time, not a timecode.

    Only a two-component token right after a lower-case time preposition and
    without an explicit range counts as one.
    """
    if inner.count(":") != 1 or "." in inner or text[start : start + 1] == "[":
        return False
    words = text[:start].split()
    if not words or words[-1] not in _CLOCK_PREPOSITIONS:
        return False
    return not re.match(r"\s*[-–—]\s*\d", text[end:])


def _is_unambiguous_timecode_match(text: str, start: int, end: int) -> bool:
    """Decide whether a `_TIMECODE`-shaped match at ``text[start:end]`` is an
    actual timecode boundary, as opposed to prose (e.g. a two-component date
    like ``23.05``) that happens to match the same token pattern. Shared by
    bare-timecode detection and sync fragment splitting.
    """
    token = text[start:end]
    if start and text[start - 1] in "0123456789:.-":
        return False
    inner = token.strip("[]")
    if _is_clock_time_in_speech(text, start, end, inner):
        return False
    # After a ``+`` only a two-digit hour is a splice timecode: ``+ 5:30 утра``
    # is a clock time and ``+ 3.10.20`` a date (see scenario_structure).
    if (
        not token.startswith("[")
        and text[:start].rstrip().endswith("+")
        and re.match(r"\d[:.\-]", inner)
    ):
        return False
    if token.startswith("[") or ":" in inner or inner.count(".") >= 2 or "-" in inner:
        return True

    # A token wrapped in round brackets, e.g. "(23.05)", is unambiguous even
    # in the two-component dot notation: the brackets themselves mark it as a
    # structural timecode rather than prose.
    before, after = text[:start], text[end:]
    if before.rstrip().endswith("(") and after.lstrip().startswith(")"):
        return True

    # Two-component dot notation (for example 23.05) is ambiguous with a date.
    # Treat it as a timecode only when an explicit range removes that ambiguity.
    return bool(re.match(rf"^\s*[-–—]\s*{_TIMECODE_TOKEN}", after))


# Closed set of common pronouns/conjunctions that can start a sentence and
# therefore satisfy `_SYNC_NAME_ONLY_LABEL`/`_SYNC_LABEL_METADATA` purely by
# capitalization, without being an actual speaker name (e.g. "То, что
# получилось у нас, это живая история... 00:03:04 Мы делали...").
_SYNC_CANDIDATE_STOPWORDS = {
    "то",
    "это",
    "она",
    "он",
    "они",
    "мы",
    "вы",
    "я",
    "но",
    "и",
    "а",
    "да",
    "нет",
    "там",
    "тут",
    "так",
    "кто",
    "что",
    "его",
    "её",
    "их",
    "тот",
    "та",
    "те",
    "этот",
}


def _set_name_and_source(block: NormalizedBlock, label: str) -> None:
    """``Имя (фильм)`` -> speaker ``Имя`` + source_description ``фильм``; plain name as is."""
    split = _SYNC_NAME_SOURCE_SPLIT.match(label.strip())
    if split:
        block.speaker = split.group("name").strip()
        block.source_description = split.group("source").strip()
    else:
        block.speaker = label.strip()


def _looks_like_plausible_sync_credit(candidate: str) -> bool:
    """Reject a speaker-name/credit match whose first word is a bare
    sentence-leading pronoun or conjunction rather than a plausible name.
    Used only where the candidate was carved out of free-form prose (as
    opposed to an already-isolated label), so a real multi-word name such as
    "Мария Смольникова" is unaffected.
    """
    first_word = candidate.split(",", 1)[0].strip().split(" ")[:1]
    return not first_word or first_word[0].casefold() not in _SYNC_CANDIDATE_STOPWORDS


# ``СНХ. <кредит>: речь``: after a full stop the text before the colon is a
# credit only when it looks like a short name or role.  Finite-verb endings are
# the unambiguous ones (past tense, 3pl, 2nd person); the ambiguous ``-л/-ла/
# -ет/-ит/-ем/-им`` are skipped on purpose: they end real names (Павел, Мила,
# Максим) and the capital-letter rule already rejects lowercase verbs.
_DOT_CREDIT_VERB_RE = re.compile(
    r"^[а-яё]{3,}(?:лся|лась|лись|ли|ло|ете|ите|ешь|ишь|ют|ат|ят)$", re.IGNORECASE
)
_DOT_CREDIT_STOPWORDS = frozenset(
    {
        "хорошо",
        "плохо",
        "ладно",
        "конечно",
        "понятно",
        "ясно",
        "итак",
        "вот",
        "ну",
        "тогда",
        "потом",
        "теперь",
        "значит",
        "кстати",
        "например",
        "сегодня",
        "вчера",
        "завтра",
        "здесь",
        "потому",
        "когда",
        "если",
        "почему",
        "как",
        "это",
        "эти",
        "все",
        "всё",
    }
)
PUNCTUATION_ONLY_WARNING = "punctuation-only paragraph treated as technical: check manually"
EDITORIAL_CUE_CAPS_WARNING = "editorial cue by caps: check manually"
# Finite-verb endings only (no ``-ла/-ло``: they end nouns such as ``СЕРИАЛА``).
_CAPS_CUE_VERB_RE = re.compile(
    r"\b[а-яё]{3,}(?:ется|ится|ются|ятся|лся|лась|лись|ет|ит|ют|ат|ят|ут)\b", re.IGNORECASE
)
SYNC_CREDIT_REJECTED_WARNING = "sync credit after full stop not recognized: speaker not identified"


def _is_plausible_dot_sync_credit(speaker: str) -> bool:
    """1-3 capitalised words, no finite verb, no stop-word (``Отец``, ``Иванов Иван``)."""
    words = re.sub(r"\([^)]*\)", " ", speaker).split()
    if not 1 <= len(words) <= 3:
        return False
    for word in words:
        core = word.strip("«»\"“”'-–—")
        if not core or not core[0].isupper():
            return False
        if core.casefold() in _DOT_CREDIT_STOPWORDS or core.casefold() in _SYNC_CANDIDATE_STOPWORDS:
            return False
        if _DOT_CREDIT_VERB_RE.match(core):
            return False
    return True


def _is_rejected_dot_sync_credit(text: str) -> bool:
    """``СНХ. Мы пришли: да`` -- the marker is explicit, the credit is not."""
    match = _EXPLICIT_SYNC_CREDIT.match(_structural_text(text))
    if not match or match.group("sep") != ".":
        return False
    speaker = match.group("speaker").strip(" \t,;:")
    return not (
        not re.search(r"[.!?…,;]", speaker)
        and _looks_like_plausible_sync_credit(speaker)
        and _is_plausible_dot_sync_credit(speaker)
    )


def _timecode_note_kind(text: str) -> str:
    """``5:50 заметка`` / ``31-55 кт мозга`` / ``20:30-дискотека``.

    ``"note"``: a paragraph that opens with a short two-component timecode (not
    bracketed, not a ``T1 - T2`` range) followed by a link or a short remark
    (up to ``_TIMECODE_NOTE_MAX_WORDS`` words, no finite verb) is an editor's
    note about the source.  ``"ambiguous"``: the same opening, but the tail
    reads like speech (a finite verb or a long remark) -- ``12:30 Мы пришли
    домой`` / ``10-15 человек пришли``: the caller must keep it ``unknown`` so a
    person decides.  ``""``: not a short timecode note.  Full ``H:MM:SS``
    timecodes and ``(00:12:30)`` keep opening a sync.
    """
    match = _TIMECODE.match(text.lstrip())
    if not match:
        return ""
    token = match.group(1)
    if not re.fullmatch(r"\d{1,2}[:.\-][0-5]\d", token):
        return ""
    stripped = text.lstrip()
    if not _is_unambiguous_timecode_match(stripped, match.start(1), match.end(1)):
        return ""
    if _TIMECODE_RANGE.match(stripped):
        return ""
    tail = re.sub(r"^\s*[-–—]?\s*", "", stripped[match.end(1) :])
    if _URL_RE.search(tail):
        return "note"
    if tail.rstrip().endswith((".", "!", "?", "…", "»", '"', "”")):
        return ""
    if len(tail.split()) <= _TIMECODE_NOTE_MAX_WORDS and not _PERSONAL_VERB_RE.search(tail):
        return "note"
    return "ambiguous"


def _is_unlabelled_timecode_note(text: str) -> bool:
    return _timecode_note_kind(text) == "note"


def _starts_with_unambiguous_timecode(text: str) -> bool:
    """Recognize a bare timecode without mistaking a prose-leading date for one."""
    stripped = _structural_text(text).lstrip()
    match = re.match(rf"^\(\s*({_TIMECODE_TOKEN})\s*\)", stripped) or _TIMECODE.match(stripped)
    if not match:
        return False
    return _is_unambiguous_timecode_match(stripped, match.start(1), match.end(1))


_CREDIT_SOURCE_MARK = re.compile(r"(?:^|\s)ИСХ(?:ОДНИК)?\.?\s+(?P<name>\S.*)$")
_CREDIT_NAME_WITH_BRACKET = re.compile(
    r"[А-ЯЁA-Z][А-ЯЁа-яёA-Za-z\-]+(?:\s+[А-ЯЁA-Z][А-ЯЁа-яёA-Za-z\-]+)*"
    r"(?:\s*\([А-ЯЁа-яёA-Za-z\-\s]{2,40}\))?"
)


def _is_credit_name(speaker: str) -> bool:
    """A name, optionally with a surname in brackets: ``ИГУМЕН НИКИТА (ВИХЛЯЕВ)``."""
    return bool(_CREDIT_NAME_WITH_BRACKET.fullmatch(speaker))


_CREDIT_FILE_TOKEN = re.compile(r"(?:\d{1,3}|[^\s_]*_\S*|(?=\S*\d)(?=\S*[A-Za-z])\S+)")
_CREDIT_FILE_TAIL_WORD = re.compile(r"[A-Z][A-Za-z0-9_\-.]*")


def _split_credit_media_tail(speaker: str) -> tuple[str, str]:
    """Split ``ОТЕЦ АВРААМ 01`` / ``ИМЯ 03_09_26_ARHITECTOR DEMIDOV`` into name and file tail.

    The tail starts at the first take number (``01``) or file-like token (``_``
    or Latin letters with digits) and may continue with Latin capital words.  A
    speaker without such a tail (or consisting only of one) is returned as is.
    """
    words = speaker.split()
    for position in range(1, len(words)):
        if not _CREDIT_FILE_TOKEN.fullmatch(words[position]):
            continue
        tail = words[position + 1 :]
        if all(
            _CREDIT_FILE_TOKEN.fullmatch(w) or _CREDIT_FILE_TAIL_WORD.fullmatch(w) for w in tail
        ):
            return " ".join(words[:position]), " ".join(words[position:])
        break
    return speaker, ""


_SNH_SPOKEN_CREDIT = re.compile(r"^\s*(?P<marker>СНХ|СИНХРОН|SYNC)\s+(?P<rest>\S.*)$", re.DOTALL)
_SPOKEN_CREDIT_MAX_WORDS = 4
_SPOKEN_CONTENT_MIN_CHARS = 20


def _parse_snh_spoken_credit(text: str) -> dict[str, str] | None:
    """Parse ``СНХ ИМЯ ФАМИЛИЯ речь…`` with no timecode and no dash.

    The credit is the leading run of all-caps words; speech starts at the first
    word that is not all-caps, or at a one-letter capital preposition followed
    by a lowercase word (``СНХ АЛЕНА САДОВНИЦА К некоторым вещам``).
    """
    match = _SNH_SPOKEN_CREDIT.match(_structural_text(text))
    if not match:
        return None
    rest = match.group("rest")
    if _TIMECODE.search(rest[:80]):
        return None
    words = rest.split()
    count = 0
    while count < len(words):
        word = words[count]
        bare = word.strip(",;:")
        if len(bare) < 2 or not re.fullmatch(r"[А-ЯЁ][А-ЯЁ\-]+", bare):
            break
        count += 1
    if not 1 <= count <= _SPOKEN_CREDIT_MAX_WORDS or count == len(words):
        return None
    speaker = " ".join(w.strip(",;:") for w in words[:count])
    content = " ".join(words[count:])
    first = words[count]
    follower = words[count + 1] if count + 1 < len(words) else ""
    speech_start = (
        first[0].islower()
        or (len(first) == 1 and first.isupper() and follower[:1].islower())
        or (first[0].isupper() and first[1:2].islower())
    )
    if not speech_start or len(content) < _SPOKEN_CONTENT_MIN_CHARS:
        return None
    if not _is_credit_name(speaker) or not _looks_like_plausible_sync_credit(speaker):
        return None
    return {
        "marker": match.group("marker"),
        "speaker": speaker,
        "role": "",
        "source": "",
        "content": content,
        "header_only": "",
        "dashed": "",
    }


def _parse_snh_dash_credit(text: str) -> dict[str, str] | None:
    """Credit of a ``СНХ`` paragraph: timed form first, then spoken (no timecode)."""
    return _parse_snh_timed_credit(text) or _parse_snh_spoken_credit(text)


def _parse_snh_timed_credit(text: str) -> dict[str, str] | None:
    """Parse ``СНХ Имя[, роль] — [файл] 00:05:42 — реплика``.

    Returns marker/speaker/role/source/content (``content`` starts at the first
    timecode and is empty for a header-only paragraph), or ``None`` when the
    paragraph is not this form.
    """
    match = _SNH_DASH_CREDIT.match(_structural_text(text))
    if not match:
        return None
    credit = match.group("credit").strip()
    if _TIMECODE.search(credit):
        return None
    # ``ИСХ <имя файла>`` names the source recording, never the person or role.
    credit_source = ""
    source_mark = _CREDIT_SOURCE_MARK.search(credit)
    if source_mark:
        credit_source = source_mark.group("name").strip()
        credit = credit[: source_mark.start()].strip()
    speaker, _, role = credit.partition(",")
    speaker, role = speaker.strip(), role.strip(" \t,;:")
    speaker, name_tail = _split_credit_media_tail(speaker)
    credit_source = credit_source or name_tail
    if "(" in speaker and ")" not in speaker:
        speaker += ")"  # ``ИГУМЕН НИКИТА (ВИХЛЯЕВ``: the author left the bracket open
    if not _is_credit_name(speaker) or not _looks_like_plausible_sync_credit(speaker):
        return None
    rest = match.group("rest").strip()
    timecode_at = -1
    for found in _TIMECODE.finditer(rest):
        if _is_unambiguous_timecode_match(rest, found.start(), found.end()):
            timecode_at = found.start()
            break
    if not match.group("dash") and timecode_at != 0:
        return None
    if timecode_at < 0:
        source, content = rest, ""
    else:
        source, content = rest[:timecode_at], rest[timecode_at:].strip()
    source = source.strip(" \t–—-")
    if source and not _MEDIA_FILE_NAME.match(source):
        return None
    source = source or credit_source
    if not source and timecode_at < 0:
        return None
    return {
        "marker": match.group("marker"),
        "speaker": speaker,
        "role": role,
        "source": source,
        "content": content,
        "header_only": "" if content else "1",
        "dashed": "1" if match.group("dash") else "",
    }


_UNLABELED_CREDIT = re.compile(
    r"^\s*(?P<credit>(?:(?!ИСХ(?:ОДНИК)?\.?\s)[А-ЯЁ][А-ЯЁ\-]+\s+){0,4}"
    r"(?!ИСХ(?:ОДНИК)?\.?\s)[А-ЯЁ][А-ЯЁ\-]+(?:\s*\([А-ЯЁ\-\s]{2,40}\))?)"
    r"\s+(?P<tail>\S.*)$",
    re.DOTALL,
)


def _parse_unlabeled_sync_credit(text: str) -> dict[str, str] | None:
    """Parse a sync paragraph whose all-caps credit has no ``СНХ`` label.

    ``ИГУМЕН ИМЯ (ФАМИЛИЯ) ИСХ АВТОР ИМЯ 00.35:30 речь`` -> the whole credit before
    ``ИСХ`` is the speaker, the words up to the timecode are the source (parsed by
    the ``СНХ`` credit logic).  Without ``ИСХ`` only a credit with a bracketed
    surname directly followed by a timecode qualifies; a plain all-caps name before
    a timecode is already handled by the generic label logic.
    """
    view = _structural_text(text)
    match = _UNLABELED_CREDIT.match(view)
    if not match:
        return None
    structure = _paragraph_structure(view)
    if structure.has_label or structure.is_technical_cue:
        return None
    credit, tail = match.group("credit"), match.group("tail")
    if normalize_marker(credit) in set(_BUILTIN_MARKERS) | _TECH_MARKERS:
        return None
    if _CREDIT_SOURCE_MARK.match(f" {tail}"):
        parsed = _parse_snh_timed_credit(f"СНХ {view}")
        if (
            not parsed
            or parsed["header_only"]
            or not parsed["source"]
            or not parsed["content"]
            or parsed["speaker"] != credit
            or any(word[:1].islower() for word in parsed["source"].split())
        ):
            return None
        return {**parsed, "marker": ""}
    if "(" not in credit or not _starts_with_unambiguous_timecode(tail):
        return None
    if not _is_credit_name(credit) or not _looks_like_plausible_sync_credit(credit):
        return None
    content = tail.strip()
    if not _TIMECODE.sub("", content, count=1).strip(" \t[]()–—-.:"):
        return None
    return {
        "marker": "",
        "speaker": credit,
        "role": "",
        "source": "",
        "content": content,
        "header_only": "",
        "dashed": "",
    }


def _merge_snh_credit_headers(paragraphs: list[str]) -> list[str]:
    """Join a header-only ``СНХ Имя — файл`` paragraph with the next timecoded one."""
    merged: list[str] = []
    index = 0
    while index < len(paragraphs):
        current = paragraphs[index]
        credit = _parse_snh_dash_credit(current)
        if (
            credit
            and credit["header_only"]
            and index + 1 < len(paragraphs)
            and _starts_with_unambiguous_timecode(paragraphs[index + 1])
        ):
            merged.append(f"{current.rstrip()} {paragraphs[index + 1].lstrip()}")
            index += 2
            continue
        merged.append(current)
        index += 1
    return merged


def _split_label(text: str) -> tuple[str, str]:
    # Timecodes are located first: a colon inside ``(00:42:32)`` must never be
    # taken for a label/description separator.
    text = _structural_text(text)
    # Generic labels are deliberately limited: prose containing an ordinary colon
    # should not accidentally become a learned marker.
    if _TIMECODE.match(text.strip()):
        return "", text.strip()
    explicit_sync = _EXPLICIT_SYNC_CREDIT.match(text)
    if explicit_sync:
        marker = explicit_sync.group("marker").strip()
        speaker = explicit_sync.group("speaker").strip(" \t,;:")
        if explicit_sync.group("sep") == "," or (
            not re.search(r"[.!?…,;]", speaker)
            and _looks_like_plausible_sync_credit(speaker)
            and _is_plausible_dot_sync_credit(speaker)
        ):
            return f"{marker}, {speaker}", explicit_sync.group("content").strip()
    dash_credit = _parse_snh_dash_credit(text)
    if dash_credit:
        return dash_credit["marker"], dash_credit["content"]
    unlabeled_credit = _parse_unlabeled_sync_credit(text)
    if unlabeled_credit:
        return unlabeled_credit["speaker"], unlabeled_credit["content"]
    # Service labels (ЗК/З.К./СНХ/ЛАЙФ/ЛАЙФЫ/СТЕНДАП/technical markers) and
    # montage cues are recognised by the shared structural parser BEFORE any
    # "capitalised words are a hero" heuristic below. The author's block
    # number is dropped only when it is unambiguous (see scenario_structure).
    structure = _paragraph_structure(text)
    if structure.is_technical_cue:
        return "", text.strip()
    if structure.has_label:
        body = structure.speech
        if structure.label == "ЛАЙФ":
            timed = _life_timed_body(body)
            if timed is not None:
                _description, parts = timed
                return structure.label_raw, " ".join(part.text for part in parts if part.text)
            described = _LIFE_DESCRIBED.match(body)
            if described:
                body = described.group(2).strip()
        return structure.label_raw, body
    # A formal speaker credit can be much longer than an ordinary marker. It
    # is safe to accept only when its content starts with a structural timecode
    # (optionally after a "Продолжение синхрона" note).
    speaker = re.match(r"^\s*([^:\n]{2,240}?)\s*:\s*(.+)$", text, flags=re.DOTALL)
    if speaker:
        candidate = speaker.group(1).strip()
        content = speaker.group(2).strip()
        sync_content = _SYNC_CONTINUATION_NOTE.sub("", content)
        if (
            (_SYNC_LABEL_METADATA.match(candidate) or _SYNC_NAME_ONLY_LABEL.match(candidate))
            and _looks_like_plausible_sync_credit(candidate)
            and _TIMECODE.match(sync_content)
        ):
            return candidate, content
    clear_speaker = _CLEAR_SPEAKER_CREDIT.match(text)
    if (
        clear_speaker
        and not re.match(r"^(?:голос|зк|зтм|титр)\b", clear_speaker.group("speaker"), re.IGNORECASE)
        and _looks_like_plausible_sync_credit(clear_speaker.group("speaker"))
    ):
        content = clear_speaker.group("content").strip()
        credit = clear_speaker.group("speaker").strip()
        if len(content) >= 35 and (
            "," in text[: clear_speaker.start("content")]
            or _TIMECODE.search(content)
            or clear_speaker.group("separator").strip().endswith(("-", "–", "—"))
        ):
            return credit, content
    # Some scripts put a timecode directly after the speaker credit, without a
    # colon: "Бартон 00:37 — 01:07 ...". Validate the prefix as a name or a
    # name-and-role credit before treating it as a label.
    timecoded_speaker = re.match(
        rf"^\s*(.{{2,240}}?)[.\s]*({_TIMECODE_TOKEN}.*)$", text, flags=re.DOTALL
    )
    if timecoded_speaker:
        candidate = timecoded_speaker.group(1).strip()
        if (
            _SYNC_LABEL_METADATA.match(candidate) or _SYNC_NAME_ONLY_LABEL.match(candidate)
        ) and _looks_like_plausible_sync_credit(candidate):
            return candidate, timecoded_speaker.group(2).strip()
    generic = re.match(r"^\s*([^:\n]{1,60}?)\s*:\s*(.+)$", text, flags=re.DOTALL)
    if generic:
        candidate = generic.group(1).strip()
        # A candidate without any letter ("- 00" from "- (00:34:21) ...") is the
        # head of a timecode, never a label.
        if (
            not candidate.startswith("(")
            and len(candidate.split()) <= 3
            and re.search(r"[^\W\d_]", candidate)
        ):
            return candidate, generic.group(2).strip()
    bare_speaker = _BARE_SPEAKER_PREFIX.match(text)
    if bare_speaker:
        content = bare_speaker.group("content").strip()
        # Avoid turning short all-caps production headings into interviewees.
        if len(content) >= 40 and re.search(r"[а-яё]", content):
            return bare_speaker.group("speaker"), content
    normalized_text = normalize_marker(text)
    if normalized_text in set(_BUILTIN_MARKERS) | _TECH_MARKERS:
        return text.strip(), ""
    return "", text.strip()


_LIFE_SCENE_HEAD_MAX = 160
# ``Description: quote``; a colon followed by a digit belongs to a timecode.
_LIFE_DESCRIBED = re.compile(r"^([^:\n«»]{2,100}):(?!\d)\s*(.+)$", re.DOTALL)


def _life_timed_body(body: str) -> tuple[str, tuple[Any, ...]] | None:
    """Scene description and timed parts of a LIFE body, or ``None``.

    The description is the text before the first timecode; it must look like a
    scene title (short, no sentence punctuation), otherwise the old
    description/quote handling stays in charge.
    """
    timed = split_timed_parts(body)
    if not timed.parts:
        return None
    head = timed.head
    # A closing ``.``/``!`` belongs to the title (``Драка.``); sentence
    # punctuation inside it means the head is speech, not a scene title.
    head = re.sub(r"[\s.!?…]+$", "", head) if re.search(r"\w", head) else head
    if len(head) > _LIFE_SCENE_HEAD_MAX or re.search(r"[.!?…]", head):
        return None
    quoted = re.fullmatch(r"«([^»]*)»", head)
    return (quoted.group(1).strip() if quoted else head), timed.parts


def _classify_by_rules(
    block: NormalizedBlock,
    memory: NormalizationMemory,
    video_cue_re: re.Pattern[str] = _VIDEO_CUE_KEYWORD_RE,
    in_scene: bool = False,
) -> None:
    marker = normalize_marker(block.original_label)
    stripped = _structural_text(block.original_text).strip()
    label_only_sync = _SYNC_LABEL_ONLY_MARKER.match(stripped)
    if label_only_sync:
        # A label-only paragraph ("Синх Сторожевой") carries no quote of its
        # own: it only announces the interviewee for the paragraph(s) that
        # follow. Treat it as a (contentless) sync block so the speaker is
        # available for continuation inheritance in
        # ``_annotate_continuation_context``.
        _apply_type(block, BlockType.SYNC.value, DecisionSource.BUILTIN_RULE.value, 1.0)
        block.speaker = label_only_sync.group(1).strip()
        block.text_for_tts = ""
        # Explicit "context without a clip": downstream stages must not turn
        # the header into a quote of its own.
        block.label_only = True
        return
    inline_sync = None if block.original_label else split_inline_sync_label(stripped)
    if inline_sync:
        # ``Синх СторожевойТо, что...``: the label is glued to the quote (or
        # followed by a splice marker). The unambiguous border makes it a sync
        # quote of that speaker; it must never reach TTS as narration.
        _apply_type(block, BlockType.SYNC.value, DecisionSource.BUILTIN_RULE.value, 0.9)
        block.speaker = inline_sync[0]
        block.text_for_tts = ""
        return
    if (
        not block.original_label
        and stripped
        and not re.search(r"\w", stripped)
        and not re.match(r"^\(\s*(?:…|\.\.\.)\s*\)", stripped)
    ):
        # ``.`` / ``…`` / ``—`` carries no speech: it must neither continue a
        # voiceover nor reach TTS, but a person should see it.
        _apply_type(block, BlockType.TECHNICAL.value, DecisionSource.BUILTIN_RULE.value, 0.9)
        block.warnings.append(PUNCTUATION_ONLY_WARNING)
        return
    structure = _paragraph_structure(block.original_text)
    if structure.has_label_conflict:
        # ``ЗК СНХ ...``: two competing service labels. Which one the author
        # meant is not decidable from the text; leave the block unknown so a
        # human decides and nothing from the credit reaches TTS.
        return
    if structure.is_technical_cue and not marker:
        _apply_type(block, BlockType.TECHNICAL.value, DecisionSource.BUILTIN_RULE.value, 0.95)
        return
    if _PRESENTER_HEADING_ONLY.match(stripped):
        _apply_type(block, BlockType.TECHNICAL.value, DecisionSource.BUILTIN_RULE.value, 0.95)
        return
    if marker:
        exact = memory.resolve_exact(marker)
        if exact and exact.get("conflict"):
            block.warnings.append(f"Conflicting learned rules for '{block.original_label}'")
            return
        if exact:
            rule = exact["rule"]
            if rule["source"] == "manual":
                _apply_type(block, rule["target_type"], DecisionSource.MEMORY.value, 1.0)
                block.memory_rule_id = int(rule["id"])
                return
        if marker not in _BUILTIN_MARKERS and _PLAYBACK_LIFE_CUE.search(stripped):
            _apply_type(block, BlockType.LIFE.value, DecisionSource.BUILTIN_RULE.value, 1.0)
            return
        if marker in _BUILTIN_MARKERS:
            if _BUILTIN_MARKERS[marker] == BlockType.VOICEOVER.value:
                _label, marked_content = _split_label(block.original_text)
                if _EDITORIAL_VOICEOVER_CUE.match(marked_content) or (
                    marked_content.endswith(":")
                    and re.search(r"\bголос\b", marked_content, re.IGNORECASE)
                ):
                    _apply_type(
                        block, BlockType.TECHNICAL.value, DecisionSource.BUILTIN_RULE.value, 1.0
                    )
                    return
            _apply_type(block, _BUILTIN_MARKERS[marker], DecisionSource.BUILTIN_RULE.value, 1.0)
            return
        if marker in _TECH_MARKERS:
            _apply_type(block, BlockType.TECHNICAL.value, DecisionSource.BUILTIN_RULE.value, 1.0)
            return
        if exact:
            rule = exact["rule"]
            _apply_type(
                block, rule["target_type"], DecisionSource.MEMORY.value, float(rule["confidence"])
            )
            block.memory_rule_id = int(rule["id"])
            return
        suggestion = memory.suggest(marker)
        if suggestion:
            block.suggested_type = str(suggestion["rule"]["target_type"])
            block.suggested_label = str(suggestion["rule"]["marker"])
            block.warnings.append(
                f"Possible learned alias '{block.suggested_label}' ({suggestion['score']:.0%})"
            )
    if not marker and _PLAYBACK_LIFE_CUE.search(stripped):
        _apply_type(block, BlockType.LIFE.value, DecisionSource.BUILTIN_RULE.value, 1.0)
        return
    if _EDITORIAL_VOICEOVER_CUE.match(stripped) or (
        stripped.endswith(":") and not _TIMECODE.search(stripped)
    ):
        _apply_type(block, BlockType.TECHNICAL.value, DecisionSource.BUILTIN_RULE.value, 0.95)
        if not marker and _EDITORIAL_VOICEOVER_CUE.match(stripped) and stripped.isupper():
            # A cue recognised only by capitals can swallow shouted narration.
            if SECTION_HEADING_WARNING not in block.warnings:
                block.warnings.append(SECTION_HEADING_WARNING)
            if stripped.endswith(".") or _CAPS_CUE_VERB_RE.search(stripped):
                block.warnings.append(EDITORIAL_CUE_CAPS_WARNING)
        return
    _label, content = _split_label(block.original_text)
    sync_content = _SYNC_CONTINUATION_NOTE.sub("", content)
    if _EXPLICIT_SYNC_LABEL.match(block.original_label):
        _apply_type(block, BlockType.SYNC.value, DecisionSource.BUILTIN_RULE.value, 1.0)
    elif (
        _SYNC_LABEL_METADATA.match(block.original_label)
        or _SYNC_NAME_ONLY_LABEL.match(block.original_label)
    ) and _TIMECODE.match(sync_content):
        _apply_type(block, BlockType.SYNC.value, DecisionSource.BUILTIN_RULE.value, 1.0)
    elif (
        _NAMED_VOICE_LABEL.match(block.original_label) and content and not _TIMECODE.match(content)
    ):
        # ``Голос «X»: текст`` is narration read in a character's voice; an
        # empty text is only a cue (handled above as technical).
        _apply_type(block, BlockType.VOICEOVER.value, DecisionSource.BUILTIN_RULE.value, 0.9)
    elif _parse_unlabeled_sync_credit(block.original_text):
        _apply_type(block, BlockType.SYNC.value, DecisionSource.BUILTIN_RULE.value, 0.8)
    elif (
        not block.original_label
        and (note_kind := _timecode_note_kind(stripped))
        and not (in_scene and not _URL_RE.search(stripped))
    ):
        if note_kind == "note":
            _apply_type(block, BlockType.TECHNICAL.value, DecisionSource.BUILTIN_RULE.value, 0.7)
        if TIMECODE_NOTE_WARNING not in block.warnings:
            block.warnings.append(TIMECODE_NOTE_WARNING)
    elif _starts_with_unambiguous_timecode(stripped) or re.match(r"^\s*\((?:…|\.\.\.)\)", stripped):
        _apply_type(block, BlockType.SYNC.value, DecisionSource.BUILTIN_RULE.value, 0.9)
    elif _EDITORIAL_TIMECODE_PREFIX.match(stripped):
        _apply_type(block, BlockType.SYNC.value, DecisionSource.BUILTIN_RULE.value, 0.9)
    elif _STRUCTURAL_HEADING.match(stripped):
        _apply_type(block, BlockType.TECHNICAL.value, DecisionSource.BUILTIN_RULE.value, 1.0)
    elif _URL_RE.search(stripped):
        # A link to source footage is always a technical cue, never text
        # meant to be spoken.
        _apply_type(block, BlockType.TECHNICAL.value, DecisionSource.BUILTIN_RULE.value, 1.0)
    elif _BULLET_PREFIX_RE.match(stripped):
        # A bulleted list item (song/film title, credit, etc) is always a
        # technical cue, regardless of any label.
        _apply_type(block, BlockType.TECHNICAL.value, DecisionSource.BUILTIN_RULE.value, 1.0)
    elif _looks_like_technical_dash_line(stripped):
        _apply_type(block, BlockType.TECHNICAL.value, DecisionSource.BUILTIN_RULE.value, 0.9)
    elif _looks_like_section_heading(stripped):
        _apply_type(block, BlockType.TECHNICAL.value, DecisionSource.BUILTIN_RULE.value, 0.9)
    elif (
        (_BARE_SPEAKER_PREFIX.match(stripped) or _CLEAR_SPEAKER_CREDIT.match(stripped))
        and block.original_label
        and len(content) >= 35
        and marker not in _BUILTIN_MARKERS
    ):
        _apply_type(block, BlockType.SYNC.value, DecisionSource.BUILTIN_RULE.value, 0.8)
    elif (
        re.match(r"^(?:планы?|портреты)\b", stripped, flags=re.IGNORECASE)
        or not block.original_label
    ) and _looks_like_video_cue(stripped, video_cue_re):
        _apply_type(block, BlockType.TECHNICAL.value, DecisionSource.BUILTIN_RULE.value, 1.0)
    elif (
        block.order == 1
        and not block.original_label
        and (_looks_like_scenario_title(stripped) or _looks_like_scenario_heading(stripped))
    ):
        _apply_type(block, BlockType.TECHNICAL.value, DecisionSource.BUILTIN_RULE.value, 0.9)
    elif stripped.startswith("(") and stripped.endswith(")"):
        _apply_type(block, BlockType.TECHNICAL.value, DecisionSource.BUILTIN_RULE.value, 0.8)


def _apply_type(block: NormalizedBlock, target_type: str, source: str, confidence: float) -> None:
    block.type = target_type if target_type in VALID_BLOCK_TYPES else BlockType.UNKNOWN.value
    block.source = source
    block.confidence = max(0.0, min(1.0, confidence))
    if block.type == BlockType.VOICEOVER.value and not block.text_for_tts:
        block.text_for_tts = block.normalized_text
    elif block.type == BlockType.LIFE.value:
        block.text_for_tts = ""
    if block.type != BlockType.LIFE.value:
        _assign_first_timecodes(block)


def _assign_first_timecodes(block: NormalizedBlock) -> None:
    """Take the first (and, for non-sync, second) timecode of the paragraph."""
    view = _structural_text(block.original_text)
    timecodes = _TIMECODE.findall(view)
    if not timecodes:
        return
    block.timecode_start = _canonicalize_timecode(timecodes[0])
    if len(timecodes) > 1 and block.type != BlockType.SYNC.value:
        # LIFE parts joined by ``+`` are separate inserts: only an explicit
        # ``T1 - T2`` range gives a LIFE block an end.
        if block.type != BlockType.LIFE.value or _TIMECODE_RANGE.search(view):
            block.timecode_end = _canonicalize_timecode(timecodes[1])


def _extract_block_metadata(block: NormalizedBlock) -> None:
    if block.type == BlockType.LIFE.value:
        life = _LIFE_PREFIX.match(_structural_text(block.original_text))
        block.speaker = ""
        block.role = ""
        block.text_for_tts = ""
        body = life.group("body").strip() if life else ""
        if not life and block.continuation_of_type == BlockType.LIFE.value:
            body = _structural_text(block.original_text).lstrip(" \t-‐‑‒–—―+")
        timed = _life_timed_body(body) if body else None
        if timed is not None:
            description, parts = timed
            block.source_description = description
            block.sync_fragments = []
            kept = [part for part in parts if part.text]
            if not life and kept:
                block.normalized_text = " ".join(part.text for part in kept)
            if len(parts) > 1:
                # A timecode with no text is never dropped silently.
                for part in parts:
                    if not part.text and part.start:
                        block.warnings.append(
                            f"Timecode {part.start.stored} in a '+' splice has no text: "
                            "check manually"
                        )
            first = kept[0] if kept else parts[0]
            block.timecode_start = first.start.stored if first.start else ""
            block.timecode_end = first.end.stored if first.end else ""
            if len(kept) > 1:
                # A ``+`` splice is two independent inserts (each with its own
                # entry point and no exit), serialised like sync fragments.
                block.sync_fragments = [
                    SyncFragment(
                        text=part.text,
                        timecode_start=part.start.stored if part.start else "",
                        timecode_end=part.end.stored if part.end else "",
                    )
                    for part in kept
                ]
            return
        _assign_first_timecodes(block)
        if life:
            body = life.group("body").strip()
            quoted = re.match(r"^«([^»]*)»", body)
            described = _LIFE_DESCRIBED.match(body)
            if quoted:
                block.source_description = quoted.group(1).strip()
            elif described:
                block.source_description = described.group(1).strip(" \t,;:")
        return
    if block.type == BlockType.HOST.value and not block.speaker:
        block.speaker = block.original_label or "ВЕДУЩИЙ"
    if block.type != BlockType.SYNC.value:
        return
    normalized_view = _structural_text(block.normalized_text)
    fragment_source = _SYNC_CONTINUATION_NOTE.sub("", normalized_view)
    has_sync_metadata = False
    explicit_label = _EXPLICIT_SYNC_LABEL.match(block.original_label)
    inline_sync = None if block.original_label else split_inline_sync_label(normalized_view)
    match = _SYNC_METADATA.match(normalized_view)
    dash_credit = _parse_snh_dash_credit(block.original_text)
    unlabeled_credit = _parse_unlabeled_sync_credit(block.original_text)
    unnamed_label = _paragraph_structure(block.original_text).metadata.get("service_label")
    if unnamed_label:
        presenter_parts = parse_presenter_parts(block.original_text)
        if presenter_parts and len({part.speaker for part in presenter_parts}) == 1:
            # Attribution is explicit in the caption. Keep every splice, even
            # without clocks; different presenters still need a manual split.
            block.speaker = presenter_parts[0].speaker
            block.source_description = " / ".join(
                dict.fromkeys(part.description for part in presenter_parts if part.description)
            )
            fragments = []
            for part in presenter_parts:
                fragments.extend(
                    _extract_sync_fragments(_structural_text(part.text))
                    or [SyncFragment(text=part.text, timecode_start="")]
                )
            block.sync_fragments = fragments
            block.normalized_text = " ".join(fragment.text for fragment in fragments)
            block.timecode_start = fragments[0].timecode_start
            block.timecode_end = fragments[0].timecode_end
            block.warnings = [
                warning
                for warning in block.warnings
                if warning not in {STANDUP_WARNING, LEAD_IN_WARNING}
            ]
            return
        # ``СТЕНДАП`` / ``ПОДВОДКА``: a presenter is announced, not named. The
        # speaker stays empty (the structure parser already warned); timecoded
        # parts are still extracted below.
        fragment_source = normalized_view
        has_sync_metadata = True
    elif inline_sync and block.speaker == inline_sync[0]:
        fragment_source = inline_sync[1]
        has_sync_metadata = True
        if not _extract_sync_fragments(fragment_source):
            # No timecode and no splice: the whole rest is one searchable quote.
            block.sync_fragments = [SyncFragment(text=fragment_source, timecode_start="")]
            block.normalized_text = fragment_source
            return
    elif dash_credit and normalize_marker(block.original_label) == normalize_marker(
        dash_credit["marker"]
    ):
        block.speaker = dash_credit["speaker"]
        block.role = dash_credit["role"]
        if dash_credit["source"]:
            block.source_description = dash_credit["source"]
        elif dash_credit["content"] and (
            dash_credit["dashed"] or re.match(rf"{_TIMECODE_TOKEN}\s*[—–]", dash_credit["content"])
        ):
            # Only the dash forms (``СНХ Имя — … 00:05:42``, ``СНХ ИМЯ 00:12:06 — …``)
            # imply a media file; the author format ``СНХ ИМЯ ТАЙМКОД текст`` has
            # none (Stage 2 binds media).
            block.warnings.append("Sync credit has no source media file")
        fragment_source = dash_credit["content"]
        has_sync_metadata = True
    elif unlabeled_credit and normalize_marker(block.original_label) == normalize_marker(
        unlabeled_credit["speaker"]
    ):
        block.speaker = unlabeled_credit["speaker"]
        block.source_description = unlabeled_credit["source"]
        block.warnings.append("sync credit without СНХ label: check manually")
        fragment_source = unlabeled_credit["content"]
        has_sync_metadata = True
    elif explicit_label:
        block.speaker = explicit_label.group(1).strip(" \t,;:")
        block.role = ""
        has_sync_metadata = True
    elif block.original_label and normalize_marker(block.original_label) not in _BUILTIN_MARKERS:
        credited = _SYNC_LABEL_METADATA.match(block.original_label)
        if credited:
            block.speaker = credited.group(1).strip()
            block.role = credited.group(2).strip()
        elif _SYNC_NAME_ONLY_LABEL.match(block.original_label):
            _set_name_and_source(block, block.original_label)
        if block.speaker:
            has_sync_metadata = True
        elif match:
            block.speaker = match.group(1).strip()
            block.role = match.group(2).strip()
            fragment_source = normalized_view[match.start(3) :].strip()
            has_sync_metadata = True
    elif match:
        block.speaker = match.group(1).strip()
        block.role = match.group(2).strip()
        fragment_source = normalized_view[match.start(3) :].strip()
        has_sync_metadata = True
    else:
        label_match = _SYNC_LABEL_METADATA.match(block.original_label)
        name_only_match = _SYNC_NAME_ONLY_LABEL.match(block.original_label)
        if label_match and _TIMECODE.match(fragment_source):
            block.speaker = label_match.group(1).strip()
            block.role = label_match.group(2).strip()
            has_sync_metadata = True
        elif name_only_match and _TIMECODE.match(fragment_source):
            _set_name_and_source(block, block.original_label)
            has_sync_metadata = True
        elif block.continuation_of_type == BlockType.SYNC.value and block.speaker:
            # The preceding labelled sync supplied the interviewee. Keep a
            # timecoded paragraph as its own searchable source range --
            # whether the timecode leads the paragraph (the older supported
            # form) or sits mid-sentence (e.g. after a label-only "Синх
            # <Фамилия>" carrier). ``_extract_sync_fragments`` below is a
            # no-op when the text has no timecode at all, so this is safe
            # even for a purely connective continuation paragraph.
            has_sync_metadata = True
        elif (
            block.original_label
            and normalize_marker(block.original_label) not in _BUILTIN_MARKERS
            and _BARE_SPEAKER_PREFIX.match(block.original_text)
        ):
            block.speaker = block.original_label.strip()

    if block.role.casefold() in {"sync", "снх", "синхрон"}:
        block.role = ""
        block.warnings.append("Technical sync type was removed from role")

    # A bare timecoded paragraph is an older supported sync form whose
    # normalized_text intentionally retains the leading timecode. Structured
    # fragments apply only when the block also has explicit speaker metadata.
    if not has_sync_metadata:
        return
    fragments = _extract_sync_fragments(fragment_source)
    if unnamed_label and fragments and not fragments[0].timecode_start:
        lead = fragments[0].text
        if lead and lead == lead.upper() and any(ch.isalpha() for ch in lead):
            # ``ДАРЬЯ У БОЛЬШОГО СОБОРА`` before the first timecode is the
            # presenter's caption, not a spoken fragment: keep it as the
            # description (the text itself stays in ``original_text``).
            block.source_description = lead
            block.warnings.append(
                "Capitalised lead-in line is kept as description, not speech: check manually"
            )
            fragments = fragments[1:]
    if not fragments:
        return
    block.sync_fragments = fragments
    first_timed = next(
        (fragment for fragment in fragments if fragment.timecode_start), fragments[0]
    )
    block.timecode_start = first_timed.timecode_start
    block.timecode_end = first_timed.timecode_end
    block.normalized_text = " ".join(fragment.text for fragment in fragments if fragment.text)


def _strip_trailing_media_credit(text: str) -> str:
    """Drop a media file name that ends ``text`` right before the next timecode.

    ``... Дональд Трамп. ZOOM0007_Tr1 (1).WAV`` -> ``... Дональд Трамп.``. The
    name starts after the last sentence boundary; without one only a short
    all-name text or the last whitespace-free token is treated as the name.
    """
    match = _TRAILING_MEDIA_CREDIT.search(text)
    if not match:
        return text
    name = match.group("name")
    start = match.start("name")
    boundary = None
    for boundary in _SENTENCE_BOUNDARY.finditer(name):
        pass
    if boundary is not None:
        start += boundary.end()
    elif not (start == 0 and len(name.split()) <= 4):
        start += len(name) - len(name.split()[-1])
    return text[:start].rstrip(" \t\r\n–—-")


def _extract_sync_fragments(text: str) -> list[SyncFragment]:
    # A raw `_TIMECODE` occurrence only becomes a fragment boundary when it is
    # unambiguous (see `_is_unambiguous_timecode_match`). Anything else -- for
    # example a bare date like "23.05" spoken inside a quote -- stays part of
    # the surrounding fragment's text instead of splitting it in two.
    boundaries: list[dict[str, Any]] = []
    search_from = 0
    while True:
        match = _TIMECODE.search(text, search_from)
        if not match:
            break
        if not _is_unambiguous_timecode_match(text, match.start(), match.end()):
            search_from = match.end()
            continue
        content_start = match.end()
        end_value = ""
        range_match = re.match(rf"^\s*[-–—]\s*({_TIMECODE_TOKEN})", text[content_start:])
        if range_match:
            end_value = range_match.group(1)
            content_start += range_match.end()
        # The punctuation belongs to the quote; only structural whitespace and
        # separators right after the boundary/range are discarded.
        separator = re.match(r"[.,\s]*", text[content_start:])
        assert separator is not None
        content_start += separator.end()
        boundaries.append(
            {
                "token_start": match.start(),
                "content_start": content_start,
                "start": match.group(1),
                "end": end_value,
            }
        )
        search_from = content_start

    fragments: list[SyncFragment] = []
    if boundaries:
        leading = _strip_trailing_media_credit(text[: boundaries[0]["token_start"]]).strip(
            " \t\r\n–—-"
        )
        leading = re.sub(r"\.\s*,", ",", leading)
        if leading:
            fragments.append(SyncFragment(text=leading, timecode_start=""))
    for index, boundary in enumerate(boundaries):
        text_end = (
            boundaries[index + 1]["token_start"] if index + 1 < len(boundaries) else len(text)
        )
        fragment_text = text[boundary["content_start"] : text_end]
        if index + 1 < len(boundaries):
            # A ``+`` right before the next timecode is the splice mark, not speech.
            fragment_text = re.sub(r"\s\+\s*$", "", fragment_text)
            fragment_text = _strip_trailing_media_credit(fragment_text)
        fragment_text = fragment_text.strip(" \t\r\n–—-")
        fragment_text = re.sub(r"\.\s*,", ",", fragment_text)
        if not fragment_text:
            continue
        fragments.append(
            SyncFragment(
                text=fragment_text,
                timecode_start=_canonicalize_timecode(boundary["start"]),
                timecode_end=_canonicalize_timecode(boundary["end"]),
            )
        )

    # ``СКЛ`` / ``СКЛЕЙКА`` is an editorial splice, not spoken text. Each
    # side must be found and cut independently so omitted speech between the
    # phrases never leaks into the assembled range. Only the first side keeps
    # the explicit timecode; later sides are searched in the same speaker
    # source without pretending they start at that exact timestamp.
    if not fragments and re.search(r"\b(?:СКЛ|СКЛЕЙКА)\b", text, flags=re.IGNORECASE):
        fragments = [SyncFragment(text=text.strip(), timecode_start="")]
    expanded: list[SyncFragment] = []
    for fragment in fragments:
        parts = []
        for raw_part in re.split(
            r"\(\s*(?:СКЛ|СКЛЕЙКА)\s*\)|\b(?:СКЛ|СКЛЕЙКА)\b",
            fragment.text,
            flags=re.IGNORECASE,
        ):
            # Remove only separators adjacent to the splice marker. Sentence
            # punctuation at the far end of each fragment belongs to speech.
            part = raw_part.strip(" \t\r\n–—-").lstrip(",.;:")
            parts.append(part)
        parts = [part for part in parts if part]
        for index, part in enumerate(parts):
            expanded.append(
                SyncFragment(
                    text=part,
                    timecode_start=fragment.timecode_start if index == 0 else "",
                    timecode_end=fragment.timecode_end if index == 0 else "",
                )
            )
    return expanded


def _canonicalize_timecode(value: str) -> str:
    value = value.strip().strip("[]")
    if re.fullmatch(r"\d{1,2}-\d{2}", value):
        return value.replace("-", ":")
    if re.fullmatch(r"\d{1,2}[.:]\d{2}\s+\d{2}", value):
        parts = re.split(r"[.:\s]+", value)
        return ":".join(parts)
    # ``01.24.08`` / ``00.35:30`` -> ``01:24:08``; ``30:42.20`` -> ``30:42``.
    # A dotted ``23.05`` is kept as written (it can be a date).
    timecode = parse_timecode(value)
    return timecode.stored if timecode else value


def _llm_sync_suggestion_has_signal(block: NormalizedBlock, item: dict[str, Any]) -> bool:
    """Decide whether a block has any real sync signal worth trusting an
    LLM's "sync" verdict for: a timecode (in the source text or in what the
    LLM itself returned), a recognizable speaker label/credit, or an
    inherited speaker from an adjoining sync block. A plain narrated
    sentence with none of these is never a sync, no matter what the LLM
    guessed -- see `_apply_llm_results`.
    """
    if _TIMECODE.search(block.original_text):
        return True
    if str(item.get("timecode_start") or "").strip():
        return True
    if str(item.get("speaker") or "").strip():
        return True
    label = block.original_label
    if label and (
        _EXPLICIT_SYNC_LABEL.match(label)
        or _SYNC_LABEL_METADATA.match(label)
        or _SYNC_NAME_ONLY_LABEL.match(label)
    ):
        return True
    if _SYNC_LABEL_ONLY_MARKER.match(block.original_text.strip()):
        return True
    return block.continuation_of_type == BlockType.SYNC.value


_LLM_PLACEHOLDERS = frozenset({"unknown", "none", "null", "n/a", "na", "нет", "неизвестно"})


def _llm_field_value(value: Any) -> str:
    """Text of an LLM metadata field; placeholder words ("unknown", "null") mean "empty"."""
    text = str(value).strip() if value else ""
    return "" if text.casefold() in _LLM_PLACEHOLDERS else text


NARRATION_RECOVERED_WARNING = (
    "LLM left a plain narration-like paragraph unresolved; kept as voiceover: check manually"
)
_RECOVERED_LLM_WARNING_PREFIXES = ("LLM returned an unknown type", "LLM confidence ")
_VOICE_READING_CUE_RE = re.compile(r"\b(?:голос|читает|читают|зачитывает)\b", re.IGNORECASE)
_NARRATION_MIN_WORDS = 4
_LLM_SCENE_OPINIONS = frozenset({BlockType.SYNC.value, BlockType.LIFE.value, BlockType.HOST.value})


def _is_voice_reading_cue(block: NormalizedBlock) -> bool:
    """``Голос «Лили»:`` / ``... читает письмо:`` -- a cue announcing text read aloud."""
    text = block.original_text.strip()
    return (
        block.type == BlockType.TECHNICAL.value
        and text.endswith(":")
        and not _TIMECODE.search(text)
        and bool(_VOICE_READING_CUE_RE.search(text))
    )


_LINK_RE = re.compile(r"(?:https?:)?//|\bwww\.|\b[\w-]+\.(?:ru|com|org|net|by|ua)\b", re.IGNORECASE)
_FOOTAGE_REMARK_START_RE = re.compile(
    r"^(?:здесь|тут|сюда|далее|потом)?\s*(?:кадры?|фото\w*|видео\w*|хроник\w*|съ[её]мк\w*|"
    r"фрагмент\w*|архив\w*|титр\w*|заставк\w*|график\w*|карт[аы]|анимаци\w*)\b",
    re.IGNORECASE,
)


def _looks_like_narration_sentence(text: str) -> bool:
    """Capitalised unlabelled prose with a finite verb.

    Not a heading or slogan (all caps), not a lower-case remark ("кадры из
    фильма о ..."), a link, a footage remark, or a caption ending with ``:``.
    """
    stripped = text.strip()
    words = stripped.split()
    first_letter = next((char for char in stripped if char.isalpha()), "")
    return (
        len(words) >= _NARRATION_MIN_WORDS
        and first_letter.isupper()
        and any(char.islower() for char in stripped)
        and not stripped.isupper()
        and not stripped.endswith(":")
        and not _LINK_RE.search(stripped)
        and not _FOOTAGE_REMARK_START_RE.match(stripped)
        and not _TIMECODE.search(stripped)
        and bool(_PERSONAL_VERB_RE.search(stripped))
    )


_CREDIT_LINE_RE = re.compile(
    r"[А-ЯЁA-Z][\w\-]+(?:\s+[А-ЯЁA-Z][\w\-]+){1,2}\s*,\s*[^\s,]+(?:\s+[^\s,]+){0,2}"
)


def _looks_like_credit_line(text: str) -> bool:
    """``Имя Фамилия, роль``: capitalised name, a comma, a short role; no finite verb.

    Verse such as ``Кроме любви твоей,`` (lowercase words, trailing comma) is not one.
    """
    stripped = text.strip()
    return (
        _CREDIT_LINE_RE.fullmatch(stripped) is not None
        and len(stripped.split()) <= 5
        and not _SENTENCE_END_PUNCT_RE.search(stripped)
        and not _PERSONAL_VERB_RE.search(stripped)
    )


def _llm_pinned_non_voiceover(block: NormalizedBlock) -> bool:
    """The LLM (or a guard) already pointed the block elsewhere: do not override."""
    return (
        any("contradicts the preceding LIFE scene" in warning for warning in block.warnings)
        or bool(block.scene_uid)
        or block.suggested_type not in ("", BlockType.VOICEOVER.value)
    )


def _recover_llm_unresolved_voiceover(
    blocks: list[NormalizedBlock], video_cue_re: re.Pattern[str] = _VIDEO_CUE_KEYWORD_RE
) -> None:
    """Return LLM-unresolved plain paragraphs to voiceover where the layout says so.

    Run once after every LLM batch is applied, in document order (so a chain
    of paragraphs resolves one by one):

    * a paragraph straight after a voiceover block (or after a cue such as
      ``Голос «Лили»:``) continues it -- a letter, a quotation or lines of
      verse split over several paragraphs, exactly as an unlabelled paragraph
      continues a ``ЗК`` block before the LLM runs;
    * otherwise an ordinary sentence the model refused to classify is kept as
      narration.

    Every recovery replaces the low-confidence/unknown warning with
    ``NARRATION_RECOVERED_WARNING`` so the reviewer still sees it.  Labelled
    blocks, all-caps ambiguities, timecoded text, dialogue replies, splices,
    footage/shot-list remarks, blocks right after (or inside) a LIFE/SYNC
    scene and blocks the LLM pinned to another type are never touched.
    """
    previous: NormalizedBlock | None = None
    in_scene = False  # previous block is a LIFE/SYNC scene or unresolved text inside one
    for block in blocks:
        after_scene = in_scene or (
            previous is not None and previous.type in (BlockType.LIFE.value, BlockType.SYNC.value)
        )
        in_scene = after_scene and block.type == BlockType.UNKNOWN.value
        if (
            not after_scene
            and block.type == BlockType.UNKNOWN.value
            and block.source == DecisionSource.LLM.value
            and not block.original_label
            and not _llm_pinned_non_voiceover(block)
            and AMBIGUOUS_CAPS_WARNING not in block.warnings
            and not _paragraph_structure(block.original_text).has_label_conflict
            and not _TIMECODE.search(block.original_text)
            and not is_plus_splice(block.original_text)
            and not is_dialogue_reply(block.original_text)
            and _technical_heuristic_confidence(block.original_text, video_cue_re) is None
            and not _LINK_RE.search(block.original_text)
            and not _FOOTAGE_REMARK_START_RE.match(block.original_text.strip())
        ):
            continues = (
                previous is not None
                and (previous.type == BlockType.VOICEOVER.value or _is_voice_reading_cue(previous))
                and bool(re.search(r"[^\W\d_]", block.original_text))
                and not _looks_like_credit_line(block.original_text)
            )
            if continues or _looks_like_narration_sentence(block.original_text):
                block.warnings = [
                    warning
                    for warning in block.warnings
                    if not warning.startswith(_RECOVERED_LLM_WARNING_PREFIXES)
                ]
                if not block.suggested_type:
                    block.suggested_type = BlockType.VOICEOVER.value
                block.warnings.append(NARRATION_RECOVERED_WARNING)
                if continues:
                    block.continuation_of_type = BlockType.VOICEOVER.value
                    _apply_type(
                        block, BlockType.VOICEOVER.value, DecisionSource.BUILTIN_RULE.value, 0.8
                    )
                else:
                    _apply_type(
                        block, BlockType.VOICEOVER.value, DecisionSource.BUILTIN_RULE.value, 0.7
                    )
                block.text_for_tts = block.normalized_text
        previous = block


def _apply_llm_results(
    blocks: list[NormalizedBlock],
    results: list[dict[str, Any]],
    *,
    video_cue_re: re.Pattern[str] = _VIDEO_CUE_KEYWORD_RE,
    all_blocks: list[NormalizedBlock] | None = None,
) -> None:
    by_uid = {str(item.get("uid", "")): item for item in results}
    position = {b.uid: i for i, b in enumerate(all_blocks or [])}
    for block in blocks:
        item = by_uid.get(block.uid)
        if not item:
            block.warnings.append("LLM did not return a classification for this block")
            continue
        target_type = str(item.get("type", BlockType.UNKNOWN.value))
        if target_type not in VALID_BLOCK_TYPES:
            block.warnings.append(f"LLM returned invalid type '{target_type}'")
            continue
        try:
            confidence = float(item.get("confidence", 0.6))
        except (TypeError, ValueError):
            block.warnings.append(f"LLM returned invalid confidence {item.get('confidence')!r}")
            continue
        if target_type != BlockType.TECHNICAL.value and _EDITORIAL_VOICEOVER_CUE.match(
            block.original_text.strip()
        ):
            # A footage remark ("ФР СЕРИАЛА, ГДЕ ... – ЕСТЬ") is never speech:
            # the model cannot turn it into voiceover/sync/host/life.
            block.warnings.append(
                f"LLM type '{target_type}' ignored: the line is an editorial cue (technical)"
            )
            _apply_type(block, BlockType.TECHNICAL.value, DecisionSource.BUILTIN_RULE.value, 0.95)
            continue
        if target_type == BlockType.SYNC.value and not _llm_sync_suggestion_has_signal(block, item):
            # Never trust an LLM "sync" label on faith: a real sync always
            # has a timecode somewhere, a recognizable speaker credit/label,
            # or inherits one from an adjoining sync block. A plain ZK-style
            # announcement sentence has none of those, so fall back to
            # whatever type the block would have received without this LLM
            # suggestion (the same unknown/technical-heuristic path used for
            # a low-confidence or explicitly "unknown" result below).
            block.warnings.append(
                "LLM sync suggestion rejected: no timecode or recognizable speaker label"
            )
            target_type = BlockType.UNKNOWN.value
        if confidence < _LLM_CONFIDENCE_THRESHOLD or target_type == BlockType.UNKNOWN.value:
            # The LLM is unsure (or explicitly punted with "unknown"). Give
            # the builtin technical-cue heuristics one more chance before
            # leaving the block for manual resolution: `_classify_by_rules`
            # already tried them once, but some are gated by label/position
            # there to avoid ever preempting a sync/voiceover marker, and a
            # block that reaches the LLM has no such marker to protect.
            fallback_confidence = _technical_heuristic_confidence(block.original_text, video_cue_re)
            if fallback_confidence is not None:
                _apply_type(
                    block,
                    BlockType.TECHNICAL.value,
                    DecisionSource.BUILTIN_RULE.value,
                    fallback_confidence,
                )
                continue
            block.source = DecisionSource.LLM.value
            llm_type = str(item.get("type", ""))
            if llm_type in _LLM_SCENE_OPINIONS and not block.suggested_type:
                # Remember an explicit sync/life/host opinion: the narration
                # recovery must not override it.
                block.suggested_type = llm_type
            block.confidence = max(0.0, min(1.0, confidence))
            if confidence < _LLM_CONFIDENCE_THRESHOLD:
                block.warnings.append(
                    f"LLM confidence {confidence:.0%} is below the confirmation threshold"
                )
            else:
                block.warnings.append("LLM returned an unknown type")
            continue
        if (
            target_type == BlockType.VOICEOVER.value
            and not block.original_label
            and all_blocks is not None
            and position.get(block.uid, 0) > 0
            and all_blocks[position[block.uid] - 1].type == BlockType.LIFE.value
        ):
            # An unmarked paragraph straight after a LIFE scene cannot be
            # turned into narration by the model alone: keep it unresolved.
            block.source = DecisionSource.LLM.value
            block.confidence = max(0.0, min(1.0, confidence))
            if not block.suggested_type:
                block.suggested_type = target_type
            block.warnings.append(
                "LLM voiceover suggestion contradicts the preceding LIFE scene: check manually"
            )
            continue
        _apply_type(block, target_type, DecisionSource.LLM.value, confidence)
        if block.original_label:
            if item.get("label_is_marker"):
                block.label_confirmed = True
                _label, content = _split_label(block.original_text)
                block.normalized_text = content or block.original_text
            else:
                block.original_label = ""
        for key in ("speaker", "role", "timecode_start", "timecode_end", "notes"):
            value = _llm_field_value(item.get(key))
            if value:
                setattr(block, key, value)
        if block.type == BlockType.VOICEOVER.value:
            if _llm_text_truncated(block):
                # The model saw only an excerpt, so its text_for_tts would
                # silently drop the omitted middle. Keep the full local text.
                block.text_for_tts = block.normalized_text
            else:
                proposed = str(item.get("text_for_tts", "")).strip()
                if proposed != block.normalized_text.strip():
                    # The LLM boundary: drop a label the model echoed (once).
                    proposed = strip_zk_label(proposed).strip()
                # TTS preparation (numbers to words, dropped cues) is allowed;
                # a text that no longer resembles the source is not.
                if proposed and not _llm_text_close_to_source(proposed, block.normalized_text):
                    block.warnings.append(
                        "LLM text_for_tts differs from the source text and was ignored"
                    )
                    proposed = ""
                block.text_for_tts = proposed or block.normalized_text
        if _llm_text_truncated(block):
            block.warnings.append(
                "LLM classified a truncated excerpt of this oversized block (head and tail only)"
            )
        _extract_block_metadata(block)


_NUMERAL_ENDING = (
    r"(?:ого|его|ому|ему|ыми|ими|ой|ей|ым|им|ом|ем|ый|ий|ая|яя|ое|ее|ые|ие|ую|юю|ых|их|ью"
    r"|ть|ти|ми|ах|ях|а|я|е|и|о|у|ы|ь|ю|й)?"
)
_NUMERAL_STEMS = (
    "одиннадцат|двенадцат|тринадцат|четырнадцат|пятнадцат|шестнадцат|семнадцат|восемнадцат|"
    "девятнадцат|двадцат|тридцат|сороков|сорок|пятидесят|шестидесят|семидесят|восьмидесят|"
    "девяност|пятьдесят|шестьдесят|семьдесят|восемьдесят|десят|девят|восьм|восем|седьм|сем|"
    "шест|пят|четверт|четыр|трет|трех|три|втор|перв|двух|две|два|одн|один|нол|тысяч|миллион|"
    "миллиард|двест|трист|четыреста|пятьсот|шестьсот|семьсот|восемьсот|девятьсот|сотн|сот|"
    "полтор|пол"
)
_NUMERAL_WORD_RE = re.compile(rf"(?:{_NUMERAL_STEMS}){_NUMERAL_ENDING}|сто|ста|стам|стами|стах")
# Words that exist only to carry a date/number ("1915 г." -> "тысяча девятьсот
# пятнадцатого года"); dropped on both sides before comparing.
_DATE_UNIT_WORD_RE = re.compile(
    r"год(?:а|у|ом|е|ы|ов|ах|ам|ами)?|ве[кч](?:а|у|ом|е|ов|ах|ам|ами)?|процент\w*|"
    r"январ[яе]|феврал[яе]|март[ае]?|апрел[яе]|ма[яе]|июн[яе]|июл[яе]|август[ае]?|"
    r"сентябр[яе]|октябр[яе]|ноябр[яе]|декабр[яе]"
)
# Digit tokens of the source: numbers, dates (12.05.1920), ranges (1915-1917),
# ordinal suffixes (1915-го) and following abbreviations (г., гг., в., вв.).
_SOURCE_NUMBER_RE = re.compile(
    r"\d+(?:[.,:/\-\u2013\u2014]\d+)*(?:[-\u2013]?[а-яё]{1,3}(?![а-яё]))?"
    r"(?:\s*(?:гг?|вв?|г\.г)\.)?|%",
    re.IGNORECASE,
)
_WORD_RE = re.compile(r"[a-zа-я]+")


def _words_without_numbers(text: str, *, source: bool) -> str:
    if source:
        text = _SOURCE_NUMBER_RE.sub(" ", text)
    words = _WORD_RE.findall(text.lower().replace("ё", "е"))
    kept = [
        word
        for word in words
        if not _NUMERAL_WORD_RE.fullmatch(word) and not _DATE_UNIT_WORD_RE.fullmatch(word)
    ]
    return " ".join(kept)


def _llm_text_close_to_source(proposed: str, source: str) -> bool:
    """True when an LLM ``text_for_tts`` is still the same speech as the source.

    TTS preparation spells numbers out (``В 1915 году`` -> ``В тысяча девятьсот
    пятнадцатом году``), so digits (and ``г./гг./в.``) are removed from the
    source and numeral words from both texts before comparing the rest.
    """
    a, b = " ".join(proposed.split()), " ".join(source.split())
    if a == b:
        return True
    if not b:
        return False
    rest_a = _words_without_numbers(a, source=False)
    rest_b = _words_without_numbers(b, source=True)
    if not rest_a and not rest_b:
        # Nothing but numbers on both sides: the proposal must still be speech.
        return bool(_WORD_RE.search(a.lower()))
    if not rest_a or not rest_b or not (0.5 <= len(rest_a) / len(rest_b) <= 2.0):
        return False
    if SequenceMatcher(None, rest_a, rest_b, autojunk=False).ratio() < 0.6:
        return False
    # Short texts can pass the character check by accident: the shared words
    # must also be a good part of both texts.
    words_a, words_b = rest_a.split(), rest_b.split()
    return SequenceMatcher(None, words_a, words_b, autojunk=False).ratio() >= 0.5


def _llm_cache_path(output_path: Path) -> Path:
    parent = Path(output_path).parent
    work_dir = parent if parent.name == "work" else parent / "work"
    return work_dir / "normalization_llm_cache.json"


def _load_llm_cache(path: Path) -> dict[str, Any]:
    try:
        payload = read_json(path)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {"version": _LLM_CACHE_VERSION, "entries": {}}
    if (
        not isinstance(payload, dict)
        or payload.get("version") != _LLM_CACHE_VERSION
        or not isinstance(payload.get("entries"), dict)
    ):
        return {"version": _LLM_CACHE_VERSION, "entries": {}}
    return payload


def _classifier_identity(
    classifier: LLMClassifier | None, *, llm_preset: str = "standard"
) -> dict[str, Any]:
    if classifier is None:
        settings = get_settings()
        return {
            "kind": "openrouter",
            "model": settings.openrouter_model,
            "llm_preset": llm_preset,
            "provider": {"sort": "throughput"} if llm_preset == "fast" else {},
            "base_url": settings.openrouter_base_url,
            "temperature": 0.1,
            "response_format": "json_object",
            "system_prompt": _OPENROUTER_SYSTEM_PROMPT,
            "instructions": _OPENROUTER_INSTRUCTIONS,
        }
    target = classifier
    code = getattr(target, "__code__", None)
    if code is None:
        target = getattr(classifier, "__call__", classifier)
        code = getattr(target, "__code__", None)
    code_hash = ""
    if code is not None:
        code_hash = sha256(
            json.dumps(
                _stable_code_identity(code),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
    return {
        "kind": "custom",
        "module": getattr(classifier, "__module__", type(classifier).__module__),
        "qualname": getattr(classifier, "__qualname__", type(classifier).__qualname__),
        "code_hash": code_hash,
    }


def _stable_code_identity(code: Any) -> dict[str, Any]:
    def stable_constant(value: Any) -> Any:
        if hasattr(value, "co_code") and hasattr(value, "co_consts"):
            return _stable_code_identity(value)
        if isinstance(value, tuple):
            return [stable_constant(item) for item in value]
        if isinstance(value, frozenset):
            items = [stable_constant(item) for item in value]
            return {
                "frozenset": sorted(
                    items,
                    key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True),
                )
            }
        if isinstance(value, (str, int, float, bool, type(None))):
            return value
        if isinstance(value, bytes):
            return {"bytes": value.hex()}
        return {"type": type(value).__qualname__, "value": str(value)}

    return {
        "code": bytes(code.co_code).hex(),
        "constants": [stable_constant(value) for value in code.co_consts],
        "names": list(code.co_names),
        "varnames": list(code.co_varnames),
        "freevars": list(code.co_freevars),
        "cellvars": list(code.co_cellvars),
        "argcount": code.co_argcount,
        "kwonlyargcount": code.co_kwonlyargcount,
    }


def _llm_rules_signature(memory: NormalizationMemory) -> str:
    # ``list_rules()`` with no scope filter returns every alias scope (both
    # 'marker' and 'video_cue'), so learned/disabled video-cue keywords are
    # already part of this signature. The scope is included explicitly below
    # so a marker-scope rule and a video_cue-scope rule that happen to share
    # the same normalized marker/type/status/source are not indistinguishable
    # in the hashed representation.
    memory_rules = [
        {
            "scope": str(rule.get("scope") or ""),
            "normalized_marker": str(rule.get("normalized_marker") or ""),
            "target_type": str(rule.get("target_type") or ""),
            "status": str(rule.get("status") or ""),
            "source": str(rule.get("source") or ""),
        }
        for rule in memory.list_rules()
    ]
    memory_rules.sort(
        key=lambda item: (
            item["scope"],
            item["normalized_marker"],
            item["target_type"],
            item["status"],
            item["source"],
        )
    )
    rules = {
        "builtin_markers": sorted(_BUILTIN_MARKERS.items()),
        "technical_markers": sorted(_TECH_MARKERS),
        "sync_metadata": _SYNC_METADATA.pattern,
        "sync_label_metadata": _SYNC_LABEL_METADATA.pattern,
        "sync_name_only": _SYNC_NAME_ONLY_LABEL.pattern,
        "editorial_timecode_prefix": _EDITORIAL_TIMECODE_PREFIX.pattern,
        "confidence_threshold": _LLM_CONFIDENCE_THRESHOLD,
        "memory_rules": memory_rules,
    }
    return sha256(json.dumps(rules, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _stable_llm_input(payload: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{key: value for key, value in item.items() if key != "uid"} for item in payload]


def _llm_cache_fingerprint(
    payload: list[dict[str, Any]],
    *,
    hint: str,
    classifier: LLMClassifier | None,
    rules_signature: str,
    llm_preset: str = "standard",
) -> str:
    material = {
        "version": _LLM_CACHE_VERSION,
        "structure_parser_version": STRUCTURE_PARSER_VERSION,
        "scene_context_version": SCENE_CONTEXT_VERSION,
        "input": _stable_llm_input(payload),
        "hint": hint.strip(),
        "classifier": _classifier_identity(classifier, llm_preset=llm_preset),
        "rules_signature": rules_signature,
    }
    return sha256(
        json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()


def _cacheable_llm_results(
    payload: list[dict[str, Any]], results: Any
) -> list[dict[str, Any]] | None:
    if not isinstance(results, list):
        return None
    requested_uids = [str(item.get("uid") or "") for item in payload]
    by_uid: dict[str, dict[str, Any]] = {}
    for result in results:
        if not isinstance(result, dict):
            return None
        uid = str(result.get("uid") or "")
        if not uid or uid in by_uid:
            return None
        by_uid[uid] = result
    if set(by_uid) != set(requested_uids):
        return None
    decisions: list[dict[str, Any]] = []
    for uid in requested_uids:
        decision = dict(by_uid[uid])
        decision.pop("uid", None)
        if str(decision.get("type") or "") not in VALID_BLOCK_TYPES:
            return None
        try:
            float(decision.get("confidence", 0.6))
            json.dumps(decision, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            return None
        decisions.append(decision)
    return decisions


def _llm_cached_results(
    cache: dict[str, Any], cache_key: str, payload: list[dict[str, Any]]
) -> list[dict[str, Any]] | None:
    entry = cache.get("entries", {}).get(cache_key)
    if not isinstance(entry, dict) or not isinstance(entry.get("results"), list):
        return None
    decisions = entry["results"]
    if len(decisions) != len(payload) or not all(isinstance(item, dict) for item in decisions):
        return None
    restored: list[dict[str, Any]] = []
    for request, decision in zip(payload, decisions):
        item = dict(decision)
        item["uid"] = str(request.get("uid") or "")
        restored.append(item)
    if _cacheable_llm_results(payload, restored) is None:
        return None
    return restored


def _cache_llm_results(
    path: Path,
    cache: dict[str, Any],
    cache_key: str,
    payload: list[dict[str, Any]],
    results: Any,
) -> bool:
    decisions = _cacheable_llm_results(payload, results)
    if decisions is None:
        return False
    entries = cache.setdefault("entries", {})
    entries[cache_key] = {"created_at": _utcnow(), "results": decisions}
    if len(entries) > _LLM_CACHE_MAX_ENTRIES:
        oldest = sorted(
            entries,
            key=lambda key: str(entries[key].get("created_at") or ""),
        )[: len(entries) - _LLM_CACHE_MAX_ENTRIES]
        for key in oldest:
            entries.pop(key, None)
    cache["version"] = _LLM_CACHE_VERSION
    try:
        _atomic_write_json(path, cache)
    except (OSError, TypeError, ValueError):
        entries.pop(cache_key, None)
        return False
    return True


def _llm_batches(
    blocks: list[NormalizedBlock],
    max_chars: int = 12_000,
    *,
    inputs: dict[str, dict[str, Any]] | None = None,
) -> list[list[NormalizedBlock]]:
    batches: list[list[NormalizedBlock]] = []
    current: list[NormalizedBlock] = []
    current_size = 0
    for block in blocks:
        item = inputs[block.uid] if inputs is not None else _llm_input(block)
        block_size = len(json.dumps(item, ensure_ascii=False))
        if current and current_size + block_size > max_chars:
            batches.append(current)
            current = []
            current_size = 0
        current.append(block)
        current_size += block_size
    if current:
        batches.append(current)
    return batches


def _llm_text_truncated(block: NormalizedBlock) -> bool:
    return len(block.original_text) > _LLM_BLOCK_TEXT_MAX_CHARS


def _llm_text_excerpt(text: str) -> str:
    """Head and tail of an oversized block.

    Classification depends on the label/first words (head) and on a trailing
    timecode or speaker credit (tail); the middle adds cost, not signal.
    """
    if len(text) <= _LLM_BLOCK_TEXT_MAX_CHARS:
        return text
    return text[:_LLM_BLOCK_HEAD_CHARS] + _LLM_TRUNCATION_MARKER + text[-_LLM_BLOCK_TAIL_CHARS:]


def _llm_input(block: NormalizedBlock, context: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return only classification context, avoiding duplicated draft fields.

    ``context`` carries a few truncated neighbouring paragraphs and is added
    only for unresolved blocks (it is reference material, not a target).
    """
    item: dict[str, Any] = {
        "uid": block.uid,
        "original_text": _llm_text_excerpt(block.original_text),
        "original_label": block.original_label,
        "suggested_type": block.suggested_type,
        "suggested_label": block.suggested_label,
        "continuation_of_type": block.continuation_of_type,
    }
    if _llm_text_truncated(block):
        item["text_truncated"] = True
    if context:
        item["context"] = context
    return item


def _openrouter_classifier(
    blocks: list[dict[str, Any]],
    hint: str,
    *,
    llm_preset: str = "standard",
    usage_callback: Callable[[dict[str, Any]], None] | None = None,
    budget: CostBudget | None = None,
) -> list[dict[str, Any]]:
    llm_preset = normalize_llm_preset(llm_preset)
    settings = get_settings()
    if not settings.openrouter_api_key:
        raise RuntimeError("OPENROUTER_API_KEY is not set")
    try:
        import requests
    except ImportError as exc:
        raise RuntimeError("requests is not installed") from exc
    items_json = json.dumps(blocks, ensure_ascii=False)
    prompt = _OPENROUTER_INSTRUCTIONS + f"Author hint: {hint or '(none)'}. Items: {items_json}"
    # Every item is answered with its own fields; voiceover items also echo
    # their text in text_for_tts, so the answer is about as long as the input
    # plus ~150 characters of JSON per item.
    max_tokens = resolve_max_tokens(
        settings,
        expected_output_chars=len(items_json) + 150 * len(blocks),
        floor=1024,
    )
    json_body: dict[str, Any] = {
        "model": settings.openrouter_model,
        "messages": [
            {"role": "system", "content": _OPENROUTER_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
        "max_tokens": max_tokens,
        # Without this flag OpenRouter omits ``usage.cost`` from the answer.
        "usage": {"include": True},
    }
    if llm_preset == "fast":
        json_body["provider"] = {"sort": "throughput"}
    request_kwargs = {
        "headers": {
            "Authorization": f"Bearer {settings.openrouter_api_key}",
            "Content-Type": "application/json",
        },
        "json": json_body,
        "timeout": 120,
    }
    response = None
    reservation_id: str | None = None
    for attempt in range(3):
        # Reserve before every HTTP attempt; CostBudgetExceeded is not a
        # RequestException, so the retry handling below never swallows it.
        if budget is not None:
            reservation_id = budget.reserve_llm_attempt(json_body["messages"], max_tokens)
        try:
            response = requests.post(
                f"{settings.openrouter_base_url}/chat/completions", **request_kwargs
            )
        except requests.RequestException as exc:
            if budget is not None:
                budget.release_if_unsent(reservation_id, exc)
            if attempt == 2:
                raise
            time.sleep(2**attempt)
            continue
        if budget is not None:
            budget.release_if_rejected(reservation_id, response)
        if response.status_code not in {408, 429, 500, 502, 503, 504} or attempt == 2:
            break
        time.sleep(2**attempt)
    assert response is not None
    response.raise_for_status()
    body = response.json()
    if budget is not None and reservation_id is not None:
        budget.settle_response(reservation_id, body)
    report_json_usage(usage_callback, body, model=settings.openrouter_model)
    raise_if_truncated(body, model=settings.openrouter_model, max_tokens=max_tokens)
    content = body["choices"][0]["message"]["content"]
    if not isinstance(content, str):
        content = json.dumps(content, ensure_ascii=False)
    content = content.strip()
    if not content.startswith("{"):
        start, end = content.find("{"), content.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("OpenRouter response does not contain a JSON object")
        content = content[start : end + 1]
    parsed = json.loads(content)
    items = parsed.get("items", [])
    if not isinstance(items, list):
        raise ValueError("OpenRouter response does not contain an items array")
    return items


def _validate_structure(scenario: NormalizedScenario) -> None:
    errors: list[str] = []
    if not scenario.source or not scenario.source_hash:
        errors.append("Scenario source identity is missing")
    seen: set[str] = set()
    for expected_order, block in enumerate(scenario.blocks, start=1):
        if not block.uid or block.uid in seen:
            errors.append(f"Block {expected_order} has missing or duplicate uid")
        seen.add(block.uid)
        if block.order != expected_order:
            errors.append("Block order must be consecutive and match list order")
        if not block.original_text.strip():
            errors.append(f"Block {expected_order} has empty original text")
        if not block.normalized_text.strip():
            errors.append(f"Block {expected_order} has empty normalized text")
        if block.type not in VALID_BLOCK_TYPES:
            errors.append(f"Block {expected_order} has invalid type '{block.type}'")
        if block.source not in VALID_DECISION_SOURCES:
            errors.append(f"Block {expected_order} has invalid source '{block.source}'")
    if errors:
        raise NormalizationValidationError(errors)


def _prepared_projection(scenario: NormalizedScenario) -> dict[str, Any]:
    voiceovers = [
        block
        for block in scenario.blocks
        if block.type == BlockType.VOICEOVER.value and not block.absorbed_by_uid
    ]
    return {
        "source": scenario.source,
        "used_llm": any(block.source == DecisionSource.LLM.value for block in scenario.blocks),
        "manual_markers_used": any(
            block.source == DecisionSource.MANUAL.value for block in scenario.blocks
        ),
        "extraction_method": "normalization",
        "normalization_revision": scenario.revision,
        "scenario_revision_id": scenario.scenario_revision_id,
        "parent_revision_id": scenario.parent_revision_id,
        "items": [
            {
                "id": f"zk{index}",
                "block_uid": block.uid,
                # Stored text is sanitized exactly once on write (parse/save);
                # the projection never strips again.
                "text_original": block.normalized_text,
                "text_for_tts": block.text_for_tts.strip(),
                "pause_ms": 300,
                "notes": block.notes,
            }
            for index, block in enumerate(voiceovers, start=1)
        ],
    }


def _raw_projection(scenario: NormalizedScenario) -> str:
    counters: dict[str, int] = {}
    prefixes = {
        BlockType.VOICEOVER.value: "ЗК",
        BlockType.SYNC.value: "СНХ",
        BlockType.HOST.value: "ВЕДУЩИЙ",
        BlockType.LIFE.value: "ЛАЙФ",
        BlockType.TECHNICAL.value: "ТЕХ",
        BlockType.OTHER.value: "ПРОЧЕЕ",
        BlockType.UNKNOWN.value: "НЕИЗВЕСТНО",
    }
    lines: list[str] = []
    for block in scenario.blocks:
        if block.absorbed_by_uid:
            continue
        counters[block.type] = counters.get(block.type, 0) + 1
        prefix = prefixes[block.type]
        lines.append(f"{prefix}{counters[block.type]}: {block.normalized_text}")
    return "\n".join(lines) + "\n"


def _file_hash(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _path_lock(path: Path) -> threading.RLock:
    key = str(Path(path).resolve())
    with _PATH_LOCKS_GUARD:
        return _PATH_LOCKS.setdefault(key, threading.RLock())


def _infer_learning_candidates(
    scenario: NormalizedScenario, memory: NormalizationMemory
) -> list[dict[str, Any]]:
    candidates: dict[str, dict[str, Any]] = {}
    rules = memory.list_rules()
    for block in scenario.blocks:
        from_type = block.proposed_type
        if (
            not from_type
            or block.type == from_type
            or block.type in {BlockType.UNKNOWN.value, BlockType.OTHER.value}
            or not block.original_label
            or not block.label_confirmed
        ):
            continue
        source_label, content = _split_label(block.original_text)
        normalized = normalize_marker(block.original_label)
        if not content or normalize_marker(source_label) != normalized:
            continue
        if memory.is_suppressed(block.original_label):
            continue
        matching = [r for r in rules if r["normalized_marker"] == normalized]
        if any(r["target_type"] == block.type and r["status"] == "active" for r in matching):
            continue
        candidate_id = sha256(
            f"correction-v1\n{normalized}\n{from_type}\n{block.type}".encode("utf-8")
        ).hexdigest()[:20]
        item = candidates.setdefault(
            candidate_id,
            {
                "id": candidate_id,
                "marker": block.original_label,
                "normalized_marker": normalized,
                "from_type": from_type,
                "target_type": block.type,
                "reason": "manual_type_change",
                "block_uids": [],
                "orders": [],
                "snippets": [],
                "status": "new",
                "confirmations": 0,
                "required_confirmations": 2,
            },
        )
        item["block_uids"].append(block.uid)
        item["orders"].append(block.order)
        item["snippets"].append(content[:160])
        existing = next(
            (r for r in matching if r["target_type"] == block.type and r["source"] == "correction"),
            None,
        )
        if existing:
            item["status"] = existing["status"]
            item["confirmations"] = int(existing["confirmations"])
    result = list(candidates.values())
    result.extend(_infer_video_cue_candidates(scenario, memory))
    return result


def _infer_video_cue_candidates(
    scenario: NormalizedScenario, memory: NormalizationMemory
) -> list[dict[str, Any]]:
    """Propose video-cue keywords from unlabelled blocks the user reclassified.

    The memory is global, so nothing here is learned implicitly: each keyword is
    a separate candidate that is written only when the user answers
    ``remember`` for it (see :func:`_learn_from_confirmation`).  Speaker, role
    and scene corrections never produce candidates.
    """
    known = {normalize_marker(word) for word in _VIDEO_CUE_KEYWORDS}
    known.update(str(rule["normalized_marker"]) for rule in memory.list_rules(scope="video_cue"))
    candidates: dict[str, dict[str, Any]] = {}
    for block in scenario.blocks:
        if (
            block.original_label
            or block.source != DecisionSource.MANUAL.value
            or block.type != BlockType.TECHNICAL.value
            or not block.proposed_type
            or block.proposed_type == block.type
            or block.absorbed_by_uid
        ):
            continue
        for keyword in _extract_manual_video_cue_keywords(block.normalized_text, set(known)):
            normalized = normalize_marker(keyword)
            candidate_id = sha256(f"video-cue-v1\n{normalized}".encode("utf-8")).hexdigest()[:20]
            item = candidates.setdefault(
                candidate_id,
                {
                    "id": candidate_id,
                    "marker": keyword,
                    "normalized_marker": normalized,
                    "from_type": block.proposed_type,
                    "target_type": BlockType.TECHNICAL.value,
                    "reason": "manual_video_cue",
                    "scope": "video_cue",
                    "block_uids": [],
                    "orders": [],
                    "snippets": [],
                    "status": "new",
                    "confirmations": 0,
                    "required_confirmations": 1,
                },
            )
            item["block_uids"].append(block.uid)
            item["orders"].append(block.order)
            item["snippets"].append(block.normalized_text.strip()[:160])
    return list(candidates.values())


def _validate_learning_decisions(
    candidates: list[dict[str, Any]], decisions: list[dict[str, Any]] | None
) -> dict[str, str]:
    valid_ids = {item["id"] for item in candidates}
    result = {item["id"]: "this_time" for item in candidates}
    seen: set[str] = set()
    for decision in decisions or []:
        candidate_id = str(decision.get("candidate_id") or decision.get("id") or "")
        action = str(decision.get("action") or "")
        if candidate_id in seen or candidate_id not in valid_ids:
            raise ValueError("Invalid or duplicate learning candidate decision")
        if action not in {"remember", "this_time", "never"}:
            raise ValueError("Learning action must be remember, this_time or never")
        seen.add(candidate_id)
        result[candidate_id] = action
    return result


# Matches a single alphabetic (Cyrillic or Latin) word, used to pull
# candidate video-cue keywords out of a manually-corrected unlabelled block's
# text (see `_extract_manual_video_cue_keywords`). Deliberately excludes
# digits/punctuation so the result is always a plain, learnable word.
_MANUAL_VIDEO_CUE_WORD_RE = re.compile(r"[A-Za-zА-Яа-яЁё]+")
_MANUAL_VIDEO_CUE_MAX_WORDS = 8
_MANUAL_VIDEO_CUE_MIN_WORD_LEN = 4
_MANUAL_VIDEO_CUE_MAX_KEYWORDS_PER_BLOCK = 3


def _extract_manual_video_cue_keywords(text: str, known_markers: set[str]) -> list[str]:
    """Pull up to :data:`_MANUAL_VIDEO_CUE_MAX_KEYWORDS_PER_BLOCK` new
    video-cue keywords out of an unlabelled block's text that an operator has
    just manually reclassified as technical, so the same unmarked shot-list
    remark is recognized by rule alone next time (see
    `_learn_from_confirmation`).

    Deliberately conservative -- this must never turn ordinary narration into
    a learned keyword:
      * skips URLs and bulleted lines outright (already covered
        unconditionally by `_URL_RE`/`_BULLET_PREFIX_RE`; the lexicon would
        only be noise from these)
      * skips anything containing a personal (finite) verb, or longer than
        `_MANUAL_VIDEO_CUE_MAX_WORDS` words -- the same "looks like a shot
        list, not a sentence" signal `_looks_like_video_cue` relies on
      * only lower-cased, purely alphabetic words of at least
        `_MANUAL_VIDEO_CUE_MIN_WORD_LEN` characters qualify, and words
        already present in ``known_markers`` (normalized) are skipped
    """
    stripped = text.strip()
    if not stripped:
        return []
    if _URL_RE.search(stripped) or _BULLET_PREFIX_RE.match(stripped):
        return []
    if len(stripped.split()) > _MANUAL_VIDEO_CUE_MAX_WORDS:
        return []
    if _PERSONAL_VERB_RE.search(stripped):
        return []
    keywords: list[str] = []
    for match in _MANUAL_VIDEO_CUE_WORD_RE.finditer(stripped):
        word = match.group(0).lower()
        if len(word) < _MANUAL_VIDEO_CUE_MIN_WORD_LEN:
            continue
        normalized = normalize_marker(word)
        if not normalized or normalized in known_markers:
            continue
        known_markers.add(normalized)
        keywords.append(word)
        if len(keywords) >= _MANUAL_VIDEO_CUE_MAX_KEYWORDS_PER_BLOCK:
            break
    return keywords


def _learn_from_confirmation(
    scenario: NormalizedScenario,
    memory_path: Path,
    candidates: list[dict[str, Any]],
    decisions: dict[str, str],
) -> None:
    with NormalizationMemory(memory_path) as memory:
        learned_video_cue_keyword = False
        for candidate in candidates:
            action = decisions.get(candidate["id"], "this_time")
            if candidate.get("scope") == "video_cue":
                # Explicit decisions only: ``this_time``/``never`` leave the
                # global video-cue lexicon untouched.
                if action == "remember":
                    memory.record_manual(
                        candidate["marker"], candidate["target_type"], scope="video_cue"
                    )
                    learned_video_cue_keyword = True
                continue
            if action == "remember":
                memory.record_correction_confirmation(
                    candidate["marker"], candidate["target_type"], scenario.source_hash
                )
            elif action == "never":
                memory.suppress_marker(candidate["marker"])
        for block in scenario.blocks:
            if (
                not block.original_label
                or not block.label_confirmed
                or block.type
                in {
                    BlockType.UNKNOWN.value,
                    BlockType.OTHER.value,
                }
            ):
                continue
            if block.source == DecisionSource.MANUAL.value:
                if block.memory_rule_id:
                    try:
                        memory.record_correction(block.memory_rule_id)
                    except KeyError:
                        pass
            elif block.source == DecisionSource.LLM.value:
                memory.record_llm_confirmation(
                    block.original_label, block.type, scenario.source_hash
                )
        if learned_video_cue_keyword:
            # A newly learned keyword was just written to this db's
            # scope='video_cue' rules, but `_seed_video_cue_rules` may
            # already have this db marked as "seeded" from an earlier
            # normalization run in this process; keep the process-local cache
            # honest so it never masks what memory now actually contains.
            invalidate_video_cue_seed_cache(memory.path)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()
