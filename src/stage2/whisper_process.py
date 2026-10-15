"""Process-isolated Whisper execution with progress forwarding and a hard timeout."""

from __future__ import annotations

import atexit
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
import math
import multiprocessing
from multiprocessing.connection import Connection
import os
from pathlib import Path
import threading
import time
from typing import Any, Callable

from .whisper_runner import transcribe_audio


ProgressCallback = Callable[[float, float, int], None]
DEFAULT_TIMEOUT_SECONDS = 4 * 60 * 60
DEFAULT_IDLE_TIMEOUT_SECONDS = 15 * 60
_UNLOAD_AFTER_JOB_ENV = "SYNC_WHISPER_UNLOAD_AFTER_JOB"
_IDLE_TIMEOUT_ENV = "SYNC_WHISPER_IDLE_TIMEOUT_SECONDS"
# Watchdog poll interval bounds. The watchdog sleeps for an interval computed
# from the configured idle timeout (idle_timeout / 10) to balance responsiveness
# against battery drain: idle workers are checked frequently when timeouts are
# short (responding to recently-lowered limits), but infrequently when disabled
# or timeouts are long. The interval is clamped to [0.02, 1.0] seconds.
_WATCHDOG_MIN_POLL_SECONDS = 0.02
_WATCHDOG_MAX_POLL_SECONDS = 1.0


class WhisperProcessError(RuntimeError):
    """Raised when the isolated Whisper worker exits without a result."""


class WhisperTimeoutError(TimeoutError):
    """Raised after the isolated Whisper worker exceeds its deadline."""


class WhisperCancelledError(RuntimeError):
    """Raised after a caller cooperatively cancels active inference."""


@dataclass
class _Worker:
    process: Any
    connection: Connection
    key: tuple[str, ...]


_worker_lock = threading.Lock()
_worker: _Worker | None = None
_worker_owner_pid = os.getpid()
_request_number = 0
# Monotonic timestamp of the last time the cached worker finished a request,
# or None while no worker is cached / one is currently in flight.
_worker_last_active: float | None = None
# Idle timeout the watchdog thread currently applies, refreshed on every
# request (see `_configured_idle_timeout`) rather than cached at import time.
_worker_idle_timeout_seconds = float(DEFAULT_IDLE_TIMEOUT_SECONDS)
_watchdog_thread: threading.Thread | None = None


def _after_fork_child() -> None:
    """Drop parent-owned IPC state after an application process forks."""
    global _request_number, _worker, _worker_lock, _worker_owner_pid
    global _worker_last_active, _watchdog_thread
    if _worker is not None:
        _worker.connection.close()
    _worker = None
    _worker_lock = threading.Lock()
    _worker_owner_pid = os.getpid()
    _request_number = 0
    _worker_last_active = None
    # The watchdog thread object does not survive a fork as a running OS
    # thread (only the calling thread does); forget it so the next worker
    # lazily starts a fresh watchdog scoped to this child process.
    _watchdog_thread = None


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork_child)


