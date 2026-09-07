from __future__ import annotations

import re
import logging
import wave
from pathlib import Path
from typing import Any

from .models import FullTimeline, TimelineSegment

logger = logging.getLogger(__name__)

# ЗАКАДР: text  or  ЗК: text  or  ЗК1 text  or  ЗК1: text  or  ЗК4.1 text  or  ЗК38А text
# \b after ЗАКАДР prevents false match on ЗАКАДРОВЫЙ.
# Suffix group consumes decimal and letter variants (ЗК4.1, ЗК38А) so they don't bleed into text.
_ZK_RE = re.compile(
    r"^\s*(?:ЗАКАДР\b|ЗК)(\d*(?:\.\d+)?[А-ЯЁA-Za-z]?)"
    r"(?=\s|[:\-–]|$)\s*[:\-–]?\s*(.*)",
    re.IGNORECASE | re.DOTALL,
)

# Prefixed SNH: "СНХ NAME, ROLE, HH:MM:SS text"  (comma before timecode)
_SNH_PREFIXED_RE = re.compile(
    r"^\s*СНХ\s+([А-ЯЁA-Z][А-ЯЁа-яёA-Za-z\s\-]{1,60}),\s*([^,]{2,60}),\s*(\d{2}[:.]\d{2}(?:[:.]\d{2})?)[.\s]*(.*)",
    re.DOTALL,
)

# Classic SNH: "NAME, ROLE: HH:MM:SS text"
_SNH_RE = re.compile(
    r"^([А-ЯЁA-Z][А-ЯЁа-яёA-Za-z\s\-]{2,60}),\s*([^:]{2,60}):\s*(\d{2}[:.]\d{2}[:.]\d{2})[.\s]*(.*)",
    re.DOTALL,
)

# Timecode pattern (MM.SS, HH.MM.SS, HH:MM:SS, etc.)
_TC_PAT = r"\d{1,2}[:.]\d{2}(?:[:.]\d{2})?"

# Flexible SNH: "NAME[_N][(ROLE)] TIMECODE[.] text"  (name and timecode on same line)
_SNH_NAME_PAT = r"([А-ЯЁ]{2,}(?:\s[А-ЯЁ]{2,})+(?:_\d+)?)\s*(?:\(([^)]*)\))?"
_SNH_FLEX_RE = re.compile(
    r"^" + _SNH_NAME_PAT + r"\s*:?\s*(" + _TC_PAT + r")[.\s–\-]*(.*)",
    re.DOTALL,
)

# SNH name only on its own line (timecode will be on next line)
_SNH_NAME_ONLY_RE = re.compile(r"^" + _SNH_NAME_PAT + r"\s*$")

# A standalone timecode line: "MM.SS. text" or "HH:MM:SS text"
_TIMECODE_LINE_RE = re.compile(r"^(" + _TC_PAT + r")[.\s–\-]*(.*)", re.DOTALL)

SNH_PLACEHOLDER_DURATION = 10.0  # seconds

# A part shorter than this many words is treated as a speech pause ("Ну... я
# думаю") rather than a deliberate splice between two distant source cuts, so
# it gets glued back onto its neighbour instead of becoming its own fragment.
_ELLIPSIS_MIN_WORDS = 4

# Matches "..." (any run of 2+ dots) or the single-character "…", with
# optional surrounding whitespace, as a splice marker between spliced parts
# of a sync/host line.
_ELLIPSIS_SPLIT_RE = re.compile(r"\s*(?:\.{2,}|…)\s*")

# Matches at least one alphanumeric character (Latin, Cyrillic, or digit)
_ALPHANUMERIC_RE = re.compile(r"[0-9A-Za-zА-Яа-яЁё]")

# Common Russian abbreviations whose trailing "." must not be treated as a
# sentence boundary (т. д., т. е., т. п., г. Минск, ...). Compared against
# the bare token (without the trailing dot) preceding a candidate boundary.
_SENTENCE_ABBREVIATIONS = {
    "т", "е", "п", "д", "г", "гг", "ул", "обл", "им", "см", "кг", "руб",
    "стр", "рис", "проф", "акад", "др", "пр",
}

