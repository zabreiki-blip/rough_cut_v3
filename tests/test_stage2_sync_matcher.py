from __future__ import annotations

import pytest
from unittest.mock import patch

import stage2.sync_matcher as sync_matcher_module

from stage2.sync_matcher import (
    MatcherConfig,
    SyncMatcher,
    _candidate_starts,
    _extract_numbers,
    _length_samples,
    _timed_tokens,
    normalize_text,
    normalize_token,
    parse_timecode,
)
from stage2.sync_models import AlignmentRequest, SyncSource, WordIndex, WordTimestamp
from stage2.sync_transcript import ingest_word_index


def _index(source_id: str, text: str, start: float, step: float = 0.5) -> WordIndex:
    words = text.split()
    return WordIndex(
        source_id=source_id,
        words=[
            WordTimestamp(word, start + index * step, start + (index + 1) * step)
            for index, word in enumerate(words)
        ],
    )


def _source(
    source_id: str = "vasilieva_01",
    speaker: str = "Татьяна Васильева",
    **kwargs: object,
) -> SyncSource:
    return SyncSource(
        id=source_id,
        speaker=speaker,
        premiere_sequence=f"SYNC_{source_id.upper()}",
        **kwargs,
    )


def test_russian_normalization_handles_case_yo_punctuation_and_inflections() -> None:
    assert normalize_text("Ёлкой, ЁЛКАМИ!") == ["елк", "елк"]
    assert normalize_text("энергетический вампир") == normalize_text("энергетическим вампиром")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("ПЁТР", "петр"),
        ("Ｐетр", "pетр"),
        ("AI", "ai"),
        ("Ли", "ли"),
    ],
)
def test_normalize_token_applies_only_safe_folding_to_short_or_latin_tokens(
    value: str,
    expected: str,
) -> None:
    assert normalize_token(value) == expected


def test_speaker_filter_matches_yo_and_case_variants() -> None:
    phrase = "точная цитата героя для синхронизации"
    result = SyncMatcher(
        [_source("petr", "Пётр Иванов")],
        [_index("petr", phrase, 10.0)],
    ).match(AlignmentRequest(segment_id="yo-name", text=phrase, speaker="ПЕТРА ИВАНОВА"))

    assert result.source_id == "petr"


def test_surname_alias_selects_source_without_matching_given_name() -> None:
    phrase = "цитата записана только у нужного человека"
    sources = [_source("vera", "Вера Сторожева"), _source("other", "Вера Иванова")]
    indexes = [_index("vera", phrase, 10), _index("other", phrase, 20)]
    result = SyncMatcher(sources, indexes).match(
        AlignmentRequest(segment_id="surname", text=phrase, speaker="СТОРОЖЕВА")
    )
    assert result.source_id == "vera"


def test_shared_given_name_does_not_admit_wrong_interview() -> None:
    phrase = "цитата записана только у другого человека"
    result = SyncMatcher([_source("other", "Вера Иванова")], [_index("other", phrase, 10)]).match(
        AlignmentRequest(segment_id="given-name", text=phrase, speaker="Вера Сторожева")
    )
    assert result.status == "not_found"


def test_decorated_surname_source_remains_eligible() -> None:
    phrase = "эту цитату сказала героиня интервью"
    result = SyncMatcher(
        [_source("viewing", "Сторожева дача")], [_index("viewing", phrase, 10)]
    ).match(AlignmentRequest(segment_id="decorated", text=phrase, speaker="Вера Сторожева"))
    assert result.source_id == "viewing"


def test_registered_short_alias_matches_full_speaker_name() -> None:
    phrase = "эту цитату сказала героиня интервью"
    result = SyncMatcher(
        [_source("viewing", "Люля", speakers=("Луэлла",))],
        [_index("viewing", phrase, 10)],
    ).match(AlignmentRequest(segment_id="alias", text=phrase, speaker="Луэлла Краснощекова"))
    assert result.source_id == "viewing"


def test_transcript_tokens_are_built_once_per_matcher() -> None:
    index = _index("s1", "одна и та же точная цитата героя", 10.0)
    with patch.object(
        sync_matcher_module,
        "_timed_tokens",
        wraps=sync_matcher_module._timed_tokens,
    ) as tokenize:
        matcher = SyncMatcher([_source("s1")], [index])
        for segment_id in ("one", "two"):
            matcher.match(
                AlignmentRequest(
                    segment_id=segment_id,
                    text="точная цитата героя",
                    source_ids=("s1",),
                )
            )

    assert tokenize.call_count == 1


