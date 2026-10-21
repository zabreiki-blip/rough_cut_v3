from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cost_budget import CostBudgetExceeded
from stage2.match_retiming import (
    apply_retime_to_match,
    load_retime_map,
    retime_map_path,
)
from stage2.sync_models import AlignmentRequest, TimecodeMode
from stage2.sync_offset_clusters import (
    DEFAULT_AUTO_THRESHOLD,
    OffsetClusterConfig,
    apply_offset_cluster_promotions,
    is_promotion_candidate,
)
from stage2.sync_storage import SyncMatchStore
from sync_cost_tracker import ProgressPersistGate, SyncCostTracker


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


class SyncMatchCheckpoint:
    """Late-bound checkpoint of partial matches, shared with the job handlers.

    ``run_sync_job`` creates it before anything can fail and calls
    ``checkpoint()`` from its cancel/failure handlers. Until the matching phase
    ``activate``s it (right after the LLM budget is initialised) ``checkpoint()``
    is a no-op, exactly like the closure it replaces; afterwards it persists the
    partial matches. An explicit object instead of a rebound closure or a
    global: the handler and the phase see the same instance, and the
    "not started yet" state is visible in one place.
    """

    def __init__(self) -> None:
        self._write: Callable[[], None] | None = None

    @property
    def active(self) -> bool:
        return self._write is not None

    def activate(self, write: Callable[[], None]) -> None:
        self._write = write

    def checkpoint(self) -> None:
        if self._write is not None:
            self._write()


@dataclass(frozen=True)
class SyncMatchContext:
    """Job parameters and service dependencies of the matching phase.

    Built once per job by ``run_sync_job`` from values it has already taken
    off ``services`` (so patches applied before the job starts still apply).
    The phase never sees ``services`` itself.
    """

    job_id: str
    paths: Mapping[str, Path]
    match_store: SyncMatchStore
    timecode_mode: TimecodeMode
    rematch_confirmed: bool
    search_window_sec: float
    # ``llm_cost_env`` frozen at job start, or ``None`` for callers that bypass
    # the start route (then ``environ()`` is used).
    job_llm_cost_env: Any
    ensure_not_cancelled: Callable[[], None]
    should_persist_progress: ProgressPersistGate
    semantic_usage_callback: Callable[[dict[str, Any]], Any]
    logger: logging.Logger
    # Resolved at call time, like the ``services.os`` lookups they replace.
    getenv: Callable[[str, str], str]
    environ: Callable[[], Mapping[str, str]]
    data_lock: AbstractContextManager[Any]
    update_job: Callable[..., Any]
    utc_now_iso: Callable[[], str]
    load_normalization: Callable[[Any], dict[str, Any]]
    alignment_units: Callable[..., list[dict[str, Any]]]
    matcher_cls: Callable[..., Any]
    matcher_config_cls: Callable[..., Any]
    get_settings: Callable[[], Any]
    semantic_review_candidates: Callable[[Any], Any]
    normalize_text: Callable[[str], Any]
    review_candidates_with_llm: Callable[..., Any]
    apply_semantic_review: Callable[..., Any]
    alignment_request_cls: type[AlignmentRequest]
    matching_fingerprint: Callable[..., str]
    sync_summary: Callable[[Sequence[Any]], dict[str, Any]]
    # ``(paths, source_id) -> index file``: next to it lies the retime map used to carry
    # confirmed matches over to retimed word times. ``None`` disables that pass.
    word_index_path: Callable[[Any, str], Path] | None = None


@dataclass(frozen=True)
class SyncMatchResult:
    """What ``run_sync_job`` needs from the matching phase to finish the job."""

    matches: list[Any]
    llm_reviewed: int
    llm_review_failures: list[dict[str, str]]
    segment_failures: list[dict[str, str]]
    processed: int
    total_segments: int


@dataclass
class _ReviewState:
    """LLM-review switches and counters that change while segments are matched."""

    enabled: bool
    threshold: float
    limit: int
    reviewed: int = 0
    failures: list[dict[str, str]] = field(default_factory=list)


