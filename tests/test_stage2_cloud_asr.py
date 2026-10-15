"""Tests for the ported cloud ASR adapter (src/stage2/cloud_asr.py).

Covers only the pure parsing/stitching logic plus the public
``transcribe_cloud()`` entry point with a fully mocked network layer
(httpx transport) and mocked ffmpeg/ffprobe subprocess calls — no real
network access, no real ffmpeg/ffprobe binary required.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from stage2 import cloud_asr
from stage2.asr_contract import AsrSegment
from stage2.cloud_asr import (
    CloudAudioAsrAdapter,
    _parse_timecode,
    _parse_transcript_with_own_end,
    _reconcile_speakers,
    _rescale_stretched_timestamps,
    _stitch_chunk,
    plan_cloud_chunks,
    parse_speaker_legend,
    parse_transcript,
    transcribe_cloud,
)

# Captured at import time, BEFORE the autouse `_no_real_ffmpeg_ffprobe`
# fixture (below) monkeypatches `CloudAudioAsrAdapter._extract_chunk_mp3` for
# every test — needed by tests that exercise the *real* ffmpeg-subprocess
# logic (e.g. the ffmpeg-hang timeout test) despite the autouse stub.
_REAL_EXTRACT_CHUNK_MP3 = CloudAudioAsrAdapter._extract_chunk_mp3


# ---------------------------------------------------------------------------
# _parse_timecode
# ---------------------------------------------------------------------------


def test_parse_timecode_minutes_seconds() -> None:
    assert _parse_timecode("1", "23", None) == 83.0


def test_parse_timecode_hours_minutes_seconds() -> None:
    assert _parse_timecode("1", "02", "03") == 3723.0


def test_parse_timecode_with_fraction() -> None:
    assert _parse_timecode("1", "23", None, ".4") == pytest.approx(83.4)


def test_parse_timecode_zero() -> None:
    assert _parse_timecode("0", "00", None) == 0.0


# ---------------------------------------------------------------------------
# parse_transcript / _parse_transcript_with_own_end
# ---------------------------------------------------------------------------


def test_parse_transcript_old_format_start_only() -> None:
    text = "[00:01] ГОЛОС 1: Привет\n[00:05] ГОЛОС 2: Как дела"
    segments = parse_transcript(text)
    assert [s.text for s in segments] == ["Привет", "Как дела"]
    assert segments[0].start == 1.0
    # old format has no own end -> approximated by next segment's start.
    assert segments[0].end == 5.0
    assert segments[0].speaker == "ГОЛОС 1"


def test_parse_transcript_new_format_explicit_end_flag() -> None:
    text = "[00:01.0 — 00:04.5] ГОЛОС 1: Привет, как дела\n[00:05.0 — 00:06.0] ГОЛОС 2: Хорошо"
    segments, own_end = _parse_transcript_with_own_end(text)
    assert segments[0].start == pytest.approx(1.0)
    assert segments[0].end == pytest.approx(4.5)
    assert own_end[0] is True
    assert segments[1].end == pytest.approx(6.0)
    assert own_end[1] is True


def test_parse_transcript_offset_sec_applied() -> None:
    text = "[00:01] ГОЛОС 1: Привет"
    segments = parse_transcript(text, offset_sec=100.0)
    assert segments[0].start == 101.0


def test_parse_transcript_empty_speaker_label_dropped() -> None:
    text = "[00:01] ГОЛОС 4:\n[00:02] ГОЛОС 1: Текст"
    segments = parse_transcript(text)
    # The empty-content line must not create a phantom segment.
    assert len(segments) == 1
    assert segments[0].text == "Текст"


def test_parse_transcript_raises_on_unparseable_text() -> None:
    with pytest.raises(cloud_asr.TranscriptParseError):
        parse_transcript("просто текст без таймкодов")


# ---------------------------------------------------------------------------
# _plan_chunks
# ---------------------------------------------------------------------------


def _plan_chunks_for(
    duration_sec: float, *, chunk_minutes: float = 20.0, overlap_sec: float = 60.0
):
    adapter = CloudAudioAsrAdapter(
        api_key="test-key",
        chunk_minutes=chunk_minutes,
        overlap_sec=overlap_sec,
    )
    return adapter._plan_chunks(duration_sec)


def test_plan_chunks_single_chunk_for_short_file() -> None:
    parts = _plan_chunks_for(10 * 60)
    assert parts == [(0.0, 600.0)]


def test_plan_chunks_multiple_chunks_with_overlap() -> None:
    parts = _plan_chunks_for(45 * 60)
    assert len(parts) == 3
    assert parts[0] == (0.0, 1200.0)
    # second chunk starts overlap_sec before its nominal boundary.
    assert parts[1] == (1200.0 - 60.0, 2400.0)
    assert parts[2] == (2400.0 - 60.0, 2700.0)


def test_plan_chunks_zero_duration() -> None:
    assert _plan_chunks_for(0.0) == [(0.0, 0.0)]


def test_plan_chunks_exactly_chunk_length() -> None:
    parts = _plan_chunks_for(20 * 60)
    assert parts == [(0.0, 1200.0)]


def test_plan_cloud_chunks_counts_overlap_in_billable_duration() -> None:
    parts = plan_cloud_chunks(2700.0, 20.0, 60.0)
    assert parts == [(0.0, 1200.0), (1140.0, 2400.0), (2340.0, 2700.0)]
    assert sum(end - start for start, end in parts) == 2820.0


@pytest.mark.parametrize(
    ("duration", "chunk_minutes", "overlap", "expected"),
    [
        (0.0, 20.0, 60.0, [(0.0, 0.0)]),
        (600.0, 20.0, 60.0, [(0.0, 600.0)]),
        (1200.0, 20.0, 60.0, [(0.0, 1200.0)]),
        (2700.0, 0.0, 60.0, [(0.0, 2700.0)]),
        (130.0, 1.0, 90.0, [(0.0, 60.0), (0.0, 120.0), (30.0, 130.0)]),
        (130.0, 1.0, 0.0, [(0.0, 60.0), (60.0, 120.0), (120.0, 130.0)]),
    ],
)
def test_plan_cloud_chunks_preserves_adapter_boundaries(
    duration: float, chunk_minutes: float, overlap: float, expected: list[tuple[float, float]]
) -> None:
    assert plan_cloud_chunks(duration, chunk_minutes, overlap) == expected
    assert _plan_chunks_for(duration, chunk_minutes=chunk_minutes, overlap_sec=overlap) == expected


@pytest.mark.parametrize("position", [0, 1, 2])
@pytest.mark.parametrize("value", [-1.0, float("nan"), float("inf")])
def test_plan_cloud_chunks_rejects_invalid_inputs(position: int, value: float) -> None:
    args = [2700.0, 20.0, 60.0]
    args[position] = value
    with pytest.raises(ValueError):
        plan_cloud_chunks(*args)


def test_rescale_stretched_timestamps_preserves_relative_position() -> None:
    """Cloud models can return a uniformly longer timeline than ffprobe."""
    segments = [
        _seg("Первая", 0.0, 120.0, "ГОЛОС 1"),
        _seg("Искомая фраза", 419.938, 493.1, "ГОЛОС 1"),
    ]

    repaired, scale = _rescale_stretched_timestamps(segments, 404.64)

    assert scale == pytest.approx(404.64 / 493.1)
    assert repaired[-1].end == pytest.approx(404.64)
    # This mirrors the real Киркоров source: 419.938 becomes ~344.7 s,
    # close to the script timecode 00:05:43.
    assert repaired[-1].start == pytest.approx(344.7, abs=0.1)


def test_rescale_stretched_timestamps_keeps_small_rounding_overrun() -> None:
    segments = [_seg("Реплика", 59.0, 60.5, "ГОЛОС 1")]

    repaired, scale = _rescale_stretched_timestamps(segments, 60.0)

    assert repaired == segments
    assert scale is None


# ---------------------------------------------------------------------------
# _stitch_chunk
# ---------------------------------------------------------------------------


def _seg(text: str, start: float, end: float, speaker: str | None) -> AsrSegment:
    return AsrSegment(text=text, start=start, end=end, words=[], speaker=speaker)


def test_stitch_chunk_drops_duplicate_in_overlap_zone() -> None:
    accumulated = [
        _seg("Привет как дела", 1180.0, 1185.0, "ГОЛОС 1"),
        _seg("Хорошо спасибо", 1185.0, 1195.0, "ГОЛОС 2"),
    ]
    new_segments = [
        _seg("Привет как дела", 0.0, 5.0, "ГОЛОС 1"),  # duplicate (in overlap zone)
        _seg("Новая реплика после перекрытия", 65.0, 70.0, "ГОЛОС 1"),
    ]
    own_end_flags = [False, False]
    out, out_flags, next_number, label_map = _stitch_chunk(
        accumulated,
        new_segments,
        own_end_flags,
        part_start=1140.0,  # nominal_start(1200) - overlap(60)
        boundary=1200.0,
        next_speaker_number=3,
    )
    assert len(out) == 1
    assert out[0].text == "Новая реплика после перекрытия"
    # duplicate's speaker must map to the accumulated speaker, not a new number.
    assert label_map["ГОЛОС 1"] == "ГОЛОС 1"


def test_stitch_chunk_no_boundary_just_renumbers() -> None:
    out, out_flags, next_number, label_map = _stitch_chunk(
        [],
        [_seg("Первая часть", 0.0, 5.0, "ГОЛОС 1")],
        [True],
        part_start=0.0,
        boundary=None,
        next_speaker_number=1,
    )
    assert len(out) == 1
    assert out[0].speaker == "ГОЛОС 1"
    assert next_number == 2


# ---------------------------------------------------------------------------
# parse_speaker_legend / _reconcile_speakers
# ---------------------------------------------------------------------------


def test_parse_speaker_legend_basic() -> None:
    text = "ГОЛОС 1 — мужской голос, ведущий (Иван Сивопляс)\nГОЛОС 2 — женский голос"
    legend = parse_speaker_legend(text)
    assert legend == {
        "ГОЛОС 1": "мужской голос, ведущий (Иван Сивопляс)",
        "ГОЛОС 2": "женский голос",
    }


def test_parse_speaker_legend_none_returns_empty() -> None:
    assert parse_speaker_legend(None) == {}


def test_reconcile_speakers_merges_same_person_by_name() -> None:
    segments = [
        _seg("Первая реплика", 0.0, 5.0, "ГОЛОС 1"),
        _seg("Вторая реплика", 100.0, 105.0, "ГОЛОС 3"),
    ]
    descriptions = {
        "ГОЛОС 1": "мужской голос (Иван Сивопляс)",
        "ГОЛОС 3": "мужской голос, краевед (Иван Сивопляс)",
    }
    merged, speakers, final_descriptions = _reconcile_speakers(
        segments, speaker_descriptions=descriptions
    )
    assert len(speakers) == 1
    assert merged[0].speaker == merged[1].speaker == speakers[0]


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("мужской голос (Пётр Иванов)", "МУЖСКОЙ ГОЛОС (Петра Иванова)"),
        ("женский голос (Анна-Мария)", "ЖЕНСКИЙ ГОЛОС (АННА-МАРИЯ)"),
    ],
)
def test_reconcile_speakers_folds_russian_name_variants(first: str, second: str) -> None:
    segments = [
        _seg("Первая реплика", 0.0, 5.0, "ГОЛОС 1"),
        _seg("Вторая реплика", 100.0, 105.0, "ГОЛОС 2"),
    ]
    merged, speakers, _ = _reconcile_speakers(
        segments,
        speaker_descriptions={"ГОЛОС 1": first, "ГОЛОС 2": second},
    )
    assert len(speakers) == 1
    assert merged[0].speaker == merged[1].speaker


def test_reconcile_speakers_keeps_different_people_separate() -> None:
    segments = [
        _seg("A", 0.0, 5.0, "ГОЛОС 1"),
        _seg("B", 100.0, 105.0, "ГОЛОС 2"),
    ]
    descriptions = {
        "ГОЛОС 1": "мужской голос (Иван Сивопляс)",
        "ГОЛОС 2": "женский голос (Елена Петрова)",
    }
    merged, speakers, _ = _reconcile_speakers(segments, speaker_descriptions=descriptions)
    assert len(speakers) == 2


def test_reconcile_speakers_does_not_merge_anonymous_same_sex_different_voices() -> None:
    """Два РАЗНЫХ анонимных голоса одного пола с типовыми описаниями («пол,
    роль, манера речи», без личных имён) не должны склеиваться в одного
    говорящего только из-за совпадения слова «мужской»/«женский»: пол сам по
    себе — не признак различия людей одного пола, а обозначения пола не
    должны входить в значимые слова меры Жаккара (см. _DESCRIPTION_STOPWORDS).
    Before the fix: {"мужской", "уверенный", "тон"} vs
    {"мужской", "доброжелательный", "тон"} give Jaccard 2/4 = 0.5 (>= порога)
    and were incorrectly merged.
    """
    segments = [
        _seg("A", 0.0, 5.0, "ГОЛОС 1"),
        _seg("B", 100.0, 105.0, "ГОЛОС 2"),
    ]
    descriptions = {
        "ГОЛОС 1": "Мужской голос, уверенный тон",
        "ГОЛОС 2": "Мужской голос, доброжелательный тон",
    }
    merged, speakers, _ = _reconcile_speakers(segments, speaker_descriptions=descriptions)
    assert len(speakers) == 2
    assert merged[0].speaker != merged[1].speaker


# ---------------------------------------------------------------------------
# transcribe_cloud (mocked httpx + mocked ffmpeg/ffprobe)
# ---------------------------------------------------------------------------


def _model_response(
    text: str, *, cost: float | None = 0.0123, status_code: int = 200
) -> httpx.Response:
    payload: dict[str, Any] = {
        "choices": [{"message": {"content": text}}],
    }
    if cost is not None:
        payload["usage"] = {"cost": cost}
    return httpx.Response(status_code, json=payload)


@pytest.fixture(autouse=True)
def _no_real_ffmpeg_ffprobe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Avoid touching disk/ffmpeg entirely: fixed duration + dummy mp3 bytes."""

    async def fake_extract_chunk_mp3(
        self: CloudAudioAsrAdapter, media_path, start_sec, duration_sec
    ) -> bytes:
        return b"fake-mp3-bytes"

    monkeypatch.setattr(CloudAudioAsrAdapter, "_probe_duration", lambda self, media_path: 60.0)
    monkeypatch.setattr(CloudAudioAsrAdapter, "_extract_chunk_mp3", fake_extract_chunk_mp3)


