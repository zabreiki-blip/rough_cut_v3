"""Audio-LLM verification of sync matches as a post-processing step.

Runs after the matching phase (like ``sync_asr_failures``): candidates that the
text matcher is least sure about are cut from the guide WAV and sent to
``stage2.sync_audio_verify.verify_clip``. The verdict can promote a
``needs_review`` match, demote an ``auto_matched`` one, or leave it untouched.
Manually ``confirmed`` matches are never touched.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from cost_budget import CostBudget, CostBudgetExceeded
from stage2.sync_audio_verify import (
    DEFAULT_MARGIN_SEC,
    AudioVerdict,
    estimate_clip_usd,
    verify_clip,
)
from stage2.sync_matcher import match_timing_unreliable, unreliable_timing_source_ids
from sync_match_phase import _is_manual_match

logger = logging.getLogger(__name__)

AUDIO_MISMATCH_REASON = "audio_mismatch"
AUDIO_PARTIAL_REASON = "audio_partial"
AUDIO_WIDE_RANGE_REASON = "audio_match_wide_range"
DEFAULT_MAX_CLIPS = 200
WORDS_PER_SEC = 2.0  # quote speech estimate: 0.5 s per word, at least 3 s
MIN_SPEECH_ESTIMATE_SEC = 3.0
WIDE_RANGE_FACTOR = 2.0
WIDE_RANGE_SLACK_SEC = 4.0
DEFAULT_AUTO_BELOW = 0.9
DEFAULT_MAX_USD = Decimal("0.5")
DEFAULT_WORKERS = 4
PROMOTE_MIN_COVERAGE = 0.9
PROMOTE_MIN_CONFIDENCE = 0.85
CACHE_SUBDIR = "audio-verify"

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}
_KEY_NAMES = ("SYNC_CLOUD_ASR_API_KEY", "OPENROUTER_API_KEY")


def _has_api_key(environ: Mapping[str, str]) -> bool:
    return any(str(environ.get(name) or "").strip() for name in _KEY_NAMES)


def resolve_audio_verify(
    requested: bool | None, asr_engine: str, environ: Mapping[str, str]
) -> tuple[bool, str]:
    """Whether to verify by audio, plus a reason when it is requested but impossible.

    Priority: the job parameter ``audio_verify``; then env ``SYNC_AUDIO_VERIFY``;
    otherwise on only for ``asr_engine == "cloud"`` with an API key present.
    """
    has_key = _has_api_key(environ)
    explicit = requested
    if explicit is None:
        raw = str(environ.get("SYNC_AUDIO_VERIFY") or "").strip().lower()
        if raw in _TRUE:
            explicit = True
        elif raw in _FALSE:
            explicit = False
    if explicit is None:
        return (asr_engine == "cloud" and has_key), ""
    if explicit and not has_key:
        return (
            False,
            "проверка по звуку отключена: не задан ключ API (SYNC_CLOUD_ASR_API_KEY / OPENROUTER_API_KEY)",
        )
    return explicit, ""


def audio_auto_below(environ: Mapping[str, str]) -> float:
    try:
        value = float(str(environ.get("SYNC_AUDIO_VERIFY_AUTO_BELOW") or DEFAULT_AUTO_BELOW))
    except ValueError:
        return DEFAULT_AUTO_BELOW
    return max(0.0, min(1.0, value))


def audio_max_usd(environ: Mapping[str, str]) -> Decimal:
    try:
        value = Decimal(str(environ.get("SYNC_AUDIO_VERIFY_MAX_USD") or DEFAULT_MAX_USD).strip())
    except InvalidOperation:
        return DEFAULT_MAX_USD
    return value if value.is_finite() and value >= 0 else DEFAULT_MAX_USD


def audio_max_clips(environ: Mapping[str, str]) -> int:
    try:
        return max(0, int(str(environ.get("SYNC_AUDIO_VERIFY_MAX_CLIPS") or DEFAULT_MAX_CLIPS)))
    except ValueError:
        return DEFAULT_MAX_CLIPS


def audio_workers(environ: Mapping[str, str]) -> int:
    try:
        return max(
            1, min(16, int(str(environ.get("SYNC_AUDIO_VERIFY_WORKERS") or DEFAULT_WORKERS)))
        )
    except ValueError:
        return DEFAULT_WORKERS


def _is_timecode_only(match: Any) -> bool:
    """No quote to compare against: the candidate sits on a bare scenario timecode."""
    return (
        getattr(match, "strategy", "") == "timecode_only"
        or match.metadata.get("review_reason") == "timecode_only"
    )


def _has_range(match: Any) -> bool:
    return (
        bool(match.source_id)
        and match.source_in_sec is not None
        and match.source_out_sec is not None
        and match.source_out_sec > match.source_in_sec
        and bool(str(match.query_text or "").strip())
    )


def select_candidates(
    matches: Iterable[Any], indexes: Iterable[Any], *, auto_below: float = DEFAULT_AUTO_BELOW
) -> list[Any]:
    """Matches worth an audio check, most important first.

    Order: imprecise timing (interpolated index not retimed, or a window-refined
    match) first, then ``needs_review``, then low-confidence ``auto_matched``.
    """
    imprecise_sources = unreliable_timing_source_ids(indexes)
    ranked: list[tuple[int, Any]] = []
    for match in matches:
        if match.status not in {"auto_matched", "needs_review"} or not _has_range(match):
            continue
        if _is_manual_match(match) or _is_timecode_only(match):
            continue
        if match.metadata.get("block_type") == "life":
            continue
        if match_timing_unreliable(match, imprecise_sources):
            rank = 0
        elif match.status == "needs_review":
            rank = 1
        elif match.confidence < auto_below:
            rank = 2
        else:
            continue
        ranked.append((rank, match))
    ranked.sort(key=lambda item: item[0])  # stable: keeps the matcher's order inside a rank
    return [match for _rank, match in ranked]


def _range_too_wide(match: Any) -> bool:
    """The edit range is much longer than the quote's estimated speech."""
    words = len(str(match.query_text or "").split())
    estimate = max(MIN_SPEECH_ESTIMATE_SEC, words / WORDS_PER_SEC)
    duration = float(match.source_out_sec) - float(match.source_in_sec)
    return duration > estimate * WIDE_RANGE_FACTOR + WIDE_RANGE_SLACK_SEC


