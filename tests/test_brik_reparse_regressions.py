"""General regressions from a real-scenario reparse (depersonalised). No network, no LLM."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from stage1.normalization_models import NormalizedBlock
from stage1.normalizer import (
    NARRATION_RECOVERED_WARNING,
    _apply_llm_results,
    _llm_field_value,
    _looks_like_narration_sentence,
    generate_normalization,
)

LIFE = "ЛАЙФ СЦЕНА (00:34:00) Идут по тропинке."
VOICE = "ЗК1: Дом стоял на краю деревни."
SENTENCE = "Они остановились у яблони и долго молчали."


def _run(tmp_path: Path, paragraphs: list[str], **kwargs: Any) -> list[dict[str, Any]]:
    source = tmp_path / "scenario.txt"
    source.write_text("\n\n".join(paragraphs) + "\n", encoding="utf-8")
    result = generate_normalization(
        source,
        tmp_path / "out.json",
        memory_path=tmp_path / "memory.sqlite3",
        use_llm=kwargs.pop("use_llm", False),
        **kwargs,
    )
    return result["blocks"]


def _llm(tmp_path: Path, paragraphs: list[str], **answer: Any) -> list[dict[str, Any]]:
    def classifier(batch: list[dict[str, Any]], hint: str) -> list[dict[str, Any]]:
        return [{"uid": item["uid"], **answer} for item in batch]

    return _run(tmp_path, paragraphs, use_llm=True, classifier=classifier)


def _warned(block: dict[str, Any], needle: str) -> bool:
    return any(needle in warning for warning in block["warnings"])


def test_zk_ellipsis_text_is_voiceover(tmp_path: Path) -> None:
    blocks = _run(tmp_path, ["ЗК.… Это было давно, но помнится.", "ЗК2: Дальше."])
    assert blocks[0]["type"] == "voiceover"
    assert blocks[0]["text_for_tts"] == "… Это было давно, но помнится."


@pytest.mark.parametrize("llm_type", ["voiceover", "sync", "host", "life"])
def test_editorial_footage_cue_stays_technical_even_if_llm_disagrees(llm_type: str) -> None:
    block = NormalizedBlock(
        uid="b1",
        order=0,
        original_text="ФР СЕРИАЛА, ГДЕ ГЕРОЙ ВХОДИТ В ДОМ – ЕСТЬ",
        normalized_text="ФР СЕРИАЛА, ГДЕ ГЕРОЙ ВХОДИТ В ДОМ – ЕСТЬ",
    )
    _apply_llm_results([block], [{"uid": "b1", "type": llm_type, "confidence": 0.95}])
    assert block.type == "technical" and block.text_for_tts == ""
    assert any("editorial cue" in warning for warning in block.warnings)


def test_footage_cue_is_technical_through_pipeline(tmp_path: Path) -> None:
    cue = "ФР СЕРИАЛА, ГДЕ ГЕРОЙ ВХОДИТ В ДОМ – ЕСТЬ"
    blocks = _llm(tmp_path, [cue, SENTENCE, "ЗК2: Дальше."], type="voiceover", confidence=0.95)
    assert blocks[0]["type"] == "technical" and blocks[0]["text_for_tts"] == ""


@pytest.mark.parametrize("placeholder", ["unknown", "null", "None", "N/A", "неизвестно", " "])
def test_llm_placeholder_speaker_is_empty(placeholder: str) -> None:
    assert _llm_field_value(placeholder) == ""
    assert _llm_field_value(None) == ""
    assert _llm_field_value(" Иван ") == "Иван"


def test_llm_unknown_speaker_does_not_reach_block(tmp_path: Path) -> None:
    blocks = _llm(
        tmp_path,
        [SENTENCE, "ЗК5: Дальше."],
        type="voiceover",
        confidence=0.9,
        speaker="unknown",
        role="null",
    )
    assert blocks[0]["speaker"] == "" and blocks[0]["role"] == ""


def test_llm_unknown_on_plain_sentence_becomes_voiceover_with_warning(tmp_path: Path) -> None:
    blocks = _llm(tmp_path, [SENTENCE, "ЗК5: Дальше."], type="unknown", confidence=0.4)
    block = blocks[0]
    assert block["type"] == "voiceover"
    assert block["text_for_tts"]
    assert _warned(block, NARRATION_RECOVERED_WARNING)
    assert not _warned(block, "LLM confidence")


def test_letter_continuation_after_voiceover_is_voiceover_with_warning(tmp_path: Path) -> None:
    paragraphs = [
        "Голос Лили читает письмо:",
        "Дорогая мама! Пишу тебе из города.",
        "Здесь холодно, но мы держимся.",
        "Целую тебя, твоя Лиля.",
    ]
    blocks = _llm(tmp_path, paragraphs, type="unknown", confidence=0.3)
    assert blocks[0]["type"] == "technical"
    for block in blocks[1:]:
        assert block["type"] == "voiceover", block
        assert _warned(block, NARRATION_RECOVERED_WARNING)


def test_verse_lines_after_voiceover_continue_it(tmp_path: Path) -> None:
    paragraphs = ["Голос Лили:", "Тихо в роще", "Дождь идёт", "Ночь"]
    blocks = _llm(tmp_path, paragraphs, type="unknown", confidence=0.3)
    assert [b["type"] for b in blocks] == ["technical"] + ["voiceover"] * 3
    assert all(_warned(b, NARRATION_RECOVERED_WARNING) for b in blocks[1:])


def test_line_after_life_scene_is_not_recovered(tmp_path: Path) -> None:
    blocks = _llm(tmp_path, [LIFE, SENTENCE, "Тихо в роще"], type="unknown", confidence=0.3)
    assert blocks[1]["type"] == "unknown" and blocks[1]["text_for_tts"] == ""
    assert blocks[2]["type"] == "unknown"
    assert not _warned(blocks[1], NARRATION_RECOVERED_WARNING)


def test_llm_voiceover_after_life_keeps_contradiction_warning(tmp_path: Path) -> None:
    blocks = _llm(tmp_path, [LIFE, SENTENCE, "ЗК5: Дальше."], type="voiceover", confidence=0.95)
    assert blocks[1]["type"] == "unknown"
    assert _warned(blocks[1], "contradicts the preceding LIFE scene")
    assert not _warned(blocks[1], NARRATION_RECOVERED_WARNING)


@pytest.mark.parametrize("llm_type", ["sync", "life"])
def test_llm_sync_or_life_suggestion_is_not_overridden(tmp_path: Path, llm_type: str) -> None:
    blocks = _llm(tmp_path, [SENTENCE, "ЗК5: Дальше."], type=llm_type, confidence=0.95)
    assert blocks[0]["type"] != "voiceover"
    assert not _warned(blocks[0], NARRATION_RECOVERED_WARNING)


@pytest.mark.parametrize(
    "text",
    [
        "кадры из фильма о том, как они жили",
        "здесь фото дома, где они жили",
        "Фото дома, где они жили когда-то",
        "Смотреть можно здесь //disk.yandex.ru/d/abc123 и там",
        "Голос Лили читает письмо матери:",
        "ОНИ ПРИШЛИ И УШЛИ ОБРАТНО ДОМОЙ",
        "Да.",
    ],
)
def test_not_narration_sentence(text: str) -> None:
    assert not _looks_like_narration_sentence(text)


def test_narration_sentence_positive() -> None:
    assert _looks_like_narration_sentence(SENTENCE)


@pytest.mark.parametrize(
    "text",
    [
        "кадры из фильма о том, как они жили",
        "здесь фото дома, где они жили",
        "Смотреть можно здесь //disk.yandex.ru/d/abc123 и там",
    ],
)
def test_remark_after_reading_cue_is_never_recovered_as_narration(
    tmp_path: Path, text: str
) -> None:
    blocks = _llm(tmp_path, ["Голос Лили:", text, "ЗК2: Дальше."], type="unknown", confidence=0.3)
    assert not _warned(blocks[1], NARRATION_RECOVERED_WARNING)


def test_name_with_source_in_brackets_and_timecode_is_sync(tmp_path: Path) -> None:
    blocks = _run(
        tmp_path, ["Анна Лесная (фильм): 38:38 Он любил играть в карты и долго рассказывал."]
    )
    block = blocks[0]
    assert block["type"] == "sync"
    assert block["speaker"] == "Анна Лесная"
    assert block["source_description"] == "фильм"
    assert block["timecode_start"] == "38:38"


@pytest.mark.parametrize("separator", [".", ","])
def test_sync_marker_with_name_and_colon_gets_speaker(tmp_path: Path, separator: str) -> None:
    blocks = _run(tmp_path, [f"СНХ{separator} Отец: очень похож и талантлив, как мама."])
    assert blocks[0]["type"] == "sync"
    assert blocks[0]["speaker"] == "Отец"


def test_sync_marker_dot_followed_by_a_sentence_is_not_a_speaker(tmp_path: Path) -> None:
    blocks = _run(
        tmp_path, ["СНХ. Он пришёл домой поздно вечером, усталый и голодный: никого не было."]
    )
    assert blocks[0]["speaker"] == ""


def test_named_voice_reads_text_is_voiceover_without_label_in_tts(tmp_path: Path) -> None:
    blocks = _run(tmp_path, ["Голос «Поэта»: Я знаю, город будет, я знаю, саду цвесть."])
    block = blocks[0]
    assert block["type"] == "voiceover"
    assert block["text_for_tts"] == "Я знаю, город будет, я знаю, саду цвесть."


def test_named_voice_cue_without_text_stays_technical(tmp_path: Path) -> None:
    assert _run(tmp_path, ["Голос «Поэта»:"])[0]["type"] == "technical"


def test_name_with_source_in_brackets_without_colon_is_sync(tmp_path: Path) -> None:
    blocks = _run(tmp_path, ["Анна Лесная (Фильм) 38:38 Он любил играть в карты."])
    block = blocks[0]
    assert block["type"] == "sync"
    assert block["speaker"] == "Анна Лесная"
    assert block["source_description"] == "Фильм"


def test_name_with_role_in_brackets_is_not_a_source(tmp_path: Path) -> None:
    blocks = _run(tmp_path, ["Анна Лесная (эксперт) 38:38 Он любил играть в карты."])
    assert blocks[0]["source_description"] == ""
    assert blocks[0]["type"] != "sync" or blocks[0]["speaker"] != "Анна Лесная (эксперт)"
