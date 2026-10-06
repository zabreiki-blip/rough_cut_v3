from __future__ import annotations

from functools import wraps
from types import ModuleType

from flask import Flask, jsonify, request

from stage1.normalizer import invalidate_video_cue_seed_cache


def _resolve_tts_direction(services: ModuleType, payload: dict, provider: str | None):
    settings = services.get_settings()
    effective_provider = str(provider or settings.tts_provider or "openrouter").strip().lower()
    style = services.normalize_tts_style(payload.get("tts_style"))
    model = None
    if effective_provider == "openrouter":
        model = services.normalize_openrouter_tts_model(
            payload.get("tts_model"), settings.openrouter_tts_model
        )
    return model, style


def _revision_speaker_dir(
    services: ModuleType,
    payload: dict,
    paths: dict,
    revision_id: object,
):
    """Route the default speaker directory into the active revision.

    A caller can still provide an explicit project-local directory for a
    one-off operation.  The UI's historical default (``speaker/``) is
    redirected so V2 never publishes positional aliases into V1's folder.
    """
    requested = services._abs_path(payload.get("speaker_dir", ""), paths["speaker"], paths["root"])
    # A restored TTS job may still supply the previous revision's alias dir.
    # Its prepared scenario is current, so writing there would leave the
    # active revision incomplete and make its asset manifest inconsistent.
    revisions_root = (paths["speaker"] / "revisions").resolve()
    if (
        requested.resolve() == paths["speaker"].resolve()
        or requested.resolve().parent == revisions_root
    ):
        return services._revision_speaker_dir(paths, revision_id)
    return requested


