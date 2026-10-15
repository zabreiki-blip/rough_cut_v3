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


@pytest.mark.filterwarnings("ignore:This process.*multi-threaded.*:DeprecationWarning")
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


def test_cloud_options_are_decimal_safe_and_reach_child_runner() -> None:
    from decimal import Decimal

    options = {
        "max_cost_usd": Decimal("0.123456789123456789"),
        "usd_per_minute_estimate": Decimal("0.06"),
        "initial_cost_usd": Decimal("0.03"),
    }

    def inspect_options(_path, *, cloud_options, **_kwargs):
        return {"options": cloud_options}

    with patch("stage2.whisper_process.transcribe_audio", side_effect=inspect_options):
        result = transcribe_audio_in_process(
            "unused.wav",
            backend="cloud-audio",
            cloud_options=options,
            timeout_seconds=2,
            _start_method="fork",
        )

    assert result["options"]["max_cost_usd"] == "0.123456789123456789"
    assert result["options"]["usd_per_minute_estimate"] == "0.06"
    assert result["options"]["initial_cost_usd"] == "0.03"
    assert 0 < result["options"]["timeout_sec"] <= 2
    assert options["max_cost_usd"] == Decimal("0.123456789123456789")


@pytest.mark.parametrize("stop", ["cancel", "timeout"])
def test_cloud_options_budget_snapshot_survives_cancel_or_timeout(stop) -> None:
    cancel = threading.Event()
    reported = threading.Event()
    observed_progress = []

    def hang(_path, *, progress_callback, budget_callback, **_kwargs):
        budget_callback(
            {"limit_usd": 1.0, "spent_usd": 0.03, "reserved_usd": 0.12, "total_usd": 0.15}
        )
        progress_callback(1.0, 60.0, 2)
        time.sleep(5)
        return {}

    def on_progress(done, total, words):
        observed_progress.append((done, total, words))
        reported.set()
        if stop == "cancel":
            cancel.set()

    started = time.monotonic()
    error_type = WhisperCancelledError if stop == "cancel" else WhisperTimeoutError
    with patch("stage2.whisper_process.transcribe_audio", side_effect=hang):
        with pytest.raises(error_type) as caught:
            transcribe_audio_in_process(
                "unused.wav",
                backend="cloud-audio",
                cloud_options={
                    "initial_cost_usd": "0.03",
                    "max_cost_usd": "1",
                    "usd_per_minute_estimate": "0.06",
                },
                cancel_event=cancel,
                timeout_seconds=0.2,
                _start_method="fork",
                progress_callback=on_progress,
            )

    assert reported.is_set()
    assert observed_progress == [(1.0, 60.0, 2)]
    assert caught.value.cost_usd == pytest.approx(0.03)
    assert caught.value.reserved_cost_usd == pytest.approx(0.12)
    assert caught.value.cost_limit_usd == 1.0
    assert whisper_process._worker is None
    assert time.monotonic() - started < 1.0


def test_cloud_options_budget_error_crosses_process_boundary() -> None:
    from cost_budget import CostBudgetExceeded

    def fail(_path, **_kwargs):
        error = CostBudgetExceeded("budget exceeded")
        error.cost_usd = 0.04
        error.reserved_cost_usd = 0.12
        error.cost_limit_usd = 0.15
        raise error

    with patch("stage2.whisper_process.transcribe_audio", side_effect=fail):
        with pytest.raises(WhisperProcessError, match="CostBudgetExceeded") as caught:
            transcribe_audio_in_process(
                "unused.wav",
                cloud_options={"max_cost_usd": "0.15"},
                timeout_seconds=2,
                _start_method="fork",
            )

    assert caught.value.cost_usd == pytest.approx(0.04)
    assert caught.value.reserved_cost_usd == pytest.approx(0.12)
    assert caught.value.cost_limit_usd == pytest.approx(0.15)


def test_cancel_while_waiting_for_worker_lock_is_prompt() -> None:
    cancel = threading.Event()
    errors = []

    def request():
        try:
            transcribe_audio_in_process(
                "unused.wav",
                backend="cloud-audio",
                cancel_event=cancel,
                _start_method="fork",
                cloud_options={"initial_cost_usd": "0.03", "max_cost_usd": "1"},
            )
        except BaseException as error:
            errors.append(error)

    whisper_process._worker_lock.acquire()
    thread = threading.Thread(target=request, daemon=True)
    try:
        thread.start()
        cancel.set()
        thread.join(timeout=0.5)
        assert not thread.is_alive(), "cancellation waited behind the busy worker"
    finally:
        whisper_process._worker_lock.release()
        thread.join(timeout=2)

    assert len(errors) == 1
    assert isinstance(errors[0], WhisperCancelledError)
    assert errors[0].cost_usd == pytest.approx(0.03)
    assert errors[0].reserved_cost_usd == 0.0
    assert whisper_process._worker is None


