from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import json
import logging
import os
import re
from typing import Any, Callable, Iterable

from .config import Settings, get_settings
from .io_utils import (
    extract_marked_blocks_from_docx,
    extract_paragraphs_from_docx,
    extract_text,
    read_json,
    write_json,
)
from .models import NarrationBlock
from .openrouter_client import SYSTEM_PROMPT as _OPENROUTER_SYSTEM_PROMPT
from .openrouter_client import analyze_with_openrouter

logger = logging.getLogger(__name__)

# --- LLM analysis cache (per scenario chunk) ---
# Increment when the cached payload shape or the classifier contract changes
# so stale entries are transparently invalidated instead of misapplied.
_LLM_CACHE_VERSION = 1
_LLM_CACHE_MAX_ENTRIES = 2000

# --- Regex patterns ---

NARRATOR_TAG_RE = re.compile(
    r"\[\[\s*(?:VO|NARRATOR|ЗК|ЗАКАДР)\s*\]\](.*?)\[\[\s*/\s*(?:VO|NARRATOR|ЗК|ЗАКАДР)\s*\]\]",
    flags=re.I | re.S,
)
NARRATOR_MARKER_RE = re.compile(
    r"^\s*(?P<label>VO|NARRATOR|ЗК|ЗАКАДР)(?=(?:\s|[:\-–]|$|\d))"
    r"\s*(?P<suffix>\d+(?:\.\d+)?[A-ZА-Я]?|[A-ZА-Я])?"
    r"\s*(?P<separator>[:\-–])?\s*(?P<rest>.*)$",
    flags=re.I,
)
SPEAKER_PREFIX_RE = re.compile(
    r"^\s*(?:СПИКЕР|СНХ|ВЕДУЩИЙ)(?:\s*-?\s*\d+)?\s*[.:\-–]",
    flags=re.I,
)
SPEAKER_INLINE_RE = re.compile(
    r"(?:^|\n|\s)(?:СПИКЕР(?:\s*-?\s*\d+)?|СНХ|ВЕДУЩИЙ(?:\s*-?\s*\d+)?)\s*[.:\-–]\s*",
    flags=re.I,
)
TECH_PREFIX_RE = re.compile(
    r"^\s*(?:ВОТ\s+ССЫЛКА|ВИДЕО(?:\s*-?\s*\d+)?|ЛАЙ[ВФ](?:\s*-?\s*[\wА-Яа-яЁё]+)?|"
    r"ВНИМАНИЕ|ШАПКА|ОТБИВКА|ТИТР|ПЛАШКА|ГРАФИКА|ИНФОГРАФИКА|КАРТА|"
    r"ФОТОГРАФИ[ЯИ]|ФОТО|АРХИВН\w*|РЕКВИЗИТ|"
    r"АНОНС(?:Ы|\s*Ы)?(?:\s*-?\s*\d+)?|ЭКСПЕРИМЕНТ(?:\s*-?\s*\d+)?|"
    r"ШПИГЕЛЬ|СКЛЕЙКА|ВЖИК|НЕТ\s+ПЕРЕХОДА)\b",
    flags=re.I,
)
TECH_INLINE_RE = re.compile(
    r"(?:^|\s)(?:ВОТ\s+ССЫЛКА|ВИДЕО(?:\s*-?\s*\d+)?|ЛАЙ[ВФ](?:\s*-?\s*\d+)?|ВНИМАНИЕ)\s*[:\-–]\s*[^.!?\n]*(?:[.!?]|$)",
    flags=re.I,
)
# Timecoded interview fragments: [06:34] (...) text
TIMECODE_RE = re.compile(
    r"^\s*\[?\d{1,2}:\d{2}(?::\d{2})?\]?\s*",
)
# Lines that are mostly timecode + ellipsis (interview sync)
SYNC_LINE_RE = re.compile(
    r"^\s*(?:\[?\d{1,2}:\d{2}(?::\d{2})?\]?\s*)?(?:\(…\)|\(\.\.\.\)|\.\.\.)?\s*$",
)
# SNH label line: "СНХ Имя Фамилия, Файл ..."
SNH_LABEL_RE = re.compile(
    r"^\s*СНХ\s+\S",
    flags=re.I,
)
INTERVIEW_SPEAKER_LINE_RE = re.compile(
    r"^\s*(?:СНХ\s+)?"
    r"[А-ЯЁ][А-ЯЁа-яё-]+(?:\s+[А-ЯЁ][А-ЯЁа-яё-]+){0,3}"
    r"(?:,\s*[^.!?\n]{0,80})?"
    r"[:\s\-–]*\b\d{2}[:.]\d{2}(?:[:.]\d{2})?\b",
    flags=re.I,
)


