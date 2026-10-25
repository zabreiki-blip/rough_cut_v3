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
    assert result["build"]["stale_reasons"] == ["timeline_rebuild_required"]
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
    http, _paths = _complete_setup(client, extra=NEEDS_REVIEW)
    client[2].setattr(api_stage3_routes.threading, "Thread", _ImmediateThread)

    http.post("/stage3/jobs/prepare/start", json={"project": "BRIK", "mode": "complete"})
    processing = _status(http)["processing"]

    assert processing["status"] == "failed"
    assert processing["superseded"] is False
    assert processing["error"] == "assembly_incomplete"


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
