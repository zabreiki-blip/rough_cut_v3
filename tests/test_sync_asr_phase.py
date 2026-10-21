from __future__ import annotations

import copy
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from cost_budget import CostBudgetExceeded
from sync_asr_phase import SyncAsrContext, SyncAsrResult, run_asr_phase
from sync_cost_tracker import SyncCostTracker, _AsrMetrics

_CACHE_KEY = "transcription_cache"


class _Cancelled(Exception):
    pass


class _WhisperCancelled(Exception):
    pass


def _source(source_id: str) -> Any:
    return SimpleNamespace(id=source_id, duration_sec=10.0, source_kind="review_media")


def _index(source_id: str) -> Any:
    return SimpleNamespace(source_id=source_id, metadata={_CACHE_KEY: {"audio": {}}})


class _Harness:
    def __init__(self, tmp_path: Path, source_ids: list[str]) -> None:
        self.tmp_path = tmp_path
        self.updates: list[tuple[tuple, dict]] = []
        self.transcribe_calls: list[str] = []
        self.transcribe_errors: dict[str, Exception] = {}
        self.cached: dict[str, Any] = {}
        self.saved: list[Path] = []
        self.cancel_event = threading.Event()
        for source_id in source_ids:
            (tmp_path / f"{source_id}.wav").write_bytes(b"audio")
        self.metrics: _AsrMetrics = {
            "asr_backend_used": "",
            "asr_cost_usd_total": 0.0,
            "asr_timestamp_precision": "",
            "transcription_wall_sec_total": 0.0,
        }
        self.tracker = SyncCostTracker(
            job_id="job-1",
            update_job=self._update,
            should_persist=lambda: False,
            asr_metrics=self.metrics,
        )

    def _update(self, *args: Any, **kwargs: Any) -> None:
        self.updates.append((args, kwargs))

    def _ensure_not_cancelled(self) -> None:
        if self.cancel_event.is_set():
            raise _Cancelled("Sync job cancelled")

    def _load(self, path: Path, source_id: str) -> Any:
        return self.cached[source_id]

    def _save(self, path: Path, index: Any) -> None:
        self.saved.append(path)
        path.write_text("{}")

    def _transcribe(self, audio_path: Path, **_kwargs: Any) -> dict[str, Any]:
        source_id = audio_path.stem
        self.transcribe_calls.append(source_id)
        error = self.transcribe_errors.get(source_id)
        if error is not None:
            raise error
        return {"model": "m", "language": "ru", "backend": "faster-whisper", "words": []}

    def context(self, **overrides: Any) -> SyncAsrContext:
        values: dict[str, Any] = {
            "job_id": "job-1",
            "paths": self.tmp_path,
            "backend": "faster-whisper",
            "model": "small",
            "language": "ru",
            "source_ids": (),
            "force_transcribe": False,
            "cloud_source_options": None,
            "cancel_event": self.cancel_event,
            "ensure_not_cancelled": self._ensure_not_cancelled,
            "should_persist_progress": lambda: False,
            "asr_usage_callback": lambda _event: None,
            "asr_semaphore": threading.Semaphore(1),
            "getenv": lambda _name: None,
            "monotonic": lambda: 0.0,
            "app_config": {},
            "job_cancelled_error": _Cancelled,
            "whisper_cancelled_error": _WhisperCancelled,
            "transcription_cache_key": _CACHE_KEY,
            "update_job": self._update,
            "utc_now_iso": lambda: "2026-01-01T00:00:00+00:00",
            "word_index_path": lambda paths, sid: self.tmp_path / f"{sid}.index.json",
            "transcription_config": lambda **kw: {"backend": kw["backend"], "model": kw["model"]},
            "resolve_audio_path": lambda source, paths: self.tmp_path / f"{source.id}.wav",
            "load_word_index": self._load,
            "save_word_index": self._save,
            "rescale_stretched_word_index": lambda index, duration: None,
            "transcription_cache_metadata": lambda *a, **kw: {"audio": {}},
            "audio_stat_signature": lambda path: {},
            "audio_cache_signature": lambda path: {"sample_sha256": "x"},
            "word_index_audio_matches": lambda *a, **kw: True,
            "transcribe_audio": self._transcribe,
            "ingest_word_index": lambda result, sid, **kw: _index(sid),
        }
        values.update(overrides)
        return SyncAsrContext(**values)

    def run(self, sources: list[Any], **overrides: Any) -> SyncAsrResult:
        return run_asr_phase(
            self.context(**overrides),
            sources,
            cost_tracker=self.tracker,
            asr_metrics=self.metrics,
        )


def test_reuses_ready_index_without_transcription(tmp_path: Path) -> None:
    h = _Harness(tmp_path, ["a"])
    h.cached["a"] = _index("a")
    (tmp_path / "a.index.json").write_text("{}")

    result = h.run([_source("a")])

    assert [item.source_id for item in result.indexes] == ["a"]
    assert (result.reused, result.transcribed, result.source_failures) == (1, 0, [])
    assert h.transcribe_calls == []
    assert h.updates[-1] == (
        ("job-1",),
        {"transcribed": 0, "reused_indexes": 1, "source_failures": []},
    )


def test_transcribes_missing_index_and_saves_it(tmp_path: Path) -> None:
    h = _Harness(tmp_path, ["a"])

    result = h.run([_source("a")])

    assert [item.source_id for item in result.indexes] == ["a"]
    assert (result.reused, result.transcribed) == (0, 1)
    assert h.transcribe_calls == ["a"]
    assert h.saved == [tmp_path / "a.index.json"]
    assert h.metrics["asr_backend_used"] == "faster-whisper"
    assert h.metrics["asr_timestamp_precision"] == "word"


def test_transcription_error_is_recorded_and_next_source_processed(tmp_path: Path) -> None:
    h = _Harness(tmp_path, ["a", "b"])
    h.transcribe_errors["a"] = RuntimeError("boom")

    result = h.run([_source("a"), _source("b")])

    assert h.transcribe_calls == ["a", "b"]
    assert result.source_failures == [{"source_id": "a", "error": "boom"}]
    assert [item.source_id for item in result.indexes] == ["b"]
    assert (result.reused, result.transcribed) == (0, 1)
    # The last progress update carries both the counters and the failure.
    assert h.updates[-1][1] == {
        "transcribed": 1,
        "reused_indexes": 0,
        "source_failures": [{"source_id": "a", "error": "boom"}],
    }


def test_cancel_before_source_raises_job_cancelled(tmp_path: Path) -> None:
    h = _Harness(tmp_path, ["a"])
    h.cancel_event.set()

    with pytest.raises(_Cancelled):
        h.run([_source("a")])

    assert h.transcribe_calls == []


def test_whisper_cancelled_error_becomes_job_cancelled(tmp_path: Path) -> None:
    h = _Harness(tmp_path, ["a", "b"])
    h.transcribe_errors["a"] = _WhisperCancelled("stopped")

    with pytest.raises(_Cancelled):
        h.run([_source("a"), _source("b")])

    assert h.transcribe_calls == ["a"]


def test_scoped_job_skips_unindexed_foreign_source(tmp_path: Path) -> None:
    h = _Harness(tmp_path, ["a", "b"])

    result = h.run([_source("a"), _source("b")], source_ids=("a",))

    assert h.transcribe_calls == ["a"]
    assert [item.source_id for item in result.indexes] == ["a"]


def _cloud_options(**overrides: Any) -> dict[str, Any]:
    options: dict[str, Any] = {
        "audio_signature": {"sample_sha256": "x"},
        "max_cost_usd": "10",
        "usd_per_minute_estimate": "0.1",
        "duration_sec": 60.0,
        "chunk_minutes": 10.0,
        "overlap_sec": 0.0,
    }
    options.update(overrides)
    return options


