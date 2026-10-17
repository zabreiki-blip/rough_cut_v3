"""In-process tests for the Whisper worker IPC protocol.

The real forked/spawned process is replaced by scripted fakes, so the child-side
loop (which coverage cannot see inside a subprocess) and every parent-side error
branch run deterministically without sleeps or child processes.
"""

from __future__ import annotations

import math
import threading
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

import stage2.whisper_process as wp
from stage2.whisper_process import (
    WhisperCancelledError,
    WhisperProcessError,
    WhisperTimeoutError,
    _Worker,
    transcribe_audio_in_process,
)


class FakeConnection:
    """Scripted duplex endpoint. Queue items may be callables of the request id."""

    def __init__(self, incoming: list[Any] | None = None, *, send_error: Exception | None = None):
        self.incoming = list(incoming or [])
        self.sent: list[Any] = []
        self.send_error = send_error
        self.closed = False
        self.request_id: int | None = None
        self.poll_error: Exception | None = None

    def poll(self, timeout: float = 0.0) -> bool:
        if self.poll_error is not None:
            raise self.poll_error
        return bool(self.incoming)

    def recv(self) -> Any:
        if not self.incoming:
            raise EOFError
        item = self.incoming.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item(self.request_id) if callable(item) else item

    def send(self, message: Any) -> None:
        if self.send_error is not None:
            raise self.send_error
        if message and message[0] == "transcribe":
            self.request_id = message[1]
        self.sent.append(message)

    def close(self) -> None:
        self.closed = True


class FakeProcess:
    def __init__(self, *, alive: bool = True, exitcode: int | None = None, stubborn: bool = False):
        self.alive = alive
        self.exitcode = exitcode
        self.stubborn = stubborn
        self.calls: list[str] = []

    def is_alive(self) -> bool:
        return self.alive

    def join(self, timeout: float | None = None) -> None:
        self.calls.append("join")

    def terminate(self) -> None:
        self.calls.append("terminate")
        self.alive = self.stubborn

    def kill(self) -> None:
        self.calls.append("kill")
        self.alive = False


@pytest.fixture(autouse=True)
def _clean_worker_state():
    wp._shutdown_worker()
    yield
    wp._shutdown_worker()


def _run_with(connection: FakeConnection, process: FakeProcess | None = None, **kwargs: Any) -> Any:
    worker = _Worker(process=process or FakeProcess(), connection=connection, key=("local",))
    kwargs.setdefault("timeout_seconds", 5.0)
    with patch.object(wp, "_get_worker_locked", return_value=worker):
        return transcribe_audio_in_process("a.wav", **kwargs)


# --- child-side loop ---------------------------------------------------------------


def _command(request_id: int = 1, **overrides: Any) -> tuple:
    values = {
        "audio": "a.wav",
        "backend": "local",
        "model": "tiny",
        "language": "ru",
        "options": None,
    }
    values.update(overrides)
    return (
        "transcribe",
        request_id,
        values["audio"],
        values["backend"],
        values["model"],
        values["language"],
        values["options"],
    )


def test_child_loop_stops_on_stop_empty_and_eof_and_always_closes() -> None:
    for incoming in ([("stop",)], [()], [None], []):
        conn = FakeConnection(incoming)
        wp._transcribe_worker(conn)  # type: ignore[arg-type]
        assert conn.closed is True
        assert conn.sent == []


def test_child_loop_ignores_unknown_commands_and_serves_multiple_requests() -> None:
    conn = FakeConnection([("ping",), _command(1), _command(2), ("stop",)])

    def fake_transcribe(path, **kwargs):
        kwargs["progress_callback"](1.0, 2.0, 3)
        kwargs["budget_callback"]({"spent_usd": 0.5})
        return {"words": [path]}

    with patch.object(wp, "transcribe_audio", side_effect=fake_transcribe) as transcribe:
        wp._transcribe_worker(conn)  # type: ignore[arg-type]

    assert [m[0] for m in conn.sent] == ["progress", "budget", "result"] * 2
    assert conn.sent[2] == ("result", 1, {"words": ["a.wav"]})
    assert conn.sent[3][1] == 2
    assert transcribe.call_args.kwargs["backend"] == "local"
    assert transcribe.call_args.kwargs["model"] == "tiny"
    assert transcribe.call_args.kwargs["language"] == "ru"


