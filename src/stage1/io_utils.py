from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any
import zipfile

from atomic_io import atomic_write_json


def normalize_whitespace(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _env_int(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, ""))
    except ValueError:
        return default
    return value if value > 0 else default


_MIB = 1024 * 1024
# Limits are read at call time so they can be tuned via environment variables.
DOCX_MAX_FILE_BYTES_ENV = "ROUGH_CUT_DOCX_MAX_FILE_BYTES"
DOCX_MAX_UNCOMPRESSED_BYTES_ENV = "ROUGH_CUT_DOCX_MAX_UNCOMPRESSED_BYTES"
DOCX_MAX_MEMBERS_ENV = "ROUGH_CUT_DOCX_MAX_MEMBERS"
DEFAULT_DOCX_MAX_FILE_BYTES = 50 * _MIB
DEFAULT_DOCX_MAX_UNCOMPRESSED_BYTES = 200 * _MIB
DEFAULT_DOCX_MAX_MEMBERS = 5000
# Compression ratio is only suspicious once the payload is non-trivial.
DOCX_SUSPICIOUS_RATIO = 100
DOCX_RATIO_MIN_UNCOMPRESSED_BYTES = 10 * _MIB


def validate_docx_archive(path: Path) -> None:
    """Reject oversized, malformed or zip-bomb-like .docx before parsing it.

    Raises ValueError with a user-facing message (mapped to 4xx by the API).
    """
    path = Path(path)
    max_file = _env_int(DOCX_MAX_FILE_BYTES_ENV, DEFAULT_DOCX_MAX_FILE_BYTES)
    max_total = _env_int(DOCX_MAX_UNCOMPRESSED_BYTES_ENV, DEFAULT_DOCX_MAX_UNCOMPRESSED_BYTES)
    max_members = _env_int(DOCX_MAX_MEMBERS_ENV, DEFAULT_DOCX_MAX_MEMBERS)

    file_size = path.stat().st_size
    if file_size > max_file:
        raise ValueError(f"DOCX file is too large: {file_size} bytes (limit {max_file} bytes)")
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
    except (zipfile.BadZipFile, EOFError, OSError, NotImplementedError) as exc:
        raise ValueError(f"DOCX file is corrupted or is not a valid .docx archive: {exc}") from exc

    if len(infos) > max_members:
        raise ValueError(f"DOCX archive has too many entries: {len(infos)} (limit {max_members})")
    total = sum(info.file_size for info in infos)
    if total > max_total:
        raise ValueError(
            f"DOCX archive expands to too much data: {total} bytes (limit {max_total} bytes)"
        )
    compressed = sum(info.compress_size for info in infos)
    if (
        total >= DOCX_RATIO_MIN_UNCOMPRESSED_BYTES
        and total > max(compressed, 1) * DOCX_SUSPICIOUS_RATIO
    ):
        raise ValueError(
            "DOCX archive has a suspicious compression ratio "
            f"({total} bytes from {compressed} compressed); possible zip bomb"
        )


def _open_docx(document_factory: Any, path: Path) -> Any:
    """Validate the archive, then open it with python-docx."""
    validate_docx_archive(path)
    return document_factory(str(path))


def extract_text_from_docx(path: Path) -> str:
    try:
        from docx import Document  # lazy import for clearer error messages
    except ImportError as exc:
        raise RuntimeError(
            "python-docx is not installed. Install dependencies from requirements.txt."
        ) from exc
    doc = _open_docx(Document, path)
    lines = list(_iter_docx_text_blocks(doc))
    return normalize_whitespace("\n".join(lines))


def extract_paragraphs(path: Path) -> list[str]:
    """Extract logical paragraphs while preserving soft line breaks in DOCX."""
    path = Path(path)
    if path.suffix.lower() == ".docx":
        try:
            from docx import Document
        except ImportError as exc:
            raise RuntimeError(
                "python-docx is not installed. Install dependencies from requirements.txt."
            ) from exc
        return [
            normalized
            for text in _iter_docx_text_blocks(_open_docx(Document, path))
            if (normalized := normalize_whitespace(text))
        ]
    text = extract_text(path)
    return [line.strip() for line in text.splitlines() if line.strip()]


def _iter_docx_text_blocks(document: Any):
    """Yield body paragraphs and table-cell paragraphs in document order."""
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    seen_cells: set[int] = set()
    for child in document.element.body.iterchildren():
        tag = str(child.tag)
        if tag.endswith("}p"):
            text = (Paragraph(child, document).text or "").strip()
            if text:
                yield text
        elif tag.endswith("}tbl"):
            table = Table(child, document)
            for row in table.rows:
                for cell in row.cells:
                    cell_key = id(cell._tc)
                    if cell_key in seen_cells:
                        continue
                    seen_cells.add(cell_key)
                    for paragraph in cell.paragraphs:
                        text = (paragraph.text or "").strip()
                        if text:
                            yield text


def _run_is_marked(run: Any) -> bool:
    font = getattr(run, "font", None)
    if font is not None and getattr(font, "highlight_color", None) is not None:
        return True

    # Word "заливка" is often saved in XML as w:highlight / w:shd.
    if _xml_has_marking(getattr(run, "_element", None)):
        return True
    return False


