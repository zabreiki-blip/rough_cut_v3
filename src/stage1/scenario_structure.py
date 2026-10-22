"""Pure structural parsing of scenario paragraphs (no LLM, no IO).

One paragraph is split into a service label (``ЗК``, ``З.К.``, ``СНХ``,
``ЛАЙФ``/``ЛАЙФЫ``, ``СТЕНДАП`` ...), the author's block number, and the
speech that follows.  Technical montage cues (``БЫСТРАЯ НАРЕЗКА ...``) are
recognised here as well, so callers can classify them *before* any
"capitalised words are a hero name" heuristic runs.

The source text is never modified: every part is reported together with its
``(start, end)`` range in the original paragraph.

Author number versus speech
---------------------------
A number right after a label is the author's block number (it never reaches
TTS) only when it is unambiguous; otherwise it is speech and is preserved.

* Glued to the label (``ЗК14 Текст``, ``ЗК1.10 Текст``, ``ЗК0.1``, ``Диктор-1``):
  up to three digits per component, optionally a single trailing letter, and it
  must be followed by whitespace, a separator (``, : ; - – —``), a period that
  is not part of a longer number, or the end of the paragraph.
* Separated by a space (``ЗК 5: Текст``, ``ЗК 5. Текст``, ``ЗК 5 - Текст``,
  ``ЗК 5``): the number must be followed by ``:`` (not a clock time such as
  ``5:30``), by a period then whitespace or the end, by a dash followed by a
  capital letter or ``«``, or end the paragraph.
* A glued number is speech when a group of exactly three digits (``ЗК1 000
  рублей``) or a month name (``ЗК1 июля``) follows, or a dash with a lower-case
  continuation (``ЗК1 - это``).
* Everything else stays in the speech: ``ЗК 1915 год``, ``ЗК 3 года спустя``,
  ``ЗК 25 марта``, ``ЗК 1.10 рубля`` and any number of four or more digits
  (``ЗК1915 год``) -- a year is never a block number.

Ambiguity is resolved towards keeping the text: a mistaken "speech" costs one
visible digit, a mistaken "number" silently loses source text.

Timecodes
---------
Full timecodes are located *before* any split on a colon, comma or dash, so
``(00:42:32)`` never breaks a description at ``(00`` and ``[29:41.93 - 30:42.20]``
never leaves a stray ``93``.  A timecode keeps the author's spelling
(``Timecode.raw``) and offers an unambiguous value (``Timecode.normalized``,
``HH:MM:SS`` with the fraction when written).  ``Timecode.stored`` is what the
scenario fields carry: ``HH:MM:SS`` for full timecodes and the unchanged
``MM:SS`` for timecodes without hours (the project already reads those as
minutes and seconds; existing scenarios and tests rely on it).  Fractions are
dropped there because ``sync_matcher.parse_timecode`` reads a fourth component
as frames, so ``29:41.93`` is stored as ``29:41``.
A date, a year or a clock time in speech
(``в 1915.10``, ``12.05.1915``, ``в 6:30``) is not a timecode: apart from the
colon-only ``HH:MM:SS`` form a token is accepted only in a structural place (in
brackets, right after the label or credit, after ``+``, in a range).
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Iterable, Mapping, Sequence

from .normalization_memory import normalize_marker

# Core label -> block type candidates.  ``normalizer`` passes its own, wider
# table (``_BUILTIN_MARKERS`` + ``_TECH_MARKERS``); this one covers the
# families the pure functions must understand on their own.
DEFAULT_MARKER_TYPES: Mapping[str, str] = {
    "зк": "voiceover",
    "zk": "voiceover",
    "з к": "voiceover",
    "з": "voiceover",
    "снх": "sync",
    "синхр": "sync",
    "синхрон": "sync",
    "sync": "sync",
    "лайф": "life",
    "лайфы": "life",
    "life": "life",
    "стендап": "sync",
    "подводка": "sync",
    "диктор": "voiceover",
}

# Normalised marker -> canonical label shown in ``ParagraphStructure.label``.
_CANONICAL_LABELS: Mapping[str, str] = {
    "зк": "ЗК",
    "zk": "ЗК",
    "з к": "ЗК",
    "з": "ЗК",
    "снх": "СНХ",
    "синхр": "СНХ",
    "синхрон": "СНХ",
    "sync": "СНХ",
    "лайф": "ЛАЙФ",
    "лайфы": "ЛАЙФ",
    "life": "ЛАЙФ",
    "стендап": "СТЕНДАП",
    "подводка": "ПОДВОДКА",
}

# Labels that announce a presenter/host without naming them.  They are never a
# speaker: the block is flagged for a manual check (``СТЕНДАП``) -- and
# ``ПОДВОДКА`` only counts when written in capitals, so the ordinary word
# ("Подводка к сюжету ...") stays speech.
_UNNAMED_SPEAKER_LABELS: Mapping[str, str] = {"СТЕНДАП": "standup", "ПОДВОДКА": "lead_in"}
_CAPITALS_ONLY_MARKERS = frozenset({"подводка"})

# A second service label right after ``ЗК`` is an author's typo or a missing
# separator (``ЗК СНХ ...``); the block must be reviewed, not guessed.
_CONFLICTING_NEXT_LABELS = frozenset({"снх", "синхрон", "sync", "лайф", "лайфы", "life", "стендап"})

# Montage directives that are not a person's name even when written in capitals.
_TECHNICAL_CUE_WORDS = frozenset(
    {
        "нарезка",
        "нарезки",
        "возвращаемся",
        "перебивка",
        "перебивки",
        "монтаж",
        "заставка",
    }
)
_TECHNICAL_CUE_MAX_PREFIX_WORDS = 3

STANDUP_WARNING = "Stand-up label: speaker is not identified, check manually"
LEAD_IN_WARNING = "Lead-in label: speaker is not identified, check manually"
_UNNAMED_SPEAKER_WARNINGS: Mapping[str, str] = {
    "standup": STANDUP_WARNING,
    "lead_in": LEAD_IN_WARNING,
}

_NUM = r"\d{1,3}(?:\.\d{1,3})*[A-Za-zА-Яа-яЁё]?"
# What after a number shows that it is speech: a group of exactly three digits
# (``1 000 рублей``) or a month name in any case (``1 июля``).
_MONTH = (
    r"(?i:(?:январ|феврал|март|апрел|июн|июл|август|сентябр|октябр|ноябр|декабр)"
    r"(?:ь|я|ю|е|а|у|ом|ём|ем)?|ма(?:й|я|ю|е|ем))"
)
_SPEECH_AFTER_NUMBER = rf"\s+(?:\d{{3}}(?!\d)|{_MONTH}(?![А-Яа-яЁё]))"
# A dash after a number separates it only when a capital letter or a quote
# follows (``ЗК 12 - Текст``); ``ЗК 5 - это много`` keeps the number.
_NOT_SEPARATOR_DASH = r"\s*[-–—]\s*(?![А-ЯЁA-Z«])"
_GLUED_NUMBER = re.compile(
    rf"[-.]?(?P<n>{_NUM})(?!{_SPEECH_AFTER_NUMBER}|\s+[-–—]\s+(?![А-ЯЁA-Z«]))"
    r"(?=$|\s|[,;\-–—]|:(?!\d)|\.(?!\d))"
)
_SPACED_NUMBER = re.compile(
    rf"\s+(?P<n>{_NUM})(?=\s*:(?!\d)|\.(?=\s|$)|\s*[-–—]\s*(?=[А-ЯЁA-Z«])|\s*$)"
)
_SEPARATOR = re.compile(r"\s*[,.:\-–—]\s*|\s+|$")
# ``З.К.`` / ``З.к`` / ``ЗК`` / ``З К``.
_ZK_HEAD = re.compile(r"\s*(?P<label>з\s*\.?\s*к\.?|zk)", re.IGNORECASE)
_WORD = re.compile(r"[A-Za-zА-Яа-яЁё]+")
_GLUE_START = re.compile(r"[А-ЯЁA-Z«(]")


@dataclass(frozen=True)
class ParagraphStructure:
    """Result of parsing one paragraph.  All ``*_start``/``*_end`` are offsets
    into ``text`` (the untouched source paragraph)."""

    text: str
    label: str = ""  # canonical, e.g. "ЗК"
    label_raw: str = ""  # as written, without the author number, e.g. "З.К."
    author_number: str = ""  # "14", "1.10"; "" when absent
    label_start: int = 0
    label_end: int = 0  # end of label including the author number
    speech: str = ""
    speech_start: int = 0
    speech_end: int = 0
    candidate_type: str = ""  # voiceover/sync/life/technical/host/unknown or ""
    metadata: Mapping[str, str] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()

    @property
    def has_label(self) -> bool:
        return bool(self.label)

    @property
    def is_technical_cue(self) -> bool:
        return "technical_cue" in self.metadata

    @property
    def has_label_conflict(self) -> bool:
        return "label_conflict" in self.metadata


def match_author_number(text: str, pos: int) -> tuple[str, int] | None:
    """Return ``(number, end)`` when an author number follows a label ending at ``pos``.

    See the module docstring for the rule.  ``end`` is the offset right after the
    number; a separator (if any) is not consumed.
    """
    glued = _GLUED_NUMBER.match(text, pos)
    if glued:
        return glued.group("n"), glued.end("n")
    spaced = _SPACED_NUMBER.match(text, pos)
    if spaced:
        return spaced.group("n"), spaced.end("n")
    return None


def parse_paragraph(text: str, marker_types: Mapping[str, str] | None = None) -> ParagraphStructure:
    """Split ``text`` into label, author number and speech.

    ``marker_types`` maps normalised markers (see ``normalize_marker``) to block
    type candidates; it defaults to ``DEFAULT_MARKER_TYPES``.
    """
    markers = DEFAULT_MARKER_TYPES if marker_types is None else marker_types
    head = _match_head(text, markers)
    if head is None:
        cue = detect_technical_cue(text)
        if cue:
            stripped = text.strip()
            start = len(text) - len(text.lstrip())
            return ParagraphStructure(
                text=text,
                speech=stripped,
                speech_start=start,
                speech_end=start + len(stripped),
                candidate_type="technical",
                metadata={"technical_cue": cue},
            )
        stripped = text.strip()
        start = len(text) - len(text.lstrip())
        return ParagraphStructure(
            text=text, speech=stripped, speech_start=start, speech_end=start + len(stripped)
        )

    label_start, label_raw_end, glue_allowed = head
    label_raw = text[label_start:label_raw_end]
    number = ""
    label_end = label_raw_end
    speech_start: int | None
    found = match_author_number(text, label_raw_end)
    if found:
        number, label_end = found
        sep = _SEPARATOR.match(text, label_end)
        speech_start = sep.end() if sep else label_end
    else:
        sep = _SEPARATOR.match(text, label_raw_end)
        if sep:
            speech_start = sep.end()
        elif glue_allowed or text[label_raw_end : label_raw_end + 1].isdigit():
            # ``З.КВ 1915`` (marker glued to the first word) or a digit run that
            # is not an author number (``ЗК1915 год``): the text is speech.
            speech_start = label_raw_end
        else:
            speech_start = None
    if speech_start is None:  # pragma: no cover - guarded by _match_head
        return ParagraphStructure(text=text, speech=text.strip())

    speech = text[speech_start:].rstrip()
    speech_end = speech_start + len(speech)
    speech = speech.strip()
    normalized = normalize_marker(label_raw)
    label = _CANONICAL_LABELS.get(normalized, label_raw.strip().upper())
    candidate = markers.get(normalized, "")
    metadata: dict[str, str] = {}
    warnings: list[str] = []
    if number:
        metadata["author_number"] = number
    unnamed = _UNNAMED_SPEAKER_LABELS.get(label, "")
    if not unnamed and label == "СНХ":
        # ``СНХ ПОДВОДКА ДАРЬЯ ...``: the second word is a service label, not a
        # speaker; skip it and flag the block.
        nested = _WORD.match(speech)
        if nested and nested.group(0).isupper():
            nested_label = _CANONICAL_LABELS.get(normalize_marker(nested.group(0)), "")
            unnamed = _UNNAMED_SPEAKER_LABELS.get(nested_label, "")
            if unnamed:
                label_end = speech_start + nested.end()
                rest = _SEPARATOR.match(text, label_end)
                speech_start = rest.end() if rest else label_end
                speech = text[speech_start:].rstrip()
                speech_end = speech_start + len(speech)
                speech = speech.strip()
    if unnamed:
        metadata["service_label"] = unnamed
        warnings.append(_UNNAMED_SPEAKER_WARNINGS[unnamed])
    if label == "ЗК":
        first = _WORD.match(speech)
        if (
            first
            and first.group(0).isupper()
            and normalize_marker(first.group(0)) in _CONFLICTING_NEXT_LABELS
        ):
            conflicting = first.group(0)
            candidate = "unknown"
            metadata["label_conflict"] = f"{label}+{conflicting}"
            warnings.append(
                f"Ambiguous label combination '{label} {conflicting}': "
                "check the block type manually"
            )
    return ParagraphStructure(
        text=text,
        label=label,
        label_raw=label_raw.strip(),
        author_number=number,
        label_start=label_start,
        label_end=label_end,
        speech=speech,
        speech_start=speech_start,
        speech_end=speech_end,
        candidate_type=candidate,
        metadata=metadata,
        warnings=tuple(warnings),
    )


def _match_head(text: str, markers: Mapping[str, str]) -> tuple[int, int, bool] | None:
    """Locate a label at the start of ``text``: ``(start, end, glue_allowed)``."""
    zk = _ZK_HEAD.match(text)
    if zk:
        end = zk.end("label")
        label = zk.group("label")
        following = text[end : end + 1]
        if not _WORD.match(following):
            return zk.start("label"), end, False
        # ``З.КВ 1915 году``: a dotted marker glued to a capitalised word.
        if "." in label and _GLUE_START.match(following):
            return zk.start("label"), end, True
    # Other markers, longest first so "закадровый текст" beats "з".
    start = len(text) - len(text.lstrip())
    lowered = text[start:].casefold().replace("ё", "е")
    for marker in sorted(markers, key=len, reverse=True):
        if marker in {"зк", "з к", "zk"}:
            continue  # handled by the dedicated head above
        if not lowered.startswith(marker):
            continue
        end = start + len(marker)
        if _WORD.match(text[end : end + 1]):
            continue
        if marker in _CAPITALS_ONLY_MARKERS and not text[start:end].isupper():
            continue
        if marker == "з" and not _SEPARATOR.match(text, end):
            continue  # a lone "З" label needs an explicit separator
        return start, end, False
    return None


def detect_technical_cue(text: str) -> str:
    """Return the leading capitalised montage directive, or ``""``.

    ``БЫСТРАЯ НАРЕЗКА красивых кадров`` -> ``БЫСТРАЯ НАРЕЗКА``.  Only a closed set
    of directive words within the first few capitalised words counts, so a hero
    name in capitals (``ИВАН ПЕТРОВ``) is never mistaken for a cue.

    A cue is a *remark*, not speech that happens to start with a directive word:
    the capitalised prefix (up to the first lower-case word) must hold at least
    two words of two or more letters (``БЫСТРАЯ НАРЕЗКА``; a one-letter ``Я``/
    ``В`` does not count), or a single cue word must be followed by the end of
    the paragraph, a bracket or a dash with a lower-case description
    (``ВОЗВРАЩАЕМСЯ – показываем монастырь``).  ``МОНТАЖ был сложным``,
    ``ПЕРЕБИВКА: Иван говорит`` and ``Я ВОЗВРАЩАЕМСЯ домой`` stay speech.
    """
    words: list[str] = []
    prefix_end = 0
    for token in re.finditer(r"\S+", text):
        word = token.group(0).strip(".,:;!?()[]«»\"'-–—")
        if not word or not any(ch.isalpha() for ch in word) or not word.isupper():
            break
        words.append(word)
        prefix_end = token.end()
    cue = ""
    for index, word in enumerate(words[: _TECHNICAL_CUE_MAX_PREFIX_WORDS + 1]):
        if word.casefold().replace("ё", "е") in _TECHNICAL_CUE_WORDS:
            cue = " ".join(words[: index + 1])
            break
    if not cue:
        return ""
    if sum(1 for word in words if len(word) >= 2) >= 2:
        return cue
    rest = text[prefix_end:].lstrip()
    if not rest or rest[0] in "([":
        return cue
    if rest[0] in "-–—" and rest[1:].lstrip()[:1].islower():
        return cue
    return ""


def _number_key(number: str) -> str:
    """``07`` -> ``7``, ``01.010`` -> ``1.10``: components without leading zeros."""
    return re.sub(r"(?<!\d)0+(?=\d)", "", number)


def find_repeated_author_numbers(
    structures: Sequence[ParagraphStructure], labels: Iterable[str] = ("ЗК",)
) -> dict[tuple[str, str], list[int]]:
    """Group indexes of ``structures`` sharing the same ``(label, author_number)``.

    Only keys occurring more than once are returned.  Speaker indexes
    (``Диктор 1``) legitimately repeat, so only numbering labels are checked
    (default ``ЗК``).  Numbers are compared component-wise without leading
    zeros, so ``ЗК7`` and ``ЗК07`` are one number; the key carries that form.
    """
    wanted = frozenset(labels)
    seen: dict[tuple[str, str], list[int]] = defaultdict(list)
    for index, structure in enumerate(structures):
        if structure.author_number and structure.label in wanted:
            seen[(structure.label, _number_key(structure.author_number))].append(index)
    return {key: indexes for key, indexes in seen.items() if len(indexes) > 1}


# --- timecodes --------------------------------------------------------------

_TC_RE = re.compile(
    r"(?<![\w.:])(?P<a>\d{1,2})(?P<s1>[:.])(?P<b>[0-5]\d)"
    r"(?:(?P<s2>[:.])(?P<c>\d{2})(?:(?P<s3>[.,])(?P<f>\d{1,3}))?)?"
    r"(?!\d)(?![:.]\d)"
)
_RANGE_JOINER = re.compile(r"\s*[-\u2013\u2014]\s*")
_SENTENCE_END = re.compile(r"[.!?\u2026]")
_CLOSERS = {"(": ")", "[": "]"}
_HEADER_ZONE_MAX = 240
# Label families whose header may carry a timecode right after a credit.
_TIMECODE_LABELS = frozenset({"СНХ", "ЛАЙФ", "СТЕНДАП", "ПОДВОДКА"})


@dataclass(frozen=True)
class Timecode:
    """One timecode: the author's spelling plus an unambiguous value."""

    raw: str
    hours: int | None  # None: written without hours (``35:45``)
    minutes: int
    seconds: int
    fraction: str = ""
    separator: str = ":"  # first separator as written
    ambiguous: bool = False  # ``HH:MM.SS`` read as hours:minutes:seconds

    @property
    def normalized(self) -> str:
        """``HH:MM:SS`` (plus ``.fraction`` when written), always with hours."""
        value = f"{self.hours or 0:02d}:{self.minutes:02d}:{self.seconds:02d}"
        return f"{value}.{self.fraction}" if self.fraction else value

    @property
    def stored(self) -> str:
        """Value for scenario fields: ``HH:MM:SS``, or ``MM:SS`` when written
        without hours (``35:45`` and ``29:41.93`` -> ``29:41``; a dotted
        ``MM.SS`` is kept as written).  The fraction is dropped:
        ``sync_matcher.parse_timecode`` reads a fourth component as frames, so
        ``29:41.93`` would be misread."""
        if self.hours is not None:
            return f"{self.hours:02d}:{self.minutes:02d}:{self.seconds:02d}"
        if self.separator == "." and not self.fraction:
            return self.raw
        lead = re.match(r"\d+", self.raw)
        return f"{lead.group(0) if lead else '00'}:{self.seconds:02d}"

    @property
    def total_seconds(self) -> float:
        base = (self.hours or 0) * 3600 + self.minutes * 60 + self.seconds
        return base + (float(f"0.{self.fraction}") if self.fraction else 0.0)


