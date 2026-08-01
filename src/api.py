from __future__ import annotations

from datetime import UTC, datetime
import hmac
import hashlib
from importlib.metadata import PackageNotFoundError, version as package_version
import json
import os
from pathlib import Path
import platform
import re
import threading
import time
from typing import BinaryIO
import uuid
import wave

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from flask import Flask, abort, jsonify, render_template, request, send_from_directory

from atomic_io import atomic_write_json
from job_runtime import get_job, release_project, reserve_project, update_job

from stage1.analyzer import analyze_script
from stage1.config import get_settings, validate_env
from stage1.gemini_tts import TtsCancelledError, synthesize_phrase, synthesize_tts
from stage1.normalizer import (
    NormalizationMemory,
    NormalizationError,
    NormalizationValidationError,
    RevisionConflictError,
    SourceChangedError,
    confirm_normalization,
    generate_normalization,
    import_legacy_normalization,
    load_normalization,
    preview_learning_candidates,
    reset_normalization,
    save_normalization,
)
from stage1.validator import validate_stage1
from stage2.timeline_parser import (
    normalized_sync_fragments,
    parse_normalized_timeline,
    sync_fragment_id,
)
from stage2.placeholder import generate_placeholder
from stage2.sync_llm_review import SemanticCandidate, review_candidates_with_llm
from stage2.sync_matcher import MatcherConfig, SyncMatcher, normalize_text
from stage2.sync_models import (
    AlignmentRequest,
    SyncAlternative,
    SyncMatch,
    SyncSource,
    WordIndex,
)
from stage2.sync_storage import SyncMatchStore, SyncSourceStore
from stage2.sync_transcript import ingest_word_index, load_word_index, save_word_index
from stage2.whisper_process import (
    WhisperCancelledError,
    transcribe_audio_in_process as transcribe_audio,
)
from stage2.xml_export import build_xml


app = Flask(__name__)
_CONFIG_WARNINGS = validate_env()
for _warning in _CONFIG_WARNINGS:
    app.logger.warning("Configuration: %s", _warning)
ROOT_DIR = Path(__file__).resolve().parent.parent
PROJECTS_DIR = ROOT_DIR / "projects"
NORMALIZATION_MEMORY_PATH = ROOT_DIR / "data" / "normalization_memory.sqlite3"
PROJECT_NAME_RE = re.compile(r"^[A-Za-zА-Яа-яЁё0-9_-]{1,64}$")
SYNC_SOURCE_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,96}$")
_TTS_JOBS: dict[str, dict] = {}
_TTS_JOBS_LOCK = threading.Lock()
_TTS_JOBS_MAX = 200
_TTS_JOBS_TTL_SECONDS = 24 * 60 * 60
_TTS_CONCURRENT_MAX = 3
_TTS_SEMAPHORE = threading.Semaphore(_TTS_CONCURRENT_MAX)
_TTS_ACTIVE_PROJECTS: set[str] = set()
_TTS_CANCEL_EVENTS: dict[str, threading.Event] = {}
_SOURCE_HASH_CACHE: dict[str, tuple[int, int, str]] = {}
_SOURCE_HASH_CACHE_LOCK = threading.Lock()
_SYNC_JOBS: dict[str, dict] = {}
_SYNC_JOBS_LOCK = threading.Lock()
_SYNC_ACTIVE_PROJECTS: set[str] = set()
_SYNC_CANCEL_EVENTS: dict[str, threading.Event] = {}
_SYNC_DATA_LOCK = threading.Lock()
_NORMALIZATION_JOBS: dict[str, dict] = {}
_NORMALIZATION_JOBS_LOCK = threading.Lock()
_NORMALIZATION_ACTIVE_PROJECTS: set[str] = set()
_NORMALIZATION_JOBS_MAX = 50


class AudioUploadTooLargeError(ValueError):
    """Raised after the actual uploaded stream exceeds its byte limit."""


class _JobCancelled(RuntimeError):
    """Internal control-flow signal for cooperative background-job stops."""


def _api_token() -> str:
    configured = app.config.get("API_TOKEN")
    if configured is not None:
        return str(configured).strip()
    return os.getenv("ROUGH_CUT_API_TOKEN", "").strip()


def _request_token() -> str:
    auth = request.authorization
    if auth and auth.type.lower() == "basic":
        return str(auth.password or "")
    header = request.headers.get("Authorization", "").strip()
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return request.headers.get("X-API-Token", "").strip()


