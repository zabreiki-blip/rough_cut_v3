from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import api
import api_stage4_routes
from atomic_io import atomic_write_json
from stage4.versioning import ensure_baseline


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
    return api.app.test_client()


def _block(uid: str, text: str) -> dict:
    return {
        "uid": uid,
        "normalized_text": text,
        "original_text": text,
        "type": "sync",
        "speaker": "Иван",
        "original_label": "Иван",
        "text_for_tts": "",
    }


# ---------------------------------------------------------------------------
# _active_project_job
# ---------------------------------------------------------------------------


def test_active_project_job_returns_empty_string_when_nothing_active(client) -> None:
    assert api_stage4_routes._active_project_job(api, "demo") == ""


# ---------------------------------------------------------------------------
# /stage4/inbox error branches
# ---------------------------------------------------------------------------


def test_stage4_inbox_rejects_invalid_project_name(client) -> None:
    response = client.get("/stage4/inbox", query_string={"project": "bad name!"})

    assert response.status_code == 400
    assert response.get_json()["ok"] is False


def test_stage4_inbox_reports_internal_error(client, monkeypatch: pytest.MonkeyPatch) -> None:
    api._ensure_project("demo")

    def boom(_project: str) -> dict:
        raise RuntimeError("boom")

    monkeypatch.setattr(api, "_discover_project_inbox", boom)

    response = client.get("/stage4/inbox?project=demo")

    assert response.status_code == 500
    assert response.get_json() == {"ok": False, "error": "internal server error"}


