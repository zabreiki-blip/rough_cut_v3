from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import UTC, datetime
import threading
from typing import Any


@dataclass
class JobRegistry:
    """Thread-safe in-memory state shared by one background-job subsystem."""

    jobs: dict[str, dict[str, Any]] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)
    active_projects: set[str] = field(default_factory=set)
    cancel_events: dict[str, threading.Event] = field(default_factory=dict)

    def reserve(self, project: str) -> bool:
        return reserve_project(self.lock, self.active_projects, project)

    def release(self, project: str) -> None:
        release_project(self.lock, self.active_projects, project)

    def update(
        self,
        job_id: str,
        updates: dict[str, Any],
        *,
        increments: dict[str, int | float] | None = None,
    ) -> dict[str, Any] | None:
        return update_job(self.lock, self.jobs, job_id, updates, increments=increments)

    def get(self, job_id: str) -> dict[str, Any] | None:
        return get_job(self.lock, self.jobs, job_id)


def acquire_slot(
    semaphore: threading.Semaphore,
    cancel_event: threading.Event | None = None,
    *,
    poll_seconds: float = 0.25,
) -> bool:
    """Wait (queue) for a global concurrency slot, like the TTS job loop.

    Returns ``True`` once the slot is held (the caller must release it), or
    ``False`` if ``cancel_event`` was set while still waiting.
    """

    while True:
        if cancel_event is not None and cancel_event.is_set():
            return False
        if semaphore.acquire(timeout=poll_seconds):
            return True


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


TERMINAL_JOB_STATUSES = frozenset({"completed", "failed", "cancelled", "interrupted"})


def trim_terminal_jobs(
    jobs: dict[str, dict[str, Any]],
    *,
    max_jobs: int,
    ttl_seconds: float | None = None,
    terminal_statuses: Collection[str] = TERMINAL_JOB_STATUSES,
    now: datetime | None = None,
) -> list[str]:
    """Evict finished jobs by TTL, then oldest-first down to ``max_jobs``.

    The caller must hold the lock that guards ``jobs``. Only jobs whose status
    is in ``terminal_statuses`` are ever removed, so queued/running jobs are
    never evicted even when the cap is exceeded. Returns the evicted ids.
    """
    current = now or datetime.now(UTC)
    dropped: list[str] = []

    def stamp(job: dict[str, Any]) -> str:
        return str(job.get("finished_at") or job.get("created_at") or "")

    def sort_time(job: dict[str, Any]) -> datetime:
        # Compare instants, not ISO strings ("Z" vs "+00:00" sort wrongly).
        # Unparseable times count as the oldest so they are evicted first.
        try:
            parsed = datetime.fromisoformat(stamp(job))
        except (TypeError, ValueError):
            return datetime.min.replace(tzinfo=UTC)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)

    if ttl_seconds is not None:
        for job_id, job in list(jobs.items()):
            if job.get("status") not in terminal_statuses:
                continue
            try:
                parsed = datetime.fromisoformat(stamp(job))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=UTC)
                expired = (current - parsed).total_seconds() > ttl_seconds
            except (TypeError, ValueError):
                expired = False
            if expired:
                jobs.pop(job_id, None)
                dropped.append(job_id)
    excess = len(jobs) - max_jobs
    if excess > 0:
        terminal = sorted(
            (item for item in jobs.items() if item[1].get("status") in terminal_statuses),
            key=lambda item: (sort_time(item[1]), item[0]),
        )
        for job_id, _job in terminal[:excess]:
            jobs.pop(job_id, None)
            dropped.append(job_id)
    return dropped