def test_teaser_and_full_quote_can_reuse_overlapping_source_range() -> None:
    transcript = (
        "это важное решение изменит жизнь всего города "
        "потому что жители давно просили новый общественный транспорт"
    )
    matcher = SyncMatcher([_source("s1")], [_index("s1", transcript, 120.0)])

    teaser = matcher.match(
        AlignmentRequest(
            segment_id="teaser-block",
            text="это важное решение изменит жизнь всего города",
            source_ids=("s1",),
        )
    )
    full = matcher.match(
        AlignmentRequest(
            segment_id="full-block",
            text=transcript,
            source_ids=("s1",),
        )
    )

    assert teaser.segment_id == "teaser-block"
    assert full.segment_id == "full-block"
    assert teaser.source_id == full.source_id == "s1"
    assert teaser.source_in_sec == pytest.approx(full.source_in_sec)
    assert teaser.source_out_sec is not None
    assert full.source_out_sec is not None
    assert teaser.source_out_sec < full.source_out_sec


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("01:30", 90.0),
        ("01.30", 90.0),
        ("00:01:30", 90.0),
        ("00.42.51", 2571.0),
        ("01:00:00:12", 3600.48),
        ("12.5", 12.5),
        (12.5, 12.5),
    ],
)
def test_parse_timecode_formats(value: str | float, expected: float) -> None:
    assert parse_timecode(value, fps=25) == pytest.approx(expected)


def test_fuzzy_match_tolerates_whisper_insertions_and_word_inflections() -> None:
    transcript = (
        "совсем другой разговор перед началом "
        "она была ну настоящим энергетическим вампиром "
        "а рядом с ней находился известный поэт "
        "дальше новый вопрос"
    )
    matcher = SyncMatcher(
        [_source()],
        [_index("vasilieva_01", transcript, 1000.0)],
    )
    result = matcher.match(
        AlignmentRequest(
            segment_id="snh1",
            text="Она была энергетический вампир, а рядом поэт",
            speaker="ТАТЬЯНА ВАСИЛЬЕВА",
            approximate_timecode="00:16:50",
        )
    )

    assert result.source_id == "vasilieva_01"
    assert result.status in {"auto_matched", "needs_review"}
    assert result.confidence >= 0.70
    assert result.source_in_sec is not None and 999.0 <= result.source_in_sec <= 1001.0
    assert result.metadata["speech_in_sec"] == pytest.approx(1002.5)
    assert result.metadata["speech_out_sec"] > result.metadata["speech_in_sec"]
    assert "энергетическим" in result.matched_text


def test_near_timecode_wins_when_quote_occurs_twice() -> None:
    phrase = "этот редкий ключевой фрагмент звучит именно здесь"
    first = _index("s1", phrase, 100.0)
    second = _index("s1", phrase, 500.0)
    index = WordIndex(source_id="s1", words=[*first.words, *second.words])
    matcher = SyncMatcher([_source("s1")], [index])

    result = matcher.match(
        AlignmentRequest(
            segment_id="snh2",
            text=phrase,
            speaker="Татьяна Васильева",
            approximate_timecode="00:08:20",
        )
    )

    assert result.strategy == "near_timecode"
    assert result.source_in_sec is not None and result.source_in_sec > 490


def test_missing_local_match_falls_back_to_full_transcript_and_requires_review() -> None:
    phrase = "уникальный рассказ о старинном семейном архиве"
    matcher = SyncMatcher(
        [_source("s1")],
        [_index("s1", phrase, 900.0)],
        MatcherConfig(search_window_sec=30.0),
    )

    result = matcher.match(
        AlignmentRequest(
            segment_id="snh3",
            text=phrase,
            approximate_timecode="00:01:00",
            source_ids=("s1",),
        )
    )

    assert result.source_id == "s1"
    assert result.strategy == "fallback_full"
    assert result.status == "needs_review"
    assert result.confidence > 0.9


