"""Pure-function tests for ``stage1.scenario_structure`` (no LLM, no IO)."""

from __future__ import annotations

import pytest

from stage1.scenario_structure import (
    detect_technical_cue,
    find_repeated_author_numbers,
    find_timecodes,
    canonicalize_timecodes,
    match_author_number,
    parse_paragraph,
    parse_timecode,
)


@pytest.mark.parametrize(
    ("text", "label", "label_raw", "number", "speech"),
    [
        ("ЗК1 Текст", "ЗК", "ЗК", "1", "Текст"),
        ("ЗК14 В том году…", "ЗК", "ЗК", "14", "В том году…"),
        ("ЗК14: Текст", "ЗК", "ЗК", "14", "Текст"),
        ("ЗК 1: Текст", "ЗК", "ЗК", "1", "Текст"),
        ("ЗК 5: Текст", "ЗК", "ЗК", "5", "Текст"),
        ("ЗК 5. Текст", "ЗК", "ЗК", "5", "Текст"),
        ("ЗК 5 - Текст", "ЗК", "ЗК", "5", "Текст"),
        ("ЗК0.1 25 марта 2025 года.", "ЗК", "ЗК", "0.1", "25 марта 2025 года."),
        ("ЗК1.10 Продолжение.", "ЗК", "ЗК", "1.10", "Продолжение."),
        ("З.К. Текст", "ЗК", "З.К.", "", "Текст"),
        ("З.К.7 Текст", "ЗК", "З.К.", "7", "Текст"),
        ("З.КВ 1915 году", "ЗК", "З.К", "", "В 1915 году"),
        ("СНХ Текст речи", "СНХ", "СНХ", "", "Текст речи"),
        ("ЛАЙФ Сцена", "ЛАЙФ", "ЛАЙФ", "", "Сцена"),
        ("ЛАЙФЫ Сцена", "ЛАЙФ", "ЛАЙФЫ", "", "Сцена"),
        ("Диктор-1 Текст реплики.", "ДИКТОР", "Диктор", "1", "Текст реплики."),
        ("ЗК", "ЗК", "ЗК", "", ""),
        ("ЗК 12 - Текст", "ЗК", "ЗК", "12", "Текст"),
        ("ЗК 5 — «Цитата»", "ЗК", "ЗК", "5", "«Цитата»"),
        ("ЗК 5:  Текст", "ЗК", "ЗК", "5", "Текст"),
        ("ZK 5: text", "ЗК", "ZK", "5", "text"),
        ("ZK5 text", "ЗК", "ZK", "5", "text"),
        ("Zk Text", "ЗК", "Zk", "", "Text"),
    ],
)
def test_label_number_and_speech(
    text: str, label: str, label_raw: str, number: str, speech: str
) -> None:
    result = parse_paragraph(text)
    assert (result.label, result.label_raw, result.author_number, result.speech) == (
        label,
        label_raw,
        number,
        speech,
    )


@pytest.mark.parametrize(
    ("text", "speech"),
    [
        ("ЗК 1915 год был тяжёлым.", "1915 год был тяжёлым."),
        ("ЗК 3 года спустя он вернулся.", "3 года спустя он вернулся."),
        ("ЗК 25 марта 2025 года.", "25 марта 2025 года."),
        ("ЗК 1.10 рубля.", "1.10 рубля."),
        ("ЗК 1915: год был тяжёлым.", "1915: год был тяжёлым."),
        ("ЗК1915 год был тяжёлым.", "1915 год был тяжёлым."),
        ("ЗК 5 - 10 лет спустя.", "5 - 10 лет спустя."),
        ("Диктор 1915 год был тяжёлым.", "1915 год был тяжёлым."),
        ("ЗК1 000 рублей", "1 000 рублей"),
        ("ЗК10 000 рублей", "10 000 рублей"),
        ("ЗК1 июля", "1 июля"),
        ("ЗК2 Июля Текст", "2 Июля Текст"),
        ("ЗК3 МАЯ Текст", "3 МАЯ Текст"),
        ("ЗК1 сентября.", "1 сентября."),
        ("ЗК 5:30 утра", "5:30 утра"),
        ("ЗК 12:30 вечера", "12:30 вечера"),
        ("ЗК5:30 утра", "5:30 утра"),
        ("ЗК 5 — это много", "5 — это много"),
        ("ЗК 3 - это три", "3 - это три"),
        ("ЗК1 - это", "1 - это"),
        ("Диктор 5 — это", "5 — это"),
    ],
)
def test_number_that_is_speech_is_kept(text: str, speech: str) -> None:
    result = parse_paragraph(text)
    assert result.author_number == ""
    assert result.speech == speech


