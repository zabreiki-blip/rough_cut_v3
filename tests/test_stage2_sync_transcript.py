from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from stage2.sync_models import SyncSource, WordIndex
from stage2.sync_transcript import WordIndexGenerator, ingest_word_index, load_word_index


def test_ingests_whisperx_nested_word_timestamps() -> None:
    payload = {
        "language": "ru",
        "segments": [
            {
                "words": [
                    {"word": " Привет,", "start": 0.2, "end": 0.6, "score": 0.98},
                    {"word": "мир", "start": 0.7, "end": 1.0, "score": 0.96},
                ]
            }
        ],
    }
    index = ingest_word_index(payload, "speaker_01", model="large-v3")

    assert index.source_id == "speaker_01"
    assert index.model == "large-v3"
    assert [item.word for item in index.words] == ["Привет", "мир"]
    assert index.words[0].confidence == pytest.approx(0.98)
    assert index.metadata["timestamp_precision"] == "word"


def test_ingests_openai_top_level_words_and_infers_source() -> None:
    index = ingest_word_index(
        {
            "source_id": "s1",
            "backend": "mlx-whisper",
            "created_at": "2026-07-24T10:00:00Z",
            "backend_metadata": {"detected_language": "ru"},
            "words": [
                {"word": "раз", "start": 1, "end": 1.2},
                {"word": "два", "start": 1.3, "end": 1.6},
            ],
        }
    )
    assert index.source_id == "s1"
    assert index.duration_sec == pytest.approx(1.6)
    assert index.generated_at == "2026-07-24T10:00:00Z"
    assert index.metadata["backend"] == "mlx-whisper"


def test_hf_chunks_are_tokenized_and_interval_is_interpolated() -> None:
    index = ingest_word_index(
        {
            "chunks": [
                {"text": "энергетическим вампиром", "timestamp": [10.0, 12.0]}
            ]
        },
        "s1",
    )
    assert [item.word for item in index.words] == ["энергетическим", "вампиром"]
    assert index.words[0].start == 10.0
    assert index.words[0].end == 11.0
    assert index.words[1].end == 12.0
    assert index.metadata["timestamp_precision"] == "interpolated"


def test_invalid_entries_are_skipped_but_empty_transcript_is_rejected() -> None:
    with pytest.raises(ValueError, match="no valid timed words"):
        ingest_word_index(
            {"words": [{"word": "", "start": 0, "end": 1}, {"word": "назад", "start": 2, "end": 1}]},
            "s1",
        )


def test_non_finite_timestamps_are_skipped_without_poisoning_valid_words() -> None:
    index = ingest_word_index(
        {
            "words": [
                {"word": "сломано", "start": float("nan"), "end": 1},
                {"word": "готово", "start": 2, "end": 2.5},
            ]
        },
        "s1",
    )
    assert [item.word for item in index.words] == ["готово"]


def test_load_word_index_supports_json_file() -> None:
    with TemporaryDirectory() as temporary:
        path = Path(temporary) / "words.json"
        path.write_text(
            json.dumps([{"word": "слово", "start": 0.0, "end": 0.4}], ensure_ascii=False),
            encoding="utf-8",
        )
        index = load_word_index(path, "source")
        assert index.words[0].word == "слово"


def test_generator_protocol_decouples_alignment_from_whisper_invocation() -> None:
    class FakeGenerator:
        def generate(self, source: SyncSource) -> WordIndex:
            return WordIndex(source_id=source.id)

    assert isinstance(FakeGenerator(), WordIndexGenerator)