def test_ambiguous_equal_quotes_require_review_and_return_alternative() -> None:
    phrase = "мы обязательно вернемся к этому вопросу завтра"
    left = _index("s1", phrase, 100.0)
    right = _index("s1", phrase, 110.0)
    matcher = SyncMatcher(
        [_source("s1")],
        [WordIndex(source_id="s1", words=[*left.words, *right.words])],
    )

    result = matcher.match(
        AlignmentRequest(
            segment_id="snh4",
            text=phrase,
            approximate_timecode=105.0,
            source_ids=("s1",),
        )
    )

    assert result.status == "needs_review"
    assert result.metadata["ambiguous"] is True
    assert result.alternatives
    assert abs(result.alternatives[0].source_in_sec - result.source_in_sec) > 5  # type: ignore[operator]


def test_overlapping_variants_of_same_occurrence_are_not_ambiguous() -> None:
    phrase = "маленькая женщина которая покоряла великих людей"
    transcript = "нет маленькая маленькая женщина которая покоряла больших великих людей"
    result = SyncMatcher(
        [_source("s1")],
        [_index("s1", transcript, 100.0)],
    ).match(
        AlignmentRequest(
            segment_id="one-occurrence",
            text=phrase,
            speaker="Татьяна Васильева",
            approximate_timecode=101.0,
            source_ids=("s1",),
        )
    )
    assert result.metadata["ambiguous"] is False
    assert result.status == "auto_matched"


def test_low_similarity_returns_not_found_with_ranked_candidates() -> None:
    matcher = SyncMatcher(
        [_source("s1")],
        [_index("s1", "погода будет солнечной и теплой весь день", 20.0)],
    )
    result = matcher.match(
        AlignmentRequest(
            segment_id="snh5",
            text="космический корабль завершил посадку",
            source_ids=("s1",),
        )
    )
    assert result.status == "not_found"
    assert result.source_id == ""
    assert result.source_in_sec is None


def test_speaker_filter_selects_only_matching_registered_sequences() -> None:
    sources = [
        _source("vasilieva", "Татьяна Васильева"),
        _source("petrov", "Иван Петров"),
    ]
    indexes = [
        _index("vasilieva", "совсем другой текст без совпадения", 10.0),
        _index("petrov", "нужная точная цитата героя находится здесь", 10.0),
    ]
    result = SyncMatcher(sources, indexes).match(
        AlignmentRequest(
            segment_id="snh6",
            text="нужная точная цитата героя находится здесь",
            speaker="Татьяна Васильева",
        )
    )
    assert result.status == "not_found"


def test_rendered_viewing_wins_over_duplicate_legacy_premiere_source() -> None:
    phrase = "эта переставленная цитата найдена в другом месте интервью"
    sources = [
        _source("legacy", "Марк Бартон"),
        _source(
            "viewing",
            "Бартон",
            source_kind="review_media",
            media_path="media/reviews/180626_Бартон.mp4",
        ),
    ]
    indexes = [
        _index("legacy", phrase, 50.0),
        _index("viewing", phrase, 500.0),
    ]

    result = SyncMatcher(sources, indexes).match(
        AlignmentRequest(
            segment_id="reordered-snh",
            text=phrase,
            speaker="Бартон",
        )
    )

    assert result.source_id == "viewing"
    assert result.source_in_sec is not None and result.source_in_sec > 490
    assert result.metadata["ambiguous"] is False


@pytest.mark.parametrize("speaker", ["Ирина Котова", "Алексей Иванов", "Мария Петрова"])
def test_multi_speaker_source_is_eligible_for_every_registered_speaker(
    speaker: str,
) -> None:
    phrase = "редкая цитата записана на общей секвенции интервью"
    source = _source(
        "shared",
        "Ирина Котова",
        speakers=("Алексей Иванов", "Мария Петрова"),
    )

    result = SyncMatcher([source], [_index("shared", phrase, 10.0)]).match(
        AlignmentRequest(
            segment_id=f"shared-{speaker}",
            text=phrase,
            speaker=speaker,
        )
    )

    assert result.source_id == "shared"
    assert result.status in {"auto_matched", "needs_review"}


def test_multi_speaker_source_rejects_an_unregistered_speaker() -> None:
    phrase = "редкая цитата записана на общей секвенции интервью"
    source = _source(
        "shared",
        "Ирина Котова",
        speakers=("Алексей Иванов", "Мария Петрова"),
    )

    result = SyncMatcher([source], [_index("shared", phrase, 10.0)]).match(
        AlignmentRequest(
            segment_id="shared-unregistered",
            text=phrase,
            speaker="Сергей Сидоров",
        )
    )

    assert result.status == "not_found"
    assert result.strategy == "insufficient_input"


