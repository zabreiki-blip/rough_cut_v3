"""Pure job-local cloud ASR budget accounting."""

from decimal import Decimal

import pytest

from cost_budget import CostBudget, CostBudgetExceeded, CostGuardConfig


def test_budget_config_defaults_to_disabled() -> None:
    config = CostGuardConfig.from_environ({})
    assert config.max_usd_per_job == Decimal("0")
    assert config.cloud_asr_usd_per_minute == Decimal("0")


def test_budget_config_reads_decimal_values_without_float_rounding() -> None:
    config = CostGuardConfig.from_environ({
        "ROUGH_CUT_MAX_USD_PER_JOB": " 0.30 ",
        "ROUGH_CUT_CLOUD_ASR_USD_PER_MINUTE_ESTIMATE": "0.10",
    })
    budget = CostBudget(config.max_usd_per_job, config.cloud_asr_usd_per_minute)
    for _ in range(3):
        budget.reserve_attempt(60)
    assert budget.snapshot()["reserved_usd"] == 0.3
    with pytest.raises(CostBudgetExceeded):
        budget.reserve_attempt(0.01)


@pytest.mark.parametrize("name", [
    "ROUGH_CUT_MAX_USD_PER_JOB", "ROUGH_CUT_CLOUD_ASR_USD_PER_MINUTE_ESTIMATE",
])
@pytest.mark.parametrize("value", ["-1", "NaN", "Infinity", "-Infinity", "invalid"])
def test_budget_config_rejects_invalid_numbers(name: str, value: str) -> None:
    with pytest.raises(ValueError, match=name):
        CostGuardConfig.from_environ({name: value})


def test_budget_config_treats_blank_values_as_disabled() -> None:
    config = CostGuardConfig.from_environ({
        "ROUGH_CUT_MAX_USD_PER_JOB": " ",
        "ROUGH_CUT_CLOUD_ASR_USD_PER_MINUTE_ESTIMATE": "",
    })
    assert config.max_usd_per_job == config.cloud_asr_usd_per_minute == Decimal("0")


def test_budget_config_requires_rate_when_limit_is_enabled() -> None:
    with pytest.raises(ValueError, match="ROUGH_CUT_CLOUD_ASR_USD_PER_MINUTE_ESTIMATE"):
        CostGuardConfig.from_environ({"ROUGH_CUT_MAX_USD_PER_JOB": "1"})


def test_budget_disabled_still_tracks_estimates_and_actuals() -> None:
    budget = CostBudget(Decimal("0"), Decimal("0.10"))
    reservation = budget.reserve_attempt(6000)
    budget.settle(reservation, "12.50")
    assert budget.snapshot() == {
        "limit_usd": 0.0, "spent_usd": 12.5, "reserved_usd": 0.0, "total_usd": 12.5,
    }


def test_budget_allows_disabled_zero_rate() -> None:
    budget = CostBudget(Decimal("0"), Decimal("0"))
    budget.reserve_attempt(6000)
    assert budget.estimate_seconds(6000) == Decimal("0")


def test_budget_estimate_uses_decimal_arithmetic() -> None:
    budget = CostBudget(Decimal("1"), Decimal("0.1"))
    assert budget.estimate_seconds(0.3) == Decimal("0.0005")


def test_budget_rejects_crossing_limit_before_mutating_reservations() -> None:
    budget = CostBudget(Decimal("0.3"), Decimal("0.1"), Decimal("0.1"))
    budget.reserve_attempt(120)
    before = budget.snapshot()
    with pytest.raises(CostBudgetExceeded):
        budget.reserve_attempt(1)
    assert budget.snapshot() == before
    assert before["total_usd"] == 0.3


def test_budget_settlement_replaces_estimate_and_releases_headroom() -> None:
    budget = CostBudget(Decimal("0.3"), Decimal("0.1"), Decimal("0.05"))
    reservation = budget.reserve_attempt(120)
    budget.settle(reservation, 0.1)
    budget.reserve_attempt(90)
    assert budget.snapshot() == {
        "limit_usd": 0.3, "spent_usd": 0.15, "reserved_usd": 0.15, "total_usd": 0.3,
    }