# Candidate sentence-terminator: a single "." (an ellipsis of 2+ dots is
# handled separately by split_text_by_ellipsis and must not be re-split
# here) or a run of "!"/"?", followed by whitespace and either end-of-string
# or an uppercase letter (Cyrillic or Latin) starting the next sentence.
# Decimal numbers ("1.5") never match: the digit that follows a decimal dot
# is not whitespace + an uppercase letter, so the lookahead fails on its own.
_SENTENCE_TERMINATOR_RE = re.compile(r"(?:(?<!\.)\.(?!\.)|[!?]+)(?=\s+[А-ЯЁA-Z]|\s*$)")

# Maximum silence gap (seconds) between the source end of one matched
# sync/host fragment and the source start of the very next fragment of the
# *same* canonical block for them to be treated as one continuous take and
# collapsed into a single timeline item instead of two adjacent clips. This
# approximates a natural breath/sentence pause for a single uninterrupted
# take. Fragments that were deliberately spliced from different moments of
# the source (a scriptwriter's ellipsis/sentence split across takes) are
# measured, on real projects, 3.3-28.9s apart and must stay separate clips,
# well above this threshold.
_FRAGMENT_MERGE_MAX_GAP_SEC = 1.5


def _is_abbreviation_or_initial(word: str) -> bool:
    """True if `word` (the token right before a candidate '.') is an
    abbreviation ("т", "г", ...) or a single-letter initial ("А", "С")."""
    w = word.strip()
    if not w:
        return False
    if len(w) == 1 and w.isalpha():
        return True
    return w.lower() in _SENTENCE_ABBREVIATIONS


def _split_on_sentence_boundaries(text: str) -> list[str]:
    """Split `text` at accepted sentence-terminator positions, keeping the
    terminating punctuation attached to the preceding part."""
    boundaries: list[int] = []
    for m in _SENTENCE_TERMINATOR_RE.finditer(text):
        if m.group() == ".":
            prefix = text[: m.start()]
            word_match = re.search(r"(\S+)\s*$", prefix)
            word = word_match.group(1) if word_match else ""
            if _is_abbreviation_or_initial(word):
                continue
        boundaries.append(m.end())
    if not boundaries:
        return [text]
    parts: list[str] = []
    prev = 0
    for end in boundaries:
        parts.append(text[prev:end])
        prev = end
    tail = text[prev:]
    if tail.strip():
        parts.append(tail)
    return parts


def sync_fragment_id(block_uid: str, index: int, count: int) -> str:
    """Return a stable match id while preserving legacy ids for one-part blocks."""
    return block_uid if count <= 1 else f"{block_uid}::fragment:{index + 1}"


