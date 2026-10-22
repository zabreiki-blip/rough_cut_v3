from stage1.presenter_cues import PresenterPart, parse_presenter_parts


def test_parses_presenter_and_location_caption() -> None:
    assert parse_presenter_parts(
        "СТЕНДАП ДАРЬЯ У РЕКИ КУЛИКОВО Великий Дон воспели многие поэты."
    ) == (PresenterPart("ДАРЬЯ", "У РЕКИ КУЛИКОВО", "Великий Дон воспели многие поэты."),)


def test_parses_presenter_without_location_caption() -> None:
    assert parse_presenter_parts("СТЕНДАП ДАРЬЯ После крестного хода...") == (
        PresenterPart("ДАРЬЯ", "", "После крестного хода..."),
    )


def test_preserves_timecode_for_downstream_splitter() -> None:
    source = "ПОДВОДКА ДАРЬЯ У БОЛЬШОГО СОБОРА [29:41.93 - 30:42.20] Мы привыкли..."
    assert parse_presenter_parts(source) == (
        PresenterPart("ДАРЬЯ", "У БОЛЬШОГО СОБОРА", "[29:41.93 - 30:42.20] Мы привыкли..."),
    )


def test_snh_leadin_marker_and_multiword_name() -> None:
    assert parse_presenter_parts("СНХ ПОДВОДКА МАРИЯ ИВАНОВА У ХРАМА Она говорит.") == (
        PresenterPart("МАРИЯ ИВАНОВА", "У ХРАМА", "Она говорит."),
    )


def test_plus_standup_yields_separate_parts_and_preserves_other_plus() -> None:
    assert parse_presenter_parts("+ СТЕНДАП ДАРЬЯ У МАЛОГО СОБОРА По его приказу... + ещё") == (
        PresenterPart("ДАРЬЯ", "У МАЛОГО СОБОРА", "По его приказу... + ещё"),
    )


def test_two_explicit_cues_split_into_separate_parts() -> None:
    assert parse_presenter_parts(
        "СТЕНДАП ДАРЬЯ У РЕКИ Первый текст. + СТЕНДАП МАРИЯ У ХРАМА Второй текст."
    ) == (
        PresenterPart("ДАРЬЯ", "У РЕКИ", "Первый текст."),
        PresenterPart("МАРИЯ", "У ХРАМА", "Второй текст."),
    )


def test_rejects_ambiguous_or_generic_cues() -> None:
    for source in (
        "СТЕНДАП Здесь мы расскажем.",
        "СТЕНДАП ТЕКСТ ВЕДУЩЕГО",
        "СТЕНДАП В КАДРЕ Здесь мы расскажем.",
        "СТЕНДАП У РЕКИ КУЛИКОВО Сегодня тепло.",
    ):
        assert parse_presenter_parts(source) == ()


def test_returns_empty_for_text_without_recognized_marker() -> None:
    assert parse_presenter_parts("ДАРЬЯ У РЕКИ Сейчас расскажем.") == ()


def test_marker_must_be_at_start_or_explicit_plus_continuation() -> None:
    assert parse_presenter_parts("В тексте был стендап ДАРЬЯ говорит.") == ()
    assert parse_presenter_parts(
        "СТЕНДАП ДАРЬЯ Она говорит, а потом произносит «+ СТЕНДАП МАРИЯ»."
    ) == (PresenterPart("ДАРЬЯ", "", "Она говорит, а потом произносит «+ СТЕНДАП МАРИЯ»."),)


def test_ambiguous_later_part_invalidates_entire_result() -> None:
    assert parse_presenter_parts("СТЕНДАП ДАРЬЯ Первый текст. + СТЕНДАП Здесь непонятно.") == ()


def test_timecode_variants_begin_spoken_text_and_are_preserved() -> None:
    assert parse_presenter_parts("СТЕНДАП МАРИЯ У ХРАМА (00:01:00) Я расскажу.") == (
        PresenterPart("МАРИЯ", "У ХРАМА", "(00:01:00) Я расскажу."),
    )
    assert parse_presenter_parts("ПОДВОДКА МАРИЯ У ХРАМА 00:01:00 Я расскажу.") == (
        PresenterPart("МАРИЯ", "У ХРАМА", "00:01:00 Я расскажу."),
    )


def test_capital_pronoun_starts_spoken_copy_and_quotes_are_kept() -> None:
    assert parse_presenter_parts("СТЕНДАП МАРИЯ Я расскажу «важную историю».") == (
        PresenterPart("МАРИЯ", "", "Я расскажу «важную историю»."),
    )


def test_name_is_bounded_and_uppercase_prose_does_not_extend_it() -> None:
    assert parse_presenter_parts("СТЕНДАП МАРИЯ ИВАНОВА ВЕСТИ СЕГОДНЯ важный сюжет.") == (
        PresenterPart("МАРИЯ ИВАНОВА", "", "ВЕСТИ СЕГОДНЯ важный сюжет."),
    )


def test_marker_requires_word_boundary() -> None:
    assert parse_presenter_parts("СТЕНДАПЕР МАРИЯ Она говорит.") == ()
    assert parse_presenter_parts("СТЕНДАП МАРИЯ Она говорит.") == (
        PresenterPart("МАРИЯ", "", "Она говорит."),
    )


def test_opening_quote_before_speech_is_preserved() -> None:
    assert parse_presenter_parts("СТЕНДАП МАРИЯ «Я расскажу историю».") == (
        PresenterPart("МАРИЯ", "", "«Я расскажу историю»."),
    )


def test_plus_marker_inside_quoted_speech_is_not_split_or_dropped() -> None:
    source = "СТЕНДАП МАРИЯ Она говорит: «текст + СТЕНДАП ДАРЬЯ»."
    assert parse_presenter_parts(source) == (
        PresenterPart("МАРИЯ", "", "Она говорит: «текст + СТЕНДАП ДАРЬЯ»."),
    )