def test_all_speakers_source_is_searched_for_any_named_speaker() -> None:
    phrase = "эта цитата находится только на мастер секвенции"
    sources = [
        _source("master", "", all_speakers=True),
        _source("dedicated", "Ирина Котова"),
    ]
    indexes = [
        _index("master", phrase, 20.0),
        _index("dedicated", "совсем другой текст без совпадения", 20.0),
    ]

    result = SyncMatcher(sources, indexes).match(
        AlignmentRequest(
            segment_id="master-any-speaker",
            text=phrase,
            speaker="Ирина Котова",
        )
    )

    # Catch-all sources remain eligible alongside a speaker-specific source.
    assert result.source_id == "master"
    assert result.status in {"auto_matched", "needs_review"}


def test_missing_source_speaker_metadata_falls_back_to_text_search() -> None:
    phrase = "редкая точная цитата находится в этом интервью"
    result = SyncMatcher(
        [_source("s1", "")],
        [_index("s1", phrase, 12.0)],
    ).match(
        AlignmentRequest(
            segment_id="snh-missing-speaker",
            text=phrase,
            speaker="Имя из сторонней расшифровки",
        )
    )
    assert result.source_id == "s1"
    assert result.status in {"auto_matched", "needs_review"}


def test_differently_named_speaker_does_not_search_unrelated_interview() -> None:
    phrase = "редкая точная цитата другого героя"
    result = SyncMatcher(
        [_source("vasilieva", "Татьяна Васильева")],
        [_index("vasilieva", phrase, 10.0)],
    ).match(
        AlignmentRequest(
            segment_id="other-speaker",
            text=phrase,
            speaker="Александр Ирбе",
        )
    )
    assert result.status == "not_found"
    assert result.strategy == "insufficient_input"


def test_speaker_mismatch_records_filtered_source_in_metadata() -> None:
    phrase = "редкая точная цитата другого героя"
    result = SyncMatcher(
        [_source("vasilieva", "Татьяна Васильева")],
        [_index("vasilieva", phrase, 10.0)],
    ).match(
        AlignmentRequest(
            segment_id="other-speaker-metadata",
            text=phrase,
            speaker="Александр Ирбе",
        )
    )
    assert result.status == "not_found"
    filtered = result.metadata["speaker_filtered_sources"]
    assert filtered == [{"source_id": "vasilieva", "source_speakers": ["Татьяна Васильева"]}]


def test_speaker_filtered_sources_absent_when_every_source_is_eligible() -> None:
    phrase = "эта цитата находится только на мастер секвенции"
    sources = [
        _source("master", "", all_speakers=True),
        _source("dedicated", "Ирина Котова"),
    ]
    indexes = [
        _index("master", phrase, 20.0),
        _index("dedicated", "совсем другой текст без совпадения", 20.0),
    ]

    result = SyncMatcher(sources, indexes).match(
        AlignmentRequest(
            segment_id="master-any-speaker-metadata",
            text=phrase,
            speaker="Ирина Котова",
        )
    )

    assert "speaker_filtered_sources" not in result.metadata


def test_speaker_filtered_sources_present_on_not_found_with_candidates() -> None:
    sources = [
        _source("vasilieva", "Татьяна Васильева"),
        _source("petrov", "Иван Петров"),
    ]
    indexes = [
        _index("vasilieva", "совсем другой текст без совпадения", 10.0),
        _index("petrov", "нужная точная цитата героя находится здесь", 10.0),
    ]
    result = SyncMatcher(sources, indexes).match(
        AlignmentRequest(
            segment_id="snh6-metadata",
            text="нужная точная цитата героя находится здесь",
            speaker="Татьяна Васильева",
        )
    )
    assert result.status == "not_found"
    filtered = result.metadata["speaker_filtered_sources"]
    assert filtered == [{"source_id": "petrov", "source_speakers": ["Иван Петров"]}]


def test_long_queries_bound_candidate_search_without_losing_rare_anchor() -> None:
    words = ["обычное"] * 1500 + ["уникальныйякорь"] + ["обычное"] * 1500
    index = _index("s1", " ".join(words), 0.0, step=0.01)
    query = normalize_text(" ".join(["обычное"] * 50 + ["уникальныйякорь"] + ["обычное"] * 50))
    starts = _candidate_starts(query, _timed_tokens(index))
    assert len(starts) <= 240
    assert any(1445 <= start <= 1455 for start in starts)


