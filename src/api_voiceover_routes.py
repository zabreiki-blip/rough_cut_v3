"""Routes of the recorded voiceover (one shared recording, ranges per block).

All routes are project-scoped under ``/stage1/recorded-voiceover``. Mutating JSON
operations carry ``expected_revision_id`` (the active scenario revision); edits of
the draft map and the publication also carry ``expected_generation`` and are
rejected with 409 when either is stale. Recognition and publication are mutually
exclusive with TTS, revision activation and each other (project registries).

The draft map holds proposals and user decisions; ``POST /apply`` publishes them per
block into the published map (see ``voiceover.publish``). Nothing here creates
``zkN`` aliases or changes the TTS manifest.
"""

from __future__ import annotations

from collections.abc import Callable
import os
from pathlib import Path
import threading
from types import ModuleType
from typing import Any, Mapping
import unicodedata
import uuid

from flask import Flask, jsonify, request, send_file

from api_voiceover_jobs import (
    VOICEOVER_TERMINAL_STATUSES,
    create_voiceover_job,
    release_voiceover_project,
    reserve_voiceover_project,
    run_voiceover_job,
)
from voiceover.alignment import _active_voiceover_blocks
from voiceover.models import VoiceoverRange
from voiceover.publish import (
    build_published_map,
    decide_block,
    load_published,
    publish_recorded_voiceover,
    recover_publication,
    tts_available_uids,
)
from voiceover.models import RecordingSource
from voiceover.storage import (
    PreparedRecording,
    StaleMapError,
    commit_prepared_recording,
    discard_prepared_recording,
    load_recorded_voiceover,
    prepare_recording_file,
    prepare_voiceover_scan,
    safe_project_file,
    save_recorded_voiceover,
)

ALLOWED_SOURCE_SUFFIXES = (".wav", ".mp3")
_ACTIVE_JOB_STATUSES = {"queued", "running", "cancelling"}


class _Reject(Exception):
    """Carries a ready JSON error response out of nested helpers."""

    def __init__(self, response: Any) -> None:
        super().__init__("rejected")
        self.response = response


def _start_worker(target: Callable[..., None], kwargs: dict[str, Any]) -> None:
    threading.Thread(target=target, kwargs=kwargs, daemon=True).start()


def _fail(services: ModuleType, message: str, status: int) -> _Reject:
    return _Reject(services._json_error(message, status))


def _payload(services: ModuleType) -> dict[str, Any]:
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise _fail(services, "JSON object body is required", 400)
    return data


def _project_of(services: ModuleType, raw: object) -> str:
    try:
        return services._resolve_project_name(None if raw is None else str(raw))
    except ValueError as exc:
        raise _fail(services, str(exc), 400) from exc


def _confirmed_document(services: ModuleType, project: str) -> dict[str, Any]:
    document, _reason = services._confirmed_normalization(project)
    if document is None:
        raise _Reject(services._normalization_required(project))
    return document


def _check_revision(services: ModuleType, payload: Mapping[str, Any], document: Mapping) -> None:
    expected = payload.get("expected_revision_id")
    if not isinstance(expected, str):
        raise _fail(services, "expected_revision_id is required", 400)
    if expected != str(document.get("scenario_revision_id") or ""):
        raise _fail(services, "scenario revision changed; reload the state", 409)


def _check_generation(
    services: ModuleType, payload: Mapping[str, Any], data: Mapping[str, Any]
) -> None:
    expected = payload.get("expected_generation")
    if isinstance(expected, bool) or not isinstance(expected, int):
        raise _fail(services, "expected_generation is required", 400)
    if expected != data["generation"]:
        raise _fail(services, "markup changed since it was loaded (generation mismatch)", 409)


def _check_draft_revision(services: ModuleType, data: Mapping, document: Mapping) -> None:
    draft_revision = str(data.get("scenario_revision_id") or "")
    if draft_revision and draft_revision != str(document.get("scenario_revision_id") or ""):
        raise _fail(
            services, "markup belongs to another scenario revision; run recognition again", 409
        )


