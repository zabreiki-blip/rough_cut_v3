from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import api_stage3_routes
from stage3 import builds as stage3_builds
from test_stage3_api import _complete_setup, _nested_prepare_setup, _segment
from test_stage3_api import client as stage3_client  # noqa: F401


@pytest.fixture()
def client(stage3_client):  # noqa: F811
    return stage3_client


def _prepare(http, mode: str | None = None):
    body = {"project": "BRIK"}
    if mode:
        body["mode"] = mode
    return http.post("/stage3/timeline/prepare", json=body)


def _builds_dir(paths) -> Path:
    return paths["work"] / "builds"


def _build_dirs(paths) -> list[str]:
    root = _builds_dir(paths)
    return sorted(item.name for item in root.iterdir() if item.is_dir()) if root.is_dir() else []


def _append_life(paths, seg_id: str, start: float) -> None:
    path = paths["work"] / "full_timeline.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["segments"].append(_segment(seg_id, "life", start, 1.0))
    path.write_text(json.dumps(data), encoding="utf-8")


def test_same_prepare_twice_yields_one_build(client) -> None:
    http, paths = _complete_setup(client)

    first = _prepare(http).get_json()["result"]
    second = _prepare(http).get_json()["result"]

    assert first["build_id"] == second["build_id"]
    assert _build_dirs(paths) == [first["build_id"]]
    root = _builds_dir(paths) / first["build_id"]
    assert sorted(item.name for item in root.iterdir()) == [
        "assembly_plan.json",
        "build.json",
        "source_mapping.json",
        "timeline.xml",
    ]
    build = json.loads((root / "build.json").read_text(encoding="utf-8"))
    assert build["schema_version"] == 1
    assert build["mode"] == "draft"
    assert build["complete"] is True
    assert build["outputs"]["xml_sha256"] == stage3_builds.sha256_file(root / "timeline.xml")
    assert build["outputs"]["xml_filename"].endswith(f"_{first['build_id']}.xml")
    assert build["summary"] == json.loads((root / "assembly_plan.json").read_text())["summary"]
    assert set(build["inputs"]) == {
        "scenario_revision_id",
        "normalization_revision",
        "timeline_sha256",
        "sync_matches_fingerprint",
        "settings_sha256",
        "wav",
        "sources",
        "media_mapping_sha256",
    }
    latest = json.loads((_builds_dir(paths) / "latest.json").read_text(encoding="utf-8"))
    assert latest["build_id"] == first["build_id"]
    mapping = json.loads((paths["work"] / "stage3_source_mapping.json").read_text())
    assert mapping["build_id"] == first["build_id"]
    assert (paths["work"] / first["filename"]).is_file()


def test_changed_wav_timeline_or_mode_gives_new_build_id(client) -> None:
    http, paths = _complete_setup(client)
    base = _prepare(http).get_json()["result"]["build_id"]

    complete = _prepare(http, "complete").get_json()["result"]["build_id"]
    assert complete != base

    wav = paths["work"] / "zk.wav"
    wav.write_bytes(b"y")  # same size, different content
    os.utime(wav, ns=(1_000_000_000, 1_000_000_000))
    changed_wav = _prepare(http).get_json()["result"]["build_id"]
    assert changed_wav not in {base, complete}

    _append_life(paths, "l9", 10.0)
    changed_timeline = _prepare(http).get_json()["result"]["build_id"]
    assert changed_timeline not in {base, complete, changed_wav}
    assert len(_build_dirs(paths)) == 4


def test_build_id_depends_on_matches_fingerprint_and_sources(tmp_path: Path) -> None:
    media = tmp_path / "v1.mp4"
    media.write_bytes(b"abc")
    plan = {"items": [], "summary": {}}
    timeline = {"fps": 25, "sync_matches_fingerprint": "fp-1", "scenario_revision_id": "rev_1"}

    def inputs(data: dict, size_bump: int = 0):
        if size_bump:
            media.write_bytes(b"abc" * (size_bump + 1))
        return stage3_builds.collect_inputs(
            timeline_data=data,
            timeline_sha256="t",
            plan=plan,
            mapping_catalog={"v1": {"path": str(media)}},
            unmapped_source_ids=[],
        )

    one = inputs(timeline)
    assert inputs(timeline) == one
    other_fp = inputs({**timeline, "sync_matches_fingerprint": "fp-2"})
    assert stage3_builds.compute_build_id(one, "draft") != stage3_builds.compute_build_id(
        other_fp, "draft"
    )
    bigger = inputs(timeline, size_bump=2)
    assert bigger["sources"][0]["size"] != one["sources"][0]["size"]
    assert stage3_builds.compute_build_id(one, "draft") != stage3_builds.compute_build_id(
        bigger, "draft"
    )
    assert stage3_builds.compute_build_id(one, "draft") != stage3_builds.compute_build_id(
        one, "complete"
    )


