"""Routes for reviewing a rendered Rough Cut timeline.

Audio is accepted by path only when it is already inside the selected project.
CEP produces that WAV locally, which keeps this endpoint free of arbitrary
server-path access and avoids duplicating the large guide upload protocol.
"""

from __future__ import annotations

from pathlib import Path
from types import ModuleType
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
import math
import os
import time
import wave

from flask import Flask, has_request_context, jsonify, request

from api_final_review_jobs import (
    FinalReviewRequestConflict,
    canonical_decimal_string,
    find_final_review_iteration,
    final_review_deadline,
)
from cost_budget import CostGuardConfig
from stage2.cloud_asr import DEFAULT_CHUNK_MINUTES, DEFAULT_OVERLAP_SEC, plan_cloud_chunks
from stage2.whisper_runner import resolve_backend


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


def _money(value: object, name: str) -> Decimal:
    try:
        amount = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"{name} must be a finite non-negative decimal") from exc
    if not amount.is_finite() or amount < 0:
        raise ValueError(f"{name} must be a finite non-negative decimal")
    return amount


def _estimate_final_review(
    duration_sec: float,
    backend: str,
    requested_options: Mapping[str, object],
    environ: Mapping[str, str],
) -> tuple[dict, dict]:
    """Pure preflight: use the adapter's overlapping chunk plan and server budget."""
    if not math.isfinite(duration_sec) or duration_sec <= 0:
        raise ValueError("final review WAV duration must be positive and finite")
    result = {
        "duration": duration_sec,
        "duration_sec": duration_sec,
        "planned_chunks": 0,
        "estimated_cost_usd": "0",
        "cost_limit_usd": 0.0,
        "enforced": False,
    }
    if backend != "cloud-audio":
        try:
            result["cost_limit_usd"] = float(_money(
                environ.get("ROUGH_CUT_MAX_USD_PER_JOB", "").strip() or "0",
                "ROUGH_CUT_MAX_USD_PER_JOB",
            ))
        except ValueError:
            pass  # Cloud-only configuration must not prevent local work.
        return result, {}
    guard = CostGuardConfig.from_environ(environ)
    chunk_minutes = float(str(requested_options.get("chunk_minutes", DEFAULT_CHUNK_MINUTES)))
    raw_overlap = requested_options.get("overlap_sec")
    if raw_overlap is None:
        # Match the adapter's fallback for an invalid environment overlap.
        try:
            raw_overlap = float(environ.get("SYNC_CLOUD_ASR_OVERLAP_SEC", "").strip())
        except ValueError:
            raw_overlap = DEFAULT_OVERLAP_SEC
    overlap_sec = float(str(raw_overlap))
    chunks = plan_cloud_chunks(duration_sec, chunk_minutes, overlap_sec)
    seconds = sum((Decimal(str(end - start)) for start, end in chunks), Decimal("0"))
    estimate = seconds * guard.cloud_asr_usd_per_minute / Decimal("60")
    amount = _money(estimate, "estimated_cost_usd")
    limit = _money(guard.max_usd_per_job, "cost_limit_usd")
    if limit > 0 and amount <= 0:
        raise ValueError("enabled cost limit requires a positive cloud ASR estimate")
    result.update(
        planned_chunks=len(chunks), estimated_cost_usd=canonical_decimal_string(amount),
        cost_limit_usd=float(limit), enforced=limit > 0,
    )
    options = {
        "duration_sec": duration_sec,
        "chunk_minutes": chunk_minutes,
        "overlap_sec": overlap_sec,
        "max_cost_usd": str(guard.max_usd_per_job),
        "usd_per_minute_estimate": str(guard.cloud_asr_usd_per_minute),
    }
    if limit > 0 and estimate > limit:
        raise OverflowError("estimated cloud ASR cost exceeds the job cost limit")
    return result, options


def _preflight(
    audio_path: Path, payload: dict, *, duration_sec: float | None = None
) -> tuple[dict, dict, str]:
    backend = resolve_backend(str(payload.get("backend") or "") or None)
    requested_options = payload.get("cloud_options") or {}
    if not isinstance(requested_options, dict):
        raise ValueError("cloud_options must be an object")
    estimate, options = _estimate_final_review(
        _wav_duration(audio_path) if duration_sec is None else duration_sec,
        backend, requested_options, os.environ,
    )
    return estimate, options, backend