def test_no_anchor_candidate_starts_are_fixed_size_and_deterministic() -> None:
    index = _index("s1", " ".join(["фоновый"] * 10_000), 0.0, step=0.01)
    query = normalize_text("совершенно иная короткая реплика")
    tokens = _timed_tokens(index)

    first = _candidate_starts(query, tokens)
    second = _candidate_starts(query, tokens)

    assert first == second
    assert len(first) == 240
    assert first[0] == 0
    assert first[-1] == len(tokens) - 1


def test_no_exact_anchor_keeps_likely_whisper_spelling_match() -> None:
    words = ["фоновый"] * 5_000
    words[3_217:3_220] = ["необычьная", "архитиктурная", "исторея"]
    index = _index("s1", " ".join(words), 0.0, step=0.01)
    starts = _candidate_starts(
        normalize_text("необычная архитектурная история"),
        _timed_tokens(index),
    )

    assert len(starts) <= 240
    assert any(3_215 <= start <= 3_219 for start in starts)


def test_low_score_timecode_fallback_shares_bounded_start_budget() -> None:
    transcript = " ".join(["фоновый"] * 4_000)
    matcher = SyncMatcher(
        [_source("s1")],
        [_index("s1", transcript, 0.0, step=0.25)],
        MatcherConfig(search_window_sec=30.0),
    )
    original = sync_matcher_module._candidate_starts
    calls: list[tuple[int, set[int]]] = []

    def recording_candidate_starts(
        query: list[str],
        tokens: list[sync_matcher_module._Token],
        *,
        max_starts: int = 240,
        eligible_starts: object = None,
    ) -> list[int]:
        starts = original(
            query,
            tokens,
            max_starts=max_starts,
            eligible_starts=eligible_starts,  # type: ignore[arg-type]
        )
        calls.append((max_starts, {round(tokens[start].word.start * 1000) for start in starts}))
        return starts

    with patch.object(
        sync_matcher_module,
        "_candidate_starts",
        side_effect=recording_candidate_starts,
    ):
        matcher.match(
            AlignmentRequest(
                segment_id="bounded-fallback",
                text="совершенно иная короткая реплика",
                approximate_timecode=500.0,
                source_ids=("s1",),
            )
        )

    assert len(calls) == 2
    assert sum(len(starts) for _limit, starts in calls) <= 240
    assert calls[0][0] == 120
    assert calls[1][0] == 120
    assert calls[0][1].isdisjoint(calls[1][1])


def test_long_length_ranges_are_sampled_then_refined() -> None:
    lengths, step = _length_samples(70, 156, 104)
    assert step > 1
    assert 70 in lengths and 104 in lengths and 156 in lengths
    assert len(lengths) < 25


def test_long_repetitive_paragraph_scoring_stays_bounded() -> None:
    phrase = " ".join(["повторяющийся"] * 100 + ["редкийякорь"])
    result = SyncMatcher(
        [_source("s1")],
        [_index("s1", phrase, 10.0, step=0.02)],
    ).match(
        AlignmentRequest(
            segment_id="long",
            text=phrase,
            source_ids=("s1",),
        )
    )
    assert result.confidence > 0.9


def test_display_timecode_and_guide_offset_are_converted_to_sequence_seconds() -> None:
    source = _source(
        "s1",
        sequence_start_tc="01:00:00:00",
        guide_offset_sec=10.0,
    )
    index = _index("s1", "точная цитата расположена после начала гида", 20.0)
    result = SyncMatcher([source], [index]).match(
        AlignmentRequest(
            segment_id="snh7",
            text="точная цитата расположена после начала гида",
            approximate_timecode="01:00:30:00",
            source_ids=("s1",),
            timecode_mode="display",
        )
    )
    assert result.strategy == "near_timecode"
    assert result.source_in_sec == pytest.approx(28.0)
    assert result.source_out_sec == pytest.approx(34.5)


def test_speech_past_registered_sequence_duration_is_rejected_even_with_handles() -> None:
    source = _source("s1", duration_sec=3.0)
    index = _index("s1", "короткая точная цитата в самом конце", 1.0)
    result = SyncMatcher([source], [index]).match(
        AlignmentRequest(
            segment_id="snh8",
            text="короткая точная цитата в самом конце",
            source_ids=("s1",),
        )
    )
    assert result.status == "not_found"
    assert result.source_out_sec is None


