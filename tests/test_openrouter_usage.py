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


@pytest.mark.parametrize("reserve", ["0.17", "0"])
def test_pending_callback_persists_latest_reserve_separately_from_confirmed_spend(
    tmp_path: Path, reserve: str
) -> None:
    path = tmp_path / "usage.json"
    # Existing version-1 ledgers remain writable without losing history.
    path.write_text(json.dumps({"schema_version": 1, "events": []}))
    callback = make_usage_callback(path, stage="assembly", operation="asr", job_id="job-1")
    callback({"event_id": "pending-1", "pending": True, "reserved_cost_usd": "0.20"})
    callback({"event_id": "pending-1", "pending": True, "reserved_cost_usd": reserve})
    callback({"event_id": "known-1", "cost_usd": "0.03"})

    ledger = json.loads(path.read_text())
    assert ledger["schema_version"] == 2
    pending = next(event for event in ledger["events"] if event["status"] == "pending")
    assert pending["reserved_cost_usd"] == reserve
    assert pending["cost_usd"] is None
    assert usage_summary(path)["total_cost_usd"] == pytest.approx(0.03)
    assert usage_summary(path)["reserved_cost_usd"] == pytest.approx(float(reserve))

    callback({"event_id": "pending-1", "cost_usd": "0.04"})
    assert usage_summary(path)["total_cost_usd"] == pytest.approx(0.07)
    assert usage_summary(path)["reserved_cost_usd"] == 0


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


def test_concurrent_reconcile_queries_each_pending_event_once(tmp_path: Path) -> None:
    import threading

    path = tmp_path / "usage.json"
    record_usage(
        path,
        stage="tts",
        operation="x",
        model="m",
        generation_id="gen-1",
        pending=True,
        base_url="https://or.test/api",
    )
    entered = threading.Event()
    release = threading.Event()
    calls: list[str] = []

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"data": {"total_cost": 0.01}}

    def blocking_request(*args, **kwargs):
        calls.append(kwargs["params"]["id"])
        entered.set()
        assert release.wait(5)
        return Response()

    def counting_request(*args, **kwargs):
        calls.append(kwargs["params"]["id"])
        return Response()

    first = threading.Thread(
        target=reconcile_pending,
        args=(path,),
        kwargs={"api_key": "k", "request_fn": blocking_request},
    )
    first.start()
    try:
        assert entered.wait(5)
        reconcile_pending(path, api_key="k", request_fn=counting_request)
        assert calls == ["gen-1"]
    finally:
        release.set()
        first.join(5)

    assert calls == ["gen-1"]
    assert usage_summary(path)["pending_count"] == 0


def test_reconcile_releases_claim_when_request_fails(tmp_path: Path) -> None:
    path = tmp_path / "usage.json"
    record_usage(
        path,
        stage="tts",
        operation="x",
        model="m",
        generation_id="gen-1",
        pending=True,
        base_url="https://or.test/api",
    )

    def failing_request(*args, **kwargs):
        raise RuntimeError("network down")

    reconcile_pending(path, api_key="k", request_fn=failing_request)

    from openrouter_usage import _CLAIMED, has_due_pending

    assert not _CLAIMED
    assert usage_summary(path)["pending_count"] == 1
    # после неудачи действует backoff, но claim снят
    assert has_due_pending(path) is False


def test_partial_claims_are_released_when_claiming_fails_midway(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import openrouter_usage

    path = tmp_path / "usage.json"
    for index in range(3):
        record_usage(
            path,
            stage="tts",
            operation="x",
            model="m",
            generation_id=f"gen-{index}",
            pending=True,
            base_url="https://or.test/api",
        )
    real_is_due = openrouter_usage._is_due
    calls = {"count": 0}

    def flaky_is_due(event, now):
        calls["count"] += 1
        if calls["count"] == 2:
            raise RuntimeError("boom while claiming")
        return real_is_due(event, now)

    monkeypatch.setattr(openrouter_usage, "_is_due", flaky_is_due)
    with pytest.raises(RuntimeError, match="boom while claiming"):
        reconcile_pending(path, api_key="k", request_fn=lambda *a, **k: None)

    assert not openrouter_usage._CLAIMED


def test_events_without_event_id_are_not_claimed_or_reconciled(tmp_path: Path) -> None:
    import openrouter_usage

    path = tmp_path / "usage.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "events": [
                    {"generation_id": "gen-a", "status": "pending"},
                    {"generation_id": "gen-b", "status": "pending"},
                ],
            }
        ),
        encoding="utf-8",
    )
    requests: list[object] = []

    def request_fn(*args, **kwargs):
        requests.append(kwargs)
        raise RuntimeError("must not be called")

    reconcile_pending(path, api_key="k", request_fn=request_fn)

    assert requests == []
    assert openrouter_usage.has_due_pending(path) is False
    assert not openrouter_usage._CLAIMED