@dataclass(frozen=True)
class TimecodeSpan:
    """A timecode (or ``first - last`` range) located in a paragraph.

    ``start``/``end`` cover the brackets too: ``(00:42:32)``, an unclosed
    ``(00:42:32`` or ``[29:41.93 - 30:42.20]``."""

    start: int
    end: int
    first: Timecode
    last: Timecode | None = None
    wrapper: str = ""  # "", "()", "(", "[]", "["
    joiner: str = ""  # text between the range ends, e.g. " - "

    @property
    def is_range(self) -> bool:
        return self.last is not None

    @property
    def canonical(self) -> str:
        if self.last is None:
            return self.first.stored
        return f"{self.first.stored}{self.joiner}{self.last.stored}"


def _interpret(match: re.Match[str], *, force_minutes: bool = False) -> Timecode | None:
    a, s1, b, s2, c, s3, f = (match.group(k) for k in ("a", "s1", "b", "s2", "c", "s3", "f"))
    raw = match.group(0)
    if c is None:
        return Timecode(raw, None, int(a), int(b), separator=s1)
    seconds = int(c)
    if s3 is not None:  # HH:MM:SS.ff
        return Timecode(raw, int(a), int(b), seconds, f, s1) if seconds <= 59 else None
    if s1 == ":" and s2 == ".":
        if seconds > 59 or int(a) > 23 or force_minutes:
            # ``29:41.93``: minutes, seconds and a fraction (not hours).
            return Timecode(raw, None, int(a), int(b), c, s1)
        return Timecode(raw, int(a), int(b), seconds, "", s1, ambiguous=True)
    if seconds > 59:
        return None
    if (s1 == "." or s2 == ".") and int(a) > 23:
        return None  # a date such as ``31.12.99``
    return Timecode(raw, int(a), int(b), seconds, "", s1)


