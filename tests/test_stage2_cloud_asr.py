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
    find_coverage_holes,
    plan_cloud_chunks,
    resolve_chunk_minutes,
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
    # Короткие тестовые ответы выглядят «разреженными»; повторные запросы по
    # дырам включаются только в тестах покрытия (ниже).
    monkeypatch.setenv("SYNC_CLOUD_ASR_HOLE_RETRIES", "0")
    for name in (
        "SYNC_CLOUD_ASR_CHUNK_MINUTES",
        "SYNC_CLOUD_ASR_HOLE_MIN_SEC",
        "SYNC_CLOUD_ASR_HOLE_MAX_WPM",
    ):
        monkeypatch.delenv(name, raising=False)


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


@pytest.mark.parametrize(
    "error_type,detail",
    [(httpx.ReadTimeout, "HTTP timeout (ReadTimeout)"), (httpx.ConnectError, "")],
)
def test_exhausted_http_retries_preserve_empty_timeout_classification(
    monkeypatch, error_type, detail
):
    calls = []

    def handler(request):
        calls.append(request)
        raise error_type("", request=request)

    async def no_sleep(_self, _attempt):
        pass

    _patch_async_client(monkeypatch, handler)
    monkeypatch.setattr(CloudAudioAsrAdapter, "_sleep_backoff", no_sleep)
    with pytest.raises(cloud_asr.CloudAudioAsrError) as caught:
        transcribe_cloud(
            Path("unused.wav"), api_key="test-key", max_cost_usd="1", usd_per_minute_estimate="0.06"
        )

    assert len(calls) == 3
    assert str(caught.value) == (
        "Ошибка сети при запросе к облачной ASR-модели после 3 попыток: " + detail
    )
    assert caught.value.reserved_cost_usd == pytest.approx(0.18)


