"""Conservative detection of production metadata in scenario text.

These rules use generic editorial syntax and section headings, never project
names or production-specific identifiers. Ambiguous text stays as content.
"""

from __future__ import annotations

import re

# Included in the normalizer's LLM cache signature; bump when these rules change.
PRODUCTION_METADATA_VERSION = 1


_TITLE_NOTE_RE = re.compile(
    r"\(\s*(?:рабоч(?:ее|ий)\s+назван(?:ие|ия)|working\s+title)\s*\)\s*$", re.IGNORECASE
)
_AIRDATE_RE = re.compile(
    r"\s*(?:ЭФИР|ВЫХОД|ДАТА\s+ЭФИРА)\s*[:№#-]?\s*"
    r"(?:\d{1,2}[./-]\d{1,2}(?:[./-]\d{2,4})?|\d{4}[./-]\d{1,2}[./-]\d{1,2})"
    r"(?:\s+(?:в\s*)?\d{1,2}:\d{2})?\s*",
    re.IGNORECASE,
)
_NUMBERED_SECTION_RE = re.compile(
    r"\s*(?:ЗК|З\.\s*К\.|ТЕХ|СНХ)?\s*\d{1,3}\s*[.:)\-]?\s*(?P<title>.+?)\s*[.!]?\s*",
    re.IGNORECASE,
)
_OUTLINE_TITLES = {
    "информационный повод",
    "тема выпуска",
    "тема сюжета",
    "название сюжета",
    "рабочее название",
    "цель сюжета",
    "задача сюжета",
    "план сюжета",
    "содержание сюжета",
    "содержание выпуска",
    "структура сюжета",
}
_SPEAKER_PREFIX_RE = re.compile(r"^\s*(?:СНХ|СИНХРОН|ЗК|З\.\s*К\.|ТЕХ|СИНХ)\b", re.IGNORECASE)


def _is_outline_title(value: str) -> bool:
    normalized = re.sub(r"[\s.!?…:;]+$", "", value.strip().lower())
    return normalized in _OUTLINE_TITLES


def _mostly_uppercase_title(value: str) -> bool:
    words = re.findall(r"[^\W\d_]+", value, flags=re.UNICODE)
    return len(words) >= 2 and sum(word == word.upper() for word in words) / len(words) >= 0.85


def _is_explicit_title_note(value: str) -> bool:
    # A note is evidence only when the whole block is a single title card;
    # marker-prefixed speech and mixed-case prose remain content.
    if "\n" in value or _SPEAKER_PREFIX_RE.match(value) or not _TITLE_NOTE_RE.search(value):
        return False
    header = _TITLE_NOTE_RE.sub("", value).strip()
    return _mostly_uppercase_title(header)


def _is_explicit_title_label(value: str) -> bool:
    # A bare ALL CAPS prefix can be a speaker name. Require a generic editorial
    # label to make the title-card interpretation clear.
    match = re.fullmatch(r"\s*(?:ПРОЕКТ|ФИЛЬМ|НАЗВАНИЕ)\s*:\s*(.+?)\s*", value, re.IGNORECASE)
    return bool(match and _mostly_uppercase_title(match.group(1)))


def detect_production_metadata(text: str, *, first_block: bool = False) -> bool:
    """Return whether *text* is explicit production metadata rather than speech."""
    value = str(text or "")
    if not value.strip():
        return False
    # The entire block must consist of the metadata card. A date followed by
    # narration is preserved as a single content block.
    if _AIRDATE_RE.fullmatch(value):
        return True
    if first_block and (_is_explicit_title_note(value) or _is_explicit_title_label(value)):
        return True
    match = _NUMBERED_SECTION_RE.fullmatch(value)
    return bool(match and _is_outline_title(match.group("title")))
