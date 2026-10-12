from __future__ import annotations

import dataclasses

import pytest

from stage2.asr_contract import AsrProgress, AsrRunResult, AsrSegment, AsrWord


def _word(**overrides: object) -> AsrWord:
    values: dict[str, object] = {"word": "привет", "start": 0.0, "end": 0.5}
    values.update(overrides)
    return AsrWord(**values)  # type: ignore[arg-type]


def test_asr_progress_optional_fields_default_to_none() -> None:
    progress = AsrProgress(done=1, total=4, message="Распознавание")

    assert (progress.done, progress.total, progress.message) == (1, 4, "Распознавание")
    assert progress.processed_sec is None
    assert progress.duration_sec is None
    assert progress.word_count is None


def test_asr_progress_is_mutable_and_accepts_audio_position_fields() -> None:
    progress = AsrProgress(done=0, total=0, message="", processed_sec=1.5, duration_sec=10.0)
    progress.done = 3
    progress.word_count = 42

    assert progress.done == 3
    assert progress.word_count == 42
    assert progress.processed_sec == 1.5
    assert progress.duration_sec == 10.0


def test_asr_word_defaults_and_equality() -> None:
    word = _word()

    assert word.probability is None
    assert word == _word()
    assert word != _word(probability=0.9)
    assert hash(word) == hash(_word())


def test_asr_word_is_frozen() -> None:
    word = _word()

    with pytest.raises(dataclasses.FrozenInstanceError):
        word.word = "другое"  # type: ignore[misc]


def test_asr_segment_defaults_for_adapters_without_diarization() -> None:
    segment = AsrSegment(text="Привет", start=0.0, end=1.0, words=[_word()])

    assert segment.speaker is None
    assert segment.has_explicit_end is False
    assert segment.words == [_word()]


def test_asr_segment_accepts_speaker_and_explicit_end() -> None:
    segment = AsrSegment(
        text="Ёлка",
        start=1.0,
        end=2.0,
        words=[],
        speaker="ГОЛОС 1",
        has_explicit_end=True,
    )

    assert segment.speaker == "ГОЛОС 1"
    assert segment.has_explicit_end is True
    assert segment.words == []


def test_asr_segment_is_frozen() -> None:
    segment = AsrSegment(text="a", start=0.0, end=1.0, words=[])

    with pytest.raises(dataclasses.FrozenInstanceError):
        segment.end = 2.0  # type: ignore[misc]


def _run_result(**overrides: object) -> AsrRunResult:
    values: dict[str, object] = {
        "words": [],
        "segments": [],
        "language": "ru",
        "duration_sec": 12.5,
        "backend": "cloud",
        "model": "m",
        "device": "cpu",
        "compute_type": "int8",
        "params": {"beam": 5},
        "word_count": 0,
    }
    values.update(overrides)
    return AsrRunResult(**values)  # type: ignore[arg-type]


def test_asr_run_result_defaults() -> None:
    result = _run_result()

    assert result.speakers == []
    assert result.cost_usd is None
    assert result.params == {"beam": 5}


def test_asr_run_result_speakers_default_is_not_shared_between_instances() -> None:
    first = _run_result()
    second = _run_result()
    first.speakers.append("ГОЛОС 1")

    assert second.speakers == []


def test_asr_run_result_requires_core_fields() -> None:
    with pytest.raises(TypeError):
        AsrRunResult(words=[], segments=[])  # type: ignore[call-arg]


def test_asr_run_result_carries_explicit_speakers_and_cost() -> None:
    result = _run_result(speakers=["ГОЛОС 1", "ГОЛОС 2"], cost_usd=0.0123)

    assert result.speakers == ["ГОЛОС 1", "ГОЛОС 2"]
    assert result.cost_usd == pytest.approx(0.0123)
