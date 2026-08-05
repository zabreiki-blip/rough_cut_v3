from __future__ import annotations

from pathlib import Path
from types import ModuleType

from flask import Flask, jsonify, request

from stage4.versioning import activate_revision, import_revision, list_revisions


def register_stage4_routes(app: Flask, services: ModuleType) -> None:
    @app.get("/stage4/revisions")
    def stage4_revisions_list():
        try:
            project = services._resolve_project_name(request.args.get("project"))
            paths = services._ensure_project(project)
            revisions = list_revisions(paths)
        except Exception as exc:
            return services._internal_error(exc)
        return jsonify({"ok": True, "result": {"project": project, "revisions": revisions}})

    @app.post("/stage4/revisions/import")
    def stage4_revision_import():
        payload = request.get_json(silent=True) or {}
        try:
            project = services._resolve_project_name(payload.get("project"))
            paths = services._ensure_project(project)
            raw_input = str(payload.get("input_path") or "").strip()
            input_path = None
            if raw_input:
                input_path = services._abs_path(raw_input, paths["input"] / raw_input, paths["root"])
            result = import_revision(
                project, paths,
                generate_normalization=services.generate_normalization,
                memory_path=services.NORMALIZATION_MEMORY_PATH,
                input_path=input_path,
                use_llm=bool(payload.get("use_llm", False)),
                hint=str(payload.get("hint") or ""),
            )
        except (FileNotFoundError, ValueError) as exc:
            return services._json_error(str(exc), 400)
        except Exception as exc:
            return services._internal_error(exc)
        return jsonify({"ok": True, "result": result})

    @app.get("/stage4/diffs/<diff_id>")
    def stage4_diff_get(diff_id: str):
        try:
            project = services._resolve_project_name(request.args.get("project"))
            paths = services._ensure_project(project)
            if not re_fullmatch_id(diff_id):
                raise ValueError("invalid diff id")
            diff_path = paths["scenario_diffs"] / f"{diff_id}.json"
            if not diff_path.is_file():
                raise FileNotFoundError("diff not found")
            result = services._read_json_safe(diff_path)
        except (FileNotFoundError, ValueError) as exc:
            return services._json_error(str(exc), 404)
        except Exception as exc:
            return services._internal_error(exc)
        return jsonify({"ok": True, "result": result})

    @app.post("/stage4/revisions/<revision_id>/activate")
    def stage4_revision_activate(revision_id: str):
        payload = request.get_json(silent=True) or {}
        try:
            if not revision_id.startswith("rev_") or not revision_id[4:].isdigit():
                raise ValueError("invalid revision id")
            project = services._resolve_project_name(payload.get("project"))
            paths = services._ensure_project(project)
            result = activate_revision(revision_id, paths)
        except (FileNotFoundError, ValueError) as exc:
            return services._json_error(str(exc), 400)
        except Exception as exc:
            return services._internal_error(exc)
        return jsonify({"ok": True, "result": result})


def re_fullmatch_id(value: str) -> bool:
    return bool(value) and all(character.isalnum() or character in {"_", "-"} for character in value)
