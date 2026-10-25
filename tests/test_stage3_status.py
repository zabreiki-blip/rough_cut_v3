"""GET /stage3/status: processing / build / assembly are three independent dimensions."""

from __future__ import annotations

import json

import pytest

import api_stage3_routes
from stage2.sync_models import SyncMatch
from stage2.sync_storage import SyncMatchStore
from stage_common import sync_matches_fingerprint
from test_stage3_api import (
    _complete_setup,
    _ImmediateThread,
    _nested_prepare_setup,
    _segment,
)
from test_stage3_api import client as stage3_client  # noqa: F401


@pytest.fixture()
def client(stage3_client):  # noqa: F811
    return stage3_client


def _status(http) -> dict:
    response = http.get("/stage3/status?project=BRIK")
    assert response.status_code == 200
    body = response.get_json()
    assert body["ok"] is True
    assert body["result"] == {k: v for k, v in body.items() if k not in {"ok", "result"}}
    return body["result"]


def _prepare(http, mode: str | None = None):
    body = {"project": "BRIK"}
    if mode:
        body["mode"] = mode
    return http.post("/stage3/timeline/prepare", json=body)


def _rewrite_timeline(paths, **fields) -> None:
    path = paths["work"] / "full_timeline.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data.update(fields)
    path.write_text(json.dumps(data), encoding="utf-8")


def _confirm(client, **fields) -> None:
    services = client[1].config["TEST_STAGE3_SERVICES"]
    confirmed = {"revision": "norm-1", **fields}
    services._confirmed_normalization = lambda _project: (confirmed, "")


def _append_life(paths, seg_id: str, start: float) -> None:
    path = paths["work"] / "full_timeline.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["segments"].append(_segment(seg_id, "life", start, 1.0))
    path.write_text(json.dumps(data), encoding="utf-8")


NEEDS_REVIEW = [_segment("r1", "snh", 4.0, 3.0, match_status="needs_review")]


# --- assembly -----------------------------------------------------------------------------


def test_status_without_timeline_is_no_timeline_not_an_error(client) -> None:
    http, _app, _monkeypatch = client

    result = _status(http)

    assert result["project"] == "BRIK"
    assert result["assembly"]["level"] == "no_timeline"
    assert result["assembly"]["reason"] == "timeline_missing"
    assert result["processing"]["status"] == "idle"
    assert result["processing"]["job_id"] is None
    assert result["build"]["build_id"] is None
    assert result["build"]["is_current"] is False


def test_status_stale_timeline_is_no_timeline_with_reason(client) -> None:
    http, _paths = _complete_setup(client)
    _confirm(client, revision="norm-2")

    response = http.get("/stage3/status?project=BRIK")

    assert response.status_code == 200
    assembly = response.get_json()["result"]["assembly"]
    assert assembly["level"] == "no_timeline"
    assert assembly["reason"] == "timeline_rebuild_required"
    assert "timeline_rebuild_required" in assembly["message"]


def test_status_draft_with_blockers_lists_them_by_reason(client) -> None:
    http, paths = _complete_setup(client, extra=NEEDS_REVIEW)
    before = sorted(path.name for path in paths["work"].iterdir())

    result = _status(http)

    assembly = result["assembly"]
    assert assembly["level"] == "draft"
    assert assembly["blocker_count"] == 1
    assert assembly["blockers_by_reason"] == {"needs_review": 1}
    assert [(b["segment_id"], b["reason"]) for b in assembly["blockers"]] == [
        ("r1", "needs_review")
    ]
    assert assembly["blockers"][0]["detail"]
    assert assembly["placeholders"]["segments"] == 1
    assert assembly["placeholders"]["duration_sec"] == 3.0
    assert "needs_review" in assembly["placeholders"]["by_reason"]
    assert isinstance(assembly["open_tasks"], int)
    assert sorted(path.name for path in paths["work"].iterdir()) == before  # read-only


def test_status_complete_ready_has_no_blockers(client) -> None:
    http, _paths = _complete_setup(client)

    assembly = _status(http)["assembly"]

    assert assembly["level"] == "complete_ready"
    assert assembly["blocker_count"] == 0
    assert assembly["blockers"] == []
    assert assembly["blockers_by_reason"] == {}
    assert assembly["placeholders"]["segments"] == 0


def test_status_reports_active_revision_from_confirmed_normalization(client) -> None:
    http, _paths = _complete_setup(client)
    _confirm(client, scenario_revision_id="rev_0007")

    assert _status(http)["active_revision_id"] == "rev_0007"


# --- build --------------------------------------------------------------------------------