def test_child_loop_reports_error_with_cost_fields_and_exits() -> None:
    class CostError(RuntimeError):
        cost_usd = 0.25
        reserved_cost_usd = 0.5
        cost_limit_usd = 1.0
        cost_pending = True

    conn = FakeConnection([_command(7), _command(8)])
    with patch.object(wp, "transcribe_audio", side_effect=CostError("quota")):
        wp._transcribe_worker(conn)  # type: ignore[arg-type]

    # A failed inference retires the worker: the second command is never served.
    assert conn.sent == [("error", 7, "CostError", "quota", 0.25, 0.5, 1.0, True)]
    assert len(conn.incoming) == 1
    assert conn.closed is True


def test_child_loop_survives_broken_pipe_while_reporting_error() -> None:
    conn = FakeConnection([_command()], send_error=BrokenPipeError())
    with patch.object(wp, "transcribe_audio", side_effect=ValueError("boom")):
        wp._transcribe_worker(conn)  # type: ignore[arg-type]

    assert conn.closed is True


def test_child_loop_also_reports_base_exceptions() -> None:
    conn = FakeConnection([_command()])
    with patch.object(wp, "transcribe_audio", side_effect=KeyboardInterrupt("stop")):
        wp._transcribe_worker(conn)  # type: ignore[arg-type]

    assert conn.sent[0][:4] == ("error", 1, "KeyboardInterrupt", "stop")


# --- parent-side protocol ------------------------------------------------------------


def test_result_message_is_returned_and_progress_budget_callbacks_fire() -> None:
    progress: list[tuple] = []
    budgets: list[dict] = []
    conn = FakeConnection(
        [
            lambda rid: ("progress", rid, 1, 4, 9),
            lambda rid: ("budget", rid, {"spent_usd": 1.0, "reserved_usd": 0.0, "limit_usd": 2.0}),
            lambda rid: ("budget", rid, {"spent_usd": 1.0, "reserved_usd": 0.0, "limit_usd": 2.0}),
            lambda rid: ("result", rid, {"words": []}),
        ]
    )

    result = _run_with(
        conn,
        progress_callback=lambda *a: progress.append(a),
        budget_callback=budgets.append,
    )

    assert result == {"words": []}
    assert progress == [(1.0, 4.0, 9)]
    assert len(budgets) == 1, "identical consecutive snapshots are published once"


def test_callback_exceptions_do_not_break_the_transcription() -> None:
    def boom(*_args: Any) -> None:
        raise RuntimeError("ui exploded")

    conn = FakeConnection(
        [
            lambda rid: ("progress", rid, 1, 2, 3),
            lambda rid: ("budget", rid, {"spent_usd": 0.1, "reserved_usd": 0, "limit_usd": 0}),
            lambda rid: ("result", rid, "ok"),
        ]
    )

    assert _run_with(conn, progress_callback=boom, budget_callback=boom) == "ok"


def test_error_message_becomes_process_error_with_budget_attributes() -> None:
    conn = FakeConnection(
        [lambda rid: ("error", rid, "RuntimeError", "bad audio", 0.3, 0.4, 5.0, True)]
    )

    with pytest.raises(WhisperProcessError, match="RuntimeError: bad audio") as info:
        _run_with(conn)

    err = info.value
    assert (err.cost_usd, err.reserved_cost_usd, err.cost_limit_usd) == (0.3, 0.4, 5.0)  # type: ignore[attr-defined]
    assert err.cost_pending is True  # type: ignore[attr-defined]
    assert conn.closed is True


def test_short_error_message_keeps_default_budget_attributes() -> None:
    conn = FakeConnection([lambda rid: ("error", rid, "OSError", "disk")])

    with pytest.raises(WhisperProcessError, match="OSError: disk") as info:
        _run_with(conn)

    assert info.value.cost_usd == 0.0  # type: ignore[attr-defined]
    assert info.value.cost_pending is False  # type: ignore[attr-defined]


def test_unknown_message_kind_is_a_protocol_error() -> None:
    conn = FakeConnection([lambda rid: ("surprise", rid)])

    with pytest.raises(WhisperProcessError, match="unknown message: 'surprise'"):
        _run_with(conn)

    assert conn.closed is True


def test_response_for_another_request_is_a_protocol_error() -> None:
    conn = FakeConnection([("result", -1, "stale")])

    with pytest.raises(WhisperProcessError, match="unexpected response"):
        _run_with(conn)


@pytest.mark.parametrize("incoming", [[EOFError()], [None], [()]])
def test_closed_pipe_or_empty_message_reports_exit_code(incoming: list[Any]) -> None:
    conn = FakeConnection(incoming)
    conn.poll = lambda timeout=0.0: True  # type: ignore[method-assign]

    with pytest.raises(WhisperProcessError, match=r"exited without a result \(exit code 7\)"):
        _run_with(conn, FakeProcess(alive=True, exitcode=7))


