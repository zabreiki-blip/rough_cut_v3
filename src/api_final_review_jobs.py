"""State management and worker orchestration for final timeline review."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from decimal import Decimal
import os
import threading
import time
from typing import Any

from api_service_contracts import FinalReviewJobServices
from job_runtime import asr_slot, trim_terminal_jobs
from stage2.whisper_process import WhisperCancelledError, WhisperTimeoutError


FINAL_REVIEW_JOBS_MAX = 200
FINAL_REVIEW_JOBS_TTL_SECONDS = 24 * 60 * 60
FINAL_REVIEW_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled", "interrupted"})


class FinalReviewRequestConflict(ValueError):
    """An Apply request ID has already been used for another review job."""


def canonical_decimal_string(value: object) -> str:
    amount = Decimal(str(value))
    if amount == 0:
        return "0"
    rendered = format(amount, "f")
    return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered


def final_review_deadline() -> float:
    raw = os.getenv("FINAL_REVIEW_TIMEOUT_SECONDS", "").strip() or "7200"
    try:
        seconds = int(raw)
    except ValueError as exc:
        raise ValueError("FINAL_REVIEW_TIMEOUT_SECONDS must be a positive integer") from exc
    if seconds <= 0:
        raise ValueError("FINAL_REVIEW_TIMEOUT_SECONDS must be a positive integer")
    return time.monotonic() + seconds


def reserve_final_review_project(services: FinalReviewJobServices, project: str) -> bool:
    with services._project_transition_lock(project):
        if services._project_has_active_mutation(project):
            return False
        return services.reserve_project(
            services._FINAL_REVIEW_JOBS_LOCK,
            services._FINAL_REVIEW_ACTIVE_PROJECTS,
            project,
        )


def release_final_review_project(services: FinalReviewJobServices, project: str) -> None:
    services.release_project(
        services._FINAL_REVIEW_JOBS_LOCK,
        services._FINAL_REVIEW_ACTIVE_PROJECTS,
        project,
    )


def trim_final_review_jobs_locked(services: FinalReviewJobServices) -> None:
    """Bound terminal job history; active jobs are never evicted.

    Caller holds ``_FINAL_REVIEW_JOBS_LOCK``. The per-project status file keeps
    the latest job on disk, so eviction from memory does not lose it.
    """
    try:
        now = datetime.fromisoformat(services._utc_now_iso())
    except (TypeError, ValueError):
        return
    trim_terminal_jobs(
        services._FINAL_REVIEW_JOBS,
        terminal_statuses=FINAL_REVIEW_TERMINAL_STATUSES,
        now=now,
        ttl_seconds=FINAL_REVIEW_JOBS_TTL_SECONDS,
        max_jobs=FINAL_REVIEW_JOBS_MAX,
        on_evict=lambda job_id: services._FINAL_REVIEW_CANCEL_EVENTS.pop(job_id, None),
    )


def create_final_review_job(
    services: FinalReviewJobServices,
    project: str,
    audio_path: Path,
    payload: dict[str, Any],
) -> dict[str, Any]:
    now = services._utc_now_iso()
    job = {
        "id": services.uuid.uuid4().hex,
        "project": project,
        "status": "queued",
        "message": "Queued",
        "audio_path": str(audio_path.relative_to(services._project_paths(project)["root"])),
        "source_sequence": dict(payload.get("source_sequence") or {}),
        "created_at": now,
        "assembly_manifest_id": str(payload.get("assembly_manifest_id") or ""),
        "started_at": "",
        "finished_at": "",
        "error": "",
        "report_path": "",
        "estimated_cost_usd": canonical_decimal_string(payload.get("estimated_cost_usd", "0")),
        "cost_limit_usd": payload.get("cost_limit_usd", 0.0),
        "cost_usd": 0.0,
        "reserved_cost_usd": 0.0,
        "cost_pending": False,
        "transcription_processed_sec": 0.0,
        "transcription_duration_sec": payload.get("duration_sec", 0.0),
        "transcription_words": 0,
        "deadline_monotonic": (
            payload["deadline_monotonic"]
            if "deadline_monotonic" in payload
            else final_review_deadline()
        ),
    }
    job_id = str(job["id"])
    with services._FINAL_REVIEW_JOBS_LOCK:
        services._FINAL_REVIEW_JOBS[job_id] = job
        services._FINAL_REVIEW_CANCEL_EVENTS[job_id] = threading.Event()
        try:
            services._atomic_write_json_file(
                services._project_paths(project)["final_review_job_status"], job
            )
        except Exception:
            services._FINAL_REVIEW_JOBS.pop(job_id, None)
            services._FINAL_REVIEW_CANCEL_EVENTS.pop(job_id, None)
            raise
        trim_final_review_jobs_locked(services)
    return dict(job)


def get_final_review_job(services: FinalReviewJobServices, job_id: str) -> dict[str, Any] | None:
    return services.get_job(
        services._FINAL_REVIEW_JOBS_LOCK,
        services._FINAL_REVIEW_JOBS,
        job_id,
    )


def load_persisted_final_review_job(
    services: FinalReviewJobServices, project: str
) -> dict[str, Any] | None:
    """Restore the latest final-review checkpoint after a process restart."""
    payload = services._read_json_safe(services._project_paths(project)["final_review_job_status"])
    if not isinstance(payload, dict):
        return None
    if payload.get("project") != project or not str(payload.get("id") or "").strip():
        return None
    restored = dict(payload)
    if str(restored.get("status") or "").lower() in {"queued", "running", "cancelling"}:
        # The worker thread died with the process. Report a terminal state
        # rather than allowing the CEP panel to poll a job that cannot advance.
        restored["status"] = "interrupted"
        restored["message"] = "Interrupted by restart; start final review again"
    return restored


def get_latest_final_review_job(
    services: FinalReviewJobServices, project: str
) -> dict[str, Any] | None:
    with services._FINAL_REVIEW_JOBS_LOCK:
        candidates = [
            dict(job)
            for job in services._FINAL_REVIEW_JOBS.values()
            if job.get("project") == project
        ]
    if candidates:
        return max(candidates, key=lambda job: str(job.get("created_at") or ""))
    return load_persisted_final_review_job(services, project)


def cancel_final_review_job(
    services: FinalReviewJobServices, job_id: str
) -> tuple[dict[str, Any] | None, str | None]:
    with services._FINAL_REVIEW_JOBS_LOCK:
        job = services._FINAL_REVIEW_JOBS.get(job_id)
        if job is None:
            return None, "job not found"
        if job.get("status") not in {"queued", "running", "cancelling"}:
            return dict(job), None
        event = services._FINAL_REVIEW_CANCEL_EVENTS.get(job_id)
        if event is None:
            return None, "job worker is not active"
        event.set()
        job.update(status="cancelling", message="Cancellation requested; stopping final review")
        snapshot = dict(job)
        services._atomic_write_json_file(
            services._project_paths(str(job["project"]))["final_review_job_status"], snapshot
        )
        return snapshot, None


def update_final_review_job(
    services: FinalReviewJobServices,
    job_id: str,
    *,
    persist: bool = True,
    **updates: Any,
) -> dict[str, Any] | None:
    # Serialize mutation and persistence so a slow progress write cannot
    # overwrite a newer cancellation or terminal checkpoint.
    with services._FINAL_REVIEW_JOBS_LOCK:
        job = services._FINAL_REVIEW_JOBS.get(job_id)
        if job is None:
            return None
        if updates.get("status") == "running" and job.get("status") == "cancelling":
            updates.pop("status")
            updates.pop("message", None)
        job.update(updates)
        snapshot = dict(job)
        if persist:
            services._atomic_write_json_file(
                services._project_paths(str(snapshot["project"]))["final_review_job_status"],
                snapshot,
            )
        if snapshot.get("status") in FINAL_REVIEW_TERMINAL_STATUSES:
            trim_final_review_jobs_locked(services)
        return snapshot


def run_final_review_job(
    services: FinalReviewJobServices,
    *,
    job_id: str,
    project: str,
    audio_path: Path,
    language: str,
    backend: str | None,
    model: str | None,
    source_sequence: dict[str, Any],
    cloud_options: dict[str, Any] | None = None,
) -> None:
    usage_callback = None
    usage_recorded = False
    reserved_usage_recorded = False
    with services._FINAL_REVIEW_JOBS_LOCK:
        job = dict(services._FINAL_REVIEW_JOBS.get(job_id) or {})
        cancel_event = services._FINAL_REVIEW_CANCEL_EVENTS.get(job_id)
    deadline = job.get("deadline_monotonic")
    last_persisted = float("-inf")
    provider_costs = backend is None or backend in {"cloud-audio", "cloud", "openrouter"}

    def check_stop() -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise WhisperCancelledError("Final review cancelled")
        if deadline is not None and time.monotonic() >= deadline:
            raise WhisperTimeoutError("Final review whole-job deadline exceeded")

    def checkpoint(**updates: Any) -> None:
        nonlocal last_persisted
        now = time.monotonic()
        persist = now - last_persisted >= 1.0
        services._update_final_review_job(job_id, persist=persist, **updates)
        if persist:
            last_persisted = now

    def progress(processed: float, duration: float, words: int) -> None:
        checkpoint(
            transcription_processed_sec=processed,
            transcription_duration_sec=duration,
            transcription_words=words,
        )

    def budget(snapshot: dict[str, float]) -> None:
        if not provider_costs:
            return
        checkpoint(
            cost_usd=snapshot["spent_usd"],
            reserved_cost_usd=snapshot["reserved_usd"],
            cost_limit_usd=snapshot["limit_usd"],
            **(
                {"cost_pending": bool(snapshot["cost_pending"])}
                if "cost_pending" in snapshot
                else {}
            ),
        )

    def cost_updates(source: Any) -> dict[str, Any]:
        values: dict[str, Any] = {}
        if not provider_costs:
            return values
        for field in ("cost_usd", "reserved_cost_usd", "cost_limit_usd"):
            value = source.get(field) if isinstance(source, dict) else getattr(source, field, None)
            if value is not None:
                values[field] = float(value)
        pending = (
            source.get("cost_pending")
            if isinstance(source, dict)
            else getattr(source, "cost_pending", None)
        )
        if pending is not None:
            values["cost_pending"] = bool(pending)
        return values

    def record_usage(
        cost: Any,
        selected_model: str,
        *,
        cost_pending: bool = False,
        reserved_cost_usd: Any = None,
        allow_unknown: bool = False,
    ) -> None:
        nonlocal usage_recorded, reserved_usage_recorded
        if usage_callback is None:
            return
        if not reserved_usage_recorded and (cost_pending or (allow_unknown and cost is None)):
            # An unresolved reservation is exposure, not confirmed spend.
            # Keep a distinct stable pending event so the real ledger retains
            # it without counting the estimate toward actual provider cost.
            usage_callback(
                {
                    "event_id": f"final-review-asr:{job_id}:reserved",
                    "model": selected_model,
                    "cost_usd": None,
                    "reserved_cost_usd": reserved_cost_usd,
                    "pending": True,
                }
            )
            reserved_usage_recorded = True
        if not usage_recorded and cost is not None and (not cost_pending or cost > 0):
            usage_callback(
                {
                    "event_id": f"final-review-asr:{job_id}",
                    "model": selected_model,
                    "cost_usd": cost,
                }
            )
            usage_recorded = True

    def finish_error(exc: Exception, *, cancelled: bool = False) -> None:
        failed_cost = cost_updates(exc)
        latest = get_final_review_job(services, job_id) or {}
        try:
            if provider_costs and (backend == "cloud-audio" or "cost_usd" in failed_cost):
                record_usage(
                    failed_cost.get("cost_usd", latest.get("cost_usd")),
                    str(model or ""),
                    cost_pending=bool(
                        failed_cost.get("cost_pending", latest.get("cost_pending", False))
                    ),
                    reserved_cost_usd=failed_cost.get(
                        "reserved_cost_usd", latest.get("reserved_cost_usd")
                    ),
                )
        finally:
            services._update_final_review_job(
                job_id,
                status="cancelled" if cancelled else "failed",
                message="Final review cancelled" if cancelled else "Final review failed",
                error="" if cancelled else str(exc),
                finished_at=services._utc_now_iso(),
                **({"asr_waiting": False} if latest.get("asr_waiting") else {}),
                **failed_cost,
            )

    try:
        check_stop()
        usage_callback = services._make_openrouter_usage_callback(
            project, stage="assembly", operation="final_review_asr", job_id=job_id
        )
        paths = services._project_paths(project)
        manifest = services._read_json_safe(paths["assembly_manifest"])
        services.validate_assembly_manifest(
            manifest,
            timeline_path=paths["timeline"],
            scenario_path=paths["normalized"],
            audio_path=audio_path,
        )
        services._update_final_review_job(
            job_id,
            status="running",
            message="Transcribing rendered timeline",
            started_at=services._utc_now_iso(),
        )
        check_stop()
        # The slot covers only the ASR call. Waiting honours cancel and the
        # whole-job deadline, and a stopped waiter never takes a slot.
        with asr_slot(
            services._ASR_SEMAPHORE,
            check_stop=check_stop,
            on_wait=lambda: services._update_final_review_job(
                job_id, message="Waiting for an ASR slot", asr_waiting=True
            ),
            on_acquired=lambda: services._update_final_review_job(
                job_id, message="Transcribing rendered timeline", asr_waiting=False
            ),
        ):
            result = services.transcribe_audio(
                audio_path,
                backend=backend,
                model=model,
                language=language,
                cancel_event=cancel_event,
                deadline_monotonic=deadline,
                cloud_options=cloud_options,
                progress_callback=progress,
                budget_callback=budget,
            )
        result_backend_metadata = result.get("backend_metadata") or {}
        final_cost = cost_updates(result_backend_metadata)
        final_cost.update(cost_updates(result))
        final_snapshot = services._update_final_review_job(job_id, **final_cost) or final_cost
        if provider_costs and str(result.get("backend") or backend or "") == "cloud-audio":
            record_usage(
                final_cost.get("cost_usd"),
                str(result.get("model") or model or ""),
                cost_pending=bool(final_snapshot.get("cost_pending", False)),
                reserved_cost_usd=final_snapshot.get("reserved_cost_usd"),
                allow_unknown=True,
            )
        check_stop()
        services.validate_assembly_manifest(
            manifest,
            timeline_path=paths["timeline"],
            scenario_path=paths["normalized"],
            audio_path=audio_path,
        )
        index = services.ingest_word_index(
            result,
            "final-render",
            model=str(result.get("model") or model or ""),
            language=str(result.get("language") or language),
        )
        check_stop()
        timeline_data = services._read_json_safe(paths["work"] / "full_timeline.json")
        timeline = services.FullTimeline.from_dict(timeline_data)
        report = services.review_rendered_timeline(timeline, index)
        report.update(
            {
                "project": project,
                "job_id": job_id,
                "created_at": services._utc_now_iso(),
                "audio_path": str(audio_path.relative_to(paths["root"])),
                "source_sequence": source_sequence,
                "scenario_revision_id": str(timeline_data.get("scenario_revision_id") or ""),
                "assembly_manifest_id": str(manifest.get("manifest_id") or ""),
                "assembly_summary": dict(manifest.get("summary") or {}),
                "assembly_findings": list(manifest.get("findings") or []),
            }
        )
        # Cancellation and artifact publication share one commit boundary.
        # A cancel accepted before this lock always wins; after publication
        # callers observe the existing terminal job without changing it.
        with services._FINAL_REVIEW_JOBS_LOCK:
            check_stop()
            services.save_word_index(paths["final_review_words"], index)
            check_stop()
            services._atomic_write_json_file(paths["final_review_report"], report)
            current = services._FINAL_REVIEW_JOBS.get(job_id)
            if current is not None:
                finished_at = services._utc_now_iso()
                check_stop()
                current.update(
                    status="completed",
                    message="Final review completed",
                    finished_at=finished_at,
                    report_path=str(paths["final_review_report"].relative_to(paths["root"])),
                )
                services._atomic_write_json_file(paths["final_review_job_status"], dict(current))
                trim_final_review_jobs_locked(services)
    except WhisperCancelledError as exc:
        finish_error(exc, cancelled=True)
    except Exception as exc:
        finish_error(exc)
    finally:
        with services._FINAL_REVIEW_JOBS_LOCK:
            services._FINAL_REVIEW_CANCEL_EVENTS.pop(job_id, None)
        services._release_final_review_project(project)


def find_final_review_iteration(
    history: dict[str, Any], job_id: str, request_id: str
) -> dict[str, Any] | None:
    if request_id:
        for item in history.get("iterations") or []:
            if item.get("request_id") == request_id:
                if item.get("review_job_id") != job_id:
                    raise FinalReviewRequestConflict(
                        "request_id has already been used for another final review job"
                    )
                return item
    return None


def record_final_review_iteration(
    services: FinalReviewJobServices,
    project: str,
    job: dict[str, Any],
    corrections: list[dict[str, Any]],
    request_id: str = "",
) -> dict[str, Any]:
    with services._project_transition_lock(project):
        paths = services._project_paths(project)
        history = services._read_json_safe(paths["final_review_iterations"])
        existing = find_final_review_iteration(history, job["id"], request_id)
        if existing is not None:
            return existing
        items = list(history.get("iterations") or [])
        iteration = {
            "id": f"iteration_{len(items) + 1:04d}",
            "review_job_id": job["id"],
            "source_sequence": dict(job.get("source_sequence") or {}),
            "corrections": corrections,
            "status": "prepared",
            "created_at": services._utc_now_iso(),
        }
        if request_id:
            iteration["request_id"] = request_id
        items.append(iteration)
        services._atomic_write_json_file(paths["final_review_iterations"], {"iterations": items})
        return iteration
