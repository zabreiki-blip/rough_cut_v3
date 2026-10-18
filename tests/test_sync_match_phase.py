from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from cost_budget import CostBudgetExceeded
from sync_match_phase import (
    SyncMatchCheckpoint,
    SyncMatchContext,
    SyncMatchResult,
    _make_checkpoint_writer,
    _match_signature,
    run_match_phase,
)


class _Cancelled(Exception):
    pass


def _match(segment_id: str, *, status: str = "auto_matched", confidence: float = 0.95) -> Any:
    return SimpleNamespace(
        segment_id=segment_id,
        status=status,
        confidence=confidence,
        query_text="some long enough quote",
        speaker="",
        metadata={},
    )


class _Store:
    def __init__(self, items: list[Any] | None = None) -> None:
        self.items = list(items or [])
        self.replaced: list[list[str]] = []

    def list(self) -> list[Any]:
        return list(self.items)

    def replace_all(self, items: list[Any]) -> None:
        self.items = list(items)
        self.replaced.append([item.segment_id for item in items])


class _Tracker:
    llm_budget = None

    def __init__(self, *, budget_ok: bool = True) -> None:
        self.budget_ok = budget_ok
        self.budget_inits = 0

    def init_llm_budget(self, environ: Any) -> bool:
        self.budget_inits += 1
        return self.budget_ok

    def llm_cost_fields(self) -> dict[str, Any]:
        return {"llm_cost_usd": 0.0}


class _Harness:
    def __init__(self, segment_ids: list[str], *, review: bool = False) -> None:
        self.updates: list[tuple[tuple, dict]] = []
        self.store = _Store()
        self.segment_ids = segment_ids
        self.cancel_after: int | None = None
        self.matched: list[str] = []
        self.match_status = "auto_matched"
        self.match_confidence = 0.95
        self.review_calls = 0
        self.review_error: Exception | None = None
        self.review = review
        self.cancelled = False
        self.tracker = _Tracker()
        self.checkpoint = SyncMatchCheckpoint()
        self.env = {"SYNC_LLM_REVIEW_ENABLED": "1"}

    def _update(self, *args: Any, **kwargs: Any) -> None:
        self.updates.append((args, kwargs))

    def _ensure_not_cancelled(self) -> None:
        if self.cancel_after is not None and len(self.matched) >= self.cancel_after:
            raise _Cancelled("Sync job cancelled")

    def _match_segment(self, request: Any) -> Any:
        self.matched.append(request.segment_id)
        return _match(
            request.segment_id, status=self.match_status, confidence=self.match_confidence
        )

    def _review(self, **kwargs: Any) -> Any:
        self.review_calls += 1
        if self.review_error is not None:
            raise self.review_error
        return None

    def context(self, **overrides: Any) -> SyncMatchContext:
        units = [{"segment_id": sid, "text": f"text {sid}"} for sid in self.segment_ids]
        harness = self

        class _Matcher:
            def __init__(self, *_args: Any) -> None:
                pass

            def match(self, request: Any) -> Any:
                return harness._match_segment(request)

        values: dict[str, Any] = {
            "job_id": "job-1",
            "paths": {"normalized": "norm"},
            "match_store": self.store,
            "timecode_mode": "none",
            "rematch_confirmed": False,
            "search_window_sec": 60.0,
            "job_llm_cost_env": {},
            "ensure_not_cancelled": self._ensure_not_cancelled,
            "should_persist_progress": lambda force=False: False,
            "semantic_usage_callback": lambda _event: None,
            "logger": logging.getLogger("test_sync_match_phase"),
            "getenv": lambda name, default: self.env.get(name, default),
            "environ": lambda: self.env,
            "data_lock": threading.Lock(),
            "update_job": self._update,
            "utc_now_iso": lambda: "2026-01-01T00:00:00+00:00",
            "load_normalization": lambda _path: {"revision": 3, "blocks": []},
            "alignment_units": lambda blocks, **kw: units,
            "matcher_cls": _Matcher,
            "matcher_config_cls": lambda **kw: kw,
            "get_settings": lambda: SimpleNamespace(
                openrouter_api_key="key" if self.review else "", openrouter_model="m"
            ),
            "semantic_review_candidates": lambda match: ["cand"],
            "normalize_text": lambda text: list(str(text)),
            "review_candidates_with_llm": self._review,
            "apply_semantic_review": lambda match, *a, **kw: match,
            "alignment_request_cls": lambda **kw: SimpleNamespace(**kw),
            "matching_fingerprint": lambda **kw: "fp",
            "sync_summary": lambda matches: {"matched": len(matches)},
        }
        values.update(overrides)
        return SyncMatchContext(**values)

    def run(self, **overrides: Any) -> SyncMatchResult:
        return run_match_phase(
            self.context(**overrides),
            [SimpleNamespace(id="s", speaker_names=[])],
            [SimpleNamespace(source_id="s")],
            cost_tracker=self.tracker,  # type: ignore[arg-type]
            checkpoint=self.checkpoint,
        )


