from __future__ import annotations

from pathlib import Path
from types import ModuleType

from flask import Flask, jsonify, request

from stage4.versioning import (
    RevisionStateConflictError,
    activate_revision,
    import_revision,
    inspect_revision_state,
    list_revisions,
    reconcile_revision_state,
)


def _active_project_job(services: ModuleType, project: str) -> str:
    registries = [
        ("озвучки", services._TTS_JOBS_LOCK, services._TTS_ACTIVE_PROJECTS),
        (
            "нормализации",
            services._NORMALIZATION_JOBS_LOCK,
            services._NORMALIZATION_ACTIVE_PROJECTS,
        ),
        ("сопоставления СНХ", services._SYNC_JOBS_LOCK, services._SYNC_ACTIVE_PROJECTS),
    ]
    stage3_lock = getattr(services, "_STAGE3_JOBS_LOCK", None)
    stage3_active = getattr(services, "_STAGE3_ACTIVE_PROJECTS", None)
    if stage3_lock is not None and stage3_active is not None:
        registries.append(("подготовки Stage 3", stage3_lock, stage3_active))
    for label, lock, active_projects in registries:
        with lock:
            if project in active_projects:
                return label
    return ""


def register_stage4_routes(app: Flask, services: ModuleType) -> None:
    @app.get("/stage4/inbox")
    def stage4_inbox():
        try:
            project = services._resolve_project_name(request.args.get("project"))
            result = services._discover_project_inbox(project)
        except ValueError as exc:
            return services._json_error(str(exc), 400)
        except Exception as exc:
            return services._internal_error(exc)
        return jsonify({"ok": True, "result": {"project": project, **result}})

    @app.get("/stage4/revisions")
    def stage4_revisions_list():
        try:
            project = services._resolve_project_name(request.args.get("project"))
            paths = services._ensure_project(project)
            revisions = list_revisions(paths)
            integrity = inspect_revision_state(paths)
        except Exception as exc:
            return services._internal_error(exc)
        return jsonify({
            "ok": True,
            "result": {
                "project": project,
                "revisions": revisions,
                "integrity": integrity,
            },
        })

    @app.post("/stage4/revisions/import")
    def stage4_revision_import():
        payload = request.get_json(silent=True) or {}
        try:
            project = services._resolve_project_name(payload.get("project"))
            paths = services._ensure_project(project)
            raw_input = str(payload.get("input_path") or "").strip()
            input_path = None
            if raw_input:
                requested = Path(raw_input)
                if not requested.is_absolute():
                    requested = paths["root"] / requested
                input_path = services._abs_path(
                    str(requested), requested, paths["root"]
                )
            with services._project_transition_lock(project):
                active_job = _active_project_job(services, project)
                if active_job:
                    return services._json_error(
                        f"Дождитесь завершения {active_job} перед импортом редакции",
                        409,
                    )
                result = import_revision(
                    project, paths,
                    generate_normalization=services.generate_normalization,
                    memory_path=services.NORMALIZATION_MEMORY_PATH,
                    input_path=input_path,
                    use_llm=bool(payload.get("use_llm", False)),
                    hint=str(payload.get("hint") or ""),
                )
        except RevisionStateConflictError as exc:
            return services._json_error(str(exc), 409)
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
            with services._project_transition_lock(project):
                active_job = _active_project_job(services, project)
                if active_job:
                    return services._json_error(
                        f"Дождитесь завершения {active_job} перед переключением редакции",
                        409,
                    )
                paths = services._ensure_project(project)
                result = activate_revision(revision_id, paths)
        except RevisionStateConflictError as exc:
            return services._json_error(str(exc), 409)
        except (FileNotFoundError, ValueError) as exc:
            return services._json_error(str(exc), 400)
        except Exception as exc:
            return services._internal_error(exc)
        return jsonify({"ok": True, "result": result})

    @app.post("/stage4/revisions/<revision_id>/reconcile")
    def stage4_revision_reconcile(revision_id: str):
        payload = request.get_json(silent=True) or {}
        try:
            if not revision_id.startswith("rev_") or not revision_id[4:].isdigit():
                raise ValueError("invalid revision id")
            project = services._resolve_project_name(payload.get("project"))
            with services._project_transition_lock(project):
                active_job = _active_project_job(services, project)
                if active_job:
                    return services._json_error(
                        f"Дождитесь завершения {active_job} перед восстановлением редакции",
                        409,
                    )
                paths = services._ensure_project(project)
                result = reconcile_revision_state(revision_id, paths)
        except (FileNotFoundError, ValueError) as exc:
            return services._json_error(str(exc), 400)
        except Exception as exc:
            return services._internal_error(exc)
        return jsonify({"ok": True, "result": result})


def re_fullmatch_id(value: str) -> bool:
    return bool(value) and all(character.isalnum() or character in {"_", "-"} for character in value)
