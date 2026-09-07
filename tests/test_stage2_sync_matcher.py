from __future__ import annotations

import pytest
from unittest.mock import patch

import stage2.sync_matcher as sync_matcher_module

from stage2.sync_matcher import (
    MatcherConfig,
    SyncMatcher,
    _Candidate,
    _candidate_starts,
    _has_plausible_duration,
    _has_reliable_word_timestamps,
    _length_samples,
    _timed_tokens,
    _Token,
    _trim_span_to_match,
    normalize_text,
    normalize_token,
    parse_timecode,
)
from stage2.sync_models import AlignmentRequest, SyncSource, WordIndex, WordTimestamp


def _index(
    source_id: str,
    text: str,
    start: float,
    step: float = 0.5,
    metadata: dict | None = None,
) -> WordIndex:
    words = text.split()
    return WordIndex(
        source_id=source_id,
        words=[
            WordTimestamp(word, start + index * step, start + (index + 1) * step)
            for index, word in enumerate(words)
        ],
        metadata=dict(metadata) if metadata is not None else {},
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


def _span(words: list[str], start: float = 0.0, step: float = 0.5) -> list[_Token]:
    return [
        _Token(
            normalize_token(word),
            WordTimestamp(word, start + index * step, start + (index + 1) * step),
        )
        for index, word in enumerate(words)
    ]


def test_trim_span_to_match_drops_only_unmatched_head_and_tail_words() -> None:
    query = normalize_text("мы хотели разладить отношения")
    span = _span(["ну", "мы", "хотели", "разладить", "отношения", "вот", "я", "ж"])

    trimmed = _trim_span_to_match(query, span)

    assert trimmed is not None
    assert [item.word.word for item in trimmed] == ["мы", "хотели", "разладить", "отношения"]
    # Trimming only cuts the edges; nothing inside the matched run is dropped.
    assert trimmed == span[1:5]


def test_trim_span_to_match_is_a_no_op_when_the_whole_span_matches() -> None:
    query = normalize_text("мы хотели разладить отношения")
    span = _span(["мы", "хотели", "разладить", "отношения"])

    trimmed = _trim_span_to_match(query, span)

    assert trimmed == span


def test_trim_span_to_match_returns_none_when_nothing_matches() -> None:
    query = normalize_text("абсолютно другая фраза")
    span = _span(["ну", "мы", "хотели", "разладить", "отношения", "вот"])

    assert _trim_span_to_match(query, span) is None


def test_matched_span_excludes_trailing_noise_and_raises_confidence() -> None:
    transcript = "вступление перед цитатой они хотели разладить отношения ну потом дальше по тексту"
    matcher = SyncMatcher(
        [_source()],
        [_index("vasilieva_01", transcript, 1000.0)],
    )

    result = matcher.match(
        AlignmentRequest(
            segment_id="trim-tail",
            text="они хотели разладить отношения",
            speaker="Татьяна Васильева",
        )
    )

    assert result.status == "auto_matched"
    assert result.confidence >= 0.95
    assert "ну" not in result.matched_text.lower().split()
    # The trailing filler word ("ну", 0.5s long) must not leak into the clip
    # boundary beyond the configured tail padding.
    assert result.source_out_sec is not None and result.source_out_sec < 1004.6


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


def test_anchor_bounds_restrict_search_to_interval_and_use_between_anchors_strategy() -> None:
    phrase = "этот редкий ключевой фрагмент звучит именно здесь"
    inside = _index("s1", phrase, 500.0)
    outside = _index("s1", phrase, 100.0)
    index = WordIndex(source_id="s1", words=[*outside.words, *inside.words])
    matcher = SyncMatcher([_source("s1")], [index])

    result = matcher.match(
        AlignmentRequest(
            segment_id="snh-anchor",
            text=phrase,
            anchor_source_id="s1",
            anchor_lower_sec=400.0,
            anchor_upper_sec=600.0,
        )
    )

    assert result.strategy == "between_anchors"
    assert result.source_id == "s1"
    assert result.source_in_sec is not None and result.source_in_sec > 490


def test_anchor_bounds_never_fall_back_outside_the_interval() -> None:
    phrase = "уникальный рассказ о старинном семейном архиве"
    matcher = SyncMatcher(
        [_source("s1")],
        [_index("s1", phrase, 900.0)],
    )

    result = matcher.match(
        AlignmentRequest(
            segment_id="snh-anchor-2",
            text=phrase,
            anchor_source_id="s1",
            anchor_lower_sec=0.0,
            anchor_upper_sec=30.0,
        )
    )

    assert result.status == "not_found"
    assert result.strategy != "fallback_full"
    assert result.source_id == ""


def test_unset_anchor_fields_keep_prior_behaviour() -> None:
    phrase = "этот редкий ключевой фрагмент звучит именно здесь"
    first = _index("s1", phrase, 100.0)
    second = _index("s1", phrase, 500.0)
    index = WordIndex(source_id="s1", words=[*first.words, *second.words])
    matcher = SyncMatcher([_source("s1")], [index])

    result = matcher.match(
        AlignmentRequest(
            segment_id="snh-anchor-3",
            text=phrase,
            speaker="Татьяна Васильева",
            approximate_timecode="00:08:20",
        )
    )

    assert result.strategy == "near_timecode"
    assert result.source_in_sec is not None and result.source_in_sec > 490


def _labeled_index(
    source_id: str,
    text: str,
    speaker_labels: list[str],
    start: float = 0.0,
    step: float = 0.5,
) -> WordIndex:
    words = text.split()
    assert len(words) == len(speaker_labels)
    return WordIndex(
        source_id=source_id,
        words=[
            WordTimestamp(
                word,
                start + index * step,
                start + (index + 1) * step,
                speaker=speaker_labels[index],
            )
            for index, word in enumerate(words)
        ],
        metadata={"has_speaker_labels": True},
    )


def test_voice_label_rejects_candidate_dominated_by_another_voice() -> None:
    phrase = "мы обсуждали важные детали проекта завтра"
    index = _labeled_index("s1", phrase, ["ГОЛОС 2"] * len(phrase.split()))
    matcher = SyncMatcher([_source("s1")], [index])

    result = matcher.match(
        AlignmentRequest(
            segment_id="voice-1",
            text=phrase,
            voice_label="ГОЛОС 1",
        )
    )

    assert result.status == "not_found"
    assert result.source_id == ""


def test_voice_label_accepts_candidate_from_requested_voice() -> None:
    phrase = "мы обсуждали важные детали проекта завтра"
    index = _labeled_index("s1", phrase, ["ГОЛОС 1"] * len(phrase.split()))
    matcher = SyncMatcher([_source("s1")], [index])

    result = matcher.match(
        AlignmentRequest(
            segment_id="voice-2",
            text=phrase,
            voice_label="ГОЛОС 1",
        )
    )

    assert result.status == "auto_matched"
    assert result.source_id == "s1"
    assert result.strategy == "voice_scoped"


def test_voice_label_is_ignored_when_source_has_no_speaker_labels() -> None:
    phrase = "мы обсуждали важные детали проекта завтра"
    index = _index("s1", phrase, 0.0)
    matcher_with_voice = SyncMatcher([_source("s1")], [index])
    matcher_without_voice = SyncMatcher([_source("s1")], [index])

    with_voice = matcher_with_voice.match(
        AlignmentRequest(segment_id="voice-3", text=phrase, voice_label="ГОЛОС 1")
    )
    without_voice = matcher_without_voice.match(
        AlignmentRequest(segment_id="voice-3", text=phrase)
    )

    assert with_voice.status == without_voice.status == "auto_matched"
    assert with_voice.source_in_sec == without_voice.source_in_sec
    assert with_voice.strategy == without_voice.strategy != "voice_scoped"


def test_voice_share_threshold_around_min_voice_share() -> None:
    assert sync_matcher_module._MIN_VOICE_SHARE == 0.6
    phrase_words = "раз два три четыре пять шесть семь восемь девять десять".split()
    # 5.5/10 words from the requested voice: below the 0.6 threshold.
    below_threshold_labels = ["ГОЛОС 1"] * 5 + ["ГОЛОС 2"] * 5
    below_index = _labeled_index("s1", " ".join(phrase_words), below_threshold_labels)
    below_matcher = SyncMatcher([_source("s1")], [below_index])
    below_result = below_matcher.match(
        AlignmentRequest(segment_id="voice-below", text=" ".join(phrase_words), voice_label="ГОЛОС 1")
    )
    assert below_result.status == "not_found"

    # 6.5/10 words from the requested voice: above the 0.6 threshold.
    above_threshold_labels = ["ГОЛОС 1"] * 7 + ["ГОЛОС 2"] * 3
    above_index = _labeled_index("s1", " ".join(phrase_words), above_threshold_labels)
    above_matcher = SyncMatcher([_source("s1")], [above_index])
    above_result = above_matcher.match(
        AlignmentRequest(segment_id="voice-above", text=" ".join(phrase_words), voice_label="ГОЛОС 1")
    )
    assert above_result.status == "auto_matched"
    assert above_result.source_id == "s1"


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


def test_multi_speaker_source_is_isolated_from_an_unregistered_speaker() -> None:
    # Every registered source is tagged with a real, explicitly named
    # speaker, and none of them match the request. A last-resort fallback
    # that searched the mismatched source anyway used to weaken isolation
    # between unrelated interviews (see the RVK_COSTEL regression), so the
    # source pool is now correctly empty and the segment is left unmatched.
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


def test_differently_named_speaker_is_isolated_from_the_only_source() -> None:
    # A single registered source tagged with an unrelated real name is not
    # a valid match for a different named speaker's text, so the request
    # correctly falls through to "insufficient_input" instead of being
    # silently routed into someone else's interview.
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


def test_media_scan_derived_speaker_token_remains_eligible_for_real_speaker() -> None:
    # An auto-scanned viewing carries a "speaker" derived from its file name
    # (e.g. "KOSTEL SP 001"), which never matches a real scenario speaker
    # name. Stage 3 flags such sources via metadata (managed_by +
    # media_name matching the derived label), so the matcher must ignore
    # that token and keep the source searchable for any real speaker.
    phrase = "редкая точная цитата настоящего героя"
    source = _source(
        "kostel",
        "KOSTEL SP 001",
        all_speakers=False,
        metadata={"managed_by": "stage3_media_scan", "media_name": "KOSTEL_SP_001.mp4"},
    )
    result = SyncMatcher([source], [_index("kostel", phrase, 10.0)]).match(
        AlignmentRequest(
            segment_id="media-scan-derived-speaker",
            text=phrase,
            speaker="ЕЛЕНА МИНЧЕНКО",
        )
    )
    assert result.source_id == "kostel"
    assert result.strategy != "insufficient_input"
    assert result.status != "not_found"


def test_two_media_scan_sources_both_remain_eligible_for_a_third_party_speaker() -> None:
    # Regression test for the RVK_COSTEL bug: two auto-scanned viewings with
    # file-derived "speaker" labels (one all_speakers=True, one False) must
    # both stay eligible for a request naming a real speaker that belongs to
    # neither -- the correct interview must not be starved out by the other
    # auto-scanned viewing's stale universal fallback.
    phrase = "уникальная фраза из настоящего интервью"
    sources = [
        _source(
            "media_e4ef",
            "KOSTEL SP 001",
            all_speakers=False,
            metadata={"managed_by": "stage3_media_scan", "media_name": "KOSTEL_SP_001.mp4"},
        ),
        _source(
            "media_bb23",
            "C7862",
            all_speakers=True,
            metadata={"managed_by": "stage3_media_scan", "media_name": "C7862.mp4"},
        ),
    ]
    indexes = [
        _index("media_e4ef", phrase, 10.0),
        _index("media_bb23", "совсем другой текст без совпадения", 10.0),
    ]
    matcher = SyncMatcher(sources, indexes)
    eligible = {
        source.id
        for source in matcher._eligible_sources(
            AlignmentRequest(
                segment_id="two-scan-sources",
                text=phrase,
                speaker="ЕЛЕНА МИНЧЕНКО",
            )
        )
    }
    assert eligible == {"media_e4ef", "media_bb23"}

    result = matcher.match(
        AlignmentRequest(
            segment_id="two-scan-sources-match",
            text=phrase,
            speaker="ЕЛЕНА МИНЧЕНКО",
        )
    )
    assert result.source_id == "media_e4ef"
    assert result.status == "auto_matched"


def test_media_scan_source_with_manually_named_speaker_stays_isolated() -> None:
    # Once a user has renamed a scanned source's speaker to a real person's
    # name, that name is no longer a file-name guess (the derived label from
    # the media name does not match it), so it must be treated like any
    # other named speaker and keep the usual isolation between interviews.
    # Both sources here are all_speakers=False so the deliberate "search me
    # for anyone" universal-source behaviour cannot mask the isolation check.
    sources = [
        _source(
            "media_e4ef",
            "KOSTEL SP 001",
            all_speakers=False,
            metadata={"managed_by": "stage3_media_scan", "media_name": "KOSTEL_SP_001.mp4"},
        ),
        _source(
            "media_bb23",
            "ЕЛЕНА МИНЧЕНКО",
            all_speakers=False,
            metadata={"managed_by": "stage3_media_scan", "media_name": "20260807_C7862.mp4"},
        ),
    ]
    indexes = [
        _index("media_e4ef", "любой текст", 10.0),
        _index("media_bb23", "любой другой текст", 10.0),
    ]
    matcher = SyncMatcher(sources, indexes)
    eligible = {
        source.id
        for source in matcher._eligible_sources(
            AlignmentRequest(
                segment_id="named-scan-source-isolated",
                text="любой текст",
                speaker="Другой Спикер",
            )
        )
    }
    # "Другой Спикер" matches neither source's real name: media_e4ef stays
    # eligible because its speaker label is derived and thus ignored, while
    # media_bb23 is excluded because its (manually set, real) name does not
    # match the request.
    assert eligible == {"media_e4ef"}


def test_exact_speaker_match_still_wins_over_untagged_source() -> None:
    # Priority order holds regardless of the derived-label handling: when a
    # source with an exact speaker match exists, an unrelated named
    # speaker's source is not searched at all.
    phrase = "редкая точная цитата настоящего героя"
    sources = [
        _source("vasilieva", "Татьяна Васильева"),
        _source(
            "kostel",
            "KOSTEL SP 001",
            all_speakers=False,
            metadata={"managed_by": "stage3_media_scan", "media_name": "KOSTEL_SP_001.mp4"},
        ),
    ]
    indexes = [
        _index("vasilieva", phrase, 10.0),
        _index("kostel", "совсем другой текст без совпадения", 10.0),
    ]
    result = SyncMatcher(sources, indexes).match(
        AlignmentRequest(
            segment_id="exact-still-wins",
            text=phrase,
            speaker="Татьяна Васильева",
        )
    )
    assert result.source_id == "vasilieva"


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


def _duration_candidate(start: float, end: float) -> _Candidate:
    return _Candidate(
        source_id="s1",
        start=start,
        end=end,
        confidence=0.5,
        text="irrelevant",
        strategy="full_transcript",
    )


def test_plausible_duration_rejects_implausibly_fast_candidate() -> None:
    # 40 query words matched to a 3-second span implies ~13.3 words/sec,
    # far above any realistic Russian speech rate -- a text-similarity
    # fluke, not a real utterance.
    candidate = _duration_candidate(100.0, 103.0)
    assert _has_plausible_duration(candidate, query_len=40, config=MatcherConfig()) is False


def test_plausible_duration_accepts_normal_pace_candidate() -> None:
    # 40 words over 15 seconds is ~2.7 words/sec, well within normal speech.
    candidate = _duration_candidate(100.0, 115.0)
    assert _has_plausible_duration(candidate, query_len=40, config=MatcherConfig()) is True


def test_plausible_duration_rejects_implausibly_slow_candidate() -> None:
    # 40 words over 300 seconds is ~0.13 words/sec -- far too slow to be a
    # real reading of that much text; likely an over-wide, wrongly trimmed
    # span.
    candidate = _duration_candidate(100.0, 400.0)
    assert _has_plausible_duration(candidate, query_len=40, config=MatcherConfig()) is False


def test_plausible_duration_is_skipped_for_very_short_queries() -> None:
    # A 2-3 word query has too little statistical signal for a words-per-sec
    # sanity check to be reliable, so the filter is a no-op regardless of
    # duration.
    fast_candidate = _duration_candidate(100.0, 100.2)
    slow_candidate = _duration_candidate(100.0, 500.0)
    config = MatcherConfig()
    assert _has_plausible_duration(fast_candidate, query_len=2, config=config) is True
    assert _has_plausible_duration(slow_candidate, query_len=3, config=config) is True


def test_match_keeps_correct_slow_paced_match_despite_duration_filter() -> None:
    # A realistic ~3.3 words/sec reading of a 20-word quote must still auto-
    # match once the duration-plausibility filter is applied.
    transcript = (
        "мы долго обсуждали дальнейшую судьбу этого проекта и в итоге решили "
        "переработать сценарий полностью прежде чем показывать его команде"
    )
    matcher = SyncMatcher(
        [_source()],
        [_index("vasilieva_01", transcript, 1000.0, step=0.3)],
    )

    result = matcher.match(
        AlignmentRequest(
            segment_id="duration-ok",
            text=transcript,
            speaker="Татьяна Васильева",
        )
    )

    assert result.status == "auto_matched"


def test_match_reports_duration_implausible_candidate_as_alternative_not_lost() -> None:
    # The only textual match is stretched over a physically implausible
    # duration (40 words in ~0.4s). It must not surface as a confident
    # match, but it also must not vanish from diagnostics -- an operator
    # should still see it listed as an alternative for manual review.
    query_text = " ".join(f"слово{i}редкое" for i in range(40))
    matcher = SyncMatcher(
        [_source()],
        [_index("vasilieva_01", query_text, 1000.0, step=0.01)],
    )

    result = matcher.match(
        AlignmentRequest(
            segment_id="duration-bad",
            text=query_text,
            speaker="Татьяна Васильева",
        )
    )

    assert result.status == "not_found"
    assert result.alternatives
    assert result.alternatives[0].confidence > 0.9


def test_reliable_word_timestamps_prefers_backend_metadata_precision() -> None:
    index = _index(
        "s1",
        "ok",
        0.0,
        metadata={
            "backend": "cloud-audio",
            "timestamp_precision": "word",  # stale/misleading top-level value
            "backend_metadata": {"timestamp_precision": "interpolated"},
        },
    )
    assert _has_reliable_word_timestamps(index) is False


def test_reliable_word_timestamps_falls_back_to_top_level_precision() -> None:
    index = _index("s1", "ok", 0.0, metadata={"timestamp_precision": "interpolated"})
    assert _has_reliable_word_timestamps(index) is False

    index_word = _index("s1", "ok", 0.0, metadata={"timestamp_precision": "word"})
    assert _has_reliable_word_timestamps(index_word) is True


def test_reliable_word_timestamps_falls_back_to_cloud_audio_backend_name() -> None:
    # No explicit precision recorded anywhere: only cloud-audio is known to
    # interpolate, so it is the only backend treated as unreliable by name.
    cloud_index = _index("s1", "ok", 0.0, metadata={"backend": "cloud-audio"})
    assert _has_reliable_word_timestamps(cloud_index) is False

    local_index = _index("s1", "ok", 0.0, metadata={"backend": "mlx-whisper"})
    assert _has_reliable_word_timestamps(local_index) is True

    unlabeled_index = _index("s1", "ok", 0.0)
    assert _has_reliable_word_timestamps(unlabeled_index) is True


def test_match_skips_duration_filter_for_interpolated_source() -> None:
    # Same physically-impossible-tempo transcript as the word-level test
    # above (40 words crammed into ~0.4s), but this source is tagged as
    # having cloud-audio interpolated timestamps -- the speech-rate filter
    # must not reject a textually exact match on that source, since the
    # implausible rate is a timestamp artifact, not a text-match problem.
    query_text = " ".join(f"слово{i}редкое" for i in range(40))
    matcher = SyncMatcher(
        [_source()],
        [
            _index(
                "vasilieva_01",
                query_text,
                1000.0,
                step=0.01,
                metadata={"backend_metadata": {"timestamp_precision": "interpolated"}},
            )
        ],
    )

    result = matcher.match(
        AlignmentRequest(
            segment_id="duration-interpolated",
            text=query_text,
            speaker="Татьяна Васильева",
        )
    )

    assert result.status == "auto_matched"


def test_match_applies_duration_filter_for_word_level_source() -> None:
    # The exact same crammed-timing candidate, but this time the source is
    # explicitly tagged as word-level (measured) timestamps: the implausible
    # rate is real, and the filter must still reject it as unfiltered above.
    query_text = " ".join(f"слово{i}редкое" for i in range(40))
    matcher = SyncMatcher(
        [_source()],
        [
            _index(
                "vasilieva_01",
                query_text,
                1000.0,
                step=0.01,
                metadata={"timestamp_precision": "word"},
            )
        ],
    )

    result = matcher.match(
        AlignmentRequest(
            segment_id="duration-word-level",
            text=query_text,
            speaker="Татьяна Васильева",
        )
    )

    assert result.status == "not_found"
    assert result.alternatives
    assert result.alternatives[0].confidence > 0.9
