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
    # Two lines; has_explicit_end is checked on the first one.
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


def test_last_utterance_keeps_own_end_before_trailing_silence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Audit 2026-10-21 #4: 45-50 s in a chunk that ends at 60 s must stay 45-50 s,
    # otherwise the interpolated words are smeared over 15 s of silence.
    text = "[00:01.0 — 00:02.0] ГОЛОС 1: Первая реплика\n[00:45.0 — 00:50.0] ГОЛОС 1: Последняя реплика"
    _patch_async_client(monkeypatch, lambda request: _model_response(text))

    result = transcribe_cloud(Path("/tmp/does-not-matter.wav"), api_key="test-key")

    last = result["segments"][-1]
    assert last["start"] == pytest.approx(45.0)
    assert last["end"] == pytest.approx(50.0)
    assert last["has_explicit_end"] is True


def test_last_utterance_own_end_in_last_part_is_not_extended_to_duration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Регрессия: последняя часть файла (60 s), реплика 45-50 s с собственным концом
    # не должна растягиваться до duration_sec.
    text = "[00:01.0 — 00:02.0] ГОЛОС 1: Первая реплика\n[00:45.0 — 00:50.0] ГОЛОС 1: Последняя реплика"
    _patch_async_client(monkeypatch, lambda request: _model_response(text))

    result = transcribe_cloud(Path("/tmp/does-not-matter.wav"), api_key="test-key")

    last = result["segments"][-1]
    assert last["start"] == pytest.approx(45.0)
    assert last["end"] == pytest.approx(50.0)
    assert last["has_explicit_end"] is True


def test_last_utterance_without_own_end_is_still_extended_to_part_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text = "[00:01] ГОЛОС 1: Первая реплика\n[00:45] ГОЛОС 1: Последняя реплика"
    _patch_async_client(monkeypatch, lambda request: _model_response(text))

    result = transcribe_cloud(Path("/tmp/does-not-matter.wav"), api_key="test-key")

    last = result["segments"][-1]
    assert last["start"] == pytest.approx(45.0)
    assert last["end"] == pytest.approx(60.0)
    assert last["has_explicit_end"] is False


def test_last_utterance_own_end_beyond_part_end_is_clamped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text = "[00:01.0 — 00:02.0] ГОЛОС 1: Первая реплика\n[00:45.0 — 01:30.0] ГОЛОС 1: Последняя"
    _patch_async_client(monkeypatch, lambda request: _model_response(text))

    result = transcribe_cloud(Path("/tmp/does-not-matter.wav"), api_key="test-key")

    last = result["segments"][-1]
    assert last["end"] == pytest.approx(60.0)
    assert last["has_explicit_end"] is False


