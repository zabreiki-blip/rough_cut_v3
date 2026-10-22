from __future__ import annotations

from typing import Any

import pytest

from stage2.sync_models import SyncMatch, SyncSource
from stage2.sync_offset_clusters import (
    OffsetClusterConfig,
    OffsetPoint,
    apply_offset_cluster_promotions,
    find_offset_cluster_promotions,
    is_promotion_candidate,
)


def _pts(source_id: str, offsets: list[float], prefix: str = "") -> list[OffsetPoint]:
    return [
        OffsetPoint(f"{prefix}{source_id}-{i}", source_id, off) for i, off in enumerate(offsets)
    ]


def test_two_clusters_in_one_source_are_promoted_independently() -> None:
    points = _pts("a", [660, 670, 680, 700, 1570, 1590, 1610, 1640])

    result = find_offset_cluster_promotions(points, link_sec=60, min_support=3)

    assert set(result) == {p.key for p in points}
    low = result["a-0"]
    high = result["a-4"]
    assert (low.min_sec, low.max_sec, low.support) == (670, 700, 3)
    assert (high.min_sec, high.max_sec, high.support) == (1590, 1640, 3)
    assert low.source_id == "a"


def test_cluster_of_two_points_is_not_valid() -> None:
    assert find_offset_cluster_promotions(_pts("a", [660, 670]), min_support=3) == {}
    # two far points + one cluster of 4: only the cluster is promoted
    points = _pts("a", [100, 105, 900, 910, 920, 930])
    result = find_offset_cluster_promotions(points, link_sec=60, min_support=3)
    assert set(result) == {"a-2", "a-3", "a-4", "a-5"}


def test_leave_one_out_three_points_do_not_confirm_themselves() -> None:
    points = _pts("a", [660, 670, 680])
    # each point has only 2 other supporters
    assert find_offset_cluster_promotions(points, min_support=3) == {}
    # with min_support=2 the same points do confirm each other
    assert set(find_offset_cluster_promotions(points, min_support=2)) == {p.key for p in points}


def test_four_points_each_confirmed_by_three_others() -> None:
    points = _pts("a", [660, 670, 680, 690])

    result = find_offset_cluster_promotions(points, min_support=3)

    assert set(result) == {p.key for p in points}
    assert all(item.support == 3 for item in result.values())


def test_outlier_outside_cluster_is_not_promoted() -> None:
    points = _pts("a", [660, 670, 680, 690, 1000])

    result = find_offset_cluster_promotions(points, link_sec=60, min_support=3)

    assert "a-4" not in result
    assert len(result) == 4


def test_point_within_link_distance_of_cluster_edge_is_promoted() -> None:
    points = _pts("a", [660, 670, 680, 690, 745])  # 745 - 690 = 55 <= 60

    assert "a-4" in find_offset_cluster_promotions(points, link_sec=60, min_support=3)
    points = _pts("a", [660, 670, 680, 690, 751])  # 61 > 60
    assert "a-4" not in find_offset_cluster_promotions(points, link_sec=60, min_support=3)


def test_sources_are_not_mixed() -> None:
    points = _pts("a", [660, 670]) + _pts("b", [665, 675])

    assert find_offset_cluster_promotions(points, min_support=3) == {}
    points = _pts("a", [660, 670, 680, 690]) + _pts("b", [665])
    result = find_offset_cluster_promotions(points, min_support=3)
    assert set(result) == {f"a-{i}" for i in range(4)}


def test_non_promotable_points_support_but_are_not_returned() -> None:
    points = _pts("a", [660, 670, 680]) + [OffsetPoint("anchor", "a", 690, promotable=False)]

    result = find_offset_cluster_promotions(points, min_support=3)

    assert set(result) == {"a-0", "a-1", "a-2"}
    assert all(item.support == 3 for item in result.values())


def test_invalid_parameters_rejected() -> None:
    with pytest.raises(ValueError):
        find_offset_cluster_promotions([], link_sec=0)
    with pytest.raises(ValueError):
        find_offset_cluster_promotions([], min_support=0)


# ---- match-level adapter -------------------------------------------------------------


def _source(source_id: str = "src", **kwargs: Any) -> SyncSource:
    return SyncSource(id=source_id, speaker="A", premiere_sequence="Seq", **kwargs)


