"""Promote strong global-fallback matches that agree on a systematic timecode shift.

A scenario timecode can be shifted against the source transcript by a stretch of the
file (an edit that dropped or inserted material). The matcher then finds a perfect
quote far from ``approximate_timecode`` through the global ``fallback_full`` search and
leaves it in ``needs_review`` only because of that disagreement. When many such matches
of one source share (almost) the same ``found_in - approx`` offset, the shift is real:
each of them is confirmed by the others and may be auto-matched.

The clustering core (:func:`find_offset_cluster_promotions`) is a pure function. The
match-level adapter (:func:`apply_offset_cluster_promotions`) only reads/writes match
status and metadata; it performs no I/O.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from math import isfinite
from statistics import median
from types import SimpleNamespace
from typing import Any

from .sync_matcher import MatcherConfig, _approx_for_source, normalize_text

FALLBACK_STRATEGY = "fallback_full"
PROMOTED_BY = "offset_cluster"
DEFAULT_LINK_SEC = 60.0
DEFAULT_MIN_SUPPORT = 3
DEFAULT_AUTO_THRESHOLD = 0.78
# A real shift covers a stretch of the file, not all of it: a chain of loosely linked
# offsets wider than this is coincidence, not one systematic shift.
DEFAULT_MAX_WIDTH_SEC = 240.0
# Very short quotes ("да", "ну конечно") occur many times, a perfect hit proves nothing.
DEFAULT_MIN_WORDS = 5


@dataclass(frozen=True)
class OffsetPoint:
    """One anchor: the offset ``found_in - approx`` of a clean fallback match."""

    key: str
    source_id: str
    offset_sec: float
    # Only promotable points are returned; every point supports the others.
    promotable: bool = True


@dataclass(frozen=True)
class OffsetCluster:
    source_id: str
    offset_sec: float  # offset of the promoted point itself
    min_sec: float  # extent of the supporting cluster (promoted point excluded)
    max_sec: float
    center_sec: float
    support: int  # number of OTHER anchors in the cluster


def _clusters(sorted_offsets: Sequence[float], link_sec: float) -> list[list[float]]:
    clusters: list[list[float]] = []
    for value in sorted_offsets:
        if clusters and value - clusters[-1][-1] <= link_sec:
            clusters[-1].append(value)
        else:
            clusters.append([value])
    return clusters


def find_offset_cluster_promotions(
    points: Iterable[OffsetPoint],
    *,
    link_sec: float = DEFAULT_LINK_SEC,
    min_support: int = DEFAULT_MIN_SUPPORT,
    max_width_sec: float = DEFAULT_MAX_WIDTH_SEC,
) -> dict[str, OffsetCluster]:
    """Return the cluster that confirms each promotable point, keyed by ``point.key``.

    Offsets are clustered per source by single linkage (neighbours closer than
    ``link_sec``). A point is confirmed when, with the point itself left out, a cluster
    of at least ``min_support`` other points exists and the point lies within
    ``link_sec`` of that cluster's extent. Leave-one-out stops a point from confirming
    itself; the other points may be promotable or plain anchors. A supporting cluster
    wider than ``max_width_sec`` is not one shift and is ignored; the width is checked
    both without the point and for the cluster the point belongs to with it, so a chain
    of loosely linked offsets is not accepted by leaving a middle point out.
    """
    if not (isfinite(link_sec) and link_sec > 0):
        raise ValueError("link_sec must be positive")
    if min_support < 1:
        raise ValueError("min_support must be at least 1")
    if not (isfinite(max_width_sec) and max_width_sec > 0):
        raise ValueError("max_width_sec must be positive")
    by_source: dict[str, list[OffsetPoint]] = defaultdict(list)
    for point in points:
        if isfinite(point.offset_sec):
            by_source[point.source_id].append(point)

    result: dict[str, OffsetCluster] = {}
    for source_id, group in by_source.items():
        if len(group) <= min_support:
            continue
        group.sort(key=lambda item: item.offset_sec)
        full_clusters = _clusters([item.offset_sec for item in group], link_sec)
        for index, point in enumerate(group):
            if not point.promotable:
                continue
            # Leaving the point out can split a chain apart; the cluster it belongs to
            # WITH it must be a plausible single shift as well.
            own = next(c for c in full_clusters if c[0] <= point.offset_sec <= c[-1])
            if own[-1] - own[0] > max_width_sec:
                continue
            others = [item.offset_sec for i, item in enumerate(group) if i != index]
            best: list[float] | None = None
            for cluster in _clusters(others, link_sec):
                if len(cluster) < min_support or cluster[-1] - cluster[0] > max_width_sec:
                    continue
                if not cluster[0] - link_sec <= point.offset_sec <= cluster[-1] + link_sec:
                    continue
                if best is None or len(cluster) > len(best):
                    best = cluster
            if best is not None:
                result[point.key] = OffsetCluster(
                    source_id=source_id,
                    offset_sec=point.offset_sec,
                    min_sec=best[0],
                    max_sec=best[-1],
                    center_sec=float(median(best)),
                    support=len(best),
                )
    return result


# ---- match-level adapter -------------------------------------------------------------


def _env_float(raw: str, default: float) -> float:
    try:
        value = float(str(raw).strip())
    except ValueError:
        return default
    return value if isfinite(value) and value > 0 else default


def _env_int(raw: str, default: int) -> int:
    try:
        value = int(str(raw).strip())
    except ValueError:
        return default
    return value if value >= 1 else default


@dataclass(frozen=True)
class OffsetClusterConfig:
    enabled: bool = True
    link_sec: float = DEFAULT_LINK_SEC
    min_support: int = DEFAULT_MIN_SUPPORT
    max_width_sec: float = DEFAULT_MAX_WIDTH_SEC
    min_words: int = DEFAULT_MIN_WORDS

    @classmethod
    def from_env(cls, getenv: Callable[[str, str], str]) -> "OffsetClusterConfig":
        """``SYNC_OFFSET_CLUSTER_{ENABLED,LINK_SEC,MIN_SUPPORT,MAX_WIDTH_SEC,MIN_WORDS}``.

        Bad values fall back to the defaults.
        """
        enabled = str(getenv("SYNC_OFFSET_CLUSTER_ENABLED", "1")).strip().lower() not in {
            "0",
            "false",
            "no",
            "off",
        }
        return cls(
            enabled=enabled,
            link_sec=_env_float(getenv("SYNC_OFFSET_CLUSTER_LINK_SEC", ""), DEFAULT_LINK_SEC),
            min_support=_env_int(
                getenv("SYNC_OFFSET_CLUSTER_MIN_SUPPORT", ""), DEFAULT_MIN_SUPPORT
            ),
            max_width_sec=_env_float(
                getenv("SYNC_OFFSET_CLUSTER_MAX_WIDTH_SEC", ""), DEFAULT_MAX_WIDTH_SEC
            ),
            min_words=_env_int(getenv("SYNC_OFFSET_CLUSTER_MIN_WORDS", ""), DEFAULT_MIN_WORDS),
        )


@dataclass
class OffsetClusterReport:
    promoted: list[str] = field(default_factory=list)
    demoted: list[str] = field(default_factory=list)


def _metadata(match: Any) -> dict[str, Any] | None:
    metadata = getattr(match, "metadata", None)
    return metadata if isinstance(metadata, dict) else None


def _is_manual(match: Any, metadata: dict[str, Any]) -> bool:
    return getattr(match, "status", None) == "confirmed" or bool(
        metadata.get("manual_updated_at")
    )


def _was_promoted(match: Any, metadata: dict[str, Any]) -> bool:
    return (
        getattr(match, "status", None) == "auto_matched"
        and metadata.get("promoted_by") == PROMOTED_BY
    )


def _enough_words(match: Any, min_words: int) -> bool:
    return len(normalize_text(str(getattr(match, "query_text", "") or ""))) >= min_words


def is_promotion_candidate(
    match: Any, auto_threshold: float, min_words: int = DEFAULT_MIN_WORDS
) -> bool:
    """A strong fallback match whose only obstacle to auto status is the fallback itself.

    Manual/confirmed matches, LLM-reviewed ones (a different strategy), ambiguous,
    flagged, low-confidence or too short matches never qualify. A match without the
    ``gap_flags`` key predates the internal-gap check, so its gaps are unknown: it
    does not qualify either.
    """
    metadata = _metadata(match)
    if metadata is None or _is_manual(match, metadata):
        return False
    if "gap_flags" not in metadata or not _enough_words(match, min_words):
        return False
    if getattr(match, "strategy", "") != FALLBACK_STRATEGY:
        return False
    if getattr(match, "status", None) not in ("needs_review", "auto_matched"):
        return False
    if not str(getattr(match, "approximate_timecode", "") or "").strip():
        return False
    if float(getattr(match, "confidence", 0.0) or 0.0) < auto_threshold:
        return False
    if metadata.get("ambiguous"):
        return False
    if metadata.get("semantic_flags") or metadata.get("gap_flags"):
        return False
    return True


def _manual_speech_in(match: Any, metadata: dict[str, Any]) -> float | None:
    """``speech_in`` coordinate of a manual/confirmed match from its CURRENT range.

    The editor may have moved the range, so the stale ``speech_in_sec`` metadata is not
    used. The matcher stores ``source_in = speech_in - head_padding_sec`` (``speech_in``
    is the coordinate system of the scenario timecode), so the padding is added back.
    """
    source_in = getattr(match, "source_in_sec", None)
    if not isinstance(source_in, (int, float)) or not isfinite(source_in):
        return None
    padding = metadata.get("head_padding_sec")
    if not isinstance(padding, (int, float)) or not isfinite(padding):
        padding = MatcherConfig.head_padding_sec
    return float(source_in) + float(padding)


def _offset(
    match: Any, source: Any, default_timecode_mode: str, *, manual: bool = False
) -> float | None:
    metadata = _metadata(match) or {}
    speech_in = _manual_speech_in(match, metadata) if manual else metadata.get("speech_in_sec")
    if not isinstance(speech_in, (int, float)) or not isfinite(speech_in):
        return None
    request = SimpleNamespace(
        approximate_timecode=getattr(match, "approximate_timecode", ""),
        timecode_mode=str(metadata.get("timecode_mode") or default_timecode_mode),
    )
    try:
        approx = _approx_for_source(request, source)  # type: ignore[arg-type]
    except ValueError:
        return None
    if approx is None:
        return None
    # ``speech_in_sec`` already includes ``guide_offset_sec``, like the scenario approx.
    return float(speech_in) - approx


def apply_offset_cluster_promotions(
    matches: Iterable[Any],
    sources: Iterable[Any],
    config: OffsetClusterConfig,
    *,
    auto_threshold: float = DEFAULT_AUTO_THRESHOLD,
    default_timecode_mode: str = "relative",
) -> OffsetClusterReport:
    """Promote ``needs_review`` fallback matches confirmed by a shared offset cluster.

    Mutates ``status``/``metadata`` of the affected matches in place. ``strategy`` is
    kept (``fallback_full``); the reason is stored in ``metadata["promoted_by"]`` and
    ``metadata["offset_cluster"]``. A match promoted by an earlier run is re-evaluated
    and returned to ``needs_review`` when its support disappeared (or the pass is
    disabled). Confirmed and manually edited matches are never changed, but they are
    anchors that support the others (offset from their current range).
    """
    matches = list(matches)
    report = OffsetClusterReport()
    by_id = {str(getattr(source, "id", "")): source for source in sources}
    by_key: dict[str, Any] = {}
    points: list[OffsetPoint] = []
    if config.enabled:
        for match in matches:
            source_id = str(getattr(match, "source_id", "") or "")
            source = by_id.get(source_id)
            segment_id = str(getattr(match, "segment_id", "") or "")
            if source is None or not segment_id:
                continue
            metadata = _metadata(match)
            if metadata is None:
                continue
            if _is_manual(match, metadata):
                # An editor's decision is ground truth for the shift: an anchor of any
                # strategy, never promoted itself.
                if not str(getattr(match, "approximate_timecode", "") or "").strip():
                    continue
                if not _enough_words(match, config.min_words):
                    continue
                offset = _offset(match, source, default_timecode_mode, manual=True)
                promotable = False
            else:
                if not is_promotion_candidate(match, auto_threshold, config.min_words):
                    continue
                offset = _offset(match, source, default_timecode_mode)
                promotable = match.status == "needs_review" or _was_promoted(match, metadata)
            if offset is None:
                continue
            by_key[segment_id] = match
            points.append(
                OffsetPoint(
                    key=segment_id,
                    source_id=source_id,
                    offset_sec=offset,
                    promotable=promotable,
                )
            )
    clusters = (
        find_offset_cluster_promotions(
            points,
            link_sec=config.link_sec,
            min_support=config.min_support,
            max_width_sec=config.max_width_sec,
        )
        if points
        else {}
    )
    for key, cluster in clusters.items():
        match = by_key[key]
        metadata = match.metadata
        if match.status == "needs_review":
            match.status = "auto_matched"
            report.promoted.append(key)
        metadata["promoted_by"] = PROMOTED_BY
        metadata["offset_cluster"] = {
            "source_id": cluster.source_id,
            "offset_sec": round(cluster.offset_sec, 3),
            "center_sec": round(cluster.center_sec, 3),
            "min_sec": round(cluster.min_sec, 3),
            "max_sec": round(cluster.max_sec, 3),
            "support": cluster.support,
            "link_sec": config.link_sec,
            "min_support": config.min_support,
            "max_width_sec": config.max_width_sec,
            "promoted": True,
        }
    for match in matches:
        metadata = _metadata(match)
        if metadata is None or not _was_promoted(match, metadata) or _is_manual(match, metadata):
            continue
        key = str(getattr(match, "segment_id", "") or "")
        if key in clusters:
            continue
        match.status = "needs_review"
        metadata.pop("promoted_by", None)
        metadata.pop("offset_cluster", None)
        report.demoted.append(key)
    return report
