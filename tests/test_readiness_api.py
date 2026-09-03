from __future__ import annotations

from pathlib import Path

import pytest

import api
import api_core_routes


VALID_STATUSES = {"ok", "warn", "error"}


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    projects = tmp_path / "projects"
    projects.mkdir()
    monkeypatch.setattr(api, "ROOT_DIR", tmp_path)
    monkeypatch.setattr(api, "PROJECTS_DIR", projects)
    monkeypatch.setitem(api.app.config, "TESTING", True)
    # Authentication is intentionally bypassed in tests unless API_TOKEN is set.
    monkeypatch.delitem(api.app.config, "API_TOKEN", raising=False)
    return api.app.test_client()


def test_readiness_response_structure(client) -> None:
    response = client.get("/system/readiness")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is True
    assert payload["overall"] in VALID_STATUSES
    checks = payload["checks"]
    assert isinstance(checks, list)
    assert checks, "readiness должен возвращать хотя бы один пункт"

    for check in checks:
        assert check["status"] in VALID_STATUSES
        assert isinstance(check["id"], str) and check["id"]
        assert isinstance(check["label"], str) and check["label"]
        assert isinstance(check["detail"], str) and check["detail"]
        # Кириллица должна присутствовать в описании и (если есть) в подсказке.
        assert any("а" <= ch <= "я" or "А" <= ch <= "Я" for ch in check["detail"])
        assert "hint" in check


def test_readiness_never_leaks_api_key_values(client, monkeypatch: pytest.MonkeyPatch) -> None:
    canary_openrouter = "sk-secret-canary-12345"
    canary_gemini = "sk-secret-canary-67890"
    canary_cloud_asr = "sk-secret-canary-13579"
    monkeypatch.setenv("OPENROUTER_API_KEY", canary_openrouter)
    monkeypatch.setenv("GEMINI_API_KEY", canary_gemini)
    monkeypatch.setenv("SYNC_CLOUD_ASR_API_KEY", canary_cloud_asr)

    response = client.get("/system/readiness")

    assert response.status_code == 200
    raw_body = response.get_data(as_text=True)

    for canary in (canary_openrouter, canary_gemini, canary_cloud_asr):
        assert canary not in raw_body
        # Частичные/замаскированные фрагменты тоже не должны просочиться.
        assert canary[:6] not in raw_body
        assert canary[-5:] not in raw_body
    assert "12345" not in raw_body
    assert "67890" not in raw_body
    assert "13579" not in raw_body
    assert "sk-se" not in raw_body


def test_readiness_degrades_gracefully_without_ffmpeg(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(api_core_routes.shutil, "which", lambda _name: None)

    response = client.get("/system/readiness")

    assert response.status_code == 200
    payload = response.get_json()
    checks_by_id = {check["id"]: check for check in payload["checks"]}
    assert checks_by_id["ffmpeg"]["status"] in {"error", "warn"}
    assert payload["overall"] in VALID_STATUSES


def test_readiness_whisper_backend_ok_when_only_cloud_configured(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(api_core_routes, "available_backends", lambda: [])
    monkeypatch.delenv("SYNC_CLOUD_ASR_API_KEY", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-fake-cloud-key")

    response = client.get("/system/readiness")

    assert response.status_code == 200
    payload = response.get_json()
    checks_by_id = {check["id"]: check for check in payload["checks"]}
    check = checks_by_id["whisper_backend"]
    assert check["status"] == "ok"
    assert "облач" in check["detail"].lower()


def test_readiness_whisper_backend_warns_when_nothing_available(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(api_core_routes, "available_backends", lambda: [])
    monkeypatch.delenv("SYNC_CLOUD_ASR_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    response = client.get("/system/readiness")

    assert response.status_code == 200
    payload = response.get_json()
    checks_by_id = {check["id"]: check for check in payload["checks"]}
    check = checks_by_id["whisper_backend"]
    assert check["status"] == "warn"
    assert "requirements-whisper.txt" in check["hint"]
    assert "облач" in check["hint"].lower()


def test_readiness_requires_auth_like_other_protected_routes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    projects = tmp_path / "projects"
    projects.mkdir()
    monkeypatch.setattr(api, "ROOT_DIR", tmp_path)
    monkeypatch.setattr(api, "PROJECTS_DIR", projects)
    monkeypatch.setitem(api.app.config, "TESTING", True)
    api.app.config["API_TOKEN"] = "correct-token"
    try:
        test_client = api.app.test_client()

        unauthorized = test_client.get("/system/readiness")
        assert unauthorized.status_code == 401

        authorized = test_client.get(
            "/system/readiness", headers={"Authorization": "Bearer correct-token"}
        )
        assert authorized.status_code == 200
    finally:
        api.app.config.pop("API_TOKEN", None)


def test_readiness_ui_contract_in_template_and_script() -> None:
    root = Path(__file__).parents[1]
    # `stage1.js` is a thin ES-module entry point (see U7); the readiness DOM
    # references live in `state.js` and the fetch call lives in `readiness.js`.
    state_js = (root / "src" / "static" / "stage1" / "state.js").read_text(encoding="utf-8")
    readiness_js = (root / "src" / "static" / "stage1" / "readiness.js").read_text(encoding="utf-8")
    project_html = (root / "src" / "templates" / "project.html").read_text(encoding="utf-8")

    assert 'id="readiness_panel"' in project_html
    assert 'id="readiness_summary_badge"' in project_html
    assert 'id="readiness_list"' in project_html

    assert 'readiness_panel: el("readiness_panel")' in state_js
    assert 'readiness_summary_badge: el("readiness_summary_badge")' in state_js
    assert 'readiness_list: el("readiness_list")' in state_js
    assert '"/system/readiness"' in readiness_js