def test_words_past_registered_duration_cannot_auto_match_zero_length_range() -> None:
    source = _source("s1", duration_sec=3.0)
    index = _index("s1", "точная цитата уже за пределами секвенции", 4.0)
    result = SyncMatcher([source], [index]).match(
        AlignmentRequest(
            segment_id="snh9",
            text="точная цитата уже за пределами секвенции",
            source_ids=("s1",),
        )
    )

    assert result.status == "not_found"
    assert result.source_in_sec is None
    assert result.source_out_sec is None


def test_negated_scenario_matched_to_non_negated_transcript_needs_review() -> None:
    scenario = "Я не согласен с этим решением комиссии"
    transcript = "Я согласен с этим решением комиссии"
    result = SyncMatcher(
        [_source("s1")],
        [_index("s1", transcript, 20.0)],
    ).match(
        AlignmentRequest(
            segment_id="negation-flip",
            text=scenario,
            source_ids=("s1",),
        )
    )

    assert result.status == "needs_review"
    assert result.source_id == "s1"
    assert result.source_in_sec is not None and result.source_out_sec is not None
    assert result.confidence > 0
    assert result.metadata["semantic_flags"] == ["negation"]


def test_repeated_asr_negation_does_not_create_false_semantic_warning() -> None:
    scenario = "Она многое мне тогда не рассказывала"
    transcript = "Она многое мне тогда не не рассказывала"
    result = SyncMatcher(
        [_source("s1")],
        [_index("s1", transcript, 20.0)],
    ).match(
        AlignmentRequest(
            segment_id="asr-stutter",
            text=scenario,
            source_ids=("s1",),
        )
    )

    assert result.status == "auto_matched"
    assert result.metadata["semantic_flags"] == []


def test_joined_and_stuttered_neschastlivy_share_negation_meaning() -> None:
    scenario = "Когда люди несчастливы, они не рожают детей"
    transcript = "Когда люди не не счастливы, они не рожают детей"
    result = SyncMatcher(
        [_source("s1")],
        [_index("s1", transcript, 20.0)],
    ).match(
        AlignmentRequest(
            segment_id="joined-negation",
            text=scenario,
            source_ids=("s1",),
        )
    )

    assert result.status == "auto_matched"
    assert result.metadata["semantic_flags"] == []


def test_changed_number_between_scenario_and_transcript_needs_review() -> None:
    scenario = "Завод в этом квартале произвел 150 новых машин для города"
    transcript = "Завод в этом квартале произвел 500 новых машин для города"
    result = SyncMatcher(
        [_source("s1")],
        [_index("s1", transcript, 20.0)],
    ).match(
        AlignmentRequest(
            segment_id="number-flip",
            text=scenario,
            source_ids=("s1",),
        )
    )

    assert result.status == "needs_review"
    assert result.source_id == "s1"
    assert result.source_in_sec is not None and result.source_out_sec is not None
    assert result.confidence > 0
    assert result.metadata["semantic_flags"] == ["number"]


def test_number_spelled_out_in_transcript_matches_same_digit_in_scenario() -> None:
    scenario = "Завод в этом квартале произвел 150 новых машин для города"
    transcript = "Завод в этом квартале произвел сто пятьдесят новых машин для города"
    result = SyncMatcher(
        [_source("s1")],
        [_index("s1", transcript, 20.0)],
    ).match(
        AlignmentRequest(
            segment_id="number-spelled-out",
            text=scenario,
            source_ids=("s1",),
        )
    )

    assert result.status == "auto_matched"
    assert result.metadata["semantic_flags"] == []


def test_clean_match_without_numbers_or_negation_stays_auto_matched() -> None:
    phrase = "результаты голосования будут объявлены завтра утром"
    result = SyncMatcher(
        [_source("s1")],
        [_index("s1", phrase, 20.0)],
    ).match(
        AlignmentRequest(
            segment_id="clean-match",
            text=phrase,
            source_ids=("s1",),
        )
    )

    assert result.status == "auto_matched"
    assert result.metadata["semantic_flags"] == []


def _gapped_index(source_id: str, parts: list[tuple[str, float]]) -> WordIndex:
    words: list[WordTimestamp] = []
    for text, start in parts:
        for offset, word in enumerate(text.split()):
            words.append(WordTimestamp(word, start + offset * 0.3, start + offset * 0.3 + 0.3))
    return WordIndex(source_id=source_id, words=words)


