from __future__ import annotations

import io
import json
from pathlib import Path
import threading
import wave
from unittest.mock import patch

import pytest

from stage1.gemini_tts import TtsCancelledError, synthesize_tts


def _prepared(path: Path, *item_ids: str) -> Path:
    path.write_text(
        json.dumps(
            {"items": [{"id": item_id, "text_for_tts": f"text {item_id}"} for item_id in item_ids]}
        ),
        encoding="utf-8",
    )
    return path


def _wav_bytes() -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(24_000)
        output.writeframes(b"\x00\x00" * 240)
    return buffer.getvalue()


def _real_tts_patches(synthesize):
    return (
        patch("stage1.gemini_tts.shutil.which", return_value="/usr/bin/say"),
        patch("stage1.gemini_tts._synthesize_wav_bytes", side_effect=synthesize),
    )


def test_dry_run_progress_schema_and_order(tmp_path: Path) -> None:
    prepared = _prepared(tmp_path / "prepared.json", "zk1", "zk2")
    existing = tmp_path / "zk2.wav"
    existing.write_bytes(_wav_bytes())
    events: list[dict] = []

    result = synthesize_tts(prepared, tmp_path, dry_run=True, progress_callback=events.append)

    assert result["files_count"] == 1
    assert result["skipped_count"] == 1
    assert events == [
        {"event": "started", "total": 2, "completed": 0, "generated": 0, "skipped": 0},
        {
            "event": "item_started",
            "id": "zk1",
            "index": 1,
            "total": 2,
            "completed": 0,
            "generated": 0,
            "skipped": 0,
        },
        {
            "event": "item_generated",
            "id": "zk1",
            "index": 1,
            "total": 2,
            "completed": 1,
            "generated": 1,
            "skipped": 0,
            "attempts": 1,
        },
        {
            "event": "item_started",
            "id": "zk2",
            "index": 2,
            "total": 2,
            "completed": 1,
            "generated": 1,
            "skipped": 0,
        },
        {
            "event": "item_skipped",
            "id": "zk2",
            "index": 2,
            "total": 2,
            "completed": 2,
            "generated": 1,
            "skipped": 1,
        },
        {"event": "completed", "total": 2, "completed": 2, "generated": 1, "skipped": 1},
    ]


def test_cache_hit_progress_schema_and_order(tmp_path: Path) -> None:
    prepared = _prepared(tmp_path / "prepared.json", "zk1")
    which_patch, synthesize_patch = _real_tts_patches(lambda *_args, **_kwargs: _wav_bytes())
    with which_patch, synthesize_patch:
        synthesize_tts(prepared, tmp_path, provider="macos", voice_name="Yuri", gender="male")

    events: list[dict] = []
    with (
        patch("stage1.gemini_tts.shutil.which", return_value="/usr/bin/say"),
        patch("stage1.gemini_tts._synthesize_wav_bytes") as synthesize,
    ):
        result = synthesize_tts(
            prepared,
            tmp_path,
            provider="macos",
            voice_name="Yuri",
            gender="male",
            progress_callback=events.append,
        )

    synthesize.assert_not_called()
    assert result["files_count"] == 0
    assert result["skipped_count"] == 1
    assert events == [
        {"event": "started", "total": 1, "completed": 0, "generated": 0, "skipped": 0},
        {
            "event": "item_started",
            "id": "zk1",
            "index": 1,
            "total": 1,
            "completed": 0,
            "generated": 0,
            "skipped": 0,
        },
        {
            "event": "item_skipped",
            "id": "zk1",
            "index": 1,
            "total": 1,
            "completed": 1,
            "generated": 0,
            "skipped": 1,
        },
        {"event": "completed", "total": 1, "completed": 1, "generated": 0, "skipped": 1},
    ]


