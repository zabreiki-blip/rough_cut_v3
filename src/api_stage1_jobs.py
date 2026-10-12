from __future__ import annotations

from pathlib import Path
from typing import Any

from api_service_contracts import NormalizationJobServices, TtsJobServices
from stage4.revision_assets import merge_voiceover_manifest_item


def persist_tts_job(services: TtsJobServices, job: dict) -> None:
    """Best-effort atomic snapshot of the latest TTS job for one project."""
    try:
        project = services._normalize_project_name(str(job.get("project") or ""))
        path = services._project_paths(project)["tts_job_status"]
        services._atomic_write_json_file(path, job)
    except (OSError, TypeError, ValueError):
        services.app.logger.exception("Could not persist TTS job status")


def load_persisted_tts_job(services: TtsJobServices, project: str) -> dict | None:
    payload = services._read_json_safe(services._project_paths(project)["tts_job_status"])
    if not isinstance(payload, dict):
        return None
    if payload.get("project") != project or not str(payload.get("id") or "").strip():
        return None
    restored = dict(payload)
    if str(restored.get("status") or "").lower() in {
        "queued",
        "running",
        "cancelling",
    }:
        restored["status"] = "interrupted"
        restored["event"] = "interrupted"
        restored["message"] = "Interrupted; generated WAV files can be resumed"
        restored["current_id"] = ""
    return restored


def trim_tts_jobs_locked(services: TtsJobServices) -> None:
    now = services.datetime.now(services.UTC)
    terminal_ids = [
        job_id
        for job_id, job in services._TTS_JOBS.items()
        if job.get("status") in {"completed", "failed", "cancelled"}
    ]
    for job_id in terminal_ids:
        stamp = services._TTS_JOBS[job_id].get("finished_at") or ""
        try:
            expired = (
                now - services.datetime.fromisoformat(stamp)
            ).total_seconds() > services._TTS_JOBS_TTL_SECONDS
        except (TypeError, ValueError):
            expired = False
        if expired:
            services._TTS_JOBS.pop(job_id, None)
    terminal_ids = [
        job_id
        for job_id, job in services._TTS_JOBS.items()
        if job.get("status") in {"completed", "failed", "cancelled"}
    ]
    ordered_ids = sorted(
        terminal_ids,
        key=lambda job_id: (
            services._TTS_JOBS[job_id].get("finished_at")
            or services._TTS_JOBS[job_id].get("created_at")
            or "",
            job_id,
        ),
    )
    drop_count = max(0, len(services._TTS_JOBS) - services._TTS_JOBS_MAX)
    for job_id in ordered_ids[:drop_count]:
        services._TTS_JOBS.pop(job_id, None)


def reserve_tts_project(services: TtsJobServices, project: str) -> bool:
    return services.reserve_project(
        services._TTS_JOBS_LOCK,
        services._TTS_ACTIVE_PROJECTS,
        project,
    )


def release_tts_project(services: TtsJobServices, project: str) -> None:
    services.release_project(
        services._TTS_JOBS_LOCK,
        services._TTS_ACTIVE_PROJECTS,
        project,
    )


def create_tts_job(
    services: TtsJobServices,
    project: str,
    *,
    prepared_path: Path | None = None,
    speaker_dir: Path | None = None,
    provider: str | None = None,
    voice: str | None = None,
    gender: str | None = None,
    dry_run: bool = False,
    speed: float = 1.0,
    tts_model: str | None = None,
    tts_style: str = "",
) -> dict:
    job_id = services.uuid.uuid4().hex
    now = services._utc_now_iso()
    job = {
        "id": job_id,
        "project": project,
        "status": "queued",
        "event": "queued",
        "message": "Queued",
        "total": 0,
        "done": 0,
        "generated": 0,
        "skipped": 0,
        "attempts": 0,
        "retry_count": 0,
        "current_id": "",
        "last_error": "",
        "prepared_path": str(prepared_path) if prepared_path is not None else "",
        "speaker_dir": str(speaker_dir) if speaker_dir is not None else "",
        "provider": (provider or "").strip().lower(),
        "voice": (voice or "").strip(),
        "gender": (gender or "").strip().lower(),
        "dry_run": bool(dry_run),
        "speed": float(speed),
        "tts_model": (tts_model or "").strip(),
        "tts_style": (tts_style or "").strip(),
        "resolved_voice": "",
        "error": "",
        "created_at": now,
        "started_at": "",
        "finished_at": "",
        "result": None,
    }
    with services._TTS_JOBS_LOCK:
        services._TTS_JOBS[job_id] = job
        services._TTS_CANCEL_EVENTS[job_id] = services.threading.Event()
        services._trim_tts_jobs_locked()
        snapshot = dict(job)
    services._persist_tts_job(snapshot)
    return snapshot