def _estimate(services: ModuleType):
    payload = request.get_json(silent=True) or {}
    try:
        project = services._resolve_project_name(payload.get("project"))
        paths = services._ensure_project(project)
        audio_path = _project_audio_path(services, paths, payload.get("audio_path"))
        if not audio_path.is_file():
            return services._json_error("final review WAV was not found", 404)
        estimate, _options, _backend = _preflight(audio_path, payload)
    except OverflowError as exc:
        return services._json_error(str(exc), 409)
    except (TypeError, ValueError, RuntimeError) as exc:
        return services._json_error(str(exc), 400)
    return jsonify({"ok": True, "result": estimate})


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
    try:
        estimate, cloud_options, backend = _preflight(audio_path, payload, duration_sec=wav_duration)
        if payload.get("confirmed_estimate_usd") is not None:
            confirmed = _money(payload["confirmed_estimate_usd"], "confirmed_estimate_usd")
            if confirmed != Decimal(estimate["estimated_cost_usd"]):
                return services._json_error("cloud ASR estimate changed; confirm the new estimate", 409)
        deadline = final_review_deadline()
    except OverflowError as exc:
        return services._json_error(str(exc), 409)
    except (TypeError, ValueError, RuntimeError) as exc:
        return services._json_error(str(exc), 400)
    if not services._reserve_final_review_project(project):
        return services._json_error(f"another mutating job is active for project {project}", 409)
    job = None
    try:
        # Acceptance starts the deadline before snapshot work or worker queueing.
        payload.update(estimate, deadline_monotonic=deadline)
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
                "backend": backend,
                "model": str(payload.get("model") or "") or None,
                "source_sequence": dict(payload.get("source_sequence") or {}),
                "cloud_options": cloud_options,
            },
            daemon=True,
        )
        worker.start()
    except Exception as exc:
        try:
            if job is not None:
                try:
                    services._update_final_review_job(
                        job["id"], status="failed", message="Final review failed to start",
                        error=str(exc), finished_at=services._utc_now_iso(),
                    )
                finally:
                    with services._FINAL_REVIEW_JOBS_LOCK:
                        services._FINAL_REVIEW_CANCEL_EVENTS.pop(job["id"], None)
        finally:
            services._release_final_review_project(project)
        if isinstance(exc, ValueError):
            return services._json_error(str(exc), 409)
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


def _latest(services: ModuleType):
    try:
        raw_project = request.args.get("project")
        if not str(raw_project or "").strip():
            raise ValueError("project is required")
        project = services._resolve_project_name(raw_project)
    except ValueError as exc:
        return services._json_error(str(exc), 400)
    job = services._get_latest_final_review_job(project)
    if job is None:
        return services._json_error("job not found", 404)
    return jsonify({"ok": True, "result": job})


def _cancel(services: ModuleType, job_id: str):
    job, error = services._cancel_final_review_job(job_id)
    if error == "job not found":
        payload = request.get_json(silent=True) or {}
        raw_project = request.args.get("project") or payload.get("project")
        if raw_project:
            try:
                project = services._resolve_project_name(raw_project)
            except ValueError as exc:
                return services._json_error(str(exc), 400)
            persisted = services._load_persisted_final_review_job(project)
            if persisted and str(persisted.get("id") or "") == job_id:
                return jsonify({"ok": True, "result": persisted}), 200
    if error:
        return services._json_error(error, 404 if error == "job not found" else 409)
    return jsonify({"ok": True, "result": job}), (
        202 if job and job.get("status") == "cancelling" else 200
    )


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


def _apply_result(project: str, iteration: dict):
    return jsonify({
        "ok": True,
        "result": {
            "project": project,
            "iteration": iteration,
            "corrections": iteration["corrections"],
        },
    })


def _apply(services: ModuleType, job_id: str):
    payload = request.get_json(silent=True) or {}
    request_id = payload.get("request_id", "")
    if not isinstance(request_id, str):
        return services._json_error("request_id must be a string", 400)
    request_id = request_id.strip()
    if len(request_id) > 128:
        return services._json_error("request_id must be at most 128 characters", 400)
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
    try:
        with services._project_transition_lock(project):
            paths = services._project_paths(project)
            if request_id:
                history = services._read_json_safe(paths["final_review_iterations"])
                existing = find_final_review_iteration(history, job_id, request_id)
                if existing is not None:
                    return _apply_result(project, existing)
            report = services._read_json_safe(paths["final_review_report"])
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
            iteration = services._record_final_review_iteration(project, job, corrections, request_id)
    except FinalReviewRequestConflict as exc:
        return services._json_error(str(exc), 409)
    return _apply_result(project, iteration)


def register_final_review_routes(app: Flask, services: ModuleType) -> None:
    @app.get("/stage2/final-review/latest")
    def final_review_latest():
        return _latest(services)

    @app.post("/stage2/final-review/estimate")
    def final_review_estimate():
        return _estimate(services)

    @app.post("/stage2/final-review/start")
    def final_review_start():
        return _start(services)

    @app.get("/stage2/final-review/jobs/<job_id>")
    def final_review_status(job_id: str):
        return _status(services, job_id)

    @app.post("/stage2/final-review/jobs/<job_id>/cancel")
    def final_review_cancel(job_id: str):
        return _cancel(services, job_id)

    @app.post("/stage2/final-review/jobs/<job_id>/apply")
    def final_review_apply(job_id: str):
        return _apply(services, job_id)