def test_stage4_revisions_list_reports_internal_error(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    api._ensure_project("demo")

    def boom(_paths) -> dict:
        raise RuntimeError("boom")

    monkeypatch.setattr(api_stage4_routes, "list_revisions", boom)

    response = client.get("/stage4/revisions?project=demo")

    assert response.status_code == 500
    assert response.get_json() == {"ok": False, "error": "internal server error"}


# ---------------------------------------------------------------------------
# POST /stage4/revisions/import
# ---------------------------------------------------------------------------


def test_stage4_revision_import_rejects_path_outside_project(
    client, tmp_path: Path
) -> None:
    api._ensure_project("demo")
    outside = tmp_path / "outside.docx"
    outside.write_bytes(b"outside")

    response = client.post(
        "/stage4/revisions/import",
        json={"project": "demo", "input_path": str(outside)},
    )

    assert response.status_code == 400
    assert response.get_json()["ok"] is False


def test_stage4_revision_import_blocked_by_active_job(client) -> None:
    api._ensure_project("demo")
    with api._TTS_JOBS_LOCK:
        api._TTS_ACTIVE_PROJECTS.add("demo")
    try:
        response = client.post("/stage4/revisions/import", json={"project": "demo"})
    finally:
        with api._TTS_JOBS_LOCK:
            api._TTS_ACTIVE_PROJECTS.discard("demo")

    assert response.status_code == 409
    assert "импорт" in response.get_json()["error"]


def test_stage4_revision_import_returns_400_when_no_baseline_exists(client) -> None:
    api._ensure_project("demo")

    response = client.post("/stage4/revisions/import", json={"project": "demo"})

    assert response.status_code == 400
    assert response.get_json()["ok"] is False


def test_stage4_revision_import_returns_409_on_state_conflict(client) -> None:
    paths = api._ensure_project("demo")
    atomic_write_json(paths["scenario_versions"] / "rev_0001.json", {
        "revision_id": "rev_0001",
    })
    atomic_write_json(paths["work"] / "active_revision.json", {
        "revision_id": "rev_0001",
    })
    atomic_write_json(paths["normalized"], {
        "scenario_revision_id": "rev_0002",
        "blocks": [],
    })

    response = client.post("/stage4/revisions/import", json={"project": "demo"})

    assert response.status_code == 409
    assert response.get_json()["ok"] is False


def test_stage4_revision_import_succeeds_and_returns_new_revision(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = api._ensure_project("demo")
    source_v1 = paths["input"] / "scenario.docx"
    source_v1.write_bytes(b"v1")
    atomic_write_json(paths["normalized"], {
        "source": str(source_v1),
        "source_hash": hashlib.sha256(b"v1").hexdigest(),
        "status": "confirmed",
        "blocks": [_block("stable-a", "Первая")],
    })
    source_v2 = paths["input"] / "v2.docx"
    source_v2.write_bytes(b"v2")

    def fake_generate(input_path, output_path, **_kwargs):
        value = {
            "source": str(input_path),
            "source_hash": hashlib.sha256(b"v2").hexdigest(),
            "status": "draft",
            "blocks": [_block("stable-a", "Первая")],
        }
        atomic_write_json(output_path, value)
        return value

    monkeypatch.setattr(api, "generate_normalization", fake_generate)

    response = client.post(
        "/stage4/revisions/import",
        json={"project": "demo", "input_path": str(source_v2)},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is True
    assert payload["result"]["revision"]["revision_id"] == "rev_0002"


# ---------------------------------------------------------------------------
# GET /stage4/diffs/<diff_id>
# ---------------------------------------------------------------------------


def test_stage4_diff_get_returns_stored_diff(client) -> None:
    paths = api._ensure_project("demo")
    diff_path = paths["scenario_diffs"] / "diff_1.json"
    diff_path.write_text(json.dumps({"summary": {"unchanged": 1}}), encoding="utf-8")

    response = client.get("/stage4/diffs/diff_1?project=demo")

    assert response.status_code == 200
    assert response.get_json()["result"]["summary"] == {"unchanged": 1}


def test_stage4_diff_get_rejects_invalid_id(client) -> None:
    api._ensure_project("demo")

    response = client.get("/stage4/diffs/bad%20id?project=demo")

    assert response.status_code == 404
    assert response.get_json()["error"] == "invalid diff id"


def test_stage4_diff_get_returns_404_when_missing(client) -> None:
    api._ensure_project("demo")

    response = client.get("/stage4/diffs/diff_missing?project=demo")

    assert response.status_code == 404
    assert response.get_json()["error"] == "diff not found"


# ---------------------------------------------------------------------------
# POST /stage4/revisions/<revision_id>/reconcile
# ---------------------------------------------------------------------------


def test_stage4_revision_reconcile_rejects_invalid_id_format(client) -> None:
    api._ensure_project("demo")

    response = client.post(
        "/stage4/revisions/not-a-valid-id/reconcile", json={"project": "demo"}
    )

    assert response.status_code == 400
    assert response.get_json()["ok"] is False


def test_stage4_revision_reconcile_returns_400_when_revision_missing(client) -> None:
    api._ensure_project("demo")

    response = client.post(
        "/stage4/revisions/rev_0001/reconcile", json={"project": "demo"}
    )

    assert response.status_code == 400
    assert response.get_json()["ok"] is False


def test_stage4_revision_reconcile_blocked_by_active_job(client) -> None:
    api._ensure_project("demo")
    with api._TTS_JOBS_LOCK:
        api._TTS_ACTIVE_PROJECTS.add("demo")
    try:
        response = client.post(
            "/stage4/revisions/rev_0001/reconcile", json={"project": "demo"}
        )
    finally:
        with api._TTS_JOBS_LOCK:
            api._TTS_ACTIVE_PROJECTS.discard("demo")

    assert response.status_code == 409
    assert "восстановлен" in response.get_json()["error"]


def test_stage4_revision_reconcile_restores_divergent_work_state(client) -> None:
    paths = api._ensure_project("demo")
    source = paths["input"] / "v1.docx"
    source.write_bytes(b"v1")
    atomic_write_json(paths["normalized"], {
        "source": str(source),
        "source_hash": hashlib.sha256(b"v1").hexdigest(),
        "status": "confirmed",
        "revision": 2,
        "blocks": [_block("one", "Первая")],
    })
    ensure_baseline("demo", paths)
    atomic_write_json(paths["normalized"], {
        "scenario_revision_id": "",
        "status": "draft",
        "revision": 1,
        "blocks": [_block("other", "Черновик")],
    })

    response = client.post(
        "/stage4/revisions/rev_0001/reconcile", json={"project": "demo"}
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is True
    assert payload["result"]["reconciled"] is True
