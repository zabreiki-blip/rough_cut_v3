"""Job runners create one CostBudget per job and hand it to the paid calls."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import patch

import pytest

import api
from cost_budget import CostBudget, CostBudgetExceeded

_ENV = {
    "ROUGH_CUT_MAX_USD_PER_JOB": "2",
    "ROUGH_CUT_CLOUD_ASR_USD_PER_MINUTE_ESTIMATE": "0.1",
    "ROUGH_CUT_LLM_USD_PER_1K_TOKENS_ESTIMATE": "0.5",
    "ROUGH_CUT_TTS_USD_PER_1K_CHARS_ESTIMATE": "0.25",
}


@pytest.fixture()
def job_env(monkeypatch: pytest.MonkeyPatch):
    for name, value in _ENV.items():
        monkeypatch.setenv(name, value)
    with TemporaryDirectory() as temporary:
        base = Path(temporary).resolve()
        projects = base / "projects"
        projects.mkdir()
        with (
            patch.object(api, "ROOT_DIR", base),
            patch.object(api, "PROJECTS_DIR", projects),
            patch.object(api, "NORMALIZATION_MEMORY_PATH", base / "data" / "memory.sqlite3"),
        ):
            api.app.config.update(TESTING=True)
            api.app.config.pop("API_TOKEN", None)
            yield base, projects


def _run_tts(job_id: str) -> dict[str, Any]:
    api._run_tts_job(
        job_id=job_id,
        project="demo",
        prepared_path=Path("unused-prepared.json"),
        speaker_dir=Path("unused-speaker"),
        provider=None,
        voice_name=None,
        gender=None,
        dry_run=False,
    )
    state = api._get_tts_job(job_id)
    assert state is not None
    return state


def test_tts_job_creates_budget_from_env_and_publishes_snapshot(job_env) -> None:
    assert api._reserve_tts_project("demo")
    created = api._create_tts_job("demo")
    seen: list[CostBudget | None] = []

    def fake_synthesize_tts(**kwargs: Any) -> dict[str, Any]:
        budget = kwargs.get("budget")
        seen.append(budget)
        assert budget is not None
        reservation = budget.reserve_tts_attempt("a" * 1000)
        budget.settle(reservation, None, estimate_if_unknown=True)
        return {"items_total": 0, "files_count": 0, "skipped_count": 0}

    with (
        patch.object(api, "synthesize_tts", side_effect=fake_synthesize_tts),
        patch.object(api, "_save_voiceover_revision_assets"),
    ):
        state = _run_tts(created["id"])

    assert state["status"] == "completed"
    assert isinstance(seen[0], CostBudget)
    assert seen[0].limit_usd == 2
    assert state["cost_budget"]["limit_usd"] == 2.0
    assert state["cost_budget"]["spent_usd"] == 0.25
    assert state["cost_budget"]["reserved_usd"] == 0.0


def test_tts_job_budget_exceeded_fails_job_with_clear_error(job_env) -> None:
    assert api._reserve_tts_project("demo")
    created = api._create_tts_job("demo")

    def fake_synthesize_tts(**kwargs: Any) -> dict[str, Any]:
        budget = kwargs["budget"]
        budget.reserve_tts_attempt("a" * 7000)  # 1.75 of 2 USD
        budget.reserve_tts_attempt("a" * 7000)  # exceeds the limit
        raise AssertionError("unreachable")

    with patch.object(api, "synthesize_tts", side_effect=fake_synthesize_tts):
        state = _run_tts(created["id"])

    assert state["status"] == "failed"
    assert "job cost limit of $2" in state["error"]
    assert state["cost_budget"]["reserved_usd"] == 1.75
    with api._TTS_JOBS_LOCK:
        assert "demo" not in api._TTS_ACTIVE_PROJECTS


def test_tts_job_without_cost_env_still_passes_accounting_budget(
    job_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in _ENV:
        monkeypatch.delenv(name)
    assert api._reserve_tts_project("demo")
    created = api._create_tts_job("demo")
    seen: list[CostBudget] = []

    def fake_synthesize_tts(**kwargs: Any) -> dict[str, Any]:
        seen.append(kwargs["budget"])
        return {"items_total": 0, "files_count": 0, "skipped_count": 0}

    with (
        patch.object(api, "synthesize_tts", side_effect=fake_synthesize_tts),
        patch.object(api, "_save_voiceover_revision_assets"),
    ):
        state = _run_tts(created["id"])

    assert state["status"] == "completed"
    assert seen[0].limit_usd == 0
    assert state["cost_budget"]["total_usd"] == 0.0


class _ImmediateThread:
    def __init__(self, *, target, kwargs, **_unused):
        self.target = target
        self.kwargs = kwargs

    def start(self):
        self.target(**self.kwargs)


def _start_normalization(client, projects: Path) -> dict[str, Any]:
    source = projects / "demo" / "input" / "scenario.txt"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("ДИКТОР: Первый закадровый блок.\n", encoding="utf-8")
    with patch.object(api.threading, "Thread", _ImmediateThread):
        response = client.post(
            "/stage1/normalization-jobs/start",
            json={"project": "demo", "input_path": str(source), "use_llm": True},
        )
    assert response.status_code == 202
    job_id = response.get_json()["result"]["id"]
    return client.get(f"/stage1/normalization-jobs/{job_id}").get_json()["result"]


def test_normalization_job_passes_budget_and_publishes_snapshot(job_env) -> None:
    _base, projects = job_env
    seen: list[Any] = []

    def fake_generate(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        budget = kwargs.get("budget")
        seen.append(budget)
        assert budget is not None
        reservation = budget.reserve_llm_attempt([{"role": "user", "content": "x" * 400}], 900)
        budget.settle(reservation, "0.125")
        return {"blocks": []}

    with patch.object(api, "generate_normalization", side_effect=fake_generate):
        job = _start_normalization(api.app.test_client(), projects)

    assert job["status"] == "completed"
    assert isinstance(seen[0], CostBudget)
    assert seen[0].limit_usd == 2
    assert job["cost_budget"]["spent_usd"] == 0.125


def test_normalization_job_budget_exceeded_fails_job_with_clear_error(job_env) -> None:
    _base, projects = job_env

    def fake_generate(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        raise CostBudgetExceeded("LLM request would exceed the job cost limit of $2")

    with patch.object(api, "generate_normalization", side_effect=fake_generate):
        job = _start_normalization(api.app.test_client(), projects)

    assert job["status"] == "failed"
    assert "job cost limit of $2" in job["error"]
    assert "cost_budget" in job