def _stage1_analyze(services: ModuleType):
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
            raise ValueError("output_path must be the project's canonical scenario_prepared.json")
        if raw_output_path.resolve() != raw_default.resolve():
            raise ValueError("raw_output_path must be the project's canonical scenario_raw.txt")
        result = services.analyze_script(
            input_path=input_path,
            output_path=output_path,
            raw_output_path=raw_output_path,
            use_llm=bool(payload.get("use_llm", True)),
            usage_callback=services._make_openrouter_usage_callback(
                project, stage="normalization", operation="legacy_analyze"
            ),
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
    response.headers["Warning"] = '299 - "Legacy analyzer; use /stage1/normalization/generate"'
    return response


def _stage1_normalization_job_start(services: ModuleType):
    payload = request.get_json(silent=True) or {}
    try:
        project = services._resolve_project_name(payload.get("project"))
        paths = services._ensure_project(project)
        input_files = services._discover_input_files(paths["input"], project)
        default_input = (
            services.ROOT_DIR / input_files[0] if input_files else paths["input"] / "scenario.docx"
        )
        input_path = services._abs_path(
            str(payload.get("input_path") or ""), default_input, paths["root"]
        )
        if not input_path.is_file():
            raise FileNotFoundError(f"Input file not found: {input_path}")
        llm_preset = services.normalize_llm_preset(payload.get("llm_preset"))
    except ValueError as exc:
        return services._json_error(str(exc), 400)
    except FileNotFoundError as exc:
        return services._json_error(str(exc), 404)

    if not services._reserve_normalization_project(project):
        return services._json_error(f"normalization already active for project {project}", 409)
    job = services._create_normalization_job(
        project,
        input_path=input_path,
        use_llm=bool(payload.get("use_llm", True)),
        hint=str(payload.get("hint") or payload.get("author_hint") or ""),
        llm_preset=llm_preset,
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
            "llm_preset": job["llm_preset"],
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


def _stage1_normalization_job_get(services: ModuleType, job_id: str):
    job = services._get_normalization_job(job_id)
    if job is None:
        try:
            project = services._resolve_project_name(request.args.get("project"))
        except ValueError as exc:
            return services._json_error(str(exc), 400)
        persisted = services._load_persisted_normalization_job(project)
        if persisted and str(persisted.get("id") or "") == job_id:
            job = persisted
    if job is None:
        return services._json_error("normalization job not found", 404)
    return jsonify({"ok": True, "result": job})


def _stage1_normalization_job_status(services: ModuleType):
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


def _stage1_openrouter_usage(services: ModuleType):
    try:
        project = services._resolve_project_name(request.args.get("project"))
    except ValueError as exc:
        return services._json_error(str(exc), 400)
    return jsonify(
        {
            "ok": True,
            "result": services._openrouter_usage_summary(project, reconcile=True),
        }
    )


def _stage1_normalization_generate(services: ModuleType):
    payload = request.get_json(silent=True) or {}
    try:
        project = services._resolve_project_name(payload.get("project"))
        paths = services._ensure_project(project)
        input_files = services._discover_input_files(paths["input"], project)
        default_input = (
            services.ROOT_DIR / input_files[0] if input_files else paths["input"] / "scenario.docx"
        )
        input_path = services._abs_path(
            str(payload.get("input_path") or ""), default_input, paths["root"]
        )
        llm_preset = services.normalize_llm_preset(payload.get("llm_preset"))
        result = services.generate_normalization(
            input_path,
            paths["normalized"],
            memory_path=services.NORMALIZATION_MEMORY_PATH,
            use_llm=bool(payload.get("use_llm", True)),
            hint=str(payload.get("hint") or payload.get("author_hint") or ""),
            llm_preset=llm_preset,
            usage_callback=services._make_openrouter_usage_callback(
                project, stage="normalization", operation="normalize"
            ),
        )
    except Exception as exc:
        return services._normalization_exception(exc)
    return jsonify({"ok": True, "project": project, "result": result})


def _stage1_normalization_get(services: ModuleType):
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


def _stage1_normalization_save(services: ModuleType):
    payload = request.get_json(silent=True) or {}
    try:
        project = services._resolve_project_name(payload.get("project"))
        paths = services._ensure_project(project)
        revision_value = payload.get("revision", payload.get("expected_revision"))
        if revision_value is None:
            return services._json_error("revision is required", 400)
        expected_revision = int(revision_value)
        document = services._normalization_payload_for_save(payload, paths["normalized"])
        result = services.save_normalization(
            paths["normalized"], document, expected_revision=expected_revision
        )
    except Exception as exc:
        return services._normalization_exception(exc)
    return jsonify({"ok": True, "project": project, "result": result})


def _stage1_normalization_reset(services: ModuleType):
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


def _stage1_normalization_confirm(services: ModuleType):
    payload = request.get_json(silent=True) or {}
    try:
        project = services._resolve_project_name(payload.get("project"))
        paths = services._ensure_project(project)
        revision_value = payload.get("revision", payload.get("expected_revision"))
        if revision_value is None:
            return services._json_error("revision is required", 400)
        expected_revision = int(revision_value)
        if any(key in {"blocks", "hint"} for key in payload):
            document = services._normalization_payload_for_save(payload, paths["normalized"])
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


def _stage1_normalization_learning_preview(services: ModuleType):
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


def _stage1_normalization_memory_get(services: ModuleType):
    memory = None
    try:
        memory = services._memory_instance()
        rules = memory.list_rules()
        status_filter = str(request.args.get("status") or "").strip().lower()
        type_filter = str(request.args.get("block_type") or "").strip().lower()
        search = str(request.args.get("search") or "").strip().casefold()
        if status_filter:
            rules = [rule for rule in rules if str(rule.get("status", "")).lower() == status_filter]
        if type_filter:
            rules = [
                rule
                for rule in rules
                if str(rule.get("block_type") or rule.get("target_type") or "").lower()
                == type_filter
            ]
        if search:
            rules = [
                rule
                for rule in rules
                if search in str(rule.get("alias") or rule.get("marker") or "").casefold()
            ]
    except Exception as exc:
        return services._normalization_exception(exc)
    finally:
        if memory is not None:
            services._close_memory(memory)
    return jsonify({"ok": True, "result": {"rules": rules}})


def _stage1_normalization_memory_create(services: ModuleType):
    payload = request.get_json(silent=True) or {}
    marker = str(payload.get("alias") or payload.get("marker") or "").strip()
    target_type = str(payload.get("block_type") or payload.get("target_type") or "").strip()
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


def _stage1_normalization_memory_update(services: ModuleType, rule_id: int):
    payload = request.get_json(silent=True) or {}
    updates = {key: value for key, value in payload.items() if key not in {"project", "id"}}
    if "alias" in updates:
        updates["marker"] = updates.pop("alias")
    if "block_type" in updates:
        updates["target_type"] = updates.pop("block_type")
    memory = None
    try:
        memory = services._memory_instance()
        result = memory.update_rule(rule_id, **updates)
        # A video-cue rule's marker/target_type/status can drift out of sync
        # with what the built-in seed would (re)write (e.g. renaming a seeded
        # keyword, or changing its target type). The per-process "already
        # seeded" cache must not hide that drift until the next restart, so
        # invalidate it whenever a scope='video_cue' rule changes.
        if result.get("scope") == "video_cue":
            invalidate_video_cue_seed_cache(memory.path)
    except Exception as exc:
        return services._normalization_exception(exc)
    finally:
        if memory is not None:
            services._close_memory(memory)
    return jsonify({"ok": True, "result": result})


def _stage1_normalization_memory_delete(services: ModuleType, rule_id: int):
    memory = None
    try:
        memory = services._memory_instance()
        rule = memory.get_rule(rule_id)
        result = memory.delete_rule(rule_id)
        # Deleting a scope='video_cue' rule must not be silently undone by
        # the next normalization run only because that database was already
        # seeded earlier in this process; invalidate the cache so the deleted
        # rule is deterministically reseeded on the next run regardless of
        # process lifetime.
        if result and rule is not None and rule.get("scope") == "video_cue":
            invalidate_video_cue_seed_cache(memory.path)
    except Exception as exc:
        return services._normalization_exception(exc)
    finally:
        if memory is not None:
            services._close_memory(memory)
    return jsonify({"ok": True, "result": result})


def _stage1_tts(services: ModuleType):
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
        "openrouter",
        "macos",
    }:
        return services._json_error("provider must be 'openrouter' or 'macos'", 400)
    gender = payload.get("gender")
    if gender and str(gender).strip().lower() not in {"female", "male"}:
        return services._json_error("gender must be 'female' or 'male'", 400)
    try:
        tts_model, tts_style = _resolve_tts_direction(services, payload, provider)
    except ValueError as exc:
        return services._json_error(str(exc), 400)

    if not services._reserve_tts_project(project):
        return services._json_error(f"TTS already active for project {project}", 409)
    try:
        prepared_path = services._confirmed_prepared_path(payload, paths)
        speaker_dir = _revision_speaker_dir(
            services,
            payload,
            paths,
            services._read_json_safe(prepared_path).get("scenario_revision_id"),
        )
        result = services.synthesize_tts(
            prepared_path=prepared_path,
            speaker_dir=speaker_dir,
            provider=provider,
            voice_name=payload.get("voice_name"),
            gender=gender,
            dry_run=bool(payload.get("dry_run", False)),
            usage_callback=services._make_openrouter_usage_callback(
                project,
                stage="voiceover",
                operation="tts",
                revision_id=str(
                    services._read_json_safe(prepared_path).get("scenario_revision_id") or ""
                ),
            ),
            tts_model=tts_model,
            tts_style=tts_style,
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


def _stage1_tts_job_start(services: ModuleType):
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
        "openrouter",
        "macos",
    }:
        return services._json_error("provider must be 'openrouter' or 'macos'", 400)
    gender = payload.get("gender")
    if gender and str(gender).strip().lower() not in {"female", "male"}:
        return services._json_error("gender must be 'female' or 'male'", 400)
    try:
        speed = float(payload.get("speed", 1.0))
    except (TypeError, ValueError):
        return services._json_error("speed must be a number between 0.5 and 1.5", 400)
    speed = max(0.5, min(1.5, speed))
    try:
        tts_model, tts_style = _resolve_tts_direction(services, payload, provider)
    except ValueError as exc:
        return services._json_error(str(exc), 400)

    try:
        prepared_path = services._confirmed_prepared_path(payload, paths)
        speaker_dir = _revision_speaker_dir(
            services,
            payload,
            paths,
            services._read_json_safe(prepared_path).get("scenario_revision_id"),
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
            tts_model=tts_model,
            tts_style=tts_style,
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
                "tts_model": tts_model,
                "tts_style": tts_style,
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
                "status_url": f"/stage1/tts-jobs/{job['id']}?project={project}",
            },
        }
    )


