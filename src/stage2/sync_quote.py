"""Scenario quote helpers shared by the sync alignment units and the LIFE assignment check."""

from __future__ import annotations

import re

from stage1.scenario_structure import find_timecodes, normalize_timecode_value

from .timeline_parser import life_parts


def _sync_prefix_end(text: str, timecode: str, timecode_end: str) -> int | None:
    """End offset of the leading timecode prefix of ``text``, or ``None``.

    Stored timecodes are normalized (``01:02:03``) while the text keeps the
    author's spelling (``01.02.03``, ``(00:42:32)``, ``[29:41.93 - 30:42.20]``),
    so the match is structural and compares normalized values.  Older projects
    stored the spot spelling; both forms normalize alike.  Only a timestamp in
    the scenario prefix is structural: one mentioned later in the quote stays
    part of the quote.
    """
    wanted = normalize_timecode_value(timecode)
    wanted_end = normalize_timecode_value(timecode_end)
    spans = find_timecodes(text, header_zone=True)
    for index, span in enumerate(spans):
        if span.start > 180:
            break
        if span.first.stored != wanted:
            continue
        end = span.end
        if span.last is None and wanted_end and index + 1 < len(spans):
            following = spans[index + 1]
            between = text[end : following.start]
            if (
                following.start - end <= 32
                and not re.search(r"[A-Za-zА-Яа-яЁё0-9]", between)
                and following.first.stored == wanted_end
            ):
                end = following.end
        return end
    # Spelling that is not a structural timecode (e.g. a legacy ``MM.SS``).
    position = text.find(timecode)
    if 0 <= position <= 180:
        end = position + len(timecode)
        if timecode_end:
            end_position = text.find(timecode_end, end, end + 32)
            if end_position >= 0 and not re.search(r"[A-Za-zА-Яа-яЁё0-9]", text[end:end_position]):
                end = end_position + len(timecode_end)
        return end
    return None


def _sync_quote(block: dict) -> str:
    """Remove a scenario speaker/role/timecode prefix before fuzzy matching."""
    text = str(block.get("normalized_text") or block.get("original_text") or "").strip()
    if not text:
        return ""

    timecode = str(block.get("timecode_start") or "").strip()
    timecode_end = str(block.get("timecode_end") or "").strip()
    if timecode:
        end = _sync_prefix_end(text, timecode, timecode_end)
        if end is not None:
            quote = text[end:].lstrip(" \t\r\n:;,.…—–-")
            if quote:
                return quote

    speaker = str(block.get("speaker") or "").strip()
    role = str(block.get("role") or "").strip()
    if speaker and text.casefold().startswith(speaker.casefold()):
        remainder = text[len(speaker) :].lstrip(" \t,")
        # A malformed legacy label can put the start of the quote into the
        # role field (``Лукьянова: начало реплики``). Its name prefix is
        # structural, while the rest must remain searchable text.
        role_prefix = role.split(":", 1)[0].strip() if ":" in role else role
        if role_prefix and remainder.casefold().startswith(role_prefix.casefold()):
            remainder = remainder[len(role_prefix) :]
        quote = remainder.lstrip(" \t\r\n:;,.…—–-")
        if quote:
            return quote
    return text


def life_unit_fragments(block: dict, *, life_only: bool = True) -> list[dict[str, str]]:
    """Independent LIFE inserts of a block, as alignment units see them.

    A ``+`` splice is stored as several inserts; a block without them (or with a single
    one) stays one unit.  Single source for ``_sync_alignment_units`` and the LIFE
    assignment carry-over check.  ``life_only=False`` skips the ``+`` splice (host blocks).
    """
    return (life_parts(block) if life_only else []) or [
        {
            "text": _sync_quote(block),
            "timecode_start": str(block.get("timecode_start") or "").strip(),
            "timecode_end": str(block.get("timecode_end") or "").strip(),
        }
    ]