def test_dead_worker_without_message_is_detected_even_if_poll_fails() -> None:
    conn = FakeConnection()
    conn.poll_error = OSError("bad fd")

    with pytest.raises(WhisperProcessError, match=r"exit code -9"):
        _run_with(conn, FakeProcess(alive=False, exitcode=-9))


def test_ipc_send_failure_is_wrapped_and_worker_discarded() -> None:
    conn = FakeConnection(send_error=BrokenPipeError("gone"))

    with pytest.raises(WhisperProcessError, match="IPC connection failed") as info:
        _run_with(conn)

    assert isinstance(info.value.__cause__, BrokenPipeError)
    assert conn.closed is True


def test_unload_after_job_retires_worker_gracefully(monkeypatch) -> None:
    monkeypatch.setenv("SYNC_WHISPER_UNLOAD_AFTER_JOB", "1")
    conn = FakeConnection([lambda rid: ("result", rid, "ok")])
    process = FakeProcess(alive=True)

    assert _run_with(conn, process) == "ok"

    assert ("stop",) in conn.sent
    assert conn.closed is True


# --- validation of inputs ------------------------------------------------------------------


@pytest.mark.parametrize("value", [math.inf, -math.inf, math.nan])
def test_non_finite_deadline_is_rejected(value: float) -> None:
    with pytest.raises(ValueError, match="deadline_monotonic must be finite"):
        transcribe_audio_in_process("a.wav", deadline_monotonic=value, timeout_seconds=1.0)


@pytest.mark.parametrize("bad", [0, -1, math.nan, math.inf])
def test_cloud_timeout_option_must_be_positive_and_finite(bad: float) -> None:
    with pytest.raises(ValueError, match="cloud timeout_sec"):
        transcribe_audio_in_process(
            "a.wav", backend="cloud", cloud_options={"timeout_sec": bad}, timeout_seconds=5.0
        )


@pytest.mark.parametrize("bad", [0, -2.5, math.nan, math.inf, "abc"])
def test_invalid_whisper_timeout_is_rejected(bad: Any) -> None:
    with pytest.raises(ValueError, match="SYNC_WHISPER_TIMEOUT_SECONDS"):
        wp._configured_timeout(bad)


def test_timeout_is_read_from_environment(monkeypatch) -> None:
    monkeypatch.setenv("SYNC_WHISPER_TIMEOUT_SECONDS", "12.5")
    assert wp._configured_timeout(None) == 12.5
    assert wp._configured_timeout(3) == 3.0


# --- cancellation / deadline with queued accounting --------------------------------------------


def test_cancel_drains_budget_and_error_messages_for_current_request_only() -> None:
    cancel = threading.Event()
    conn = FakeConnection()
    process = FakeProcess(alive=True)

    def send(message: Any) -> None:
        if message[0] == "transcribe":
            rid = message[1]
            conn.incoming.extend(
                [
                    ("budget", rid - 100, {"spent_usd": 99.0}),  # stale request: ignored
                    ("budget", rid, {"spent_usd": 1.5, "reserved_usd": 0.5, "limit_usd": 3.0}),
                    ("error", rid, "X", "y", 2.0, None, 4.0),
                ]
            )
            cancel.set()

    conn.send = send  # type: ignore[method-assign]

    with pytest.raises(WhisperCancelledError) as info:
        _run_with(conn, process, cancel_event=cancel)

    err = info.value
    assert err.cost_usd == 2.0  # type: ignore[attr-defined]
    assert err.reserved_cost_usd == 0.5  # type: ignore[attr-defined]
    assert err.cost_limit_usd == 4.0  # type: ignore[attr-defined]
    assert "terminate" in process.calls


def test_cancel_tolerates_pipe_failure_while_draining() -> None:
    cancel = threading.Event()
    conn = FakeConnection()

    def send(_message: Any) -> None:
        cancel.set()
        conn.incoming.append(EOFError())

    conn.send = send  # type: ignore[method-assign]
    conn.poll = lambda timeout=0.0: bool(conn.incoming)  # type: ignore[method-assign]

    with pytest.raises(WhisperCancelledError):
        _run_with(conn, cancel_event=cancel)

    assert conn.closed is True


def test_expired_whole_job_deadline_raises_timeout_before_touching_a_worker() -> None:
    with patch.object(wp, "_get_worker_locked") as get_worker:
        with pytest.raises(WhisperTimeoutError, match="exceeded"):
            transcribe_audio_in_process(
                "a.wav", deadline_monotonic=time.monotonic() - 1, timeout_seconds=5.0
            )

    get_worker.assert_not_called()