def test_timeout_whole_job_deadline_includes_worker_queue_time() -> None:
    errors = []

    def request():
        try:
            transcribe_audio_in_process(
                "unused.wav",
                deadline_monotonic=time.monotonic() + 0.05,
                timeout_seconds=10,
                _start_method="fork",
            )
        except BaseException as error:
            errors.append(error)

    whisper_process._worker_lock.acquire()
    thread = threading.Thread(target=request, daemon=True)
    try:
        thread.start()
        thread.join(timeout=0.5)
        assert not thread.is_alive(), "whole-job deadline ignored worker queue time"
    finally:
        whisper_process._worker_lock.release()
        thread.join(timeout=2)

    assert len(errors) == 1
    assert isinstance(errors[0], WhisperTimeoutError)
    assert whisper_process._worker is None


def test_timeout_remaining_deadline_bounds_cloud_inference_options() -> None:
    def inspect_options(_path, *, cloud_options, **_kwargs):
        return {"timeout": cloud_options["timeout_sec"]}

    with patch("stage2.whisper_process.transcribe_audio", side_effect=inspect_options):
        result = transcribe_audio_in_process(
            "unused.wav",
            backend="cloud-audio",
            timeout_seconds=10,
            deadline_monotonic=time.monotonic() + 0.5,
            _start_method="fork",
        )

    assert 0 < result["timeout"] <= 0.5


def test_cancel_preserves_budget_message_queued_behind_progress() -> None:
    cancel = threading.Event()
    budget_sent = multiprocessing.get_context("fork").Event()

    def hang(_path, *, progress_callback, budget_callback, **_kwargs):
        progress_callback(0, 60, 0)
        budget_callback(
            {"limit_usd": 1.0, "spent_usd": 0.03, "reserved_usd": 0.06, "total_usd": 0.09}
        )
        budget_sent.set()
        time.sleep(5)
        return {}

    def on_progress(*_args):
        assert budget_sent.wait(timeout=1)
        cancel.set()

    with patch("stage2.whisper_process.transcribe_audio", side_effect=hang):
        with pytest.raises(WhisperCancelledError) as caught:
            transcribe_audio_in_process(
                "unused.wav",
                cancel_event=cancel,
                timeout_seconds=2,
                _start_method="fork",
                progress_callback=on_progress,
            )

    assert caught.value.reserved_cost_usd == pytest.approx(0.06)
    assert caught.value.cost_usd == pytest.approx(0.03)


@pytest.mark.parametrize("stop", ["cancel", "timeout"])
def test_cloud_options_parent_stop_interrupts_provider_http(tmp_path, monkeypatch, stop) -> None:
    import asyncio
    import httpx
    from stage2 import cloud_asr

    context = multiprocessing.get_context("fork")
    calls = context.Value("i", 0)
    entered_http = context.Event()
    cancel = threading.Event()
    audio_path = tmp_path / "audio.wav"
    audio_path.touch()

    async def extract(*_args):
        return b"mp3"

    async def handler(_request):
        with calls.get_lock():
            calls.value += 1
        entered_http.set()
        await asyncio.sleep(5)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "[00:01] ГОЛОС 1: Готово"}}],
                "usage": {"cost": 0.01},
            },
        )

    real_client = httpx.AsyncClient
    monkeypatch.setattr(cloud_asr.CloudAudioAsrAdapter, "_probe_duration", lambda *_: 120.0)
    monkeypatch.setattr(cloud_asr.CloudAudioAsrAdapter, "_extract_chunk_mp3", extract)
    monkeypatch.setattr(
        cloud_asr.httpx,
        "AsyncClient",
        lambda **_kw: real_client(transport=httpx.MockTransport(handler)),
    )

    def cancel_after_http_starts():
        if entered_http.wait(timeout=2):
            cancel.set()

    thread = None
    if stop == "cancel":
        thread = threading.Thread(target=cancel_after_http_starts, daemon=True)
        thread.start()
    started = time.monotonic()
    error_type = WhisperCancelledError if stop == "cancel" else WhisperTimeoutError
    try:
        with pytest.raises(error_type) as caught:
            transcribe_audio_in_process(
                audio_path,
                backend="cloud-audio",
                cancel_event=cancel,
                timeout_seconds=0.2 if stop == "timeout" else 2,
                _start_method="fork",
                cloud_options={
                    "api_key": "test-key",
                    "chunk_minutes": 1,
                    "overlap_sec": 0,
                    "max_cost_usd": "1",
                    "usd_per_minute_estimate": "0.06",
                },
            )
    finally:
        if thread is not None:
            thread.join(timeout=2)

    assert entered_http.is_set()
    assert calls.value == 1
    assert time.monotonic() - started < 1.0
    assert caught.value.reserved_cost_usd == pytest.approx(0.06)
    assert whisper_process._worker is None