def _recognition_running(services: ModuleType, project: str) -> bool:
    runtime = services._VOICEOVER_RUNTIME
    with runtime.lock:
        return project in runtime.active_projects


def _blocks_by_uid(document: Mapping[str, Any]) -> dict[str, Any]:
    return {block.uid: block for block in _active_voiceover_blocks(document)}


def _latest_job(services: ModuleType, project: str, paths: Mapping[str, Path]) -> dict | None:
    runtime = services._VOICEOVER_RUNTIME
    with runtime.lock:
        candidates = [dict(item) for item in runtime.jobs.values() if item["project"] == project]
    if candidates:
        return max(candidates, key=lambda item: str(item.get("created_at") or ""))
    return _persisted_job(services, project, paths)


def _persisted_job(
    services: ModuleType, project: str, paths: Mapping[str, Path]
) -> dict[str, Any] | None:
    stored = services._read_json_safe(paths["recorded_voiceover_job_status"])
    if not stored or stored.get("project") != project:
        return None
    runtime = services._VOICEOVER_RUNTIME
    with runtime.lock:
        alive = stored.get("id") in runtime.jobs
    if not alive and stored.get("status") in _ACTIVE_JOB_STATUSES:
        # The worker died with the previous server process: report it, do not hang.
        stored = {
            **stored,
            "status": "interrupted",
            "message": "Recognition was interrupted by a restart; start it again",
            "finished_at": services._utc_now_iso(),
        }
        services._atomic_write_json_file(paths["recorded_voiceover_job_status"], stored)
    return stored


def _block_view(block: Any, item: Mapping | None, tts: set[str]) -> dict[str, Any]:
    source, origin, reason = decide_block(item, block.sha256, block.uid in tts)
    return {
        "block_uid": block.uid,
        "display_text": block.display_text,
        "text_sha256": block.sha256,
        "range": dict(item) if item is not None else None,
        "text_changed": bool(item is not None and item.get("text_sha256") != block.sha256),
        "tts_available": block.uid in tts,
        "publish_preview": {"voice_source": source, "origin": origin, "reason": reason},
    }


def _state(
    services: ModuleType, project: str, paths: Mapping[str, Path], document: Mapping | None
) -> dict[str, Any]:
    # Read-only: finishing an interrupted publication is done under reservation + lock.
    data = load_recorded_voiceover(paths)
    published = load_published(paths)
    tts = tts_available_uids(paths)
    ranges = {item["block_uid"]: item for item in data["ranges"]}
    blocks = (
        [
            _block_view(block, ranges.get(block.uid), tts)
            for block in _active_voiceover_blocks(document)
        ]
        if document
        else []
    )
    active_revision = str((document or {}).get("scenario_revision_id") or "")
    draft_revision = str(data.get("scenario_revision_id") or "")
    return {
        "project": project,
        "scenario_confirmed": document is not None,
        "scenario_revision_id": active_revision,
        "generation": data["generation"],
        "draft_revision_id": draft_revision,
        "draft_stale": bool(draft_revision and draft_revision != active_revision),
        "recordings": data["recordings"],
        "blocks": blocks,
        "published": {
            "pending_recovery": Path(paths["recorded_voiceover_publish_intent"]).is_file(),
            "exists": bool(published),
            "published_sha256": published.get("published_sha256", ""),
            "published_generation": published.get("published_generation", 0),
            "draft_generation": published.get("draft_generation"),
            "scenario_revision_id": published.get("scenario_revision_id", ""),
            "summary": published.get("summary", {}),
            "is_current": bool(published)
            and published.get("draft_generation") == data["generation"]
            and published.get("scenario_revision_id") == active_revision,
        },
        "job": _latest_job(services, project, paths),
    }