def test_mixed_pending_and_cache_progress_is_monotonic(tmp_path: Path) -> None:
    cached_prepared = _prepared(tmp_path / "cached.json", "zk2")
    which_patch, synthesize_patch = _real_tts_patches(lambda *_args, **_kwargs: _wav_bytes())
    with which_patch, synthesize_patch:
        synthesize_tts(
            cached_prepared,
            tmp_path,
            provider="macos",
            voice_name="Yuri",
            gender="male",
        )

    prepared = _prepared(tmp_path / "prepared.json", "zk1", "zk2")
    events: list[dict] = []
    which_patch, synthesize_patch = _real_tts_patches(lambda *_args, **_kwargs: _wav_bytes())
    with which_patch, synthesize_patch:
        synthesize_tts(
            prepared,
            tmp_path,
            provider="macos",
            voice_name="Yuri",
            gender="male",
            progress_callback=events.append,
        )

    completed = [event["completed"] for event in events]
    assert completed == sorted(completed)
    assert [event.get("id") for event in events if event["event"] == "item_started"] == [
        "zk1",
        "zk2",
    ]
    assert [event["event"] for event in events] == [
        "started",
        "item_started",
        "item_generated",
        "item_started",
        "item_skipped",
        "completed",
    ]


def test_retry_then_success_progress_schema_and_order(tmp_path: Path) -> None:
    prepared = _prepared(tmp_path / "prepared.json", "zk1")
    events: list[dict] = []
    attempts = iter([TimeoutError("request timed out"), _wav_bytes()])

    def synthesize(*_args, **_kwargs):
        outcome = next(attempts)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    which_patch, synthesize_patch = _real_tts_patches(synthesize)
    with which_patch, synthesize_patch, patch("stage1.gemini_tts.time.sleep") as sleep:
        result = synthesize_tts(
            prepared,
            tmp_path,
            provider="macos",
            voice_name="Yuri",
            gender="male",
            progress_callback=events.append,
        )

    sleep.assert_called_once_with(8)
    assert result["files_count"] == 1
    assert events == [
        {"event": "started", "total": 1, "completed": 0, "generated": 0, "skipped": 0},
        {
            "event": "item_started",
            "id": "zk1",
            "index": 1,
            "total": 1,
            "completed": 0,
            "generated": 0,
            "skipped": 0,
        },
        {
            "event": "item_retrying",
            "id": "zk1",
            "index": 1,
            "total": 1,
            "completed": 0,
            "generated": 0,
            "skipped": 0,
            "attempts": 1,
            "error": "request timed out",
        },
        {
            "event": "item_generated",
            "id": "zk1",
            "index": 1,
            "total": 1,
            "completed": 1,
            "generated": 1,
            "skipped": 0,
            "attempts": 2,
        },
        {"event": "completed", "total": 1, "completed": 1, "generated": 1, "skipped": 0},
    ]


def test_terminal_failure_progress_schema_and_order(tmp_path: Path) -> None:
    prepared = _prepared(tmp_path / "prepared.json", "zk1")
    events: list[dict] = []

    def fail(*_args, **_kwargs):
        raise ValueError("permanent failure")

    which_patch, synthesize_patch = _real_tts_patches(fail)
    with which_patch, synthesize_patch, patch("stage1.gemini_tts.time.sleep") as sleep:
        with pytest.raises(ValueError, match="permanent failure"):
            synthesize_tts(
                prepared,
                tmp_path,
                provider="macos",
                voice_name="Yuri",
                gender="male",
                progress_callback=events.append,
            )

    sleep.assert_not_called()
    assert events == [
        {"event": "started", "total": 1, "completed": 0, "generated": 0, "skipped": 0},
        {
            "event": "item_started",
            "id": "zk1",
            "index": 1,
            "total": 1,
            "completed": 0,
            "generated": 0,
            "skipped": 0,
        },
        {
            "event": "item_failed",
            "id": "zk1",
            "index": 1,
            "total": 1,
            "completed": 0,
            "generated": 0,
            "skipped": 0,
            "attempts": 1,
            "error": "permanent failure",
        },
    ]