def test_status_complete_build_is_current(client) -> None:
    http, _paths = _complete_setup(client)
    built = _prepare(http, "complete").get_json()["result"]

    build = _status(http)["build"]

    assert build["build_id"] == built["build_id"]
    assert build["mode"] == "complete"
    assert build["complete"] is True
    assert build["is_current"] is True
    assert build["stale_reasons"] == []
    assert build["live_timeline_unavailable_reason"] is None
    assert build["created_at"]


def test_status_draft_build_reports_placeholders(client) -> None:
    http, _paths = _complete_setup(client, extra=NEEDS_REVIEW)
    _prepare(http)

    build = _status(http)["build"]

    assert build["mode"] == "draft"
    assert build["complete"] is False
    assert build["is_current"] is True
    assert build["summary"]["placeholders"]["segments"] == 1


def test_status_build_is_stale_after_timeline_change(client) -> None:
    http, paths = _complete_setup(client)
    _prepare(http, "complete")
    _append_life(paths, "l9", 10.0)

    result = _status(http)

    assert result["build"]["is_current"] is False
    assert result["build"]["stale_reasons"] == ["timeline_sha256"]
    assert result["assembly"]["level"] != "no_timeline"


def test_status_build_is_stale_after_sync_fingerprint_change(client) -> None:
    _http, paths = _nested_prepare_setup(client, [])
    http = _http
    store = SyncMatchStore(paths["sync_matches"])

    def write_timeline(out_sec: float) -> None:
        current = SyncMatch(
            segment_id="uid-1",
            query_text="q",
            source_id="v1",
            source_in_sec=1.0,
            source_out_sec=out_sec,
            status="confirmed",
            metadata={"normalization_revision": "norm-1"},
        )
        store.replace_all([current])
        segment = _segment(
            "uid-1",
            "snh",
            0.0,
            out_sec - 1.0,
            match_status="confirmed",
            source_id="v1",
            source_in_sec=1.0,
            source_out_sec=out_sec,
        )
        (paths["work"] / "full_timeline.json").write_text(
            json.dumps(
                {
                    "project": "BRIK",
                    "fps": 25,
                    "normalization_revision": "norm-1",
                    "sync_matches_fingerprint": sync_matches_fingerprint([current]),
                    "segments": [segment],
                }
            ),
            encoding="utf-8",
        )

    write_timeline(3.0)
    assert _prepare(http).status_code == 200
    assert _status(http)["build"]["is_current"] is True

    write_timeline(4.0)  # synced range edited and timeline rebuilt: new fingerprint

    build = _status(http)["build"]
    assert build["is_current"] is False
    assert "sync_matches_fingerprint" in build["stale_reasons"]


def test_status_build_is_stale_after_scenario_revision_change(client) -> None:
    http, paths = _complete_setup(client)
    _rewrite_timeline(paths, scenario_revision_id="rev_0001")
    _confirm(client, scenario_revision_id="rev_0001")
    _prepare(http, "complete")
    assert _status(http)["build"]["is_current"] is True

    _rewrite_timeline(paths, scenario_revision_id="rev_0002")
    _confirm(client, scenario_revision_id="rev_0002")

    result = _status(http)
    assert result["active_revision_id"] == "rev_0002"
    assert result["build"]["is_current"] is False
    assert "scenario_revision_id" in result["build"]["stale_reasons"]


def test_status_build_is_not_current_when_live_timeline_is_stale(client) -> None:
    http, _paths = _complete_setup(client)
    _prepare(http, "complete")
    _confirm(client, revision="norm-2")

    result = _status(http)

    assert result["build"]["is_current"] is False
    assert result["build"]["stale_reasons"] == []
    assert result["build"]["live_timeline_unavailable_reason"] == "timeline_rebuild_required"
    assert result["assembly"]["level"] == "no_timeline"


# --- processing / superseded jobs ----------------------------------------------------------


def test_status_processing_reports_completed_job_with_build(client) -> None:
    http, paths = _complete_setup(client)
    client[2].setattr(api_stage3_routes.threading, "Thread", _ImmediateThread)
    _rewrite_timeline(paths, scenario_revision_id="rev_0001")
    _confirm(client, scenario_revision_id="rev_0001")

    started = http.post("/stage3/jobs/prepare/start", json={"project": "BRIK"}).get_json()["result"]
    result = _status(http)

    processing = result["processing"]
    assert processing["status"] == "completed"
    assert processing["job_id"] == started["job_id"]
    assert processing["kind"] == "prepare"
    assert processing["finished_at"]
    assert processing["build_id"] == result["build"]["build_id"]
    assert processing["scenario_revision_id"] == "rev_0001"
    assert processing["superseded"] is False


