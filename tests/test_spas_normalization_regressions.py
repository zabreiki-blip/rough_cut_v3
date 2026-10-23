"""Depersonalised regressions for the scenario-parsing defects found on SPAS_DM_1s_001.

Cases live in ``fixtures/scenario_parsing_cases.json`` (no real names, no full
DOCX).  Parse tests go through the public ``generate_normalization(...)`` with
``use_llm=False`` and a temporary empty memory.  Confirmation-gate tests check
the reason code and the block UID of every saved-state defect.
"""

from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path
from typing import Any

import pytest

from stage1.normalizer import (
    generate_normalization,
    validate_for_confirmation,
    validate_for_confirmation_detailed,
)

_FIXTURE = Path(__file__).parent / "fixtures" / "scenario_parsing_cases.json"
_CASES = json.loads(_FIXTURE.read_text(encoding="utf-8"))
_PARSE = {case["id"]: case for case in _CASES["parse_cases"]}
_SAVED = _CASES["saved_state_cases"]
_NUMBERING_WARNING = re.compile(r"duplicate|repeat|numbering|нумер|повтор", re.IGNORECASE)


def _parse(case_id: str) -> dict[str, Any]:
    """Run the normalizer without LLM on the case paragraphs."""
    paragraphs = _PARSE[case_id]["paragraphs"]
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        source = root / "scenario.txt"
        source.write_text("\n\n".join(paragraphs) + "\n", encoding="utf-8")
        return generate_normalization(
            source,
            root / "out.json",
            memory_path=root / "memory.sqlite3",
            use_llm=False,
        )


def _tc(value: str) -> str:
    return value.replace(".", ":")


def _assert_expected(result: dict[str, Any], case_id: str) -> None:
    case = _PARSE[case_id]
    blocks = result["blocks"]
    assert len(blocks) == len(case["paragraphs"])
    for index, expected in enumerate(case["expected_blocks"]):
        for field, value in expected.items():
            assert blocks[index][field] == value, (
                f"{case_id}: block {index + 1} field {field!r}: "
                f"expected {value!r}, got {blocks[index][field]!r}"
            )


def _assert_fragments(result: dict[str, Any], case_id: str) -> None:
    for block, expected in zip(result["blocks"], _PARSE[case_id]["expected_fragments"]):
        actual = [
            {
                "text": item["text"],
                "timecode_start": _tc(item["timecode_start"]),
                "timecode_end": _tc(item.get("timecode_end", "")),
            }
            for item in block["sync_fragments"]
        ]
        assert actual == expected


# --- ЗК labels (task 2) -----------------------------------------------------


def test_zk_glued_number_without_colon_is_clean_voiceover() -> None:
    _assert_expected(_parse("zk_glued_number"), "zk_glued_number")


def test_zk_spaced_label_keeps_year_in_speech() -> None:
    # Control: passes today.
    _assert_expected(_parse("zk_year_in_speech"), "zk_year_in_speech")


def test_zk_spaced_number_with_colon_is_stripped_from_tts() -> None:
    _assert_expected(_parse("zk_spaced_number_colon"), "zk_spaced_number_colon")


def test_zk_glued_number_with_colon_control() -> None:
    # Control: passes today.
    _assert_expected(_parse("zk_colon_control"), "zk_colon_control")


def test_repeated_zk_number_gives_two_blocks_and_numbering_warning() -> None:
    result = _parse("zk_repeated_author_number")
    _assert_expected(result, "zk_repeated_author_number")
    first, second = result["blocks"]
    assert first["uid"] != second["uid"]
    warnings = list(result["warnings"]) + [w for b in result["blocks"] for w in b["warnings"]]
    assert any(_NUMBERING_WARNING.search(w) for w in warnings), (
        f"no numbering warning for repeated author number, warnings={warnings!r}"
    )


# --- technical cue vs. hero (task 2) ----------------------------------------


def test_montage_cue_is_technical_not_a_hero() -> None:
    _assert_expected(_parse("technical_cue_not_hero"), "technical_cue_not_hero")


