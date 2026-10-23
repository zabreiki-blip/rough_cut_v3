"""Background recognition and alignment of a recorded voiceover.

A job binds immutable recording source IDs (with their content hashes) and a
snapshot of the scenario revision, runs the local ASR (shared slot, cancellable,
cached by file hash + ASR configuration) and stores the alignment as *proposals*
in the draft map ``work/recorded_voiceover.json``. Nothing is published into the
active voiceover here, and the paid cloud ASR backend is never used.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import threading
from typing import Any, Mapping

from api_service_contracts import VoiceoverJobServices
from job_runtime import asr_slot, get_job, release_project, reserve_project, trim_terminal_jobs
from stage2.sync_models import WordIndex
from stage2.sync_transcript import ingest_word_index
from stage2.whisper_process import WhisperCancelledError
from voiceover.alignment import PartIndex, align_voiceover
from voiceover.models import RecordingSource
from voiceover.storage import (
    asr_cache_path,
    load_recorded_voiceover,
    safe_project_file,
    save_recorded_voiceover,
    scan_recorded_voiceover,
)

VOICEOVER_JOBS_MAX = 100
VOICEOVER_JOBS_TTL_SECONDS = 24 * 60 * 60
VOICEOVER_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled", "interrupted"})
_CLOUD_BACKENDS = frozenset({"cloud-audio", "cloud", "openrouter"})


def local_asr_backend() -> str:
    """Configured backend if it is local, otherwise ``auto`` (local autodetection).

    A cloud backend in the environment must never be used for the automatic
    recognition of a recording: it is paid.
    """

    configured = os.getenv("SYNC_WHISPER_BACKEND", "auto").strip().lower() or "auto"
    return "auto" if configured in _CLOUD_BACKENDS else configured


def _asr_config(backend: str, model: str, language: str) -> dict[str, str]:
    return {"backend": backend, "model": model, "language": language}


def _blocks_snapshot_hash(normalization: Mapping[str, Any]) -> str:
    payload = [
        [
            block.get("uid"),
            block.get("order"),
            block.get("text_for_tts") or block.get("normalized_text"),
        ]
        for block in normalization.get("blocks") or []
        if isinstance(block, dict)
        and block.get("type") == "voiceover"
        and not block.get("absorbed_by_uid")
    ]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode("utf-8")).hexdigest()


def reserve_voiceover_project(services: VoiceoverJobServices, project: str) -> bool:
    runtime = services._VOICEOVER_RUNTIME
    return reserve_project(runtime.lock, runtime.active_projects, project)


def release_voiceover_project(services: VoiceoverJobServices, project: str) -> None:
    runtime = services._VOICEOVER_RUNTIME
    release_project(runtime.lock, runtime.active_projects, project)


def _trim_locked(services: VoiceoverJobServices) -> None:
    from datetime import datetime

    try:
        now = datetime.fromisoformat(services._utc_now_iso())
    except (TypeError, ValueError):
        return
    runtime = services._VOICEOVER_RUNTIME
    trim_terminal_jobs(
        runtime.jobs,
        terminal_statuses=VOICEOVER_TERMINAL_STATUSES,
        now=now,
        ttl_seconds=VOICEOVER_JOBS_TTL_SECONDS,
        max_jobs=VOICEOVER_JOBS_MAX,
        on_evict=lambda job_id: runtime.cancel_events.pop(job_id, None),
    )


def create_voiceover_job(
    services: VoiceoverJobServices,
    project: str,
    normalization: Mapping[str, Any],
) -> dict[str, Any]:
    """Queue a job over all registered recording parts (immutable IDs + revision snapshot)."""

    paths = services._project_paths(project)
    recordings = load_recorded_voiceover(paths)["recordings"]
    if not recordings:
        raise ValueError("no recorded voiceover to align")
    runtime = services._VOICEOVER_RUNTIME
    job = {
        "id": os.urandom(8).hex(),
        "project": project,
        "status": "queued",
        "message": "Queued",
        "created_at": services._utc_now_iso(),
        "started_at": "",
        "finished_at": "",
        "error": "",
        "recording_ids": [item["id"] for item in recordings],
        "recording_sha256": {item["id"]: item["sha256"] for item in recordings},
        "scenario_revision_id": str(normalization.get("scenario_revision_id") or ""),
        "normalization_version": normalization.get("revision", 1),
        "blocks_sha256": _blocks_snapshot_hash(normalization),
        "asr_backend": local_asr_backend(),
        "asr_waiting": False,
        "transcription_processed_sec": 0.0,
        "transcription_duration_sec": 0.0,
        "summary": {},
    }
    with runtime.lock:
        runtime.jobs[job["id"]] = job
        runtime.cancel_events[job["id"]] = threading.Event()
        services._atomic_write_json_file(paths["recorded_voiceover_job_status"], job)
        _trim_locked(services)
    return dict(job)


def get_voiceover_job(services: VoiceoverJobServices, job_id: str) -> dict[str, Any] | None:
    runtime = services._VOICEOVER_RUNTIME
    return get_job(runtime.lock, runtime.jobs, job_id)


def update_voiceover_job(
    services: VoiceoverJobServices, job_id: str, *, persist: bool = True, **updates: Any
) -> dict[str, Any] | None:
    runtime = services._VOICEOVER_RUNTIME
    with runtime.lock:
        job = runtime.jobs.get(job_id)
        if job is None:
            return None
        if updates.get("status") == "running" and job.get("status") == "cancelling":
            updates.pop("status")
            updates.pop("message", None)
        job.update(updates)
        snapshot = dict(job)
        if persist:
            services._atomic_write_json_file(
                services._project_paths(str(snapshot["project"]))["recorded_voiceover_job_status"],
                snapshot,
            )
        if snapshot["status"] in VOICEOVER_TERMINAL_STATUSES:
            _trim_locked(services)
        return snapshot


def cancel_voiceover_job(
    services: VoiceoverJobServices, job_id: str
) -> tuple[dict[str, Any] | None, str | None]:
    runtime = services._VOICEOVER_RUNTIME
    with runtime.lock:
        job = runtime.jobs.get(job_id)
        if job is None:
            return None, "job not found"
        if job.get("status") not in {"queued", "running", "cancelling"}:
            return dict(job), None
        event = runtime.cancel_events.get(job_id)
        if event is None:
            return None, "job worker is not active"
        event.set()
        job.update(status="cancelling", message="Cancellation requested")
        return dict(job), None


def _merge_ranges(
    existing: list[dict[str, Any]], proposals: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Proposals replace automatic ranges; confirmed/manual decisions on unchanged text stay."""

    def decided(item: Mapping[str, Any]) -> bool:
        return item.get("status") == "confirmed" or bool(item.get("manual_range"))

    keep = {item["block_uid"]: item for item in existing if decided(item)}
    merged: list[dict[str, Any]] = []
    used: set[str] = set()
    for proposal in proposals:
        uid = proposal["block_uid"]
        previous = keep.get(uid)
        if previous is not None and previous.get("text_sha256") == proposal["text_sha256"]:
            merged.append(previous)
        else:
            merged.append(proposal)
        used.add(uid)
    merged.extend(item for uid, item in keep.items() if uid not in used)
    return merged