def test_matches_all_segments_and_persists_final_checkpoint() -> None:
    h = _Harness(["a", "b", "c"])

    result = h.run()

    assert h.matched == ["a", "b", "c"]
    assert [m.segment_id for m in result.matches] == ["a", "b", "c"]
    assert (result.processed, result.total_segments) == (3, 3)
    assert (result.llm_reviewed, result.llm_review_failures, result.segment_failures) == (0, [], [])
    assert h.store.replaced == [["a", "b", "c"]]
    meta = result.matches[0].metadata
    assert meta["normalization_revision"] == 3
    assert meta["matching_fingerprint"] == "fp"
    assert meta["parent_block_uid"] == "a"
    assert h.updates[0][1]["message"] == "Matching scenario quotes"
    assert h.updates[-1][1]["done"] == 3
    # LLM review is off without an API key: the budget is never initialised.
    assert h.tracker.budget_inits == 0


def test_segment_without_uid_and_matcher_error_are_recorded_as_failures() -> None:
    h = _Harness(["", "a", "b"])
    original = h._match_segment

    def flaky(request: Any) -> Any:
        if request.segment_id == "a":
            raise ValueError("boom")
        return original(request)

    h._match_segment = flaky  # type: ignore[method-assign]

    result = h.run()

    assert result.segment_failures == [
        {"segment_id": "", "error": "sync/host block has no stable uid"},
        {"segment_id": "a", "error": "boom"},
    ]
    assert [m.segment_id for m in result.matches] == ["b"]
    assert result.total_segments == 2


def test_cost_budget_exceeded_in_review_disables_review_and_matching_continues(
    caplog: pytest.LogCaptureFixture,
) -> None:
    h = _Harness(["a", "b", "c"], review=True)
    h.match_status = "needs_review"
    h.match_confidence = 0.1
    h.review_error = CostBudgetExceeded("limit hit")

    with caplog.at_level(logging.WARNING, logger="test_sync_match_phase"):
        result = h.run()

    assert h.review_calls == 1  # no paid call after the budget is exhausted
    assert h.matched == ["a", "b", "c"]
    assert [m.segment_id for m in result.matches] == ["a", "b", "c"]
    assert (result.llm_reviewed, result.llm_review_failures) == (0, [])
    assert result.segment_failures == []
    assert any(
        args == ("job-1",) and kwargs == {"llm_budget_error": "limit hit"}
        for args, kwargs in h.updates
    )
    assert "LLM review stopped, job budget exceeded: limit hit" in caplog.text
    assert h.tracker.budget_inits == 1


def test_review_error_is_recorded_and_deterministic_match_kept() -> None:
    h = _Harness(["a"], review=True)
    h.match_status = "needs_review"
    h.match_confidence = 0.1
    h.review_error = RuntimeError("llm down")

    result = h.run()

    assert result.llm_reviewed == 1
    assert result.llm_review_failures == [{"segment_id": "a", "error": "llm down"}]
    assert [m.segment_id for m in result.matches] == ["a"]