def test_budget_unknown_cost_keeps_estimate_reserved() -> None:
    budget = CostBudget(Decimal("0.1"), Decimal("0.1"))
    reservation = budget.reserve_attempt(60)
    budget.settle(reservation, None)
    assert budget.snapshot() == {
        "limit_usd": 0.1, "spent_usd": 0.0, "reserved_usd": 0.1, "total_usd": 0.1,
    }
    with pytest.raises(CostBudgetExceeded):
        budget.reserve_attempt(1)
    budget.settle(reservation, "0.02")
    assert budget.snapshot()["reserved_usd"] == 0.0


def test_budget_records_provider_overrun_and_blocks_later_attempt() -> None:
    budget = CostBudget(Decimal("0.1"), Decimal("0.1"))
    reservation = budget.reserve_attempt(60)
    budget.settle(reservation, "0.2")
    assert budget.snapshot()["spent_usd"] == 0.2
    with pytest.raises(CostBudgetExceeded):
        budget.reserve_attempt(1)


def test_budget_reservations_are_distinct_and_job_local() -> None:
    budget = CostBudget(Decimal("1"), Decimal("0.1"))
    other = CostBudget(Decimal("1"), Decimal("0.1"))
    first = budget.reserve_attempt(60)
    second = budget.reserve_attempt(60)
    assert isinstance(first, str) and first
    assert isinstance(second, str) and second != first
    with pytest.raises(KeyError):
        other.settle(first, "0.1")
    budget.settle(first, "0.05")
    with pytest.raises(KeyError):
        budget.settle(first, "0.05")
    assert budget.snapshot()["reserved_usd"] == 0.1
    assert other.snapshot()["total_usd"] == 0.0


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1, "invalid"])
@pytest.mark.parametrize("field", ["limit_usd", "usd_per_minute", "initial_spent_usd"])
def test_budget_rejects_invalid_constructor_values(field: str, value: object) -> None:
    kwargs = {"limit_usd": Decimal("1"), "usd_per_minute": Decimal("0.1")}
    kwargs[field] = value
    with pytest.raises(ValueError, match=field):
        CostBudget(**kwargs)


def test_budget_requires_rate_when_limit_is_enabled() -> None:
    with pytest.raises(ValueError, match="usd_per_minute"):
        CostBudget(Decimal("1"), Decimal("0"))


@pytest.mark.parametrize("seconds", [float("nan"), float("inf"), -1])
def test_budget_rejects_invalid_durations(seconds: float) -> None:
    budget = CostBudget(Decimal("1"), Decimal("0.1"))
    with pytest.raises(ValueError, match="seconds"):
        budget.estimate_seconds(seconds)
    with pytest.raises(ValueError, match="seconds"):
        budget.reserve_attempt(seconds)
    assert budget.snapshot()["total_usd"] == 0.0


@pytest.mark.parametrize("actual", [float("nan"), float("inf"), -1, "invalid", object()])
def test_budget_rejects_invalid_provider_cost_without_mutation(actual: object) -> None:
    budget = CostBudget(Decimal("1"), Decimal("0.1"))
    reservation = budget.reserve_attempt(60)
    before = budget.snapshot()
    with pytest.raises(ValueError, match="actual_usd"):
        budget.settle(reservation, actual)
    assert budget.snapshot() == before
    budget.settle(reservation, 0)
    assert budget.snapshot()["total_usd"] == 0.0


def test_zero_dollar_reservations_remain_pending_until_actual_zero_is_known():
    budget = CostBudget(Decimal('0'), Decimal('0'))
    assert budget.has_pending_reservations is False
    reservation = budget.reserve_attempt(60)
    assert budget.snapshot()['reserved_usd'] == 0
    assert budget.has_pending_reservations is True
    budget.settle(reservation, None)
    assert budget.has_pending_reservations is True
    budget.settle(reservation, 0)
    assert budget.snapshot()['spent_usd'] == 0
    assert budget.has_pending_reservations is False
