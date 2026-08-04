from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import threading

from flask import Flask, jsonify
import pytest

import api_stage3_routes
from stage2.sync_models import SyncSource
from stage2.sync_storage import SyncMatchStore, SyncSourceStore


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    app = Flask(__name__)
    app.config.update(TESTING=True)
    project_root = tmp_path / "projects" / "BRIK"
    project_root.mkdir(parents=True)
    paths = {
        "root": project_root,
        "sync_sources": project_root / "work" / "sync_sources.json",
        "sync_matches": project_root / "work" / "sync_matches.json",
        "work": project_root / "work",
    }
    services = SimpleNamespace(
        PROJECTS_DIR=tmp_path / "projects",
        _resolve_project_name=lambda value: str(value or "default"),
        _ensure_project=lambda _project: paths,
        _sync_stores=lambda current: (
            SyncSourceStore(current["sync_sources"]),
            SyncMatchStore(current["sync_matches"]),
        ),
        SyncSource=SyncSource,
        _SYNC_DATA_LOCK=threading.RLock(),
        _json_error=lambda message, status=400: (jsonify({"ok": False, "error": message}), status),
        _internal_error=lambda _exc: (jsonify({"ok": False, "error": "internal server error"}), 500),
    )
    app.config["TEST_STAGE3_SERVICES"] = services
    api_stage3_routes.register_stage3_routes(app, services)
    return app.test_client(), app, monkeypatch


def test_scan_api_uses_project_review_directory_automatically(client) -> None:
    http, app, monkeypatch = client
    captured = {}

    def fake_scan(directory, **kwargs):
        captured.update(directory=directory, **kwargs)
        return {"directory": "/review-media/Брик", "relative_directory": "Брик", "recursive": True, "sources": []}

    monkeypatch.setattr(api_stage3_routes, "scan_media_directory", fake_scan)
    response = http.post(
        "/stage3/media/scan",
        json={
            "project": "BRIK",
            "recursive": True,
        },
    )

    assert response.status_code == 200
    assert response.get_json()["result"]["project"] == "BRIK"
    assert response.get_json()["result"]["registered_sources"] == 0
    assert Path(captured["directory"]) == captured["media_root"] / "media" / "reviews"
    assert captured["recursive"] is True
    assert captured["media_root"].name == "BRIK"


def test_scan_registers_viewing_as_headless_whisper_source(client) -> None:
    http, app, monkeypatch = client
    services = app.config["TEST_STAGE3_SERVICES"]

    monkeypatch.setattr(api_stage3_routes, "scan_media_directory", lambda *_a, **_k: {
        "directory": "/app/projects/BRIK/media/reviews",
        "relative_directory": "media/reviews",
        "recursive": False,
        "sources": [{
            "source_id": "media_123",
            "fingerprint": "abc",
            "name": "Курская.mp4",
            "relative_path": "media/reviews/Курская.mp4",
            "container_path": "/app/projects/BRIK/media/reviews/Курская.mp4",
            "duration_sec": 123.5,
            "fps": 25.0,
        }],
    })

    response = http.post("/stage3/media/scan", json={"project": "BRIK"})

    assert response.status_code == 200
    assert response.get_json()["result"]["registered_sources"] == 1
    source_store, _match_store = services._sync_stores(services._ensure_project("BRIK"))
    source = source_store.get("media_123")
    assert source is not None
    assert source.source_kind == "review_media"
    assert source.guide_audio == "media/reviews/Курская.mp4"
    assert source.speaker == "Курская"
    assert source.all_speakers is False


@pytest.mark.parametrize(
    ("payload", "error"),
    [
        (None, "JSON object is required"),
        ({"project": "BRIK", "directory": "/review-media", "recursive": "yes"}, "recursive must be a boolean"),
    ],
)
def test_scan_api_validates_payload(client, payload, error: str) -> None:
    http, _app, _monkeypatch = client
    response = http.post("/stage3/media/scan", json=payload)
    assert response.status_code == 400
    assert response.get_json() == {"ok": False, "error": error}


def test_scan_api_returns_catalog_validation_error(client) -> None:
    http, _app, monkeypatch = client

    def reject(*_args, **_kwargs):
        raise api_stage3_routes.MediaCatalogError("directory resolves outside the configured media root")

    monkeypatch.setattr(api_stage3_routes, "scan_media_directory", reject)
    response = http.post(
        "/stage3/media/scan",
        json={"project": "BRIK", "directory": "/review-media/link"},
    )
    assert response.status_code == 400
    assert "outside" in response.get_json()["error"]


def test_project_root_symlink_escape_is_rejected(client, tmp_path: Path) -> None:
    http, app, _monkeypatch = client
    services = app.config["TEST_STAGE3_SERVICES"]
    outside = tmp_path / "outside"
    outside.mkdir()
    services._ensure_project = lambda _project: {"root": outside}

    response = http.post("/stage3/media/scan", json={"project": "BRIK"})

    assert response.status_code == 400
    assert "outside the projects root" in response.get_json()["error"]