def _stage1_tts_job_status(services: ModuleType, job_id: str):
    job = services._get_tts_job(job_id)
    if not job:
        try:
            project = services._resolve_project_name(request.args.get("project"))
        except ValueError as exc:
            return services._json_error(str(exc), 400)
        persisted = services._load_persisted_tts_job(project)
        if persisted and str(persisted.get("id") or "") == job_id:
            job = persisted
    if not job:
        return services._json_error("job not found", 404)
    return jsonify({"ok": True, "result": job})


def _stage1_tts_job_status_alias(services: ModuleType):
    try:
        project = services._resolve_project_name(request.args.get("project"))
    except ValueError as exc:
        return services._json_error(str(exc), 400)
    with services._TTS_JOBS_LOCK:
        candidates = [
            dict(item) for item in services._TTS_JOBS.values() if item.get("project") == project
        ]
    if candidates:
        latest = max(candidates, key=lambda item: str(item.get("created_at") or ""))
        return jsonify({"ok": True, "result": latest})
    persisted = services._load_persisted_tts_job(project)
    if persisted is None:
        return services._json_error("job not found", 404)
    return jsonify({"ok": True, "result": persisted})


def _stage1_tts_job_cancel(services: ModuleType, job_id: str):
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


def _stage1_tts_clear(services: ModuleType):
    payload = request.get_json(silent=True) or {}
    try:
        project = services._resolve_project_name(payload.get("project"))
    except ValueError as exc:
        return services._json_error(str(exc), 400)

    paths = services._ensure_project(project)
    if services._project_has_active_mutation(project):
        return services._json_error(
            "Нельзя очистить озвучку, пока выполняется фоновая задача проекта", 409
        )

    removed = 0
    normalized = services._read_json_safe(paths["normalized"])
    speaker_dir = services._revision_speaker_dir(paths, normalized.get("scenario_revision_id"))
    projection_dirs = [speaker_dir]
    if speaker_dir.resolve() != paths["speaker"].resolve():
        # Clean stale pre-versioning aliases too.  They are never canonical
        # revision assets and keeping them would make a legacy UI appear to
        # retain audio after the user explicitly cleared it.
        projection_dirs.append(paths["speaker"])
    for directory in projection_dirs:
        for pattern in ("zk*.wav", "zk*.wav.meta"):
            for path in directory.glob(pattern):
                if path.is_file():
                    path.unlink()
                    removed += 1

    validation_manifest = paths["speaker"] / "manifest.json"
    if validation_manifest.is_file():
        validation_manifest.unlink()
        removed += 1

    services._atomic_write_json_file(
        paths["active_voiceover_manifest"],
        {
            "schema_version": 1,
            "scenario_revision_id": str(normalized.get("scenario_revision_id") or ""),
            "items": [],
        },
    )

    if paths["tts_job_status"].is_file():
        paths["tts_job_status"].unlink()
        removed += 1
    with services._TTS_JOBS_LOCK:
        stale_job_ids = [
            job_id
            for job_id, job in services._TTS_JOBS.items()
            if job.get("project") == project
            and job.get("status") not in {"queued", "running", "cancelling"}
        ]
        for job_id in stale_job_ids:
            services._TTS_JOBS.pop(job_id, None)
            services._TTS_CANCEL_EVENTS.pop(job_id, None)

    return jsonify(
        {
            "ok": True,
            "result": {
                "project": project,
                "files_removed": removed,
                "manifests_reset": 2,
                "history_preserved": True,
            },
        }
    )


