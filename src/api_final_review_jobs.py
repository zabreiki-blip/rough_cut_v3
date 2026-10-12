"""State management and worker orchestration for final timeline review."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from api_service_contracts import FinalReviewJobServices
from job_runtime import trim_terminal_jobs

_FINAL_REVIEW_JOBS_MAX = 100
_FINAL_REVIEW_JOBS_TTL_SECONDS = 24 * 60 * 60


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
    }
    job_id = str(job["id"])
    with services._FINAL_REVIEW_JOBS_LOCK:
        services._FINAL_REVIEW_JOBS[job_id] = job
        trim_terminal_jobs(
            services._FINAL_REVIEW_JOBS,
            max_jobs=_FINAL_REVIEW_JOBS_MAX,
            ttl_seconds=_FINAL_REVIEW_JOBS_TTL_SECONDS,
        )
    services._atomic_write_json_file(
        services._project_paths(project)["final_review_job_status"], job
    )
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
    if str(restored.get("status") or "").lower() in {"queued", "running"}:
        # The worker thread died with the process. Report a terminal state
        # rather than allowing the CEP panel to poll a job that cannot advance.
        restored["status"] = "interrupted"
        restored["message"] = "Interrupted by restart; start final review again"
    return restored


def update_final_review_job(
    services: FinalReviewJobServices,
    job_id: str,
    **updates: Any,
) -> dict[str, Any] | None:
    snapshot = services.update_job(
        services._FINAL_REVIEW_JOBS_LOCK,
        services._FINAL_REVIEW_JOBS,
        job_id,
        updates,
    )
    if snapshot:
        services._atomic_write_json_file(
            services._project_paths(str(snapshot["project"]))["final_review_job_status"],
            snapshot,
        )
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
) -> None:
    usage_callback = services._make_openrouter_usage_callback(
        project,
        stage="assembly",
        operation="final_review_asr",
        job_id=job_id,
    )
    try:
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
        result = services.transcribe_audio(
            audio_path,
            backend=backend,
            model=model,
            language=language,
        )
        result_backend_metadata = result.get("backend_metadata") or {}
        if str(result.get("backend") or backend or "") == "cloud-audio":
            usage_callback(
                {
                    "event_id": f"final-review-asr:{job_id}",
                    "model": str(result.get("model") or model or ""),
                    "cost_usd": result_backend_metadata.get("cost_usd"),
                }
            )
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
        services.save_word_index(paths["final_review_words"], index)
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
        services._atomic_write_json_file(paths["final_review_report"], report)
        services._update_final_review_job(
            job_id,
            status="completed",
            message="Final review completed",
            finished_at=services._utc_now_iso(),
            report_path=str(paths["final_review_report"].relative_to(paths["root"])),
        )
    except Exception as exc:
        failed_cost = getattr(exc, "cost_usd", None)
        if failed_cost is not None:
            usage_callback(
                {
                    "event_id": f"final-review-asr:{job_id}",
                    "model": str(model or ""),
                    "cost_usd": failed_cost,
                }
            )
        services._update_final_review_job(
            job_id,
            status="failed",
            message="Final review failed",
            error=str(exc),
            finished_at=services._utc_now_iso(),
        )
    finally:
        services._release_final_review_project(project)


def record_final_review_iteration(
    services: FinalReviewJobServices,
    project: str,
    job: dict[str, Any],
    corrections: list[dict[str, Any]],
) -> dict[str, Any]:
    paths = services._project_paths(project)
    history = services._read_json_safe(paths["final_review_iterations"])
    items = list(history.get("iterations") or [])
    iteration = {
        "id": f"iteration_{len(items) + 1:04d}",
        "review_job_id": job["id"],
        "source_sequence": dict(job.get("source_sequence") or {}),
        "corrections": corrections,
        "status": "prepared",
        "created_at": services._utc_now_iso(),
    }
    items.append(iteration)
    services._atomic_write_json_file(paths["final_review_iterations"], {"iterations": items})
    return iteration
