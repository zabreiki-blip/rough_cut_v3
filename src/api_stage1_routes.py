from __future__ import annotations

from functools import wraps
from types import ModuleType

from flask import Flask, jsonify, request


def register_stage1_routes(app: Flask, services: ModuleType) -> None:
    """Register Stage 1 routes while resolving mutable services via the API facade."""

    def serialize_project_mutation(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            payload = request.get_json(silent=True) or {}
            try:
                project = services._resolve_project_name(payload.get("project"))
            except ValueError as exc:
                return services._json_error(str(exc), 400)
            with services._project_transition_lock(project):
                if services._project_has_active_mutation(project):
                    return services._json_error(
                        "another mutating job is active for this project", 409
                    )
                return view(*args, **kwargs)
        return wrapped

    @app.post("/stage1/analyze")
    @serialize_project_mutation
    def stage1_analyze():
        payload = request.get_json(silent=True) or {}

        try:
            project = services._resolve_project_name(payload.get("project"))
        except ValueError as exc:
            return services._json_error(str(exc), 400)

        paths = services._ensure_project(project)
        input_files = services._discover_input_files(paths["input"], project)
        input_default = (
            input_files[0]
            if input_files
            else str((paths["input"] / "scenario.docx").relative_to(services.ROOT_DIR))
        )
        output_default = paths["prepared"]
        raw_default = paths["raw"]

        try:
            input_path = services._abs_path(
                payload.get("input_path", input_default),
                services.ROOT_DIR / input_default,
                paths["root"],
            )
            output_path = services._abs_path(
                payload.get("output_path", ""), output_default, paths["root"]
            )
            raw_output_path = services._abs_path(
                payload.get("raw_output_path", ""), raw_default, paths["root"]
            )
            if output_path.resolve() != output_default.resolve():
                raise ValueError(
                    "output_path must be the project's canonical scenario_prepared.json"
                )
            if raw_output_path.resolve() != raw_default.resolve():
                raise ValueError(
                    "raw_output_path must be the project's canonical scenario_raw.txt"
                )
            result = services.analyze_script(
                input_path=input_path,
                output_path=output_path,
                raw_output_path=raw_output_path,
                use_llm=bool(payload.get("use_llm", True)),
            )
        except ValueError as exc:
            return services._json_error(str(exc), 400)
        except FileNotFoundError as exc:
            return services._json_error(str(exc), 404)
        except Exception as exc:
            return services._internal_error(exc)

        result["project"] = project
        response = jsonify({"ok": True, "result": result})
        response.headers["Deprecation"] = "true"
        response.headers["Warning"] = (
            '299 - "Legacy analyzer; use /stage1/normalization/generate"'
        )
        return response

    @app.post("/stage1/normalization-jobs/start")
    def stage1_normalization_job_start():
        payload = request.get_json(silent=True) or {}
        try:
            project = services._resolve_project_name(payload.get("project"))
            paths = services._ensure_project(project)
            input_files = services._discover_input_files(paths["input"], project)
            default_input = (
                services.ROOT_DIR / input_files[0]
                if input_files
                else paths["input"] / "scenario.docx"
            )
            input_path = services._abs_path(
                str(payload.get("input_path") or ""), default_input, paths["root"]
            )
            if not input_path.is_file():
                raise FileNotFoundError(f"Input file not found: {input_path}")
        except ValueError as exc:
            return services._json_error(str(exc), 400)
        except FileNotFoundError as exc:
            return services._json_error(str(exc), 404)

        if not services._reserve_normalization_project(project):
            return services._json_error(
                f"normalization already active for project {project}", 409
            )
        job = services._create_normalization_job(
            project,
            input_path=input_path,
            use_llm=bool(payload.get("use_llm", True)),
            hint=str(payload.get("hint") or payload.get("author_hint") or ""),
        )
        worker = services.threading.Thread(
            target=services._run_normalization_job,
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
            services._release_normalization_project(project)
            raise
        return jsonify({"ok": True, "result": job}), 202

    @app.get("/stage1/normalization-jobs/<job_id>")
    def stage1_normalization_job_get(job_id: str):
        job = services._get_normalization_job(job_id)
        if job is None:
            return services._json_error("normalization job not found", 404)
        return jsonify({"ok": True, "result": job})

    @app.get("/stage1/normalization-jobs/status")
    def stage1_normalization_job_status():
        try:
            project = services._resolve_project_name(request.args.get("project"))
        except ValueError as exc:
            return services._json_error(str(exc), 400)
        with services._NORMALIZATION_JOBS_LOCK:
            candidates = [
                dict(job)
                for job in services._NORMALIZATION_JOBS.values()
                if job.get("project") == project
            ]
        latest = (
            max(candidates, key=lambda item: item.get("created_at") or "")
            if candidates
            else services._load_persisted_normalization_job(project)
        )
        return jsonify({"ok": True, "result": latest or {}})

    @app.post("/stage1/normalization/generate")
    @serialize_project_mutation
    def stage1_normalization_generate():
        payload = request.get_json(silent=True) or {}
        try:
            project = services._resolve_project_name(payload.get("project"))
            paths = services._ensure_project(project)
            input_files = services._discover_input_files(paths["input"], project)
            default_input = (
                services.ROOT_DIR / input_files[0]
                if input_files
                else paths["input"] / "scenario.docx"
            )
            input_path = services._abs_path(
                str(payload.get("input_path") or ""), default_input, paths["root"]
            )
            result = services.generate_normalization(
                input_path,
                paths["normalized"],
                memory_path=services.NORMALIZATION_MEMORY_PATH,
                use_llm=bool(payload.get("use_llm", True)),
                hint=str(payload.get("hint") or payload.get("author_hint") or ""),
            )
        except Exception as exc:
            return services._normalization_exception(exc)
        return jsonify({"ok": True, "project": project, "result": result})

    @app.get("/stage1/normalization")
    def stage1_normalization_get():
        try:
            project = services._resolve_project_name(request.args.get("project"))
            paths = services._ensure_project(project)
            if not paths["normalized"].exists() and paths["prepared"].exists():
                input_files = services._discover_input_files(paths["input"], project)
                source_path = services.ROOT_DIR / input_files[0] if input_files else None
                services.import_legacy_normalization(
                    paths["prepared"],
                    paths["raw"],
                    paths["normalized"],
                    source_path=source_path,
                )
            result = services.load_normalization(paths["normalized"])
        except Exception as exc:
            return services._normalization_exception(exc)
        return jsonify({"ok": True, "project": project, "result": result})

    @app.put("/stage1/normalization")
    @serialize_project_mutation
    def stage1_normalization_save():
        payload = request.get_json(silent=True) or {}
        try:
            project = services._resolve_project_name(payload.get("project"))
            paths = services._ensure_project(project)
            revision_value = payload.get("revision", payload.get("expected_revision"))
            if revision_value is None:
                return services._json_error("revision is required", 400)
            expected_revision = int(revision_value)
            document = services._normalization_payload_for_save(
                payload, paths["normalized"]
            )
            result = services.save_normalization(
                paths["normalized"], document, expected_revision=expected_revision
            )
        except Exception as exc:
            return services._normalization_exception(exc)
        return jsonify({"ok": True, "project": project, "result": result})

    @app.post("/stage1/normalization/reset")
    @serialize_project_mutation
    def stage1_normalization_reset():
        payload = request.get_json(silent=True) or {}
        try:
            project = services._resolve_project_name(payload.get("project"))
            paths = services._ensure_project(project)
            revision_value = payload.get("revision", payload.get("expected_revision"))
            if revision_value is None:
                return services._json_error("revision is required", 400)
            result = services.reset_normalization(
                paths["normalized"],
                expected_revision=int(revision_value),
                source_root=paths["root"],
            )
        except Exception as exc:
            return services._normalization_exception(exc)
        return jsonify({"ok": True, "project": project, "result": result})

    @app.post("/stage1/normalization/confirm")
    @serialize_project_mutation
    def stage1_normalization_confirm():
        payload = request.get_json(silent=True) or {}
        try:
            project = services._resolve_project_name(payload.get("project"))
            paths = services._ensure_project(project)
            revision_value = payload.get("revision", payload.get("expected_revision"))
            if revision_value is None:
                return services._json_error("revision is required", 400)
            expected_revision = int(revision_value)
            if any(key in {"blocks", "hint"} for key in payload):
                document = services._normalization_payload_for_save(
                    payload, paths["normalized"]
                )
                saved = services.save_normalization(
                    paths["normalized"], document, expected_revision=expected_revision
                )
                expected_revision = int(saved.get("revision", expected_revision + 1))

            result = services.confirm_normalization(
                paths["normalized"],
                paths["prepared"],
                paths["raw"],
                memory_path=services.NORMALIZATION_MEMORY_PATH,
                expected_revision=expected_revision,
                learning_decisions=payload.get("learning_decisions"),
            )
        except Exception as exc:
            return services._normalization_exception(exc)
        return jsonify({"ok": True, "project": project, "result": result})

    @app.post("/stage1/normalization/confirm/preview")
    def stage1_normalization_learning_preview():
        payload = request.get_json(silent=True) or {}
        try:
            project = services._resolve_project_name(payload.get("project"))
            paths = services._ensure_project(project)
            revision_value = payload.get("revision", payload.get("expected_revision"))
            if revision_value is None:
                return services._json_error("revision is required", 400)
            candidates = services.preview_learning_candidates(
                paths["normalized"],
                memory_path=services.NORMALIZATION_MEMORY_PATH,
                expected_revision=int(revision_value),
            )
        except Exception as exc:
            return services._normalization_exception(exc)
        return jsonify(
            {
                "ok": True,
                "project": project,
                "result": {
                    "revision": int(revision_value),
                    "candidates": candidates,
                },
            }
        )

    @app.get("/stage1/normalization/memory")
    def stage1_normalization_memory_get():
        memory = None
        try:
            memory = services._memory_instance()
            rules = memory.list_rules()
            status_filter = str(request.args.get("status") or "").strip().lower()
            type_filter = str(request.args.get("block_type") or "").strip().lower()
            search = str(request.args.get("search") or "").strip().casefold()
            if status_filter:
                rules = [
                    rule
                    for rule in rules
                    if str(rule.get("status", "")).lower() == status_filter
                ]
            if type_filter:
                rules = [
                    rule
                    for rule in rules
                    if str(
                        rule.get("block_type") or rule.get("target_type") or ""
                    ).lower()
                    == type_filter
                ]
            if search:
                rules = [
                    rule
                    for rule in rules
                    if search
                    in str(rule.get("alias") or rule.get("marker") or "").casefold()
                ]
        except Exception as exc:
            return services._normalization_exception(exc)
        finally:
            if memory is not None:
                services._close_memory(memory)
        return jsonify({"ok": True, "result": {"rules": rules}})

    @app.post("/stage1/normalization/memory")
    def stage1_normalization_memory_create():
        payload = request.get_json(silent=True) or {}
        marker = str(payload.get("alias") or payload.get("marker") or "").strip()
        target_type = str(
            payload.get("block_type") or payload.get("target_type") or ""
        ).strip()
        if not marker or not target_type:
            return services._json_error("alias and block_type are required", 400)
        memory = None
        try:
            memory = services._memory_instance()
            result = memory.create_rule(marker, target_type, source="manual")
        except Exception as exc:
            return services._normalization_exception(exc)
        finally:
            if memory is not None:
                services._close_memory(memory)
        return jsonify({"ok": True, "result": result}), 201

    @app.patch("/stage1/normalization/memory/<int:rule_id>")
    def stage1_normalization_memory_update(rule_id: int):
        payload = request.get_json(silent=True) or {}
        updates = {
            key: value
            for key, value in payload.items()
            if key not in {"project", "id"}
        }
        if "alias" in updates:
            updates["marker"] = updates.pop("alias")
        if "block_type" in updates:
            updates["target_type"] = updates.pop("block_type")
        memory = None
        try:
            memory = services._memory_instance()
            result = memory.update_rule(rule_id, **updates)
        except Exception as exc:
            return services._normalization_exception(exc)
        finally:
            if memory is not None:
                services._close_memory(memory)
        return jsonify({"ok": True, "result": result})

    @app.delete("/stage1/normalization/memory/<int:rule_id>")
    def stage1_normalization_memory_delete(rule_id: int):
        memory = None
        try:
            memory = services._memory_instance()
            result = memory.delete_rule(rule_id)
        except Exception as exc:
            return services._normalization_exception(exc)
        finally:
            if memory is not None:
                services._close_memory(memory)
        return jsonify({"ok": True, "result": result})

    @app.post("/stage1/tts")
    def stage1_tts():
        payload = request.get_json(silent=True) or {}
        try:
            project = services._resolve_project_name(payload.get("project"))
        except ValueError as exc:
            return services._json_error(str(exc), 400)

        paths = services._ensure_project(project)
        gate = services._normalization_required(project)
        if gate:
            return gate
        projection_gate = services._prepared_projection_required(project)
        if projection_gate:
            return projection_gate

        provider = payload.get("provider")
        if provider and str(provider).strip().lower() not in {
            "gemini",
            "openrouter",
            "macos",
        }:
            return services._json_error(
                "provider must be 'gemini' or 'openrouter' or 'macos'", 400
            )
        gender = payload.get("gender")
        if gender and str(gender).strip().lower() not in {"female", "male"}:
            return services._json_error("gender must be 'female' or 'male'", 400)

        if not services._reserve_tts_project(project):
            return services._json_error(f"TTS already active for project {project}", 409)
        try:
            prepared_path = services._confirmed_prepared_path(payload, paths)
            speaker_dir = services._abs_path(
                payload.get("speaker_dir", ""), paths["speaker"], paths["root"]
            )
            result = services.synthesize_tts(
                prepared_path=prepared_path,
                speaker_dir=speaker_dir,
                provider=provider,
                voice_name=payload.get("voice_name"),
                gender=gender,
                dry_run=bool(payload.get("dry_run", False)),
            )
            services._save_voiceover_revision_assets(project, result)
        except ValueError as exc:
            return services._json_error(str(exc), 400)
        except FileNotFoundError as exc:
            return services._json_error(str(exc), 404)
        except Exception as exc:
            return services._internal_error(exc)
        finally:
            services._release_tts_project(project)

        result["project"] = project
        return jsonify({"ok": True, "result": result})

    @app.post("/stage1/tts-jobs/start")
    def stage1_tts_job_start():
        payload = request.get_json(silent=True) or {}
        try:
            project = services._resolve_project_name(payload.get("project"))
        except ValueError as exc:
            return services._json_error(str(exc), 400)

        paths = services._ensure_project(project)
        gate = services._normalization_required(project)
        if gate:
            return gate
        projection_gate = services._prepared_projection_required(project)
        if projection_gate:
            return projection_gate

        provider = payload.get("provider")
        if provider and str(provider).strip().lower() not in {
            "gemini",
            "openrouter",
            "macos",
        }:
            return services._json_error(
                "provider must be 'gemini' or 'openrouter' or 'macos'", 400
            )
        gender = payload.get("gender")
        if gender and str(gender).strip().lower() not in {"female", "male"}:
            return services._json_error("gender must be 'female' or 'male'", 400)
        try:
            speed = float(payload.get("speed", 1.0))
        except (TypeError, ValueError):
            return services._json_error(
                "speed must be a number between 0.5 and 1.5", 400
            )
        speed = max(0.5, min(1.5, speed))

        try:
            prepared_path = services._confirmed_prepared_path(payload, paths)
            speaker_dir = services._abs_path(
                payload.get("speaker_dir", ""), paths["speaker"], paths["root"]
            )
        except ValueError as exc:
            return services._json_error(str(exc), 400)

        if not services._reserve_tts_project(project):
            return services._json_error(f"TTS already active for project {project}", 409)
        try:
            job = services._create_tts_job(
                project=project,
                prepared_path=prepared_path,
                speaker_dir=speaker_dir,
                provider=provider,
                voice=payload.get("voice_name"),
                gender=gender,
                dry_run=bool(payload.get("dry_run", False)),
                speed=speed,
            )
            worker = services.threading.Thread(
                target=services._run_tts_job,
                kwargs={
                    "job_id": job["id"],
                    "project": project,
                    "prepared_path": prepared_path,
                    "speaker_dir": speaker_dir,
                    "provider": provider,
                    "voice_name": payload.get("voice_name"),
                    "gender": gender,
                    "dry_run": bool(payload.get("dry_run", False)),
                    "speed": speed,
                },
                daemon=True,
            )
            worker.start()
        except Exception:
            services._release_tts_project(project)
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
        job = services._get_tts_job(job_id)
        if not job:
            return services._json_error("job not found", 404)
        return jsonify({"ok": True, "result": job})

    @app.get("/stage1/tts-jobs/status")
    def stage1_tts_job_status_alias():
        try:
            project = services._resolve_project_name(request.args.get("project"))
        except ValueError as exc:
            return services._json_error(str(exc), 400)
        with services._TTS_JOBS_LOCK:
            candidates = [
                dict(item)
                for item in services._TTS_JOBS.values()
                if item.get("project") == project
            ]
        if candidates:
            latest = max(
                candidates, key=lambda item: str(item.get("created_at") or "")
            )
            return jsonify({"ok": True, "result": latest})
        persisted = services._load_persisted_tts_job(project)
        if persisted is None:
            return services._json_error("job not found", 404)
        return jsonify({"ok": True, "result": persisted})

    @app.post("/stage1/tts-jobs/<job_id>/cancel")
    def stage1_tts_job_cancel(job_id: str):
        with services._TTS_JOBS_LOCK:
            job = services._TTS_JOBS.get(job_id)
            if job is None:
                return services._json_error("job not found", 404)
            if job.get("status") not in {"queued", "running", "cancelling"}:
                return services._json_error("job is not active", 409)
            event = services._TTS_CANCEL_EVENTS.get(job_id)
            if event is None:
                return services._json_error("job worker is not active", 409)
            event.set()
            job.update(
                {
                    "status": "cancelling",
                    "event": "cancelling",
                    "message": "Cancellation requested; finishing current TTS call",
                }
            )
            snapshot = dict(job)
        services._persist_tts_job(snapshot)
        return jsonify({"ok": True, "result": snapshot}), 202

    @app.post("/stage1/tts-phrase")
    def stage1_tts_phrase():
        payload = request.get_json(silent=True) or {}
        try:
            project = services._resolve_project_name(payload.get("project"))
        except ValueError as exc:
            return services._json_error(str(exc), 400)
        paths = services._ensure_project(project)

        provider = payload.get("provider")
        if provider and str(provider).strip().lower() not in {
            "gemini",
            "openrouter",
            "macos",
        }:
            return services._json_error(
                "provider must be 'gemini' or 'openrouter' or 'macos'", 400
            )
        gender = payload.get("gender")
        if gender and str(gender).strip().lower() not in {"female", "male"}:
            return services._json_error("gender must be 'female' or 'male'", 400)
        try:
            speed = float(payload.get("speed", 1.0))
        except (TypeError, ValueError):
            return services._json_error(
                "speed must be a number between 0.5 and 1.5", 400
            )
        speed = max(0.5, min(1.5, speed))

        if not services._reserve_tts_project(project):
            return services._json_error(f"TTS already active for project {project}", 409)
        try:
            speaker_dir = services._abs_path(
                payload.get("speaker_dir", ""), paths["speaker"], paths["root"]
            )
            result = services.synthesize_phrase(
                text=str(payload.get("text", "")),
                speaker_dir=speaker_dir,
                provider=provider,
                voice_name=payload.get("voice_name"),
                gender=gender,
                file_name=payload.get("file_name"),
                dry_run=bool(payload.get("dry_run", False)),
                speed=speed,
            )
        except ValueError as exc:
            return services._json_error(str(exc), 400)
        except FileNotFoundError as exc:
            return services._json_error(str(exc), 404)
        except Exception as exc:
            return services._internal_error(exc)
        finally:
            services._release_tts_project(project)

        result["project"] = project
        out_file = services.Path(result.get("file", ""))
        if (
            out_file.suffix.lower() == ".wav"
            and out_file.parent.resolve() == paths["speaker"].resolve()
        ):
            result["playback_url"] = f"/speaker/{project}/{out_file.name}"
        return jsonify({"ok": True, "result": result})

    @app.post("/stage1/validate")
    def stage1_validate():
        payload = request.get_json(silent=True) or {}
        try:
            project = services._resolve_project_name(payload.get("project"))
        except ValueError as exc:
            return services._json_error(str(exc), 400)

        paths = services._ensure_project(project)
        gate = services._normalization_required(project)
        if gate:
            return gate
        projection_gate = services._prepared_projection_required(project)
        if projection_gate:
            return projection_gate
        try:
            prepared_path = services._confirmed_prepared_path(payload, paths)
            speaker_dir = services._abs_path(
                payload.get("speaker_dir", ""), paths["speaker"], paths["root"]
            )
            manifest_path = services._abs_path(
                payload.get("manifest_path", ""),
                paths["speaker"] / "manifest.json",
                paths["root"],
            )
            result = services.validate_stage1(
                prepared_path=prepared_path,
                speaker_dir=speaker_dir,
                manifest_path=manifest_path,
                project_name=project,
            )
        except ValueError as exc:
            return services._json_error(str(exc), 400)
        except FileNotFoundError as exc:
            return services._json_error(str(exc), 404)
        except Exception as exc:
            return services._internal_error(exc)

        result["project"] = project
        status_code = 200 if result.get("ok") else 422
        return jsonify({"ok": result.get("ok", False), "result": result}), status_code
