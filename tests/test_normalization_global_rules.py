"""Production rules work from a new source, without saved project repairs."""

from pathlib import Path

import pytest

from stage1.normalizer import (
    generate_normalization,
    reset_normalization,
    save_normalization,
    validate_for_confirmation_detailed,
)


def _generate(root: Path, text: str, *, name: str = "scenario") -> dict:
    source = root / f"{name}.txt"
    source.write_text(text, encoding="utf-8")
    return generate_normalization(
        source, root / f"{name}.json", memory_path=root / "memory.sqlite3", use_llm=False
    )


def test_fresh_generation_separates_metadata_credits_and_speech(tmp_path: Path) -> None:
    text = "\n".join(
        [
            "ТАЙГА: ИСТОРИЯ РЕКИ (рабочее название)",
            "ЭФИР 05.02.2027",
            "ЗК 0 ИНФОРМАЦИОННЫЙ ПОВОД.",
            "ЗК-7-А",
            "Мы начинаем рассказ о большой реке.",
            "Анна Соколова, биолог. Файл интервью 2.wav:",
            "мы изучаем эту реку давно. 00:01:12 Здесь начинается её история.",
            "00:02:14 Продолжение рассказа.",
            "ЗК 7-Б",
            "Этот рассказ продолжается дальше.",
        ]
    )
    first = _generate(tmp_path, text)
    blocks = first["blocks"]
    assert [b["type"] for b in blocks] == [
        "technical",
        "technical",
        "technical",
        "technical",
        "voiceover",
        "sync",
        "sync",
        "sync",
        "technical",
        "voiceover",
    ]
    assert blocks[5]["label_only"] is True
    assert all(not b["timecode_start"] and not b["timecode_end"] for b in blocks[:3])
    assert all(b["speaker"] == "Анна Соколова" for b in blocks[5:8])
    assert all(b["source_description"] == "интервью 2.wav" for b in blocks[5:8])
    assert "мы изучаем эту реку давно." in " ".join(p["text"] for p in blocks[6]["sync_fragments"])
    assert [b["text_for_tts"] for b in blocks if b["type"] == "voiceover"] == [
        "Мы начинаем рассказ о большой реке.",
        "Этот рассказ продолжается дальше.",
    ]
    assert not [i for i in validate_for_confirmation_detailed(first) if i["severity"] == "error"]
    assert not any("Repeated author number" in w for b in blocks for w in b["warnings"])
    # An explicit reset really reparses the source; it must not need old decisions.
    reset_normalization(tmp_path / "scenario.json", expected_revision=first["revision"])
    second = _generate(tmp_path, text)
    projection = lambda doc: [
        (b["type"], b["speaker"], b["source_description"], b["text_for_tts"], b["label_only"])
        for b in doc["blocks"]
    ]
    assert projection(second) == projection(first)
    assert not any(b["manual_fields"] for b in second["blocks"])


@pytest.mark.parametrize(
    "header,speaker,role",
    [
        ("Анна Соколова. Файл интервью 2.wav:", "Анна Соколова", ""),
        ("АННА СОКОЛОВА, биолог. Файл интервью 2.wav:", "АННА СОКОЛОВА", "биолог"),
        ("Анна Соколова, биолог. Анна. Файл интервью 2.wav:", "Анна Соколова", "биолог"),
        ("ИВАН ПЕТРОВ, инженер.Файл запись.wav:", "ИВАН ПЕТРОВ", "инженер"),
        ("Анна Соколова. Анна. Файл интервью 2.wav:", "Анна Соколова", ""),
        (
            "Мария Петрова, инженер по г. Москве. Файл запись.wav:",
            "Мария Петрова",
            "инженер по г. Москве",
        ),
    ],
)
def test_generic_file_credit_forms(tmp_path: Path, header: str, speaker: str, role: str) -> None:
    result = _generate(tmp_path, f"{header}\n00:02:04 Это реплика героя.\n")
    credit, speech = result["blocks"]
    assert credit["label_only"] is True
    assert credit["speaker"] == speech["speaker"] == speaker
    assert credit["role"] == speech["role"] == role
    assert speech["normalized_text"] == "Это реплика героя."
    assert not [i for i in validate_for_confirmation_detailed(result) if i["severity"] == "error"]


@pytest.mark.parametrize(
    "text",
    [
        "История нашего города. Файл 1:",
        "Иван Петров сказал, что мы успели вовремя. Файл 1:",
        "Иван Петров, мы решили вернуться домой. Файл 1:",
    ],
)
def test_file_word_in_prose_does_not_create_credit(tmp_path: Path, text: str) -> None:
    result = _generate(tmp_path, text)
    assert not result["blocks"][0]["label_only"]
    assert not result["blocks"][0]["speaker"]


def test_supplying_source_filename_resolves_warning_across_save_and_regeneration(
    tmp_path: Path,
) -> None:
    text = "СНХ-ПЕТРОВ 00:02:14 — Это реплика героя.\n"
    first = _generate(tmp_path, text)
    warning = "Sync credit has no source media file"
    assert warning in first["blocks"][0]["warnings"]
    first["blocks"][0]["source_description"] = "INTERVIEW.mp3"
    saved = save_normalization(
        tmp_path / "scenario.json", first, expected_revision=first["revision"]
    )
    assert warning not in saved["blocks"][0]["warnings"]
    for _ in range(2):
        regenerated = _generate(tmp_path, text)
        assert regenerated["blocks"][0]["source_description"] == "INTERVIEW.mp3"
        assert warning not in regenerated["blocks"][0]["warnings"]
    # Removing the actual filename must surface the unresolved source again.
    regenerated["blocks"][0]["source_description"] = ""
    emptied = save_normalization(
        tmp_path / "scenario.json", regenerated, expected_revision=regenerated["revision"]
    )
    assert warning in emptied["blocks"][0]["warnings"]


@pytest.mark.parametrize("date", ["10.11.26", "17.11.2026", "2026-11-17"])
def test_date_in_editorial_header_is_never_assigned_as_media_timecode(
    tmp_path: Path, date: str
) -> None:
    result = _generate(tmp_path, f"Название. «Река» Эфир {date}. Выпуск № 2.\n")
    block = result["blocks"][0]
    assert block["type"] == "technical"
    assert block["timecode_start"] == block["timecode_end"] == ""
