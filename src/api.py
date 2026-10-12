from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, datetime
import hmac
import hashlib
from importlib.metadata import PackageNotFoundError, version as package_version
import json
import os
from pathlib import Path
import platform
import re
import sys
import threading
import time
from typing import BinaryIO, cast
import uuid
import weakref

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

from flask import Flask, Request, jsonify, request
from werkzeug.exceptions import RequestEntityTooLarge

from api_core_routes import register_core_routes
import api_final_review_jobs as final_review_jobs
import api_stage1_jobs as stage1_jobs
from api_service_contracts import (
    FinalReviewJobServices,
    NormalizationJobServices,
    SyncWorkerServices,
    TtsJobServices,
)
from api_stage1_routes import register_stage1_routes
from api_stage2_routes import register_stage2_routes
from api_final_review_routes import register_final_review_routes
from api_stage3_routes import register_stage3_routes
from api_stage4_routes import register_stage4_routes
from api_sync_worker import run_sync_job
from atomic_io import atomic_write_json
from auth_throttle import FailedAuthThrottle
from job_runtime import (
    JobRegistry,
    acquire_slot,
    get_job,
    release_project,
    reserve_project,
    update_job,
)
from openrouter_usage import make_usage_callback, reconcile_pending, usage_summary
from inbox_service import AUDIO_EXTENSIONS, discover_project_inbox
from logging_config import configure_logging
from project_service import (
    PROJECT_NAME_RE,
    discover_input_files as discover_project_input_files,
    ensure_project as ensure_project_layout,
    list_projects as list_project_directories,
    normalize_project_name as validate_project_name,
    project_paths as build_project_paths,
    read_json_safe as read_project_json_safe,
    resolve_path as resolve_project_path,
    resolve_project_name as select_project_name,
)

# Extracted route and worker modules resolve the symbols below through this
# compatibility facade at call time. Tests and integrations also patch them on
# ``api``, so they are intentionally retained without direct local references.
from stage1.analyzer import analyze_script
from stage1.config import get_settings, validate_env, warning_variable_names
from stage1.gemini_tts import (
    TtsCancelledError,
    normalize_openrouter_tts_model,
    normalize_tts_style,
    synthesize_phrase,
    synthesize_tts,
)
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
    normalize_llm_preset,
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
from stage2.placeholder import generate_placeholder, generate_versioned_placeholder
from stage2.sync_llm_review import SemanticCandidate, review_candidates_with_llm
from stage2.sync_matcher import MatcherConfig, SyncMatcher, normalize_text
from stage2.sync_models import (
    AlignmentRequest,
    SyncAlternative,
    SyncMatch,
    SyncSource,
    WordIndex,
)
from stage2.models import FullTimeline
from stage2.final_review import review_rendered_timeline
from stage2.assembly_manifest import (
    build_assembly_manifest,
    attach_scenario_revision,
    validate_assembly_manifest,
)
from stage2.sync_storage import SyncMatchStore, SyncSourceStore
from stage2.sync_transcript import (
    ingest_word_index,
    load_word_index,
    rescale_stretched_word_index,
    save_word_index,
)
from stage2.whisper_process import (
    WhisperCancelledError,
    transcribe_audio_in_process as transcribe_audio,
)
from stage2.xml_export import build_xml
from stage4.revision_assets import (
    revision_speaker_alias_dir,
    save_tts_manifest as save_voiceover_tts_manifest,
)
from stage4.versioning import inspect_revision_state, repair_active_revision_metadata


_DEFAULT_SYNC_AUDIO_MAX_BYTES = 4 * 1024 * 1024 * 1024
_REQUEST_BODY_MULTIPART_HEADROOM = 16 * 1024 * 1024
_DEFAULT_JSON_BODY_MAX_BYTES = 16 * 1024 * 1024
_DEFAULT_HEAVY_JOBS_MAX = 2


def _positive_env_int(name: str, default: int) -> int:
    try:
        value = int(str(os.getenv(name, default)).strip())
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _non_negative_env_int(name: str, default: int) -> int:
    try:
        value = int(str(os.getenv(name, default)).strip())
    except (TypeError, ValueError):
        return default
    return value if value >= 0 else default


def _request_body_limit() -> int:
    """Return the body limit for WAV/media upload endpoints only.

    ``SYNC_AUDIO_MAX_BYTES`` limits WAV bytes after multipart parsing. Flask's
    request limit must leave room for multipart metadata, otherwise a valid
    maximum-size audio upload would be rejected before the streaming guard.
    """

    sync_audio_limit = _positive_env_int("SYNC_AUDIO_MAX_BYTES", _DEFAULT_SYNC_AUDIO_MAX_BYTES)
    minimum = sync_audio_limit + _REQUEST_BODY_MULTIPART_HEADROOM
    configured = _positive_env_int("ROUGH_CUT_MAX_REQUEST_BODY_BYTES", minimum)
    return max(configured, minimum)


def _default_request_body_limit() -> int:
    """Return the (small) body limit applied to every non-upload request."""

    return _positive_env_int("ROUGH_CUT_MAX_JSON_BODY_BYTES", _DEFAULT_JSON_BODY_MAX_BYTES)


def _heavy_job_limit() -> int:
    """Return the cross-project cap for normalization and cloud-ASR jobs."""

    return _positive_env_int("ROUGH_CUT_MAX_HEAVY_JOBS", _DEFAULT_HEAVY_JOBS_MAX)


# Endpoints that legitimately receive large binary bodies. Everything else
# (JSON APIs) is held to ``_default_request_body_limit``.
_UPLOAD_ENDPOINTS = frozenset({"stage2_sync_source_audio"})


class _LimitedRequest(Request):
    """Apply the large body limit only to upload endpoints."""

    # Explicit per-request override. Stored locally so the setter works on both
    # Flask 3.0 (read-only base property) and Flask 3.1 (read-write base property).
    _explicit_max_content_length: int | None = None

    @property
    def max_content_length(self) -> int | None:
        if self._explicit_max_content_length is not None:
            return self._explicit_max_content_length
        if self.method in {"PUT", "POST"} and self.endpoint in _UPLOAD_ENDPOINTS:
            return _request_body_limit()
        return super().max_content_length

    @max_content_length.setter
    def max_content_length(self, value: int | None) -> None:
        self._explicit_max_content_length = value