def test_tail_after_kept_own_end_is_still_a_coverage_hole(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The kept end must not hide the unrecognised tail: it is still reported.
    text = "[00:01.0 — 00:02.0] ГОЛОС 1: Первая реплика\n[00:45.0 — 00:50.0] ГОЛОС 1: Последняя реплика"
    _patch_async_client(monkeypatch, lambda request: _model_response(text))
    monkeypatch.setenv("SYNC_CLOUD_ASR_HOLE_MIN_SEC", "5")

    result = transcribe_cloud(Path("/tmp/does-not-matter.wav"), api_key="test-key")

    coverage = result["coverage"]
    assert [round(a) for a, _ in coverage["unrecovered_ranges"]] == [2, 50]
    assert result["segments"][-1]["end"] == pytest.approx(50.0)


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
    monkeypatch.setenv("SYNC_CLOUD_ASR_PARSE_RETRIES", "0")  # проверяем учёт стоимости, не повтор
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
    monkeypatch.setenv("SYNC_CLOUD_ASR_PARSE_RETRIES", "0")  # проверяем учёт стоимости, не повтор
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


def test_find_coverage_holes_leading_gap_before_first_segment_is_hole() -> None:
    # Единственная реплика 80-90 с явным концом: начало части 0-80 не покрыто,
    # хвост 90-100 короче min_sec и дырой не считается.
    segments = [_hole_seg(80, 90, 25)]
    assert find_coverage_holes(segments, [True], 0.0, 100.0) == [(0.0, 80.0)]
    # Без собственного конца результат по началу тот же.
    assert find_coverage_holes(segments, [False], 0.0, 100.0) == [(0.0, 80.0)]
    # Длинный хвост (>= min_sec) тоже дыра, начальная идёт первой.
    assert find_coverage_holes(segments, [True], 0.0, 150.0) == [(0.0, 80.0), (90.0, 150.0)]


def test_find_coverage_holes_short_leading_gap_is_not_hole() -> None:
    segments = [_hole_seg(20, 90, 100)]
    assert find_coverage_holes(segments, [True], 0.0, 100.0) == []
    # Ровно min_sec - дыра (как и для промежутков между репликами).
    segments = [_hole_seg(30, 90, 100)]
    assert find_coverage_holes(segments, [True], 0.0, 100.0) == [(0.0, 30.0)]


def test_find_coverage_holes_leading_gap_skips_overlap_covered_by_previous_part() -> None:
    # Часть 280-600 перекрывает предыдущую на 280-300: этот кусок уже покрыт
    # предыдущей частью, поэтому начальный пропуск считается от covered_until.
    segments = [_hole_seg(320, 400, 200)]
    assert find_coverage_holes(segments, [True], 280.0, 400.0) == [(280.0, 320.0)]
    assert find_coverage_holes(segments, [True], 280.0, 400.0, covered_until=300.0) == []
    segments = [_hole_seg(340, 400, 150)]
    # Пропуск 300-340 = 40 с >= min_sec: дыра, но без уже покрытых 280-300.
    assert find_coverage_holes(segments, [True], 280.0, 400.0, covered_until=300.0) == [
        (300.0, 340.0)
    ]
    # Весь пропуск внутри перекрытия: дыры нет.
    segments = [_hole_seg(310, 400, 150)]
    assert find_coverage_holes(segments, [True], 270.0, 400.0, covered_until=300.0) == []


def test_find_coverage_holes_empty_part_is_hole() -> None:
    assert find_coverage_holes([], [], 100.0, 400.0) == [(100.0, 400.0)]
    assert find_coverage_holes([], [], 100.0, 400.0, covered_until=120.0) == [(120.0, 400.0)]
    assert find_coverage_holes([], [], 100.0, 140.0, covered_until=120.0) == []
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


def test_leading_hole_starts_at_part_start_for_first_part_and_at_covered_until_for_next(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Две части по 5 минут с перекрытием 60 с (части 0..300 и 240..540). Повторы
    # выключены, поэтому найденные дыры попадают в unrecovered_ranges как есть.
    # Часть 0: речь начинается с 01:00 -> начальная дыра от part_start (0..60).
    # Часть 1: речь начинается с абсолютных 340 с; зона 240..300 уже покрыта
    # предыдущей частью -> начальная дыра считается от covered_until (300..340).
    monkeypatch.setenv("SYNC_CLOUD_ASR_HOLE_RETRIES", "0")
    extracted: list[tuple[float, float]] = []

    async def extract(_self, _path, start, duration):
        extracted.append((start, duration))
        return b"mp3"

    monkeypatch.setattr(CloudAudioAsrAdapter, "_extract_chunk_mp3", extract)
    replies = iter(
        [
            f"[01:00 — 05:00] ГОЛОС 1: {_words(1000)}",
            f"[01:40 — 05:00] ГОЛОС 1: {_words(1000)}",
        ]
    )
    _patch_async_client(monkeypatch, lambda _request: _model_response(next(replies)))

    result = transcribe_cloud(
        Path("unused.wav"),
        api_key="test-key",
        chunk_minutes=5,
        overlap_sec=60,
        duration_sec=540.0,
    )

    assert extracted == [(0.0, 300.0), (240.0, 300.0)]
    coverage = result["coverage"]
    assert coverage["holes_detected"] == 2
    assert coverage["unrecovered_ranges"] == [[0.0, 60.0], [300.0, 340.0]]


# ---------------------------------------------------------------------------
# Локальный ремонт неисправленных дыр (stage2/local_hole_repair.py)
# ---------------------------------------------------------------------------

from stage2 import local_hole_repair  # noqa: E402

_HOLE_CLOUD_PART = (
    f"[00:00] ГОЛОС 1: {_words(99)} раз два\n"
    f"[01:00] ГОЛОС 1: {_words(10)}\n"
    f"[04:00] ГОЛОС 1: {_words(100)}"
)


class _FakeClip:
    def __init__(self, start: float, end: float) -> None:
        self.start = start
        self.end = end
        self.path = Path("clip.wav")
        self.samples = None

    def cleanup(self) -> None:
        pass


def _install_local_engine(
    monkeypatch: pytest.MonkeyPatch,
    *,
    speech_sec: float = 30.0,
    words: list[tuple[str, float, float]] | None = None,
    error: Exception | None = None,
    on_transcribe=None,
) -> dict[str, Any]:
    """Мокает ffmpeg-нарезку, VAD и транскрайбер локального ремонта."""
    monkeypatch.setenv("SYNC_CLOUD_ASR_LOCAL_REPAIR", "1")
    state: dict[str, Any] = {"clips": [], "transcribed": 0, "models": []}

    def load_clip(_audio_path, start, end, **_kwargs):
        state["clips"].append((start, end))
        return _FakeClip(start, end)

    def speech_seconds(_samples, lo, hi):
        state["vad_range"] = (lo, hi)
        return speech_sec

    def transcribe_clip(_path, model, language, progress_callback=None):
        state["transcribed"] += 1
        state["models"].append((model, language))
        if on_transcribe is not None:
            on_transcribe()
        if error is not None:
            raise error
        return [{"word": w, "start": s, "end": e} for w, s, e in words or []]

    monkeypatch.setattr(local_hole_repair, "_load_clip", load_clip)
    monkeypatch.setattr(local_hole_repair, "_speech_seconds", speech_seconds)
    monkeypatch.setattr(local_hole_repair, "_transcribe_clip", transcribe_clip)
    return state


def _hole_words(count: int, first_clip_sec: float = 10.0, step: float = 4.0):
    return [
        (f"w{i}", first_clip_sec + i * step, first_clip_sec + i * step + 0.5) for i in range(count)
    ]


def test_local_repair_silence_goes_to_silent_ranges(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _install_local_engine(monkeypatch, speech_sec=0.2)
    result, _ = _coverage_call(monkeypatch, [lambda: _model_response(_SPARSE_PART, cost=0.05)])
    monkeypatch.setenv("SYNC_CLOUD_ASR_LOCAL_REPAIR", "1")
    coverage = result["coverage"]
    assert coverage["silent_ranges"] == [[60.0, 240.0]]
    assert coverage["unrecovered_ranges"] == []
    assert coverage["unrecovered_sec"] == 0.0
    assert coverage["holes_repaired_local"] == 0
    assert coverage["local_repaired_ranges"] == []
    assert state["transcribed"] == 0
    assert sum(len(seg["text"].split()) for seg in result["segments"]) == 210
    # Речь проверяется на самой дыре (в координатах вырезки с полем 3 с).
    assert state["clips"] == [(57.0, 243.0)]
    assert state["vad_range"] == (3.0, 183.0)


def test_local_repair_inserts_words_at_absolute_time(monkeypatch: pytest.MonkeyPatch) -> None:
    # Вырезка начинается с 57 с: слова 1.0 (58 с) и 185.0 (242 с) лежат в поле
    # вокруг дыры и не должны попасть в результат — там уже есть облачные слова.
    words = [("лишнееслово", 1.0, 1.4), *_hole_words(12, 10.0, 4.0), ("хвост", 185.0, 185.4)]
    state = _install_local_engine(monkeypatch, words=words)
    result, _ = _coverage_call(monkeypatch, [lambda: _model_response(_SPARSE_PART, cost=0.05)])
    coverage = result["coverage"]
    assert state["transcribed"] == 1
    assert coverage["unrecovered_ranges"] == []
    assert coverage["unrecovered_sec"] == 0.0
    assert coverage["holes_repaired_local"] == 1
    assert coverage["local_repaired_ranges"] == [[60.0, 240.0]]
    assert coverage["silent_ranges"] == []
    segments = result["segments"]
    starts = [seg["start"] for seg in segments]
    assert starts == sorted(starts)
    local = [seg for seg in segments if seg.get("timestamp_source") == "local-whisper"]
    assert local
    local_words = [w for seg in local for w in seg["words"]]
    assert [w["word"] for w in local_words] == [f"w{i}" for i in range(12)]
    assert local_words[0]["start"] == pytest.approx(67.0)
    assert local_words[-1]["start"] == pytest.approx(57.0 + 10.0 + 11 * 4.0)
    all_text = " ".join(seg["text"] for seg in segments)
    assert "лишнееслово" not in all_text and "хвост" not in all_text
    # Старый разреженный кусок (10 «слово») заменён, облачные края на месте.
    assert sum(len(seg["text"].split()) for seg in segments) == 100 + 12 + 100
    assert all("timestamp_source" not in seg for seg in segments if seg not in local)
    assert state["models"] == [("medium", "ru")]


def test_local_repair_trims_duplicate_boundary_words(monkeypatch: pytest.MonkeyPatch) -> None:
    # Облачная реплика перед дырой заканчивается на «раз два»; локальный проход
    # начинает с тех же слов — они не должны дублироваться.
    words = [("раз", 5.0, 5.3), ("два", 5.4, 5.7), *_hole_words(12, 10.0, 4.0)]
    _install_local_engine(monkeypatch, words=words)
    result, _ = _coverage_call(monkeypatch, [lambda: _model_response(_HOLE_CLOUD_PART, cost=0.05)])
    texts = " ".join(seg["text"] for seg in result["segments"]).split()
    pairs = [(a, b) for a, b in zip(texts, texts[1:]) if (a, b) == ("раз", "два")]
    assert len(pairs) == 1
    assert result["coverage"]["unrecovered_ranges"] == []


def test_local_repair_engine_error_keeps_hole(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_local_engine(monkeypatch, error=RuntimeError("model load failed"))
    result, _ = _coverage_call(monkeypatch, [lambda: _model_response(_SPARSE_PART, cost=0.05)])
    coverage = result["coverage"]
    assert coverage["unrecovered_ranges"] == [[60.0, 240.0]]
    assert coverage["holes_repaired_local"] == 0
    assert "model load failed" in json.dumps(coverage["local_repair"], ensure_ascii=False)
    assert sum(len(seg["text"].split()) for seg in result["segments"]) == 210


def test_local_repair_not_better_keeps_hole(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_local_engine(monkeypatch, words=_hole_words(3, 10.0, 4.0))
    result, _ = _coverage_call(monkeypatch, [lambda: _model_response(_SPARSE_PART, cost=0.05)])
    assert result["coverage"]["unrecovered_ranges"] == [[60.0, 240.0]]
    assert result["coverage"]["local_repaired_ranges"] == []
    assert sum(len(seg["text"].split()) for seg in result["segments"]) == 210


def test_local_repair_disabled_by_env(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _install_local_engine(monkeypatch, words=_hole_words(10))
    monkeypatch.setenv("SYNC_CLOUD_ASR_LOCAL_REPAIR", "0")
    result, _ = _coverage_call(monkeypatch, [lambda: _model_response(_SPARSE_PART, cost=0.05)])
    coverage = result["coverage"]
    assert state["transcribed"] == 0 and state["clips"] == []
    assert coverage["unrecovered_ranges"] == [[60.0, 240.0]]
    assert coverage["holes_repaired_local"] == 0
    assert coverage["silent_ranges"] == []
    assert coverage["local_repair"]["skipped_reason"] == "disabled"


def test_clip_repair_is_left_to_the_retime_when_it_is_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from stage2 import index_retiming

    state = _install_local_engine(monkeypatch, words=_hole_words(10))
    monkeypatch.setenv("SYNC_CLOUD_ASR_RETIME", "1")
    monkeypatch.setattr(index_retiming, "_faster_whisper_available", lambda: True)
    result, _ = _coverage_call(monkeypatch, [lambda: _model_response(_SPARSE_PART, cost=0.05)])
    coverage = result["coverage"]
    assert state["transcribed"] == 0 and state["clips"] == []
    assert coverage["unrecovered_ranges"] == [[60.0, 240.0]]
    assert coverage["local_repair"]["skipped_reason"] == "handled-by-retime"


def test_local_repair_skipped_when_faster_whisper_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _install_local_engine(monkeypatch, words=_hole_words(10))
    from stage2 import whisper_runner

    monkeypatch.setattr(whisper_runner, "_module_available", lambda _module: False)
    result, _ = _coverage_call(monkeypatch, [lambda: _model_response(_SPARSE_PART, cost=0.05)])
    coverage = result["coverage"]
    assert state["transcribed"] == 0
    assert coverage["unrecovered_ranges"] == [[60.0, 240.0]]
    assert "faster-whisper" in coverage["local_repair"]["skipped_reason"]


def test_local_repair_not_attempted_without_holes(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _install_local_engine(monkeypatch, words=_hole_words(10))
    dense = f"[00:00] ГОЛОС 1: {_words(100)}\n[01:00] ГОЛОС 1: {_words(300)}\n[04:00] ГОЛОС 1: {_words(100)}"
    result, _ = _coverage_call(monkeypatch, [lambda: _model_response(dense, cost=0.05)])
    assert state["clips"] == []
    assert result["coverage"]["local_repair"].get("skipped_reason") is None
    assert result["coverage"]["holes_repaired_local"] == 0


def test_local_repair_reports_progress_and_honours_cancel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cancel = asyncio.Event()
    _install_local_engine(monkeypatch, words=_hole_words(10), on_transcribe=lambda: cancel.set())
    messages: list[str] = []
    with pytest.raises(asyncio.CancelledError):
        _coverage_call(
            monkeypatch,
            [lambda: _model_response(_SPARSE_PART, cost=0.05)],
            cancel=cancel,
            progress_callback=lambda progress: messages.append(progress.message),
        )
    assert any("Локальн" in message for message in messages)


def test_local_repair_settings_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "SYNC_CLOUD_ASR_LOCAL_REPAIR",
        "SYNC_CLOUD_ASR_LOCAL_REPAIR_MODEL",
        "SYNC_CLOUD_ASR_LOCAL_REPAIR_MARGIN_SEC",
        "SYNC_CLOUD_ASR_LOCAL_REPAIR_MIN_SPEECH_SEC",
        "SYNC_WHISPER_MODEL",
    ):
        monkeypatch.delenv(name, raising=False)
    settings = local_hole_repair.LocalRepairSettings.from_env()
    assert (settings.enabled, settings.model) == (True, "medium")
    assert (settings.margin_sec, settings.min_speech_sec) == (3.0, 1.0)
    monkeypatch.setenv("SYNC_WHISPER_MODEL", "small")
    assert local_hole_repair.LocalRepairSettings.from_env().model == "small"
    monkeypatch.setenv("SYNC_CLOUD_ASR_LOCAL_REPAIR_MODEL", "large-v3")
    monkeypatch.setenv("SYNC_CLOUD_ASR_LOCAL_REPAIR_MARGIN_SEC", "5")
    monkeypatch.setenv("SYNC_CLOUD_ASR_LOCAL_REPAIR_MIN_SPEECH_SEC", "2.5")
    monkeypatch.setenv("SYNC_CLOUD_ASR_LOCAL_REPAIR", "off")
    settings = local_hole_repair.LocalRepairSettings.from_env()
    assert (settings.enabled, settings.model) == (False, "large-v3")
    assert (settings.margin_sec, settings.min_speech_sec) == (5.0, 2.5)
    monkeypatch.setenv("SYNC_CLOUD_ASR_LOCAL_REPAIR_MARGIN_SEC", "bad")
    assert local_hole_repair.LocalRepairSettings.from_env().margin_sec == 3.0


# ---------------------------------------------------------------------------
# Ревью локального ремонта: дедлайн, реальный механизм остановки, мелочи
# ---------------------------------------------------------------------------

import threading  # noqa: E402
import tempfile  # noqa: E402

from stage2.asr_contract import AsrWord  # noqa: E402


def test_local_repair_deadline_returns_cloud_result_when_engine_cannot_be_interrupted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Загрузка модели не вызывает колбэк прогресса: поток нельзя прервать.
    release = threading.Event()
    _install_local_engine(
        monkeypatch, words=_hole_words(12), on_transcribe=lambda: release.wait(10.0)
    )
    monkeypatch.setenv("SYNC_CLOUD_ASR_LOCAL_REPAIR_DEADLINE_RESERVE_SEC", "1")
    messages: list[str] = []
    started = time.monotonic()
    try:
        result, _ = _coverage_call(
            monkeypatch,
            [lambda: _model_response(_SPARSE_PART, cost=0.05)],
            timeout_sec=2.0,
            progress_callback=lambda progress: messages.append(progress.message),
        )
        elapsed = time.monotonic() - started
        calls_at_return = len(messages)
    finally:
        release.set()
    # Платный облачный результат не потерян, общий дедлайн не наступил.
    assert elapsed < 2.0
    coverage = result["coverage"]
    assert coverage["unrecovered_ranges"] == [[60.0, 240.0]]
    assert coverage["holes_repaired_local"] == 0
    assert coverage["local_repair"]["stopped"] == "deadline"
    assert sum(len(seg["text"].split()) for seg in result["segments"]) == 210
    assert result["cost_usd"] == pytest.approx(0.05)
    # Брошенный поток после освобождения ничего не шлёт наружу.
    time.sleep(0.4)
    assert len(messages) == calls_at_return


def test_local_repair_soft_deadline_already_passed_skips_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _install_local_engine(monkeypatch, words=_hole_words(12))
    monkeypatch.setenv("SYNC_CLOUD_ASR_LOCAL_REPAIR_DEADLINE_RESERVE_SEC", "600")
    result, _ = _coverage_call(
        monkeypatch, [lambda: _model_response(_SPARSE_PART, cost=0.05)], timeout_sec=30.0
    )
    coverage = result["coverage"]
    assert state["clips"] == [] and state["transcribed"] == 0
    assert coverage["unrecovered_ranges"] == [[60.0, 240.0]]
    assert coverage["local_repair"]["stopped"] == "deadline"


def test_local_repair_own_time_limit_applies_without_global_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = threading.Event()
    _install_local_engine(
        monkeypatch, words=_hole_words(12), on_transcribe=lambda: release.wait(10.0)
    )
    monkeypatch.setenv("SYNC_CLOUD_ASR_LOCAL_REPAIR_MAX_SEC", "0.5")
    started = time.monotonic()
    try:
        result, _ = _coverage_call(monkeypatch, [lambda: _model_response(_SPARSE_PART, cost=0.05)])
        elapsed = time.monotonic() - started
    finally:
        release.set()
    assert elapsed < 5.0
    assert result["coverage"]["local_repair"]["stopped"] == "deadline"
    assert result["coverage"]["unrecovered_ranges"] == [[60.0, 240.0]]


def test_local_repair_settings_new_limits_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SYNC_CLOUD_ASR_LOCAL_REPAIR_MAX_SEC", raising=False)
    monkeypatch.delenv("SYNC_CLOUD_ASR_LOCAL_REPAIR_DEADLINE_RESERVE_SEC", raising=False)
    default = local_hole_repair.LocalRepairSettings.from_env()
    assert default.max_sec == 0.0 and default.deadline_reserve_sec > 0
    monkeypatch.setenv("SYNC_CLOUD_ASR_LOCAL_REPAIR_MAX_SEC", "90")
    monkeypatch.setenv("SYNC_CLOUD_ASR_LOCAL_REPAIR_DEADLINE_RESERVE_SEC", "7")
    settings = local_hole_repair.LocalRepairSettings.from_env()
    assert (settings.max_sec, settings.deadline_reserve_sec) == (90.0, 7.0)


class _TrackedClip(_FakeClip):
    def __init__(self, start: float, end: float, log: list[str]) -> None:
        super().__init__(start, end)
        self._log = log

    def cleanup(self) -> None:
        self._log.append("cleanup")


def _direct_repair(
    monkeypatch: pytest.MonkeyPatch, transcribe, **kwargs: Any
) -> tuple[local_hole_repair.LocalRepairOutcome, list[str]]:
    log: list[str] = []
    monkeypatch.setattr(
        local_hole_repair, "_load_clip", lambda _p, s, e, **_k: _TrackedClip(s, e, log)
    )
    monkeypatch.setattr(local_hole_repair, "_speech_seconds", lambda *_a: 30.0)
    monkeypatch.setattr(local_hole_repair, "_transcribe_clip", transcribe)
    segments = [AsrSegment(text="a b", start=0.0, end=5.0, words=[], speaker="ГОЛОС 1")]
    outcome = local_hole_repair.repair_holes_locally(
        Path("x.wav"),
        [(60.0, 120.0), (200.0, 260.0)],
        segments,
        [True],
        duration_sec=300.0,
        language="ru",
        settings=local_hole_repair.LocalRepairSettings(),
        **kwargs,
    )
    return outcome, log


def test_engine_progress_callback_raises_stop_on_cancel(monkeypatch: pytest.MonkeyPatch) -> None:
    cancel = threading.Event()

    def transcribe(_path, _model, _language, progress_callback=None):
        cancel.set()  # отмена приходит, пока движок работает
        progress_callback(1.0, 10.0, 0)  # _Stop должен пройти сквозь вызов
        raise AssertionError("движок не был остановлен колбэком")

    outcome, log = _direct_repair(monkeypatch, transcribe, cancel=cancel)
    assert outcome.stopped == "cancelled"
    assert sorted(outcome.unrecovered) == [(60.0, 120.0), (200.0, 260.0)]
    assert outcome.repaired == [] and log == ["cleanup"]


def test_engine_progress_callback_raises_stop_on_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    def transcribe(_path, _model, _language, progress_callback=None):
        time.sleep(0.15)  # дедлайн наступает во время работы движка
        progress_callback(1.0, 10.0, 0)
        raise AssertionError("движок не был остановлен колбэком")

    outcome, log = _direct_repair(
        monkeypatch, transcribe, deadline_monotonic=time.monotonic() + 0.05
    )
    assert outcome.stopped == "deadline"
    assert sorted(outcome.unrecovered) == [(60.0, 120.0), (200.0, 260.0)]
    assert log == ["cleanup"]
    assert outcome.report["stopped"] == "deadline"


def test_abort_event_stops_engine_like_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    abort = threading.Event()

    def transcribe(_path, _model, _language, progress_callback=None):
        abort.set()
        progress_callback(1.0, 10.0, 0)
        raise AssertionError("движок не был остановлен колбэком")

    outcome, log = _direct_repair(monkeypatch, transcribe, abort=abort)
    assert outcome.stopped == "deadline" and log == ["cleanup"]


def test_worker_key_includes_local_repair_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    from stage2.whisper_process import _worker_key

    names = [
        "SYNC_CLOUD_ASR_LOCAL_REPAIR",
        "SYNC_CLOUD_ASR_LOCAL_REPAIR_MODEL",
        "SYNC_CLOUD_ASR_LOCAL_REPAIR_MARGIN_SEC",
        "SYNC_CLOUD_ASR_LOCAL_REPAIR_MIN_SPEECH_SEC",
        "SYNC_CLOUD_ASR_LOCAL_REPAIR_MAX_SEC",
        "SYNC_CLOUD_ASR_LOCAL_REPAIR_DEADLINE_RESERVE_SEC",
        "SYNC_CLOUD_ASR_HOLE_VAD",
    ]
    for name in names:
        monkeypatch.delenv(name, raising=False)
    base = _worker_key("cloud-audio", "m", "ru", "fork")
    for name, value in zip(names, ["0", "large-v3", "5", "2", "60", "9", "0"]):
        monkeypatch.setenv(name, value)
        changed = _worker_key("cloud-audio", "m", "ru", "fork")
        assert changed != base, name
        monkeypatch.delenv(name)
        assert _worker_key("cloud-audio", "m", "ru", "fork") == base


def test_rescale_does_not_scale_segments_with_real_timestamps() -> None:
    cloud = AsrSegment(text="облако", start=0.0, end=100.0, words=[])
    local = AsrSegment(
        text="точно",
        start=50.0,
        end=51.0,
        words=[AsrWord(word="точно", start=50.0, end=51.0)],
        timestamp_source="local-whisper",
    )
    scaled, scale = _rescale_stretched_timestamps([cloud, local], 50.0)
    assert scale == pytest.approx(0.5)
    assert (scaled[0].start, scaled[0].end) == (0.0, 50.0)
    assert (scaled[1].start, scaled[1].end) == (50.0, 51.0)
    assert [(w.start, w.end) for w in scaled[1].words] == [(50.0, 51.0)]


def test_local_repair_skipped_when_timestamp_scale_is_stretched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _install_local_engine(monkeypatch, words=_hole_words(12))
    # Модель выдала метки далеко за концом файла (300 с): шкала будет сжата,
    # дыры посчитаны в неверном времени.
    stretched = _SPARSE_PART + f"\n[09:00] ГОЛОС 1: {_words(100)}"
    result, _ = _coverage_call(monkeypatch, [lambda: _model_response(stretched, cost=0.05)])
    assert state["clips"] == [] and state["transcribed"] == 0
    local = result["coverage"]["local_repair"]
    assert "scale" in local["skipped_reason"]
    assert result["coverage"]["holes_repaired_local"] == 0


def test_local_repair_progress_exception_keeps_repaired_holes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_local_engine(monkeypatch, words=_hole_words(12))

    def progress(progress):
        if progress.message.startswith("Локальн"):
            raise RuntimeError("ui exploded")

    result, _ = _coverage_call(
        monkeypatch,
        [lambda: _model_response(_SPARSE_PART, cost=0.05)],
        progress_callback=progress,
    )
    coverage = result["coverage"]
    assert coverage["local_repaired_ranges"] == [[60.0, 240.0]]
    assert coverage["unrecovered_ranges"] == []


def test_local_repair_progress_never_goes_backwards(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_local_engine(monkeypatch, words=_hole_words(12))
    seen: list[tuple[str, float | None]] = []
    _coverage_call(
        monkeypatch,
        [lambda: _model_response(_SPARSE_PART, cost=0.05)],
        progress_callback=lambda p: seen.append((p.message, p.processed_sec)),
    )
    assert any(message.startswith("Локальн") for message, _ in seen)
    values = [value for _, value in seen if value is not None]
    assert values == sorted(values)


def test_local_repair_inserted_segments_inherit_neighbour_speaker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_local_engine(monkeypatch, words=_hole_words(12))
    result, _ = _coverage_call(monkeypatch, [lambda: _model_response(_SPARSE_PART, cost=0.05)])
    local = [seg for seg in result["segments"] if seg.get("timestamp_source") == "local-whisper"]
    assert local and all(seg["speaker"] == "ГОЛОС 1" for seg in local)


def test_local_repair_speaker_falls_back_to_following_segment() -> None:
    previous = AsrSegment(text="а б", start=0.0, end=5.0, words=[], speaker=None)
    following = AsrSegment(text="в г", start=300.0, end=305.0, words=[], speaker="ГОЛОС 2")
    words = [{"word": f"w{i}", "start": 10.0 + i * 4.0, "end": 10.5 + i * 4.0} for i in range(12)]
    spliced = local_hole_repair._splice(
        [previous, following], [True, True], (60.0, 240.0), words, 50.0
    )
    assert spliced is not None
    inserted = [s for s in spliced[0] if s.timestamp_source == "local-whisper"]
    assert inserted and all(s.speaker == "ГОЛОС 2" for s in inserted)


def test_local_repair_model_ignores_cloud_whisper_model_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SYNC_CLOUD_ASR_LOCAL_REPAIR_MODEL", raising=False)
    monkeypatch.setenv("SYNC_WHISPER_MODEL", "google/gemini-3.7-flash")
    assert local_hole_repair.LocalRepairSettings.from_env().model == "medium"
    monkeypatch.setenv("SYNC_WHISPER_MODEL", "mlx-community/whisper-small-mlx")
    assert local_hole_repair.LocalRepairSettings.from_env().model == "medium"
    monkeypatch.setenv("SYNC_WHISPER_MODEL", "small")
    assert local_hole_repair.LocalRepairSettings.from_env().model == "small"
    # Явная настройка ремонта главнее.
    monkeypatch.setenv("SYNC_CLOUD_ASR_LOCAL_REPAIR_MODEL", "Systran/faster-whisper-small")
    assert local_hole_repair.LocalRepairSettings.from_env().model == "Systran/faster-whisper-small"


def test_stale_local_repair_tempdirs_are_removed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    old_time = time.time() - 2 * 3600
    stale = tmp_path / "rough_cut_local_repair_old"
    fresh = tmp_path / "rough_cut_local_repair_new"
    foreign = tmp_path / "other_prefix_old"
    for directory in (stale, fresh, foreign):
        directory.mkdir()
        (directory / "clip.wav").write_bytes(b"x")
    stale_file = tmp_path / "rough_cut_local_repair_file"
    stale_file.write_bytes(b"x")
    import os

    for path in (stale, foreign, stale_file):
        os.utime(path, (old_time, old_time))
    _direct_repair(monkeypatch, lambda *_a, **_k: [])
    assert not stale.exists()
    assert fresh.exists() and foreign.exists() and stale_file.exists()


# ---------------------------------------------------------------------------
# VAD-проверка дыр до платного облачного повтора (SYNC_CLOUD_ASR_HOLE_VAD)
# ---------------------------------------------------------------------------


def _install_hole_vad(
    monkeypatch: pytest.MonkeyPatch,
    intervals: list[tuple[float, float]] | Exception,
) -> dict[str, Any]:
    """Включает VAD-проверку дыр; мокает нарезку и VAD (интервалы в секундах
    от начала вырезки)."""
    monkeypatch.setenv("SYNC_CLOUD_ASR_HOLE_VAD", "1")
    monkeypatch.setenv("SYNC_CLOUD_ASR_HOLE_RETRIES", "1")
    monkeypatch.setenv("SYNC_CLOUD_ASR_HOLE_SPLIT_SEC", "600")
    state: dict[str, Any] = {"clips": [], "vad": []}

    def load_clip(_audio_path, start, end, **_kwargs):
        state["clips"].append((start, end))
        return _FakeClip(start, end)

    def speech_intervals(_samples, lo, hi):
        state["vad"].append((lo, hi))
        if isinstance(intervals, Exception):
            raise intervals
        return list(intervals)

    monkeypatch.setattr(local_hole_repair, "_load_clip", load_clip)
    monkeypatch.setattr(local_hole_repair, "_speech_intervals", speech_intervals)
    return state


def test_hole_vad_silence_is_not_sent_to_paid_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _install_hole_vad(monkeypatch, [(5.0, 5.4)])
    # Второго ответа нет: платный повтор по тишине вызвал бы StopIteration.
    result, extracted = _coverage_call(
        monkeypatch, [lambda: _model_response(_SPARSE_PART, cost=0.05)]
    )

    assert extracted == [(0.0, 300.0)]
    assert result["cost_usd"] == pytest.approx(0.05)
    coverage = result["coverage"]
    assert coverage["silent_ranges"] == [[60.0, 240.0]]
    assert coverage["silent_sec"] == 180.0
    assert coverage["holes_detected"] == 1
    assert coverage["holes_skipped_silent"] == 1
    assert coverage["holes_repaired"] == 0
    assert coverage["unrecovered_ranges"] == []
    assert coverage["hole_vad"]["enabled"] is True
    # Речь проверяется на самой дыре (вырезка с полем 3 с).
    assert state["clips"] == [(57.0, 243.0)]
    assert state["vad"] == [(3.0, 183.0)]


def test_hole_vad_speech_is_sent_to_paid_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_hole_vad(monkeypatch, [(3.0, 183.0)])
    retry = f"[00:03] ГОЛОС 1: {_words(150)}\n[01:30] ГОЛОС 1: {_words(150)}"
    result, extracted = _coverage_call(
        monkeypatch,
        [
            lambda: _model_response(_SPARSE_PART, cost=0.05),
            lambda: _model_response(retry, cost=0.02),
        ],
    )
    assert extracted == [(0.0, 300.0), (57.0, 186.0)]
    assert result["cost_usd"] == pytest.approx(0.07)
    coverage = result["coverage"]
    assert coverage["silent_ranges"] == []
    assert coverage["holes_skipped_silent"] == 0
    assert coverage["holes_repaired"] == 1


def test_hole_vad_error_falls_back_to_paid_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_hole_vad(monkeypatch, RuntimeError("vad broke"))
    retry = f"[00:03] ГОЛОС 1: {_words(150)}\n[01:30] ГОЛОС 1: {_words(150)}"
    result, extracted = _coverage_call(
        monkeypatch,
        [
            lambda: _model_response(_SPARSE_PART, cost=0.05),
            lambda: _model_response(retry, cost=0.02),
        ],
    )
    assert extracted == [(0.0, 300.0), (57.0, 186.0)]
    coverage = result["coverage"]
    assert coverage["silent_ranges"] == []
    assert coverage["holes_repaired"] == 1
    assert coverage["hole_vad"]["errors"][0]["range"] == [60.0, 240.0]
    assert "vad broke" in coverage["hole_vad"]["errors"][0]["error"]


def test_hole_vad_disabled_keeps_old_behaviour(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _install_hole_vad(monkeypatch, [])  # тишина, но проверка выключена
    monkeypatch.setenv("SYNC_CLOUD_ASR_HOLE_VAD", "0")
    result, extracted = _coverage_call(
        monkeypatch,
        [
            lambda: _model_response(_SPARSE_PART, cost=0.05),
            lambda: _model_response(f"[00:03] ГОЛОС 1: {_words(5)}", cost=0.01),
        ],
    )
    assert extracted == [(0.0, 300.0), (57.0, 186.0)]
    assert state["clips"] == [] and state["vad"] == []
    coverage = result["coverage"]
    assert coverage["silent_ranges"] == []
    assert coverage["holes_skipped_silent"] == 0
    assert coverage["hole_vad"] == {"enabled": False, "skipped_reason": "disabled"}


def test_hole_vad_without_faster_whisper_keeps_old_behaviour(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from stage2 import whisper_runner

    state = _install_hole_vad(monkeypatch, [])
    monkeypatch.setattr(whisper_runner, "_module_available", lambda _module: False)
    result, extracted = _coverage_call(
        monkeypatch,
        [
            lambda: _model_response(_SPARSE_PART, cost=0.05),
            lambda: _model_response(f"[00:03] ГОЛОС 1: {_words(5)}", cost=0.01),
        ],
    )
    assert extracted == [(0.0, 300.0), (57.0, 186.0)]
    assert state["clips"] == []
    coverage = result["coverage"]
    assert coverage["silent_ranges"] == []
    assert coverage["hole_vad"]["enabled"] is False
    assert "faster-whisper" in coverage["hole_vad"]["skipped_reason"]


def test_hole_vad_narrows_hole_to_speech_with_margin(monkeypatch: pytest.MonkeyPatch) -> None:
    # Дыра 60..240, вырезка 57..243: речь на 100..110 и 111..115 (абс.) —
    # в координатах вырезки 43..53 и 54..58. Поле 1.5 с, зазор 1 с < 3 с.
    _install_hole_vad(monkeypatch, [(43.0, 53.0), (54.0, 58.0)])
    retry = f"[00:03] ГОЛОС 1: {_words(60)}"
    result, extracted = _coverage_call(
        monkeypatch,
        [
            lambda: _model_response(_SPARSE_PART, cost=0.05),
            lambda: _model_response(retry, cost=0.02),
        ],
    )
    # Облаку уходит только 98.5..116.5 (+3 с поля вырезки с каждой стороны).
    assert extracted == [(0.0, 300.0), (95.5, 24.0)]
    coverage = result["coverage"]
    assert coverage["silent_ranges"] == [[60.0, 98.5], [116.5, 240.0]]
    assert coverage["holes_detected"] == 1
    assert coverage["holes_skipped_silent"] == 0
    assert coverage["hole_vad"]["narrowed"] == 1


def test_hole_vad_edge_gap_shorter_than_min_gap_keeps_hole_whole(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Речь почти по всей дыре: вырезать нечего, дыра уходит целиком.
    _install_hole_vad(monkeypatch, [(4.0, 182.0)])
    result, extracted = _coverage_call(
        monkeypatch,
        [
            lambda: _model_response(_SPARSE_PART, cost=0.05),
            lambda: _model_response(f"[00:03] ГОЛОС 1: {_words(5)}", cost=0.01),
        ],
    )
    assert extracted == [(0.0, 300.0), (57.0, 186.0)]
    assert result["coverage"]["silent_ranges"] == []
    assert result["coverage"]["hole_vad"]["narrowed"] == 0


def test_hole_vad_cancel_before_paid_retry_stops_without_extra_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_hole_vad(monkeypatch, [(3.0, 183.0)])
    cancel = asyncio.Event()

    def vad_and_cancel(_samples, lo, hi):
        cancel.set()
        return [(3.0, 183.0)]

    monkeypatch.setattr(local_hole_repair, "_speech_intervals", vad_and_cancel)
    with pytest.raises(asyncio.CancelledError):
        _coverage_call(
            monkeypatch,
            [lambda: _model_response(_SPARSE_PART, cost=0.05)],
            cancel=cancel,
        )


def test_hole_vad_unit_narrow_to_speech() -> None:
    narrow = local_hole_repair._narrow_to_speech
    # Две речевые группы далеко друг от друга и от краёв.
    keep, silent = narrow((0.0, 100.0), [(20.0, 22.0), (70.0, 72.0)])
    assert keep == [(18.5, 23.5), (68.5, 73.5)]
    assert silent == [(0.0, 18.5), (23.5, 68.5), (73.5, 100.0)]
    # Нет речи в интервалах — дыра как есть.
    assert narrow((0.0, 10.0), []) == ([(0.0, 10.0)], [])
