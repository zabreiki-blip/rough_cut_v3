from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import api
from openrouter_usage import record_usage


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    projects = tmp_path / "projects"
    projects.mkdir()
    monkeypatch.setattr(api, "ROOT_DIR", tmp_path)
    monkeypatch.setattr(api, "PROJECTS_DIR", projects)
    monkeypatch.setattr(
        api,
        "NORMALIZATION_MEMORY_PATH",
        tmp_path / "data" / "normalization_memory.sqlite3",
    )
    monkeypatch.setitem(api.app.config, "TESTING", True)
    monkeypatch.delitem(api.app.config, "API_TOKEN", raising=False)
    monkeypatch.setattr(
        api,
        "get_settings",
        lambda: SimpleNamespace(openrouter_api_key="secret", openrouter_base_url="https://or.test"),
    )
    api._OPENROUTER_RECONCILE_ACTIVE.clear()
    return api.app.test_client()


def _record_pending(project: str = "demo") -> None:
    paths = api._ensure_project(project)
    record_usage(
        paths["openrouter_usage"],
        stage="tts",
        operation="x",
        model="m",
        generation_id="gen-1",
        pending=True,
        base_url="https://or.test",
    )


def test_usage_get_never_calls_provider_inline_and_dedupes_background_job(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    _record_pending()
    entered = threading.Event()
    release = threading.Event()
    calls: list[str] = []

    def blocking_reconcile(path, **kwargs):
        calls.append(str(path))
        entered.set()
        release.wait(5)

    monkeypatch.setattr(api, "reconcile_pending", blocking_reconcile)
    try:
        started = time.monotonic()
        first = client.get("/stage1/openrouter-usage?project=demo")
        assert entered.wait(5)
        second = client.get("/stage1/openrouter-usage?project=demo")
        third = client.get("/stage1/openrouter-usage?project=demo")
        elapsed = time.monotonic() - started

        assert elapsed < 2.0
        for response in (first, second, third):
            result = response.get_json()["result"]
            assert response.status_code == 200
            assert result["status"] == "ok"
            assert result["pending_count"] == 1
        assert len(calls) == 1
    finally:
        release.set()
        deadline = time.monotonic() + 5
        while api._OPENROUTER_RECONCILE_ACTIVE and time.monotonic() < deadline:
            time.sleep(0.01)
    assert not api._OPENROUTER_RECONCILE_ACTIVE


def test_usage_get_survives_failing_provider(client, monkeypatch: pytest.MonkeyPatch) -> None:
    _record_pending()

    def boom(path, **kwargs):
        raise RuntimeError("provider down")

    monkeypatch.setattr(api, "reconcile_pending", boom)
    response = client.get("/stage1/openrouter-usage?project=demo")
    assert response.status_code == 200
    assert response.get_json()["result"]["pending_count"] == 1
    deadline = time.monotonic() + 5
    while api._OPENROUTER_RECONCILE_ACTIVE and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not api._OPENROUTER_RECONCILE_ACTIVE  # слот проекта освобождён после ошибки


def test_usage_get_without_pending_starts_no_background_job(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(api, "reconcile_pending", lambda path, **kw: calls.append(str(path)))
    response = client.get("/stage1/openrouter-usage?project=demo")
    assert response.status_code == 200
    assert calls == []
