from __future__ import annotations

import logging
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from sync_cost_tracker import SyncCostTracker, _AsrMetrics

_LLM_ENV = {
    "ROUGH_CUT_MAX_USD_PER_JOB": "10",
    "ROUGH_CUT_LLM_USD_PER_1K_TOKENS_ESTIMATE": "1",
}


def _make(persist: bool = True) -> tuple[SyncCostTracker, list[tuple[tuple, dict]], _AsrMetrics]:
    calls: list[tuple[tuple, dict]] = []
    metrics: _AsrMetrics = {
        "asr_backend_used": "",
        "asr_cost_usd_total": 0.0,
        "asr_timestamp_precision": "",
        "transcription_wall_sec_total": 0.0,
    }

    def update(*args: Any, **kwargs: Any) -> None:
        calls.append((args, kwargs))

    tracker = SyncCostTracker(
        job_id="job-1",
        update_job=update,
        should_persist=lambda: persist,
        asr_metrics=metrics,
    )
    return tracker, calls, metrics


def test_sums_asr_across_sources_and_updates_job() -> None:
    tracker, calls, metrics = _make()
    tracker.account_cloud_source("a", Decimal("0"), {"cost_usd": "1.5", "reserved_cost_usd": "0"})
    tracker.account_cloud_source(
        "b", Decimal("0"), {"cost_usd": "0.5", "reserved_cost_usd": "2", "cost_pending": True}
    )
    assert tracker.cloud_totals() == (Decimal("2.0"), Decimal("2"), True)
    assert tracker.cloud_exposure() == Decimal("4.0")
    assert metrics["asr_cost_usd_total"] == 2.0
    args, kwargs = calls[-1]
    assert args == ("job-1",)
    assert kwargs["persist"] is True
    assert kwargs["cost_usd"] == 2.0
    assert kwargs["reserved_cost_usd"] == 2.0
    assert kwargs["cost_pending"] is True
    assert kwargs["asr_cost_usd_total"] == 2.0


def test_repeated_source_replaces_instead_of_adding() -> None:
    tracker, calls, _ = _make()
    tracker.account_cloud_source("a", Decimal("0"), {"cost_usd": "1", "reserved_cost_usd": "3"})
    tracker.account_cloud_source("a", Decimal("0"), {"cost_usd": "2", "reserved_cost_usd": "0"})
    assert tracker.cloud_totals() == (Decimal("2"), Decimal("0"), False)
    assert len(calls) == 2
    assert tracker.source_cost("a") == {
        "known": Decimal("2"),
        "reserved": Decimal("0"),
        "pending": False,
    }
    assert tracker.source_cost("missing") is None


def test_snapshot_payload_subtracts_initial_exposure_and_reads_attrs() -> None:
    tracker, _, _ = _make()
    snapshot = {"spent_usd": "5", "reserved_usd": "1"}
    tracker.account_cloud_source("a", Decimal("4"), snapshot, snapshot=True)
    assert tracker.cloud_totals() == (Decimal("1"), Decimal("1"), True)
    # Spent below the initial exposure never goes negative; attribute payloads work.
    exc = SimpleNamespace(cost_usd="1", reserved_cost_usd=None, cost_pending=False)
    tracker.account_cloud_source("a", Decimal("4"), exc)
    assert tracker.cloud_totals() == (Decimal("0"), Decimal("1"), False)


def test_persist_flag_is_passed_through() -> None:
    tracker, calls, _ = _make(persist=False)
    tracker.account_cloud_source("a", Decimal("0"), {"cost_usd": "1"})
    assert calls[0][1]["persist"] is False


def test_llm_cost_fields_empty_without_budget() -> None:
    tracker, _, _ = _make()
    assert tracker.llm_budget is None
    assert tracker.llm_cost_fields() == {}


def test_llm_budget_initial_is_asr_known_plus_reserved() -> None:
    tracker, calls, _ = _make()
    tracker.account_cloud_source("a", Decimal("0"), {"cost_usd": "1", "reserved_cost_usd": "2"})
    n_calls = len(calls)
    assert tracker.init_llm_budget(_LLM_ENV) is True
    budget = tracker.llm_budget
    assert budget is not None
    assert budget.snapshot()["spent_usd"] == 3.0
    assert len(calls) == n_calls

    fields = tracker.llm_cost_fields()
    assert fields["llm_cost_usd"] == 0.0
    assert fields["cost_usd"] == 1.0
    assert fields["reserved_cost_usd"] == 2.0
    assert fields["cost_pending"] is True
    assert fields["llm_estimated_cost_usd"] == budget.snapshot()["estimated_spent_usd"]


def test_llm_cost_fields_reflect_llm_spend_and_reservation() -> None:
    tracker, _, _ = _make()
    tracker.account_cloud_source("a", Decimal("0"), {"cost_usd": "1", "reserved_cost_usd": "0"})
    assert tracker.init_llm_budget(_LLM_ENV) is True
    budget = tracker.llm_budget
    assert budget is not None
    budget.reserve_llm_attempt([{"role": "user", "content": "x" * 4000}], 0)  # ~1000 tokens = $1
    fields = tracker.llm_cost_fields()
    assert fields["reserved_cost_usd"] == 1.0
    assert fields["cost_pending"] is True
    assert fields["llm_cost_usd"] == 0.0
    assert fields["cost_usd"] == 1.0


def test_invalid_env_leaves_no_budget_and_reports_error(caplog) -> None:
    tracker, calls, _ = _make()
    bad = {"ROUGH_CUT_MAX_USD_PER_JOB": "not-a-number"}
    with caplog.at_level(logging.WARNING, logger="sync_cost_tracker"):
        assert tracker.init_llm_budget(bad) is False
    assert tracker.llm_budget is None
    assert tracker.llm_cost_fields() == {}
    args, kwargs = calls[-1]
    assert args == ("job-1",)
    assert set(kwargs) == {"llm_budget_error"}
    assert "ROUGH_CUT_MAX_USD_PER_JOB" in kwargs["llm_budget_error"]
    assert "LLM review disabled" in caplog.text