def _match(
    segment_id: str,
    *,
    approx: str = "00:01:00",
    speech_in: float = 700.0,
    status: str = "needs_review",
    strategy: str = "fallback_full",
    confidence: float = 0.9,
    source_id: str = "src",
    query_text: str = "some long enough quote here",
    source_in: float | None = None,
    **metadata: Any,
) -> SyncMatch:
    meta = {
        "ambiguous": False,
        "semantic_flags": [],
        "gap_flags": [],
        "timecode_mode": "relative",
        "speech_in_sec": speech_in,
        **metadata,
    }
    return SyncMatch(
        segment_id=segment_id,
        query_text=query_text,
        approximate_timecode=approx,
        source_id=source_id,
        source_in_sec=max(0.0, speech_in - 2) if source_in is None else source_in,
        source_out_sec=(speech_in + 5) if source_in is None else source_in + 7,
        confidence=confidence,
        status=status,  # type: ignore[arg-type]
        strategy=strategy,
        metadata=meta,
    )


def _cluster_matches() -> list[SyncMatch]:
    # offsets (speech_in - 60 s approx): 640, 650, 660, 670
    return [_match(f"m{i}", speech_in=60 + 640 + 10 * i) for i in range(4)]


def _apply(matches: list[SyncMatch], sources: list[SyncSource] | None = None, **kw: Any):
    return apply_offset_cluster_promotions(
        matches,
        sources or [_source()],
        OffsetClusterConfig(**kw),
        auto_threshold=0.78,
        default_timecode_mode="relative",
    )


def test_apply_promotes_and_records_metadata_without_touching_strategy() -> None:
    matches = _cluster_matches()

    report = _apply(matches)

    assert report.promoted == ["m0", "m1", "m2", "m3"]
    first = matches[0]
    assert first.status == "auto_matched"
    assert first.strategy == "fallback_full"
    cluster = first.metadata["offset_cluster"]
    assert cluster["source_id"] == "src"
    assert cluster["support"] == 3
    assert (cluster["min_sec"], cluster["max_sec"]) == (650.0, 670.0)
    assert cluster["offset_sec"] == 640.0
    assert cluster["promoted"] is True
    assert first.metadata["promoted_by"] == "offset_cluster"


def test_apply_uses_guide_offset_coordinates_and_display_timecode() -> None:
    # speech_in already includes guide_offset_sec; display mode subtracts sequence start tc
    matches = [
        _match(f"d{i}", approx="01:00:30:00", speech_in=630 + 10 * i, timecode_mode="display")
        for i in range(4)
    ]
    source = _source(sequence_start_tc="01:00:00:00", guide_offset_sec=5.0)

    report = _apply(matches, [source])

    # approx = 30 s in source coordinates; offsets 600..630 -> one cluster
    assert report.promoted == ["d0", "d1", "d2", "d3"]
    assert matches[0].metadata["offset_cluster"]["offset_sec"] == 600.0


@pytest.mark.parametrize(
    "override",
    [
        {"confidence": 0.5},
        {"ambiguous": True},
        {"semantic_flags": ["number_mismatch"]},
        {"gap_flags": ["large_time_gap"]},
        {"timing_flags": ["timing_unrefined"]},
        {"plausibility_flags": ["collapsed_word_times"]},
        {"strategy": "near_timecode"},
        {"strategy": "llm_semantic_review"},
        {"approx": ""},
        {"status": "not_found"},
    ],
)
def test_ineligible_matches_are_not_promoted_nor_support(override: dict[str, Any]) -> None:
    matches = _cluster_matches()[:3] + [_match("x", speech_in=60 + 645, **override)]

    report = _apply(matches)

    # 3 eligible points only -> leave-one-out gives 2 supporters, nobody is promoted
    assert report.promoted == []
    assert all(m.status == matches[i].status for i, m in enumerate(matches))
    assert [m.status for m in matches[:3]] == ["needs_review"] * 3
    assert "offset_cluster" not in matches[3].metadata


def test_confirmed_match_is_not_changed_even_inside_cluster() -> None:
    matches = _cluster_matches()
    confirmed = _match("c", speech_in=60 + 655, status="confirmed")
    before = confirmed.to_dict()

    _apply(matches + [confirmed])

    assert confirmed.to_dict() == before


def test_disabled_config_changes_nothing() -> None:
    matches = _cluster_matches()

    report = _apply(matches, enabled=False)

    assert report.promoted == [] and report.demoted == []
    assert [m.status for m in matches] == ["needs_review"] * 4


def test_unknown_source_or_missing_speech_in_is_skipped() -> None:
    matches = _cluster_matches()[:3]
    matches.append(_match("other", speech_in=700, source_id="missing"))
    no_speech = _match("nospeech")
    del no_speech.metadata["speech_in_sec"]
    matches.append(no_speech)

    assert _apply(matches).promoted == []