def test_cancellation_progress_schema_and_order(tmp_path: Path) -> None:
    prepared = _prepared(tmp_path / "prepared.json", "zk1")
    events: list[dict] = []
    provider_can_finish = threading.Event()
    cancelled = threading.Event()

    def record(event: dict) -> None:
        events.append(event)
        if event["event"] == "item_started":
            provider_can_finish.set()

    def synthesize(*_args, **_kwargs):
        assert provider_can_finish.wait(timeout=1)
        cancelled.set()
        return _wav_bytes()

    which_patch, synthesize_patch = _real_tts_patches(synthesize)
    with which_patch, synthesize_patch, patch("stage1.gemini_tts.time.sleep") as sleep:
        with pytest.raises(TtsCancelledError, match="cancelled"):
            synthesize_tts(
                prepared,
                tmp_path,
                provider="macos",
                voice_name="Yuri",
                gender="male",
                progress_callback=record,
                cancel_check=cancelled.is_set,
            )

    sleep.assert_not_called()
    assert events == [
        {"event": "started", "total": 1, "completed": 0, "generated": 0, "skipped": 0},
        {
            "event": "item_started",
            "id": "zk1",
            "index": 1,
            "total": 1,
            "completed": 0,
            "generated": 0,
            "skipped": 0,
        },
    ]


def test_cancellation_drops_queued_provider_requests(tmp_path: Path) -> None:
    prepared = _prepared(tmp_path / "prepared.json", "zk1", "zk2")
    cancelled = threading.Event()
    release_provider = threading.Event()
    calls: list[str] = []

    def synthesize(_provider, text, *_args, **_kwargs):
        calls.append(text)
        cancelled.set()
        assert release_provider.wait(timeout=2)
        return _wav_bytes()

    which_patch, synthesize_patch = _real_tts_patches(synthesize)
    try:
        with which_patch, synthesize_patch, patch.dict("os.environ", {"TTS_MAX_WORKERS": "1"}):
            with pytest.raises(TtsCancelledError, match="cancelled"):
                synthesize_tts(
                    prepared,
                    tmp_path,
                    provider="macos",
                    voice_name="Yuri",
                    gender="male",
                    cancel_check=cancelled.is_set,
                )
    finally:
        release_provider.set()

    assert calls == ["text zk1"]


def test_parallel_completion_still_publishes_progress_in_scenario_order(
    tmp_path: Path,
) -> None:
    prepared = _prepared(tmp_path / "prepared.json", "zk1", "zk2")
    events: list[dict] = []
    second_finished = threading.Event()
    provider_completion_order: list[str] = []
    lock = threading.Lock()

    def synthesize(_provider, text, *_args, **_kwargs):
        if text == "text zk1":
            assert second_finished.wait(timeout=1)
        with lock:
            provider_completion_order.append(text)
        if text == "text zk2":
            second_finished.set()
        return _wav_bytes()

    which_patch, synthesize_patch = _real_tts_patches(synthesize)
    with which_patch, synthesize_patch, patch.dict("os.environ", {"TTS_MAX_WORKERS": "2"}):
        synthesize_tts(
            prepared,
            tmp_path,
            provider="macos",
            voice_name="Yuri",
            gender="male",
            progress_callback=events.append,
        )

    assert provider_completion_order == ["text zk2", "text zk1"]
    assert events == [
        {"event": "started", "total": 2, "completed": 0, "generated": 0, "skipped": 0},
        {
            "event": "item_started",
            "id": "zk1",
            "index": 1,
            "total": 2,
            "completed": 0,
            "generated": 0,
            "skipped": 0,
        },
        {
            "event": "item_generated",
            "id": "zk1",
            "index": 1,
            "total": 2,
            "completed": 1,
            "generated": 1,
            "skipped": 0,
            "attempts": 1,
        },
        {
            "event": "item_started",
            "id": "zk2",
            "index": 2,
            "total": 2,
            "completed": 1,
            "generated": 1,
            "skipped": 0,
        },
        {
            "event": "item_generated",
            "id": "zk2",
            "index": 2,
            "total": 2,
            "completed": 2,
            "generated": 2,
            "skipped": 0,
            "attempts": 1,
        },
        {"event": "completed", "total": 2, "completed": 2, "generated": 2, "skipped": 0},
    ]
