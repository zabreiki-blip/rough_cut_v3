"""Scene / dialogue / continuation context (plan task 4). No network, no LLM."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from stage1.io_utils import extract_paragraphs
from stage1.normalization_models import NormalizedBlock
from stage1.normalizer import generate_normalization
from stage1.scenario_context import (
    SPLICE_CONTINUES_WARNING,
    is_dialogue_reply,
    is_plus_splice,
    is_section_heading,
    neighbour_context,
    plan_scene_context,
)

LIFE = "ЛАЙФ СЦЕНА (00:34:00) Идут по тропинке."


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


def _fake(type_: str, text: str, label: str = "", cont: str = "") -> Any:
    return SimpleNamespace(
        type=type_, original_text=text, original_label=label, continuation_of_type=cont
    )


# --- pure predicates ---------------------------------------------------------


def test_predicates() -> None:
    assert is_dialogue_reply("— Да.") and is_dialogue_reply("- Вот эту?")
    assert not is_dialogue_reply("-") and not is_dialogue_reply("Да.")
    assert is_plus_splice("+ 00.43.25 Это чудо") and is_plus_splice("+ (00:43:25) Это")
    assert not is_plus_splice("+ плюс один") and not is_plus_splice("00.43.25 Это")
    assert is_section_heading("ПОСЛУШАНИЕ В САДУ") and is_section_heading("ЛИНИЯ НИКИТЫ")
    assert not is_section_heading("Послушание в саду")
    assert not is_section_heading("ЭТО ВСЁ БЫЛО ДАВНО.")
    assert not is_section_heading("ЛАЙФ 00:34:00")
    assert not is_section_heading("ВОЗВРАЩАЕМСЯ В 1941 ГОД")
    assert not is_section_heading("ЛЮДИ ПРИЕЗЖАЮТ СЮДА ИЗДАЛЕКА")
    assert is_section_heading("ВНИМАНИЕ ОПАСНОСТЬ ОБВАЛА КРЫШИ")
    assert not is_section_heading("А" * 80)


def test_plan_replies_belong_to_open_scene_and_label_closes_it() -> None:
    blocks = [
        _fake("life", LIFE, "ЛАЙФ"),
        _fake("unknown", "— Вот эту?"),
        _fake("voiceover", "ЗК5 Текст", "ЗК"),
        _fake("unknown", "— Да."),
    ]
    plan = plan_scene_context(blocks)
    assert plan[0] is None and plan[2] is None and plan[3] is None
    assert plan[1] is not None and plan[1].scene_index == 0 and plan[1].scene_type == "life"


def test_plan_orphan_splice_stays_unknown_with_warning() -> None:
    plan = plan_scene_context(
        [_fake("voiceover", "ЗК Текст", "ЗК"), _fake("unknown", "+ 00.43.25 Да")]
    )
    assert plan[1] is not None and plan[1].scene_type == "" and "check manually" in plan[1].warning


def test_neighbour_context_is_bounded() -> None:
    blocks = [_fake("life", "а" * 500) for _ in range(6)]
    context = neighbour_context(blocks, 3)
    assert len(context["before"]) == 2 and len(context["after"]) == 2
    assert all(len(item["text"]) <= 120 for item in context["before"] + context["after"])
    assert neighbour_context(blocks[:1], 0) == {}


# --- normalizer integration --------------------------------------------------


def test_heading_is_technical_not_tts_and_closes_scene(tmp_path: Path) -> None:
    blocks = _run(
        tmp_path,
        [
            "ЗК1 Первый абзац диктора.",
            "ПОСЛУШАНИЕ В САДУ",  # complete block before, LIFE after: strong
            LIFE,
            "ЛИНИЯ НИКИТЫ",  # repeated paragraph: strong
            "— Ответ после заголовка",
            "ЛИНИЯ НИКИТЫ",
        ],
    )
    heading = blocks[1]
    assert heading["type"] == "technical" and heading["text_for_tts"] == ""
    assert heading["warnings"] and heading["continuation_of_type"] == ""
    assert blocks[3]["type"] == "technical" and blocks[3]["text_for_tts"] == ""
    assert blocks[5]["type"] == "technical"
    assert blocks[4]["type"] == "unknown"  # heading closed the LIFE scene


@pytest.mark.parametrize(
    "slogan", ["СЛАВА РОССИИ", "ХРИСТОС ВОСКРЕСЕ", "ГОСПОДИ ПОМИЛУЙ", "НИКОГДА БОЛЬШЕ"]
)
def test_slogan_between_voiceover_blocks_is_not_silently_technical(
    tmp_path: Path, slogan: str
) -> None:
    blocks = _run(
        tmp_path,
        ["ЗК1 Первый абзац диктора.", slogan, "ЗК2 Второй абзац диктора."],
    )
    block = blocks[1]
    assert block["type"] == "unknown"
    assert block["suggested_type"] == "technical"
    assert block["text_for_tts"] == "" and block["continuation_of_type"] == ""
    assert any("heading or spoken text" in w for w in block["warnings"])
    assert blocks[0]["type"] == blocks[2]["type"] == "voiceover"


def test_ambiguous_caps_line_is_not_sent_to_llm_or_given_voiceover(tmp_path: Path) -> None:
    seen: list[str] = []

    def classifier(items: list[dict[str, Any]], hint: str) -> list[dict[str, Any]]:
        seen.extend(str(item.get("original_text", "")) for item in items)
        return [
            {"uid": item["uid"], "type": "voiceover", "confidence": 0.95, "text_for_tts": "X"}
            for item in items
        ]

    blocks = _run(
        tmp_path,
        ["ЗК1 Первый абзац диктора.", "СЛАВА РОССИИ", "ЗК2 Второй абзац диктора."],
        use_llm=True,
        classifier=classifier,
    )
    assert blocks[1]["type"] == "unknown" and blocks[1]["text_for_tts"] == ""
    assert not any("СЛАВА" in text for text in seen)


def test_caps_line_before_marked_block_after_complete_sentence_is_technical(
    tmp_path: Path,
) -> None:
    blocks = _run(tmp_path, ["ЗК1 Конец мысли.", "ПОСЛУШАНИЕ В САДУ", LIFE])
    assert blocks[1]["type"] == "technical"
    # ...but not in the middle of a thought.
    blocks = _run(tmp_path, ["ЗК1 Мысль продолжается без точки", "ПОСЛУШАНИЕ В САДУ", LIFE])
    assert blocks[1]["type"] == "unknown"


def test_heading_chain_before_repeated_heading_is_technical(tmp_path: Path) -> None:
    blocks = _run(
        tmp_path,
        [
            "СТЕНДАП ДАРЬЯ Текст стендапа закончен.",
            "ПЕРЕХОД НА НАШЕ ВРЕМЯ ЧЕРЕЗ ГРАФИКУ",
            "ЛИНИЯ НИКИТЫ",
            "ЗК1 Текст.",
            "ЛИНИЯ НИКИТЫ",
        ],
    )
    assert [b["type"] for b in blocks[1:3]] == ["technical", "technical"]


def test_dash_figure_is_not_a_dialogue_reply(tmp_path: Path) -> None:
    assert not is_dialogue_reply("–15 градусов мороза, говорит синоптик.")
    assert not is_dialogue_reply("— 15 градусов")
    assert is_dialogue_reply("– Вот эту?") and is_dialogue_reply("-Да.")
    blocks = _run(tmp_path, [LIFE, "– Вот эту?", "–15 градусов мороза стояло той зимой."])
    assert blocks[1]["type"] == "life"
    assert blocks[2]["type"] != "life" and blocks[2]["scene_uid"] == ""


def test_timecoded_dash_reply_keeps_following_life_context(tmp_path: Path) -> None:
    timed = "- (00:42:43) Персики. Это у нас поздний сорт."
    splice = "+ 00.43.25 Тоже поздний."
    short = "- Да."
    blocks = _run(tmp_path, [LIFE, timed, splice, short, "ЗК1 Новый дикторский текст."])

    opener, timed_reply, splice_reply, short_reply, voiceover = blocks
    assert [b["type"] for b in blocks] == ["life", "life", "life", "life", "voiceover"]
    assert all(not b["speaker"] for b in (timed_reply, splice_reply, short_reply))
    assert all(b["scene_uid"] == opener["uid"] for b in (timed_reply, splice_reply, short_reply))
    assert timed_reply["timecode_start"] == "00:42:43"
    assert splice_reply["timecode_start"] == "00:43:25"
    assert timed_reply["sync_fragments"] == splice_reply["sync_fragments"] == []
    assert voiceover["scene_uid"] == "" and voiceover["continuation_of_type"] == ""


def test_timecoded_reply_requires_speech_and_structural_timecode() -> None:
    assert is_dialogue_reply("- (00:42:43) — Персики")
    assert is_dialogue_reply("– 00:40:58 Абсолютно")
    assert not is_dialogue_reply("–15 градусов мороза")
    assert not is_dialogue_reply("- 12.03.25 Это дата")
    assert not is_dialogue_reply("- (00:42:43)")


def test_long_dash_paragraph_joined_to_scene_carries_warning(tmp_path: Path) -> None:
    long_reply = "— " + "Это очень длинный абзац без разрыва. " * 12
    blocks = _run(tmp_path, [LIFE, long_reply])
    assert any("long dash paragraph" in w for w in blocks[1]["warnings"])


def test_labelled_caps_speech_is_still_voiceover(tmp_path: Path) -> None:
    blocks = _run(tmp_path, ["ЗК ПОСЛУШАНИЕ В САДУ", "ЗК2 ВЕЧНАЯ ПАМЯТЬ ГЕРОЯМ"])
    assert [b["type"] for b in blocks] == ["voiceover", "voiceover"]
    assert blocks[0]["text_for_tts"] == "ПОСЛУШАНИЕ В САДУ"
    assert blocks[1]["text_for_tts"] == "ВЕЧНАЯ ПАМЯТЬ ГЕРОЯМ"


def test_plus_splice_continues_life_scene_without_merging(tmp_path: Path) -> None:
    blocks = _run(tmp_path, [LIFE, "— Вот эту?", "+ 00.35.18 Вот здесь я буду срезать?"])
    life, reply, splice = blocks
    assert splice["type"] == "life" and splice["speaker"] == ""
    assert splice["continuation_of_type"] == "life"
    assert splice["timecode_start"] == "00:35:18"
    assert splice["timecode_start"] != life["timecode_start"]
    assert SPLICE_CONTINUES_WARNING in splice["warnings"]
    assert len({b["uid"] for b in blocks}) == 3
    assert splice["scene_uid"] == reply["scene_uid"] == life["uid"] and life["scene_uid"] == ""


def test_plus_splice_after_voiceover_is_unknown_with_warning(tmp_path: Path) -> None:
    blocks = _run(tmp_path, ["ЗК1 Текст диктора.", "+ 00.43.25 Это вообще чудо какое-то."])
    assert blocks[1]["type"] == "unknown" and blocks[1]["speaker"] == ""
    assert any("check manually" in w for w in blocks[1]["warnings"])


def test_sync_scene_reply_does_not_invent_speaker(tmp_path: Path) -> None:
    blocks = _run(tmp_path, ["СНХ Иван Петров (00:01:00) Я так считаю.", "— Да.", "— Нет."])
    assert blocks[0]["speaker"] == "Иван Петров"
    for reply in blocks[1:]:
        assert reply["type"] == "unknown" and reply["speaker"] == ""
        assert reply["suggested_type"] == "sync"
        assert reply["scene_uid"] == blocks[0]["uid"]
        assert any("interviewer question or hero" in w for w in reply["warnings"])


def test_blank_paragraphs_do_not_reach_parser(tmp_path: Path) -> None:
    source = tmp_path / "s.txt"
    source.write_text(f"{LIFE}\n\n\n   \n— Вот эту?\n\n", encoding="utf-8")
    assert extract_paragraphs(source) == [LIFE, "— Вот эту?"]
    from docx import Document

    document = Document()
    for text in (LIFE, "", "   ", "— Вот эту?"):
        document.add_paragraph(text)
    docx = tmp_path / "s.docx"
    document.save(str(docx))
    assert extract_paragraphs(docx) == [LIFE, "— Вот эту?"]


def test_scene_uid_is_optional_in_stored_json() -> None:
    old = {"uid": "b1", "order": 1, "original_text": "x", "normalized_text": "x"}
    assert NormalizedBlock.from_dict(old).scene_uid == ""
    block = NormalizedBlock.from_dict({**old, "scene_uid": "b0"})
    assert NormalizedBlock.from_dict(block.to_dict()).scene_uid == "b0"


def test_reparse_keeps_scene_uid_consistent_with_stable_uids(tmp_path: Path) -> None:
    paragraphs = [LIFE, "— Вот эту?"]
    first = _run(tmp_path, paragraphs)
    second = _run(tmp_path, paragraphs)
    assert [b["uid"] for b in second] == [b["uid"] for b in first]
    assert second[1]["scene_uid"] == second[0]["uid"]


# --- LLM path (fake classifier, no network) ------------------------------------


def test_llm_gets_bounded_context_only_for_unresolved_and_cannot_rewrite(tmp_path: Path) -> None:
    seen: list[list[dict[str, Any]]] = []

    def classifier(batch: list[dict[str, Any]], hint: str) -> list[dict[str, Any]]:
        seen.append(batch)
        return [
            {
                "uid": item["uid"],
                "type": "voiceover",
                "confidence": 0.95,
                "text_for_tts": "ПЕРЕПИСАНО",
            }
            for item in batch
        ]

    blocks = _run(
        tmp_path,
        [
            "ЗК1 " + "Длинный текст. " * 40,
            "БЫСТРАЯ НАРЕЗКА красивых кадров (пчелы, цветы)",
            "Они остановились у яблони и долго молчали.",
            "ЗК5: Дальше текст диктора.",
        ],
        use_llm=True,
        classifier=classifier,
    )
    assert len(seen) == 1 and len(seen[0]) == 1  # only the unresolved block is sent
    context = seen[0][0]["context"]
    assert context["before"][0]["label"] == "ЗК" and len(context["before"][0]["text"]) <= 120
    assert context["before"][1]["type"] == "technical"
    assert context["after"][0]["type"] == "voiceover"
    assert blocks[2]["text_for_tts"] == "Они остановились у яблони и долго молчали."
    assert blocks[2]["normalized_text"] == "Они остановились у яблони и долго молчали."
    assert any("differs from the source" in w for w in blocks[2]["warnings"])


def test_llm_voiceover_after_life_scene_is_warning_not_silent_apply(tmp_path: Path) -> None:
    def classifier(batch: list[dict[str, Any]], hint: str) -> list[dict[str, Any]]:
        return [{"uid": item["uid"], "type": "voiceover", "confidence": 0.95} for item in batch]

    blocks = _run(
        tmp_path,
        [LIFE, "Они остановились у яблони и долго молчали.", "ЗК5: Дальше."],
        use_llm=True,
        classifier=classifier,
    )
    assert blocks[1]["type"] == "unknown" and blocks[1]["text_for_tts"] == ""
    assert blocks[1]["suggested_type"] == "voiceover"
    assert any("contradicts the preceding LIFE scene" in w for w in blocks[1]["warnings"])


# --- LLM text_for_tts: numbers spelled out are allowed ------------------------


@pytest.mark.parametrize(
    ("source", "proposed"),
    [
        ("В 1915 году.", "В тысяча девятьсот пятнадцатом году."),
        (
            "Он родился в 2024 г. в маленьком городе.",
            "Он родился в две тысячи двадцать четвёртом году в маленьком городе.",
        ),
        (
            "Приказ подписали 12.05.1920 в Москве.",
            "Приказ подписали двенадцатого мая тысяча девятьсот двадцатого года в Москве.",
        ),
        (
            "Война шла в 1915–1917 гг. на всех фронтах.",
            "Война шла в тысяча девятьсот пятнадцатом — тысяча девятьсот семнадцатом годах "
            "на всех фронтах.",
        ),
        ("Было 45 человек.", "Было сорок пять человек."),
    ],
)
def test_llm_tts_with_numbers_spelled_out_is_close_to_source(source: str, proposed: str) -> None:
    from stage1.normalizer import _llm_text_close_to_source

    assert _llm_text_close_to_source(proposed, source)


@pytest.mark.parametrize(
    ("source", "proposed"),
    [
        ("В 1915 году монастырь закрыли.", "Сегодня погода хорошая, гуляем."),
        ("В 1915 году монастырь закрыли.", "Монастырь любили все."),
        ("Они остановились у яблони и долго молчали.", "ПЕРЕПИСАНО"),
        ("В 1915 году монастырь закрыли, а братию выслали.", "В тысяча девятьсот пятнадцатом."),
    ],
)
def test_llm_tts_bad_rewrite_is_still_rejected(source: str, proposed: str) -> None:
    from stage1.normalizer import _llm_text_close_to_source

    assert not _llm_text_close_to_source(proposed, source)


# --- save_normalization: changed scene opener ---------------------------------


def _generate_scene(tmp_path: Path) -> tuple[Path, dict[str, Any]]:
    source = tmp_path / "scenario.txt"
    source.write_text(f"{LIFE}\n\n— Вот эту?\n\n— Да.\n", encoding="utf-8")
    out = tmp_path / "out.json"
    payload = generate_normalization(
        source, out, memory_path=tmp_path / "memory.sqlite3", use_llm=False
    )
    return out, payload


def test_changed_scene_opener_warns_replies_without_retyping(tmp_path: Path) -> None:
    from stage1.normalizer import save_normalization

    out, payload = _generate_scene(tmp_path)
    assert payload["blocks"][1]["scene_uid"] == payload["blocks"][0]["uid"]
    payload["blocks"][0]["type"] = "voiceover"
    saved = save_normalization(out, payload)
    for reply in saved["blocks"][1:]:
        assert "scene opener changed: check reply type" in reply["warnings"]
        assert reply["type"] == "life"
    assert "scene opener changed: check reply type" not in saved["blocks"][0]["warnings"]
    # Restoring the opener removes the warning again.
    saved["blocks"][0]["type"] = "life"
    again = save_normalization(out, saved)
    assert all(
        "scene opener changed: check reply type" not in b["warnings"] for b in again["blocks"]
    )


def test_missing_scene_opener_warns_replies(tmp_path: Path) -> None:
    from stage1.normalizer import save_normalization

    out, payload = _generate_scene(tmp_path)
    payload["blocks"][1]["scene_uid"] = "no-such-block"
    saved = save_normalization(out, payload)
    assert "scene opener changed: check reply type" in saved["blocks"][1]["warnings"]
    assert saved["blocks"][1]["type"] == "life"


def test_dash_then_timecode_is_still_a_reply() -> None:
    assert is_dialogue_reply("- (00:42:43) Персики. Это у нас поздний сорт.")
    assert is_dialogue_reply("– 00:40:58 Абсолютно.")
    assert not is_dialogue_reply("–15 градусов мороза")
