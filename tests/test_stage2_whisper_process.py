from __future__ import annotations

import multiprocessing
import time
from unittest.mock import patch

import pytest

from stage2.whisper_process import (
    WhisperProcessError,
    WhisperTimeoutError,
    transcribe_audio_in_process,
)


pytestmark = pytest.mark.skipif(
    "fork" not in multiprocessing.get_all_start_methods(),
    reason="process-isolation tests require fork for inherited test doubles",
)


def test_spawn_worker_reports_real_preflight_error(tmp_path) -> None:
    missing = tmp_path / "missing.wav"

    with pytest.raises(WhisperProcessError, match="FileNotFoundError"):
        transcribe_audio_in_process(
            missing,
            timeout_seconds=5.0,
            _start_method="spawn",
        )


def test_process_forwards_progress_and_returns_result() -> None:
    def fake_transcribe(_path, *, progress_callback, **_kwargs):
        progress_callback(5.0, 10.0, 42)
        return {"words": [{"word": "тест", "start": 0.0, "end": 1.0}]}

    progress = []
    with patch("stage2.whisper_process.transcribe_audio", side_effect=fake_transcribe):
        result = transcribe_audio_in_process(
            "unused.wav",
            timeout_seconds=2.0,
            _start_method="fork",
            progress_callback=lambda done, total, words: progress.append(
                (done, total, words)
            ),
        )

    assert progress == [(5.0, 10.0, 42)]
    assert result["words"][0]["word"] == "тест"


def test_process_propagates_worker_error() -> None:
    def fail_transcription(_path, **_kwargs):
        raise RuntimeError("model failed")

    with (
        patch("stage2.whisper_process.transcribe_audio", side_effect=fail_transcription),
        pytest.raises(WhisperProcessError, match="RuntimeError: model failed"),
    ):
        transcribe_audio_in_process(
            "unused.wav",
            timeout_seconds=2.0,
            _start_method="fork",
        )


def test_timeout_terminates_worker_promptly() -> None:
    def hang(_path, **_kwargs):
        time.sleep(5.0)
        return {"words": []}

    started = time.monotonic()
    with (
        patch("stage2.whisper_process.transcribe_audio", side_effect=hang),
        pytest.raises(WhisperTimeoutError, match="exceeded"),
    ):
        transcribe_audio_in_process(
            "unused.wav",
            timeout_seconds=0.1,
            _start_method="fork",
        )

    assert time.monotonic() - started < 2.0
