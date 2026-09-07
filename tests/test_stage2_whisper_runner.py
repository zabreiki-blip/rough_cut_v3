from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from stage2.cloud_asr import CloudAudioAsrError
from stage2.whisper_runner import (
    _MODEL_CACHE,
    _cached_model,
    WhisperTranscriptionError,
    WhisperUnavailableError,
    _default_model,
    _transcribe_cloud,
    _transcribe_faster,
    _words_from_mapping,
    resolve_backend,
    transcribe_audio,
)


class WhisperRunnerTest(unittest.TestCase):
    def test_flattens_and_sorts_word_timestamps(self) -> None:
        result = {
            "segments": [
                {
                    "words": [
                        {"word": " второе", "start": 1.2, "end": 1.8},
                        {"word": "первое", "start": 0.2, "end": 0.8},
                    ]
                },
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
                segments = iter(
                    [
                        SimpleNamespace(
                            end=5.0,
                            words=[SimpleNamespace(word=" один", start=1.0, end=1.5)],
                        ),
                        SimpleNamespace(
                            end=10.0,
                            words=[SimpleNamespace(word="два", start=9.0, end=9.5)],
                        ),
                    ]
                )
                return segments, SimpleNamespace(
                    duration=10.0,
                    language="ru",
                    language_probability=0.99,
                )

        progress = []
        fake_module = SimpleNamespace(
            BatchedInferencePipeline=object,
            WhisperModel=FakeModel,
        )
        with (
            patch.dict("sys.modules", {"faster_whisper": fake_module}),
            patch.dict("os.environ", {}, clear=True),
        ):
            words, metadata = _transcribe_faster(
                Path("audio.wav"),
                "progress-test-model",
                "ru",
                lambda processed, duration, count: progress.append((processed, duration, count)),
            )

        self.assertEqual(progress, [(0.0, 10.0, 0), (5.0, 10.0, 1), (10.0, 10.0, 2)])
        self.assertEqual(len(words), 2)
        self.assertEqual(metadata["duration_sec"], 10.0)

    def test_faster_whisper_default_decoding_kwargs_preserve_behavior(self) -> None:
        calls = {}

        class FakeModel:
            def __init__(self, *args, **kwargs) -> None:
                calls["init"] = (args, kwargs)

            def transcribe(self, *args, **kwargs):
                calls["transcribe"] = (args, kwargs)
                return iter(
                    [
                        SimpleNamespace(
                            end=2.0,
                            words=[SimpleNamespace(word="тест", start=1.0, end=2.0)],
                        ),
                    ]
                ), SimpleNamespace(
                    duration=2.0,
                    language="ru",
                    language_probability=0.98,
                )

        class UnexpectedBatchPipeline:
            def __init__(self, _model) -> None:
                raise AssertionError("batch pipeline must be disabled by default")

        fake_module = SimpleNamespace(
            BatchedInferencePipeline=UnexpectedBatchPipeline,
            WhisperModel=FakeModel,
        )
        with (
            patch.dict("sys.modules", {"faster_whisper": fake_module}),
            patch.dict("os.environ", {}, clear=True),
        ):
            _words, metadata = _transcribe_faster(
                Path("audio.wav"),
                "default-kwargs-test-model",
                "ru",
            )

        self.assertEqual(
            calls["init"],
            (
                ("default-kwargs-test-model",),
                {"device": "auto", "compute_type": "default"},
            ),
        )
        self.assertEqual(calls["transcribe"][0], ("audio.wav",))
        self.assertEqual(
            calls["transcribe"][1],
            {
                "language": "ru",
                "word_timestamps": True,
                "vad_filter": True,
                "beam_size": 5,
                "best_of": 5,
            },
        )
        self.assertEqual(metadata["device"], "auto")
        self.assertEqual(metadata["compute_type"], "default")
        self.assertEqual(metadata["beam_size"], 5)
        self.assertEqual(metadata["best_of"], 5)
        self.assertEqual(metadata["batch_size"], 0)
        self.assertEqual(metadata["cpu_threads"], 0)
        self.assertFalse(metadata["batched"])

    def test_faster_whisper_uses_configured_batch_pipeline(self) -> None:
        calls = {}

        class FakeModel:
            def __init__(self, *args, **kwargs) -> None:
                calls["model"] = self
                calls["init"] = (args, kwargs)

            def transcribe(self, *_args, **_kwargs):
                raise AssertionError("model.transcribe must not be used in batch mode")

        class FakeBatchPipeline:
            def __init__(self, model) -> None:
                calls["pipeline_model"] = model

            def transcribe(self, *args, **kwargs):
                calls["transcribe"] = (args, kwargs)
                return iter(
                    [
                        SimpleNamespace(
                            end=3.0,
                            words=[SimpleNamespace(word="батч", start=2.0, end=3.0)],
                        ),
                    ]
                ), SimpleNamespace(
                    duration=3.0,
                    language="ru",
                    language_probability=0.97,
                )

        fake_module = SimpleNamespace(
            BatchedInferencePipeline=FakeBatchPipeline,
            WhisperModel=FakeModel,
        )
        environment = {
            "SYNC_WHISPER_DEVICE": "cpu",
            "SYNC_WHISPER_COMPUTE_TYPE": "int8",
            "SYNC_WHISPER_BEAM_SIZE": "1",
            "SYNC_WHISPER_BEST_OF": "2",
            "SYNC_WHISPER_BATCH_SIZE": "8",
            "SYNC_WHISPER_CPU_THREADS": "6",
        }
        with (
            patch.dict("sys.modules", {"faster_whisper": fake_module}),
            patch.dict("os.environ", environment, clear=True),
        ):
            _words, metadata = _transcribe_faster(
                Path("audio.wav"),
                "batch-kwargs-test-model",
                "ru",
            )

        self.assertIs(calls["pipeline_model"], calls["model"])
        self.assertEqual(
            calls["init"],
            (
                ("batch-kwargs-test-model",),
                {"device": "cpu", "compute_type": "int8", "cpu_threads": 6},
            ),
        )
        self.assertEqual(
            calls["transcribe"][1],
            {
                "language": "ru",
                "word_timestamps": True,
                "vad_filter": True,
                "beam_size": 1,
                "best_of": 2,
                "batch_size": 8,
            },
        )
        self.assertEqual(metadata["device"], "cpu")
        self.assertEqual(metadata["compute_type"], "int8")
        self.assertEqual(metadata["beam_size"], 1)
        self.assertEqual(metadata["best_of"], 2)
        self.assertEqual(metadata["batch_size"], 8)
        self.assertEqual(metadata["cpu_threads"], 6)
        self.assertTrue(metadata["batched"])

    def test_faster_whisper_uses_fast_cpu_defaults(self) -> None:
        calls = {}

        class FakeModel:
            def __init__(self, *args, **kwargs) -> None:
                calls["init"] = (args, kwargs)

            def transcribe(self, *_args, **kwargs):
                calls["transcribe"] = kwargs
                return iter(
                    [
                        SimpleNamespace(
                            end=1.0,
                            words=[SimpleNamespace(word="тест", start=0.0, end=1.0)],
                        ),
                    ]
                ), SimpleNamespace(
                    duration=1.0,
                    language="ru",
                    language_probability=0.99,
                )

        fake_module = SimpleNamespace(
            BatchedInferencePipeline=object,
            WhisperModel=FakeModel,
        )
        with (
            patch.dict("sys.modules", {"faster_whisper": fake_module}),
            patch.dict("os.environ", {"SYNC_WHISPER_DEVICE": "cpu"}, clear=True),
        ):
            _words, metadata = _transcribe_faster(
                Path("audio.wav"),
                "cpu-defaults-test-model",
                "ru",
            )

        self.assertEqual(
            calls["init"],
            (("cpu-defaults-test-model",), {"device": "cpu", "compute_type": "int8"}),
        )
        self.assertEqual(calls["transcribe"]["beam_size"], 1)
        self.assertEqual(metadata["compute_type"], "int8")
        self.assertEqual(metadata["beam_size"], 1)

    def test_transcription_duration_prefers_backend_metadata(self) -> None:
        with TemporaryDirectory() as directory:
            audio_path = Path(directory) / "audio.wav"
            audio_path.touch()
            with (
                patch(
                    "stage2.whisper_runner.resolve_backend",
                    return_value="faster-whisper",
                ),
                patch(
                    "stage2.whisper_runner._transcribe_faster",
                    return_value=(
                        [{"word": "тест", "start": 1.0, "end": 2.0}],
                        {"duration_sec": 10.0},
                    ),
                ),
            ):
                result = transcribe_audio(
                    audio_path,
                    backend="faster-whisper",
                    model="medium",
                )

        self.assertEqual(result["duration_sec"], 10.0)

    @patch(
        "stage2.whisper_runner.available_backends", return_value=["faster-whisper", "mlx-whisper"]
    )
    def test_resolve_backend_accepts_explicit_cloud_audio(self, _available) -> None:
        self.assertEqual(resolve_backend("cloud-audio"), "cloud-audio")
        self.assertEqual(resolve_backend("cloud"), "cloud-audio")
        self.assertEqual(resolve_backend("openrouter"), "cloud-audio")

    @patch(
        "stage2.whisper_runner.available_backends", return_value=["faster-whisper", "mlx-whisper"]
    )
    def test_resolve_backend_auto_never_selects_cloud_audio(self, _available) -> None:
        # cloud-audio is paid and must be opted into explicitly; auto must
        # keep choosing a local backend even if cloud-audio were somehow
        # reported as "available".
        self.assertIn(resolve_backend("auto"), ("faster-whisper", "mlx-whisper"))
        self.assertEqual(resolve_backend(None), resolve_backend("auto"))

    def test_transcribe_cloud_interpolates_words_and_preserves_metadata(self) -> None:
        cloud_result = {
            "segments": [
                {
                    "text": "раз два три четыре",
                    "start": 0.0,
                    "end": 4.0,
                    "speaker": "ГОЛОС 1",
                    "has_explicit_end": True,
                },
            ],
            "speakers": ["ГОЛОС 1"],
            "language": "ru",
            "model": "google/gemini-3.7-flash",
            "backend": "cloud-audio",
            "duration_sec": 4.0,
            "cost_usd": 0.0123,
        }
        with TemporaryDirectory() as directory:
            audio_path = Path(directory) / "audio.wav"
            audio_path.touch()
            with patch(
                "stage2.whisper_runner.transcribe_cloud",
                return_value=cloud_result,
            ):
                result = transcribe_audio(
                    audio_path,
                    backend="cloud-audio",
                    model="google/gemini-3.7-flash",
                )

        self.assertEqual(result["backend"], "cloud-audio")
        self.assertEqual(result["word_count"], 4)
        words = result["words"]
        self.assertEqual([w["word"] for w in words], ["раз", "два", "три", "четыре"])
        # Words must strictly increase in time and stay inside the segment.
        starts = [w["start"] for w in words]
        self.assertEqual(starts, sorted(starts))
        self.assertEqual(len(set(starts)), len(starts))
        self.assertGreaterEqual(words[0]["start"], 0.0)
        self.assertLessEqual(words[-1]["end"], 4.0)
        for earlier, later in zip(words, words[1:]):
            self.assertLessEqual(earlier["end"], later["start"])

        self.assertEqual(result["segments"], cloud_result["segments"])
        meta = result["backend_metadata"]
        self.assertEqual(meta["speakers"], ["ГОЛОС 1"])
        self.assertEqual(meta["cost_usd"], 0.0123)
        self.assertEqual(meta["timestamp_precision"], "interpolated")
        self.assertNotIn("segments", meta)
        # Every interpolated word inherits its source segment's diarization label.
        self.assertTrue(all(w["speaker"] == "ГОЛОС 1" for w in words))

    def test_transcribe_cloud_leaves_speaker_empty_for_unlabelled_segments(self) -> None:
        cloud_result = {
            "segments": [
                {"text": "без метки", "start": 0.0, "end": 1.0, "speaker": None},
            ],
            "speakers": [],
            "language": "ru",
            "model": "google/gemini-3.7-flash",
            "backend": "cloud-audio",
            "duration_sec": 1.0,
            "cost_usd": 0.0,
        }
        with TemporaryDirectory() as directory:
            audio_path = Path(directory) / "audio.wav"
            audio_path.touch()
            with patch(
                "stage2.whisper_runner.transcribe_cloud",
                return_value=cloud_result,
            ):
                result = transcribe_audio(
                    audio_path,
                    backend="cloud-audio",
                    model="google/gemini-3.7-flash",
                )

        for word in result["words"]:
            self.assertNotIn("speaker", word)

    def test_transcribe_cloud_ignores_local_model_name_and_uses_cloud_default(
        self,
    ) -> None:
        cloud_result = {
            "segments": [
                {"text": "раз", "start": 0.0, "end": 1.0, "has_explicit_end": True},
            ],
            "language": "ru",
            "model": "google/gemini-3.7-flash",
            "backend": "cloud-audio",
            "duration_sec": 1.0,
        }
        with TemporaryDirectory() as directory:
            audio_path = Path(directory) / "audio.wav"
            audio_path.touch()
            with patch(
                "stage2.whisper_runner.transcribe_cloud",
                return_value=cloud_result,
            ) as mocked:
                transcribe_audio(
                    audio_path,
                    backend="cloud-audio",
                    # A locally-named Whisper model (as sent by a stale UI
                    # selection) must never be forwarded to the cloud
                    # chat-ASR backend, which needs a "provider/model" id.
                    model="small",
                )

        used_model = mocked.call_args.kwargs.get("model", mocked.call_args.args[1:2])
        # transcribe_cloud is invoked with the resolved cloud default model,
        # not the local "small" model name.
        self.assertNotEqual(used_model, "small")
        self.assertEqual(used_model, _default_model("cloud-audio"))

    def test_transcribe_cloud_preserves_explicit_cloud_model_id(self) -> None:
        cloud_result = {
            "segments": [
                {"text": "раз", "start": 0.0, "end": 1.0, "has_explicit_end": True},
            ],
            "language": "ru",
            "model": "google/gemini-3.7-flash",
            "backend": "cloud-audio",
            "duration_sec": 1.0,
        }
        with TemporaryDirectory() as directory:
            audio_path = Path(directory) / "audio.wav"
            audio_path.touch()
            with patch(
                "stage2.whisper_runner.transcribe_cloud",
                return_value=cloud_result,
            ) as mocked:
                result = transcribe_audio(
                    audio_path,
                    backend="cloud-audio",
                    model="google/gemini-3.7-flash",
                )

        used_model = mocked.call_args.kwargs.get("model", mocked.call_args.args[1:2])
        self.assertEqual(used_model, "google/gemini-3.7-flash")
        self.assertEqual(result["model"], "google/gemini-3.7-flash")

    @patch("stage2.whisper_runner.available_backends", return_value=["openai-whisper"])
    def test_local_backend_keeps_explicit_local_model_name(self, _available) -> None:
        fake_result = {"language": "ru"}
        with (
            TemporaryDirectory() as directory,
            patch(
                "stage2.whisper_runner._transcribe_openai",
                return_value=(
                    [{"word": "раз", "start": 0.0, "end": 1.0}],
                    fake_result,
                ),
            ) as mocked,
        ):
            audio_path = Path(directory) / "audio.wav"
            audio_path.touch()
            result = transcribe_audio(
                audio_path,
                backend="openai-whisper",
                model="small",
            )

        mocked.assert_called_once_with(
            audio_path.resolve(), "small", "ru"
        )
        self.assertEqual(result["model"], "small")

    def test_transcribe_cloud_missing_api_key_raises_clear_error(self) -> None:
        with (
            TemporaryDirectory() as directory,
            patch.dict("os.environ", {}, clear=True),
        ):
            audio_path = Path(directory) / "audio.wav"
            audio_path.touch()
            with self.assertRaises(WhisperTranscriptionError) as ctx:
                _transcribe_cloud(audio_path, "google/gemini-3.7-flash", "ru", None)

        message = str(ctx.exception)
        self.assertIn("Облачное распознавание", message)
        self.assertNotIn("Bearer", message)

    def test_transcribe_cloud_wraps_provider_errors(self) -> None:
        with (
            TemporaryDirectory() as directory,
            patch(
                "stage2.whisper_runner.transcribe_cloud",
                side_effect=CloudAudioAsrError("сбой сети"),
            ),
        ):
            audio_path = Path(directory) / "audio.wav"
            audio_path.touch()
            with self.assertRaises(WhisperTranscriptionError):
                _transcribe_cloud(audio_path, "google/gemini-3.7-flash", "ru", None)

    def test_faster_whisper_rejects_invalid_integer_settings(self) -> None:
        fake_module = SimpleNamespace(
            BatchedInferencePipeline=object,
            WhisperModel=object,
        )
        invalid_settings = (
            ("SYNC_WHISPER_BEAM_SIZE", "0"),
            ("SYNC_WHISPER_BEST_OF", "-1"),
            ("SYNC_WHISPER_BATCH_SIZE", "-1"),
            ("SYNC_WHISPER_BATCH_SIZE", "1.5"),
            ("SYNC_WHISPER_CPU_THREADS", "-1"),
            ("SYNC_WHISPER_CPU_THREADS", "1.5"),
        )
        for name, value in invalid_settings:
            with self.subTest(name=name, value=value):
                with (
                    patch.dict("sys.modules", {"faster_whisper": fake_module}),
                    patch.dict("os.environ", {name: value}, clear=True),
                ):
                    with self.assertRaisesRegex(ValueError, name):
                        _transcribe_faster(
                            Path("audio.wav"),
                            "invalid-env-test-model",
                            "ru",
                        )


if __name__ == "__main__":
    unittest.main()
