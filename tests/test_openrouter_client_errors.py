"""Failure-mode tests for ``stage1.openrouter_client.analyze_with_openrouter``.

No network: ``requests.post`` is mocked and tenacity back-off is replaced by
``wait_none`` so retries are instantaneous.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import requests
import tenacity

from openrouter_limits import OpenRouterOutputTruncatedError
from stage1 import openrouter_client
from stage1.config import get_settings
from stage1.openrouter_client import (
    _extract_json,
    _normalize_blocks,
    analyze_with_openrouter,
)


def _settings(**overrides: Any):
    settings = get_settings()
    settings.openrouter_api_key = "test-key"
    settings.openrouter_max_tokens = 0
    for key, value in overrides.items():
        setattr(settings, key, value)
    return settings


def _ok(content: Any, *, finish_reason: str = "stop") -> MagicMock:
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = {
        "choices": [{"message": {"content": content}, "finish_reason": finish_reason}]
    }
    return resp


def _http_error(status: int) -> requests.HTTPError:
    response = requests.Response()
    response.status_code = status
    return requests.HTTPError(f"{status} error", response=response)


def _failing(status: int) -> MagicMock:
    resp = MagicMock()
    resp.raise_for_status.side_effect = _http_error(status)
    return resp


@pytest.fixture(autouse=True)
def _no_backoff():
    with patch.object(openrouter_client, "wait_exponential", lambda **_kw: tenacity.wait_none()):
        yield


VALID_CONTENT = json.dumps({"items": [{"text_original": "Текст", "text_for_tts": "Текст"}]})


def test_missing_api_key_fails_before_any_request() -> None:
    settings = _settings(openrouter_api_key="")
    with patch.object(openrouter_client.requests, "post") as post:
        with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
            analyze_with_openrouter(settings, "Текст")
    post.assert_not_called()


def test_missing_requests_dependency_is_reported() -> None:
    with patch.object(openrouter_client, "requests", None):
        with pytest.raises(RuntimeError, match="requests is not installed"):
            analyze_with_openrouter(_settings(), "Текст")


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_transient_http_status_is_retried_until_success(status: int) -> None:
    with patch.object(
        openrouter_client.requests,
        "post",
        side_effect=[_failing(status), _failing(status), _ok(VALID_CONTENT)],
    ) as post:
        blocks = analyze_with_openrouter(_settings(), "Текст")

    assert post.call_count == 3
    assert [b.text_for_tts for b in blocks] == ["Текст"]


def test_transient_status_gives_up_after_three_attempts_and_reraises() -> None:
    with patch.object(openrouter_client.requests, "post", side_effect=[_failing(503)] * 5) as post:
        with pytest.raises(requests.HTTPError):
            analyze_with_openrouter(_settings(), "Текст")

    assert post.call_count == 3


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_client_errors_are_not_retried(status: int) -> None:
    with patch.object(openrouter_client.requests, "post", return_value=_failing(status)) as post:
        with pytest.raises(requests.HTTPError):
            analyze_with_openrouter(_settings(), "Текст")

    assert post.call_count == 1


@pytest.mark.parametrize("exc_type", [requests.ConnectionError, requests.Timeout])
def test_network_errors_are_retried(exc_type: type[Exception]) -> None:
    with patch.object(
        openrouter_client.requests,
        "post",
        side_effect=[exc_type("boom"), _ok(VALID_CONTENT)],
    ) as post:
        blocks = analyze_with_openrouter(_settings(), "Текст")

    assert post.call_count == 2
    assert len(blocks) == 1


def test_http_error_without_response_is_not_retried() -> None:
    resp = MagicMock()
    resp.raise_for_status.side_effect = requests.HTTPError("no response")
    with patch.object(openrouter_client.requests, "post", return_value=resp) as post:
        with pytest.raises(requests.HTTPError):
            analyze_with_openrouter(_settings(), "Текст")

    assert post.call_count == 1


def test_unexpected_exception_type_is_not_retried() -> None:
    with patch.object(
        openrouter_client.requests, "post", side_effect=ValueError("bad payload")
    ) as post:
        with pytest.raises(ValueError):
            analyze_with_openrouter(_settings(), "Текст")

    assert post.call_count == 1


def test_without_tenacity_request_is_sent_once_without_retry() -> None:
    with (
        patch.object(openrouter_client, "retry", None),
        patch.object(
            openrouter_client.requests, "post", side_effect=[_failing(503), _ok(VALID_CONTENT)]
        ) as post,
    ):
        with pytest.raises(requests.HTTPError):
            analyze_with_openrouter(_settings(), "Текст")

    assert post.call_count == 1


def test_without_tenacity_success_path_still_parses() -> None:
    with (
        patch.object(openrouter_client, "retry", None),
        patch.object(openrouter_client.requests, "post", return_value=_ok(VALID_CONTENT)),
    ):
        blocks = analyze_with_openrouter(_settings(), "Текст")

    assert blocks[0].id == "zk1"


def test_finish_reason_length_raises_and_is_never_retried() -> None:
    with patch.object(
        openrouter_client.requests,
        "post",
        return_value=_ok('{"items": [{"text_orig', finish_reason="length"),
    ) as post:
        with pytest.raises(OpenRouterOutputTruncatedError, match="max_tokens"):
            analyze_with_openrouter(_settings(), "Текст")

    assert post.call_count == 1


def test_explicit_max_tokens_is_sent_and_reported_when_truncated() -> None:
    settings = _settings(openrouter_max_tokens=777)
    with patch.object(
        openrouter_client.requests, "post", return_value=_ok("{", finish_reason="length")
    ) as post:
        with pytest.raises(OpenRouterOutputTruncatedError, match="max_tokens=777"):
            analyze_with_openrouter(settings, "Текст")

    assert post.call_args.kwargs["json"]["max_tokens"] == 777


@pytest.mark.parametrize("content", ["not json at all", "", "[1, 2, 3]"])
def test_content_without_json_object_raises_value_error(content: str) -> None:
    with patch.object(openrouter_client.requests, "post", return_value=_ok(content)):
        with pytest.raises(ValueError, match="does not contain JSON"):
            analyze_with_openrouter(_settings(), "Текст")


def test_malformed_json_object_raises_decode_error() -> None:
    with patch.object(openrouter_client.requests, "post", return_value=_ok('{"items": [')):
        with pytest.raises(json.JSONDecodeError):
            analyze_with_openrouter(_settings(), "Текст")


@pytest.mark.parametrize(
    "content",
    ['{"items": []}', '{"items": "text"}', '{"other": 1}', '{"items": null}'],
)
def test_empty_or_invalid_items_are_rejected(content: str) -> None:
    with patch.object(openrouter_client.requests, "post", return_value=_ok(content)):
        with pytest.raises(ValueError, match="empty or invalid items"):
            analyze_with_openrouter(_settings(), "Текст")


def test_missing_choices_raises_key_error() -> None:
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = {"error": {"message": "rate limited"}}
    with patch.object(openrouter_client.requests, "post", return_value=resp):
        with pytest.raises(KeyError):
            analyze_with_openrouter(_settings(), "Текст")


def test_structured_dict_content_is_serialised_and_parsed() -> None:
    content = {"items": [{"text_original": "A", "text_for_tts": "B"}]}
    with patch.object(openrouter_client.requests, "post", return_value=_ok(content)):
        blocks = analyze_with_openrouter(_settings(), "Текст")

    assert (blocks[0].text_original, blocks[0].text_for_tts) == ("A", "B")


def test_request_uses_bearer_header_and_configured_base_url() -> None:
    settings = _settings(openrouter_base_url="https://example.test/api/v1")
    with patch.object(openrouter_client.requests, "post", return_value=_ok(VALID_CONTENT)) as post:
        analyze_with_openrouter(settings, "Текст")

    assert post.call_args.args[0] == "https://example.test/api/v1/chat/completions"
    assert post.call_args.kwargs["headers"]["Authorization"] == "Bearer test-key"
    assert post.call_args.kwargs["json"]["max_tokens"] >= 2048


def test_extract_json_handles_fenced_and_embedded_objects() -> None:
    assert _extract_json('  {"a": 1}  ') == {"a": 1}
    assert _extract_json('```json\n{"a": 2}\n```') == {"a": 2}
    assert _extract_json('```\n{"a": 3}\n```') == {"a": 3}
    assert _extract_json('Вот ответ: {"a": 4} спасибо') == {"a": 4}


def test_normalize_blocks_applies_defaults_and_coercions() -> None:
    blocks = _normalize_blocks(
        [
            {"text_original": " Один ", "pause_ms": "450", "notes": " n "},
            {"text_original": "Два", "text_for_tts": "", "pause_ms": "abc"},
            {"text_original": "Три", "pause_ms": None},
            {},
        ]
    )

    assert [b.id for b in blocks] == ["zk1", "zk2", "zk3", "zk4"]
    assert (blocks[0].text_original, blocks[0].text_for_tts) == ("Один", "Один")
    assert blocks[0].pause_ms == 450 and blocks[0].notes == "n"
    assert blocks[1].text_for_tts == "Два" and blocks[1].pause_ms == 300
    assert blocks[2].pause_ms == 300
    assert blocks[3].text_original == "" and blocks[3].text_for_tts == ""