def test_real_hero_without_timecode_stays_sync() -> None:
    # Control: passes today.
    _assert_expected(_parse("real_hero_without_timecode"), "real_hero_without_timecode")


def test_lifes_variant_is_life_not_speaker() -> None:
    _assert_expected(_parse("lifes_variant"), "lifes_variant")


# --- timecodes, credits, splices (task 3) -----------------------------------


def test_life_description_and_speech_survive_parenthesised_timecode() -> None:
    _assert_expected(_parse("life_timecode_in_parentheses"), "life_timecode_in_parentheses")


def test_snh_credit_keeps_name_role_time_and_speech_apart() -> None:
    result = _parse("snh_role_with_timecode")
    _assert_expected(result, "snh_role_with_timecode")
    _assert_fragments(result, "snh_role_with_timecode")


def test_life_splice_is_two_fragments_not_a_range() -> None:
    result = _parse("life_splice")
    _assert_expected(result, "life_splice")
    _assert_fragments(result, "life_splice")


# --- timecode forms, credits and splices: depersonalised variants (task 3) ---


def _normalize(*paragraphs: str) -> list[dict[str, Any]]:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        source = root / "scenario.txt"
        source.write_text("\n\n".join(paragraphs) + "\n", encoding="utf-8")
        result = generate_normalization(
            source, root / "out.json", memory_path=root / "memory.sqlite3", use_llm=False
        )
    return result["blocks"]


def _frags(block: dict[str, Any]) -> list[tuple[str, str, str]]:
    return [
        (item["text"], item["timecode_start"], item.get("timecode_end", ""))
        for item in block["sync_fragments"]
    ]


def test_snh_credit_with_dotted_timecode_keeps_source_and_normalizes_value() -> None:
    (block,) = _normalize("СНХ ИМЯ 01.24.08 Реплика")
    assert block["type"] == "sync" and block["speaker"] == "ИМЯ" and block["role"] == ""
    # Stored value is unambiguous; the author's spelling stays in original_text.
    assert block["timecode_start"] == "01:24:08"
    assert block["original_text"] == "СНХ ИМЯ 01.24.08 Реплика"
    assert block["normalized_text"] == "Реплика"
    assert _frags(block) == [("Реплика", "01:24:08", "")]


def test_snh_credit_with_mixed_separator_timecode() -> None:
    (block,) = _normalize("СНХ ИМЯ ФАМИЛИЯ, РОЛЬ 00.35:30 Реплика")
    assert (block["speaker"], block["role"]) == ("ИМЯ ФАМИЛИЯ", "РОЛЬ")
    assert _frags(block) == [("Реплика", "00:35:30", "")]


def test_unclosed_parenthesis_before_timecode_in_snh_credit() -> None:
    (block,) = _normalize("СНХ ИМЯ ФАМИЛИЯ, РОЛЬ (00:35:45 еще при патриархе")
    assert (block["speaker"], block["role"]) == ("ИМЯ ФАМИЛИЯ", "РОЛЬ")
    assert block["timecode_start"] == "00:35:45"
    assert block["normalized_text"] == "еще при патриархе"
    assert _frags(block) == [("еще при патриархе", "00:35:45", "")]


def test_unclosed_parenthesis_before_timecode_in_life() -> None:
    (block,) = _normalize("ЛАЙФ СЦЕНА (00:42:32 - А что это за деревце?")
    assert block["type"] == "life" and block["source_description"] == "СЦЕНА"
    assert block["timecode_start"] == "00:42:32"
    assert block["normalized_text"] == "А что это за деревце?"


def test_unclosed_parenthesis_whose_closing_mark_ends_the_paragraph() -> None:
    (block,) = _normalize("ЛАЙФ СЦЕНА (00:34:00 идут по тропинке, будут обрезать розы)")
    assert block["source_description"] == "СЦЕНА"
    assert block["timecode_start"] == "00:34:00"
    assert block["normalized_text"] == "идут по тропинке, будут обрезать розы"