def _source_response(services: ModuleType, project: str, paths: Mapping[str, Path], recording):
    document, _reason = services._confirmed_normalization(project)
    return jsonify(
        {
            "ok": True,
            "result": {
                "recording": recording.to_dict(),
                "state": _state(services, project, paths, document),
            },
        }
    )


def _clean_upload_name(raw: str, suffix: str) -> str:
    name = unicodedata.normalize("NFC", Path(raw or "").name)
    name = "".join(ch for ch in name if ch.isprintable() and ch not in '<>:"/\\|?*').strip(" .")
    stem = Path(name).stem or "recording"
    return f"{stem}{suffix}"


def _source_upload(services: ModuleType):
    form = request.form
    project = _project_of(services, form.get("project") or request.args.get("project"))
    paths = services._ensure_project(project)
    inbox_id = (form.get("inbox_file_id") or request.args.get("inbox_file_id") or "").strip()
    uploaded = request.files.get("audio") or request.files.get("file")
    # Copying, hashing and ffmpeg run before the project lock; it is held only to register.
    if inbox_id:
        return _register_inbox_file(services, project, paths, inbox_id)
    if uploaded is None or not uploaded.filename:
        raise _fail(services, "multipart field 'audio' or 'inbox_file_id' is required", 400)
    suffix = Path(uploaded.filename).suffix.lower()
    if suffix not in ALLOWED_SOURCE_SUFFIXES:
        raise _fail(services, "recording must be a WAV or MP3 file", 415)
    max_bytes = int(
        services.app.config.get("SYNC_AUDIO_MAX_BYTES")
        or os.getenv("SYNC_AUDIO_MAX_BYTES")
        or 4 * 1024 * 1024 * 1024
    )
    if request.content_length is not None and request.content_length > max_bytes:
        raise _fail(services, "audio upload is too large", 413)
    folder = Path(paths["inbox_voiceover"])
    folder.mkdir(parents=True, exist_ok=True)
    services._cleanup_stale_uploads(folder)
    final = folder / _clean_upload_name(uploaded.filename, suffix)
    if final.exists():
        final = final.with_name(f"{final.stem}-{uuid.uuid4().hex[:6]}{suffix}")
    temporary = final.with_name(f".{final.name}.{uuid.uuid4().hex}.upload")
    try:
        services._copy_stream_limited(uploaded.stream, temporary, max_bytes)
        os.replace(temporary, final)
    except services.AudioUploadTooLargeError as exc:
        temporary.unlink(missing_ok=True)
        raise _fail(services, str(exc), 413) from exc
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return _register_file(services, project, paths, final, uploaded_copy=True)


def _register_inbox_file(services: ModuleType, project: str, paths, inbox_id: str):
    for item in services._discover_project_inbox(project)["voiceover_files"]:
        if inbox_id in (item.get("fingerprint"), item.get("relative_path")):
            return _register_file(
                services, project, paths, Path(paths["root"]) / item["relative_path"]
            )
    raise _fail(services, "inbox file not found", 404)


def _register_file(
    services: ModuleType, project: str, paths, file: Path, *, uploaded_copy: bool = False
):
    """Prepare outside the lock, then register under it (not while a job is active).

    ``uploaded_copy`` is a file that this request just wrote into the drop folder: it
    is removed when the request fails or the recording is already registered.
    """

    prepared: PreparedRecording | RecordingSource | None = None
    try:
        try:
            prepared = prepare_recording_file(paths, file)
        except Exception as exc:  # unreadable/non-PCM/ffmpeg failure/wave.Error
            raise _fail(services, f"invalid recording: {exc}", 415) from exc
        with services._project_transition_lock(project):
            if _recognition_running(services, project):
                raise _fail(services, "recognition or publication is running; try again later", 409)
            if isinstance(prepared, RecordingSource):
                recording = prepared
                if uploaded_copy:  # the same content is already registered: no duplicate
                    file.unlink(missing_ok=True)
            else:
                try:
                    recording = commit_prepared_recording(paths, prepared)
                except Exception as exc:
                    raise _fail(services, f"invalid recording: {exc}", 415) from exc
            return _source_response(services, project, paths, recording)
    except BaseException:
        if uploaded_copy:
            file.unlink(missing_ok=True)
        raise
    finally:
        if isinstance(prepared, PreparedRecording):
            discard_prepared_recording(prepared)  # no-op once committed


