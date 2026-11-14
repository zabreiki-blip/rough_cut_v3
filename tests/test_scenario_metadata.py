from __future__ import annotations

import pytest

from stage1.scenario_metadata import detect_production_metadata


@pytest.mark.parametrize(
    "text",
    [
        "БУЧА: КРОВАВЫЙ СПЕКТАКЛЬ (рабочее название)",
        "ANY PROJECT: UNTITLED INVESTIGATION (working title)",
        "ЭФИР 17.11.2026",
        "Дата эфира: 2026-11-17",
        "ЗК 0 ИНФОРМАЦИОННЫЙ ПОВОД.",
        "ЗК 0 ТЕМА ВЫПУСКА",
    ],
)
def test_detects_generic_production_metadata(text: str) -> None:
    assert detect_production_metadata(text, first_block=True)


def test_working_title_note_requires_opening_block() -> None:
    text = "НЕИЗВЕСТНЫЙ ПРОЕКТ: КРОВАВЫЙ СПЕКТАКЛЬ (рабочее название)"
    assert detect_production_metadata(text, first_block=True)
    assert not detect_production_metadata(text)


@pytest.mark.parametrize(
    "text",
    [
        "ИВАНОВ: Я пришёл сюда вчера.",
        "СНХ ИВАНОВ: Я пришёл сюда вчера.",
        "ЗК: ИНФОРМАЦИОННЫЙ ПОВОД СТАЛ ЯСЕН ПОЗЖЕ.",
        "ЗК: БЕЛАРУСЬ. САРЬЯ. ХРАМ.",
        "Сегодня мы обсудим информационный повод выпуска.",
        "ЗК 0 В этом году состоялась премьера.",
    ],
)
def test_preserves_speech_and_ordinary_prose(text: str) -> None:
    assert not detect_production_metadata(text, first_block=True)


@pytest.mark.parametrize(
    "text",
    [
        "ЭФИР 17.11.2026 Мы говорим в прямом эфире.",
        "ЭФИР 17.11.2026\nСлова диктора",
        "ИВАНОВ: Я ПРИШЁЛ СЮДА ВЧЕРА",
        "СНХ ИВАНОВ: У ФИЛЬМА ЕСТЬ (рабочее название)",
        "ЗК: Моя история (working title)",
        "0 ИНФОРМАЦИОННЫЙ ПОВОД.\nРеальное повествование",
    ],
)
def test_never_discards_speech_or_content_after_metadata_like_prefix(text: str) -> None:
    assert not detect_production_metadata(text, first_block=True)


def test_recognizes_generic_project_title_label_with_working_title_note() -> None:
    assert detect_production_metadata(
        "ПРОЕКТ: ЛЕС: ИСТОРИЯ ОДНОГО РЕШЕНИЯ (рабочее название)",
        first_block=True,
    )


@pytest.mark.parametrize("text", ["ЛЕС: ИСТОРИЯ ОДНОГО РЕШЕНИЯ", "THE FOREST: A STORY OF CHANGE"])
def test_unmarked_uppercase_title_card_is_ambiguous(text: str) -> None:
    assert not detect_production_metadata(text, first_block=True)