def apply_verdict(
    match: Any,
    verdict: AudioVerdict,
    *,
    now: str,
    unreliable_sources: set[str] | frozenset[str] = frozenset(),
) -> str:
    """Apply one verdict in place; returns promoted/rejected/partial/kept/error."""
    record: dict[str, Any] = {
        "verdict": verdict.verdict,
        "coverage": verdict.coverage,
        "confidence": verdict.confidence,
        "heard_text": verdict.heard_text,
        "model": verdict.model,
        "at": now,
    }
    if verdict.verdict == "error":
        record["reason"] = verdict.reason
        match.metadata["audio_verified"] = record
        return "error"
    match.metadata["audio_verified"] = record
    if verdict.verdict == "match":
        if _is_timecode_only(match):
            return "kept"  # nothing to compare the sound with: an editor sets the bounds
        if (
            verdict.coverage >= PROMOTE_MIN_COVERAGE
            and verdict.confidence >= PROMOTE_MIN_CONFIDENCE
        ):
            if match.status != "needs_review":
                return "kept"
            if match_timing_unreliable(match, set(unreliable_sources)) and not match.metadata.get(
                "window_refined"
            ):
                return "kept"  # the sound is right, but the range is still imprecise
            if _range_too_wide(match):
                previous = match.metadata.get("review_reason")
                if previous and previous != AUDIO_WIDE_RANGE_REASON:
                    match.metadata.setdefault("review_reason_before_audio", previous)
                match.metadata["review_reason"] = AUDIO_WIDE_RANGE_REASON
                return "kept"
            match.status = "auto_matched"
            previous = match.metadata.pop("review_reason", None)
            if previous:
                match.metadata["review_reason_before_audio"] = previous
            return "promoted"
        return "kept"  # звучит похоже, но недостаточно уверенно — статус не меняем
    reason = AUDIO_MISMATCH_REASON if verdict.verdict == "no_match" else AUDIO_PARTIAL_REASON
    match.status = "needs_review"
    match.metadata["review_reason"] = reason
    return "rejected" if verdict.verdict == "no_match" else "partial"