def update_tts_job(
    services: TtsJobServices,
    job_id: str,
    *,
    persist: bool = True,
    increments: dict[str, int | float] | None = None,
    **updates: Any,
) -> dict | None:
    snapshot = services.update_job(
        services._TTS_JOBS_LOCK,
        services._TTS_JOBS,
        job_id,
        updates,
        increments=increments,
    )
    if snapshot is None:
        return None
    if persist:
        services._persist_tts_job(snapshot)
    return snapshot


def get_tts_job(services: TtsJobServices, job_id: str) -> dict | None:
    return services.get_job(
        services._TTS_JOBS_LOCK,
        services._TTS_JOBS,
        job_id,
    )


def tts_cancel_event(services: TtsJobServices, job_id: str):
    with services._TTS_JOBS_LOCK:
        return services._TTS_CANCEL_EVENTS.get(job_id)


def run_tts_job(
    services: TtsJobServices,
    *,
    job_id: str,
    project: str,
    prepared_path: Path,
    speaker_dir: Path,
    provider: str | None,
    voice_name: str | None,
    gender: str | None,
    dry_run: bool,
    speed: float = 1.0,
    tts_model: str | None = None,
    tts_style: str = "",
) -> None:
    cancel_event = services._tts_cancel_event(job_id)
    project_paths = services._project_paths(project)
    prepared_payload = services._read_json_safe(prepared_path)
    tts_revision_id = str(prepared_payload.get("scenario_revision_id") or "")
    block_uid_by_id = {
        str(item.get("id") or ""): str(item.get("block_uid") or "")
        for item in prepared_payload.get("items", [])
        if isinstance(item, dict) and item.get("id")
    }
    progress_persist_lock = services.threading.Lock()
    last_progress_persist_at = 0.0

    def should_persist_progress(event: str) -> bool:
        nonlocal last_progress_persist_at
        now = services.time.monotonic()
        force = event in {"item_retrying", "item_failed", "cancelled"}
        with progress_persist_lock:
            if not force and now - last_progress_persist_at < 1.0:
                return False
            last_progress_persist_at = now
            return True

    acquired = False
    while not acquired:
        if cancel_event is not None and cancel_event.is_set():
            services._update_tts_job(
                job_id,
                status="cancelled",
                event="cancelled",
                message="Cancelled before synthesis started",
                current_id="",
                finished_at=services._utc_now_iso(),
            )
            services._release_tts_project(project)
            with services._TTS_JOBS_LOCK:
                services._TTS_CANCEL_EVENTS.pop(job_id, None)
            return
        acquired = services._TTS_SEMAPHORE.acquire(timeout=0.25)

    def progress_callback(progress: dict) -> None:
        if cancel_event is not None and cancel_event.is_set():
            return
        event = str(progress.get("event", "")).strip() or "progress"
        total = int(progress.get("total") or 0)
        done = int(progress.get("completed") or 0)
        generated = int(progress.get("generated") or 0)
        skipped = int(progress.get("skipped") or 0)
        attempts = int(progress.get("attempts") or 0)
        current_id = str(progress.get("id") or "").strip()
        error_text = str(progress.get("error") or "").strip()
        msg = (
            f"{event}: {done}/{total} ({current_id})" if current_id else f"{event}: {done}/{total}"
        )
        if event in {"item_generated", "item_skipped"} and current_id:
            # Merge this block into the active manifest as soon as its
            # sidecar is on disk, rather than waiting for the whole batch to
            # finish. A batch that is later cancelled or fails partway
            # through must not leave already-resynthesized blocks recorded
            # with a stale fingerprint (see merge_voiceover_manifest_item).
            meta = services._read_json_safe(speaker_dir / f"{current_id}.wav.meta")
            fingerprint = str(meta.get("fingerprint") or "") if isinstance(meta, dict) else ""
            if fingerprint:
                merge_voiceover_manifest_item(
                    project_paths,
                    revision_id=tts_revision_id,
                    block_id=current_id,
                    block_uid=block_uid_by_id.get(current_id) or current_id,
                    fingerprint=fingerprint,
                    speaker_dir=speaker_dir,
                )
        existing = services._get_tts_job(job_id) or {}
        services._update_tts_job(
            job_id,
            persist=should_persist_progress(event),
            status="running",
            event=event,
            message=msg,
            total=total,
            done=done,
            generated=generated,
            skipped=skipped,
            current_id=current_id,
            attempts=attempts,
            last_error=error_text or str(existing.get("last_error") or ""),
            increments={"retry_count": 1} if event == "item_retrying" else None,
        )

    try:
        services._update_tts_job(
            job_id,
            status="running",
            event="started",
            message="Started",
            started_at=services._utc_now_iso(),
            provider=(provider or "").strip().lower(),
            dry_run=bool(dry_run),
            tts_model=(tts_model or "").strip(),
            tts_style=(tts_style or "").strip(),
            attempts=0,
            retry_count=0,
            last_error="",
        )
        result = services.synthesize_tts(
            prepared_path=prepared_path,
            speaker_dir=speaker_dir,
            provider=provider,
            voice_name=voice_name,
            gender=gender,
            dry_run=dry_run,
            progress_callback=progress_callback,
            cancel_check=(cancel_event.is_set if cancel_event is not None else None),
            speed=speed,
            usage_callback=services._make_openrouter_usage_callback(
                project,
                stage="voiceover",
                operation="tts",
                job_id=job_id,
                revision_id=tts_revision_id,
            ),
            tts_model=tts_model,
            tts_style=tts_style,
        )
        services._save_voiceover_revision_assets(project, result)
        total = int(result.get("items_total") or 0)
        generated = int(result.get("files_count") or 0)
        skipped = int(result.get("skipped_count") or 0)
        services._update_tts_job(
            job_id,
            status="completed",
            event="completed",
            message=f"Completed: {generated} generated, {skipped} skipped",
            total=total,
            done=total,
            generated=generated,
            skipped=skipped,
            attempts=0,
            current_id="",
            error="",
            last_error="",
            finished_at=services._utc_now_iso(),
            provider=str(result.get("provider", "")),
            tts_model=str(result.get("model", "")),
            tts_style=str(result.get("style", "")),
            resolved_voice=str(result.get("voice", "")),
            gender=str(result.get("gender", "")),
            dry_run=bool(result.get("dry_run", False)),
            result=result,
        )
    except services.TtsCancelledError:
        services._update_tts_job(
            job_id,
            status="cancelled",
            event="cancelled",
            message="Cancelled; generated WAV files were kept",
            current_id="",
            finished_at=services._utc_now_iso(),
        )
    except Exception as exc:
        services.app.logger.exception(
            "TTS job failed project=%s job=%s",
            project,
            job_id,
        )
        services._update_tts_job(
            job_id,
            status="failed",
            event="failed",
            message="Failed",
            error=str(exc),
            last_error=str(exc),
            finished_at=services._utc_now_iso(),
        )
    finally:
        if acquired:
            services._TTS_SEMAPHORE.release()
        services._release_tts_project(project)
        with services._TTS_JOBS_LOCK:
            services._TTS_CANCEL_EVENTS.pop(job_id, None)