def test_pre_set_cancel_event_raises_immediately() -> None:
    cancel = threading.Event()
    cancel.set()
    with patch.object(wp, "_get_worker_locked") as get_worker:
        with pytest.raises(WhisperCancelledError):
            transcribe_audio_in_process("a.wav", cancel_event=cancel, timeout_seconds=5.0)

    get_worker.assert_not_called()


# --- worker lifecycle helpers ------------------------------------------------------------------


def test_stop_process_skips_dead_process_and_escalates_to_kill() -> None:
    dead = FakeProcess(alive=False)
    wp._stop_process(dead)
    assert dead.calls == ["join"]

    stubborn = FakeProcess(alive=True, stubborn=True)
    wp._stop_process(stubborn)
    assert stubborn.calls == ["terminate", "join", "kill", "join"]

    polite = FakeProcess(alive=True)
    wp._stop_process(polite)
    assert polite.calls == ["terminate", "join"]


def test_stop_process_without_kill_method_does_not_crash() -> None:
    class NoKill:
        def is_alive(self) -> bool:
            return True

        def terminate(self) -> None: ...

        def join(self, timeout: float | None = None) -> None: ...

    wp._stop_process(NoKill())


def test_retire_worker_graceful_tolerates_broken_pipe_and_joins_dead_process() -> None:
    conn = FakeConnection(send_error=BrokenPipeError())
    process = FakeProcess(alive=True)
    wp._retire_worker(_Worker(process, conn, ("k",)), graceful=True)  # type: ignore[arg-type]
    assert conn.closed and "terminate" in process.calls

    dead = FakeProcess(alive=False)
    conn2 = FakeConnection()
    wp._retire_worker(_Worker(dead, conn2, ("k",)), graceful=True)  # type: ignore[arg-type]
    assert conn2.sent == [] and conn2.closed and dead.calls == ["join"]


def test_shutdown_is_a_no_op_in_a_forked_child(monkeypatch) -> None:
    conn = FakeConnection()
    monkeypatch.setattr(wp, "_worker", _Worker(FakeProcess(), conn, ("k",)))  # type: ignore[arg-type]
    monkeypatch.setattr(wp, "_worker_owner_pid", -1)

    wp._shutdown_worker()

    assert wp._worker is not None and conn.closed is False
    monkeypatch.setattr(wp, "_worker", None)


def test_after_fork_child_resets_inherited_state(monkeypatch) -> None:
    conn = FakeConnection()
    monkeypatch.setattr(wp, "_worker", _Worker(FakeProcess(), conn, ("k",)))  # type: ignore[arg-type]
    monkeypatch.setattr(wp, "_request_number", 41)
    monkeypatch.setattr(wp, "_worker_last_active", 1.0)
    monkeypatch.setattr(wp, "_watchdog_thread", threading.current_thread())
    monkeypatch.setattr(wp, "_worker_lock", wp._worker_lock)
    monkeypatch.setattr(wp, "_worker_owner_pid", -1)

    wp._after_fork_child()

    assert conn.closed is True
    assert wp._worker is None and wp._request_number == 0
    assert wp._worker_last_active is None and wp._watchdog_thread is None
    assert wp._worker_owner_pid != -1
    assert wp._worker_lock.acquire(blocking=False)
    wp._worker_lock.release()


def test_start_worker_closes_both_pipe_ends_when_process_fails_to_start() -> None:
    parent, child = FakeConnection(), FakeConnection()

    class Context:
        def Pipe(self, duplex: bool = True):
            return parent, child

        def Process(self, **_kwargs: Any):
            return SimpleNamespace(start=self._boom)

        @staticmethod
        def _boom() -> None:
            raise OSError("cannot fork")

    with patch.object(wp.multiprocessing, "get_context", return_value=Context()):
        with pytest.raises(OSError, match="cannot fork"):
            wp._start_worker_locked(("local", "spawn"))

    assert parent.closed and child.closed


def test_get_worker_replaces_dead_or_mismatched_worker(monkeypatch) -> None:
    old = _Worker(FakeProcess(alive=False), FakeConnection(), ("old",))
    fresh = _Worker(FakeProcess(), FakeConnection(), ("new",))
    monkeypatch.setattr(wp, "_worker", old)
    with patch.object(wp, "_start_worker_locked", return_value=fresh) as start:
        assert wp._get_worker_locked(("new",)) is fresh

    start.assert_called_once_with(("new",))
    assert old.connection.closed is True  # type: ignore[attr-defined]
    monkeypatch.setattr(wp, "_worker", None)