def test_status_processing_failed_job_without_any_build_is_not_superseded(client) -> None:
    http, paths = _complete_setup(client, extra=NEEDS_REVIEW)
    client[2].setattr(api_stage3_routes.threading, "Thread", _ImmediateThread)
    _rewrite_timeline(paths, scenario_revision_id="rev_0001")
    _confirm(client, scenario_revision_id="rev_0001")

    started = http.post(
        "/stage3/jobs/prepare/start", json={"project": "BRIK", "mode": "complete"}
    ).get_json()["result"]
    processing = _status(http)["processing"]

    assert processing["status"] == "failed"
    assert processing["superseded"] is False  # same revision and timeline as now
    assert processing["error"] == "assembly_incomplete"
    assert processing["scenario_revision_id"] == "rev_0001"  # recorded at job start
    job = http.get(started["status_url"]).get_json()["result"]
    assert job["scenario_revision_id"] == "rev_0001"
    assert job["timeline_sha256"]
    assert job["superseded"] is False


def test_failed_job_of_another_revision_is_superseded(client) -> None:
    http, paths = _complete_setup(client, extra=NEEDS_REVIEW)
    client[2].setattr(api_stage3_routes.threading, "Thread", _ImmediateThread)
    _rewrite_timeline(paths, scenario_revision_id="rev_0001")
    _confirm(client, scenario_revision_id="rev_0001")
    started = http.post(
        "/stage3/jobs/prepare/start", json={"project": "BRIK", "mode": "complete"}
    ).get_json()["result"]
    _rewrite_timeline(paths, scenario_revision_id="rev_0002")
    _confirm(client, scenario_revision_id="rev_0002")

    processing = _status(http)["processing"]
    job = http.get(started["status_url"]).get_json()["result"]

    assert processing["status"] == "failed"
    assert processing["stale_revision"] is True
    assert processing["superseded"] is True
    assert job["superseded"] is True
    assert job["stale_revision"] is True


def test_failed_job_of_an_older_timeline_is_superseded(client) -> None:
    http, paths = _complete_setup(client, extra=NEEDS_REVIEW)
    client[2].setattr(api_stage3_routes.threading, "Thread", _ImmediateThread)
    started = http.post(
        "/stage3/jobs/prepare/start", json={"project": "BRIK", "mode": "complete"}
    ).get_json()["result"]
    _append_life(paths, "l9", 10.0)

    job = http.get(started["status_url"]).get_json()["result"]

    assert job["status"] == "failed"
    assert job["superseded"] is True
    assert _status(http)["processing"]["superseded"] is True


def _persist_running_prepare_job(paths, *, created_at: str) -> None:
    job = {
        "id": "oldjob",
        "project": "BRIK",
        "kind": "prepare",
        "status": "running",
        "progress": 40,
        "message": "x",
        "result": None,
        "error": "",
        "created_at": created_at,
        "started_at": created_at,
        "finished_at": None,
    }
    (paths["work"] / "stage3_prepare_job_status.json").write_text(json.dumps(job), encoding="utf-8")


def test_interrupted_job_superseded_is_the_same_on_both_read_paths(client) -> None:
    http, paths = _complete_setup(client)
    _persist_running_prepare_job(paths, created_at="2020-01-01T00:00:00+00:00")
    assert _prepare(http).status_code == 200  # a build published after the job was created

    via_status = _status(http)["processing"]
    via_alias = http.get("/stage3/jobs/status?project=BRIK&kind=prepare").get_json()["result"]
    via_status_after = _status(http)["processing"]  # alias persisted the interrupted state

    assert via_status["status"] == "interrupted"
    assert via_alias["status"] == "interrupted"
    assert via_status["superseded"] is True
    assert via_alias["superseded"] is True
    assert via_status_after["superseded"] is True


def test_failed_job_older_than_latest_build_is_superseded(client) -> None:
    http, _paths = _complete_setup(client, extra=NEEDS_REVIEW)
    client[2].setattr(api_stage3_routes.threading, "Thread", _ImmediateThread)
    started = http.post(
        "/stage3/jobs/prepare/start", json={"project": "BRIK", "mode": "complete"}
    ).get_json()["result"]
    failed = http.get(started["status_url"]).get_json()["result"]
    assert failed["status"] == "failed"
    assert failed["superseded"] is False

    assert _prepare(http).status_code == 200  # a later (draft) build supersedes the failure

    job = http.get(started["status_url"]).get_json()["result"]
    assert job["status"] == "failed"
    assert job["superseded"] is True
    alias = http.get("/stage3/jobs/status?project=BRIK&kind=prepare").get_json()["result"]
    assert alias["superseded"] is True
    assert _status(http)["processing"]["superseded"] is True


