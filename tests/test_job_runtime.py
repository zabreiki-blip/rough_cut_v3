from __future__ import annotations

import threading

import api
from job_runtime import JobRegistry


def test_registry_owns_job_state_and_returns_snapshots() -> None:
    registry = JobRegistry()
    registry.jobs["job-1"] = {"status": "queued"}

    snapshot = registry.update("job-1", {"status": "running"})

    assert snapshot == {"status": "running"}
    assert snapshot is not registry.jobs["job-1"]
    assert registry.get("missing") is None


def test_registry_reserves_projects_atomically() -> None:
    registry = JobRegistry()
    outcomes: list[bool] = []

    def reserve() -> None:
        outcomes.append(registry.reserve("demo"))

    threads = [threading.Thread(target=reserve) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert outcomes.count(True) == 1
    assert outcomes.count(False) == 7
    registry.release("demo")
    assert registry.reserve("demo")


def test_registry_increments_field_atomically_under_concurrency() -> None:
    registry = JobRegistry()
    registry.jobs["job-1"] = {"retry_count": 0}
    iterations = 200

    def bump() -> None:
        for _ in range(iterations):
            registry.update("job-1", {}, increments={"retry_count": 1})

    threads = [threading.Thread(target=bump) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert registry.jobs["job-1"]["retry_count"] == 8 * iterations


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


def test_trim_terminal_jobs_applies_ttl_and_cap_but_keeps_active_jobs() -> None:
    from datetime import UTC, datetime, timedelta

    from job_runtime import trim_terminal_jobs

    now = datetime(2026, 1, 10, tzinfo=UTC)

    def stamp(days: float) -> str:
        return (now - timedelta(days=days)).isoformat()

    jobs = {
        "running": {"status": "running", "created_at": stamp(30)},
        "queued": {"status": "queued", "created_at": stamp(30)},
        "old": {"status": "completed", "finished_at": stamp(5)},
        "bad": {"status": "failed", "finished_at": "not-a-date"},
        "b": {"status": "completed", "finished_at": stamp(0.3)},
        "c": {"status": "failed", "finished_at": stamp(0.2)},
        "d": {"status": "completed", "finished_at": stamp(0.1)},
    }

    dropped = trim_terminal_jobs(jobs, max_jobs=5, ttl_seconds=86400, now=now)

    assert dropped == ["old", "bad"]  # unparseable time is the oldest
    assert set(jobs) == {"running", "queued", "b", "c", "d"}

    trim_terminal_jobs(jobs, max_jobs=0, now=now)
    assert set(jobs) == {"running", "queued"}
