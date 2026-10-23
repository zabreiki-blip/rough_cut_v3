"""Manual sync/host confirmations survive a normalization revision bump when the text is unchanged."""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pytest

import api
from stage2.sync_models import SyncSource
from stage2.timeline_parser import parse_normalized_timeline
from stage_common import (
    current_sync_matches,
    normalization_unit_texts,
    unit_text_sha256,
)


@pytest.fixture()
def sync_api():
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


def _normalization(*, revision: int, sync_text: str = "Реплика синхрона") -> dict:
    return {
        "source": "",
        "source_hash": "",
        "status": "confirmed",
        "revision": revision,
        "scenario_revision_id": "",
        "blocks": [
            {
                "uid": "sync-a",
                "order": 1,
                "type": "sync",
                "speaker": "Иван",
                "original_text": f"Иван: 00:10 {sync_text}",
                "normalized_text": f"Иван: 00:10 {sync_text}",
                "timecode_start": "00:10",
            },
            {
                "uid": "host-a",
                "order": 2,
                "type": "host",
                "speaker": "Ведущий",
                "original_text": "Текст ведущего",
                "normalized_text": "Текст ведущего",
            },
        ],
    }


def _write(projects: Path, **kwargs) -> dict:
    document = _normalization(**kwargs)
    path = projects / "demo" / "work" / "scenario_normalized.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    return document


def _add_source(projects: Path) -> None:
    paths = api._ensure_project("demo")
    source_store, _ = api._sync_stores(paths)
    source_store.save(
        SyncSource(id="src1", speaker="Иван", premiere_sequence="SEQ", sequence_id="seq-1")
    )


def _confirm(client, uid: str = "sync-a"):
    response = client.patch(
        f"/stage2/sync-matches/{uid}",
        json={
            "project": "demo",
            "status": "confirmed",
            "source_id": "src1",
            "source_in_sec": 12.0,
            "source_out_sec": 18.5,
        },
    )
    assert response.status_code == 200, response.get_json()


def _store(projects: Path):
    return api._sync_stores(api._ensure_project("demo"))[1]


def _stored(projects: Path) -> dict:
    return {item.segment_id: item for item in _store(projects).list()}


def _get(client) -> dict:
    response = client.get("/stage2/sync-matches?project=demo")
    assert response.status_code == 200
    return response.get_json()["result"]


def test_patch_confirmed_stores_unit_text_hash_for_sync_and_host(sync_api) -> None:
    client, projects = sync_api
    document = _write(projects, revision=15)
    _add_source(projects)

    _confirm(client, "sync-a")
    _confirm(client, "host-a")

    stored = _stored(projects)
    texts = normalization_unit_texts(document)
    for uid in ("sync-a", "host-a"):
        assert stored[uid].metadata["unit_text_sha256"] == unit_text_sha256(texts[uid])
    assert "life_text_sha256" not in stored["sync-a"].metadata


def test_patch_not_confirmed_does_not_store_hash(sync_api) -> None:
    client, projects = sync_api
    _write(projects, revision=15)
    _add_source(projects)

    response = client.patch(
        "/stage2/sync-matches/sync-a",
        json={"project": "demo", "status": "needs_review", "source_id": "src1"},
    )

    assert response.status_code == 200, response.get_json()
    assert "unit_text_sha256" not in _stored(projects)["sync-a"].metadata


def test_confirmed_sync_survives_revision_bump_in_get_and_timeline(sync_api) -> None:
    client, projects = sync_api
    _write(projects, revision=15)
    _add_source(projects)
    _confirm(client)

    document = _write(projects, revision=19)  # re-confirmed normalization, same text

    result = _get(client)
    assert [(m["segment_id"], m["status"]) for m in result["matches"]] == [("sync-a", "confirmed")]
    assert result["confirmed"] == 1
    assert result["stale_confirmations"] == []
    assert result["stale_life_assignments"] == []
    current, stale = current_sync_matches(list(_stored(projects).values()), document)
    assert [m.segment_id for m in current] == ["sync-a"] and stale == []
    timeline = parse_normalized_timeline(
        document,
        projects,
        projects,
        "TEST",
        sync_matches={m.segment_id: m for m in current},
        sync_sources={"src1": {"premiere_sequence": "SEQ", "sequence_id": "seq-1"}},
    )
    segment = next(s for s in timeline.segments if s.type == "snh")
    assert segment.source_id == "src1"
    assert (segment.source_in_sec, segment.source_out_sec) == (12.0, 18.5)
    assert segment.match_status == "confirmed"


def test_confirmed_sync_with_changed_text_is_not_applied_and_reported_stale(sync_api) -> None:
    client, projects = sync_api
    _write(projects, revision=15)
    _add_source(projects)
    _confirm(client)

    document = _write(projects, revision=19, sync_text="Совсем другая реплика")

    result = _get(client)
    assert result["matches"] == []
    assert result["stale_confirmations"] == ["sync-a"]
    assert result["stale_life_assignments"] == []
    current, stale = current_sync_matches(list(_stored(projects).values()), document)
    assert current == [] and stale == ["sync-a"]


def test_confirmed_sync_with_missing_source_is_not_carried(sync_api) -> None:
    client, projects = sync_api
    _write(projects, revision=15)
    _add_source(projects)
    _confirm(client)
    paths = api._ensure_project("demo")
    api._sync_stores(paths)[0].replace_all([])

    _write(projects, revision=19)

    assert _get(client)["matches"] == []


def _strip_hash(projects: Path, *, query_text: str) -> None:
    store = _store(projects)
    items = store.list()
    for item in items:
        item.metadata.pop("unit_text_sha256", None)
        item.metadata.pop("life_text_sha256", None)
        item.query_text = query_text
    store.replace_all(items)


def test_legacy_confirmed_without_hash_is_carried_when_query_text_equals_unit_text(
    sync_api,
) -> None:
    client, projects = sync_api
    _write(projects, revision=15)
    _add_source(projects)
    _confirm(client)
    _strip_hash(projects, query_text="  реплика   СИНХРОНА ")

    _write(projects, revision=19)

    result = _get(client)
    assert [m["segment_id"] for m in result["matches"]] == ["sync-a"]
    assert result["stale_confirmations"] == []


def test_legacy_confirmed_without_hash_and_other_query_text_is_stale(sync_api) -> None:
    client, projects = sync_api
    _write(projects, revision=15)
    _add_source(projects)
    _confirm(client)
    _strip_hash(projects, query_text="Старая реплика")

    _write(projects, revision=19)

    result = _get(client)
    assert result["matches"] == []
    assert result["stale_confirmations"] == ["sync-a"]


def test_patch_after_carry_over_keeps_whole_unit_and_restamps_revision(sync_api) -> None:
    client, projects = sync_api
    _write(projects, revision=15)
    _add_source(projects)
    _confirm(client)
    _write(projects, revision=19)

    _confirm(client)  # re-confirm after the bump

    metadata = _stored(projects)["sync-a"].metadata
    assert metadata["normalization_revision"] == 19
    assert "unit_text_sha256" in metadata
