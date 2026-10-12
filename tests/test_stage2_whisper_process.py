from __future__ import annotations

import multiprocessing
import os
import threading
import time
from unittest.mock import patch

import pytest

import stage2.whisper_process as whisper_process
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
            progress_callback=lambda done, total, words: progress.append((done, total, words)),
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
        first = transcribe_audio_in_process("first.wav", timeout_seconds=2.0, _start_method="fork")
        second = transcribe_audio_in_process(
            "second.wav", timeout_seconds=2.0, _start_method="fork"
        )

    assert first["worker_pid"] == second["worker_pid"]
    assert first["path"].endswith("first.wav")
    assert second["path"].endswith("second.wav")


def test_process_is_retired_after_each_success_when_memory_mode_is_enabled() -> None:
    def fake_transcribe(path, **_kwargs):
        return {"path": path, "worker_pid": os.getpid()}

    with (
        patch("stage2.whisper_process.transcribe_audio", side_effect=fake_transcribe),
        patch.dict("os.environ", {"SYNC_WHISPER_UNLOAD_AFTER_JOB": "1"}, clear=True),
    ):
        first = transcribe_audio_in_process("first.wav", timeout_seconds=2.0, _start_method="fork")
        second = transcribe_audio_in_process(
            "second.wav", timeout_seconds=2.0, _start_method="fork"
        )

    assert first["worker_pid"] != second["worker_pid"]


def test_rejects_invalid_unload_after_job_setting() -> None:
    with patch.dict("os.environ", {"SYNC_WHISPER_UNLOAD_AFTER_JOB": "sometimes"}, clear=True):
        with pytest.raises(ValueError, match="SYNC_WHISPER_UNLOAD_AFTER_JOB"):
            transcribe_audio_in_process("unused.wav", timeout_seconds=2.0, _start_method="fork")


def test_rejects_invalid_idle_timeout_setting() -> None:
    with patch.dict("os.environ", {"SYNC_WHISPER_IDLE_TIMEOUT_SECONDS": "-1"}, clear=True):
        with pytest.raises(ValueError, match="SYNC_WHISPER_IDLE_TIMEOUT_SECONDS"):
            transcribe_audio_in_process("unused.wav", timeout_seconds=2.0, _start_method="fork")

    with patch.dict("os.environ", {"SYNC_WHISPER_IDLE_TIMEOUT_SECONDS": "soon"}, clear=True):
        with pytest.raises(ValueError, match="SYNC_WHISPER_IDLE_TIMEOUT_SECONDS"):
            transcribe_audio_in_process("unused.wav", timeout_seconds=2.0, _start_method="fork")


def test_idle_worker_is_retired_after_timeout() -> None:
    def fake_transcribe(path, **_kwargs):
        return {"path": path, "worker_pid": os.getpid()}

    with (
        patch("stage2.whisper_process.transcribe_audio", side_effect=fake_transcribe),
        patch.dict("os.environ", {"SYNC_WHISPER_IDLE_TIMEOUT_SECONDS": "0.2"}, clear=True),
    ):
        transcribe_audio_in_process("first.wav", timeout_seconds=2.0, _start_method="fork")
        assert whisper_process._worker is not None

        # Poll in short steps instead of a single long sleep: fast on the
        # common case, still bounded and deterministic if the watchdog is slow.
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and whisper_process._worker is not None:
            time.sleep(0.02)

    assert whisper_process._worker is None


def test_idle_timeout_disabled_keeps_worker_warm() -> None:
    def fake_transcribe(path, **_kwargs):
        return {"path": path, "worker_pid": os.getpid()}

    with (
        patch("stage2.whisper_process.transcribe_audio", side_effect=fake_transcribe),
        patch.dict("os.environ", {"SYNC_WHISPER_IDLE_TIMEOUT_SECONDS": "0"}, clear=True),
    ):
        first = transcribe_audio_in_process("first.wav", timeout_seconds=2.0, _start_method="fork")
        # Longer than the watchdog's poll interval: with auto-unload disabled
        # the worker must still be warm afterwards.
        time.sleep(0.3)
        second = transcribe_audio_in_process(
            "second.wav", timeout_seconds=2.0, _start_method="fork"
        )

    assert first["worker_pid"] == second["worker_pid"]


def test_active_transcription_survives_idle_watchdog() -> None:
    def hang(_path, **_kwargs):
        time.sleep(0.3)
        return {"words": []}

    with (
        patch("stage2.whisper_process.transcribe_audio", side_effect=hang),
        patch.dict("os.environ", {"SYNC_WHISPER_IDLE_TIMEOUT_SECONDS": "0.05"}, clear=True),
    ):
        result = transcribe_audio_in_process(
            "unused.wav", timeout_seconds=5.0, _start_method="fork"
        )

    assert result == {"words": []}


