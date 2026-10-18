"""Per-request LLM/TTS cost budget on synchronous routes (D24)."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

import api
from atomic_io import atomic_write_json
from cost_budget import CostBudget
from stage4.versioning import ensure_baseline, import_revision


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
    for name in (
        "ROUGH_CUT_MAX_USD_PER_JOB",
        "ROUGH_CUT_LLM_USD_PER_1K_TOKENS_ESTIMATE",
        "ROUGH_CUT_TTS_USD_PER_1K_CHARS_ESTIMATE",
    ):
        monkeypatch.delenv(name, raising=False)
    return api.app.test_client()


@pytest.fixture()
def http_post(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    post = MagicMock(name="requests.post")
    monkeypatch.setattr("requests.post", post)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    return post


def _tight_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """A limit that any positive-rate estimate exceeds."""
    monkeypatch.setenv("ROUGH_CUT_MAX_USD_PER_JOB", "0.0001")
    monkeypatch.setenv("ROUGH_CUT_LLM_USD_PER_1K_TOKENS_ESTIMATE", "100")
    monkeypatch.setenv("ROUGH_CUT_TTS_USD_PER_1K_CHARS_ESTIMATE", "100")


def _scenario(project: str = "demo") -> Path:
    paths = api._ensure_project(project)
    source = paths["input"] / "scenario.txt"
    source.write_text("ДИКТОР: Первый закадровый блок.\n", encoding="utf-8")
    return source


def _bypass_tts_gates(monkeypatch: pytest.MonkeyPatch, project: str = "demo") -> Path:
    paths = api._ensure_project(project)
    atomic_write_json(paths["prepared"], {"items": [{"id": "a", "text": "x"}]})
    atomic_write_json(paths["normalized"], {"scenario_revision_id": ""})
    monkeypatch.setattr(api, "_normalization_required", lambda *_a, **_k: None)
    monkeypatch.setattr(api, "_prepared_projection_required", lambda *_a, **_k: None)
    monkeypatch.setattr(api, "_confirmed_prepared_path", lambda *_a, **_k: paths["prepared"])
    return paths["prepared"]


# --- legacy analyze --------------------------------------------------------


def test_legacy_analyze_over_limit_returns_409_without_http(
    client, monkeypatch: pytest.MonkeyPatch, http_post: MagicMock
) -> None:
    _tight_limit(monkeypatch)
    source = _scenario()

    response = client.post(
        "/stage1/analyze",
        json={"project": "demo", "input_path": str(source), "use_llm": True},
    )

    assert response.status_code == 409
    assert response.get_json()["ok"] is False
    assert "cost limit" in response.get_json()["error"]
    http_post.assert_not_called()


def test_legacy_analyze_passes_one_budget(client, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[Any] = []

    def fake_analyze(**kwargs):
        seen.append(kwargs.get("budget"))
        return {"blocks_count": 1}

    monkeypatch.setattr(api, "analyze_script", fake_analyze)
    source = _scenario()

    response = client.post(
        "/stage1/analyze",
        json={"project": "demo", "input_path": str(source), "use_llm": False},
    )

    assert response.status_code == 200
    assert isinstance(seen[0], CostBudget)
    assert seen[0].limit_usd == 0


# --- sync normalization/generate ------------------------------------------


def test_sync_normalization_over_limit_returns_409_without_http(
    client, monkeypatch: pytest.MonkeyPatch, http_post: MagicMock
) -> None:
    _tight_limit(monkeypatch)
    source = _scenario()

    def fake_generate(input_path, output_path, *, budget=None, **_kwargs):
        # Same reservation the real LLM client performs before any HTTP call.
        budget.reserve_llm_attempt([{"role": "user", "content": "текст"}], 100)
        raise AssertionError("must not be reached")

    monkeypatch.setattr(api, "generate_normalization", fake_generate)

    response = client.post(
        "/stage1/normalization/generate",
        json={"project": "demo", "input_path": str(source), "use_llm": True},
    )

    assert response.status_code == 409
    assert "cost limit" in response.get_json()["error"]
    http_post.assert_not_called()


def test_sync_normalization_passes_budget(client, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[Any] = []

    def fake_generate(input_path, output_path, **kwargs):
        seen.append(kwargs.get("budget"))
        return {"blocks": []}

    monkeypatch.setattr(api, "generate_normalization", fake_generate)
    source = _scenario()

    response = client.post(
        "/stage1/normalization/generate",
        json={"project": "demo", "input_path": str(source), "use_llm": False},
    )

    assert response.status_code == 200
    assert response.get_json()["result"] == {"blocks": []}
    assert isinstance(seen[0], CostBudget)


# --- sync tts/generate -----------------------------------------------------


def test_sync_tts_over_limit_returns_409_without_http(
    client, monkeypatch: pytest.MonkeyPatch, http_post: MagicMock
) -> None:
    _tight_limit(monkeypatch)
    _bypass_tts_gates(monkeypatch)

    def fake_tts(*, budget=None, **_kwargs):
        budget.reserve_tts_attempt("текст для озвучки")
        raise AssertionError("must not be reached")

    monkeypatch.setattr(api, "synthesize_tts", fake_tts)

    response = client.post("/stage1/tts", json={"project": "demo", "provider": "openrouter"})

    assert response.status_code == 409
    assert "cost limit" in response.get_json()["error"]
    http_post.assert_not_called()
    assert not api._TTS_ACTIVE_PROJECTS


def test_sync_tts_passes_budget(client, monkeypatch: pytest.MonkeyPatch) -> None:
    _bypass_tts_gates(monkeypatch)
    seen: list[Any] = []

    def fake_tts(**kwargs):
        seen.append(kwargs.get("budget"))
        return {"items": []}

    monkeypatch.setattr(api, "synthesize_tts", fake_tts)
    monkeypatch.setattr(api, "_save_voiceover_revision_assets", lambda *_a, **_k: None)

    response = client.post("/stage1/tts", json={"project": "demo", "dry_run": True})

    assert response.status_code == 200
    assert isinstance(seen[0], CostBudget)
    assert seen[0].limit_usd == 0


# --- tts phrase (real synthesize_phrase: budget reaches the HTTP layer) ------


def test_tts_phrase_over_limit_returns_409_without_http(
    client, monkeypatch: pytest.MonkeyPatch, http_post: MagicMock
) -> None:
    _tight_limit(monkeypatch)
    api._ensure_project("demo")

    response = client.post(
        "/stage1/tts-phrase",
        json={"project": "demo", "text": "Привет, мир", "provider": "openrouter"},
    )

    assert response.status_code == 409
    assert "cost limit" in response.get_json()["error"]
    http_post.assert_not_called()
    assert not api._TTS_ACTIVE_PROJECTS


def test_tts_phrase_passes_budget(client, monkeypatch: pytest.MonkeyPatch) -> None:
    api._ensure_project("demo")
    seen: list[Any] = []

    def fake_phrase(**kwargs):
        seen.append(kwargs.get("budget"))
        return {"file": ""}

    monkeypatch.setattr(api, "synthesize_phrase", fake_phrase)

    response = client.post("/stage1/tts-phrase", json={"project": "demo", "text": "Привет"})

    assert response.status_code == 200
    assert isinstance(seen[0], CostBudget)


# --- invalid budget configuration: fail closed ------------------------------


@pytest.mark.parametrize(
    ("path", "payload", "service"),
    [
        ("/stage1/analyze", {"use_llm": True}, "analyze_script"),
        ("/stage1/normalization/generate", {"use_llm": True}, "generate_normalization"),
        ("/stage1/tts", {"provider": "openrouter"}, "synthesize_tts"),
        ("/stage1/tts-phrase", {"text": "Привет", "provider": "openrouter"}, "synthesize_phrase"),
    ],
)
def test_invalid_budget_config_fails_closed_with_400(
    client, monkeypatch: pytest.MonkeyPatch, http_post: MagicMock, path, payload, service
) -> None:
    monkeypatch.setenv("ROUGH_CUT_MAX_USD_PER_JOB", "not-a-number")
    _bypass_tts_gates(monkeypatch)
    paid = MagicMock(name=service)
    monkeypatch.setattr(api, service, paid)

    response = client.post(path, json={"project": "demo", **payload})

    assert response.status_code == 400
    assert "ROUGH_CUT_MAX_USD_PER_JOB" in response.get_json()["error"]
    paid.assert_not_called()
    http_post.assert_not_called()
    assert not api._TTS_ACTIVE_PROJECTS


# --- stage4 revision import (versioning.generate_normalization) --------------


def _baseline_project(client_paths: dict[str, Path]) -> Path:
    source_v1 = client_paths["input"] / "scenario.docx"
    source_v1.write_bytes(b"v1")
    block = {
        "uid": "stable-a",
        "normalized_text": "Первая",
        "original_text": "Первая",
        "type": "sync",
        "speaker": "Иван",
        "original_label": "Иван",
        "text_for_tts": "",
    }
    atomic_write_json(
        client_paths["normalized"],
        {
            "source": str(source_v1),
            "source_hash": hashlib.sha256(b"v1").hexdigest(),
            "status": "confirmed",
            "blocks": [block],
        },
    )
    source_v2 = client_paths["input"] / "v2.docx"
    source_v2.write_bytes(b"v2")
    return source_v2


def test_import_revision_forwards_budget_to_generate_normalization(client, tmp_path: Path) -> None:
    paths = api._ensure_project("demo")
    source_v2 = _baseline_project(paths)
    ensure_baseline("demo", paths)
    budget = CostBudget.for_llm_tts({})
    seen: list[Any] = []

    def fake_generate(input_path, output_path, **kwargs):
        seen.append(kwargs.get("budget"))
        value = {
            "source": str(input_path),
            "source_hash": hashlib.sha256(b"v2").hexdigest(),
            "status": "draft",
            "blocks": [],
        }
        atomic_write_json(output_path, value)
        return value

    import_revision(
        "demo",
        paths,
        generate_normalization=fake_generate,
        memory_path=tmp_path / "memory.sqlite3",
        input_path=source_v2,
        budget=budget,
    )

    assert seen == [budget]


def test_stage4_import_over_limit_returns_409_without_http(
    client, monkeypatch: pytest.MonkeyPatch, http_post: MagicMock
) -> None:
    _tight_limit(monkeypatch)
    paths = api._ensure_project("demo")
    source_v2 = _baseline_project(paths)

    def fake_generate(input_path, output_path, *, budget=None, **_kwargs):
        budget.reserve_llm_attempt([{"role": "user", "content": "текст"}], 100)
        raise AssertionError("must not be reached")

    monkeypatch.setattr(api, "generate_normalization", fake_generate)

    response = client.post(
        "/stage4/revisions/import",
        json={"project": "demo", "input_path": str(source_v2), "use_llm": True},
    )

    assert response.status_code == 409
    assert "cost limit" in response.get_json()["error"]
    http_post.assert_not_called()


def test_stage4_import_invalid_budget_config_is_400(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ROUGH_CUT_LLM_USD_PER_1K_TOKENS_ESTIMATE", "-1")
    paths = api._ensure_project("demo")
    source_v2 = _baseline_project(paths)
    paid = MagicMock()
    monkeypatch.setattr(api, "generate_normalization", paid)

    response = client.post(
        "/stage4/revisions/import",
        json={"project": "demo", "input_path": str(source_v2), "use_llm": True},
    )

    assert response.status_code == 400
    assert "ROUGH_CUT_LLM_USD_PER_1K_TOKENS_ESTIMATE" in response.get_json()["error"]
    paid.assert_not_called()