def _match_narrator_marker_line(text: str) -> str | None:
    stripped = text.strip()
    if not stripped:
        return None

    match = NARRATOR_MARKER_RE.match(stripped)
    if not match:
        return None

    suffix = (match.group("suffix") or "").strip()
    separator = match.group("separator")
    if not suffix and not separator:
        return None

    return match.group("rest").strip()


def _is_sync_or_interview(text: str) -> bool:
    """Check if paragraph looks like an interview fragment (timecoded sync)."""
    stripped = text.strip()
    if TIMECODE_RE.match(stripped):
        return True
    if stripped.startswith("(") and "…" in stripped:
        return True
    if stripped.startswith("(...)") or stripped.startswith("(…)"):
        return True
    if INTERVIEW_SPEAKER_LINE_RE.match(stripped):
        return True
    return False


def _is_technical(text: str) -> bool:
    """Check if paragraph is a technical marker."""
    stripped = text.strip()
    if TECH_PREFIX_RE.match(stripped):
        return True
    # Very short ALL-CAPS lines are likely technical markers
    if (
        len(stripped) < 50
        and stripped == stripped.upper()
        and _match_narrator_marker_line(stripped) is None
    ):
        return True
    return False


def _is_speaker_or_snh(text: str) -> bool:
    """Check if paragraph is a speaker/SNH label."""
    stripped = text.strip()
    if SPEAKER_PREFIX_RE.match(stripped):
        return True
    if SNH_LABEL_RE.match(stripped):
        return True
    return False


def _split_long_text(text: str, max_len: int = 300) -> Iterable[str]:
    text = text.strip()
    if not text:
        return []
    if len(text) <= max_len:
        return [text]
    # Split on sentence boundaries: .!? and also ; — for Russian style
    sentences = re.split(r"(?<=[.!?;])\s+", text)
    chunks: list[str] = []
    current: list[str] = []
    cur_len = 0
    for sent in sentences:
        if not sent:
            continue
        if cur_len + len(sent) + 1 > max_len and current:
            chunks.append(" ".join(current).strip())
            current = [sent]
            cur_len = len(sent)
        else:
            current.append(sent)
            cur_len += len(sent) + 1
    if current:
        chunks.append(" ".join(current).strip())
    return chunks


# === Extraction strategies ===


def _from_docx_formatting(input_path: Path) -> tuple[list[NarrationBlock], str]:
    """Primary strategy: use bold/formatting from .docx to identify voiceover.

    Returns (blocks, method) where method describes what was used.
    Bold paragraphs = NOT voiceover (presenter, SNH labels, section titles).
    Non-bold, non-technical paragraphs = voiceover.
    """
    if input_path.suffix.lower() != ".docx":
        return [], ""

    paragraphs = extract_paragraphs_from_docx(input_path)
    if not paragraphs:
        return [], ""

    # Check if the document actually uses bold meaningfully
    bold_count = sum(1 for p in paragraphs if p.is_bold)
    non_bold_count = len(paragraphs) - bold_count
    if bold_count < 3 or non_bold_count < 3:
        # Not enough formatting contrast — don't use this strategy
        return [], ""

    blocks: list[NarrationBlock] = []
    for para in paragraphs:
        text = para.text.strip()
        if not text:
            continue

        # Bold = presenter/SNH/titles — skip
        if para.is_bold:
            continue

        # Filter timecoded interview fragments
        if _is_sync_or_interview(text):
            continue

        # Filter technical markers
        if _is_technical(text):
            continue

        # Filter speaker/SNH labels (in case they aren't bold)
        if _is_speaker_or_snh(text):
            continue

        # Clean timecodes that might be inline
        text = TIMECODE_RE.sub("", text).strip()
        # Clean leading ellipsis from interview remnants
        text = re.sub(r"^\s*(?:\(…\)|\(\.\.\.\))\s*", "", text).strip()

        if not text or len(text) < 10:
            continue

        blocks.append(
            NarrationBlock(
                id="",
                text_original=text,
                text_for_tts=text,
                notes="docx_formatting",
            )
        )

    return blocks, "docx_formatting"