def test_build_is_current_compares_only_given_subset() -> None:
    build = {
        "inputs": {
            "timeline_sha256": "t1",
            "sync_matches_fingerprint": "f1",
            "scenario_revision_id": "rev_1",
            "normalization_revision": "n1",
        }
    }
    assert stage3_builds.build_is_current(build, {})
    assert stage3_builds.build_is_current(build, {"timeline_sha256": "t1"})
    assert not stage3_builds.build_is_current(build, {"timeline_sha256": "t2"})
    assert not stage3_builds.build_is_current(build, {"sync_matches_fingerprint": "f2"})
    assert not stage3_builds.build_is_current(build, {"scenario_revision_id": "rev_2"})
    assert not stage3_builds.build_is_current(build, {"normalization_revision": "n2"})
    legacy = {"inputs": {"timeline_sha256": "", "scenario_revision_id": ""}}
    assert stage3_builds.build_is_current(
        legacy, {"timeline_sha256": "x", "scenario_revision_id": "r"}
    )


def test_rename_failure_keeps_latest_and_leaves_no_partial_directory(client, monkeypatch) -> None:
    http, paths = _complete_setup(client)
    first = _prepare(http).get_json()["result"]["build_id"]
    latest_path = _builds_dir(paths) / "latest.json"
    latest_before = latest_path.read_bytes()
    _append_life(paths, "l9", 10.0)
    real_rename = os.rename

    def failing_rename(src, dst, *args, **kwargs):
        if Path(src).name.startswith(".tmp-"):
            raise OSError("disk exploded")
        return real_rename(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "rename", failing_rename)
    response = _prepare(http)

    assert response.status_code == 500
    assert latest_path.read_bytes() == latest_before
    assert _build_dirs(paths) == [first]  # no half-built or temporary directory


def test_publish_with_same_id_and_different_xml_is_an_invariant_error(tmp_path: Path) -> None:
    inputs = {"timeline_sha256": "t"}
    kwargs = {
        "mode": "draft",
        "complete": True,
        "inputs": inputs,
        "plan": {"items": [], "summary": {}},
        "xml_filename": "a.xml",
        "mapping_payload": {},
    }
    first = stage3_builds.publish_build(tmp_path / "builds", xml_text="<a/>", **kwargs)
    again = stage3_builds.publish_build(tmp_path / "builds", xml_text="<a/>", **kwargs)
    assert again == first
    with pytest.raises(stage3_builds.BuildInvariantError, match="not deterministic"):
        stage3_builds.publish_build(tmp_path / "builds", xml_text="<b/>", **kwargs)
    assert [p.name for p in (tmp_path / "builds").iterdir()] == [first["build_id"]]


def test_old_build_downloads_with_stale_header_after_new_build(client) -> None:
    http, paths = _complete_setup(client)
    old = _prepare(http).get_json()["result"]
    fresh_old = http.get(old["download_url"])
    assert fresh_old.status_code == 200
    assert "X-RC-Build-Stale" not in fresh_old.headers
    assert fresh_old.headers["X-RC-Build-Id"] == old["build_id"]

    _append_life(paths, "l9", 10.0)
    new = _prepare(http).get_json()["result"]
    assert new["build_id"] != old["build_id"]

    stale = http.get(f"/stage3/export/xml?project=BRIK&build_id={old['build_id']}")
    assert stale.status_code == 200
    assert stale.headers["X-RC-Build-Stale"] == "1"
    assert stale.headers["X-RC-Build-Id"] == old["build_id"]
    assert old["build_id"] in stale.headers["Content-Disposition"]
    assert stale.data == (_builds_dir(paths) / old["build_id"] / "timeline.xml").read_bytes()
    assert stale.data != (_builds_dir(paths) / new["build_id"] / "timeline.xml").read_bytes()

    current = http.get(new["download_url"])
    assert current.status_code == 200
    assert "X-RC-Build-Stale" not in current.headers


