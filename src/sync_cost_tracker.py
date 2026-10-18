from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from decimal import Decimal
from typing import Any, TypedDict

from cost_budget import CostBudget

_module_logger = logging.getLogger(__name__)


class _AsrMetrics(TypedDict):
    """Per-key typed job-status fields for ASR usage metrics.

    A plain ``dict[str, object]`` forces every value to the ``object``
    type, which breaks ``+=`` on the numeric fields under mypy. A
    ``TypedDict`` keeps each key's own declared type (``str``/``float``)
    without changing the dict's shape, so it still unpacks into
    ``_update_sync_job(**asr_metrics)`` exactly as before.
    """

    asr_backend_used: str
    asr_cost_usd_total: float
    asr_timestamp_precision: str
    transcription_wall_sec_total: float


class SyncCostTracker:
    """Cost accounting of one sync job: cloud ASR per source plus the LLM budget.

    Not thread-safe by design, like the closures it replaces: every caller
    (ASR budget callbacks, the matching loop) runs synchronously in the job
    thread, so no lock is taken. ``asr_metrics`` is shared with the caller,
    which also mutates the non-cost keys; the tracker only writes
    ``asr_cost_usd_total``.
    """

    def __init__(
        self,
        *,
        job_id: str,
        update_job: Callable[..., Any],
        should_persist: Callable[[], bool],
        asr_metrics: _AsrMetrics,
        logger: logging.Logger = _module_logger,
    ) -> None:
        self._job_id = job_id
        self._update_job = update_job
        self._should_persist = should_persist
        self._asr_metrics = asr_metrics
        self._logger = logger
        self._cloud_costs: dict[str, dict[str, Any]] = {}
        # The semantic-review budget starts from the cloud ASR exposure of this
        # job, so a single job limit covers ASR and LLM spend together.
        self._llm_budget: CostBudget | None = None
        self._llm_initial = Decimal("0")

    @property
    def llm_budget(self) -> CostBudget | None:
        return self._llm_budget

    def cloud_totals(self) -> tuple[Decimal, Decimal, bool]:
        costs = self._cloud_costs
        return (
            sum((item["known"] for item in costs.values()), Decimal("0")),
            sum((item["reserved"] for item in costs.values()), Decimal("0")),
            any(item["pending"] for item in costs.values()),
        )

    def cloud_exposure(self) -> Decimal:
        """Confirmed plus reserved cloud ASR spend of the whole job so far."""
        known_total, reserved_total, _pending = self.cloud_totals()
        return known_total + reserved_total

    def source_cost(self, source_id: str) -> dict[str, Any] | None:
        """Live cost entry of one source (``known``/``reserved``/``pending``), if any."""
        return self._cloud_costs.get(source_id)

    def account_cloud_source(
        self,
        source_id: str,
        initial_exposure: Decimal,
        payload: Any,
        *,
        snapshot: bool = False,
    ) -> None:
        def value(name: str) -> Any:
            return payload.get(name) if isinstance(payload, dict) else getattr(payload, name, None)

        current = self._cloud_costs.setdefault(
            source_id,
            {"known": Decimal("0"), "reserved": Decimal("0"), "pending": False},
        )
        known = value("spent_usd" if snapshot else "cost_usd")
        reserved = value("reserved_usd" if snapshot else "reserved_cost_usd")
        pending = value("cost_pending")
        if known is not None:
            # The adapter treats all prior exposure as initial spent. Only
            # the current source's delta is newly confirmed provider cost.
            current["known"] = max(Decimal("0"), Decimal(str(known)) - initial_exposure)
        if reserved is not None:
            current["reserved"] = Decimal(str(reserved))
        if pending is not None:
            current["pending"] = bool(pending)
        elif reserved is not None:
            current["pending"] = current["reserved"] > 0
        known_total, reserved_total, pending_total = self.cloud_totals()
        self._asr_metrics["asr_cost_usd_total"] = float(known_total)
        self._update_job(
            self._job_id,
            persist=self._should_persist(),
            cost_usd=float(known_total),
            reserved_cost_usd=float(reserved_total),
            cost_pending=pending_total,
            **self._asr_metrics,
        )

    def init_llm_budget(self, environ: Mapping[str, str]) -> bool:
        """Create the LLM budget seeded with the ASR exposure; fail closed on bad env.

        Returns ``False`` (budget stays ``None``, error recorded in the job
        status) when the cost settings are invalid.
        """
        exposure = self.cloud_exposure()
        try:
            self._llm_budget = CostBudget.for_llm_tts(environ, exposure)
            self._llm_initial = exposure
        except ValueError as exc:
            self._logger.warning(
                "Sync job=%s: LLM review disabled, invalid cost settings: %s", self._job_id, exc
            )
            self._update_job(self._job_id, llm_budget_error=str(exc))
            return False
        return True

    def llm_cost_fields(self) -> dict[str, Any]:
        llm_budget = self._llm_budget
        if llm_budget is None:
            return {}
        snapshot = llm_budget.snapshot()
        known_total, reserved_total, pending_total = self.cloud_totals()
        llm_spent = max(0.0, snapshot["spent_usd"] - float(self._llm_initial))
        return {
            "cost_usd": float(known_total) + llm_spent,
            "reserved_cost_usd": float(reserved_total) + snapshot["reserved_usd"],
            "cost_pending": pending_total or llm_budget.has_pending_reservations,
            "llm_cost_usd": llm_spent,
            # Part of llm_cost_usd booked by estimate, not by provider-reported cost.
            "llm_estimated_cost_usd": snapshot["estimated_spent_usd"],
        }
