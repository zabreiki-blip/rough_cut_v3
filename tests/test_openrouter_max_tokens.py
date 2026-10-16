"""Cost caps for paid OpenRouter chat calls: max_tokens, truncation, oversized blocks."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import httpx
import pytest

import stage1.normalizer as normalizer_module
from openrouter_limits import (
    AUTO_MAX_TOKENS_CEILING,
    OpenRouterOutputTruncatedError,
    resolve_max_tokens,
)
from stage1.config import get_settings, validate_env
from stage1.openrouter_client import analyze_with_openrouter
from stage2 import cloud_asr
from stage2.cloud_asr import CloudAudioAsrAdapter, transcribe_cloud
from stage2.sync_llm_review import SemanticCandidate, review_candidates_with_llm


class _Response:
    status_code = 200

    def __init__(self, content: str, finish_reason: str = "stop") -> None:
        self._content = content
        self._finish_reason = finish_reason

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return {
            "id": "gen-1",
            "usage": {"cost": 0.002},
            "choices": [
                {
                    "message": {"content": self._content},
                    "finish_reason": self._finish_reason,
                }
            ],
        }


_LEGACY_OK = '{"items":[{"text_original":"Текст.","text_for_tts":"Текст.","pause_ms":300}]}'


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENROUTER_MAX_TOKENS", raising=False)
    monkeypatch.delenv("SYNC_CLOUD_ASR_MAX_TOKENS", raising=False)


def _settings() -> Any:
    settings = get_settings()
    settings.openrouter_api_key = "test-key"
    settings.openrouter_model = "test/model"
    settings.openrouter_base_url = "https://example.test/v1"
    return settings


# --- settings / helper ------------------------------------------------------


def test_settings_read_openrouter_max_tokens_env(monkeypatch: pytest.MonkeyPatch) -> None:
    assert get_settings().openrouter_max_tokens == 0
    monkeypatch.setenv("OPENROUTER_MAX_TOKENS", "777")
    assert get_settings().openrouter_max_tokens == 777
    monkeypatch.setenv("OPENROUTER_MAX_TOKENS", "garbage")
    assert get_settings().openrouter_max_tokens == 0
    monkeypatch.setenv("OPENROUTER_MAX_TOKENS", "-5")
    assert get_settings().openrouter_max_tokens == 0


def test_validate_env_warns_on_bad_max_tokens() -> None:
    assert any("OPENROUTER_MAX_TOKENS" in w for w in validate_env({"OPENROUTER_MAX_TOKENS": "x"}))
    assert any("OPENROUTER_MAX_TOKENS" in w for w in validate_env({"OPENROUTER_MAX_TOKENS": "0"}))
    assert not any(
        "OPENROUTER_MAX_TOKENS" in w for w in validate_env({"OPENROUTER_MAX_TOKENS": "4096"})
    )


def test_resolve_max_tokens_scales_with_expected_output_and_is_capped() -> None:
    settings = SimpleNamespace(openrouter_max_tokens=0)
    small = resolve_max_tokens(settings, expected_output_chars=100, floor=1024)
    medium = resolve_max_tokens(settings, expected_output_chars=9_000, floor=1024)
    huge = resolve_max_tokens(settings, expected_output_chars=10_000_000, floor=1024)
    assert small == 1024
    assert medium >= 9_000 / 1.5  # does not cut a normal answer
    assert huge == AUTO_MAX_TOKENS_CEILING


def test_resolve_max_tokens_explicit_value_wins() -> None:
    settings = SimpleNamespace(openrouter_max_tokens=50_000)
    assert resolve_max_tokens(settings, expected_output_chars=10, floor=1024) == 50_000
    # settings objects without the attribute (legacy test doubles) fall back to auto
    assert resolve_max_tokens(SimpleNamespace(), expected_output_chars=10, floor=1024) == 1024


# --- legacy openrouter_client ----------------------------------------------


def test_legacy_client_payload_has_max_tokens() -> None:
    with patch(
        "stage1.openrouter_client.requests.post", return_value=_Response(_LEGACY_OK)
    ) as post:
        analyze_with_openrouter(_settings(), "Текст.")
    assert post.call_args.kwargs["json"]["max_tokens"] >= 2048


def test_legacy_client_default_does_not_truncate_a_full_chunk() -> None:
    # analyzer chunks are up to 6000 chars; the answer repeats the text twice
    # (text_original + text_for_tts) inside JSON.
    text = "Слово " * 1000
    with patch(
        "stage1.openrouter_client.requests.post", return_value=_Response(_LEGACY_OK)
    ) as post:
        analyze_with_openrouter(_settings(), text)
    assert post.call_args.kwargs["json"]["max_tokens"] >= 2 * len(text) / 2.5


def test_legacy_client_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_MAX_TOKENS", "777")
    settings = get_settings()
    settings.openrouter_api_key = "test-key"
    with patch(
        "stage1.openrouter_client.requests.post", return_value=_Response(_LEGACY_OK)
    ) as post:
        analyze_with_openrouter(settings, "Текст.")
    assert post.call_args.kwargs["json"]["max_tokens"] == 777


def test_legacy_client_length_finish_reason_is_not_retried() -> None:
    usage: list[dict] = []
    with patch(
        "stage1.openrouter_client.requests.post",
        return_value=_Response('{"items":[', finish_reason="length"),
    ) as post:
        with pytest.raises(OpenRouterOutputTruncatedError, match="OPENROUTER_MAX_TOKENS"):
            analyze_with_openrouter(_settings(), "Текст.", usage.append)
    assert post.call_count == 1
    assert usage and usage[0]["cost_usd"] == "0.002"  # the paid call is still accounted


# --- normalizer classifier --------------------------------------------------


def _classifier_settings(**extra: Any) -> Any:
    return SimpleNamespace(
        openrouter_api_key="test-key",
        openrouter_model="model-x",
        openrouter_base_url="https://example.invalid/api/v1",
        **extra,
    )


def test_classifier_payload_has_max_tokens() -> None:
    with (
        patch.object(normalizer_module, "get_settings", return_value=_classifier_settings()),
        patch("requests.post", return_value=_Response('{"items": []}')) as post,
    ):
        normalizer_module._openrouter_classifier([{"uid": "u1", "original_text": "text"}], "hint")
    assert post.call_args.kwargs["json"]["max_tokens"] >= 1024


def test_classifier_default_covers_a_full_batch() -> None:
    blocks = [
        {"uid": f"u{i}", "original_text": "Текст абзаца " * 40, "original_label": ""}
        for i in range(20)
    ]
    size = len(json.dumps(blocks, ensure_ascii=False))
    assert size <= 12_000 + 2_000
    with (
        patch.object(normalizer_module, "get_settings", return_value=_classifier_settings()),
        patch("requests.post", return_value=_Response('{"items": []}')) as post,
    ):
        normalizer_module._openrouter_classifier(blocks, "")
    # every voiceover echoes its text in text_for_tts
    assert post.call_args.kwargs["json"]["max_tokens"] >= size / 2.5


def test_classifier_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    with (
        patch.object(
            normalizer_module,
            "get_settings",
            return_value=_classifier_settings(openrouter_max_tokens=555),
        ),
        patch("requests.post", return_value=_Response('{"items": []}')) as post,
    ):
        normalizer_module._openrouter_classifier([{"uid": "u1", "original_text": "t"}], "")
    assert post.call_args.kwargs["json"]["max_tokens"] == 555


def test_classifier_length_finish_reason_is_not_retried() -> None:
    usage: list[dict] = []
    with (
        patch.object(normalizer_module, "get_settings", return_value=_classifier_settings()),
        patch("requests.post", return_value=_Response('{"items":[', "length")) as post,
        patch.object(normalizer_module.time, "sleep") as sleep,
    ):
        with pytest.raises(OpenRouterOutputTruncatedError, match="OPENROUTER_MAX_TOKENS"):
            normalizer_module._openrouter_classifier(
                [{"uid": "u1", "original_text": "t"}], "", usage_callback=usage.append
            )
    assert post.call_count == 1
    sleep.assert_not_called()
    assert usage


# --- sync LLM review --------------------------------------------------------


def _candidates() -> list[SemanticCandidate]:
    return [SemanticCandidate("s", 1, 2, 0.6, "текст")]


def test_sync_review_payload_has_max_tokens_and_env_override() -> None:
    calls: list[dict] = []

    def request(url: str, **kwargs: Any) -> _Response:
        calls.append(kwargs["json"])
        return _Response('{"selected_index":-1,"confidence":0.1,"reason":"-"}')

    review_candidates_with_llm(
        query_text="цитата",
        speaker="",
        candidates=_candidates(),
        settings=_settings(),
        request_fn=request,
    )
    assert calls[0]["max_tokens"] >= 512
    settings = _settings()
    settings.openrouter_max_tokens = 333
    review_candidates_with_llm(
        query_text="цитата",
        speaker="",
        candidates=_candidates(),
        settings=settings,
        request_fn=request,
    )
    assert calls[1]["max_tokens"] == 333


def test_sync_review_length_finish_reason_raises_once() -> None:
    calls: list[int] = []

    def request(url: str, **kwargs: Any) -> _Response:
        calls.append(1)
        return _Response('{"selected_index":', "length")

    with pytest.raises(OpenRouterOutputTruncatedError, match="OPENROUTER_MAX_TOKENS"):
        review_candidates_with_llm(
            query_text="цитата",
            speaker="",
            candidates=_candidates(),
            settings=_settings(),
            request_fn=request,
        )
    assert len(calls) == 1


# --- cloud ASR --------------------------------------------------------------


def _asr_response(text: str, finish_reason: str = "stop") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": text}, "finish_reason": finish_reason}],
            "usage": {"cost": 0.01},
        },
    )


@pytest.fixture
def _asr_env(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_extract(self: Any, media_path: Any, start_sec: Any, duration_sec: Any) -> bytes:
        return b"fake"

    monkeypatch.setattr(CloudAudioAsrAdapter, "_probe_duration", lambda self, media_path: 60.0)
    monkeypatch.setattr(CloudAudioAsrAdapter, "_extract_chunk_mp3", fake_extract)


def _patch_asr_client(monkeypatch: pytest.MonkeyPatch, handler: Any) -> None:
    real = httpx.AsyncClient
    transport = httpx.MockTransport(handler)

    def fake(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs.pop("timeout", None)
        return real(transport=transport)

    monkeypatch.setattr(cloud_asr.httpx, "AsyncClient", fake)


def test_cloud_asr_payload_has_max_tokens_and_env_override(
    monkeypatch: pytest.MonkeyPatch, _asr_env: None
) -> None:
    payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        return _asr_response("[00:01.0 — 00:02.0] ГОЛОС 1: Привет")

    _patch_asr_client(monkeypatch, handler)
    transcribe_cloud(Path("/nonexistent.wav"), api_key="k")
    assert payloads[0]["max_tokens"] >= 16_384
    monkeypatch.setenv("SYNC_CLOUD_ASR_MAX_TOKENS", "4321")
    transcribe_cloud(Path("/nonexistent.wav"), api_key="k")
    assert payloads[1]["max_tokens"] == 4321


def test_cloud_asr_length_finish_reason_fails_without_retry(
    monkeypatch: pytest.MonkeyPatch, _asr_env: None
) -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return _asr_response("[00:01.0 — 00:02.0] ГОЛОС 1: Прив", "length")

    _patch_asr_client(monkeypatch, handler)
    with pytest.raises(cloud_asr.CloudAudioAsrError, match="SYNC_CLOUD_ASR_MAX_TOKENS") as info:
        transcribe_cloud(Path("/nonexistent.wav"), api_key="k")
    assert len(calls) == 1
    assert info.value.cost_usd == pytest.approx(0.01)


# --- oversized normalizer block --------------------------------------------


def _block(text: str, uid: str = "u1") -> Any:
    return normalizer_module.NormalizedBlock(
        uid=uid,
        order=1,
        original_text=text,
        normalized_text=text,
    )


def test_llm_input_truncates_oversized_block_deterministically() -> None:
    text = "Начало блока. " + "ы" * 500_000 + " Конец блока."
    item = normalizer_module._llm_input(_block(text))
    assert len(item["original_text"]) < 9_000
    assert item["original_text"].startswith("Начало блока.")
    assert item["original_text"].endswith("Конец блока.")
    assert item["text_truncated"] is True
    assert normalizer_module._llm_input(_block(text)) == item


def test_llm_input_leaves_normal_block_unchanged() -> None:
    item = normalizer_module._llm_input(_block("Обычный абзац."))
    assert item["original_text"] == "Обычный абзац."
    assert "text_truncated" not in item


def test_oversized_block_yields_bounded_batch() -> None:
    huge = _block("x" * 2_000_000, "big")
    batches = normalizer_module._llm_batches([huge])
    size = sum(
        len(json.dumps(normalizer_module._llm_input(b), ensure_ascii=False))
        for batch in batches
        for b in batch
    )
    assert size < 12_000


def test_truncated_block_keeps_its_own_text_for_tts() -> None:
    long_text = "Диктор читает. " + "а" * 100_000
    block = _block(long_text)
    normalizer_module._apply_llm_results(
        [block],
        [
            {
                "uid": "u1",
                "type": "voiceover",
                "confidence": 0.95,
                "text_for_tts": "Диктор читает. […] обрезок",
            }
        ],
    )
    assert block.type == "voiceover"
    assert block.text_for_tts == long_text  # never the LLM echo of a truncated prefix
    assert any("truncat" in w.lower() for w in block.warnings)


def test_normal_block_still_takes_llm_text_for_tts() -> None:
    block = _block("Диктор читает 5 книг.")
    normalizer_module._apply_llm_results(
        [block],
        [
            {
                "uid": "u1",
                "type": "voiceover",
                "confidence": 0.95,
                "text_for_tts": "Диктор читает пять книг.",
            }
        ],
    )
    assert block.text_for_tts == "Диктор читает пять книг."