def test_completed_job_of_an_older_build_is_superseded(client) -> None:
    http, paths = _complete_setup(client)
    client[2].setattr(api_stage3_routes.threading, "Thread", _ImmediateThread)
    started = http.post("/stage3/jobs/prepare/start", json={"project": "BRIK"}).get_json()["result"]
    first = http.get(started["status_url"]).get_json()["result"]
    assert first["status"] == "completed"
    assert first["superseded"] is False

    _append_life(paths, "l9", 10.0)
    second_build = _prepare(http).get_json()["result"]["build_id"]
    assert second_build != first["build_id"]

    job = http.get(started["status_url"]).get_json()["result"]
    assert job["superseded"] is True


def test_status_marks_job_of_another_revision_as_superseded(client) -> None:
    http, paths = _complete_setup(client)
    client[2].setattr(api_stage3_routes.threading, "Thread", _ImmediateThread)
    _rewrite_timeline(paths, scenario_revision_id="rev_0001")
    _confirm(client, scenario_revision_id="rev_0001")
    http.post("/stage3/jobs/prepare/start", json={"project": "BRIK"})
    _rewrite_timeline(paths, scenario_revision_id="rev_0002")
    _confirm(client, scenario_revision_id="rev_0002")

    processing = _status(http)["processing"]

    assert processing["scenario_revision_id"] == "rev_0001"
    assert processing["stale_revision"] is True
    assert processing["superseded"] is True


def test_scan_job_is_never_superseded_by_a_build(client) -> None:
    http, _paths = _complete_setup(client)
    client[2].setattr(api_stage3_routes.threading, "Thread", _ImmediateThread)
    client[2].setattr(api_stage3_routes, "scan_media_directory", lambda *_a, **_k: {"sources": []})
    started = http.post("/stage3/jobs/scan/start", json={"project": "BRIK"}).get_json()["result"]
    _prepare(http)

    job = http.get(started["status_url"]).get_json()["result"]

    assert job["kind"] == "scan"
    assert job["superseded"] is False


# --- invalid / failing timeline (M1, M2) ---------------------------------------------------


@pytest.mark.parametrize("raw", ["[]", "null", '"text"', "5", "{broken"])
def test_status_invalid_timeline_is_no_timeline_not_500(client, raw) -> None:
    http, paths = _complete_setup(client)
    _prepare(http, "complete")
    (paths["work"] / "full_timeline.json").write_text(raw, encoding="utf-8")

    result = _status(http)

    assert result["assembly"]["level"] == "no_timeline"
    assert result["assembly"]["reason"] == "timeline_invalid"
    assert result["build"]["live_timeline_unavailable_reason"] == "timeline_invalid"
    assert result["build"]["is_current"] is False
    builds = http.get("/stage3/builds?project=BRIK")
    assert builds.status_code == 200
    assert builds.get_json()["result"]["latest"]["is_current"] is False


def test_status_does_not_mask_unexpected_timeline_errors(client) -> None:
    http, _paths = _complete_setup(client)

    def boom(*_a, **_k):
        raise RuntimeError("unexpected")

    client[2].setattr(api_stage3_routes, "_load_current_timeline", boom)

    assert http.get("/stage3/status?project=BRIK").status_code == 500


def test_status_plan_failure_is_level_error_with_processing_and_build(client) -> None:
    http, _paths = _complete_setup(client)
    _prepare(http, "complete")

    def boom(*_a, **_k):
        raise RuntimeError("plan exploded")

    client[2].setattr(api_stage3_routes, "_compute_plan", boom)

    response = http.get("/stage3/status?project=BRIK")

    assert response.status_code == 200
    result = response.get_json()["result"]
    assert result["assembly"]["level"] == "error"
    assert result["assembly"]["reason"] == "plan_failed"
    assert "plan exploded" in result["assembly"]["message"]
    assert result["processing"]["status"] == "idle"
    assert result["build"]["mode"] == "complete"
    assert result["build"]["is_current"] is True


# --- read-only guarantees (B1) -------------------------------------------------------------


def _tree_snapshot(root) -> dict:
    snapshot = {}
    for item in sorted(root.rglob("*")):
        stat = item.stat()
        snapshot[item.relative_to(root).as_posix()] = (
            item.is_dir(),
            0 if item.is_dir() else stat.st_size,
            0 if item.is_dir() else stat.st_mtime_ns,
        )
    return snapshot


