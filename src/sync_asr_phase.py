from __future__ import annotations

import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from api_final_review_jobs import canonical_decimal_string
from cost_budget import CostBudget, CostBudgetExceeded
from job_runtime import asr_slot
from stage2 import local_hole_repair
from stage2.cached_index_repair import has_unrecovered_holes, repair_cached_index_holes
from stage2.cloud_asr import plan_cloud_chunks
from stage2.whisper_runner import is_no_words_error, resolve_backend
from sync_cost_tracker import ProgressPersistGate, SyncCostTracker, _AsrMetrics


@dataclass(frozen=True)
class SyncAsrContext:
    """Job parameters and service dependencies of the ASR phase of one sync job.

    Built once per job by ``run_sync_job`` from values it has already taken
    off ``services`` (so patches applied before the job starts still apply).
    The phase never sees ``services`` itself.
    """

    job_id: str
    paths: Mapping[str, Path]
    backend: str | None
    model: str | None
    language: str
    source_ids: tuple[str, ...]
    force_transcribe: bool
    # Per-source cloud ASR options frozen at the cost preflight, or ``None``.
    cloud_source_options: Mapping[str, Any] | None
    cancel_event: threading.Event | None
    ensure_not_cancelled: Callable[[], None]
    should_persist_progress: ProgressPersistGate
    asr_usage_callback: Callable[[dict[str, Any]], Any]
    asr_semaphore: threading.Semaphore
    # Resolved at call time, like the ``services.os`` / ``services.time`` /
    # ``services.app`` lookups they replace.
    getenv: Callable[[str], str | None]
    monotonic: Callable[[], float]
    app_config: Mapping[str, Any]
    job_cancelled_error: type[Exception]
    whisper_cancelled_error: type[Exception]
    transcription_cache_key: str
    update_job: Callable[..., Any]
    utc_now_iso: Callable[[], str]
    word_index_path: Callable[[Any, str], Path]
    transcription_config: Callable[..., Any]
    resolve_audio_path: Callable[[Any, Any], Path]
    load_word_index: Callable[[Path, str], Any]
    save_word_index: Callable[[Path, Any], Any]
    rescale_stretched_word_index: Callable[[Any, float], Any]
    transcription_cache_metadata: Callable[..., Any]
    audio_stat_signature: Callable[[Path], Any]
    audio_cache_signature: Callable[[Path], Any]
    word_index_audio_matches: Callable[..., bool]
    transcribe_audio: Callable[..., Any]
    ingest_word_index: Callable[..., Any]


@dataclass(frozen=True)
class SyncAsrResult:
    """What the matching phase needs from the ASR phase."""

    indexes: list[Any]
    source_failures: list[dict[str, str]]
    transcribed: int
    reused: int
    # Non-fatal per-source notes, e.g. cloud ASR coverage holes it could not
    # recover: {"source_id": ..., "warning": ...}.
    source_warnings: list[dict[str, str]] = field(default_factory=list)


def _coverage_warning(source_id: str, metadata: Mapping[str, Any]) -> dict[str, str] | None:
    coverage = metadata.get("coverage")
    if not isinstance(coverage, Mapping):
        return None
    try:
        unrecovered_sec = float(coverage.get("unrecovered_sec") or 0.0)
    except (TypeError, ValueError):
        return None
    if unrecovered_sec <= 0:
        return None
    ranges = ", ".join(
        f"{float(item[0]):.0f}–{float(item[1]):.0f} с"
        for item in (coverage.get("unrecovered_ranges") or [])[:5]
        if isinstance(item, (list, tuple)) and len(item) == 2
    )
    return {
        "source_id": source_id,
        "warning": (
            f"В источнике {source_id} не распознано {unrecovered_sec / 60.0:.1f} мин речи"
            + (f" ({ranges})" if ranges else "")
        ),
    }


_CLOUD_BACKENDS = frozenset({"cloud", "cloud-audio", "openrouter"})


