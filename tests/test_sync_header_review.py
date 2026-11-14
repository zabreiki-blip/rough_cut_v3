"""A reviewed file-credit header can become a real sync quote."""

from pathlib import Path
import json

from stage1.normalizer import (
    generate_normalization,
    save_normalization,
    validate_for_confirmation_detailed,
)
from stage1.normalization_models import NormalizedBlock
import api
from stage2.timeline_parser import is_label_only_sync


def _generate(tmp_path: Path) -> dict:
    source = tmp_path / "scenario.txt"
    source.write_text(
        "Руслан Каленчук, политолог. Файл 1:\n00:01:12 Первая реплика.\n",
        encoding="utf-8",
    )
    return generate_normalization(
        source, tmp_path / "normalized.json", memory_path=tmp_path / "memory.sqlite3", use_llm=False
    )


def test_reviewed_file_header_quote_survives_same_source_regeneration(tmp_path: Path) -> None:
    generated = _generate(tmp_path)
    header = generated["blocks"][0]
    header["normalized_text"] = "Первая реплика."
    header["timecode_start"] = "00:01:12"

    saved = save_normalization(
        tmp_path / "normalized.json", generated, expected_revision=generated["revision"]
    )
    reviewed = saved["blocks"][0]
    assert reviewed["label_only"] is False
    assert not is_label_only_sync(reviewed)

    regenerated = _generate(tmp_path)
    header = regenerated["blocks"][0]
    assert header["normalized_text"] == "Первая реплика."
    assert header["timecode_start"] == "00:01:12"
    assert header["label_only"] is False
    assert not is_label_only_sync(header)
    assert not [
        issue
        for issue in validate_for_confirmation_detailed(regenerated)
        if issue["severity"] == "error"
    ]


def test_metadata_review_keeps_a_genuine_header_context_only(tmp_path: Path) -> None:
    generated = _generate(tmp_path)
    generated["blocks"][0]["role"] = "ведущий эксперт"
    save_normalization(
        tmp_path / "normalized.json", generated, expected_revision=generated["revision"]
    )
    regenerated = _generate(tmp_path)
    assert regenerated["blocks"][0]["role"] == "ведущий эксперт"
    assert regenerated["blocks"][0]["label_only"] is True
    assert "sync_only_credit" not in {
        issue["code"] for issue in validate_for_confirmation_detailed(regenerated)
    }


def test_regeneration_repairs_pre_fix_saved_quote_flag(tmp_path: Path) -> None:
    generated = _generate(tmp_path)
    header = generated["blocks"][0]
    header["normalized_text"] = "Цитата из старого сохранения."
    header["timecode_start"] = "00:01:12"
    header["label_only"] = True
    header["manual_fields"] = ["normalized_text", "timecode_start"]
    header["manually_edited"] = True
    output = tmp_path / "normalized.json"
    output.write_text(json.dumps(generated, ensure_ascii=False), encoding="utf-8")

    regenerated = _generate(tmp_path)
    header = regenerated["blocks"][0]
    assert header["normalized_text"] == "Цитата из старого сохранения."
    assert header["timecode_start"] == "00:01:12"
    assert header["label_only"] is False
    assert not is_label_only_sync(header)
    assert api._sync_alignment_units([header])


def test_client_flag_cannot_hide_a_timed_sync_quote(tmp_path: Path) -> None:
    generated = _generate(tmp_path)
    header = generated["blocks"][0]
    header["label_only"] = True
    header["timecode_start"] = "00:01:12"
    saved = save_normalization(
        tmp_path / "normalized.json", generated, expected_revision=generated["revision"]
    )
    assert saved["blocks"][0]["label_only"] is False


def test_untimed_quote_replacing_header_text_becomes_sync_speech(tmp_path: Path) -> None:
    generated = _generate(tmp_path)
    header = generated["blocks"][0]
    header["normalized_text"] = "Цитата без таймкода."
    saved = save_normalization(
        tmp_path / "normalized.json", generated, expected_revision=generated["revision"]
    )
    assert saved["blocks"][0]["label_only"] is False
    assert not is_label_only_sync(saved["blocks"][0])

    regenerated = _generate(tmp_path)
    assert regenerated["blocks"][0]["normalized_text"] == "Цитата без таймкода."
    assert regenerated["blocks"][0]["label_only"] is False

    header = regenerated["blocks"][0]
    header["normalized_text"] = header["original_text"]
    header["label_only"] = False
    restored = save_normalization(
        tmp_path / "normalized.json", regenerated, expected_revision=regenerated["revision"]
    )
    assert "sync_only_credit" in {
        issue["code"] for issue in validate_for_confirmation_detailed(restored)
    }


def test_retyping_sync_header_clears_sync_context_flag(tmp_path: Path) -> None:
    generated = _generate(tmp_path)
    generated["blocks"][0]["type"] = "voiceover"
    generated["blocks"][0]["normalized_text"] = "Это речь ведущего."
    saved = save_normalization(
        tmp_path / "normalized.json", generated, expected_revision=generated["revision"]
    )
    assert saved["blocks"][0]["label_only"] is False

    regenerated = _generate(tmp_path)
    assert regenerated["blocks"][0]["type"] == "voiceover"
    assert regenerated["blocks"][0]["label_only"] is False


def test_reviewed_bare_header_quote_overrides_legacy_shape_fallback(tmp_path: Path) -> None:
    source = tmp_path / "scenario.txt"
    output = tmp_path / "normalized.json"
    source.write_text("Синх Сторожевой\n00:01:12 Первая реплика.\n", encoding="utf-8")

    def generate() -> dict:
        return generate_normalization(
            source, output, memory_path=tmp_path / "memory.sqlite3", use_llm=False
        )

    generated = generate()
    generated["blocks"][0]["normalized_text"] = "Это новая реплика героя без таймкода."
    saved = save_normalization(output, generated, expected_revision=generated["revision"])
    header = saved["blocks"][0]
    assert header["label_only"] is False
    assert not is_label_only_sync(header)
    assert api._sync_alignment_units([header])

    regenerated = generate()
    header = regenerated["blocks"][0]
    assert header["normalized_text"] == "Это новая реплика героя без таймкода."
    assert header["label_only"] is False
    assert not is_label_only_sync(header)
    assert api._sync_alignment_units([header])


def test_canonicalized_legacy_bare_header_keeps_shape_fallback() -> None:
    legacy = {
        "uid": "legacy",
        "order": 1,
        "type": "sync",
        "original_text": "Синх Сторожевой",
        "normalized_text": "Синх Сторожевой",
        "speaker": "Сторожевой",
    }
    canonical = NormalizedBlock.from_dict(legacy).to_dict()
    assert canonical["label_only"] is False
    assert is_label_only_sync(legacy)
    assert is_label_only_sync(canonical)