configure_logging()
app = Flask(__name__)
app.request_class = _LimitedRequest
# UI modules import one another. Revalidation prevents a browser from
# combining a fresh entry module with a cached older dependency after deploy.
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0
app.config["MAX_CONTENT_LENGTH"] = _default_request_body_limit()
_CONFIG_WARNINGS = validate_env()
for _warning in _CONFIG_WARNINGS:
    app.logger.warning("Configuration: %s", _warning)
ROOT_DIR = Path(__file__).resolve().parent.parent
PROJECTS_DIR = ROOT_DIR / "projects"
NORMALIZATION_MEMORY_PATH = ROOT_DIR / "data" / "normalization_memory.sqlite3"
SYNC_SOURCE_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,96}$")


@dataclass(frozen=True)
class ActiveJobRegistry:
    """A named lock + in-progress-project set guarding project mutations.

    Route modules register their own job registries here explicitly instead of
    reaching into ``api``'s (or another module's) globals at runtime, so the
    active-mutation contract is statically visible and cannot silently depend
    on blueprint registration order.
    """

    label: str
    lock: threading.Lock | threading.RLock
    active_projects: set[str]


_JOB_REGISTRIES: dict[str, ActiveJobRegistry] = {}


def register_job_registry(
    label: str, lock: threading.Lock | threading.RLock, active_projects: set[str]
) -> None:
    """Register (or replace) the job registry published under ``label``.

    Re-registering an already-known label replaces its entry in place instead
    of appending a duplicate, so priority order among registries is preserved.
    """

    _JOB_REGISTRIES[label] = ActiveJobRegistry(label, lock, active_projects)


def iter_job_registries() -> tuple[ActiveJobRegistry, ...]:
    """Return all registered job registries in registration order."""

    return tuple(_JOB_REGISTRIES.values())


_TTS_RUNTIME = JobRegistry()
_TTS_JOBS = _TTS_RUNTIME.jobs
_TTS_JOBS_LOCK = _TTS_RUNTIME.lock
_TTS_JOBS_MAX = 200
_TTS_JOBS_TTL_SECONDS = 24 * 60 * 60
_TTS_CONCURRENT_MAX = 3
_TTS_SEMAPHORE = threading.Semaphore(_TTS_CONCURRENT_MAX)
# Cross-project cap for normalization and cloud-ASR jobs. Like the TTS slot,
# an over-limit job stays queued (polling for the slot) instead of failing.
_HEAVY_JOB_SEMAPHORE = threading.Semaphore(_heavy_job_limit())
_TTS_ACTIVE_PROJECTS = _TTS_RUNTIME.active_projects
_TTS_CANCEL_EVENTS = _TTS_RUNTIME.cancel_events
_SOURCE_HASH_CACHE_MAX = 256
_SOURCE_HASH_CACHE: OrderedDict[str, tuple[int, int, str]] = OrderedDict()
_SOURCE_HASH_CACHE_LOCK = threading.Lock()
_SYNC_RUNTIME = JobRegistry()
_SYNC_JOBS = _SYNC_RUNTIME.jobs
_SYNC_JOBS_LOCK = _SYNC_RUNTIME.lock
_SYNC_JOBS_MAX = 200
_SYNC_JOBS_TTL_SECONDS = 24 * 60 * 60
_SYNC_ACTIVE_PROJECTS = _SYNC_RUNTIME.active_projects
_SYNC_CANCEL_EVENTS = _SYNC_RUNTIME.cancel_events
_SYNC_DATA_LOCK = threading.Lock()
_FINAL_REVIEW_RUNTIME = JobRegistry()
_FINAL_REVIEW_JOBS = _FINAL_REVIEW_RUNTIME.jobs
_FINAL_REVIEW_JOBS_LOCK = _FINAL_REVIEW_RUNTIME.lock
_FINAL_REVIEW_ACTIVE_PROJECTS = _FINAL_REVIEW_RUNTIME.active_projects
_NORMALIZATION_RUNTIME = JobRegistry()
_NORMALIZATION_JOBS = _NORMALIZATION_RUNTIME.jobs
_NORMALIZATION_JOBS_LOCK = _NORMALIZATION_RUNTIME.lock
_NORMALIZATION_ACTIVE_PROJECTS = _NORMALIZATION_RUNTIME.active_projects
_NORMALIZATION_JOBS_MAX = 50
# Weak values: an entry lives only while some caller holds or waits on the lock
# (``with _project_transition_lock(p):`` keeps a strong reference for the whole
# block), so an in-use lock can never be recreated, while idle entries vanish.
_PROJECT_TRANSITION_LOCKS: weakref.WeakValueDictionary[str, threading.RLock] = (
    weakref.WeakValueDictionary()
)
_PROJECT_TRANSITION_LOCKS_GUARD = threading.Lock()

# Priority order matters: the first matching registry's label is reported to
# callers (e.g. ``api_stage4_routes._active_project_job``). Stage 3 registers
# itself later, via ``register_stage3_routes``, so it is appended last.
register_job_registry("озвучки", _TTS_JOBS_LOCK, _TTS_ACTIVE_PROJECTS)
register_job_registry("нормализации", _NORMALIZATION_JOBS_LOCK, _NORMALIZATION_ACTIVE_PROJECTS)
register_job_registry("сопоставления СНХ", _SYNC_JOBS_LOCK, _SYNC_ACTIVE_PROJECTS)
register_job_registry("финальной проверки", _FINAL_REVIEW_JOBS_LOCK, _FINAL_REVIEW_ACTIVE_PROJECTS)


class AudioUploadTooLargeError(ValueError):
    """Raised after the actual uploaded stream exceeds its byte limit."""