def parse_timecode(value: str) -> Timecode | None:
    """Parse one timecode written alone (brackets allowed); ``None`` otherwise."""
    match = _TC_RE.fullmatch(value.strip().strip("[]").strip())
    return _interpret(match) if match else None


def normalize_timecode_value(value: object) -> str:
    """Stored form of one timecode (``01.02.03`` -> ``01:02:03``).

    Lets consumers compare timecodes saved with the old spot spelling and the
    normalized one.  Anything that is not a single timecode comes back stripped
    and otherwise untouched."""
    text = str(value or "").strip()
    parsed = parse_timecode(text.strip("()[] "))
    return parsed.stored if parsed else text


def iter_timecode_tokens(text: str) -> list[tuple[int, int, Timecode]]:
    """Every timecode-shaped token of ``text`` with no context rule applied.

    For callers that already know which timecodes are structural (from stored
    fields) and only need to find their spelling in the text."""
    tokens = []
    for match in _TC_RE.finditer(text):
        parsed = _interpret(match)
        if parsed is not None:
            tokens.append((match.start(), match.end(), parsed))
    return tokens


def _credit_like(word: str) -> bool:
    """Upper-case credit word, file name or ``NAME)`` right before a timecode."""
    if not any(ch.isalpha() for ch in word):
        return False
    return word.isupper() or "_" in word or bool(re.search(r"\.\w{2,4}$", word))


