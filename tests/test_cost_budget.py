"""Pure job-local cloud ASR budget accounting."""

from decimal import Decimal

import pytest

from cost_budget import CostBudget, CostBudgetExceeded, CostGuardConfig


def test_budget_config_defaults_to_disabled() -> None:
    config = CostGuardConfig.from_environ({})
    assert config.max_usd_per_job == Decimal("0")
    assert config.cloud_asr_usd_per_minute == Decimal("0")


def test_budget_config_reads_decimal_values_without_float_rounding() -> None:
    config = CostGuardConfig.from_environ(
        {
            "ROUGH_CUT_MAX_USD_PER_JOB": " 0.30 ",
            "ROUGH_CUT_CLOUD_ASR_USD_PER_MINUTE_ESTIMATE": "0.10",
        }
    )
    budget = CostBudget(config.max_usd_per_job, config.cloud_asr_usd_per_minute)
    for _ in range(3):
        budget.reserve_attempt(60)
    assert budget.snapshot()["reserved_usd"] == 0.3
    with pytest.raises(CostBudgetExceeded):
        budget.reserve_attempt(0.01)


@pytest.mark.parametrize(
    "name",
    [
        "ROUGH_CUT_MAX_USD_PER_JOB",
        "ROUGH_CUT_CLOUD_ASR_USD_PER_MINUTE_ESTIMATE",
    ],
)
@pytest.mark.parametrize("value", ["-1", "NaN", "Infinity", "-Infinity", "invalid"])
def test_budget_config_rejects_invalid_numbers(name: str, value: str) -> None:
    with pytest.raises(ValueError, match=name):
        CostGuardConfig.from_environ({name: value})


def test_budget_config_treats_blank_values_as_disabled() -> None:
    config = CostGuardConfig.from_environ(
        {
            "ROUGH_CUT_MAX_USD_PER_JOB": " ",
            "ROUGH_CUT_CLOUD_ASR_USD_PER_MINUTE_ESTIMATE": "",
        }
    )
    assert config.max_usd_per_job == config.cloud_asr_usd_per_minute == Decimal("0")


def test_budget_config_requires_rate_when_limit_is_enabled() -> None:
    with pytest.raises(ValueError, match="ROUGH_CUT_CLOUD_ASR_USD_PER_MINUTE_ESTIMATE"):
        CostGuardConfig.from_environ({"ROUGH_CUT_MAX_USD_PER_JOB": "1"})


def test_budget_disabled_still_tracks_estimates_and_actuals() -> None:
    budget = CostBudget(Decimal("0"), Decimal("0.10"))
    reservation = budget.reserve_attempt(6000)
    budget.settle(reservation, "12.50")
    assert budget.snapshot() == {
        "limit_usd": 0.0,
        "spent_usd": 12.5,
        "reserved_usd": 0.0,
        "total_usd": 12.5,
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
        "limit_usd": 0.3,
        "spent_usd": 0.15,
        "reserved_usd": 0.15,
        "total_usd": 0.3,
    }