def run_voiceover_job(
    services: VoiceoverJobServices,
    *,
    job_id: str,
    project: str,
    normalization: Mapping[str, Any],
    language: str = "ru",
) -> None:
    runtime = services._VOICEOVER_RUNTIME
    with runtime.lock:
        cancel_event = runtime.cancel_events.get(job_id)
        job = dict(runtime.jobs.get(job_id) or {})

    def check_stop() -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise WhisperCancelledError("Voiceover recognition cancelled")

    try:
        check_stop()
        paths = services._project_paths(project)
        backend = str(job.get("asr_backend") or local_asr_backend())
        model = os.getenv("SYNC_WHISPER_MODEL", "").strip()
        config = _asr_config(backend, model, language)
        data = load_recorded_voiceover(paths)
        by_id = {item["id"]: item for item in data["recordings"]}
        sources: list[RecordingSource] = []
        for recording_id in job["recording_ids"]:
            item = by_id.get(recording_id)
            if item is None or item["sha256"] != job["recording_sha256"].get(recording_id):
                raise ValueError(f"recording {recording_id} changed or was removed")
            sources.append(RecordingSource.from_dict(item))
        update_voiceover_job(
            services,
            job_id,
            status="running",
            message="Recognizing recording",
            started_at=services._utc_now_iso(),
        )

        parts: list[PartIndex] = []
        for source in sources:
            check_stop()
            audio = safe_project_file(Path(paths["root"]), source.asset)
            if audio is None:
                raise ValueError(f"recording file is missing: {source.asset}")
            cache = asr_cache_path(paths, source.sha256, config)
            payload: Any = None
            if cache.is_file():
                payload = services._read_json_safe(cache).get("result")
            if not isinstance(payload, dict):
                payload = _transcribe(
                    services,
                    job_id,
                    audio,
                    backend,
                    model or None,
                    language,
                    check_stop,
                    cancel_event,
                )
                check_stop()
                services._atomic_write_json_file(
                    cache, {"sha256": source.sha256, "config": config, "result": payload}
                )
            try:
                index = ingest_word_index(
                    payload,
                    source.id,
                    model=str(payload.get("model") or model),
                    language=str(payload.get("language") or language),
                )
            except ValueError:  # no timed words: every block stays "missing"
                index = WordIndex(source_id=source.id, words=[])
            parts.append(PartIndex(source.id, index, source.duration_sec))

        check_stop()
        update_voiceover_job(services, job_id, message="Aligning blocks to the script")
        proposals = [item.to_dict() for item in align_voiceover(normalization, parts)]
        summary = {
            "blocks": len(proposals),
            "needs_review": sum(item["status"] == "needs_review" for item in proposals),
            "missing": sum(item["status"] == "missing" for item in proposals),
            "with_duplicates": sum(bool(item["takes"]) for item in proposals),
        }

        # Cancellation and the draft-map commit share one boundary.
        with runtime.lock:
            check_stop()
            current = load_recorded_voiceover(paths)
            current_ids = {item["id"]: item["sha256"] for item in current["recordings"]}
            if any(
                current_ids.get(rid) != job["recording_sha256"][rid] for rid in job["recording_ids"]
            ):
                raise ValueError("recordings changed while the job was running")
            current["ranges"] = _merge_ranges(current["ranges"], proposals)
            current["scenario_revision_id"] = job.get("scenario_revision_id", "")
            current["normalization_version"] = job.get("normalization_version", 1)
            save_recorded_voiceover(paths, current)
        update_voiceover_job(
            services,
            job_id,
            status="completed",
            message="Proposals are ready for review",
            finished_at=services._utc_now_iso(),
            asr_waiting=False,
            summary=summary,
        )
    except WhisperCancelledError:
        update_voiceover_job(
            services,
            job_id,
            status="cancelled",
            message="Cancelled",
            finished_at=services._utc_now_iso(),
            asr_waiting=False,
        )
    except Exception as exc:  # job failures are reported through the job record
        update_voiceover_job(
            services,
            job_id,
            status="failed",
            message="Recognition failed",
            error=str(exc),
            finished_at=services._utc_now_iso(),
            asr_waiting=False,
        )
    finally:
        with runtime.lock:
            runtime.cancel_events.pop(job_id, None)
        release_voiceover_project(services, project)