class _JobCancelled(RuntimeError):
    """Internal control-flow signal for cooperative background-job stops."""


def _project_transition_lock(project: str) -> threading.RLock:
    """Serialize job reservations with scenario-version transitions per project."""

    with _PROJECT_TRANSITION_LOCKS_GUARD:
        lock = _PROJECT_TRANSITION_LOCKS.get(project)
        if lock is None:
            lock = threading.RLock()
            _PROJECT_TRANSITION_LOCKS[project] = lock
        return lock


def _project_has_active_mutation(project: str) -> bool:
    for registry in iter_job_registries():
        with registry.lock:
            if project in registry.active_projects:
                return True
    return False


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
        "1",
        "true",
        "yes",
        "on",
    }


def _cep_cors_origin() -> str:
    if not _cep_cors_enabled():
        return ""
    origin = request.headers.get("Origin", "").strip()
    return origin if origin in {"null", "file://"} else ""


_AUTH_THROTTLE = FailedAuthThrottle(
    max_failures=_non_negative_env_int("ROUGH_CUT_AUTH_MAX_FAILURES", 10),
    window_seconds=_positive_env_int("ROUGH_CUT_AUTH_FAILURE_WINDOW_SECONDS", 60),
)


def _token_matches(supplied: str, expected: str) -> bool:
    if not supplied or not expected:
        return False
    # Bytes keep compare_digest timing-safe and total for non-ASCII input.
    return hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))


def _request_is_authenticated() -> bool:
    """Whether this request carries valid credentials (or tests bypass auth)."""

    if app.config.get("TESTING") and app.config.get("API_TOKEN") is None:
        return True
    return _token_matches(_request_token(), _api_token())


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
    client_key = request.remote_addr or "unknown"
    # Locked-out clients are refused before any comparison so a brute-force
    # run cannot learn whether a guess was right once the budget is spent.
    retry_after = _AUTH_THROTTLE.retry_after(client_key)
    if retry_after:
        throttled = jsonify({"ok": False, "error": "too many failed authentication attempts"})
        throttled.status_code = 429
        throttled.headers["Retry-After"] = str(retry_after)
        return throttled
    supplied = _request_token()
    if _token_matches(supplied, expected):
        _AUTH_THROTTLE.record_success(client_key)
        return None
    # A request with no credentials at all (e.g. the first browser navigation
    # that triggers the Basic challenge) is not a guess and is not counted.
    if supplied:
        _AUTH_THROTTLE.record_failure(client_key)
    response = jsonify({"ok": False, "error": "unauthorized"})
    response.status_code = 401
    # A Basic challenge makes Chromium-based CEP panels display a native
    # username/password dialog even though the panel authenticates with Bearer.
    # Keep the challenge for direct browser navigation only.
    header = request.headers.get("Authorization", "").strip()
    if not _cep_cors_origin() and not header.lower().startswith("bearer "):
        response.headers["WWW-Authenticate"] = 'Basic realm="Rough Cut", charset="UTF-8"'
    return response


@app.after_request
def add_cep_cors_headers(response):
    origin = _cep_cors_origin()
    if not origin:
        return response
    response.headers["Access-Control-Allow-Origin"] = origin
    response.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type, X-API-Token"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, PATCH, DELETE, OPTIONS"
    response.headers["Access-Control-Max-Age"] = "600"
    response.headers.add("Vary", "Origin")
    return response


def _redact_local_paths(message: str) -> str:
    """Show filesystem locations relative to the project tree, never absolute.

    A base only matches on a path-segment boundary: ``/app`` must not eat the
    start of ``/application/x`` or the middle of ``https://host/app/y``.
    """

    labels: dict[str, str] = {}
    for base, label in ((PROJECTS_DIR, "projects"), (ROOT_DIR, "")):
        for candidate in {str(base), str(Path(base).resolve())}:
            candidate = candidate.rstrip(os.sep)
            if candidate:
                labels.setdefault(candidate, label)
    if not labels:
        return message
    alternation = "|".join(re.escape(item) for item in sorted(labels, key=len, reverse=True))
    pattern = re.compile(
        rf"(?<![\w.:/~-])(?P<base>{alternation})"
        r"(?:(?P<sep>/)|(?=$|[\s:'\",;)\]]))"
    )

    def replace(match: re.Match[str]) -> str:
        label = labels[match.group("base")]
        if match.group("sep"):
            return f"{label}/" if label else ""
        return label or "."

    return pattern.sub(replace, message)


def _display_path(path: Path | str) -> str:
    """Return ``path`` relative to the project tree for user-facing messages."""

    return _redact_local_paths(str(path))


def _json_error(message: str, status_code: int = 400):
    return jsonify({"ok": False, "error": _redact_local_paths(str(message))}), status_code


@app.errorhandler(RequestEntityTooLarge)
def request_entity_too_large(_exc: RequestEntityTooLarge):
    return _json_error("request body is too large", 413)


def _internal_error(exc: Exception):
    app.logger.exception("Unhandled API error", exc_info=exc)
    return _json_error("internal server error", 500)


def _normalize_project_name(name: str) -> str:
    return validate_project_name(name)


def _project_paths(project: str) -> dict[str, Path]:
    return build_project_paths(project, PROJECTS_DIR)


def _make_openrouter_usage_callback(
    project: str,
    *,
    stage: str,
    operation: str,
    job_id: str = "",
    revision_id: str = "",
):
    paths = _ensure_project(project)
    settings = get_settings()
    return make_usage_callback(
        paths["openrouter_usage"],
        stage=stage,
        operation=operation,
        job_id=job_id,
        revision_id=revision_id,
        base_url=settings.openrouter_base_url,
    )


def _openrouter_usage_summary(project: str, *, reconcile: bool = False) -> dict:
    paths = _ensure_project(project)
    if reconcile:
        try:
            import requests

            settings = get_settings()
            reconcile_pending(
                paths["openrouter_usage"],
                api_key=settings.openrouter_api_key,
                request_fn=requests.get,
            )
        except Exception:
            app.logger.exception("Could not reconcile OpenRouter TTS usage")
    return usage_summary(paths["openrouter_usage"])


