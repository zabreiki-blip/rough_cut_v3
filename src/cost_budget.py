"""Decimal-backed cost reservations local to one sequential ASR job."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from uuid import uuid4


class CostBudgetExceeded(RuntimeError):
    """The next estimated provider attempt would exceed the job limit."""


def _nonnegative_decimal(value: object, name: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{name} must be a finite non-negative decimal") from exc
    if not number.is_finite() or number < 0:
        raise ValueError(f"{name} must be a finite non-negative decimal")
    return number


@dataclass(frozen=True)
class CostGuardConfig:
    max_usd_per_job: Decimal
    cloud_asr_usd_per_minute: Decimal

    @classmethod
    def from_environ(cls, environ: Mapping[str, str]) -> CostGuardConfig:
        limit_name = "ROUGH_CUT_MAX_USD_PER_JOB"
        rate_name = "ROUGH_CUT_CLOUD_ASR_USD_PER_MINUTE_ESTIMATE"
        limit = _nonnegative_decimal(environ.get(limit_name, "").strip() or "0", limit_name)
        rate = _nonnegative_decimal(environ.get(rate_name, "").strip() or "0", rate_name)
        if limit > 0 and rate == 0:
            raise ValueError(f"{rate_name} must be positive when {limit_name} is positive")
        return cls(max_usd_per_job=limit, cloud_asr_usd_per_minute=rate)


class CostBudget:
    """Reserve before dispatch, then replace estimates with known actuals.

    A zero limit disables enforcement while retaining accounting. Unknown
    provider costs remain reserved, so subsequent attempts cannot reuse that
    headroom. Instances belong to a single job and require sequential access.
    """

    def __init__(
        self,
        limit_usd: Decimal,
        usd_per_minute: Decimal,
        initial_spent_usd: Decimal = Decimal("0"),
    ) -> None:
        self.limit_usd = _nonnegative_decimal(limit_usd, "limit_usd")
        self.usd_per_minute = _nonnegative_decimal(usd_per_minute, "usd_per_minute")
        self._spent_usd = _nonnegative_decimal(initial_spent_usd, "initial_spent_usd")
        if self.limit_usd > 0 and self.usd_per_minute == 0:
            raise ValueError("usd_per_minute must be positive when limit_usd is positive")
        self._reservations: dict[str, Decimal] = {}

    def estimate_seconds(self, seconds: float) -> Decimal:
        duration = _nonnegative_decimal(seconds, "seconds")
        return duration * self.usd_per_minute / Decimal("60")

    def _reserved_usd(self) -> Decimal:
        return sum(self._reservations.values(), Decimal("0"))

    @property
    def has_pending_reservations(self) -> bool:
        """Unknown provider cost stays pending even when its estimate is zero."""
        return bool(self._reservations)

    def reserve_attempt(self, seconds: float) -> str:
        estimate = self.estimate_seconds(seconds)
        projected_total = self._spent_usd + self._reserved_usd() + estimate
        if self.limit_usd > 0 and projected_total > self.limit_usd:
            raise CostBudgetExceeded(
                f"Cloud ASR attempt would exceed the job cost limit of ${self.limit_usd}"
            )
        reservation_id = uuid4().hex
        self._reservations[reservation_id] = estimate
        return reservation_id

    def settle(self, reservation_id: str, actual_usd: object) -> None:
        # Validate the ID even when provider usage is absent.
        self._reservations[reservation_id]
        if actual_usd is None:
            return
        actual = _nonnegative_decimal(actual_usd, "actual_usd")
        del self._reservations[reservation_id]
        self._spent_usd += actual

    def snapshot(self) -> dict[str, float]:
        reserved = self._reserved_usd()
        return {
            "limit_usd": float(self.limit_usd),
            "spent_usd": float(self._spent_usd),
            "reserved_usd": float(reserved),
            "total_usd": float(self._spent_usd + reserved),
        }