def _transcribe(
    services: VoiceoverJobServices,
    job_id: str,
    audio: Path,
    backend: str,
    model: str | None,
    language: str,
    check_stop: Any,
    cancel_event: Any,
) -> dict[str, Any]:
    def progress(processed: float, duration: float, _words: int) -> None:
        update_voiceover_job(
            services,
            job_id,
            persist=False,
            transcription_processed_sec=processed,
            transcription_duration_sec=duration,
        )

    with asr_slot(
        services._ASR_SEMAPHORE,
        check_stop=check_stop,
        on_wait=lambda: update_voiceover_job(
            services, job_id, message="Waiting for an ASR slot", asr_waiting=True
        ),
        on_acquired=lambda: update_voiceover_job(
            services, job_id, message="Recognizing recording", asr_waiting=False
        ),
    ):
        # Local backend only: no cloud options, no budget callback.
        return services.transcribe_audio(
            audio,
            backend=backend,
            model=model,
            language=language,
            cancel_event=cancel_event,
            progress_callback=progress,
        )


def trigger_voiceover_autodetect(
    services: VoiceoverJobServices,
    project: str,
    normalization: Mapping[str, Any] | None = None,
    *,
    converter: Any = None,
    run_async: bool = True,
) -> dict[str, Any] | None:
    """Scan ``ЗАГРУЗИТЬ/Закадр/``; if a new recording appeared, queue a local alignment job.

    Returns the queued job, or ``None`` when nothing new was found, no confirmed
    scenario is available or another voiceover job already runs for the project
    (the recording stays registered and can be aligned on a later trigger).
    """

    paths = services._project_paths(project)
    if not scan_recorded_voiceover(paths, converter=converter):
        return None
    document = (
        normalization
        if normalization is not None
        else services._read_json_safe(paths["normalized"])
    )
    if not document or not document.get("blocks"):
        return None
    if not reserve_voiceover_project(services, project):
        return None
    try:
        job = create_voiceover_job(services, project, document)
    except Exception:
        release_voiceover_project(services, project)
        raise
    kwargs = {
        "services": services,
        "job_id": job["id"],
        "project": project,
        "normalization": document,
    }
    if run_async:
        threading.Thread(target=run_voiceover_job, kwargs=kwargs, daemon=True).start()
    else:
        run_voiceover_job(**kwargs)
    return job