def _real_scan_setup(client):
    """Timeline plus two review files on disk, one with a cached ffprobe entry, one without."""
    from stage3 import media_catalog

    http, app, monkeypatch = client
    services = app.config["TEST_STAGE3_SERVICES"]
    paths = services._ensure_project("BRIK")
    reviews = paths["root"] / "media" / "reviews"
    reviews.mkdir(parents=True)
    paths["work"].mkdir(parents=True, exist_ok=True)
    (reviews / "cached.mp4").write_bytes(b"aaa")
    metadata = {
        "duration_sec": 10.0,
        "fps": 25.0,
        "width": 1920,
        "height": 1080,
        "media_kind": "video",
        "has_audio": True,
    }
    monkeypatch.setattr(media_catalog, "probe_media", lambda *_a, **_k: dict(metadata))
    media_catalog.scan_media_directory(str(reviews), media_root=paths["root"])  # fills the cache
    (reviews / "fresh.mp4").write_bytes(b"bbb")  # never probed
    (paths["work"] / "full_timeline.json").write_text(
        json.dumps(
            {
                "project": "BRIK",
                "fps": 25,
                "normalization_revision": "norm-1",
                "segments": [_segment("l1", "life", 0.0, 2.0)],
            }
        ),
        encoding="utf-8",
    )
    calls = []

    def counting_probe(*args, **kwargs):
        calls.append(args)
        raise AssertionError("GET /stage3/status must not run ffprobe")

    monkeypatch.setattr(media_catalog, "probe_media", counting_probe)
    return http, paths, calls


def test_status_is_read_only_and_never_probes_media(client) -> None:
    http, paths, calls = _real_scan_setup(client)
    services = client[1].config["TEST_STAGE3_SERVICES"]

    def forbidden(*_a, **_k):
        raise AssertionError("status must not call _ensure_project")

    services._ensure_project = forbidden
    before = _tree_snapshot(paths["root"])

    result = _status(http)

    assert calls == []
    assert _tree_snapshot(paths["root"]) == before
    assert result["assembly"]["catalog_incomplete"] is True
    assert result["assembly"]["uncached_media_files"] == 1
    assert result["assembly"]["level"] in {"draft", "complete_ready"}


def test_status_without_ffprobe_cache_file_creates_nothing(client) -> None:
    http, paths, calls = _real_scan_setup(client)
    (paths["work"] / "stage3_ffprobe_cache.json").unlink()
    before = _tree_snapshot(paths["root"])

    result = _status(http)

    assert calls == []
    assert _tree_snapshot(paths["root"]) == before
    assert result["assembly"]["uncached_media_files"] == 2


def test_status_for_missing_project_is_404_and_creates_nothing(client, tmp_path) -> None:
    http, app, _monkeypatch = client
    services = app.config["TEST_STAGE3_SERVICES"]
    ghost = {
        "root": tmp_path / "projects" / "GHOST",
        "work": tmp_path / "projects" / "GHOST" / "work",
    }
    services._project_paths = lambda _project: ghost
    services._ensure_project = lambda _project: (_ for _ in ()).throw(AssertionError("mkdir"))

    response = http.get("/stage3/status?project=GHOST")

    assert response.status_code == 404
    assert not ghost["root"].exists()


def test_scan_media_directory_cache_only_neither_probes_nor_writes(tmp_path, monkeypatch) -> None:
    from stage3 import media_catalog

    root = tmp_path / "proj"
    reviews = root / "media"
    reviews.mkdir(parents=True)
    (reviews / "a.mp4").write_bytes(b"a")
    monkeypatch.setattr(
        media_catalog, "probe_media", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError)
    )

    catalog = media_catalog.scan_media_directory(str(reviews), media_root=root, cache_only=True)

    assert catalog["sources"] == []
    assert catalog["uncached_files"] == 1
    assert not (root / "work").exists()


def test_confirmed_normalization_read_only_skips_activation_recovery(tmp_path, monkeypatch) -> None:
    import api
    from stage4 import versioning
    from test_stage2_sync_api import _write_confirmed_normalization

    projects = tmp_path / "projects"
    projects.mkdir()
    monkeypatch.setattr(api, "PROJECTS_DIR", projects)
    monkeypatch.setattr(api, "ROOT_DIR", tmp_path)
    _write_confirmed_normalization(projects)

    def forbidden(_paths):
        raise AssertionError("read-only normalization lookup must not recover activations")

    monkeypatch.setattr(versioning, "recover_interrupted_activation", forbidden)

    document, reason = api._confirmed_normalization_read_only("demo")

    assert reason in {"confirmed", "source_changed"}
    assert reason != "revision_state_conflict"
    with pytest.raises(AssertionError):
        api._confirmed_normalization("demo")  # the regular lookup still recovers
