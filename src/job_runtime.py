from __future__ import annotations

from collections.abc import Callable, Collection, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
import os
import threading
from typing import Any


@dataclass
class JobRegistry:
    """Thread-safe in-memory state shared by one background-job subsystem."""

    jobs: dict[str, dict[str, Any]] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)
    active_projects: set[str] = field(default_factory=set)
    cancel_events: dict[str, threading.Event] = field(default_factory=dict)


def reserve_project(lock: threading.Lock, active: set[str], project: str) -> bool:
    with lock:
        if project in active:
            return False
        active.add(project)
        return True


def release_project(lock: threading.Lock, active: set[str], project: str) -> None:
    with lock:
        active.discard(project)


def update_job(
    lock: threading.Lock,
    jobs: dict[str, dict[str, Any]],
    job_id: str,
    updates: dict[str, Any],
    *,
    increments: dict[str, int | float] | None = None,
) -> dict[str, Any] | None:
    """Apply ``updates`` and ``increments`` to a job under a single lock.

    ``increments`` is a mapping of field name to delta, applied atomically
    (read-modify-write under ``lock``) so concurrent callers (e.g. worker
    threads reporting progress) never lose an increment to a race between
    reading and writing the field.
    """
    with lock:
        job = jobs.get(job_id)
        if job is None:
            return None
        job.update(updates)
        if increments:
            for key, delta in increments.items():
                job[key] = (job.get(key) or 0) + delta
        return dict(job)


def get_job(
    lock: threading.Lock,
    jobs: dict[str, dict[str, Any]],
    job_id: str,
) -> dict[str, Any] | None:
    with lock:
        job = jobs.get(job_id)
        return dict(job) if job is not None else None


def trim_terminal_jobs(
    jobs: dict[str, dict[str, Any]],
    *,
    terminal_statuses: Collection[str],
    now: datetime,
    ttl_seconds: float,
    max_jobs: int,
    on_evict: Callable[[str], None] | None = None,
) -> list[str]:
    """Bound a job registry; the caller must hold the registry lock.

    Terminal jobs older than ``ttl_seconds`` (by ``finished_at``, falling back
    to ``created_at``) are dropped, then the oldest terminal jobs are dropped
    until ``len(jobs) <= max_jobs``. Non-terminal jobs are never evicted, so the
    registry may exceed ``max_jobs`` while many jobs are active. Returns the
    evicted ids.
    """

    def stamp(job_id: str) -> str:
        job = jobs[job_id]
        return str(job.get("finished_at") or job.get("created_at") or "")

    evicted: list[str] = []

    def evict(job_id: str) -> None:
        jobs.pop(job_id, None)
        evicted.append(job_id)
        if on_evict is not None:
            on_evict(job_id)

    def terminal_ids() -> list[str]:
        return [job_id for job_id, job in jobs.items() if job.get("status") in terminal_statuses]

    for job_id in terminal_ids():
        try:
            expired = (now - datetime.fromisoformat(stamp(job_id))).total_seconds() > ttl_seconds
        except (TypeError, ValueError):
            expired = False
        if expired:
            evict(job_id)

    drop_count = max(0, len(jobs) - max_jobs)
    for job_id in sorted(terminal_ids(), key=lambda job_id: (stamp(job_id), job_id))[:drop_count]:
        evict(job_id)
    return evicted


def asr_concurrency_limit(env: Mapping[str, str] | None = None) -> int:
    """Return the global ASR job cap from ``ROUGH_CUT_MAX_ASR_JOBS`` (default 1).

    Non-integer or non-positive values fall back to 1 rather than disabling
    the limit, so a typo can never make transcription unbounded.
    """
    source = os.environ if env is None else env
    try:
        value = int(str(source.get("ROUGH_CUT_MAX_ASR_JOBS", "1")).strip())
    except (TypeError, ValueError):
        return 1
    return value if value >= 1 else 1


@contextmanager
def asr_slot(
    semaphore: Any,
    *,
    check_stop: Callable[[], None],
    on_wait: Callable[[], object] | None = None,
    on_acquired: Callable[[], object] | None = None,
) -> Iterator[None]:
    """Hold one global ASR slot for the body, waiting cancel-aware.

    ``check_stop`` must raise to abort (cancel/deadline); it runs before every
    acquire attempt, so a job cancelled while queued never takes a slot and
    never reaches the transcription call. ``on_wait`` fires once when the slot
    is not immediately free, ``on_acquired`` once after a wait. The slot is
    released only when it was actually acquired, even if the body or a hook
    raises.
    """
    waited = False
    while True:
        check_stop()
        if semaphore.acquire(timeout=0.25):
            break
        if not waited:
            waited = True
            if on_wait is not None:
                on_wait()
    try:
        if waited:
            if on_acquired is not None:
                on_acquired()
            check_stop()
        yield
    finally:
        semaphore.release()
