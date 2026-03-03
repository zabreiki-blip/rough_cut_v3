from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path
import re
import threading
import uuid

from flask import Flask, abort, jsonify, render_template, request, send_from_directory

from stage1.analyzer import analyze_script
from stage1.config import get_settings
from stage1.gemini_tts import synthesize_phrase, synthesize_tts
from stage1.validator import validate_stage1


app = Flask(__name__)
ROOT_DIR = Path(__file__).resolve().parent.parent
PROJECTS_DIR = ROOT_DIR / "projects"
PROJECT_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
_TTS_JOBS: dict[str, dict] = {}
_TTS_JOBS_LOCK = threading.Lock()
_TTS_JOBS_MAX = 200


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
    }


def _resolve_project_name(raw: str | None) -> str:
    if raw:
        return _normalize_project_name(raw)
    projects = _list_projects()
    if projects:
        return projects[0]
    return "default"


def _abs_path(value: str, fallback: Path) -> Path:
    if not value:
        return fallback
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = ROOT_DIR / candidate
    return candidate


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
) -> None:
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

    input_path = _abs_path(payload.get("input_path", input_default), ROOT_DIR / input_default)
    output_path = _abs_path(payload.get("output_path", ""), output_default)
    raw_output_path = _abs_path(payload.get("raw_output_path", ""), raw_default)
    use_llm = bool(payload.get("use_llm", True))

    try:
        result = analyze_script(
            input_path=input_path,
            output_path=output_path,
            raw_output_path=raw_output_path,
            use_llm=use_llm,
        )
    except FileNotFoundError as exc:
        return _json_error(str(exc), 404)
    except Exception as exc:
        return _json_error(str(exc), 500)

    result["project"] = project
    return jsonify({"ok": True, "result": result})


@app.post("/stage1/tts")
def stage1_tts():
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

    prepared_path = _abs_path(payload.get("prepared_path", ""), paths["work"] / "scenario_prepared.json")
    speaker_dir = _abs_path(payload.get("speaker_dir", ""), paths["speaker"])
    voice_name = payload.get("voice_name")
    dry_run = bool(payload.get("dry_run", False))

    try:
        result = synthesize_tts(
            prepared_path=prepared_path,
            speaker_dir=speaker_dir,
            provider=provider,
            voice_name=voice_name,
            gender=gender,
            dry_run=dry_run,
        )
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

    provider = payload.get("provider")
    if provider and str(provider).strip().lower() not in {"gemini", "openrouter", "macos"}:
        return _json_error("provider must be 'gemini' or 'openrouter' or 'macos'", 400)

    gender = payload.get("gender")
    if gender and str(gender).strip().lower() not in {"female", "male"}:
        return _json_error("gender must be 'female' or 'male'", 400)

    prepared_path = _abs_path(payload.get("prepared_path", ""), paths["work"] / "scenario_prepared.json")
    speaker_dir = _abs_path(payload.get("speaker_dir", ""), paths["speaker"])
    voice_name = payload.get("voice_name")
    dry_run = bool(payload.get("dry_run", False))

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
    speaker_dir = _abs_path(payload.get("speaker_dir", ""), paths["speaker"])
    voice_name = payload.get("voice_name")
    file_name = payload.get("file_name")
    dry_run = bool(payload.get("dry_run", False))

    try:
        result = synthesize_phrase(
            text=text,
            speaker_dir=speaker_dir,
            provider=provider,
            voice_name=voice_name,
            gender=gender,
            file_name=file_name,
            dry_run=dry_run,
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

    prepared_path = _abs_path(payload.get("prepared_path", ""), paths["work"] / "scenario_prepared.json")
    speaker_dir = _abs_path(payload.get("speaker_dir", ""), paths["speaker"])
    manifest_path = _abs_path(payload.get("manifest_path", ""), paths["speaker"] / "manifest.json")

    try:
        result = validate_stage1(
            prepared_path=prepared_path,
            speaker_dir=speaker_dir,
            manifest_path=manifest_path,
        )
    except FileNotFoundError as exc:
        return _json_error(str(exc), 404)
    except Exception as exc:
        return _json_error(str(exc), 500)

    result["project"] = project
    status_code = 200 if result.get("ok") else 422
    return jsonify({"ok": result.get("ok", False), "result": result}), status_code
