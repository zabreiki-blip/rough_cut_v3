"""Устойчивость облачного ASR к «плохим» ответам модели.

- JSON-ответ ``{"splits": [...]}`` разбирается явным адаптером;
- пустой ответ / ``None`` - контролируемый отказ части с понятной причиной
  (после ограниченного числа повторов);
- успешно распознанные части сохраняются между запусками: повторно
  запрашиваются (и оплачиваются) только неудавшиеся.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from stage2 import cloud_asr
from stage2.cloud_asr import CloudAudioAsrAdapter, parse_transcript, transcribe_cloud

# Autouse-фикстура основного файла (фиксированная длительность, без ffmpeg).
from test_stage2_cloud_asr import (  # noqa: F401
    _model_response,
    _no_real_ffmpeg_ffprobe,
    _patch_async_client,
)

@pytest.fixture(autouse=True)
def _isolated_chunk_cache(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Кэш частей каждого теста - в своём каталоге (не общий temp)."""
    monkeypatch.setenv("SYNC_CLOUD_ASR_CHUNK_CACHE_DIR", str(tmp_path / "asr-cache"))


SPLITS = {
    "splits": [
        {"start": "00:00.0", "end": "00:07.1", "speaker": "ГОЛОС 1", "text": "Первая реплика"},
        {"start": "00:07.5", "end": "00:12.0", "speaker": "ГОЛОС 2", "text": "Вторая: с двоеточием"},
    ]
}


def test_json_splits_are_parsed_by_explicit_adapter() -> None:
    segments = parse_transcript(json.dumps(SPLITS, ensure_ascii=False), offset_sec=100.0)

    assert [s.text for s in segments] == ["Первая реплика", "Вторая: с двоеточием"]
    assert [s.speaker for s in segments] == ["ГОЛОС 1", "ГОЛОС 2"]
    assert segments[0].start == pytest.approx(100.0)
    assert segments[0].end == pytest.approx(107.1)
    assert segments[1].start == pytest.approx(107.5)
    assert segments[1].end == pytest.approx(112.0)


def test_json_splits_in_markdown_fence_with_numeric_seconds() -> None:
    payload = {"splits": [{"start": 1.5, "end": 4, "speaker": 3, "text": "Число секунд"}]}
    text = "```json\n" + json.dumps(payload) + "\n```"

    (segment,) = parse_transcript(text)

    assert (segment.start, segment.end) == (pytest.approx(1.5), pytest.approx(4.0))
    assert segment.speaker == "ГОЛОС 3"
    assert segment.text == "Число секунд"


def test_json_without_usable_splits_is_a_controlled_parse_error() -> None:
    with pytest.raises(cloud_asr.TranscriptParseError):
        parse_transcript(json.dumps({"splits": []}))
    with pytest.raises(cloud_asr.TranscriptParseError):
        parse_transcript(json.dumps({"splits": [{"start": "bad", "text": "x"}]}))


@pytest.mark.parametrize("text", ["", "   \n", "None"])
def test_empty_or_none_text_is_a_controlled_parse_error(text: str) -> None:
    with pytest.raises(cloud_asr.TranscriptParseError, match="пуст"):
        parse_transcript(text)


