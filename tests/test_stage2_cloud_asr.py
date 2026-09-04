"""Tests for the ported cloud ASR adapter (src/stage2/cloud_asr.py).

Covers only the pure parsing/stitching logic plus the public
``transcribe_cloud()`` entry point with a fully mocked network layer
(httpx transport) and mocked ffmpeg/ffprobe subprocess calls — no real
network access, no real ffmpeg/ffprobe binary required.
"""

from __future__ import annotations

import json
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
    _stitch_chunk,
    parse_speaker_legend,
    parse_transcript,
    transcribe_cloud,
)


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
