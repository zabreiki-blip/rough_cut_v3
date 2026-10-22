"""Коррекция диапазона СНХ-совпадения по локальной расшифровке (кейс ПОПОВ, UIP_ATOM)."""

from __future__ import annotations

import json
from typing import Any

import pytest

import api
from stage2.sync_matcher import MatcherConfig, SyncMatcher
from stage2.sync_models import AlignmentRequest, SyncSource, WordIndex, WordTimestamp

PHRASE = (
    "доля халатности или доля вседозволенности в работе с этими боеприпасами "
    "могла привести к тому что почти сорок единиц было утрачено"
)
OTHER = (
    "то есть тайвань угрожал бы им при создании ядерного боеприпаса своего "
    "когда знали они уже бы технологию производства"
)
FILLER = "погода сегодня стояла удивительно тёплая море было спокойным а чайки кружили над берегом"


def _words(text: str, start: float, step: float = 0.4, duration: float | None = None):
    dur = step if duration is None else duration
    return [
        WordTimestamp(word, start + i * step, start + i * step + dur)
        for i, word in enumerate(text.split())
    ]


def _index(words: list[WordTimestamp], **metadata: Any) -> WordIndex:
    return WordIndex(source_id="s1", words=words, metadata=dict(metadata))


def _local(words: list[WordTimestamp]) -> WordIndex:
    return _index(words, timestamp_precision="word", index_kind="local_whisper")


def _source() -> SyncSource:
    return SyncSource(id="s1", speaker="Попов", premiere_sequence="SEQ")


def _request(approx: float = 269.0) -> AlignmentRequest:
    return AlignmentRequest(
        segment_id="seg", text=PHRASE, speaker="Попов", approximate_timecode=approx
    )


def _collapsed_duplicated_cloud() -> WordIndex:
    # Облако: текст продублирован, времена схлопнуты (20 слов за ~0.4 с) и сдвинуты.
    words = _words(PHRASE, 235.27, step=0.02) + _words(PHRASE, 235.8, step=0.02)
    return _index(words, timestamp_precision="retimed")


def test_collapsed_duplicated_cloud_adopts_local_range_and_is_auto() -> None:
    local = _local(_words(PHRASE, 268.1) + _words(FILLER, 276.5))
    request = _request()

    # Без локальной расшифровки: прежнее поведение, неверный диапазон -> проверка.
    plain = SyncMatcher([_source()], [_collapsed_duplicated_cloud()]).match(request)
    assert plain.status == "needs_review"
    assert plain.metadata["speech_in_sec"] == pytest.approx(235.27, abs=0.05)

    result = SyncMatcher([_source()], [_collapsed_duplicated_cloud()], local_indexes=[local]).match(
        request
    )

    n = len(PHRASE.split())
    assert result.metadata["range_source"] == "local_transcript"
    assert result.metadata["speech_in_sec"] == pytest.approx(268.1, abs=0.01)
    assert result.metadata["speech_out_sec"] == pytest.approx(268.1 + n * 0.4, abs=0.01)
    assert result.source_in_sec == pytest.approx(268.1 - 2.0, abs=0.01)
    assert result.source_out_sec == pytest.approx(268.1 + n * 0.4 + 1.5, abs=0.01)
    assert result.status == "auto_matched"
    assert "plausibility_flags" not in result.metadata
    assert result.metadata["timing_reliable"] is True
    assert result.metadata["timing_flags"] == []
    # аудит: исходный диапазон облака и сработавшие флаги сохранены
    assert result.metadata["cloud_speech_in_sec"] == pytest.approx(235.27, abs=0.05)
    assert result.metadata["cloud_source_in_sec"] == pytest.approx(235.27 - 2.0, abs=0.05)
    corrected = result.metadata["range_corrected_flags"]
    assert "collapsed_word_times" in corrected and "range_disagrees_with_local" in corrected
    # исходный облачный диапазон остаётся доступным среди альтернатив
    assert any(abs(alt.source_in_sec - (235.27 - 2.0)) < 0.1 for alt in result.alternatives)
    json.dumps(result.to_dict())  # сериализуется


