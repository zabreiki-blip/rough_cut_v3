from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, TypedDict

from api_final_review_jobs import canonical_decimal_string
from api_service_contracts import SyncWorkerServices
from job_runtime import asr_slot
from cost_budget import CostBudget, CostBudgetExceeded
from stage2.cloud_asr import plan_cloud_chunks
from stage2.whisper_runner import resolve_backend

logger = logging.getLogger(__name__)


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
    cloud_costs: dict[str, dict[str, Any]] = {}

    def cloud_totals() -> tuple[Decimal, Decimal, bool]:
        return (
            sum((item["known"] for item in cloud_costs.values()), Decimal("0")),
            sum((item["reserved"] for item in cloud_costs.values()), Decimal("0")),
            any(item["pending"] for item in cloud_costs.values()),
        )

    def account_cloud_source(
        source_id: str,
        initial_exposure: Decimal,
        payload: Any,
        *,
        snapshot: bool = False,
    ) -> None:
        def value(name: str) -> Any:
            return payload.get(name) if isinstance(payload, dict) else getattr(payload, name, None)

        current = cloud_costs.setdefault(
            source_id,
            {"known": Decimal("0"), "reserved": Decimal("0"), "pending": False},
        )
        known = value("spent_usd" if snapshot else "cost_usd")
        reserved = value("reserved_usd" if snapshot else "reserved_cost_usd")
        pending = value("cost_pending")
        if known is not None:
            # The adapter treats all prior exposure as initial spent. Only
            # the current source's delta is newly confirmed provider cost.
            current["known"] = max(Decimal("0"), Decimal(str(known)) - initial_exposure)
        if reserved is not None:
            current["reserved"] = Decimal(str(reserved))
        if pending is not None:
            current["pending"] = bool(pending)
        elif reserved is not None:
            current["pending"] = current["reserved"] > 0
        known_total, reserved_total, pending_total = cloud_totals()
        asr_metrics["asr_cost_usd_total"] = float(known_total)
        _update_sync_job(
            job_id,
            persist=should_persist_progress(),
            cost_usd=float(known_total),
            reserved_cost_usd=float(reserved_total),
            cost_pending=pending_total,
            **asr_metrics,
        )

    # The semantic-review budget starts from the cloud ASR exposure of this job,
    # so a single job limit covers ASR and LLM spend together.
    llm_budget_state: dict[str, Any] = {"budget": None, "initial": Decimal("0")}

    def llm_cost_fields() -> dict[str, Any]:
        llm_budget = llm_budget_state["budget"]
        if llm_budget is None:
            return {}
        snapshot = llm_budget.snapshot()
        known_total, reserved_total, pending_total = cloud_totals()
        llm_spent = max(0.0, snapshot["spent_usd"] - float(llm_budget_state["initial"]))
        return {
            "cost_usd": float(known_total) + llm_spent,
            "reserved_cost_usd": float(reserved_total) + snapshot["reserved_usd"],
            "cost_pending": pending_total or llm_budget.has_pending_reservations,
            "llm_cost_usd": llm_spent,
            # Part of llm_cost_usd booked by estimate, not by provider-reported cost.
            "llm_estimated_cost_usd": snapshot["estimated_spent_usd"],
        }

    def should_persist_progress(*, force: bool = False) -> bool:
        nonlocal last_progress_persist_at
        now = time.monotonic()
        with progress_persist_lock:
            if not force and now - last_progress_persist_at < 1.0:
                return False
            last_progress_persist_at = now
            return True

    # Not dead code: the cancel/failure handlers below call ``checkpoint_matches``
    # even when the job stops before matching starts. This no-op is the binding
    # for that window; it is rebound to the real checkpoint once matching begins.
    def checkpoint_matches() -> None:
        return None

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
            initial_exposure = Decimal("0")
            cloud_dispatch = False
            usage_recorded = False
            pending_usage_recorded = False

            def record_cloud_usage(selected_model: str) -> None:
                nonlocal usage_recorded, pending_usage_recorded
                current = cloud_costs.get(source.id)
                if current is None:
                    return
                if current["pending"] and not pending_usage_recorded:
                    asr_usage_callback(
                        {
                            "event_id": f"sync-asr:{job_id}:{source.id}:reserved",
                            "model": selected_model,
                            "cost_usd": None,
                            "reserved_cost_usd": str(current["reserved"]),
                            "pending": True,
                        }
                    )
                    pending_usage_recorded = True
                if not usage_recorded and (not current["pending"] or current["known"] > 0):
                    asr_usage_callback(
                        {
                            "event_id": f"sync-asr:{job_id}:{source.id}",
                            "model": selected_model,
                            "cost_usd": float(current["known"]),
                        }
                    )
                    usage_recorded = True

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
                        isinstance(cache_metadata, dict)
                        and isinstance(stored_audio, dict)
                        and not stored_audio.get("sample_sha256")
                        and stored_audio == _audio_stat_signature(audio_path)
                    ):
                        cache_metadata["audio"] = audio_signature
                        save_word_index(index_path, cached_index)

                if cached_index is not None:
                    # A word index is always relative to its guide media, so
                    # it cannot legitimately extend materially past ffprobe
                    # duration regardless of which backend originally wrote
                    # it. Repair old cloud caches as well as imported indexes.
                    scale = rescale_stretched_word_index(
                        cached_index, float(source.duration_sec or 0.0)
                    )
                    if scale is not None:
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
                    resolved_backend = resolve_backend(config["backend"])
                    if resolved_backend != config["backend"]:
                        config = _transcription_config(
                            backend=resolved_backend,
                            model=model,
                            language=language,
                        )
                    source_duration = max(0.0, float(source.duration_sec or 0.0))
                    if cloud_source_options is not None:
                        source_duration = float(
                            (cloud_source_options.get(source.id) or {}).get(
                                "duration_sec", source_duration
                            )
                        )
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
                    cloud_kwargs: dict[str, Any] = {}
                    if cloud_source_options is None and config["backend"] in {
                        "cloud",
                        "cloud-audio",
                        "openrouter",
                    }:
                        raise CostBudgetExceeded(
                            "Cloud backend changed since cost preflight; start the sync job again"
                        )
                    if cloud_source_options is not None:
                        options = dict(cloud_source_options.get(source.id) or {})
                        if options.pop("audio_signature", None) != input_audio_signature:
                            raise CostBudgetExceeded(
                                "Cloud source changed since cost preflight; start the sync job again"
                            )
                        known_total, reserved_total, _pending = cloud_totals()
                        initial_exposure = known_total + reserved_total
                        budget = CostBudget(
                            Decimal(options["max_cost_usd"]),
                            Decimal(options["usd_per_minute_estimate"]),
                            initial_exposure,
                        )
                        chunks = plan_cloud_chunks(
                            options["duration_sec"],
                            options["chunk_minutes"],
                            options["overlap_sec"],
                        )
                        # Reserve each planned chunk independently so Decimal
                        # addition matches preflight at fractional boundaries.
                        for start, end in chunks:
                            budget.reserve_attempt(end - start)
                        options["initial_cost_usd"] = canonical_decimal_string(initial_exposure)

                        def report_budget(snapshot: dict[str, Any]) -> None:
                            account_cloud_source(
                                source.id, initial_exposure, snapshot, snapshot=True
                            )

                        cloud_kwargs = {"cloud_options": options, "budget_callback": report_budget}
                    transcription_started_at = time.monotonic()
                    # The global ASR slot covers only the transcription call;
                    # matching and persistence run outside it. A job cancelled
                    # while queued raises _JobCancelled without taking a slot.
                    with asr_slot(
                        services._ASR_SEMAPHORE,
                        check_stop=ensure_not_cancelled,
                        on_wait=lambda: _update_sync_job(
                            job_id, message="Waiting for an ASR slot", asr_waiting=True
                        ),
                        on_acquired=lambda: _update_sync_job(
                            job_id, message="Transcribing audio", asr_waiting=False
                        ),
                    ):
                        # Marked only once the call really starts: a job
                        # cancelled while queued for a slot dispatched nothing,
                        # so it must not be accounted as cloud spend.
                        cloud_dispatch = cloud_source_options is not None
                        result = transcribe_audio(
                            audio_path,
                            backend=config["backend"],
                            model=model,
                            language=language,
                            progress_callback=report_progress,
                            cancel_event=cancel_event,
                            timeout_seconds=(
                                app.config.get("SYNC_WHISPER_TIMEOUT_SECONDS") or None
                            ),
                            **cloud_kwargs,
                        )
                    result_backend_metadata = result.get("backend_metadata") or {}
                    if cloud_dispatch:
                        account_cloud_source(source.id, initial_exposure, result_backend_metadata)
                        record_cloud_usage(str(result.get("model") or model or ""))
                    elif str(result.get("backend") or backend or "") == "cloud-audio":
                        asr_usage_callback(
                            {
                                "event_id": f"sync-asr:{job_id}:{source.id}",
                                "model": str(result.get("model") or model or ""),
                                "cost_usd": result_backend_metadata.get("cost_usd"),
                            }
                        )
                    transcription_wall_sec = time.monotonic() - transcription_started_at
                    if _audio_cache_signature(audio_path) != input_audio_signature:
                        raise RuntimeError(
                            f"audio guide changed during transcription: {audio_path.name}"
                        )
                    asr_metrics["transcription_wall_sec_total"] += transcription_wall_sec
                    asr_metrics["asr_backend_used"] = str(result.get("backend") or backend or "")
                    if not cloud_dispatch:
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
                failed_cost = getattr(exc, "cost_usd", None)
                if cloud_dispatch:
                    account_cloud_source(source.id, initial_exposure, exc)
                    record_cloud_usage(str(model or ""))
                elif failed_cost is not None:
                    asr_usage_callback(
                        {
                            "event_id": f"sync-asr:{job_id}:{source.id}",
                            "model": str(model or ""),
                            "cost_usd": failed_cost,
                        }
                    )
                if isinstance(exc, WhisperCancelledError) or (
                    cancel_event is not None and cancel_event.is_set()
                ):
                    raise _JobCancelled("Sync job cancelled") from exc
                if isinstance(exc, CostBudgetExceeded):
                    raise
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
        indexed_source_ids = {item.source_id for item in indexes}
        matching_sources = [item for item in sources if item.id in indexed_source_ids]
        source_speakers = {
            tuple(normalize_text(name))
            for source in matching_sources
            for name in source.speaker_names
            if normalize_text(name)
        }
        existing = {item.segment_id: item for item in match_store.list()}
        confirmed_parent_ids = {
            segment_id
            for segment_id, item in existing.items()
            if item.status == "confirmed"
            and item.metadata.get("normalization_revision") in (None, "", normalization_revision)
            and str(item.metadata.get("scenario_revision_id") or "") in ("", scenario_revision_id)
        }
        units = _sync_alignment_units(
            blocks,
            source_speakers=source_speakers,
            preserve_whole_uids=confirmed_parent_ids,
        )
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
        if llm_review_enabled:
            known_total, reserved_total, _pending = cloud_totals()
            # Prefer the settings frozen at job start; fall back to the environment
            # for callers that bypass the start route.
            llm_cost_env = job.get("llm_cost_env")
            if not isinstance(llm_cost_env, dict):
                llm_cost_env = os.environ
            try:
                llm_budget_state["budget"] = CostBudget.for_llm_tts(
                    llm_cost_env, known_total + reserved_total
                )
                llm_budget_state["initial"] = known_total + reserved_total
            except ValueError as exc:
                # Fail closed: without a valid budget no paid review call is made.
                llm_review_enabled = False
                logger.warning(
                    "Sync job=%s: LLM review disabled, invalid cost settings: %s", job_id, exc
                )
                _update_sync_job(job_id, llm_budget_error=str(exc))
        llm_reviewed = 0
        llm_review_failures: list[dict[str, str]] = []
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
                            usage_callback=semantic_usage_callback,
                            budget=llm_budget_state["budget"],
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
                    except CostBudgetExceeded as exc:
                        # The review is an optional improvement: stop paid calls and
                        # finish with deterministic matching for the remaining segments.
                        llm_review_enabled = False
                        logger.warning(
                            "Sync job=%s: LLM review stopped, job budget exceeded: %s", job_id, exc
                        )
                        _update_sync_job(job_id, llm_budget_error=str(exc))
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
            except CostBudgetExceeded:
                raise
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
                **llm_cost_fields(),
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
            **llm_cost_fields(),
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
            **llm_cost_fields(),
        )
    finally:
        _release_sync_project(project)
        with _SYNC_JOBS_LOCK:
            _SYNC_CANCEL_EVENTS.pop(job_id, None)
