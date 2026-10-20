from __future__ import annotations

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
