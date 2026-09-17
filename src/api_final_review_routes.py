"""Routes for reviewing a rendered Rough Cut timeline.

Audio is accepted by path only when it is already inside the selected project.
CEP produces that WAV locally, which keeps this endpoint free of arbitrary
server-path access and avoids duplicating the large guide upload protocol.
"""

from __future__ import annotations

from pathlib import Path
from types import ModuleType
import time
import wave

from flask import Flask, has_request_context, jsonify, request


def _project_audio_path(services: ModuleType, paths: dict[str, Path], value: object) -> Path:
    raw = str(value or "").strip()
    if not raw:
        return paths["final_review_audio"]
    candidate = Path(raw)
    resolved = (
        candidate.resolve() if candidate.is_absolute() else (paths["root"] / candidate).resolve()
    )
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


def _wav_duration(path: Path) -> float:
    try:
        with wave.open(str(path), "rb") as source:
            return source.getnframes() / float(source.getframerate())
    except (OSError, EOFError, wave.Error, ZeroDivisionError) as exc:
        raise ValueError(f"invalid final review WAV: {exc}") from exc


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
    timeline_path = paths["work"] / "full_timeline.json"
    if not timeline_path.is_file():
        return services._json_error("full_timeline.json not found — run Build first", 404)
    try:
        timeline = services.FullTimeline.from_dict(services._read_json_safe(timeline_path))
    except (TypeError, ValueError) as exc:
        return services._json_error(f"invalid full_timeline.json: {exc}", 400)
    if not audio_path.is_file():
        return services._json_error("final review WAV was not found", 404)
    if not _stable_wav(audio_path):
        return services._json_error("final review WAV is still being exported", 409)
    try:
        wav_duration = _wav_duration(audio_path)
        sequence = dict(payload.get("source_sequence") or {})
        expected_duration = float(sequence.get("durationSeconds") or timeline.total_duration_sec)
    except (TypeError, ValueError) as exc:
        return services._json_error(str(exc), 400)
    duration_tolerance = max(1.0, expected_duration * 0.002)
    if expected_duration <= 0 or abs(wav_duration - expected_duration) > duration_tolerance:
        return services._json_error(
            "stale_snapshot: final review WAV duration "
            f"{wav_duration:.3f}s does not match sequence duration {expected_duration:.3f}s",
            409,
        )
    if not services._reserve_final_review_project(project):
        return services._json_error(f"another mutating job is active for project {project}", 409)
    try:
        manifest = services.build_assembly_manifest(
            project=project,
            timeline=services.FullTimeline.from_dict(services._read_json_safe(timeline_path)),
            timeline_path=timeline_path,
            scenario_path=paths["normalized"],
            audio_path=audio_path,
            source_sequence=dict(payload.get("source_sequence") or {}),
            layout=dict(payload.get("assembly_layout") or {}),
        )
        services.attach_scenario_revision(manifest, paths["normalized"])
        services._atomic_write_json_file(paths["assembly_manifest"], manifest)
        payload["assembly_manifest_id"] = str(manifest["manifest_id"])
        job = services._create_final_review_job(project, audio_path, payload)
        worker = services.threading.Thread(
            target=services._run_final_review_job,
            kwargs={
                "job_id": job["id"],
                "project": project,
                "audio_path": audio_path,
                "language": str(payload.get("language") or "ru"),
                "backend": str(payload.get("backend") or "") or None,
                "model": str(payload.get("model") or "") or None,
                "source_sequence": dict(payload.get("source_sequence") or {}),
            },
            daemon=True,
        )
        worker.start()
    except ValueError as exc:
        services._release_final_review_project(project)
        return services._json_error(str(exc), 409)
    except Exception:
        services._release_final_review_project(project)
        raise
    return jsonify(
        {
            "ok": True,
            "result": {
                "project": project,
                "job_id": job["id"],
                "status": "queued",
                "status_url": f"/stage2/final-review/jobs/{job['id']}?project={project}",
            },
        }
    ), 202


def _status(services: ModuleType, job_id: str):
    job = services._get_final_review_job(job_id)
    if not job and has_request_context() and request.args.get("project"):
        try:
            project = services._resolve_project_name(request.args.get("project"))
        except ValueError as exc:
            return services._json_error(str(exc), 400)
        persisted = getattr(services, "_load_persisted_final_review_job", lambda _project: None)(
            project
        )
        if persisted and str(persisted.get("id") or "") == job_id:
            job = persisted
    if not job:
        return services._json_error("job not found", 404)
    return jsonify({"ok": True, "result": job})


def _apply(services: ModuleType, job_id: str):
    payload = request.get_json(silent=True) or {}
    job = services._get_final_review_job(job_id)
    if not job:
        try:
            project = services._resolve_project_name(payload.get("project"))
        except ValueError as exc:
            return services._json_error(str(exc), 400)
        persisted = getattr(services, "_load_persisted_final_review_job", lambda _project: None)(
            project
        )
        if persisted and str(persisted.get("id") or "") == job_id:
            job = persisted
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
    if str(report.get("job_id") or "") != str(job_id):
        return services._json_error("final review report does not belong to this job", 409)
    if str(report.get("project") or "") != project:
        return services._json_error("final review report does not belong to this project", 409)
    if str(report.get("assembly_manifest_id") or "") != str(job.get("assembly_manifest_id") or ""):
        return services._json_error("final review assembly snapshot changed", 409)
    report_sequence = dict(report.get("source_sequence") or {})
    job_sequence = dict(job.get("source_sequence") or {})
    if str(report_sequence.get("sequenceId") or "") != str(job_sequence.get("sequenceId") or ""):
        return services._json_error("final review sequence identity changed", 409)
    corrections = report.get("corrections") if isinstance(report.get("corrections"), list) else []
    iteration = services._record_final_review_iteration(project, job, corrections)
    return jsonify(
        {
            "ok": True,
            "result": {"project": project, "iteration": iteration, "corrections": corrections},
        }
    )


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
