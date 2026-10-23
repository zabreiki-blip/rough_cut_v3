"""Decimal-backed cost reservations local to one job (cloud ASR, LLM, TTS)."""

from __future__ import annotations

import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import uuid4

from requests.exceptions import ConnectionError as RequestsConnectionError
from requests.exceptions import ConnectTimeout as RequestsConnectTimeout
from urllib3.exceptions import NewConnectionError

from openrouter_usage import extract_cost


class CostBudgetExceeded(RuntimeError):
    """The next estimated provider attempt would exceed the job limit."""


# Depth limit for walking exception chains (requests -> urllib3 -> OS error).
_MAX_CAUSE_DEPTH = 6


def _has_new_connection_error(error: BaseException) -> bool:
    """Whether urllib3's NewConnectionError (incl. DNS failure) is in the error chain.

    Follows ``args[0]``, ``.reason`` (MaxRetryError) and ``__cause__`` (not ``__context__``,
    which may hold an earlier retry's error),
    breadth-first, at most ``_MAX_CAUSE_DEPTH`` levels deep.
    """
    level: list[BaseException] = [error]
    for _ in range(_MAX_CAUSE_DEPTH):
        next_level: list[BaseException] = []
        for node in level:
            if isinstance(node, NewConnectionError):
                return True
            candidates = (
                node.args[0] if node.args else None,
                getattr(node, "reason", None),
                node.__cause__,
            )
            next_level.extend(c for c in candidates if isinstance(c, BaseException))
        if not next_level:
            break
        level = next_level
    return False


# Environment variables that configure a job budget for LLM/TTS spend.
LLM_TTS_ENV_NAMES = (
    "ROUGH_CUT_MAX_USD_PER_JOB",
    "ROUGH_CUT_LLM_USD_PER_1K_TOKENS_ESTIMATE",
    "ROUGH_CUT_TTS_USD_PER_1K_CHARS_ESTIMATE",
)

# Rough prompt size model for providers that do not report it before dispatch.
_CHARS_PER_TOKEN = 4


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
    llm_usd_per_1k_tokens: Decimal = Decimal("0")
    tts_usd_per_1k_chars: Decimal = Decimal("0")

    @classmethod
    def for_llm_tts(cls, environ: Mapping[str, str]) -> CostGuardConfig:
        """Config for LLM/TTS jobs: the cloud ASR rate is neither read nor required."""
        limit_name = "ROUGH_CUT_MAX_USD_PER_JOB"
        llm_name = "ROUGH_CUT_LLM_USD_PER_1K_TOKENS_ESTIMATE"
        tts_name = "ROUGH_CUT_TTS_USD_PER_1K_CHARS_ESTIMATE"
        return cls(
            max_usd_per_job=_nonnegative_decimal(
                environ.get(limit_name, "").strip() or "0", limit_name
            ),
            cloud_asr_usd_per_minute=Decimal("0"),
            llm_usd_per_1k_tokens=_nonnegative_decimal(
                environ.get(llm_name, "").strip() or "0", llm_name
            ),
            tts_usd_per_1k_chars=_nonnegative_decimal(
                environ.get(tts_name, "").strip() or "0", tts_name
            ),
        )

    @classmethod
    def from_environ(cls, environ: Mapping[str, str]) -> CostGuardConfig:
        limit_name = "ROUGH_CUT_MAX_USD_PER_JOB"
        rate_name = "ROUGH_CUT_CLOUD_ASR_USD_PER_MINUTE_ESTIMATE"
        limit = _nonnegative_decimal(environ.get(limit_name, "").strip() or "0", limit_name)
        rate = _nonnegative_decimal(environ.get(rate_name, "").strip() or "0", rate_name)
        if limit > 0 and rate == 0:
            raise ValueError(f"{rate_name} must be positive when {limit_name} is positive")
        llm_name = "ROUGH_CUT_LLM_USD_PER_1K_TOKENS_ESTIMATE"
        tts_name = "ROUGH_CUT_TTS_USD_PER_1K_CHARS_ESTIMATE"
        llm_rate = _nonnegative_decimal(environ.get(llm_name, "").strip() or "0", llm_name)
        tts_rate = _nonnegative_decimal(environ.get(tts_name, "").strip() or "0", tts_name)
        return cls(
            max_usd_per_job=limit,
            cloud_asr_usd_per_minute=rate,
            llm_usd_per_1k_tokens=llm_rate,
            tts_usd_per_1k_chars=tts_rate,
        )