def test_tail_truncated_cloud_adopts_full_local_range() -> None:
    head = " ".join(PHRASE.split()[:17])
    cloud = _index(_words(head, 277.3) + _words(FILLER, 284.5))
    local = _local(_words(PHRASE, 277.3))
    result = SyncMatcher([_source()], [cloud], local_indexes=[local]).match(_request(277.0))
    n = len(PHRASE.split())
    assert result.metadata["range_source"] == "local_transcript"
    assert "tail_truncated_vs_local" in result.metadata["range_corrected_flags"]
    assert result.metadata["speech_out_sec"] == pytest.approx(277.3 + n * 0.4, abs=0.01)
    assert result.source_out_sec == pytest.approx(277.3 + n * 0.4 + 1.5, abs=0.01)
    assert result.status == "auto_matched"


def test_head_truncated_cloud_adopts_full_local_range() -> None:
    tail = " ".join(PHRASE.split()[3:])
    cloud = _index(_words(tail, 101.2))
    local = _local(_words(PHRASE, 100.0))
    result = SyncMatcher([_source()], [cloud], local_indexes=[local]).match(_request(100.0))
    assert result.metadata["range_source"] == "local_transcript"
    assert "head_truncated_vs_local" in result.metadata["range_corrected_flags"]
    assert result.metadata["speech_in_sec"] == pytest.approx(100.0, abs=0.01)
    assert result.source_in_sec == pytest.approx(98.0, abs=0.01)


def test_range_disagreement_adopts_local_range() -> None:
    cloud = _index(_words(PHRASE, 100.0))
    local = _local(_words(PHRASE, 150.0))
    result = SyncMatcher([_source()], [cloud], local_indexes=[local]).match(_request(100.0))
    assert result.metadata["range_source"] == "local_transcript"
    assert result.metadata["speech_in_sec"] == pytest.approx(150.0, abs=0.01)
    assert result.metadata["cloud_speech_in_sec"] == pytest.approx(100.0, abs=0.01)


def test_ambiguous_local_candidates_keep_cloud_range_for_review() -> None:
    cloud = _index(_words(PHRASE, 100.0))
    # одна и та же фраза дважды в окне поиска локальной расшифровки
    local = _local(_words(PHRASE, 150.0) + _words(FILLER, 160.0) + _words(PHRASE, 200.0))
    result = SyncMatcher([_source()], [cloud], local_indexes=[local]).match(_request(100.0))
    assert result.status == "needs_review"
    assert "range_source" not in result.metadata
    assert result.metadata["speech_in_sec"] == pytest.approx(100.0, abs=0.01)
    assert "range_disagrees_with_local" in result.metadata["plausibility_flags"]
    assert "range_corrected_flags" not in result.metadata


def test_weak_local_evidence_keeps_cloud_range_for_review() -> None:
    cloud = _index(_words(PHRASE, 100.0))
    half = " ".join(PHRASE.split()[:9])
    local = _local(_words(half, 150.0) + _words(FILLER, 154.0))
    result = SyncMatcher([_source()], [cloud], local_indexes=[local]).match(_request(100.0))
    assert "range_source" not in result.metadata
    assert result.metadata["speech_in_sec"] == pytest.approx(100.0, abs=0.01)
    assert result.status == "needs_review"


def test_semantic_flags_of_local_text_block_auto() -> None:
    # локальный текст с изменённым числом: семантический шлюз не пускает в auto
    changed = PHRASE.replace("сорок", "четыреста")
    cloud = _index(_words(PHRASE, 100.0))
    local = _local(_words(changed, 150.0))
    result = SyncMatcher([_source()], [cloud], local_indexes=[local]).match(_request(100.0))
    assert result.status != "auto_matched"


def test_consistent_local_does_not_change_anything() -> None:
    cloud = _index(_words(PHRASE, 100.0), timestamp_precision="retimed")
    local = _local(_words(PHRASE, 100.1))
    request = _request(100.0)
    baseline = SyncMatcher([_source()], [cloud]).match(request)
    result = SyncMatcher([_source()], [cloud], local_indexes=[local]).match(request)
    assert result.to_dict() == baseline.to_dict()
    assert "range_source" not in result.metadata


def test_zero_max_alternatives_still_corrects_range() -> None:
    cloud = _index(_words(PHRASE, 100.0))
    local = _local(_words(PHRASE, 150.0))
    config = MatcherConfig(max_alternatives=0)
    result = SyncMatcher([_source()], [cloud], config, local_indexes=[local]).match(_request(100.0))
    assert result.metadata["range_source"] == "local_transcript"
    assert result.alternatives == []


def test_matching_fingerprint_version_is_bumped() -> None:
    assert api._SYNC_MATCHING_FINGERPRINT_VERSION == 6
