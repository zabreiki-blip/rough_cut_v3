"""Routes for reviewing a rendered Rough Cut timeline.

Audio is accepted by path only when it is already inside the selected project.
CEP produces that WAV locally, which keeps this endpoint free of arbitrary
server-path access and avoids duplicating the large guide upload protocol.
"""

from __future__ import annotations

from pathlib import Path
from types import ModuleType
import time

from flask import Flask, jsonify, request


def _project_audio_path(services: ModuleType, paths: dict[str, Path], value: object) -> Path:
    raw = str(value or "").strip()
    if not raw:
        return paths["final_review_audio"]
    candidate = Path(raw)
    resolved = candidate.resolve() if candidate.is_absolute() else (paths["root"] / candidate).resolve()
    if not resolved.is_relative_to(paths["root"].resolve()) or resolved.suffix.lower() != ".wav":
        raise ValueError("audio_path must be a WAV inside the project directory")
    return resolved


def _stable_wav(path: Path) -> bool:
    """Reject an AME/Premiere file while its writer still has it open."""
    first = path.stat()
    time.sleep(2)
    second = path.stat()
    return first.st_size > 44 and (first.st_size, first.st_mtime_ns) == (
        second.st_size,
        second.st_mtime_ns,
    )


def _start(services: ModuleType):
    payload = request.get_json(silent=True) or {}
    try:
        project = services._resolve_project_name(payload.get("project"))
        paths = services._ensure_project(project)
        audio_path = _project_audio_path(services, paths, payload.get("audio_path"))
    except ValueError as exc:
        return services._json_error(str(exc), 400)
    gate = services._normalization_required(project)
    if gate:
        return gate
    if not audio_path.is_file():
        return services._json_error("final review WAV was not found", 404)
    if not _stable_wav(audio_path):
        return services._json_error("final review WAV is still being exported", 409)
    if not services._reserve_final_review_project(project):
        return services._json_error(f"another mutating job is active for project {project}", 409)
    try:
        job = services._create_final_review_job(project, audio_path, payload)
        worker = services.threading.Thread(
            target=services._run_final_review_job,
            kwargs={"job_id": job["id"], "project": project, "audio_path": audio_path,
                    "language": str(payload.get("language") or "ru"),
                    "backend": str(payload.get("backend") or "") or None,
                    "model": str(payload.get("model") or "") or None,
                    "source_sequence": dict(payload.get("source_sequence") or {})},
            daemon=True,
        )
        worker.start()
    except Exception:
        services._release_final_review_project(project)
        raise
    return jsonify({"ok": True, "result": {"project": project, "job_id": job["id"], "status": "queued", "status_url": f"/stage2/final-review/jobs/{job['id']}"}}), 202


def _status(services: ModuleType, job_id: str):
    job = services._get_final_review_job(job_id)
    if not job:
        return services._json_error("job not found", 404)
    return jsonify({"ok": True, "result": job})


def _apply(services: ModuleType, job_id: str):
    payload = request.get_json(silent=True) or {}
    job = services._get_final_review_job(job_id)
    if not job:
        return services._json_error("job not found", 404)
    if job.get("status") != "completed":
        return services._json_error("final review is not completed", 409)
    try:
        project = services._resolve_project_name(payload.get("project") or job.get("project"))
    except ValueError as exc:
        return services._json_error(str(exc), 400)
    if project != job.get("project"):
        return services._json_error("project does not match final review job", 409)
    report = services._read_json_safe(services._project_paths(project)["final_review_report"])
    corrections = report.get("corrections") if isinstance(report.get("corrections"), list) else []
    iteration = services._record_final_review_iteration(project, job, corrections)
    return jsonify({"ok": True, "result": {"project": project, "iteration": iteration, "corrections": corrections}})


def register_final_review_routes(app: Flask, services: ModuleType) -> None:
    @app.post("/stage2/final-review/start")
    def final_review_start():
        return _start(services)

    @app.get("/stage2/final-review/jobs/<job_id>")
    def final_review_status(job_id: str):
        return _status(services, job_id)

    @app.post("/stage2/final-review/jobs/<job_id>/apply")
    def final_review_apply(job_id: str):
        return _apply(services, job_id)
