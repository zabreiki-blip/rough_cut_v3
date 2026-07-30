from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from stage2.whisper_runner import (
    _MODEL_CACHE,
    _cached_model,
    WhisperUnavailableError,
    _default_model,
    _transcribe_faster,
    _words_from_mapping,
    resolve_backend,
)


class WhisperRunnerTest(unittest.TestCase):
    def test_flattens_and_sorts_word_timestamps(self) -> None:
        result = {
            "segments": [
                {"words": [
                    {"word": " второе", "start": 1.2, "end": 1.8},
                    {"word": "первое", "start": 0.2, "end": 0.8},
                ]},
            ],
        }

        self.assertEqual(
            _words_from_mapping(result),
            [
                {"word": "первое", "start": 0.2, "end": 0.8},
                {"word": "второе", "start": 1.2, "end": 1.8},
            ],
        )

    def test_invalid_words_are_ignored(self) -> None:
        result = {
            "words": [
                {"word": "", "start": 0, "end": 1},
                {"word": "назад", "start": 2, "end": 1},
                {"word": "нечисло", "start": float("nan"), "end": 2},
                {"word": "готово", "start": "3.25", "end": "3.75"},
            ],
        }

        self.assertEqual(
            _words_from_mapping(result),
            [{"word": "готово", "start": 3.25, "end": 3.75}],
        )

    @patch("stage2.whisper_runner.available_backends", return_value=["faster-whisper"])
    def test_auto_selects_available_backend(self, _available) -> None:
        self.assertEqual(resolve_backend("auto"), "faster-whisper")

    @patch("stage2.whisper_runner.available_backends", return_value=[])
    def test_requested_missing_backend_is_explicit(self, _available) -> None:
        with self.assertRaises(WhisperUnavailableError):
            resolve_backend("mlx-whisper")

    def test_unknown_backend_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            resolve_backend("mystery")

    @patch.dict("os.environ", {"SYNC_WHISPER_MODEL": "medium"})
    def test_mlx_model_alias_maps_to_hugging_face_repo(self) -> None:
        self.assertEqual(
            _default_model("mlx-whisper"),
            "mlx-community/whisper-medium-mlx",
        )

    def test_heavy_models_are_cached_by_backend_configuration(self) -> None:
        key = ("test-backend", "test-model")
        _MODEL_CACHE.pop(key, None)
        loaded = []

        first, _first_lock = _cached_model(key, lambda: loaded.append(1) or object())
        second, _second_lock = _cached_model(key, lambda: loaded.append(2) or object())

        self.assertIs(first, second)
        self.assertEqual(loaded, [1])
        _MODEL_CACHE.pop(key, None)

    def test_faster_whisper_reports_streaming_audio_progress(self) -> None:
        class FakeModel:
            def __init__(self, *_args, **_kwargs) -> None:
                pass

            def transcribe(self, *_args, **_kwargs):
                segments = iter([
                    SimpleNamespace(
                        end=5.0,
                        words=[SimpleNamespace(word=" один", start=1.0, end=1.5)],
                    ),
                    SimpleNamespace(
                        end=10.0,
                        words=[SimpleNamespace(word="два", start=9.0, end=9.5)],
                    ),
                ])
                return segments, SimpleNamespace(
                    duration=10.0,
                    language="ru",
                    language_probability=0.99,
                )

        progress = []
        fake_module = SimpleNamespace(WhisperModel=FakeModel)
        with patch.dict("sys.modules", {"faster_whisper": fake_module}):
            words, metadata = _transcribe_faster(
                Path("audio.wav"),
                "progress-test-model",
                "ru",
                lambda processed, duration, count: progress.append(
                    (processed, duration, count)
                ),
            )

        self.assertEqual(progress, [(0.0, 10.0, 0), (5.0, 10.0, 1), (10.0, 10.0, 2)])
        self.assertEqual(len(words), 2)
        self.assertEqual(metadata["duration_sec"], 10.0)


if __name__ == "__main__":
    unittest.main()