@dataclass
class AudioVerifyResult:
    checked: int = 0  # candidates sent to the verifier
    verified: int = 0  # non-error verdicts
    promoted: int = 0
    rejected: int = 0  # no_match
    partial: int = 0
    errors: int = 0
    skipped: int = 0  # candidates left unchecked (budget or clip limit)
    cost_usd: float = 0.0
    budget_exhausted: bool = False
    clips_limited: bool = False
    warnings: list[str] = field(default_factory=list)
    changed_ids: list[str] = field(default_factory=list)
    verdicts: dict[str, AudioVerdict] = field(default_factory=dict)

    def job_fields(self) -> dict[str, Any]:
        return {
            "audio_verified": self.verified,
            "audio_promoted": self.promoted,
            "audio_rejected": self.rejected,
            "audio_partial": self.partial,
            "audio_errors": self.errors,
            "audio_skipped": self.skipped,
            "audio_cost_usd": self.cost_usd,
        }


def _clip_key(match: Any) -> tuple[Any, ...]:
    return (
        match.source_id,
        round(float(match.source_in_sec), 3),
        round(float(match.source_out_sec), 3),
        " ".join(str(match.query_text or "").split()),
        match.speaker,
    )


def _group_identical_clips(candidates: Sequence[Any]) -> list[list[Any]]:
    """One verifier call per identical clip (same range, quote and speaker)."""
    groups: dict[tuple[Any, ...], list[Any]] = {}
    for match in candidates:
        groups.setdefault(_clip_key(match), []).append(match)
    return list(groups.values())


