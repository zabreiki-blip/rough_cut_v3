"""Pre-dispatch cost reservations for paid OpenRouter LLM and TTS HTTP calls."""

from __future__ import annotations

import json
import os
import sys
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import requests
import tenacity

import stage1.analyzer as analyzer_module
import stage1.normalizer as normalizer_module
from cost_budget import CostBudget, CostBudgetExceeded
from stage1 import openrouter_client
from stage1.config import get_settings
from stage1.gemini_tts import (
    _is_retryable_tts_error,
    _synthesize_openrouter_wav,
    _synthesize_with_retry,
)
from stage1.models import NarrationBlock
from stage1.openrouter_client import analyze_with_openrouter
from stage2.sync_llm_review import SemanticCandidate, review_candidates_with_llm

LLM_RATE = Decimal("1")
TTS_RATE = Decimal("1")


def _budget(
    limit: str = "0",
    *,
    llm: Decimal = Decimal("0"),
    tts: Decimal = Decimal("0"),
) -> CostBudget:
    # A positive limit requires a positive ASR rate by the existing contract.
    asr_rate = Decimal("0.1") if Decimal(limit) > 0 else Decimal("0")
    return CostBudget(
        Decimal(limit),
        asr_rate,
        llm_usd_per_1k_tokens=llm,
        tts_usd_per_1k_chars=tts,
    )


def _snapshot(budget: CostBudget) -> dict[str, float]:
    return budget.snapshot()


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENROUTER_MAX_TOKENS", raising=False)


@pytest.fixture(autouse=True)
def _no_backoff():
    with patch.object(openrouter_client, "wait_exponential", lambda **_kw: tenacity.wait_none()):
        yield


# --- openrouter_client.analyze_with_openrouter ------------------------------


def _analyze_settings() -> Any:
    settings = get_settings()
    settings.openrouter_api_key = "test-key"
    settings.openrouter_max_tokens = 0
    return settings


_ANALYZE_CONTENT = json.dumps({"items": [{"text_original": "Текст", "text_for_tts": "Текст"}]})


def _analyze_response(cost: float | None = None) -> MagicMock:
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    body: dict[str, Any] = {
        "choices": [{"message": {"content": _ANALYZE_CONTENT}, "finish_reason": "stop"}]
    }
    if cost is not None:
        body["usage"] = {"cost": cost}
    resp.json.return_value = body
    return resp


def test_analyze_over_limit_does_not_post_and_raises() -> None:
    budget = _budget("0.5", llm=LLM_RATE)
    with patch.object(openrouter_client.requests, "post") as post:
        with pytest.raises(CostBudgetExceeded):
            analyze_with_openrouter(_analyze_settings(), "Текст", budget=budget)
    post.assert_not_called()
    assert _snapshot(budget)["reserved_usd"] == 0.0


def test_analyze_requests_usage_accounting_and_settles_actual_cost() -> None:
    budget = _budget(llm=LLM_RATE)
    with patch.object(
        openrouter_client.requests, "post", return_value=_analyze_response(0.002)
    ) as post:
        analyze_with_openrouter(_analyze_settings(), "Текст", budget=budget)
    assert post.call_args.kwargs["json"]["usage"] == {"include": True}
    assert _snapshot(budget)["spent_usd"] == 0.002
    assert _snapshot(budget)["reserved_usd"] == 0.0


def test_analyze_settles_estimate_when_provider_returns_no_cost() -> None:
    budget = _budget(llm=LLM_RATE)
    with patch.object(openrouter_client.requests, "post", return_value=_analyze_response()):
        analyze_with_openrouter(_analyze_settings(), "Текст", budget=budget)
    snapshot = _snapshot(budget)
    assert snapshot["spent_usd"] >= 2.0  # at least the 2048-token output cap
    assert snapshot["reserved_usd"] == 0.0
    assert not budget.has_pending_reservations