def test_budget_unknown_cost_keeps_estimate_reserved() -> None:
    budget = CostBudget(Decimal("0.1"), Decimal("0.1"))
    reservation = budget.reserve_attempt(60)
    budget.settle(reservation, None)
    assert budget.snapshot() == {
        "limit_usd": 0.1,
        "spent_usd": 0.0,
        "reserved_usd": 0.1,
        "total_usd": 0.1,
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
    budget = CostBudget(Decimal("0"), Decimal("0"))
    assert budget.has_pending_reservations is False
    reservation = budget.reserve_attempt(60)
    assert budget.snapshot()["reserved_usd"] == 0
    assert budget.has_pending_reservations is True
    budget.settle(reservation, None)
    assert budget.has_pending_reservations is True
    budget.settle(reservation, 0)
    assert budget.snapshot()["spent_usd"] == 0
    assert budget.has_pending_reservations is False


# --- LLM/TTS reservations and thread safety -------------------------------------


def test_budget_config_reads_llm_and_tts_rates() -> None:
    config = CostGuardConfig.from_environ(
        {
            "ROUGH_CUT_LLM_USD_PER_1K_TOKENS_ESTIMATE": "0.002",
            "ROUGH_CUT_TTS_USD_PER_1K_CHARS_ESTIMATE": "0.015",
        }
    )
    assert config.llm_usd_per_1k_tokens == Decimal("0.002")
    assert config.tts_usd_per_1k_chars == Decimal("0.015")
    default = CostGuardConfig.from_environ({})
    assert default.llm_usd_per_1k_tokens == Decimal("0")
    assert default.tts_usd_per_1k_chars == Decimal("0")


@pytest.mark.parametrize(
    "name",
    [
        "ROUGH_CUT_LLM_USD_PER_1K_TOKENS_ESTIMATE",
        "ROUGH_CUT_TTS_USD_PER_1K_CHARS_ESTIMATE",
    ],
)
@pytest.mark.parametrize("value", ["-1", "nan", "inf", "abc"])
def test_budget_config_rejects_invalid_llm_tts_rates(name: str, value: str) -> None:
    with pytest.raises(ValueError, match=name):
        CostGuardConfig.from_environ({name: value})


def test_llm_estimate_is_prompt_tokens_plus_max_tokens() -> None:
    budget = CostBudget(Decimal("0"), Decimal("0"), llm_usd_per_1k_tokens=Decimal("2"))
    # ceil(10 / 4) = 3 prompt tokens, 997 completion tokens -> 1000 tokens
    assert budget.estimate_llm(10, 997) == Decimal("2")
    assert budget.estimate_llm(0, 500) == Decimal("1")


def test_tts_estimate_is_chars_over_thousand() -> None:
    budget = CostBudget(Decimal("0"), Decimal("0"), tts_usd_per_1k_chars=Decimal("3"))
    assert budget.estimate_tts(500) == Decimal("1.5")


def test_zero_rates_estimate_zero_and_always_reserve() -> None:
    budget = CostBudget(Decimal("0"), Decimal("0"))
    assert budget.estimate_llm(10_000, 10_000) == Decimal("0")
    assert budget.estimate_tts(10_000) == Decimal("0")
    budget.reserve_llm_attempt([{"role": "user", "content": "x" * 100}], 1000)
    budget.reserve_tts_attempt("text")
    assert budget.snapshot()["reserved_usd"] == 0.0
    assert budget.has_pending_reservations


def test_llm_reservation_counts_message_text_of_all_messages() -> None:
    budget = CostBudget(Decimal("0"), Decimal("0"), llm_usd_per_1k_tokens=Decimal("1"))
    messages = [
        {"role": "system", "content": "a" * 40},
        {"role": "user", "content": [{"type": "text", "text": "b" * 40}, {"type": "image"}]},
    ]
    budget.reserve_llm_attempt(messages, 980)
    # ceil(80 / 4) = 20 prompt tokens + 980 = 1000 tokens
    assert budget.snapshot()["reserved_usd"] == 1.0


def test_llm_reserve_over_limit_raises_and_reserves_nothing() -> None:
    budget = CostBudget(Decimal("1"), Decimal("0.1"), llm_usd_per_1k_tokens=Decimal("1"))
    with pytest.raises(CostBudgetExceeded, match=r"\$1"):
        budget.reserve_llm_attempt([{"role": "user", "content": ""}], 1001)
    assert budget.snapshot()["reserved_usd"] == 0.0
    assert not budget.has_pending_reservations


def test_tts_reserve_over_limit_raises() -> None:
    budget = CostBudget(Decimal("1"), Decimal("0.1"), tts_usd_per_1k_chars=Decimal("1"))
    budget.reserve_tts_attempt("a" * 600)
    with pytest.raises(CostBudgetExceeded):
        budget.reserve_tts_attempt("a" * 600)


def test_settle_unknown_cost_falls_back_to_estimate_when_requested() -> None:
    budget = CostBudget(Decimal("5"), Decimal("0.1"), tts_usd_per_1k_chars=Decimal("2"))
    reservation = budget.reserve_tts_attempt("a" * 500)
    budget.settle(reservation, None, estimate_if_unknown=True)
    assert budget.snapshot()["spent_usd"] == 1.0
    assert budget.snapshot()["reserved_usd"] == 0.0
    assert not budget.has_pending_reservations


def test_settle_actual_cost_replaces_estimate_even_with_fallback_flag() -> None:
    budget = CostBudget(Decimal("5"), Decimal("0.1"), tts_usd_per_1k_chars=Decimal("2"))
    reservation = budget.reserve_tts_attempt("a" * 500)
    budget.settle(reservation, "0.25", estimate_if_unknown=True)
    assert budget.snapshot()["spent_usd"] == 0.25


def test_settle_unknown_cost_without_flag_keeps_reservation() -> None:
    budget = CostBudget(Decimal("5"), Decimal("0.1"), tts_usd_per_1k_chars=Decimal("2"))
    reservation = budget.reserve_tts_attempt("a" * 500)
    budget.settle(reservation, None)
    assert budget.snapshot()["reserved_usd"] == 1.0


def test_from_config_carries_all_rates_and_initial_spend() -> None:
    config = CostGuardConfig.from_environ(
        {
            "ROUGH_CUT_MAX_USD_PER_JOB": "10",
            "ROUGH_CUT_CLOUD_ASR_USD_PER_MINUTE_ESTIMATE": "0.1",
            "ROUGH_CUT_LLM_USD_PER_1K_TOKENS_ESTIMATE": "1",
            "ROUGH_CUT_TTS_USD_PER_1K_CHARS_ESTIMATE": "2",
        }
    )
    budget = CostBudget.from_config(config, initial_spent_usd=Decimal("3"))
    assert budget.limit_usd == Decimal("10")
    assert budget.estimate_seconds(60) == Decimal("0.1")
    assert budget.estimate_llm(0, 1000) == Decimal("1")
    assert budget.estimate_tts(1000) == Decimal("2")
    assert budget.snapshot()["spent_usd"] == 3.0


def test_concurrent_reservations_are_consistent() -> None:
    import threading

    threads_count = 32
    per_thread = 50
    budget = CostBudget(Decimal("0"), Decimal("0"), tts_usd_per_1k_chars=Decimal("1"))
    barrier = threading.Barrier(threads_count)
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            barrier.wait()
            for _ in range(per_thread):
                reservation = budget.reserve_tts_attempt("a" * 1000)
                budget.snapshot()
                budget.settle(reservation, "1")
        except BaseException as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(threads_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    snapshot = budget.snapshot()
    assert snapshot["spent_usd"] == float(threads_count * per_thread)
    assert snapshot["reserved_usd"] == 0.0
    assert not budget.has_pending_reservations


def test_concurrent_reservations_never_exceed_the_limit() -> None:
    import threading

    budget = CostBudget(Decimal("10"), Decimal("0.1"), tts_usd_per_1k_chars=Decimal("1"))
    barrier = threading.Barrier(40)
    granted: list[str] = []
    denied: list[int] = []
    guard = threading.Lock()

    def worker() -> None:
        barrier.wait()
        try:
            reservation = budget.reserve_tts_attempt("a" * 1000)
        except CostBudgetExceeded:
            with guard:
                denied.append(1)
        else:
            with guard:
                granted.append(reservation)

    threads = [threading.Thread(target=worker) for _ in range(40)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(granted) == 10
    assert len(denied) == 30
    assert budget.snapshot()["reserved_usd"] == 10.0