def run_audio_verification(
    matches: Sequence[Any],
    sources: Sequence[Any],
    indexes: Iterable[Any],
    *,
    resolve_audio_path: Callable[[Any], Path],
    cache_dir: Path,
    budget: CostBudget | None,
    environ: Mapping[str, str],
    now: str,
    usage_callback: Callable[[dict[str, Any]], Any] | None = None,
    ensure_not_cancelled: Callable[[], None] | None = None,
    on_progress: Callable[[AudioVerifyResult, int], None] | None = None,
    verify: Callable[..., AudioVerdict] | None = None,
    result: AudioVerifyResult | None = None,
) -> AudioVerifyResult:
    """Verify the selected matches and mutate them in place (no store access).

    ``result`` may be passed in to keep the partial outcome when the run is
    interrupted (cancellation): paid verdicts are then still listed in ``changed_ids``.
    """
    verify = verify or verify_clip
    result = result if result is not None else AudioVerifyResult()
    indexes = list(indexes)
    imprecise_sources = unreliable_timing_source_ids(indexes)
    candidates = select_candidates(matches, indexes, auto_below=audio_auto_below(environ))
    if not candidates:
        return result
    groups = _group_identical_clips(candidates)
    max_clips = audio_max_clips(environ)
    if len(groups) > max_clips:
        groups = groups[:max_clips]
        result.clips_limited = True
    sources_by_id = {source.id: source for source in sources}
    max_usd = float(audio_max_usd(environ))
    workers = audio_workers(environ)
    callback_lock = threading.Lock()

    def locked_usage(payload: dict[str, Any]) -> None:
        if usage_callback is not None:
            with callback_lock:
                usage_callback(payload)

    def run_one(match: Any) -> AudioVerdict:
        source = sources_by_id.get(match.source_id)
        if source is None:
            return AudioVerdict("error", reason="unknown source")
        try:
            audio_path = resolve_audio_path(source)
        except Exception as exc:
            return AudioVerdict("error", reason=f"audio path: {exc}")
        return verify(
            audio_path=audio_path,
            start_sec=float(match.source_in_sec),
            end_sec=float(match.source_out_sec),
            quote=match.query_text,
            speaker=match.speaker,
            settings=environ,
            budget=budget,
            usage_callback=locked_usage,
            cache_dir=cache_dir,
        )

    def attempt(group: list[Any]) -> tuple[AudioVerdict | None, Exception | None]:
        try:
            return run_one(group[0]), None
        except Exception as exc:
            return None, exc

    def clip_estimate(match: Any) -> float:
        span = float(match.source_out_sec) - float(match.source_in_sec)
        return float(estimate_clip_usd(span + 2 * DEFAULT_MARGIN_SEC, environ))

    pool = ThreadPoolExecutor(max_workers=workers) if workers > 1 else None
    position = 0
    try:
        while position < len(groups):
            if ensure_not_cancelled is not None:
                ensure_not_cancelled()
            # Check the limit before EVERY clip, counting the estimates of clips already
            # launched in this batch: the overshoot is bounded by one clip.
            batch: list[list[Any]] = []
            inflight = 0.0
            while position + len(batch) < len(groups) and len(batch) < workers:
                if result.cost_usd + inflight >= max_usd:
                    result.budget_exhausted = True
                    break
                group = groups[position + len(batch)]
                batch.append(group)
                inflight += clip_estimate(group[0])
            if not batch:
                break
            outcomes = list(pool.map(attempt, batch)) if pool else [attempt(g) for g in batch]
            stop = result.budget_exhausted
            for group, (verdict, error) in zip(batch, outcomes):
                if isinstance(error, CostBudgetExceeded):
                    logger.warning("Audio verification stopped, job budget exceeded: %s", error)
                    result.budget_exhausted = True
                    stop = True
                    continue
                if verdict is None:
                    verdict = AudioVerdict("error", reason=f"unexpected: {error}")
                if not verdict.cached and verdict.cost_usd:
                    result.cost_usd += float(verdict.cost_usd)  # one paid call per clip
                for match in group:
                    result.checked += 1
                    outcome = apply_verdict(
                        match, verdict, now=now, unreliable_sources=imprecise_sources
                    )
                    result.verdicts[match.segment_id] = verdict
                    result.changed_ids.append(match.segment_id)
                    if outcome == "error":
                        result.errors += 1
                        continue
                    result.verified += 1
                    if outcome == "promoted":
                        result.promoted += 1
                    elif outcome == "rejected":
                        result.rejected += 1
                    elif outcome == "partial":
                        result.partial += 1
            position += len(batch)
            if on_progress is not None:
                on_progress(result, len(candidates))
            if stop:
                break
    finally:
        if pool is not None:
            pool.shutdown(wait=True)
    if result.budget_exhausted or result.clips_limited:
        # Unchecked = everything not yet sent, plus batch members stopped by the job budget.
        result.skipped = len(candidates) - (result.verified + result.errors)
    if result.budget_exhausted:
        result.warnings.append(
            f"проверка по звуку остановлена: исчерпан бюджет, не проверено {result.skipped} "
            f"из {len(candidates)} (потрачено ${result.cost_usd:.4f})"
        )
    elif result.clips_limited:
        result.warnings.append(
            f"проверка по звуку ограничена {max_clips} фрагментами "
            f"(SYNC_AUDIO_VERIFY_MAX_CLIPS), не проверено {result.skipped} из {len(candidates)}"
        )
    return result


def snapshot_matches(matches: Iterable[Any]) -> dict[str, tuple[Any, ...]]:
    """State of stored matches the verification started from (to detect later edits)."""
    return {
        item.segment_id: (item.status, item.source_in_sec, item.source_out_sec)
        for item in matches
    }


def persist_audio_results(
    stored: Sequence[Any],
    matches: Iterable[Any],
    changed_ids: Iterable[str],
    snapshot: Mapping[str, tuple[Any, ...]] | None = None,
) -> bool:
    """Copy audio fields onto the stored matches verified in THIS run.

    Manual matches (confirmed / hand-edited) stay untouched, and so does any stored match
    whose status or range differs from ``snapshot`` (the user edited it during the job).
    ``stored`` is mutated in place; returns whether anything changed.
    """
    wanted = set(changed_ids)
    by_id = {match.segment_id: match for match in matches if match.segment_id in wanted}
    changed = False
    for item in stored:
        source = by_id.get(item.segment_id)
        if source is None or _is_manual_match(item):
            continue
        if snapshot is not None:
            before = snapshot.get(item.segment_id)
            if before is None or before != (item.status, item.source_in_sec, item.source_out_sec):
                continue
        item.status = source.status
        for key in ("audio_verified", "review_reason", "review_reason_before_audio"):
            if key in source.metadata:
                item.metadata[key] = source.metadata[key]
            else:
                item.metadata.pop(key, None)
        changed = True
    return changed