def test_life_mixed_separator_timecode() -> None:
    (block,) = _normalize("ЛАЙФ СЦЕНА (00:30.00) Текст сцены")
    assert block["source_description"] == "СЦЕНА"
    assert block["timecode_start"] == "00:30:00" and block["normalized_text"] == "Текст сцены"


def test_bracketed_range_with_fractions_is_one_range_not_a_stray_number() -> None:
    (block,) = _normalize("СНХ ИМЯ ФАМИЛИЯ [29:41.93 - 30:42.20] Мы привыкли видеть собор")
    assert _frags(block) == [("Мы привыкли видеть собор", "29:41", "30:42")]
    assert "93" not in block["normalized_text"]
    assert (block["timecode_start"], block["timecode_end"]) == ("29:41", "30:42")


def test_source_file_name_stays_out_of_role_and_speaker() -> None:
    (block,) = _normalize("СНХ ИМЯ ФАМИЛИЯ, РОЛЬ ИСХ DM_01_FILE_NAME (00:20:55) реплика")
    assert (block["speaker"], block["role"]) == ("ИМЯ ФАМИЛИЯ", "РОЛЬ")
    assert block["source_description"] == "DM_01_FILE_NAME"
    assert _frags(block) == [("реплика", "00:20:55", "")]


def test_source_file_name_with_unclosed_name_bracket() -> None:
    (block,) = _normalize("СНХ ИГУМЕН ИМЯ (ФАМИЛИЯ ИСХ DM_01_FILE_NAME (00:26:56) встаю рано")
    assert block["speaker"] == "ИГУМЕН ИМЯ (ФАМИЛИЯ)" and block["role"] == ""
    assert block["source_description"] == "DM_01_FILE_NAME"
    assert _frags(block) == [("встаю рано", "00:26:56", "")]


def test_source_file_name_with_dash_and_dotted_timecode() -> None:
    (block,) = _normalize("СНХ ИМЯ ФАМИЛИЯ ИСХ DM_01_FILE_NAME - (02:03:18) всю ночь")
    assert block["speaker"] == "ИМЯ ФАМИЛИЯ" and block["source_description"] == "DM_01_FILE_NAME"
    assert _frags(block) == [("всю ночь", "02:03:18", "")]


def test_snh_splice_with_plus_gives_two_fragments_without_plus() -> None:
    (block,) = _normalize("СНХ ИМЯ 01.45.10 Первая часть. + (01:46:54) Вторая часть.")
    assert _frags(block) == [
        ("Первая часть.", "01:45:10", ""),
        ("Вторая часть.", "01:46:54", ""),
    ]


def test_reverse_time_order_is_kept_as_written() -> None:
    (block,) = _normalize("ЛАЙФ СЦЕНА (00:52:40) Первый текст + 00.05.20 Второй текст")
    assert _frags(block) == [
        ("Первый текст", "00:52:40", ""),
        ("Второй текст", "00:05:20", ""),
    ]


def test_life_range_stays_one_range() -> None:
    (block,) = _normalize("ЛАЙФ СЦЕНА (00:05:20–00:05:40) Первый текст")
    assert (block["timecode_start"], block["timecode_end"]) == ("00:05:20", "00:05:40")
    assert block["sync_fragments"] == [] and block["normalized_text"] == "Первый текст"
    (plain,) = _normalize("ЛАЙФ СЦЕНА 00:05:20 - 00:05:40 Первый текст")
    assert (plain["timecode_start"], plain["timecode_end"]) == ("00:05:20", "00:05:40")
    assert plain["sync_fragments"] == []


def test_dates_and_clock_times_in_speech_are_neither_timecodes_nor_splices() -> None:
    (life,) = _normalize(
        "ЛАЙФ СЦЕНА (00:05:20) Основан в 1915.10 году, а 12.05.1915 открыт + 23.05 это дата."
    )
    assert life["timecode_start"] == "00:05:20" and life["timecode_end"] == ""
    assert life["sync_fragments"] == []
    assert "12.05.1915" in life["normalized_text"] and "1915.10" in life["normalized_text"]
    (sync,) = _normalize("СНХ ИМЯ 00:05:20 Служба в 6:30. Вторая в 8:30, потом 23.05 праздник.")
    assert _frags(sync) == [("Служба в 6:30. Вторая в 8:30, потом 23.05 праздник.", "00:05:20", "")]