def _configured_timeout(value: float | None) -> float:
    raw: float | str = (
        value
        if value is not None
        else os.getenv("SYNC_WHISPER_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT_SECONDS))
    )
    try:
        timeout = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("SYNC_WHISPER_TIMEOUT_SECONDS must be a positive number") from exc
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("SYNC_WHISPER_TIMEOUT_SECONDS must be a positive number")
    return timeout


def _configured_idle_timeout() -> float:
    raw = os.getenv(_IDLE_TIMEOUT_ENV, str(DEFAULT_IDLE_TIMEOUT_SECONDS))
    try:
        idle_timeout = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{_IDLE_TIMEOUT_ENV} must be a non-negative number") from exc
    if idle_timeout < 0:
        raise ValueError(f"{_IDLE_TIMEOUT_ENV} must be a non-negative number")
    return idle_timeout


def _unload_after_job() -> bool:
    """Return whether a successful request should release the cached model process.

    Keeping the worker alive is the historical default: it makes later requests
    faster because the model stays loaded.  Local installations that prefer
    memory over warm-start speed can opt in to retiring it after each success.
    """
    raw = os.getenv(_UNLOAD_AFTER_JOB_ENV, "").strip().lower()
    if raw in {"", "0", "false", "no", "off"}:
        return False
    if raw in {"1", "true", "yes", "on"}:
        return True
    raise ValueError(f"{_UNLOAD_AFTER_JOB_ENV} must be one of: 1, true, yes, on, 0, false, no, off")


def _transcribe_worker(connection: Connection) -> None:
    """Serve transcription requests until the parent retires this worker."""
    try:
        while True:
            try:
                command = connection.recv()
            except EOFError:
                break
            if not command or command[0] == "stop":
                break
            if command[0] != "transcribe":
                continue

            _, request_id, audio_path, backend, model, language, cloud_options = command

            def report(
                processed_sec: float,
                duration_sec: float,
                word_count: int,
            ) -> None:
                connection.send(
                    (
                        "progress",
                        request_id,
                        processed_sec,
                        duration_sec,
                        word_count,
                    )
                )

            def report_budget(snapshot: dict[str, float]) -> None:
                connection.send(("budget", request_id, snapshot))

            try:
                result = transcribe_audio(
                    audio_path,
                    backend=backend,
                    model=model,
                    language=language,
                    progress_callback=report,
                    cloud_options=cloud_options,
                    budget_callback=report_budget,
                )
                connection.send(("result", request_id, result))
            except BaseException as exc:
                try:
                    connection.send(
                        (
                            "error",
                            request_id,
                            type(exc).__name__,
                            str(exc),
                            getattr(exc, "cost_usd", None),
                            getattr(exc, "reserved_cost_usd", None),
                            getattr(exc, "cost_limit_usd", None),
                        )
                    )
                except (BrokenPipeError, EOFError, OSError):
                    pass
                # A failed inference may leave a backend/model in an unknown state.
                # Exit so the next request receives a clean process and IPC channel.
                break
    finally:
        connection.close()


def _stop_process(process, grace_seconds: float = 2.0) -> None:
    if not process.is_alive():
        process.join(timeout=0.1)
        return
    process.terminate()
    process.join(timeout=grace_seconds)
    if process.is_alive():
        kill = getattr(process, "kill", None)
        if callable(kill):
            kill()
        process.join(timeout=grace_seconds)


def _retire_worker(worker: _Worker, *, graceful: bool = False) -> None:
    if graceful and worker.process.is_alive():
        try:
            worker.connection.send(("stop",))
            worker.process.join(timeout=0.5)
        except (BrokenPipeError, EOFError, OSError):
            pass
    worker.connection.close()
    if worker.process.is_alive():
        _stop_process(worker.process)
    else:
        worker.process.join(timeout=0.1)


def _discard_worker_locked(worker: _Worker, *, graceful: bool = False) -> None:
    global _worker, _worker_last_active
    if _worker is worker:
        _worker = None
        _worker_last_active = None
    _retire_worker(worker, graceful=graceful)


def _watchdog_loop(owner_pid: int) -> None:
    """Retire the cached worker once it has sat idle past the configured timeout.

    A single daemon thread per process runs this loop for the whole process
    lifetime. It never fights an active transcription for the worker: the
    lock is acquired with a short timeout, so a busy worker is simply left
    alone until the next poll.
    """
    while True:
        idle_timeout = _worker_idle_timeout_seconds
        # Compute adaptive poll interval from the configured idle timeout to
        # balance responsiveness (short sleeps for short timeouts) against
        # battery drain (long sleeps when timeouts are long or disabled).
        if idle_timeout > 0:
            poll_interval = max(
                _WATCHDOG_MIN_POLL_SECONDS,
                min(_WATCHDOG_MAX_POLL_SECONDS, idle_timeout / 10),
            )
        else:
            # When auto-unload is disabled (idle_timeout <= 0), sleep longer
            # but keep polling in case the setting changes.
            poll_interval = _WATCHDOG_MAX_POLL_SECONDS
        time.sleep(poll_interval)
        if os.getpid() != owner_pid:
            # This process forked; the OS thread backing this loop no longer
            # exists in the child, and `_after_fork_child` already started a
            # fresh watchdog for it. Stop pretending to be alive here.
            return
        if idle_timeout <= 0:
            continue  # 0 disables auto-unload; keep polling in case it changes
        acquired = _worker_lock.acquire(timeout=0.05)
        if not acquired:
            continue  # an active transcription owns the lock; do not disturb it
        try:
            worker = _worker
            last_active = _worker_last_active
            if worker is None or last_active is None:
                continue
            if time.monotonic() - last_active >= idle_timeout:
                _discard_worker_locked(worker, graceful=True)
        finally:
            _worker_lock.release()


def _ensure_watchdog_started_locked() -> None:
    """Lazily start the one-per-process idle watchdog when a worker is created."""
    global _watchdog_thread
    if _watchdog_thread is not None and _watchdog_thread.is_alive():
        return
    thread = threading.Thread(
        target=_watchdog_loop,
        args=(os.getpid(),),
        daemon=True,
        name="whisper-idle-watchdog",
    )
    _watchdog_thread = thread
    thread.start()


def _start_worker_locked(
    key: tuple[str, ...],
) -> _Worker:
    context = multiprocessing.get_context(key[-1])
    parent_connection, child_connection = context.Pipe(duplex=True)
    # typeshed's get_context(str) return type is BaseContext, which omits the
    # Process attribute that all concrete context subclasses actually provide.
    process = context.Process(  # type: ignore[attr-defined]
        target=_transcribe_worker,
        args=(child_connection,),
        daemon=True,
    )
    try:
        process.start()
    except BaseException:
        parent_connection.close()
        child_connection.close()
        raise
    child_connection.close()
    # Lazily ensure the idle watchdog is running before handing back a worker
    # that may otherwise sit warm and unattended for the rest of the session.
    _ensure_watchdog_started_locked()
    return _Worker(process=process, connection=parent_connection, key=key)


def _get_worker_locked(
    key: tuple[str, ...],
) -> _Worker:
    global _worker
    if _worker is not None and (_worker.key != key or not _worker.process.is_alive()):
        _discard_worker_locked(_worker, graceful=_worker.process.is_alive())
    if _worker is None:
        _worker = _start_worker_locked(key)
    return _worker


def _shutdown_worker() -> None:
    """Stop the cached worker (also used by tests and interpreter cleanup)."""
    global _worker, _worker_last_active

    # A forked child can inherit the parent's bookkeeping, but does not own its
    # Process object or pipe endpoint.
    if os.getpid() != _worker_owner_pid:
        return
    # Do not let interpreter shutdown wait behind a multi-hour inference. If a
    # daemon job thread owns the lock, retire its process directly; shutdown is
    # already terminal and the bounded stop path is safer than hanging a deploy.
    acquired = _worker_lock.acquire(timeout=0.1)
    try:
        worker = _worker
        _worker = None
        _worker_last_active = None
        if worker is not None:
            _retire_worker(worker, graceful=acquired)
    finally:
        if acquired:
            _worker_lock.release()


atexit.register(_shutdown_worker)


def _worker_key(
    backend: str | None,
    model: str | None,
    language: str,
    start_method: str,
) -> tuple[str, ...]:
    """Include every setting that can change the cached model or pipeline."""
    return (
        str(backend or os.getenv("SYNC_WHISPER_BACKEND", "auto")).strip().lower(),
        str(model or os.getenv("SYNC_WHISPER_MODEL", "")).strip(),
        language,
        os.getenv("SYNC_WHISPER_DEVICE", "auto").strip().lower(),
        os.getenv("SYNC_WHISPER_COMPUTE_TYPE", "default").strip().lower(),
        os.getenv("SYNC_WHISPER_BEAM_SIZE", "5").strip(),
        os.getenv("SYNC_WHISPER_BEST_OF", "5").strip(),
        os.getenv("SYNC_WHISPER_BATCH_SIZE", "0").strip(),
        os.getenv("SYNC_WHISPER_CPU_THREADS", "0").strip(),
        start_method,
    )


def transcribe_audio_in_process(
    audio_path: str | Path,
    *,
    backend: str | None = None,
    model: str | None = None,
    language: str = "ru",
    progress_callback: ProgressCallback | None = None,
    cancel_event: Any | None = None,
    timeout_seconds: float | None = None,
    cloud_options: Mapping[str, Any] | None = None,
    deadline_monotonic: float | None = None,
    _start_method: str = "spawn",
) -> dict[str, Any]:
    """Transcribe in a reusable, killable child process.

    An absolute whole-job deadline includes time queued behind another request.
    The configured Whisper timeout continues to bound inference itself. Parent
    cancellation and either deadline terminate even an in-flight cloud request.
    Matching model configurations reuse a worker unless auto-unload is enabled.
    """
    global _request_number, _worker_last_active, _worker_idle_timeout_seconds

    timeout = _configured_timeout(timeout_seconds)
    unload_after_job = _unload_after_job()
    idle_timeout = _configured_idle_timeout()
    key = _worker_key(backend, model, language, _start_method)
    if deadline_monotonic is not None and not math.isfinite(deadline_monotonic):
        raise ValueError("deadline_monotonic must be finite")
    is_cloud = key[0] in {"cloud-audio", "cloud", "openrouter"}
    # Preserve Decimal precision when values cross the process boundary. These
    # are per-request options, so changing a budget never replaces a warm model.
    options: dict[str, Any] | None = None
    last_budget = {"spent_usd": 0.0, "reserved_usd": 0.0, "limit_usd": 0.0}
    configured_cloud_timeout = timeout
    if is_cloud:
        options = {
            name: str(value) if isinstance(value, Decimal) else value
            for name, value in (cloud_options or {}).items()
        }
        # A warm worker inherited its environment at startup. Resolve mutable
        # defaults in the parent on every request, including disabled budgets.
        for option, variable in (
            ("max_cost_usd", "ROUGH_CUT_MAX_USD_PER_JOB"),
            ("usd_per_minute_estimate", "ROUGH_CUT_CLOUD_ASR_USD_PER_MINUTE_ESTIMATE"),
        ):
            if options.get(option) is None:
                options[option] = os.getenv(variable, "0").strip() or "0"
        last_budget["spent_usd"] = float(options.get("initial_cost_usd") or 0)
        last_budget["limit_usd"] = float(options.get("max_cost_usd") or 0)
        if options.get("timeout_sec") is not None:
            configured_cloud_timeout = float(options["timeout_sec"])
            if not math.isfinite(configured_cloud_timeout) or configured_cloud_timeout <= 0:
                raise ValueError("cloud timeout_sec must be a finite positive number")
    deadline = deadline_monotonic
    request_id: int | None = None

    def attach_budget(error: BaseException) -> BaseException:
        error.cost_usd = last_budget["spent_usd"]  # type: ignore[attr-defined]
        error.reserved_cost_usd = last_budget["reserved_usd"]  # type: ignore[attr-defined]
        error.cost_limit_usd = last_budget["limit_usd"]  # type: ignore[attr-defined]
        return error

    def check_stop(worker: _Worker | None = None) -> None:
        nonlocal last_budget
        error: BaseException | None = None
        if cancel_event is not None and cancel_event.is_set():
            error = WhisperCancelledError("Whisper transcription cancelled")
        elif deadline is not None and time.monotonic() >= deadline:
            error = WhisperTimeoutError(f"Whisper transcription exceeded {timeout:g} seconds")
        if error is not None:
            if worker is not None:
                # Stop dispatch first, then retain accounting already written
                # to the pipe, including updates queued behind UI progress.
                _stop_process(worker.process)
                try:
                    while worker.connection.poll(0):
                        message = worker.connection.recv()
                        if message[1] != request_id:
                            continue
                        if message[0] == "budget":
                            last_budget = dict(message[2])
                        elif message[0] == "error":
                            for index, field in enumerate(
                                ("spent_usd", "reserved_usd", "limit_usd"), start=4
                            ):
                                if len(message) > index and message[index] is not None:
                                    last_budget[field] = message[index]
                except (EOFError, OSError):
                    pass
                _discard_worker_locked(worker)
            raise attach_budget(error)

    # Only the lock owner may retire a worker. A queued caller can stop without
    # disturbing another project's active inference.
    while True:
        check_stop()
        wait = 0.05
        if deadline is not None:
            wait = min(wait, max(0.0, deadline - time.monotonic()))
        if _worker_lock.acquire(timeout=wait):
            break
    try:
        check_stop()
        inference_deadline = time.monotonic() + timeout
        deadline = min(deadline, inference_deadline) if deadline is not None else inference_deadline
        _worker_idle_timeout_seconds = idle_timeout
        worker = _get_worker_locked(key)
        check_stop(worker)
        # Cloud requests get a cooperative timeout too; the parent remains the
        # hard stop and kills the child if its request does not return in time.
        if options is not None:
            options["timeout_sec"] = min(deadline - time.monotonic(), configured_cloud_timeout)
        _request_number += 1
        request_id = _request_number
        try:
            worker.connection.send(
                (
                    "transcribe",
                    request_id,
                    str(Path(audio_path).resolve()),
                    backend,
                    model,
                    language,
                    options,
                )
            )
        except (BrokenPipeError, EOFError, OSError) as exc:
            _discard_worker_locked(worker)
            raise attach_budget(
                WhisperProcessError("Whisper worker IPC connection failed")
            ) from exc

        while True:
            check_stop(worker)
            remaining = deadline - time.monotonic()
            try:
                response_ready = worker.connection.poll(min(0.05, max(0.0, remaining)))
            except (EOFError, OSError):
                response_ready = False
            if response_ready:
                try:
                    message = worker.connection.recv()
                except (EOFError, OSError):
                    message = None
                if not message:
                    exitcode = worker.process.exitcode
                    _discard_worker_locked(worker)
                    raise attach_budget(
                        WhisperProcessError(
                            f"Whisper worker exited without a result (exit code {exitcode})"
                        )
                    )

                kind = message[0]
                message_request_id = message[1]
                if message_request_id != request_id:
                    _discard_worker_locked(worker)
                    raise attach_budget(
                        WhisperProcessError("Whisper worker returned an unexpected response")
                    )
                if kind == "budget":
                    last_budget = dict(message[2])
                elif kind == "progress":
                    if progress_callback is not None:
                        try:
                            progress_callback(float(message[2]), float(message[3]), int(message[4]))
                        except Exception:
                            pass
                elif kind == "result":
                    check_stop(worker)
                    result = message[2]
                    if unload_after_job:
                        _discard_worker_locked(worker, graceful=True)
                    else:
                        _worker_last_active = time.monotonic()
                    return result
                elif kind == "error":
                    worker_error = f"{message[2]}: {message[3]}"
                    _discard_worker_locked(worker)
                    error = attach_budget(WhisperProcessError(worker_error))
                    for index, field in enumerate(
                        ("cost_usd", "reserved_cost_usd", "cost_limit_usd"), start=4
                    ):
                        if len(message) > index and message[index] is not None:
                            setattr(error, field, message[index])
                    raise error
                else:
                    _discard_worker_locked(worker)
                    raise attach_budget(
                        WhisperProcessError(f"Whisper worker returned an unknown message: {kind!r}")
                    )
            elif not worker.process.is_alive():
                exitcode = worker.process.exitcode
                _discard_worker_locked(worker)
                raise attach_budget(
                    WhisperProcessError(
                        f"Whisper worker exited without a result (exit code {exitcode})"
                    )
                )
    finally:
        _worker_lock.release()