def _context_ok(text: str, pos: int, *, header_zone: bool, first: bool) -> bool:
    before = text[:pos].rstrip()
    if not before:
        return True
    last = before[-1]
    if last in "+(,:;[":
        return True
    if last in "-\u2013\u2014":
        earlier = before[:-1].rstrip()
        return not earlier or earlier[-1] in "+)" or _credit_like(earlier.split()[-1])
    if last == ")" or _credit_like(before.split()[-1]):
        return True
    return (
        header_zone
        and first
        and len(before) < _HEADER_ZONE_MAX
        and not _SENTENCE_END.search(before)
    )


def find_timecodes(text: str, *, header_zone: bool = False) -> list[TimecodeSpan]:
    """Locate structural timecodes, ranges and their brackets in ``text``.

    ``header_zone`` additionally accepts the first token of a labelled header
    (``СНХ Имя, роль 00.01.02 ...``) even after a lower-case word.
    """
    matches = list(_TC_RE.finditer(text))
    spans: list[TimecodeSpan] = []
    index = 0
    while index < len(matches):
        match = matches[index]
        first = _interpret(match)
        index += 1
        if first is None:
            continue
        last: Timecode | None = None
        joiner = ""
        end = match.end()
        if index < len(matches):
            nxt = matches[index]
            gap = text[match.end() : nxt.start()]
            if _RANGE_JOINER.fullmatch(gap):
                candidate = _interpret(nxt, force_minutes=first.hours is None)
                if candidate is not None:
                    if first.ambiguous and candidate.hours is None:
                        first = _interpret(match, force_minutes=True) or first
                    last, joiner, end = candidate, gap, nxt.end()
                    index += 1
        probe = match.start()
        while probe > 0 and text[probe - 1] == " ":
            probe -= 1
        opener = text[probe - 1] if probe > 0 and text[probe - 1] in _CLOSERS else ""
        # After a ``+`` only a two-digit hour counts (``+ 00.52.40``): a clock
        # time (``+ 5:30 утра``) or a date (``+ 3.10.20``) is ordinary speech.
        if not opener and probe > 0 and text[probe - 1] == "+" and len(match.group("a")) < 2:
            continue
        colon_only = first.hours is not None and "." not in first.raw
        # ``23.05`` is indistinguishable from a date: only brackets or a range
        # make it a timecode.
        if first.hours is None and first.separator == "." and not first.fraction:
            if not (opener or last is not None):
                continue
        if not (
            colon_only
            or opener
            or last is not None
            or _context_ok(text, match.start(), header_zone=header_zone, first=not spans)
        ):
            continue
        start, wrapper = match.start(), ""
        if opener:
            start = probe - 1
            tail = end
            while tail < len(text) and text[tail] == " ":
                tail += 1
            if tail < len(text) and text[tail] == _CLOSERS[opener]:
                end, wrapper = tail + 1, opener + _CLOSERS[opener]
            else:
                wrapper = opener
        spans.append(TimecodeSpan(start, end, first, last, wrapper, joiner))
    return spans