def test_empty_first_splice_part_hands_start_to_the_text_and_warns() -> None:
    (block,) = _normalize("ЛАЙФ СЦЕНА (00:01:00) + 00:02:00 Второй")
    assert block["normalized_text"] == "Второй"
    assert block["timecode_start"] == "00:02:00" and block["sync_fragments"] == []
    assert any("00:01:00" in w and "no text" in w for w in block["warnings"])


def test_empty_middle_and_trailing_splice_parts_warn_with_their_timecode() -> None:
    (middle,) = _normalize("ЛАЙФ СЦЕНА (00:01:00) Первый + 00:02:00 + 00:03:00 Третий")
    assert _frags(middle) == [("Первый", "00:01:00", ""), ("Третий", "00:03:00", "")]
    assert any("00:02:00" in w and "no text" in w for w in middle["warnings"])
    (tail,) = _normalize("ЛАЙФ СЦЕНА (00:01:00) Первый + 00:02:00 Второй + 00:03:00")
    assert _frags(tail) == [("Первый", "00:01:00", ""), ("Второй", "00:02:00", "")]
    assert any("00:03:00" in w and "no text" in w for w in tail["warnings"])
    (clean,) = _normalize("ЛАЙФ СЦЕНА (00:01:00) Первый + 00:02:00 Второй")
    assert not [w for w in clean["warnings"] if "no text" in w]


def test_clock_time_and_date_after_plus_are_speech_not_a_splice() -> None:
    (clock,) = _normalize("ЛАЙФ СЦЕНА (00:01:00) Он сказал + 5:30 утра")
    assert clock["sync_fragments"] == [] and clock["timecode_start"] == "00:01:00"
    assert clock["normalized_text"] == "Он сказал + 5:30 утра"
    (date,) = _normalize("ЛАЙФ СЦЕНА (00:01:00) счёт 1:0 + 3.10.20 что-то")
    assert date["sync_fragments"] == [] and date["normalized_text"] == "счёт 1:0 + 3.10.20 что-то"
    (sync,) = _normalize("СНХ ИМЯ 00:01:00 Он сказал + 5:30 утра")
    assert _frags(sync) == [("Он сказал + 5:30 утра", "00:01:00", "")]


@pytest.mark.parametrize("head", ["Драка.", "Драка!", "Драка"])
def test_life_description_with_sentence_punctuation_is_not_cut_inside_timecode(head: str) -> None:
    (block,) = _normalize(f"ЛАЙФ {head} (00:01:00) Первый + 00:02:00 Второй")
    assert block["source_description"] == "Драка"
    assert block["timecode_start"] == "00:01:00"
    assert _frags(block) == [("Первый", "00:01:00", ""), ("Второй", "00:02:00", "")]
    assert block["normalized_text"] == "Первый Второй"


def test_life_description_with_colon_is_still_split_but_not_inside_timecode() -> None:
    (block,) = _normalize("ЛАЙФ Драка. 00:01:00 Первый")
    assert block["source_description"] != "Драка. 00"
    assert block["timecode_start"] == "00:01:00" and block["normalized_text"] == "Первый"


# --- scene context (task 4) -------------------------------------------------


@pytest.mark.parametrize("case_id", ["dialog_in_life_scene", "dialog_in_life_scene_hyphen"])
def test_dialogue_replies_stay_in_life_scene_and_zk_starts_new_block(case_id: str) -> None:
    result = _parse(case_id)
    _assert_expected(result, case_id)
    zk = result["blocks"][3]
    assert zk["speaker"] == "" and zk["uid"] not in {b["uid"] for b in result["blocks"][:3]}


# --- confirmation gate (task 5) ---------------------------------------------


def _scenario(blocks: list[dict[str, Any]]) -> dict[str, Any]:
    return {"source": "fixture.txt", "source_hash": "fixture-hash", "blocks": blocks}