def _save_voiceover_revision_assets(project: str, result: dict) -> dict:
    return save_voiceover_tts_manifest(_ensure_project(project), result)


def _revision_speaker_dir(paths: dict[str, Path], revision_id: object) -> Path:
    """Return the version-local TTS alias directory, with legacy fallback."""
    try:
        return revision_speaker_alias_dir(paths, str(revision_id or ""))
    except ValueError:
        return paths["speaker"]


def _discover_project_inbox(project: str) -> dict:
    return discover_project_inbox(_ensure_project(project)["root"])


def _ensure_project(project: str) -> dict[str, Path]:
    return ensure_project_layout(project, PROJECTS_DIR)


def _list_projects() -> list[str]:
    return list_project_directories(PROJECTS_DIR)


def _read_json_safe(path: Path) -> dict:
    return read_project_json_safe(
        path,
        on_error=lambda failed_path, exc: app.logger.warning(
            "Ignoring unreadable JSON %s: %s", failed_path, exc
        ),
    )


def _atomic_write_json_file(path: Path, payload: dict) -> None:
    atomic_write_json(path, payload)


def _normalization_source_is_current(document: dict, project_root: Path) -> bool:
    """Best-effort source hash check, compatible with old normalization drafts."""
    source_meta = document.get("source")
    source_dict = source_meta if isinstance(source_meta, dict) else {}
    expected_hash = (
        str(
            document.get("source_sha256")
            or document.get("source_hash")
            or source_dict.get("sha256")
            or ""
        )
        .strip()
        .lower()
    )
    source_value = (
        document.get("source_path")
        or source_dict.get("path")
        or (source_meta if isinstance(source_meta, str) else "")
        or ""
    )
    if not expected_hash or not source_value:
        return True
    try:
        source_path = _abs_path(
            str(source_value), project_root / "input" / "scenario.docx", project_root
        )
        if not source_path.is_file():
            return False
        stat = source_path.stat()
        cache_key = str(source_path)
        with _SOURCE_HASH_CACHE_LOCK:
            cached = _SOURCE_HASH_CACHE.get(cache_key)
            if cached is not None:
                _SOURCE_HASH_CACHE.move_to_end(cache_key)
        if cached and cached[:2] == (stat.st_mtime_ns, stat.st_size):
            current_hash = cached[2]
        else:
            current_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()
            with _SOURCE_HASH_CACHE_LOCK:
                _SOURCE_HASH_CACHE[cache_key] = (stat.st_mtime_ns, stat.st_size, current_hash)
                _SOURCE_HASH_CACHE.move_to_end(cache_key)
                while len(_SOURCE_HASH_CACHE) > _SOURCE_HASH_CACHE_MAX:
                    _SOURCE_HASH_CACHE.popitem(last=False)
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
    try:
        if inspect_revision_state(paths).get("blocking"):
            return None, "revision_state_conflict"
    except (OSError, ValueError):
        return None, "revision_state_conflict"
    if str(document.get("status", "")).lower() != "confirmed":
        return None, "draft"
    if not _normalization_source_is_current(document, paths["root"]):
        return None, "source_changed"
    return document, "confirmed"


def _normalization_required(project: str):
    document, reason = _confirmed_normalization(project)
    if document is not None:
        return None
    return jsonify(
        {
            "ok": False,
            "error": "normalization_confirmation_required",
            "message": "Сначала подтвердите актуальную нормализацию сценария",
            "reason": reason,
            "project": project,
        }
    ), 409


def _prepared_projection_required(project: str):
    paths = _project_paths(project)
    confirmed, reason = _confirmed_normalization(project)
    prepared = _read_json_safe(paths["prepared"])
    if confirmed is not None and (
        prepared.get("normalization_revision") == confirmed.get("revision")
        and str(prepared.get("source") or "") == str(confirmed.get("source") or "")
    ):
        return None
    return jsonify(
        {
            "ok": False,
            "error": "normalization_projection_stale",
            "message": "Подтверждённая проекция для озвучки устарела; подтвердите сценарий повторно",
            "reason": reason if confirmed is None else "prepared_changed",
            "project": project,
        }
    ), 409


def _confirmed_prepared_path(payload: dict, paths: dict[str, Path]) -> Path:
    prepared_path = _abs_path(payload.get("prepared_path", ""), paths["prepared"], paths["root"])
    if prepared_path.resolve() != paths["prepared"].resolve():
        raise ValueError("prepared_path must reference confirmed scenario_prepared.json")
    return prepared_path


def _discover_input_files(input_dir: Path, project: str) -> list[str]:
    legacy = discover_project_input_files(input_dir, ROOT_DIR)
    try:
        paths = _ensure_project(project)
        inbox = discover_project_inbox(paths["root"])
        prefix = paths["root"].relative_to(ROOT_DIR)
        friendly = [
            str(prefix / str(item["relative_path"]))
            for item in inbox.get("scenario_files", [])
            if item.get("source") == "upload"
        ]
    except (OSError, ValueError):
        friendly = []
    return list(dict.fromkeys(friendly + legacy))