def test_candidate_spanning_huge_time_gap_needs_review_with_gap_flag() -> None:
    index = _gapped_index("s1", [("мы обязательно", 10.0), ("вернемся завтра", 900.0)])
    result = SyncMatcher([_source("s1")], [index]).match(
        AlignmentRequest(
            segment_id="stitched",
            text="мы обязательно вернемся завтра",
            source_ids=("s1",),
        )
    )

    assert result.status == "needs_review"
    assert result.metadata["max_internal_gap_sec"] > 800
    assert "large_time_gap" in result.metadata["gap_flags"]


def test_natural_pause_below_threshold_stays_auto_matched() -> None:
    index = _gapped_index("s1", [("мы обязательно", 10.0), ("вернемся завтра", 12.1)])
    result = SyncMatcher([_source("s1")], [index]).match(
        AlignmentRequest(
            segment_id="pause",
            text="мы обязательно вернемся завтра",
            source_ids=("s1",),
        )
    )

    assert result.status == "auto_matched"
    assert result.metadata["gap_flags"] == []
    assert 1.0 < result.metadata["max_internal_gap_sec"] < 2.0


def test_internal_gap_threshold_is_configurable() -> None:
    index = _gapped_index("s1", [("мы обязательно", 10.0), ("вернемся завтра", 12.1)])
    result = SyncMatcher(
        [_source("s1")], [index], MatcherConfig(max_internal_gap_sec=1.0)
    ).match(
        AlignmentRequest(
            segment_id="pause-strict",
            text="мы обязательно вернемся завтра",
            source_ids=("s1",),
        )
    )

    assert result.status == "needs_review"
    assert result.metadata["gap_flags"] == ["large_time_gap"]


def _match_phrase(index: WordIndex, config: MatcherConfig | None = None, segment_id: str = "q"):
    return SyncMatcher([_source("s1")], [index], config).match(
        AlignmentRequest(
            segment_id=segment_id,
            text="мы обязательно вернемся завтра",
            source_ids=("s1",),
        )
    )


def _interpolated_index(start: float, end: float) -> WordIndex:
    return ingest_word_index(
        {"segments": [{"text": "мы обязательно вернемся завтра", "start": start, "end": end}]},
        "s1",
    )


def test_interpolated_segment_with_hidden_pause_needs_review_by_word_duration() -> None:
    index = _interpolated_index(10.0, 900.0)
    assert index.metadata["timestamp_precision"] == "interpolated"
    result = _match_phrase(index)

    assert result.status == "needs_review"
    assert result.metadata["gap_flags"] == ["large_time_gap"]
    # Interpolated words are contiguous: the hidden pause is not an inter-word gap.
    assert result.metadata["max_internal_gap_sec"] == 0.0
    assert result.metadata["max_word_duration_sec"] == pytest.approx(222.5)
    assert result.metadata["timestamp_precision"] == "interpolated"


def test_normal_interpolated_segment_stays_auto_matched() -> None:
    result = _match_phrase(_interpolated_index(10.0, 12.0))

    assert result.status == "auto_matched"
    assert result.metadata["gap_flags"] == []
    assert result.metadata["max_word_duration_sec"] == pytest.approx(0.5)
    assert result.metadata["timestamp_precision"] == "interpolated"


def test_real_word_timestamps_with_stretched_word_need_review() -> None:
    words = [
        WordTimestamp("мы", 10.0, 10.3),
        WordTimestamp("обязательно", 10.3, 40.0),
        WordTimestamp("вернемся", 40.0, 40.3),
        WordTimestamp("завтра", 40.3, 40.6),
    ]
    result = _match_phrase(WordIndex(source_id="s1", words=words))

    assert result.status == "needs_review"
    assert result.metadata["gap_flags"] == ["large_time_gap"]
    assert result.metadata["max_word_duration_sec"] == pytest.approx(29.7)


def test_word_duration_exactly_at_threshold_stays_auto_matched() -> None:
    words = [
        WordTimestamp("мы", 10.0, 10.3),
        WordTimestamp("обязательно", 10.3, 15.3),
        WordTimestamp("вернемся", 15.3, 15.6),
        WordTimestamp("завтра", 15.6, 15.9),
    ]
    result = _match_phrase(WordIndex(source_id="s1", words=words))

    assert result.status == "auto_matched"
    assert result.metadata["gap_flags"] == []