def _jobs_start(services: ModuleType):
    payload = _payload(services)
    project = _project_of(services, payload.get("project"))
    paths = services._ensure_project(project)
    with services._project_transition_lock(project):
        document = _confirmed_document(services, project)
        _check_revision(services, payload, document)
        if not load_recorded_voiceover(paths)["recordings"]:
            raise _fail(services, "no recorded voiceover to align", 400)
        if not reserve_voiceover_project(services, project):
            raise _fail(services, f"another project task is active for {project}", 409)
        try:
            recover_publication(paths)
            job = create_voiceover_job(services, project, document)
            _start_worker(
                run_voiceover_job,
                {
                    "services": services,
                    "job_id": job["id"],
                    "project": project,
                    "normalization": document,
                },
            )
        except Exception:
            release_voiceover_project(services, project)
            raise
    return jsonify(
        {
            "ok": True,
            "result": {
                "project": project,
                "job_id": job["id"],
                "status": job["status"],
                "status_url": f"/stage1/recorded-voiceover/jobs/{job['id']}?project={project}",
            },
        }
    ), 202


def _scan(services: ModuleType):
    payload = _payload(services)
    project = _project_of(services, payload.get("project"))
    paths = services._ensure_project(project)
    prepared = prepare_voiceover_scan(paths)  # hashing/copy/ffmpeg outside the lock
    try:
        with services._project_transition_lock(project):
            document, _reason = services._confirmed_normalization(project)
            if "expected_revision_id" in payload and document is not None:
                _check_revision(services, payload, document)
            outcome = services._autodetect_voiceover(project, document, prepared)
            state = _state(services, project, paths, document)
    finally:
        for item in prepared:
            discard_prepared_recording(item)  # no-op for the committed ones
    message = ""
    if outcome.pending:
        message = "Идёт распознавание: новая запись будет обработана после его завершения."
    elif outcome.registered and not outcome.scenario_confirmed:
        message = "Запись зарегистрирована. Подтвердите сценарий, чтобы запустить распознавание."
    return jsonify(
        {
            "ok": True,
            "result": {
                "job": outcome.job,
                "registered": len(outcome.registered),
                "pending": outcome.pending,
                "scenario_confirmed": outcome.scenario_confirmed,
                "message": message,
                "state": state,
            },
        }
    )


def _job_status(services: ModuleType, job_id: str):
    project = _project_of(services, request.args.get("project"))
    paths = services._ensure_project(project)
    job = services._get_voiceover_job(job_id)
    if job is None:
        stored = _persisted_job(services, project, paths)
        job = stored if stored and stored.get("id") == job_id else None
    if job is None or job.get("project") != project:
        raise _fail(services, "job not found", 404)
    return jsonify({"ok": True, "result": job})


def _job_cancel(services: ModuleType, job_id: str):
    payload = request.get_json(silent=True)
    payload = payload if isinstance(payload, dict) else {}
    project = _project_of(services, payload.get("project") or request.args.get("project"))
    services._ensure_project(project)
    current = services._get_voiceover_job(job_id)
    if current is None or current.get("project") != project:
        raise _fail(services, "job not found", 404)
    job, error = services._cancel_voiceover_job(job_id)
    if job is None:
        raise _fail(services, error or "job not found", 409)
    if job["status"] in VOICEOVER_TERMINAL_STATUSES:
        raise _fail(services, "job is not active", 409)
    return jsonify({"ok": True, "result": job}), 202


def _prepare_edit(services: ModuleType, payload: Mapping[str, Any]):
    project = _project_of(services, payload.get("project"))
    paths = services._ensure_project(project)
    document = _confirmed_document(services, project)
    _check_revision(services, payload, document)
    return project, paths, document