def _build_status(project: str) -> dict:
    paths = _project_paths(project)
    prepared_path = paths["work"] / "scenario_prepared.json"
    manifest_path = paths["active_voiceover_manifest"]

    prepared = _read_json_safe(prepared_path)
    manifest = _read_json_safe(manifest_path)
    normalization = _read_json_safe(paths["normalized"])
    timeline = _read_json_safe(paths["timeline"])
    confirmed_normalization, normalization_reason = _confirmed_normalization(project)
    try:
        revision_integrity = inspect_revision_state(paths)
    except (OSError, ValueError):
        revision_integrity = {
            "status": "invalid",
            "blocking": True,
            "message": "Не удалось проверить состояние редакций.",
        }

    prepared_items = prepared.get("items", []) if isinstance(prepared, dict) else []
    manifest_items = manifest.get("items", []) if isinstance(manifest, dict) else []

    active_speaker_dir = _revision_speaker_dir(paths, normalization.get("scenario_revision_id"))
    wav_paths = [p for p in active_speaker_dir.glob("zk*.wav") if p.is_file()]
    wav_files = sorted(p.name for p in wav_paths)

    missing_ids: list[str] = []
    if isinstance(prepared_items, list):
        existing = {p.stem for p in wav_paths}
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
        "scenario_revision_id": str(normalization.get("scenario_revision_id") or ""),
        "revision_integrity": revision_integrity,
        "normalization_confirmed": confirmed_normalization is not None,
        "normalization_reason": normalization_reason,
        "timeline_ready": bool(
            paths["timeline"].is_file()
            and confirmed_normalization
            and timeline.get("normalization_revision") == confirmed_normalization.get("revision")
        ),
        "openrouter_usage": _openrouter_usage_summary(project),
    }


def _resolve_project_name(raw: str | None) -> str:
    return select_project_name(raw, PROJECTS_DIR)


def _abs_path(value: str, fallback: Path, root: Path = PROJECTS_DIR) -> Path:
    return resolve_project_path(
        value,
        fallback,
        root_dir=ROOT_DIR,
        allowed_root=root,
    )


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _tts_job_services() -> TtsJobServices:
    """Resolve dependencies at call time so legacy API patches keep working."""

    return cast(TtsJobServices, sys.modules[__name__])


def _normalization_job_services() -> NormalizationJobServices:
    return cast(NormalizationJobServices, sys.modules[__name__])


def _persist_tts_job(job: dict) -> None:
    stage1_jobs.persist_tts_job(_tts_job_services(), job)


def _load_persisted_tts_job(project: str) -> dict | None:
    return stage1_jobs.load_persisted_tts_job(_tts_job_services(), project)


def _trim_tts_jobs_locked() -> None:
    stage1_jobs.trim_tts_jobs_locked(_tts_job_services())


def _reserve_tts_project(project: str) -> bool:
    with _project_transition_lock(project):
        if _project_has_active_mutation(project):
            return False
        return stage1_jobs.reserve_tts_project(_tts_job_services(), project)


def _release_tts_project(project: str) -> None:
    stage1_jobs.release_tts_project(_tts_job_services(), project)


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
    tts_model: str | None = None,
    tts_style: str = "",
) -> dict:
    return stage1_jobs.create_tts_job(
        _tts_job_services(),
        project,
        prepared_path=prepared_path,
        speaker_dir=speaker_dir,
        provider=provider,
        voice=voice,
        gender=gender,
        dry_run=dry_run,
        speed=speed,
        tts_model=tts_model,
        tts_style=tts_style,
    )


def _update_tts_job(
    job_id: str,
    *,
    persist: bool = True,
    **updates,
) -> dict | None:
    return stage1_jobs.update_tts_job(
        _tts_job_services(),
        job_id,
        persist=persist,
        **updates,
    )


def _get_tts_job(job_id: str) -> dict | None:
    return stage1_jobs.get_tts_job(_tts_job_services(), job_id)


def _tts_cancel_event(job_id: str) -> threading.Event | None:
    return stage1_jobs.tts_cancel_event(_tts_job_services(), job_id)


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
    tts_model: str | None = None,
    tts_style: str = "",
) -> None:
    stage1_jobs.run_tts_job(
        _tts_job_services(),
        job_id=job_id,
        project=project,
        prepared_path=prepared_path,
        speaker_dir=speaker_dir,
        provider=provider,
        voice_name=voice_name,
        gender=gender,
        dry_run=dry_run,
        speed=speed,
        tts_model=tts_model,
        tts_style=tts_style,
    )


register_core_routes(app, sys.modules[__name__])


def _expose_registered_views(*endpoint_names: str) -> None:
    """Keep the legacy ``api.<view>`` import surface during route extraction."""
    module_globals = globals()
    for endpoint_name in endpoint_names:
        module_globals[endpoint_name] = app.view_functions[endpoint_name]


_expose_registered_views(
    "project_home",
    "list_projects",
    "create_project",
    "delete_project",
    "project_dashboard",
    "health",
    "stage1_status",
    "speaker_file",
)


def _normalization_exception(exc: Exception):
    if isinstance(exc, RevisionConflictError):
        return _json_error(str(exc) or "normalization revision conflict", 409)
    if isinstance(exc, SourceChangedError):
        return _json_error(str(exc) or "normalization source changed", 409)
    if isinstance(exc, NormalizationValidationError):
        return jsonify(
            {
                "ok": False,
                "error": "normalization_validation_failed",
                "message": str(exc),
                "errors": list(getattr(exc, "errors", []) or []),
            }
        ), 422
    if isinstance(exc, FileNotFoundError):
        return _json_error(str(exc), 404)
    if isinstance(exc, KeyError):
        return _json_error("normalization memory rule not found", 404)
    if isinstance(exc, (NormalizationError, ValueError)):
        return _json_error(str(exc), 400)
    return _internal_error(exc)


def _normalization_payload_for_save(payload: dict, normalized_path: Path) -> dict:
    current = load_normalization(normalized_path)
    editable = {key: value for key, value in payload.items() if key in {"blocks", "hint"}}
    # The UI normally sends only blocks; retaining metadata prevents accidental
    # loss of source hashes and other server-owned document fields.
    current.update(editable)
    return current


def _persist_normalization_job(job: dict) -> None:
    stage1_jobs.persist_normalization_job(_normalization_job_services(), job)


def _create_normalization_job(
    project: str,
    *,
    input_path: Path,
    use_llm: bool,
    hint: str,
    llm_preset: str,
) -> dict:
    return stage1_jobs.create_normalization_job(
        _normalization_job_services(),
        project,
        input_path=input_path,
        use_llm=use_llm,
        hint=hint,
        llm_preset=llm_preset,
    )


def _update_normalization_job(job_id: str, **updates) -> dict | None:
    return stage1_jobs.update_normalization_job(
        _normalization_job_services(),
        job_id,
        **updates,
    )


