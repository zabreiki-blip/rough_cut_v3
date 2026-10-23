"""Manual LIFE assignment: carry-over across normalization revisions, unassign, server block_type."""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pytest

import api
from stage2.sync_models import SyncSource
from stage2.timeline_parser import parse_normalized_timeline
from stage_common import current_sync_matches, life_text_sha256


@pytest.fixture()
def life_api():
    with TemporaryDirectory() as temporary:
        base = Path(temporary).resolve()
        projects = base / "projects"
        projects.mkdir()
        with (
            patch.object(api, "ROOT_DIR", base),
            patch.object(api, "PROJECTS_DIR", projects),
        ):
            api.app.config.update(TESTING=True)
            api.app.config.pop("API_TOKEN", None)
            yield api.app.test_client(), projects


def _write_normalization(projects: Path, *, revision: int, life_text: str = "Лайф А") -> None:
    path = projects / "demo" / "work" / "scenario_normalized.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "source": "",
        "source_hash": "",
        "status": "confirmed",
        "revision": revision,
        "scenario_revision_id": "",
        "blocks": [
            {
                "uid": "life-a",
                "order": 1,
                "type": "life",
                "original_text": life_text,
                "normalized_text": life_text,
            },
            {
                "uid": "sync-a",
                "order": 2,
                "type": "sync",
                "speaker": "Иван",
                "original_text": "Иван: 00:10 Реплика синхрона",
                "normalized_text": "Иван: 00:10 Реплика синхрона",
                "timecode_start": "00:10",
            },
        ],
    }
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")


def _add_source(projects: Path) -> None:
    paths = api._ensure_project("demo")
    source_store, _match_store = api._sync_stores(paths)
    source_store.save(
        SyncSource(id="src1", speaker="Иван", premiere_sequence="SEQ", sequence_id="seq-1")
    )


def _patch(client, uid: str, **body):
    return client.patch(f"/stage2/sync-matches/{uid}", json={"project": "demo", **body})


def _assign_life(client, uid: str = "life-a"):
    response = _patch(
        client, uid, status="confirmed", source_id="src1", source_in_sec=2.0, source_out_sec=6.0
    )
    assert response.status_code == 200, response.get_json()
    return response


def _stored(projects: Path) -> dict:
    paths = api._ensure_project("demo")
    return {item.segment_id: item for item in api._sync_stores(paths)[1].list()}


def _get_matches(client) -> dict:
    response = client.get("/stage2/sync-matches?project=demo")
    assert response.status_code == 200
    return response.get_json()["result"]


def test_patch_life_stores_text_hash_and_server_block_type(life_api) -> None:
    client, projects = life_api
    _write_normalization(projects, revision=7)
    _add_source(projects)

    _assign_life(client)

    metadata = _stored(projects)["life-a"].metadata
    assert metadata["block_type"] == "life"
    assert metadata["life_text_sha256"] == life_text_sha256("Лайф А")


def test_client_block_type_never_hides_a_sync_unit_from_the_summary(life_api) -> None:
    client, projects = life_api
    _write_normalization(projects, revision=7)
    _add_source(projects)

    response = _patch(
        client,
        "sync-a",
        status="confirmed",
        source_id="src1",
        source_in_sec=1.0,
        source_out_sec=3.0,
        metadata={"block_type": "life"},
    )

    assert response.status_code == 200, response.get_json()
    stored = _stored(projects)["sync-a"]
    assert "block_type" not in stored.metadata
    summary = api._sync_summary([stored])
    assert summary["confirmed"] == 1 and summary["life_assigned"] == 0
    result = _get_matches(client)
    assert result["confirmed"] == 1 and result["life_assigned"] == 0


def test_client_block_type_is_overwritten_to_life_for_a_life_unit(life_api) -> None:
    client, projects = life_api
    _write_normalization(projects, revision=7)
    _add_source(projects)

    _patch(
        client,
        "life-a",
        status="confirmed",
        source_id="src1",
        source_in_sec=1.0,
        source_out_sec=3.0,
        metadata={"block_type": "sync"},
    )

    assert _stored(projects)["life-a"].metadata["block_type"] == "life"


def test_life_assignment_survives_revision_bump_when_text_is_unchanged(life_api) -> None:
    client, projects = life_api
    _write_normalization(projects, revision=7)
    _add_source(projects)
    _assign_life(client)

    _write_normalization(projects, revision=8)  # re-saved normalization, same LIFE text

    result = _get_matches(client)
    assert [item["segment_id"] for item in result["matches"]] == ["life-a"]
    assert result["life_assigned"] == 1
    assert result["stale_life_assignments"] == []
    normalization = json.loads(
        (projects / "demo" / "work" / "scenario_normalized.json").read_text(encoding="utf-8")
    )
    current, stale = current_sync_matches(list(_stored(projects).values()), normalization)
    assert [item.segment_id for item in current] == ["life-a"] and stale == []
    timeline = parse_normalized_timeline(
        normalization,
        projects,
        projects,
        "TEST",
        sync_matches={item.segment_id: item for item in current},
        sync_sources={"src1": {"premiere_sequence": "SEQ", "sequence_id": "seq-1"}},
    )
    life = next(segment for segment in timeline.segments if segment.type == "life")
    assert life.source_id == "src1"
    assert (life.source_in_sec, life.source_out_sec) == (2.0, 6.0)


def test_life_assignment_with_changed_text_is_dropped_and_reported_stale(life_api) -> None:
    client, projects = life_api
    _write_normalization(projects, revision=7)
    _add_source(projects)
    _assign_life(client)

    _write_normalization(projects, revision=8, life_text="Лайф Б, другой текст")

    result = _get_matches(client)
    assert result["matches"] == []
    assert result["stale_life_assignments"] == ["life-a"]
    normalization = json.loads(
        (projects / "demo" / "work" / "scenario_normalized.json").read_text(encoding="utf-8")
    )
    current, stale = current_sync_matches(list(_stored(projects).values()), normalization)
    assert current == [] and stale == ["life-a"]
    timeline = parse_normalized_timeline(
        normalization, projects, projects, "TEST", sync_matches={}, sync_sources={}
    )
    life = next(segment for segment in timeline.segments if segment.type == "life")
    assert life.source_id == ""


def test_life_assignment_can_be_removed_with_not_found(life_api) -> None:
    client, projects = life_api
    _write_normalization(projects, revision=7)
    _add_source(projects)
    _assign_life(client)

    response = _patch(client, "life-a", status="not_found")

    assert response.status_code == 200, response.get_json()
    (saved,) = response.get_json()["result"]["matches"]
    assert saved["status"] == "not_found" and not saved["source_id"]
    assert "life-a" not in _stored(projects)
    result = _get_matches(client)
    assert result["matches"] == [] and result["life_assigned"] == 0
    normalization = json.loads(
        (projects / "demo" / "work" / "scenario_normalized.json").read_text(encoding="utf-8")
    )
    current, _stale = current_sync_matches(list(_stored(projects).values()), normalization)
    assert current == []


def test_unassigning_a_sync_unit_with_not_found_still_keeps_the_match(life_api) -> None:
    client, projects = life_api
    _write_normalization(projects, revision=7)

    response = _patch(client, "sync-a", status="not_found")

    assert response.status_code == 200, response.get_json()
    assert _stored(projects)["sync-a"].status == "not_found"
