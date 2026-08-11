from __future__ import annotations

from collections import defaultdict
import json
from pathlib import Path

import pytest

import api


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
    # Authentication is intentionally bypassed in tests unless API_TOKEN is set.
    monkeypatch.delitem(api.app.config, "API_TOKEN", raising=False)
    return api.app.test_client()


def test_public_route_and_http_method_contract() -> None:
    expected = {
        "/": {"GET"},
        "/health": {"GET"},
        "/project/<project>": {"GET"},
        "/projects": {"GET", "POST"},
        "/projects/<project>": {"DELETE"},
        "/speaker/<project>/<path:filename>": {"GET"},
        "/stage1/analyze": {"POST"},
        "/stage1/normalization": {"GET", "PUT"},
        "/stage1/normalization-jobs/<job_id>": {"GET"},
        "/stage1/normalization-jobs/start": {"POST"},
        "/stage1/normalization-jobs/status": {"GET"},
        "/stage1/normalization/confirm": {"POST"},
        "/stage1/normalization/confirm/preview": {"POST"},
        "/stage1/normalization/generate": {"POST"},
        "/stage1/normalization/memory": {"GET", "POST"},
        "/stage1/normalization/memory/<int:rule_id>": {"DELETE", "PATCH"},
        "/stage1/normalization/reset": {"POST"},
        "/stage1/status": {"GET"},
        "/stage1/tts": {"POST"},
        "/stage1/tts-jobs/<job_id>": {"GET"},
        "/stage1/tts-jobs/<job_id>/cancel": {"POST"},
        "/stage1/tts-jobs/start": {"POST"},
        "/stage1/tts-jobs/status": {"GET"},
        "/stage1/tts-phrase": {"POST"},
        "/stage1/validate": {"POST"},
        "/stage2/build": {"POST"},
        "/stage2/export/xml": {"GET"},
        "/stage2/sync-jobs/<job_id>": {"GET"},
        "/stage2/sync-jobs/<job_id>/cancel": {"POST"},
        "/stage2/sync-jobs/start": {"POST"},
        "/stage2/sync-jobs/status": {"GET"},
        "/stage2/sync-matches": {"GET", "PATCH"},
        "/stage2/sync-matches/<block_uid>": {"PATCH"},
        "/stage2/sync-sources": {"DELETE", "GET", "POST"},
        "/stage2/sync-sources/<source_id>/audio": {"GET", "PUT"},
        "/stage2/sync-sources/<source_id>/words": {"PUT"},
        "/stage2/timeline": {"GET"},
        "/stage3/media/scan": {"POST"},
        "/stage3/timeline/prepare": {"POST"},
        "/stage3/jobs/scan/start": {"POST"},
        "/stage3/jobs/prepare/start": {"POST"},
        "/stage3/jobs/<job_id>": {"GET"},
        "/stage3/jobs/status": {"GET"},
        "/stage3/export/xml": {"GET"},
        "/stage4/revisions": {"GET"},
        "/stage4/inbox": {"GET"},
        "/stage4/revisions/import": {"POST"},
        "/stage4/revisions/<revision_id>/activate": {"POST"},
        "/stage4/revisions/<revision_id>/reconcile": {"POST"},
        "/stage4/diffs/<diff_id>": {"GET"},
    }
    actual: dict[str, set[str]] = defaultdict(set)
    for rule in api.app.url_map.iter_rules():
        if rule.endpoint == "static":
            continue
        actual[rule.rule].update(rule.methods - {"HEAD", "OPTIONS"})

    assert dict(actual) == expected


def test_extracted_views_keep_legacy_api_module_exports() -> None:
    for endpoint in (
        "health",
        "project_home",
        "stage1_analyze",
        "stage1_normalization_generate",
        "stage1_tts_job_start",
        "stage1_validate",
    ):
        assert getattr(api, endpoint) is api.app.view_functions[endpoint]


def test_health_response_contract(client) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.is_json
    payload = response.get_json()
    assert payload["ok"] is True
    assert payload["status"] == "healthy"
    assert isinstance(payload["configuration_warnings"], list)
    assert set(payload) == {"ok", "status", "configuration_warnings"}


def test_project_collection_response_contract(client) -> None:
    assert client.get("/projects").get_json() == {"ok": True, "projects": []}

    created = client.post("/projects", json={"project": "Монтаж_01"})
    assert created.status_code == 200
    assert created.get_json() == {
        "ok": True,
        "project": "Монтаж_01",
        "url": "/project/Монтаж_01",
    }
    assert client.get("/projects").get_json() == {
        "ok": True,
        "projects": ["Монтаж_01"],
    }

    deleted = client.delete("/projects/Монтаж_01")
    assert deleted.status_code == 200
    assert deleted.get_json() == {"ok": True, "project": "Монтаж_01"}


