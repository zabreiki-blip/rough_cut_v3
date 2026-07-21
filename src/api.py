from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import re
import threading
import uuid

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from flask import Flask, abort, jsonify, render_template, request, send_from_directory

from stage1.analyzer import analyze_script
from stage1.config import get_settings
from stage1.gemini_tts import synthesize_phrase, synthesize_tts
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
    save_normalization,
)
from stage1.validator import validate_stage1
from stage2.timeline_parser import parse_normalized_timeline
from stage2.placeholder import generate_placeholder
from stage2.xml_export import build_xml


app = Flask(__name__)
ROOT_DIR = Path(__file__).resolve().parent.parent
PROJECTS_DIR = ROOT_DIR / "projects"
NORMALIZATION_MEMORY_PATH = ROOT_DIR / "data" / "normalization_memory.sqlite3"
PROJECT_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
_TTS_JOBS: dict[str, dict] = {}
_TTS_JOBS_LOCK = threading.Lock()
_TTS_JOBS_MAX = 200
_TTS_CONCURRENT_MAX = 3
_TTS_SEMAPHORE = threading.Semaphore(_TTS_CONCURRENT_MAX)


def _json_error(message: str, status_code: int = 400):
    return jsonify({"ok": False, "error": message}), status_code


def _normalize_project_name(name: str) -> str:
    project = (name or "").strip()
    if not PROJECT_NAME_RE.match(project):
        raise ValueError(
            "project must match ^[a-zA-Z0-9_-]{1,64}$"
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
    }


def _ensure_project(project: str) -> dict[str, Path]:
    paths = _project_paths(project)
    paths["input"].mkdir(parents=True, exist_ok=True)
    paths["work"].mkdir(parents=True, exist_ok=True)
    paths["speaker"].mkdir(parents=True, exist_ok=True)
    return paths


def _list_projects() -> list[str]:
    if not PROJECTS_DIR.exists():
        return []
    items = [p.name for p in PROJECTS_DIR.iterdir() if p.is_dir() and PROJECT_NAME_RE.match(p.name)]
    return sorted(items)


def _read_json_safe(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


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
        current_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()
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


def _trim_tts_jobs_locked() -> None:
    if len(_TTS_JOBS) <= _TTS_JOBS_MAX:
        return
    ordered_ids = sorted(
        _TTS_JOBS.keys(),
        key=lambda job_id: (
            _TTS_JOBS[job_id].get("finished_at") or _TTS_JOBS[job_id].get("created_at") or "",
            job_id,
        ),
    )
    drop_count = max(0, len(_TTS_JOBS) - _TTS_JOBS_MAX)
    for job_id in ordered_ids[:drop_count]:
        _TTS_JOBS.pop(job_id, None)


def _create_tts_job(project: str) -> dict:
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
        "provider": "",
        "voice": "",
        "gender": "",
        "dry_run": False,
        "error": "",
        "created_at": now,
        "started_at": "",
        "finished_at": "",
        "result": None,
    }
    with _TTS_JOBS_LOCK:
        _TTS_JOBS[job_id] = job
        _trim_tts_jobs_locked()
    return dict(job)


def _update_tts_job(job_id: str, **updates) -> dict | None:
    with _TTS_JOBS_LOCK:
        job = _TTS_JOBS.get(job_id)
        if not job:
            return None
        job.update(updates)
        return dict(job)


def _get_tts_job(job_id: str) -> dict | None:
    with _TTS_JOBS_LOCK:
        job = _TTS_JOBS.get(job_id)
        if not job:
            return None
        return dict(job)


def _run_tts_job(
    *,
    job_id: str,
    prepared_path: Path,
    speaker_dir: Path,
    provider: str | None,
    voice_name: str | None,
    gender: str | None,
    dry_run: bool,
    speed: float = 1.0,
) -> None:
    _TTS_SEMAPHORE.acquire()
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
            speed=speed,
        )
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
        _TTS_SEMAPHORE.release()

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
        voice=str(result.get("voice", "")),
        gender=str(result.get("gender", "")),
        dry_run=bool(result.get("dry_run", False)),
        result=result,
    )


@app.get("/")
def project_home():
    PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    return render_template("index.html", projects=_list_projects())


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
    return jsonify({"ok": True, "status": "healthy"})


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
        return _json_error(str(exc), 500)

    result["project"] = project
    return jsonify({"ok": True, "result": result})


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
    return _json_error(str(exc), 500)


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
        )
    except Exception as exc:
        return _normalization_exception(exc)
    return jsonify({"ok": True, "project": project, "result": result})


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
        return _json_error(str(exc), 500)

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

    with _TTS_JOBS_LOCK:
        running = [
            j for j in _TTS_JOBS.values()
            if j.get("project") == project and j.get("status") == "running"
        ]
        if running:
            return _json_error(
                f"TTS-задача уже выполняется для проекта {project}: {running[0]['id']}", 409
            )

    job = _create_tts_job(project=project)
    worker = threading.Thread(
        target=_run_tts_job,
        kwargs={
            "job_id": job["id"],
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
        return _json_error(str(exc), 500)

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
        return _json_error(str(exc), 500)

    result["project"] = project
    status_code = 200 if result.get("ok") else 422
    return jsonify({"ok": result.get("ok", False), "result": result}), status_code


# ── Stage 2 ────────────────────────────────────────────────────────────────────

@app.post("/stage2/build")
def stage2_build():
    """Build full_timeline.json from confirmed normalization + speaker WAVs."""
    payload = request.get_json(silent=True) or {}

    try:
        project = _resolve_project_name(payload.get("project"))
    except ValueError as exc:
        return _json_error(str(exc), 400)

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

    try:
        normalization = load_normalization(paths["normalized"])
        timeline = parse_normalized_timeline(
            normalization=normalization,
            speaker_dir=speaker_dir,
            placeholder_dir=placeholder_dir,
            project=project,
            fps=fps,
        )
    except Exception as exc:
        return _json_error(str(exc), 500)

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
            "warnings": (
                ([f"ZK без WAV (будут пропущены в XML): {', '.join(zk_missing_wav)}"] if zk_missing_wav else [])
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

    from stage2.models import FullTimeline, TimelineSegment
    segs = [TimelineSegment(**{k: v for k, v in s.items() if k != "end_sec"})
            for s in data["segments"]]
    tl = FullTimeline(
        project=data["project"],
        segments=segs,
        fps=data.get("fps", 25),
        width=data.get("width", 1920),
        height=data.get("height", 1080),
    )

    try:
        xml_str = build_xml(tl)
    except Exception as exc:
        return _json_error(str(exc), 500)

    from flask import Response
    return Response(
        xml_str,
        mimetype="application/xml",
        headers={"Content-Disposition": f'attachment; filename="{project}_timeline.xml"'},
    )