def test_previously_promoted_match_is_demoted_when_support_disappears() -> None:
    matches = _cluster_matches()
    _apply(matches)
    assert all(m.status == "auto_matched" for m in matches)

    report = _apply(matches[:3])

    assert report.promoted == []
    assert report.demoted == ["m0", "m1", "m2"]
    assert all(m.status == "needs_review" for m in matches[:3])
    assert "offset_cluster" not in matches[0].metadata
    assert "promoted_by" not in matches[0].metadata


def test_promotion_is_idempotent_and_disabling_reverts() -> None:
    matches = _cluster_matches()
    _apply(matches)
    assert _apply(matches).promoted == []  # already auto, nothing newly promoted
    assert all(m.status == "auto_matched" for m in matches)

    report = _apply(matches, enabled=False)

    assert report.demoted == ["m0", "m1", "m2", "m3"]
    assert all(m.status == "needs_review" for m in matches)


def test_is_promotion_candidate() -> None:
    assert is_promotion_candidate(_match("a"), 0.78)
    assert not is_promotion_candidate(_match("a", confidence=0.7), 0.78)
    assert not is_promotion_candidate(_match("a", strategy="near_timecode"), 0.78)


def test_config_from_env() -> None:
    env = {
        "SYNC_OFFSET_CLUSTER_ENABLED": "0",
        "SYNC_OFFSET_CLUSTER_LINK_SEC": "30",
        "SYNC_OFFSET_CLUSTER_MIN_SUPPORT": "5",
    }
    cfg = OffsetClusterConfig.from_env(lambda name, default: env.get(name, default))
    assert (cfg.enabled, cfg.link_sec, cfg.min_support) == (False, 30.0, 5)

    default = OffsetClusterConfig.from_env(lambda name, default: default)
    assert (default.enabled, default.link_sec, default.min_support) == (True, 60.0, 3)
    assert (default.max_width_sec, default.min_words) == (240.0, 5)
    tuned = OffsetClusterConfig.from_env(
        lambda name, default: {
            "SYNC_OFFSET_CLUSTER_MAX_WIDTH_SEC": "100",
            "SYNC_OFFSET_CLUSTER_MIN_WORDS": "8",
        }.get(name, default)
    )
    assert (tuned.max_width_sec, tuned.min_words) == (100.0, 8)

    bad = OffsetClusterConfig.from_env(
        lambda name, default: {
            "SYNC_OFFSET_CLUSTER_LINK_SEC": "abc",
            "SYNC_OFFSET_CLUSTER_MIN_SUPPORT": "-2",
            "SYNC_OFFSET_CLUSTER_MAX_WIDTH_SEC": "-1",
            "SYNC_OFFSET_CLUSTER_MIN_WORDS": "x",
        }.get(name, default)
    )
    assert (bad.link_sec, bad.min_support) == (60.0, 3)
    assert (bad.max_width_sec, bad.min_words) == (240.0, 5)


# ---- cluster width ---------------------------------------------------------------------


def test_wide_chain_cluster_is_not_valid() -> None:
    # single linkage chains 0,55,...,330 into one 330 s wide cluster: not a shift
    points = _pts("a", [0, 55, 110, 165, 220, 275, 330])

    assert (
        find_offset_cluster_promotions(points, link_sec=60, min_support=3, max_width_sec=240) == {}
    )
    # without the width limit the chain would confirm every point
    assert (
        len(find_offset_cluster_promotions(points, link_sec=60, min_support=3, max_width_sec=1000))
        == 7
    )


@pytest.mark.parametrize("offsets", [[0, 29, 58, 87], [0, 30, 60, 90, 120, 150]])
def test_realistic_cluster_widths_are_promoted(offsets: list[float]) -> None:
    result = find_offset_cluster_promotions(
        _pts("a", offsets), link_sec=60, min_support=3, max_width_sec=240
    )

    assert set(result) == {f"a-{i}" for i in range(len(offsets))}


def test_width_limit_must_be_positive() -> None:
    with pytest.raises(ValueError):
        find_offset_cluster_promotions([], max_width_sec=0)


def test_apply_wide_chain_is_not_promoted() -> None:
    matches = [_match(f"w{i}", speech_in=60 + 55 * i) for i in range(7)]

    assert _apply(matches).promoted == []
    assert {m.status for m in matches} == {"needs_review"}


def test_apply_width_is_configurable() -> None:
    matches = [_match(f"w{i}", speech_in=60 + 30 * i) for i in range(6)]  # width 150

    assert _apply(matches, max_width_sec=100).promoted == []
    assert len(_apply(matches, max_width_sec=240).promoted) == 6


# ---- minimum words ---------------------------------------------------------------------