def _timecode_scope(text: str) -> tuple[bool, bool]:
    """``(skip, header_zone)`` for a paragraph: narration and technical cues are
    never rewritten; labelled sync/life headers get the header-zone rule."""
    structure = parse_paragraph(text)
    if structure.candidate_type in {"voiceover", "technical"}:
        return True, False
    return False, structure.label in _TIMECODE_LABELS


def find_structural_timecodes(text: str) -> list[TimecodeSpan]:
    """``find_timecodes`` with the paragraph-level policy applied."""
    skip, header = _timecode_scope(text)
    return [] if skip else find_timecodes(text, header_zone=header)


def _needs_rewrite(span: TimecodeSpan) -> bool:
    first = span.first
    if span.last is None and first.hours is None and first.separator == "." and not first.fraction:
        return False  # ``23.05``: kept as written (indistinguishable from a date)
    if span.last is None and span.wrapper in {"", "[]"} and not first.fraction:
        return first.raw != first.stored
    return True


@lru_cache(maxsize=2048)
def canonicalize_timecodes(text: str) -> str:
    """Rewrite structural timecodes into their ``stored`` colon form.

    Brackets around a timecode and an unclosed ``(`` before it are dropped
    (``(00:42:32 - А что`` -> ``00:42:32 - А что``); dotted and mixed forms
    become ``HH:MM:SS``; ``[29:41.93 - 30:42.20]`` becomes ``29:41 - 30:42``.
    Everything else is untouched, so the result is a *parsing view* of the
    paragraph -- the source text itself is never modified.
    """
    pieces: list[str] = []
    cursor = 0
    unclosed = False
    for span in find_structural_timecodes(text):
        if not _needs_rewrite(span):
            continue
        prefix = text[cursor : span.start]
        pieces.append(prefix)
        if prefix and not prefix[-1].isspace():
            pieces.append(" ")
        pieces.append(span.canonical)
        if span.end < len(text) and text[span.end].isalnum():
            pieces.append(" ")
        cursor = span.end
        unclosed = unclosed or span.wrapper in {"(", "["}
    if not pieces:
        return text
    pieces.append(text[cursor:])
    result = "".join(pieces)
    stripped = result.rstrip()
    if unclosed and stripped.endswith(")") and result.count(")") > result.count("("):
        result = stripped[:-1].rstrip() + result[len(stripped) :]
    return result