def test_review_disabled_when_llm_budget_cannot_be_initialised() -> None:
    h = _Harness(["a"], review=True)
    h.match_status = "needs_review"
    h.match_confidence = 0.1
    h.tracker = _Tracker(budget_ok=False)

    h.run()

    assert h.review_calls == 0


def test_checkpoint_is_noop_before_phase_and_saves_partial_matches_after_start() -> None:
    h = _Harness(["a", "b", "c"])
    h.cancel_after = 2  # cancelled before the third segment

    h.checkpoint.checkpoint()  # not active yet: nothing to write
    assert not h.checkpoint.active
    assert h.store.replaced == []

    with pytest.raises(_Cancelled):
        h.run()

    assert h.checkpoint.active
    assert h.store.replaced == []  # phase itself did not checkpoint (fewer than 10 segments)
    h.checkpoint.checkpoint()  # what the cancel handler of run_sync_job does
    assert h.store.replaced == [["a", "b"]]
    h.checkpoint.checkpoint()  # clean state: no second write
    assert h.store.replaced == [["a", "b"]]


def test_checkpoint_stays_inactive_when_setup_of_phase_fails() -> None:
    h = _Harness(["a"])

    def broken(_path: Any) -> dict[str, Any]:
        raise OSError("no normalization")

    with pytest.raises(OSError):
        h.run(load_normalization=broken)

    assert not h.checkpoint.active
    h.checkpoint.checkpoint()
    assert h.store.replaced == []


def test_periodic_checkpoint_every_ten_segments() -> None:
    h = _Harness([f"s{i}" for i in range(12)])

    h.run()

    assert [len(ids) for ids in h.store.replaced] == [10, 12]


# --- skipping of already matched segments --------------------------------------


def _prior(segment_id: str, *, status: str, fingerprint: str = "fp", **metadata: Any) -> Any:
    item = _match(segment_id, status=status)
    item.metadata = {"normalization_revision": 3, "matching_fingerprint": fingerprint, **metadata}
    return item


def test_unconfirmed_prior_with_same_fingerprint_is_not_rematched() -> None:
    h = _Harness(["a"])
    prior = _prior("a", status="needs_review", fingerprint="fp")
    h.store.items = [prior]

    result = h.run()

    assert h.matched == []
    assert result.matches == [prior]
    assert result.processed == 1


def test_unconfirmed_prior_with_other_fingerprint_is_rematched() -> None:
    h = _Harness(["a"])
    h.store.items = [_prior("a", status="needs_review", fingerprint="old")]

    result = h.run()

    assert h.matched == ["a"]
    assert result.matches[0].metadata["matching_fingerprint"] == "fp"


def test_prior_from_other_normalization_revision_is_rematched() -> None:
    h = _Harness(["a"])
    h.store.items = [_prior("a", status="needs_review", normalization_revision=2)]

    h.run()

    assert h.matched == ["a"]


def test_confirmed_prior_is_kept_unless_rematch_confirmed() -> None:
    h = _Harness(["a"])
    prior = _prior("a", status="confirmed", fingerprint="old")
    h.store.items = [prior]

    result = h.run()

    assert h.matched == []
    assert result.matches == [prior]


def test_rematch_confirmed_forces_rematch_of_older_confirmed_prior() -> None:
    h = _Harness(["a"])
    h.store.items = [_prior("a", status="confirmed", manual_updated_at="2025-01-01T00:00:00+00:00")]

    result = h.run(rematch_confirmed=True)

    assert h.matched == ["a"]
    assert [m.status for m in result.matches] == ["auto_matched"]
    # The manual edit predates the job, so the fresh rematch is what is saved.
    assert [m.status for m in h.store.items] == ["auto_matched"]


def test_rematch_confirmed_replaces_legacy_confirmed_without_manual_timestamp() -> None:
    h = _Harness(["a"])
    h.store.items = [_prior("a", status="confirmed")]

    result = h.run(rematch_confirmed=True)

    assert h.matched == ["a"]
    assert [m.status for m in result.matches] == ["auto_matched"]
    assert [m.status for m in h.store.items] == ["auto_matched"]


