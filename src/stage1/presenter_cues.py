"""Conservative parser for explicit presenter captions in scenario text."""

from __future__ import annotations

from dataclasses import dataclass
import re

from .scenario_structure import find_timecodes


@dataclass(frozen=True)
class PresenterPart:
    speaker: str
    description: str
    text: str


_MARKER = re.compile(r"(?:(?:СНХ)\s+)?(?:СТЕНДАП|ПОДВОДКА)(?![А-Яа-яЁёA-Za-z])")
_CONTINUATION = re.compile(r"\+\s*(?:(?:СНХ)\s+)?(?:СТЕНДАП|ПОДВОДКА)(?![А-Яа-яЁёA-Za-z])")
_WORD_RE = re.compile(r"[^\W\d_]+(?:[-'][^\W\d_]+)*", re.UNICODE)
_LOCATION_START = {"У", "В", "ВО", "НА", "ИЗ", "ОКОЛО", "ПЕРЕД", "ЗА", "ОТ"}
_SPOKEN_CAPS = {
    "Я",
    "МЫ",
    "ТЫ",
    "ВЫ",
    "ОН",
    "ОНА",
    "ОНО",
    "ОНИ",
    "ЭТО",
    "ЭТОТ",
    "ЭТА",
    "ЭТИ",
    "А",
    "И",
    "НО",
    "ЧТО",
    "КАК",
    "КОГДА",
    "ГДЕ",
    "ЗДЕСЬ",
    "ТУТ",
    "СЕЙЧАС",
    "ПОТОМ",
    "СЕГОДНЯ",
    "ЗАВТРА",
    "РАССКАЖУ",
    "РАССКАЖЕМ",
    "ВЕСТИ",
    "ИСТОРИЯ",
    "ИСТОРИЮ",
    "СЮЖЕТ",
    "ПОКАЖУ",
    "ПОКАЖЕМ",
}
_NON_NAMES = {"ТЕКСТ", "ВЕДУЩЕГО", "ВЕДУЩАЯ", "ВЕДУЩИЙ", "КАМЕРА", "СТЕНДАП", "ПОДВОДКА"}


def _is_upper_token(token: str) -> bool:
    letters = [char for char in token if char.isalpha()]
    return bool(letters) and all(char.isupper() for char in letters)


def _inside_quotes(text: str, end: int) -> bool:
    """Whether a candidate offset falls inside a quote-delimited spoken phrase."""
    stack: list[str] = []
    pairs = {"«": "»", "“": "”", "„": "“"}
    for char in text[:end]:
        if char in pairs:
            stack.append(pairs[char])
        elif char in {'"', "”", "»"}:
            if stack and stack[-1] == char:
                stack.pop()
            elif char == '"':
                stack.append(char)
    return bool(stack)


def _segments(text: str) -> list[tuple[int, int]]:
    """Return marker-body ranges, accepting only a leading cue and unquoted + continuations."""
    leading = re.match(r"\s*\+?\s*", text)
    assert leading is not None
    first = _MARKER.match(text, leading.end())
    if first is None:
        return []
    markers = [first]
    markers.extend(
        match
        for match in _CONTINUATION.finditer(text, first.end())
        if not _inside_quotes(text, match.start())
    )
    return [
        (marker.end(), markers[i + 1].start() if i + 1 < len(markers) else len(text))
        for i, marker in enumerate(markers)
    ]


def parse_presenter_parts(text: str) -> tuple[PresenterPart, ...]:
    """Extract explicitly attributed presenter parts; decline uncertain captions."""
    if not isinstance(text, str) or not text.strip():
        return ()
    segments = _segments(text)
    if not segments:
        return ()

    parts: list[PresenterPart] = []
    for start, end in segments:
        segment = text[start:end]
        if end < len(text):
            segment = re.sub(r"\s*\+\s*$", "", segment)
        segment = segment.strip()
        if not segment:
            return ()

        # Structural timecodes are part of spoken copy, even when followed by a
        # capitalized pronoun. find_timecodes also handles ranges and wrappers.
        timecodes = find_timecodes(segment, header_zone=True)
        time_start = min((span.start for span in timecodes), default=None)
        words = list(_WORD_RE.finditer(segment))
        spoken_word = next(
            (
                word
                for word in words
                if not _is_upper_token(word.group()) or word.group() in _SPOKEN_CAPS
            ),
            None,
        )
        boundaries = [
            pos
            for pos in (time_start, spoken_word.start() if spoken_word else None)
            if pos is not None
        ]
        if not boundaries:
            return ()
        speech_start = min(boundaries)
        if spoken_word is not None and speech_start == spoken_word.start():
            while speech_start > 0 and segment[speech_start - 1] in '«“"':
                speech_start -= 1
        caption = segment[:speech_start].strip()
        spoken = segment[speech_start:].strip()
        caption_words = [match.group() for match in _WORD_RE.finditer(caption)]
        if not caption_words or any(not _is_upper_token(word) for word in caption_words):
            return ()

        location_index = next(
            (i for i, word in enumerate(caption_words) if word in _LOCATION_START),
            len(caption_words),
        )
        name_words = caption_words[:location_index]
        description_words = caption_words[location_index:]
        if not name_words or len(name_words) > 3:
            return ()
        if any(word in _SPOKEN_CAPS or word in _NON_NAMES for word in name_words):
            return ()
        if name_words[0] in _LOCATION_START:
            return ()
        parts.append(PresenterPart(" ".join(name_words), " ".join(description_words), spoken))

    return tuple(parts)