def test_empty_stage_responses_keep_public_envelopes(client) -> None:
    status = client.get("/stage1/status?project=demo")
    assert status.status_code == 200
    status_payload = status.get_json()
    assert status_payload["ok"] is True
    assert status_payload["result"]["project"] == "demo"
    assert set(status_payload) == {"ok", "result"}

    sources = client.get("/stage2/sync-sources?project=demo")
    assert sources.status_code == 200
    assert sources.get_json() == {
        "ok": True,
        "result": {"project": "demo", "sources": []},
    }

    matches = client.get("/stage2/sync-matches?project=demo")
    assert matches.status_code == 200
    assert matches.get_json() == {
        "ok": True,
        "result": {
            "project": "demo",
            "matches": [],
            "auto_matched": 0,
            "confirmed": 0,
            "needs_review": 0,
            "not_found": 0,
            "matched": 0,
            "review": 0,
            "unmatched": 0,
        },
    }


def test_stage4_inbox_lists_friendly_scenario_and_review_files(client, tmp_path: Path) -> None:
    assert client.post("/projects", json={"project": "demo"}).status_code == 200
    project = tmp_path / "projects" / "demo" / "ЗАГРУЗИТЬ"
    (project / "Сценарии" / "Редакция 2.docx").write_bytes(b"scenario-v2")
    (project / "Просмотровки" / "Интервью.wav").write_bytes(b"audio")

    response = client.get("/stage4/inbox?project=demo")

    assert response.status_code == 200
    result = response.get_json()["result"]
    assert [item["name"] for item in result["scenario_files"]] == ["Редакция 2.docx"]
    assert [item["name"] for item in result["review_files"]] == ["Интервью.wav"]


def test_stage4_activation_is_blocked_while_project_tts_is_running(client) -> None:
    with api._TTS_JOBS_LOCK:
        api._TTS_ACTIVE_PROJECTS.add("demo")
    try:
        response = client.post(
            "/stage4/revisions/rev_0001/activate",
            json={"project": "demo"},
        )
    finally:
        with api._TTS_JOBS_LOCK:
            api._TTS_ACTIVE_PROJECTS.discard("demo")

    assert response.status_code == 409
    assert "озвучки" in response.get_json()["error"]


@pytest.mark.parametrize(
    ("lock_name", "active_name", "job_label"),
    [
        ("_NORMALIZATION_JOBS_LOCK", "_NORMALIZATION_ACTIVE_PROJECTS", "нормализации"),
        ("_SYNC_JOBS_LOCK", "_SYNC_ACTIVE_PROJECTS", "сопоставления СНХ"),
        ("_STAGE3_JOBS_LOCK", "_STAGE3_ACTIVE_PROJECTS", "подготовки Stage 3"),
    ],
)
def test_stage4_activation_is_blocked_by_other_mutating_jobs(
    client, lock_name: str, active_name: str, job_label: str
) -> None:
    lock = getattr(api, lock_name)
    active = getattr(api, active_name)
    with lock:
        active.add("demo")
    try:
        response = client.post(
            "/stage4/revisions/rev_0001/activate",
            json={"project": "demo"},
        )
    finally:
        with lock:
            active.discard("demo")

    assert response.status_code == 409
    assert job_label in response.get_json()["error"]


def test_stage4_mismatched_work_state_is_reported_and_activation_is_blocked(
    client,
) -> None:
    paths = api._ensure_project("demo")
    (paths["scenario_versions"] / "rev_0001.json").write_text(
        json.dumps({"revision_id": "rev_0001"}), encoding="utf-8"
    )
    (paths["work"] / "active_revision.json").write_text(
        json.dumps({"revision_id": "rev_0001"}), encoding="utf-8"
    )
    paths["normalized"].write_text(
        json.dumps({"scenario_revision_id": "rev_0002", "blocks": []}),
        encoding="utf-8",
    )

    listed = client.get("/stage4/revisions?project=demo")
    activated = client.post(
        "/stage4/revisions/rev_0001/activate", json={"project": "demo"}
    )

    integrity = listed.get_json()["result"]["integrity"]
    assert integrity["status"] == "active_work_mismatch"
    assert integrity["blocking"] is True
    assert activated.status_code == 409


@pytest.mark.parametrize(
    ("url", "error"),
    [
        ("/stage1/normalization-jobs/missing", "normalization job not found"),
        ("/stage1/tts-jobs/missing", "job not found"),
        ("/stage2/sync-jobs/missing", "job not found"),
    ],
)
def test_unknown_job_error_contract(client, url: str, error: str) -> None:
    response = client.get(url)

    assert response.status_code == 404
    assert response.get_json() == {"ok": False, "error": error}


def test_missing_timeline_error_contract(client) -> None:
    response = client.get("/stage2/timeline?project=demo")

    assert response.status_code == 404
    assert response.get_json() == {
        "ok": False,
        "error": "full_timeline.json not found — run Build first",
    }
