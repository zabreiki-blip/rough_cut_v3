"""Pure helpers for scene / dialogue / continuation context in a scenario.

A LIFE, SYNC or HOST block opens a *scene*.  Unmarked dialogue replies
(``— Вот эту?``) and ``+ 00.43.25 …`` splices that follow it belong to that
scene until any other block (a new explicit label, a technical heading,
narration, an unrelated paragraph) closes it.  Nothing here invents a speaker:
a reply only proves membership of the scene, never the identity of the hero.

The functions work on duck-typed blocks (``type``, ``original_label``,
``original_text``, ``continuation_of_type``) and never mutate them; the
normalizer applies the returned plan.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from .scenario_structure import find_timecodes

# Bump whenever scene/dialogue/continuation context (and the neighbour context
# sent to the LLM) changes behaviour; part of the LLM cache fingerprint.
SCENE_CONTEXT_VERSION = 3

SCENE_TYPES = frozenset({"life", "sync", "host"})
_SPLICE_TYPES = frozenset({"life", "sync"})

SPLICE_CONTINUES_WARNING = "leading '+' splice continues previous block: check manually"
SPLICE_ORPHAN_WARNING = (
    "leading '+' splice has no previous LIFE/SYNC block to continue: check manually"
)
DASH_REPLY_UNDECIDED_WARNING = (
    "dash reply after {scene}: interviewer question or hero? check manually"
)
LONG_REPLY_WARNING = "long dash paragraph joined to the {scene} scene as a reply: check manually"
SCENE_OPENER_CHANGED_WARNING = "scene opener changed: check reply type"
AMBIGUOUS_CAPS_WARNING = "all-caps line: heading or spoken text? check manually"
SECTION_HEADING_WARNING = (
    "all-caps section heading treated as technical (not spoken): check manually"
)

_DIALOGUE_DASHES = "-‐‑‒–—―"
# A dash must be followed by a letter (optionally after an opening quote or an
# ellipsis). Timed replies are checked separately with the structural parser.
_DIALOGUE_REPLY_RE = re.compile(rf"^\s*[{_DIALOGUE_DASHES}]\s*[«\"„…]?\s*[^\W\d_]")
# A dash paragraph longer than this is more likely narration than a replica.
_REPLY_MAX_CHARS = 300
_PLUS_SPLICE_RE = re.compile(r"^\s*\+\s*\(?\s*\d{1,2}\s*[.:]\s*\d{2}")
_SENTENCE_END_RE = re.compile(r"[.!?…]\s*$")
# Present-tense / reflexive endings of an all-caps finite verb: a line with one
# is narration ("ВОЗВРАЩАЕМСЯ ДОМОЙ", "ЛЮДИ ПРИЕЗЖАЮТ СЮДА"), not a heading.  A
# false positive only leaves the line to the previous behaviour.
_CAPS_VERB_RE = re.compile(r"\b[А-ЯЁ]{3,}(?:ЮТ|АТ|ЯТ|ЕТ|ИТ|ЕМ|ИМ|ЕТЕ|ИТЕ|ЕШЬ|ИШЬ|СЯ|СЬ)\b")
_HEADING_MAX_CHARS = 60
_HEADING_MAX_WORDS = 8


@dataclass(frozen=True)
class SceneLink:
    """Relation of one block to the scene context.

    ``scene_index`` is the index of the block that opened the scene (-1 when
    there is none).  ``kind`` is ``reply``, ``splice`` or ``continuation``
    (a block that already had the scene type and only needs the link).
    ``scene_type`` is the type to assign; empty means "leave unknown".
    ``suggested_type`` is only a hint for a block left unknown (a dash reply
    after SYNC may be the interviewer's off-screen question, not the hero).
    """

    scene_index: int
    kind: str
    scene_type: str = ""
    warning: str = ""
    suggested_type: str = ""


def is_dialogue_reply(text: str) -> bool:
    """``— Да.`` / ``- Вот эту?``: a dash-led replica without a label."""
    if _DIALOGUE_REPLY_RE.match(text):
        return True

    # Interview transcripts often put the source timecode before the words:
    # ``- (00:42:43) Персики`` or ``– 00:40:58 — Абсолютно``. Require a
    # structural timecode at the start and actual speech after it. This keeps
    # negative figures and date-like text from opening a scene reply.
    dash = re.match(rf"^\s*[{_DIALOGUE_DASHES}]\s*", text)
    if not dash:
        return False
    body = text[dash.end() :]
    spans = find_timecodes(body)
    if not spans or spans[0].start != 0:
        return False
    span = spans[0]
    # A bare dotted token is also a date (``12.03.25``); only accept dotted
    # timecodes here when wrapped, where the author's intent is explicit.
    if span.first.separator == "." and not span.wrapper:
        return False
    tail = body[span.end :].lstrip()
    tail = re.sub(rf"^[{_DIALOGUE_DASHES}]\s*", "", tail)
    tail = re.sub(r"^\s*[«\"„…]\s*", "", tail)
    return bool(re.match(r"[^\W\d_]", tail.lstrip(), flags=re.UNICODE))


def is_long_reply(text: str) -> bool:
    """A dash-led paragraph too long to be trusted as a short replica."""
    return len(text.strip()) > _REPLY_MAX_CHARS


def is_plus_splice(text: str) -> bool:
    """``+ 00.43.25 …``: an editorial splice continuing the previous cut."""
    return bool(_PLUS_SPLICE_RE.match(text))


def is_section_heading(text: str) -> bool:
    """Short all-caps line without timecode, e.g. ``ПОСЛУШАНИЕ В САДУ``.

    The caller must have established that the paragraph carries no service
    label: a labelled all-caps line (``ЗК ТЕКСТ ГРОМКО``) is real speech.
    """
    stripped = text.strip()
    if not stripped or len(stripped) > _HEADING_MAX_CHARS:
        return False
    if len(stripped.split()) > _HEADING_MAX_WORDS:
        return False
    letters = [char for char in stripped if char.isalpha()]
    if len(letters) < 2 or any(char.islower() for char in letters):
        return False
    if _SENTENCE_END_RE.search(stripped) or stripped[0] in _DIALOGUE_DASHES + "+(":
        return False
    if any(char.isdigit() for char in stripped) or _CAPS_VERB_RE.search(stripped):
        return False
    return not find_timecodes(stripped)


_SENTENCE_CLOSERS = '.!?…»"”)'


def classify_section_headings(blocks: Sequence[Any], candidates: Sequence[bool]) -> list[str]:
    """Verdict per block for unlabelled short all-caps lines.

    ``candidates[i]`` says the caller already established that block ``i`` is
    an unlabelled all-caps heading-shaped line.  The verdict is ``"technical"``
    only on a strong signal, otherwise ``"ambiguous"`` (``""`` for a
    non-candidate):

    * the same text stands as its own paragraph at least twice
      (``ЛИНИЯ НИКИТЫ`` repeated through the film), or
    * the next block is an explicit scene/technical block (LIFE, SYNC, HOST,
      stand-up, a technical remark) or itself a strong heading, AND the
      previous block is complete: it ends a sentence or is technical, so the
      line does not cut a thought in half.

    A slogan between two voiceover blocks has neither signal and stays
    ambiguous: it may be spoken text.
    """
    texts = [" ".join(str(block.original_text).split()) for block in blocks]
    counts: dict[str, int] = {}
    for text in texts:
        counts[text] = counts.get(text, 0) + 1
    verdicts = [""] * len(blocks)
    for index in range(len(blocks) - 1, -1, -1):
        if not candidates[index]:
            continue
        verdicts[index] = "ambiguous"
        if counts[texts[index]] >= 2:
            verdicts[index] = "technical"
            continue
        following = blocks[index + 1] if index + 1 < len(blocks) else None
        if following is None:
            continue
        strong_next = verdicts[index + 1] == "technical" or str(following.type) in (
            SCENE_TYPES | {"technical"}
        )
        previous = blocks[index - 1] if index > 0 else None
        complete_before = previous is None or (
            str(previous.type) == "technical" or texts[index - 1].endswith(tuple(_SENTENCE_CLOSERS))
        )
        if strong_next and complete_before:
            verdicts[index] = "technical"
    return verdicts


def plan_scene_context(blocks: Sequence[Any]) -> list[SceneLink | None]:
    """Return one optional :class:`SceneLink` per block (same order)."""
    plan: list[SceneLink | None] = []
    scene_start: int | None = None
    scene_type = ""
    previous_type = ""
    for index, block in enumerate(blocks):
        link: SceneLink | None = None
        effective = str(block.type)
        if effective == "unknown" and not block.original_label:
            if is_plus_splice(block.original_text):
                if previous_type in _SPLICE_TYPES and scene_start is not None:
                    link = SceneLink(scene_start, "splice", previous_type, SPLICE_CONTINUES_WARNING)
                else:
                    link = SceneLink(-1, "splice", "", SPLICE_ORPHAN_WARNING)
            elif scene_start is not None and is_dialogue_reply(block.original_text):
                # In a LIFE scene a dash replica is part of the scene.  After
                # SYNC/HOST it may be the interviewer's question (voiceover),
                # so it is not typed automatically.
                undecided = scene_type != "life"
                warning = (
                    DASH_REPLY_UNDECIDED_WARNING.format(scene=scene_type.upper())
                    if undecided
                    else ""
                )
                if is_long_reply(block.original_text):
                    warning = LONG_REPLY_WARNING.format(scene=scene_type.upper())
                link = SceneLink(
                    scene_start,
                    "reply",
                    "" if undecided else scene_type,
                    warning,
                    scene_type if undecided else "",
                )
        elif (
            effective in SCENE_TYPES
            and scene_start is not None
            and effective == scene_type
            and getattr(block, "continuation_of_type", "") == effective
        ):
            link = SceneLink(scene_start, "continuation")
        plan.append(link)
        if link is not None and link.suggested_type:
            effective = scene_type  # undecided reply: the scene stays open
        elif link is not None and link.scene_type:
            effective = link.scene_type  # joined to the open scene
        elif link is None or link.kind != "continuation":
            if effective in SCENE_TYPES:
                scene_start, scene_type = index, effective
            else:
                scene_start, scene_type = None, ""
        previous_type = effective
    return plan


def neighbour_context(
    blocks: Sequence[Any], index: int, *, window: int = 2, max_chars: int = 120
) -> dict[str, list[dict[str, str]]]:
    """Truncated neighbours (``window`` before/after) of ``blocks[index]``."""

    def brief(block: Any) -> dict[str, str]:
        text = " ".join(str(block.original_text).split())
        # An unclassified neighbour is not a verdict: sending "unknown" made
        # the model copy it onto plain narration sitting in a run of
        # still-unresolved paragraphs.
        kind = str(block.type)
        item = {"type": "unresolved" if kind == "unknown" else kind, "text": text[:max_chars]}
        if block.original_label:
            item["label"] = str(block.original_label)
        return item

    before = [brief(b) for b in blocks[max(0, index - window) : index]]
    after = [brief(b) for b in blocks[index + 1 : index + 1 + window]]
    context: dict[str, list[dict[str, str]]] = {}
    if before:
        context["before"] = before
    if after:
        context["after"] = after
    return context
