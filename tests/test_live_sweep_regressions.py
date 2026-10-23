"""Regressions from a live-project sweep (depersonalised). No network, no LLM."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from stage1.io_utils import strip_word_field_codes
from stage1.normalizer import (
    TIMECODE_NOTE_WARNING,
    generate_normalization,
    validate_for_confirmation_detailed,
)
from stage1.scenario_context import SCENE_CONTEXT_VERSION

SYNC = "СНХ Иванов Иван (00:10:00) Я пришёл сюда давно."
LIFE = "ЛАЙФ СЦЕНА (00:34:00) Идут по тропинке."
VOICE = "ЗК: Дом стоял на краю деревни."


def _run(tmp_path: Path, paragraphs: list[str]) -> dict[str, Any]:
    source = tmp_path / "scenario.txt"
    source.write_text("\n\n".join(paragraphs) + "\n", encoding="utf-8")
    return generate_normalization(
        source, tmp_path / "out.json", memory_path=tmp_path / "memory.sqlite3", use_llm=False
    )


def _blocks(tmp_path: Path, paragraphs: list[str]) -> list[dict[str, Any]]:
    return _run(tmp_path, paragraphs)["blocks"]


def _codes(result: dict[str, Any], severity: str) -> set[str]:
    return {
        issue["code"]
        for issue in validate_for_confirmation_detailed(result)
        if issue["severity"] == severity
    }


# --- 1. Word field codes -------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            'HYPERLINK "https://www.example.ru/a/b"Хроника Жанны -НТВ и',
            "https://www.example.ru/a/b Хроника Жанны -НТВ и",
        ),
        ('5:50 HYPERLINK "https://x.ru/y" \\l "z"2019 год', "5:50 https://x.ru/y 2019 год"),
        ('HYPERLINK "https://x.ru/a"https://x.ru/a', "https://x.ru/a"),
        ("PAGEREF _Toc123 \\h", ""),
        ('TOC \\o "1-3" \\h \\z Глава', " Глава"),
        ("Текст \\* MERGEFORMAT дальше", "Текст дальше"),
        ("Первая\nPAGE 3\nВторая", "Первая\n\nВторая"),
        (
            "Ordinary PAGE and TOC words, PAGE 12 of text",
            "Ordinary PAGE and TOC words, PAGE 12 of text",
        ),
        ("Обычный текст: ссылка https://x.ru/a", "Обычный текст: ссылка https://x.ru/a"),
    ],
)
def test_strip_word_field_codes(raw: str, expected: str) -> None:
    assert strip_word_field_codes(raw) == expected


# --- 2. Dash reply after SYNC -------------------------------------------


def test_dash_reply_after_sync_is_not_sync(tmp_path: Path) -> None:
    result = _run(tmp_path, [SYNC, "- Чем отличается оплошность от операции?", VOICE])
    sync, reply, _voice = result["blocks"]
    assert reply["type"] == "unknown"
    assert reply["suggested_type"] == "sync"
    assert reply["scene_uid"] == sync["uid"]
    assert reply["speaker"] == ""
    assert any("interviewer question or hero" in w for w in reply["warnings"])
    assert "sync_no_speaker" not in _codes(result, "error")
    assert SCENE_CONTEXT_VERSION >= 3


def test_second_dash_reply_stays_in_the_sync_scene(tmp_path: Path) -> None:
    blocks = _blocks(tmp_path, [SYNC, "- Вопрос первый?", "- Вопрос второй?"])
    assert [b["type"] for b in blocks[1:]] == ["unknown", "unknown"]
    assert blocks[2]["scene_uid"] == blocks[0]["uid"]


def test_dash_reply_after_life_stays_life(tmp_path: Path) -> None:
    blocks = _blocks(tmp_path, [LIFE, "- Вот эту?", "- Да."])
    assert [b["type"] for b in blocks[1:]] == ["life", "life"]
    assert blocks[1]["scene_uid"] == blocks[0]["uid"]


# --- 3. Timecode notes ---------------------------------------------------


@pytest.mark.parametrize("note", ["31-55 кт мозга", "5:50 заметка про героя", "20:30-дискотека"])
def test_unlabelled_timecode_note_is_technical(tmp_path: Path, note: str) -> None:
    block = _blocks(tmp_path, [VOICE, note])[1]
    assert block["type"] == "technical"
    assert TIMECODE_NOTE_WARNING in block["warnings"]


def test_full_timecode_and_splice_keep_sync_behaviour(tmp_path: Path) -> None:
    blocks = _blocks(
        tmp_path,
        [
            VOICE,
            "00:12:30 Я родился в деревне, там и вырос.",
            SYNC,
            "+ 00.43.25 Дальше говорит он.",
        ],
    )
    assert blocks[1]["type"] == "sync"
    assert blocks[3]["type"] == "sync" and blocks[3]["scene_uid"] == blocks[2]["uid"]


# --- 4. СНХ with a common noun as speaker -------------------------------


def test_sync_dot_role_word_colon_gets_speaker(tmp_path: Path) -> None:
    block = _blocks(tmp_path, ["СНХ. Отец: очень похож и талантлив, как мама. Пальцы дедушкины."])[
        0
    ]
    assert block["type"] == "sync"
    assert block["speaker"] == "Отец"


# --- 5. Numbered presenter headings -------------------------------------


@pytest.mark.parametrize("heading", ["ПОДВОДКА № 6", "СТЕНДАП 3", "ПОДВОДКА"])
def test_presenter_heading_without_speech_is_technical(tmp_path: Path, heading: str) -> None:
    result = _run(tmp_path, [VOICE, heading, VOICE])
    block = result["blocks"][1]
    assert block["type"] == "technical"
    assert not any("speaker is not identified" in w for w in block["warnings"])
    assert "sync_no_speaker" not in _codes(result, "error")


def test_presenter_label_with_speech_stays_sync(tmp_path: Path) -> None:
    block = _blocks(tmp_path, [VOICE, "СТЕНДАП 00:10:00 Мы сегодня в студии, и это важно."])[1]
    assert block["type"] == "sync"


# --- 6. sync_no_speaker severity ----------------------------------------


def test_sync_no_speaker_is_error_for_explicit_label_and_splice_inherits_speaker(
    tmp_path: Path,
) -> None:
    explicit = _run(tmp_path, [VOICE, "СТЕНДАП 00:10:00 Мы сегодня в студии, и это важно."])
    assert "sync_no_speaker" in _codes(explicit, "error")
    spliced = _run(tmp_path, [SYNC, "+ 00.43.25 Дальше говорит он."])
    assert spliced["blocks"][1]["type"] == "sync"
    # A ``+`` splice is the same hero's recording: it inherits the speaker.
    assert spliced["blocks"][1]["speaker"] == "Иванов Иван"
    assert "sync_no_speaker" not in _codes(spliced, "error")
    assert "sync_no_speaker" not in _codes(spliced, "warning")


# --- 7. Round 2: speech is never lost silently; doubt stays visible ------


@pytest.mark.parametrize(
    "line",
    [
        "СНХ. Мы пришли: да, давно.",
        "СНХ. Хорошо: пошли",
        "СНХ. Это было давно: да.",
    ],
)
def test_dot_sync_non_credit_stays_speech_without_context_speaker(
    tmp_path: Path, line: str
) -> None:
    result = _run(tmp_path, [SYNC, line])
    block = result["blocks"][1]
    assert block["type"] == "sync"
    assert block["speaker"] == ""
    assert line.split(". ", 1)[1].split(": ", 1)[0] in block["normalized_text"]
    assert ":" in block["normalized_text"]
    assert "sync_no_speaker" in _codes(result, "error")


def test_dot_sync_role_word_credit_still_works_after_other_sync(tmp_path: Path) -> None:
    block = _blocks(tmp_path, [SYNC, "СНХ. Отец: Пальцы дедушкины."])[1]
    assert block["speaker"] == "Отец" and "Пальцы" in block["normalized_text"]


@pytest.mark.parametrize(
    "line",
    [
        "12:30 Мы пришли домой поздно вечером",
        "10-15 человек пришли на встречу",
        "12:30 Мы вернулись домой, и всё было тихо в доме",
    ],
)
def test_timecode_prefixed_speech_is_unknown_not_technical(tmp_path: Path, line: str) -> None:
    result = _run(tmp_path, [VOICE, line])
    block = result["blocks"][1]
    assert block["type"] == "unknown"
    assert TIMECODE_NOTE_WARNING in block["warnings"]
    assert "unknown_type" in _codes(result, "error")


@pytest.mark.parametrize("scene", [SYNC, LIFE])
@pytest.mark.parametrize("line", ["20:30 Первый текст", "5:20 Первый текст без точки"])
def test_short_timecode_inside_open_scene_is_not_a_note(
    tmp_path: Path, scene: str, line: str
) -> None:
    block = _blocks(tmp_path, [scene, line])[1]
    assert block["type"] in {"sync", "life"}
    assert TIMECODE_NOTE_WARNING not in block["warnings"]


@pytest.mark.parametrize(
    "note", ["31-55 кт мозга", "5:50 https://example.ru/video", "20:30-дискотека"]
)
def test_genuine_timecode_notes_stay_technical(tmp_path: Path, note: str) -> None:
    block = _blocks(tmp_path, [VOICE, note])[1]
    assert block["type"] == "technical" and TIMECODE_NOTE_WARNING in block["warnings"]


def test_splice_after_named_sync_inherits_speaker_without_warning(tmp_path: Path) -> None:
    result = _run(tmp_path, [SYNC, "+ 00.43.25 Дальше говорит он."])
    block = result["blocks"][1]
    assert block["speaker"] == "Иванов Иван"
    assert not {"sync_no_speaker"} & (_codes(result, "error") | _codes(result, "warning"))


def test_splice_after_sync_without_speaker_stays_error(tmp_path: Path) -> None:
    result = _run(tmp_path, [VOICE, "СТЕНДАП 00:10:00 Мы в студии.", "+ 00.43.25 Дальше."])
    assert result["blocks"][2]["speaker"] == ""
    assert "sync_no_speaker" in _codes(result, "error")


@pytest.mark.parametrize("sep", [".", ","])
def test_sync_credit_with_inline_timecode_does_not_eat_timecode(tmp_path: Path, sep: str) -> None:
    block = _blocks(tmp_path, [VOICE, f"СНХ{sep} Иванов 00:10:00 Привет"])[1]
    assert block["type"] == "sync"
    assert block["speaker"] == "Иванов"
    assert "00:10:00" in str(block["timecode_start"])
    assert "Привет" in block["normalized_text"]


@pytest.mark.parametrize("junk", [".", "…", "—"])
def test_punctuation_only_paragraph_is_technical_and_not_voiced(tmp_path: Path, junk: str) -> None:
    blocks = _blocks(tmp_path, ["Голос «Ведущий»: Текст про дом.", junk, VOICE])
    assert blocks[1]["type"] == "technical" and blocks[1]["text_for_tts"] == ""
    assert any("punctuation-only" in w for w in blocks[1]["warnings"])


def _llm_first_voiced(tmp_path: Path, paragraphs: list[str]) -> list[dict[str, Any]]:
    """The model voices the first paragraph and refuses to classify the rest."""

    def classifier(batch: list[dict[str, Any]], hint: str) -> list[dict[str, Any]]:
        return [
            {
                "uid": item["uid"],
                "type": "voiceover" if index == 0 else "unknown",
                "confidence": 0.95 if index == 0 else 0.2,
            }
            for index, item in enumerate(batch)
        ]

    source = tmp_path / "scenario.txt"
    source.write_text("\n\n".join(paragraphs) + "\n", encoding="utf-8")
    return generate_normalization(
        source,
        tmp_path / "out.json",
        memory_path=tmp_path / "memory.sqlite3",
        use_llm=True,
        classifier=classifier,
    )["blocks"]


NARRATION = "Дом стоял на краю деревни, и никто не помнил его хозяев."


@pytest.mark.parametrize("credit", ["Иван Петров, режиссёр", "Анна Смирнова, автор сценария"])
def test_llm_unresolved_credit_after_voiceover_stays_unknown(tmp_path: Path, credit: str) -> None:
    blocks = _llm_first_voiced(tmp_path, [NARRATION, credit])
    assert blocks[0]["type"] == "voiceover"
    assert blocks[1]["type"] == "unknown"


def test_llm_unresolved_ordinary_continuation_after_voiceover_is_recovered(
    tmp_path: Path,
) -> None:
    blocks = _llm_first_voiced(tmp_path, [NARRATION, "Потом наступила зима, и всё замерло."])
    assert blocks[1]["type"] == "voiceover"


def test_caps_editorial_cue_with_verb_warns(tmp_path: Path) -> None:
    block = _blocks(tmp_path, [VOICE, "ФРАГМЕНТ ФИЛЬМА ПОКАЗЫВАЕТ ГЕРОЯ В ДЕТСТВЕ."])[1]
    assert block["type"] == "technical"
    assert "editorial cue by caps: check manually" in block["warnings"]


def test_caps_footage_cue_keeps_section_heading_warning(tmp_path: Path) -> None:
    block = _blocks(tmp_path, [VOICE, "ФР СЕРИАЛА, ГДЕ ГЕРОЙ – ЕСТЬ"])[1]
    assert block["type"] == "technical"
    assert any("section heading" in w for w in block["warnings"])