def _match_signature(match: Any) -> str:
    """Stable fingerprint of a stored match, used to detect changes during a job."""
    to_dict = getattr(match, "to_dict", None)
    payload = (
        to_dict()
        if callable(to_dict)
        else {
            "status": getattr(match, "status", None),
            "source_id": getattr(match, "source_id", None),
            "metadata": getattr(match, "metadata", None),
        }
    )
    return json.dumps(payload, sort_keys=True, default=str)


def _make_checkpoint_writer(
    ctx: SyncMatchContext,
    *,
    existing: dict[str, Any],
    initial_signatures: Mapping[str, str],
    checkpoint_state: dict[str, bool],
    rematched_this_run: set[str],
    matching_started_at_dt: datetime | None,
    normalization_revision: Any,
    scenario_revision_id: str,
) -> Callable[[], None]:
    match_store = ctx.match_store

    def checkpoint_matches() -> None:
        if not checkpoint_state["dirty"]:
            return
        with ctx.data_lock:
            persisted = {item.segment_id: item for item in match_store.list()}
            # A manual edit is only authoritative over a fresh rematch when
            # it happened concurrently with this job (edited at/after
            # ``matching_started_at``), or when this run never recomputed
            # the segment at all. An older confirmation/edit that predates
            # the job must yield to an explicit forced rematch, otherwise
            # ``rematch_confirmed=True`` can never actually replace a
            # previously confirmed range. A confirmation without a
            # ``manual_updated_at`` is legacy data: every current write path
            # (the PATCH matches route) stamps it, so such a record
            # predates the job and yields to a forced rematch, unless the
            # stored record differs from the snapshot taken at phase start
            # (a confirmation written during the job by a path that does not
            # stamp the timestamp).
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
                    edited_during_run = initial_signatures.get(persisted_id) != _match_signature(
                        persisted_match
                    )
                else:
                    edited_during_run = False
                not_rematched_this_run = persisted_id not in rematched_this_run
                if persisted_is_current and (edited_during_run or not_rematched_this_run):
                    existing[persisted_id] = persisted_match
                elif persisted_id not in existing:
                    existing[persisted_id] = persisted_match
            match_store.replace_all(list(existing.values()))
            checkpoint_state["dirty"] = False

    return checkpoint_matches


def _retime_confirmed_matches(
    ctx: SyncMatchContext,
    existing: dict[str, Any],
    sources: Sequence[Any],
    indexes: Sequence[Any],
) -> tuple[list[str], list[str]]:
    """Carry confirmed matches over to the retimed word times of their source.

    The cloud index words were retimed by a local Whisper pass, but the matching
    below never touches confirmed matches, so their ``source_in/out`` would stay in the
    old cloud time. For every confirmed match of a source whose retime map belongs to
    the current index, unedited boundaries are recomputed (status stays confirmed);
    hand-edited or ambiguous ones are only flagged ``retime_pending_review``.
    Returns ``(retimed ids, ids flagged for review)``; matches are changed in place.
    """
    if ctx.word_index_path is None:
        return [], []
    sources_by_id = {str(getattr(item, "id", "")): item for item in sources}
    indexes_by_id = {str(getattr(item, "source_id", "")): item for item in indexes}
    retimed: list[str] = []
    review: list[str] = []
    for source_id in sorted(
        {str(getattr(m, "source_id", "")) for m in existing.values() if m.status == "confirmed"}
    ):
        index, source = indexes_by_id.get(source_id), sources_by_id.get(source_id)
        if index is None or source is None:
            continue
        try:
            rmap = load_retime_map(
                retime_map_path(ctx.word_index_path(ctx.paths, source_id), source_id)
            )
        except Exception:
            ctx.logger.warning("Sync job=%s: retime map of %s unreadable", ctx.job_id, source_id)
            continue
        if rmap is None or not rmap.matches_index(index):
            continue  # not retimed, or the map belongs to another state of the index
        for segment_id, match in existing.items():
            if match.source_id != source_id or match.status != "confirmed":
                continue
            try:
                outcome = apply_retime_to_match(
                    match,
                    rmap,
                    now_iso=ctx.utc_now_iso(),
                    guide_offset_sec=float(getattr(source, "guide_offset_sec", 0.0) or 0.0),
                    duration_sec=float(getattr(source, "duration_sec", 0.0) or 0.0),
                )
            except Exception:
                ctx.logger.warning(
                    "Sync job=%s: retime of confirmed match %s failed",
                    ctx.job_id,
                    segment_id,
                    exc_info=True,
                )
                continue
            if outcome == "retimed":
                retimed.append(segment_id)
            elif outcome == "review":
                review.append(segment_id)
    return retimed, review


