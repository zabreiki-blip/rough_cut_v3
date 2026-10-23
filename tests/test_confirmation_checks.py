"""Severity and scope of the structural confirmation checks."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from stage1.normalization_models import NormalizedBlock, NormalizedScenario
from stage1.normalizer import (
    NormalizationValidationError,
    _apply_llm_results,
    _prepared_projection,
    clean_voiceover_tts_text,
    confirm_normalization,
    generate_normalization,
    save_normalization,
    strip_zk_label,
    validate_for_confirmation_detailed,
)

_FULL = "Первая фраза длинная про пчёл и мёд. Вторая часть рассказывает про улей, воск и нектар."
_CUT = "Первая фраза длинная про пчёл и мёд."


def _block(**fields: Any) -> dict[str, Any]:
    base = {
        "uid": "u1",
        "order": 1,
        "original_text": "x",
        "normalized_text": "x",
        "type": "voiceover",
        "text_for_tts": "x",
        "source": "builtin_rule",
        "confidence": 1.0,
    }
    return {**base, **fields}


def _issues(*blocks: dict[str, Any]) -> list[dict[str, Any]]:
    scenario = {"source": "f.txt", "source_hash": "h", "blocks": list(blocks)}
    return validate_for_confirmation_detailed(scenario)


def _codes(block: dict[str, Any], severity: str = "error") -> set[str]:
    return {i["code"] for i in _issues(block) if i["severity"] == severity}


def test_silent_technical_block_with_service_label_in_tts_is_not_flagged() -> None:
    # UIP_ATOM_001 block 138: a technical cue keeps original_text as TTS but is never voiced.
    block = _block(
        type="technical",
        original_text="ПОДВОДКА № 6",
        normalized_text="ПОДВОДКА № 6",
        text_for_tts="ПОДВОДКА № 6",
    )
    assert "service_label_in_tts" not in _codes(block)


def test_voiced_service_label_in_tts_is_still_blocking() -> None:
    block = _block(original_text="ПОДВОДКА № 6", text_for_tts="ПОДВОДКА № 6")
    assert "service_label_in_tts" in _codes(block)


def test_absorbed_voiceover_with_label_is_not_flagged() -> None:
    block = _block(text_for_tts="ЗК1 Текст", absorbed_by_uid="u0")
    assert "service_label_in_tts" not in _codes(block)


@pytest.mark.parametrize(
    "speech",
    [
        "Лайф в Москве стал дороже.",
        "Синхрон пловцов впечатлил",
        "Директор сказал ЗК: так говорят в студии",
        "зк",
    ],
)
def test_ordinary_speech_in_voiceover_is_not_a_service_label(speech: str) -> None:
    block = _block(original_text=speech, normalized_text=speech, text_for_tts=speech)
    assert "service_label_in_tts" not in {i["code"] for i in _issues(block)}


@pytest.mark.parametrize("tts", ["ЗК: Текст", "ЗК14 В том году", "З.К.: Текст", "ZK Текст"])
def test_leading_zk_label_in_voiceover_tts_is_warning_not_blocking(tts: str) -> None:
    # The ЗК label only marks the voiceover; it is removed from TTS automatically.
    block = _block(original_text=tts, text_for_tts=tts)
    assert "service_label_in_tts" not in _codes(block)
    assert "service_label_in_tts" in _codes(block, "warning")


@pytest.mark.parametrize("label", ["СНХ", "ЛАЙФ", "СТЕНДАП", "ПОДВОДКА"])
def test_other_leading_service_label_in_voiceover_tts_stays_blocking(label: str) -> None:
    block = _block(original_text=f"{label}: Текст", text_for_tts=f"{label}: Текст")
    assert "service_label_in_tts" in _codes(block)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("ЗК: Текст", "Текст"),
        ("ЗК Текст", "Текст"),
        ("ЗК14: Текст", "Текст"),
        ("З.К.: Текст", "Текст"),
        ("зк: текст", "текст"),
        ("ZK Текст", "Текст"),
        ("ЗК - Текст  ", "Текст  "),
        ("ЗК 1915 год", "1915 год"),
        ("ЗК 5:30 утра", "5:30 утра"),
        ("Он сказал ЗК: да", "Он сказал ЗК: да"),
        ("З. Гиппиус", "З. Гиппиус"),
        ("ЗК", "ЗК"),
    ],
)
def test_strip_zk_label_removes_only_leading_label(raw: str, expected: str) -> None:
    assert strip_zk_label(raw) == expected


def test_clean_voiceover_tts_text_strips_zk_label() -> None:
    assert clean_voiceover_tts_text("ЗК14: Текст") == "Текст"
    assert clean_voiceover_tts_text("ЗК: Он ответил: «ЗК»") == "Он ответил: «ЗК»"


def _draft(tmp_path: Path, *blocks: dict[str, Any]) -> Path:
    path = tmp_path / "normalized.json"
    scenario = {"source": "f.txt", "source_hash": "h", "blocks": list(blocks)}
    path.write_text(json.dumps(scenario, ensure_ascii=False), encoding="utf-8")
    return path


def test_save_removes_zk_label_typed_into_tts_and_warns(tmp_path: Path) -> None:
    path = _draft(tmp_path, _block(original_text="Текст", normalized_text="Текст"))
    scenario = json.loads(path.read_text(encoding="utf-8"))
    scenario["blocks"][0]["text_for_tts"] = "ЗК: Текст"
    saved = save_normalization(path, scenario)
    block = saved["blocks"][0]
    assert block["text_for_tts"] == "Текст"
    assert "ЗК label removed from TTS text" in block["warnings"]
    issues = validate_for_confirmation_detailed(saved)
    assert not [i for i in issues if i["severity"] == "error"]
    assert "zk_label_removed" in {i["code"] for i in issues if i["severity"] == "warning"}


def test_prepared_projection_does_not_strip_stored_text_again() -> None:
    # Sanitizing happens once on write (parse/save); a stored ``ЗК РФ ...`` is speech.
    scenario = NormalizedScenario.from_dict(
        {
            "source": "f.txt",
            "source_hash": "h",
            "blocks": [_block(normalized_text="ЗК РФ запрещает", text_for_tts=" ЗК РФ запрещает ")],
        }
    )
    (item,) = _prepared_projection(scenario)["items"]
    assert (item["text_original"], item["text_for_tts"]) == ("ЗК РФ запрещает",) * 2


def test_apply_llm_results_strips_zk_label_from_tts() -> None:
    block = NormalizedBlock.from_dict(
        _block(type="unknown", text_for_tts="", original_text="Он пришёл домой.")
    )
    block.normalized_text = "Он пришёл домой."
    result = [
        {
            "uid": "u1",
            "type": "voiceover",
            "confidence": 0.9,
            "text_for_tts": "ЗК: Он пришёл домой.",
        }
    ]
    _apply_llm_results([block], result, video_cue_re=None, all_blocks=[block])
    assert block.text_for_tts == "Он пришёл домой."


def test_manual_shortening_of_voiceover_is_warning_and_confirmable() -> None:
    for extra in ({"manual_fields": ["normalized_text"]}, {"manual_fields": ["text_for_tts"]}):
        block = _block(
            original_text=_FULL,
            normalized_text=_CUT,
            text_for_tts=_CUT,
            original_label="ЗК",
            **extra,
        )
        assert "unmatched_speech" not in _codes(block)
        assert "unmatched_speech" in _codes(block, "warning")
    legacy = _block(
        original_text=_FULL,
        normalized_text=_CUT,
        text_for_tts=_CUT,
        original_label="ЗК",
        source="manual",
    )
    assert "unmatched_speech" in _codes(legacy, "warning")
    assert "unmatched_speech" not in _codes(legacy)


def test_unrelated_manual_edit_does_not_excuse_automatic_loss() -> None:
    block = _block(
        original_text=_FULL,
        normalized_text=_CUT,
        text_for_tts=_CUT,
        original_label="ЗК",
        manual_fields=["speaker"],
        manually_edited=True,
    )
    assert "unmatched_speech" in _codes(block)


def test_automatic_loss_is_blocking() -> None:
    block = _block(
        original_text=_FULL, normalized_text=_CUT, text_for_tts=_CUT, original_label="ЗК"
    )
    assert "unmatched_speech" in _codes(block)


def _generated(tmp_path: Path) -> tuple[Path, dict[str, Any]]:
    source = tmp_path / "scenario.txt"
    source.write_text("ЗК " + _FULL + "\n", encoding="utf-8")
    output = tmp_path / "normalized.json"
    generate_normalization(source, output, memory_path=tmp_path / "m.sqlite3", use_llm=False)
    return output, json.loads(output.read_text(encoding="utf-8"))


def _confirm(tmp_path: Path, output: Path) -> dict[str, Any]:
    return confirm_normalization(
        output, tmp_path / "prepared.json", tmp_path / "raw.txt", memory_path=tmp_path / "m.sqlite3"
    )


def test_confirm_passes_with_manual_shortening_and_warnings(tmp_path: Path) -> None:
    output, data = _generated(tmp_path)
    block = data["blocks"][0]
    block.update(normalized_text=_CUT, text_for_tts=_CUT, manual_fields=["normalized_text"])
    block["warnings"] = ["Stand-up label: speaker is not identified, check manually"]
    output.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    assert _confirm(tmp_path, output)["status"] == "confirmed"


def test_validation_error_issues_exclude_warnings(tmp_path: Path) -> None:
    output, data = _generated(tmp_path)
    block = data["blocks"][0]
    block.update(normalized_text=_CUT, text_for_tts=_CUT, manual_fields=[])
    block["warnings"] = ["Stand-up label: speaker is not identified, check manually"]
    output.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(NormalizationValidationError) as raised:
        _confirm(tmp_path, output)
    issues = raised.value.issues
    assert {i["code"] for i in issues} == {"unmatched_speech"}
    assert all(i["severity"] == "error" for i in issues)
    assert not any("Stand-up" in message for message in raised.value.errors)


def test_zk_label_is_removed_exactly_once_on_generation(tmp_path: Path) -> None:
    source = tmp_path / "s.txt"
    source.write_text("ЗК: ЗК РФ запрещает это.\n\nЗК14: Он сказал.\n", encoding="utf-8")
    scenario = generate_normalization(
        source, tmp_path / "n.json", memory_path=tmp_path / "m.sqlite3", use_llm=False
    )
    first, second = scenario["blocks"]
    assert first["text_for_tts"] == first["normalized_text"] == "ЗК РФ запрещает это."
    assert second["text_for_tts"] == "Он сказал."
    (item, _other) = _prepared_projection(NormalizedScenario.from_dict(scenario))["items"]
    assert item["text_for_tts"] == "ЗК РФ запрещает это."


def test_clean_voiceover_tts_text_is_single_pass_and_idempotent_on_result() -> None:
    assert clean_voiceover_tts_text("ЗК: ЗК РФ запрещает это.") == "ЗК РФ запрещает это."
    assert clean_voiceover_tts_text("ЗК: ЗК РФ запрещает это.", strip_zk=False) == (
        "ЗК: ЗК РФ запрещает это."
    )


def test_save_keeps_unchanged_speech_that_starts_with_zk_and_strips_typed_label(
    tmp_path: Path,
) -> None:
    path = _draft(
        tmp_path,
        _block(
            original_text="ЗК: ЗК РФ запрещает это.",
            normalized_text="ЗК РФ запрещает это.",
            text_for_tts="ЗК РФ запрещает это.",
        ),
    )
    scenario = json.loads(path.read_text(encoding="utf-8"))
    saved = save_normalization(path, scenario)
    block = saved["blocks"][0]
    assert block["text_for_tts"] == block["normalized_text"] == "ЗК РФ запрещает это."
    assert "ЗК label removed from TTS text" not in block["warnings"]
    saved["blocks"][0]["text_for_tts"] = "ЗК: ЗК РФ запрещает это!"
    again = save_normalization(path, saved)
    assert again["blocks"][0]["text_for_tts"] == "ЗК РФ запрещает это!"


@pytest.mark.parametrize("tts", ["ЗК СНХ воздух", "ЗК", "ЗК:"])
def test_zk_label_left_in_voiced_tts_is_blocking(tmp_path: Path, tts: str) -> None:
    # The sanitizer deliberately keeps conflicts and bare labels, so they would be voiced.
    path = _draft(tmp_path, _block(original_text="Текст", normalized_text="Текст"))
    scenario = json.loads(path.read_text(encoding="utf-8"))
    scenario["blocks"][0]["text_for_tts"] = tts
    saved = save_normalization(path, scenario)
    assert saved["blocks"][0]["text_for_tts"] == tts
    errors = {
        i["code"] for i in validate_for_confirmation_detailed(saved) if i["severity"] == "error"
    }
    assert "service_label_in_tts" in errors


@pytest.mark.parametrize("block_type", ["sync", "host", "life", "technical"])
def test_silent_block_types_never_reach_the_voiced_projection(block_type: str) -> None:
    # Only non-absorbed voiceover TTS is exported, so other types are not gated.
    block = _block(
        type=block_type,
        original_text="СНХ 3 текст",
        normalized_text="текст",
        text_for_tts="СНХ 3 текст",
        speaker="Иван",
    )
    assert "service_label_in_tts" not in _codes(block)
    scenario = NormalizedScenario.from_dict(
        {"source": "f.txt", "source_hash": "h", "blocks": [block]}
    )
    assert _prepared_projection(scenario)["items"] == []


def test_automatic_zk_removal_on_save_is_not_a_manual_edit(tmp_path: Path) -> None:
    path = _draft(
        tmp_path, _block(original_text="Текст", normalized_text="Текст", text_for_tts="Текст")
    )
    scenario = json.loads(path.read_text(encoding="utf-8"))
    scenario["blocks"][0]["text_for_tts"] = "ЗК: Текст"
    block = save_normalization(path, scenario)["blocks"][0]
    assert block["text_for_tts"] == "Текст"
    assert "ЗК label removed from TTS text" in block["warnings"]
    assert block["manual_fields"] == []
    assert block["manually_edited"] is False
    # A real edit is still a manual one.
    scenario = json.loads(path.read_text(encoding="utf-8"))
    scenario["blocks"][0]["text_for_tts"] = "ЗК: Другой текст"
    block = save_normalization(path, scenario)["blocks"][0]
    assert "text_for_tts" in block["manual_fields"]