def test_transcribe_cloud_accepts_json_splits_response(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_async_client(
        monkeypatch, lambda request: _model_response(json.dumps(SPLITS, ensure_ascii=False))
    )

    result = transcribe_cloud(Path("/tmp/does-not-matter.wav"), api_key="test-key")

    assert [s["text"] for s in result["segments"]] == ["Первая реплика", "Вторая: с двоеточием"]


def _none_response(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200, json={"choices": [{"message": {"content": None}}], "usage": {"cost": 0.01}}
    )


def test_none_content_fails_with_clear_reason_after_bounded_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return _none_response(request)

    _patch_async_client(monkeypatch, handler)

    with pytest.raises(cloud_asr.CloudAudioAsrError, match="пуст") as exc_info:
        transcribe_cloud(Path("/tmp/does-not-matter.wav"), api_key="test-key")

    assert calls["n"] == 2  # один повтор по умолчанию, не больше
    assert "часть" in str(exc_info.value)
    assert exc_info.value.cost_usd is None or exc_info.value.cost_usd >= 0


def test_parse_retries_can_be_disabled_by_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SYNC_CLOUD_ASR_PARSE_RETRIES", "0")
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return _none_response(request)

    _patch_async_client(monkeypatch, handler)

    with pytest.raises(cloud_asr.CloudAudioAsrError):
        transcribe_cloud(Path("/tmp/does-not-matter.wav"), api_key="test-key")

    assert calls["n"] == 1


def test_transient_none_is_recovered_by_one_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    good = "[00:01.0 — 00:02.0] ГОЛОС 1: Реплика"
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return _none_response(request) if calls["n"] == 1 else _model_response(good, cost=0.02)

    _patch_async_client(monkeypatch, handler)

    result = transcribe_cloud(Path("/tmp/does-not-matter.wav"), api_key="test-key")

    assert calls["n"] == 2
    assert [s["text"] for s in result["segments"]] == ["Реплика"]
    assert result["cost_usd"] == pytest.approx(0.03)  # оба платных запроса учтены


# -- сохранение успешных частей между запусками ------------------------------


def _three_chunks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SYNC_CLOUD_ASR_PARSE_RETRIES", "0")
    monkeypatch.setattr(
        CloudAudioAsrAdapter,
        "_plan_chunks",
        lambda self, duration_sec: [(0.0, 20.0), (20.0, 40.0), (40.0, 60.0)],
    )


def _chunk_text(n: int) -> str:
    return f"[00:01.0 — 00:02.0] ГОЛОС 1: Реплика части {n}"


def test_successful_parts_are_not_requested_again_on_rerun(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _three_chunks(monkeypatch)
    audio = tmp_path / "guide.wav"
    audio.write_bytes(b"RIFF" + b"0" * 64)
    prompts: list[str] = []
    fail_part_three = {"on": True}

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        prompts.append(str(payload["messages"]))
        if fail_part_three["on"] and len(prompts) == 3:
            return _none_response(request)
        # Во втором запуске запрашивается только часть 3.
        return _model_response(_chunk_text(len(prompts) if fail_part_three["on"] else 3), cost=0.02)

    _patch_async_client(monkeypatch, handler)

    with pytest.raises(cloud_asr.CloudAudioAsrError, match="части 3 из 3"):
        transcribe_cloud(audio, api_key="test-key")
    assert len(prompts) == 3

    fail_part_three["on"] = False
    result = transcribe_cloud(audio, api_key="test-key")

    assert len(prompts) == 4  # повторена только часть 3
    assert [s["text"] for s in result["segments"]] == [
        "Реплика части 1",
        "Реплика части 2",
        "Реплика части 3",
    ]
    # Оплачена только новая часть: две первые уже оплачены прошлым запуском.
    assert result["cost_usd"] == pytest.approx(0.02)
    # Кэш частей убирается после полного успеха.
    assert not list(tmp_path.glob(".*asr-chunks*"))


def test_changed_audio_invalidates_saved_parts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _three_chunks(monkeypatch)
    audio = tmp_path / "guide.wav"
    audio.write_bytes(b"RIFF" + b"0" * 64)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        # Часть 3 первого запуска падает, остальные успешны.
        if calls["n"] == 3:
            return _none_response(request)
        return _model_response(_chunk_text(calls["n"]), cost=0.02)

    _patch_async_client(monkeypatch, handler)
    with pytest.raises(cloud_asr.CloudAudioAsrError):
        transcribe_cloud(audio, api_key="test-key")
    assert calls["n"] == 3

    audio.write_bytes(b"RIFF" + b"1" * 99)  # запись заменили
    transcribe_cloud(audio, api_key="test-key")

    assert calls["n"] == 6  # все три части заново: старый кэш не применяется


# -- повторы части: границы, бюджет, дедлайн ---------------------------------


def test_parse_retries_env_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SYNC_CLOUD_ASR_PARSE_RETRIES", "99")
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return _none_response(request)

    _patch_async_client(monkeypatch, handler)

    with pytest.raises(cloud_asr.CloudAudioAsrError):
        transcribe_cloud(Path("/tmp/does-not-matter.wav"), api_key="test-key")

    assert calls["n"] == 1 + cloud_asr.MAX_PARSE_RETRIES


def test_unparseable_text_is_retried_and_reason_names_response_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return _model_response("просто текст без таймкодов", cost=0.01)

    _patch_async_client(monkeypatch, handler)

    with pytest.raises(cloud_asr.CloudAudioAsrError) as exc_info:
        transcribe_cloud(Path("/tmp/does-not-matter.wav"), api_key="test-key")

    assert calls["n"] == 2
    assert "части 1 из 1" in str(exc_info.value)
    assert "без разметки таймкодов" in str(exc_info.value)
    assert exc_info.value.cost_usd == pytest.approx(0.02)  # оба платных запроса учтены


def test_parse_retry_goes_through_budget_and_is_refused_when_exhausted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"n": 0}
    snapshots: list[dict[str, float]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return _none_response(request)  # cost 0.01

    _patch_async_client(monkeypatch, handler)

    # Оценка попытки 60 с = 0.06; лимита 0.065 хватает на первую попытку, но
    # не на повтор (0.01 потрачено + 0.06 резерва > 0.065).
    with pytest.raises(Exception) as exc_info:
        transcribe_cloud(
            Path("/tmp/does-not-matter.wav"),
            api_key="test-key",
            max_cost_usd="0.065",
            usd_per_minute_estimate="0.06",
            budget_callback=snapshots.append,
        )

    assert type(exc_info.value).__name__ == "CostBudgetExceeded"
    assert calls["n"] == 1
    assert len(snapshots) >= 2  # резерв и учёт первой попытки прошли через callback
    assert getattr(exc_info.value, "cost_usd", None) == pytest.approx(0.01)


def test_parse_retry_respects_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    import time

    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        time.sleep(0.3)  # съедает весь дедлайн до решения о повторе
        return _none_response(request)

    _patch_async_client(monkeypatch, handler)

    with pytest.raises(TimeoutError):
        transcribe_cloud(Path("/tmp/does-not-matter.wav"), api_key="test-key", timeout_sec=0.1)

    assert calls["n"] == 1


# -- кэш успешных частей: расположение, ключ, атомарность ---------------------


def test_chunk_cache_lives_in_given_dir_not_beside_media(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _three_chunks(monkeypatch)
    audio = tmp_path / "media" / "guide.wav"
    audio.parent.mkdir()
    audio.write_bytes(b"RIFF" + b"0" * 64)
    cache_dir = tmp_path / "project" / "work" / "transcripts" / "asr-chunks"
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return _none_response(request) if calls["n"] == 3 else _model_response(_chunk_text(calls["n"]))

    _patch_async_client(monkeypatch, handler)

    with pytest.raises(cloud_asr.CloudAudioAsrError):
        transcribe_cloud(audio, api_key="test-key", chunk_cache_dir=cache_dir)

    files = list(cache_dir.iterdir())
    assert len(files) == 1 and files[0].suffix == ".json"  # без *.tmp: запись атомарна
    saved = json.loads(files[0].read_text(encoding="utf-8"))
    assert sorted(saved["chunks"]) == ["0", "1"]  # только успешные части
    assert [p.name for p in audio.parent.iterdir()] == ["guide.wav"]  # рядом с медиа пусто

    result = transcribe_cloud(audio, api_key="test-key", chunk_cache_dir=cache_dir)

    assert calls["n"] == 4
    assert len(result["segments"]) == 3
    assert not list(cache_dir.iterdir())  # убрано после полного успеха


def test_changed_model_invalidates_saved_parts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _three_chunks(monkeypatch)
    audio = tmp_path / "guide.wav"
    audio.write_bytes(b"RIFF" + b"0" * 64)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return _none_response(request) if calls["n"] == 3 else _model_response(_chunk_text(calls["n"]))

    _patch_async_client(monkeypatch, handler)
    with pytest.raises(cloud_asr.CloudAudioAsrError):
        transcribe_cloud(audio, api_key="test-key", model="model-a")
    assert calls["n"] == 3

    transcribe_cloud(audio, api_key="test-key", model="model-b")

    assert calls["n"] == 6  # другая модель - сохранённые части не применяются


# -- частично битый JSON: не молча ---------------------------------------------


def test_partially_broken_json_splits_warns_and_leaves_detectable_hole(
    caplog: pytest.LogCaptureFixture,
) -> None:
    payload = {
        "splits": [
            {"start": "00:00.0", "end": "00:05.0", "speaker": "ГОЛОС 1", "text": "Начало"},
            {"end": "00:50.0", "speaker": "ГОЛОС 1", "text": "Нет start"},
            {"start": "00:10.0", "speaker": "ГОЛОС 1"},  # нет text
            {"start": "00:55.0", "end": "00:58.0", "speaker": "ГОЛОС 1", "text": "Конец"},
        ]
    }

    with caplog.at_level("WARNING", logger="stage2.cloud_asr"):
        segments, own_end = cloud_asr._parse_transcript_with_own_end(json.dumps(payload))

    assert [s.text for s in segments] == ["Начало", "Конец"]
    assert any("пропущено 2 из 4" in record.getMessage() for record in caplog.records)
    # Выпавшие реплики оставили разрыв 5..55 с: его находит поиск дыр покрытия.
    holes = cloud_asr.find_coverage_holes(segments, own_end, 0.0, 60.0)
    assert holes == [(5.0, 55.0)]
