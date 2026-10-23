"""Label-only sync headers, glued ``Синх <Имя><цитата>`` labels and service-label checks."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from stage1.normalization_models import NormalizedBlock
from stage1.normalizer import generate_normalization, validate_for_confirmation_detailed
from stage1.scenario_structure import (
    DEFAULT_MARKER_TYPES,
    SYNC_LABEL_ONLY_RE,
    parse_paragraph as parse_paragraph_structure,
)
from stage2.timeline_parser import parse_normalized_timeline


def _normalize(tmp_path: Path, *paragraphs: str) -> dict:
    source = tmp_path / "scenario.txt"
    source.write_text("\n".join(paragraphs) + "\n", encoding="utf-8")
    return generate_normalization(
        source,
        tmp_path / "normalized.json",
        memory_path=tmp_path / "memory.sqlite3",
        use_llm=False,
    )


# --- A: label-only sync header is context, not a clip --------------------------------


def test_label_only_sync_block_is_flagged_and_survives_serialization(tmp_path: Path) -> None:
    result = _normalize(
        tmp_path,
        "Синх Смольникова",
        "00:00:24 - До предложения сняться в кино я только знала, что это ужасная женщина.",
    )
    header, quote = result["blocks"]
    assert header["type"] == "sync"
    assert header["label_only"] is True
    assert header["speaker"] == "Смольникова"
    assert quote["label_only"] is False
    assert quote["speaker"] == "Смольникова"
    saved = json.loads((tmp_path / "normalized.json").read_text(encoding="utf-8"))
    assert saved["blocks"][0]["label_only"] is True
    assert NormalizedBlock.from_dict(saved["blocks"][0]).label_only is True
    assert NormalizedBlock.from_dict({**saved["blocks"][1]}).label_only is False
    # Older JSON without the key still loads.
    legacy = dict(saved["blocks"][0])
    legacy.pop("label_only")
    assert NormalizedBlock.from_dict(legacy).label_only is False


def test_label_only_sync_block_makes_no_timeline_segment(tmp_path: Path) -> None:
    result = _normalize(
        tmp_path,
        "Синх Смольникова",
        "00:00:24 - До предложения сняться в кино я только знала, что это ужасная женщина.",
    )
    result["status"] = "confirmed"
    timeline = parse_normalized_timeline(result, tmp_path, tmp_path, "TEST")
    snh = [segment for segment in timeline.segments if segment.type == "snh"]
    assert len(snh) == 1
    assert snh[0].speaker == "Смольникова"
    assert snh[0].text.startswith("До предложения")


def test_legacy_label_only_block_without_flag_makes_no_timeline_segment(tmp_path: Path) -> None:
    result = _normalize(tmp_path, "Синх Смольникова", "00:00:24 - Первая реплика интервью.")
    result["blocks"][0].pop("label_only", None)
    result["status"] = "confirmed"
    timeline = parse_normalized_timeline(result, tmp_path, tmp_path, "TEST")
    assert [segment.type for segment in timeline.segments] == ["snh"]


def test_sync_block_with_own_quote_is_not_label_only(tmp_path: Path) -> None:
    result = _normalize(tmp_path, "Синх Смольникова: 00:00:24 - Реплика с цитатой в той же строке.")
    block = result["blocks"][0]
    assert block["type"] == "sync"
    assert block["label_only"] is False


# --- B: label glued to the quote ------------------------------------------------------


def test_glued_label_and_quote_is_parsed_as_sync(tmp_path: Path) -> None:
    result = _normalize(
        tmp_path,
        "ЗК. Фильм о Лиле Брик вышел в этом году.",
        "Синх СторожевойТо, что получилось у нас, это живая история. "
        "СКЛ это живые люди. Обаятельные, необаятельные.",
    )
    block = result["blocks"][1]
    assert block["type"] == "sync"
    assert block["speaker"] == "Сторожевой"
    assert block["text_for_tts"] == ""
    assert block["label_only"] is False
    texts = [fragment["text"] for fragment in block["sync_fragments"]]
    assert texts[0].startswith("То, что получилось у нас")
    assert all("Синх" not in text and "СКЛ" not in text for text in texts)
    assert len(texts) == 2
    assert validate_for_confirmation_detailed(result) == []


def test_spaced_label_with_splice_marker_is_parsed_as_sync(tmp_path: Path) -> None:
    result = _normalize(
        tmp_path,
        "ЗК. Фильм о Лиле Брик вышел в этом году.",
        "Синх Сторожева СКЛ Эльза привела Маяковского СКЛ к Лиле домой.",
    )
    block = result["blocks"][1]
    assert block["type"] == "sync"
    assert block["speaker"] == "Сторожева"
    assert block["text_for_tts"] == ""
    assert [fragment["text"] for fragment in block["sync_fragments"]] == [
        "Эльза привела Маяковского",
        "к Лиле домой.",
    ]


def test_ambiguous_label_inside_voiceover_paragraph_blocks_confirmation(tmp_path: Path) -> None:
    result = _normalize(
        tmp_path,
        "ЗК. Фильм о Лиле Брик вышел в этом году.",
        "Синх Сторожева Эльза привела Маяковского к Лиле домой.",
    )
    block = result["blocks"][1]
    assert block["type"] != "sync" or block["speaker"] == "Сторожева"
    errors = [i for i in validate_for_confirmation_detailed(result) if i["severity"] == "error"]
    assert errors and all(i["order"] == 2 for i in errors)


@pytest.mark.parametrize(
    "paragraph",
    [
        "Синхронизация данных прошла без сбоев.",
    ],
)
def test_ordinary_words_starting_with_sinkh_are_not_sync(tmp_path: Path, paragraph: str) -> None:
    block = _normalize(tmp_path, "ЗК. Вводная фраза для контекста.", paragraph)["blocks"][1]
    assert block["type"] != "sync"


# --- C: confirmation checks ------------------------------------------------------------


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


def _issues(block: dict[str, Any]) -> list[dict[str, Any]]:
    scenario = {"source": "f.txt", "source_hash": "h", "blocks": [block]}
    return validate_for_confirmation_detailed(scenario)


def _error_codes(block: dict[str, Any]) -> set[str]:
    return {i["code"] for i in _issues(block) if i["severity"] == "error"}


@pytest.mark.parametrize(
    "tts",
    [
        "Синх СторожевойТо, что получилось у нас, это живая история.",
        "СИНХ Смольникова Это живая история.",
        "Синхрон Сторожевой: это живая история.",
        "снх Сторожева Эльза привела Маяковского к Лиле домой.",
        "Эльза привела Маяковского СКЛ к Лиле домой, и попросили почитать стихи.",
        "Эльза привела Маяковского (скл) к Лиле домой, и попросили почитать стихи.",
        "Это живая история. СНХ Сторожева. Дальше обычный закадровый текст.",
    ],
)
def test_sync_labels_anywhere_in_voiceover_tts_block(tts: str) -> None:
    block = _block(original_text=tts, normalized_text=tts, text_for_tts=tts)
    assert "service_label_in_tts" in _error_codes(block)


@pytest.mark.parametrize(
    "tts",
    [
        "НА БЭКСТЕЙДЖЕ",
        "КАДРЫ ИЗ ФИЛЬМА, СТАКАН ПАДАЕТ",
        "СЮДА НУЖНО СНХ. МОЖЕТ ЧТО-ТО ПРО СТАЛИНА (КОРОТКО!)",
        "РЕКОНСТРУКЦИЯ ПИСЬМА",
    ],
)
def test_all_caps_montage_note_in_voiceover_tts_blocks(tts: str) -> None:
    block = _block(
        original_label="ЗК.",
        original_text=f"ЗК. {tts}",
        normalized_text=tts,
        text_for_tts=tts,
    )
    assert "montage_note_in_tts" in _error_codes(block)


@pytest.mark.parametrize("tts", ["ФСБ РФ", "НАТО США", "НЕТ НЕТ"])
def test_all_caps_text_without_montage_word_is_only_warning(tts: str) -> None:
    block = _block(original_text=tts, normalized_text=tts, text_for_tts=tts)
    codes = {i["code"]: i["severity"] for i in _issues(block)}
    assert codes.get("montage_note_in_tts") == "warning"
    assert "montage_note_in_tts" not in _error_codes(block)


@pytest.mark.parametrize("tts", ["КАДРЫ ИЗ ФИЛЬМА, СТАКАН ПАДАЕТ", "РЕКОНСТРУКЦИЯ ПИСЬМА"])
def test_all_caps_montage_word_without_zk_label_blocks(tts: str) -> None:
    block = _block(original_text=tts, normalized_text=tts, text_for_tts=tts)
    assert "montage_note_in_tts" in _error_codes(block)


def test_all_caps_montage_note_after_manual_review_is_only_a_warning() -> None:
    block = _block(
        text_for_tts="НА БЭКСТЕЙДЖЕ",
        normalized_text="НА БЭКСТЕЙДЖЕ",
        manual_fields=["text_for_tts"],
        manually_edited=True,
    )
    codes = {i["code"]: i["severity"] for i in _issues(block)}
    assert codes.get("montage_note_in_tts") == "warning"


@pytest.mark.parametrize(
    "tts",
    [
        "СССР и США подписали договор.",
        "ФБР",
        "Лиля Брик жила в Москве, а потом уехала в Париж.",
        "В 1930 году МХАТ показал спектакль, и весь город говорил только об этом событии.",
        "Синхрон пловцов впечатлил",
        "Склейка кадров заняла несколько дней.",
        "Лайф в Москве стал дороже.",
    ],
)
def test_ordinary_voiceover_is_not_flagged_by_new_checks(tts: str) -> None:
    block = _block(original_text=tts, normalized_text=tts, text_for_tts=tts)
    assert _error_codes(block) == set()


@pytest.mark.parametrize("speaker", ["СЮДА", "КАДРЫ", "ВОЗДУХ НА МУЗЫКЕ", "СЮДА НУЖНО"])
def test_sync_speaker_that_is_a_montage_command_blocks(speaker: str) -> None:
    block = _block(
        type="sync",
        speaker=speaker,
        original_label=speaker.split()[0],
        original_text=f"{speaker} НУЖНО СНХ. Может что-то про Сталина.",
        normalized_text="Может что-то про Сталина.",
        text_for_tts="",
    )
    assert "false_hero_from_cue" in _error_codes(block)


@pytest.mark.parametrize("speaker", ["СТЫЧКИН", "Евгений Стычкин", "Дана Курская"])
def test_sync_speaker_that_is_a_person_is_not_flagged(speaker: str) -> None:
    block = _block(
        type="sync",
        speaker=speaker,
        original_text=f"{speaker}: 00:02:01 конечно, это любовный треугольник.",
        normalized_text="конечно, это любовный треугольник.",
        text_for_tts="",
    )
    assert "false_hero_from_cue" not in _error_codes(block)


# --- D: label-only name must not swallow service labels; glued form after СНХ ----------


@pytest.mark.parametrize(
    ("header", "speaker"),
    [
        ("СИНХ МАКСИМОВА", "МАКСИМОВА"),
        ("Синх Смольникова", "Смольникова"),
        ("СНХ Татьяна Васильева", "Татьяна Васильева"),
        ("Синхрон Ёлкина", "Ёлкина"),
    ],
)
def test_label_only_header_with_name(tmp_path: Path, header: str, speaker: str) -> None:
    block = _normalize(tmp_path, "ЗК. Вводная фраза для контекста.", header)["blocks"][1]
    assert block["type"] == "sync"
    assert block["label_only"] is True
    assert block["speaker"] == speaker


@pytest.mark.parametrize(
    "paragraph",
    [
        "Синх Сторожева СКЛ Эльза",
        "Синх Сторожева СКЛЕЙКА Эльза",
        "СНХ Сторожева ЗК Эльза",
        "Синх Сторожева и тогда мы пошли",
    ],
)
def test_service_label_or_lowercase_word_is_not_part_of_label_only_name(paragraph: str) -> None:
    assert SYNC_LABEL_ONLY_RE.match(paragraph) is None


def test_label_only_regex_keeps_name_without_service_words() -> None:
    match = SYNC_LABEL_ONLY_RE.match("СНХ Татьяна Васильева")
    assert match and match.group(1) == "Татьяна Васильева"
    assert SYNC_LABEL_ONLY_RE.match("синх Смольникова")  # marker is case-insensitive


def test_spaced_label_with_cyrillic_splice_in_name_position(tmp_path: Path) -> None:
    result = _normalize(
        tmp_path,
        "ЗК. Фильм о Лиле Брик вышел в этом году.",
        "Синх Сторожева СКЛ Эльза привела Маяковского СКЛ к Лиле домой.",
    )
    block = result["blocks"][1]
    assert block["type"] == "sync"
    assert block["label_only"] is False
    assert block["speaker"] == "Сторожева"
    assert all("СКЛ" not in f["text"] and "Синх" not in f["text"] for f in block["sync_fragments"])
    assert "СКЛ" not in block["speaker"]


def test_spaced_label_with_lowercase_word_is_not_label_only(tmp_path: Path) -> None:
    block = _normalize(
        tmp_path,
        "ЗК. Фильм о Лиле Брик вышел в этом году.",
        "Синх Сторожева и тогда мы пошли",
    )["blocks"][1]
    assert block["label_only"] is False


@pytest.mark.parametrize("label", ["Синх", "СНХ", "синх", "СИНХ"])
def test_glued_name_after_sync_marker_variants(tmp_path: Path, label: str) -> None:
    result = _normalize(
        tmp_path,
        "ЗК. Фильм о Лиле Брик вышел в этом году.",
        f"{label} СторожевойТо, что получилось у нас, это живая история. СКЛ это живые люди.",
    )
    block = result["blocks"][1]
    assert block["type"] == "sync"
    assert block["speaker"] == "Сторожевой"
    assert block["text_for_tts"] == ""
    assert block["label_only"] is False
    assert block["normalized_text"].startswith("То, что")
    texts = [fragment["text"] for fragment in block["sync_fragments"]]
    assert texts and texts[0].startswith("То, что")
    assert all(
        "Синх" not in t and "СНХ" not in t and "Сторожевой" not in t and "СКЛ" not in t
        for t in texts
    )
    assert validate_for_confirmation_detailed(result) == []


def test_synh_is_a_canonical_sync_marker() -> None:
    structure = parse_paragraph_structure("Синх Иванов: 00:00:10 - Реплика героя.")
    assert structure.label == "СНХ"
    assert DEFAULT_MARKER_TYPES["синх"] == "sync"


@pytest.mark.parametrize("tts", ["СИНХРОН Иванов сказал это в кадре.", "Синхрон Иванов сказал это."])
def test_synhron_name_in_voiceover_tts_blocks(tts: str) -> None:
    block = _block(original_text=tts, normalized_text=tts, text_for_tts=tts)
    assert "service_label_in_tts" in _error_codes(block)


def test_yolkina_is_recognised_as_a_name_in_label_only_regex() -> None:
    match = SYNC_LABEL_ONLY_RE.match("Синх Ёлкина")
    assert match and match.group(1) == "Ёлкина"
