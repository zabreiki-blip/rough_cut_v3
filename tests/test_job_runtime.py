from __future__ import annotations

from datetime import UTC, datetime, timedelta
import threading
import time

import pytest

import api
from job_runtime import (
    JobRegistry,
    asr_concurrency_limit,
    asr_slot,
    release_project,
    reserve_project,
    trim_terminal_jobs,
    update_job,
)


def test_update_job_returns_snapshot_copy() -> None:
    registry = JobRegistry()
    registry.jobs["job-1"] = {"status": "queued"}

    snapshot = update_job(registry.lock, registry.jobs, "job-1", {"status": "running"})

    assert snapshot == {"status": "running"}
    assert snapshot is not registry.jobs["job-1"]
    assert update_job(registry.lock, registry.jobs, "missing", {}) is None


def test_reserve_project_is_atomic() -> None:
    registry = JobRegistry()
    outcomes: list[bool] = []

    def reserve() -> None:
        outcomes.append(reserve_project(registry.lock, registry.active_projects, "demo"))

    threads = [threading.Thread(target=reserve) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert outcomes.count(True) == 1
    assert outcomes.count(False) == 7
    release_project(registry.lock, registry.active_projects, "demo")
    assert reserve_project(registry.lock, registry.active_projects, "demo")


def test_update_job_increments_field_atomically_under_concurrency() -> None:
    registry = JobRegistry()
    registry.jobs["job-1"] = {"retry_count": 0}
    iterations = 200

    def bump() -> None:
        for _ in range(iterations):
            update_job(registry.lock, registry.jobs, "job-1", {}, increments={"retry_count": 1})

    threads = [threading.Thread(target=bump) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert registry.jobs["job-1"]["retry_count"] == 8 * iterations


# --- trim_terminal_jobs ----------------------------------------------------

_NOW = datetime(2026, 1, 10, tzinfo=UTC)
_TERMINAL = {"completed", "failed"}


def _stamp(days_ago: float) -> str:
    return (_NOW - timedelta(days=days_ago)).isoformat()


def _trim(jobs: dict, *, max_jobs: int = 100, ttl: float = 86400.0, on_evict=None) -> list[str]:
    return trim_terminal_jobs(
        jobs,
        terminal_statuses=_TERMINAL,
        now=_NOW,
        ttl_seconds=ttl,
        max_jobs=max_jobs,
        on_evict=on_evict,
    )


def test_trim_drops_oldest_terminal_over_limit_and_keeps_active() -> None:
    jobs = {
        "run": {"status": "running", "created_at": _stamp(0.5)},
        "old": {"status": "completed", "finished_at": _stamp(0.3)},
        "mid": {"status": "failed", "finished_at": _stamp(0.2)},
        "new": {"status": "completed", "finished_at": _stamp(0.1)},
    }

    evicted = _trim(jobs, max_jobs=2)

    assert evicted == ["old", "mid"]
    assert set(jobs) == {"run", "new"}


def test_trim_never_evicts_active_even_over_limit() -> None:
    jobs = {f"j{i}": {"status": "running", "created_at": _stamp(5)} for i in range(4)}

    assert _trim(jobs, max_jobs=1) == []
    assert len(jobs) == 4


def test_trim_expires_terminal_by_ttl_and_calls_on_evict() -> None:
    jobs = {
        "stale": {"status": "completed", "finished_at": _stamp(2)},
        "fresh": {"status": "completed", "finished_at": _stamp(0.5)},
        "run": {"status": "running", "created_at": _stamp(9)},
    }
    seen: list[str] = []

    evicted = _trim(jobs, on_evict=seen.append)

    assert evicted == ["stale"] == seen
    assert set(jobs) == {"fresh", "run"}


def test_trim_ttl_falls_back_to_created_at() -> None:
    jobs = {
        "stale": {"status": "failed", "created_at": _stamp(2)},
        "fresh": {"status": "failed", "created_at": _stamp(0.5)},
    }

    assert _trim(jobs) == ["stale"]


def test_trim_unparsable_stamp_is_not_expired() -> None:
    jobs = {
        "bad": {"status": "completed", "finished_at": "garbage"},
        "none": {"status": "completed"},
    }

    assert _trim(jobs) == []
    assert set(jobs) == {"bad", "none"}


def test_trim_limit_ties_broken_by_job_id() -> None:
    same = _stamp(0.1)
    jobs = {
        "b": {"status": "completed", "finished_at": same},
        "c": {"status": "completed", "finished_at": same},
        "a": {"status": "completed", "finished_at": same},
    }

    assert _trim(jobs, max_jobs=1) == ["a", "b"]
    assert set(jobs) == {"c"}


def test_api_job_facade_honors_reassigned_legacy_state(monkeypatch) -> None:
    jobs: dict[str, dict] = {}
    lock = threading.Lock()
    active: set[str] = set()
    monkeypatch.setattr(api, "_TTS_JOBS", jobs)
    monkeypatch.setattr(api, "_TTS_JOBS_LOCK", lock)
    monkeypatch.setattr(api, "_TTS_ACTIVE_PROJECTS", active)

    assert api._reserve_tts_project("demo")
    jobs["job-1"] = {"status": "queued"}
    assert api._update_tts_job("job-1", persist=False, status="running") == {"status": "running"}
    assert api._get_tts_job("job-1") == {"status": "running"}
    api._release_tts_project("demo")
    assert active == set()


# --- B8: global ASR concurrency limit -------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, 1), ("", 1), ("garbage", 1), ("0", 1), ("-3", 1), ("1.5", 1), ("2", 2), (" 4 ", 4)],
)
def test_asr_concurrency_limit_parses_env(raw, expected) -> None:
    env = {} if raw is None else {"ROUGH_CUT_MAX_ASR_JOBS": raw}
    assert asr_concurrency_limit(env) == expected


def test_api_exposes_asr_semaphore_with_default_one() -> None:
    assert isinstance(api._ASR_SEMAPHORE, type(threading.Semaphore()))
    assert api._ASR_CONCURRENT_MAX >= 1


def test_asr_slot_second_waiter_blocks_until_first_releases() -> None:
    semaphore = threading.Semaphore(1)
    first_in = threading.Event()
    release_first = threading.Event()
    order: list[str] = []
    waits: list[str] = []

    def first() -> None:
        with asr_slot(semaphore, check_stop=lambda: None):
            order.append("first-in")
            first_in.set()
            release_first.wait(5)
            order.append("first-out")

    def second() -> None:
        with asr_slot(
            semaphore,
            check_stop=lambda: None,
            on_wait=lambda: waits.append("wait"),
            on_acquired=lambda: waits.append("acquired"),
        ):
            order.append("second-in")

    t1 = threading.Thread(target=first)
    t1.start()
    assert first_in.wait(5)
    t2 = threading.Thread(target=second)
    t2.start()
    time.sleep(0.6)
    assert order == ["first-in"]
    assert waits == ["wait"]
    release_first.set()
    t1.join(5)
    t2.join(5)
    assert order == ["first-in", "first-out", "second-in"]
    assert waits == ["wait", "acquired"]
    assert semaphore.acquire(blocking=False)


def test_asr_slot_cancel_while_waiting_never_enters_and_never_releases() -> None:
    semaphore = threading.Semaphore(1)
    assert semaphore.acquire(blocking=False)  # someone else holds the only slot
    cancelled = threading.Event()
    entered: list[str] = []

    def check_stop() -> None:
        if cancelled.is_set():
            raise RuntimeError("cancelled")

    threading.Timer(0.3, cancelled.set).start()
    with pytest.raises(RuntimeError, match="cancelled"):
        with asr_slot(semaphore, check_stop=check_stop):
            entered.append("body")
    assert entered == []
    # The foreign slot must still be held: nothing was released by the waiter.
    assert not semaphore.acquire(blocking=False)
    semaphore.release()


def test_asr_slot_releases_on_exception_in_body() -> None:
    semaphore = threading.Semaphore(1)
    with pytest.raises(ValueError):
        with asr_slot(semaphore, check_stop=lambda: None):
            raise ValueError("boom")
    assert semaphore.acquire(blocking=False)
    semaphore.release()


def test_asr_slot_releases_when_post_acquire_hook_raises() -> None:
    semaphore = threading.Semaphore(1)
    assert semaphore.acquire(blocking=False)
    threading.Timer(0.3, semaphore.release).start()

    def boom() -> None:
        raise RuntimeError("hook failed")

    with pytest.raises(RuntimeError, match="hook failed"):
        with asr_slot(semaphore, check_stop=lambda: None, on_wait=lambda: None, on_acquired=boom):
            pass
    assert semaphore.acquire(blocking=False)
    semaphore.release()
