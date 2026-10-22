from __future__ import annotations

import re
import logging
import wave
from pathlib import Path
from typing import Any, Iterable

from stage1.scenario_structure import find_timecodes, iter_timecode_tokens, normalize_timecode_value

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

_COMPOUND_SPLIT = re.compile(r"\s*(?:\.{2,}|…)\s*")
_SENTENCE_END = re.compile(r"(?:(?<!\.)\.(?!\.)|[!?]+)(?=\s+[А-ЯЁA-Z])")
_ABBREVIATIONS = {"т", "е", "п", "д", "г", "гг", "ул", "обл", "им", "см", "др"}


def _merge_short_parts(parts: list[str]) -> list[str]:
    """Keep pauses and short interjections with a neighbouring speech part."""
    merged: list[str] = []
    for part in parts:
        part = part.strip()
        if not re.search(r"\w", part):
            continue
        if merged and len(part.split()) < 4:
            merged[-1] = f"{merged[-1]} {part}"
        else:
            merged.append(part)
    while len(merged) > 1 and len(merged[0].split()) < 4:
        merged[:2] = [f"{merged[0]} {merged[1]}"]
    return merged


def split_compound_quote(text: str) -> list[str]:
    """Find independently searchable phrases in a quote assembled from takes."""
    result: list[str] = []
    for portion in _merge_short_parts(_COMPOUND_SPLIT.split(text)):
        boundaries: list[int] = []
        for match in _SENTENCE_END.finditer(portion):
            if match.group() == ".":
                previous = re.search(r"(\S+)\s*$", portion[: match.start()])
                word = previous.group(1) if previous else ""
                if len(word) == 1 and word.isalpha() or word.casefold() in _ABBREVIATIONS:
                    continue
            boundaries.append(match.end())
        start = 0
        sentences: list[str] = []
        for end in boundaries:
            sentences.append(portion[start:end])
            start = end
        sentences.append(portion[start:])
        result.extend(_merge_short_parts(sentences))
    return result or ([text.strip()] if text.strip() else [])


def _merge_adjacent_sync_fragments(segments: list[TimelineSegment]) -> list[TimelineSegment]:
    """Keep neighbouring phrases from one continuous take in a single clip."""
    merged: list[TimelineSegment] = []
    for segment in segments:
        previous = merged[-1] if merged else None
        if (
            previous is not None
            and previous.type == segment.type == "snh"
            and previous.parent_block_uid
            and previous.parent_block_uid == segment.parent_block_uid
            and previous.match_status in {"auto_matched", "confirmed"}
            and segment.match_status in {"auto_matched", "confirmed"}
            and previous.source_id == segment.source_id
            and previous.source_in_sec <= segment.source_in_sec
            and previous.source_out_sec <= segment.source_out_sec
            and previous.speech_out_sec > previous.speech_in_sec
            and segment.speech_out_sec > segment.speech_in_sec
            and previous.speech_in_sec <= segment.speech_in_sec
            and previous.speech_out_sec <= segment.speech_out_sec
            and -1.5 <= segment.speech_in_sec - previous.speech_out_sec <= 1.5
        ):
            previous.text = f"{previous.text} {segment.text}"
            previous.spoken_text = f"{previous.spoken_text} {segment.spoken_text}".strip()
            previous.source_out_sec = segment.source_out_sec
            previous.speech_out_sec = segment.speech_out_sec
            previous.duration_sec = previous.source_out_sec - previous.source_in_sec
            previous.match_confidence = min(previous.match_confidence, segment.match_confidence)
            previous.timecode_end = segment.timecode_end
        else:
            merged.append(segment)
    return merged


def sync_fragment_id(block_uid: str, index: int, count: int) -> str:
    """Return a stable match id while preserving legacy ids for one-part blocks."""
    return block_uid if count <= 1 else f"{block_uid}::fragment:{index + 1}"


def normalized_sync_fragments(block: dict, *, preserve_whole: bool = False) -> list[dict[str, str]]:
    """Read canonical fragments and split unconfirmed composite quotes."""
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
    if preserve_whole:
        return fragments
    expanded: list[dict[str, str]] = []
    for fragment in fragments:
        parts = split_compound_quote(fragment["text"])
        for index, part in enumerate(parts):
            expanded.append(
                {
                    "text": part,
                    "timecode_start": fragment["timecode_start"] if index == 0 else "",
                    "timecode_end": fragment["timecode_end"] if index == 0 else "",
                }
            )
    return expanded or fragments