_RealAsyncClient = httpx.AsyncClient


def _patch_async_client(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    transport = httpx.MockTransport(handler)

    def fake_async_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs.pop("timeout", None)
        return _RealAsyncClient(transport=transport)

    monkeypatch.setattr(cloud_asr.httpx, "AsyncClient", fake_async_client)


def test_transcribe_cloud_returns_expected_structure(monkeypatch: pytest.MonkeyPatch) -> None:
    # Two lines: only the FIRST one keeps its own model-provided end — the
    # transcript's last segment always has its end forced to the technical
    # chunk boundary (see transcribe_words), so has_explicit_end is checked
    # on the non-last segment.
    text = "[00:01.0 — 00:02.0] ГОЛОС 1: Привет мир\n[00:03.0 — 00:04.0] ГОЛОС 1: Вторая реплика"

    def handler(request: httpx.Request) -> httpx.Response:
        return _model_response(text, cost=0.05)

    _patch_async_client(monkeypatch, handler)

    result = transcribe_cloud(Path("/tmp/does-not-matter.wav"), api_key="test-key")

    assert result["backend"] == "cloud-audio"
    assert result["language"] == "ru"
    assert result["duration_sec"] == 60.0
    assert result["cost_usd"] == pytest.approx(0.05)
    assert len(result["segments"]) == 2
    seg = result["segments"][0]
    assert set(seg.keys()) == {"text", "start", "end", "speaker", "has_explicit_end"}
    assert seg["text"] == "Привет мир"
    assert seg["has_explicit_end"] is True
    assert result["speakers"] == [seg["speaker"]]


def test_transcribe_cloud_retries_on_http_429(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"count": 0}
    text = "[00:01.0 — 00:02.0] ГОЛОС 1: Готово после ретрая"

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        if calls["count"] == 1:
            return httpx.Response(429, text="rate limited")
        return _model_response(text, cost=0.01)

    _patch_async_client(monkeypatch, handler)

    async def no_sleep(self: CloudAudioAsrAdapter, attempt: int) -> None:
        return None

    monkeypatch.setattr(CloudAudioAsrAdapter, "_sleep_backoff", no_sleep)

    result = transcribe_cloud(Path("/tmp/does-not-matter.wav"), api_key="test-key")

    assert calls["count"] == 2
    assert result["segments"][0]["text"] == "Готово после ретрая"
    assert result["cost_usd"] == pytest.approx(0.01)


def test_call_model_payload_requests_cost_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    """OpenRouter отдаёт `usage.cost` только если запрос явно попросил об
    этом через `usage.include = True` — без этого `_extract_cost` всегда
    получает None."""
    captured: dict[str, Any] = {}
    text = "[00:01.0 — 00:02.0] ГОЛОС 1: Привет"

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        return _model_response(text, cost=0.01)

    _patch_async_client(monkeypatch, handler)

    transcribe_cloud(Path("/tmp/does-not-matter.wav"), api_key="test-key")

    assert captured["payload"]["usage"] == {"include": True}


def test_transcribe_words_preserves_cost_on_parse_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Если последний из нескольких чанков не удалось распарсить, стоимость
    уже успешно распознанных предыдущих чанков не должна теряться, а сама
    ошибка должна быть CloudAudioAsrError (а не TranscriptParseError/иное
    исключение, которое вызывающий код не ловит)."""
    unparseable = "просто текст без таймкодов, без разметки"
    parseable = "[00:01.0 — 00:02.0] ГОЛОС 1: Реплика"

    monkeypatch.setattr(
        CloudAudioAsrAdapter,
        "_plan_chunks",
        lambda self, duration_sec: [(0.0, 20.0), (20.0, 40.0), (40.0, 60.0)],
    )

    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        if calls["count"] <= 2:
            return _model_response(parseable, cost=0.02)
        return _model_response(unparseable, cost=None)

    _patch_async_client(monkeypatch, handler)

    with pytest.raises(cloud_asr.CloudAudioAsrError) as exc_info:
        transcribe_cloud(Path("/tmp/does-not-matter.wav"), api_key="test-key")

    assert calls["count"] == 3
    assert exc_info.value.cost_usd == pytest.approx(0.04)


# ---------------------------------------------------------------------------
# _extract_chunk_mp3: a hung ffmpeg process must not block the run forever.
# ---------------------------------------------------------------------------


class _HangingProcess:
    """Fake asyncio subprocess whose `communicate()` never returns on its
    own (simulates a hung ffmpeg) but supports `kill()`/`wait()`."""

    def __init__(self) -> None:
        self.killed = False
        self.waited = False
        self.returncode: int | None = None

    async def communicate(self) -> tuple[bytes, bytes]:
        await asyncio.sleep(10.0)
        return b"stdout-should-not-be-reached", b""

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    async def wait(self) -> int:
        self.waited = True
        return self.returncode or 0


def test_extract_chunk_mp3_kills_hung_ffmpeg_after_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cloud_asr, "_EXTRACT_CHUNK_TIMEOUT_SEC", 0.05)
    process = _HangingProcess()

    async def fake_create_subprocess_exec(*args: Any, **kwargs: Any) -> _HangingProcess:
        return process

    monkeypatch.setattr(cloud_asr.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    adapter = CloudAudioAsrAdapter(api_key="test-key")

    async def scenario() -> None:
        # Call the ORIGINAL (unpatched-by-autouse-fixture) implementation
        # directly — `_REAL_EXTRACT_CHUNK_MP3` was captured at module import
        # time, before `_no_real_ffmpeg_ffprobe` replaces the bound method.
        await _REAL_EXTRACT_CHUNK_MP3(adapter, Path("/tmp/does-not-matter.wav"), 0.0, 5.0)

    start = time.monotonic()
    with pytest.raises(cloud_asr.CloudAudioAsrError, match="не ответил"):
        asyncio.run(scenario())
    elapsed = time.monotonic() - start

    assert elapsed < 5.0  # bounded by the (patched) timeout, not the 10s hang
    assert process.killed is True
    assert process.waited is True


# ---------------------------------------------------------------------------
# Cancellation must interrupt the network request/retry wait, not only the
# gap between chunks (see transcribe_words / _call_model / _await_cancellable).
# ---------------------------------------------------------------------------


def test_await_cancellable_interrupts_slow_awaitable() -> None:
    async def scenario() -> None:
        cancel = asyncio.Event()

        async def slow() -> str:
            await asyncio.sleep(10.0)
            return "done"

        async def trigger() -> None:
            await asyncio.sleep(0.01)
            cancel.set()

        asyncio.ensure_future(trigger())
        start = time.monotonic()
        with pytest.raises(asyncio.CancelledError):
            await CloudAudioAsrAdapter._await_cancellable(slow(), cancel)
        assert time.monotonic() - start < 5.0

    asyncio.run(scenario())


def test_await_cancellable_returns_result_when_not_cancelled() -> None:
    async def scenario() -> None:
        cancel = asyncio.Event()

        async def quick() -> str:
            return "ok"

        result = await CloudAudioAsrAdapter._await_cancellable(quick(), cancel)
        assert result == "ok"

    asyncio.run(scenario())


def test_call_model_raises_immediately_if_already_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If cancellation is requested before `_call_model` even starts, no HTTP
    request should be attempted at all."""
    called = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        called["count"] += 1
        return _model_response("[00:01.0 — 00:02.0] ГОЛОС 1: X")

    _patch_async_client(monkeypatch, handler)

    async def scenario() -> None:
        adapter = CloudAudioAsrAdapter(api_key="test-key")
        cancel = asyncio.Event()
        cancel.set()
        async with httpx.AsyncClient() as client:
            with pytest.raises(asyncio.CancelledError):
                await adapter._call_model(client, "audio", prompt="prompt", cancel=cancel)

    asyncio.run(scenario())
    assert called["count"] == 0


def test_call_model_cancellation_interrupts_slow_network_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cancellation requested while the (slow) request to the model is
    in flight must interrupt it promptly instead of waiting out the full
    request/backoff duration (see `_await_cancellable`)."""

    async def slow_handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(10.0)
        return _model_response("[00:01.0 — 00:02.0] ГОЛОС 1: X")

    _patch_async_client(monkeypatch, slow_handler)

    async def scenario() -> None:
        adapter = CloudAudioAsrAdapter(api_key="test-key")
        cancel = asyncio.Event()

        async def trigger() -> None:
            await asyncio.sleep(0.02)
            cancel.set()

        asyncio.ensure_future(trigger())

        start = time.monotonic()
        async with httpx.AsyncClient() as client:
            with pytest.raises(asyncio.CancelledError):
                await adapter._call_model(client, "audio", prompt="prompt", cancel=cancel)
        assert time.monotonic() - start < 5.0

    asyncio.run(scenario())


# Budget accounting is observable at the HTTP boundary: no request can consume
# headroom that has not already been published as a reservation.
def test_budget_reserves_before_every_http_attempt(monkeypatch) -> None:
    snapshots = []
    reservations_at_http = []

    def handler(_request):
        reservations_at_http.append(snapshots[-1]["reserved_usd"])
        if len(reservations_at_http) == 1:
            return httpx.Response(429, text="retry")
        return _model_response("[00:01] ГОЛОС 1: Готово", cost=0.02)

    async def no_sleep(self, attempt):
        pass

    _patch_async_client(monkeypatch, handler)
    monkeypatch.setattr(CloudAudioAsrAdapter, "_sleep_backoff", no_sleep)
    result = transcribe_cloud(
        Path("unused.wav"),
        api_key="test-key",
        max_cost_usd="1",
        usd_per_minute_estimate="0.06",
        budget_callback=snapshots.append,
    )

    assert reservations_at_http == pytest.approx([0.06, 0.12])
    assert result["cost_usd"] == pytest.approx(0.02)
    assert result["reserved_cost_usd"] == pytest.approx(0.06)
    assert result["cost_limit_usd"] == 1.0
    assert snapshots[-1] == pytest.approx(
        {
            "limit_usd": 1.0,
            "spent_usd": 0.02,
            "reserved_usd": 0.06,
            "total_usd": 0.08,
        }
    )


def test_budget_prevents_retry_when_reservation_would_cross_limit(monkeypatch) -> None:
    from cost_budget import CostBudgetExceeded

    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(503, text="retry")

    async def no_sleep(self, attempt):
        pass

    _patch_async_client(monkeypatch, handler)
    monkeypatch.setattr(CloudAudioAsrAdapter, "_sleep_backoff", no_sleep)
    with pytest.raises(CostBudgetExceeded) as caught:
        transcribe_cloud(
            Path("unused.wav"),
            api_key="test-key",
            max_cost_usd="0.1",
            usd_per_minute_estimate="0.06",
        )

    assert len(calls) == 1
    assert caught.value.cost_usd == 0.0
    assert caught.value.reserved_cost_usd == pytest.approx(0.06)
    assert caught.value.cost_limit_usd == pytest.approx(0.1)


def test_budget_unknown_cost_keeps_reservation_and_blocks_next_chunk(monkeypatch) -> None:
    from cost_budget import CostBudgetExceeded

    calls = []
    monkeypatch.setattr(CloudAudioAsrAdapter, "_probe_duration", lambda *_: 120.0)

    def handler(request):
        calls.append(request)
        return _model_response("[00:01] ГОЛОС 1: Готово", cost=None)

    _patch_async_client(monkeypatch, handler)
    with pytest.raises(CostBudgetExceeded) as caught:
        transcribe_cloud(
            Path("unused.wav"),
            api_key="test-key",
            chunk_minutes=1,
            overlap_sec=0,
            max_cost_usd="0.09",
            usd_per_minute_estimate="0.06",
        )

    assert len(calls) == 1
    assert caught.value.cost_usd == 0.0
    assert caught.value.reserved_cost_usd == pytest.approx(0.06)


def test_budget_actual_cost_replaces_reservation_for_next_chunk(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(CloudAudioAsrAdapter, "_probe_duration", lambda *_: 120.0)

    def handler(request):
        calls.append(request)
        return _model_response("[00:01] ГОЛОС 1: Готово", cost=0.01)

    _patch_async_client(monkeypatch, handler)
    result = transcribe_cloud(
        Path("unused.wav"),
        api_key="test-key",
        chunk_minutes=1,
        overlap_sec=0,
        max_cost_usd="0.08",
        usd_per_minute_estimate="0.06",
    )

    assert len(calls) == 2
    assert result["cost_usd"] == pytest.approx(0.02)
    assert result["reserved_cost_usd"] == 0.0


def test_budget_initial_spend_prevents_first_http_attempt(monkeypatch) -> None:
    from cost_budget import CostBudgetExceeded

    calls = []
    _patch_async_client(monkeypatch, lambda request: calls.append(request))
    with pytest.raises(CostBudgetExceeded) as caught:
        transcribe_cloud(
            Path("unused.wav"),
            api_key="test-key",
            max_cost_usd="0.1",
            usd_per_minute_estimate="0.06",
            initial_cost_usd="0.05",
        )

    assert len(calls) == 0
    assert caught.value.cost_usd == pytest.approx(0.05)
    assert caught.value.reserved_cost_usd == 0.0


def test_budget_asr_error_preserves_settled_and_unknown_cost(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(CloudAudioAsrAdapter, "_probe_duration", lambda *_: 120.0)

    def handler(request):
        calls.append(request)
        return _model_response(
            "[00:01] ГОЛОС 1: Готово" if len(calls) == 1 else "invalid transcript",
            cost=0.01 if len(calls) == 1 else None,
        )

    _patch_async_client(monkeypatch, handler)
    with pytest.raises(cloud_asr.CloudAudioAsrError) as caught:
        transcribe_cloud(
            Path("unused.wav"),
            api_key="test-key",
            chunk_minutes=1,
            overlap_sec=0,
            max_cost_usd="1",
            usd_per_minute_estimate="0.06",
            initial_cost_usd="0.03",
        )

    assert len(calls) == 2
    assert caught.value.cost_usd == pytest.approx(0.04)
    assert caught.value.reserved_cost_usd == pytest.approx(0.06)
    assert caught.value.cost_limit_usd == 1.0


def test_deadline_interrupts_http_and_prevents_further_chunks(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(CloudAudioAsrAdapter, "_probe_duration", lambda *_: 120.0)

    async def handler(request):
        calls.append(request)
        await asyncio.sleep(5)
        return _model_response("[00:01] ГОЛОС 1: Готово", cost=0.01)

    _patch_async_client(monkeypatch, handler)
    started = time.monotonic()
    with pytest.raises(TimeoutError) as caught:
        transcribe_cloud(
            Path("unused.wav"),
            api_key="test-key",
            chunk_minutes=1,
            overlap_sec=0,
            max_cost_usd="1",
            usd_per_minute_estimate="0.06",
            timeout_sec=0.05,
        )

    assert len(calls) == 1
    assert time.monotonic() - started < 1.0
    assert caught.value.reserved_cost_usd == pytest.approx(0.06)


def test_cancellation_before_start_prevents_http_dispatch(monkeypatch) -> None:
    calls = []
    cancel = asyncio.Event()
    cancel.set()
    _patch_async_client(monkeypatch, lambda request: calls.append(request))

    with pytest.raises(asyncio.CancelledError):
        transcribe_cloud(Path("unused.wav"), api_key="test-key", cancel=cancel)

    assert len(calls) == 0


def test_cancellation_after_first_chunk_prevents_next_http_dispatch(monkeypatch) -> None:
    calls = []
    cancel = asyncio.Event()
    monkeypatch.setattr(CloudAudioAsrAdapter, "_probe_duration", lambda *_: 120.0)

    def handler(request):
        calls.append(request)
        return _model_response("[00:01] ГОЛОС 1: Готово", cost=0.01)

    _patch_async_client(monkeypatch, handler)
    with pytest.raises(asyncio.CancelledError) as caught:
        transcribe_cloud(
            Path("unused.wav"),
            api_key="test-key",
            chunk_minutes=1,
            overlap_sec=0,
            cancel=cancel,
            progress_callback=lambda _: cancel.set(),
            max_cost_usd="1",
            usd_per_minute_estimate="0.06",
        )

    assert len(calls) == 1
    assert caught.value.cost_usd == pytest.approx(0.01)
    assert caught.value.reserved_cost_usd == 0.0
