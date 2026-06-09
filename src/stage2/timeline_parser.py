from __future__ import annotations

import re
import wave
from pathlib import Path

from .models import FullTimeline, TimelineSegment

# ЗАКАДР: text  or  ЗК: text  or  ЗК1 text  or  ЗК1: text  or  ЗК4.1 text  or  ЗК38А text
# \b after ЗАКАДР prevents false match on ЗАКАДРОВЫЙ.
# Suffix group consumes decimal and letter variants (ЗК4.1, ЗК38А) so they don't bleed into text.
_ZK_RE = re.compile(
    r"^\s*(?:ЗАКАДР\b|ЗК)(\d*(?:\.\d+)?[А-ЯЁA-Za-z]?)\s*[:\-–]?\s*(.*)",
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


def _wav_duration(path: Path) -> float:
    try:
        with wave.open(str(path), "rb") as f:
            return f.getnframes() / f.getframerate()
    except Exception:
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
            segments.append(TimelineSegment(
                id=seg_id,
                type="zk",
                text=text,
                duration_sec=duration,
                wav_path=str(wav_path) if wav_path.exists() else "",
            ))
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
            while i < len(lines):
                nxt = lines[i].strip()
                if not nxt or _is_new_segment(nxt):
                    break
                text = (text + " " + nxt).strip()
                i += 1
            ph_path = placeholder_dir / f"{seg_id}_placeholder.png"
            segments.append(TimelineSegment(
                id=seg_id, type="snh", text=text,
                duration_sec=SNH_PLACEHOLDER_DURATION,
                speaker=speaker, role=role, timecode=timecode,
                placeholder_path=str(ph_path),
            ))
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
            while i < len(lines):
                nxt = lines[i].strip()
                if not nxt or _is_new_segment(nxt):
                    break
                text = (text + " " + nxt).strip()
                i += 1
            ph_path = placeholder_dir / f"{seg_id}_placeholder.png"
            segments.append(TimelineSegment(
                id=seg_id, type="snh", text=text,
                duration_sec=SNH_PLACEHOLDER_DURATION,
                speaker=speaker, role=role, timecode=timecode,
                placeholder_path=str(ph_path),
            ))
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
            while i < len(lines):
                nxt = lines[i].strip()
                if not nxt or _is_new_segment(nxt):
                    break
                text = (text + " " + nxt).strip()
                i += 1
            ph_path = placeholder_dir / f"{seg_id}_placeholder.png"
            segments.append(TimelineSegment(
                id=seg_id, type="snh", text=text,
                duration_sec=SNH_PLACEHOLDER_DURATION,
                speaker=speaker, role=role, timecode=timecode,
                placeholder_path=str(ph_path),
            ))
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
                    while i < len(lines):
                        nxt = lines[i].strip()
                        if not nxt or _is_new_segment(nxt):
                            break
                        text = (text + " " + nxt).strip()
                        i += 1
                    ph_path = placeholder_dir / f"{seg_id}_placeholder.png"
                    segments.append(TimelineSegment(
                        id=seg_id, type="snh", text=text,
                        duration_sec=SNH_PLACEHOLDER_DURATION,
                        speaker=speaker, role=role, timecode=timecode,
                        placeholder_path=str(ph_path),
                    ))
                    continue

        # ── TECH ─────────────────────────────────────────────
        tech_counter += 1
        segments.append(TimelineSegment(
            id=f"tech{tech_counter}",
            type="tech",
            text=line,
            duration_sec=0.0,
        ))

    # Calculate timeline positions (tech = zero duration, no shift)
    cursor = 0.0
    for seg in segments:
        seg.start_sec = cursor
        if seg.type != "tech":
            cursor += seg.duration_sec

    return FullTimeline(project=project, segments=segments, fps=fps)