def persist_normalization_job(services: NormalizationJobServices, job: dict) -> None:
    try:
        project = services._normalize_project_name(str(job.get("project") or ""))
        services._atomic_write_json_file(
            services._project_paths(project)["normalization_job_status"],
            job,
        )
    except (OSError, TypeError, ValueError):
        services.app.logger.exception("Could not persist normalization job status")


def create_normalization_job(
    services: NormalizationJobServices,
    project: str,
    *,
    input_path: Path,
    use_llm: bool,
    hint: str,
    llm_preset: str,
) -> dict:
    now = services._utc_now_iso()
    job = {
        "id": services.uuid.uuid4().hex,
        "project": project,
        "status": "queued",
        "message": "Queued",
        "input_path": str(input_path),
        "use_llm": bool(use_llm),
        "hint": hint,
        "llm_preset": llm_preset,
        "created_at": now,
        "started_at": "",
        "finished_at": "",
        "error": "",
        "result": None,
    }
    with services._NORMALIZATION_JOBS_LOCK:
        services._NORMALIZATION_JOBS[job["id"]] = job
        terminal = sorted(
            (
                (job_id, item)
                for job_id, item in services._NORMALIZATION_JOBS.items()
                if item.get("status") in {"completed", "failed", "cancelled"}
            ),
            key=lambda pair: pair[1].get("finished_at") or pair[1].get("created_at") or "",
        )
        drop_count = max(
            0,
            len(services._NORMALIZATION_JOBS) - services._NORMALIZATION_JOBS_MAX,
        )
        for old_id, _old_job in terminal[:drop_count]:
            services._NORMALIZATION_JOBS.pop(old_id, None)
        snapshot = dict(job)
    services._persist_normalization_job(snapshot)
    return snapshot