def life_parts(block: dict) -> list[dict[str, str]]:
    """Independent LIFE inserts of a block (a ``+`` splice), or ``[]``.

    Stored in the existing ``sync_fragments`` as ``text/timecode_start/
    timecode_end``.  Fewer than two usable parts means the legacy single-block
    behaviour, so older LIFE blocks (empty list) are read exactly as before.
    """
    if not block.get("sync_fragments"):
        return []
    parts = normalized_sync_fragments(block, preserve_whole=True)
    return parts if len(parts) > 1 else []


_LEADING_LABELS = re.compile(r"\s*(?:(?:СНХ|ЛАЙФ)\s*[,.:–-]?\s*)*", re.IGNORECASE)


def _strip_structural_timecodes(text: str, known: Iterable[str]) -> str:
    """Drop timecodes that are scenario structure, keep numbers that are speech.

    Structural: bracketed, leading (after SNH/LIFE labels) or equal to a
    timecode stored on the block (``timecode_start``/``timecode_end``/
    fragments).  ``12.05.1915`` or ``в 5:30 утра`` in a quote stay.
    """
    wanted = {normalize_timecode_value(item) for item in known if item}
    leading_match = _LEADING_LABELS.match(text)
    assert leading_match is not None  # The label prefix also matches an empty string.
    leading_end = leading_match.end()
    bracketed = {span.start: span for span in find_timecodes(text) if span.wrapper}
    pieces: list[str] = []
    cursor = 0
    for start, end, parsed in iter_timecode_tokens(text):
        if start < cursor:
            continue
        span = next((s for s in bracketed.values() if s.start <= start < s.end), None)
        if span is not None:
            start, end = span.start, span.end
        elif not (start == leading_end or parsed.stored in wanted):
            continue
        pieces.append(text[cursor:start])
        pieces.append(" ")
        cursor = end
    pieces.append(text[cursor:])
    return "".join(pieces)