def _from_marked_docx(input_path: Path) -> list[NarrationBlock]:
    """Highlighted blocks from .docx (highlight color / shading)."""
    if input_path.suffix.lower() != ".docx":
        return []

    highlighted_paragraphs = [
        para.text.strip()
        for para in extract_paragraphs_from_docx(input_path)
        if para.has_highlight and para.text.strip()
    ]
    if highlighted_paragraphs:
        manual_blocks = _extract_manual_narrator_blocks("\n".join(highlighted_paragraphs))
        if manual_blocks:
            for block in manual_blocks:
                block.notes = "manual_highlight_marker"
            return manual_blocks

    marked_texts = extract_marked_blocks_from_docx(input_path)
    blocks: list[NarrationBlock] = []
    for text in marked_texts:
        text = text.strip()
        if not text:
            continue
        blocks.append(
            NarrationBlock(
                id="",
                text_original=text,
                text_for_tts=text,
                notes="manual_highlight",
            )
        )
    return blocks


def _extract_manual_narrator_blocks(scenario_text: str) -> list[NarrationBlock]:
    """Extract blocks wrapped in [[VO]]...[[/VO]] or prefixed with ЗК: etc."""
    found: list[tuple[int, int, str, str]] = []

    for match in NARRATOR_TAG_RE.finditer(scenario_text):
        text = match.group(1).strip()
        if text:
            found.append((match.start(), match.end(), text, "manual_tag"))

    current_start: int | None = None
    current_end: int | None = None
    current_note = "manual_prefix"
    current_lines: list[str] = []
    offset = 0

    def flush_current() -> None:
        nonlocal current_start, current_end, current_note, current_lines
        if current_start is None:
            return
        text = "\n".join(line for line in current_lines if line).strip()
        if text:
            found.append((current_start, current_end or current_start, text, current_note))
        current_start = None
        current_end = None
        current_note = "manual_prefix"
        current_lines = []

    for raw_line in scenario_text.splitlines(keepends=True):
        line = raw_line.rstrip("\r\n")
        stripped = line.strip()
        marker_rest = _match_narrator_marker_line(stripped)

        if marker_rest is not None:
            flush_current()
            current_start = offset
            current_end = offset + len(raw_line)
            current_note = "manual_numbered" if re.search(r"\d", stripped) else "manual_prefix"
            current_lines = [marker_rest] if marker_rest else []
        elif current_start is not None:
            if not stripped:
                flush_current()
            elif (
                _is_speaker_or_snh(stripped)
                or _is_sync_or_interview(stripped)
                or _is_technical(stripped)
            ):
                flush_current()
            else:
                current_lines.append(stripped)
                current_end = offset + len(raw_line)

        offset += len(raw_line)

    flush_current()

    found.sort(key=lambda x: x[0])

    return [
        NarrationBlock(
            id="",
            text_original=text,
            text_for_tts=text,
            notes=note,
            source_spans=[(start, end)],
        )
        for start, end, text, note in found
    ]


# === Text cleanup ===


