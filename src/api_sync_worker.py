from __future__ import annotations

from datetime import datetime, timezone
from typing import TypedDict


def _parse_iso_datetime(value: object) -> datetime | None:
    """Parse an ISO-8601 timestamp, tolerating a bare ``Z`` suffix.

    Returns ``None`` when ``value`` is not a parseable timestamp so callers
    can decide how to treat unrecognized input.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


class _AsrMetrics(TypedDict):
    """Per-key typed job-status fields for ASR usage metrics.

    A plain ``dict[str, object]`` forces every value to the ``object``
    type, which breaks ``+=`` on the numeric fields under mypy (see
    api_sync_worker.py history). A ``TypedDict`` keeps each key's own
    declared type (``str``/``float``) without changing the dict's shape,
    so it still unpacks into ``_update_sync_job(**asr_metrics)`` exactly
    as before.
    """

    asr_backend_used: str
    asr_cost_usd_total: float
    asr_timestamp_precision: str
    transcription_wall_sec_total: float


def run_sync_job(
    services,
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
    _project_paths = services._project_paths
    _sync_stores = services._sync_stores
    _sync_cancel_event = services._sync_cancel_event
    threading = services.threading
    time = services.time
    _JobCancelled = services._JobCancelled
    _update_sync_job = services._update_sync_job
    _utc_now_iso = services._utc_now_iso
    _word_index_path = services._word_index_path
    _transcription_config = services._transcription_config
    _resolve_sync_audio_path = services._resolve_sync_audio_path
    load_word_index = services.load_word_index
    _TRANSCRIPTION_CACHE_KEY = services._TRANSCRIPTION_CACHE_KEY
    _transcription_cache_metadata = services._transcription_cache_metadata
    save_word_index = services.save_word_index
    _audio_stat_signature = services._audio_stat_signature
    _audio_cache_signature = services._audio_cache_signature
    app = services.app
    os = services.os
    transcribe_audio = services.transcribe_audio
    ingest_word_index = services.ingest_word_index
    WhisperCancelledError = services.WhisperCancelledError
    _word_index_audio_matches = services._word_index_audio_matches
    load_normalization = services.load_normalization
    _sync_alignment_units = services._sync_alignment_units
    SyncMatcher = services.SyncMatcher
    MatcherConfig = services.MatcherConfig
    get_settings = services.get_settings
    _semantic_review_candidates = services._semantic_review_candidates
    normalize_text = services.normalize_text
    review_candidates_with_llm = services.review_candidates_with_llm
    _apply_semantic_review = services._apply_semantic_review
    AlignmentRequest = services.AlignmentRequest
    _SYNC_DATA_LOCK = services._SYNC_DATA_LOCK
    _sync_matching_fingerprint = services._sync_matching_fingerprint
    _sync_summary = services._sync_summary
    _release_sync_project = services._release_sync_project
    _SYNC_JOBS_LOCK = services._SYNC_JOBS_LOCK
    _SYNC_CANCEL_EVENTS = services._SYNC_CANCEL_EVENTS
    paths = _project_paths(project)
    source_store, match_store = _sync_stores(paths)
    cancel_event = _sync_cancel_event(job_id)
    progress_persist_lock = threading.Lock()
    last_progress_persist_at = 0.0
    asr_metrics: _AsrMetrics = {
        "asr_backend_used": "",
        "asr_cost_usd_total": 0.0,
        "asr_timestamp_precision": "",
        "transcription_wall_sec_total": 0.0,
    }

    def should_persist_progress(*, force: bool = False) -> bool:
        nonlocal last_progress_persist_at
        now = time.monotonic()
        with progress_persist_lock:
            if not force and now - last_progress_persist_at < 1.0:
                return False
            last_progress_persist_at = now
            return True

    def checkpoint_matches() -> None:
        return None

    def ensure_not_cancelled() -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise _JobCancelled("Sync job cancelled")

    try:
        ensure_not_cancelled()
        _update_sync_job(
            job_id,
            status="running",
            message="Preparing word indexes",
            started_at=_utc_now_iso(),
            whisper_model=str(model or os.getenv("SYNC_WHISPER_MODEL") or ""),
            source_scope=source_scope,
            asr_engine=asr_engine,
        )
        all_sources = source_store.list()
        # ``source_ids`` scopes forced transcription, not the matching pool.
        # This lets the CEP panel export one guide at a time while every
        # already-indexed source remains eligible for scenario matching.
        sources = (
            [source for source in all_sources if source.source_kind == "review_media"]
            if source_scope == "review_media"
            else all_sources
        )
        indexes = []
        source_failures: list[dict[str, str]] = []
        transcribed = 0
        reused = 0
        _update_sync_job(job_id, total_sources=len(sources))

        for source in sources:
            ensure_not_cancelled()
            _update_sync_job(job_id, current_source_id=source.id)
            index_path = _word_index_path(paths, source.id)
            selected_for_transcription = not source_ids or source.id in source_ids
            cached_index = None
            audio_path = None
            try:
                should_force = force_transcribe and selected_for_transcription
                config = _transcription_config(backend=backend, model=model, language=language)
                audio_resolution_error = None
                try:
                    audio_path = _resolve_sync_audio_path(source, paths)
                    audio_is_available = audio_path.is_file()
                except ValueError as exc:
                    audio_resolution_error = exc
                    audio_path = None
                    audio_is_available = False
                audio_signature = (
                    _audio_cache_signature(audio_path)
                    if audio_is_available and audio_path is not None
                    else None
                )

                cache_load_error = None
                if index_path.is_file():
                    try:
                        cached_index = load_word_index(index_path, source.id)
                    except (OSError, TypeError, ValueError) as exc:
                        cache_load_error = exc
                if cached_index is None and not audio_is_available:
                    if cache_load_error is not None:
                        raise cache_load_error
                    if audio_resolution_error is not None:
                        raise audio_resolution_error

                # Preserve legacy indexes on their first read. We cannot know
                # whether an older file was generated or manually corrected,
                # so migrate its cache metadata without changing its words.
                if (
                    cached_index is not None
                    and audio_is_available
                    and audio_path is not None
                    and not isinstance(cached_index.metadata.get(_TRANSCRIPTION_CACHE_KEY), dict)
                ):
                    cached_index.metadata[_TRANSCRIPTION_CACHE_KEY] = _transcription_cache_metadata(
                        audio_path,
                        config,
                        origin="legacy",
                        audio_signature=audio_signature,
                    )
                    save_word_index(index_path, cached_index)

                if cached_index is not None and audio_is_available and audio_path is not None:
                    cache_metadata = cached_index.metadata.get(_TRANSCRIPTION_CACHE_KEY)
                    stored_audio = (
                        cache_metadata.get("audio") if isinstance(cache_metadata, dict) else None
                    )
                    if (
                        isinstance(stored_audio, dict)
                        and not stored_audio.get("sample_sha256")
                        and stored_audio == _audio_stat_signature(audio_path)
                    ):
                        cache_metadata["audio"] = audio_signature
                        save_word_index(index_path, cached_index)

                cache_matches_audio = bool(
                    cached_index is not None
                    and audio_is_available
                    and audio_path is not None
                    and _word_index_audio_matches(
                        cached_index,
                        audio_path,
                        audio_signature=audio_signature,
                    )
                )
                if source_ids and not selected_for_transcription:
                    if cached_index is None:
                        if cache_load_error is not None:
                            raise cache_load_error
                        # Another registered sequence has not been indexed yet.
                        # It joins the matching pool after its own CEP run.
                        continue
                    # A scoped CEP job must never unexpectedly transcribe other
                    # sources. Keep their last good indexes in the matching pool;
                    # they will be refreshed when selected by a later job.
                    index = cached_index
                    reused += 1
                elif (
                    cached_index is not None
                    and not should_force
                    and (cache_matches_audio or not audio_is_available)
                ):
                    # A completed transcript is an expensive durable artifact.
                    # Switching Small/Medium changes only future files; it must
                    # not invalidate a transcript for unchanged media.
                    index = cached_index
                    reused += 1
                else:
                    if audio_path is None:
                        raise FileNotFoundError(
                            f"audio guide not found for sync source {source.id}"
                        )
                    source_duration = max(0.0, float(source.duration_sec or 0.0))
                    _update_sync_job(
                        job_id,
                        message="Transcribing audio",
                        transcription_processed_sec=0.0,
                        transcription_duration_sec=source_duration,
                        transcription_words=0,
                        transcription_source_started_at=_utc_now_iso(),
                        transcription_timeout_seconds=float(
                            app.config.get("SYNC_WHISPER_TIMEOUT_SECONDS")
                            or os.getenv("SYNC_WHISPER_TIMEOUT_SECONDS")
                            or 4 * 60 * 60
                        ),
                    )

                    def report_progress(
                        processed_sec: float,
                        duration_sec: float,
                        word_count: int,
                    ) -> None:
                        if cancel_event is not None and cancel_event.is_set():
                            return
                        _update_sync_job(
                            job_id,
                            persist=should_persist_progress(),
                            transcription_processed_sec=processed_sec,
                            transcription_duration_sec=(duration_sec or source_duration),
                            transcription_words=word_count,
                        )

                    input_audio_signature = audio_signature or _audio_cache_signature(audio_path)
                    transcription_started_at = time.monotonic()
                    result = transcribe_audio(
                        audio_path,
                        backend=backend,
                        model=model,
                        language=language,
                        progress_callback=report_progress,
                        cancel_event=cancel_event,
                        timeout_seconds=(app.config.get("SYNC_WHISPER_TIMEOUT_SECONDS") or None),
                    )
                    transcription_wall_sec = time.monotonic() - transcription_started_at
                    if _audio_cache_signature(audio_path) != input_audio_signature:
                        raise RuntimeError(
                            f"audio guide changed during transcription: {audio_path.name}"
                        )
                    result_backend_metadata = result.get("backend_metadata") or {}
                    asr_metrics["transcription_wall_sec_total"] += transcription_wall_sec
                    asr_metrics["asr_backend_used"] = str(result.get("backend") or backend or "")
                    asr_metrics["asr_cost_usd_total"] += float(
                        result_backend_metadata.get("cost_usd") or 0.0
                    )
                    asr_metrics["asr_timestamp_precision"] = str(
                        result_backend_metadata.get("timestamp_precision") or "word"
                    )
                    _update_sync_job(job_id, persist=False, **asr_metrics)
                    index = ingest_word_index(
                        result,
                        source.id,
                        model=str(result.get("model") or model or ""),
                        language=str(result.get("language") or language),
                    )
                    index.metadata[_TRANSCRIPTION_CACHE_KEY] = _transcription_cache_metadata(
                        audio_path,
                        config,
                        audio_signature=input_audio_signature,
                    )
                    save_word_index(index_path, index)
                    transcribed += 1
                indexes.append(index)
            except Exception as exc:
                if isinstance(exc, WhisperCancelledError) or (
                    cancel_event is not None and cancel_event.is_set()
                ):
                    raise _JobCancelled("Sync job cancelled") from exc
                if (
                    cached_index is not None
                    and audio_path is not None
                    and _word_index_audio_matches(cached_index, audio_path)
                ):
                    # A decoding/config refresh may fail temporarily. Keep the
                    # last transcript for matching when it still describes the
                    # exact same WAV; never fall back across an audio change.
                    indexes.append(cached_index)
                    reused += 1
                source_failures.append({"source_id": source.id, "error": str(exc)})
            _update_sync_job(
                job_id,
                transcribed=transcribed,
                reused_indexes=reused,
                source_failures=list(source_failures),
            )

        normalization = load_normalization(paths["normalized"])
        normalization_revision = normalization.get("revision")
        scenario_revision_id = str(normalization.get("scenario_revision_id") or "")
        blocks = [
            block
            for block in sorted(
                normalization.get("blocks", []),
                key=lambda item: int(item.get("order", 0) or 0),
            )
            if str(block.get("type") or "").lower() in {"sync", "host"}
        ]
        units = _sync_alignment_units(blocks)
        indexed_source_ids = {item.source_id for item in indexes}
        matching_sources = [item for item in sources if item.id in indexed_source_ids]
        matcher = SyncMatcher(
            matching_sources,
            indexes,
            MatcherConfig(search_window_sec=search_window_sec),
        )
        settings = get_settings()
        llm_review_enabled = bool(settings.openrouter_api_key) and str(
            os.getenv("SYNC_LLM_REVIEW_ENABLED", "1")
        ).strip().lower() not in {"0", "false", "no", "off"}
        try:
            llm_review_threshold = float(os.getenv("SYNC_LLM_REVIEW_THRESHOLD", "0.80"))
        except ValueError:
            llm_review_threshold = 0.80
        llm_review_threshold = max(0.0, min(1.0, llm_review_threshold))
        try:
            llm_review_limit = max(0, int(os.getenv("SYNC_LLM_REVIEW_MAX_BLOCKS", "30")))
        except ValueError:
            llm_review_limit = 30
        llm_reviewed = 0
        llm_review_failures: list[dict[str, str]] = []
        existing = {item.segment_id: item for item in match_store.list()}
        checkpoint_state = {"dirty": False}
        matching_started_at = _utc_now_iso()
        matching_started_at_dt = _parse_iso_datetime(matching_started_at)
        rematched_this_run: set[str] = set()

        def checkpoint_matches() -> None:
            if not checkpoint_state["dirty"]:
                return
            with _SYNC_DATA_LOCK:
                persisted = {item.segment_id: item for item in match_store.list()}
                # A manual edit is only authoritative over a fresh rematch when
                # it happened concurrently with this job (edited at/after
                # ``matching_started_at``), or when this run never recomputed
                # the segment at all. An older confirmation/edit that predates
                # the job must yield to an explicit forced rematch, otherwise
                # ``rematch_confirmed=True`` can never actually replace a
                # previously confirmed range.
                for persisted_id, persisted_match in persisted.items():
                    persisted_revision = persisted_match.metadata.get("normalization_revision")
                    persisted_scenario_revision_id = str(
                        persisted_match.metadata.get("scenario_revision_id") or ""
                    )
                    persisted_is_current = persisted_revision in (
                        None,
                        "",
                        normalization_revision,
                    ) and (
                        not persisted_scenario_revision_id
                        or not scenario_revision_id
                        or persisted_scenario_revision_id == scenario_revision_id
                    )
                    manual_updated_at = persisted_match.metadata.get("manual_updated_at")
                    if manual_updated_at:
                        manual_updated_at_dt = _parse_iso_datetime(manual_updated_at)
                        if manual_updated_at_dt is None or matching_started_at_dt is None:
                            # Unparseable timestamp: stay conservative and
                            # treat the manual edit as authoritative so we
                            # never silently discard human work.
                            edited_during_run = True
                        else:
                            edited_during_run = manual_updated_at_dt >= matching_started_at_dt
                    elif persisted_match.status == "confirmed":
                        # A confirmed record with no manual-edit timestamp at
                        # all cannot be time-compared against the job start
                        # (legacy data, or a confirmation raced in without
                        # going through the PATCH endpoint). Stay
                        # conservative rather than silently discard it.
                        edited_during_run = True
                    else:
                        edited_during_run = False
                    not_rematched_this_run = persisted_id not in rematched_this_run
                    if persisted_is_current and (edited_during_run or not_rematched_this_run):
                        existing[persisted_id] = persisted_match
                    elif persisted_id not in existing:
                        existing[persisted_id] = persisted_match
                match_store.replace_all(list(existing.values()))
                checkpoint_state["dirty"] = False

        current_ids = {
            str(unit.get("segment_id") or "").strip()
            for unit in units
            if str(unit.get("segment_id") or "").strip()
        }
        matching_fingerprint = _sync_matching_fingerprint(
            units=units,
            sources=matching_sources,
            indexes=indexes,
            normalization_revision=normalization_revision,
            timecode_mode=timecode_mode,
            search_window_sec=search_window_sec,
            llm_enabled=llm_review_enabled,
            llm_model=settings.openrouter_model,
            llm_threshold=llm_review_threshold,
            llm_limit=llm_review_limit,
            scenario_revision_id=scenario_revision_id,
        )
        total_segments = len(current_ids)
        _update_sync_job(
            job_id,
            message="Matching scenario quotes",
            total_segments=total_segments,
            total=total_segments,
            current_source_id="",
            matching_started_at=matching_started_at,
            matching_fingerprint=matching_fingerprint,
            scenario_revision_id=scenario_revision_id,
        )

        segment_failures: list[dict[str, str]] = []
        processed = 0
        for unit in units:
            ensure_not_cancelled()
            segment_id = str(unit.get("segment_id") or "").strip()
            if not segment_id:
                segment_failures.append(
                    {
                        "segment_id": "",
                        "error": "sync/host block has no stable uid",
                    }
                )
                continue
            _update_sync_job(
                job_id,
                persist=False,
                current_segment_id=segment_id,
            )
            prior = existing.get(segment_id)
            prior_revision = prior.metadata.get("normalization_revision") if prior else None
            prior_scenario_revision_id = (
                str(prior.metadata.get("scenario_revision_id") or "") if prior else ""
            )
            prior_is_current = (
                prior_revision in (None, "", normalization_revision)
                # Legacy matches predate scenario identities. Keep accepting an
                # empty ID, but never reuse a match explicitly owned by another
                # scenario revision even when numeric revisions happen to match.
                and (
                    not prior_scenario_revision_id
                    or not scenario_revision_id
                    or prior_scenario_revision_id == scenario_revision_id
                )
            )
            prior_matching_fingerprint = (
                str(prior.metadata.get("matching_fingerprint") or "") if prior else ""
            )
            if prior and not prior_is_current:
                # Do not let a stale match contribute to progress/final summary
                # or survive a failed rematch for the active scenario revision.
                existing.pop(segment_id, None)
            if (
                prior
                and prior_is_current
                and (
                    (prior.status == "confirmed" and not rematch_confirmed)
                    or (
                        prior.status != "confirmed"
                        and prior_matching_fingerprint == matching_fingerprint
                    )
                )
            ):
                processed += 1
                current = [item for key, item in existing.items() if key in current_ids]
                _update_sync_job(
                    job_id,
                    persist=should_persist_progress(),
                    done=processed,
                    **_sync_summary(current),
                    segment_failures=list(segment_failures),
                )
                continue
            try:
                match = matcher.match(
                    AlignmentRequest(
                        segment_id=segment_id,
                        text=str(unit.get("text") or ""),
                        speaker=str(unit.get("speaker") or ""),
                        approximate_timecode=unit.get("timecode_start"),
                        timecode_mode=timecode_mode,  # type: ignore[arg-type]
                    )
                )
                semantic_candidates = _semantic_review_candidates(match)
                if (
                    llm_review_enabled
                    and llm_reviewed < llm_review_limit
                    and match.status != "auto_matched"
                    and match.confidence < llm_review_threshold
                    and len(normalize_text(match.query_text)) >= 5
                    and semantic_candidates
                ):
                    try:
                        decision = review_candidates_with_llm(
                            query_text=match.query_text,
                            speaker=match.speaker,
                            candidates=semantic_candidates,
                            settings=settings,
                        )
                        llm_reviewed += 1
                        if decision is not None:
                            match = _apply_semantic_review(
                                match,
                                semantic_candidates,
                                decision.candidate_index,
                                llm_confidence=decision.confidence,
                                reason=decision.reason,
                                model=decision.model,
                            )
                    except Exception as exc:
                        llm_reviewed += 1
                        llm_review_failures.append(
                            {
                                "segment_id": segment_id,
                                "error": str(exc),
                            }
                        )
                match.metadata["normalization_revision"] = normalization_revision
                match.metadata["scenario_revision_id"] = scenario_revision_id
                match.metadata["matching_fingerprint"] = matching_fingerprint
                match.metadata["parent_block_uid"] = str(unit.get("parent_block_uid") or segment_id)
                match.metadata["fragment_index"] = int(unit.get("fragment_index") or 0)
                match.metadata["fragment_count"] = int(unit.get("fragment_count") or 1)
                existing[segment_id] = match
                rematched_this_run.add(segment_id)
                checkpoint_state["dirty"] = True
            except Exception as exc:
                segment_failures.append(
                    {
                        "segment_id": segment_id,
                        "error": str(exc),
                    }
                )
            processed += 1
            if processed % 10 == 0:
                checkpoint_matches()
            current = [item for key, item in existing.items() if key in current_ids]
            _update_sync_job(
                job_id,
                persist=should_persist_progress(force=(processed % 10 == 0)),
                done=processed,
                **_sync_summary(current),
                segment_failures=list(segment_failures),
                llm_reviewed=llm_reviewed,
                llm_review_failures=list(llm_review_failures),
            )

        checkpoint_matches()
        all_matches = [item for key, item in existing.items() if key in current_ids]
        ensure_not_cancelled()
        summary = _sync_summary(all_matches)
        _update_sync_job(
            job_id,
            status="completed",
            message="Completed",
            finished_at=_utc_now_iso(),
            current_segment_id="",
            source_failures=source_failures,
            segment_failures=segment_failures,
            llm_reviewed=llm_reviewed,
            llm_review_failures=llm_review_failures,
            **asr_metrics,
            **summary,
        )
    except _JobCancelled:
        try:
            checkpoint_matches()
        except Exception:
            app.logger.exception("Could not checkpoint cancelled sync job=%s", job_id)
        _update_sync_job(
            job_id,
            status="cancelled",
            message="Cancelled; saved indexes and matches can be resumed",
            current_source_id="",
            current_segment_id="",
            finished_at=_utc_now_iso(),
        )
    except Exception as exc:
        app.logger.exception(
            "Sync job failed project=%s job=%s",
            project,
            job_id,
        )
        try:
            checkpoint_matches()
        except Exception:
            app.logger.exception("Could not checkpoint failed sync job=%s", job_id)
        _update_sync_job(
            job_id,
            status="failed",
            message="Failed",
            error=str(exc),
            finished_at=_utc_now_iso(),
        )
    finally:
        _release_sync_project(project)
        with _SYNC_JOBS_LOCK:
            _SYNC_CANCEL_EVENTS.pop(job_id, None)