def test_empty_read_timeout_message_survives_real_runner_and_worker_ipc(tmp_path, monkeypatch):
    import multiprocessing
    from stage2.whisper_process import (
        WhisperProcessError,
        _shutdown_worker,
        transcribe_audio_in_process,
    )

    if "fork" not in multiprocessing.get_all_start_methods():
        pytest.skip("real HTTP error propagation test requires inherited MockTransport")
    _shutdown_worker()
    audio = tmp_path / "guide.wav"
    audio.write_bytes(b"fixture")
    monkeypatch.setenv("SYNC_CLOUD_ASR_API_KEY", "test-key")

    def handler(request):
        raise httpx.ReadTimeout("", request=request)

    async def no_sleep(_self, _attempt):
        pass

    _patch_async_client(monkeypatch, handler)
    monkeypatch.setattr(CloudAudioAsrAdapter, "_sleep_backoff", no_sleep)
    try:
        with pytest.raises(WhisperProcessError) as caught:
            transcribe_audio_in_process(
                audio,
                backend="cloud-audio",
                timeout_seconds=5,
                cloud_options={"max_cost_usd": "1", "usd_per_minute_estimate": "0.06"},
                _start_method="fork",
            )
    finally:
        _shutdown_worker()

    assert str(caught.value) == (
        "WhisperTranscriptionError: Облачное распознавание речи не удалось: "
        "Ошибка сети при запросе к облачной ASR-модели после 3 попыток: HTTP timeout (ReadTimeout)"
    )
    assert caught.value.reserved_cost_usd == pytest.approx(0.18)


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
    assert result["cost_pending"] is True
    assert snapshots[-1] == pytest.approx(
        {
            "limit_usd": 1.0,
            "spent_usd": 0.02,
            "reserved_usd": 0.06,
            "total_usd": 0.08,
            "estimated_spent_usd": 0.0,
            "cost_pending": True,
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


@pytest.mark.parametrize("allowance,expected_timeout", [(7200, 900), (30, 30)])
def test_http_request_timeout_caps_remaining_whole_job_allowance(
    monkeypatch, allowance, expected_timeout
) -> None:
    timeouts = []

    def handler(request):
        timeouts.append(request.extensions["timeout"]["read"])
        return _model_response("[00:01] ГОЛОС 1: Готово", cost=0.01)

    monkeypatch.setattr(
        cloud_asr.httpx,
        "AsyncClient",
        lambda **kwargs: _RealAsyncClient(transport=httpx.MockTransport(handler), **kwargs),
    )
    transcribe_cloud(
        Path("unused.wav"),
        api_key="test-key",
        timeout_sec=allowance,
        budget_callback=lambda _snapshot: time.sleep(0.02),
    )

    assert len(timeouts) == 1
    assert expected_timeout - 1 < timeouts[0] <= expected_timeout
    if allowance < 900:
        assert timeouts[0] < allowance - 0.01


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


def test_deadline_expiring_inside_budget_callback_prevents_http_dispatch(monkeypatch) -> None:
    calls = []

    def handler(request):
        calls.append(request)
        return _model_response("[00:01] ГОЛОС 1: Готово", cost=0.01)

    def blocking_callback(_snapshot):
        time.sleep(0.08)

    _patch_async_client(monkeypatch, handler)
    with pytest.raises(TimeoutError):
        transcribe_cloud(
            Path("unused.wav"),
            api_key="test-key",
            timeout_sec=0.02,
            max_cost_usd="1",
            usd_per_minute_estimate="0.06",
            budget_callback=blocking_callback,
        )

    assert len(calls) == 0


def test_confirmed_duration_freezes_chunk_boundaries_and_reservations(monkeypatch):
    extracted = []
    snapshots = []

    async def extract(_self, _path, start, duration):
        extracted.append((start, duration))
        return b"mp3"

    monkeypatch.setattr(CloudAudioAsrAdapter, "_probe_duration", lambda *_: 151.0)
    monkeypatch.setattr(CloudAudioAsrAdapter, "_extract_chunk_mp3", extract)
    _patch_async_client(
        monkeypatch, lambda _request: _model_response("[00:01] ГОЛОС 1: Готово", cost=None)
    )
    result = transcribe_cloud(
        Path("unused.wav"),
        api_key="test-key",
        chunk_minutes=1,
        overlap_sec=10,
        duration_sec=150.0,
        max_cost_usd="0.17",
        usd_per_minute_estimate="0.06",
        budget_callback=snapshots.append,
    )

    assert extracted == [(0.0, 60.0), (50.0, 70.0), (110.0, 40.0)]
    assert result["duration_sec"] == 150.0
    assert result["reserved_cost_usd"] == pytest.approx(0.17)
    assert snapshots[-1]["reserved_usd"] == pytest.approx(0.17)


@pytest.mark.parametrize("duration", [0, -1, float("nan"), float("inf")])
def test_confirmed_duration_rejects_invalid_values_before_http(monkeypatch, duration):
    calls = []
    _patch_async_client(
        monkeypatch,
        lambda request: calls.append(request) or _model_response("[00:01] ГОЛОС 1: Тест"),
    )
    with pytest.raises(ValueError, match="duration_sec"):
        transcribe_cloud(Path("unused.wav"), api_key="test-key", duration_sec=duration)
    assert calls == []


# ---------------------------------------------------------------------------
# Покрытие: дыры внутри части и их повторное распознавание
# ---------------------------------------------------------------------------


def _words(count: int) -> str:
    return " ".join(["слово"] * count)


def _hole_seg(start: float, end: float, words: int) -> AsrSegment:
    return AsrSegment(text=_words(words), start=start, end=end, words=[], speaker="ГОЛОС 1")


def test_default_chunking_is_five_minutes_with_short_overlap() -> None:
    assert cloud_asr.DEFAULT_CHUNK_MINUTES == 5.0
    assert cloud_asr.DEFAULT_OVERLAP_SEC == 20.0
    parts = plan_cloud_chunks(
        1200.0, cloud_asr.DEFAULT_CHUNK_MINUTES, cloud_asr.DEFAULT_OVERLAP_SEC
    )
    assert parts == [(0.0, 300.0), (280.0, 600.0), (580.0, 900.0), (880.0, 1200.0)]
    adapter = CloudAudioAsrAdapter(api_key="test-key")
    assert adapter._plan_chunks(1200.0) == parts


def test_chunk_minutes_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    assert resolve_chunk_minutes() == 5.0
    monkeypatch.setenv("SYNC_CLOUD_ASR_CHUNK_MINUTES", "10")
    assert resolve_chunk_minutes() == 10.0
    assert CloudAudioAsrAdapter(api_key="test-key").chunk_minutes == 10.0
    for bad in ("abc", "0", "-3", "nan"):
        monkeypatch.setenv("SYNC_CLOUD_ASR_CHUNK_MINUTES", bad)
        assert resolve_chunk_minutes() == 5.0


def test_find_coverage_holes_normal_density_has_no_holes() -> None:
    segments = [_hole_seg(0, 60, 150), _hole_seg(60, 120, 140), _hole_seg(120, 180, 160)]
    assert find_coverage_holes(segments, [False] * 3, 0.0, 180.0) == []


def test_find_coverage_holes_sparse_long_segment_is_hole() -> None:
    segments = [_hole_seg(0, 60, 150), _hole_seg(60, 300, 12), _hole_seg(300, 360, 150)]
    assert find_coverage_holes(segments, [False] * 3, 0.0, 360.0) == [(60.0, 300.0)]


def test_find_coverage_holes_short_sparse_segment_is_not_hole() -> None:
    segments = [_hole_seg(0, 20, 2), _hole_seg(20, 60, 100)]
    assert find_coverage_holes(segments, [False, False], 0.0, 60.0) == []


def test_find_coverage_holes_long_segment_with_own_end_is_not_hole() -> None:
    segments = [_hole_seg(0, 120, 20), _hole_seg(120, 180, 150)]
    assert find_coverage_holes(segments, [True, False], 0.0, 180.0) == []


def test_find_coverage_holes_gap_after_explicit_end_is_hole() -> None:
    segments = [_hole_seg(0, 20, 60), _hole_seg(100, 160, 150)]
    assert find_coverage_holes(segments, [True, False], 0.0, 160.0) == [(20.0, 100.0)]


def test_find_coverage_holes_tail_is_hole() -> None:
    segments = [_hole_seg(0, 60, 150), _hole_seg(60, 300, 10)]
    assert find_coverage_holes(segments, [False, False], 0.0, 300.0) == [(60.0, 300.0)]
    # Последний сегмент с явным концом, за которым тишина до конца части.
    segments = [_hole_seg(0, 60, 150), _hole_seg(60, 80, 50)]
    assert find_coverage_holes(segments, [False, True], 0.0, 300.0) == [(80.0, 300.0)]


def test_find_coverage_holes_empty_part_is_hole() -> None:
    assert find_coverage_holes([], [], 100.0, 400.0) == [(100.0, 400.0)]
    assert find_coverage_holes([], [], 100.0, 110.0) == []


def test_find_coverage_holes_thresholds_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    segments = [_hole_seg(0, 60, 150), _hole_seg(60, 120, 10)]
    assert find_coverage_holes(segments, [False, False], 0.0, 120.0) == [(60.0, 120.0)]
    monkeypatch.setenv("SYNC_CLOUD_ASR_HOLE_MIN_SEC", "90")
    assert find_coverage_holes(segments, [False, False], 0.0, 120.0) == []
    monkeypatch.setenv("SYNC_CLOUD_ASR_HOLE_MIN_SEC", "30")
    monkeypatch.setenv("SYNC_CLOUD_ASR_HOLE_MAX_WPM", "5")
    assert find_coverage_holes(segments, [False, False], 0.0, 120.0) == []


_SPARSE_PART = (
    f"[00:00] ГОЛОС 1: {_words(100)}\n[01:00] ГОЛОС 1: {_words(10)}\n[04:00] ГОЛОС 1: {_words(100)}"
)


def _coverage_call(
    monkeypatch: pytest.MonkeyPatch, replies: list, **kwargs: Any
) -> tuple[dict[str, Any], list[tuple[float, float]]]:
    extracted: list[tuple[float, float]] = []

    async def extract(_self, _path, start, duration):
        extracted.append((start, duration))
        return b"mp3"

    async def no_sleep(_self, _attempt):
        pass

    monkeypatch.setattr(CloudAudioAsrAdapter, "_extract_chunk_mp3", extract)
    monkeypatch.setattr(CloudAudioAsrAdapter, "_sleep_backoff", no_sleep)
    calls = iter(replies)
    _patch_async_client(monkeypatch, lambda _request: next(calls)())
    result = transcribe_cloud(
        Path("unused.wav"),
        api_key="test-key",
        chunk_minutes=10,
        overlap_sec=0,
        duration_sec=300.0,
        **kwargs,
    )
    return result, extracted


def test_hole_is_re_recognized_only_on_its_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SYNC_CLOUD_ASR_HOLE_RETRIES", "1")
    monkeypatch.setenv("SYNC_CLOUD_ASR_HOLE_SPLIT_SEC", "600")
    retry = f"[00:03] ГОЛОС 1: {_words(150)}\n[01:30] ГОЛОС 1: {_words(150)}"
    result, extracted = _coverage_call(
        monkeypatch,
        [
            lambda: _model_response(_SPARSE_PART, cost=0.05),
            lambda: _model_response(retry, cost=0.02),
        ],
    )

    # Вторая нарезка — дыра (60..240) с полем 3 с с каждой стороны.
    assert extracted == [(0.0, 300.0), (57.0, 186.0)]
    assert result["cost_usd"] == pytest.approx(0.07)
    segments = result["segments"]
    assert [round(seg["start"]) for seg in segments] == [0, 60, 147, 240]
    assert sum(len(seg["text"].split()) for seg in segments) == 100 + 300 + 100
    coverage = result["coverage"]
    assert coverage["holes_detected"] == 1
    assert coverage["holes_repaired"] == 1
    assert coverage["unrecovered_sec"] == 0.0
    assert coverage["unrecovered_ranges"] == []
    assert coverage["words_per_minute"] == pytest.approx(500 / 5)


def test_hole_retry_not_better_keeps_original_and_reports_range(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SYNC_CLOUD_ASR_HOLE_SPLIT_SEC", "600")
    monkeypatch.setenv("SYNC_CLOUD_ASR_HOLE_RETRIES", "1")
    result, _ = _coverage_call(
        monkeypatch,
        [
            lambda: _model_response(_SPARSE_PART, cost=0.05),
            lambda: _model_response(f"[00:03] ГОЛОС 1: {_words(5)}", cost=0.01),
        ],
    )
    assert sum(len(seg["text"].split()) for seg in result["segments"]) == 210
    assert result["coverage"]["holes_repaired"] == 0
    assert result["coverage"]["unrecovered_ranges"] == [[60.0, 240.0]]
    assert result["cost_usd"] == pytest.approx(0.06)


def test_failed_hole_retry_does_not_fail_the_file(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SYNC_CLOUD_ASR_HOLE_SPLIT_SEC", "600")
    monkeypatch.setenv("SYNC_CLOUD_ASR_HOLE_RETRIES", "1")
    result, _ = _coverage_call(
        monkeypatch,
        [
            lambda: _model_response(_SPARSE_PART, cost=0.05),
            lambda: httpx.Response(500, text="boom"),
            lambda: httpx.Response(500, text="boom"),
            lambda: httpx.Response(500, text="boom"),
        ],
    )
    assert len(result["segments"]) == 3
    assert result["cost_usd"] == pytest.approx(0.05)
    coverage = result["coverage"]
    assert coverage["holes_detected"] == 1
    assert coverage["holes_repaired"] == 0
    assert coverage["unrecovered_sec"] == pytest.approx(180.0)
    assert coverage["unrecovered_ranges"] == [[60.0, 240.0]]


def test_hole_retries_disabled_makes_no_extra_request(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SYNC_CLOUD_ASR_HOLE_SPLIT_SEC", "600")
    result, extracted = _coverage_call(
        monkeypatch, [lambda: _model_response(_SPARSE_PART, cost=0.05)]
    )
    assert extracted == [(0.0, 300.0)]
    assert result["coverage"]["holes_detected"] == 1
    assert result["coverage"]["unrecovered_sec"] == pytest.approx(180.0)


def test_hole_retry_over_budget_keeps_paid_file_and_cancel_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SYNC_CLOUD_ASR_HOLE_SPLIT_SEC", "600")
    monkeypatch.setenv("SYNC_CLOUD_ASR_HOLE_RETRIES", "1")
    # 5 минут по 0.06 USD/мин = 0.30; повтор дыры (186 с, ~0.186) не влезает в 0.40.
    # Уже оплаченный файл не должен пропасть: повтор пропускается, дыра
    # остаётся в отчёте о покрытии, лишнего запроса к модели нет.
    result, extracted = _coverage_call(
        monkeypatch,
        [lambda: _model_response(_SPARSE_PART, cost=0.30)],
        max_cost_usd="0.40",
        usd_per_minute_estimate="0.06",
    )
    # Ответ модели в фикстуре один: второй запрос упал бы на StopIteration.
    assert len(extracted) == 2
    assert result["cost_usd"] == pytest.approx(0.30)
    assert result["coverage"]["holes_repaired"] == 0
    assert result["coverage"]["unrecovered_sec"] > 0

    cancel = asyncio.Event()

    def first_reply() -> httpx.Response:
        cancel.set()
        return _model_response(_SPARSE_PART, cost=0.05)

    with pytest.raises(asyncio.CancelledError):
        _coverage_call(monkeypatch, [first_reply], cancel=cancel)


def test_cloud_transcription_fingerprint_changes_with_timestamp_mapping() -> None:
    import api

    cloud = api._transcription_config(backend="cloud-audio", model="m", language="ru")
    assert cloud["cloud_timestamp_mapping"] == "4"
    local = api._transcription_config(backend="faster-whisper", model="m", language="ru")
    assert local["cloud_timestamp_mapping"] == ""
    assert api._transcription_config_fingerprint(cloud) != api._transcription_config_fingerprint(
        {**cloud, "cloud_timestamp_mapping": "3"}
    )


def test_long_hole_is_re_recognized_in_short_pieces(monkeypatch: pytest.MonkeyPatch) -> None:
    # Модель «замолкает» и на повторе длинного куска, поэтому дыра 60..240 c
    # повторяется кусками не длиннее 120 с: (60, 150) и (150, 240).
    monkeypatch.setenv("SYNC_CLOUD_ASR_HOLE_RETRIES", "1")
    piece = f"[00:03] ГОЛОС 1: {_words(150)}"
    result, extracted = _coverage_call(
        monkeypatch,
        [
            lambda: _model_response(_SPARSE_PART, cost=0.05),
            lambda: _model_response(piece, cost=0.01),
            lambda: _model_response(piece, cost=0.01),
        ],
    )

    assert extracted == [(0.0, 300.0), (57.0, 96.0), (147.0, 96.0)]
    assert result["cost_usd"] == pytest.approx(0.07)
    segments = result["segments"]
    assert [round(seg["start"]) for seg in segments] == [0, 60, 150, 240]
    assert sum(len(seg["text"].split()) for seg in segments) == 100 + 150 + 150 + 100
    coverage = result["coverage"]
    assert coverage["holes_repaired"] == coverage["holes_detected"] == 2
    assert coverage["unrecovered_sec"] == 0.0


def test_failed_piece_of_long_hole_is_reported_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SYNC_CLOUD_ASR_HOLE_RETRIES", "1")
    result, _extracted = _coverage_call(
        monkeypatch,
        [
            lambda: _model_response(_SPARSE_PART, cost=0.05),
            lambda: _model_response(f"[00:03] ГОЛОС 1: {_words(150)}", cost=0.01),
            lambda: _model_response("[00:03] ГОЛОС 1: тишина", cost=0.01),
        ],
    )

    coverage = result["coverage"]
    assert coverage["holes_repaired"] == 1
    assert coverage["unrecovered_ranges"] == [[150.0, 240.0]]