def test_analyze_retry_reserves_again_for_every_attempt() -> None:
    budget = _budget(llm=LLM_RATE)
    with patch.object(
        openrouter_client.requests,
        "post",
        side_effect=[requests.ConnectionError("boom"), _analyze_response(0.002)],
    ) as post:
        analyze_with_openrouter(_analyze_settings(), "Текст", budget=budget)
    assert post.call_count == 2
    snapshot = _snapshot(budget)
    # The failed attempt stays reserved (provider may have billed it); the
    # second attempt was reserved separately and settled by its actual cost.
    assert snapshot["spent_usd"] == 0.002
    assert snapshot["reserved_usd"] >= 2.0


def test_analyze_retry_is_blocked_when_second_reservation_exceeds_limit() -> None:
    probe = _budget(llm=LLM_RATE)
    with patch.object(openrouter_client.requests, "post", return_value=_analyze_response()):
        analyze_with_openrouter(_analyze_settings(), "Текст", budget=probe)
    one_attempt = Decimal(str(probe.snapshot()["spent_usd"]))

    budget = _budget(str(one_attempt * Decimal("1.5")), llm=LLM_RATE)
    with patch.object(
        openrouter_client.requests,
        "post",
        side_effect=[requests.ConnectionError("boom"), _analyze_response(0.002)],
    ) as post:
        with pytest.raises(CostBudgetExceeded):
            analyze_with_openrouter(_analyze_settings(), "Текст", budget=budget)
    assert post.call_count == 1


def test_analyze_without_budget_or_with_zero_rates_is_unchanged() -> None:
    with patch.object(openrouter_client.requests, "post", return_value=_analyze_response()) as post:
        assert len(analyze_with_openrouter(_analyze_settings(), "Текст")) == 1
        zero = _budget("0")
        assert len(analyze_with_openrouter(_analyze_settings(), "Текст", budget=zero)) == 1
    assert post.call_count == 2
    assert _snapshot(zero)["total_usd"] == 0.0


# --- normalizer classifier --------------------------------------------------


def _classifier_settings() -> Any:
    return SimpleNamespace(
        openrouter_api_key="test-key",
        openrouter_model="model-x",
        openrouter_base_url="https://example.invalid/api/v1",
    )


class _ChatResponse:
    def __init__(self, status_code: int = 200, cost: float | None = None) -> None:
        self.status_code = status_code
        self._cost = cost

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))

    def json(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "choices": [{"message": {"content": '{"items": []}'}, "finish_reason": "stop"}]
        }
        if self._cost is not None:
            body["usage"] = {"cost": self._cost}
        return body


def _classify(budget: CostBudget | None) -> Any:
    return normalizer_module._openrouter_classifier(
        [{"uid": "u1", "original_text": "text"}], "hint", budget=budget
    )


def test_classifier_over_limit_does_not_post_and_raises() -> None:
    budget = _budget("0.5", llm=LLM_RATE)
    with (
        patch.object(normalizer_module, "get_settings", return_value=_classifier_settings()),
        patch("requests.post") as post,
    ):
        with pytest.raises(CostBudgetExceeded):
            _classify(budget)
    post.assert_not_called()


def test_classifier_requests_usage_accounting_and_settles_actual_cost() -> None:
    budget = _budget(llm=LLM_RATE)
    with (
        patch.object(normalizer_module, "get_settings", return_value=_classifier_settings()),
        patch("requests.post", return_value=_ChatResponse(cost=0.004)) as post,
    ):
        _classify(budget)
    assert post.call_args.kwargs["json"]["usage"] == {"include": True}
    assert _snapshot(budget)["spent_usd"] == 0.004
    assert _snapshot(budget)["reserved_usd"] == 0.0


def test_classifier_settles_estimate_when_provider_returns_no_cost() -> None:
    budget = _budget(llm=LLM_RATE)
    with (
        patch.object(normalizer_module, "get_settings", return_value=_classifier_settings()),
        patch("requests.post", return_value=_ChatResponse()),
    ):
        _classify(budget)
    assert _snapshot(budget)["spent_usd"] >= 1.0  # at least the 1024-token output cap
    assert not budget.has_pending_reservations


def test_classifier_retry_loop_reserves_before_every_attempt() -> None:
    budget = _budget(llm=LLM_RATE)
    responses = [_ChatResponse(503), _ChatResponse(503), _ChatResponse(cost=0.004)]
    with (
        patch.object(normalizer_module, "get_settings", return_value=_classifier_settings()),
        patch("requests.post", side_effect=responses) as post,
        patch.object(normalizer_module.time, "sleep"),
    ):
        _classify(budget)
    assert post.call_count == 3
    snapshot = _snapshot(budget)
    assert snapshot["spent_usd"] == 0.004
    assert snapshot["reserved_usd"] >= 2.0  # two failed attempts remain reserved