def _group_adjacent_matched_fragments(items: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Cluster consecutive matched raw fragment records of one canonical
    block that belong to a single uninterrupted take on the source tape.

    A scenario line is split (by `normalized_sync_fragments`) into several
    independently matchable sub-fragments, but a script writer's cut is
    often silent — the speaker never actually paused. Two consecutive
    fragments are folded into one cluster when both are matched, share the
    same `source_id`, keep source order (both start and end monotonic —
    the next fragment does not start before the previous one did, and does
    not end before the previous one did), and the silence between them is
    bounded: gap must satisfy -_FRAGMENT_MERGE_MAX_GAP_SEC <= gap <=
    _FRAGMENT_MERGE_MAX_GAP_SEC (where gap = next.source_in - prev.source_out).
    A small negative gap — mild padding overlap from the matcher — counts as
    adjacent; a large negative gap (strong overlap) indicates a nesting error
    and blocks merge. Grouping is transitive: three consecutive adjacent
    fragments collapse into a single cluster. Unmatched fragments, fragments
    separated by a gap, or fragments breaking monotonicity, start a new
    cluster of their own.
    """
    groups: list[list[dict[str, Any]]] = []
    for item in items:
        prev = groups[-1][-1] if groups else None
        gap = item["source_in_sec"] - prev["source_out_sec"] if prev else 0
        if (
            prev is not None
            and item["matched"]
            and prev["matched"]
            and item["source_id"] == prev["source_id"]
            and item["source_in_sec"] >= prev["source_in_sec"]
            and item["source_out_sec"] >= prev["source_out_sec"]
            and -_FRAGMENT_MERGE_MAX_GAP_SEC <= gap <= _FRAGMENT_MERGE_MAX_GAP_SEC
        ):
            groups[-1].append(item)
        else:
            groups.append([item])
    return groups


def split_text_by_ellipsis(text: str) -> list[str]:
    """Split a spliced SNH/host line into independently matchable parts.

    Scriptwriters glue speech taken from different moments of the source
    into a single line, separating the pieces with an ellipsis ("..." or
    "…"). Each such piece can come from anywhere in the source (including
    out of chronological order), so it must be matched on its own rather
    than as one continuous run of words. A short piece (fewer than
    ``_ELLIPSIS_MIN_WORDS`` words) is instead treated as a mid-sentence
    pause and glued back onto its neighbour, restoring the "..." separator
    so no text is lost.

    Only parts containing at least one alphanumeric character are considered
    meaningful. Empty strings or punctuation-only parts are filtered out.
    If no meaningful parts remain, returns an empty list.
    """
    raw_parts = [part.strip() for part in _ELLIPSIS_SPLIT_RE.split(text)]
    # Filter to keep only parts with at least one alphanumeric character
    parts = [part for part in raw_parts if part and _ALPHANUMERIC_RE.search(part)]
    if len(parts) == 0:
        return []
    if len(parts) <= 1:
        return [text.strip()] if _ALPHANUMERIC_RE.search(text) else []

    merged: list[str] = []
    for part in parts:
        if len(part.split()) < _ELLIPSIS_MIN_WORDS and merged:
            merged[-1] = f"{merged[-1]}... {part}"
        else:
            merged.append(part)

    # A short leading part has no previous piece to attach to yet; glue it
    # onto the part that follows instead.
    while len(merged) > 1 and len(merged[0].split()) < _ELLIPSIS_MIN_WORDS:
        first, second, *rest = merged
        merged = [f"{first}... {second}", *rest]

    return merged


def split_text_by_sentences(text: str) -> list[str]:
    """Split a (post-ellipsis) sync/host part on sentence boundaries.

    Scriptwriters also glue whole sentences spoken at different moments of
    the source into one line without an ellipsis. This splits on
    ./!/? followed by whitespace and a capital letter (or end of string),
    while not splitting on abbreviations ("т. д.", "т. е.", "г. Минск"),
    initials ("А. С. Пушкин") or decimal numbers ("1.5") and without
    touching ellipsis splice markers (handled separately).

    A part shorter than ``_ELLIPSIS_MIN_WORDS`` words is glued back onto its
    neighbour (previous, or next if it is the first part) so no meaningful
    text is dropped as its own fragment.

    Returns the original text as a single part if no split was possible, or
    an empty list if the text has no meaningful (alphanumeric) content.
    """
    raw_parts = _split_on_sentence_boundaries(text)
    parts = [part for part in (p.strip() for p in raw_parts) if part and _ALPHANUMERIC_RE.search(part)]
    if len(parts) == 0:
        return []
    if len(parts) <= 1:
        return [text.strip()] if _ALPHANUMERIC_RE.search(text) else []

    merged: list[str] = []
    for part in parts:
        if len(part.split()) < _ELLIPSIS_MIN_WORDS and merged:
            merged[-1] = f"{merged[-1]} {part}"
        else:
            merged.append(part)

    while len(merged) > 1 and len(merged[0].split()) < _ELLIPSIS_MIN_WORDS:
        first, second, *rest = merged
        merged = [f"{first} {second}", *rest]

    return merged


def normalized_sync_fragments(block: dict) -> list[dict[str, str]]:
    """Read canonical speaker fragments with a backward-compatible fallback.

    Each stored fragment is further split, in cascade, first on ellipsis
    splice markers and then each resulting part on sentence boundaries,
    into a flat list of independently matchable sub-fragments; every
    sub-fragment inherits its parent's timecodes as a hint for the sync
    matcher. Order is preserved throughout.

    Parts containing only punctuation or whitespace are filtered out.
    If no meaningful sub-fragments remain after splitting, and no canonical
    fragments were provided, a single fragment with empty text is returned
    for backward compatibility: the calling code (sync_fragment_id in api.py)
    expects at least one result to avoid unexpected empty lists.
    """
    raw_fragments = block.get("sync_fragments")
    fragments: list[dict[str, str]] = []
    if isinstance(raw_fragments, list):
        for value in raw_fragments:
            if not isinstance(value, dict):
                continue
            text = str(value.get("text") or "").strip()
            if not text:
                continue
            fragments.append(
                {
                    "text": text,
                    "timecode_start": str(value.get("timecode_start") or "").strip(),
                    "timecode_end": str(value.get("timecode_end") or "").strip(),
                }
            )
    if not fragments:
        fragments = [
            {
                "text": str(
                    block.get("normalized_text") or block.get("original_text") or ""
                ).strip(),
                "timecode_start": str(block.get("timecode_start") or "").strip(),
                "timecode_end": str(block.get("timecode_end") or "").strip(),
            }
        ]

    expanded: list[dict[str, str]] = []
    for fragment in fragments:
        # First split spliced parts (ellipsis), then split each part on
        # sentence boundaries, preserving order end-to-end.
        for ellipsis_part in split_text_by_ellipsis(fragment["text"]):
            for part in split_text_by_sentences(ellipsis_part):
                expanded.append(
                    {
                        "text": part,
                        "timecode_start": fragment["timecode_start"],
                        "timecode_end": fragment["timecode_end"],
                    }
                )

    # Backward compatibility: if no meaningful fragments were produced,
    # return at least one (with empty text) to avoid unexpected empty lists
    # in calling code that assumes at least one fragment per block.
    if not expanded:
        expanded = [
            {
                "text": "",
                "timecode_start": fragments[0]["timecode_start"] if fragments else "",
                "timecode_end": fragments[0]["timecode_end"] if fragments else "",
            }
        ]

    return expanded


def _wav_duration(path: Path) -> float:
    try:
        with wave.open(str(path), "rb") as f:
            return f.getnframes() / f.getframerate()
    except Exception as exc:
        logger.warning("Could not read WAV duration path=%s error=%s", path, exc)
        return 0.0


def _strip_extra_timecodes(text: str) -> str:
    """Remove inline extra timecodes like '00:28:12.' from speaker text."""
    return re.sub(r"\s*\d{2}[:.]\d{2}[:.]\d{2}\.?\s*", " ", text).strip()


def _is_new_segment(line: str) -> bool:
    """True if line starts a new ZK or SNH segment."""
    return bool(
        _ZK_RE.match(line)
        or _SNH_PREFIXED_RE.match(line)
        or _SNH_RE.match(line)
        or _SNH_FLEX_RE.match(line)
        or _SNH_NAME_ONLY_RE.match(line)
    )


def _collect_snh_continuation(
    lines: list[str],
    start_index: int,
    text: str,
) -> tuple[str, int]:
    """Join continuation lines until a blank line or the next segment marker."""
    i = start_index
    while i < len(lines):
        nxt = lines[i].strip()
        if not nxt or _is_new_segment(nxt):
            break
        text = (text + " " + nxt).strip()
        i += 1
    return text, i


def _append_snh_segment(
    segments: list[TimelineSegment],
    placeholder_dir: Path,
    seg_id: str,
    *,
    speaker: str,
    role: str,
    timecode: str,
    text: str,
) -> None:
    segments.append(
        TimelineSegment(
            id=seg_id,
            type="snh",
            text=text,
            duration_sec=SNH_PLACEHOLDER_DURATION,
            speaker=speaker,
            role=role,
            timecode=timecode,
            placeholder_path=str(placeholder_dir / f"{seg_id}_placeholder.png"),
        )
    )


def parse_timeline(
    raw_text: str,
    speaker_dir: Path,
    placeholder_dir: Path,
    project: str,
    fps: int = 25,
) -> FullTimeline:
    segments: list[TimelineSegment] = []
    zk_counter = 0
    snh_counter = 0
    tech_counter = 0

    lines = raw_text.splitlines()
    i = 0

    while i < len(lines):
        line = lines[i].strip()
        i += 1

        if not line:
            continue

        # ── ZK ──────────────────────────────────────────────
        m_zk = _ZK_RE.match(line)
        if m_zk:
            zk_counter += 1
            seg_id = f"zk{zk_counter}"
            text_parts = [m_zk.group(2).strip()]
            # Collect continuation lines (multiline ЗАКАДР block)
            while i < len(lines):
                nxt = lines[i].strip()
                if not nxt or _is_new_segment(nxt):
                    break
                text_parts.append(nxt)
                i += 1
            text = " ".join(p for p in text_parts if p)
            wav_path = speaker_dir / f"{seg_id}.wav"
            duration = _wav_duration(wav_path) if wav_path.exists() else 0.0
            segments.append(
                TimelineSegment(
                    id=seg_id,
                    type="zk",
                    text=text,
                    duration_sec=duration,
                    wav_path=str(wav_path) if wav_path.exists() else "",
                )
            )
            continue

        # ── SNH prefixed: "СНХ NAME, ROLE, TC text" ─────────
        m_prefixed = _SNH_PREFIXED_RE.match(line)
        if m_prefixed:
            snh_counter += 1
            seg_id = f"snh{snh_counter}"
            speaker = m_prefixed.group(1).strip()
            role = m_prefixed.group(2).strip()
            timecode = m_prefixed.group(3).strip()
            text = _strip_extra_timecodes(m_prefixed.group(4).strip())
            text, i = _collect_snh_continuation(lines, i, text)
            _append_snh_segment(
                segments,
                placeholder_dir,
                seg_id,
                speaker=speaker,
                role=role,
                timecode=timecode,
                text=text,
            )
            continue

        # ── SNH ─────────────────────────────────────────────
        m_snh = _SNH_RE.match(line)
        if m_snh:
            snh_counter += 1
            seg_id = f"snh{snh_counter}"
            speaker = m_snh.group(1).strip()
            role = m_snh.group(2).strip()
            timecode = m_snh.group(3).strip()
            text = _strip_extra_timecodes(m_snh.group(4).strip())
            text, i = _collect_snh_continuation(lines, i, text)
            _append_snh_segment(
                segments,
                placeholder_dir,
                seg_id,
                speaker=speaker,
                role=role,
                timecode=timecode,
                text=text,
            )
            continue

        # ── SNH FLEX: "NAME[(ROLE)] TIMECODE text" ───────────
        m_flex = _SNH_FLEX_RE.match(line)
        if m_flex:
            snh_counter += 1
            seg_id = f"snh{snh_counter}"
            speaker = m_flex.group(1).strip()
            role = (m_flex.group(2) or "СНХ").strip()
            timecode = m_flex.group(3).strip()
            text = _strip_extra_timecodes(m_flex.group(4).strip())
            text, i = _collect_snh_continuation(lines, i, text)
            _append_snh_segment(
                segments,
                placeholder_dir,
                seg_id,
                speaker=speaker,
                role=role,
                timecode=timecode,
                text=text,
            )
            continue

        # ── SNH NAME ONLY: name on line, timecode on next line ─
        m_name = _SNH_NAME_ONLY_RE.match(line)
        if m_name:
            # Peek at next non-empty line — must be a timecode
            j = i
            while j < len(lines) and not lines[j].strip():
                j += 1
            if j < len(lines):
                nxt_line = lines[j].strip()
                m_tc = _TIMECODE_LINE_RE.match(nxt_line)
                if m_tc:
                    i = j + 1  # consume the timecode line
                    snh_counter += 1
                    seg_id = f"snh{snh_counter}"
                    speaker = m_name.group(1).strip()
                    role = (m_name.group(2) or "СНХ").strip()
                    timecode = m_tc.group(1).strip()
                    text = _strip_extra_timecodes(m_tc.group(2).strip())
                    text, i = _collect_snh_continuation(lines, i, text)
                    _append_snh_segment(
                        segments,
                        placeholder_dir,
                        seg_id,
                        speaker=speaker,
                        role=role,
                        timecode=timecode,
                        text=text,
                    )
                    continue

        # ── TECH ─────────────────────────────────────────────
        tech_counter += 1
        segments.append(
            TimelineSegment(
                id=f"tech{tech_counter}",
                type="tech",
                text=line,
                duration_sec=0.0,
            )
        )

    # Calculate timeline positions (tech = zero duration, no shift)
    cursor = 0.0
    for seg in segments:
        seg.start_sec = cursor
        if seg.type != "tech":
            cursor += seg.duration_sec

    return FullTimeline(project=project, segments=segments, fps=fps)


def parse_normalized_timeline(
    normalization: dict,
    speaker_dir: Path,
    placeholder_dir: Path,
    project: str,
    fps: int = 25,
    sync_matches: dict[str, object] | None = None,
    sync_sources: dict[str, object] | None = None,
) -> FullTimeline:
    """Build a timeline directly from a confirmed canonical normalization."""
    if str(normalization.get("status", "")).lower() != "confirmed":
        raise ValueError("normalized scenario must be confirmed")

    segments: list[TimelineSegment] = []
    counters = {"voiceover": 0, "sync": 0, "host": 0, "tech": 0}
    blocks = sorted(
        normalization.get("blocks", []),
        key=lambda item: int(item.get("order", 0) or 0),
    )
    matches = sync_matches or {}
    sources = sync_sources or {}

    def field(item: object | None, name: str, default: Any = "") -> Any:
        if item is None:
            return default
        if isinstance(item, dict):
            return item.get(name, default)
        return getattr(item, name, default)

    for block in blocks:
        block_type = str(block.get("type", "unknown")).lower()
        text = str(block.get("normalized_text") or block.get("original_text") or "").strip()
        block_uid = str(block.get("uid") or "").strip()
        versioning = block.get("versioning")
        revision_status = str(
            versioning.get("change_status") if isinstance(versioning, dict) else ""
        ).strip()

        if block_type == "voiceover":
            counters["voiceover"] += 1
            seg_id = f"zk{counters['voiceover']}"
            wav_path = speaker_dir / f"{seg_id}.wav"
            segments.append(
                TimelineSegment(
                    id=seg_id,
                    type="zk",
                    text=text,
                    duration_sec=_wav_duration(wav_path) if wav_path.exists() else 0.0,
                    block_uid=block_uid,
                    revision_status=revision_status,
                    wav_path=str(wav_path) if wav_path.exists() else "",
                )
            )
            continue

        if block_type in {"sync", "host"}:
            prefix = "snh" if block_type == "sync" else "host"
            fragments = (
                normalized_sync_fragments(block)
                if block_type == "sync"
                else [
                    {
                        "text": text,
                        "timecode_start": str(block.get("timecode_start") or "").strip(),
                        "timecode_end": str(block.get("timecode_end") or "").strip(),
                    }
                ]
            )
            raw_items: list[dict[str, Any]] = []
            for fragment_index, fragment in enumerate(fragments):
                fragment_uid = sync_fragment_id(block_uid, fragment_index, len(fragments))
                # Ordinal legacy ids (snh1, snh2, ...) become ambiguous as soon
                # as one canonical block expands into multiple timeline items.
                # Keep that fallback only for a one-part block. The tentative
                # id mirrors the ordinal position this fragment would get if
                # counted sequentially from here — the same id it always got
                # before fragment merging existed (merging never applies to a
                # one-part block, so it stays accurate).
                tentative_seg_id = f"{prefix}{counters[block_type] + fragment_index + 1}"
                match = matches.get(fragment_uid)
                if match is None and len(fragments) == 1:
                    match = matches.get(tentative_seg_id)
                match_status = str(field(match, "status", "") or "")
                source_id = str(field(match, "source_id", "") or "")
                source_in = float(field(match, "source_in_sec", 0.0) or 0.0)
                source_out = float(field(match, "source_out_sec", 0.0) or 0.0)
                matched = (
                    match_status in {"auto_matched", "confirmed"}
                    and source_id
                    and source_out > source_in >= 0
                )
                raw_items.append(
                    {
                        "fragment_uid": fragment_uid,
                        "fragment_index": fragment_index,
                        "text": fragment["text"],
                        "timecode_start": fragment["timecode_start"],
                        "timecode_end": fragment["timecode_end"],
                        "match_status": match_status,
                        "source_id": source_id,
                        "source": sources.get(source_id),
                        "source_in_sec": source_in,
                        "source_out_sec": source_out,
                        "matched": matched,
                        "confidence": float(field(match, "confidence", 0.0) or 0.0),
                    }
                )

            for group in _group_adjacent_matched_fragments(raw_items):
                counters[block_type] += 1
                seg_id = f"{prefix}{counters[block_type]}"
                first, last = group[0], group[-1]
                matched = first["matched"]
                if len(group) == 1:
                    merged_block_uid = first["fragment_uid"]
                else:
                    merged_block_uid = (
                        f"{block_uid}::fragment:"
                        f"{first['fragment_index'] + 1}-{last['fragment_index'] + 1}"
                    )
                text = " ".join(item["text"] for item in group)
                source = first["source"]
                segments.append(
                    TimelineSegment(
                        id=seg_id,
                        type="snh" if block_type == "sync" else "host",
                        text=text,
                        duration_sec=(
                            last["source_out_sec"] - first["source_in_sec"]
                            if matched
                            else SNH_PLACEHOLDER_DURATION
                        ),
                        block_uid=merged_block_uid,
                        parent_block_uid=block_uid,
                        revision_status=revision_status,
                        speaker=str(
                            block.get("speaker") or block.get("original_label") or prefix
                        ).strip(),
                        role=str(
                            block.get("role") or ("СНХ" if block_type == "sync" else "ВЕДУЩИЙ")
                        ).strip(),
                        timecode=first["timecode_start"],
                        timecode_end=last["timecode_end"],
                        source_id=first["source_id"],
                        sequence_name=str(
                            field(source, "premiere_sequence", field(source, "sequence_name", ""))
                            or ""
                        ),
                        sequence_id=str(field(source, "sequence_id", "") or ""),
                        source_in_sec=first["source_in_sec"],
                        source_out_sec=last["source_out_sec"],
                        match_status=first["match_status"],
                        match_confidence=sum(item["confidence"] for item in group) / len(group),
                        placeholder_path=str(placeholder_dir / f"{seg_id}_placeholder.png"),
                    )
                )
            continue

        counters["tech"] += 1
        segments.append(
            TimelineSegment(
                id=f"tech{counters['tech']}",
                type="tech",
                text=text,
                duration_sec=0.0,
                block_uid=block_uid,
                revision_status=revision_status,
            )
        )

    cursor = 0.0
    for segment in segments:
        segment.start_sec = cursor
        if segment.type != "tech":
            cursor += segment.duration_sec
    return FullTimeline(project=project, segments=segments, fps=fps)
