from __future__ import annotations

from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

from stage1.io_utils import extract_text_from_doc


def test_doc_extraction_prefers_antiword_without_visual_line_wrapping(tmp_path: Path) -> None:
    source = tmp_path / "scenario.doc"
    source.write_bytes(b"legacy-word-placeholder")
    extracted = (
        "ЗАКАДР: Один длинный логический абзац без искусственных переносов.\n"
        "ИРИНА КОТОВА, ИСТОРИК: 01:22:29. Реплика.\n"
    )

    with patch(
        "stage1.io_utils.subprocess.run",
        return_value=CompletedProcess([], 0, stdout=extracted, stderr=""),
    ) as run:
        result = extract_text_from_doc(source)

    assert run.call_args.args[0] == ["antiword", "-w", "0", str(source)]
    assert result.splitlines() == extracted.strip().splitlines()