def test_rematch_confirmed_keeps_manual_edit_made_during_job() -> None:
    h = _Harness(["a"])
    h.store.items = [_prior("a", status="confirmed")]
    original = h._match_segment
    manual = _prior("a", status="confirmed", manual_updated_at="2026-01-01T00:00:05+00:00")

    def match_and_edit(request: Any) -> Any:
        fresh = original(request)
        h.store.items = [manual]  # user confirms via PATCH while the job runs
        return fresh

    h._match_segment = match_and_edit  # type: ignore[method-assign]

    result = h.run(rematch_confirmed=True)

    assert h.store.items == [manual]
    assert result.matches == [manual]


def test_cost_budget_exceeded_outside_review_propagates_from_phase() -> None:
    h = _Harness(["a", "b"])

    def exhausted(request: Any) -> Any:
        raise CostBudgetExceeded("job limit hit")

    h._match_segment = exhausted  # type: ignore[method-assign]

    with pytest.raises(CostBudgetExceeded, match="job limit hit"):
        h.run()

    assert h.checkpoint.active  # handlers of run_sync_job can still save partial matches


# --- merge in the checkpoint writer --------------------------------------------

_STARTED = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
_BEFORE = "2026-01-01T11:00:00+00:00"
_AFTER = "2026-01-01T13:00:00Z"


def _writer(
    store: _Store,
    existing: dict[str, Any],
    *,
    rematched: set[str] | None = None,
    dirty: bool = True,
    scenario_revision_id: str = "",
    started: datetime | None = _STARTED,
    initial: list[Any] | None = None,
) -> tuple[Any, dict[str, bool]]:
    state = {"dirty": dirty}
    ctx: Any = SimpleNamespace(match_store=store, data_lock=threading.Lock())
    write = _make_checkpoint_writer(
        ctx,
        existing=existing,
        initial_signatures={
            item.segment_id: _match_signature(item)
            for item in (store.items if initial is None else initial)
        },
        checkpoint_state=state,
        rematched_this_run=rematched if rematched is not None else set(),
        matching_started_at_dt=started,
        normalization_revision=3,
        scenario_revision_id=scenario_revision_id,
    )
    return write, state


def _stored(segment_id: str, *, status: str = "needs_review", **metadata: Any) -> Any:
    item = _match(segment_id, status=status)
    item.metadata = {"normalization_revision": 3, **metadata}
    return item


def _fresh(segment_id: str) -> Any:
    item = _match(segment_id)
    item.metadata = {"normalization_revision": 3}
    return item


def test_checkpoint_writer_skips_write_when_not_dirty() -> None:
    store = _Store([_stored("a")])
    write, state = _writer(store, {"a": _fresh("a")}, dirty=False)

    write()

    assert store.replaced == []
    assert state["dirty"] is False


def test_checkpoint_writer_clears_dirty_flag_after_write() -> None:
    store = _Store()
    write, state = _writer(store, {"a": _fresh("a")})

    write()
    write()

    assert store.replaced == [["a"]]
    assert state["dirty"] is False


def test_checkpoint_writer_keeps_manual_edit_made_during_run() -> None:
    manual = _stored("a", status="confirmed", manual_updated_at=_AFTER)
    store = _Store([manual])
    fresh = _fresh("a")
    existing = {"a": fresh}
    write, _ = _writer(store, existing, rematched={"a"})

    write()

    assert store.items == [manual]
    assert existing["a"] is manual


def test_checkpoint_writer_overwrites_manual_edit_older_than_job_when_rematched() -> None:
    old_edit = _stored("a", status="confirmed", manual_updated_at=_BEFORE)
    store = _Store([old_edit])
    fresh = _fresh("a")
    write, _ = _writer(store, {"a": fresh}, rematched={"a"})

    write()

    assert store.items == [fresh]