def _spoken_text(
    text: str, *, block_type: str, speaker: str = "", timecodes: Iterable[str] = ()
) -> str:
    """Return speech-only text without changing the canonical scenario."""
    value = re.sub(r"\b(?:СКЛ|СКЛЕЙКА)\b", " ", text, flags=re.IGNORECASE)
    value = _strip_structural_timecodes(value, timecodes)
    life_cue = bool(re.match(r"^\s*ЛАЙФ\b", value, flags=re.IGNORECASE))
    value = re.sub(r"^\s*(?:(?:СНХ|ЛАЙФ)\s*[,.:–-]?\s*)+", "", value, flags=re.IGNORECASE)
    if life_cue:
        value = re.sub(r"^\s*«[^»]{1,80}»\s*", "", value)
    if speaker:
        value = re.sub(
            r"^\s*" + re.escape(speaker.strip()) + r"\s*[,.:–-]\s*",
            "",
            value,
            flags=re.IGNORECASE,
        )
    if block_type == "sync":
        value = re.sub(
            r"^\s*(?:отец|мать|мама|папа|брат|сестра|ведущий|ведущая)\s*:\s*",
            "",
            value,
            flags=re.IGNORECASE,
        )
    return re.sub(r"\s+", " ", value).strip(" ,.:;–—-")


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
    counters = {"voiceover": 0, "sync": 0, "host": 0, "life": 0, "tech": 0}
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
        if block.get("absorbed_by_uid"):
            continue
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
                    spoken_text=_spoken_text(
                        str(block.get("text_for_tts") or text), block_type=block_type
                    ),
                )
            )
            continue

        if block_type in {"sync", "host"}:
            prefix = "snh" if block_type == "sync" else "host"
            fragments = (
                normalized_sync_fragments(
                    block,
                    preserve_whole=str(field(matches.get(block_uid), "status", "")) == "confirmed",
                )
                if block_type == "sync"
                else [
                    {
                        "text": text,
                        "timecode_start": str(block.get("timecode_start") or "").strip(),
                        "timecode_end": str(block.get("timecode_end") or "").strip(),
                    }
                ]
            )
            for fragment_index, fragment in enumerate(fragments):
                counters[block_type] += 1
                seg_id = f"{prefix}{counters[block_type]}"
                fragment_uid = sync_fragment_id(block_uid, fragment_index, len(fragments))
                # Ordinal legacy ids (snh1, snh2, ...) become ambiguous as soon
                # as one canonical block expands into multiple timeline items.
                # Keep that fallback only for a one-part block.
                match = matches.get(fragment_uid)
                if match is None and len(fragments) == 1:
                    match = matches.get(seg_id)
                match_status = str(field(match, "status", "") or "")
                source_id = str(field(match, "source_id", "") or "")
                source = sources.get(source_id)
                source_in = float(field(match, "source_in_sec", 0.0) or 0.0)
                source_out = float(field(match, "source_out_sec", 0.0) or 0.0)
                match_metadata = field(match, "metadata", {})
                if not isinstance(match_metadata, dict):
                    match_metadata = {}
                speech_in = float(match_metadata.get("speech_in_sec", source_in) or source_in)
                speech_out = float(match_metadata.get("speech_out_sec", source_out) or source_out)
                if match_status == "confirmed" and match_metadata.get("retime_pending_review"):
                    # Word times were retimed under a kept manual range: not admitted to
                    # the build until the editor re-confirms (which clears the flag).
                    match_status = "needs_review"
                matched = (
                    match_status in {"auto_matched", "confirmed"}
                    and source_id
                    and source_out > source_in >= 0
                )
                segments.append(
                    TimelineSegment(
                        id=seg_id,
                        type="snh" if block_type == "sync" else "host",
                        text=fragment["text"],
                        duration_sec=(
                            source_out - source_in if matched else SNH_PLACEHOLDER_DURATION
                        ),
                        block_uid=fragment_uid,
                        parent_block_uid=block_uid,
                        revision_status=revision_status,
                        speaker=str(
                            block.get("speaker") or block.get("original_label") or prefix
                        ).strip(),
                        role=str(
                            block.get("role") or ("СНХ" if block_type == "sync" else "ВЕДУЩИЙ")
                        ).strip(),
                        timecode=fragment["timecode_start"],
                        timecode_end=fragment["timecode_end"],
                        source_id=source_id,
                        sequence_name=str(
                            field(source, "premiere_sequence", field(source, "sequence_name", ""))
                            or ""
                        ),
                        sequence_id=str(field(source, "sequence_id", "") or ""),
                        source_in_sec=source_in,
                        source_out_sec=source_out,
                        speech_in_sec=speech_in,
                        speech_out_sec=speech_out,
                        match_status=match_status,
                        match_confidence=float(field(match, "confidence", 0.0) or 0.0),
                        placeholder_path=str(placeholder_dir / f"{seg_id}_placeholder.png"),
                        spoken_text=_spoken_text(
                            fragment["text"],
                            block_type=block_type,
                            speaker=str(block.get("speaker") or ""),
                            timecodes=(
                                str(fragment.get("timecode_start") or ""),
                                str(fragment.get("timecode_end") or ""),
                                str(block.get("timecode_start") or ""),
                                str(block.get("timecode_end") or ""),
                            ),
                        ),
                    )
                )
            continue

        if block_type == "life":
            parts = life_parts(block)
            for part_index, part in enumerate(
                parts
                or [
                    {
                        "text": text,
                        "timecode_start": str(block.get("timecode_start") or "").strip(),
                        "timecode_end": str(block.get("timecode_end") or "").strip(),
                    }
                ]
            ):
                counters["life"] += 1
                seg_id = f"life{counters['life']}"
                segments.append(
                    TimelineSegment(
                        id=seg_id,
                        type="life",
                        text=part["text"],
                        duration_sec=SNH_PLACEHOLDER_DURATION,
                        block_uid=(
                            sync_fragment_id(block_uid, part_index, len(parts))
                            if parts
                            else block_uid
                        ),
                        parent_block_uid=block_uid if parts else "",
                        revision_status=revision_status,
                        speaker="ЛАЙФ",
                        role=str(block.get("source_description") or "ОРИГИНАЛЬНЫЙ ЗВУК").strip(),
                        timecode=part["timecode_start"],
                        timecode_end=part["timecode_end"],
                        placeholder_path=str(placeholder_dir / f"{seg_id}_placeholder.png"),
                        spoken_text="",
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

    segments = _merge_adjacent_sync_fragments(segments)
    sync_ordinal = 0
    for segment in segments:
        if segment.type == "snh":
            sync_ordinal += 1
            segment.id = f"snh{sync_ordinal}"
            if segment.placeholder_path:
                segment.placeholder_path = str(
                    Path(segment.placeholder_path).with_name(f"{segment.id}_placeholder.png")
                )
    cursor = 0.0
    for segment in segments:
        segment.start_sec = cursor
        if segment.type != "tech":
            cursor += segment.duration_sec
    return FullTimeline(project=project, segments=segments, fps=fps)