def test_download_by_build_id_validates_and_verifies_hash(client) -> None:
    http, paths = _complete_setup(client)
    build_id = _prepare(http).get_json()["result"]["build_id"]

    assert http.get("/stage3/export/xml?project=BRIK&build_id=0123456789abcdef").status_code == 404
    assert http.get("/stage3/export/xml?project=BRIK&build_id=../work").status_code == 404

    (_builds_dir(paths) / build_id / "timeline.xml").write_text("<tampered/>", encoding="utf-8")
    response = http.get(f"/stage3/export/xml?project=BRIK&build_id={build_id}")
    assert response.status_code == 500
    assert "corrupted" in response.get_json()["error"]
    assert http.get("/stage3/export/xml?project=BRIK").status_code == 500


def test_audit_scenario_prepare_change_timeline_409_prepare_serves_new_xml(client) -> None:
    http, paths = _complete_setup(client)
    first = _prepare(http).get_json()["result"]
    first_xml = http.get("/stage3/export/xml?project=BRIK")
    assert first_xml.status_code == 200

    _append_life(paths, "l9", 10.0)
    refused = http.get("/stage3/export/xml?project=BRIK")
    assert refused.status_code == 409
    assert "stage3_rebuild_required" in refused.get_json()["error"]

    second = _prepare(http).get_json()["result"]
    served = http.get("/stage3/export/xml?project=BRIK")
    assert served.status_code == 200
    assert served.headers["X-RC-Build-Id"] == second["build_id"] != first["build_id"]
    assert served.data != first_xml.data


def test_legacy_project_without_builds_downloads_as_before(client) -> None:
    http, paths = _nested_prepare_setup(client, [_segment("l1", "life", 0.0, 2.0)])
    (paths["work"] / "legacy.xml").write_text("<legacy/>", encoding="utf-8")
    (paths["work"] / "stage3_source_mapping.json").write_text(
        json.dumps({"nested_timeline_filename": "legacy.xml"}), encoding="utf-8"
    )

    response = http.get("/stage3/export/xml?project=BRIK")

    assert response.status_code == 200
    assert response.data == b"<legacy/>"
    assert "X-RC-Build-Id" not in response.headers
    assert not (paths["work"] / "builds").exists()


def test_complete_mode_with_blockers_publishes_no_build(client) -> None:
    http, paths = _complete_setup(client, wav=False)

    response = _prepare(http, "complete")

    assert response.status_code == 409
    assert not _builds_dir(paths).exists()


def test_rotation_keeps_latest_pointer_and_newest_builds(client, monkeypatch) -> None:
    monkeypatch.setattr(stage3_builds, "BUILDS_KEEP", 3)
    http, paths = _complete_setup(client)
    ids = []
    for index in range(5):
        _append_life(paths, f"l{index}", 20.0 + index)
        ids.append(_prepare(http).get_json()["result"]["build_id"])

    assert _build_dirs(paths) == sorted(ids[-3:])
    assert http.get(f"/stage3/export/xml?project=BRIK&build_id={ids[0]}").status_code == 404


def test_rotation_never_removes_build_pointed_to_by_latest(tmp_path: Path) -> None:
    root = tmp_path / "builds"
    ids = [
        stage3_builds.publish_build(
            root,
            mode="draft",
            complete=True,
            inputs={"n": index},
            plan={"items": [], "summary": {}},
            xml_text=f"<x{index}/>",
            xml_filename="x.xml",
            mapping_payload={},
        )["build_id"]
        for index in range(4)
    ]
    stage3_builds.set_latest(root, ids[0])  # the oldest build is the current one

    removed = stage3_builds.rotate_builds(root, keep=2)

    assert ids[0] not in removed
    assert sorted(p.name for p in root.iterdir() if p.is_dir()) == sorted([ids[0], ids[2], ids[3]])


def test_builds_history_is_newest_first_with_latest_pointer(client) -> None:
    http, _paths = _complete_setup(client)
    assert http.get("/stage3/builds?project=BRIK").get_json()["result"]["builds"] == []
    first = _prepare(http).get_json()["result"]["build_id"]
    second = _prepare(http, "complete").get_json()["result"]["build_id"]

    result = http.get("/stage3/builds?project=BRIK").get_json()["result"]

    assert [item["build_id"] for item in result["builds"]] == [second, first]
    assert result["latest_build_id"] == second
    newest = result["builds"][0]
    assert set(newest) == {"build_id", "created_at", "mode", "complete", "summary"}
    assert newest["mode"] == "complete"
    assert newest["summary"]["items"] >= 2
    assert api_stage3_routes.stage3_builds is stage3_builds