def test_classifier_retry_is_blocked_by_budget_and_not_swallowed() -> None:
    probe = _budget(llm=LLM_RATE)
    with (
        patch.object(normalizer_module, "get_settings", return_value=_classifier_settings()),
        patch("requests.post", return_value=_ChatResponse()),
    ):
        _classify(probe)
    one_attempt = Decimal(str(probe.snapshot()["spent_usd"]))

    budget = _budget(str(one_attempt * Decimal("1.5")), llm=LLM_RATE)
    with (
        patch.object(normalizer_module, "get_settings", return_value=_classifier_settings()),
        patch("requests.post", side_effect=[_ChatResponse(503), _ChatResponse()]) as post,
        patch.object(normalizer_module.time, "sleep"),
    ):
        with pytest.raises(CostBudgetExceeded):
            _classify(budget)
    assert post.call_count == 1


def test_classifier_network_error_retry_reserves_again() -> None:
    budget = _budget(llm=LLM_RATE)
    with (
        patch.object(normalizer_module, "get_settings", return_value=_classifier_settings()),
        patch(
            "requests.post",
            side_effect=[requests.ConnectionError("boom"), _ChatResponse(cost=0.004)],
        ) as post,
        patch.object(normalizer_module.time, "sleep"),
    ):
        _classify(budget)
    assert post.call_count == 2
    assert _snapshot(budget)["reserved_usd"] >= 1.0


def test_classifier_without_budget_or_with_zero_rates_is_unchanged() -> None:
    with (
        patch.object(normalizer_module, "get_settings", return_value=_classifier_settings()),
        patch("requests.post", return_value=_ChatResponse()) as post,
    ):
        assert _classify(None) == []
        zero = _budget("0")
        assert _classify(zero) == []
    assert post.call_count == 2
    assert _snapshot(zero)["total_usd"] == 0.0


def _write_scenario(root: Path) -> Path:
    source = root / "scenario.txt"
    source.write_text("Абзац без явной разметки. " * 5, encoding="utf-8")
    return source


def test_generate_normalization_forwards_budget_to_default_classifier(tmp_path: Path) -> None:
    budget = _budget(llm=LLM_RATE)
    seen: list[Any] = []

    def classifier(items, _hint, **kwargs):
        seen.append(kwargs.get("budget"))
        return [{"uid": item["uid"], "type": "other", "confidence": 0.9} for item in items]

    with patch.object(normalizer_module, "_openrouter_classifier", side_effect=classifier):
        normalizer_module.generate_normalization(
            _write_scenario(tmp_path),
            tmp_path / "out.json",
            memory_path=tmp_path / "memory.sqlite3",
            budget=budget,
        )
    assert seen == [budget]


def test_generate_normalization_does_not_swallow_budget_exceeded(tmp_path: Path) -> None:
    def classifier(items, _hint, **_kwargs):
        raise CostBudgetExceeded("LLM request would exceed the job cost limit of $1")

    with patch.object(normalizer_module, "_openrouter_classifier", side_effect=classifier):
        with pytest.raises(CostBudgetExceeded):
            normalizer_module.generate_normalization(
                _write_scenario(tmp_path),
                tmp_path / "out.json",
                memory_path=tmp_path / "memory.sqlite3",
                budget=_budget("1", llm=LLM_RATE),
            )


def test_generate_normalization_does_not_swallow_budget_exceeded_from_pool(
    tmp_path: Path,
) -> None:
    source = tmp_path / "scenario.txt"
    source.write_text("\n".join(["А" * 7000, "Б" * 7000, "В" * 7000]), encoding="utf-8")

    def classifier(items, _hint, **_kwargs):
        raise CostBudgetExceeded("LLM request would exceed the job cost limit of $1")

    with patch.object(normalizer_module, "_openrouter_classifier", side_effect=classifier):
        with pytest.raises(CostBudgetExceeded):
            normalizer_module.generate_normalization(
                source,
                tmp_path / "out.json",
                memory_path=tmp_path / "memory.sqlite3",
                budget=_budget("1", llm=LLM_RATE),
            )