_GATE_IDS = [case["id"] for case in _SAVED]


_EXPECTED_CODES = {
    "snh_label_in_tts": {"service_label_in_tts"},
    "false_hero_from_cue": {"false_hero_from_cue"},
    "life_description_broken_by_timecode": {"timecode_in_role", "timecode_remnant_in_text"},
    "role_contains_timecode": {"timecode_in_role", "timecode_remnant_in_text"},
    "life_splice_saved_as_range": {"timecode_in_role", "lost_splice"},
}


@pytest.mark.parametrize("case", _SAVED, ids=_GATE_IDS)
def test_defective_saved_block_is_not_confirmable_silently(case: dict[str, Any]) -> None:
    scenario = _scenario(case["bad"])
    errors = validate_for_confirmation(scenario)
    assert errors, f"{case['id']}: {case['defect']} passed validation without remarks"
    blocking = [i for i in validate_for_confirmation_detailed(scenario) if i["severity"] == "error"]
    assert _EXPECTED_CODES[case["id"]] <= {i["code"] for i in blocking}
    assert {i["uid"] for i in blocking} == {case["bad"][0]["uid"]}
    assert all(i["order"] == 1 and i["message"] for i in blocking)


@pytest.mark.parametrize("case", _SAVED, ids=_GATE_IDS)
def test_corrected_saved_block_is_confirmable(case: dict[str, Any]) -> None:
    # Control: the same data after the fix must stay confirmable.
    assert validate_for_confirmation(_scenario(case["fixed"])) == []


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


def _codes(blocks: list[dict[str, Any]], severity: str = "error") -> set[str]:
    return {
        i["code"]
        for i in validate_for_confirmation_detailed(_scenario(blocks))
        if i["severity"] == severity
    }


def test_lost_speech_is_blocking_but_trimmed_cue_is_not() -> None:
    full = "ЗК1 Первая фраза длинная про пчёл и мёд. Вторая часть рассказывает про улей, воск и нектар."
    cut = "Первая фраза длинная про пчёл и мёд."
    lost = _block(original_text=full, normalized_text=cut, text_for_tts=cut, original_label="ЗК")
    assert "unmatched_speech" in _codes([lost])
    kept = _block(
        original_text=full,
        normalized_text=full[4:],
        text_for_tts=full[4:],
        original_label="ЗК",
    )
    assert _codes([kept]) == set()


def test_warnings_are_not_blocking_and_carry_codes() -> None:
    block = _block(
        warnings=[
            "Repeated author number ЗК7 (2 occurrences, first in block 1): check numbering",
            "Stand-up label: speaker is not identified, check manually",
            "LLM returned an unknown type",
        ],
        source="llm",
        confidence=0.4,
    )
    scenario = _scenario([block])
    assert validate_for_confirmation(scenario) == []
    assert _codes([block], "warning") == {
        "repeated_author_number",
        "standup_label",
        "low_confidence_llm",
    }


def test_snh_without_media_file_has_no_media_warning() -> None:
    (plain,) = _normalize("СНХ ИМЯ 01.24.08 Реплика")
    assert "Sync credit has no source media file" not in plain["warnings"]
    (dashed,) = _normalize("СНХ Имя — ZOOM0007.WAV 00:05:42 — Реплика")
    assert dashed["warnings"] == [] and dashed["source_description"] == "ZOOM0007.WAV"
    (empty,) = _normalize("СНХ Имя — 00:05:42 — Реплика")
    assert "Sync credit has no source media file" in empty["warnings"]


# --- SYNC credits without a dash / with take numbers and file names -----------


def _parse_paragraphs(paragraphs: list[str]) -> list[dict[str, Any]]:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        source = root / "scenario.txt"
        source.write_text("\n\n".join(paragraphs) + "\n", encoding="utf-8")
        return generate_normalization(
            source,
            root / "out.json",
            memory_path=root / "memory.sqlite3",
            use_llm=False,
        )["blocks"]