def _is_manual_match(match: Any) -> bool:
    """Confirmed or hand-edited: the user's decision, never reviewed or overwritten."""
    metadata = getattr(match, "metadata", None) or {}
    return match.status == "confirmed" or bool(metadata.get("manual_updated_at"))


def _review_match(
    ctx: SyncMatchContext,
    state: _ReviewState,
    cost_tracker: SyncCostTracker,
    settings: Any,
    match: Any,
    segment_id: str,
) -> Any:
    """Optionally let the LLM re-rank the candidates of a weak match.

    Returns the (possibly replaced) match. ``CostBudgetExceeded`` switches the
    review off for the rest of the job; any other review error is recorded in
    ``state.failures`` and the deterministic match is kept.
    """
    semantic_candidates = ctx.semantic_review_candidates(match)
    if not (
        state.enabled
        and state.reviewed < state.limit
        and match.status != "auto_matched"
        and match.confidence < state.threshold
        and len(ctx.normalize_text(match.query_text)) >= 5
        and semantic_candidates
    ):
        return match
    try:
        decision = ctx.review_candidates_with_llm(
            query_text=match.query_text,
            speaker=match.speaker,
            candidates=semantic_candidates,
            settings=settings,
            usage_callback=ctx.semantic_usage_callback,
            budget=cost_tracker.llm_budget,
        )
        state.reviewed += 1
        if decision is not None:
            match = ctx.apply_semantic_review(
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
        state.enabled = False
        ctx.logger.warning(
            "Sync job=%s: LLM review stopped, job budget exceeded: %s", ctx.job_id, exc
        )
        ctx.update_job(ctx.job_id, llm_budget_error=str(exc))
    except Exception as exc:
        state.reviewed += 1
        state.failures.append(
            {
                "segment_id": segment_id,
                "error": str(exc),
            }
        )
    return match


def run_match_phase(
    ctx: SyncMatchContext,
    sources: Sequence[Any],
    indexes: Sequence[Any],
    *,
    cost_tracker: SyncCostTracker,
    checkpoint: SyncMatchCheckpoint,
) -> SyncMatchResult:
    """Match every scenario segment against the indexed sources.

    ``checkpoint`` is activated once matching state exists, so the caller's
    cancel/failure handlers persist the partial matches from then on.
    Cancellation raises whatever ``ctx.ensure_not_cancelled`` raises;
    ``CostBudgetExceeded`` outside the optional LLM review propagates; any
    other per-segment failure is recorded in ``segment_failures``.
    """
    job_id = ctx.job_id
    _update_sync_job = ctx.update_job
    ensure_not_cancelled = ctx.ensure_not_cancelled
    should_persist_progress = ctx.should_persist_progress
    getenv = ctx.getenv
    _sync_summary = ctx.sync_summary
    match_store = ctx.match_store

    normalization = ctx.load_normalization(ctx.paths["normalized"])
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
        tuple(ctx.normalize_text(name))
        for source in matching_sources
        for name in source.speaker_names
        if ctx.normalize_text(name)
    }
    existing = {item.segment_id: item for item in match_store.list()}
    initial_signatures = {key: _match_signature(item) for key, item in existing.items()}
    confirmed_parent_ids = {
        segment_id
        for segment_id, item in existing.items()
        if item.status == "confirmed"
        and item.metadata.get("normalization_revision") in (None, "", normalization_revision)
        and str(item.metadata.get("scenario_revision_id") or "") in ("", scenario_revision_id)
    }
    units = ctx.alignment_units(
        blocks,
        source_speakers=source_speakers,
        preserve_whole_uids=confirmed_parent_ids,
    )
    matcher_config = ctx.matcher_config_cls(search_window_sec=ctx.search_window_sec)
    matcher = ctx.matcher_cls(matching_sources, indexes, matcher_config)
    auto_threshold = float(getattr(matcher_config, "auto_threshold", DEFAULT_AUTO_THRESHOLD))
    offset_cluster_config = OffsetClusterConfig.from_env(getenv)
    settings = ctx.get_settings()
    llm_review_enabled = bool(settings.openrouter_api_key) and str(
        getenv("SYNC_LLM_REVIEW_ENABLED", "1")
    ).strip().lower() not in {"0", "false", "no", "off"}
    try:
        llm_review_threshold = float(getenv("SYNC_LLM_REVIEW_THRESHOLD", "0.80"))
    except ValueError:
        llm_review_threshold = 0.80
    llm_review_threshold = max(0.0, min(1.0, llm_review_threshold))
    try:
        llm_review_limit = max(0, int(getenv("SYNC_LLM_REVIEW_MAX_BLOCKS", "30")))
    except ValueError:
        llm_review_limit = 30
    if llm_review_enabled:
        # Prefer the settings frozen at job start; fall back to the environment
        # for callers that bypass the start route.
        llm_cost_env = ctx.job_llm_cost_env
        if not isinstance(llm_cost_env, dict):
            llm_cost_env = ctx.environ()
        if not cost_tracker.init_llm_budget(llm_cost_env):
            # Fail closed: without a valid budget no paid review call is made.
            llm_review_enabled = False
    review = _ReviewState(
        enabled=llm_review_enabled,
        threshold=llm_review_threshold,
        limit=llm_review_limit,
    )
    checkpoint_state = {"dirty": False}
    matching_started_at = ctx.utc_now_iso()
    matching_started_at_dt = _parse_iso_datetime(matching_started_at)
    rematched_this_run: set[str] = set()

    checkpoint_matches = _make_checkpoint_writer(
        ctx,
        existing=existing,
        initial_signatures=initial_signatures,
        checkpoint_state=checkpoint_state,
        rematched_this_run=rematched_this_run,
        matching_started_at_dt=matching_started_at_dt,
        normalization_revision=normalization_revision,
        scenario_revision_id=scenario_revision_id,
    )
    # From here on the cancel/failure handlers save partial matches.
    checkpoint.activate(checkpoint_matches)

    # Confirmed matches are never rematched below, so move them to the retimed word
    # times here. Marking them as rematched makes the checkpoint merge keep this copy
    # unless the user edited the match during the run (then theirs wins).
    retimed_ids, review_ids = _retime_confirmed_matches(ctx, existing, sources, indexes)
    if retimed_ids or review_ids:
        rematched_this_run.update(retimed_ids)
        rematched_this_run.update(review_ids)
        checkpoint_state["dirty"] = True
        ctx.logger.warning(
            "Sync job=%s: source timecodes were refined: %d confirmed matches moved, "
            "%d need a boundary check",
            job_id,
            len(retimed_ids),
            len(review_ids),
        )
        ctx.update_job(
            job_id,
            persist=False,
            retime_retimed_matches=len(retimed_ids),
            retime_review_segment_ids=review_ids,
        )

    current_ids = {
        str(unit.get("segment_id") or "").strip()
        for unit in units
        if str(unit.get("segment_id") or "").strip()
    }
    matching_fingerprint = ctx.matching_fingerprint(
        units=units,
        sources=matching_sources,
        indexes=indexes,
        normalization_revision=normalization_revision,
        timecode_mode=ctx.timecode_mode,
        search_window_sec=ctx.search_window_sec,
        llm_enabled=review.enabled,
        llm_model=settings.openrouter_model,
        llm_threshold=review.threshold,
        llm_limit=review.limit,
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
    # Strong fallback matches may be promoted by the offset-cluster pass once every
    # segment is matched; their paid LLM review waits until that is decided.
    deferred_review: list[str] = []
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
                (prior.status == "confirmed" and not ctx.rematch_confirmed)
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
                ctx.alignment_request_cls(
                    segment_id=segment_id,
                    text=str(unit.get("text") or ""),
                    speaker=str(unit.get("speaker") or ""),
                    approximate_timecode=unit.get("timecode_start"),
                    timecode_mode=ctx.timecode_mode,
                )
            )
            # A match whose review is deferred must not look finished: the fingerprint
            # (the only thing the reuse branch above trusts) is stamped once the
            # offset-cluster pass promoted it or its review ran. If the job is
            # cancelled or fails before that, the next run rematches it and the review
            # is not lost.
            review_deferred = offset_cluster_config.enabled and is_promotion_candidate(
                match, auto_threshold, offset_cluster_config.min_words
            )
            if review_deferred:
                deferred_review.append(segment_id)
            else:
                match = _review_match(ctx, review, cost_tracker, settings, match, segment_id)
            match.metadata["normalization_revision"] = normalization_revision
            match.metadata["scenario_revision_id"] = scenario_revision_id
            if not review_deferred:
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
            llm_reviewed=review.reviewed,
            llm_review_failures=list(review.failures),
            **cost_tracker.llm_cost_fields(),
        )

    # Offset-cluster pass: a systematic timecode shift shared by several strong
    # fallback matches of one source confirms each of them. It runs before the LLM
    # review of those matches, so promoted ones never reach the paid review.
    current_matches = [item for key, item in existing.items() if key in current_ids]
    offset_report = apply_offset_cluster_promotions(
        current_matches,
        matching_sources,
        offset_cluster_config,
        auto_threshold=auto_threshold,
        default_timecode_mode=ctx.timecode_mode,
    )
    if offset_report.promoted or offset_report.demoted:
        checkpoint_state["dirty"] = True
        # Reused matches were not rematched: without this the checkpoint merge would
        # restore their stale persisted copies over the promoted/reverted ones.
        rematched_this_run.update(offset_report.promoted)
        rematched_this_run.update(offset_report.demoted)
        ctx.logger.info(
            "Sync job=%s: offset-cluster pass promoted %d, reverted %d fallback matches",
            job_id,
            len(offset_report.promoted),
            len(offset_report.demoted),
        )
    # The pass decided the promoted ones: they need no review and may be reused.
    # Whatever is still ``needs_review`` gets its review below, one at a time.
    pending_review: list[str] = []
    for segment_id in deferred_review:
        deferred = existing.get(segment_id)
        if deferred is None or _is_manual_match(deferred):
            continue  # edited by the user during the run: their copy is authoritative
        if deferred.status == "auto_matched":
            deferred.metadata["matching_fingerprint"] = matching_fingerprint
        elif deferred.status == "needs_review":
            pending_review.append(segment_id)
    if deferred_review:
        checkpoint_state["dirty"] = True  # fingerprints were stamped on promoted matches
    if pending_review:
        _update_sync_job(job_id, persist=False, message="Reviewing deferred matches")
    for segment_id in pending_review:
        ensure_not_cancelled()
        deferred = existing.get(segment_id)
        if deferred is None or _is_manual_match(deferred) or deferred.status != "needs_review":
            continue
        try:
            reviewed = _review_match(ctx, review, cost_tracker, settings, deferred, segment_id)
            reviewed.metadata["matching_fingerprint"] = matching_fingerprint
            existing[segment_id] = reviewed
            checkpoint_state["dirty"] = True
        except CostBudgetExceeded:
            raise
        except Exception as exc:
            # Keep the unreviewed match (no fingerprint: the next run retries it).
            segment_failures.append({"segment_id": segment_id, "error": str(exc)})
        current = [item for key, item in existing.items() if key in current_ids]
        _update_sync_job(
            job_id,
            persist=should_persist_progress(),
            done=processed,
            **_sync_summary(current),
            segment_failures=list(segment_failures),
            llm_reviewed=review.reviewed,
            llm_review_failures=list(review.failures),
            **cost_tracker.llm_cost_fields(),
        )
    checkpoint_matches()
    return SyncMatchResult(
        matches=[item for key, item in existing.items() if key in current_ids],
        llm_reviewed=review.reviewed,
        llm_review_failures=review.failures,
        segment_failures=segment_failures,
        processed=processed,
        total_segments=total_segments,
    )
