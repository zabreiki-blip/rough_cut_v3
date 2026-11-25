"""HTTP endpoints for translated sync project artifacts."""

import wave

from flask import jsonify, request
from translated_sync.service import import_transcript, load_transcript


def register_translated_sync_routes(app, services):
    @app.route("/stage2/sync-sources/<source_id>/transcript-docx", methods=["GET", "POST"])
    def translated_transcript(source_id):
        try:
            project = services._resolve_project_name(
                request.args.get("project")
                if request.method == "GET"
                else request.form.get("project")
            )
            source_id = services._sync_source_id(source_id)
            paths = services._ensure_project(project)
            with services._project_transition_lock(project):
                if request.method == "POST" and services._project_has_active_mutation(project):
                    return services._json_error(
                        "another mutating job is active for this project", 409
                    )
                with services._SYNC_DATA_LOCK:
                    source_store, match_store = services._sync_stores(paths)
                    source = source_store.get(source_id)
                    if source is None:
                        return services._json_error("sync source not found", 404)
                    if request.method == "GET":
                        result = load_transcript(paths, source_id)
                        if result is None:
                            return services._json_error("transcript not found", 404)
                    else:
                        upload = request.files.get("file")
                        if (
                            upload is None
                            or not upload.filename
                            or not upload.filename.lower().endswith(".docx")
                        ):
                            return services._json_error("DOCX file is required", 400)
                        max_bytes = 32 * 1024 * 1024
                        data = upload.stream.read(max_bytes + 1)
                        if len(data) > max_bytes:
                            return services._json_error("DOCX file is too large", 413)
                        try:
                            guide = services._resolve_guide_media_path(source, paths)
                            guide_duration = services._wav_duration_or_none(
                                guide
                            ) or services._media_duration_or_none(guide)
                            result = import_transcript(
                                paths,
                                source,
                                data,
                                request.form.get("timecode_mode", "relative"),
                                request.form.get("sequence_start_tc"),
                                source_store,
                                match_store,
                                guide_duration=guide_duration,
                            )
                        except Exception as exc:
                            return services._json_error(f"invalid transcript: {exc}", 400)
            return jsonify({"ok": True, "result": result})
        except (ValueError, TypeError) as exc:
            return services._json_error(str(exc), 400)

    @app.post("/stage2/translated-sync/match")
    def translated_match():
        import json
        import os
        from api_stage2_routes import _confirmed_parent_ids
        from cost_budget import CostBudget, CostBudgetExceeded
        from stage2.sync_llm_review import SemanticCandidate
        from stage2.sync_models import AlignmentRequest, SyncAlternative
        from stage_common import classify_sync_matches, unit_text_sha256
        from sync_match_phase import _unit_source_hint
        from translated_sync.matching import _time_seconds, match_translated_quote
        from translated_sync.transcript import TranscriptCue

        payload = request.get_json(silent=True) or {}
        reserved = False
        try:
            semantic = payload.get("semantic", False)
            if not isinstance(semantic, bool):
                raise ValueError("semantic must be a boolean")
            project = services._resolve_project_name(payload.get("project"))
            if semantic:
                if not services._reserve_sync_project(project):
                    return services._json_error(
                        "another mutating job is active for this project", 409
                    )
                reserved = True
            paths = services._ensure_project(project)
            with services._project_transition_lock(project), services._SYNC_DATA_LOCK:
                if not semantic and services._project_has_active_mutation(project):
                    return services._json_error(
                        "another mutating job is active for this project", 409
                    )
                confirmed, _ = services._confirmed_normalization(project)
                if confirmed is None:
                    return services._normalization_required(project) or services._json_error(
                        "normalization_confirmation_required", 409
                    )
                source_store, match_store = services._sync_stores(paths)
                all_sources = source_store.list()
                all_ids = {s.id for s in all_sources}
                selected = payload.get("source_ids", sorted(all_ids))
                if not isinstance(selected, list) or any(
                    not isinstance(x, str) or x not in all_ids for x in selected
                ):
                    raise ValueError("source_ids contains an unknown source")
                sources = [s for s in all_sources if s.id in selected]
                imports = {s.id: load_transcript(paths, s.id) for s in sources}
                cues = [
                    TranscriptCue.from_dict(c)
                    for doc in imports.values()
                    for c in (doc or {}).get("cues", [])
                ]
                if not cues:
                    raise ValueError("no imported transcript cues for selected sources")
                requested_mode = payload.get("timecode_mode")
                if requested_mode is not None and requested_mode not in {"relative", "display"}:
                    raise ValueError("invalid timecode_mode")
                modes = {doc.get("timecode_mode") for doc in imports.values() if doc is not None}
                if requested_mode is None and len(modes) > 1:
                    raise ValueError(
                        "selected transcript imports use mixed timecode modes; choose timecode_mode or match sources separately"
                    )
                effective_mode = requested_mode or next(iter(modes), "relative")
                matches = match_store.list()
                snapshot = json.dumps(
                    [
                        confirmed,
                        [s.to_dict() for s in all_sources],
                        imports,
                        [m.to_dict() for m in matches],
                    ],
                    sort_keys=True,
                    ensure_ascii=False,
                )
                by_id = {m.segment_id: m for m in matches}
                accepted, _, _ = classify_sync_matches(matches, confirmed, all_ids)
                current_confirmed = {m.segment_id for m in accepted if m.status == "confirmed"}
                blocks = [
                    b
                    for b in confirmed.get("blocks", [])
                    if str(b.get("type") or "").lower() in {"sync", "host"}
                ]
                units = services._sync_alignment_units(
                    blocks, preserve_whole_uids=_confirmed_parent_ids(matches, confirmed, all_ids)
                )
            saved = []
            calls = 0
            budget_exhausted = False
            limits = {
                "enabled": semantic,
                "max_candidates_per_unit": 24,
                "max_calls": 8,
                "timeout_seconds": 45,
                "calls": 0,
                "limited": False,
                "statuses": [],
            }
            budget = CostBudget.for_llm_tts(os.environ) if semantic else None
            source_map = {source.id: source for source in sources}
            for unit in units:
                segment_id, text = (
                    str(unit.get("segment_id") or ""),
                    str(unit.get("text") or "").strip(),
                )
                if not segment_id or not text:
                    continue
                old = by_id.get(segment_id)
                if old and (
                    old.segment_id in current_confirmed
                    or (old.source_id and old.source_id not in selected)
                    or (old.source_id and old.status == "auto_matched")
                ):
                    continue
                alignment = AlignmentRequest(
                    segment_id,
                    text,
                    speaker=str(unit.get("speaker") or ""),
                    approximate_timecode=unit.get("timecode_start"),
                    source_ids=tuple(selected),
                    timecode_mode=effective_mode,
                    source_hint=_unit_source_hint(unit),
                )
                match = match_translated_quote(alignment, cues, sources)
                if semantic and (match.status == "not_found" or match.confidence < 0.65):

                    def rank(cue):
                        source = source_map[cue.source_id]
                        hint = _time_seconds(alignment.approximate_timecode, source.fps)
                        if hint is not None:
                            hint = (
                                hint - (_time_seconds(source.sequence_start_tc, source.fps) or 0)
                                if effective_mode == "display"
                                else hint + source.guide_offset_sec
                            )
                        speaker = alignment.speaker.casefold()
                        mismatch = bool(
                            speaker
                            and cue.speaker
                            and speaker not in cue.speaker.casefold()
                            and cue.speaker.casefold() not in speaker
                        )
                        source_hint = alignment.source_hint.casefold()
                        source_names = (
                            source.id,
                            source.premiere_sequence,
                            source.sequence_id,
                            source.media_path,
                        )
                        source_mismatch = bool(
                            source_hint
                            and not any(
                                source_hint in name.casefold() for name in source_names if name
                            )
                        )
                        return (
                            source_mismatch,
                            mismatch,
                            abs(cue.start_sec - hint) if hint is not None else 0,
                            cue.source_id,
                            cue.start_sec,
                            cue.origin_index,
                        )

                    pool = sorted(cues, key=rank)
                    if len(pool) > 24:
                        limits["limited"] = True
                    pool = pool[:24]
                    if calls >= 8 or budget_exhausted:
                        limits["limited"] = True
                        limits["statuses"].append(
                            {
                                "segment_id": segment_id,
                                "status": "budget_exhausted" if budget_exhausted else "call_limit",
                            }
                        )
                    elif pool:
                        calls += 1
                        try:
                            candidates = [
                                SemanticCandidate(
                                    c.source_id, c.start_sec, c.end_sec, 0.0, c.russian_text
                                )
                                for c in pool
                            ]
                            decision = services.review_candidates_with_llm(
                                query_text=text,
                                speaker="",
                                candidates=candidates,
                                budget=budget,
                                timeout_seconds=45.0,
                                usage_callback=services._make_openrouter_usage_callback(
                                    project, stage="sync", operation="translated_word_semantic"
                                ),
                            )
                            if decision is None:
                                limits["statuses"].append(
                                    {"segment_id": segment_id, "status": "rejected_or_unavailable"}
                                )
                            else:
                                index = decision.candidate_index
                                if (
                                    isinstance(index, bool)
                                    or not isinstance(index, int)
                                    or not 0 <= index < len(pool)
                                    or not 0 <= decision.confidence <= 1
                                ):
                                    raise ValueError("invalid semantic decision")
                                cue = pool[index]
                                match.source_id, match.source_in_sec, match.source_out_sec = (
                                    cue.source_id,
                                    cue.start_sec,
                                    cue.end_sec,
                                )
                                (
                                    match.status,
                                    match.strategy,
                                    match.matched_text,
                                    match.confidence,
                                ) = (
                                    "needs_review",
                                    "translated_semantic",
                                    cue.russian_text,
                                    decision.confidence,
                                )
                                match.alternatives = [
                                    SyncAlternative(
                                        c.source_id, c.start_sec, c.end_sec, 0, c.russian_text, c.id
                                    )
                                    for c in pool
                                    if c.id != cue.id
                                ][:5]
                                match.metadata = {
                                    "document_cue_id": cue.id,
                                    "origin_index": cue.origin_index,
                                    "approximate": cue.approximate,
                                    "original_text": cue.original_text,
                                    "cue_speaker": cue.speaker,
                                    "raw_start_tc": cue.raw_start_tc,
                                    "raw_end_tc": cue.raw_end_tc,
                                    "explicit_end": cue.explicit_end,
                                    "timing_precision": "coarse_window",
                                }
                                limits["statuses"].append(
                                    {"segment_id": segment_id, "status": "selected"}
                                )
                        except Exception as exc:
                            if isinstance(exc, CostBudgetExceeded):
                                budget_exhausted = True
                                limits["limited"] = True
                            limits["statuses"].append(
                                {
                                    "segment_id": segment_id,
                                    "status": "error",
                                    "error": str(exc)[:240],
                                }
                            )
                match.metadata.update(
                    {
                        "normalization_revision": confirmed.get("revision"),
                        "scenario_revision_id": str(confirmed.get("scenario_revision_id") or ""),
                        "unit_text_sha256": unit_text_sha256(text),
                        "parent_block_uid": str(unit.get("parent_block_uid") or segment_id),
                        "fragment_index": int(unit.get("fragment_index") or 0),
                        "fragment_count": int(unit.get("fragment_count") or 1),
                    }
                )
                if old and "translation_audio" in old.metadata:
                    match.metadata["translation_audio"] = old.metadata["translation_audio"]
                by_id[segment_id] = match
                saved.append(match)
            limits["calls"] = calls
            with services._project_transition_lock(project), services._SYNC_DATA_LOCK:
                if not semantic and services._project_has_active_mutation(project):
                    return services._json_error(
                        "another mutating job is active for this project", 409
                    )
                now_confirmed, _ = services._confirmed_normalization(project)
                new_sources, new_matches = services._sync_stores(paths)
                now_sources = new_sources.list()
                now_imports = {
                    s.id: load_transcript(paths, s.id) for s in now_sources if s.id in selected
                }
                now = json.dumps(
                    [
                        now_confirmed,
                        [s.to_dict() for s in now_sources],
                        now_imports,
                        [m.to_dict() for m in new_matches.list()],
                    ],
                    sort_keys=True,
                    ensure_ascii=False,
                )
                if snapshot != now:
                    return services._json_error("matching inputs changed; retry", 409)
                new_matches.replace_all(list(by_id.values()))
            return jsonify(
                {
                    "ok": True,
                    "result": {
                        "project": project,
                        "matches": [services._match_api_dict(m) for m in saved],
                        "semantic": limits,
                    },
                }
            )
        except (ValueError, TypeError) as exc:
            return services._json_error(str(exc), 400)
        finally:
            if reserved:
                services._release_sync_project(project)

    def current(services, project, segment_id):
        from api_stage2_routes import _confirmed_parent_ids
        from stage_common import classify_sync_matches

        paths = services._ensure_project(project)
        confirmed, _reason = services._confirmed_normalization(project)
        if confirmed is None:
            raise ValueError("normalization_confirmation_required")
        sources, store = services._sync_stores(paths)
        matches = store.list()
        source_ids = {s.id for s in sources.list()}
        blocks = [
            b
            for b in confirmed.get("blocks", [])
            if str(b.get("type") or "").lower() in {"sync", "host"}
        ]
        units = services._sync_alignment_units(
            blocks,
            preserve_whole_uids=_confirmed_parent_ids(matches, confirmed, source_ids),
            include_host_standup=True,
        )
        unit = next((u for u in units if u.get("segment_id") == segment_id), None)
        accepted, _, _ = classify_sync_matches(matches, confirmed, source_ids)
        match = next((m for m in accepted if m.segment_id == segment_id), None)
        if not unit or not match or not match.source_id:
            raise ValueError("current sync match not found")
        return paths, store, match, str(unit.get("text") or "")

    def owned_file(paths, relative):
        from pathlib import Path

        root = Path(paths["root"]).resolve()
        path = (root / relative).resolve(strict=True)
        allowed = (
            Path(paths["voiceover_recordings"]).resolve(),
            (Path(paths["work"]) / "translated_sync" / "tts").resolve(),
        )
        if (
            not path.is_relative_to(root)
            or not path.is_file()
            or not any(path.is_relative_to(folder) for folder in allowed)
        ):
            raise ValueError("audio path is not an owned translated sync asset")
        return path

    def file_sha256(path):
        import hashlib

        with path.open("rb") as audio_file:
            return hashlib.file_digest(audio_file, "sha256").hexdigest()

    def recording_variant(paths, recording, text, values):
        from translated_sync.audio import text_sha256

        path = owned_file(paths, recording["asset"])
        return {
            "path": str(path),
            "sha256": file_sha256(path),
            "text_sha256": text_sha256(text),
            "duration_sec": recording["duration_sec"],
            "source_in_sec": float(values.get("source_in_sec", 0)),
            "source_out_sec": float(values.get("source_out_sec", recording["duration_sec"])),
            "recording_id": recording["id"],
        }

    def update_audio(paths, store, match, text, payload, variant=None):
        from copy import deepcopy
        from translated_sync.audio import resolve_translation_audio
        from voiceover.storage import load_recorded_voiceover

        config = deepcopy(match.metadata.get("translation_audio") or {"variants": {}})
        config.setdefault("variants", {})
        if "enabled" in payload and not isinstance(payload["enabled"], bool):
            raise ValueError("enabled must be a boolean")
        if payload.get("recording_id"):
            recordings = load_recorded_voiceover(paths)["recordings"]
            recording = next((r for r in recordings if r["id"] == payload["recording_id"]), None)
            if recording is None:
                raise ValueError("recording not found")
            variant = recording_variant(paths, recording, text, payload)
        if variant is not None:
            config["variants"]["recorded"] = variant
            config["active"] = "recorded"
            config["enabled"] = True
        for key in ("active", "delay_sec", "fade_sec", "duck_db", "gain_db"):
            if key in payload:
                config[key] = payload[key]
        if "enabled" in payload:
            config["enabled"] = payload["enabled"]
        elif "active" in payload:
            config["enabled"] = True
        if variant is None and any(k in payload for k in ("source_in_sec", "source_out_sec")):
            selected = config["variants"].get(config.get("active"))
            if not selected:
                raise ValueError("audio variant unavailable")
            for key in ("source_in_sec", "source_out_sec"):
                if key in payload:
                    selected[key] = payload[key]
        if config.get("enabled", True):
            projection = resolve_translation_audio(config, text)
            import wave

            file = owned_file(paths, projection["path"])
            if file_sha256(file) != projection["sha256"]:
                raise ValueError("audio checksum mismatch")
            with wave.open(str(file), "rb") as audio:
                duration = audio.getnframes() / audio.getframerate()
            if abs(duration - projection["source_duration_sec"]) > 0.001:
                raise ValueError("audio duration mismatch")
        match.metadata["translation_audio"] = config
        store.save(match)
        return config

    @app.route("/stage2/translated-sync/<segment_id>/audio", methods=["GET", "PATCH"])
    def translated_audio(segment_id):
        from voiceover.storage import load_recorded_voiceover

        payload = request.get_json(silent=True) or {}
        try:
            project = services._resolve_project_name(
                request.args.get("project") if request.method == "GET" else payload.get("project")
            )
            with services._project_transition_lock(project):
                if request.method != "GET" and services._project_has_active_mutation(project):
                    return services._json_error(
                        "another mutating job is active for this project", 409
                    )
                with services._SYNC_DATA_LOCK:
                    paths, store, match, text = current(services, project, segment_id)
                    config = match.metadata.get("translation_audio") or {"variants": {}}
                    if request.method == "PATCH":
                        config = update_audio(paths, store, match, text, payload)
                    recordings = load_recorded_voiceover(paths)["recordings"]
            return jsonify(
                {
                    "ok": True,
                    "result": {
                        "segment_id": segment_id,
                        "text": text,
                        "translation_audio": config,
                        "recordings": recordings,
                    },
                }
            )
        except (ValueError, TypeError, OSError, EOFError, wave.Error) as exc:
            return services._json_error(str(exc), 400)

    @app.post("/stage2/translated-sync/<segment_id>/recording")
    def translated_recording(segment_id):
        from pathlib import Path
        from voiceover.storage import (
            prepare_recording_file,
            commit_prepared_recording,
            discard_prepared_recording,
            PreparedRecording,
        )

        try:
            project = services._resolve_project_name(request.form.get("project"))
            upload = request.files.get("audio")
            if not upload or Path(upload.filename or "").suffix.lower() not in {".wav", ".mp3"}:
                raise ValueError("WAV or MP3 audio is required")
            with services._project_transition_lock(project):
                if services._project_has_active_mutation(project):
                    return services._json_error(
                        "another mutating job is active for this project", 409
                    )
                with services._SYNC_DATA_LOCK:
                    paths, _, initial_match, initial_text = current(services, project, segment_id)
                    normalization, _ = services._confirmed_normalization(project)
                    snapshot = (
                        normalization.get("revision"),
                        normalization.get("scenario_revision_id"),
                        initial_text,
                        initial_match.source_id,
                        initial_match.source_in_sec,
                        initial_match.source_out_sec,
                    )
            inbox = Path(paths["inbox_voiceover"])
            inbox.mkdir(parents=True, exist_ok=True)
            import uuid

            temp = inbox / (uuid.uuid4().hex + Path(upload.filename).suffix.lower())
            prepared = None
            try:
                services._copy_stream_limited(
                    upload.stream,
                    temp,
                    int(services.app.config.get("SYNC_AUDIO_MAX_BYTES") or 4 * 1024**3),
                )
                prepared = prepare_recording_file(paths, temp)
                with services._project_transition_lock(project):
                    if services._project_has_active_mutation(project):
                        return services._json_error(
                            "another mutating job is active for this project", 409
                        )
                    with services._SYNC_DATA_LOCK:
                        paths, store, match, text = current(services, project, segment_id)
                        normalization, _ = services._confirmed_normalization(project)
                        now = (
                            normalization.get("revision"),
                            normalization.get("scenario_revision_id"),
                            text,
                            match.source_id,
                            match.source_in_sec,
                            match.source_out_sec,
                        )
                        if now != snapshot:
                            return services._json_error(
                                "sync match changed during recording preparation", 409
                            )
                        values = {
                            k: request.form[k]
                            for k in ("source_in_sec", "source_out_sec")
                            if k in request.form
                        }
                        import math

                        start = float(values.get("source_in_sec", 0))
                        end = float(values.get("source_out_sec", prepared.duration_sec))
                        if (
                            not math.isfinite(start)
                            or not math.isfinite(end)
                            or not 0 <= start < end <= prepared.duration_sec
                        ):
                            raise ValueError("recording source range is invalid")
                        recording = (
                            commit_prepared_recording(paths, prepared)
                            if isinstance(prepared, PreparedRecording)
                            else prepared
                        )
                        config = update_audio(
                            paths,
                            store,
                            match,
                            text,
                            {},
                            recording_variant(paths, recording.to_dict(), text, values),
                        )
                return jsonify(
                    {"ok": True, "result": {"segment_id": segment_id, "translation_audio": config}}
                )
            finally:
                temp.unlink(missing_ok=True)
                if isinstance(prepared, PreparedRecording):
                    discard_prepared_recording(prepared)
        except (ValueError, TypeError, OSError, EOFError, wave.Error) as exc:
            return services._json_error(str(exc), 400)

    @app.get("/stage2/translated-sync/<segment_id>/audio-file")
    def translated_audio_file(segment_id):
        from copy import deepcopy
        from flask import send_file
        from translated_sync.audio import resolve_translation_audio

        try:
            project = services._resolve_project_name(request.args.get("project"))
            with services._project_transition_lock(project), services._SYNC_DATA_LOCK:
                paths, _, match, text = current(services, project, segment_id)
                config = deepcopy(match.metadata.get("translation_audio") or {})
                if request.args.get("kind"):
                    config["active"] = request.args["kind"]
                projection = resolve_translation_audio(config, text)
                path = owned_file(paths, projection["path"])
                if file_sha256(path) != projection["sha256"]:
                    raise ValueError("audio checksum mismatch")
                return send_file(path, mimetype="audio/wav", conditional=True)
        except (ValueError, TypeError, OSError, EOFError, wave.Error) as exc:
            return services._json_error(str(exc), 400)

    @app.post("/stage2/translated-sync/<segment_id>/tts")
    def translated_tts(segment_id):
        import hashlib
        import json
        import math
        import os
        from pathlib import Path
        import tempfile
        import wave
        from api_stage1_routes import _resolve_tts_direction
        from cost_budget import CostBudget, CostBudgetExceeded
        from translated_sync.audio import resolve_translation_audio, text_sha256

        payload = request.get_json(silent=True) or {}
        try:
            project = services._resolve_project_name(payload.get("project"))
            provider = payload.get("provider")
            if provider and str(provider).lower() not in {"macos", "openrouter"}:
                raise ValueError("invalid TTS provider")
            speed = float(payload.get("speed", 1))
            if not math.isfinite(speed) or not 0.5 <= speed <= 1.5:
                raise ValueError("speed must be between 0.5 and 1.5")
            model, style = _resolve_tts_direction(services, payload, provider)
            if not services._reserve_tts_project(project):
                return services._json_error("another mutating job is active for this project", 409)
            try:
                with services._project_transition_lock(project), services._SYNC_DATA_LOCK:
                    paths, _, match, text = current(services, project, segment_id)
                    normalization, _ = services._confirmed_normalization(project)
                    snapshot = (
                        normalization.get("revision"),
                        normalization.get("scenario_revision_id"),
                        text,
                        match.source_id,
                        match.source_in_sec,
                        match.source_out_sec,
                        match.metadata.get("normalization_revision"),
                        match.metadata.get("scenario_revision_id"),
                    )
                directory = Path(paths["work"]) / "translated_sync" / "tts"
                directory.mkdir(parents=True, exist_ok=True)
                with tempfile.TemporaryDirectory(dir=directory) as temporary:
                    result = services.synthesize_phrase(
                        text=text,
                        speaker_dir=Path(temporary),
                        provider=provider,
                        voice_name=payload.get("voice_name"),
                        gender=payload.get("gender"),
                        file_name="speech.wav",
                        dry_run=False,
                        speed=speed,
                        usage_callback=services._make_openrouter_usage_callback(
                            project, stage="voiceover", operation="translated_sync_tts"
                        ),
                        tts_model=model,
                        tts_style=style,
                        budget=CostBudget.for_llm_tts(os.environ),
                    )
                    output = Path(result.get("file", "")).resolve(strict=True)
                    if not output.is_relative_to(Path(temporary).resolve()):
                        raise ValueError("synthesized audio is outside its temporary directory")
                    with wave.open(str(output), "rb") as wav:
                        duration = wav.getnframes() / wav.getframerate()
                    if duration <= 0:
                        raise ValueError("synthesized WAV is empty")
                    digest = file_sha256(output)
                    settings = json.dumps(
                        [
                            text,
                            provider,
                            payload.get("voice_name"),
                            payload.get("gender"),
                            speed,
                            model,
                            style,
                        ],
                        ensure_ascii=False,
                    ).encode()
                    destination = directory / (
                        hashlib.sha256(settings).hexdigest() + "-" + digest + ".wav"
                    )
                    with services._project_transition_lock(project), services._SYNC_DATA_LOCK:
                        paths, store, match, new_text = current(services, project, segment_id)
                        normalization, _ = services._confirmed_normalization(project)
                        now = (
                            normalization.get("revision"),
                            normalization.get("scenario_revision_id"),
                            new_text,
                            match.source_id,
                            match.source_in_sec,
                            match.source_out_sec,
                            match.metadata.get("normalization_revision"),
                            match.metadata.get("scenario_revision_id"),
                        )
                        if snapshot != now:
                            return services._json_error("sync match changed during synthesis", 409)
                        if not destination.exists():
                            os.replace(output, destination)
                        config = dict(match.metadata.get("translation_audio") or {})
                        config["variants"] = dict(config.get("variants") or {})
                        config["enabled"] = True
                        config["variants"]["tts"] = {
                            "path": str(destination.resolve()),
                            "sha256": digest,
                            "text_sha256": text_sha256(text),
                            "duration_sec": duration,
                            "source_in_sec": 0.0,
                            "source_out_sec": duration,
                        }
                        config["active"] = "tts"
                        resolve_translation_audio(config, text)
                        match.metadata["translation_audio"] = config
                        store.save(match)
                return jsonify(
                    {"ok": True, "result": {"segment_id": segment_id, "translation_audio": config}}
                )
            finally:
                services._release_tts_project(project)
        except CostBudgetExceeded as exc:
            return services._json_error(str(exc), 409)
        except (ValueError, TypeError, OSError, wave.Error) as exc:
            return services._json_error(str(exc), 400)
        except Exception as exc:
            return services._internal_error(exc)