def test_short_quotes_are_neither_promoted_nor_support() -> None:
    short = [_match(f"s{i}", speech_in=60 + 640 + 10 * i, query_text="да нет ну") for i in range(2)]
    matches = _cluster_matches()[:3] + short  # only 3 long points -> no support

    assert _apply(matches).promoted == []
    assert {m.status for m in matches} == {"needs_review"}


def test_short_quote_is_not_promoted_even_with_support() -> None:
    matches = _cluster_matches() + [_match("short", speech_in=60 + 645, query_text="да, конечно")]

    report = _apply(matches)

    assert "short" not in report.promoted
    assert matches[-1].status == "needs_review"
    assert len(report.promoted) == 4


def test_min_words_uses_matcher_normalization_and_is_configurable() -> None:
    # "5-ю" normalizes to two tokens, like the matcher's index
    text = "Это 2 слова, ну и 5-ю"
    matches = [_match(f"n{i}", speech_in=60 + 640 + 10 * i, query_text=text) for i in range(4)]

    assert _apply(matches, min_words=8).promoted == []
    assert len(_apply(matches, min_words=7).promoted) == 4


def test_is_promotion_candidate_checks_min_words() -> None:
    assert not is_promotion_candidate(_match("a", query_text="one two three four"), 0.78)
    assert is_promotion_candidate(_match("a", query_text="one two three four"), 0.78, min_words=4)


# ---- manual / confirmed anchors ------------------------------------------------------------


def test_manually_confirmed_match_is_an_anchor_for_the_others() -> None:
    # exactly min_support + 1 points: 3 promotable + 1 confirmed anchor
    matches = _cluster_matches()[:3]
    confirmed = _match(
        "c",
        speech_in=60 + 650,
        status="confirmed",
        strategy="near_timecode",
        manual_updated_at="2026-01-01T00:00:00+00:00",
    )
    before = confirmed.to_dict()
    matches.append(confirmed)

    report = _apply(matches)

    assert report.promoted == ["m0", "m1", "m2"]
    assert confirmed.to_dict() == before


def test_confirmed_fallback_match_uses_its_current_source_in_not_speech_in() -> None:
    # The user moved the range: metadata speech_in_sec is stale, source_in_sec is real.
    # speech_in coordinate = source_in + head_padding_sec (2 s): 60+640+2 -> offset 640.
    matches = _cluster_matches()[:3]
    moved = _match(
        "c",
        speech_in=5000.0,
        status="confirmed",
        source_in=60 + 650 - 2,
        manual_updated_at="2026-01-01T00:00:00+00:00",
    )
    matches.append(moved)

    assert _apply(matches).promoted == ["m0", "m1", "m2"]
    # moved far away: no longer supports the cluster
    moved.source_in_sec = 3000.0
    moved.source_out_sec = 3005.0
    for m in matches[:3]:
        m.status = "needs_review"
        m.metadata.pop("promoted_by", None)
        m.metadata.pop("offset_cluster", None)
    assert _apply(matches).promoted == []


def test_manual_anchor_without_speech_in_metadata_uses_default_padding() -> None:
    matches = _cluster_matches()[:3]
    manual = _match(
        "c",
        status="needs_review",
        source_in=60 + 650 - 2,
        manual_updated_at="2026-01-01T00:00:00+00:00",
    )
    del manual.metadata["speech_in_sec"]
    matches.append(manual)

    assert _apply(matches).promoted == ["m0", "m1", "m2"]


def test_confirmed_anchor_without_source_in_or_approx_or_short_text_is_ignored() -> None:
    base = _cluster_matches()[:3]
    for kwargs in (
        {"approx": ""},
        {"query_text": "да нет"},
        {"source_id": "missing"},
    ):
        anchor = _match("c", speech_in=60 + 650, status="confirmed", **kwargs)
        assert _apply(base + [anchor]).promoted == []
    nothing = _match("c", status="confirmed")
    nothing.source_in_sec = None
    nothing.source_out_sec = None
    assert _apply(base + [nothing]).promoted == []


# ---- legacy matches -------------------------------------------------------------------------


def test_match_without_gap_flags_key_is_not_promotable_nor_support() -> None:
    matches = _cluster_matches()[:3]
    legacy = _match("legacy", speech_in=60 + 650)
    del legacy.metadata["gap_flags"]
    matches.append(legacy)

    assert not is_promotion_candidate(legacy, 0.78)
    assert _apply(matches).promoted == []
    assert legacy.status == "needs_review"


def test_legacy_confirmed_match_without_gap_flags_still_anchors() -> None:
    matches = _cluster_matches()[:3]
    legacy = _match("legacy", speech_in=60 + 650, status="confirmed")
    del legacy.metadata["gap_flags"]
    matches.append(legacy)

    assert _apply(matches).promoted == ["m0", "m1", "m2"]
