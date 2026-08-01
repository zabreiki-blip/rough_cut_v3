from __future__ import annotations

import threading
from typing import Any


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