def test_cancel_before_cloud_start_retains_environment_budget_limit(monkeypatch) -> None:
    cancel = threading.Event()
    cancel.set()
    monkeypatch.setenv("ROUGH_CUT_MAX_USD_PER_JOB", "0.15")
    monkeypatch.setenv("ROUGH_CUT_CLOUD_ASR_USD_PER_MINUTE_ESTIMATE", "0.06")

    with pytest.raises(WhisperCancelledError) as caught:
        transcribe_audio_in_process(
            "unused.wav",
            backend="cloud-audio",
            cancel_event=cancel,
            _start_method="fork",
        )

    assert caught.value.cost_usd == 0.0
    assert caught.value.reserved_cost_usd == 0.0
    assert caught.value.cost_limit_usd == pytest.approx(0.15)
    assert whisper_process._worker is None


@pytest.mark.parametrize("new_limit,new_rate", [("0.01", "0.06"), ("1", "2")])
def test_cloud_options_refresh_environment_budget_in_reused_worker(
    tmp_path, monkeypatch, new_limit, new_rate
) -> None:
    import httpx
    from stage2 import cloud_asr

    calls = multiprocessing.get_context("fork").Value("i", 0)
    audio_path = tmp_path / "audio.wav"
    audio_path.touch()
    monkeypatch.setenv("ROUGH_CUT_MAX_USD_PER_JOB", "1")
    monkeypatch.setenv("ROUGH_CUT_CLOUD_ASR_USD_PER_MINUTE_ESTIMATE", "0.06")

    async def extract(*_args):
        return b"mp3"

    def handler(_request):
        with calls.get_lock():
            calls.value += 1
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "[00:01] ГОЛОС 1: Готово"}}],
                "usage": {"cost": 0.005},
            },
        )

    real_client = httpx.AsyncClient
    monkeypatch.setattr(cloud_asr.CloudAudioAsrAdapter, "_probe_duration", lambda *_: 60.0)
    monkeypatch.setattr(cloud_asr.CloudAudioAsrAdapter, "_extract_chunk_mp3", extract)
    monkeypatch.setattr(
        cloud_asr.httpx,
        "AsyncClient",
        lambda **_kw: real_client(transport=httpx.MockTransport(handler)),
    )
    first = transcribe_audio_in_process(
        audio_path,
        backend="cloud-audio",
        timeout_seconds=2,
        _start_method="fork",
        cloud_options={"api_key": "test-key"},
    )
    assert first["cost_limit_usd"] == 1.0
    assert calls.value == 1
    assert whisper_process._worker is not None
    monkeypatch.setenv("ROUGH_CUT_MAX_USD_PER_JOB", new_limit)
    monkeypatch.setenv("ROUGH_CUT_CLOUD_ASR_USD_PER_MINUTE_ESTIMATE", new_rate)

    with pytest.raises(WhisperProcessError, match="CostBudgetExceeded") as caught:
        transcribe_audio_in_process(
            audio_path,
            backend="cloud-audio",
            timeout_seconds=2,
            _start_method="fork",
            cloud_options={"api_key": "test-key"},
        )

    assert calls.value == 1
    assert caught.value.cost_limit_usd == float(new_limit)
    assert caught.value.reserved_cost_usd == 0.0


@pytest.mark.parametrize(
    "bad_options",
    [
        {"max_cost_usd": "invalid"},
        {"initial_cost_usd": "invalid"},
        {"timeout_sec": "invalid"},
    ],
)
def test_local_backend_ignores_malformed_cloud_options_and_reuses_worker(monkeypatch, bad_options):
    monkeypatch.setenv("ROUGH_CUT_MAX_USD_PER_JOB", "invalid")
    monkeypatch.setenv("ROUGH_CUT_CLOUD_ASR_USD_PER_MINUTE_ESTIMATE", "invalid")

    def local(_path, *, cloud_options=None, **_kwargs):
        return {"worker_pid": os.getpid(), "cloud_options": cloud_options}

    with patch("stage2.whisper_process.transcribe_audio", side_effect=local):
        first = transcribe_audio_in_process(
            "unused.wav",
            backend="faster-whisper",
            timeout_seconds=2,
            _start_method="fork",
        )
        second = transcribe_audio_in_process(
            "unused.wav",
            backend="faster-whisper",
            timeout_seconds=2,
            _start_method="fork",
            cloud_options=bad_options,
        )
        third = transcribe_audio_in_process(
            "unused.wav",
            backend="faster-whisper",
            timeout_seconds=2,
            _start_method="fork",
        )

    assert first["worker_pid"] == second["worker_pid"] == third["worker_pid"]
    assert first["cloud_options"] is None
    assert second["cloud_options"] is None
    assert third["cloud_options"] is None
    assert whisper_process._worker is not None
    assert whisper_process._worker.process.is_alive()
    assert whisper_process._worker_last_active is not None