@pytest.mark.parametrize(
    ("text", "number", "speech"),
    [
        ("ЗК1.4 Март 2005 года. Авиабаза.", "1.4", "Март 2005 года. Авиабаза."),
        ("ЗК0.1 Июля он вернулся.", "0.1", "Июля он вернулся."),
        ("ЗК 1.4: Март 2005 года.", "1.4", "Март 2005 года."),
    ],
)
def test_compound_author_number_is_always_a_number(text: str, number: str, speech: str) -> None:
    result = parse_paragraph(text)
    assert result.author_number == number
    assert result.speech == speech


def test_ranges_point_into_the_untouched_source() -> None:
    text = "  ЗК14: В том году…  "
    result = parse_paragraph(text)
    assert text[result.label_start : result.label_end] == "ЗК14"
    assert text[result.speech_start : result.speech_end] == "В том году…"
    assert result.text == text


def test_candidate_types_and_standup_warning() -> None:
    assert parse_paragraph("ЗК1 Текст").candidate_type == "voiceover"
    assert parse_paragraph("СНХ Текст").candidate_type == "sync"
    assert parse_paragraph("ЛАЙФЫ Сцена - (00:28:40) Вопрос?").candidate_type == "life"
    standup = parse_paragraph("СТЕНДАП ДАРЬЯ У СОБОРА Мы привыкли")
    assert standup.label == "СТЕНДАП"
    assert standup.speech == "ДАРЬЯ У СОБОРА Мы привыкли"
    assert standup.warnings, "stand-up speaker is not known: must be flagged for review"


def test_zk_followed_by_snh_is_a_label_conflict_not_voiceover() -> None:
    result = parse_paragraph("ЗК СНХ ИВАН ПЕТРОВ - (01:32:12) Донской монастырь…")
    assert result.has_label_conflict
    assert result.candidate_type == "unknown"
    assert result.warnings
    # A lowercase word that merely resembles a label stays ordinary speech.
    assert not parse_paragraph("ЗК Синхрон двух часов показал разницу.").has_label_conflict


@pytest.mark.parametrize(
    "text",
    [
        "БЫСТРАЯ НАРЕЗКА красивых кадров (улей, пчелы, служба)",
        "БЫСТРАЯ НАРЕЗКА красивых кадров",
        "ВОЗВРАЩАЕМСЯ В МОСКВУ ПО КАРТИНКЕ – показываем монастырь",
        "НАРЕЗКА",
    ],
)
def test_technical_cue(text: str) -> None:
    result = parse_paragraph(text)
    assert result.is_technical_cue
    assert result.candidate_type == "technical"
    assert not result.has_label


@pytest.mark.parametrize(
    "text",
    [
        "МОНТАЖ был сложным, говорит режиссёр.",
        "ПЕРЕБИВКА: Иван говорит своё",
        "Я ВОЗВРАЩАЕМСЯ домой",
        "ВОЗВРАЩАЕМСЯ В 1941 ГОД",
        "ВОЗВРАЩАЕМСЯ домой",
    ],
)
def test_speech_starting_with_a_cue_word_is_not_a_technical_cue(text: str) -> None:
    assert detect_technical_cue(text) == ""
    result = parse_paragraph(text)
    assert not result.is_technical_cue
    assert result.speech == text


@pytest.mark.parametrize(
    ("text", "cue"),
    [
        ("ПЕРЕБИВКА", "ПЕРЕБИВКА"),
        ("МОНТАЖ (быстрый, под музыку)", "МОНТАЖ"),
        ("ВОЗВРАЩАЕМСЯ – показываем монастырь", "ВОЗВРАЩАЕМСЯ"),
        ("ВОЗВРАЩАЕМСЯ В МОСКВУ ПО КАРТИНКЕ – показываем монастырь", "ВОЗВРАЩАЕМСЯ"),
    ],
)
def test_cue_with_description_is_technical(text: str, cue: str) -> None:
    assert detect_technical_cue(text) == cue


