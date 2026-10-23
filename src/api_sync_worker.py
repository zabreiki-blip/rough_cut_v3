from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from api_service_contracts import SyncWorkerServices
from sync_asr_failures import mark_asr_failed_matches
from sync_audio_verify_phase import (
    CACHE_SUBDIR,
    AudioVerifyResult,
    persist_audio_results,
    snapshot_matches,
    resolve_audio_verify,
    run_audio_verification,
)
from sync_asr_phase import SyncAsrContext, run_asr_phase
from sync_match_phase import (
    SyncMatchCheckpoint,
    SyncMatchContext,
    run_match_phase,
)
from sync_cost_tracker import SyncCostTracker, _AsrMetrics
from job_runtime import asr_slot
from stage2.whisper_runner import is_no_words_error

logger = logging.getLogger(__name__)


def make_window_transcriber(
    transcribe_audio: Callable[..., dict[str, Any]],
    *,
    asr_semaphore: Any,
    cancel_event: threading.Event | None,
    ensure_not_cancelled: Callable[[], None],
    language: str,
    update_job: Callable[..., object],
    job_id: str,
    timeout_seconds: float | None = None,
) -> Callable[[Path, str], list[dict[str, Any]]]:
    """Local-Whisper transcriber of one window clip for the sync window refine.

    Same discipline as the ordinary worker ASR: the global ASR slot is held only around
    the call (waiting cancel-aware) and the killable Whisper process gets the job's
    ``cancel_event``, so a cancelled job interrupts the window instead of finishing it.
    """

    def transcribe_window(clip: Path, model: str) -> list[dict[str, Any]]:
        with asr_slot(
            asr_semaphore,
            check_stop=ensure_not_cancelled,
            on_wait=lambda: update_job(
                job_id, message="Waiting for an ASR slot", asr_waiting=True
            ),
            on_acquired=lambda: update_job(
                job_id, message="Уточнение границ по окну", asr_waiting=False
            ),
        ):
            try:
                result = transcribe_audio(
                    clip,
                    backend="faster-whisper",
                    model=model,
                    language=language,
                    cancel_event=cancel_event,
                    timeout_seconds=timeout_seconds,
                )
            except Exception as exc:
                # No speech after VAD is an empty window, not an engine failure.
                if is_no_words_error(exc):
                    return []
                raise
        return list(result.get("words") or [])

    return transcribe_window


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
    audio_verify: bool | None = None,
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

        def _optional_sync_audio_path(source: Any, source_paths: Any) -> Path | None:
            # The window refine treats a missing guide as "unavailable", not as a job failure.
            try:
                return _resolve_sync_audio_path(source, source_paths)
            except Exception:
                logger.warning(
                    "Guide audio of source %s unavailable for window refine",
                    getattr(source, "id", ""),
                    exc_info=True,
                )
                return None

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
                resolve_audio_path=_optional_sync_audio_path,
                transcribe_window=make_window_transcriber(
                    transcribe_audio,
                    asr_semaphore=services._ASR_SEMAPHORE,
                    cancel_event=cancel_event,
                    ensure_not_cancelled=ensure_not_cancelled,
                    language=language,
                    update_job=_update_sync_job,
                    job_id=job_id,
                    timeout_seconds=(app.config.get("SYNC_WHISPER_TIMEOUT_SECONDS") or None),
                ),
            ),
            sources,
            indexes,
            cost_tracker=cost_tracker,
            checkpoint=match_checkpoint,
        )
        ensure_not_cancelled()
        # Real ASR failures (transcription was attempted) vs. sources that
        # could not be read at all (guide file missing/unreadable): the second
        # group is "no recording", not "ASR failed".
        degraded_sources = [
            str(source_id) for source_id in asr_result.asr_failed_source_ids if source_id
        ]
        unavailable_sources = [
            str(item["source_id"])
            for item in source_failures
            if item.get("source_id") and str(item["source_id"]) not in set(degraded_sources)
        ]
        if asr_result.asr_failed_source_ids:
            # A failed ASR must not look like "no recording": relabel segments a
            # failed source could have served, in memory and in the store.
            # A source whose guide file is missing keeps ``no_eligible_source``.
            failed_ids = set(asr_result.asr_failed_source_ids)
            changed = set(
                mark_asr_failed_matches(
                    match_result.matches, sources, failed_ids, normalize_text
                )
            )
            if changed:
                with _SYNC_DATA_LOCK:
                    stored = match_store.list()
                    relabelled = mark_asr_failed_matches(
                        [item for item in stored if item.segment_id in changed],
                        sources,
                        failed_ids,
                        normalize_text,
                    )
                    if relabelled:
                        match_store.replace_all(stored)
        audio_fields: dict[str, object] = {}
        enabled, audio_note = resolve_audio_verify(audio_verify, asr_engine, os.environ)
        source_warnings = list(asr_result.source_warnings)
        if audio_note:
            source_warnings.append({"source_id": "", "warning": audio_note})
        if enabled:
            budget = cost_tracker.llm_budget
            if budget is None:
                cost_env = job.get("llm_cost_env")
                if not cost_tracker.init_llm_budget(
                    cost_env if isinstance(cost_env, dict) else os.environ
                ):
                    source_warnings.append(
                        {
                            "source_id": "",
                            "warning": "проверка по звуку отключена: неверные настройки бюджета",
                        }
                    )
                budget = cost_tracker.llm_budget
            if budget is not None:
                _update_sync_job(job_id, message="Verifying matches by audio")

                def report_audio_progress(partial: AudioVerifyResult, total: int) -> None:
                    _update_sync_job(
                        job_id,
                        persist=should_persist_progress(),
                        message=f"Verifying matches by audio: {partial.checked}/{total}",
                        **partial.job_fields(),
                    )

                audio_result = AudioVerifyResult()
                with _SYNC_DATA_LOCK:
                    audio_snapshot = snapshot_matches(match_store.list())
                try:
                    run_audio_verification(
                        match_result.matches,
                        sources,
                        indexes,
                        resolve_audio_path=lambda source: _resolve_sync_audio_path(source, paths),
                        cache_dir=paths["sync_transcripts"] / CACHE_SUBDIR,
                        budget=budget,
                        environ=os.environ,
                        now=_utc_now_iso(),
                        usage_callback=services._make_openrouter_usage_callback(
                            project, stage="assembly", operation="sync_audio_verify", job_id=job_id
                        ),
                        ensure_not_cancelled=ensure_not_cancelled,
                        on_progress=report_audio_progress,
                        result=audio_result,
                    )
                finally:
                    # Verdicts already paid for are kept even if the job is cancelled;
                    # only matches verified in this run are written, and never over an
                    # edit made while the job was running.
                    if audio_result.changed_ids:
                        with _SYNC_DATA_LOCK:
                            stored = match_store.list()
                            if persist_audio_results(
                                stored,
                                match_result.matches,
                                audio_result.changed_ids,
                                audio_snapshot,
                            ):
                                match_store.replace_all(stored)
                source_warnings.extend(
                    {"source_id": "", "warning": text} for text in audio_result.warnings
                )
                audio_fields = audio_result.job_fields()
        if len(source_warnings) != len(asr_result.source_warnings):
            audio_fields["source_warnings"] = source_warnings
        summary = _sync_summary(match_result.matches)
        # ``status`` stays "completed" (terminal for polling and the UI); an
        # incomplete result is expressed by ``complete`` / ``degraded_sources``.
        _update_sync_job(
            job_id,
            status="completed",
            message=(
                "Completed with errors: "
                + "; ".join(
                    part
                    for part in (
                        f"ASR failed for {len(degraded_sources)} source(s)"
                        if degraded_sources
                        else "",
                        f"guide file unavailable for {len(unavailable_sources)} source(s)"
                        if unavailable_sources
                        else "",
                    )
                    if part
                )
                if degraded_sources or unavailable_sources
                else "Completed"
            ),
            complete=not (degraded_sources or unavailable_sources),
            degraded_sources=degraded_sources,
            unavailable_sources=unavailable_sources,
            finished_at=_utc_now_iso(),
            current_segment_id="",
            source_failures=source_failures,
            segment_failures=match_result.segment_failures,
            llm_reviewed=match_result.llm_reviewed,
            llm_review_failures=match_result.llm_review_failures,
            **cost_tracker.llm_cost_fields(),
            **asr_metrics,
            **audio_fields,
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