def _cloud_index_is_outdated(cache: Any, current_mapping: Any, coverage: Any = None) -> bool:
    """Whether a generated cloud transcript predates the current cloud mapping.

    Completed transcripts are normally reused across config changes, but cloud
    indexes from older mappings are known to be defective (mapping "2" and
    earlier: 20-minute parts that silently lost 30-45% of speech). Manual
    corrections and non-cloud transcripts are never invalidated here.
    Indexes from mapping "3" onwards carry a coverage report; they are redone
    only while speech is still missing (``coverage.unrecovered_sec > 0``).
    """
    if not current_mapping or not isinstance(cache, dict):
        return False
    if cache.get("origin") != "generated":
        return False
    config = cache.get("config")
    if not isinstance(config, dict):
        return False
    if str(config.get("backend") or "").strip().lower() not in _CLOUD_BACKENDS:
        return False
    stored_mapping = str(config.get("cloud_timestamp_mapping") or "")
    if stored_mapping == str(current_mapping):
        return False
    if stored_mapping.isdigit() and int(stored_mapping) >= 3 and isinstance(coverage, dict):
        try:
            return float(coverage.get("unrecovered_sec") or 0.0) > 0
        except (TypeError, ValueError):
            return True
    return True


# Statuses that explain a skipped repair to the user; "disabled" stays silent.
_REPAIR_WARNING_STATUSES = frozenset({"failed", "unavailable", "no_audio", "scale_repaired"})


def _source_label(source: Any) -> str:
    for name in ("premiere_sequence", "speaker"):
        value = str(getattr(source, name, "") or "").strip()
        if value:
            return value
    return str(source.id)


def _repair_cached_index_holes(
    ctx: SyncAsrContext,
    source: Any,
    index: Any,
    index_path: Path,
    audio_path: Path | None,
) -> list[dict[str, str]]:
    """Locally repair coverage holes of a reused cloud index and save it.

    Costs nothing in the cloud and never fails the phase: any problem leaves
    the index as it was and is returned as per-source warnings. The index is
    updated in place, so matching in this very run sees the repaired words.
    Only ``origin == "generated"`` indexes are repaired: manual corrections
    must never be rewritten, and a legacy index (migrated without knowing
    whether it was edited by hand) is skipped with an explanatory warning.
    Holes that remain afterwards get the same coverage warning as a fresh
    transcription. Cancellation raises ``ctx.job_cancelled_error``.
    """
    if not has_unrecovered_holes(index):
        return []
    cache = index.metadata.get(ctx.transcription_cache_key)
    origin = cache.get("origin") if isinstance(cache, dict) else "legacy"
    if origin != "generated":
        if origin == "legacy" and local_hole_repair.LocalRepairSettings.from_env().enabled:
            return [
                {
                    "source_id": source.id,
                    "warning": (
                        f"Локальный ремонт пропусков в источнике {source.id} пропущен: "
                        "индекс legacy, перезапустите распознавание источника"
                    ),
                }
            ]
        return []
    label = _source_label(source)
    cancel_event = ctx.cancel_event

    def on_progress(progress: Any) -> None:
        ctx.update_job(
            ctx.job_id,
            persist=ctx.should_persist_progress(),
            message=(
                f"Локальный ремонт пропусков: {label}, дыра {progress.done + 1} из {progress.total}"
            ),
        )

    def transcribe_clip(
        clip_path: Path, model: str, language: str, progress_callback: Any = None
    ) -> list[dict[str, Any]]:
        # The same killable Whisper worker process the rest of the app uses:
        # cancellation terminates it instead of waiting for a thread.
        result = ctx.transcribe_audio(
            clip_path,
            backend="faster-whisper",
            model=model,
            language=language,
            cancel_event=cancel_event,
            timeout_seconds=(ctx.app_config.get("SYNC_WHISPER_TIMEOUT_SECONDS") or None),
        )
        return list(result.get("words") or [])

    def transcribe_clip_safe(
        clip_path: Path, model: str, language: str, progress_callback: Any = None
    ) -> list[dict[str, Any]]:
        try:
            return transcribe_clip(clip_path, model, language, progress_callback)
        except Exception as exc:
            # Whisper found no speech in the clip after VAD: an empty result
            # (the hole stays), not an engine failure to be retried every run.
            if is_no_words_error(exc):
                return []
            raise

    previous_words = list(index.words)
    previous_backend_metadata = index.metadata.get("backend_metadata")
    try:
        with asr_slot(
            ctx.asr_semaphore,
            check_stop=ctx.ensure_not_cancelled,
            on_wait=lambda: ctx.update_job(
                ctx.job_id, message="Waiting for an ASR slot", asr_waiting=True
            ),
            on_acquired=lambda: ctx.update_job(
                ctx.job_id, message=f"Локальный ремонт пропусков: {label}", asr_waiting=False
            ),
        ):
            result = repair_cached_index_holes(
                index,
                audio_path,
                language=ctx.language,
                now_iso=ctx.utc_now_iso,
                transcribe=transcribe_clip_safe,
                cancel=cancel_event,
                on_progress=on_progress,
                duration_sec=float(getattr(source, "duration_sec", 0.0) or 0.0),
            )
        if result.status == "cancelled":
            raise ctx.job_cancelled_error("Sync job cancelled")
        if result.changed:
            try:
                ctx.save_word_index(index_path, index)
            except Exception:
                index.words = previous_words
                index.metadata["backend_metadata"] = previous_backend_metadata
                raise
    except ctx.job_cancelled_error:
        raise
    except ctx.whisper_cancelled_error as exc:
        raise ctx.job_cancelled_error("Sync job cancelled") from exc
    except Exception as exc:
        if cancel_event is not None and cancel_event.is_set():
            raise ctx.job_cancelled_error("Sync job cancelled") from exc
        return [
            {
                "source_id": source.id,
                "warning": (
                    f"Локальный ремонт пропусков в источнике {source.id} не выполнен: "
                    f"{type(exc).__name__}: {exc}"
                ),
            }
        ]
    warnings: list[dict[str, str]] = []
    if result.status in _REPAIR_WARNING_STATUSES:
        warnings.append(
            {
                "source_id": source.id,
                "warning": (
                    f"Локальный ремонт пропусков в источнике {source.id} пропущен: {result.reason}"
                ),
            }
        )
    if result.status != "disabled":
        coverage_warning = _coverage_warning(
            source.id, index.metadata.get("backend_metadata") or {}
        )
        if coverage_warning is not None:
            warnings.append(coverage_warning)
    return warnings


