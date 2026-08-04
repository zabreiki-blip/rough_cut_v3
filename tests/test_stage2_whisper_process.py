from __future__ import annotations

import multiprocessing
import os
import threading
import time
from unittest.mock import patch

import pytest

from stage2.whisper_process import (
    _shutdown_worker,
    WhisperCancelledError,
    WhisperProcessError,
    WhisperTimeoutError,
    transcribe_audio_in_process,
)


pytestmark = pytest.mark.skipif(
    "fork" not in multiprocessing.get_all_start_methods(),
    reason="process-isolation tests require fork for inherited test doubles",
)


@pytest.fixture(autouse=True)
def clean_up_cached_worker():
    _shutdown_worker()
    yield
    _shutdown_worker()


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


def test_process_is_reused_for_matching_configuration() -> None:
    def fake_transcribe(path, **_kwargs):
        return {"path": path, "worker_pid": os.getpid()}

    with (
        patch("stage2.whisper_process.transcribe_audio", side_effect=fake_transcribe),
        patch.dict("os.environ", {}, clear=True),
    ):
        first = transcribe_audio_in_process(
            "first.wav", timeout_seconds=2.0, _start_method="fork"
        )
        second = transcribe_audio_in_process(
            "second.wav", timeout_seconds=2.0, _start_method="fork"
        )

    assert first["worker_pid"] == second["worker_pid"]
    assert first["path"].endswith("first.wav")
    assert second["path"].endswith("second.wav")


def test_process_restarts_when_inference_configuration_changes() -> None:
    def fake_transcribe(_path, **_kwargs):
        return {"worker_pid": os.getpid()}

    with (
        patch("stage2.whisper_process.transcribe_audio", side_effect=fake_transcribe),
        patch.dict("os.environ", {}, clear=True),
    ):
        first = transcribe_audio_in_process(
            "first.wav", timeout_seconds=2.0, _start_method="fork"
        )
        with patch.dict(
            "os.environ", {"SYNC_WHISPER_CPU_THREADS": "8"}, clear=False
        ):
            second = transcribe_audio_in_process(
                "second.wav", timeout_seconds=2.0, _start_method="fork"
            )

    assert first["worker_pid"] != second["worker_pid"]


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


def test_worker_restarts_after_error() -> None:
    def fail_transcription(_path, **_kwargs):
        raise RuntimeError(f"model failed in {os.getpid()}")

    with patch("stage2.whisper_process.transcribe_audio", side_effect=fail_transcription):
        with pytest.raises(WhisperProcessError, match="RuntimeError: model failed") as exc:
            transcribe_audio_in_process(
                "unused.wav", timeout_seconds=2.0, _start_method="fork"
            )
    failed_worker_pid = int(str(exc.value).rsplit(" ", 1)[1])

    def succeed(_path, **_kwargs):
        return {"worker_pid": os.getpid()}

    with patch("stage2.whisper_process.transcribe_audio", side_effect=succeed):
        result = transcribe_audio_in_process(
            "unused.wav", timeout_seconds=2.0, _start_method="fork"
        )

    assert result["worker_pid"] != failed_worker_pid


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


def test_cancel_event_terminates_worker_promptly() -> None:
    def hang(_path, *, progress_callback, **_kwargs):
        progress_callback(0.0, 1.0, 0)
        time.sleep(5.0)
        return {"words": []}

    cancel_event = threading.Event()
    started = time.monotonic()
    with (
        patch("stage2.whisper_process.transcribe_audio", side_effect=hang),
        pytest.raises(WhisperCancelledError, match="cancelled"),
    ):
        transcribe_audio_in_process(
            "unused.wav",
            cancel_event=cancel_event,
            timeout_seconds=10.0,
            _start_method="fork",
            progress_callback=lambda *_args: cancel_event.set(),
        )

    assert cancel_event.is_set()
    assert time.monotonic() - started < 2.0


def test_worker_restarts_after_timeout() -> None:
    def hang(_path, *, progress_callback, **_kwargs):
        progress_callback(0.0, 1.0, os.getpid())
        time.sleep(5.0)
        return {"words": []}

    timed_out_worker_pids = []
    with patch("stage2.whisper_process.transcribe_audio", side_effect=hang):
        with pytest.raises(WhisperTimeoutError, match="exceeded"):
            transcribe_audio_in_process(
                "unused.wav",
                timeout_seconds=0.1,
                _start_method="fork",
                progress_callback=lambda _done, _total, pid: timed_out_worker_pids.append(
                    pid
                ),
            )

    def succeed(_path, **_kwargs):
        return {"worker_pid": os.getpid()}

    with patch("stage2.whisper_process.transcribe_audio", side_effect=succeed):
        result = transcribe_audio_in_process(
            "unused.wav", timeout_seconds=2.0, _start_method="fork"
        )

    assert timed_out_worker_pids
    assert result["worker_pid"] != timed_out_worker_pids[0]


@pytest.mark.filterwarnings(
    "ignore:This process.*multi-threaded.*:DeprecationWarning"
)
def test_shutdown_does_not_wait_for_long_running_request() -> None:
    worker_started = threading.Event()
    errors = []

    def hang(_path, *, progress_callback, **_kwargs):
        progress_callback(0.0, 1.0, 0)
        time.sleep(5.0)
        return {"words": []}

    def run_request() -> None:
        try:
            transcribe_audio_in_process(
                "unused.wav",
                timeout_seconds=10.0,
                _start_method="fork",
                progress_callback=lambda *_args: worker_started.set(),
            )
        except WhisperProcessError as exc:
            errors.append(exc)

    with patch("stage2.whisper_process.transcribe_audio", side_effect=hang):
        request_thread = threading.Thread(target=run_request, daemon=True)
        request_thread.start()
        assert worker_started.wait(timeout=1.0)
        started = time.monotonic()
        _shutdown_worker()
        elapsed = time.monotonic() - started
        request_thread.join(timeout=2.0)

    assert elapsed < 2.0
    assert not request_thread.is_alive()
    assert errors