def test_hero_in_capitals_is_not_a_technical_cue() -> None:
    assert detect_technical_cue("ИВАН ПЕТРОВ, историк (00:03:37) в это время") == ""
    assert detect_technical_cue("Нарезка кадров") == ""  # not capitalised
    assert detect_technical_cue("СНХ, ИВАН ПЕТРОВ: Это речь.") == ""


def test_real_hero_credit_keeps_its_label() -> None:
    result = parse_paragraph("СНХ, ИВАН ПЕТРОВ: Это речь.")
    assert result.label == "СНХ"
    assert result.speech == "ИВАН ПЕТРОВ: Это речь."


def test_match_author_number_boundaries() -> None:
    assert match_author_number("ЗК14 Текст", 2) == ("14", 4)
    assert match_author_number("ЗК 5: Текст", 2) == ("5", 4)
    assert match_author_number("ЗК 3 года", 2) is None
    assert match_author_number("ЗК 1915 год", 2) is None


def test_leading_zero_author_number_is_the_same_number() -> None:
    items = [
        parse_paragraph(text) for text in ("ЗК7 а", "ЗК07 б", "ЗК1.10 в", "ЗК01.010 г", "ЗК0 д")
    ]
    assert find_repeated_author_numbers(items) == {("ЗК", "7"): [0, 1], ("ЗК", "1.10"): [2, 3]}


def test_lead_in_label_is_a_flagged_service_label() -> None:
    bare = parse_paragraph("ПОДВОДКА ДАРЬЯ У БОЛЬШОГО СОБОРА [29:41.93 - 30:42.20] Мы привыкли")
    assert bare.label == "ПОДВОДКА"
    assert bare.speech.startswith("ДАРЬЯ У БОЛЬШОГО СОБОРА")
    assert bare.warnings
    nested = parse_paragraph(
        "СНХ ПОДВОДКА ДАРЬЯ У БОЛЬШОГО СОБОРА [29:41.93 - 30:42.20] Мы привыкли"
    )
    assert nested.label == "СНХ"
    assert nested.candidate_type == "sync"
    assert nested.speech.startswith("ДАРЬЯ У БОЛЬШОГО СОБОРА")
    assert nested.warnings
    # Ordinary speech that starts with the same word is not a label.
    assert not parse_paragraph("Подводка к сюжету получилась длинной.").has_label


def test_timecode_without_hours_is_stored_as_minutes_seconds_without_fraction() -> None:
    # Uniform with the existing contract: no hours written -> ``MM:SS``; the
    # fraction is dropped because a fourth component would be read as frames.
    assert parse_timecode("29:41.93").stored == "29:41"
    assert parse_timecode("35:45").stored == "35:45"
    assert parse_timecode("00:42:32").stored == "00:42:32"
    assert canonicalize_timecodes("[29:41.93 - 30:42.20] Мы") == "29:41 - 30:42 Мы"


def test_find_repeated_author_numbers() -> None:
    items = [
        parse_paragraph(text)
        for text in ("ЗК7 а", "ЗК8 б", "ЗК7 в", "Диктор 1 г", "Диктор 1 д", "Просто текст")
    ]
    assert find_repeated_author_numbers(items) == {("ЗК", "7"): [0, 2]}


@pytest.mark.parametrize(
    "tail", ["+ 00:52:40 Второй", "+ 00.52.40 Второй", "+ (01:46:54) Второй", "+ 02:27 Второй"]
)
def test_splice_timecode_after_plus_needs_two_digit_components(tail: str) -> None:
    spans = find_timecodes(f"00:05:20 Первый {tail}", header_zone=True)
    assert len(spans) == 2


@pytest.mark.parametrize(
    "tail", ["+ 5:30 утра", "+ 3.10.20 что-то", "+ 1:00:00 ночи", "+ 7.15 вечера"]
)
def test_single_digit_hour_or_date_after_plus_is_speech(tail: str) -> None:
    spans = find_timecodes(f"00:05:20 Он сказал {tail}", header_zone=True)
    assert len(spans) == 1
