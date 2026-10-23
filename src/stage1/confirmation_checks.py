"""Structural quality checks that gate normalization confirmation.

The checks catch defects that a saved (possibly LLM-edited) block can carry
without any warning: a service label inside the TTS text, a timecode fragment
in a role, a false hero made of a technical cue, a lost ``+`` splice and speech
that the block no longer contains.  They are deliberately conservative: each
needs concrete evidence in the block itself, so correct data never trips them.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import re
from typing import Any, Callable

from .scenario_structure import (
    ParagraphStructure,
    find_structural_timecodes,
    is_zk_label,
    normalize_timecode_value,
)

ERROR = "error"
WARNING = "warning"

# Blocking codes introduced for the saved-block defect classes.
SERVICE_LABEL_IN_TTS = "service_label_in_tts"
TIMECODE_IN_ROLE = "timecode_in_role"
TIMECODE_REMNANT_IN_TEXT = "timecode_remnant_in_text"
FALSE_HERO_FROM_CUE = "false_hero_from_cue"
LOST_SPLICE = "lost_splice"
UNMATCHED_SPEECH = "unmatched_speech"
MONTAGE_NOTE_IN_TTS = "montage_note_in_tts"


@dataclass(frozen=True)
class ConfirmationIssue:
    code: str
    uid: str
    order: int
    message: str
    severity: str = ERROR

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


StructureOf = Callable[[str], ParagraphStructure]

_SERVICE_LABELS = frozenset({"ЗК", "СНХ", "ЛАЙФ", "СТЕНДАП", "ПОДВОДКА"})
# Words that name a montage directive or a service label; never a person.
_SERVICE_WORDS = frozenset(
    {
        "быстрая",
        "быстрый",
        "нарезка",
        "нарезки",
        "возвращаемся",
        "перебивка",
        "перебивки",
        "монтаж",
        "заставка",
        "стендап",
        "подводка",
        "лайф",
        "лайфы",
        "снх",
        "синхрон",
        "зк",
        "закадр",
        "life",
        "sync",
        # Montage remarks that the parser can take for a speaker name
        # (``СЮДА НУЖНО СНХ``, ``КАДРЫ ИЗ ФИЛЬМА``, ``ВОЗДУХ НА МУЗЫКЕ``).
        "сюда",
        "кадр",
        "кадры",
        "кадров",
        "нужно",
        "воздух",
        "вставка",
        "вставить",
        "фрагмент",
        "реконструкция",
        "хроника",
        "титр",
        "титры",
        "музыка",
        "фото",
        "фотография",
        "скл",
        "склейка",
    }
)
_TIMECODE_FRAGMENT = re.compile(
    r"\(\s*[\d:.]*\s*$|(?<![\d:.])\d{1,2}[:.][0-5]\d[:.][0-5]\d(?![\d:.])"
)
# Sync / splice labels are never narration, wherever they stand in the voiced
# text: ``Синх СторожевойТо``, ``... СКЛ ...``, ``СЮДА НУЖНО СНХ``.  ``Синхрон``
# alone is a normal word, so it counts only before a capitalised name.
_SYNC_LABEL_IN_TEXT = re.compile(
    r"(?<![А-Яа-яЁёA-Za-z])(?:(?i:синх|снх|скл)(?![А-Яа-яЁёA-Za-z])|(?i:синхрон)\s+[А-ЯЁ])"
)
_MONTAGE_NOTE_MAX_WORDS = 12
_TEXT_REMNANT = re.compile(r"^\s*(?:\)|\d{1,2}[:.][0-5]\d(?:[:.][0-5]\d)?\s*\))")
_WORD = re.compile(r"[^\W\d_]{4,}")
_MIN_MISSING_WORDS = 4
_MIN_MISSING_RATIO = 0.25
_PREFIX = 5


def _fold(word: str) -> str:
    return word.casefold().replace("ё", "е")


def _words(text: str) -> list[str]:
    return [_fold(match.group(0)) for match in _WORD.finditer(text)]


def check_service_label_in_tts(block: Any, structure_of: StructureOf) -> list[tuple[str, str, str]]:
    """Service label at the start of voiced TTS as ``(code, message, severity)``.

    A leading ``ЗК`` label only marks the voiceover and the sanitizer removes it
    from TTS, so it is a warning -- but only when the sanitizer can really remove
    it.  A label conflict (``ЗК СНХ воздух``) or a bare label is left in place on
    purpose and would be voiced, so it blocks, like any other service label
    (``СНХ``, ``ЛАЙФ``, ``СТЕНДАП``, ``ПОДВОДКА``).
    """
    # Only a voiceover block that is not absorbed is actually voiced: the prepared
    # projection (the only TTS input) contains nothing else.  sync/host/life/
    # technical blocks may carry a TTS string, but it is silent and never exported.
    if block.type != "voiceover" or block.absorbed_by_uid:
        return []
    tts = str(block.text_for_tts or "")
    if not tts.strip():
        return []
    head = structure_of(tts)
    found = _SYNC_LABEL_IN_TEXT.search(tts)
    if found and not (head.label in _SERVICE_LABELS and head.label_raw.isupper()):
        label = found.group(0).strip()
        return [
            (
                SERVICE_LABEL_IN_TTS,
                f"Block {block.order} has a sync/splice label '{label}' in its TTS text",
                ERROR,
            )
        ]
    if head.label in _SERVICE_LABELS and (head.author_number or head.label_raw.isupper()):
        removable = (
            is_zk_label(head.label_raw) and not head.has_label_conflict and bool(head.speech)
        )
        severity = WARNING if removable else ERROR
        return [
            (
                SERVICE_LABEL_IN_TTS,
                f"Block {block.order} has a service label '{head.label_raw}' in its TTS text",
                severity,
            )
        ]
    return []


_MONTAGE_STEM_LEN = 5
_MONTAGE_STEMS = frozenset(
    w[:_MONTAGE_STEM_LEN] for w in _SERVICE_WORDS if len(w) >= _MONTAGE_STEM_LEN
)


def _has_montage_word(text: str) -> bool:
    """True if any word of ``text`` is a montage/service word, matched by stem."""
    for token in re.findall(r"[^\W\d_]+", text):
        folded = _fold(token)
        if folded in _SERVICE_WORDS:
            return True
        if len(folded) >= _MONTAGE_STEM_LEN and folded[:_MONTAGE_STEM_LEN] in _MONTAGE_STEMS:
            return True
    return False


def _from_zk_label(block: Any) -> bool:
    """True if the block came from a ``ЗК.`` voiceover label (command block)."""
    label = str(getattr(block, "original_label", "") or "").strip()
    if not label:
        words = str(getattr(block, "original_text", "") or "").split()
        label = words[0].rstrip(".:") if words else ""
    return bool(label) and is_zk_label(label)


def check_montage_note_in_tts(block: Any) -> list[tuple[str, str, str]]:
    """Voiced text written entirely in capitals may be a montage remark.

    ``НА БЭКСТЕЙДЖЕ``, ``КАДРЫ ИЗ ФИЛЬМА...``, ``РЕКОНСТРУКЦИЯ ПИСЬМА`` are
    instructions for the editor, not narration.  The block is blocking only if
    it contains a montage/service word or comes from a ``ЗК.`` label; a plain
    capitals line (``ФСБ РФ``, ``НАТО США``) is only a warning.  A reviewer who
    deliberately authored the text or the block type also gets only a warning.
    """
    if block.type != "voiceover" or block.absorbed_by_uid:
        return []
    tts = str(block.text_for_tts or "").strip()
    letters = [ch for ch in tts if ch.isalpha()]
    words = [w for w in re.findall(r"[^\W\d_]+", tts) if len(w) >= 2]
    if (
        not letters
        or not all(ch.isupper() for ch in letters)
        or len(words) < 2
        or len(words) > _MONTAGE_NOTE_MAX_WORDS
    ):
        return []
    fields = set(getattr(block, "manual_fields", None) or [])
    manual = _speech_edited_by_human(block) or "type" in fields
    blocking = _has_montage_word(tts) or _from_zk_label(block)
    return [
        (
            MONTAGE_NOTE_IN_TTS,
            f"Block {block.order} voiceover text is written in capitals and looks like a "
            f"montage note: '{tts[:40]}'",
            ERROR if blocking and not manual else WARNING,
        )
    ]


def check_timecode_in_fields(block: Any) -> list[tuple[str, str]]:
    issues: list[tuple[str, str]] = []
    for field_name in ("speaker", "role", "source_description"):
        value = str(getattr(block, field_name, "") or "")
        if value and _TIMECODE_FRAGMENT.search(value):
            issues.append(
                (
                    TIMECODE_IN_ROLE,
                    f"Block {block.order} has a timecode fragment in {field_name}: '{value}'",
                )
            )
    if block.type in {"voiceover", "unknown", "technical"}:
        return issues
    original_start = block.original_text.lstrip()
    texts = [("normalized_text", block.normalized_text)] + [
        (f"sync fragment {index}", fragment.text)
        for index, fragment in enumerate(block.sync_fragments, start=1)
    ]
    for name, text in texts:
        if _TEXT_REMNANT.match(text) and not _TEXT_REMNANT.match(original_start):
            issues.append(
                (
                    TIMECODE_REMNANT_IN_TEXT,
                    f"Block {block.order} {name} starts with a timecode remnant: "
                    f"'{text.strip()[:20]}'",
                )
            )
            break
    return issues


def check_false_hero(block: Any) -> list[tuple[str, str]]:
    if block.type not in {"sync", "host"} or not block.speaker.strip():
        return []
    words = {_fold(w) for w in re.findall(r"[^\W\d_]+", block.speaker)}
    hit = sorted(words & _SERVICE_WORDS)
    if not hit:
        return []
    return [
        (
            FALSE_HERO_FROM_CUE,
            f"Block {block.order} speaker '{block.speaker}' is a technical cue, not a person",
        )
    ]


def _splice_starts(text: str) -> list[str]:
    """Stored starts of the first timecode and of every ``+``-joined one."""
    spans = find_structural_timecodes(text)
    starts: list[str] = []
    spliced = False
    for index, span in enumerate(spans):
        joined = index > 0 and text[spans[index - 1].end : span.start].rstrip().endswith("+")
        if index == 0 or joined:
            starts.append(span.first.stored)
        spliced = spliced or joined
    return starts if spliced else []


def check_lost_splice(block: Any) -> list[tuple[str, str]]:
    if block.type not in {"life", "sync", "host"} or block.absorbed_by_uid:
        return []
    starts = _splice_starts(block.original_text)
    if not starts:
        return []
    covered = {normalize_timecode_value(f.timecode_start) for f in block.sync_fragments}
    missing = [value for value in starts if normalize_timecode_value(value) not in covered]
    if not missing:
        return []
    return [
        (
            LOST_SPLICE,
            f"Block {block.order} lost the explicit '+' splice: no fragment starts at "
            f"{', '.join(missing)}",
        )
    ]


_SPEECH_FIELDS = frozenset({"normalized_text", "text_for_tts"})


def _speech_edited_by_human(block: Any) -> bool:
    """True when a reviewer deliberately authored the block's speech text."""
    fields = set(getattr(block, "manual_fields", None) or [])
    if fields & _SPEECH_FIELDS:
        return True
    # Legacy manual blocks pin every reviewed field without listing them.  The
    # block stores no pre-edit text (only proposed_type/label/source), so a real
    # text change cannot be told from an untouched one here; the downgrade to a
    # warning is kept for them deliberately.
    return block.source == "manual" and not fields


