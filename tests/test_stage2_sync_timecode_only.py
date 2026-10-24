"""Sync-блок с одним таймкодом вместо цитаты: кандидат прямо на таймкоде (needs_review)."""

from __future__ import annotations

import pytest

import sync_audio_verify_phase as phase
from stage2.sync_audio_verify import AudioVerdict
from stage2.sync_matcher import SyncMatcher
from stage2.sync_models import AlignmentRequest, SyncSource, WordIndex, WordTimestamp

SPEAKER = "Виктор Литовкин"


def _source(source_id: str, speaker: str = SPEAKER, **kwargs: object) -> SyncSource:
    return SyncSource(
        id=source_id, speaker=speaker, premiere_sequence=f"SYNC_{source_id.upper()}", **kwargs
    )


def _words(*items: tuple[str, float, float]) -> WordIndex:
    return WordIndex(
        source_id="s1", words=[WordTimestamp(word, start, end) for word, start, end in items]
    )


def _request(text: str = "00:05:42", **kwargs: object) -> AlignmentRequest:
    return AlignmentRequest(
        segment_id="b9", text=text, speaker="Виктор ЛИТОВКИН", approximate_timecode=text, **kwargs
    )


def _phrase_index(source_id: str = "s1", start: float = 342.0, offset: float = 0.0) -> WordIndex:
    # Фраза 342.0-345.0 с, пауза 1.5 с, затем следующая фраза.
    items = [("Это", start, start + 0.5), ("важная", start + 0.5, start + 1.5)]
    items += [("мысль", start + 1.5, start + 3.0)]
    items += [("Потом", start + 4.5, start + 5.0), ("другое", start + 5.0, start + 5.5)]
    return WordIndex(
        source_id=source_id, words=[WordTimestamp(w, s + offset, e + offset) for w, s, e in items]
    )


def test_timecode_only_gives_needs_review_candidate_ending_at_pause() -> None:
    source = _source("s1", duration_sec=600.0)
    result = SyncMatcher([source], [_phrase_index()]).match(_request())
    assert result.status == "needs_review"
    assert result.strategy == "timecode_only"
    assert result.metadata["review_reason"] == "timecode_only"
    assert result.source_id == "s1"
    assert result.source_in_sec == pytest.approx(340.0)  # таймкод - head padding 2 с
    assert result.metadata["speech_in_sec"] == pytest.approx(342.0)
    assert result.metadata["speech_out_sec"] == pytest.approx(345.0)  # пауза 1.5 с > 0.7 с
    assert result.source_out_sec == pytest.approx(346.5)  # + tail padding 1.5 с


def test_timecode_only_without_words_uses_fixed_ten_seconds() -> None:
    source = _source("s1", duration_sec=600.0)
    result = SyncMatcher([source], [WordIndex(source_id="s1")]).match(_request())
    assert result.status == "needs_review"
    assert result.metadata["review_reason"] == "timecode_only"
    assert result.metadata["speech_in_sec"] == pytest.approx(342.0)
    assert result.metadata["speech_out_sec"] == pytest.approx(352.0)


def test_timecode_only_caps_continuous_speech_at_thirty_seconds() -> None:
    source = _source("s1", duration_sec=600.0)
    words = [(f"слово{i}", 342.0 + i * 0.5, 342.0 + (i + 1) * 0.5) for i in range(100)]
    result = SyncMatcher([source], [_words(*words)]).match(_request())
    assert result.metadata["speech_out_sec"] == pytest.approx(372.0)


def test_timecode_only_ends_at_sentence_punctuation() -> None:
    source = _source("s1", duration_sec=600.0)
    index = _words(("Раз", 342.0, 342.4), ("два.", 342.4, 342.8), ("Три", 342.8, 343.2))
    result = SyncMatcher([source], [index]).match(_request())
    assert result.metadata["speech_out_sec"] == pytest.approx(342.8)


def test_timecode_only_uses_display_mode_and_guide_offset() -> None:
    source = _source(
        "s1", duration_sec=7200.0, sequence_start_tc="01:00:00:00", guide_offset_sec=10.0
    )
    index = _phrase_index(start=20.0)
    request = AlignmentRequest("b9", "01:00:30", SPEAKER, "01:00:30", timecode_mode="display")
    result = SyncMatcher([source], [index]).match(request)
    # 30 с по секвенции -> слова гида с 20 с лежат на 30 с секвенции (offset 10 с).
    assert result.status == "needs_review"
    assert result.metadata["speech_in_sec"] == pytest.approx(30.0)
    assert result.source_in_sec == pytest.approx(28.0)


def test_timecode_beyond_source_duration_stays_not_found() -> None:
    source = _source("s1", duration_sec=300.0)
    result = SyncMatcher([source], [_phrase_index()]).match(_request())
    assert result.status == "not_found"
    assert result.metadata["unmatched_reason"] == "timecode_out_of_range"


def test_timecode_only_without_speaker_source_stays_not_found() -> None:
    source = _source("s1", speaker="Совсем Другой")
    result = SyncMatcher([source], [_phrase_index()]).match(_request())
    assert result.status == "not_found"
    assert result.metadata["unmatched_reason"] == "no_eligible_source"


def test_media_file_name_prefers_matching_source_over_speaker_order() -> None:
    other = _source("s1", duration_sec=600.0, metadata={"media_name": "ZOOM0001.WAV"})
    wanted = _source(
        "s2",
        duration_sec=600.0,
        metadata={"media_name": "x.mov", "audio_guide_name": "ZOOM0007_Tr1 (1).WAV"},
    )
    indexes = [_phrase_index("s1"), _phrase_index("s2", start=342.0)]
    request = _request(source_hint="ZOOM0007_Tr1 (1).WAV")
    result = SyncMatcher([other, wanted], indexes).match(request)
    assert result.source_id == "s2"
    assert result.status == "needs_review"
    assert result.metadata["review_reason"] == "timecode_only"


def test_media_file_name_matches_even_when_speaker_differs() -> None:
    wanted = _source(
        "s2",
        speaker="Другая запись",
        duration_sec=600.0,
        metadata={"media_name": "zoom0007_tr1 (1).wav"},
    )
    result = SyncMatcher([wanted], [_phrase_index("s2")]).match(
        _request(source_hint="ZOOM0007_Tr1 (1).WAV")
    )
    assert result.source_id == "s2"


def test_quote_with_text_is_not_treated_as_timecode_only() -> None:
    source = _source("s1", duration_sec=600.0)
    result = SyncMatcher([source], [_phrase_index()]).match(
        AlignmentRequest("b9", "Это важная мысль", SPEAKER, "00:05:42")
    )
    assert result.strategy != "timecode_only"


def _timecode_match():
    source = _source("s1", duration_sec=600.0)
    return SyncMatcher([source], [_phrase_index()]).match(_request())


def test_audio_verify_never_selects_nor_promotes_timecode_only() -> None:
    match = _timecode_match()
    assert phase.select_candidates([match], []) == []
    verdict = AudioVerdict("match", coverage=1.0, confidence=0.99, heard_text="x", model="m")
    phase.apply_verdict(match, verdict, now="2026-01-01T00:00:00Z")
    assert match.status == "needs_review"
    assert match.metadata["review_reason"] == "timecode_only"


def test_unit_source_hint_comes_from_block_source_description() -> None:
    import sync_match_phase

    unit = {"block": {"source_description": " ZOOM0007_Tr1 (1).WAV "}}
    assert sync_match_phase._unit_source_hint(unit) == "ZOOM0007_Tr1 (1).WAV"
    assert sync_match_phase._unit_source_hint({}) == ""