def _get_normalization_job(job_id: str) -> dict | None:
    return stage1_jobs.get_normalization_job(_normalization_job_services(), job_id)


def _reserve_normalization_project(project: str) -> bool:
    with _project_transition_lock(project):
        if _project_has_active_mutation(project):
            return False
        return stage1_jobs.reserve_normalization_project(
            _normalization_job_services(),
            project,
        )


def _release_normalization_project(project: str) -> None:
    stage1_jobs.release_normalization_project(
        _normalization_job_services(),
        project,
    )


def _load_persisted_normalization_job(project: str) -> dict | None:
    return stage1_jobs.load_persisted_normalization_job(
        _normalization_job_services(),
        project,
    )


def _run_normalization_job(
    *,
    job_id: str,
    project: str,
    input_path: Path,
    output_path: Path,
    use_llm: bool,
    hint: str,
    llm_preset: str,
) -> None:
    stage1_jobs.run_normalization_job(
        _normalization_job_services(),
        job_id=job_id,
        project=project,
        input_path=input_path,
        output_path=output_path,
        use_llm=use_llm,
        hint=hint,
        llm_preset=llm_preset,
    )


def _memory_instance() -> NormalizationMemory:
    NORMALIZATION_MEMORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    return NormalizationMemory(NORMALIZATION_MEMORY_PATH)


def _close_memory(memory: NormalizationMemory) -> None:
    close = getattr(memory, "close", None)
    if callable(close):
        close()


register_stage1_routes(app, sys.modules[__name__])
_expose_registered_views(
    "stage1_analyze",
    "stage1_normalization_job_start",
    "stage1_normalization_job_get",
    "stage1_normalization_job_status",
    "stage1_openrouter_usage",
    "stage1_normalization_generate",
    "stage1_normalization_get",
    "stage1_normalization_save",
    "stage1_normalization_reset",
    "stage1_normalization_confirm",
    "stage1_normalization_learning_preview",
    "stage1_normalization_memory_get",
    "stage1_normalization_memory_create",
    "stage1_normalization_memory_update",
    "stage1_normalization_memory_delete",
    "stage1_tts",
    "stage1_tts_job_start",
    "stage1_tts_job_status",
    "stage1_tts_job_status_alias",
    "stage1_tts_job_cancel",
    "stage1_tts_phrase",
    "stage1_validate",
)


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
    result["block_uid"] = str(match.metadata.get("parent_block_uid") or match.segment_id)
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
        (str(backend).strip() if backend is not None else "")
        or os.getenv("SYNC_WHISPER_BACKEND", "auto").strip()
        or "auto"
    )
    configured_model = (
        (str(model).strip() if model is not None else "")
        or os.getenv("SYNC_WHISPER_MODEL", "").strip()
        or "<backend-default>"
    )
    cloud_timestamp_mapping = (
        "2" if configured_backend.lower() in {"cloud", "cloud-audio", "openrouter"} else ""
    )
    return {
        "backend": configured_backend.lower(),
        "model": configured_model,
        "language": str(language).strip().lower(),
        "beam_size": os.getenv("SYNC_WHISPER_BEAM_SIZE", "5").strip() or "5",
        "best_of": os.getenv("SYNC_WHISPER_BEST_OF", "5").strip() or "5",
        "batch_size": os.getenv("SYNC_WHISPER_BATCH_SIZE", "0").strip() or "0",
        "device": os.getenv("SYNC_WHISPER_DEVICE", "auto").strip().lower() or "auto",
        "compute_type": (
            os.getenv("SYNC_WHISPER_COMPUTE_TYPE", "default").strip().lower() or "default"
        ),
        "runtime_platform": f"{platform.system()}-{platform.machine()}",
        "faster_whisper_version": _installed_version("faster-whisper"),
        "mlx_whisper_version": _installed_version("mlx-whisper"),
        "openai_whisper_version": _installed_version("openai-whisper"),
        # Bump only cloud indexes: cloud ASR now repairs rare stretched
        # timestamp scales before word interpolation, so older cached words
        # must not be reused with the obsolete scale.
        "cloud_timestamp_mapping": cloud_timestamp_mapping,
    }


