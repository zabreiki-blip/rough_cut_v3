from __future__ import annotations

import threading
from pathlib import Path

import pytest

from api_sync_worker import make_window_transcriber
from stage2.whisper_runner import NO_WORDS_ERROR_MARKER, WhisperTranscriptionError


class _Cancelled(Exception):
    pass


def _make(transcribe, *, cancel: threading.Event | None = None, semaphore=None):
    cancel = cancel or threading.Event()
    semaphore = semaphore or threading.Semaphore(1)

    def ensure_not_cancelled() -> None:
        if cancel.is_set():
            raise _Cancelled("cancelled")

    messages: list[dict] = []
    fn = make_window_transcriber(
        transcribe,
        asr_semaphore=semaphore,
        cancel_event=cancel,
        ensure_not_cancelled=ensure_not_cancelled,
        language="ru",
        update_job=lambda job_id, **kw: messages.append(kw),
        job_id="job",
        timeout_seconds=7.0,
    )
    return fn, cancel, semaphore, messages


def test_passes_cancel_event_and_releases_slot() -> None:
    calls = []

    def transcribe(clip, **kwargs):
        calls.append((clip, kwargs))
        return {"words": [{"word": "x", "start": 0, "end": 1}]}

    fn, cancel, semaphore, _ = _make(transcribe)
    assert fn(Path("c.wav"), "small") == [{"word": "x", "start": 0, "end": 1}]
    ((clip, kwargs),) = calls
    assert kwargs["cancel_event"] is cancel
    assert kwargs["backend"] == "faster-whisper"
    assert kwargs["model"] == "small" and kwargs["language"] == "ru"
    assert kwargs["timeout_seconds"] == 7.0
    assert semaphore.acquire(blocking=False)  # slot was released


def test_cancelled_before_slot_never_transcribes() -> None:
    called = []
    fn, cancel, semaphore, _ = _make(lambda *a, **k: called.append(1) or {})
    cancel.set()
    with pytest.raises(_Cancelled):
        fn(Path("c.wav"), "small")
    assert not called
    assert semaphore.acquire(blocking=False)


def test_cancel_while_waiting_for_slot_aborts() -> None:
    semaphore = threading.Semaphore(1)
    semaphore.acquire()  # another job holds the only slot
    called = []
    fn, cancel, _, messages = _make(lambda *a, **k: called.append(1) or {}, semaphore=semaphore)
    threading.Timer(0.1, cancel.set).start()
    with pytest.raises(_Cancelled):
        fn(Path("c.wav"), "small")
    assert not called
    assert {"message": "Waiting for an ASR slot", "asr_waiting": True} in messages


def test_no_words_error_is_empty_window_and_slot_is_released() -> None:
    def transcribe(clip, **kwargs):
        raise WhisperTranscriptionError(f"backend {NO_WORDS_ERROR_MARKER}")

    fn, _, semaphore, _ = _make(transcribe)
    assert fn(Path("c.wav"), "small") == []
    assert semaphore.acquire(blocking=False)


def test_other_errors_propagate_and_release_slot() -> None:
    def transcribe(clip, **kwargs):
        raise RuntimeError("engine down")

    fn, _, semaphore, _ = _make(transcribe)
    with pytest.raises(RuntimeError):
        fn(Path("c.wav"), "small")
    assert semaphore.acquire(blocking=False)