def test_checkpoint_writer_keeps_persisted_segment_not_rematched_this_run() -> None:
    old_edit = _stored("a", status="confirmed", manual_updated_at=_BEFORE)
    store = _Store([old_edit])
    fresh_b = _fresh("b")
    write, _ = _writer(store, {"b": fresh_b}, rematched={"b"})

    write()

    assert sorted(item.segment_id for item in store.items) == ["a", "b"]
    assert next(item for item in store.items if item.segment_id == "a") is old_edit


def test_checkpoint_writer_unparseable_manual_timestamp_is_authoritative() -> None:
    manual = _stored("a", status="confirmed", manual_updated_at="not-a-date")
    store = _Store([manual])
    write, _ = _writer(store, {"a": _fresh("a")}, rematched={"a"})

    write()

    assert store.items == [manual]


def test_checkpoint_writer_unknown_job_start_makes_manual_edit_authoritative() -> None:
    manual = _stored("a", status="confirmed", manual_updated_at=_BEFORE)
    store = _Store([manual])
    write, _ = _writer(store, {"a": _fresh("a")}, rematched={"a"}, started=None)

    write()

    assert store.items == [manual]


def test_checkpoint_writer_legacy_confirmed_without_timestamp_yields_to_forced_rematch() -> None:
    legacy = _stored("a", status="confirmed")
    store = _Store([legacy])
    fresh = _fresh("a")
    existing = {"a": fresh}
    write, _ = _writer(store, existing, rematched={"a"})

    write()

    assert store.items == [fresh]
    assert existing["a"] is fresh


def test_checkpoint_writer_unstamped_confirmation_added_during_job_is_authoritative() -> None:
    raced = _stored("a", status="confirmed", confirmed_by="editor-during-job")
    store = _Store([raced])
    write, _ = _writer(store, {"a": _fresh("a")}, rematched={"a"}, initial=[])

    write()

    assert store.items == [raced]


def test_checkpoint_writer_legacy_confirmed_without_timestamp_kept_when_not_rematched() -> None:
    legacy = _stored("a", status="confirmed")
    store = _Store([legacy])
    write, _ = _writer(store, {"b": _fresh("b")}, rematched={"b"})

    write()

    assert next(item for item in store.items if item.segment_id == "a") is legacy


def test_checkpoint_writer_unconfirmed_rematched_persisted_loses_to_fresh_match() -> None:
    persisted = _stored("a", status="needs_review")
    store = _Store([persisted])
    fresh = _fresh("a")
    write, _ = _writer(store, {"a": fresh}, rematched={"a"})

    write()

    assert store.items == [fresh]


def test_checkpoint_writer_stale_revision_does_not_override_existing() -> None:
    stale = _stored("a", status="confirmed", manual_updated_at=_AFTER, normalization_revision=2)
    store = _Store([stale])
    fresh = _fresh("a")
    write, _ = _writer(store, {"a": fresh}, rematched={"a"})

    write()

    assert store.items == [fresh]


def test_checkpoint_writer_stale_revision_is_still_kept_when_nothing_replaces_it() -> None:
    stale = _stored("a", status="confirmed", normalization_revision=2)
    store = _Store([stale])
    write, _ = _writer(store, {"b": _fresh("b")}, rematched={"b"})

    write()

    assert sorted(item.segment_id for item in store.items) == ["a", "b"]


def test_checkpoint_writer_other_scenario_revision_is_not_current() -> None:
    other = _stored("a", status="confirmed", manual_updated_at=_AFTER, scenario_revision_id="scn-1")
    store = _Store([other])
    fresh = _fresh("a")
    write, _ = _writer(store, {"a": fresh}, rematched={"a"}, scenario_revision_id="scn-2")

    write()

    assert store.items == [fresh]


def test_checkpoint_writer_legacy_persisted_without_scenario_id_is_current() -> None:
    manual = _stored("a", status="confirmed", manual_updated_at=_AFTER)
    store = _Store([manual])
    write, _ = _writer(store, {"a": _fresh("a")}, rematched={"a"}, scenario_revision_id="scn-2")

    write()

    assert store.items == [manual]