def _stage1_tts_phrase(services: ModuleType):
    payload = request.get_json(silent=True) or {}
    try:
        project = services._resolve_project_name(payload.get("project"))
    except ValueError as exc:
        return services._json_error(str(exc), 400)
    paths = services._ensure_project(project)

    provider = payload.get("provider")
    if provider and str(provider).strip().lower() not in {
        "openrouter",
        "macos",
    }:
        return services._json_error("provider must be 'openrouter' or 'macos'", 400)
    gender = payload.get("gender")
    if gender and str(gender).strip().lower() not in {"female", "male"}:
        return services._json_error("gender must be 'female' or 'male'", 400)
    try:
        speed = float(payload.get("speed", 1.0))
    except (TypeError, ValueError):
        return services._json_error("speed must be a number between 0.5 and 1.5", 400)
    speed = max(0.5, min(1.5, speed))
    try:
        tts_model, tts_style = _resolve_tts_direction(services, payload, provider)
    except ValueError as exc:
        return services._json_error(str(exc), 400)

    if not services._reserve_tts_project(project):
        return services._json_error(f"TTS already active for project {project}", 409)
    try:
        normalized = services._read_json_safe(paths["normalized"])
        speaker_dir = _revision_speaker_dir(
            services, payload, paths, normalized.get("scenario_revision_id")
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
            usage_callback=services._make_openrouter_usage_callback(
                project, stage="voiceover", operation="tts_phrase"
            ),
            tts_model=tts_model,
            tts_style=tts_style,
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
    active_speaker_dir = services._revision_speaker_dir(
        paths, normalized.get("scenario_revision_id")
    )
    if (
        out_file.suffix.lower() == ".wav"
        and out_file.parent.resolve() == active_speaker_dir.resolve()
    ):
        result["playback_url"] = f"/speaker/{project}/{out_file.name}"
    return jsonify({"ok": True, "result": result})


def _stage1_validate(services: ModuleType):
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
        speaker_dir = _revision_speaker_dir(
            services,
            payload,
            paths,
            services._read_json_safe(prepared_path).get("scenario_revision_id"),
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
        return _stage1_analyze(services)

    @app.post("/stage1/normalization-jobs/start")
    def stage1_normalization_job_start():
        return _stage1_normalization_job_start(services)

    @app.get("/stage1/normalization-jobs/<job_id>")
    def stage1_normalization_job_get(job_id: str):
        return _stage1_normalization_job_get(services, job_id)

    @app.get("/stage1/normalization-jobs/status")
    def stage1_normalization_job_status():
        return _stage1_normalization_job_status(services)

    @app.get("/stage1/openrouter-usage")
    def stage1_openrouter_usage():
        return _stage1_openrouter_usage(services)

    @app.post("/stage1/normalization/generate")
    @serialize_project_mutation
    def stage1_normalization_generate():
        return _stage1_normalization_generate(services)

    @app.get("/stage1/normalization")
    def stage1_normalization_get():
        return _stage1_normalization_get(services)

    @app.put("/stage1/normalization")
    @serialize_project_mutation
    def stage1_normalization_save():
        return _stage1_normalization_save(services)

    @app.post("/stage1/normalization/reset")
    @serialize_project_mutation
    def stage1_normalization_reset():
        return _stage1_normalization_reset(services)

    @app.post("/stage1/normalization/confirm")
    @serialize_project_mutation
    def stage1_normalization_confirm():
        return _stage1_normalization_confirm(services)

    @app.post("/stage1/normalization/confirm/preview")
    def stage1_normalization_learning_preview():
        return _stage1_normalization_learning_preview(services)

    @app.get("/stage1/normalization/memory")
    def stage1_normalization_memory_get():
        return _stage1_normalization_memory_get(services)

    @app.post("/stage1/normalization/memory")
    def stage1_normalization_memory_create():
        return _stage1_normalization_memory_create(services)

    @app.patch("/stage1/normalization/memory/<int:rule_id>")
    def stage1_normalization_memory_update(rule_id: int):
        return _stage1_normalization_memory_update(services, rule_id)

    @app.delete("/stage1/normalization/memory/<int:rule_id>")
    def stage1_normalization_memory_delete(rule_id: int):
        return _stage1_normalization_memory_delete(services, rule_id)

    @app.post("/stage1/tts")
    def stage1_tts():
        return _stage1_tts(services)

    @app.post("/stage1/tts-jobs/start")
    def stage1_tts_job_start():
        return _stage1_tts_job_start(services)

    @app.get("/stage1/tts-jobs/<job_id>")
    def stage1_tts_job_status(job_id: str):
        return _stage1_tts_job_status(services, job_id)

    @app.get("/stage1/tts-jobs/status")
    def stage1_tts_job_status_alias():
        return _stage1_tts_job_status_alias(services)

    @app.post("/stage1/tts-jobs/<job_id>/cancel")
    def stage1_tts_job_cancel(job_id: str):
        return _stage1_tts_job_cancel(services, job_id)

    @app.post("/stage1/tts/clear")
    @serialize_project_mutation
    def stage1_tts_clear():
        return _stage1_tts_clear(services)

    @app.post("/stage1/tts-phrase")
    def stage1_tts_phrase():
        return _stage1_tts_phrase(services)

    @app.post("/stage1/validate")
    def stage1_validate():
        return _stage1_validate(services)
