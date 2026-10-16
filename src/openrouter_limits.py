"""Output-size limits for paid OpenRouter chat/completions calls.

Every chat request must carry ``max_tokens`` so a misbehaving model cannot
produce an unbounded (and unbounded-cost) completion.  When the limit is hit
the provider reports ``finish_reason == "length"``; the answer is cut mid-JSON
and re-sending the same request would just pay for the same truncated result,
so callers raise :class:`OpenRouterOutputTruncatedError` instead of retrying.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

MAX_TOKENS_ENV = "OPENROUTER_MAX_TOKENS"
# Automatic budgets never exceed this; many chat models cap completions at
# 16k tokens and OpenRouter rejects larger requests for them.  Set
# OPENROUTER_MAX_TOKENS explicitly to go higher.
AUTO_MAX_TOKENS_CEILING = 16_384
# Conservative: Cyrillic JSON can cost about 1 token per 1.5 characters.
_CHARS_PER_TOKEN = 1.5
_OVERHEAD_TOKENS = 512


class OpenRouterOutputTruncatedError(RuntimeError):
    """The model stopped because ``max_tokens`` was reached (answer is cut off)."""


def resolve_max_tokens(
    settings: Any,
    *,
    expected_output_chars: int,
    floor: int,
    ceiling: int = AUTO_MAX_TOKENS_CEILING,
) -> int:
    """Return the ``max_tokens`` for a request.

    An explicit ``settings.openrouter_max_tokens`` (env ``OPENROUTER_MAX_TOKENS``)
    always wins.  Otherwise the budget is derived from the expected size of the
    answer, clamped to ``[floor, ceiling]``.
    """
    explicit = getattr(settings, "openrouter_max_tokens", 0) or 0
    if isinstance(explicit, int) and explicit > 0:
        return explicit
    estimate = math.ceil(max(0, expected_output_chars) / _CHARS_PER_TOKEN) + _OVERHEAD_TOKENS
    return max(floor, min(ceiling, estimate))


def finish_reason(body: Mapping[str, Any]) -> str:
    choices = body.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], Mapping):
        return str(choices[0].get("finish_reason") or "")
    return ""


def raise_if_truncated(
    body: Mapping[str, Any],
    *,
    model: str,
    max_tokens: int,
    env_name: str = MAX_TOKENS_ENV,
) -> None:
    """Raise when the completion was cut off by the token limit."""
    if finish_reason(body) == "length":
        raise OpenRouterOutputTruncatedError(
            f"OpenRouter answer from {model} was truncated at max_tokens={max_tokens} "
            f"(finish_reason=length). It was not retried to avoid paying for the same "
            f"result again; increase {env_name} and run again."
        )
