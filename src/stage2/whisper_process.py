"""Process-isolated Whisper execution with progress forwarding and a hard timeout."""

from __future__ import annotations

import atexit
from dataclasses import dataclass
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


def _after_fork_child() -> None:
    """Drop parent-owned IPC state after an application process forks."""
    global _request_number, _worker, _worker_lock, _worker_owner_pid
    if _worker is not None:
        _worker.connection.close()
    _worker = None
    _worker_lock = threading.Lock()
    _worker_owner_pid = os.getpid()
    _request_number = 0


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
    if timeout <= 0:
        raise ValueError("SYNC_WHISPER_TIMEOUT_SECONDS must be a positive number")
    return timeout


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

            _, request_id, audio_path, backend, model, language = command

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

            try:
                result = transcribe_audio(
                    audio_path,
                    backend=backend,
                    model=model,
                    language=language,
                    progress_callback=report,
                )
                connection.send(("result", request_id, result))
            except BaseException as exc:
                try:
                    connection.send(("error", request_id, type(exc).__name__, str(exc)))
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
    global _worker
    if _worker is worker:
        _worker = None
    _retire_worker(worker, graceful=graceful)


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
    global _worker

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
    _start_method: str = "spawn",
) -> dict[str, Any]:
    """Transcribe in a reusable, killable child process.

    Sequential requests with the same backend/model/language/start-method share
    one process, allowing ``whisper_runner``'s in-process model cache to work.
    """
    global _request_number

    timeout = _configured_timeout(timeout_seconds)
    key = _worker_key(backend, model, language, _start_method)

    # One pipe carries one request at a time. Holding the lock until the result
    # also prevents callers from creating redundant model processes.
    with _worker_lock:
        # Queueing behind another project is not inference time. Start the hard
        # deadline only after this request owns the worker slot.
        deadline = time.monotonic() + timeout
        worker = _get_worker_locked(key)
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
                )
            )
        except (BrokenPipeError, EOFError, OSError) as exc:
            _discard_worker_locked(worker)
            raise WhisperProcessError("Whisper worker IPC connection failed") from exc

        while True:
            if cancel_event is not None and cancel_event.is_set():
                _discard_worker_locked(worker)
                raise WhisperCancelledError("Whisper transcription cancelled")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _discard_worker_locked(worker)
                raise WhisperTimeoutError(f"Whisper transcription exceeded {timeout:g} seconds")
            try:
                response_ready = worker.connection.poll(min(0.25, remaining))
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
                    raise WhisperProcessError(
                        f"Whisper worker exited without a result (exit code {exitcode})"
                    )

                kind = message[0]
                message_request_id = message[1]
                if message_request_id != request_id:
                    _discard_worker_locked(worker)
                    raise WhisperProcessError("Whisper worker returned an unexpected response")
                if kind == "progress":
                    if progress_callback is not None:
                        try:
                            progress_callback(
                                float(message[2]),
                                float(message[3]),
                                int(message[4]),
                            )
                        except Exception:
                            pass
                elif kind == "result":
                    return message[2]
                elif kind == "error":
                    worker_error = f"{message[2]}: {message[3]}"
                    _discard_worker_locked(worker)
                    raise WhisperProcessError(worker_error)
                else:
                    _discard_worker_locked(worker)
                    raise WhisperProcessError(
                        f"Whisper worker returned an unknown message: {kind!r}"
                    )
            elif not worker.process.is_alive():
                exitcode = worker.process.exitcode
                _discard_worker_locked(worker)
                raise WhisperProcessError(
                    f"Whisper worker exited without a result (exit code {exitcode})"
                )
