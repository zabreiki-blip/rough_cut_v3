from __future__ import annotations

from pathlib import Path

import pytest

from stage1.normalization_models import VALID_DECISION_SOURCES
from stage1.normalization_memory import DEFAULT_MEMORY_PATH
from stage1.normalizer import NormalizationValidationError, save_normalization


def _payload(source: str) -> dict:
    return {
        "source": "projects/demo/input/scenario.txt",
        "source_hash": "a" * 64,
        "status": "draft",
        "revision": 1,
        "blocks": [
            {
                "uid": "block-1",
                "order": 1,
                "original_text": "ДИКТОР: Текст",
                "normalized_text": "Текст",
                "text_for_tts": "Текст",
                "type": "voiceover",
                "source": source,
            }
        ],
    }


@pytest.mark.parametrize("decision_source", sorted(VALID_DECISION_SOURCES))
def test_all_declared_decision_sources_are_accepted(
    tmp_path: Path,
    decision_source: str,
) -> None:
    saved = save_normalization(
        tmp_path / f"{decision_source}.json",
        _payload(decision_source),
    )

    assert saved["blocks"][0]["source"] == decision_source


def test_unknown_decision_source_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(NormalizationValidationError, match="invalid source 'browser'"):
        save_normalization(tmp_path / "invalid.json", _payload("browser"))


def test_default_memory_path_is_independent_of_working_directory(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)

    assert DEFAULT_MEMORY_PATH.is_absolute()
    assert DEFAULT_MEMORY_PATH.name == "normalization_memory.sqlite3"
    assert DEFAULT_MEMORY_PATH.parent.name == "data"