def _locked_edit(services: ModuleType, project: str, paths, document, payload):
    """Common guards for a draft edit; must run inside the project transition lock."""

    if _recognition_running(services, project):
        raise _fail(services, "recognition or publication is running; try again later", 409)
    recover_publication(paths)  # nothing is publishing here: no reservation is active
    data = load_recorded_voiceover(paths)
    _check_generation(services, payload, data)
    _check_draft_revision(services, data, document)
    return data


def _number(services: ModuleType, payload: Mapping[str, Any], key: str) -> float | None:
    if key not in payload or payload[key] is None:
        return None
    value = payload[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _fail(services, f"{key} must be a number", 400)
    return float(value)


def _overlap_conflict(data: Mapping[str, Any], candidate: Mapping[str, Any]) -> str:
    if candidate["status"] != "confirmed":
        return ""
    for other in data["ranges"]:
        if (
            other["block_uid"] != candidate["block_uid"]
            and other["status"] == "confirmed"
            and other["recording_id"] == candidate["recording_id"]
            and candidate["source_in_sec"] < other["source_out_sec"]
            and other["source_in_sec"] < candidate["source_out_sec"]
        ):
            return other["block_uid"]
    return ""


def _commit_range(
    services: ModuleType, paths, data: dict[str, Any], document: Mapping, candidate: dict
) -> dict[str, Any]:
    recording = next((r for r in data["recordings"] if r["id"] == candidate["recording_id"]), None)
    try:
        parsed = VoiceoverRange.from_dict(
            candidate, duration_sec=recording["duration_sec"] if recording else 1.0
        )
        if candidate["status"] != "missing" and recording is None:
            raise ValueError("unknown recording")
    except (ValueError, TypeError) as exc:
        raise _fail(services, f"invalid range: {exc}", 400) from exc
    clash = _overlap_conflict(data, parsed.to_dict())
    if clash:
        raise _fail(services, f"range overlaps confirmed block {clash}", 409)
    result = parsed.to_dict()
    ranges = [item for item in data["ranges"] if item["block_uid"] != result["block_uid"]]
    position = next(
        (i for i, item in enumerate(data["ranges"]) if item["block_uid"] == result["block_uid"]),
        len(ranges),
    )
    ranges.insert(position, result)
    data["ranges"] = ranges
    if not data.get("scenario_revision_id"):
        data["scenario_revision_id"] = str(document.get("scenario_revision_id") or "")
    try:
        saved = save_recorded_voiceover(paths, data, expected_generation=data["generation"])
    except StaleMapError as exc:
        raise _fail(
            services, "markup changed since it was loaded (generation mismatch)", 409
        ) from exc
    return {"range": result, "generation": saved["generation"]}


def _patch_range(services: ModuleType, block_uid: str):
    payload = _payload(services)
    project, paths, document = _prepare_edit(services, payload)
    with services._project_transition_lock(project):
        data = _locked_edit(services, project, paths, document, payload)
        block = _blocks_by_uid(document).get(block_uid)
        if block is None:
            raise _fail(services, "block not found among active voiceover blocks", 404)
        existing = next((i for i in data["ranges"] if i["block_uid"] == block_uid), None)
        fresh = existing is not None and existing["text_sha256"] == block.sha256
        status = payload.get("status")
        if status is not None and status not in ("confirmed", "needs_review"):
            raise _fail(services, "status must be 'confirmed' or 'needs_review'", 400)
        bounds = {
            key: _number(services, payload, key)
            for key in ("source_in_sec", "source_out_sec", "speech_in_sec", "speech_out_sec")
        }
        has_bounds = any(v is not None for v in bounds.values())
        has_take = "selected_take" in payload
        if has_bounds and has_take:
            raise _fail(services, "pass either boundaries or selected_take, not both", 400)
        if not (has_bounds or has_take or status):
            raise _fail(services, "nothing to change", 400)

        if has_bounds:
            if bounds["source_in_sec"] is None or bounds["source_out_sec"] is None:
                raise _fail(services, "source_in_sec and source_out_sec are required", 400)
            recording_id = str(
                payload.get("recording_id")
                or (existing or {}).get("recording_id")
                or (data["recordings"][0]["id"] if len(data["recordings"]) == 1 else "")
            )
            if not recording_id:
                raise _fail(services, "recording_id is required", 400)
            base = dict(existing) if fresh else {"takes": [], "confidence": 0.0}
            if base.get("status") == "missing":
                base["takes"] = []
            candidate = {
                **base,
                "block_uid": block_uid,
                "text_sha256": block.sha256,
                "display_text": block.display_text,
                "recording_id": recording_id,
                "source_in_sec": bounds["source_in_sec"],
                "source_out_sec": bounds["source_out_sec"],
                "speech_in_sec": bounds["speech_in_sec"],
                "speech_out_sec": bounds["speech_out_sec"],
                "manual_range": True,
                "selected_take": None,
                "origin": "manual",
                "voice_source": "recorded",
                "status": status or (existing or {}).get("status") or "needs_review",
            }
            if not fresh or candidate["status"] == "missing":
                candidate["status"] = status or "needs_review"
            if bounds["speech_in_sec"] is None and bounds["speech_out_sec"] is None:
                old = (
                    base
                    if base.get("recording_id") == recording_id and base.get("status") != "missing"
                    else {}
                )
                inside = (
                    old.get("speech_in_sec") is not None
                    and candidate["source_in_sec"] <= old["speech_in_sec"]
                    and old["speech_out_sec"] <= candidate["source_out_sec"]
                )
                candidate["speech_in_sec"] = old["speech_in_sec"] if inside else None
                candidate["speech_out_sec"] = old["speech_out_sec"] if inside else None
        else:
            if not fresh or existing["status"] == "missing":
                raise _fail(services, "block has no current range", 409)
            candidate = dict(existing)
            if has_take:
                index = payload["selected_take"]
                takes = existing.get("takes") or []
                if isinstance(index, bool) or not isinstance(index, int) or not takes:
                    raise _fail(services, "selected_take must be an index of a take", 400)
                if not 0 <= index < len(takes):
                    raise _fail(services, "selected_take is outside of takes", 400)
                take = takes[index]
                candidate.update(
                    {
                        key: take[key]
                        for key in (
                            "source_in_sec",
                            "source_out_sec",
                            "speech_in_sec",
                            "speech_out_sec",
                        )
                    },
                    selected_take=index,
                    manual_range=False,
                    origin="manual",
                    voice_source="recorded",
                )
            if status:
                candidate["status"] = status
        result = _commit_range(services, paths, data, document, candidate)
    return jsonify({"ok": True, "result": {"project": project, **result}})


def _patch_voice_source(services: ModuleType, block_uid: str):
    payload = _payload(services)
    project, paths, document = _prepare_edit(services, payload)
    wanted = payload.get("voice_source")
    if wanted not in ("recorded", "tts", "none"):
        raise _fail(services, "voice_source must be 'recorded', 'tts' or 'none'", 400)
    with services._project_transition_lock(project):
        data = _locked_edit(services, project, paths, document, payload)
        block = _blocks_by_uid(document).get(block_uid)
        if block is None:
            raise _fail(services, "block not found among active voiceover blocks", 404)
        existing = next((i for i in data["ranges"] if i["block_uid"] == block_uid), None)
        fresh = existing is not None and existing["text_sha256"] == block.sha256
        if wanted == "recorded" and (not fresh or existing["status"] == "missing"):
            raise _fail(services, "block has no recorded range to use", 409)
        if fresh:
            candidate = {**existing, "voice_source": wanted, "origin": "manual"}
        else:
            candidate = {
                "block_uid": block_uid,
                "text_sha256": block.sha256,
                "display_text": block.display_text,
                "recording_id": "",
                "status": "missing",
                "voice_source": wanted,
                "origin": "manual",
            }
        result = _commit_range(services, paths, data, document, candidate)
    return jsonify({"ok": True, "result": {"project": project, **result}})


def _apply(services: ModuleType):
    payload = _payload(services)
    project, paths, document = _prepare_edit(services, payload)
    with services._project_transition_lock(project):
        # Publication excludes TTS, recognition and revision changes (and vice versa).
        if not reserve_voiceover_project(services, project):
            raise _fail(services, f"another project task is active for {project}", 409)
        try:
            recover_publication(paths)
            data = load_recorded_voiceover(paths)
            _check_generation(services, payload, data)
            _check_draft_revision(services, data, document)
            published = build_published_map(
                data,
                document,
                tts_available_uids(paths),
                previous=load_published(paths),
                published_at=services._utc_now_iso(),
            )
            publish_recorded_voiceover(paths, published)
        finally:
            release_voiceover_project(services, project)
    return jsonify(
        {
            "ok": True,
            "result": {
                "project": project,
                "scenario_revision_id": published["scenario_revision_id"],
                "published_sha256": published["published_sha256"],
                "published_generation": published["published_generation"],
                "draft_generation": published["draft_generation"],
                "summary": published["summary"],
                "blocks": published["blocks"],
            },
        }
    )


def _audio(services: ModuleType):
    project = _project_of(services, request.args.get("project"))
    paths = services._ensure_project(project)
    data = load_recorded_voiceover(paths)
    wanted = request.args.get("recording_id")
    recording = next(
        (item for item in data["recordings"] if wanted is None or item["id"] == wanted), None
    )
    if recording is None:
        raise _fail(services, "recording not found", 404)
    path = safe_project_file(Path(paths["root"]), recording["asset"])
    if path is None:
        raise _fail(services, "recording file not found", 404)
    # conditional=True answers Range requests (206) for seeking in the player.
    return send_file(path, mimetype="audio/wav", conditional=True, max_age=0)


def register_voiceover_routes(app: Flask, services: ModuleType) -> None:
    prefix = "/stage1/recorded-voiceover"

    def guarded(view: Callable[..., Any]) -> Callable[..., Any]:
        def wrapper(*args: Any, **kwargs: Any):
            try:
                return view(services, *args, **kwargs)
            except _Reject as rejected:
                return rejected.response
            except Exception as exc:
                return services._internal_error(exc)

        wrapper.__name__ = view.__name__
        return wrapper

    def state_view(services: ModuleType):
        project = _project_of(services, request.args.get("project"))
        paths = services._ensure_project(project)
        document, _reason = services._confirmed_normalization(project)
        return jsonify({"ok": True, "result": _state(services, project, paths, document)})

    def add(rule: str, method: str, view: Callable[..., Any], endpoint: str) -> None:
        wrapped = guarded(view)
        wrapped.__name__ = endpoint
        app.add_url_rule(f"{prefix}{rule}", endpoint, wrapped, methods=[method])

    add("", "GET", state_view, "recorded_voiceover_state")
    add("/source", "POST", _source_upload, "recorded_voiceover_source")
    add("/scan", "POST", _scan, "recorded_voiceover_scan")
    add("/jobs/start", "POST", _jobs_start, "recorded_voiceover_job_start")
    add("/jobs/<job_id>", "GET", _job_status, "recorded_voiceover_job_status")
    add("/jobs/<job_id>/cancel", "POST", _job_cancel, "recorded_voiceover_job_cancel")
    add("/ranges/<block_uid>", "PATCH", _patch_range, "recorded_voiceover_range_patch")
    add(
        "/blocks/<block_uid>/voice-source",
        "PATCH",
        _patch_voice_source,
        "recorded_voiceover_voice_source",
    )
    add("/apply", "POST", _apply, "recorded_voiceover_apply")
    add("/audio", "GET", _audio, "recorded_voiceover_audio")
