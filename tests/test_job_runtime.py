from __future__ import annotations

import threading
import time

import pytest

import api
from job_runtime import JobRegistry, asr_concurrency_limit, asr_slot


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
