from __future__ import annotations

from dataclasses import dataclass, field
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

    def update(self, job_id: str, updates: dict[str, Any]) -> dict[str, Any] | None:
        return update_job(self.lock, self.jobs, job_id, updates)

    def get(self, job_id: str) -> dict[str, Any] | None:
        return get_job(self.lock, self.jobs, job_id)


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
) -> dict[str, Any] | None:
    with lock:
        job = jobs.get(job_id)
        if job is None:
            return None
        job.update(updates)
        return dict(job)


def get_job(
    lock: threading.Lock,
    jobs: dict[str, dict[str, Any]],
    job_id: str,
) -> dict[str, Any] | None:
    with lock:
        job = jobs.get(job_id)
        return dict(job) if job is not None else None
