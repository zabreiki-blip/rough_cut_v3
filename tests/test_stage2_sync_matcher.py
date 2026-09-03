from __future__ import annotations

import pytest
from unittest.mock import patch

import stage2.sync_matcher as sync_matcher_module

from stage2.sync_matcher import (
    MatcherConfig,
    SyncMatcher,
    _candidate_starts,
    _length_samples,
    _timed_tokens,
    normalize_text,
    parse_timecode,
)
from stage2.sync_models import AlignmentRequest, SyncSource, WordIndex, WordTimestamp


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
    assert result.source_in_sec is not None and 1001.0 <= result.source_in_sec <= 1003.0
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
    assert result.source_in_sec == pytest.approx(29.5)
    assert result.source_out_sec == pytest.approx(34.0)


def test_tail_padding_is_clamped_to_registered_sequence_duration() -> None:
    source = _source("s1", duration_sec=3.0)
    index = _index("s1", "короткая точная цитата в самом конце", 1.0)
    result = SyncMatcher([source], [index]).match(
        AlignmentRequest(
            segment_id="snh8",
            text="короткая точная цитата в самом конце",
            source_ids=("s1",),
        )
    )
    assert result.source_out_sec == 3.0


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