def test_cloud_dispatch_cost_budget_exceeded_propagates_from_phase(tmp_path: Path) -> None:
    h = _Harness(tmp_path, ["a", "b"])
    seen_kwargs: dict[str, Any] = {}

    def transcribe(audio_path: Path, **kwargs: Any) -> dict[str, Any]:
        h.transcribe_calls.append(audio_path.stem)
        seen_kwargs.update(kwargs)
        raise CostBudgetExceeded("job limit hit")

    with pytest.raises(CostBudgetExceeded, match="job limit hit"):
        h.run(
            [_source("a"), _source("b")],
            cloud_source_options={"a": _cloud_options(), "b": _cloud_options()},
            transcribe_audio=transcribe,
        )

    # Not recorded as a per-source failure: the whole phase stops at source "a".
    assert h.transcribe_calls == ["a"]
    assert "cloud_options" in seen_kwargs and "budget_callback" in seen_kwargs
    assert not any("source_failures" in kwargs for _args, kwargs in h.updates)


def test_cloud_source_changed_since_preflight_raises_cost_budget_exceeded(
    tmp_path: Path,
) -> None:
    h = _Harness(tmp_path, ["a"])

    with pytest.raises(CostBudgetExceeded, match="Cloud source changed since cost preflight"):
        h.run(
            [_source("a")],
            cloud_source_options={"a": _cloud_options(audio_signature={"sample_sha256": "old"})},
        )

    assert h.transcribe_calls == []


def test_cloud_backend_without_preflight_options_raises_cost_budget_exceeded(
    tmp_path: Path,
) -> None:
    h = _Harness(tmp_path, ["a"])

    with pytest.raises(CostBudgetExceeded, match="Cloud backend changed since cost preflight"):
        h.run([_source("a")], backend="cloud-audio", cloud_source_options=None)

    assert h.transcribe_calls == []


def test_transcription_error_with_cancel_event_set_becomes_job_cancelled(
    tmp_path: Path,
) -> None:
    h = _Harness(tmp_path, ["a", "b"])
    failure = RuntimeError("decoder died after cancel")

    def transcribe(audio_path: Path, **_kwargs: Any) -> dict[str, Any]:
        h.transcribe_calls.append(audio_path.stem)
        h.cancel_event.set()
        raise failure

    with pytest.raises(_Cancelled, match="Sync job cancelled") as excinfo:
        h.run([_source("a"), _source("b")], transcribe_audio=transcribe)

    assert excinfo.value.__cause__ is failure
    assert h.transcribe_calls == ["a"]


def test_cached_index_is_kept_when_forced_retranscription_fails(tmp_path: Path) -> None:
    h = _Harness(tmp_path, ["a"])
    cached = _index("a")
    h.cached["a"] = cached
    (tmp_path / "a.index.json").write_text("{}")
    h.transcribe_errors["a"] = RuntimeError("refresh failed")

    result = h.run([_source("a")], force_transcribe=True)

    assert h.transcribe_calls == ["a"]
    assert result.indexes == [cached]
    assert (result.reused, result.transcribed) == (1, 0)
    assert result.source_failures == [{"source_id": "a", "error": "refresh failed"}]


def test_cached_index_is_dropped_when_it_no_longer_matches_audio(tmp_path: Path) -> None:
    h = _Harness(tmp_path, ["a"])
    h.cached["a"] = _index("a")
    (tmp_path / "a.index.json").write_text("{}")
    h.transcribe_errors["a"] = RuntimeError("refresh failed")

    result = h.run(
        [_source("a")],
        force_transcribe=True,
        word_index_audio_matches=lambda *a, **kw: False,
    )

    assert result.indexes == []
    assert (result.reused, result.transcribed) == (0, 0)
    assert result.source_failures == [{"source_id": "a", "error": "refresh failed"}]


def test_unrecovered_cloud_coverage_becomes_source_warning(tmp_path: Path) -> None:
    h = _Harness(tmp_path, ["a", "b"])

    def transcribe(audio_path: Path, **_kwargs: Any) -> dict[str, Any]:
        coverage = {"unrecovered_sec": 0.0, "unrecovered_ranges": []}
        if audio_path.stem == "a":
            coverage = {"unrecovered_sec": 180.0, "unrecovered_ranges": [[60.0, 240.0]]}
        return {
            "model": "m",
            "language": "ru",
            "backend": "faster-whisper",
            "words": [],
            "backend_metadata": {"coverage": coverage},
        }

    result = h.run([_source("a"), _source("b")], transcribe_audio=transcribe)

    assert result.source_failures == []
    assert len(result.source_warnings) == 1
    assert result.source_warnings[0]["source_id"] == "a"
    assert "не распознано 3.0 мин" in result.source_warnings[0]["warning"]
    assert any("source_warnings" in kwargs for _args, kwargs in h.updates)


def _cloud_config(**kw: Any) -> dict[str, Any]:
    backend = kw["backend"]
    is_cloud = backend in {"cloud", "cloud-audio", "openrouter"}
    return {
        "backend": backend,
        "model": kw["model"],
        "cloud_timestamp_mapping": "3" if is_cloud else "",
    }


def _cloud_index(source_id: str, *, mapping: str, origin: str = "generated") -> Any:
    return SimpleNamespace(
        source_id=source_id,
        metadata={
            _CACHE_KEY: {
                "audio": {},
                "origin": origin,
                "config": {"backend": "cloud-audio", "cloud_timestamp_mapping": mapping},
            }
        },
    )


def test_outdated_cloud_index_is_retranscribed(tmp_path: Path) -> None:
    # Cloud indexes from the old 20-minute parts silently lost speech; they
    # must not be reused once the cloud timestamp mapping has moved on.
    h = _Harness(tmp_path, ["a"])
    h.cached["a"] = _cloud_index("a", mapping="2")
    (tmp_path / "a.index.json").write_text("{}")

    result = h.run([_source("a")], transcription_config=_cloud_config)

    assert (result.reused, result.transcribed) == (0, 1)
    assert h.transcribe_calls == ["a"]


@pytest.mark.parametrize(
    "index",
    [
        _cloud_index("a", mapping="3"),
        _cloud_index("a", mapping="2", origin="manual"),
        SimpleNamespace(
            source_id="a",
            metadata={
                _CACHE_KEY: {
                    "audio": {},
                    "origin": "generated",
                    "config": {"backend": "faster-whisper", "cloud_timestamp_mapping": ""},
                }
            },
        ),
    ],
    ids=["current-cloud", "manual-cloud", "local-whisper"],
)
def test_current_manual_and_local_indexes_are_reused(tmp_path: Path, index: Any) -> None:
    h = _Harness(tmp_path, ["a"])
    h.cached["a"] = index
    (tmp_path / "a.index.json").write_text("{}")

    result = h.run([_source("a")], transcription_config=_cloud_config)

    assert (result.reused, result.transcribed) == (1, 0)
    assert h.transcribe_calls == []


def _hole_index(unrecovered_sec: float, *, mapping: str = "3", legacy: bool = False) -> Any:
    index = _cloud_index("a", mapping=mapping)
    coverage = {"unrecovered_sec": unrecovered_sec}
    if legacy:
        index.metadata["coverage"] = coverage
    else:
        # Real layout: cloud ASR writes coverage under backend_metadata.
        index.metadata["backend_metadata"] = {"coverage": coverage}
    return index