def run_asr_phase(
    ctx: SyncAsrContext,
    sources: Sequence[Any],
    *,
    cost_tracker: SyncCostTracker,
    asr_metrics: _AsrMetrics,
) -> SyncAsrResult:
    """Build or reuse a word index for every source, one source at a time.

    ``asr_metrics`` and ``cost_tracker`` are shared with the caller, which
    reports them in the final job status. Cancellation raises
    ``ctx.job_cancelled_error``; ``CostBudgetExceeded`` propagates; any other
    per-source failure is recorded in ``source_failures`` and the phase moves
    on to the next source.
    """
    job_id = ctx.job_id
    paths = ctx.paths
    backend = ctx.backend
    model = ctx.model
    language = ctx.language
    source_ids = ctx.source_ids
    force_transcribe = ctx.force_transcribe
    cloud_source_options = ctx.cloud_source_options
    cancel_event = ctx.cancel_event
    ensure_not_cancelled = ctx.ensure_not_cancelled
    should_persist_progress = ctx.should_persist_progress
    asr_usage_callback = ctx.asr_usage_callback
    asr_semaphore = ctx.asr_semaphore
    getenv = ctx.getenv
    monotonic = ctx.monotonic
    app_config = ctx.app_config
    _JobCancelled = ctx.job_cancelled_error
    WhisperCancelledError = ctx.whisper_cancelled_error
    _TRANSCRIPTION_CACHE_KEY = ctx.transcription_cache_key
    _update_sync_job = ctx.update_job
    _utc_now_iso = ctx.utc_now_iso
    _word_index_path = ctx.word_index_path
    _transcription_config = ctx.transcription_config
    _resolve_sync_audio_path = ctx.resolve_audio_path
    load_word_index = ctx.load_word_index
    save_word_index = ctx.save_word_index
    rescale_stretched_word_index = ctx.rescale_stretched_word_index
    _transcription_cache_metadata = ctx.transcription_cache_metadata
    _audio_stat_signature = ctx.audio_stat_signature
    _audio_cache_signature = ctx.audio_cache_signature
    _word_index_audio_matches = ctx.word_index_audio_matches
    transcribe_audio = ctx.transcribe_audio
    ingest_word_index = ctx.ingest_word_index

    indexes: list[Any] = []
    source_failures: list[dict[str, str]] = []
    source_warnings: list[dict[str, str]] = []
    transcribed = 0
    reused = 0

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
            current = cost_tracker.source_cost(source.id)
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
                and not (
                    audio_is_available
                    and _cloud_index_is_outdated(
                        cached_index.metadata.get(_TRANSCRIPTION_CACHE_KEY),
                        _transcription_config(
                            backend="cloud-audio", model=model, language=language
                        ).get("cloud_timestamp_mapping"),
                        cached_index.metadata.get("coverage"),
                    )
                )
            ):
                # A completed transcript is an expensive durable artifact.
                # Switching Small/Medium changes only future files; it must
                # not invalidate a transcript for unchanged media.
                index = cached_index
                reused += 1
                repair_warnings = _repair_cached_index_holes(
                    ctx,
                    source,
                    index,
                    index_path,
                    audio_path if audio_is_available else None,
                )
                if repair_warnings:
                    source_warnings.extend(repair_warnings)
                    _update_sync_job(job_id, source_warnings=list(source_warnings))
            else:
                if audio_path is None:
                    raise FileNotFoundError(f"audio guide not found for sync source {source.id}")
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
                        app_config.get("SYNC_WHISPER_TIMEOUT_SECONDS")
                        or getenv("SYNC_WHISPER_TIMEOUT_SECONDS")
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
                    initial_exposure = cost_tracker.cloud_exposure()
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
                        cost_tracker.account_cloud_source(
                            source.id, initial_exposure, snapshot, snapshot=True
                        )

                    cloud_kwargs = {"cloud_options": options, "budget_callback": report_budget}
                transcription_started_at = monotonic()
                # The global ASR slot covers only the transcription call;
                # matching and persistence run outside it. A job cancelled
                # while queued raises _JobCancelled without taking a slot.
                with asr_slot(
                    asr_semaphore,
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
                        timeout_seconds=(app_config.get("SYNC_WHISPER_TIMEOUT_SECONDS") or None),
                        **cloud_kwargs,
                    )
                result_backend_metadata = result.get("backend_metadata") or {}
                if cloud_dispatch:
                    cost_tracker.account_cloud_source(
                        source.id, initial_exposure, result_backend_metadata
                    )
                    record_cloud_usage(str(result.get("model") or model or ""))
                elif str(result.get("backend") or backend or "") == "cloud-audio":
                    asr_usage_callback(
                        {
                            "event_id": f"sync-asr:{job_id}:{source.id}",
                            "model": str(result.get("model") or model or ""),
                            "cost_usd": result_backend_metadata.get("cost_usd"),
                        }
                    )
                transcription_wall_sec = monotonic() - transcription_started_at
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
                warning = _coverage_warning(source.id, result_backend_metadata)
                if warning is not None:
                    source_warnings.append(warning)
                    _update_sync_job(job_id, source_warnings=list(source_warnings))
            indexes.append(index)
        except Exception as exc:
            failed_cost = getattr(exc, "cost_usd", None)
            if cloud_dispatch:
                cost_tracker.account_cloud_source(source.id, initial_exposure, exc)
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

    return SyncAsrResult(
        indexes=indexes,
        source_failures=source_failures,
        transcribed=transcribed,
        reused=reused,
        source_warnings=source_warnings,
    )
