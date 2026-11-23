from __future__ import annotations

import hashlib
import math
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class TranscriptCue:
    id: str
    source_id: str
    speaker: str
    russian_text: str
    original_text: str
    start_sec: float
    end_sec: float
    approximate: bool
    origin_index: int
    raw_start_tc: str | None = None
    raw_end_tc: str | None = None
    explicit_end: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "TranscriptCue":
        return cls(**{key: value[key] for key in cls.__dataclass_fields__ if key in value})


_TIME = r"\d{1,2}:\d{2}(?::\d{2}(?::\d{2})?)?(?:\.\d+)?"
_TC = re.compile(
    rf"^\s*\[(?P<approx>~)?(?P<a>{_TIME})(?:\s*[-–—]\s*(?P<b>{_TIME}))?\]\s*(?P<text>.*)$"
)
_BAD_BRACKET = re.compile(r"^\s*\[[^]]+\]")
_ROLE = re.compile(r"^(?:ОТВЕТ|ВОПРОС|РЕПЛИКА)\s+(?P<speaker>.+)$", re.I)
_PRONUNCIATION = re.compile(r"^\s*Транскрипция кириллицей\s*\(приблизительно\)\s*:", re.I)
_EXPLICIT = re.compile(
    r"^\s*(?P<label>перевод|оригинал|translation|original)\s*[:—-]\s*(?P<text>.*)$", re.I
)


def _seconds(tc: str, fps: float) -> float:
    parts = tc.split(":")
    try:
        if len(parts) == 2:
            minute, second = int(parts[0]), float(parts[1])
            if second >= 60:
                raise ValueError(f"Malformed transcript timecode: {tc}")
            result = minute * 60 + second
        elif len(parts) == 4:
            h, m, s, frame = parts
            minute = int(m)
            second = int(s)
            frame_value = float(frame)
            if minute >= 60 or second >= 60 or frame_value >= fps or not frame_value.is_integer():
                raise ValueError(f"Malformed transcript timecode: {tc}")
            result = int(h) * 3600 + minute * 60 + second + frame_value / fps
        elif len(parts) == 3:
            h, m, s = parts
            minute = int(m)
            second = float(s)
            if minute >= 60 or second >= 60:
                raise ValueError(f"Malformed transcript timecode: {tc}")
            result = int(h) * 3600 + minute * 60 + second
        else:
            raise ValueError
    except ValueError as exc:
        raise ValueError(f"Malformed transcript timecode: {tc}") from exc
    if not math.isfinite(result):
        raise ValueError(f"Malformed transcript timecode: {tc}")
    return result


def _blocks(container):
    """Yield paragraphs and tables in their actual XML order, recursively."""
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    parent = (
        container.element.body
        if hasattr(container, "element") and hasattr(container.element, "body")
        else container._tc
    )
    for child in parent.iterchildren():
        if child.tag.endswith("}p"):
            yield Paragraph(child, container)
        elif child.tag.endswith("}tbl"):
            table = Table(child, container)
            for row in table.rows:
                for cell in row.cells:
                    yield from _blocks(cell)


