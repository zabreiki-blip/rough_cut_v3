from __future__ import annotations

import json
from pathlib import Path

import pytest

from stage1.normalizer import (
    clean_voiceover_splice_markers,
    clean_voiceover_tts_text,
    confirm_normalization,
    generate_normalization,
    save_normalization,
    strip_tts_splice_markers,
)
from stage1.normalization_models import NormalizedBlock


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("текст СКЛ продолжение", "текст продолжение"),
        ("СКЛ текст", "текст"),
        ("текст\nСКЛ\nпродолжение", "текст\n\nпродолжение"),
        ("СКЛ, текст", ", текст"),
        ("текст  СКЛ   продолжение", "текст продолжение"),
        ("Первая. СКЛ Вторая.", "Первая. Вторая."),
        ("А СКЛ СКЛ Б", "А Б"),
        ("СКЛАД склон скл СКЛирован", "СКЛАД склон скл СКЛирован"),
        ("СНХ текст", "СНХ текст"),
    ],
)
def test_strip_tts_splice_markers_is_narrow_and_idempotent(raw: str, expected: str) -> None:
    cleaned = strip_tts_splice_markers(raw)
    assert cleaned == expected
    assert strip_tts_splice_markers(cleaned) == expected


def test_clean_voiceover_tts_text_removes_inline_skl() -> None:
    assert clean_voiceover_tts_text("Первая фраза СКЛ вторая фраза") == (
        "Первая фраза вторая фраза"
    )


def test_save_cleans_unchanged_legacy_splice_both_speech_fields(tmp_path: Path) -> None:
    path = tmp_path / "normalized.json"
    data = {
        "source": "source.txt",
        "source_hash": "hash",
        "blocks": [
            {
                "uid": "u1",
                "order": 1,
                "original_text": "Текст",
                "normalized_text": "Первая СКЛ вторая",
                "text_for_tts": "Первая СКЛ вторая",
                "type": "voiceover",
            }
        ],
    }
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    saved = save_normalization(path, data)
    block = saved["blocks"][0]
    assert block["normalized_text"] == block["text_for_tts"] == "Первая вторая"
    assert not any("ЗК label removed" in warning for warning in block["warnings"])


def test_generation_cleans_inline_skl(tmp_path: Path) -> None:
    source = tmp_path / "scenario.txt"
    source.write_text("Первая фраза СКЛ вторая фраза.\n", encoding="utf-8")
    result = generate_normalization(
        source,
        tmp_path / "normalized.json",
        memory_path=tmp_path / "memory.sqlite3",
        use_llm=False,
    )
    block = result["blocks"][0]
    assert "СКЛ" not in block["text_for_tts"]


def test_confirm_cleans_and_persists_legacy_splices_before_export(tmp_path: Path) -> None:
    source = tmp_path / "scenario.txt"
    source.write_text("Обычный исходник.\n", encoding="utf-8")
    output = tmp_path / "normalized.json"
    generated = generate_normalization(
        source, output, memory_path=tmp_path / "memory.sqlite3", use_llm=False
    )
    block = generated["blocks"][0]
    block["type"] = "voiceover"
    block["normalized_text"] = block["text_for_tts"] = "Первая СКЛ вторая."
    output.write_text(json.dumps(generated, ensure_ascii=False), encoding="utf-8")

    result = confirm_normalization(
        output,
        tmp_path / "prepared.json",
        tmp_path / "raw.txt",
        memory_path=tmp_path / "memory.sqlite3",
    )

    saved = json.loads(output.read_text(encoding="utf-8"))["blocks"][0]
    assert saved["normalized_text"] == saved["text_for_tts"] == "Первая вторая."
    assert result["status"] == "confirmed"
    prepared = json.loads((tmp_path / "prepared.json").read_text(encoding="utf-8"))
    assert prepared["items"][0]["text_for_tts"] == "Первая вторая."


def test_block_cleanup_excludes_non_voiceover_and_absorbed() -> None:
    ordinary = NormalizedBlock.from_dict(
        {
            "uid": "a",
            "order": 1,
            "type": "sync",
            "original_text": "СКЛ текст",
            "normalized_text": "СКЛ текст",
            "text_for_tts": "СКЛ текст",
        }
    )
    absorbed = NormalizedBlock.from_dict(
        {
            "uid": "b",
            "order": 2,
            "type": "voiceover",
            "original_text": "СКЛ текст",
            "absorbed_by_uid": "a",
            "normalized_text": "СКЛ текст",
            "text_for_tts": "СКЛ текст",
        }
    )
    clean_voiceover_splice_markers([ordinary, absorbed])
    assert ordinary.normalized_text == ordinary.text_for_tts == "СКЛ текст"
    assert absorbed.normalized_text == absorbed.text_for_tts == "СКЛ текст"
