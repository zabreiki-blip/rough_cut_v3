from __future__ import annotations

from functools import wraps
import json
import os
from pathlib import Path
from types import ModuleType
import uuid
import wave

from flask import Flask, Response, jsonify, request, send_from_directory


def _scenario_revision_matches(stored: object, current: object) -> bool:
    """Compare scenario identities while accepting artifacts from legacy files.

    An empty ID on either side means that the artifact predates scenario-level
    versioning. Explicit, different IDs are always stale.
    """
    stored_id = str(stored or "").strip()
    current_id = str(current or "").strip()
    return not stored_id or not current_id or stored_id == current_id


def _timeline_is_current(timeline: dict, normalization: dict) -> bool:
    return (
        timeline.get("normalization_revision") == normalization.get("revision")
        and _scenario_revision_matches(
            timeline.get("scenario_revision_id"),
            normalization.get("scenario_revision_id"),
        )
    )


def register_stage2_routes(app: Flask, services: ModuleType) -> None:
    """Register Stage 2 routes while resolving mutable services via the API facade."""

    def serialize_project_mutation(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            payload = request.get_json(silent=True) or {}
            raw_project = (
                request.args.get("project")
                if request.method == "GET"
                else payload.get("project")
            )
            try:
                project = services._resolve_project_name(raw_project)
            except ValueError as exc:
                return services._json_error(str(exc), 400)
            with services._project_transition_lock(project):
                return view(*args, **kwargs)
        return wrapped

    def serialize_sync_data_mutation(view=None, *, multipart: bool = False):
        """Reject source/transcript writes while a project job owns the data."""

        def decorate(target):
            @wraps(target)
            def wrapped(*args, **kwargs):
                if request.method == "GET":
                    return target(*args, **kwargs)
                payload = request.get_json(silent=True) or {}
                raw_project = (
                    request.form.get("project")
                    if multipart
                    else payload.get("project")
                )
                try:
                    project = services._resolve_project_name(raw_project)
                except ValueError as exc:
                    return services._json_error(str(exc), 400)
                with services._project_transition_lock(project):
                    if services._project_has_active_mutation(project):
                        return services._json_error(
                            "another mutating job is active for this project", 409
                        )
                    return target(*args, **kwargs)
            return wrapped

        return decorate if view is None else decorate(view)

    @app.route("/stage2/sync-sources", methods=["GET", "POST", "DELETE"])
    @serialize_sync_data_mutation
    def stage2_sync_sources():
        payload = request.get_json(silent=True) or {}
        raw_project = (
            request.args.get("project")
            if request.method == "GET"
            else payload.get("project")
        )
        try:
            project = services._resolve_project_name(raw_project)
        except ValueError as exc:
            return services._json_error(str(exc), 400)
        paths = services._ensure_project(project)
        source_store, match_store = services._sync_stores(paths)

        try:
            if request.method == "GET":
                sources = source_store.list()
                return jsonify({
                    "ok": True,
                    "result": {
                        "project": project,
                        "sources": [services._source_api_dict(item) for item in sources],
                    },
                })

            if request.method == "POST":
                source_payload = payload.get("source", payload)
                if not isinstance(source_payload, dict):
                    return services._json_error("source must be a JSON object", 400)
                source_payload = dict(source_payload)
                source_payload.pop("project", None)
                source_payload["id"] = services._sync_source_id(source_payload.get("id"))
                existing = source_store.get(source_payload["id"])
                if (
                    existing
                    and existing.sequence_id
                    and source_payload.get("sequence_id")
                    and existing.sequence_id != str(source_payload["sequence_id"])
                ):
                    return services._json_error(
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
                source = services.SyncSource.from_dict(source_payload)
                with services._SYNC_DATA_LOCK:
                    source_store.save(source)
                return jsonify({
                    "ok": True,
                    "result": {"project": project, "source": services._source_api_dict(source)},
                }), 201

            source_id = services._sync_source_id(
                payload.get("source_id") or request.args.get("source_id")
            )
            with services._SYNC_DATA_LOCK:
                deleted = source_store.delete(source_id)
                if deleted:
                    remaining_matches = [
                        item for item in match_store.list() if item.source_id != source_id
                    ]
                    match_store.replace_all(remaining_matches)
            if not deleted:
                return services._json_error("sync source not found", 404)
            # Regenerable artifacts are removed with the source record. These are
            # exact server-owned paths and never derived from an uploaded filename.
            for artifact in (
                services._canonical_audio_path(paths, source_id),
                services._word_index_path(paths, source_id),
            ):
                artifact.unlink(missing_ok=True)
            return jsonify({
                "ok": True,
                "result": {"project": project, "source_id": source_id, "deleted": True},
            })
        except (TypeError, ValueError) as exc:
            return services._json_error(str(exc), 400)
        except Exception as exc:
            return services._internal_error(exc)


    @app.route("/stage2/sync-sources/<source_id>/audio", methods=["GET", "PUT"])
    @serialize_sync_data_mutation(multipart=True)
    def stage2_sync_source_audio(source_id: str):
        try:
            source_id = services._sync_source_id(source_id)
            project = services._resolve_project_name(
                request.args.get("project")
                if request.method == "GET"
                else request.form.get("project")
            )
        except ValueError as exc:
            return services._json_error(str(exc), 400)
        paths = services._ensure_project(project)
        source_store, _match_store = services._sync_stores(paths)
        try:
            source = source_store.get(source_id)
        except ValueError as exc:
            return services._json_error(str(exc), 400)
        if source is None:
            return services._json_error("sync source not found", 404)
        if request.method == "GET":
            try:
                audio_path = services._resolve_sync_audio_path(source, paths)
            except ValueError as exc:
                return services._json_error(str(exc), 400)
            if not audio_path.is_file():
                return services._json_error("sync guide audio not found", 404)
            return send_from_directory(
                audio_path.parent, audio_path.name, as_attachment=False
            )
        uploaded = request.files.get("audio") or request.files.get("file")
        if uploaded is None or not uploaded.filename:
            return services._json_error("multipart WAV field 'audio' is required", 400)
        max_bytes = int(
            services.app.config.get("SYNC_AUDIO_MAX_BYTES")
            or os.getenv("SYNC_AUDIO_MAX_BYTES")
            or 4 * 1024 * 1024 * 1024
        )
        if request.content_length is not None and request.content_length > max_bytes:
            return services._json_error("audio upload is too large", 413)
        if Path(uploaded.filename).suffix.lower() != ".wav":
            return services._json_error("audio must be a WAV file", 415)

        destination = services._canonical_audio_path(paths, source_id)
        services._cleanup_stale_uploads(destination.parent)
        temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.upload")
        duration = 0.0
        try:
            services._copy_stream_limited(uploaded.stream, temporary, max_bytes)
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
            updated = services.SyncSource.from_dict(updated_payload)
            with services._SYNC_DATA_LOCK:
                source_store.save(updated)
            # Keep the previous regenerable transcript until a replacement has
            # been produced successfully. Its audio signature makes it stale for
            # the next job, while preserving recovery data if transcription fails.
        except services.AudioUploadTooLargeError as exc:
            temporary.unlink(missing_ok=True)
            return services._json_error(str(exc), 413)
        except (EOFError, wave.Error, ValueError) as exc:
            temporary.unlink(missing_ok=True)
            return services._json_error(f"invalid WAV file: {exc}", 415)
        except Exception as exc:
            temporary.unlink(missing_ok=True)
            return services._internal_error(exc)
        return jsonify({
            "ok": True,
            "result": {
                "project": project,
                "source": services._source_api_dict(updated),
                "audio_path": str(destination.relative_to(services.ROOT_DIR)),
                "duration_sec": duration,
            },
        })


    @app.put("/stage2/sync-sources/<source_id>/words")
    @serialize_sync_data_mutation
    def stage2_sync_source_words(source_id: str):
        payload = request.get_json(silent=True)
        if payload is None:
            return services._json_error("JSON word timestamp payload is required", 400)
        try:
            source_id = services._sync_source_id(source_id)
            project = services._resolve_project_name(
                payload.get("project") if isinstance(payload, dict) else request.args.get("project")
            )
        except ValueError as exc:
            return services._json_error(str(exc), 400)
        paths = services._ensure_project(project)
        source_store, _match_store = services._sync_stores(paths)
        try:
            source = source_store.get(source_id)
            if source is None:
                return services._json_error("sync source not found", 404)
            transcript_payload = dict(payload) if isinstance(payload, dict) else payload
            if isinstance(transcript_payload, dict):
                transcript_payload.pop("project", None)
            index = services.ingest_word_index(transcript_payload, source_id)
            try:
                audio_path = services._resolve_sync_audio_path(source, paths)
            except ValueError:
                audio_path = None
            if audio_path is not None and audio_path.is_file():
                config = services._transcription_config(
                    backend=None,
                    model=index.model or None,
                    language=index.language,
                )
                index.metadata[services._TRANSCRIPTION_CACHE_KEY] = (
                    services._transcription_cache_metadata(audio_path, config, origin="manual")
                )
            index_path = services._word_index_path(paths, source_id)
            services.save_word_index(index_path, index)
        except (TypeError, ValueError) as exc:
            return services._json_error(str(exc), 400)
        except Exception as exc:
            return services._internal_error(exc)
        return jsonify({
            "ok": True,
            "result": {
                "project": project,
                "source_id": source_id,
                "word_count": len(index.words),
                "duration_sec": index.duration_sec,
                "words_path": str(index_path.relative_to(services.ROOT_DIR)),
            },
        })


    @app.post("/stage2/sync-jobs/start")
    def stage2_sync_job_start():
        payload = request.get_json(silent=True) or {}
        try:
            project = services._resolve_project_name(payload.get("project"))
        except ValueError as exc:
            return services._json_error(str(exc), 400)
        paths = services._ensure_project(project)
        gate = services._normalization_required(project)
        if gate:
            return gate
        timecode_mode = str(payload.get("timecode_mode") or "relative").strip().lower()
        if timecode_mode not in {"relative", "display"}:
            return services._json_error("timecode_mode must be 'relative' or 'display'", 400)
        language = str(payload.get("language") or "ru").strip()
        if not language:
            return services._json_error("language must not be empty", 400)
        try:
            sources = services.SyncSourceStore(paths["sync_sources"]).list()
        except ValueError as exc:
            return services._json_error(str(exc), 400)
        if not sources:
            return services._json_error("no sync sources configured", 409)
        raw_source_ids = payload.get("source_ids") or []
        if not isinstance(raw_source_ids, list):
            return services._json_error("source_ids must be a JSON list", 400)
        try:
            source_ids = tuple(services._sync_source_id(item) for item in raw_source_ids)
        except ValueError as exc:
            return services._json_error(str(exc), 400)
        known_source_ids = {item.id for item in sources}
        unknown_source_ids = sorted(set(source_ids) - known_source_ids)
        if unknown_source_ids:
            return services._json_error(
                f"unknown sync sources: {', '.join(unknown_source_ids)}", 400
            )
        source_scope = str(payload.get("source_scope") or "all").strip().lower()
        if source_scope not in {"all", "review_media"}:
            return services._json_error(
                "source_scope must be 'all' or 'review_media'", 400
            )
        if source_scope == "review_media" and not any(
            source.source_kind == "review_media" for source in sources
        ):
            return services._json_error("no review media sources configured", 409)
        try:
            search_window_sec = float(payload.get("search_window_sec", 180.0))
        except (TypeError, ValueError):
            return services._json_error("search_window_sec must be a positive number", 400)
        if search_window_sec <= 0:
            return services._json_error("search_window_sec must be a positive number", 400)
        if not services._reserve_sync_project(project):
            return services._json_error(f"sync job already active for project {project}", 409)

        try:
            job = services._create_sync_job(project)
            worker = services.threading.Thread(
                target=services._run_sync_job,
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
                    "source_scope": source_scope,
                    "force_transcribe": bool(payload.get("force_transcribe", False)),
                    "search_window_sec": search_window_sec,
                },
                daemon=True,
            )
            worker.start()
        except Exception:
            services._release_sync_project(project)
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
        job = services._get_sync_job(job_id)
        if not job:
            return services._json_error("job not found", 404)
        return jsonify({"ok": True, "result": services._sync_job_with_live_summary(job)})


    @app.post("/stage2/sync-jobs/<job_id>/cancel")
    def stage2_sync_job_cancel(job_id: str):
        with services._SYNC_JOBS_LOCK:
            job = services._SYNC_JOBS.get(job_id)
            if job is None:
                return services._json_error("job not found", 404)
            if job.get("status") not in {"queued", "running", "cancelling"}:
                return services._json_error("job is not active", 409)
            event = services._SYNC_CANCEL_EVENTS.get(job_id)
            if event is None:
                return services._json_error("job worker is not active", 409)
            event.set()
            job.update({
                "status": "cancelling",
                "message": "Cancellation requested; saving completed work",
            })
            snapshot = dict(job)
        services._persist_sync_job(snapshot)
        return jsonify({"ok": True, "result": snapshot}), 202


    @app.get("/stage2/sync-jobs/status")
    def stage2_sync_job_status_alias():
        job_id = str(request.args.get("job_id") or "").strip()
        if job_id:
            return services.stage2_sync_job_status(job_id)
        try:
            project = services._resolve_project_name(request.args.get("project"))
        except ValueError as exc:
            return services._json_error(str(exc), 400)
        with services._SYNC_JOBS_LOCK:
            candidates = [
                dict(item) for item in services._SYNC_JOBS.values() if item.get("project") == project
            ]
        if not candidates:
            persisted = services._load_persisted_sync_job(project)
            if persisted is None:
                return services._json_error("job not found", 404)
            return jsonify({
                "ok": True,
                "result": services._sync_job_with_live_summary(persisted),
            })
        latest = max(candidates, key=lambda item: str(item.get("created_at") or ""))
        return jsonify({"ok": True, "result": services._sync_job_with_live_summary(latest)})


    @app.route("/stage2/sync-matches", methods=["GET", "PATCH"])
    @app.patch("/stage2/sync-matches/<block_uid>")
    @serialize_project_mutation
    def stage2_sync_matches(block_uid: str | None = None):
        payload = request.get_json(silent=True) or {}
        raw_project = (
            request.args.get("project")
            if request.method == "GET"
            else payload.get("project")
        )
        try:
            project = services._resolve_project_name(raw_project)
        except ValueError as exc:
            return services._json_error(str(exc), 400)
        paths = services._ensure_project(project)
        source_store, match_store = services._sync_stores(paths)
        try:
            if request.method == "GET":
                matches = match_store.list()
                confirmed, _reason = services._confirmed_normalization(project)
                normalization_document = confirmed or services._read_json_safe(paths["normalized"])
                if isinstance(normalization_document.get("blocks"), list):
                    confirmed_blocks = [
                        block for block in normalization_document.get("blocks", [])
                        if str(block.get("type") or "").lower() in {"sync", "host"}
                    ]
                    valid_ids = {
                        str(unit.get("segment_id") or "")
                        for unit in services._sync_alignment_units(confirmed_blocks)
                        if str(unit.get("segment_id") or "")
                    }
                    revision = normalization_document.get("revision")
                    scenario_revision_id = normalization_document.get(
                        "scenario_revision_id"
                    )
                    matches = [
                        item for item in matches
                        if item.segment_id in valid_ids
                        and item.metadata.get("normalization_revision") in (None, "", revision)
                        and _scenario_revision_matches(
                            item.metadata.get("scenario_revision_id"),
                            scenario_revision_id,
                        )
                    ]
                return jsonify({
                    "ok": True,
                    "result": {
                        "project": project,
                        "matches": [services._match_api_dict(item) for item in matches],
                        **services._sync_summary(matches),
                    },
                })

            updates = payload.get("matches")
            if updates is None:
                updates = [payload.get("match", payload)]
            if not isinstance(updates, list) or not all(
                isinstance(item, dict) for item in updates
            ):
                return services._json_error("matches must be a JSON list of objects", 400)
            confirmed, _reason = services._confirmed_normalization(project)
            if confirmed is None:
                gate = services._normalization_required(project)
                if gate is not None:
                    return gate
                return services._json_error(
                    "normalization_confirmation_required",
                    409,
                )
            confirmed_blocks = [
                block for block in confirmed.get("blocks", [])
                if str(block.get("type") or "").lower() in {"sync", "host"}
            ]
            valid_units = {
                str(unit.get("segment_id") or ""): unit
                for unit in services._sync_alignment_units(confirmed_blocks)
                if str(unit.get("segment_id") or "")
            }
            sources_by_id = {item.id: item for item in source_store.list()}
            source_ids = set(sources_by_id)
            revision = confirmed.get("revision")
            scenario_revision_id = str(
                confirmed.get("scenario_revision_id") or ""
            )
            saved: list[services.SyncMatch] = []
            with services._SYNC_DATA_LOCK:
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
                    metadata["scenario_revision_id"] = scenario_revision_id
                    unit = valid_units[segment_id]
                    metadata["parent_block_uid"] = str(
                        unit.get("parent_block_uid") or segment_id
                    )
                    metadata["fragment_index"] = int(unit.get("fragment_index") or 0)
                    metadata["fragment_count"] = int(unit.get("fragment_count") or 1)
                    metadata["manual_updated_at"] = services._utc_now_iso()
                    merged["metadata"] = metadata
                    source_id = str(merged.get("source_id") or "")
                    if source_id and source_id not in source_ids:
                        raise ValueError(f"unknown sync source: {source_id}")
                    match = services.SyncMatch.from_dict(merged)
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
                    "matches": [services._match_api_dict(item) for item in saved],
                },
            })
        except (TypeError, ValueError) as exc:
            return services._json_error(str(exc), 400)
        except Exception as exc:
            return services._internal_error(exc)


    @app.get("/stage2/timeline")
    def stage2_timeline():
        try:
            project = services._resolve_project_name(request.args.get("project"))
        except ValueError as exc:
            return services._json_error(str(exc), 400)
        paths = services._project_paths(project)
        timeline_path = paths["timeline"]
        if not timeline_path.is_file():
            return services._json_error("full_timeline.json not found — run Build first", 404)
        try:
            timeline = json.loads(timeline_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return services._json_error(f"invalid full_timeline.json: {exc}", 500)
        confirmed, _reason = services._confirmed_normalization(project)
        if (
            confirmed is None
            or not _timeline_is_current(timeline, confirmed)
        ):
            return services._json_error(
                "timeline_rebuild_required: нормализация изменилась после сборки таймлайна",
                409,
            )
        return jsonify({"ok": True, "result": timeline})


    @app.post("/stage2/build")
    @serialize_project_mutation
    def stage2_build():
        """Build full_timeline.json from confirmed normalization + speaker WAVs."""
        payload = request.get_json(silent=True) or {}

        try:
            project = services._resolve_project_name(payload.get("project"))
        except ValueError as exc:
            return services._json_error(str(exc), 400)

        if services._project_has_active_mutation(project):
            return services._json_error(
                "another mutating job is active for this project", 409
            )

        with services._SYNC_JOBS_LOCK:
            sync_job_active = project in services._SYNC_ACTIVE_PROJECTS
        if sync_job_active:
            return services._json_error(
                "sync_job_in_progress: дождитесь завершения сопоставления СНХ",
                409,
            )
        persisted_job = services._load_persisted_sync_job(project)
        if persisted_job and persisted_job.get("status") in {"interrupted", "cancelled"}:
            return services._json_error(
                "sync_job_interrupted: перезапустите сопоставление СНХ, чтобы продолжить с сохранённой контрольной точки",
                409,
            )

        paths = services._ensure_project(project)
        gate = services._normalization_required(project)
        if gate:
            return gate
        try:
            speaker_dir = services._abs_path(payload.get("speaker_dir", ""), paths["speaker"], paths["root"])
        except ValueError as exc:
            return services._json_error(str(exc), 400)
        placeholder_dir = paths["work"] / "placeholders"
        try:
            fps = int(payload.get("fps", 25))
        except (TypeError, ValueError):
            return services._json_error("fps must be an integer", 400)
        if fps < 1:
            return services._json_error("fps must be at least 1", 400)

        try:
            repair_revision_metadata = getattr(
                services, "repair_active_revision_metadata", None
            )
            revision_repair = (
                repair_revision_metadata(paths)
                if callable(repair_revision_metadata)
                else {"repaired": False, "reason": "service_unavailable"}
            )
            normalization = services.load_normalization(paths["normalized"])
            source_store, match_store = services._sync_stores(paths)
            sources = source_store.list()
            stored_matches = match_store.list()
            normalization_revision = normalization.get("revision")
            scenario_revision_id = normalization.get("scenario_revision_id")
            stale_match_ids: list[str] = []
            current_matches: list[services.SyncMatch] = []
            for match in stored_matches:
                match_revision = match.metadata.get("normalization_revision")
                if match_revision not in (None, "", normalization_revision):
                    stale_match_ids.append(match.segment_id)
                    continue
                if not _scenario_revision_matches(
                    match.metadata.get("scenario_revision_id"),
                    scenario_revision_id,
                ):
                    stale_match_ids.append(match.segment_id)
                    continue
                current_matches.append(match)
            timeline = services.parse_normalized_timeline(
                normalization=normalization,
                speaker_dir=speaker_dir,
                placeholder_dir=placeholder_dir,
                project=project,
                fps=fps,
                sync_matches={item.segment_id: item for item in current_matches},
                sync_sources={item.id: item for item in sources},
            )
        except ValueError as exc:
            return services._json_error(str(exc), 400)
        except Exception as exc:
            return services._internal_error(exc)

        # Generate placeholder PNGs for speaker and presenter segments.
        placeholder_warnings: list[str] = []
        for seg in timeline.segments:
            if seg.type in {"snh", "host"} and seg.placeholder_path:
                from pathlib import Path as _Path
                stable_uid = str(seg.block_uid or seg.parent_block_uid or "").strip()
                ph = _Path(seg.placeholder_path)
                if stable_uid:
                    try:
                        ph = services.generate_versioned_placeholder(
                            block_uid=stable_uid,
                            speaker=seg.speaker,
                            role=seg.role,
                            timecode=seg.timecode,
                            text=seg.text,
                            output_dir=placeholder_dir,
                            segment_type=seg.type,
                            width=timeline.width,
                            height=timeline.height,
                        )
                        seg.placeholder_path = str(ph)
                    except Exception as _ph_exc:
                        placeholder_warnings.append(
                            f"Placeholder не создан для {seg.id}: {_ph_exc}"
                        )
                        seg.placeholder_path = ""
                elif not ph.exists():
                    try:
                        services.generate_placeholder(
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
        timeline_payload["scenario_revision_id"] = str(
            normalization.get("scenario_revision_id") or ""
        )
        services._atomic_write_json_file(timeline_path, timeline_payload)

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
                "revision_metadata_repair": revision_repair,
                "segments": len(timeline.segments),
                "zk": zk_count,
                "snh": snh_count,
                "host": host_count,
                "tech": tech_count,
                "total_duration_sec": timeline.total_duration_sec,
                "timeline_path": str(timeline_path.relative_to(services.ROOT_DIR)),
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
            project = services._resolve_project_name(project_name)
        except ValueError as exc:
            return services._json_error(str(exc), 400)

        paths = services._project_paths(project)
        gate = services._normalization_required(project)
        if gate:
            return gate
        timeline_path = paths["work"] / "full_timeline.json"
        if not timeline_path.exists():
            return services._json_error("full_timeline.json not found — run Build first", 404)

        try:
            data = json.loads(timeline_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return services._json_error(f"invalid full_timeline.json: {exc}", 500)
        confirmed, _reason = services._confirmed_normalization(project)
        if not confirmed or not _timeline_is_current(data, confirmed):
            return services._json_error(
                "timeline_rebuild_required: нормализация изменилась после сборки таймлайна",
                409,
            )

        from stage2.models import FullTimeline
        try:
            tl = FullTimeline.from_dict(data)
        except (TypeError, ValueError) as exc:
            return services._json_error(f"invalid full_timeline.json: {exc}", 500)

        try:
            xml_str = services.build_xml(tl)
        except Exception as exc:
            return services._internal_error(exc)

        return Response(
            xml_str,
            mimetype="application/xml",
            headers={"Content-Disposition": f'attachment; filename="{project}_timeline.xml"'},
        )
