"""Text-less host stand-up (``ВЕДУЩИЙ № N``) and ``ОЗВ:`` voice-over labels."""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pytest

import api
from stage1.normalizer import generate_normalization, validate_for_confirmation_detailed
from stage2.assembly_plan import build_assembly_plan, validate_plan_complete
from stage2.sync_models import SyncSource
from stage2.timeline_parser import parse_normalized_timeline
from stage_common import placeholder_reason


def _normalize(tmp_path: Path, *paragraphs: str) -> dict:
    source = tmp_path / "scenario.txt"
    source.write_text("\n\n".join(paragraphs) + "\n", encoding="utf-8")
    return generate_normalization(
        source,
        tmp_path / "normalized.json",
        memory_path=tmp_path / "memory.sqlite3",
        use_llm=False,
    )


@pytest.mark.parametrize(
    ("text", "number", "remark"),
    [
        ("ВЕДУЩИЙ № 1", "№ 1", ""),
        ("ВЕДУЩИЙ № 2 Реквизит: рассекреченный отчёт", "№ 2", "Реквизит: рассекреченный отчёт"),
        (
            "ВЕДУЩИЙ № 4 (смотрит видеохронику на мониторе)",
            "№ 4",
            "смотрит видеохронику на мониторе",
        ),
    ],
)
def test_host_standup_forms_are_normalized_without_tts(
    tmp_path: Path, text: str, number: str, remark: str
) -> None:
    result = _normalize(tmp_path, text, "ЗК: Дальше закадр.")
    block = result["blocks"][0]
    assert block["type"] == "host"
    assert block["host_standup"] == number
    assert block["text_for_tts"] == ""
    assert block["source_description"] == remark


def test_host_with_real_text_is_not_a_standup(tmp_path: Path) -> None:
    result = _normalize(tmp_path, "ВЕДУЩИЙ: Сегодня мы расскажем о многом.")
    block = result["blocks"][0]
    assert block["type"] == "host"
    assert not block.get("host_standup")


def test_voiceover_dub_label_is_voiceover_with_timecode_in_metadata(tmp_path: Path) -> None:
    result = _normalize(tmp_path, "ОЗВ: 00.10 Текст озвучки здесь.", "ОЗВУЧКА: 00:12 Ещё текст.")
    first, second = result["blocks"]
    assert (first["type"], second["type"]) == ("voiceover", "voiceover")
    assert first["text_for_tts"] == "Текст озвучки здесь."
    assert second["text_for_tts"] == "Ещё текст."
    assert first["timecode_start"] == "00.10"
    assert "00.10" not in first["text_for_tts"]


def test_voiceover_dub_without_text_is_reported(tmp_path: Path) -> None:
    result = _normalize(tmp_path, "ОЗВ: 00.15", "ЗК: Хвост.")
    assert result["blocks"][0]["type"] == "voiceover"
    issues = validate_for_confirmation_detailed(result)
    assert any(i["code"] == "empty_tts" and i["severity"] == "error" for i in issues)


def test_old_json_without_host_standup_key_reads_with_default() -> None:
    from stage1.normalization_models import NormalizedBlock

    assert NormalizedBlock.__dataclass_fields__["host_standup"].default == ""


def _standup_block(uid: str = "host-1", *, remark: str = "Реквизит: отчёт") -> dict:
    return {
        "uid": uid,
        "order": 1,
        "type": "host",
        "original_text": "ВЕДУЩИЙ № 2",
        "normalized_text": "№ 2",
        "text_for_tts": "",
        "host_standup": "№ 2",
        "source_description": remark,
    }


def test_standup_is_excluded_from_auto_units_but_storable_on_request() -> None:
    block = _standup_block()
    assert api._sync_alignment_units([block]) == []
    (unit,) = api._sync_alignment_units([block], include_host_standup=True)
    assert unit["segment_id"] == "host-1"
    assert unit["text"] == "Реквизит: отчёт"


def _document(block: dict) -> dict:
    return {
        "source": "",
        "source_hash": "",
        "status": "confirmed",
        "revision": 1,
        "scenario_revision_id": "",
        "blocks": [block],
    }


def test_standup_timeline_stub_has_card_text_reason_task_and_blocker(tmp_path: Path) -> None:
    timeline = parse_normalized_timeline(
        _document(_standup_block()), tmp_path, tmp_path, "TEST", sync_matches={}, sync_sources={}
    )
    (segment,) = timeline.segments
    assert segment.type == "host"
    assert segment.duration_sec == 10.0
    assert segment.speaker == "ВЕДУЩИЙ № 2"
    assert segment.text == "Реквизит: отчёт"
    assert segment.placeholder_path.endswith(".png")
    data = segment.to_dict() if hasattr(segment, "to_dict") else vars(segment)
    assert placeholder_reason(data) == "host_standup"

    plan = build_assembly_plan(timeline)
    (item,) = plan["items"]
    assert item["reason"] == "host_standup"
    assert [task["kind"] for task in item["tasks"]] == ["assign_host_take"]
    blockers = validate_plan_complete(plan)
    assert blockers, "an unassigned stand-up must block a complete assembly"


@pytest.fixture()
def standup_api():
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


def test_manual_take_assignment_turns_stub_into_clip(standup_api) -> None:
    client, projects = standup_api
    path = projects / "demo" / "work" / "scenario_normalized.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_document(_standup_block()), ensure_ascii=False), encoding="utf-8")
    paths = api._ensure_project("demo")
    source_store, match_store = api._sync_stores(paths)
    source_store.save(SyncSource(id="src1", speaker="", premiere_sequence="SEQ", sequence_id="s1"))

    response = client.patch(
        "/stage2/sync-matches/host-1",
        json={
            "project": "demo",
            "status": "confirmed",
            "source_id": "src1",
            "source_in_sec": 2.0,
            "source_out_sec": 7.5,
        },
    )
    assert response.status_code == 200, response.get_json()
    stored = {item.segment_id: item for item in match_store.list()}
    assert stored["host-1"].metadata["block_type"] == "life"  # never auto-matched

    timeline = parse_normalized_timeline(
        json.loads(path.read_text(encoding="utf-8")),
        projects,
        projects,
        "TEST",
        sync_matches=stored,
        sync_sources={"src1": {"premiere_sequence": "SEQ", "sequence_id": "s1"}},
    )
    (segment,) = timeline.segments
    assert segment.source_id == "src1"
    assert (segment.source_in_sec, segment.source_out_sec) == (2.0, 7.5)
    assert segment.duration_sec == 5.5
    assert placeholder_reason(segment.to_dict(), mapped={"src1"}) is None


def test_standup_card_png_is_generated_from_segment_text(tmp_path: Path) -> None:
    from stage2.placeholder import generate_versioned_placeholder

    timeline = parse_normalized_timeline(
        _document(_standup_block()), tmp_path, tmp_path, "TEST", sync_matches={}, sync_sources={}
    )
    (segment,) = timeline.segments
    card = generate_versioned_placeholder(
        block_uid=segment.block_uid,
        speaker=segment.speaker,
        role=segment.role,
        timecode=segment.timecode,
        text=segment.text,
        output_dir=tmp_path,
        segment_type=segment.type,
        width=timeline.width,
        height=timeline.height,
    )
    assert Path(card).is_file() and Path(card).suffix == ".png"
