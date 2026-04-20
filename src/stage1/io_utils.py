from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
import subprocess
from typing import Any


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
    lines = [p.text.strip() for p in doc.paragraphs if p.text and p.text.strip()]
    return normalize_whitespace("\n".join(lines))


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

        result.append(DocxParagraph(
            text=text,
            is_bold=is_bold,
            has_highlight=has_highlight,
            index=idx,
        ))

    return result


def extract_text_from_doc(path: Path) -> str:
    # Try catdoc first, fall back to macOS textutil
    last_error: str = ""
    for cmd in (["catdoc", str(path)], ["textutil", "-convert", "txt", "-stdout", str(path)]):
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, check=True)
            return normalize_whitespace(proc.stdout)
        except FileNotFoundError:
            continue
        except subprocess.CalledProcessError as exc:
            last_error = (exc.stderr or exc.stdout or "").strip() or "unknown error"
            continue  # try next tool
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
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))
