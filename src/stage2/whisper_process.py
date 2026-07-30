"""Process-isolated Whisper execution with progress forwarding and a hard timeout."""
from __future__ import annotations

import multiprocessing
from multiprocessing.connection import Connection
import os
from pathlib import Path
import time
from typing import Any, Callable

from .whisper_runner import transcribe_audio


ProgressCallback = Callable[[float, float, int], None]
DEFAULT_TIMEOUT_SECONDS = 4 * 60 * 60


class WhisperProcessError(RuntimeError):
    """Raised when the isolated Whisper worker exits without a result."""


class WhisperTimeoutError(TimeoutError):
    """Raised after the isolated Whisper worker exceeds its deadline."""


def _configured_timeout(value: float | None) -> float:
    raw = value
    if raw is None:
        raw = os.getenv("SYNC_WHISPER_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT_SECONDS))
    try:
        timeout = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("SYNC_WHISPER_TIMEOUT_SECONDS must be a positive number") from exc
    if timeout <= 0:
        raise ValueError("SYNC_WHISPER_TIMEOUT_SECONDS must be a positive number")
    return timeout


def _transcribe_worker(
    connection: Connection,
    audio_path: str,
    backend: str | None,
    model: str | None,
    language: str,
) -> None:
    def report(processed_sec: float, duration_sec: float, word_count: int) -> None:
        connection.send(("progress", processed_sec, duration_sec, word_count))

    try:
        result = transcribe_audio(
            audio_path,
            backend=backend,
            model=model,
            language=language,
            progress_callback=report,
        )
        connection.send(("result", result))
    except BaseException as exc:
        try:
            connection.send(("error", type(exc).__name__, str(exc)))
        except (BrokenPipeError, EOFError, OSError):
            pass
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


def transcribe_audio_in_process(
    audio_path: str | Path,
    *,
    backend: str | None = None,
    model: str | None = None,
    language: str = "ru",
    progress_callback: ProgressCallback | None = None,
    timeout_seconds: float | None = None,
    _start_method: str = "spawn",
) -> dict[str, Any]:
    """Run one transcription in a killable child process."""
    timeout = _configured_timeout(timeout_seconds)
    context = multiprocessing.get_context(_start_method)
    receive, send = context.Pipe(duplex=False)
    process = context.Process(
        target=_transcribe_worker,
        args=(send, str(Path(audio_path).resolve()), backend, model, language),
        daemon=True,
    )
    deadline = time.monotonic() + timeout
    started = False
    result: dict[str, Any] | None = None
    worker_error = ""
    timed_out = False
    try:
        process.start()
        started = True
        send.close()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            if receive.poll(min(0.25, remaining)):
                try:
                    message = receive.recv()
                except EOFError:
                    break
                kind = message[0] if message else ""
                if kind == "progress":
                    if progress_callback is not None:
                        try:
                            progress_callback(
                                float(message[1]),
                                float(message[2]),
                                int(message[3]),
                            )
                        except Exception:
                            pass
                elif kind == "result":
                    result = message[1]
                    break
                elif kind == "error":
                    worker_error = f"{message[1]}: {message[2]}"
                    break
            elif not process.is_alive():
                break

        if timed_out:
            _stop_process(process)
            raise WhisperTimeoutError(
                f"Whisper transcription exceeded {timeout:g} seconds"
            )
        process.join(timeout=2.0)
        if process.is_alive():
            _stop_process(process)
        if worker_error:
            raise WhisperProcessError(worker_error)
        if result is None:
            raise WhisperProcessError(
                f"Whisper worker exited without a result (exit code {process.exitcode})"
            )
        return result
    finally:
        send.close()
        receive.close()
        if started and process.is_alive():
            _stop_process(process)