# --- idle watchdog -----------------------------------------------------------------------------


class _StopLoop(Exception):
    pass


def _run_watchdog(monkeypatch, owner_pid: int, iterations: int) -> list[float]:
    """Run ``_watchdog_loop`` on this thread, replacing sleep with a counter."""
    slept: list[float] = []
    real_time = time

    def fake_sleep(seconds: float) -> None:
        if threading.current_thread() is not threading.main_thread():
            real_time.sleep(seconds)
            return
        slept.append(seconds)
        if len(slept) >= iterations:
            raise _StopLoop

    fake_time = SimpleNamespace(sleep=fake_sleep, monotonic=real_time.monotonic)
    monkeypatch.setattr(wp, "time", fake_time)
    try:
        wp._watchdog_loop(owner_pid)
    except _StopLoop:
        pass
    return slept


def test_watchdog_poll_interval_is_clamped(monkeypatch) -> None:
    import os

    monkeypatch.setattr(wp, "_worker", None)
    monkeypatch.setattr(wp, "_worker_idle_timeout_seconds", 0.0)
    assert _run_watchdog(monkeypatch, os.getpid(), 1) == [1.0]  # disabled -> max poll

    monkeypatch.setattr(wp, "_worker_idle_timeout_seconds", 0.01)
    assert _run_watchdog(monkeypatch, os.getpid(), 1) == [0.02]  # tiny -> min poll

    monkeypatch.setattr(wp, "_worker_idle_timeout_seconds", 5.0)
    assert _run_watchdog(monkeypatch, os.getpid(), 1) == [0.5]

    monkeypatch.setattr(wp, "_worker_idle_timeout_seconds", 3600.0)
    assert _run_watchdog(monkeypatch, os.getpid(), 1) == [1.0]  # huge -> max poll


def test_watchdog_exits_in_a_process_that_does_not_own_it(monkeypatch) -> None:
    monkeypatch.setattr(wp, "_worker_idle_timeout_seconds", 1.0)

    slept = _run_watchdog(monkeypatch, owner_pid=-1, iterations=5)

    assert len(slept) == 1, "loop must return, not keep polling, after a fork"


def test_watchdog_retires_only_a_worker_idle_for_longer_than_the_timeout(monkeypatch) -> None:
    import os

    worker = _Worker(FakeProcess(), FakeConnection(), ("k",))
    discarded: list[_Worker] = []
    monkeypatch.setattr(wp, "_worker", worker)
    monkeypatch.setattr(wp, "_worker_idle_timeout_seconds", 10.0)
    monkeypatch.setattr(wp, "_discard_worker_locked", lambda w, graceful=False: discarded.append(w))

    # Fresh activity -> keep the worker.
    monkeypatch.setattr(wp, "_worker_last_active", time.monotonic())
    _run_watchdog(monkeypatch, os.getpid(), 2)
    assert discarded == []

    # In-flight request (no last_active) -> keep the worker.
    monkeypatch.setattr(wp, "_worker_last_active", None)
    _run_watchdog(monkeypatch, os.getpid(), 2)
    assert discarded == []

    # Idle for longer than the timeout -> retire it.
    monkeypatch.setattr(wp, "_worker_last_active", time.monotonic() - 60)
    _run_watchdog(monkeypatch, os.getpid(), 2)
    assert discarded and discarded[0] is worker
    monkeypatch.setattr(wp, "_worker", None)


def test_watchdog_leaves_a_busy_worker_alone_when_lock_is_held(monkeypatch) -> None:
    import os

    discarded: list[Any] = []
    monkeypatch.setattr(wp, "_worker", _Worker(FakeProcess(), FakeConnection(), ("k",)))
    monkeypatch.setattr(wp, "_worker_last_active", time.monotonic() - 60)
    monkeypatch.setattr(wp, "_worker_idle_timeout_seconds", 1.0)
    monkeypatch.setattr(wp, "_discard_worker_locked", lambda *a, **k: discarded.append(a))

    holder_ready = threading.Event()
    release = threading.Event()

    def hold() -> None:
        with wp._worker_lock:
            holder_ready.set()
            release.wait(5)

    thread = threading.Thread(target=hold)
    thread.start()
    try:
        assert holder_ready.wait(5)
        _run_watchdog(monkeypatch, os.getpid(), 1)
    finally:
        release.set()
        thread.join(5)

    assert discarded == []
    monkeypatch.setattr(wp, "_worker", None)
