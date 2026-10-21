from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import json
import re
import threading

from flask import Flask, jsonify
import pytest

import api_stage3_routes
from stage2.sync_models import SyncMatch, SyncSource
from stage2.sync_storage import SyncMatchStore, SyncSourceStore
from stage_common import sync_matches_fingerprint


class _ImmediateThread:
    def __init__(self, *, target, args=(), kwargs=None, daemon=None):
        self.target = target
        self.args = args
        self.kwargs = kwargs or {}

    def start(self):
        self.target(*self.args, **self.kwargs)


class _HoldingThread(_ImmediateThread):
    instances = []

    def start(self):
        self.instances.append(self)


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
        _project_paths=lambda _project: paths,
        _sync_stores=lambda current: (
            SyncSourceStore(current["sync_sources"]),
            SyncMatchStore(current["sync_matches"]),
        ),
        SyncSource=SyncSource,
        _SYNC_DATA_LOCK=threading.RLock(),
        _normalization_required=lambda _project: None,
        _confirmed_normalization=lambda _project: ({"revision": "norm-1"}, ""),
        _json_error=lambda message, status=400: (jsonify({"ok": False, "error": message}), status),
        _internal_error=lambda _exc: (
            jsonify({"ok": False, "error": "internal server error"}),
            500,
        ),
        register_job_registry=lambda label, lock, active_projects: None,
    )
    app.config["TEST_STAGE3_SERVICES"] = services
    api_stage3_routes.register_stage3_routes(app, services)
    return app.test_client(), app, monkeypatch


