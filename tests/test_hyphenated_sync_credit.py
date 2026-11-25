"""Hyphen-separated СНХ markers still carry the following timed speaker credit."""

from pathlib import Path

import pytest

from stage1.normalizer import generate_normalization, validate_for_confirmation_detailed


@pytest.mark.parametrize(
    ("marker", "speaker"),
    [
        ("СНХ-МИРОШНИК", "МИРОШНИК"),
        ("СНХ - МИРОШНИК", "МИРОШНИК"),
        ("СНХ–РОЖИН", "РОЖИН"),
        ("СНХ — РОЖИН", "РОЖИН"),
        ("СНХ-ИВАНОВ-ПЕТРОВ", "ИВАНОВ-ПЕТРОВ"),
    ],
)
def test_hyphenated_snh_timed_credit_extracts_speaker_and_splice(
    tmp_path: Path, marker: str, speaker: str
) -> None:
    source = tmp_path / "scenario.txt"
    source.write_text(
        f"{marker} 00:00:57 — Первый ответ + 00:01:08 — Второй ответ.\n",
        encoding="utf-8",
    )

    result = generate_normalization(
        source,
        tmp_path / "normalized.json",
        memory_path=tmp_path / "memory.sqlite3",
        use_llm=False,
    )

    block = result["blocks"][0]
    assert block["type"] == "sync"
    assert block["speaker"] == speaker
    assert [part["timecode_start"] for part in block["sync_fragments"]] == [
        "00:00:57",
        "00:01:08",
    ]
    assert [part["text"] for part in block["sync_fragments"]] == [
        "Первый ответ",
        "Второй ответ.",
    ]
    assert not any(
        issue["code"] == "sync_no_speaker" for issue in validate_for_confirmation_detailed(result)
    )


def test_hyphenated_snh_service_label_does_not_become_speaker(tmp_path: Path) -> None:
    source = tmp_path / "scenario.txt"
    source.write_text("СНХ-СТЕНДАП 00:00:57 — Ответ.\n", encoding="utf-8")

    result = generate_normalization(
        source,
        tmp_path / "normalized.json",
        memory_path=tmp_path / "memory.sqlite3",
        use_llm=False,
    )

    assert result["blocks"][0]["speaker"] == ""


def test_hyphenated_snh_file_header_extracts_speaker(tmp_path: Path) -> None:
    source = tmp_path / "scenario.txt"
    source.write_text(
        "СНХ-МАРКОВ. Файл запись.mp3 00:00:57 — Ответ.\n",
        encoding="utf-8",
    )

    result = generate_normalization(
        source,
        tmp_path / "normalized.json",
        memory_path=tmp_path / "memory.sqlite3",
        use_llm=False,
    )

    assert result["blocks"][0]["speaker"] == "МАРКОВ"


@pytest.mark.parametrize(
    "recording",
    ["Иванов (ZOOM0001_Tr1 (6).WAV)", "Иванов 1", "Иван Иванов военный эксперт"],
)
def test_dash_credit_joins_recording_header_with_timed_quote(
    tmp_path: Path, recording: str
) -> None:
    source = tmp_path / "scenario.txt"
    source.write_text(
        f"СНХ Иван ИВАНОВ — {recording}\n00:00:22 — Первый ответ.\n",
        encoding="utf-8",
    )

    result = generate_normalization(
        source,
        tmp_path / "normalized.json",
        memory_path=tmp_path / "memory.sqlite3",
        use_llm=False,
    )

    (block,) = result["blocks"]
    assert block["type"] == "sync"
    assert block["speaker"] == "Иван ИВАНОВ"
    assert block["source_description"] == recording
    assert not block["label_only"]
    (fragment,) = block["sync_fragments"]
    assert fragment["timecode_start"] == "00:00:22"
    assert fragment["text"] == "Первый ответ."
    assert not any(
        issue["severity"] == "error" for issue in validate_for_confirmation_detailed(result)
    )