def test_process_restarts_when_inference_configuration_changes() -> None:
    def fake_transcribe(_path, **_kwargs):
        return {"worker_pid": os.getpid()}

    with (
        patch("stage2.whisper_process.transcribe_audio", side_effect=fake_transcribe),
        patch.dict("os.environ", {}, clear=True),
    ):
        first = transcribe_audio_in_process("first.wav", timeout_seconds=2.0, _start_method="fork")
        with patch.dict("os.environ", {"SYNC_WHISPER_CPU_THREADS": "8"}, clear=False):
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
            transcribe_audio_in_process("unused.wav", timeout_seconds=2.0, _start_method="fork")
    failed_worker_pid = int(str(exc.value).rsplit(" ", 1)[1])

    def succeed(_path, **_kwargs):
        return {"worker_pid": os.getpid()}

    with patch("stage2.whisper_process.transcribe_audio", side_effect=succeed):
        result = transcribe_audio_in_process(
            "unused.wav", timeout_seconds=2.0, _start_method="fork"
        )

    assert result["worker_pid"] != failed_worker_pid


# Safety net only: a correct implementation kills the hung worker within
# milliseconds. The deadline bounds polling so a regression fails instead of
# hanging, and nothing asserts on elapsed wall-clock time.
_KILL_DEADLINE_SECONDS = 10.0


def _hang_forever() -> None:
    # Blocks until the parent kills this worker; never a timing source.
    threading.Event().wait(60.0)


def _wait_until_process_gone(pid: int) -> bool:
    deadline = time.monotonic() + _KILL_DEADLINE_SECONDS
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.01)
    return False


def test_timeout_terminates_worker_promptly() -> None:
    worker_pids: list[int] = []

    def hang(_path, *, progress_callback, **_kwargs):
        progress_callback(0.0, 1.0, os.getpid())
        _hang_forever()
        return {"words": []}

    with (
        patch("stage2.whisper_process.transcribe_audio", side_effect=hang),
        pytest.raises(WhisperTimeoutError, match="exceeded"),
    ):
        transcribe_audio_in_process(
            "unused.wav",
            timeout_seconds=0.1,
            _start_method="fork",
            progress_callback=lambda _done, _total, pid: worker_pids.append(pid),
        )

    assert worker_pids
    assert whisper_process._worker is None
    assert _wait_until_process_gone(worker_pids[0])


def test_cancel_event_terminates_worker_promptly() -> None:
    worker_pids: list[int] = []
    cancel_event = threading.Event()

    def hang(_path, *, progress_callback, **_kwargs):
        progress_callback(0.0, 1.0, os.getpid())
        _hang_forever()
        return {"words": []}

    def on_progress(_done, _total, pid) -> None:
        worker_pids.append(pid)
        cancel_event.set()

    with (
        patch("stage2.whisper_process.transcribe_audio", side_effect=hang),
        pytest.raises(WhisperCancelledError, match="cancelled"),
    ):
        transcribe_audio_in_process(
            "unused.wav",
            cancel_event=cancel_event,
            timeout_seconds=60.0,
            _start_method="fork",
            progress_callback=on_progress,
        )

    assert cancel_event.is_set()
    assert worker_pids
    assert whisper_process._worker is None
    assert _wait_until_process_gone(worker_pids[0])


def test_worker_restarts_after_timeout() -> None:
    def hang(_path, *, progress_callback, **_kwargs):
        progress_callback(0.0, 1.0, os.getpid())
        _hang_forever()
        return {"words": []}

    timed_out_worker_pids = []
    with patch("stage2.whisper_process.transcribe_audio", side_effect=hang):
        with pytest.raises(WhisperTimeoutError, match="exceeded"):
            transcribe_audio_in_process(
                "unused.wav",
                timeout_seconds=0.1,
                _start_method="fork",
                progress_callback=lambda _done, _total, pid: timed_out_worker_pids.append(pid),
            )

    def succeed(_path, **_kwargs):
        return {"worker_pid": os.getpid()}

    with patch("stage2.whisper_process.transcribe_audio", side_effect=succeed):
        result = transcribe_audio_in_process(
            "unused.wav", timeout_seconds=2.0, _start_method="fork"
        )

    assert timed_out_worker_pids
    assert result["worker_pid"] != timed_out_worker_pids[0]
    assert _wait_until_process_gone(timed_out_worker_pids[0])


@pytest.mark.filterwarnings("ignore:This process.*multi-threaded.*:DeprecationWarning")
def test_shutdown_does_not_wait_for_long_running_request() -> None:
    worker_started = threading.Event()
    worker_pids: list[int] = []
    errors = []

    def hang(_path, *, progress_callback, **_kwargs):
        progress_callback(0.0, 1.0, os.getpid())
        _hang_forever()
        return {"words": []}

    def on_progress(_done, _total, pid) -> None:
        worker_pids.append(pid)
        worker_started.set()

    def run_request() -> None:
        try:
            transcribe_audio_in_process(
                "unused.wav",
                timeout_seconds=60.0,
                _start_method="fork",
                progress_callback=on_progress,
            )
        except WhisperProcessError as exc:
            errors.append(exc)

    with patch("stage2.whisper_process.transcribe_audio", side_effect=hang):
        request_thread = threading.Thread(target=run_request, daemon=True)
        request_thread.start()
        assert worker_started.wait(timeout=_KILL_DEADLINE_SECONDS)
        # Returns instead of blocking on the in-flight request (which would
        # only end at its 60s timeout); the request thread then unblocks.
        _shutdown_worker()
        request_thread.join(timeout=_KILL_DEADLINE_SECONDS)

    assert not request_thread.is_alive()
    assert errors
    assert not isinstance(errors[0], WhisperTimeoutError)
    assert _wait_until_process_gone(worker_pids[0])