def test_word_duration_gate_uses_configured_threshold() -> None:
    result = _match_phrase(_interpolated_index(10.0, 12.0), MatcherConfig(max_internal_gap_sec=0.4))

    assert result.status == "needs_review"
    assert result.metadata["gap_flags"] == ["large_time_gap"]


def test_gap_exactly_at_threshold_stays_auto_matched() -> None:
    # Words are 0.3 s long; second part starts exactly 5.0 s after the first ends.
    index = _gapped_index("s1", [("мы обязательно", 10.0), ("вернемся завтра", 10.6 + 5.0)])
    result = _match_phrase(index, MatcherConfig(max_internal_gap_sec=5.0))

    assert result.status == "auto_matched"
    assert result.metadata["max_internal_gap_sec"] == pytest.approx(5.0)


def test_gap_just_above_threshold_needs_review() -> None:
    index = _gapped_index("s1", [("мы обязательно", 10.0), ("вернемся завтра", 10.6 + 5.01)])
    result = _match_phrase(index, MatcherConfig(max_internal_gap_sec=5.0))

    assert result.status == "needs_review"
    assert result.metadata["gap_flags"] == ["large_time_gap"]


def test_single_word_span_has_zero_gap_and_is_auto_matched() -> None:
    # The query is split ("пол года") while the transcript has one word, so the
    # best span is a single word; the low thresholds only let such a span through.
    index = WordIndex(source_id="s1", words=[WordTimestamp("полгода", 10.0, 10.5)])
    result = SyncMatcher(
        [_source("s1")],
        [index],
        MatcherConfig(auto_threshold=0.05, review_threshold=0.05),
    ).match(AlignmentRequest(segment_id="one", text="пол года", source_ids=("s1",)))

    assert result.status == "auto_matched"
    assert result.matched_text == "полгода"
    assert result.metadata["max_internal_gap_sec"] == 0.0
    assert result.metadata["max_word_duration_sec"] == pytest.approx(0.5)
    assert result.metadata["gap_flags"] == []


def test_overlapping_words_do_not_produce_negative_gap() -> None:
    words = [
        WordTimestamp("мы", 10.0, 10.6),
        WordTimestamp("обязательно", 10.3, 11.0),
        WordTimestamp("вернемся", 10.8, 11.4),
        WordTimestamp("завтра", 11.2, 11.8),
    ]
    result = _match_phrase(WordIndex(source_id="s1", words=words))

    assert result.status == "auto_matched"
    assert result.metadata["max_internal_gap_sec"] == 0.0
    assert result.metadata["gap_flags"] == []


@pytest.mark.parametrize("raw", ["12.5", " 12.5 "])
def test_gap_threshold_env_override_valid(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv("SYNC_MATCH_MAX_INTERNAL_GAP_SEC", raw)
    assert MatcherConfig().max_internal_gap_sec == 12.5


@pytest.mark.parametrize("raw", ["abc", "0", "-3", "inf", "-inf", "nan", ""])
def test_gap_threshold_env_override_invalid_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setenv("SYNC_MATCH_MAX_INTERNAL_GAP_SEC", raw)
    assert MatcherConfig().max_internal_gap_sec == sync_matcher_module.DEFAULT_MAX_INTERNAL_GAP_SEC


def test_gap_threshold_defaults_without_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SYNC_MATCH_MAX_INTERNAL_GAP_SEC", raising=False)
    assert MatcherConfig().max_internal_gap_sec == 5.0


@pytest.mark.parametrize("value", [0.0, -1.0, float("inf"), float("nan")])
def test_config_rejects_non_positive_or_non_finite_gap_threshold(value: float) -> None:
    with pytest.raises(ValueError, match="max_internal_gap_sec"):
        MatcherConfig(max_internal_gap_sec=value)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("два-три дня", [2, 3]),
        ("пять пятьдесят", [5, 50]),
        ("в 1915 году", [1915]),
        ("тысяча девятьсот пятнадцатый", [1915]),
        ("две тысячи двадцать пятый", [2025]),
        ("сорок пять тысяч", [45000]),
        ("сто пятьдесят", [150]),
        ("пять и пять", [5, 5]),
        ("5 и 5", [5, 5]),
    ],
)
def test_extract_numbers_only_sums_descending_place_values(text: str, expected: list[int]) -> None:
    assert _extract_numbers(text) == expected
