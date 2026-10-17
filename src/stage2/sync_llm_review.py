"""Constrained LLM review for low-confidence transcript candidates.

The model never searches audio and never supplies timestamps.  It may only
choose one of the ranges already produced from the Whisper word index.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Sequence

import requests

from cost_budget import CostBudget
from openrouter_limits import raise_if_truncated, resolve_max_tokens
from openrouter_usage import report_json_usage
from stage1.config import Settings, get_settings


@dataclass(frozen=True)
class SemanticCandidate:
    source_id: str
    source_in_sec: float
    source_out_sec: float
    confidence: float
    matched_text: str


@dataclass(frozen=True)
class SemanticDecision:
    candidate_index: int
    confidence: float
    reason: str
    model: str


def _json_object(value: str) -> dict[str, Any]:
    text = str(value or "").strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    else:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            text = text[start : end + 1]
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError("LLM semantic review did not return a JSON object")
    return payload


def review_candidates_with_llm(
    *,
    query_text: str,
    speaker: str,
    candidates: Sequence[SemanticCandidate],
    settings: Settings | None = None,
    request_fn: Callable[..., Any] | None = None,
    usage_callback: Callable[[dict[str, Any]], None] | None = None,
    timeout_seconds: float = 45.0,
    budget: CostBudget | None = None,
) -> SemanticDecision | None:
    """Choose a semantic match from verified transcript candidates.

    ``None`` means that the model rejected every candidate or LLM review is not
    configured.  The returned index always refers to the supplied sequence.
    """

    if not candidates:
        return None
    config = settings or get_settings()
    if not config.openrouter_api_key:
        return None
    call = request_fn or requests.post
    candidate_payload = [
        {
            "index": index,
            "transcript": item.matched_text,
            "lexical_confidence": round(float(item.confidence), 4),
        }
        for index, item in enumerate(candidates)
    ]
    prompt = (
        "Ты проверяешь семантическое совпадение цитаты сценария с вариантами "
        "расшифровки интервью. Выбери вариант только если он передаёт тот же "
        "смысл и ключевые утверждения, даже при отличиях устной речи. Нельзя "
        "придумывать новый текст, таймкоды или объединять варианты. Если точного "
        "смыслового совпадения нет, верни selected_index=-1. Ответ — только JSON: "
        '{"selected_index": 0, "confidence": 0.0, "reason": "кратко"}.\n\n'
        f"Спикер: {speaker or 'не указан'}\n"
        f"Цитата сценария: {query_text}\n"
        f"Кандидаты: {json.dumps(candidate_payload, ensure_ascii=False)}"
    )
    # The answer is a one-line JSON object ({"selected_index", "confidence",
    # "reason"}), a few dozen tokens; the floor leaves room for a verbose reason.
    max_tokens = resolve_max_tokens(config, expected_output_chars=0, floor=1024)
    messages = [
        {
            "role": "system",
            "content": "Выбирай только индекс из переданного списка или -1.",
        },
        {"role": "user", "content": prompt},
    ]
    # Reserve before dispatch; CostBudgetExceeded propagates to the caller.
    reservation_id = (
        budget.reserve_llm_attempt(messages, max_tokens) if budget is not None else None
    )
    response = call(
        f"{config.openrouter_base_url}/chat/completions",
        headers={
            "Authorization": f"Bearer {config.openrouter_api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": config.openrouter_model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "max_tokens": max_tokens,
            # Without this flag OpenRouter omits ``usage.cost`` from the answer.
            "usage": {"include": True},
            "messages": messages,
        },
        timeout=timeout_seconds,
    )
    response.raise_for_status()
    body = response.json()
    if budget is not None and reservation_id is not None:
        budget.settle_response(reservation_id, body)
    report_json_usage(usage_callback, body, model=config.openrouter_model)
    raise_if_truncated(body, model=config.openrouter_model, max_tokens=max_tokens)
    content = body["choices"][0]["message"]["content"]
    decision = _json_object(content)
    try:
        index = int(decision.get("selected_index", -1))
        confidence = float(decision.get("confidence", 0.0))
    except (TypeError, ValueError) as exc:
        raise ValueError("LLM semantic review returned invalid fields") from exc
    if index == -1:
        return None
    if index < 0 or index >= len(candidates):
        raise ValueError("LLM semantic review selected an unknown candidate")
    if not 0 <= confidence <= 1:
        raise ValueError("LLM semantic confidence must be between 0 and 1")
    return SemanticDecision(
        candidate_index=index,
        confidence=confidence,
        reason=str(decision.get("reason") or "").strip()[:240],
        model=config.openrouter_model,
    )
