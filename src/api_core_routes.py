from __future__ import annotations

import json
from pathlib import Path
import shutil
from types import ModuleType

from flask import Flask, abort, jsonify, render_template, request, send_from_directory


def _active_revision_input_path(paths: dict[str, Path], input_files: list[str]) -> str:
    """Prefer the scenario file owned by the active revision."""

    active_path = paths["work"] / "active_revision.json"
    try:
        active = json.loads(active_path.read_text(encoding="utf-8"))
        revision_id = str(active.get("revision_id") or "")
        manifest_path = paths["scenario_versions"] / f"{revision_id}.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        source_file = Path(str(manifest.get("source_file") or "")).name
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        source_file = ""
    if source_file:
        active_input = next(
            (value for value in input_files if Path(value).name == source_file),
            "",
        )
        if active_input:
            return active_input
    return input_files[0] if input_files else ""


def register_core_routes(app: Flask, services: ModuleType) -> None:
    """Register project-shell routes while resolving services through the API facade."""

    @app.get("/")
    def project_home():
        services.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
        return render_template("index.html", projects=services._list_projects())

    @app.get("/projects")
    def list_projects():
        services.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
        return jsonify({"ok": True, "projects": services._list_projects()})

    @app.post("/projects")
    def create_project():
        payload = request.get_json(silent=True) or {}
        project_raw = payload.get("project")
        if not project_raw:
            return services._json_error("project is required", 400)

        try:
            project = services._normalize_project_name(str(project_raw))
        except ValueError as exc:
            return services._json_error(str(exc), 400)

        services._ensure_project(project)
        return jsonify({"ok": True, "project": project, "url": f"/project/{project}"})

    @app.delete("/projects/<project>")
    def delete_project(project: str):
        try:
            project = services._normalize_project_name(project)
        except ValueError as exc:
            return services._json_error(str(exc), 400)

        project_dir = services.PROJECTS_DIR / project
        if not project_dir.exists():
            return services._json_error("Проект не найден", 404)

        shutil.rmtree(project_dir)
        return jsonify({"ok": True, "project": project})

    @app.get("/project/<project>")
    def project_dashboard(project: str):
        try:
            project = services._normalize_project_name(project)
        except ValueError:
            abort(404)

        settings = services.get_settings()
        paths = services._ensure_project(project)
        status = services._build_status(project)

        input_files = services._discover_input_files(paths["input"], project)
        active_input = _active_revision_input_path(paths, input_files)
        defaults = {
            "project": project,
            "input_path": (
                active_input or f"projects/{project}/input/scenario.docx"
            ),
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
            "configuration_warnings": list(services._CONFIG_WARNINGS),
        })

    @app.get("/stage1/status")
    def stage1_status():
        raw_project = request.args.get("project")
        try:
            project = services._resolve_project_name(raw_project)
            services._ensure_project(project)
        except ValueError as exc:
            return services._json_error(str(exc), 400)

        return jsonify({"ok": True, "result": services._build_status(project)})

    @app.get("/speaker/<project>/<path:filename>")
    def speaker_file(project: str, filename: str):
        try:
            project = services._normalize_project_name(project)
        except ValueError:
            abort(404)

        if "/" in filename or filename.startswith(".") or not filename.endswith(".wav"):
            abort(404)

        paths = services._project_paths(project)
        return send_from_directory(paths["speaker"], filename, as_attachment=False)