def test_sync_take_number_after_speaker_is_not_part_of_the_name() -> None:
    (block,) = _parse_paragraphs(
        ["СНХ ИВАН ПЕТРОВ 01 (01:30:16) храм построили после того, как появилась часовня."]
    )
    assert block["type"] == "sync"
    assert block["speaker"] == "ИВАН ПЕТРОВ"
    assert block["source_description"] == "01"
    assert block["timecode_start"] == "01:30:16"
    assert block["normalized_text"].startswith("храм построили")


def test_sync_media_file_name_after_speaker_goes_to_source_description() -> None:
    (block,) = _parse_paragraphs(
        ["СНХ ПЁТР СИДОРОВ 03_09_26_ARHITECTOR SIDOROV (00:05:38) В соборе проводились службы."]
    )
    assert block["type"] == "sync"
    assert block["speaker"] == "ПЁТР СИДОРОВ"
    assert block["source_description"] == "03_09_26_ARHITECTOR SIDOROV"
    assert block["timecode_start"] == "00:05:38"
    assert block["normalized_text"].startswith("В соборе")


def test_sync_without_timecode_one_letter_capital_starts_speech() -> None:
    (block,) = _parse_paragraphs(
        ["СНХ АННА САДОВНИЦА К некоторым вещам очень скептически относилась, если честно."]
    )
    assert block["type"] == "sync"
    assert block["speaker"] == "АННА САДОВНИЦА"
    assert block["normalized_text"].startswith("К некоторым")


def test_sync_without_timecode_lowercase_word_starts_speech() -> None:
    (block,) = _parse_paragraphs(
        ["СНХ САДОВНИЦА человек пролежал в коме больше месяца, и вот он решил прийти."]
    )
    assert block["type"] == "sync"
    assert block["speaker"] == "САДОВНИЦА"
    assert block["normalized_text"].startswith("человек пролежал")


def test_sync_label_with_only_caps_words_gets_no_invented_speech() -> None:
    (block,) = _parse_paragraphs(["СНХ ИВАН ПЕТРОВ"])
    assert block["speaker"] in ("", "ИВАН ПЕТРОВ")
    assert block["text_for_tts"] == ""


_UNLABELED_CREDIT_WARNING = "sync credit without СНХ label: check manually"


def test_unlabeled_credit_with_source_mark_keeps_whole_credit_and_source() -> None:
    (block,) = _normalize(
        "ИГУМЕН ИМЯ (ФАМИЛИЯ) ИСХ АВТОР ИМЯ ФАМИЛИЯ 00.35:30 речь продолжается здесь."
    )
    assert block["type"] == "sync" and block["speaker"] == "ИГУМЕН ИМЯ (ФАМИЛИЯ)"
    assert block["source_description"] == "АВТОР ИМЯ ФАМИЛИЯ"
    assert block["timecode_start"] == "00:35:30"
    assert _frags(block) == [("речь продолжается здесь.", "00:35:30", "")]
    assert _UNLABELED_CREDIT_WARNING in block["warnings"]


def test_unlabeled_credit_without_source_mark_keeps_bracketed_credit() -> None:
    (block,) = _normalize("ИГУМЕН ИМЯ (ФАМИЛИЯ) 00.35:30 речь продолжается здесь.")
    assert block["type"] == "sync" and block["speaker"] == "ИГУМЕН ИМЯ (ФАМИЛИЯ)"
    assert block["source_description"] == ""
    assert _frags(block) == [("речь продолжается здесь.", "00:35:30", "")]
    assert _UNLABELED_CREDIT_WARNING in block["warnings"]


def test_unlabeled_plain_caps_credit_and_headings_are_unchanged() -> None:
    sync, heading = _normalize(
        "ИГУМЕН ИМЯ ФАМИЛИЯ 00.35:30 речь продолжается здесь.", "ИСТОРИЯ ОДНОГО МОНАСТЫРЯ"
    )
    assert sync["speaker"] == "ИГУМЕН ИМЯ ФАМИЛИЯ" and sync["warnings"] == []
    assert heading["type"] != "sync" and heading["speaker"] == ""