def test_prepare_uses_legacy_export_when_project_has_no_viewings(client) -> None:
    http, app, monkeypatch = client
    services = app.config["TEST_STAGE3_SERVICES"]
    paths = services._ensure_project("BRIK")
    paths["work"].mkdir(parents=True, exist_ok=True)
    (paths["work"] / "full_timeline.json").write_text(
        json.dumps(
            {
                "project": "BRIK",
                "fps": 25,
                "width": 1920,
                "height": 1080,
                "normalization_revision": "norm-1",
                "segments": [{"id": "snh1", "type": "snh"}],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        api_stage3_routes,
        "scan_media_directory",
        lambda *_a, **_k: {
            "directory": "/app/projects/BRIK/media/reviews",
            "relative_directory": "media/reviews",
            "recursive": False,
            "sources": [],
        },
    )

    response = http.post("/stage3/timeline/prepare", json={"project": "BRIK"})

    assert response.status_code == 200
    result = response.get_json()["result"]
    assert result["format"] == "legacy"
    assert result["download_url"] == "/stage2/export/xml?project=BRIK"
    assert result["placeholder_segments"] == 1


def test_prepare_rejects_timeline_from_another_scenario_revision(client) -> None:
    http, app, monkeypatch = client
    services = app.config["TEST_STAGE3_SERVICES"]
    services._confirmed_normalization = lambda _project: (
        {
            "revision": "norm-1",
            "scenario_revision_id": "rev_0002",
        },
        "",
    )
    paths = services._ensure_project("BRIK")
    paths["work"].mkdir(parents=True, exist_ok=True)
    (paths["work"] / "full_timeline.json").write_text(
        json.dumps(
            {
                "project": "BRIK",
                "normalization_revision": "norm-1",
                "scenario_revision_id": "rev_0001",
                "segments": [],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        api_stage3_routes,
        "scan_media_directory",
        lambda *_a, **_k: {
            "sources": [],
        },
    )

    response = http.post("/stage3/timeline/prepare", json={"project": "BRIK"})

    assert response.status_code == 409
    assert "timeline_rebuild_required" in response.get_json()["error"]


def test_stage3_export_rejects_other_revision_and_accepts_legacy_identity(client) -> None:
    http, app, _monkeypatch = client
    services = app.config["TEST_STAGE3_SERVICES"]
    services._confirmed_normalization = lambda _project: (
        {
            "revision": "norm-1",
            "scenario_revision_id": "rev_0002",
        },
        "",
    )
    paths = services._ensure_project("BRIK")
    paths["work"].mkdir(parents=True, exist_ok=True)
    (paths["work"] / "BRIK_nested_timeline.xml").write_text("<xml/>", encoding="utf-8")
    timeline_path = paths["work"] / "full_timeline.json"
    timeline_path.write_text(
        json.dumps(
            {
                "normalization_revision": "norm-1",
                "scenario_revision_id": "rev_0001",
                "segments": [],
            }
        ),
        encoding="utf-8",
    )

    assert http.get("/stage3/export/xml?project=BRIK").status_code == 409

    timeline_path.write_text(
        json.dumps(
            {
                "normalization_revision": "norm-1",
                "segments": [],
            }
        ),
        encoding="utf-8",
    )
    exported = http.get("/stage3/export/xml?project=BRIK")
    assert exported.status_code == 200
    assert re.search(
        r'filename="?BRIK_V0_\d{8}_\d{6}\.xml"?',
        exported.headers["Content-Disposition"],
    )

    timeline_path.write_text(
        json.dumps(
            {
                "normalization_revision": "norm-1",
                "scenario_revision_id": "rev_0002",
                "segments": [],
            }
        ),
        encoding="utf-8",
    )
    (paths["work"] / "stage3_source_mapping.json").write_text(
        json.dumps(
            {
                "normalization_revision": "norm-1",
                "scenario_revision_id": "rev_0001",
            }
        ),
        encoding="utf-8",
    )
    assert http.get("/stage3/export/xml?project=BRIK").status_code == 409


def test_stage3_rejects_timeline_built_before_sync_matches_changed(client) -> None:
    http, app, monkeypatch = client
    services = app.config["TEST_STAGE3_SERVICES"]
    paths = services._ensure_project("BRIK")
    paths["work"].mkdir(parents=True, exist_ok=True)
    (paths["work"] / "BRIK_nested_timeline.xml").write_text("<xml/>", encoding="utf-8")
    monkeypatch.setattr(
        api_stage3_routes, "scan_media_directory", lambda *_a, **_k: {"sources": []}
    )
    match = SyncMatch(
        segment_id="uid-1",
        query_text="q",
        source_id="s1",
        source_in_sec=1.0,
        source_out_sec=2.0,
        status="confirmed",
        metadata={"normalization_revision": "norm-1"},
    )
    store = SyncMatchStore(paths["sync_matches"])
    store.replace_all([match])
    built_fingerprint = sync_matches_fingerprint([match])
    timeline_path = paths["work"] / "full_timeline.json"
    timeline_path.write_text(
        json.dumps(
            {
                "normalization_revision": "norm-1",
                "sync_matches_fingerprint": built_fingerprint,
                "segments": [],
            }
        ),
        encoding="utf-8",
    )
    assert http.get("/stage3/export/xml?project=BRIK").status_code == 200

    # A sync/retime pass flags the confirmed match after the timeline was built.
    match.metadata["retime_pending_review"] = True
    store.replace_all([match])
    exported = http.get("/stage3/export/xml?project=BRIK")
    assert exported.status_code == 409
    assert "timeline_rebuild_required" in exported.get_json()["error"]
    prepared = http.post("/stage3/timeline/prepare", json={"project": "BRIK"})
    assert prepared.status_code == 409
    assert "timeline_rebuild_required" in prepared.get_json()["error"]


def test_prepare_versioned_nested_export_persists_its_filename_for_download(client) -> None:
    http, app, monkeypatch = client
    services = app.config["TEST_STAGE3_SERVICES"]
    services._confirmed_normalization = lambda _project: (
        {"revision": "norm-1", "scenario_revision_id": "rev_0002"},
        "",
    )
    paths = services._ensure_project("BRIK")
    paths["work"].mkdir(parents=True, exist_ok=True)
    (paths["work"] / "full_timeline.json").write_text(
        json.dumps(
            {
                "project": "BRIK",
                "normalization_revision": "norm-1",
                "scenario_revision_id": "rev_0002",
                "segments": [],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        api_stage3_routes,
        "_scan_project_media",
        lambda *_a, **_k: {
            "sources": [
                {
                    "source_id": "review-1",
                    "name": "review.mp4",
                    "relative_path": "media/reviews/review.mp4",
                }
            ]
        },
    )
    monkeypatch.setattr(api_stage3_routes, "build_nested_xml", lambda *_a: "<xml/>")
    monkeypatch.setattr(
        api_stage3_routes,
        "map_catalog_to_timeline",
        lambda *_a: SimpleNamespace(
            catalog={}, details={}, unmapped_source_ids=[], to_dict=lambda: {}
        ),
    )

    response = http.post("/stage3/timeline/prepare", json={"project": "BRIK"})

    assert response.status_code == 200
    filename = response.get_json()["result"]["filename"]
    assert re.fullmatch(r"BRIK_V2_\d{8}_\d{6}\.xml", filename)
    assert (paths["work"] / filename).read_text(encoding="utf-8") == "<xml/>"
    mapping = json.loads((paths["work"] / "stage3_source_mapping.json").read_text())
    assert mapping["nested_timeline_filename"] == filename
    downloaded = http.get("/stage3/export/xml?project=BRIK")
    assert downloaded.status_code == 200
    assert downloaded.data == b"<xml/>"
    assert re.search(
        r'filename="?BRIK_V2_\d{8}_\d{6}\.xml"?',
        downloaded.headers["Content-Disposition"],
    )


def test_scan_api_uses_project_review_directory_automatically(client) -> None:
    http, app, monkeypatch = client
    captured = []

    def fake_scan(directory, **kwargs):
        captured.append((directory, kwargs))
        return {
            "directory": "/review-media/Брик",
            "relative_directory": "Брик",
            "recursive": True,
            "sources": [],
        }

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
    assert len(captured) == 1
    directory, kwargs = captured[0]
    assert Path(directory) == kwargs["media_root"] / "media" / "reviews"
    assert kwargs["recursive"] is True


def test_scan_api_prefers_friendly_review_inbox_when_it_has_media(client) -> None:
    http, app, monkeypatch = client
    services = app.config["TEST_STAGE3_SERVICES"]
    paths = services._ensure_project("BRIK")
    inbox_reviews = paths["root"] / "ЗАГРУЗИТЬ" / "Просмотровки"
    inbox_reviews.mkdir(parents=True)
    (inbox_reviews / "Интервью.wav").write_bytes(b"audio")
    captured = []

    def fake_scan(directory, **kwargs):
        captured.append((directory, kwargs))
        return {
            "directory": directory,
            "relative_directory": "ЗАГРУЗИТЬ/Просмотровки",
            "recursive": False,
            "sources": [],
        }

    monkeypatch.setattr(api_stage3_routes, "scan_media_directory", fake_scan)

    response = http.post("/stage3/media/scan", json={"project": "BRIK"})

    assert response.status_code == 200
    assert [Path(directory) for directory, _kwargs in captured] == [
        inbox_reviews,
        paths["root"] / "media" / "reviews",
    ]
    assert all(kwargs["media_root"].name == "BRIK" for _directory, kwargs in captured)


def test_scan_api_keeps_legacy_source_when_inbox_media_is_added(client) -> None:
    http, app, monkeypatch = client
    services = app.config["TEST_STAGE3_SERVICES"]
    paths = services._ensure_project("BRIK")
    inbox_reviews = paths["root"] / "ЗАГРУЗИТЬ" / "Просмотровки"
    inbox_reviews.mkdir(parents=True)
    (inbox_reviews / "new.wav").write_bytes(b"audio")

    def fake_scan(directory, **_kwargs):
        relative = Path(directory).relative_to(paths["root"]).as_posix()
        source_id = "inbox" if relative.startswith("ЗАГРУЗИТЬ") else "legacy"
        return {
            "directory": directory,
            "relative_directory": relative,
            "recursive": False,
            "sources": [
                {
                    "source_id": source_id,
                    "fingerprint": source_id,
                    "name": f"{source_id}.wav",
                    "relative_path": f"{relative}/{source_id}.wav",
                    "container_path": f"/tmp/{source_id}.wav",
                    "duration_sec": 1.0,
                    "fps": None,
                    "width": None,
                    "height": None,
                    "media_kind": "audio",
                    "has_audio": True,
                }
            ],
        }

    monkeypatch.setattr(api_stage3_routes, "scan_media_directory", fake_scan)
    response = http.post("/stage3/media/scan", json={"project": "BRIK"})

    assert response.status_code == 200
    source_store, _match_store = services._sync_stores(paths)
    assert {source.id for source in source_store.list()} == {"inbox", "legacy"}


def test_scan_registers_viewing_as_headless_whisper_source(client) -> None:
    http, app, monkeypatch = client
    services = app.config["TEST_STAGE3_SERVICES"]

    monkeypatch.setattr(
        api_stage3_routes,
        "scan_media_directory",
        lambda *_a, **_k: {
            "directory": "/app/projects/BRIK/media/reviews",
            "relative_directory": "media/reviews",
            "recursive": False,
            "sources": [
                {
                    "source_id": "media_123",
                    "fingerprint": "abc",
                    "name": "Курская.mp4",
                    "relative_path": "media/reviews/Курская.mp4",
                    "container_path": "/app/projects/BRIK/media/reviews/Курская.mp4",
                    "duration_sec": 123.5,
                    "fps": 25.0,
                }
            ],
        },
    )

    response = http.post("/stage3/media/scan", json={"project": "BRIK"})

    assert response.status_code == 200
    assert response.get_json()["result"]["registered_sources"] == 1
    source_store, _match_store = services._sync_stores(services._ensure_project("BRIK"))
    source = source_store.get("media_123")
    assert source is not None
    assert source.source_kind == "review_media"
    assert source.guide_audio == "media/reviews/Курская.mp4"
    # The filename-derived name is only a technical label, not a confirmed
    # speaker identity — a newly scanned source must stay searchable for
    # every speaker until an operator sets the real name(s).
    assert source.speaker == ""
    assert source.speakers == ()
    assert source.all_speakers is False
    assert source.metadata["scanned_media_speaker_hint"] == "Курская"


def test_scan_rescan_does_not_overwrite_operator_configured_speakers(client) -> None:
    http, app, monkeypatch = client
    services = app.config["TEST_STAGE3_SERVICES"]
    scan_result = {
        "directory": "/app/projects/BRIK/media/reviews",
        "relative_directory": "media/reviews",
        "recursive": False,
        "sources": [
            {
                "source_id": "media_123",
                "fingerprint": "abc",
                "name": "Курская.mp4",
                "relative_path": "media/reviews/Курская.mp4",
                "container_path": "/app/projects/BRIK/media/reviews/Курская.mp4",
                "duration_sec": 123.5,
                "fps": 25.0,
            }
        ],
    }
    monkeypatch.setattr(
        api_stage3_routes, "scan_media_directory", lambda *_a, **_k: dict(scan_result)
    )

    first = http.post("/stage3/media/scan", json={"project": "BRIK"})
    assert first.status_code == 200

    paths = services._ensure_project("BRIK")
    source_store, _match_store = services._sync_stores(paths)
    configured = source_store.get("media_123")
    source_store.save(
        SyncSource.from_dict(
            {
                **configured.to_dict(),
                "speaker": "Ирина Курская",
                "speakers": ["Пётр Иванов"],
                "all_speakers": False,
            }
        )
    )

    second = http.post("/stage3/media/scan", json={"project": "BRIK"})
    assert second.status_code == 200

    rescanned = source_store.get("media_123")
    assert rescanned.speaker == "Ирина Курская"
    assert rescanned.speakers == ("Пётр Иванов",)
    assert rescanned.all_speakers is False
    assert rescanned.metadata["scanned_media_speaker_hint"] == "Курская"


def test_scan_registered_source_stays_eligible_for_any_speaker(client) -> None:
    from stage2.sync_matcher import SyncMatcher
    from stage2.sync_models import AlignmentRequest, WordIndex, WordTimestamp

    http, app, monkeypatch = client
    services = app.config["TEST_STAGE3_SERVICES"]
    monkeypatch.setattr(
        api_stage3_routes,
        "scan_media_directory",
        lambda *_a, **_k: {
            "directory": "/app/projects/BRIK/media/reviews",
            "relative_directory": "media/reviews",
            "recursive": False,
            "sources": [
                {
                    "source_id": "media_kostel",
                    "fingerprint": "def",
                    "name": "KOSTEL_SP_001.mp4",
                    "relative_path": "media/reviews/KOSTEL_SP_001.mp4",
                    "container_path": "/app/projects/BRIK/media/reviews/KOSTEL_SP_001.mp4",
                    "duration_sec": 60.0,
                    "fps": 25.0,
                }
            ],
        },
    )

    response = http.post("/stage3/media/scan", json={"project": "BRIK"})
    assert response.status_code == 200
    paths = services._ensure_project("BRIK")
    source_store, _match_store = services._sync_stores(paths)
    source = source_store.get("media_kostel")
    assert source is not None

    rebuilt = SyncSource.from_dict(source.to_dict())
    index = WordIndex(
        source_id=rebuilt.id,
        words=[WordTimestamp("привет", 0.0, 0.5)],
    )
    matcher = SyncMatcher([rebuilt], [index])
    eligible, excluded = matcher._eligible_sources(
        AlignmentRequest(segment_id="seg-1", text="привет", speaker="Незнакомый Спикер")
    )

    assert [item.id for item in eligible] == [rebuilt.id]
    assert excluded == []


def test_scan_pairs_same_stem_video_and_wav(client) -> None:
    http, app, monkeypatch = client
    services = app.config["TEST_STAGE3_SERVICES"]
    base = "/app/projects/BRIK/media/reviews/Курская"
    monkeypatch.setattr(
        api_stage3_routes,
        "scan_media_directory",
        lambda *_a, **_k: {
            "directory": "/app/projects/BRIK/media/reviews",
            "relative_directory": "media/reviews",
            "recursive": False,
            "sources": [
                {
                    "source_id": "media_video",
                    "fingerprint": "video-fp",
                    "name": "Курская.mp4",
                    "relative_path": "media/reviews/Курская.mp4",
                    "container_path": f"{base}.mp4",
                    "duration_sec": 123.5,
                    "fps": 25.0,
                    "media_kind": "video",
                },
                {
                    "source_id": "media_audio",
                    "fingerprint": "audio-fp",
                    "name": "Курская.wav",
                    "relative_path": "media/reviews/Курская.wav",
                    "container_path": f"{base}.wav",
                    "duration_sec": 123.5,
                    "fps": None,
                    "media_kind": "audio",
                },
            ],
        },
    )

    response = http.post("/stage3/media/scan", json={"project": "BRIK"})

    assert response.status_code == 200
    assert response.get_json()["result"]["registered_sources"] == 1
    source_store, _match_store = services._sync_stores(services._ensure_project("BRIK"))
    source = source_store.get("media_video")
    assert source is not None
    assert source.guide_audio == "media/reviews/Курская.wav"
    assert source.media_path == "media/reviews/Курская.mp4"
    assert source.media_fingerprint == "audio-fp"
    assert source_store.get("media_audio") is None


def test_scan_binds_viewing_to_existing_normalized_premiere_source(client) -> None:
    http, app, monkeypatch = client
    services = app.config["TEST_STAGE3_SERVICES"]
    paths = services._ensure_project("BRIK")
    source_store, _match_store = services._sync_stores(paths)
    source_store.replace_all(
        [
            services.SyncSource.from_dict(
                {
                    "id": "ulynovsk_sinh",
                    "source_kind": "premiere_sequence",
                    "premiere_sequence": "ULINOVSK_sync",
                    "all_speakers": True,
                    "guide_audio": "work/sync_audio/ulynovsk_sinh.wav",
                    "duration_sec": 6060.96,
                }
            )
        ]
    )
    monkeypatch.setattr(
        api_stage3_routes,
        "scan_media_directory",
        lambda *_a, **_k: {
            "directory": "/app/projects/BRIK/media/reviews",
            "relative_directory": "media/reviews",
            "recursive": False,
            "sources": [
                {
                    "source_id": "media_new",
                    "fingerprint": "video-fp",
                    "name": "УЛЬЯНОВСК СНХ.mp4",
                    "relative_path": "media/reviews/УЛЬЯНОВСК СНХ.mp4",
                    "container_path": "/app/projects/BRIK/media/reviews/УЛЬЯНОВСК СНХ.mp4",
                    "duration_sec": 6060.96,
                    "fps": 25.0,
                    "media_kind": "video",
                }
            ],
        },
    )

    response = http.post("/stage3/media/scan", json={"project": "BRIK"})

    assert response.status_code == 200
    repeated_response = http.post("/stage3/media/scan", json={"project": "BRIK"})
    assert repeated_response.status_code == 200
    sources = source_store.list()
    assert [source.id for source in sources] == ["ulynovsk_sinh"]
    assert sources[0].source_kind == "review_media"
    assert sources[0].guide_audio == "work/sync_audio/ulynovsk_sinh.wav"
    assert sources[0].media_path == "media/reviews/УЛЬЯНОВСК СНХ.mp4"
    assert sources[0].all_speakers is True


def test_timeline_catalog_keeps_standalone_audio_but_collapses_companion() -> None:
    catalog = {
        "sources": [
            {"source_id": "v", "relative_path": "media/reviews/A.mp4", "media_kind": "video"},
            {"source_id": "a", "relative_path": "media/reviews/A.wav", "media_kind": "audio"},
            {"source_id": "only", "relative_path": "media/reviews/B.mp3", "media_kind": "audio"},
            {"source_id": "c_mp3", "relative_path": "media/reviews/C.mp3", "media_kind": "audio"},
            {"source_id": "c_wav", "relative_path": "media/reviews/C.wav", "media_kind": "audio"},
        ]
    }

    result = api_stage3_routes._timeline_catalog(catalog)

    assert [item["source_id"] for item in result["sources"]] == ["v", "only", "c_wav"]


@pytest.mark.parametrize(
    ("payload", "error"),
    [
        (None, "JSON object is required"),
        (
            {"project": "BRIK", "directory": "/review-media", "recursive": "yes"},
            "recursive must be a boolean",
        ),
    ],
)
def test_scan_api_validates_payload(client, payload, error: str) -> None:
    http, _app, _monkeypatch = client
    response = http.post("/stage3/media/scan", json=payload)
    assert response.status_code == 400
    assert response.get_json() == {"ok": False, "error": error}


def test_direct_scan_is_blocked_while_project_mutation_is_active(client) -> None:
    http, app, monkeypatch = client
    services = app.config["TEST_STAGE3_SERVICES"]
    services._project_has_active_mutation = lambda _project: True
    monkeypatch.setattr(
        api_stage3_routes,
        "scan_media_directory",
        lambda *_args, **_kwargs: pytest.fail("scan must not start while project is busy"),
    )

    response = http.post("/stage3/media/scan", json={"project": "BRIK"})

    assert response.status_code == 409
    assert "another mutating job" in response.get_json()["error"]


def test_scan_api_returns_catalog_validation_error(client) -> None:
    http, _app, monkeypatch = client

    def reject(*_args, **_kwargs):
        raise api_stage3_routes.MediaCatalogError(
            "directory resolves outside the configured media root"
        )

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


def test_scan_background_job_reports_progress_and_result(client) -> None:
    http, _app, monkeypatch = client
    monkeypatch.setattr(api_stage3_routes.threading, "Thread", _ImmediateThread)
    monkeypatch.setattr(
        api_stage3_routes,
        "scan_media_directory",
        lambda *_a, **_k: {
            "directory": "/app/projects/BRIK/media/reviews",
            "relative_directory": "media/reviews",
            "recursive": False,
            "sources": [],
        },
    )

    started = http.post("/stage3/jobs/scan/start", json={"project": "BRIK"})

    assert started.status_code == 200
    start_result = started.get_json()["result"]
    assert start_result["status"] == "queued"
    status = http.get(start_result["status_url"])
    assert status.status_code == 200
    job = status.get_json()["result"]
    assert job["kind"] == "scan"
    assert job["status"] == "completed"
    assert job["progress"] == 100
    assert job["result"]["registered_sources"] == 0
    latest = http.get("/stage3/jobs/status?project=BRIK&kind=scan")
    assert latest.get_json()["result"]["id"] == job["id"]


def test_prepare_background_job_returns_legacy_download_without_viewings(client) -> None:
    http, app, monkeypatch = client
    services = app.config["TEST_STAGE3_SERVICES"]
    paths = services._ensure_project("BRIK")
    paths["work"].mkdir(parents=True, exist_ok=True)
    (paths["work"] / "full_timeline.json").write_text(
        json.dumps(
            {
                "project": "BRIK",
                "fps": 25,
                "width": 1920,
                "height": 1080,
                "normalization_revision": "norm-1",
                "segments": [{"id": "snh1", "type": "snh"}],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(api_stage3_routes.threading, "Thread", _ImmediateThread)
    monkeypatch.setattr(
        api_stage3_routes,
        "scan_media_directory",
        lambda *_a, **_k: {
            "directory": "/app/projects/BRIK/media/reviews",
            "relative_directory": "media/reviews",
            "recursive": False,
            "sources": [],
        },
    )

    started = http.post("/stage3/jobs/prepare/start", json={"project": "BRIK"})
    job = http.get(started.get_json()["result"]["status_url"]).get_json()["result"]

    assert job["status"] == "completed"
    assert job["result"]["format"] == "legacy"
    assert job["result"]["download_url"] == "/stage2/export/xml?project=BRIK"


def test_stage3_job_in_flight_guard_is_per_project(client) -> None:
    http, _app, monkeypatch = client
    _HoldingThread.instances.clear()
    monkeypatch.setattr(api_stage3_routes.threading, "Thread", _HoldingThread)
    monkeypatch.setattr(
        api_stage3_routes,
        "scan_media_directory",
        lambda *_a, **_k: {
            "directory": "/app/projects/BRIK/media/reviews",
            "relative_directory": "media/reviews",
            "recursive": False,
            "sources": [],
        },
    )

    first = http.post("/stage3/jobs/scan/start", json={"project": "BRIK"})
    second = http.post("/stage3/jobs/prepare/start", json={"project": "BRIK"})

    assert first.status_code == 200
    assert second.status_code == 409
    assert "already active" in second.get_json()["error"]
    held = _HoldingThread.instances.pop()
    held.target(*held.args, **held.kwargs)


def test_stage3_background_failure_is_observable_in_status(client) -> None:
    http, _app, monkeypatch = client
    monkeypatch.setattr(api_stage3_routes.threading, "Thread", _ImmediateThread)

    def reject(*_args, **_kwargs):
        raise api_stage3_routes.MediaCatalogError("ffprobe failed")

    monkeypatch.setattr(api_stage3_routes, "scan_media_directory", reject)
    started = http.post("/stage3/jobs/scan/start", json={"project": "BRIK"})
    job = http.get(started.get_json()["result"]["status_url"]).get_json()["result"]

    assert job["status"] == "failed"
    assert job["error"] == "ffprobe failed"
    assert job["error_status"] == 400


def test_latest_stage3_job_status_survives_route_registry_restart(client) -> None:
    http, app, monkeypatch = client
    services = app.config["TEST_STAGE3_SERVICES"]
    monkeypatch.setattr(api_stage3_routes.threading, "Thread", _ImmediateThread)
    monkeypatch.setattr(
        api_stage3_routes,
        "scan_media_directory",
        lambda *_a, **_k: {
            "directory": "/app/projects/BRIK/media/reviews",
            "relative_directory": "media/reviews",
            "recursive": False,
            "sources": [],
        },
    )
    started = http.post("/stage3/jobs/scan/start", json={"project": "BRIK"})
    completed_id = started.get_json()["result"]["job_id"]

    restarted_app = Flask(__name__)
    restarted_app.config.update(TESTING=True)
    api_stage3_routes.register_stage3_routes(restarted_app, services)
    restored = restarted_app.test_client().get("/stage3/jobs/status?project=BRIK&kind=scan")

    assert restored.status_code == 200
    job = restored.get_json()["result"]
    assert job["id"] == completed_id
    assert job["status"] == "completed"


def _stub_empty_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        api_stage3_routes,
        "scan_media_directory",
        lambda *_a, **_k: {
            "directory": "/app/projects/BRIK/media/reviews",
            "relative_directory": "media/reviews",
            "recursive": False,
            "sources": [],
        },
    )


def _restarted_client(services) -> object:
    restarted_app = Flask(__name__)
    restarted_app.config.update(TESTING=True)
    api_stage3_routes.register_stage3_routes(restarted_app, services)
    return restarted_app.test_client()


def test_stage3_job_registry_is_trimmed_but_keeps_active_jobs(client) -> None:
    http, _app, monkeypatch = client
    monkeypatch.setattr(api_stage3_routes, "_STAGE3_JOBS_MAX", 2)
    _stub_empty_scan(monkeypatch)
    _HoldingThread.instances.clear()
    monkeypatch.setattr(api_stage3_routes.threading, "Thread", _HoldingThread)
    active_id = http.post("/stage3/jobs/prepare/start", json={"project": "ACTIVE"}).get_json()[
        "result"
    ]["job_id"]
    monkeypatch.setattr(api_stage3_routes.threading, "Thread", _ImmediateThread)

    job_ids = [
        http.post("/stage3/jobs/scan/start", json={"project": "BRIK"}).get_json()["result"][
            "job_id"
        ]
        for _ in range(5)
    ]

    assert http.get(f"/stage3/jobs/{job_ids[0]}?project=BRIK").status_code == 404
    assert http.get(f"/stage3/jobs/{job_ids[1]}?project=BRIK").status_code == 404
    assert http.get(f"/stage3/jobs/{job_ids[-1]}?project=BRIK").status_code == 200
    active = http.get(f"/stage3/jobs/{active_id}?project=BRIK").get_json()["result"]
    assert active["status"] == "queued"


def test_stage3_job_registry_drops_expired_terminal_jobs(client) -> None:
    http, _app, monkeypatch = client
    monkeypatch.setattr(api_stage3_routes.threading, "Thread", _ImmediateThread)
    _stub_empty_scan(monkeypatch)
    first_id = http.post("/stage3/jobs/scan/start", json={"project": "BRIK"}).get_json()["result"][
        "job_id"
    ]
    second_id = http.post("/stage3/jobs/scan/start", json={"project": "BRIK"}).get_json()["result"][
        "job_id"
    ]
    assert http.get(f"/stage3/jobs/{first_id}?project=BRIK").status_code == 200

    monkeypatch.setattr(api_stage3_routes, "_STAGE3_JOBS_TTL_SECONDS", -1.0)
    third_id = http.post("/stage3/jobs/scan/start", json={"project": "BRIK"}).get_json()["result"][
        "job_id"
    ]

    assert http.get(f"/stage3/jobs/{first_id}?project=BRIK").status_code == 404
    assert http.get(f"/stage3/jobs/{second_id}?project=BRIK").status_code == 404
    assert http.get(f"/stage3/jobs/{third_id}?project=BRIK").status_code == 200


def test_stage3_scan_and_prepare_statuses_do_not_overwrite_each_other(client) -> None:
    http, app, monkeypatch = client
    services = app.config["TEST_STAGE3_SERVICES"]
    paths = services._ensure_project("BRIK")
    monkeypatch.setattr(api_stage3_routes.threading, "Thread", _ImmediateThread)
    _stub_empty_scan(monkeypatch)
    scan_id = http.post("/stage3/jobs/scan/start", json={"project": "BRIK"}).get_json()["result"][
        "job_id"
    ]
    paths["work"].mkdir(parents=True, exist_ok=True)
    (paths["work"] / "full_timeline.json").write_text(
        json.dumps(
            {
                "project": "BRIK",
                "fps": 25,
                "width": 1920,
                "height": 1080,
                "normalization_revision": "norm-1",
                "segments": [{"id": "snh1", "type": "snh"}],
            }
        ),
        encoding="utf-8",
    )
    prepare_id = http.post("/stage3/jobs/prepare/start", json={"project": "BRIK"}).get_json()[
        "result"
    ]["job_id"]

    restarted = _restarted_client(services)

    scan = restarted.get("/stage3/jobs/status?project=BRIK&kind=scan").get_json()["result"]
    prepare = restarted.get("/stage3/jobs/status?project=BRIK&kind=prepare").get_json()["result"]
    assert (scan["id"], scan["kind"], scan["status"]) == (scan_id, "scan", "completed")
    assert (prepare["id"], prepare["kind"], prepare["status"]) == (
        prepare_id,
        "prepare",
        "completed",
    )
    assert restarted.get(f"/stage3/jobs/{scan_id}?project=BRIK").get_json()["result"]["kind"] == (
        "scan"
    )
    assert (
        restarted.get(f"/stage3/jobs/{prepare_id}?project=BRIK").get_json()["result"]["kind"]
        == "prepare"
    )
    latest = restarted.get("/stage3/jobs/status?project=BRIK").get_json()["result"]
    assert latest["id"] == prepare_id
    assert (paths["work"] / "stage3_scan_job_status.json").exists()
    assert (paths["work"] / "stage3_prepare_job_status.json").exists()
    assert not (paths["work"] / "stage3_job_status.json").exists()


def test_stage3_legacy_shared_job_status_file_is_still_read(client) -> None:
    _http, app, _monkeypatch = client
    services = app.config["TEST_STAGE3_SERVICES"]
    paths = services._ensure_project("BRIK")
    paths["work"].mkdir(parents=True, exist_ok=True)
    legacy = {
        "id": "legacyjob",
        "project": "BRIK",
        "kind": "scan",
        "status": "completed",
        "progress": 100,
        "created_at": "2026-01-01T00:00:00+00:00",
    }
    (paths["work"] / "stage3_job_status.json").write_text(json.dumps(legacy), encoding="utf-8")

    restarted = _restarted_client(services)

    by_kind = restarted.get("/stage3/jobs/status?project=BRIK&kind=scan")
    assert by_kind.get_json()["result"]["id"] == "legacyjob"
    assert restarted.get("/stage3/jobs/legacyjob?project=BRIK").status_code == 200
    assert restarted.get("/stage3/jobs/status?project=BRIK&kind=prepare").status_code == 404