def parse_docx_transcript(
    path: Path,
    source_id: str,
    *,
    duration_sec: float,
    fps: float = 25.0,
    timecode_mode: str = "relative",
    sequence_start_sec: float = 0.0,
    guide_offset_sec: float = 0.0,
) -> list[TranscriptCue]:
    from docx import Document

    if not source_id.strip():
        raise ValueError("source_id must not be empty")
    if not math.isfinite(duration_sec) or duration_sec <= 0:
        raise ValueError("duration_sec must be positive and finite")
    if not math.isfinite(fps) or fps <= 0:
        raise ValueError("fps must be positive and finite")
    if not math.isfinite(sequence_start_sec) or timecode_mode not in {"relative", "display"}:
        raise ValueError("invalid timecode mode or sequence start")

    doc = Document(str(path))
    paragraphs = [p for p in _blocks(doc) if p.text.strip()]
    media_extensions = {".wav", ".mp3", ".m4a", ".aif", ".aiff", ".mp4", ".mov", ".mxf"}
    media_headings = {
        p.text.strip()
        for p in paragraphs
        if p.style is not None
        and p.style.name.lower().startswith("heading")
        and Path(p.text.strip()).suffix.lower() in media_extensions
    }
    if len(media_headings) > 1:
        raise ValueError("DOCX contains multiple media sources; provide a source-specific export")
    records: list[dict[str, Any]] = []
    timed_anchors: list[float] = []
    speaker = ""
    pending: dict[str, Any] | None = None
    current_anchor: float | None = None
    current_raw_start: str | None = None
    current_raw_end: str | None = None
    plain_group: dict[str, Any] | None = None

    def flush_translation(text: str, index: int, *, approximate: bool | None = None) -> None:
        nonlocal pending
        if pending is None:
            return
        pending["russian"] = text.strip()
        if approximate is not None:
            pending["approximate"] = pending["approximate"] or approximate
        pending["index"] = index
        records.append(pending)
        pending = None

    for index, paragraph in enumerate(paragraphs):
        line = paragraph.text.strip()
        if _PRONUNCIATION.match(line):
            continue
        if re.match(r"^(?:источник|source)\s*[:—-]", line, re.I):
            continue

        role = _ROLE.match(line.split("\n", 1)[0])
        if role and "\n" in line:
            header, remainder = line.split("\n", 1)
            role = _ROLE.match(header.strip())
            if role:
                speaker = role.group("speaker").strip()
                line = remainder.strip()
                role = None
        tc = _TC.match(line)
        if tc:
            plain_group = None
            if pending is not None:
                # Original-only text is retained until this next anchor; it does
                # not become a Russian cue by itself.
                pending = None
            raw_tc = tc.group("a")
            start = _seconds(raw_tc, fps)
            end = _seconds(tc.group("b"), fps) if tc.group("b") else None
            if timecode_mode == "display":
                start -= sequence_start_sec
                if end is not None:
                    end -= sequence_start_sec
            text = tc.group("text").strip()
            if end is not None and (end < start or end > duration_sec):
                raise ValueError("Transcript timecode range is outside source duration")
            if start < 0 or start > duration_sec:
                raise ValueError("Transcript timecode is outside source duration")
            current_anchor = start
            current_raw_start, current_raw_end = raw_tc, tc.group("b")
            timed_anchors.append(start)
            if role:
                speaker = role.group("speaker").strip()
            if (
                text
                and re.search(r"[А-Яа-яЁё]", text)
                and not any(ch.isalpha() and not re.match(r"[А-Яа-яЁё]", ch) for ch in text)
            ):
                records.append(
                    {
                        "speaker": speaker,
                        "original": "",
                        "russian": text,
                        "start": start,
                        "end": end,
                        "approximate": end is None or bool(tc.group("approx")),
                        "index": index,
                        "raw_start_tc": raw_tc,
                        "raw_end_tc": tc.group("b"),
                        "explicit_end": end is not None,
                    }
                )
            else:
                pending = {
                    "speaker": speaker,
                    "original": text,
                    "russian": "",
                    "start": start,
                    "end": end,
                    "approximate": end is None or bool(tc.group("approx")),
                    "index": index,
                    "raw_start_tc": raw_tc,
                    "raw_end_tc": tc.group("b"),
                    "explicit_end": end is not None,
                }
            continue

        if _BAD_BRACKET.match(line):
            raise ValueError(f"Malformed transcript timecode: {line.split()[0]}")
        if role:
            speaker = role.group("speaker").strip()
            rest = line[role.end() :].strip()
            if rest:
                line = rest
            else:
                continue

        explicit = _EXPLICIT.match(line)
        if explicit:
            value = explicit.group("text").strip()
            if explicit.group("label").casefold() in {"перевод", "translation"}:
                flush_translation(value, index)
            elif pending is not None:
                pending["original"] = value
            continue

        if (
            current_anchor is not None
            and re.search(r"[А-Яа-яЁё]", line)
            and not (paragraph.style and paragraph.style.name.lower().startswith("heading"))
            and not re.match(
                r"^(?:источник|source|транскрипция|вопрос|ответ|реплика)\s*[:—-]", line, re.I
            )
        ):
            is_bold = bool(paragraph.runs) and all(
                run.bold for run in paragraph.runs if run.text.strip()
            )
            if not is_bold and (pending is not None or plain_group is not None):
                if pending is not None:
                    flush_translation(line, index)
                    plain_group = records[-1]
                elif plain_group is not None:
                    plain_group["russian"] += " " + line
                continue

        # Exported unlabeled translations are bold. Russian-only timed
        # paragraphs were emitted directly above.
        is_bold = bool(paragraph.runs) and all(
            run.bold for run in paragraph.runs if run.text.strip()
        )
        if is_bold and re.search(r"[А-Яа-яЁё]", line):
            if pending is not None:
                flush_translation(line, index)
            elif current_anchor is not None:
                records.append(
                    {
                        "speaker": speaker,
                        "original": "",
                        "russian": line,
                        "start": current_anchor,
                        "end": None,
                        "approximate": True,
                        "index": index,
                        "raw_start_tc": current_raw_start,
                        "raw_end_tc": current_raw_end,
                        "explicit_end": current_raw_end is not None,
                    }
                )
            continue
        # Untimed headings and metadata are intentionally ignored.

    if pending is not None:
        pending = None
    if not records:
        raise ValueError("No Russian transcript cues found; provide Russian text with timecodes")

    anchors = sorted(set(timed_anchors))
    cues: list[TranscriptCue] = []
    for record in records:
        start = record["start"]
        end = record["end"]
        if end is None:
            end = next((anchor for anchor in anchors if anchor > start), duration_sec)
        if end > duration_sec or end < start:
            raise ValueError("Transcript timecode is outside source duration")
        key = "|".join(
            (
                source_id,
                record["speaker"],
                record["russian"],
                record["original"],
                f"{start + guide_offset_sec:.6f}",
                f"{end + guide_offset_sec:.6f}",
                str(record["index"]),
            )
        )
        cue_id = hashlib.sha256(key.encode("utf-8")).hexdigest()[:20]
        cues.append(
            TranscriptCue(
                cue_id,
                source_id,
                record["speaker"],
                record["russian"],
                record["original"],
                start + guide_offset_sec,
                end + guide_offset_sec,
                record["approximate"],
                record["index"],
                record.get("raw_start_tc"),
                record.get("raw_end_tc"),
                record.get("explicit_end"),
            )
        )
    return cues