def _transcription_config_fingerprint(config: dict[str, str]) -> str:
    payload = json.dumps(config, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
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


def _word_index_audio_matches(
    index: WordIndex,
    audio_path: Path,
    *,
    audio_signature: dict[str, int | str] | None = None,
) -> bool:
    cache = index.metadata.get(_TRANSCRIPTION_CACHE_KEY)
    if not isinstance(cache, dict) or cache.get("version") != 1:
        return False
    try:
        current_signature = audio_signature or _audio_cache_signature(audio_path)
    except OSError:
        return False
    stored = cache.get("audio")
    if not isinstance(stored, dict):
        return False
    if stored.get("sample_sha256"):
        return stored.get("size") == current_signature.get("size") and stored.get(
            "sample_sha256"
        ) == current_signature.get("sample_sha256")
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
    text = str(block.get("normalized_text") or block.get("original_text") or "").strip()
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
        remainder = text[len(speaker) :].lstrip(" \t,")
        # A malformed legacy label can put the start of the quote into the
        # role field (``Лукьянова: начало реплики``). Its name prefix is
        # structural, while the rest must remain searchable text.
        role_prefix = role.split(":", 1)[0].strip() if ":" in role else role
        if role_prefix and remainder.casefold().startswith(role_prefix.casefold()):
            remainder = remainder[len(role_prefix) :]
        quote = remainder.lstrip(" \t\r\n:;,.…—–-")
        if quote:
            return quote
    return text


_EDITORIAL_SPEAKER_MARKERS = frozenset({"снх", "синхрон", "sync", "лайф", "life"})


def _matching_speaker(
    block: dict, speaker: str, source_speakers: set[tuple[str, ...]] | None = None
) -> str | None:
    """Return the person name to use when selecting an interview source.

    Editorial drafts commonly label a quote as ``СНХ, Киркоров: ...`` or
    ``ЛАЙФ, Орлова: ...``.  The normalizer deliberately stores the cue in
    ``speaker`` and the person in ``role``; for matching, the latter is the
    useful source selector. Do not apply this to ordinary ``Name, title``
    labels, where ``speaker`` already identifies the person. When a source
    list is supplied, an unknown role falls back to an unrestricted search
    rather than excluding every named interview. ``None`` means that this
    block has no own selector and may inherit a preceding one; an empty
    string is an explicit unrestricted selector and must not inherit.
    """
    role = str(block.get("role") or "").strip()
    is_editorial_cue = speaker.casefold() in _EDITORIAL_SPEAKER_MARKERS
    if str(block.get("type") or "").lower() == "sync" and is_editorial_cue:
        if not role:
            return ""
        candidate = role.split(":", 1)[0].strip()
        # Labels often put a cut marker after a real name: ``Маликов 00``.
        # It is not part of the source person name.
        candidate = re.sub(r"(?:\s+\d{1,2})+$", "", candidate).strip()
        candidate_tokens = tuple(normalize_text(candidate))
        if source_speakers is None or any(
            candidate_tokens and set(candidate_tokens).issubset(source_tokens)
            for source_tokens in source_speakers
        ):
            return candidate
        return ""
    return speaker or None


def _sync_alignment_units(
    blocks: list[dict],
    *,
    source_speakers: set[tuple[str, ...]] | None = None,
    preserve_whole_uids: set[str] | None = None,
) -> list[dict[str, object]]:
    """Expand a canonical speaker block into independently matchable pieces."""
    units: list[dict[str, object]] = []
    last_speaker = {"sync": "", "host": ""}
    for block in blocks:
        block_type = str(block.get("type") or "").lower()
        parent_uid = str(block.get("uid") or "").strip()
        if not parent_uid:
            units.append({"segment_id": "", "parent_block_uid": "", "block": block})
            continue
        speaker_selector = _matching_speaker(
            block, str(block.get("speaker") or "").strip(), source_speakers
        )
        if speaker_selector is None:
            speaker = last_speaker.get(block_type, "")
        else:
            speaker = speaker_selector
            if speaker:
                last_speaker[block_type] = speaker
        fragments = (
            normalized_sync_fragments(
                block
                if block.get("sync_fragments")
                else {**block, "normalized_text": _sync_quote(block)},
                preserve_whole=parent_uid in (preserve_whole_uids or set()),
            )
            if block_type == "sync"
            else [
                {
                    "text": _sync_quote(block),
                    "timecode_start": str(block.get("timecode_start") or "").strip(),
                    "timecode_end": str(block.get("timecode_end") or "").strip(),
                }
            ]
        )
        for index, fragment in enumerate(fragments):
            units.append(
                {
                    "segment_id": sync_fragment_id(parent_uid, index, len(fragments)),
                    "parent_block_uid": parent_uid,
                    "fragment_index": index,
                    "fragment_count": len(fragments),
                    "text": fragment["text"],
                    "timecode_start": fragment["timecode_start"],
                    "speaker": speaker,
                    "block": block,
                }
            )
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
        app.logger.warning("Could not refresh sync summary project=%s error=%s", project, exc)
        return snapshot
    confirmed, _reason = _confirmed_normalization(project)
    if confirmed is not None:
        normalization_revision = confirmed.get("revision")
        scenario_revision_id = str(confirmed.get("scenario_revision_id") or "")
        matches = [
            item
            for item in matches
            if item.metadata.get("normalization_revision") in (None, "", normalization_revision)
            and (
                not str(item.metadata.get("scenario_revision_id") or "")
                or not scenario_revision_id
                or str(item.metadata.get("scenario_revision_id") or "") == scenario_revision_id
            )
        ]
        confirmed_uids = {item.segment_id for item in matches if item.status == "confirmed"}
        valid_ids = {
            str(unit.get("segment_id") or "")
            for unit in _sync_alignment_units(
                [
                    block
                    for block in confirmed.get("blocks", [])
                    if str(block.get("type") or "").lower() in {"sync", "host"}
                ],
                preserve_whole_uids=confirmed_uids,
            )
        }
        matches = [item for item in matches if item.segment_id in valid_ids]
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
    scenario_revision_id: object = "",
) -> str:
    """Identify every durable input that can change a matching decision."""
    payload = {
        "version": 3,
        "normalization_revision": normalization_revision,
        "scenario_revision_id": str(scenario_revision_id or ""),
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
        candidates.append(
            SemanticCandidate(
                source_id=match.source_id,
                source_in_sec=match.source_in_sec,
                source_out_sec=match.source_out_sec,
                confidence=match.confidence,
                matched_text=match.matched_text,
            )
        )
    for alternative in match.alternatives:
        if alternative.source_id and alternative.source_out_sec > alternative.source_in_sec:
            candidates.append(
                SemanticCandidate(
                    source_id=alternative.source_id,
                    source_in_sec=alternative.source_in_sec,
                    source_out_sec=alternative.source_out_sec,
                    confidence=alternative.confidence,
                    matched_text=alternative.matched_text,
                )
            )
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
        "queued",
        "running",
        "cancelling",
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


def _trim_sync_jobs_locked() -> None:
    """Bound completed sync-job history while never evicting active jobs."""
    now = datetime.now(UTC)
    terminal_statuses = {"completed", "failed", "cancelled", "interrupted"}
    terminal_ids = [
        job_id for job_id, job in _SYNC_JOBS.items() if job.get("status") in terminal_statuses
    ]
    for job_id in terminal_ids:
        stamp = _SYNC_JOBS[job_id].get("finished_at") or _SYNC_JOBS[job_id].get("created_at") or ""
        try:
            expired = (now - datetime.fromisoformat(stamp)).total_seconds() > _SYNC_JOBS_TTL_SECONDS
        except (TypeError, ValueError):
            expired = False
        if expired:
            _SYNC_JOBS.pop(job_id, None)
            _SYNC_CANCEL_EVENTS.pop(job_id, None)

    terminal_ids = [
        job_id for job_id, job in _SYNC_JOBS.items() if job.get("status") in terminal_statuses
    ]
    ordered_ids = sorted(
        terminal_ids,
        key=lambda job_id: (
            _SYNC_JOBS[job_id].get("finished_at") or _SYNC_JOBS[job_id].get("created_at") or "",
            job_id,
        ),
    )
    drop_count = max(0, len(_SYNC_JOBS) - _SYNC_JOBS_MAX)
    for job_id in ordered_ids[:drop_count]:
        _SYNC_JOBS.pop(job_id, None)
        _SYNC_CANCEL_EVENTS.pop(job_id, None)


def _create_sync_job(project: str) -> dict:
    now = _utc_now_iso()
    job_id = uuid.uuid4().hex
    job = {
        "id": job_id,
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
        "whisper_model": "",
        "source_scope": "all",
        "asr_engine": "local",
        "asr_backend_used": "",
        "asr_cost_usd_total": 0.0,
        "asr_timestamp_precision": "",
        "transcription_wall_sec_total": 0.0,
        "created_at": now,
        "started_at": "",
        "finished_at": "",
        "error": "",
    }
    with _SYNC_JOBS_LOCK:
        _SYNC_JOBS[job_id] = job
        _SYNC_CANCEL_EVENTS[job_id] = threading.Event()
        _trim_sync_jobs_locked()
        snapshot = dict(job)
    _persist_sync_job(snapshot)
    return snapshot


def _update_sync_job(
    job_id: str,
    *,
    persist: bool = True,
    **updates,
) -> dict | None:
    snapshot = update_job(_SYNC_JOBS_LOCK, _SYNC_JOBS, job_id, updates)
    if snapshot is None:
        return None
    if persist:
        _persist_sync_job(snapshot)
    return snapshot


def _get_sync_job(job_id: str) -> dict | None:
    return get_job(_SYNC_JOBS_LOCK, _SYNC_JOBS, job_id)


def _sync_cancel_event(job_id: str) -> threading.Event | None:
    with _SYNC_JOBS_LOCK:
        return _SYNC_CANCEL_EVENTS.get(job_id)


def _sync_worker_services() -> SyncWorkerServices:
    return cast(SyncWorkerServices, sys.modules[__name__])


def _reserve_sync_project(project: str) -> bool:
    with _project_transition_lock(project):
        if _project_has_active_mutation(project):
            return False
        return reserve_project(_SYNC_JOBS_LOCK, _SYNC_ACTIVE_PROJECTS, project)


def _release_sync_project(project: str) -> None:
    release_project(_SYNC_JOBS_LOCK, _SYNC_ACTIVE_PROJECTS, project)


def _run_sync_job(
    *,
    job_id: str,
    project: str,
    backend: str | None,
    asr_engine: str = "local",
    model: str | None,
    language: str,
    timecode_mode: str,
    rematch_confirmed: bool,
    source_ids: tuple[str, ...],
    source_scope: str,
    force_transcribe: bool,
    search_window_sec: float,
) -> None:
    run_sync_job(
        _sync_worker_services(),
        job_id=job_id,
        project=project,
        backend=backend,
        asr_engine=asr_engine,
        model=model,
        language=language,
        timecode_mode=timecode_mode,
        rematch_confirmed=rematch_confirmed,
        source_ids=source_ids,
        source_scope=source_scope,
        force_transcribe=force_transcribe,
        search_window_sec=search_window_sec,
    )


def _final_review_job_services() -> FinalReviewJobServices:
    """Resolve dependencies late while exposing a typed worker boundary."""

    return cast(FinalReviewJobServices, sys.modules[__name__])


def _reserve_final_review_project(project: str) -> bool:
    return final_review_jobs.reserve_final_review_project(_final_review_job_services(), project)


def _release_final_review_project(project: str) -> None:
    final_review_jobs.release_final_review_project(_final_review_job_services(), project)


def _create_final_review_job(project: str, audio_path: Path, payload: dict) -> dict:
    return final_review_jobs.create_final_review_job(
        _final_review_job_services(), project, audio_path, payload
    )


def _get_final_review_job(job_id: str) -> dict | None:
    return final_review_jobs.get_final_review_job(_final_review_job_services(), job_id)


def _load_persisted_final_review_job(project: str) -> dict | None:
    return final_review_jobs.load_persisted_final_review_job(_final_review_job_services(), project)


def _update_final_review_job(job_id: str, **updates) -> dict | None:
    return final_review_jobs.update_final_review_job(
        _final_review_job_services(), job_id, **updates
    )


def _run_final_review_job(
    *,
    job_id: str,
    project: str,
    audio_path: Path,
    language: str,
    backend: str | None,
    model: str | None,
    source_sequence: dict,
) -> None:
    final_review_jobs.run_final_review_job(
        _final_review_job_services(),
        job_id=job_id,
        project=project,
        audio_path=audio_path,
        language=language,
        backend=backend,
        model=model,
        source_sequence=source_sequence,
    )


def _record_final_review_iteration(project: str, job: dict, corrections: list[dict]) -> dict:
    return final_review_jobs.record_final_review_iteration(
        _final_review_job_services(), project, job, corrections
    )


register_stage2_routes(app, sys.modules[__name__])
register_final_review_routes(app, sys.modules[__name__])
_expose_registered_views(
    "stage2_sync_sources",
    "stage2_sync_source_audio",
    "stage2_sync_source_words",
    "stage2_sync_job_start",
    "stage2_sync_job_status",
    "stage2_sync_job_cancel",
    "stage2_sync_job_status_alias",
    "stage2_sync_matches",
    "stage2_timeline",
    "stage2_build",
    "stage2_export_xml",
)

register_stage3_routes(app, sys.modules[__name__])
register_stage4_routes(app, sys.modules[__name__])
_expose_registered_views(
    "stage3_media_scan",
    "stage3_timeline_prepare",
    "stage3_export_xml",
)