def _mapping_config(mapping: str) -> Any:
    def config(**kw: Any) -> dict[str, Any]:
        return {**_cloud_config(**kw), "cloud_timestamp_mapping": mapping}

    return config


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("unrecovered_sec, transcribed", [(612.9, 1), (0.0, 0)])
def test_mapping_3_index_is_retranscribed_only_with_unrecovered_holes(
    tmp_path: Path, unrecovered_sec: float, transcribed: int, legacy: bool
) -> None:
    # Mapping "4" re-recognizes long holes in short pieces; mapping "3"
    # indexes are worth redoing only where speech is still missing.
    h = _Harness(tmp_path, ["a"])
    h.cached["a"] = _hole_index(unrecovered_sec, legacy=legacy)
    (tmp_path / "a.index.json").write_text("{}")

    result = h.run([_source("a")], transcription_config=_mapping_config("4"))

    assert result.transcribed == transcribed


@pytest.mark.parametrize("unrecovered_sec, transcribed", [(180.0, 1), (0.0, 0)])
def test_mapping_bump_keeps_fully_covered_mapping_4_index(
    tmp_path: Path, unrecovered_sec: float, transcribed: int
) -> None:
    # A future mapping bump ("4" -> "5") must not trigger paid re-recognition
    # of indexes whose coverage report shows no missing speech.
    h = _Harness(tmp_path, ["a"])
    h.cached["a"] = _hole_index(unrecovered_sec, mapping="4")
    (tmp_path / "a.index.json").write_text("{}")

    result = h.run([_source("a")], transcription_config=_mapping_config("5"))

    assert result.transcribed == transcribed
    assert result.reused == 1 - transcribed


# ---------------------------------------------------------------------------
# Локальный ремонт дыр закэшированного облачного индекса
# ---------------------------------------------------------------------------

from stage2 import local_hole_repair  # noqa: E402
from stage2.sync_models import WordIndex, WordTimestamp  # noqa: E402
from stage2.sync_transcript import load_word_index as real_load  # noqa: E402
from stage2.sync_transcript import save_word_index as real_save  # noqa: E402


def _holey_index(source_id: str, holes: list[list[float]] | None = None) -> WordIndex:
    holes = [[40.0, 60.0]] if holes is None else holes
    words = [
        WordTimestamp(f"c{i}", float(i), float(i) + 0.5)
        for i in range(120)
        if not any(a <= i < b for a, b in holes)
    ]
    return WordIndex(
        source_id,
        words,
        metadata={
            "backend": "cloud-audio",
            "timestamp_precision": "interpolated",
            _CACHE_KEY: {
                "audio": {"sample_sha256": "x"},
                "origin": "generated",
                "config": {"backend": "cloud-audio"},
            },
            "backend_metadata": {
                "coverage": {
                    "holes_detected": len(holes),
                    "holes_repaired": 0,
                    "unrecovered_sec": sum(b - a for a, b in holes),
                    "unrecovered_ranges": holes,
                    "words_per_minute": 100.0,
                },
                "timestamp_precision": "interpolated",
            },
        },
    )


class _RepairHarness:
    """Кэш-хит облачного индекса с дырами: реальные WordIndex/сохранение на
    диск, моки только ffmpeg-нарезки, VAD и транскрайбера (процесс-воркера)."""

    def __init__(self, h: _Harness, monkeypatch: pytest.MonkeyPatch, speech: float = 20.0):
        self.h = h
        self.engine_calls: list[dict[str, Any]] = []
        self.clips: list[tuple[float, float]] = []
        self.words_by_clip_start: dict[float, list[tuple[str, float, float]]] = {}
        self.error: Exception | None = None
        self.saves = 0
        monkeypatch.setenv("SYNC_CLOUD_ASR_LOCAL_REPAIR", "1")

        class Clip:
            path = Path("clip.wav")
            samples = None

            def cleanup(self) -> None:
                pass

        def load_clip(_p, start, end, **_k):
            self.clips.append((start, end))
            return Clip()

        monkeypatch.setattr(local_hole_repair, "_load_clip", load_clip)
        monkeypatch.setattr(local_hole_repair, "_speech_seconds", lambda *_a: speech)

    def transcribe(self, audio_path: Path, **kwargs: Any) -> dict[str, Any]:
        self.engine_calls.append({"audio": audio_path, **kwargs})
        if self.error is not None:
            raise self.error
        start = self.clips[-1][0]
        words = self.words_by_clip_start.get(start, [])
        return {"words": [{"word": w, "start": s - start, "end": e - start} for w, s, e in words]}

    def save(self, path: Path, index: Any) -> None:
        self.saves += 1
        real_save(path, index)

    def snapshot(self, index: WordIndex) -> dict[str, Any]:
        """Индекс так, как его прочитает фаза (после записи на диск)."""
        path = self.h.tmp_path / f"{index.source_id}.index.json"
        real_save(path, index)
        return copy.deepcopy(real_load(path, index.source_id).to_dict())

    def run(self, index: WordIndex, **overrides: Any) -> SyncAsrResult:
        path = self.h.tmp_path / f"{index.source_id}.index.json"
        real_save(path, index)
        self.saves = 0
        values: dict[str, Any] = {
            "transcribe_audio": self.transcribe,
            "save_word_index": self.save,
            "load_word_index": lambda p, sid: real_load(p, sid),
        }
        values.update(overrides)
        self.h.cached[index.source_id] = index
        source = _source(index.source_id)
        source.duration_sec = 120.0
        return self.h.run([source], **values)


def _local(start: float, count: int, prefix: str = "l") -> list[tuple[str, float, float]]:
    return [(f"{prefix}{i}", start + i, start + i + 0.5) for i in range(count)]