def _cep_cors_enabled() -> bool:
    configured = app.config.get("CEP_CORS_ENABLED")
    if configured is not None:
        return bool(configured)
    return os.getenv("ROUGH_CUT_CEP_CORS", "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def _cep_cors_origin() -> str:
    if not _cep_cors_enabled():
        return ""
    origin = request.headers.get("Origin", "").strip()
    return origin if origin in {"null", "file://"} else ""


@app.before_request
def require_api_authentication():
    if request.endpoint == "health":
        return None
    # CEP panels run from a local file origin. Let an explicitly enabled local
    # preflight reach Flask's automatic OPTIONS response; actual requests still
    # require the normal Bearer token below.
    if request.method == "OPTIONS" and _cep_cors_origin():
        return None
    expected = _api_token()
    # Tests must opt into auth explicitly; production always fails closed.
    if app.config.get("TESTING") and app.config.get("API_TOKEN") is None:
        return None
    if not expected:
        return _json_error("ROUGH_CUT_API_TOKEN is not configured", 503)
    supplied = _request_token()
    if supplied and hmac.compare_digest(supplied, expected):
        return None
    response = jsonify({"ok": False, "error": "unauthorized"})
    response.status_code = 401
    # A Basic challenge makes Chromium-based CEP panels display a native
    # username/password dialog even though the panel authenticates with Bearer.
    # Keep the challenge for direct browser navigation only.
    header = request.headers.get("Authorization", "").strip()
    if not _cep_cors_origin() and not header.lower().startswith("bearer "):
        response.headers["WWW-Authenticate"] = (
            'Basic realm="Rough Cut", charset="UTF-8"'
        )
    return response


@app.after_request
def add_cep_cors_headers(response):
    origin = _cep_cors_origin()
    if not origin:
        return response
    response.headers["Access-Control-Allow-Origin"] = origin
    response.headers["Access-Control-Allow-Headers"] = (
        "Authorization, Content-Type, X-API-Token"
    )
    response.headers["Access-Control-Allow-Methods"] = (
        "GET, POST, PUT, PATCH, DELETE, OPTIONS"
    )
    response.headers["Access-Control-Max-Age"] = "600"
    response.headers.add("Vary", "Origin")
    return response


def _json_error(message: str, status_code: int = 400):
    return jsonify({"ok": False, "error": message}), status_code


def _internal_error(exc: Exception):
    app.logger.exception("Unhandled API error", exc_info=exc)
    return _json_error("internal server error", 500)


def _normalize_project_name(name: str) -> str:
    project = (name or "").strip()
    if not PROJECT_NAME_RE.fullmatch(project):
        raise ValueError(
            "project may contain 1-64 Latin/Cyrillic letters, digits, '_' or '-'"
        )
    return project


def _project_paths(project: str) -> dict[str, Path]:
    root = PROJECTS_DIR / project
    return {
        "root": root,
        "input": root / "input",
        "work": root / "work",
        "speaker": root / "speaker",
        "normalized": root / "work" / "scenario_normalized.json",
        "prepared": root / "work" / "scenario_prepared.json",
        "raw": root / "work" / "scenario_raw.txt",
        "sync_sources": root / "work" / "sync_sources.json",
        "sync_matches": root / "work" / "sync_matches.json",
        "sync_job_status": root / "work" / "sync_job_status.json",
        "normalization_job_status": root / "work" / "normalization_job_status.json",
        "tts_job_status": root / "work" / "tts_job_status.json",
        "sync_audio": root / "work" / "sync_audio",
        "sync_transcripts": root / "work" / "transcripts",
        "timeline": root / "work" / "full_timeline.json",
    }


def _ensure_project(project: str) -> dict[str, Path]:
    paths = _project_paths(project)
    paths["input"].mkdir(parents=True, exist_ok=True)
    paths["work"].mkdir(parents=True, exist_ok=True)
    paths["speaker"].mkdir(parents=True, exist_ok=True)
    paths["sync_audio"].mkdir(parents=True, exist_ok=True)
    paths["sync_transcripts"].mkdir(parents=True, exist_ok=True)
    return paths


def _list_projects() -> list[str]:
    if not PROJECTS_DIR.exists():
        return []
    items = [
        p.name
        for p in PROJECTS_DIR.iterdir()
        if p.is_dir() and PROJECT_NAME_RE.fullmatch(p.name)
    ]
    return sorted(items)


def _read_json_safe(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        app.logger.warning("Ignoring unreadable JSON %s: %s", path, exc)
        return {}


def _atomic_write_json_file(path: Path, payload: dict) -> None:
    atomic_write_json(path, payload)


def _normalization_source_is_current(document: dict, project_root: Path) -> bool:
    """Best-effort source hash check, compatible with old normalization drafts."""
    source_meta = document.get("source")
    source_dict = source_meta if isinstance(source_meta, dict) else {}
    expected_hash = str(
        document.get("source_sha256")
        or document.get("source_hash")
        or source_dict.get("sha256")
        or ""
    ).strip().lower()
    source_value = (
        document.get("source_path")
        or source_dict.get("path")
        or (source_meta if isinstance(source_meta, str) else "")
        or ""
    )
    if not expected_hash or not source_value:
        return True
    try:
        source_path = _abs_path(str(source_value), project_root / "input" / "scenario.docx", project_root)
        if not source_path.is_file():
            return False
        stat = source_path.stat()
        cache_key = str(source_path)
        with _SOURCE_HASH_CACHE_LOCK:
            cached = _SOURCE_HASH_CACHE.get(cache_key)
        if cached and cached[:2] == (stat.st_mtime_ns, stat.st_size):
            current_hash = cached[2]
        else:
            current_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()
            with _SOURCE_HASH_CACHE_LOCK:
                _SOURCE_HASH_CACHE[cache_key] = (stat.st_mtime_ns, stat.st_size, current_hash)
    except (OSError, ValueError):
        return False
    return current_hash == expected_hash


def _confirmed_normalization(project: str) -> tuple[dict | None, str]:
    paths = _project_paths(project)
    normalized_path = paths["normalized"]
    if not normalized_path.exists():
        return None, "missing"
    try:
        document = load_normalization(normalized_path)
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None, "invalid"
    if str(document.get("status", "")).lower() != "confirmed":
        return None, "draft"
    if not _normalization_source_is_current(document, paths["root"]):
        return None, "source_changed"
    return document, "confirmed"


def _normalization_required(project: str):
    document, reason = _confirmed_normalization(project)
    if document is not None:
        return None
    return jsonify({
        "ok": False,
        "error": "normalization_confirmation_required",
        "message": "Сначала подтвердите актуальную нормализацию сценария",
        "reason": reason,
        "project": project,
    }), 409


def _prepared_projection_required(project: str):
    paths = _project_paths(project)
    confirmed, reason = _confirmed_normalization(project)
    prepared = _read_json_safe(paths["prepared"])
    if confirmed is not None and (
        prepared.get("normalization_revision") == confirmed.get("revision")
        and str(prepared.get("source") or "") == str(confirmed.get("source") or "")
    ):
        return None
    return jsonify({
        "ok": False,
        "error": "normalization_projection_stale",
        "message": "Подтверждённая проекция для озвучки устарела; подтвердите сценарий повторно",
        "reason": reason if confirmed is None else "prepared_changed",
        "project": project,
    }), 409


def _confirmed_prepared_path(payload: dict, paths: dict[str, Path]) -> Path:
    prepared_path = _abs_path(
        payload.get("prepared_path", ""), paths["prepared"], paths["root"]
    )
    if prepared_path.resolve() != paths["prepared"].resolve():
        raise ValueError("prepared_path must reference confirmed scenario_prepared.json")
    return prepared_path


def _discover_input_files(input_dir: Path, project: str) -> list[str]:
    if not input_dir.exists():
        return []
    files: list[str] = []
    for p in sorted(input_dir.iterdir()):
        if p.is_file() and p.suffix.lower() in {".doc", ".docx", ".txt", ".md"}:
            files.append(str(p.relative_to(ROOT_DIR)))
    return files


def _build_status(project: str) -> dict:
    paths = _project_paths(project)
    prepared_path = paths["work"] / "scenario_prepared.json"
    manifest_path = paths["speaker"] / "manifest.json"

    prepared = _read_json_safe(prepared_path)
    manifest = _read_json_safe(manifest_path)
    normalization = _read_json_safe(paths["normalized"])
    confirmed_normalization, normalization_reason = _confirmed_normalization(project)

    prepared_items = prepared.get("items", []) if isinstance(prepared, dict) else []
    manifest_items = manifest.get("items", []) if isinstance(manifest, dict) else []

    wav_files = sorted([p.name for p in paths["speaker"].glob("zk*.wav") if p.is_file()])

    missing_ids: list[str] = []
    if isinstance(prepared_items, list):
        existing = {p.stem for p in paths["speaker"].glob("zk*.wav")}
        for item in prepared_items:
            block_id = str(item.get("id", "")).strip()
            if block_id and block_id not in existing:
                missing_ids.append(block_id)

    return {
        "project": project,
        "input_files": _discover_input_files(paths["input"], project),
        "prepared_exists": prepared_path.exists(),
        "prepared_items_count": len(prepared_items) if isinstance(prepared_items, list) else 0,
        "manual_markers_used": bool(prepared.get("manual_markers_used")),
        "used_llm": bool(prepared.get("used_llm")),
        "speaker_files_count": len(wav_files),
        "speaker_files": wav_files,
        "manifest_exists": manifest_path.exists(),
        "manifest_items_count": len(manifest_items) if isinstance(manifest_items, list) else 0,
        "missing_ids": missing_ids,
        "normalization_exists": paths["normalized"].exists(),
        "normalization_status": str(normalization.get("status") or "missing"),
        "normalization_revision": normalization.get("revision"),
        "normalization_confirmed": confirmed_normalization is not None,
        "normalization_reason": normalization_reason,
    }


def _resolve_project_name(raw: str | None) -> str:
    if raw:
        return _normalize_project_name(raw)
    projects = _list_projects()
    if projects:
        return projects[0]
    return "default"


def _abs_path(value: str, fallback: Path, root: Path = PROJECTS_DIR) -> Path:
    if not value:
        return fallback
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = ROOT_DIR / candidate
    resolved = candidate.resolve()
    if not resolved.is_relative_to(root):
        raise ValueError(f"Path must be inside {root.name}/: {value}")
    return resolved


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _persist_tts_job(job: dict) -> None:
    """Best-effort atomic snapshot of the latest TTS job for one project."""
    try:
        project = _normalize_project_name(str(job.get("project") or ""))
        path = _project_paths(project)["tts_job_status"]
        _atomic_write_json_file(path, job)
    except (OSError, TypeError, ValueError):
        app.logger.exception("Could not persist TTS job status")


def _load_persisted_tts_job(project: str) -> dict | None:
    payload = _read_json_safe(_project_paths(project)["tts_job_status"])
    if not isinstance(payload, dict):
        return None
    if payload.get("project") != project or not str(payload.get("id") or "").strip():
        return None
    restored = dict(payload)
    if str(restored.get("status") or "").lower() in {
        "queued", "running", "cancelling",
    }:
        restored["status"] = "interrupted"
        restored["event"] = "interrupted"
        restored["message"] = "Interrupted; generated WAV files can be resumed"
        restored["current_id"] = ""
    return restored


def _trim_tts_jobs_locked() -> None:
    now = datetime.now(UTC)
    terminal_ids = [
        job_id for job_id, job in _TTS_JOBS.items()
        if job.get("status") in {"completed", "failed", "cancelled"}
    ]
    for job_id in terminal_ids:
        stamp = _TTS_JOBS[job_id].get("finished_at") or ""
        try:
            expired = (now - datetime.fromisoformat(stamp)).total_seconds() > _TTS_JOBS_TTL_SECONDS
        except (TypeError, ValueError):
            expired = False
        if expired:
            _TTS_JOBS.pop(job_id, None)
    terminal_ids = [
        job_id for job_id, job in _TTS_JOBS.items()
        if job.get("status") in {"completed", "failed", "cancelled"}
    ]
    ordered_ids = sorted(
        terminal_ids,
        key=lambda job_id: (
            _TTS_JOBS[job_id].get("finished_at") or _TTS_JOBS[job_id].get("created_at") or "",
            job_id,
        ),
    )
    drop_count = max(0, len(_TTS_JOBS) - _TTS_JOBS_MAX)
    for job_id in ordered_ids[:drop_count]:
        _TTS_JOBS.pop(job_id, None)


def _reserve_tts_project(project: str) -> bool:
    return reserve_project(_TTS_JOBS_LOCK, _TTS_ACTIVE_PROJECTS, project)


def _release_tts_project(project: str) -> None:
    release_project(_TTS_JOBS_LOCK, _TTS_ACTIVE_PROJECTS, project)


def _create_tts_job(
    project: str,
    *,
    prepared_path: Path | None = None,
    speaker_dir: Path | None = None,
    provider: str | None = None,
    voice: str | None = None,
    gender: str | None = None,
    dry_run: bool = False,
    speed: float = 1.0,
) -> dict:
    job_id = uuid.uuid4().hex
    now = _utc_now_iso()
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
        # Keep the complete launch configuration in every durable checkpoint.
        # The browser uses it to resume with exactly the same controls after a
        # backend or page restart.
        "prepared_path": str(prepared_path) if prepared_path is not None else "",
        "speaker_dir": str(speaker_dir) if speaker_dir is not None else "",
        "provider": (provider or "").strip().lower(),
        "voice": (voice or "").strip(),
        "gender": (gender or "").strip().lower(),
        "dry_run": bool(dry_run),
        "speed": float(speed),
        "resolved_voice": "",
        "error": "",
        "created_at": now,
        "started_at": "",
        "finished_at": "",
        "result": None,
    }
    with _TTS_JOBS_LOCK:
        _TTS_JOBS[job_id] = job
        _TTS_CANCEL_EVENTS[job_id] = threading.Event()
        _trim_tts_jobs_locked()
        snapshot = dict(job)
    _persist_tts_job(snapshot)
    return snapshot


def _update_tts_job(job_id: str, **updates) -> dict | None:
    snapshot = update_job(_TTS_JOBS_LOCK, _TTS_JOBS, job_id, updates)
    if snapshot is None:
        return None
    _persist_tts_job(snapshot)
    return snapshot


def _get_tts_job(job_id: str) -> dict | None:
    return get_job(_TTS_JOBS_LOCK, _TTS_JOBS, job_id)


def _tts_cancel_event(job_id: str) -> threading.Event | None:
    with _TTS_JOBS_LOCK:
        return _TTS_CANCEL_EVENTS.get(job_id)


def _run_tts_job(
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
) -> None:
    cancel_event = _tts_cancel_event(job_id)
    acquired = False
    while not acquired:
        if cancel_event is not None and cancel_event.is_set():
            _update_tts_job(
                job_id,
                status="cancelled",
                event="cancelled",
                message="Cancelled before synthesis started",
                current_id="",
                finished_at=_utc_now_iso(),
            )
            _release_tts_project(project)
            with _TTS_JOBS_LOCK:
                _TTS_CANCEL_EVENTS.pop(job_id, None)
            return
        acquired = _TTS_SEMAPHORE.acquire(timeout=0.25)
    _update_tts_job(
        job_id,
        status="running",
        event="started",
        message="Started",
        started_at=_utc_now_iso(),
        provider=(provider or "").strip().lower(),
        dry_run=bool(dry_run),
        attempts=0,
        retry_count=0,
        last_error="",
    )

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

        if current_id:
            msg = f"{event}: {done}/{total} ({current_id})"
        else:
            msg = f"{event}: {done}/{total}"

        existing = _get_tts_job(job_id) or {}
        retry_count = int(existing.get("retry_count") or 0)
        if event == "item_retrying":
            retry_count += 1

        _update_tts_job(
            job_id,
            status="running",
            event=event,
            message=msg,
            total=total,
            done=done,
            generated=generated,
            skipped=skipped,
            current_id=current_id,
            attempts=attempts,
            retry_count=retry_count,
            last_error=error_text or str(existing.get("last_error") or ""),
        )

    try:
        result = synthesize_tts(
            prepared_path=prepared_path,
            speaker_dir=speaker_dir,
            provider=provider,
            voice_name=voice_name,
            gender=gender,
            dry_run=dry_run,
            progress_callback=progress_callback,
            cancel_check=(cancel_event.is_set if cancel_event is not None else None),
            speed=speed,
        )
        total = int(result.get("items_total") or 0)
        generated = int(result.get("files_count") or 0)
        skipped = int(result.get("skipped_count") or 0)
        _update_tts_job(
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
            finished_at=_utc_now_iso(),
            provider=str(result.get("provider", "")),
            resolved_voice=str(result.get("voice", "")),
            gender=str(result.get("gender", "")),
            dry_run=bool(result.get("dry_run", False)),
            result=result,
        )
    except TtsCancelledError:
        _update_tts_job(
            job_id,
            status="cancelled",
            event="cancelled",
            message="Cancelled; generated WAV files were kept",
            current_id="",
            finished_at=_utc_now_iso(),
        )
        return
    except Exception as exc:
        _update_tts_job(
            job_id,
            status="failed",
            event="failed",
            message="Failed",
            error=str(exc),
            last_error=str(exc),
            finished_at=_utc_now_iso(),
        )
        return
    finally:
        if acquired:
            _TTS_SEMAPHORE.release()
        _release_tts_project(project)
        with _TTS_JOBS_LOCK:
            _TTS_CANCEL_EVENTS.pop(job_id, None)


@app.get("/")
def project_home():
    PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    return render_template("index.html", projects=_list_projects())


@app.get("/projects")
def list_projects():
    PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    return jsonify({"ok": True, "projects": _list_projects()})


@app.post("/projects")
def create_project():
    payload = request.get_json(silent=True) or {}
    project_raw = payload.get("project")
    if not project_raw:
        return _json_error("project is required", 400)

    try:
        project = _normalize_project_name(str(project_raw))
    except ValueError as exc:
        return _json_error(str(exc), 400)

    _ensure_project(project)
    return jsonify({"ok": True, "project": project, "url": f"/project/{project}"})


@app.delete("/projects/<project>")
def delete_project(project: str):
    try:
        project = _normalize_project_name(project)
    except ValueError as exc:
        return _json_error(str(exc), 400)

    project_dir = PROJECTS_DIR / project
    if not project_dir.exists():
        return _json_error("Проект не найден", 404)

    import shutil
    shutil.rmtree(project_dir)
    return jsonify({"ok": True, "project": project})


@app.get("/project/<project>")
def project_dashboard(project: str):
    try:
        project = _normalize_project_name(project)
    except ValueError:
        abort(404)

    settings = get_settings()
    paths = _ensure_project(project)
    status = _build_status(project)

    input_files = _discover_input_files(paths["input"], project)
    defaults = {
        "project": project,
        "input_path": input_files[0] if input_files else f"projects/{project}/input/scenario.docx",
        "output_path": f"projects/{project}/work/scenario_prepared.json",
        "raw_output_path": f"projects/{project}/work/scenario_raw.txt",
        "prepared_path": f"projects/{project}/work/scenario_prepared.json",
        "speaker_dir": f"projects/{project}/speaker",
        "manifest_path": f"projects/{project}/speaker/manifest.json",
        "provider": settings.tts_provider,
        "gender": settings.tts_gender,
    }
    return render_template("project.html", defaults=defaults, status=status)


@app.get("/health")
def health():
    return jsonify({
        "ok": True,
        "status": "healthy",
        "configuration_warnings": list(_CONFIG_WARNINGS),
    })


@app.get("/stage1/status")
def stage1_status():
    raw_project = request.args.get("project")
    try:
        project = _resolve_project_name(raw_project)
        _ensure_project(project)
    except ValueError as exc:
        return _json_error(str(exc), 400)

    return jsonify({"ok": True, "result": _build_status(project)})


@app.get("/speaker/<project>/<path:filename>")
def speaker_file(project: str, filename: str):
    try:
        project = _normalize_project_name(project)
    except ValueError:
        abort(404)

    if "/" in filename or filename.startswith("."):
        abort(404)
    if not filename.endswith(".wav"):
        abort(404)

    paths = _project_paths(project)
    return send_from_directory(paths["speaker"], filename, as_attachment=False)


@app.post("/stage1/analyze")
def stage1_analyze():
    payload = request.get_json(silent=True) or {}

    try:
        project = _resolve_project_name(payload.get("project"))
    except ValueError as exc:
        return _json_error(str(exc), 400)

    paths = _ensure_project(project)
    input_files = _discover_input_files(paths["input"], project)

    input_default = input_files[0] if input_files else str((paths["input"] / "scenario.docx").relative_to(ROOT_DIR))
    output_default = paths["work"] / "scenario_prepared.json"
    raw_default = paths["work"] / "scenario_raw.txt"

    use_llm = bool(payload.get("use_llm", True))

    try:
        input_path = _abs_path(payload.get("input_path", input_default), ROOT_DIR / input_default, paths["root"])
        output_path = _abs_path(payload.get("output_path", ""), output_default, paths["root"])
        raw_output_path = _abs_path(payload.get("raw_output_path", ""), raw_default, paths["root"])
        result = analyze_script(
            input_path=input_path,
            output_path=output_path,
            raw_output_path=raw_output_path,
            use_llm=use_llm,
        )
    except ValueError as exc:
        return _json_error(str(exc), 400)
    except FileNotFoundError as exc:
        return _json_error(str(exc), 404)
    except Exception as exc:
        return _internal_error(exc)

    result["project"] = project
    response = jsonify({"ok": True, "result": result})
    response.headers["Deprecation"] = "true"
    response.headers["Warning"] = '299 - "Legacy analyzer; use /stage1/normalization/generate"'
    return response


def _normalization_exception(exc: Exception):
    if isinstance(exc, RevisionConflictError):
        return _json_error(str(exc) or "normalization revision conflict", 409)
    if isinstance(exc, SourceChangedError):
        return _json_error(str(exc) or "normalization source changed", 409)
    if isinstance(exc, NormalizationValidationError):
        return jsonify({
            "ok": False,
            "error": "normalization_validation_failed",
            "message": str(exc),
            "errors": list(getattr(exc, "errors", []) or []),
        }), 422
    if isinstance(exc, FileNotFoundError):
        return _json_error(str(exc), 404)
    if isinstance(exc, KeyError):
        return _json_error("normalization memory rule not found", 404)
    if isinstance(exc, (NormalizationError, ValueError)):
        return _json_error(str(exc), 400)
    return _internal_error(exc)


def _normalization_payload_for_save(payload: dict, normalized_path: Path) -> dict:
    current = load_normalization(normalized_path)
    editable = {
        key: value for key, value in payload.items()
        if key in {"blocks", "hint"}
    }
    # The UI normally sends only blocks; retaining metadata prevents accidental
    # loss of source hashes and other server-owned document fields.
    current.update(editable)
    return current


def _persist_normalization_job(job: dict) -> None:
    try:
        project = _normalize_project_name(str(job.get("project") or ""))
        _atomic_write_json_file(
            _project_paths(project)["normalization_job_status"],
            job,
        )
    except (OSError, TypeError, ValueError):
        app.logger.exception("Could not persist normalization job status")


def _create_normalization_job(
    project: str,
    *,
    input_path: Path,
    use_llm: bool,
    hint: str,
) -> dict:
    now = _utc_now_iso()
    job = {
        "id": uuid.uuid4().hex,
        "project": project,
        "status": "queued",
        "message": "Queued",
        "input_path": str(input_path),
        "use_llm": bool(use_llm),
        "hint": hint,
        "created_at": now,
        "started_at": "",
        "finished_at": "",
        "error": "",
        "result": None,
    }
    with _NORMALIZATION_JOBS_LOCK:
        _NORMALIZATION_JOBS[job["id"]] = job
        terminal = sorted(
            (
                (job_id, item)
                for job_id, item in _NORMALIZATION_JOBS.items()
                if item.get("status") in {"completed", "failed", "cancelled"}
            ),
            key=lambda pair: (pair[1].get("finished_at") or pair[1].get("created_at") or ""),
        )
        for old_id, _old_job in terminal[:max(0, len(_NORMALIZATION_JOBS) - _NORMALIZATION_JOBS_MAX)]:
            _NORMALIZATION_JOBS.pop(old_id, None)
        snapshot = dict(job)
    _persist_normalization_job(snapshot)
    return snapshot


def _update_normalization_job(job_id: str, **updates) -> dict | None:
    snapshot = update_job(
        _NORMALIZATION_JOBS_LOCK,
        _NORMALIZATION_JOBS,
        job_id,
        updates,
    )
    if snapshot is None:
        return None
    _persist_normalization_job(snapshot)
    return snapshot


def _get_normalization_job(job_id: str) -> dict | None:
    return get_job(_NORMALIZATION_JOBS_LOCK, _NORMALIZATION_JOBS, job_id)


def _load_persisted_normalization_job(project: str) -> dict | None:
    payload = _read_json_safe(_project_paths(project)["normalization_job_status"])
    if not isinstance(payload, dict) or payload.get("project") != project:
        return None
    restored = dict(payload)
    if restored.get("status") in {"queued", "running"}:
        restored.update({
            "status": "interrupted",
            "message": "Interrupted; normalization can be restarted",
            "finished_at": _utc_now_iso(),
        })
    return restored


def _run_normalization_job(
    *,
    job_id: str,
    project: str,
    input_path: Path,
    output_path: Path,
    use_llm: bool,
    hint: str,
) -> None:
    _update_normalization_job(
        job_id,
        status="running",
        message="Normalizing scenario",
        started_at=_utc_now_iso(),
    )
    try:
        result = generate_normalization(
            input_path,
            output_path,
            memory_path=NORMALIZATION_MEMORY_PATH,
            use_llm=use_llm,
            hint=hint,
        )
        _update_normalization_job(
            job_id,
            status="completed",
            message="Completed",
            finished_at=_utc_now_iso(),
            result=result,
        )
    except Exception as exc:
        app.logger.exception("Normalization job failed project=%s job=%s", project, job_id)
        _update_normalization_job(
            job_id,
            status="failed",
            message="Failed",
            error=str(exc),
            finished_at=_utc_now_iso(),
        )
    finally:
        release_project(
            _NORMALIZATION_JOBS_LOCK,
            _NORMALIZATION_ACTIVE_PROJECTS,
            project,
        )


@app.post("/stage1/normalization-jobs/start")
def stage1_normalization_job_start():
    payload = request.get_json(silent=True) or {}
    try:
        project = _resolve_project_name(payload.get("project"))
        paths = _ensure_project(project)
        input_files = _discover_input_files(paths["input"], project)
        default_input = ROOT_DIR / input_files[0] if input_files else paths["input"] / "scenario.docx"
        input_path = _abs_path(
            str(payload.get("input_path") or ""),
            default_input,
            paths["root"],
        )
        if not input_path.is_file():
            raise FileNotFoundError(f"Input file not found: {input_path}")
    except ValueError as exc:
        return _json_error(str(exc), 400)
    except FileNotFoundError as exc:
        return _json_error(str(exc), 404)

    if not reserve_project(
        _NORMALIZATION_JOBS_LOCK,
        _NORMALIZATION_ACTIVE_PROJECTS,
        project,
    ):
        return _json_error(f"normalization already active for project {project}", 409)
    job = _create_normalization_job(
        project,
        input_path=input_path,
        use_llm=bool(payload.get("use_llm", True)),
        hint=str(payload.get("hint") or payload.get("author_hint") or ""),
    )
    worker = threading.Thread(
        target=_run_normalization_job,
        kwargs={
            "job_id": job["id"],
            "project": project,
            "input_path": input_path,
            "output_path": paths["normalized"],
            "use_llm": job["use_llm"],
            "hint": job["hint"],
        },
        daemon=True,
        name=f"normalization-{project}-{job['id'][:8]}",
    )
    try:
        worker.start()
    except Exception:
        release_project(
            _NORMALIZATION_JOBS_LOCK,
            _NORMALIZATION_ACTIVE_PROJECTS,
            project,
        )
        raise
    return jsonify({"ok": True, "result": job}), 202


@app.get("/stage1/normalization-jobs/<job_id>")
def stage1_normalization_job_get(job_id: str):
    job = _get_normalization_job(job_id)
    if job is None:
        return _json_error("normalization job not found", 404)
    return jsonify({"ok": True, "result": job})


@app.get("/stage1/normalization-jobs/status")
def stage1_normalization_job_status():
    try:
        project = _resolve_project_name(request.args.get("project"))
    except ValueError as exc:
        return _json_error(str(exc), 400)
    with _NORMALIZATION_JOBS_LOCK:
        candidates = [
            dict(job)
            for job in _NORMALIZATION_JOBS.values()
            if job.get("project") == project
        ]
    if candidates:
        latest = max(candidates, key=lambda item: item.get("created_at") or "")
    else:
        latest = _load_persisted_normalization_job(project)
    return jsonify({"ok": True, "result": latest or {}})


@app.post("/stage1/normalization/generate")
def stage1_normalization_generate():
    payload = request.get_json(silent=True) or {}
    try:
        project = _resolve_project_name(payload.get("project"))
        paths = _ensure_project(project)
        input_files = _discover_input_files(paths["input"], project)
        default_input = (
            ROOT_DIR / input_files[0]
            if input_files else paths["input"] / "scenario.docx"
        )
        input_path = _abs_path(
            str(payload.get("input_path") or ""), default_input, paths["root"]
        )
        result = generate_normalization(
            input_path,
            paths["normalized"],
            memory_path=NORMALIZATION_MEMORY_PATH,
            use_llm=bool(payload.get("use_llm", True)),
            hint=str(payload.get("hint") or payload.get("author_hint") or ""),
        )
    except Exception as exc:
        return _normalization_exception(exc)
    return jsonify({"ok": True, "project": project, "result": result})


@app.get("/stage1/normalization")
def stage1_normalization_get():
    try:
        project = _resolve_project_name(request.args.get("project"))
        paths = _ensure_project(project)
        if not paths["normalized"].exists() and paths["prepared"].exists():
            input_files = _discover_input_files(paths["input"], project)
            source_path = ROOT_DIR / input_files[0] if input_files else None
            import_legacy_normalization(
                paths["prepared"],
                paths["raw"],
                paths["normalized"],
                source_path=source_path,
            )
        result = load_normalization(paths["normalized"])
    except Exception as exc:
        return _normalization_exception(exc)
    return jsonify({"ok": True, "project": project, "result": result})


@app.put("/stage1/normalization")
def stage1_normalization_save():
    payload = request.get_json(silent=True) or {}
    try:
        project = _resolve_project_name(payload.get("project"))
        paths = _ensure_project(project)
        revision_value = payload.get("revision", payload.get("expected_revision"))
        if revision_value is None:
            return _json_error("revision is required", 400)
        expected_revision = int(revision_value)
        document = _normalization_payload_for_save(payload, paths["normalized"])
        result = save_normalization(
            paths["normalized"], document, expected_revision=expected_revision
        )
    except Exception as exc:
        return _normalization_exception(exc)
    return jsonify({"ok": True, "project": project, "result": result})


@app.post("/stage1/normalization/reset")
def stage1_normalization_reset():
    payload = request.get_json(silent=True) or {}
    try:
        project = _resolve_project_name(payload.get("project"))
        paths = _ensure_project(project)
        revision_value = payload.get("revision", payload.get("expected_revision"))
        if revision_value is None:
            return _json_error("revision is required", 400)
        result = reset_normalization(
            paths["normalized"],
            expected_revision=int(revision_value),
            source_root=paths["root"],
        )
    except Exception as exc:
        return _normalization_exception(exc)
    return jsonify({"ok": True, "project": project, "result": result})


@app.post("/stage1/normalization/confirm")
def stage1_normalization_confirm():
    payload = request.get_json(silent=True) or {}
    try:
        project = _resolve_project_name(payload.get("project"))
        paths = _ensure_project(project)
        revision_value = payload.get("revision", payload.get("expected_revision"))
        if revision_value is None:
            return _json_error("revision is required", 400)
        expected_revision = int(revision_value)

        # Allow the editor to submit its last block changes together with confirm.
        if any(key in {"blocks", "hint"} for key in payload):
            document = _normalization_payload_for_save(payload, paths["normalized"])
            saved = save_normalization(
                paths["normalized"], document, expected_revision=expected_revision
            )
            expected_revision = int(saved.get("revision", expected_revision + 1))

        result = confirm_normalization(
            paths["normalized"],
            paths["prepared"],
            paths["raw"],
            memory_path=NORMALIZATION_MEMORY_PATH,
            expected_revision=expected_revision,
            learning_decisions=payload.get("learning_decisions"),
        )
    except Exception as exc:
        return _normalization_exception(exc)
    return jsonify({"ok": True, "project": project, "result": result})


@app.post("/stage1/normalization/confirm/preview")
def stage1_normalization_learning_preview():
    payload = request.get_json(silent=True) or {}
    try:
        project = _resolve_project_name(payload.get("project"))
        paths = _ensure_project(project)
        revision_value = payload.get("revision", payload.get("expected_revision"))
        if revision_value is None:
            return _json_error("revision is required", 400)
        candidates = preview_learning_candidates(
            paths["normalized"],
            memory_path=NORMALIZATION_MEMORY_PATH,
            expected_revision=int(revision_value),
        )
    except Exception as exc:
        return _normalization_exception(exc)
    return jsonify({
        "ok": True,
        "project": project,
        "result": {"revision": int(revision_value), "candidates": candidates},
    })


def _memory_instance() -> NormalizationMemory:
    NORMALIZATION_MEMORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    return NormalizationMemory(NORMALIZATION_MEMORY_PATH)


def _close_memory(memory: NormalizationMemory) -> None:
    close = getattr(memory, "close", None)
    if callable(close):
        close()


@app.get("/stage1/normalization/memory")
def stage1_normalization_memory_get():
    memory = None
    try:
        memory = _memory_instance()
        rules = memory.list_rules()
        status_filter = str(request.args.get("status") or "").strip().lower()
        type_filter = str(request.args.get("block_type") or "").strip().lower()
        search = str(request.args.get("search") or "").strip().casefold()
        if status_filter:
            rules = [r for r in rules if str(r.get("status", "")).lower() == status_filter]
        if type_filter:
            rules = [
                r for r in rules
                if str(r.get("block_type") or r.get("target_type") or "").lower() == type_filter
            ]
        if search:
            rules = [
                r for r in rules
                if search in str(r.get("alias") or r.get("marker") or "").casefold()
            ]
    except Exception as exc:
        return _normalization_exception(exc)
    finally:
        if memory is not None:
            _close_memory(memory)
    return jsonify({"ok": True, "result": {"rules": rules}})


@app.post("/stage1/normalization/memory")
def stage1_normalization_memory_create():
    payload = request.get_json(silent=True) or {}
    marker = str(payload.get("alias") or payload.get("marker") or "").strip()
    target_type = str(payload.get("block_type") or payload.get("target_type") or "").strip()
    if not marker or not target_type:
        return _json_error("alias and block_type are required", 400)
    memory = None
    try:
        memory = _memory_instance()
        result = memory.create_rule(marker, target_type, source="manual")
    except Exception as exc:
        return _normalization_exception(exc)
    finally:
        if memory is not None:
            _close_memory(memory)
    return jsonify({"ok": True, "result": result}), 201


@app.patch("/stage1/normalization/memory/<int:rule_id>")
def stage1_normalization_memory_update(rule_id: int):
    payload = request.get_json(silent=True) or {}
    updates = {key: value for key, value in payload.items() if key not in {"project", "id"}}
    if "alias" in updates:
        updates["marker"] = updates.pop("alias")
    if "block_type" in updates:
        updates["target_type"] = updates.pop("block_type")
    memory = None
    try:
        memory = _memory_instance()
        result = memory.update_rule(rule_id, **updates)
    except Exception as exc:
        return _normalization_exception(exc)
    finally:
        if memory is not None:
            _close_memory(memory)
    return jsonify({"ok": True, "result": result})


@app.delete("/stage1/normalization/memory/<int:rule_id>")
def stage1_normalization_memory_delete(rule_id: int):
    memory = None
    try:
        memory = _memory_instance()
        result = memory.delete_rule(rule_id)
    except Exception as exc:
        return _normalization_exception(exc)
    finally:
        if memory is not None:
            _close_memory(memory)
    return jsonify({"ok": True, "result": result})


@app.post("/stage1/tts")
def stage1_tts():
    payload = request.get_json(silent=True) or {}

    try:
        project = _resolve_project_name(payload.get("project"))
    except ValueError as exc:
        return _json_error(str(exc), 400)

    paths = _ensure_project(project)
    gate = _normalization_required(project)
    if gate:
        return gate
    projection_gate = _prepared_projection_required(project)
    if projection_gate:
        return projection_gate

    provider = payload.get("provider")
    if provider and str(provider).strip().lower() not in {"gemini", "openrouter", "macos"}:
        return _json_error("provider must be 'gemini' or 'openrouter' or 'macos'", 400)

    gender = payload.get("gender")
    if gender and str(gender).strip().lower() not in {"female", "male"}:
        return _json_error("gender must be 'female' or 'male'", 400)

    voice_name = payload.get("voice_name")
    dry_run = bool(payload.get("dry_run", False))

    if not _reserve_tts_project(project):
        return _json_error(f"TTS already active for project {project}", 409)
    try:
        prepared_path = _confirmed_prepared_path(payload, paths)
        speaker_dir = _abs_path(payload.get("speaker_dir", ""), paths["speaker"], paths["root"])
        result = synthesize_tts(
            prepared_path=prepared_path,
            speaker_dir=speaker_dir,
            provider=provider,
            voice_name=voice_name,
            gender=gender,
            dry_run=dry_run,
        )
    except ValueError as exc:
        return _json_error(str(exc), 400)
    except FileNotFoundError as exc:
        return _json_error(str(exc), 404)
    except Exception as exc:
        return _internal_error(exc)
    finally:
        _release_tts_project(project)

    result["project"] = project
    return jsonify({"ok": True, "result": result})


@app.post("/stage1/tts-jobs/start")
def stage1_tts_job_start():
    payload = request.get_json(silent=True) or {}

    try:
        project = _resolve_project_name(payload.get("project"))
    except ValueError as exc:
        return _json_error(str(exc), 400)

    paths = _ensure_project(project)
    gate = _normalization_required(project)
    if gate:
        return gate
    projection_gate = _prepared_projection_required(project)
    if projection_gate:
        return projection_gate

    provider = payload.get("provider")
    if provider and str(provider).strip().lower() not in {"gemini", "openrouter", "macos"}:
        return _json_error("provider must be 'gemini' or 'openrouter' or 'macos'", 400)

    gender = payload.get("gender")
    if gender and str(gender).strip().lower() not in {"female", "male"}:
        return _json_error("gender must be 'female' or 'male'", 400)

    voice_name = payload.get("voice_name")
    dry_run = bool(payload.get("dry_run", False))
    try:
        speed = float(payload.get("speed", 1.0))
    except (TypeError, ValueError):
        return _json_error("speed must be a number between 0.5 and 1.5", 400)
    speed = max(0.5, min(1.5, speed))

    try:
        prepared_path = _confirmed_prepared_path(payload, paths)
        speaker_dir = _abs_path(payload.get("speaker_dir", ""), paths["speaker"], paths["root"])
    except ValueError as exc:
        return _json_error(str(exc), 400)

    if not _reserve_tts_project(project):
        return _json_error(f"TTS already active for project {project}", 409)

    try:
        job = _create_tts_job(
            project=project,
            prepared_path=prepared_path,
            speaker_dir=speaker_dir,
            provider=provider,
            voice=voice_name,
            gender=gender,
            dry_run=dry_run,
            speed=speed,
        )
        worker = threading.Thread(
            target=_run_tts_job,
            kwargs={
                "job_id": job["id"],
                "project": project,
                "prepared_path": prepared_path,
                "speaker_dir": speaker_dir,
                "provider": provider,
                "voice_name": voice_name,
                "gender": gender,
                "dry_run": dry_run,
                "speed": speed,
            },
            daemon=True,
        )
        worker.start()
    except Exception:
        _release_tts_project(project)
        raise

    return jsonify(
        {
            "ok": True,
            "result": {
                "project": project,
                "job_id": job["id"],
                "status": "queued",
                "status_url": f"/stage1/tts-jobs/{job['id']}",
            },
        }
    )


@app.get("/stage1/tts-jobs/<job_id>")
def stage1_tts_job_status(job_id: str):
    job = _get_tts_job(job_id)
    if not job:
        return _json_error("job not found", 404)
    return jsonify({"ok": True, "result": job})


@app.get("/stage1/tts-jobs/status")
def stage1_tts_job_status_alias():
    try:
        project = _resolve_project_name(request.args.get("project"))
    except ValueError as exc:
        return _json_error(str(exc), 400)
    with _TTS_JOBS_LOCK:
        candidates = [
            dict(item) for item in _TTS_JOBS.values()
            if item.get("project") == project
        ]
    if candidates:
        latest = max(candidates, key=lambda item: str(item.get("created_at") or ""))
        return jsonify({"ok": True, "result": latest})
    persisted = _load_persisted_tts_job(project)
    if persisted is None:
        return _json_error("job not found", 404)
    return jsonify({"ok": True, "result": persisted})


@app.post("/stage1/tts-jobs/<job_id>/cancel")
def stage1_tts_job_cancel(job_id: str):
    with _TTS_JOBS_LOCK:
        job = _TTS_JOBS.get(job_id)
        if job is None:
            return _json_error("job not found", 404)
        if job.get("status") not in {"queued", "running", "cancelling"}:
            return _json_error("job is not active", 409)
        event = _TTS_CANCEL_EVENTS.get(job_id)
        if event is None:
            return _json_error("job worker is not active", 409)
        event.set()
        job.update({
            "status": "cancelling",
            "event": "cancelling",
            "message": "Cancellation requested; finishing current TTS call",
        })
        snapshot = dict(job)
    _persist_tts_job(snapshot)
    return jsonify({"ok": True, "result": snapshot}), 202


@app.post("/stage1/tts-phrase")
def stage1_tts_phrase():
    payload = request.get_json(silent=True) or {}

    try:
        project = _resolve_project_name(payload.get("project"))
    except ValueError as exc:
        return _json_error(str(exc), 400)

    paths = _ensure_project(project)

    provider = payload.get("provider")
    if provider and str(provider).strip().lower() not in {"gemini", "openrouter", "macos"}:
        return _json_error("provider must be 'gemini' or 'openrouter' or 'macos'", 400)

    gender = payload.get("gender")
    if gender and str(gender).strip().lower() not in {"female", "male"}:
        return _json_error("gender must be 'female' or 'male'", 400)

    text = str(payload.get("text", ""))
    voice_name = payload.get("voice_name")
    file_name = payload.get("file_name")
    dry_run = bool(payload.get("dry_run", False))
    try:
        speed = float(payload.get("speed", 1.0))
    except (TypeError, ValueError):
        return _json_error("speed must be a number between 0.5 and 1.5", 400)
    speed = max(0.5, min(1.5, speed))

    if not _reserve_tts_project(project):
        return _json_error(f"TTS already active for project {project}", 409)
    try:
        speaker_dir = _abs_path(payload.get("speaker_dir", ""), paths["speaker"], paths["root"])
        result = synthesize_phrase(
            text=text,
            speaker_dir=speaker_dir,
            provider=provider,
            voice_name=voice_name,
            gender=gender,
            file_name=file_name,
            dry_run=dry_run,
            speed=speed,
        )
    except ValueError as exc:
        return _json_error(str(exc), 400)
    except FileNotFoundError as exc:
        return _json_error(str(exc), 404)
    except Exception as exc:
        return _internal_error(exc)
    finally:
        _release_tts_project(project)

    result["project"] = project
    out_file = Path(result.get("file", ""))
    if out_file.suffix.lower() == ".wav" and out_file.parent.resolve() == paths["speaker"].resolve():
        result["playback_url"] = f"/speaker/{project}/{out_file.name}"

    return jsonify({"ok": True, "result": result})


@app.post("/stage1/validate")
def stage1_validate():
    payload = request.get_json(silent=True) or {}

    try:
        project = _resolve_project_name(payload.get("project"))
    except ValueError as exc:
        return _json_error(str(exc), 400)

    paths = _ensure_project(project)
    gate = _normalization_required(project)
    if gate:
        return gate
    projection_gate = _prepared_projection_required(project)
    if projection_gate:
        return projection_gate

    try:
        prepared_path = _confirmed_prepared_path(payload, paths)
        speaker_dir = _abs_path(payload.get("speaker_dir", ""), paths["speaker"], paths["root"])
        manifest_path = _abs_path(payload.get("manifest_path", ""), paths["speaker"] / "manifest.json", paths["root"])
        result = validate_stage1(
            prepared_path=prepared_path,
            speaker_dir=speaker_dir,
            manifest_path=manifest_path,
            project_name=project,
        )
    except ValueError as exc:
        return _json_error(str(exc), 400)
    except FileNotFoundError as exc:
        return _json_error(str(exc), 404)
    except Exception as exc:
        return _internal_error(exc)

    result["project"] = project
    status_code = 200 if result.get("ok") else 422
    return jsonify({"ok": result.get("ok", False), "result": result}), status_code


# ── Stage 2 ────────────────────────────────────────────────────────────────────

def _sync_source_id(raw: object) -> str:
    source_id = str(raw or "").strip()
    if not SYNC_SOURCE_ID_RE.fullmatch(source_id):
        raise ValueError("source id must match ^[a-zA-Z0-9_-]{1,96}$")
    return source_id


def _sync_stores(paths: dict[str, Path]) -> tuple[SyncSourceStore, SyncMatchStore]:
    return SyncSourceStore(paths["sync_sources"]), SyncMatchStore(paths["sync_matches"])


def _source_api_dict(source: SyncSource) -> dict:
    result = source.to_dict()
    result["sequence_name"] = source.premiere_sequence
    return result


def _match_api_dict(match: SyncMatch) -> dict:
    result = match.to_dict()
    result["block_uid"] = str(
        match.metadata.get("parent_block_uid") or match.segment_id
    )
    return result


def _word_index_path(paths: dict[str, Path], source_id: str) -> Path:
    return paths["sync_transcripts"] / f"{_sync_source_id(source_id)}.words.json"


def _canonical_audio_path(paths: dict[str, Path], source_id: str) -> Path:
    return paths["sync_audio"] / f"{_sync_source_id(source_id)}.wav"


def _resolve_sync_audio_path(source: SyncSource, paths: dict[str, Path]) -> Path:
    """Resolve configured guide audio without allowing a project-root escape."""
    canonical = _canonical_audio_path(paths, source.id)
    if canonical.is_file():
        return canonical
    value = str(source.guide_audio or "").strip()
    if not value:
        return canonical
    configured = Path(value)
    candidates = (
        [configured]
        if configured.is_absolute()
        else [paths["root"] / configured, paths["work"] / configured]
    )
    for candidate in candidates:
        resolved = candidate.resolve()
        if not resolved.is_relative_to(paths["root"].resolve()):
            raise ValueError("guide_audio must be inside the project directory")
        if resolved.is_file():
            return resolved
    return candidates[0].resolve()


_TRANSCRIPTION_CACHE_KEY = "transcription_cache"


def _audio_stat_signature(audio_path: Path) -> dict[str, int]:
    stat = audio_path.stat()
    return {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def _audio_cache_signature(audio_path: Path) -> dict[str, int | str]:
    """Cheap content-aware identity for large guide WAV files.

    Hashing the first and last MiB avoids re-reading multi-gigabyte guides on
    every status run while detecting copies whose mtime changed and ordinary
    in-place replacements whose size did not.
    """
    stat_signature = _audio_stat_signature(audio_path)
    size = stat_signature["size"]
    sample_size = 1024 * 1024
    digest = hashlib.sha256()
    digest.update(str(size).encode("ascii"))
    with audio_path.open("rb") as stream:
        digest.update(stream.read(sample_size))
        if size > sample_size:
            stream.seek(max(0, size - sample_size))
            digest.update(stream.read(sample_size))
    return {
        **stat_signature,
        "sample_sha256": digest.hexdigest(),
    }


def _installed_version(distribution: str) -> str:
    try:
        return package_version(distribution)
    except PackageNotFoundError:
        return "<not-installed>"


def _transcription_config(
    *, backend: str | None, model: str | None, language: str
) -> dict[str, str]:
    """Return lightweight cache inputs without importing a Whisper backend."""

    configured_backend = (
        str(backend).strip() if backend is not None else ""
    ) or os.getenv("SYNC_WHISPER_BACKEND", "auto").strip() or "auto"
    configured_model = (
        str(model).strip() if model is not None else ""
    ) or os.getenv("SYNC_WHISPER_MODEL", "").strip() or "<backend-default>"
    return {
        "backend": configured_backend.lower(),
        "model": configured_model,
        "language": str(language).strip().lower(),
        "beam_size": os.getenv("SYNC_WHISPER_BEAM_SIZE", "5").strip() or "5",
        "best_of": os.getenv("SYNC_WHISPER_BEST_OF", "5").strip() or "5",
        "batch_size": os.getenv("SYNC_WHISPER_BATCH_SIZE", "0").strip() or "0",
        "device": os.getenv("SYNC_WHISPER_DEVICE", "auto").strip().lower() or "auto",
        "compute_type": (
            os.getenv("SYNC_WHISPER_COMPUTE_TYPE", "default").strip().lower()
            or "default"
        ),
        "runtime_platform": f"{platform.system()}-{platform.machine()}",
        "faster_whisper_version": _installed_version("faster-whisper"),
        "mlx_whisper_version": _installed_version("mlx-whisper"),
        "openai_whisper_version": _installed_version("openai-whisper"),
    }


def _transcription_config_fingerprint(config: dict[str, str]) -> str:
    payload = json.dumps(
        config, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _transcription_cache_metadata(
    audio_path: Path,
    config: dict[str, str],
    *,
    origin: str = "generated",
    audio_signature: dict[str, int | str] | None = None,
) -> dict[str, object]:
    return {
        "version": 1,
        "audio": audio_signature or _audio_cache_signature(audio_path),
        "config_fingerprint": _transcription_config_fingerprint(config),
        "config": config,
        "origin": origin,
    }


def _word_index_cache_is_current(
    index: WordIndex, audio_path: Path, config: dict[str, str]
) -> bool:
    cache = index.metadata.get(_TRANSCRIPTION_CACHE_KEY)
    if not isinstance(cache, dict):
        return False
    if not _word_index_audio_matches(index, audio_path):
        return False
    if cache.get("origin") == "manual":
        return True
    return cache.get("config_fingerprint") == _transcription_config_fingerprint(config)


def _word_index_audio_matches(index: WordIndex, audio_path: Path) -> bool:
    cache = index.metadata.get(_TRANSCRIPTION_CACHE_KEY)
    if not isinstance(cache, dict) or cache.get("version") != 1:
        return False
    try:
        audio_signature = _audio_cache_signature(audio_path)
    except OSError:
        return False
    stored = cache.get("audio")
    if not isinstance(stored, dict):
        return False
    if stored.get("sample_sha256"):
        return (
            stored.get("size") == audio_signature.get("size")
            and stored.get("sample_sha256") == audio_signature.get("sample_sha256")
        )
    # Backward-compatible first read. The caller upgrades this metadata after
    # a successful size/mtime comparison without retranscribing the source.
    return stored == _audio_stat_signature(audio_path)


def _copy_stream_limited(
    source: BinaryIO,
    destination: Path,
    max_bytes: int,
    *,
    chunk_size: int = 1024 * 1024,
) -> int:
    """Copy a binary stream while enforcing the limit on bytes actually read."""
    if max_bytes <= 0:
        raise ValueError("audio upload limit must be positive")
    if chunk_size <= 0:
        raise ValueError("chunk size must be positive")
    written = 0
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("xb") as output:
        while True:
            chunk = source.read(min(chunk_size, max_bytes - written + 1))
            if not chunk:
                break
            written += len(chunk)
            if written > max_bytes:
                raise AudioUploadTooLargeError("audio upload is too large")
            output.write(chunk)
        output.flush()
        os.fsync(output.fileno())
    return written


def _cleanup_stale_uploads(directory: Path, *, max_age_seconds: int = 24 * 60 * 60) -> int:
    """Remove abandoned hidden upload parts after a conservative grace period."""
    if not directory.is_dir():
        return 0
    cutoff = time.time() - max_age_seconds
    removed = 0
    for path in directory.glob(".*.upload"):
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except FileNotFoundError:
            continue
        except OSError as exc:
            app.logger.warning("Could not remove stale upload %s: %s", path, exc)
    return removed


def _sync_quote(block: dict) -> str:
    """Remove a scenario speaker/role/timecode prefix before fuzzy matching."""
    text = str(
        block.get("normalized_text") or block.get("original_text") or ""
    ).strip()
    if not text:
        return ""

    timecode = str(block.get("timecode_start") or "").strip()
    timecode_end = str(block.get("timecode_end") or "").strip()
    if timecode:
        position = text.find(timecode)
        # Only a timestamp in the scenario prefix is structural. A timestamp
        # mentioned later in the quote must remain part of the quote.
        if 0 <= position <= 180:
            end = position + len(timecode)
            if timecode_end:
                end_position = text.find(timecode_end, end, end + 32)
                if end_position >= 0:
                    between = text[end:end_position]
                    if not re.search(r"[A-Za-zА-Яа-яЁё0-9]", between):
                        end = end_position + len(timecode_end)
            quote = text[end:].lstrip(" \t\r\n:;,.…—–-")
            if quote:
                return quote

    speaker = str(block.get("speaker") or "").strip()
    role = str(block.get("role") or "").strip()
    if speaker and text.casefold().startswith(speaker.casefold()):
        remainder = text[len(speaker):].lstrip(" \t,")
        if role and remainder.casefold().startswith(role.casefold()):
            remainder = remainder[len(role):]
        quote = remainder.lstrip(" \t\r\n:;,.…—–-")
        if quote:
            return quote
    return text


def _sync_alignment_units(blocks: list[dict]) -> list[dict[str, object]]:
    """Expand a canonical speaker block into independently matchable pieces."""
    units: list[dict[str, object]] = []
    last_speaker = {"sync": "", "host": ""}
    for block in blocks:
        block_type = str(block.get("type") or "").lower()
        parent_uid = str(block.get("uid") or "").strip()
        if not parent_uid:
            units.append({"segment_id": "", "parent_block_uid": "", "block": block})
            continue
        speaker = str(block.get("speaker") or "").strip()
        if speaker:
            last_speaker[block_type] = speaker
        else:
            speaker = last_speaker.get(block_type, "")
        raw_fragments = block.get("sync_fragments")
        fragments = (
            normalized_sync_fragments(block)
            if block_type == "sync" and isinstance(raw_fragments, list) and raw_fragments
            else [{
                "text": _sync_quote(block),
                "timecode_start": str(block.get("timecode_start") or "").strip(),
                "timecode_end": str(block.get("timecode_end") or "").strip(),
            }]
        )
        for index, fragment in enumerate(fragments):
            units.append({
                "segment_id": sync_fragment_id(parent_uid, index, len(fragments)),
                "parent_block_uid": parent_uid,
                "fragment_index": index,
                "fragment_count": len(fragments),
                "text": fragment["text"],
                "timecode_start": fragment["timecode_start"],
                "speaker": speaker,
                "block": block,
            })
    return units


def _sync_summary(matches: list[SyncMatch]) -> dict[str, int]:
    auto_matched = sum(item.status == "auto_matched" for item in matches)
    confirmed = sum(item.status == "confirmed" for item in matches)
    needs_review = sum(item.status == "needs_review" for item in matches)
    not_found = sum(item.status == "not_found" for item in matches)
    return {
        "auto_matched": auto_matched,
        "confirmed": confirmed,
        "needs_review": needs_review,
        "not_found": not_found,
        "matched": auto_matched + confirmed,
        "review": needs_review,
        "unmatched": not_found,
    }


def _sync_job_with_live_summary(job: dict) -> dict:
    """Refresh terminal job counters after matches are reviewed in the browser."""
    snapshot = dict(job)
    if str(snapshot.get("status") or "") in {"queued", "running", "cancelling"}:
        return snapshot
    project = str(snapshot.get("project") or "").strip()
    if not project:
        return snapshot
    try:
        matches = SyncMatchStore(_project_paths(project)["sync_matches"]).list()
    except ValueError as exc:
        app.logger.warning(
            "Could not refresh sync summary project=%s error=%s", project, exc
        )
        return snapshot
    snapshot.update(_sync_summary(matches))
    return snapshot


def _sync_matching_fingerprint(
    *,
    units: list[dict[str, object]],
    sources: list[SyncSource],
    indexes: list[WordIndex],
    normalization_revision: object,
    timecode_mode: str,
    search_window_sec: float,
    llm_enabled: bool,
    llm_model: str,
    llm_threshold: float,
    llm_limit: int,
) -> str:
    """Identify every durable input that can change a matching decision."""
    payload = {
        "version": 1,
        "normalization_revision": normalization_revision,
        "timecode_mode": timecode_mode,
        "search_window_sec": round(float(search_window_sec), 6),
        "llm": {
            "enabled": bool(llm_enabled),
            "model": llm_model if llm_enabled else "",
            "threshold": round(float(llm_threshold), 6),
            "limit": int(llm_limit),
        },
        "units": [
            {
                "segment_id": str(item.get("segment_id") or ""),
                "parent_block_uid": str(item.get("parent_block_uid") or ""),
                "text": str(item.get("text") or ""),
                "speaker": str(item.get("speaker") or ""),
                "timecode_start": str(item.get("timecode_start") or ""),
            }
            for item in units
        ],
        "sources": [item.to_dict() for item in sources],
        # Hash the actual words, not only their count/duration. A manual word
        # correction or regenerated transcript must invalidate old decisions.
        "indexes": [item.to_dict() for item in indexes],
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _semantic_review_candidates(match: SyncMatch) -> list[SemanticCandidate]:
    candidates: list[SemanticCandidate] = []
    if (
        match.source_id
        and match.source_in_sec is not None
        and match.source_out_sec is not None
        and match.source_out_sec > match.source_in_sec
    ):
        candidates.append(SemanticCandidate(
            source_id=match.source_id,
            source_in_sec=match.source_in_sec,
            source_out_sec=match.source_out_sec,
            confidence=match.confidence,
            matched_text=match.matched_text,
        ))
    for alternative in match.alternatives:
        if alternative.source_id and alternative.source_out_sec > alternative.source_in_sec:
            candidates.append(SemanticCandidate(
                source_id=alternative.source_id,
                source_in_sec=alternative.source_in_sec,
                source_out_sec=alternative.source_out_sec,
                confidence=alternative.confidence,
                matched_text=alternative.matched_text,
            ))
    return candidates


def _apply_semantic_review(
    match: SyncMatch,
    candidates: list[SemanticCandidate],
    selected_index: int,
    *,
    llm_confidence: float,
    reason: str,
    model: str,
) -> SyncMatch:
    selected = candidates[selected_index]
    alternatives = [
        SyncAlternative(
            source_id=item.source_id,
            source_in_sec=item.source_in_sec,
            source_out_sec=item.source_out_sec,
            confidence=item.confidence,
            matched_text=item.matched_text,
        )
        for index, item in enumerate(candidates)
        if index != selected_index
    ]
    metadata = dict(match.metadata)
    metadata["llm_semantic_review"] = {
        "selected_candidate": selected_index,
        "confidence": round(llm_confidence, 4),
        "reason": reason,
        "model": model,
        "original_strategy": match.strategy,
        "original_confidence": match.confidence,
    }
    return SyncMatch(
        segment_id=match.segment_id,
        query_text=match.query_text,
        speaker=match.speaker,
        approximate_timecode=match.approximate_timecode,
        source_id=selected.source_id,
        source_in_sec=selected.source_in_sec,
        source_out_sec=selected.source_out_sec,
        confidence=selected.confidence,
        # An LLM decision is a review aid, never automatic editorial approval.
        status="needs_review",
        matched_text=selected.matched_text,
        strategy="llm_semantic_review",
        alternatives=alternatives,
        metadata=metadata,
    )


def _persist_sync_job(job: dict) -> None:
    """Best-effort atomic snapshot of the latest job for one project."""
    try:
        project = _normalize_project_name(str(job.get("project") or ""))
        path = _project_paths(project)["sync_job_status"]
        _atomic_write_json_file(path, job)
    except (OSError, TypeError, ValueError):
        app.logger.exception("Could not persist sync job status")


def _load_persisted_sync_job(project: str) -> dict | None:
    payload = _read_json_safe(_project_paths(project)["sync_job_status"])
    if not isinstance(payload, dict):
        return None
    if payload.get("project") != project or not str(payload.get("id") or "").strip():
        return None
    restored = dict(payload)
    if str(restored.get("status") or "").lower() in {
        "queued", "running", "cancelling",
    }:
        # There is no live worker for a job loaded only from disk. Present it
        # as an interrupted checkpoint instead of leaving every client polling
        # a task that can never advance. Match decisions are stored separately
        # after each segment, so a new job can safely continue from them.
        restored["status"] = "interrupted"
        restored["message"] = "Interrupted; saved progress can be resumed"
        restored["current_source_id"] = ""
        restored["current_segment_id"] = ""
    return restored


def _create_sync_job(project: str) -> dict:
    now = _utc_now_iso()
    job = {
        "id": uuid.uuid4().hex,
        "project": project,
        "status": "queued",
        "message": "Queued",
        "total_sources": 0,
        "transcribed": 0,
        "reused_indexes": 0,
        "source_failures": [],
        "total_segments": 0,
        "total": 0,
        "done": 0,
        "matched": 0,
        "review": 0,
        "unmatched": 0,
        "segment_failures": [],
        "llm_reviewed": 0,
        "llm_review_failures": [],
        "current_source_id": "",
        "current_segment_id": "",
        "transcription_processed_sec": 0.0,
        "transcription_duration_sec": 0.0,
        "transcription_words": 0,
        "transcription_source_started_at": "",
        "matching_started_at": "",
        "matching_fingerprint": "",
        "transcription_timeout_seconds": 0.0,
        "created_at": now,
        "started_at": "",
        "finished_at": "",
        "error": "",
    }
    with _SYNC_JOBS_LOCK:
        _SYNC_JOBS[job["id"]] = job
        _SYNC_CANCEL_EVENTS[job["id"]] = threading.Event()
        snapshot = dict(job)
    _persist_sync_job(snapshot)
    return snapshot


def _update_sync_job(job_id: str, **updates) -> dict | None:
    snapshot = update_job(_SYNC_JOBS_LOCK, _SYNC_JOBS, job_id, updates)
    if snapshot is None:
        return None
    _persist_sync_job(snapshot)
    return snapshot


def _get_sync_job(job_id: str) -> dict | None:
    return get_job(_SYNC_JOBS_LOCK, _SYNC_JOBS, job_id)


def _sync_cancel_event(job_id: str) -> threading.Event | None:
    with _SYNC_JOBS_LOCK:
        return _SYNC_CANCEL_EVENTS.get(job_id)


def _reserve_sync_project(project: str) -> bool:
    return reserve_project(_SYNC_JOBS_LOCK, _SYNC_ACTIVE_PROJECTS, project)


def _release_sync_project(project: str) -> None:
    release_project(_SYNC_JOBS_LOCK, _SYNC_ACTIVE_PROJECTS, project)


def _run_sync_job(
    *,
    job_id: str,
    project: str,
    backend: str | None,
    model: str | None,
    language: str,
    timecode_mode: str,
    rematch_confirmed: bool,
    source_ids: tuple[str, ...],
    force_transcribe: bool,
    search_window_sec: float,
) -> None:
    paths = _project_paths(project)
    source_store, match_store = _sync_stores(paths)
    cancel_event = _sync_cancel_event(job_id)

    def checkpoint_matches() -> None:
        return None

    def ensure_not_cancelled() -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise _JobCancelled("Sync job cancelled")

    try:
        ensure_not_cancelled()
        _update_sync_job(
            job_id,
            status="running",
            message="Preparing word indexes",
            started_at=_utc_now_iso(),
        )
        all_sources = source_store.list()
        # ``source_ids`` scopes forced transcription, not the matching pool.
        # This lets the CEP panel export one guide at a time while every
        # already-indexed source remains eligible for scenario matching.
        sources = all_sources
        indexes = []
        source_failures: list[dict[str, str]] = []
        transcribed = 0
        reused = 0
        _update_sync_job(job_id, total_sources=len(sources))

        for source in sources:
            ensure_not_cancelled()
            _update_sync_job(job_id, current_source_id=source.id)
            index_path = _word_index_path(paths, source.id)
            selected_for_transcription = not source_ids or source.id in source_ids
            cached_index = None
            audio_path = None
            try:
                should_force = force_transcribe and selected_for_transcription
                config = _transcription_config(
                    backend=backend, model=model, language=language
                )
                audio_resolution_error = None
                try:
                    audio_path = _resolve_sync_audio_path(source, paths)
                    audio_is_available = audio_path.is_file()
                except ValueError as exc:
                    audio_resolution_error = exc
                    audio_path = None
                    audio_is_available = False

                cache_load_error = None
                if index_path.is_file():
                    try:
                        cached_index = load_word_index(index_path, source.id)
                    except (OSError, TypeError, ValueError) as exc:
                        cache_load_error = exc
                if cached_index is None and not audio_is_available:
                    if cache_load_error is not None:
                        raise cache_load_error
                    if audio_resolution_error is not None:
                        raise audio_resolution_error

                # Preserve legacy indexes on their first read. We cannot know
                # whether an older file was generated or manually corrected,
                # so migrate its cache metadata without changing its words.
                if (
                    cached_index is not None
                    and audio_is_available
                    and audio_path is not None
                    and not isinstance(
                        cached_index.metadata.get(_TRANSCRIPTION_CACHE_KEY), dict
                    )
                ):
                    cached_index.metadata[_TRANSCRIPTION_CACHE_KEY] = (
                        _transcription_cache_metadata(
                            audio_path, config, origin="legacy"
                        )
                    )
                    save_word_index(index_path, cached_index)

                if (
                    cached_index is not None
                    and audio_is_available
                    and audio_path is not None
                ):
                    cache_metadata = cached_index.metadata.get(
                        _TRANSCRIPTION_CACHE_KEY
                    )
                    stored_audio = (
                        cache_metadata.get("audio")
                        if isinstance(cache_metadata, dict) else None
                    )
                    if (
                        isinstance(stored_audio, dict)
                        and not stored_audio.get("sample_sha256")
                        and stored_audio == _audio_stat_signature(audio_path)
                    ):
                        cache_metadata["audio"] = _audio_cache_signature(audio_path)
                        save_word_index(index_path, cached_index)

                cache_is_current = bool(
                    cached_index is not None
                    and audio_is_available
                    and audio_path is not None
                    and _word_index_cache_is_current(
                        cached_index, audio_path, config
                    )
                )
                if source_ids and not selected_for_transcription:
                    if cached_index is None:
                        if cache_load_error is not None:
                            raise cache_load_error
                        # Another registered sequence has not been indexed yet.
                        # It joins the matching pool after its own CEP run.
                        continue
                    # A scoped CEP job must never unexpectedly transcribe other
                    # sources. Keep their last good indexes in the matching pool;
                    # they will be refreshed when selected by a later job.
                    index = cached_index
                    reused += 1
                elif cached_index is not None and not should_force and (
                    cache_is_current or not audio_is_available
                ):
                    index = cached_index
                    reused += 1
                else:
                    if audio_path is None:
                        raise FileNotFoundError(
                            f"audio guide not found for sync source {source.id}"
                        )
                    source_duration = max(0.0, float(source.duration_sec or 0.0))
                    _update_sync_job(
                        job_id,
                        message="Transcribing audio",
                        transcription_processed_sec=0.0,
                        transcription_duration_sec=source_duration,
                        transcription_words=0,
                        transcription_source_started_at=_utc_now_iso(),
                        transcription_timeout_seconds=float(
                            app.config.get("SYNC_WHISPER_TIMEOUT_SECONDS")
                            or os.getenv("SYNC_WHISPER_TIMEOUT_SECONDS")
                            or 4 * 60 * 60
                        ),
                    )

                    def report_progress(
                        processed_sec: float,
                        duration_sec: float,
                        word_count: int,
                    ) -> None:
                        if cancel_event is not None and cancel_event.is_set():
                            return
                        _update_sync_job(
                            job_id,
                            transcription_processed_sec=processed_sec,
                            transcription_duration_sec=(duration_sec or source_duration),
                            transcription_words=word_count,
                        )

                    input_audio_signature = _audio_cache_signature(audio_path)
                    result = transcribe_audio(
                        audio_path,
                        backend=backend,
                        model=model,
                        language=language,
                        progress_callback=report_progress,
                        cancel_event=cancel_event,
                        timeout_seconds=(
                            app.config.get("SYNC_WHISPER_TIMEOUT_SECONDS")
                            or None
                        ),
                    )
                    if _audio_cache_signature(audio_path) != input_audio_signature:
                        raise RuntimeError(
                            f"audio guide changed during transcription: {audio_path.name}"
                        )
                    index = ingest_word_index(
                        result,
                        source.id,
                        model=str(result.get("model") or model or ""),
                        language=str(result.get("language") or language),
                    )
                    index.metadata[_TRANSCRIPTION_CACHE_KEY] = (
                        _transcription_cache_metadata(
                            audio_path,
                            config,
                            audio_signature=input_audio_signature,
                        )
                    )
                    save_word_index(index_path, index)
                    transcribed += 1
                indexes.append(index)
            except Exception as exc:
                if isinstance(exc, WhisperCancelledError) or (
                    cancel_event is not None and cancel_event.is_set()
                ):
                    raise _JobCancelled("Sync job cancelled") from exc
                if (
                    cached_index is not None
                    and audio_path is not None
                    and _word_index_audio_matches(cached_index, audio_path)
                ):
                    # A decoding/config refresh may fail temporarily. Keep the
                    # last transcript for matching when it still describes the
                    # exact same WAV; never fall back across an audio change.
                    indexes.append(cached_index)
                    reused += 1
                source_failures.append({"source_id": source.id, "error": str(exc)})
            _update_sync_job(
                job_id,
                transcribed=transcribed,
                reused_indexes=reused,
                source_failures=list(source_failures),
            )

        normalization = load_normalization(paths["normalized"])
        normalization_revision = normalization.get("revision")
        blocks = [
            block
            for block in sorted(
                normalization.get("blocks", []),
                key=lambda item: int(item.get("order", 0) or 0),
            )
            if str(block.get("type") or "").lower() in {"sync", "host"}
        ]
        units = _sync_alignment_units(blocks)
        indexed_source_ids = {item.source_id for item in indexes}
        matching_sources = [
            item for item in sources if item.id in indexed_source_ids
        ]
        matcher = SyncMatcher(
            matching_sources,
            indexes,
            MatcherConfig(search_window_sec=search_window_sec),
        )
        settings = get_settings()
        llm_review_enabled = (
            bool(settings.openrouter_api_key)
            and str(os.getenv("SYNC_LLM_REVIEW_ENABLED", "1")).strip().lower()
            not in {"0", "false", "no", "off"}
        )
        try:
            llm_review_threshold = float(
                os.getenv("SYNC_LLM_REVIEW_THRESHOLD", "0.80")
            )
        except ValueError:
            llm_review_threshold = 0.80
        llm_review_threshold = max(0.0, min(1.0, llm_review_threshold))
        try:
            llm_review_limit = max(
                0, int(os.getenv("SYNC_LLM_REVIEW_MAX_BLOCKS", "30"))
            )
        except ValueError:
            llm_review_limit = 30
        llm_reviewed = 0
        llm_review_failures: list[dict[str, str]] = []
        existing = {item.segment_id: item for item in match_store.list()}
        checkpoint_state = {"dirty": False}

        def checkpoint_matches() -> None:
            if not checkpoint_state["dirty"]:
                return
            with _SYNC_DATA_LOCK:
                persisted = {
                    item.segment_id: item for item in match_store.list()
                }
                # Manual edits made while matching was running are authoritative.
                for persisted_id, persisted_match in persisted.items():
                    if persisted_match.status == "confirmed" or persisted_match.metadata.get(
                        "manual_updated_at"
                    ):
                        existing[persisted_id] = persisted_match
                    elif persisted_id not in existing:
                        existing[persisted_id] = persisted_match
                match_store.replace_all(list(existing.values()))
                checkpoint_state["dirty"] = False
        current_ids = {
            str(unit.get("segment_id") or "").strip()
            for unit in units
            if str(unit.get("segment_id") or "").strip()
        }
        matching_fingerprint = _sync_matching_fingerprint(
            units=units,
            sources=matching_sources,
            indexes=indexes,
            normalization_revision=normalization_revision,
            timecode_mode=timecode_mode,
            search_window_sec=search_window_sec,
            llm_enabled=llm_review_enabled,
            llm_model=settings.openrouter_model,
            llm_threshold=llm_review_threshold,
            llm_limit=llm_review_limit,
        )
        total_segments = len(current_ids)
        _update_sync_job(
            job_id,
            message="Matching scenario quotes",
            total_segments=total_segments,
            total=total_segments,
            current_source_id="",
            matching_started_at=_utc_now_iso(),
            matching_fingerprint=matching_fingerprint,
        )

        segment_failures: list[dict[str, str]] = []
        processed = 0
        for unit in units:
            ensure_not_cancelled()
            segment_id = str(unit.get("segment_id") or "").strip()
            if not segment_id:
                segment_failures.append({
                    "segment_id": "",
                    "error": "sync/host block has no stable uid",
                })
                continue
            _update_sync_job(job_id, current_segment_id=segment_id)
            prior = existing.get(segment_id)
            prior_revision = (
                prior.metadata.get("normalization_revision") if prior else None
            )
            prior_is_current = (
                prior_revision in (None, "", normalization_revision)
            )
            prior_matching_fingerprint = (
                str(prior.metadata.get("matching_fingerprint") or "")
                if prior else ""
            )
            if (
                prior
                and prior_is_current
                and (
                    (
                        prior.status == "confirmed"
                        and not rematch_confirmed
                    )
                    or (
                        prior.status != "confirmed"
                        and prior_matching_fingerprint == matching_fingerprint
                    )
                )
            ):
                processed += 1
                current = [
                    item for key, item in existing.items()
                    if key in current_ids
                ]
                _update_sync_job(
                    job_id,
                    done=processed,
                    **_sync_summary(current),
                    segment_failures=list(segment_failures),
                )
                continue
            try:
                match = matcher.match(
                    AlignmentRequest(
                        segment_id=segment_id,
                        text=str(unit.get("text") or ""),
                        speaker=str(unit.get("speaker") or ""),
                        approximate_timecode=unit.get("timecode_start"),
                        timecode_mode=timecode_mode,  # type: ignore[arg-type]
                    )
                )
                semantic_candidates = _semantic_review_candidates(match)
                if (
                    llm_review_enabled
                    and llm_reviewed < llm_review_limit
                    and match.status != "auto_matched"
                    and match.confidence < llm_review_threshold
                    and len(normalize_text(match.query_text)) >= 5
                    and semantic_candidates
                ):
                    try:
                        decision = review_candidates_with_llm(
                            query_text=match.query_text,
                            speaker=match.speaker,
                            candidates=semantic_candidates,
                            settings=settings,
                        )
                        llm_reviewed += 1
                        if decision is not None:
                            match = _apply_semantic_review(
                                match,
                                semantic_candidates,
                                decision.candidate_index,
                                llm_confidence=decision.confidence,
                                reason=decision.reason,
                                model=decision.model,
                            )
                    except Exception as exc:
                        llm_reviewed += 1
                        llm_review_failures.append({
                            "segment_id": segment_id,
                            "error": str(exc),
                        })
                match.metadata["normalization_revision"] = normalization_revision
                match.metadata["matching_fingerprint"] = matching_fingerprint
                match.metadata["parent_block_uid"] = str(
                    unit.get("parent_block_uid") or segment_id
                )
                match.metadata["fragment_index"] = int(unit.get("fragment_index") or 0)
                match.metadata["fragment_count"] = int(unit.get("fragment_count") or 1)
                existing[segment_id] = match
                checkpoint_state["dirty"] = True
            except Exception as exc:
                segment_failures.append({
                    "segment_id": segment_id,
                    "error": str(exc),
                })
            processed += 1
            if processed % 10 == 0:
                checkpoint_matches()
            current = [
                item for key, item in existing.items() if key in current_ids
            ]
            _update_sync_job(
                job_id,
                done=processed,
                **_sync_summary(current),
                segment_failures=list(segment_failures),
                llm_reviewed=llm_reviewed,
                llm_review_failures=list(llm_review_failures),
            )

        checkpoint_matches()
        all_matches = [
            item for key, item in existing.items() if key in current_ids
        ]
        ensure_not_cancelled()
        summary = _sync_summary(all_matches)
        _update_sync_job(
            job_id,
            status="completed",
            message="Completed",
            finished_at=_utc_now_iso(),
            current_segment_id="",
            source_failures=source_failures,
            segment_failures=segment_failures,
            llm_reviewed=llm_reviewed,
            llm_review_failures=llm_review_failures,
            **summary,
        )
    except _JobCancelled:
        try:
            checkpoint_matches()
        except Exception:
            app.logger.exception("Could not checkpoint cancelled sync job=%s", job_id)
        _update_sync_job(
            job_id,
            status="cancelled",
            message="Cancelled; saved indexes and matches can be resumed",
            current_source_id="",
            current_segment_id="",
            finished_at=_utc_now_iso(),
        )
    except Exception as exc:
        try:
            checkpoint_matches()
        except Exception:
            app.logger.exception("Could not checkpoint failed sync job=%s", job_id)
        _update_sync_job(
            job_id,
            status="failed",
            message="Failed",
            error=str(exc),
            finished_at=_utc_now_iso(),
        )
    finally:
        _release_sync_project(project)
        with _SYNC_JOBS_LOCK:
            _SYNC_CANCEL_EVENTS.pop(job_id, None)


@app.route("/stage2/sync-sources", methods=["GET", "POST", "DELETE"])
def stage2_sync_sources():
    payload = request.get_json(silent=True) or {}
    raw_project = (
        request.args.get("project")
        if request.method == "GET"
        else payload.get("project")
    )
    try:
        project = _resolve_project_name(raw_project)
    except ValueError as exc:
        return _json_error(str(exc), 400)
    paths = _ensure_project(project)
    source_store, match_store = _sync_stores(paths)

    try:
        if request.method == "GET":
            sources = source_store.list()
            return jsonify({
                "ok": True,
                "result": {
                    "project": project,
                    "sources": [_source_api_dict(item) for item in sources],
                },
            })

        if request.method == "POST":
            source_payload = payload.get("source", payload)
            if not isinstance(source_payload, dict):
                return _json_error("source must be a JSON object", 400)
            source_payload = dict(source_payload)
            source_payload.pop("project", None)
            source_payload["id"] = _sync_source_id(source_payload.get("id"))
            existing = source_store.get(source_payload["id"])
            if (
                existing
                and existing.sequence_id
                and source_payload.get("sequence_id")
                and existing.sequence_id != str(source_payload["sequence_id"])
            ):
                return _json_error(
                    "source id already belongs to another Premiere sequence", 409
                )
            if existing:
                merged = existing.to_dict()
                incoming_metadata = dict(source_payload.get("metadata") or {})
                merged_metadata = dict(merged.get("metadata") or {})
                merged_metadata.update({
                    key: value for key, value in incoming_metadata.items()
                    if value not in (None, "")
                })
                merged.update(source_payload)
                # An older client may still send only the legacy singular
                # field. Do not let a previously saved list override that edit.
                if "speaker" in source_payload and "speakers" not in source_payload:
                    merged["speakers"] = []
                merged["metadata"] = merged_metadata
                if not str(source_payload.get("guide_audio") or "").strip():
                    merged["guide_audio"] = existing.guide_audio
                source_payload = merged
            source = SyncSource.from_dict(source_payload)
            with _SYNC_DATA_LOCK:
                source_store.save(source)
            return jsonify({
                "ok": True,
                "result": {"project": project, "source": _source_api_dict(source)},
            }), 201

        source_id = _sync_source_id(
            payload.get("source_id") or request.args.get("source_id")
        )
        with _SYNC_DATA_LOCK:
            deleted = source_store.delete(source_id)
            if deleted:
                remaining_matches = [
                    item for item in match_store.list() if item.source_id != source_id
                ]
                match_store.replace_all(remaining_matches)
        if not deleted:
            return _json_error("sync source not found", 404)
        # Regenerable artifacts are removed with the source record. These are
        # exact server-owned paths and never derived from an uploaded filename.
        for artifact in (
            _canonical_audio_path(paths, source_id),
            _word_index_path(paths, source_id),
        ):
            artifact.unlink(missing_ok=True)
        return jsonify({
            "ok": True,
            "result": {"project": project, "source_id": source_id, "deleted": True},
        })
    except (TypeError, ValueError) as exc:
        return _json_error(str(exc), 400)
    except Exception as exc:
        return _internal_error(exc)


@app.route("/stage2/sync-sources/<source_id>/audio", methods=["GET", "PUT"])
def stage2_sync_source_audio(source_id: str):
    try:
        source_id = _sync_source_id(source_id)
        project = _resolve_project_name(
            request.args.get("project")
            if request.method == "GET"
            else request.form.get("project")
        )
    except ValueError as exc:
        return _json_error(str(exc), 400)
    paths = _ensure_project(project)
    source_store, _match_store = _sync_stores(paths)
    try:
        source = source_store.get(source_id)
    except ValueError as exc:
        return _json_error(str(exc), 400)
    if source is None:
        return _json_error("sync source not found", 404)
    if request.method == "GET":
        try:
            audio_path = _resolve_sync_audio_path(source, paths)
        except ValueError as exc:
            return _json_error(str(exc), 400)
        if not audio_path.is_file():
            return _json_error("sync guide audio not found", 404)
        return send_from_directory(
            audio_path.parent, audio_path.name, as_attachment=False
        )
    uploaded = request.files.get("audio") or request.files.get("file")
    if uploaded is None or not uploaded.filename:
        return _json_error("multipart WAV field 'audio' is required", 400)
    max_bytes = int(
        app.config.get("SYNC_AUDIO_MAX_BYTES")
        or os.getenv("SYNC_AUDIO_MAX_BYTES")
        or 4 * 1024 * 1024 * 1024
    )
    if request.content_length is not None and request.content_length > max_bytes:
        return _json_error("audio upload is too large", 413)
    if Path(uploaded.filename).suffix.lower() != ".wav":
        return _json_error("audio must be a WAV file", 415)

    destination = _canonical_audio_path(paths, source_id)
    _cleanup_stale_uploads(destination.parent)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.upload")
    duration = 0.0
    try:
        _copy_stream_limited(uploaded.stream, temporary, max_bytes)
        with wave.open(str(temporary), "rb") as stream:
            frames = stream.getnframes()
            rate = stream.getframerate()
            if rate <= 0 or stream.getnchannels() <= 0:
                raise ValueError("invalid WAV stream")
            duration = frames / rate
        os.replace(temporary, destination)
        updated_payload = source.to_dict()
        updated_payload["guide_audio"] = str(destination.relative_to(paths["root"]))
        if not float(updated_payload.get("duration_sec") or 0):
            updated_payload["duration_sec"] = duration
        updated = SyncSource.from_dict(updated_payload)
        with _SYNC_DATA_LOCK:
            source_store.save(updated)
        # Keep the previous regenerable transcript until a replacement has
        # been produced successfully. Its audio signature makes it stale for
        # the next job, while preserving recovery data if transcription fails.
    except AudioUploadTooLargeError as exc:
        temporary.unlink(missing_ok=True)
        return _json_error(str(exc), 413)
    except (EOFError, wave.Error, ValueError) as exc:
        temporary.unlink(missing_ok=True)
        return _json_error(f"invalid WAV file: {exc}", 415)
    except Exception as exc:
        temporary.unlink(missing_ok=True)
        return _internal_error(exc)
    return jsonify({
        "ok": True,
        "result": {
            "project": project,
            "source": _source_api_dict(updated),
            "audio_path": str(destination.relative_to(ROOT_DIR)),
            "duration_sec": duration,
        },
    })


@app.put("/stage2/sync-sources/<source_id>/words")
def stage2_sync_source_words(source_id: str):
    payload = request.get_json(silent=True)
    if payload is None:
        return _json_error("JSON word timestamp payload is required", 400)
    try:
        source_id = _sync_source_id(source_id)
        project = _resolve_project_name(
            payload.get("project") if isinstance(payload, dict) else request.args.get("project")
        )
    except ValueError as exc:
        return _json_error(str(exc), 400)
    paths = _ensure_project(project)
    source_store, _match_store = _sync_stores(paths)
    try:
        source = source_store.get(source_id)
        if source is None:
            return _json_error("sync source not found", 404)
        transcript_payload = dict(payload) if isinstance(payload, dict) else payload
        if isinstance(transcript_payload, dict):
            transcript_payload.pop("project", None)
        index = ingest_word_index(transcript_payload, source_id)
        try:
            audio_path = _resolve_sync_audio_path(source, paths)
        except ValueError:
            audio_path = None
        if audio_path is not None and audio_path.is_file():
            config = _transcription_config(
                backend=None,
                model=index.model or None,
                language=index.language,
            )
            index.metadata[_TRANSCRIPTION_CACHE_KEY] = (
                _transcription_cache_metadata(audio_path, config, origin="manual")
            )
        index_path = _word_index_path(paths, source_id)
        save_word_index(index_path, index)
    except (TypeError, ValueError) as exc:
        return _json_error(str(exc), 400)
    except Exception as exc:
        return _internal_error(exc)
    return jsonify({
        "ok": True,
        "result": {
            "project": project,
            "source_id": source_id,
            "word_count": len(index.words),
            "duration_sec": index.duration_sec,
            "words_path": str(index_path.relative_to(ROOT_DIR)),
        },
    })


@app.post("/stage2/sync-jobs/start")
def stage2_sync_job_start():
    payload = request.get_json(silent=True) or {}
    try:
        project = _resolve_project_name(payload.get("project"))
    except ValueError as exc:
        return _json_error(str(exc), 400)
    paths = _ensure_project(project)
    gate = _normalization_required(project)
    if gate:
        return gate
    timecode_mode = str(payload.get("timecode_mode") or "relative").strip().lower()
    if timecode_mode not in {"relative", "display"}:
        return _json_error("timecode_mode must be 'relative' or 'display'", 400)
    language = str(payload.get("language") or "ru").strip()
    if not language:
        return _json_error("language must not be empty", 400)
    try:
        sources = SyncSourceStore(paths["sync_sources"]).list()
    except ValueError as exc:
        return _json_error(str(exc), 400)
    if not sources:
        return _json_error("no sync sources configured", 409)
    raw_source_ids = payload.get("source_ids") or []
    if not isinstance(raw_source_ids, list):
        return _json_error("source_ids must be a JSON list", 400)
    try:
        source_ids = tuple(_sync_source_id(item) for item in raw_source_ids)
    except ValueError as exc:
        return _json_error(str(exc), 400)
    known_source_ids = {item.id for item in sources}
    unknown_source_ids = sorted(set(source_ids) - known_source_ids)
    if unknown_source_ids:
        return _json_error(
            f"unknown sync sources: {', '.join(unknown_source_ids)}", 400
        )
    try:
        search_window_sec = float(payload.get("search_window_sec", 180.0))
    except (TypeError, ValueError):
        return _json_error("search_window_sec must be a positive number", 400)
    if search_window_sec <= 0:
        return _json_error("search_window_sec must be a positive number", 400)
    if not _reserve_sync_project(project):
        return _json_error(f"sync job already active for project {project}", 409)

    try:
        job = _create_sync_job(project)
        worker = threading.Thread(
            target=_run_sync_job,
            kwargs={
                "job_id": job["id"],
                "project": project,
                "backend": (
                    str(payload.get("backend")).strip()
                    if payload.get("backend") is not None
                    else None
                ),
                "model": (
                    str(payload.get("model")).strip()
                    if payload.get("model") is not None
                    else None
                ),
                "language": language,
                "timecode_mode": timecode_mode,
                "rematch_confirmed": bool(payload.get("rematch_confirmed", False)),
                "source_ids": source_ids,
                "force_transcribe": bool(payload.get("force_transcribe", False)),
                "search_window_sec": search_window_sec,
            },
            daemon=True,
        )
        worker.start()
    except Exception:
        _release_sync_project(project)
        raise
    return jsonify({
        "ok": True,
        "result": {
            "project": project,
            "job_id": job["id"],
            "status": "queued",
            "status_url": f"/stage2/sync-jobs/{job['id']}",
        },
    })


@app.get("/stage2/sync-jobs/<job_id>")
def stage2_sync_job_status(job_id: str):
    job = _get_sync_job(job_id)
    if not job:
        return _json_error("job not found", 404)
    return jsonify({"ok": True, "result": _sync_job_with_live_summary(job)})


@app.post("/stage2/sync-jobs/<job_id>/cancel")
def stage2_sync_job_cancel(job_id: str):
    with _SYNC_JOBS_LOCK:
        job = _SYNC_JOBS.get(job_id)
        if job is None:
            return _json_error("job not found", 404)
        if job.get("status") not in {"queued", "running", "cancelling"}:
            return _json_error("job is not active", 409)
        event = _SYNC_CANCEL_EVENTS.get(job_id)
        if event is None:
            return _json_error("job worker is not active", 409)
        event.set()
        job.update({
            "status": "cancelling",
            "message": "Cancellation requested; saving completed work",
        })
        snapshot = dict(job)
    _persist_sync_job(snapshot)
    return jsonify({"ok": True, "result": snapshot}), 202


@app.get("/stage2/sync-jobs/status")
def stage2_sync_job_status_alias():
    job_id = str(request.args.get("job_id") or "").strip()
    if job_id:
        return stage2_sync_job_status(job_id)
    try:
        project = _resolve_project_name(request.args.get("project"))
    except ValueError as exc:
        return _json_error(str(exc), 400)
    with _SYNC_JOBS_LOCK:
        candidates = [
            dict(item) for item in _SYNC_JOBS.values() if item.get("project") == project
        ]
    if not candidates:
        persisted = _load_persisted_sync_job(project)
        if persisted is None:
            return _json_error("job not found", 404)
        return jsonify({
            "ok": True,
            "result": _sync_job_with_live_summary(persisted),
        })
    latest = max(candidates, key=lambda item: str(item.get("created_at") or ""))
    return jsonify({"ok": True, "result": _sync_job_with_live_summary(latest)})


@app.route("/stage2/sync-matches", methods=["GET", "PATCH"])
@app.patch("/stage2/sync-matches/<block_uid>")
def stage2_sync_matches(block_uid: str | None = None):
    payload = request.get_json(silent=True) or {}
    raw_project = (
        request.args.get("project")
        if request.method == "GET"
        else payload.get("project")
    )
    try:
        project = _resolve_project_name(raw_project)
    except ValueError as exc:
        return _json_error(str(exc), 400)
    paths = _ensure_project(project)
    source_store, match_store = _sync_stores(paths)
    try:
        if request.method == "GET":
            matches = match_store.list()
            confirmed, _reason = _confirmed_normalization(project)
            normalization_document = confirmed or _read_json_safe(paths["normalized"])
            if isinstance(normalization_document.get("blocks"), list):
                confirmed_blocks = [
                    block for block in normalization_document.get("blocks", [])
                    if str(block.get("type") or "").lower() in {"sync", "host"}
                ]
                valid_ids = {
                    str(unit.get("segment_id") or "")
                    for unit in _sync_alignment_units(confirmed_blocks)
                    if str(unit.get("segment_id") or "")
                }
                revision = normalization_document.get("revision")
                matches = [
                    item for item in matches
                    if item.segment_id in valid_ids
                    and item.metadata.get("normalization_revision") in (None, "", revision)
                ]
            return jsonify({
                "ok": True,
                "result": {
                    "project": project,
                    "matches": [_match_api_dict(item) for item in matches],
                    **_sync_summary(matches),
                },
            })

        updates = payload.get("matches")
        if updates is None:
            updates = [payload.get("match", payload)]
        if not isinstance(updates, list) or not all(
            isinstance(item, dict) for item in updates
        ):
            return _json_error("matches must be a JSON list of objects", 400)
        confirmed, _reason = _confirmed_normalization(project)
        if confirmed is None:
            gate = _normalization_required(project)
            if gate:
                return gate
        assert confirmed is not None
        confirmed_blocks = [
            block for block in confirmed.get("blocks", [])
            if str(block.get("type") or "").lower() in {"sync", "host"}
        ]
        valid_units = {
            str(unit.get("segment_id") or ""): unit
            for unit in _sync_alignment_units(confirmed_blocks)
            if str(unit.get("segment_id") or "")
        }
        sources_by_id = {item.id: item for item in source_store.list()}
        source_ids = set(sources_by_id)
        revision = confirmed.get("revision")
        saved: list[SyncMatch] = []
        with _SYNC_DATA_LOCK:
            stored_by_id = {
                item.segment_id: item for item in match_store.list()
            }
            for update in updates:
                values = dict(update)
                values.pop("project", None)
                segment_id = str(
                    block_uid
                    or values.get("block_uid")
                    or values.get("segment_id")
                    or ""
                ).strip()
                if not segment_id:
                    raise ValueError("block_uid (segment_id) is required")
                if segment_id not in valid_units:
                    raise ValueError(
                        f"segment_id is not a confirmed sync/host fragment: {segment_id}"
                    )
                supplied_segment_id = str(values.get("segment_id") or segment_id).strip()
                if supplied_segment_id != segment_id:
                    raise ValueError("segment_id must match block_uid path")
                values.pop("block_uid", None)
                values["segment_id"] = segment_id
                current = stored_by_id.get(segment_id)
                merged = current.to_dict() if current else {
                    "segment_id": segment_id,
                    "query_text": "",
                }
                merged.update(values)
                metadata = dict(merged.get("metadata") or {})
                metadata["normalization_revision"] = revision
                unit = valid_units[segment_id]
                metadata["parent_block_uid"] = str(
                    unit.get("parent_block_uid") or segment_id
                )
                metadata["fragment_index"] = int(unit.get("fragment_index") or 0)
                metadata["fragment_count"] = int(unit.get("fragment_count") or 1)
                metadata["manual_updated_at"] = _utc_now_iso()
                merged["metadata"] = metadata
                source_id = str(merged.get("source_id") or "")
                if source_id and source_id not in source_ids:
                    raise ValueError(f"unknown sync source: {source_id}")
                match = SyncMatch.from_dict(merged)
                source = sources_by_id.get(match.source_id)
                if (
                    source
                    and source.duration_sec > 0
                    and match.source_out_sec is not None
                    and match.source_out_sec > source.duration_sec
                ):
                    raise ValueError(
                        f"source_out_sec exceeds source duration ({source.duration_sec})"
                    )
                stored_by_id[match.segment_id] = match
                saved.append(match)
            match_store.replace_all(list(stored_by_id.values()))
        return jsonify({
            "ok": True,
            "result": {
                "project": project,
                "matches": [_match_api_dict(item) for item in saved],
            },
        })
    except (TypeError, ValueError) as exc:
        return _json_error(str(exc), 400)
    except Exception as exc:
        return _internal_error(exc)


@app.get("/stage2/timeline")
def stage2_timeline():
    try:
        project = _resolve_project_name(request.args.get("project"))
    except ValueError as exc:
        return _json_error(str(exc), 400)
    paths = _project_paths(project)
    timeline_path = paths["timeline"]
    if not timeline_path.is_file():
        return _json_error("full_timeline.json not found — run Build first", 404)
    try:
        timeline = json.loads(timeline_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return _json_error(f"invalid full_timeline.json: {exc}", 500)
    confirmed, _reason = _confirmed_normalization(project)
    if (
        confirmed is None
        or timeline.get("normalization_revision") != confirmed.get("revision")
    ):
        return _json_error(
            "timeline_rebuild_required: нормализация изменилась после сборки таймлайна",
            409,
        )
    return jsonify({"ok": True, "result": timeline})


@app.post("/stage2/build")
def stage2_build():
    """Build full_timeline.json from confirmed normalization + speaker WAVs."""
    payload = request.get_json(silent=True) or {}

    try:
        project = _resolve_project_name(payload.get("project"))
    except ValueError as exc:
        return _json_error(str(exc), 400)

    with _SYNC_JOBS_LOCK:
        sync_job_active = project in _SYNC_ACTIVE_PROJECTS
    if sync_job_active:
        return _json_error(
            "sync_job_in_progress: дождитесь завершения сопоставления СНХ",
            409,
        )
    persisted_job = _load_persisted_sync_job(project)
    if persisted_job and persisted_job.get("status") in {"interrupted", "cancelled"}:
        return _json_error(
            "sync_job_interrupted: перезапустите сопоставление СНХ, чтобы продолжить с сохранённой контрольной точки",
            409,
        )

    paths = _ensure_project(project)
    gate = _normalization_required(project)
    if gate:
        return gate
    try:
        speaker_dir = _abs_path(payload.get("speaker_dir", ""), paths["speaker"], paths["root"])
    except ValueError as exc:
        return _json_error(str(exc), 400)
    placeholder_dir = paths["work"] / "placeholders"
    try:
        fps = int(payload.get("fps", 25))
    except (TypeError, ValueError):
        return _json_error("fps must be an integer", 400)
    if fps < 1:
        return _json_error("fps must be at least 1", 400)

    try:
        normalization = load_normalization(paths["normalized"])
        source_store, match_store = _sync_stores(paths)
        sources = source_store.list()
        stored_matches = match_store.list()
        normalization_revision = normalization.get("revision")
        stale_match_ids: list[str] = []
        current_matches: list[SyncMatch] = []
        for match in stored_matches:
            match_revision = match.metadata.get("normalization_revision")
            if match_revision not in (None, "", normalization_revision):
                stale_match_ids.append(match.segment_id)
                continue
            current_matches.append(match)
        timeline = parse_normalized_timeline(
            normalization=normalization,
            speaker_dir=speaker_dir,
            placeholder_dir=placeholder_dir,
            project=project,
            fps=fps,
            sync_matches={item.segment_id: item for item in current_matches},
            sync_sources={item.id: item for item in sources},
        )
    except ValueError as exc:
        return _json_error(str(exc), 400)
    except Exception as exc:
        return _internal_error(exc)

    # Generate placeholder PNGs for speaker and presenter segments.
    placeholder_warnings: list[str] = []
    for seg in timeline.segments:
        if seg.type in {"snh", "host"} and seg.placeholder_path:
            from pathlib import Path as _Path
            ph = _Path(seg.placeholder_path)
            if not ph.exists():
                try:
                    generate_placeholder(
                        seg_id=seg.id,
                        speaker=seg.speaker,
                        role=seg.role,
                        timecode=seg.timecode,
                        text=seg.text,
                        out_path=ph,
                        width=timeline.width,
                        height=timeline.height,
                    )
                except Exception as _ph_exc:
                    placeholder_warnings.append(
                        f"Placeholder не создан для {seg.id}: {_ph_exc}"
                    )
                    seg.placeholder_path = ""

    # Save full_timeline.json
    timeline_path = paths["work"] / "full_timeline.json"
    timeline_payload = timeline.to_dict()
    timeline_payload["normalization_revision"] = normalization.get("revision")
    timeline_path.write_text(
        __import__("json").dumps(timeline_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    zk_count = sum(1 for s in timeline.segments if s.type == "zk")
    snh_count = sum(1 for s in timeline.segments if s.type == "snh")
    host_count = sum(1 for s in timeline.segments if s.type == "host")
    tech_count = sum(1 for s in timeline.segments if s.type == "tech")
    zk_missing_wav = [s.id for s in timeline.segments if s.type == "zk" and not s.wav_path]
    sync_segments = [s for s in timeline.segments if s.type in {"snh", "host"}]
    matched_count = sum(
        s.match_status in {"auto_matched", "confirmed"}
        and s.source_out_sec > s.source_in_sec
        for s in sync_segments
    )
    review_count = sum(s.match_status == "needs_review" for s in sync_segments)
    unmatched_count = len(sync_segments) - matched_count - review_count

    return jsonify({
        "ok": True,
        "result": {
            "project": project,
            "segments": len(timeline.segments),
            "zk": zk_count,
            "snh": snh_count,
            "host": host_count,
            "tech": tech_count,
            "total_duration_sec": timeline.total_duration_sec,
            "timeline_path": str(timeline_path.relative_to(ROOT_DIR)),
            "matched": matched_count,
            "review": review_count,
            "unmatched": unmatched_count,
            "sync_summary": {
                "matched": matched_count,
                "review": review_count,
                "unmatched": unmatched_count,
            },
            "warnings": (
                ([f"ZK без WAV (будут пропущены в XML): {', '.join(zk_missing_wav)}"] if zk_missing_wav else [])
                + (
                    [f"Устаревшие sync matches пропущены: {', '.join(stale_match_ids)}"]
                    if stale_match_ids else []
                )
                + placeholder_warnings
            ),
        },
    })


@app.get("/stage2/export/xml")
def stage2_export_xml():
    """Build and download FCP7 XML for Premiere Pro."""
    project_name = request.args.get("project", "")
    try:
        project = _resolve_project_name(project_name)
    except ValueError as exc:
        return _json_error(str(exc), 400)

    paths = _project_paths(project)
    gate = _normalization_required(project)
    if gate:
        return gate
    timeline_path = paths["work"] / "full_timeline.json"
    if not timeline_path.exists():
        return _json_error("full_timeline.json not found — run Build first", 404)

    data = json.loads(timeline_path.read_text(encoding="utf-8"))
    confirmed, _reason = _confirmed_normalization(project)
    if not confirmed or data.get("normalization_revision") != confirmed.get("revision"):
        return _json_error(
            "timeline_rebuild_required: нормализация изменилась после сборки таймлайна",
            409,
        )

    from stage2.models import FullTimeline
    try:
        tl = FullTimeline.from_dict(data)
    except (TypeError, ValueError) as exc:
        return _json_error(f"invalid full_timeline.json: {exc}", 500)

    try:
        xml_str = build_xml(tl)
    except Exception as exc:
        return _internal_error(exc)

    from flask import Response
    return Response(
        xml_str,
        mimetype="application/xml",
        headers={"Content-Disposition": f'attachment; filename="{project}_timeline.xml"'},
    )