def test_generate_normalization_cancels_queued_batches_after_budget_exceeded(
    tmp_path: Path,
) -> None:
    source = tmp_path / "scenario.txt"
    source.write_text("\n".join(c * 7000 for c in "АБВГД"), encoding="utf-8")
    calls: list[int] = []

    def classifier(items, _hint, **_kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise CostBudgetExceeded("LLM request would exceed the job cost limit of $1")
        return []

    real_pool = normalizer_module.ThreadPoolExecutor
    with (
        patch.object(normalizer_module, "_openrouter_classifier", side_effect=classifier),
        patch.object(
            normalizer_module,
            "ThreadPoolExecutor",
            lambda max_workers: real_pool(max_workers=1),
        ),
    ):
        with pytest.raises(CostBudgetExceeded):
            normalizer_module.generate_normalization(
                source,
                tmp_path / "out.json",
                memory_path=tmp_path / "memory.sqlite3",
                budget=_budget("1", llm=LLM_RATE),
            )
    # Five batches; the queued ones must not start new paid calls.
    assert len(calls) < 5


# --- analyzer thread pool ---------------------------------------------------


def test_analyze_script_forwards_budget_and_does_not_swallow_exceeded(tmp_path: Path) -> None:
    source = tmp_path / "scenario.txt"
    source.write_text("Достаточно длинный абзац для диктора здесь.", encoding="utf-8")
    budget = _budget("1", llm=LLM_RATE)
    seen: list[Any] = []

    def fake(settings, chunk, *args, **kwargs):
        seen.append(kwargs.get("budget"))
        raise CostBudgetExceeded("LLM request would exceed the job cost limit of $1")

    with (
        patch.dict(os.environ, {"OPENROUTER_API_KEY": "k"}),
        patch.object(analyzer_module, "analyze_with_openrouter", side_effect=fake),
    ):
        with pytest.raises(CostBudgetExceeded):
            analyzer_module.analyze_script(
                source,
                tmp_path / "o.json",
                tmp_path / "r.txt",
                use_llm=True,
                budget=budget,
            )
    assert seen == [budget]


def test_analyze_script_cancels_queued_chunks_after_budget_exceeded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANALYZER_LLM_WORKERS", "1")
    source = tmp_path / "scenario.txt"
    source.write_text("\n\n".join(c * 5000 for c in "АБВГД"), encoding="utf-8")
    calls: list[int] = []

    def fake(settings, chunk, *args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise CostBudgetExceeded("LLM request would exceed the job cost limit of $1")
        return [NarrationBlock(id="x", text_original=chunk, text_for_tts=chunk)]

    with (
        patch.dict(os.environ, {"OPENROUTER_API_KEY": "k"}),
        patch.object(analyzer_module, "analyze_with_openrouter", side_effect=fake),
    ):
        with pytest.raises(CostBudgetExceeded):
            analyzer_module.analyze_script(
                source,
                tmp_path / "o.json",
                tmp_path / "r.txt",
                use_llm=True,
                budget=_budget("1", llm=LLM_RATE),
            )
    # Five chunks; the queued ones must not start new paid calls.
    assert len(calls) < 5


def test_analyze_script_keeps_legacy_call_shape_without_budget(tmp_path: Path) -> None:
    source = tmp_path / "scenario.txt"
    source.write_text("Достаточно длинный абзац для диктора здесь.", encoding="utf-8")

    def fake(settings, chunk):
        return [NarrationBlock(id="x", text_original=chunk, text_for_tts=chunk)]

    with (
        patch.dict(os.environ, {"OPENROUTER_API_KEY": "k"}),
        patch.object(analyzer_module, "analyze_with_openrouter", side_effect=fake),
    ):
        result = analyzer_module.analyze_script(
            source, tmp_path / "o.json", tmp_path / "r.txt", use_llm=True
        )
    assert result["used_llm"] is True


# --- sync LLM review --------------------------------------------------------


def _review_settings() -> Any:
    settings = get_settings()
    settings.openrouter_api_key = "test-key"
    settings.openrouter_model = "test/model"
    settings.openrouter_base_url = "https://example.test/v1"
    settings.openrouter_max_tokens = 0
    return settings


class _ReviewResponse:
    def __init__(self, cost: float | None = None) -> None:
        self._cost = cost

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "choices": [
                {
                    "message": {"content": '{"selected_index":-1,"confidence":0.1,"reason":"-"}'},
                    "finish_reason": "stop",
                }
            ]
        }
        if self._cost is not None:
            body["usage"] = {"cost": self._cost}
        return body


def _review(request_fn: Any, budget: CostBudget | None) -> Any:
    return review_candidates_with_llm(
        query_text="цитата",
        speaker="",
        candidates=[SemanticCandidate("s", 1, 2, 0.6, "текст")],
        settings=_review_settings(),
        request_fn=request_fn,
        budget=budget,
    )


def test_sync_review_over_limit_does_not_post_and_raises() -> None:
    request = MagicMock(return_value=_ReviewResponse())
    with pytest.raises(CostBudgetExceeded):
        _review(request, _budget("0.5", llm=LLM_RATE))
    request.assert_not_called()


def test_sync_review_default_requests_post_is_not_called_when_over_limit() -> None:
    with patch("stage2.sync_llm_review.requests.post") as post:
        with pytest.raises(CostBudgetExceeded):
            review_candidates_with_llm(
                query_text="цитата",
                speaker="",
                candidates=[SemanticCandidate("s", 1, 2, 0.6, "текст")],
                settings=_review_settings(),
                budget=_budget("0.5", llm=LLM_RATE),
            )
    post.assert_not_called()


def test_sync_review_requests_usage_accounting_and_settles_actual_cost() -> None:
    budget = _budget(llm=LLM_RATE)
    request = MagicMock(return_value=_ReviewResponse(cost=0.0011))
    _review(request, budget)
    assert request.call_args.kwargs["json"]["usage"] == {"include": True}
    assert _snapshot(budget)["spent_usd"] == 0.0011
    assert _snapshot(budget)["reserved_usd"] == 0.0


def test_sync_review_settles_estimate_when_provider_returns_no_cost() -> None:
    budget = _budget(llm=LLM_RATE)
    _review(MagicMock(return_value=_ReviewResponse()), budget)
    assert _snapshot(budget)["spent_usd"] >= 1.0
    assert not budget.has_pending_reservations


def test_sync_review_without_budget_or_with_zero_rates_is_unchanged() -> None:
    request = MagicMock(return_value=_ReviewResponse())
    assert _review(request, None) is None
    zero = _budget("0")
    assert _review(request, zero) is None
    assert request.call_count == 2
    assert _snapshot(zero)["total_usd"] == 0.0


# --- OpenRouter TTS ---------------------------------------------------------


def _tts_settings() -> Any:
    return SimpleNamespace(
        openrouter_tts_model="test-model",
        openrouter_api_key="key",
        openrouter_base_url="https://example.invalid",
        openrouter_tts_sample_rate=24000,
    )


class _TtsResponse:
    def __init__(self, status_code: int = 200) -> None:
        self.status_code = status_code
        self.content = b"\x01\x02" * 24
        self.headers: dict[str, str] = {}
        self.text = "provider failure"

    def json(self) -> dict[str, Any]:
        return {"error": "provider failure"}

    def close(self) -> None:
        return None


def _synth(post: Any, budget: CostBudget | None, text: str = "a" * 500) -> bytes:
    with patch.dict(sys.modules, {"requests": SimpleNamespace(post=post)}):
        return _synthesize_openrouter_wav(text, _tts_settings(), "voice", budget=budget)


def test_tts_over_limit_does_not_post_and_raises() -> None:
    post = MagicMock(return_value=_TtsResponse())
    budget = _budget("0.1", tts=TTS_RATE)
    with pytest.raises(CostBudgetExceeded):
        _synth(post, budget)
    post.assert_not_called()
    assert _snapshot(budget)["reserved_usd"] == 0.0


def test_tts_settles_estimate_after_success() -> None:
    post = MagicMock(return_value=_TtsResponse())
    budget = _budget("10", tts=TTS_RATE)
    assert _synth(post, budget).startswith(b"RIFF")
    assert _snapshot(budget)["spent_usd"] == 0.5
    assert _snapshot(budget)["reserved_usd"] == 0.0


def test_tts_failed_attempt_keeps_its_reservation() -> None:
    post = MagicMock(return_value=_TtsResponse(500))
    budget = _budget("10", tts=TTS_RATE)
    with pytest.raises(RuntimeError, match="OpenRouter TTS failed"):
        _synth(post, budget)
    assert _snapshot(budget)["reserved_usd"] == 0.5
    assert _snapshot(budget)["spent_usd"] == 0.0


def test_tts_estimate_uses_the_text_actually_sent_with_style_notes() -> None:
    post = MagicMock(return_value=_TtsResponse())
    budget = _budget(tts=TTS_RATE)
    with patch.dict(sys.modules, {"requests": SimpleNamespace(post=post)}):
        _synthesize_openrouter_wav(
            "Текст ролика",
            _tts_settings(),
            "Aoede",
            tts_model="google/gemini-3.1-flash-tts-preview",
            tts_style="Серьёзное документальное кино",
            budget=budget,
        )
    sent = post.call_args.kwargs["json"]["input"]
    assert _snapshot(budget)["spent_usd"] == pytest.approx(len(sent) / 1000)


def test_tts_without_budget_or_with_zero_rates_is_unchanged() -> None:
    post = MagicMock(side_effect=lambda *_a, **_k: _TtsResponse())
    assert _synth(post, None).startswith(b"RIFF")
    zero = _budget("0")
    assert _synth(post, zero).startswith(b"RIFF")
    assert post.call_count == 2
    assert _snapshot(zero)["total_usd"] == 0.0


def _retry(post: Any, budget: CostBudget | None) -> Any:
    with (
        patch.dict(sys.modules, {"requests": SimpleNamespace(post=post)}),
        patch("stage1.gemini_tts.time.sleep"),
    ):
        return _synthesize_with_retry(
            "openrouter",
            "a" * 500,
            _tts_settings(),
            "voice",
            1.0,
            None,
            None,
            "test-model",
            "",
            budget=budget,
        )


def test_tts_retry_reserves_before_each_attempt() -> None:
    post = MagicMock(side_effect=[_TtsResponse(502), _TtsResponse(502), _TtsResponse()])
    budget = _budget("10", tts=TTS_RATE)
    audio, attempts, errors, final_error, _degraded = _retry(post, budget)
    assert audio is not None and final_error is None
    assert attempts == 3
    assert post.call_count == 3
    snapshot = _snapshot(budget)
    assert snapshot["spent_usd"] == 0.5
    assert snapshot["reserved_usd"] == 1.0  # two failed attempts stay reserved


def test_tts_retry_stops_on_budget_exceeded_and_reports_it() -> None:
    post = MagicMock(side_effect=[_TtsResponse(502), _TtsResponse()])
    budget = _budget("0.75", tts=TTS_RATE)  # one 0.5 attempt fits, a second does not
    audio, attempts, errors, final_error, _degraded = _retry(post, budget)
    assert audio is None
    assert isinstance(final_error, CostBudgetExceeded)
    assert attempts == 2
    assert post.call_count == 1


def test_budget_exceeded_is_never_classified_as_retryable() -> None:
    error = CostBudgetExceeded("TTS request would exceed the job cost limit of $1 (429 timeout)")
    assert not _is_retryable_tts_error("openrouter", error)


def test_macos_connector_is_not_budgeted() -> None:
    from stage1.gemini_tts import _synthesize_wav_bytes

    budget = _budget("0.1", tts=TTS_RATE)
    with patch("stage1.gemini_tts.shutil.which", return_value=None):
        with patch(
            "stage1.gemini_tts._synthesize_macos_connector_wav", return_value=b"wav"
        ) as connector:
            result = _synthesize_wav_bytes(
                "macos", "a" * 5000, SimpleNamespace(), "voice", None, None, budget=budget
            )
    assert result == b"wav"
    connector.assert_called_once()
    assert _snapshot(budget)["total_usd"] == 0.0