@pytest.fixture
def repair(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _RepairHarness:
    return _RepairHarness(_Harness(tmp_path, ["a"]), monkeypatch)


def test_cached_cloud_index_with_holes_is_repaired_and_saved(
    repair: _RepairHarness, tmp_path: Path
) -> None:
    repair.words_by_clip_start[37.0] = _local(40.5, 15)
    usage: list[dict[str, Any]] = []

    result = repair.run(_holey_index("a"), asr_usage_callback=usage.append)

    assert (result.reused, result.transcribed, result.source_failures) == (1, 0, [])
    assert usage == []  # без облачных затрат
    call = repair.engine_calls[0]
    assert call["backend"] == "faster-whisper" and call["model"] == "medium"
    assert call["audio"] == Path("clip.wav") and call["language"] == "ru"
    assert call["cancel_event"] is repair.h.cancel_event
    index = result.indexes[0]
    assert [w.word for w in index.words if 40 <= w.start < 60] == [f"l{i}" for i in range(15)]
    cov = index.metadata["backend_metadata"]["coverage"]
    assert cov["unrecovered_ranges"] == [] and cov["local_repaired_ranges"] == [[40.0, 60.0]]
    assert cov["local_repair"]["source"] == "cached-index"
    assert cov["local_repair"]["repaired_at"] == "2026-01-01T00:00:00+00:00"
    saved = real_load(tmp_path / "a.index.json", "a")
    assert saved.to_dict() == index.to_dict()
    assert repair.saves == 1
    messages = [kw["message"] for _a, kw in repair.h.updates if "message" in kw]
    assert any("Локальный ремонт пропусков" in m and "дыра 1 из 1" in m for m in messages)


def test_second_run_with_same_settings_does_not_repair_again(repair: _RepairHarness) -> None:
    repair.words_by_clip_start[37.0] = _local(40.5, 15)
    first = repair.run(_holey_index("a"))
    calls = len(repair.engine_calls)
    assert calls == 1

    second = repair.run(real_load(repair.h.tmp_path / "a.index.json", "a"))

    assert len(repair.engine_calls) == calls and repair.saves == 0
    assert second.indexes[0].to_dict() == first.indexes[0].to_dict()


def test_model_change_repairs_only_remaining_holes(
    repair: _RepairHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    index = _holey_index("a", [[40.0, 60.0], [80.0, 100.0]])
    repair.words_by_clip_start[37.0] = _local(40.5, 15)
    repair.words_by_clip_start[77.0] = []
    first = repair.run(index)
    assert first.indexes[0].metadata["backend_metadata"]["coverage"]["unrecovered_ranges"] == [
        [80.0, 100.0]
    ]
    repair.clips.clear()

    monkeypatch.setenv("SYNC_CLOUD_ASR_LOCAL_REPAIR_MODEL", "small")
    repair.words_by_clip_start[77.0] = _local(81.0, 14, "s")
    second = repair.run(real_load(repair.h.tmp_path / "a.index.json", "a"))

    assert repair.clips == [(77.0, 103.0)]
    assert repair.engine_calls[-1]["model"] == "small"
    cov = second.indexes[0].metadata["backend_metadata"]["coverage"]
    assert cov["unrecovered_ranges"] == [] and cov["local_repair"]["model"] == "small"


def test_silent_hole_goes_to_silent_ranges_and_is_saved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repair = _RepairHarness(_Harness(tmp_path, ["a"]), monkeypatch, speech=0.1)
    result = repair.run(_holey_index("a"))
    cov = result.indexes[0].metadata["backend_metadata"]["coverage"]
    assert cov["silent_ranges"] == [[40.0, 60.0]] and cov["unrecovered_ranges"] == []
    assert repair.engine_calls == [] and repair.saves == 1


def test_engine_error_keeps_index_and_does_not_fail_the_phase(repair: _RepairHarness) -> None:
    index = _holey_index("a")
    before = repair.snapshot(index)
    repair.error = RuntimeError("model load failed")

    result = repair.run(index)

    assert result.source_failures == [] and result.reused == 1
    assert result.indexes[0].to_dict() == before and repair.saves == 0
    assert any("model load failed" in w["warning"] for w in result.source_warnings)


def test_disabled_repair_reuses_index_as_before(
    repair: _RepairHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SYNC_CLOUD_ASR_LOCAL_REPAIR", "0")
    index = _holey_index("a")
    before = repair.snapshot(index)
    result = repair.run(index)
    assert result.reused == 1 and result.source_warnings == []
    assert result.indexes[0].to_dict() == before
    assert repair.engine_calls == [] and repair.saves == 0


def test_missing_audio_skips_repair_with_reason(repair: _RepairHarness) -> None:
    index = _holey_index("a")
    before = repair.snapshot(index)
    (repair.h.tmp_path / "a.wav").unlink()

    result = repair.run(index)

    assert result.reused == 1 and result.source_failures == []
    assert result.indexes[0].to_dict() == before and repair.engine_calls == []
    assert any("аудио" in w["warning"] for w in result.source_warnings)


def test_cancel_during_local_repair_raises_job_cancelled(repair: _RepairHarness) -> None:
    def transcribe(audio_path: Path, **kwargs: Any) -> dict[str, Any]:
        repair.h.cancel_event.set()
        raise _WhisperCancelled("cancelled")

    index = _holey_index("a")
    with pytest.raises(_Cancelled):
        repair.run(index, transcribe_audio=transcribe)
    assert repair.saves == 0


def test_manual_or_non_cloud_index_is_not_repaired(repair: _RepairHarness) -> None:
    manual = _holey_index("a")
    manual.metadata[_CACHE_KEY]["origin"] = "manual"
    result = repair.run(manual)
    assert result.reused == 1 and repair.engine_calls == [] and repair.saves == 0


def real_load_roundtrip(repair: _RepairHarness, index: WordIndex) -> WordIndex:
    repair.snapshot(index)
    return real_load(repair.h.tmp_path / f"{index.source_id}.index.json", index.source_id)


def test_repair_changes_matching_fingerprint(repair: _RepairHarness) -> None:
    from api import _sync_matching_fingerprint

    def fingerprint(index: WordIndex) -> str:
        return _sync_matching_fingerprint(
            units=[{"segment_id": "s1", "text": "q"}],
            sources=[],
            indexes=[index],
            normalization_revision=1,
            timecode_mode="relative",
            search_window_sec=180.0,
            llm_enabled=False,
            llm_model="",
            llm_threshold=0.0,
            llm_limit=0,
        )

    repair.words_by_clip_start[37.0] = _local(40.5, 15)
    index = _holey_index("a")
    before = fingerprint(real_load_roundtrip(repair, index))

    result = repair.run(index)

    # индекс в памяти фазы — обновлённый, именно он уходит в сопоставление
    assert fingerprint(result.indexes[0]) != before
    saved = real_load(repair.h.tmp_path / "a.index.json", "a")
    assert fingerprint(saved) == fingerprint(result.indexes[0])


def test_unchanged_repair_saves_complete_marker_and_is_not_repeated(
    repair: _RepairHarness,
) -> None:
    index = _holey_index("a")  # движок вернёт [] -> новых слов нет
    first = repair.run(index)
    cov = first.indexes[0].metadata["backend_metadata"]["coverage"]
    assert cov["local_repair"]["complete"] is True and repair.saves == 1
    assert len(repair.engine_calls) == 1
    second = repair.run(first.indexes[0])
    assert len(repair.engine_calls) == 1 and repair.saves == 0
    assert second.reused == 1


def test_whisper_no_words_error_is_empty_result_not_engine_failure(
    repair: _RepairHarness,
) -> None:
    from stage2.whisper_runner import WhisperTranscriptionError

    repair.error = WhisperTranscriptionError(
        "faster-whisper returned no usable word timestamps for clip.wav"
    )
    result = repair.run(_holey_index("a"))
    cov = result.indexes[0].metadata["backend_metadata"]["coverage"]
    assert cov["local_repair"]["complete"] is True and repair.saves == 1
    assert not any("не выполнен" in w["warning"] for w in result.source_warnings)


def test_worker_relayed_no_words_error_is_empty_result(repair: _RepairHarness) -> None:
    from stage2.whisper_process import WhisperProcessError

    repair.error = WhisperProcessError(
        "WhisperTranscriptionError: faster-whisper returned no usable word timestamps for c.wav"
    )
    result = repair.run(_holey_index("a"))
    cov = result.indexes[0].metadata["backend_metadata"]["coverage"]
    assert cov["local_repair"]["complete"] is True and repair.saves == 1


def test_other_whisper_transcription_error_is_still_engine_failure(
    repair: _RepairHarness,
) -> None:
    from stage2.whisper_runner import WhisperTranscriptionError

    repair.error = WhisperTranscriptionError("Облачное распознавание речи не удалось: x")
    result = repair.run(_holey_index("a"))
    assert repair.saves == 0
    assert "local_repair" not in result.indexes[0].metadata["backend_metadata"]["coverage"]


def test_legacy_index_with_holes_gets_one_warning(repair: _RepairHarness) -> None:
    index = _holey_index("a")
    index.metadata[_CACHE_KEY]["origin"] = "legacy"
    result = repair.run(index)
    legacy = [w for w in result.source_warnings if "legacy" in w["warning"]]
    assert len(legacy) == 1 and "перезапустите распознавание" in legacy[0]["warning"]
    assert repair.engine_calls == [] and repair.saves == 0


def test_manual_index_with_holes_gets_no_legacy_warning(repair: _RepairHarness) -> None:
    index = _holey_index("a")
    index.metadata[_CACHE_KEY]["origin"] = "manual"
    result = repair.run(index)
    assert not any("legacy" in w["warning"] for w in result.source_warnings)


def test_remaining_holes_after_repair_give_coverage_warning(repair: _RepairHarness) -> None:
    repair.words_by_clip_start[37.0] = _local(40.5, 15)
    index = _holey_index("a", [[40.0, 60.0], [80.0, 100.0]])  # вторая дыра не поддастся
    result = repair.run(index)
    warnings = [w["warning"] for w in result.source_warnings]
    assert any("не распознано" in w and "80–100" in w for w in warnings)


def test_fully_repaired_index_gives_no_coverage_warning(repair: _RepairHarness) -> None:
    repair.words_by_clip_start[37.0] = _local(40.5, 15)
    result = repair.run(_holey_index("a"))
    assert result.source_warnings == []


def test_unavailable_engine_gives_warning_with_reason(
    repair: _RepairHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        local_hole_repair, "unavailable_reason", lambda _s: "faster-whisper не установлен"
    )
    result = repair.run(_holey_index("a"))
    assert any("faster-whisper не установлен" in w["warning"] for w in result.source_warnings)
    assert repair.engine_calls == [] and repair.saves == 0


def test_mapping_bump_after_local_repair_does_not_retranscribe(repair: _RepairHarness) -> None:
    # Local repair zeroes coverage.unrecovered_sec; a later mapping bump must
    # then reuse the repaired index instead of paying for a new cloud run.
    index = _holey_index("a")
    index.metadata[_CACHE_KEY]["config"] = {
        "backend": "cloud-audio",
        "cloud_timestamp_mapping": "4",
    }
    repair.words_by_clip_start[37.0] = _local(40.0, 20)
    # The outdated check applies only when the guide audio is present.
    (repair.h.tmp_path / "a.wav").write_bytes(b"audio")
    first = repair.run(index, transcription_config=_mapping_config("4"))
    cov = first.indexes[0].metadata["backend_metadata"]["coverage"]
    assert cov["unrecovered_sec"] == 0 and first.transcribed == 0

    second = repair.run(first.indexes[0], transcription_config=_mapping_config("5"))

    assert second.source_failures == []
    assert (second.transcribed, second.reused) == (0, 1)


# -- перерасчёт времени слов облачного индекса (локальный Whisper всего файла) --

from stage2 import index_retiming  # noqa: E402
from stage2.cached_index_repair import index_coverage  # noqa: E402


SHIFT = 5.0


def _word(i: int) -> str:
    return "".join("абвгдежзик"[int(d)] for d in str(i)) + "ка"


def _retime_index(
    source_id: str = "a",
    *,
    holes: list[list[float]] | None = None,
    origin: str | None = "generated",
    backend: str = "cloud-audio",
) -> WordIndex:
    index = _holey_index(source_id, holes if holes is not None else [])
    for i, word in enumerate(index.words):
        index.words[i] = WordTimestamp(_word(int(word.word[1:])), word.start, word.end)
    index.metadata["backend"] = backend
    if origin is None:
        index.metadata.pop(_CACHE_KEY)
    else:
        index.metadata[_CACHE_KEY]["origin"] = origin
    return index


def _true_local_words() -> list[dict[str, Any]]:
    """Whisper видит все 120 слов; истинное время = облачное + SHIFT."""
    return [{"word": _word(i), "start": i + SHIFT, "end": i + SHIFT + 0.5} for i in range(120)]


class _RetimeHarness(_RepairHarness):
    def __init__(self, h: _Harness, monkeypatch: pytest.MonkeyPatch) -> None:
        super().__init__(h, monkeypatch)
        monkeypatch.setenv("SYNC_CLOUD_ASR_RETIME", "1")
        monkeypatch.delenv("SYNC_CLOUD_ASR_RETIME_MODEL", raising=False)
        monkeypatch.delenv("SYNC_CLOUD_ASR_RETIME_MAX_SEC", raising=False)
        monkeypatch.setattr(index_retiming, "_faster_whisper_available", lambda: True)
        self.local_words = _true_local_words()
        self.whole_calls: list[dict[str, Any]] = []

    def transcribe(self, audio_path: Path, **kwargs: Any) -> dict[str, Any]:
        if audio_path.name.endswith(".wav") and audio_path.stem == "a":  # весь файл
            self.whole_calls.append(kwargs)
            if self.error is not None:
                raise self.error
            callback = kwargs.get("progress_callback")
            if callback is not None:
                callback(60.0, 120.0, 10)
            return {"words": [dict(w) for w in self.local_words]}
        return super().transcribe(audio_path, **kwargs)

    def disk(self) -> WordIndex:
        return real_load(self.h.tmp_path / "a.index.json", "a")


@pytest.fixture
def retime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _RetimeHarness:
    return _RetimeHarness(_Harness(tmp_path, ["a"]), monkeypatch)


def _marker(index: WordIndex) -> dict[str, Any]:
    return index.metadata["backend_metadata"]["retiming"]


def test_retime_cached_cloud_index_saves_new_times_and_marker(
    retime: _RetimeHarness, tmp_path: Path
) -> None:
    usage: list[dict[str, Any]] = []

    result = retime.run(_retime_index(), asr_usage_callback=usage.append)

    assert (result.reused, result.transcribed, result.source_failures) == (1, 0, [])
    assert usage == [] and result.source_warnings == []
    call = retime.whole_calls[0]
    assert call["backend"] == "faster-whisper" and call["model"] == "small"
    assert call["language"] == "ru" and call["cancel_event"] is retime.h.cancel_event
    index = result.indexes[0]
    assert index.words[50].start == pytest.approx(50 + SHIFT, abs=0.01)
    marker = _marker(index)
    assert marker["model"] == "small" and marker["version"] == index_retiming.RETIME_VERSION
    assert marker["fingerprint"] and marker["retimed_at"] == "2026-01-01T00:00:00+00:00"
    assert marker["exact_count"] >= 100 and marker["anchor_ratio"] > 0.9
    assert marker["shift_median_sec"] == pytest.approx(SHIFT, abs=0.1)
    assert "anchor_gaps" in marker and "shift_p90_sec" in marker and "shift_max_sec" in marker
    assert index.metadata["timestamp_precision"] == "retimed"
    assert index.metadata["backend_metadata"]["timestamp_precision"] == "retimed"
    assert retime.disk().to_dict() == index.to_dict()
    cache = real_json(tmp_path / "a.local_words.json")
    assert cache["model"] == "small" and cache["audio"] == {"sample_sha256": "x"}
    assert len(cache["words"]) == 120
    messages = [kw["message"] for _a, kw in retime.h.updates if "message" in kw]
    assert any("Уточнение таймкодов" in m and "50%" in m for m in messages)


def real_json(path: Path) -> dict[str, Any]:
    import json

    return json.loads(path.read_text(encoding="utf-8"))


def test_retime_is_not_repeated_with_same_fingerprint(retime: _RetimeHarness) -> None:
    retime.run(_retime_index())
    assert len(retime.whole_calls) == 1

    second = retime.run(retime.disk())

    assert len(retime.whole_calls) == 1 and retime.saves == 0
    assert second.source_failures == [] and second.source_warnings == []


def test_retime_writes_old_to_new_map_next_to_index_not_into_it(
    retime: _RetimeHarness, tmp_path: Path
) -> None:
    from stage2.match_retiming import load_retime_map

    original = _retime_index()
    old_starts = [round(w.start, 4) for w in original.words]

    result = retime.run(original)

    index = result.indexes[0]
    rmap = load_retime_map(tmp_path / "a.retime_map.json")
    assert rmap is not None and rmap.matches_index(index)
    assert rmap.old_starts == old_starts
    assert rmap.new_starts[50] == pytest.approx(50 + SHIFT, abs=0.01)
    assert rmap.new_starts == [round(w.start, 4) for w in index.words[: len(old_starts)]]
    assert "retime_map" not in str(index.to_dict().keys()) and rmap.fingerprint in str(
        _marker(index)
    )


def test_retime_map_is_not_rewritten_when_retime_is_not_repeated(
    retime: _RetimeHarness, tmp_path: Path
) -> None:
    retime.run(_retime_index())
    path = tmp_path / "a.retime_map.json"
    before = path.read_bytes()
    path.write_bytes(before + b" ")

    retime.run(retime.disk())

    assert path.read_bytes() == before + b" "


def test_rejected_retime_writes_no_map(retime: _RetimeHarness, tmp_path: Path) -> None:
    retime.local_words = [{"word": "чужой", "start": 1.0, "end": 1.5}] * 5

    retime.run(_retime_index())

    assert not (tmp_path / "a.retime_map.json").exists()


def test_new_algorithm_version_retimes_from_cached_local_words(
    retime: _RetimeHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    retime.run(_retime_index())
    old = _marker(retime.disk())
    monkeypatch.setattr(index_retiming, "RETIME_VERSION", index_retiming.RETIME_VERSION + 1)

    result = retime.run(retime.disk())

    assert len(retime.whole_calls) == 1  # Whisper не гонялся заново
    new = _marker(result.indexes[0])
    assert (
        new["version"] == index_retiming.RETIME_VERSION and new["fingerprint"] != old["fingerprint"]
    )
    assert retime.saves == 1


def test_changed_audio_ignores_cached_local_words(retime: _RetimeHarness) -> None:
    retime.run(_retime_index())
    cache_path = retime.h.tmp_path / "a.local_words.json"
    data = real_json(cache_path)
    data["audio"] = {"sample_sha256": "other"}
    import json

    cache_path.write_text(json.dumps(data), encoding="utf-8")
    index = retime.disk()
    del index.metadata["backend_metadata"]["retiming"]

    retime.run(index)

    assert len(retime.whole_calls) == 2


def test_disabled_retime_leaves_index_as_before(
    retime: _RetimeHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SYNC_CLOUD_ASR_RETIME", "0")
    index = _retime_index()
    before = retime.snapshot(index)

    result = retime.run(index)

    assert retime.whole_calls == [] and retime.saves == 0
    assert result.indexes[0].to_dict() == before
    assert not (retime.h.tmp_path / "a.local_words.json").exists()


def test_faster_whisper_missing_skips_retime_silently(
    retime: _RetimeHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(index_retiming, "_faster_whisper_available", lambda: False)

    result = retime.run(_retime_index())

    assert (
        retime.whole_calls == []
        and "retiming" not in result.indexes[0].metadata["backend_metadata"]
    )
    assert result.source_failures == []


def test_retime_engine_error_keeps_index_and_warns(retime: _RetimeHarness) -> None:
    retime.error = RuntimeError("boom")
    index = _retime_index()
    before = retime.snapshot(index)

    result = retime.run(index)

    assert result.source_failures == [] and retime.saves == 0
    assert result.indexes[0].to_dict() == before
    assert any(
        "Уточнение таймкодов" in w["warning"] and "boom" in w["warning"]
        for w in result.source_warnings
    )
    assert not (retime.h.tmp_path / "a.local_words.json").exists()


def test_cancel_during_retime_keeps_index_and_raises(retime: _RetimeHarness) -> None:
    def transcribe(audio_path: Path, **kwargs: Any) -> dict[str, Any]:
        retime.h.cancel_event.set()  # как при реальной отмене задачи
        raise _WhisperCancelled("cancelled")

    index = _retime_index()
    before = copy.deepcopy(index.to_dict())

    with pytest.raises(_Cancelled):
        retime.run(index, transcribe_audio=transcribe)

    assert retime.saves == 0 and index.to_dict() == before
    assert not (retime.h.tmp_path / "a.local_words.json").exists()


def test_retime_time_limit_stops_without_changing_index(
    retime: _RetimeHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SYNC_CLOUD_ASR_RETIME_MAX_SEC", "10")
    retime.error = TimeoutError("limit")
    clock = iter([0.0, 50.0, 50.0, 50.0, 50.0, 50.0])
    index = _retime_index()
    before = retime.snapshot(index)

    result = retime.run(index, monotonic=lambda: next(clock))

    assert result.source_failures == [] and retime.saves == 0
    assert result.indexes[0].to_dict() == before
    assert retime.whole_calls[0]["timeout_seconds"] == 10.0
    assert any("лимит" in w["warning"] for w in result.source_warnings)


def test_holes_are_filled_from_local_words_without_second_engine_call(
    retime: _RetimeHarness,
) -> None:
    index = _retime_index(holes=[[40.0, 60.0]])

    result = retime.run(index)

    assert len(retime.whole_calls) == 1 and len(retime.engine_calls) == 0
    out = result.indexes[0]
    cov = index_coverage(out)
    assert cov["unrecovered_ranges"] == [] and cov["unrecovered_sec"] == 0
    assert len(out.words) == 120
    assert cov["local_repair"]["source"] == "retime-local-words"
    assert cov["local_repaired_ranges"] and cov["holes_repaired_local"] == 1
    assert [w.start for w in out.words] == sorted(w.start for w in out.words)
    assert result.source_warnings == []
    assert retime.disk().to_dict() == out.to_dict()


def test_holes_are_filled_from_cached_local_words_when_already_retimed(
    retime: _RetimeHarness,
) -> None:
    # перерасчёт прошёл раньше (с дырой, которой тогда не было в отчёте)
    first = retime.run(_retime_index())
    index = retime.disk()
    drop = {_word(i) for i in range(40, 60)}
    index.words = [w for w in index.words if w.word not in drop]
    cov = index_coverage(index)
    cov["unrecovered_ranges"] = [[45.0, 65.0]]
    cov["unrecovered_sec"] = 20.0
    assert first.source_failures == []

    result = retime.run(index)

    assert len(retime.whole_calls) == 1 and len(result.indexes[0].words) == 120
    assert index_coverage(result.indexes[0])["unrecovered_ranges"] == []


def test_retime_changes_matching_fingerprint(retime: _RetimeHarness) -> None:
    from api import _sync_matching_fingerprint

    def fingerprint(index: WordIndex) -> str:
        return _sync_matching_fingerprint(
            units=[{"segment_id": "s1", "text": "q"}],
            sources=[],
            indexes=[index],
            normalization_revision=1,
            timecode_mode="relative",
            search_window_sec=180.0,
            llm_enabled=False,
            llm_model="",
            llm_threshold=0.0,
            llm_limit=0,
        )

    index = _retime_index()
    before = fingerprint(real_load_roundtrip(retime, index))

    result = retime.run(index)

    assert fingerprint(result.indexes[0]) != before
    assert fingerprint(retime.disk()) == fingerprint(result.indexes[0])


def test_locally_repaired_words_keep_text_order_after_retime(retime: _RetimeHarness) -> None:
    # Words of a previous local repair live in the cloud time scale: they are removed,
    # the cloud words are re-timed and the range is refilled from the local words.
    index = _retime_index()
    cov = index_coverage(index)
    cov["local_repaired_ranges"] = [[40.0, 50.0]]
    cov["holes_repaired_local"] = 1

    result = retime.run(index)

    out = result.indexes[0]
    starts = [w.start for w in out.words]
    texts = [w.word for w in out.words]
    assert starts == sorted(starts)
    assert all(a != b for a, b in zip(texts, texts[1:]))  # no duplicates in a row
    assert texts == [_word(i) for i in range(120)]  # inserts sit between their neighbours
    assert all(w.start == pytest.approx(i + SHIFT, abs=0.01) for i, w in enumerate(out.words))
    new_cov = index_coverage(out)
    ((lo, hi),) = new_cov["local_repaired_ranges"]
    assert lo == pytest.approx(44.5, abs=0.01) and hi == pytest.approx(55.0, abs=0.01)
    assert new_cov["holes_repaired_local"] == 1 and new_cov["unrecovered_ranges"] == []


# -- quality gate and attempt markers --


def _other_local_words(keep: tuple[int, ...] = (10, 40, 70, 100)) -> list[dict[str, Any]]:
    """Other speech with the same rhythm: only a few words coincide."""
    words = []
    for i in range(120):
        text = _word(i) if i in keep else "".join("мнопрстуфх"[int(d)] for d in str(i)) + "ль"
        words.append({"word": text, "start": i + SHIFT, "end": i + SHIFT + 0.5})
    return words


def _attempt(h: _Harness) -> dict[str, Any]:
    return real_json(h.tmp_path / "a.retime_attempt.json")


def _assert_rejected(retime: _RetimeHarness, result: SyncAsrResult, before: dict) -> None:
    assert result.source_failures == [] and retime.saves == 0
    assert result.indexes[0].to_dict() == before  # words and metadata (matching fingerprint)
    attempt = _attempt(retime.h)
    assert attempt["status"] == "rejected" and attempt["reason"]
    assert any("Уточнение таймкодов" in w["warning"] for w in result.source_warnings)


def test_junk_local_text_is_rejected_and_index_untouched(retime: _RetimeHarness) -> None:
    retime.local_words = _other_local_words()
    index = _retime_index()
    before = retime.snapshot(index)

    result = retime.run(index)

    _assert_rejected(retime, result, before)
    assert "retiming" not in result.indexes[0].metadata["backend_metadata"]
    assert result.indexes[0].metadata["backend_metadata"]["timestamp_precision"] == "interpolated"


def test_other_language_is_rejected(retime: _RetimeHarness) -> None:
    retime.local_words = [
        {"word": f"word{'abcdefgh'[i % 8]}{i}", "start": i + SHIFT, "end": i + SHIFT + 0.5}
        for i in range(120)
    ]
    index = _retime_index()
    before = retime.snapshot(index)

    _assert_rejected(retime, retime.run(index), before)


def test_few_anchors_are_rejected(retime: _RetimeHarness) -> None:
    retime.local_words = _true_local_words()[::6]  # 20 of 120 words
    index = _retime_index()
    before = retime.snapshot(index)

    _assert_rejected(retime, retime.run(index), before)


def test_rejected_attempt_is_not_repeated_with_same_fingerprint(retime: _RetimeHarness) -> None:
    retime.local_words = _other_local_words()
    retime.run(_retime_index())
    first = _attempt(retime.h)
    (retime.h.tmp_path / "a.local_words.json").unlink()  # even without the cached words
    semaphore = _CountingSemaphore()

    second = retime.run(
        retime.disk(),
        utc_now_iso=lambda: "2026-02-01T00:00:00+00:00",
        asr_semaphore=semaphore,
    )

    assert len(retime.whole_calls) == 1 and semaphore.acquired == 0
    assert _attempt(retime.h) == first and second.source_warnings == []


def test_rejected_attempt_is_retried_for_new_algorithm_version_from_cached_words(
    retime: _RetimeHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    retime.local_words = _other_local_words()
    retime.run(_retime_index())
    old = _attempt(retime.h)["fingerprint"]
    monkeypatch.setattr(index_retiming, "RETIME_VERSION", index_retiming.RETIME_VERSION + 1)
    semaphore = _CountingSemaphore()

    retime.run(retime.disk(), asr_semaphore=semaphore)

    assert len(retime.whole_calls) == 1 and semaphore.acquired == 0  # cached local words
    assert _attempt(retime.h)["fingerprint"] != old


def test_rejected_attempt_does_not_block_a_new_cloud_transcript(retime: _RetimeHarness) -> None:
    retime.local_words = _other_local_words()
    retime.run(_retime_index())
    retime.local_words = _true_local_words()
    (retime.h.tmp_path / "a.local_words.json").unlink()
    new = _retime_index()
    new.metadata[_CACHE_KEY]["config"] = {"backend": "cloud-audio", "other": 1}

    result = retime.run(new)

    assert result.indexes[0].metadata["backend_metadata"]["retiming"]["applied"] is True


def test_no_words_is_recorded_and_not_repeated(retime: _RetimeHarness) -> None:
    retime.local_words = []
    index = _retime_index()
    before = retime.snapshot(index)

    first = retime.run(index)
    second = retime.run(retime.disk())

    assert len(retime.whole_calls) == 1 and retime.saves == 0
    assert first.indexes[0].to_dict() == before == second.indexes[0].to_dict()
    assert _attempt(retime.h)["status"] == "no_words"
    assert second.source_warnings == []


def test_stopped_attempt_repeats_only_when_max_sec_grows(
    retime: _RetimeHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SYNC_CLOUD_ASR_RETIME_MAX_SEC", "10")
    retime.error = TimeoutError("limit")
    clock = iter([0.0, 50.0] * 5)
    retime.run(_retime_index(), monotonic=lambda: next(clock))
    attempt = _attempt(retime.h)
    assert attempt["status"] == "stopped" and attempt["max_sec"] == 10.0
    assert len(retime.whole_calls) == 1

    retime.run(retime.disk(), monotonic=lambda: next(clock))  # same limit
    assert len(retime.whole_calls) == 1

    monkeypatch.setenv("SYNC_CLOUD_ASR_RETIME_MAX_SEC", "5")  # smaller limit
    retime.run(retime.disk(), monotonic=lambda: next(clock))
    assert len(retime.whole_calls) == 1

    monkeypatch.setenv("SYNC_CLOUD_ASR_RETIME_MAX_SEC", "0")  # no limit
    retime.run(retime.disk())
    assert len(retime.whole_calls) == 2  # no limit now: tried again


def test_stopped_attempt_repeats_for_a_larger_limit(
    retime: _RetimeHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SYNC_CLOUD_ASR_RETIME_MAX_SEC", "10")
    retime.error = TimeoutError("limit")
    clock = iter([0.0, 50.0] * 3)
    retime.run(_retime_index(), monotonic=lambda: next(clock))

    monkeypatch.setenv("SYNC_CLOUD_ASR_RETIME_MAX_SEC", "600")
    retime.error = None
    result = retime.run(retime.disk())

    assert len(retime.whole_calls) == 2
    assert result.indexes[0].metadata["timestamp_precision"] == "retimed"


def test_failed_attempt_is_retried_after_retry_hours(
    retime: _RetimeHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    retime.error = RuntimeError("boom")
    index = _retime_index()
    before = retime.snapshot(index)
    retime.run(index)
    attempt = _attempt(retime.h)
    assert attempt["status"] == "failed" and "boom" in attempt["error"]
    assert attempt["attempted_at"] == "2026-01-01T00:00:00+00:00"

    soon = retime.run(retime.disk(), utc_now_iso=lambda: "2026-01-01T23:00:00+00:00")
    assert len(retime.whole_calls) == 1 and soon.source_warnings == []
    assert soon.indexes[0].to_dict() == before

    retime.run(retime.disk(), utc_now_iso=lambda: "2026-01-02T01:00:00+00:00")  # 25 h
    assert len(retime.whole_calls) == 2

    monkeypatch.setenv("SYNC_CLOUD_ASR_RETIME_RETRY_HOURS", "1")
    retime.run(retime.disk(), utc_now_iso=lambda: "2026-01-02T03:00:00+00:00")
    assert len(retime.whole_calls) == 3


def test_rejected_retime_falls_back_to_cached_index_hole_repair(retime: _RetimeHarness) -> None:
    retime.local_words = _other_local_words()
    retime.words_by_clip_start[37.0] = _local(40.5, 15)
    index = _retime_index(holes=[[40.0, 60.0]])

    result = retime.run(index)

    assert _attempt(retime.h)["status"] == "rejected"
    assert len(retime.engine_calls) == 1  # the old clip repair ran
    cov = index_coverage(result.indexes[0])
    assert cov["unrecovered_ranges"] == [] and cov["local_repair"]["source"] == "cached-index"


class _CountingSemaphore:
    def __init__(self) -> None:
        self.acquired = 0
        self._inner = threading.Semaphore(1)

    def acquire(self, *args: Any, **kwargs: Any) -> bool:
        ok = self._inner.acquire(*args, **kwargs)
        self.acquired += int(bool(ok))
        return ok

    def release(self) -> None:
        self._inner.release()


def test_slot_is_taken_only_around_the_whole_file_transcription(retime: _RetimeHarness) -> None:
    semaphore = _CountingSemaphore()
    retime.run(_retime_index(), asr_semaphore=semaphore)
    assert semaphore.acquired == 1

    again = _CountingSemaphore()  # already retimed: no slot at all
    retime.run(retime.disk(), asr_semaphore=again)
    assert again.acquired == 0 and len(retime.whole_calls) == 1


def test_slot_is_not_taken_when_local_words_are_cached(
    retime: _RetimeHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    retime.run(_retime_index())
    monkeypatch.setattr(index_retiming, "RETIME_VERSION", index_retiming.RETIME_VERSION + 1)
    semaphore = _CountingSemaphore()

    retime.run(retime.disk(), asr_semaphore=semaphore)

    assert semaphore.acquired == 0 and len(retime.whole_calls) == 1


def test_slot_is_not_taken_when_engine_or_audio_is_missing(
    retime: _RetimeHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    semaphore = _CountingSemaphore()
    monkeypatch.setattr(index_retiming, "_faster_whisper_available", lambda: False)
    retime.run(_retime_index(), asr_semaphore=semaphore)
    monkeypatch.setattr(index_retiming, "_faster_whisper_available", lambda: True)
    (retime.h.tmp_path / "a.wav").unlink()
    result = retime.run(_retime_index(), asr_semaphore=semaphore)
    assert semaphore.acquired == 0 and retime.whole_calls == []
    assert any("не найдено" in w["warning"] for w in result.source_warnings)


def test_fresh_cloud_transcription_is_retimed(retime: _RetimeHarness) -> None:
    calls: list[str] = []

    def transcribe(audio_path: Path, **kwargs: Any) -> dict[str, Any]:
        calls.append("fresh" if not calls else "whole")
        if len(calls) == 1:
            return {
                "model": "m",
                "language": "ru",
                "backend": "cloud-audio",
                "words": [],
                "backend_metadata": {"timestamp_precision": "interpolated"},
            }
        return {"words": [dict(w) for w in retime.local_words]}

    source = _source("a")
    source.duration_sec = 120.0
    result = retime.h.run(
        [source],
        transcribe_audio=transcribe,
        ingest_word_index=lambda res, sid, **kw: _retime_index(sid),
        transcription_cache_metadata=lambda *a, **kw: {
            "audio": {"sample_sha256": "x"},
            "origin": "generated",
        },
        save_word_index=real_save,
        load_word_index=real_load,
    )

    assert calls == ["fresh", "whole"] and result.transcribed == 1
    assert retime.h.metrics["asr_timestamp_precision"] == "retimed"
    out = result.indexes[0]
    assert out.words[50].start == pytest.approx(50 + SHIFT, abs=0.01)
    assert real_load(retime.h.tmp_path / "a.index.json", "a").to_dict() == out.to_dict()


def test_fresh_cloud_transcription_rejected_retime_falls_back_to_clip_repair(
    retime: _RetimeHarness,
) -> None:
    calls: list[str] = []

    def transcribe(audio_path: Path, **kwargs: Any) -> dict[str, Any]:
        calls.append("fresh" if not calls else "other")
        if len(calls) == 1:
            return {
                "model": "m",
                "language": "ru",
                "backend": "cloud-audio",
                "words": [],
                "backend_metadata": {"timestamp_precision": "interpolated"},
            }
        if len(calls) == 2:  # the whole file: another text
            return {"words": _other_local_words()}
        return {
            "words": [
                {"word": w, "start": s - 37.0, "end": e - 37.0} for w, s, e in _local(40.5, 15)
            ]
        }

    retime.clips.append((37.0, 63.0))
    retime.words_by_clip_start.clear()
    source = _source("a")
    source.duration_sec = 120.0
    result = retime.h.run(
        [source],
        transcribe_audio=transcribe,
        ingest_word_index=lambda res, sid, **kw: _retime_index(sid, holes=[[40.0, 60.0]]),
        transcription_cache_metadata=lambda *a, **kw: {
            "audio": {"sample_sha256": "x"},
            "origin": "generated",
        },
        save_word_index=real_save,
        load_word_index=real_load,
    )

    assert calls == ["fresh", "other", "other"]
    assert retime.h.metrics["asr_timestamp_precision"] == "interpolated"
    cov = index_coverage(result.indexes[0])
    assert cov["unrecovered_ranges"] == [] and cov["local_repair"]["source"] == "cached-index"
    assert any("Уточнение таймкодов" in w["warning"] for w in result.source_warnings)


@pytest.mark.parametrize("min_sec", [0.5, 0.0])
def test_remap_ranges_edges(min_sec: float) -> None:
    def words(spans: list[tuple[float, float]]) -> list[WordTimestamp]:
        return [WordTimestamp(f"w{i}", a, b) for i, (a, b) in enumerate(spans)]

    old = words([(4, 5), (10, 11), (20, 21)])
    new = words([(6, 7), (13, 14), (25, 26)])
    remap = index_retiming._remap_ranges
    # before the first word (prev < 0): follows the drift of the first word
    assert remap([(0.0, 3.0)], old, new, min_sec=min_sec) == ([(2.0, 6.0)], 0)
    # after the last word (nxt >= len): follows the drift of the last word
    assert remap([(30.0, 40.0)], old, new, min_sec=min_sec) == ([(26.0, 45.0)], 0)
    # between words: from the end of the left neighbour to the start of the right one
    assert remap([(12.0, 18.0)], old, new, min_sec=min_sec) == ([(14.0, 25.0)], 0)
    assert remap([], old, new, min_sec=min_sec) == ([], 0)
    assert remap([(1.0, 2.0)], [], [], min_sec=min_sec) == ([(1.0, 2.0)], 0)


def test_remap_ranges_collapsed_and_short_holes_are_counted() -> None:
    remap = index_retiming._remap_ranges
    old = [WordTimestamp(f"w{i}", s, s + 1) for i, s in enumerate((4, 10, 20))]
    collapsed = [
        WordTimestamp("w0", 6, 7),
        WordTimestamp("w1", 13, 26),
        WordTimestamp("w2", 25, 26),
    ]
    assert remap([(12.0, 18.0)], old, collapsed) == ([], 1)  # new end before new start
    assert remap([(12.0, 18.0)], old, collapsed, min_sec=0.0) == ([], 1)
    short = [WordTimestamp("w0", 6, 7), WordTimestamp("w1", 13, 14), WordTimestamp("w2", 14.3, 15)]
    assert remap([(12.0, 18.0)], old, short) == ([], 1)  # 0.3 s: closed by the retime
    assert remap([(12.0, 18.0)], old, short, min_sec=0.0) == ([(14.0, 14.3)], 0)
    # merging: two ranges that overlap after the move are one range, nothing counted
    assert remap([(12.0, 15.0), (15.0, 18.0)], old, short, min_sec=0.0)[1] == 0


@pytest.mark.parametrize("kind", ["manual", "legacy", "local"])
def test_non_cloud_manual_and_legacy_indexes_are_not_retimed(
    retime: _RetimeHarness, kind: str
) -> None:
    index = {
        "manual": lambda: _retime_index(origin="manual"),
        "legacy": lambda: _retime_index(origin=None),
        "local": lambda: _retime_index(backend="faster-whisper"),
    }[kind]()

    result = retime.run(index)

    assert retime.whole_calls == [] and result.source_failures == []
    assert "retiming" not in result.indexes[0].metadata["backend_metadata"]
    assert not (retime.h.tmp_path / "a.local_words.json").exists()