def _strip_speaker_cues(text: str) -> str:
    if not text:
        return text

    kept_lines: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if _is_speaker_or_snh(line):
            continue
        if INTERVIEW_SPEAKER_LINE_RE.match(line):
            continue
        cleaned_line = SPEAKER_INLINE_RE.sub(" ", line).strip()
        if cleaned_line:
            kept_lines.append(cleaned_line)

    cleaned = "\n".join(kept_lines)
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _strip_narrator_markers(text: str) -> str:
    if not text:
        return text

    kept_lines: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        marker_rest = _match_narrator_marker_line(line)
        if marker_rest is not None:
            line = marker_rest
        if line:
            kept_lines.append(line)

    cleaned = "\n".join(kept_lines)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _strip_technical_cues(text: str) -> str:
    if not text:
        return text

    kept_lines: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if TECH_PREFIX_RE.match(line):
            continue
        if _is_sync_or_interview(line):
            continue
        kept_lines.append(line)

    cleaned = "\n".join(kept_lines)
    cleaned = TECH_INLINE_RE.sub(" ", cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _cleanup_blocks(blocks: list[NarrationBlock]) -> list[NarrationBlock]:
    cleaned_blocks: list[NarrationBlock] = []
    for block in blocks:
        text_original = _strip_speaker_cues(block.text_original.strip())
        text_for_tts = _strip_speaker_cues(block.text_for_tts.strip())
        text_original = _strip_narrator_markers(text_original)
        text_for_tts = _strip_narrator_markers(text_for_tts)
        text_original = _strip_technical_cues(text_original)
        text_for_tts = _strip_technical_cues(text_for_tts)
        if not text_for_tts or len(text_for_tts) < 5:
            continue
        cleaned_blocks.append(
            NarrationBlock(
                id=block.id,
                text_original=text_original or text_for_tts,
                text_for_tts=text_for_tts,
                pause_ms=block.pause_ms,
                notes=block.notes,
                source_spans=list(block.source_spans),
            )
        )
    return cleaned_blocks


# === LLM ===


def _chunk_for_llm(scenario_text: str, max_chars: int = 6000) -> list[str]:
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", scenario_text) if p.strip()]
    if not paragraphs:
        paragraphs = [p.strip() for p in scenario_text.splitlines() if p.strip()]
    if not paragraphs:
        return []

    chunks: list[str] = []
    current: list[str] = []
    current_len = 0

    for paragraph in paragraphs:
        pieces = [paragraph]
        if len(paragraph) > max_chars:
            pieces = list(_split_long_text(paragraph, max_len=max_chars - 200))

        for piece in pieces:
            piece_len = len(piece) + 2
            if current and (current_len + piece_len) > max_chars:
                chunks.append("\n\n".join(current).strip())
                current = [piece]
                current_len = piece_len
            else:
                current.append(piece)
                current_len += piece_len

    if current:
        chunks.append("\n\n".join(current).strip())
    return chunks


# === Fallback ===


def _fallback_blocks(scenario_text: str) -> list[NarrationBlock]:
    marker_re = re.compile(r"^\s*(zk\d+)\s*[:\-–]\s*(.+?)\s*$", flags=re.I | re.M)
    marker_items = marker_re.findall(scenario_text)
    blocks: list[NarrationBlock] = []

    if marker_items:
        for idx, (_, text) in enumerate(marker_items, start=1):
            blocks.append(
                NarrationBlock(
                    id=f"zk{idx}",
                    text_original=text.strip(),
                    text_for_tts=text.strip(),
                )
            )
        return blocks

    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", scenario_text) if p.strip()]
    if not paragraphs:
        paragraphs = [p.strip() for p in scenario_text.splitlines() if p.strip()]

    idx = 1
    for paragraph in paragraphs:
        if _is_speaker_or_snh(paragraph):
            continue
        if _is_technical(paragraph):
            continue
        if _is_sync_or_interview(paragraph):
            continue
        for piece in _split_long_text(paragraph):
            blocks.append(
                NarrationBlock(
                    id=f"zk{idx}",
                    text_original=piece,
                    text_for_tts=piece,
                )
            )
            idx += 1
    return blocks


LLM_FAILED_NOTE = "llm_failed"


def _mark_llm_failed(blocks: list[NarrationBlock]) -> list[NarrationBlock]:
    """Flag heuristic-fallback blocks that replaced a failed LLM chunk."""
    for block in blocks:
        block.notes = _merge_block_notes(block.notes, LLM_FAILED_NOTE)
    return blocks


# === Numbering & merging ===


def _renumber_blocks(blocks: list[NarrationBlock]) -> list[NarrationBlock]:
    for idx, block in enumerate(blocks, start=1):
        block.id = f"zk{idx}"
    return blocks


def _join_block_text(left: str, right: str, separator: str = " ") -> str:
    left = left.strip()
    right = right.strip()
    if not left:
        return right
    if not right:
        return left
    return f"{left}{separator}{right}"


def _merge_block_notes(*note_values: str) -> str:
    merged: list[str] = []
    for note in note_values:
        for part in note.split("|"):
            cleaned = part.strip()
            if cleaned and cleaned not in merged:
                merged.append(cleaned)
    return " | ".join(merged)


def _merge_adjacent_pair(
    left: NarrationBlock,
    right: NarrationBlock,
    separator: str = " ",
) -> NarrationBlock:
    return NarrationBlock(
        id="",
        text_original=_join_block_text(
            left.text_original, right.text_original, separator=separator
        ),
        text_for_tts=_join_block_text(left.text_for_tts, right.text_for_tts, separator=separator),
        pause_ms=max(left.pause_ms, right.pause_ms),
        notes=_merge_block_notes(left.notes, right.notes),
        source_spans=sorted([*left.source_spans, *right.source_spans]),
    )


def _attach_source_spans(
    blocks: list[NarrationBlock],
    scenario_text: str,
) -> None:
    """Best-effort location of extracted blocks, preserving duplicate order."""
    cursor = 0
    for block in blocks:
        if block.source_spans:
            cursor = max(cursor, max(end for _start, end in block.source_spans))
            continue
        candidates = (block.text_original.strip(), block.text_for_tts.strip())
        for candidate in candidates:
            if not candidate:
                continue
            start = scenario_text.find(candidate, cursor)
            if start < 0:
                start = scenario_text.find(candidate)
            if start >= 0:
                end = start + len(candidate)
                block.source_spans = [(start, end)]
                cursor = end
                break


def _canonical_raw_projection(
    scenario_text: str,
    blocks: list[NarrationBlock],
) -> tuple[str, list[str]]:
    """Render legacy raw text without relying on ambiguous string replacement."""
    edits: list[tuple[int, int, str]] = []
    missing: list[str] = []
    occupied: list[tuple[int, int]] = []

    for block in blocks:
        num = block.id[2:]
        rendered_text = block.text_original.strip() or block.text_for_tts.strip()
        spans = sorted(
            (start, end)
            for start, end in block.source_spans
            if 0 <= start < end <= len(scenario_text)
        )
        usable: list[tuple[int, int]] = []
        for start, end in spans:
            if any(start < prior_end and end > prior_start for prior_start, prior_end in occupied):
                continue
            usable.append((start, end))
            occupied.append((start, end))
        if not usable:
            missing.append(f"ЗК{num}: {rendered_text}")
            continue
        first_start, first_end = usable[0]
        edits.append((first_start, first_end, f"ЗК{num}: {rendered_text}"))
        edits.extend((start, end, "") for start, end in usable[1:])

    raw_text = scenario_text
    for start, end, replacement in sorted(edits, reverse=True):
        raw_text = raw_text[:start] + replacement + raw_text[end:]
    if missing:
        raw_text = raw_text.rstrip() + "\n\n" + "\n".join(missing)
    return raw_text, missing


def _merge_candidate_score(left: NarrationBlock, right: NarrationBlock) -> int:
    left_text = left.text_for_tts.strip()
    right_text = right.text_for_tts.strip()
    score = len(left_text) + len(right_text)

    # Prefer collapsing genuinely short neighbors before touching long narration chunks.
    for text in (left_text, right_text):
        if len(text) < 80:
            score -= 40
        elif len(text) < 140:
            score -= 10

    # If the left block clearly continues into the next one, merging is cheaper.
    if left_text.endswith((",", ":", ";", "—", "-", "(")):
        score -= 30
    if right_text[:1].islower():
        score -= 20

    # Full sentence boundary is a slightly stronger signal to keep blocks separate.
    if left_text.endswith((".", "!", "?", "…")) and right_text[:1].isupper():
        score += 15

    return score


def _merge_short_blocks(blocks: list[NarrationBlock], min_len: int = 40) -> list[NarrationBlock]:
    """Merge very short blocks with the next block for natural flow."""
    if not blocks:
        return blocks
    merged: list[NarrationBlock] = []
    i = 0
    while i < len(blocks):
        block = blocks[i]
        # If block is very short and there's a next block, merge them
        if len(block.text_for_tts) < min_len and i + 1 < len(blocks):
            next_block = blocks[i + 1]
            merged.append(_merge_adjacent_pair(block, next_block))
            i += 2
        else:
            merged.append(block)
            i += 1
    return merged


def _merge_blocks_to_limit(blocks: list[NarrationBlock], max_blocks: int) -> list[NarrationBlock]:
    if max_blocks < 1 or len(blocks) <= max_blocks:
        return blocks

    merged = list(blocks)

    while len(merged) > max_blocks:
        best_idx = 0
        best_score: int | None = None

        for idx in range(len(merged) - 1):
            score = _merge_candidate_score(merged[idx], merged[idx + 1])
            if best_score is None or score < best_score:
                best_score = score
                best_idx = idx

        merged_pair = _merge_adjacent_pair(merged[best_idx], merged[best_idx + 1])
        merged = merged[:best_idx] + [merged_pair] + merged[best_idx + 2 :]

    return merged


def _llm_cache_path(output_path: Path) -> Path:
    parent = Path(output_path).parent
    work_dir = parent if parent.name == "work" else parent / "work"
    return work_dir / "analysis_llm_cache.json"


def _load_llm_cache(path: Path) -> dict[str, Any]:
    try:
        payload = read_json(path)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {"version": _LLM_CACHE_VERSION, "entries": {}}
    if (
        not isinstance(payload, dict)
        or payload.get("version") != _LLM_CACHE_VERSION
        or not isinstance(payload.get("entries"), dict)
    ):
        return {"version": _LLM_CACHE_VERSION, "entries": {}}
    return payload


def _llm_identity(settings: Settings) -> dict[str, Any]:
    """Fingerprint of the LLM configuration used for a chunk analysis call.

    Any change to the model/provider/prompt must invalidate previously
    cached results, so every field ``analyze_with_openrouter`` relies on is
    represented here explicitly.
    """
    return {
        "kind": "openrouter",
        "model": settings.openrouter_model,
        "base_url": settings.openrouter_base_url,
        "temperature": 0.2,
        "response_format": "json_object",
        "system_prompt": _OPENROUTER_SYSTEM_PROMPT,
    }


def _llm_cache_fingerprint(chunk: str, *, identity: dict[str, Any]) -> str:
    material = {
        "version": _LLM_CACHE_VERSION,
        "chunk": chunk,
        "identity": identity,
    }
    return sha256(
        json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()


def _serialize_llm_blocks(blocks: list[NarrationBlock]) -> list[dict[str, Any]] | None:
    try:
        payload = [block.to_dict() for block in blocks]
        json.dumps(payload, ensure_ascii=False)
    except (TypeError, ValueError, AttributeError):
        return None
    return payload


def _deserialize_llm_blocks(payload: Any) -> list[NarrationBlock] | None:
    if not isinstance(payload, list):
        return None
    blocks: list[NarrationBlock] = []
    for item in payload:
        if not isinstance(item, dict):
            return None
        try:
            blocks.append(
                NarrationBlock(
                    id=str(item.get("id", "")),
                    text_original=str(item.get("text_original", "")),
                    text_for_tts=str(item.get("text_for_tts", "")),
                    pause_ms=int(item.get("pause_ms", 300)),
                    notes=str(item.get("notes", "")),
                )
            )
        except (TypeError, ValueError):
            return None
    return blocks


def _cached_llm_blocks(cache: dict[str, Any], cache_key: str) -> list[NarrationBlock] | None:
    entry = cache.get("entries", {}).get(cache_key)
    if not isinstance(entry, dict):
        return None
    return _deserialize_llm_blocks(entry.get("blocks"))


def _cache_llm_blocks(
    path: Path,
    cache: dict[str, Any],
    cache_key: str,
    blocks: list[NarrationBlock],
) -> bool:
    serialized = _serialize_llm_blocks(blocks)
    if serialized is None:
        return False
    entries = cache.setdefault("entries", {})
    entries[cache_key] = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "blocks": serialized,
    }
    if len(entries) > _LLM_CACHE_MAX_ENTRIES:
        oldest = sorted(entries, key=lambda key: str(entries[key].get("created_at") or ""))[
            : len(entries) - _LLM_CACHE_MAX_ENTRIES
        ]
        for key in oldest:
            entries.pop(key, None)
    cache["version"] = _LLM_CACHE_VERSION
    try:
        write_json(path, cache)
    except (OSError, TypeError, ValueError):
        entries.pop(cache_key, None)
        return False
    return True


# === Main entry point ===


def analyze_script(
    input_path: Path,
    output_path: Path,
    raw_output_path: Path,
    use_llm: bool = True,
    usage_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict:
    if not input_path.exists():
        raise FileNotFoundError(f"Input file not found: {input_path}")

    logger.info("Legacy analysis started source=%s", input_path)

    scenario_text = extract_text(input_path)
    raw_output_path.parent.mkdir(parents=True, exist_ok=True)

    settings = get_settings()
    used_llm = False
    manual_markers_used = False
    extraction_method = ""
    blocks: list[NarrationBlock]
    llm_errors: list[str] = []

    # Strategy 1: Manual markers in text [[VO]]..[[/VO]] or ЗК:
    # Must run before highlight check — explicit ЗК markers are more reliable
    # than arbitrary highlight formatting which may capture non-voiceover blocks.
    manual_blocks = _extract_manual_narrator_blocks(scenario_text)
    if manual_blocks:
        blocks = manual_blocks
        manual_markers_used = True
        extraction_method = "manual_markers"
    else:
        # Strategy 2: Highlighted blocks from docx
        marked_docx_blocks = _from_marked_docx(input_path)
        if marked_docx_blocks:
            blocks = marked_docx_blocks
            manual_markers_used = True
            extraction_method = "highlight"
        else:
            # Strategy 3: Bold-aware formatting from .docx
            formatting_blocks, fmt_method = _from_docx_formatting(input_path)
            if formatting_blocks:
                blocks = formatting_blocks
                extraction_method = fmt_method
            elif use_llm and settings.openrouter_api_key:
                # Strategy 4: LLM analysis
                llm_blocks: list[NarrationBlock] = []
                llm_success_chunks = 0
                chunks = _chunk_for_llm(scenario_text)

                if not chunks:
                    chunks = [scenario_text]

                try:
                    configured_workers = int(os.getenv("ANALYZER_LLM_WORKERS", "4"))
                except ValueError:
                    configured_workers = 4
                max_workers = max(1, min(4, configured_workers))

                cache_path = _llm_cache_path(output_path)
                cache = _load_llm_cache(cache_path)
                identity = _llm_identity(settings)
                cache_keys = [_llm_cache_fingerprint(chunk, identity=identity) for chunk in chunks]

                chunk_results: list[list[NarrationBlock] | None] = [None] * len(chunks)
                chunk_success = [False] * len(chunks)
                pending: list[int] = []
                for idx, (chunk, cache_key) in enumerate(zip(chunks, cache_keys)):
                    cached = _cached_llm_blocks(cache, cache_key)
                    if cached is None:
                        pending.append(idx)
                    else:
                        chunk_results[idx] = cached
                        chunk_success[idx] = True

                if pending:
                    with ThreadPoolExecutor(max_workers=min(max_workers, len(pending))) as pool:
                        if usage_callback is None:
                            futures = {
                                idx: pool.submit(analyze_with_openrouter, settings, chunks[idx])
                                for idx in pending
                            }
                        else:
                            futures = {
                                idx: pool.submit(
                                    analyze_with_openrouter,
                                    settings,
                                    chunks[idx],
                                    usage_callback,
                                )
                                for idx in pending
                            }
                        # Consume in source order so output and error reporting stay stable.
                        for idx in pending:
                            try:
                                result_blocks = futures[idx].result()
                                chunk_results[idx] = result_blocks
                                chunk_success[idx] = True
                                _cache_llm_blocks(cache_path, cache, cache_keys[idx], result_blocks)
                            except Exception as exc:
                                llm_errors.append(f"chunk {idx + 1}: {type(exc).__name__}: {exc}")
                                chunk_results[idx] = _mark_llm_failed(_fallback_blocks(chunks[idx]))

                for idx in range(len(chunks)):
                    llm_blocks.extend(chunk_results[idx] or [])
                    if chunk_success[idx]:
                        llm_success_chunks += 1

                if llm_blocks:
                    blocks = llm_blocks
                else:
                    blocks = _mark_llm_failed(_fallback_blocks(scenario_text))
                used_llm = llm_success_chunks > 0
                extraction_method = "llm" if used_llm else "fallback"
            else:
                # Strategy 5: Fallback
                blocks = _fallback_blocks(scenario_text)
                extraction_method = "fallback"

    _attach_source_spans(blocks, scenario_text)
    blocks = _cleanup_blocks(blocks)
    blocks = _merge_short_blocks(blocks)
    before_merge_count = len(blocks)
    blocks = _merge_blocks_to_limit(blocks, settings.max_zk_blocks)
    blocks = _renumber_blocks(blocks)

    llm_failed_chunks = len(llm_errors)
    warnings: list[str] = []
    if llm_failed_chunks:
        degraded = sum(1 for block in blocks if LLM_FAILED_NOTE in block.notes)
        warnings.append(
            f"LLM analysis failed for {llm_failed_chunks} chunk(s); "
            f"{degraded} block(s) were extracted by heuristic fallback "
            f"(marked '{LLM_FAILED_NOTE}' in notes) and need manual review"
        )

    raw_text, raw_projection_fallbacks = _canonical_raw_projection(scenario_text, blocks)
    raw_output_path.write_text(raw_text + "\n", encoding="utf-8")

    payload = {
        "source": str(input_path),
        "used_llm": used_llm,
        "manual_markers_used": manual_markers_used,
        "extraction_method": extraction_method,
        "llm_errors": llm_errors,
        "llm_failed_chunks": llm_failed_chunks,
        "warnings": warnings,
        "raw_projection_fallbacks": raw_projection_fallbacks,
        "max_zk_blocks": settings.max_zk_blocks,
        "blocks_count_before_merge": before_merge_count,
        "items": [b.to_dict() for b in blocks],
    }
    write_json(output_path, payload)
    logger.info(
        "Legacy analysis completed source=%s method=%s blocks=%d",
        input_path,
        extraction_method,
        len(blocks),
    )

    return {
        "source": str(input_path),
        "output": str(output_path),
        "raw_output": str(raw_output_path),
        "blocks_count": len(blocks),
        "blocks_count_before_merge": before_merge_count,
        "max_zk_blocks": settings.max_zk_blocks,
        "used_llm": used_llm,
        "manual_markers_used": manual_markers_used,
        "extraction_method": extraction_method,
        "llm_errors_count": len(llm_errors),
        "llm_failed_chunks": llm_failed_chunks,
        "warnings": warnings,
    }
