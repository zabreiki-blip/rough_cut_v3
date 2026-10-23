"""Характеризационные тесты ``api_sync_worker.run_sync_job``.

Фиксируют текущее поведение веток, не покрытых основными тестами
(``test_stage2_sync_api.py``): сегмент без uid, сбои матчинга и LLM-review,
периодический checkpoint, отмена посреди цикла и ошибки checkpoint.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

import api
from stage2.sync_models import SyncMatch
import test_stage2_sync_api as base
from test_stage2_sync_api import (
    _ImmediateThread,
    _post_source,
    _seed_words,
    _start_low_confidence_llm_job,
    _write_confirmed_normalization,
)

# Переиспользуем фикстуру основного файла без дублирования (присваивание, а не
# импорт имени, чтобы параметры тестов не считались переопределением).
sync_api = base.sync_api


@pytest.fixture(autouse=True)
def _no_llm_review(monkeypatch: pytest.MonkeyPatch) -> None:
    """LLM-review выключен, если тест сам его не включает."""
    monkeypatch.setattr(api.get_settings(), "openrouter_api_key", "")


def _rewrite_blocks(projects: Path, *, count: int) -> None:
    """Дополняет нормализацию до ``count`` sync-блоков (uid ``extra-N``)."""
    path = projects / "demo" / "work" / "scenario_normalized.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    first = data["blocks"][0]
    blocks = [first]
    for number in range(1, count):
        blocks.append({**first, "uid": f"extra-{number}", "order": 10 + number})
    data["blocks"] = blocks
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _prepare(client, projects: Path, *, count: int = 1) -> None:
    _write_confirmed_normalization(projects)
    _rewrite_blocks(projects, count=count)
    assert _post_source(client).status_code == 201
    _seed_words(client)


def _fake_match(request) -> SyncMatch:
    return SyncMatch(
        segment_id=request.segment_id,
        query_text=request.text,
        speaker=request.speaker,
        source_id="vasilieva_01",
        source_in_sec=10.0,
        source_out_sec=12.5,
        confidence=0.95,
        status="auto_matched",
        matched_text="маленькая женщина",
        strategy="full_transcript",
    )


def _run(client, **patches: Any) -> dict:
    with (
        patch.object(api.threading, "Thread", _ImmediateThread),
        patch.object(api.SyncMatcher, "match", patches.pop("match", None) or _fake_match_method),
    ):
        response = client.post("/stage2/sync-jobs/start", json={"project": "demo"})
    assert response.status_code == 200, response.get_json()
    return client.get(response.get_json()["result"]["status_url"]).get_json()["result"]


def _fake_match_method(_matcher, request) -> SyncMatch:
    return _fake_match(request)


def _store(projects: Path) -> api.SyncMatchStore:
    return api.SyncMatchStore(projects / "demo" / "work" / "sync_matches.json")


def _assert_released() -> None:
    with api._SYNC_JOBS_LOCK:
        assert "demo" not in api._SYNC_ACTIVE_PROJECTS
        assert not api._SYNC_CANCEL_EVENTS


def test_segment_without_stable_uid_is_recorded_as_failure_and_skipped(sync_api) -> None:
    client, projects = sync_api
    _prepare(client, projects, count=2)
    original_units = api._sync_alignment_units

    def units_with_blank_uid(*args, **kwargs):
        # Старт-роут не пропускает блок без uid, поэтому пустой segment_id
        # подставляется на уровне единиц выравнивания.
        units = original_units(*args, **kwargs)
        units[1]["segment_id"] = ""
        return units

    with patch.object(api, "_sync_alignment_units", units_with_blank_uid):
        job = _run(client)

    assert job["status"] == "completed"
    assert job["segment_failures"] == [
        {"segment_id": "", "error": "sync/host block has no stable uid"}
    ]
    assert [item.segment_id for item in _store(projects).list()] == ["stable-sync-uid"]
    _assert_released()


def test_matcher_exception_is_isolated_per_segment_and_job_completes(sync_api) -> None:
    client, projects = sync_api
    _prepare(client, projects, count=2)

    def match(_matcher, request):
        if request.segment_id == "stable-sync-uid":
            raise ValueError("matcher boom")
        return _fake_match(request)

    job = _run(client, match=match)

    assert job["status"] == "completed"
    assert job["segment_failures"] == [{"segment_id": "stable-sync-uid", "error": "matcher boom"}]
    assert job["done"] == 2
    assert [item.segment_id for item in _store(projects).list()] == ["extra-1"]
    _assert_released()


def test_non_budget_llm_review_error_is_recorded_and_segment_still_matched(sync_api) -> None:
    client, projects = sync_api

    job = _start_low_confidence_llm_job(client, projects, RuntimeError("llm down"))

    assert job["status"] == "completed"
    assert job["llm_review_failures"] == [{"segment_id": "stable-sync-uid", "error": "llm down"}]
    # Упавший вызов тоже расходует лимит ревью.
    assert job["llm_reviewed"] == 1
    assert job["segment_failures"] == []
    saved = _store(projects).get("stable-sync-uid")
    assert saved is not None
    assert saved.status == "needs_review"
    _assert_released()


def test_checkpoint_persists_intermediate_matches_after_every_tenth_segment(sync_api) -> None:
    client, projects = sync_api
    _prepare(client, projects, count=12)
    persisted_before_call: dict[int, int] = {}
    calls = {"n": 0}

    def match(_matcher, request):
        calls["n"] += 1
        persisted_before_call[calls["n"]] = len(_store(projects).list())
        return _fake_match(request)

    job = _run(client, match=match)

    assert job["status"] == "completed"
    # До 10-го сегмента на диске пусто; перед 11-м уже лежат 10 матчей.
    assert persisted_before_call[10] == 0
    assert persisted_before_call[11] == 10
    assert len(_store(projects).list()) == 12


def test_cancel_mid_loop_checkpoints_partial_matches_and_releases_project(sync_api) -> None:
    client, projects = sync_api
    _prepare(client, projects, count=4)
    calls = {"n": 0}

    def match(_matcher, request):
        calls["n"] += 1
        if calls["n"] == 2:
            with api._SYNC_JOBS_LOCK:
                for event in api._SYNC_CANCEL_EVENTS.values():
                    event.set()
        return _fake_match(request)

    job = _run(client, match=match)

    assert job["status"] == "cancelled"
    assert job["message"] == "Cancelled; saved indexes and matches can be resumed"
    assert calls["n"] == 2
    assert [item.segment_id for item in _store(projects).list()] == [
        "stable-sync-uid",
        "extra-1",
    ]
    _assert_released()


def test_checkpoint_error_on_cancel_is_logged_and_cancelled_status_still_set(
    sync_api, caplog
) -> None:
    client, projects = sync_api
    _prepare(client, projects, count=3)
    calls = {"n": 0}

    def match(_matcher, request):
        calls["n"] += 1
        if calls["n"] == 1:
            with api._SYNC_JOBS_LOCK:
                for event in api._SYNC_CANCEL_EVENTS.values():
                    event.set()
        return _fake_match(request)

    with (
        caplog.at_level("ERROR"),
        patch.object(api.SyncMatchStore, "replace_all", side_effect=OSError("disk full")),
    ):
        job = _run(client, match=match)

    assert job["status"] == "cancelled"
    assert "Could not checkpoint cancelled sync job" in caplog.text
    _assert_released()


def test_checkpoint_error_on_failure_is_logged_and_failed_status_keeps_original_error(
    sync_api, caplog
) -> None:
    client, projects = sync_api
    _prepare(client, projects, count=1)

    original_update = api._update_sync_job

    def update(job_id, *args, **kwargs):
        if "done" in kwargs:  # прогресс после первого сегмента (dirty=True)
            raise RuntimeError("progress boom")
        return original_update(job_id, *args, **kwargs)

    with (
        caplog.at_level("ERROR"),
        patch.object(api, "_update_sync_job", update),
        patch.object(api.SyncMatchStore, "replace_all", side_effect=OSError("disk full")),
    ):
        job = _run(client)

    assert job["status"] == "failed"
    assert job["error"] == "progress boom"
    assert "Could not checkpoint failed sync job" in caplog.text
    _assert_released()


@pytest.mark.parametrize(
    "dependency",
    ["_project_paths", "_sync_stores", "_sync_cancel_event", "_make_openrouter_usage_callback"],
)
def test_early_setup_failure_fails_job_and_releases_project(sync_api, dependency: str) -> None:
    _client, _projects = sync_api
    assert api._reserve_sync_project("demo")
    created = api._create_sync_job("demo")

    original = getattr(api, dependency)
    calls = {"n": 0}

    def fail_once(*args, **kwargs):
        # Падает только первый вызов (из воркера): дальше _persist_sync_job
        # использует те же функции, и их отказ скрыл бы проверяемое поведение.
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("setup boom")
        return original(*args, **kwargs)

    with patch.object(api, dependency, fail_once):
        api._run_sync_job(
            job_id=created["id"],
            project="demo",
            backend=None,
            model=None,
            language="ru",
            timecode_mode="relative",
            rematch_confirmed=False,
            source_ids=(),
            source_scope="all",
            force_transcribe=False,
            search_window_sec=180.0,
        )

    state = api._get_sync_job(created["id"])
    assert state is not None
    assert state["status"] == "failed"
    assert state["error"] == "setup boom"
    _assert_released()
    # Проект можно резервировать повторно (иначе следующий старт даст 409).
    assert api._reserve_sync_project("demo")
    api._release_sync_project("demo")


def test_match_phase_gets_cancel_aware_window_transcriber(sync_api) -> None:
    client, projects = sync_api
    _prepare(client, projects, count=1)
    seen: dict[str, Any] = {}
    calls: list[dict[str, Any]] = []

    def fake_transcribe(clip, **kwargs):
        calls.append({"clip": clip, **kwargs})
        return {"words": [{"word": "а", "start": 0.0, "end": 0.5}]}

    import api_sync_worker

    real_run_match_phase = api_sync_worker.run_match_phase

    def spy(ctx, *args, **kwargs):
        seen["ctx"] = ctx
        seen["words"] = ctx.transcribe_window(Path("clip.wav"), "small")
        return real_run_match_phase(ctx, *args, **kwargs)

    with (
        patch.object(api_sync_worker, "run_match_phase", spy),
        patch.object(api, "transcribe_audio", fake_transcribe),
    ):
        job = _run(client)

    assert job["status"] == "completed"
    assert seen["words"] == [{"word": "а", "start": 0.0, "end": 0.5}]
    assert seen["ctx"].resolve_audio_path is not None
    (call,) = calls
    assert call["backend"] == "faster-whisper" and call["model"] == "small"
    assert call["language"] == "ru"
    assert call["cancel_event"] is not None  # the job's cancel event reaches the Whisper worker
    _assert_released()
