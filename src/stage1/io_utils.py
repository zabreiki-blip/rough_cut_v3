from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
import subprocess
from typing import Any

from atomic_io import atomic_write_json


def normalize_whitespace(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_text_from_docx(path: Path) -> str:
    try:
        from docx import Document  # lazy import for clearer error messages
    except ImportError as exc:
        raise RuntimeError(
            "python-docx is not installed. Install dependencies from requirements.txt."
        ) from exc
    doc = Document(str(path))
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
            for text in _iter_docx_text_blocks(Document(str(path)))
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

    doc = Document(str(path))
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

    doc = Document(str(path))
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


_WORD_FIELD_ARGS = r'(?:\s*(?:\\[*#@A-Za-z](?:\s+[A-Za-z]+)?|"[^"]*"|_[A-Za-z]\w*))*'
# ``HYPERLINK "url" \l "anchor"``: the URL is kept as plain text (it is a
# technical-note signal for the classifier), the keyword and switches go.
# A visible text equal to the URL is not repeated.
_HYPERLINK_RE = re.compile(
    r'\bHYPERLINK\s*"(?P<url>[^"]*)"(?:\s*\\[A-Za-z](?:\s*"[^"]*")?)*'
    r"(?:\s*(?P<dup>(?P=url))(?=\s|\Z))?",
    re.MULTILINE,
)
_WORD_FIELD_CODE_RE = re.compile(
    # ``HYPERLINK \l "anchor"`` without a URL: nothing worth keeping.
    r'\bHYPERLINK\s*\\[A-Za-z]\s*"[^"]*"(?:\s*\\[A-Za-z](?:\s*"[^"]*")?)*'
    # ``PAGEREF _Toc123 \h`` / ``TOC \o "1-3" \h`` / ``PAGE \* MERGEFORMAT``: a
    # bare word is not a field, so a switch or bookmark argument is required.
    + r"|\b(?:PAGEREF|NUMPAGES|PAGE|TOC)(?:\s+(?:\\[*#@A-Za-z](?:\s+[A-Za-z]+)?|_[A-Za-z]\w*))"
    + _WORD_FIELD_ARGS
    + r"|^[ \t]*PAGE[ \t]+\d+[ \t]*$"
    + r"|\\\*\s*(?:MERGEFORMAT|CHARFORMAT|Arabic|ROMAN|roman|Upper|Lower)\b",
    re.MULTILINE,
)


def _keep_hyperlink_url(match: re.Match[str]) -> str:
    return match.group("url") if match.group("dup") else match.group("url") + " "


def strip_word_field_codes(text: str) -> str:
    """Drop Word field instructions that legacy converters leak into the text.

    ``HYPERLINK "url"Видимый текст`` becomes ``url Видимый текст`` (the URL is
    kept, the keyword and quotes/switches are dropped); ``PAGE 3``,
    ``PAGEREF _Toc1 \\h``, ``TOC \\o "1-3"`` and ``\\* MERGEFORMAT`` vanish.
    Ordinary text, including a plain word ``page`` or ``TOC``, is left alone.
    """
    text, links = _HYPERLINK_RE.subn(_keep_hyperlink_url, text)
    cleaned, count = _WORD_FIELD_CODE_RE.subn("", text)
    if not (links or count):
        return text
    cleaned = re.sub(r"(?<=\S)[ \t]{2,}", " ", cleaned)
    return re.sub(r"[ \t]+$", "", cleaned, flags=re.MULTILINE)


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
            return normalize_whitespace(strip_word_field_codes(proc.stdout))
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