def messages_text_chars(messages: Sequence[Mapping[str, Any]]) -> int:
    """Count the text characters of chat messages (plain or multi-part content)."""
    total = 0
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            total += len(content)
        elif isinstance(content, Sequence):
            for part in content:
                if isinstance(part, Mapping):
                    text = part.get("text")
                    if isinstance(text, str):
                        total += len(text)
    return total


class CostBudget:
    """Reserve before dispatch, then replace estimates with known actuals.

    A zero limit disables enforcement while retaining accounting. Unknown
    provider costs remain reserved, so subsequent attempts cannot reuse that
    headroom. One instance belongs to a single job; all state changes are
    serialized by a lock because LLM and TTS calls run in thread pools.
    """

    def __init__(
        self,
        limit_usd: Decimal,
        usd_per_minute: Decimal,
        initial_spent_usd: Decimal = Decimal("0"),
        *,
        llm_usd_per_1k_tokens: Decimal = Decimal("0"),
        tts_usd_per_1k_chars: Decimal = Decimal("0"),
        require_asr_rate: bool = True,
    ) -> None:
        self.limit_usd = _nonnegative_decimal(limit_usd, "limit_usd")
        self.usd_per_minute = _nonnegative_decimal(usd_per_minute, "usd_per_minute")
        self.llm_usd_per_1k_tokens = _nonnegative_decimal(
            llm_usd_per_1k_tokens, "llm_usd_per_1k_tokens"
        )
        self.tts_usd_per_1k_chars = _nonnegative_decimal(
            tts_usd_per_1k_chars, "tts_usd_per_1k_chars"
        )
        self._spent_usd = _nonnegative_decimal(initial_spent_usd, "initial_spent_usd")
        if require_asr_rate and self.limit_usd > 0 and self.usd_per_minute == 0:
            raise ValueError("usd_per_minute must be positive when limit_usd is positive")
        self._estimated_spent_usd = Decimal("0")
        self._reservations: dict[str, Decimal] = {}
        self._lock = threading.RLock()

    @classmethod
    def from_config(
        cls, config: CostGuardConfig, initial_spent_usd: Decimal = Decimal("0")
    ) -> CostBudget:
        return cls(
            config.max_usd_per_job,
            config.cloud_asr_usd_per_minute,
            initial_spent_usd,
            llm_usd_per_1k_tokens=config.llm_usd_per_1k_tokens,
            tts_usd_per_1k_chars=config.tts_usd_per_1k_chars,
        )

    @classmethod
    def for_llm_tts(
        cls, environ: Mapping[str, str], initial_spent_usd: Decimal = Decimal("0")
    ) -> CostBudget:
        """Budget of a job that pays only for LLM/TTS (no cloud ASR rate needed)."""
        config = CostGuardConfig.for_llm_tts(environ)
        return cls(
            config.max_usd_per_job,
            config.cloud_asr_usd_per_minute,
            initial_spent_usd,
            llm_usd_per_1k_tokens=config.llm_usd_per_1k_tokens,
            tts_usd_per_1k_chars=config.tts_usd_per_1k_chars,
            require_asr_rate=False,
        )

    def estimate_seconds(self, seconds: float) -> Decimal:
        duration = _nonnegative_decimal(seconds, "seconds")
        return duration * self.usd_per_minute / Decimal("60")

    def estimate_llm(self, prompt_chars: int, max_tokens: int) -> Decimal:
        """Estimate one chat attempt: prompt tokens (chars / 4, rounded up) + output cap."""
        prompt_tokens = -(-max(0, int(prompt_chars)) // _CHARS_PER_TOKEN)
        tokens = prompt_tokens + max(0, int(max_tokens))
        return Decimal(tokens) / Decimal("1000") * self.llm_usd_per_1k_tokens

    def estimate_tts(self, chars: int) -> Decimal:
        return Decimal(max(0, int(chars))) / Decimal("1000") * self.tts_usd_per_1k_chars

    def _reserved_usd(self) -> Decimal:
        return sum(self._reservations.values(), Decimal("0"))

    @property
    def has_pending_reservations(self) -> bool:
        """Unknown provider cost stays pending even when its estimate is zero."""
        with self._lock:
            return bool(self._reservations)

    def _reserve(self, estimate: Decimal, what: str) -> str:
        with self._lock:
            projected_total = self._spent_usd + self._reserved_usd() + estimate
            if self.limit_usd > 0 and projected_total > self.limit_usd:
                raise CostBudgetExceeded(
                    f"{what} would exceed the job cost limit of ${self.limit_usd}"
                )
            reservation_id = uuid4().hex
            self._reservations[reservation_id] = estimate
            return reservation_id

    def reserve_attempt(self, seconds: float) -> str:
        return self._reserve(self.estimate_seconds(seconds), "Cloud ASR attempt")

    def reserve_usd(self, estimate: object, what: str = "Request") -> str:
        """Reserve an explicit USD estimate (for operations with their own rate)."""
        return self._reserve(_nonnegative_decimal(estimate, "estimate"), what)

    def reserve_llm_attempt(self, messages: Sequence[Mapping[str, Any]], max_tokens: int) -> str:
        estimate = self.estimate_llm(messages_text_chars(messages), max_tokens)
        return self._reserve(estimate, "LLM request")

    def reserve_tts_attempt(self, text: str) -> str:
        return self._reserve(self.estimate_tts(len(text)), "TTS request")

    def settle(
        self,
        reservation_id: str,
        actual_usd: object,
        *,
        estimate_if_unknown: bool = False,
    ) -> None:
        """Replace a reservation by the actual cost.

        Without a known cost the reservation stays pending (ASR behaviour) unless
        ``estimate_if_unknown`` asks to book the estimate as spent instead.
        """
        with self._lock:
            # Validate the ID even when provider usage is absent.
            estimate = self._reservations[reservation_id]
            if actual_usd is None:
                if not estimate_if_unknown:
                    return
                actual = estimate
            else:
                actual = _nonnegative_decimal(actual_usd, "actual_usd")
            del self._reservations[reservation_id]
            self._spent_usd += actual
            if actual_usd is None:
                self._estimated_spent_usd += actual

    def release(self, reservation_id: str) -> None:
        """Drop a reservation of an attempt the provider will not bill.

        Idempotent; spent is never changed. Unknown or settled IDs are ignored.
        """
        with self._lock:
            self._reservations.pop(reservation_id, None)

    def release_if_rejected(self, reservation_id: str | None, response: object) -> None:
        """Release after an HTTP answer with status >= 400 (rejected, not billed)."""
        status = getattr(response, "status_code", None)
        if reservation_id is not None and isinstance(status, int) and status >= 400:
            self.release(reservation_id)

    def release_if_unsent(self, reservation_id: str | None, error: BaseException) -> None:
        """Release only when the connection was provably never established.

        Released: ``ConnectTimeout`` and a ``ConnectionError`` whose chain holds
        urllib3 ``NewConnectionError`` (incl. DNS ``NameResolutionError``).
        Everything else stays reserved, because the provider may already have
        received and billed the request: ``ConnectionError`` wrapping
        ``ProtocolError``/``RemoteDisconnected`` (aborted after send), ``ReadTimeout``,
        ``SSLError`` and any other error.
        """
        if reservation_id is None:
            return
        if isinstance(error, RequestsConnectTimeout) or (
            isinstance(error, RequestsConnectionError) and _has_new_connection_error(error)
        ):
            self.release(reservation_id)

    def settle_response(self, reservation_id: str, body: Mapping[str, Any]) -> None:
        """Settle an LLM reservation by ``usage.cost`` or, if absent, by its estimate."""
        self.settle(reservation_id, extract_cost(body), estimate_if_unknown=True)

    def snapshot(self) -> dict[str, float]:
        with self._lock:
            reserved = self._reserved_usd()
            return {
                "limit_usd": float(self.limit_usd),
                "spent_usd": float(self._spent_usd),
                "reserved_usd": float(reserved),
                "total_usd": float(self._spent_usd + reserved),
                # Part of spent_usd booked by estimate (no provider-reported cost).
                "estimated_spent_usd": float(self._estimated_spent_usd),
            }