def update_normalization_job(
    services: NormalizationJobServices,
    job_id: str,
    **updates: Any,
) -> dict | None:
    snapshot = services.update_job(
        services._NORMALIZATION_JOBS_LOCK,
        services._NORMALIZATION_JOBS,
        job_id,
        updates,
    )
    if snapshot is None:
        return None
    services._persist_normalization_job(snapshot)
    return snapshot


def get_normalization_job(services: NormalizationJobServices, job_id: str) -> dict | None:
    return services.get_job(
        services._NORMALIZATION_JOBS_LOCK,
        services._NORMALIZATION_JOBS,
        job_id,
    )


def reserve_normalization_project(services: NormalizationJobServices, project: str) -> bool:
    return services.reserve_project(
        services._NORMALIZATION_JOBS_LOCK,
        services._NORMALIZATION_ACTIVE_PROJECTS,
        project,
    )


def release_normalization_project(services: NormalizationJobServices, project: str) -> None:
    services.release_project(
        services._NORMALIZATION_JOBS_LOCK,
        services._NORMALIZATION_ACTIVE_PROJECTS,
        project,
    )


def load_persisted_normalization_job(
    services: NormalizationJobServices, project: str
) -> dict | None:
    payload = services._read_json_safe(services._project_paths(project)["normalization_job_status"])
    if not isinstance(payload, dict) or payload.get("project") != project:
        return None
    restored = dict(payload)
    if restored.get("status") in {"queued", "running"}:
        restored.update(
            {
                "status": "interrupted",
                "message": "Interrupted; normalization can be restarted",
                "finished_at": services._utc_now_iso(),
            }
        )
    return restored


def run_normalization_job(
    services: NormalizationJobServices,
    *,
    job_id: str,
    project: str,
    input_path: Path,
    output_path: Path,
    use_llm: bool,
    hint: str,
    llm_preset: str,
) -> None:
    # Cross-project cap: wait in "queued" state for a free slot (same queueing
    # behaviour as the TTS slot) before doing any LLM/CPU-heavy work.
    slot_acquired = False
    try:
        slot_acquired = services.acquire_slot(services._HEAVY_JOB_SEMAPHORE)
        services._update_normalization_job(
            job_id,
            status="running",
            message="Normalizing scenario",
            started_at=services._utc_now_iso(),
        )
        result = services.generate_normalization(
            input_path,
            output_path,
            memory_path=services.NORMALIZATION_MEMORY_PATH,
            use_llm=use_llm,
            hint=hint,
            llm_preset=llm_preset,
            usage_callback=services._make_openrouter_usage_callback(
                project,
                stage="normalization",
                operation="normalize",
                job_id=job_id,
            ),
        )
        services._update_normalization_job(
            job_id,
            status="completed",
            message="Completed",
            finished_at=services._utc_now_iso(),
            result=result,
        )
    except Exception as exc:
        services.app.logger.exception(
            "Normalization job failed project=%s job=%s",
            project,
            job_id,
        )
        services._update_normalization_job(
            job_id,
            status="failed",
            message="Failed",
            error=str(exc),
            finished_at=services._utc_now_iso(),
        )
    finally:
        if slot_acquired:
            services._HEAVY_JOB_SEMAPHORE.release()
        services._release_normalization_project(project)
