from __future__ import annotations

import logging

from api_service_contracts import SyncWorkerServices
from sync_asr_phase import SyncAsrContext, run_asr_phase
from sync_match_phase import (
    SyncMatchCheckpoint,
    SyncMatchContext,
    run_match_phase,
)
from sync_cost_tracker import SyncCostTracker, _AsrMetrics

logger = logging.getLogger(__name__)


def run_sync_job(
    services: SyncWorkerServices,
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
    rescale_stretched_word_index = services.rescale_stretched_word_index
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

    cost_tracker = SyncCostTracker(
        job_id=job_id,
        update_job=_update_sync_job,
        should_persist=should_persist_progress,
        asr_metrics=asr_metrics,
        logger=logger,
    )

    # The cancel/failure handlers below checkpoint even when the job stops
    # before matching starts; the checkpoint is a no-op until the matching
    # phase activates it.
    match_checkpoint = SyncMatchCheckpoint()

    def ensure_not_cancelled() -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise _JobCancelled("Sync job cancelled")

    try:
        # Setup lives inside the try so a failure here still marks the job
        # failed and runs ``finally`` (project release, cancel event cleanup).
        paths = _project_paths(project)
        asr_usage_callback = services._make_openrouter_usage_callback(
            project, stage="assembly", operation="sync_asr", job_id=job_id
        )
        semantic_usage_callback = services._make_openrouter_usage_callback(
            project, stage="assembly", operation="sync_llm_review", job_id=job_id
        )
        source_store, match_store = _sync_stores(paths)
        cancel_event = _sync_cancel_event(job_id)
        job = services._get_sync_job(job_id) or {}
        cloud_source_options = job.get("cloud_source_options")
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
        _update_sync_job(job_id, total_sources=len(sources))

        asr_result = run_asr_phase(
            SyncAsrContext(
                job_id=job_id,
                paths=paths,
                backend=backend,
                model=model,
                language=language,
                source_ids=source_ids,
                force_transcribe=force_transcribe,
                cloud_source_options=cloud_source_options,
                cancel_event=cancel_event,
                ensure_not_cancelled=ensure_not_cancelled,
                should_persist_progress=should_persist_progress,
                asr_usage_callback=asr_usage_callback,
                asr_semaphore=services._ASR_SEMAPHORE,
                getenv=lambda name: os.getenv(name),
                monotonic=lambda: time.monotonic(),
                app_config=app.config,
                job_cancelled_error=_JobCancelled,
                whisper_cancelled_error=WhisperCancelledError,
                transcription_cache_key=_TRANSCRIPTION_CACHE_KEY,
                update_job=_update_sync_job,
                utc_now_iso=_utc_now_iso,
                word_index_path=_word_index_path,
                transcription_config=_transcription_config,
                resolve_audio_path=_resolve_sync_audio_path,
                load_word_index=load_word_index,
                save_word_index=save_word_index,
                rescale_stretched_word_index=rescale_stretched_word_index,
                transcription_cache_metadata=_transcription_cache_metadata,
                audio_stat_signature=_audio_stat_signature,
                audio_cache_signature=_audio_cache_signature,
                word_index_audio_matches=_word_index_audio_matches,
                transcribe_audio=transcribe_audio,
                ingest_word_index=ingest_word_index,
            ),
            sources,
            cost_tracker=cost_tracker,
            asr_metrics=asr_metrics,
        )
        indexes = asr_result.indexes
        source_failures = asr_result.source_failures

        match_result = run_match_phase(
            SyncMatchContext(
                job_id=job_id,
                paths=paths,
                match_store=match_store,
                # str validated upstream (start route); AlignmentRequest re-checks it
                timecode_mode=timecode_mode,  # type: ignore[arg-type]
                rematch_confirmed=rematch_confirmed,
                search_window_sec=search_window_sec,
                job_llm_cost_env=job.get("llm_cost_env"),
                ensure_not_cancelled=ensure_not_cancelled,
                should_persist_progress=should_persist_progress,
                semantic_usage_callback=semantic_usage_callback,
                logger=logger,
                getenv=lambda name, default: os.getenv(name, default),
                environ=lambda: os.environ,
                data_lock=_SYNC_DATA_LOCK,
                update_job=_update_sync_job,
                utc_now_iso=_utc_now_iso,
                load_normalization=load_normalization,
                alignment_units=_sync_alignment_units,
                matcher_cls=SyncMatcher,
                matcher_config_cls=MatcherConfig,
                get_settings=get_settings,
                semantic_review_candidates=_semantic_review_candidates,
                normalize_text=normalize_text,
                review_candidates_with_llm=review_candidates_with_llm,
                apply_semantic_review=_apply_semantic_review,
                alignment_request_cls=AlignmentRequest,
                matching_fingerprint=_sync_matching_fingerprint,
                sync_summary=_sync_summary,
                word_index_path=_word_index_path,
            ),
            sources,
            indexes,
            cost_tracker=cost_tracker,
            checkpoint=match_checkpoint,
        )
        ensure_not_cancelled()
        summary = _sync_summary(match_result.matches)
        _update_sync_job(
            job_id,
            status="completed",
            message="Completed",
            finished_at=_utc_now_iso(),
            current_segment_id="",
            source_failures=source_failures,
            segment_failures=match_result.segment_failures,
            llm_reviewed=match_result.llm_reviewed,
            llm_review_failures=match_result.llm_review_failures,
            **cost_tracker.llm_cost_fields(),
            **asr_metrics,
            **summary,
        )
    except _JobCancelled:
        try:
            match_checkpoint.checkpoint()
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
            match_checkpoint.checkpoint()
        except Exception:
            app.logger.exception("Could not checkpoint failed sync job=%s", job_id)
        _update_sync_job(
            job_id,
            status="failed",
            message="Failed",
            error=str(exc),
            finished_at=_utc_now_iso(),
            **cost_tracker.llm_cost_fields(),
        )
    finally:
        _release_sync_project(project)
        with _SYNC_JOBS_LOCK:
            _SYNC_CANCEL_EVENTS.pop(job_id, None)
