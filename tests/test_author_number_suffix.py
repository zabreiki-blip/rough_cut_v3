from __future__ import annotations

from pathlib import Path

import pytest

from stage1.normalizer import generate_normalization
from stage1.scenario_structure import find_repeated_author_numbers, parse_paragraph


@pytest.mark.parametrize(
    ("text", "number", "speech"),
    [
        ("ЗК-16", "16", ""),
        ("ЗК 16-Б", "16Б", ""),
        ("ЗК 16 - В", "16В", ""),
        ("ЗК 16 Г", "16Г", ""),
        ("ЗК 16 Г  ", "16Г", ""),
        ("ЗК-23-Ё: Текст.", "23Ё", "Текст."),
        ("ЗК23а", "23А", ""),
        ("ЗК 23а: текст", "23А", "текст"),
        ("ЗК 23-а", "23А", ""),
        ("ЗК-16-б", "16Б", ""),
        ("ЗК 23-а: Текст.", "23А", "Текст."),
        ("ЗК 23‐А", "23А", ""),
        ("ЗК 23‑А", "23А", ""),
        ("ЗК 23‒А", "23А", ""),
        ("ЗК‑23‑а", "23А", ""),
        ("ЗК‐16‐Б", "16Б", ""),
        ("ЗК23 А потом продолжил.", "23", "А потом продолжил."),
        ("ЗК23 а потом продолжил.", "23", "а потом продолжил."),
        ("ЗК 23 — А потом продолжил.", "23", "А потом продолжил."),
        ("ЗК 23 - А потом продолжил.", "23", "А потом продолжил."),
        ("ЗК 23 – В начале было...", "23", "В начале было..."),
    ],
)
def test_cyrillic_author_suffix_is_part_of_number(text: str, number: str, speech: str) -> None:
    parsed = parse_paragraph(text)
    assert parsed.author_number == number
    assert parsed.speech == speech


def test_suffixes_are_distinct_but_exact_full_number_repeats() -> None:
    structures = [
        parse_paragraph(text)
        for text in ("ЗК-23: a", "ЗК 23-а: b", "ЗК 23-Б: c", "ЗК-23-В: d", "ЗК23-А: e")
    ]
    repeats = find_repeated_author_numbers(structures)
    assert repeats == {("ЗК", "23А"): [1, 4]}


def test_attached_lowercase_suffix_matches_uppercase_and_keeps_yo_distinct() -> None:
    structures = [
        parse_paragraph(text)
        for text in (
            "ЗК23а: one",
            "ЗК23-А: two",
            "ЗК23А: three",
            "ЗК23Е: four",
            "ЗК23Ё: five",
        )
    ]
    assert [item.author_number for item in structures] == ["23А", "23А", "23А", "23Е", "23Ё"]
    assert find_repeated_author_numbers(structures) == {("ЗК", "23А"): [0, 1, 2]}


def test_generate_normalization_keeps_bare_suffixed_headers_out_of_tts(tmp_path: Path) -> None:
    source = tmp_path / "scenario.txt"
    source.write_text(
        "ЗК-16\nТекст шестнадцать.\nЗК 16-Б\nТекст шестнадцать Б.\n"
        "ЗК 23-А: Текст двадцать три А.\nЗК 23-Б: Текст двадцать три Б.\n"
        "ЗК23-А: Повторный номер А.\nЗК‑24‑а\nТекст с сохранённым заголовком.\n",
        encoding="utf-8",
    )
    blocks = generate_normalization(
        source,
        tmp_path / "normalized.json",
        memory_path=tmp_path / "memory.sqlite3",
        use_llm=False,
    )["blocks"]
    headers = [blocks[0], blocks[2]]
    assert all(b["notes"] == "section_marker_for=voiceover" for b in headers)
    assert all(b["text_for_tts"] == "" for b in headers)
    assert blocks[1]["text_for_tts"] == "Текст шестнадцать."
    assert blocks[3]["text_for_tts"] == "Текст шестнадцать Б."
    assert blocks[4]["text_for_tts"] == "Текст двадцать три А."
    assert blocks[5]["text_for_tts"] == "Текст двадцать три Б."
    assert blocks[6]["text_for_tts"] == "Повторный номер А."
    assert blocks[7]["text_for_tts"] == ""
    assert "ЗК‑24‑а" in blocks[7]["original_text"]
    assert blocks[8]["text_for_tts"] == "Текст с сохранённым заголовком."
    assert not any(
        "Repeated author number" in w
        for index in (0, 1, 2, 3, 5)
        for w in blocks[index]["warnings"]
    )
    repeat_warning = (
        "Repeated author number ЗК23А (2 occurrences, first in block 5): check numbering"
    )
    assert repeat_warning in blocks[4]["warnings"]
    assert repeat_warning in blocks[6]["warnings"]