def _paragraph_is_marked(paragraph: Any) -> bool:
    return _xml_has_marking(getattr(paragraph, "_p", None))


_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_IGNORE_SHD_FILLS = {"auto", "none", "", "FFFFFF", "ffffff"}


def _xml_has_marking(element: Any) -> bool:
    if element is None:
        return False
    for node in element.iter():
        tag = str(getattr(node, "tag", ""))
        if tag.endswith("}highlight"):
            val = node.get(f"{{{_W_NS}}}val", "none")
            if val and val.lower() != "none":
                return True
        elif tag.endswith("}shd"):
            fill = node.get(f"{{{_W_NS}}}fill", "auto")
            if fill and fill not in _IGNORE_SHD_FILLS:
                return True
    return False


def extract_marked_blocks_from_docx(path: Path) -> list[str]:
    try:
        from docx import Document  # lazy import for clearer error messages
    except ImportError as exc:
        raise RuntimeError(
            "python-docx is not installed. Install dependencies from requirements.txt."
        ) from exc

    doc = _open_docx(Document, path)
    blocks: list[str] = []

    for paragraph in doc.paragraphs:
        paragraph_text = (paragraph.text or "").strip()
        if not paragraph_text:
            continue

        # Skip bold paragraphs — they are presenter/SNH/titles, not voiceover
        bold_chars = 0
        total_chars = 0
        for run in paragraph.runs:
            run_len = len((run.text or "").strip())
            if run_len == 0:
                continue
            total_chars += run_len
            if run.bold:
                bold_chars += run_len
        is_mostly_bold = total_chars > 0 and bold_chars > total_chars * 0.5
        if is_mostly_bold:
            continue

        paragraph_marked = _paragraph_is_marked(paragraph)
        marked_runs = []
        for run in paragraph.runs:
            run_text = (run.text or "").strip()
            if run_text and _run_is_marked(run):
                marked_runs.append(run_text)

        if not paragraph_marked and not marked_runs:
            continue

        if paragraph_marked:
            candidate = paragraph_text
        else:
            # If only part of the paragraph is marked, take marked fragments.
            candidate = normalize_whitespace(" ".join(marked_runs))
            if len(candidate) < 8:
                candidate = paragraph_text

        if candidate:
            blocks.append(candidate)
    return blocks


@dataclass
class DocxParagraph:
    """Paragraph with formatting metadata from a .docx file."""

    text: str
    is_bold: bool
    has_highlight: bool
    index: int


def extract_paragraphs_from_docx(path: Path) -> list[DocxParagraph]:
    try:
        from docx import Document
    except ImportError as exc:
        raise RuntimeError(
            "python-docx is not installed. Install dependencies from requirements.txt."
        ) from exc

    doc = _open_docx(Document, path)
    result: list[DocxParagraph] = []

    for idx, paragraph in enumerate(doc.paragraphs):
        text = (paragraph.text or "").strip()
        if not text:
            continue

        has_highlight = _paragraph_is_marked(paragraph)
        bold_chars = 0
        total_chars = 0
        for run in paragraph.runs:
            run_text = run.text or ""
            run_len = len(run_text.strip())
            if run_len == 0:
                continue
            total_chars += run_len
            if run.bold:
                bold_chars += run_len
            if not has_highlight and _run_is_marked(run):
                has_highlight = True

        is_bold = total_chars > 0 and bold_chars > total_chars * 0.5

        result.append(
            DocxParagraph(
                text=text,
                is_bold=is_bold,
                has_highlight=has_highlight,
                index=idx,
            )
        )

    return result


def extract_text_from_doc(path: Path) -> str:
    # ``catdoc`` wraps output to the document's visual line width by default,
    # turning one Word paragraph into many fake scenario blocks. Prefer
    # antiword with unlimited line width; keep catdoc ``-w`` and macOS textutil
    # as fallbacks for unusual legacy files.
    last_error: str = ""
    for cmd in (
        ["antiword", "-w", "0", str(path)],
        ["catdoc", "-w", str(path)],
        ["textutil", "-convert", "txt", "-stdout", str(path)],
    ):
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=True,
                timeout=60,
            )
            return normalize_whitespace(proc.stdout)
        except FileNotFoundError:
            continue
        except subprocess.CalledProcessError as exc:
            last_error = (exc.stderr or exc.stdout or "").strip() or "unknown error"
            continue  # try next tool
        except subprocess.TimeoutExpired:
            last_error = f"{cmd[0]} timed out after 60 seconds"
            continue
    if last_error:
        raise RuntimeError(f"Failed to parse .doc: {last_error}")
    raise RuntimeError("Neither catdoc nor textutil found. Convert .doc to .docx first.")


def extract_text(input_path: Path) -> str:
    suffix = input_path.suffix.lower()
    if suffix == ".docx":
        return extract_text_from_docx(input_path)
    if suffix == ".doc":
        return extract_text_from_doc(input_path)
    if suffix in {".txt", ".md"}:
        return normalize_whitespace(input_path.read_text(encoding="utf-8"))
    raise ValueError(f"Unsupported input format: {input_path.suffix}")


def write_json(path: Path, payload: dict[str, Any]) -> None:
    atomic_write_json(path, payload)


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))