def timecode_warnings(text: str) -> list[str]:
    """Warnings for timecodes that had to be guessed (``HH:MM.SS``)."""
    return [
        f"Ambiguous timecode '{span.first.raw}' read as {span.first.stored}: check manually"
        for span in find_structural_timecodes(text)
        if span.first.ambiguous
    ]


@dataclass(frozen=True)
class TimedPart:
    text: str
    start: Timecode | None
    end: Timecode | None = None


@dataclass(frozen=True)
class TimedBody:
    """Text before the first timecode plus one part per timecode."""

    head: str
    parts: tuple[TimedPart, ...]


def split_timed_parts(body: str, *, header_zone: bool = True) -> TimedBody:
    """Split ``body`` at structural timecodes.

    ``00:05:20 Первый + 00.52.40 Второй`` gives two parts with their own
    starts and no end (a splice is two inserts, never one interval);
    ``00:05:20-00:05:40 Первый`` is one part with a range.  Order is kept as
    written -- a splice may go back in time.
    """
    spans = find_timecodes(body, header_zone=header_zone)
    if not spans:
        return TimedBody(body.strip(), ())
    parts: list[TimedPart] = []
    for position, span in enumerate(spans):
        stop = spans[position + 1].start if position + 1 < len(spans) else len(body)
        chunk = body[span.end : stop]
        if position + 1 < len(spans):
            chunk = re.sub(r"\s*\+\s*$", "", chunk)
        chunk = chunk.strip().lstrip("-\u2013\u2014.,:;").strip()
        parts.append(TimedPart(chunk, span.first, span.last))
    head = body[: spans[0].start].strip().rstrip("-\u2013\u2014,:;").strip()
    return TimedBody(head, tuple(parts))