def check_unmatched_speech(block: Any, structure_of: StructureOf) -> list[tuple[str, str, str]]:
    """Lost-speech findings as ``(code, message, severity)``.

    A deliberate manual shortening is only a warning; automatic loss blocks.
    """
    if block.type not in {"voiceover", "sync", "life", "host"} or block.absorbed_by_uid:
        return []
    original = block.original_text
    structure = structure_of(original)
    speech = structure.speech if structure.has_label else original
    spans = find_structural_timecodes(original)
    if spans and structure.has_label:
        # Drop timecode wrappers so remnants such as "00" never count as words.
        speech = re.sub(r"\(\s*\d[\d:.\s\-–—]*\)?", " ", speech)
    words = [w for w in _words(speech)]
    if len(words) < _MIN_MISSING_WORDS:
        return []
    haystack: list[str] = []
    for value in (
        block.normalized_text,
        block.original_label,
        block.speaker,
        block.role,
        block.source_description,
        block.notes,
        *(fragment.text for fragment in block.sync_fragments),
    ):
        haystack.extend(_words(str(value or "")))
    prefixes = {word[:_PREFIX] for word in haystack}
    missing = [word for word in words if word[:_PREFIX] not in prefixes]
    if len(missing) >= _MIN_MISSING_WORDS and len(missing) / len(words) >= _MIN_MISSING_RATIO:
        return [
            (
                UNMATCHED_SPEECH,
                f"Block {block.order} lost {len(missing)} of {len(words)} spoken words "
                f"(e.g. '{missing[0]}')",
                WARNING if _speech_edited_by_human(block) else ERROR,
            )
        ]
    return []


def structural_checks(block: Any, structure_of: StructureOf) -> list[tuple[str, str, str]]:
    """Structural defects of one block as ``(code, message, severity)``."""
    plain = [
        *check_timecode_in_fields(block),
        *check_false_hero(block),
        *check_lost_splice(block),
    ]
    return [
        *check_service_label_in_tts(block, structure_of),
        *((code, message, ERROR) for code, message in plain),
        *check_montage_note_in_tts(block),
        *check_unmatched_speech(block, structure_of),
    ]
