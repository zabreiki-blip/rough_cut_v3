from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from openrouter_usage import (
    make_usage_callback,
    reconcile_pending,
    record_usage,
    report_json_usage,
    usage_summary,
)
from stage1 import normalizer as normalizer_module


def test_records_exact_cost_and_deduplicates_generation(tmp_path: Path) -> None:
    path = tmp_path / "openrouter_usage.json"
    callback = make_usage_callback(
        path,
        stage="normalization",
        operation="normalize",
        job_id="job-1",
        revision_id="rev-2",
    )

    body = {"id": "gen-1", "model": "model-a", "usage": {"cost": 0.0123}}
    report_json_usage(callback, body, model="fallback")
    report_json_usage(callback, body, model="fallback")

    summary = usage_summary(path)
    assert summary["total_cost_usd"] == pytest.approx(0.0123)
    ledger = json.loads(path.read_text(encoding="utf-8"))
    assert len(ledger["events"]) == 1
    assert ledger["events"][0]["stage"] == "normalization"
    assert ledger["events"][0]["revision_id"] == "rev-2"


def test_parallel_events_accumulate_without_lost_updates(tmp_path: Path) -> None:
    path = tmp_path / "openrouter_usage.json"

    def add(index: int) -> None:
        record_usage(
            path,
            stage="voiceover",
            operation="tts",
            model="voice-model",
            cost_usd="0.001",
            generation_id=f"gen-{index}",
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(add, range(40)))

    assert usage_summary(path)["total_cost_usd"] == pytest.approx(0.04)


def test_pending_tts_is_resolved_from_generation_metadata(tmp_path: Path) -> None:
    path = tmp_path / "openrouter_usage.json"
    record_usage(
        path,
        stage="voiceover",
        operation="tts",
        model="voice-model",
        generation_id="gen-tts",
        pending=True,
        base_url="https://openrouter.ai/api/v1",
    )

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"data": {"total_cost": 0.0042}}

    calls: list[dict] = []

    def request_fn(*args, **kwargs):
        calls.append({"args": args, "kwargs": kwargs})
        return Response()

    assert usage_summary(path)["pending_count"] == 1
    reconcile_pending(path, api_key="secret", request_fn=request_fn)

    summary = usage_summary(path)
    assert summary["pending_count"] == 0
    assert summary["total_cost_usd"] == pytest.approx(0.0042)
    assert calls[0]["kwargs"]["params"] == {"id": "gen-tts"}


def test_corrupt_ledger_is_reported_and_not_overwritten(tmp_path: Path) -> None:
    path = tmp_path / "openrouter_usage.json"
    path.write_text("not-json", encoding="utf-8")

    assert usage_summary(path)["status"] == "unavailable"
    assert path.read_text(encoding="utf-8") == "not-json"


def test_normalizer_reports_paid_response_before_content_parse_fails() -> None:
    class Response:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "id": "gen-invalid",
                "usage": {"cost": 0.003},
                "choices": [{"message": {"content": "not json"}}],
            }

    settings = SimpleNamespace(
        openrouter_api_key="key",
        openrouter_model="test/model",
        openrouter_base_url="https://openrouter.ai/api/v1",
    )
    usage: list[dict] = []
    with (
        patch.object(normalizer_module, "get_settings", return_value=settings),
        patch("requests.post", return_value=Response()),
        pytest.raises(ValueError, match="JSON object"),
    ):
        normalizer_module._openrouter_classifier(
            [{"uid": "block-1", "text": "text"}], "", usage_callback=usage.append
        )

    assert usage[0]["generation_id"] == "gen-invalid"
    assert usage[0]["cost_usd"] == "0.003"


def _classifier_settings() -> SimpleNamespace:
    return SimpleNamespace(
        openrouter_api_key="key",
        openrouter_model="test/model",
        openrouter_base_url="https://openrouter.ai/api/v1",
        openrouter_max_tokens=4321,
    )


def _classifier_response(content: str, status_code: int = 200):
    class Response:
        def __init__(self) -> None:
            self.status_code = status_code

        def raise_for_status(self) -> None:
            if status_code >= 400:
                import requests

                raise requests.HTTPError("error", response=self)

        def json(self) -> dict:
            return {"choices": [{"message": {"content": content}}]}

    return Response()


def test_classifier_sends_max_tokens() -> None:
    with (
        patch.object(normalizer_module, "get_settings", return_value=_classifier_settings()),
        patch(
            "requests.post", return_value=_classifier_response('{"items":[{"uid":"a"}]}')
        ) as post,
    ):
        normalizer_module._openrouter_classifier([{"uid": "a"}], "")

    assert post.call_args.kwargs["json"]["max_tokens"] == 4321


def test_classifier_does_not_retry_client_errors() -> None:
    with (
        patch.object(normalizer_module, "get_settings", return_value=_classifier_settings()),
        patch("requests.post", return_value=_classifier_response("{}", 400)) as post,
        patch.object(normalizer_module.time, "sleep") as sleep,
        pytest.raises(Exception, match="error"),
    ):
        normalizer_module._openrouter_classifier([{"uid": "a"}], "")

    assert post.call_count == 1
    sleep.assert_not_called()


def test_classifier_does_not_retry_non_transient_request_exceptions() -> None:
    import requests

    with (
        patch.object(normalizer_module, "get_settings", return_value=_classifier_settings()),
        patch("requests.post", side_effect=requests.exceptions.InvalidURL("bad")) as post,
        patch.object(normalizer_module.time, "sleep"),
        pytest.raises(requests.exceptions.InvalidURL),
    ):
        normalizer_module._openrouter_classifier([{"uid": "a"}], "")

    assert post.call_count == 1


def test_classifier_retries_chunked_encoding_error() -> None:
    import requests

    ok = _classifier_response('{"items":[{"uid":"a"}]}')
    with (
        patch.object(normalizer_module, "get_settings", return_value=_classifier_settings()),
        patch(
            "requests.post",
            side_effect=[requests.exceptions.ChunkedEncodingError("cut"), ok],
        ) as post,
        patch.object(normalizer_module.time, "sleep") as sleep,
    ):
        normalizer_module._openrouter_classifier([{"uid": "a"}], "")

    assert post.call_count == 2
    sleep.assert_called_once()


def test_classifier_does_not_retry_ssl_errors() -> None:
    import requests

    with (
        patch.object(normalizer_module, "get_settings", return_value=_classifier_settings()),
        patch("requests.post", side_effect=requests.exceptions.SSLError("cert")) as post,
        patch.object(normalizer_module.time, "sleep") as sleep,
        pytest.raises(requests.exceptions.SSLError),
    ):
        normalizer_module._openrouter_classifier([{"uid": "a"}], "")

    assert post.call_count == 1
    sleep.assert_not_called()


@pytest.mark.parametrize(
    "content",
    ['{"items":[]}', '{"other":1}', '{"items":["x"]}', '{"items":"x"}'],
)
def test_classifier_rejects_empty_or_invalid_items(content: str) -> None:
    with (
        patch.object(normalizer_module, "get_settings", return_value=_classifier_settings()),
        patch("requests.post", return_value=_classifier_response(content)),
        pytest.raises(ValueError),
    ):
        normalizer_module._openrouter_classifier([{"uid": "a"}], "")
