"""``ВЕДУЩИЙ № N`` header followed by the stand-up text: the header is a label, the text is host."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

import api
from stage1.normalizer import (
    confirm_normalization,
    generate_normalization,
    save_normalization,
)
from stage2.timeline_parser import parse_normalized_timeline
from stage_common import normalization_unit_texts

UIP_LIKE = [
    "ЗК: Вступление закадром.",
    "ВЕДУЩИЙ № 1",
    "Эта скандальная история случилась двадцать лет назад. Давайте разбираться.",
    "ЗК1.1",
    "Рассказ после метки.",
    "ВЕДУЩИЙ № 2 Реквизит: рассекреченный отчёт ВВС США",
    "Перечень таких странных происшествий открывает случай 1956 года.",
    "У меня в руках документ, который долго скрывали.",
    "ЗК2.1",
    "Ещё закадровый текст.",
    "ВЕДУЩИЙ № 3",
    "А вот фотоулика, к которой стоит приглядеться.",
    "ЗК3.1",
    "Третий закадровый текст.",
    "ВЕДУЩИЙ № 4 (смотрит видеохронику на мониторе)",
    "Эти уникальные кадры отсняли операторы ВВС США.",
    "ЗК4.1",
    "Четвёртый закадровый текст.",
    "ВЕДУЩИЙ № 5",
    "ЗК5.1",
    "Финальный закадровый текст.",
]


def _source(tmp_path: Path, paragraphs: list[str] = UIP_LIKE) -> Path:
    source = tmp_path / "scenario.txt"
    source.write_text("\n\n".join(paragraphs) + "\n", encoding="utf-8")
    return source


def _normalize(tmp_path: Path, paragraphs: list[str] = UIP_LIKE, **kwargs: Any) -> dict:
    return generate_normalization(
        _source(tmp_path, paragraphs),
        tmp_path / "normalized.json",
        memory_path=tmp_path / "memory.sqlite3",
        use_llm=False,
        **kwargs,
    )


def _by_text(document: dict, prefix: str) -> dict:
    (block,) = [b for b in document["blocks"] if b["original_text"].startswith(prefix)]
    return block


def _as_saved_before_the_rule(document: dict) -> dict:
    """The shape of an already normalized project: LLM voiceover text, stub header."""
    old = copy.deepcopy(document)
    for block in old["blocks"]:
        if block["host_standup"]:
            block["label_only"] = False
        elif block["type"] == "host" and not block["original_label"]:
            block.update(type="voiceover", source="llm", speaker="")
    return old


def test_generation_types_text_after_header_as_host_without_llm(tmp_path: Path) -> None:
    seen: list[str] = []

    def classifier(batch, hint):
        seen.extend(json.dumps(item, ensure_ascii=False) for item in batch)
        return [
            {"id": item["id"], "type": "voiceover", "label": "", "confidence": 1.0}
            for item in batch
        ]

    result = generate_normalization(
        _source(tmp_path),
        tmp_path / "normalized.json",
        memory_path=tmp_path / "memory.sqlite3",
        use_llm=True,
        classifier=classifier,
    )
    # The deterministic rule runs before the LLM: the stand-up texts are never sent to it.
    assert not any("скандальная" in item or "Перечень" in item for item in seen), seen
    for prefix in ("Эта скандальная", "Перечень таких", "У меня в руках", "А вот фотоулика"):
        assert _by_text(result, prefix)["type"] == "host", prefix
    for prefix in ("ВЕДУЩИЙ № 1", "ВЕДУЩИЙ № 2", "ВЕДУЩИЙ № 3", "ВЕДУЩИЙ № 4"):
        header = _by_text(result, prefix)
        assert header["label_only"] is True, prefix
        assert header["host_standup"].startswith("№")
    # The narration around the stand-ups is untouched.
    assert _by_text(result, "Рассказ после метки")["type"] == "voiceover"
    assert _by_text(result, "Финальный")["type"] == "voiceover"


def test_remark_is_kept_for_the_text_block(tmp_path: Path) -> None:
    result = _normalize(tmp_path)
    header = _by_text(result, "ВЕДУЩИЙ № 2")
    first = _by_text(result, "Перечень")
    assert header["source_description"] == "Реквизит: рассекреченный отчёт ВВС США"
    assert "Реквизит: рассекреченный отчёт ВВС США" in first["notes"]
    assert "№ 2" in first["notes"]
    # Only the first text block carries the remark.
    assert _by_text(result, "У меня в руках")["notes"] == ""


def test_standup_without_text_stays_a_card_placeholder(tmp_path: Path) -> None:
    result = _normalize(tmp_path)
    header = _by_text(result, "ВЕДУЩИЙ № 5")
    assert header["type"] == "host"
    assert header["host_standup"] == "№ 5"
    assert header["label_only"] is False


def test_existing_document_is_repaired_by_confirmation_without_llm(tmp_path: Path) -> None:
    generated = _normalize(tmp_path)
    old = _as_saved_before_the_rule(generated)
    assert _by_text(old, "Эта скандальная")["type"] == "voiceover"
    normalized = tmp_path / "normalized.json"
    normalized.write_text(json.dumps(old, ensure_ascii=False), encoding="utf-8")

    confirmed = confirm_normalization(
        normalized,
        tmp_path / "prepared.json",
        tmp_path / "raw.txt",
        memory_path=tmp_path / "memory.sqlite3",
        expected_revision=old["revision"],
    )

    for prefix in ("Эта скандальная", "Перечень таких", "У меня в руках", "А вот фотоулика"):
        assert _by_text(confirmed, prefix)["type"] == "host", prefix
    assert _by_text(confirmed, "ВЕДУЩИЙ № 1")["label_only"] is True
    assert _by_text(confirmed, "ВЕДУЩИЙ № 5")["label_only"] is False
    on_disk = json.loads(normalized.read_text(encoding="utf-8"))
    assert _by_text(on_disk, "Перечень")["type"] == "host"
    prepared = json.loads((tmp_path / "prepared.json").read_text(encoding="utf-8"))
    spoken = " ".join(item["text_for_tts"] for item in prepared["items"])
    assert "скандальная" not in spoken and "Перечень" not in spoken
    assert "Рассказ после метки." in spoken


def test_save_applies_the_rule_to_an_existing_document(tmp_path: Path) -> None:
    generated = _normalize(tmp_path)
    old = _as_saved_before_the_rule(generated)
    normalized = tmp_path / "normalized.json"
    normalized.write_text(json.dumps(old, ensure_ascii=False), encoding="utf-8")

    saved = save_normalization(normalized, old, expected_revision=old["revision"])

    assert _by_text(saved, "Эта скандальная")["type"] == "host"
    assert _by_text(saved, "ВЕДУЩИЙ № 1")["label_only"] is True
    # Not a reviewer edit: the rule must not pin the fields as manual.
    assert _by_text(saved, "Эта скандальная")["manual_fields"] == []
    again = save_normalization(normalized, saved, expected_revision=saved["revision"])
    assert [b["type"] for b in again["blocks"]] == [b["type"] for b in saved["blocks"]]


@pytest.mark.parametrize(
    "pin",
    [
        {"manual_fields": ["type"]},
        {"manually_edited": True},
        {"manual_fields": ["normalized_text"], "manually_edited": True},
        {"source": "manual"},
    ],
)
def test_manual_edits_are_not_overridden(tmp_path: Path, pin: dict) -> None:
    old = _as_saved_before_the_rule(_normalize(tmp_path))
    pinned = _by_text(old, "Перечень")
    pinned.update(pin)
    normalized = tmp_path / "normalized.json"
    normalized.write_text(json.dumps(old, ensure_ascii=False), encoding="utf-8")

    saved = save_normalization(normalized, old, expected_revision=old["revision"])

    assert _by_text(saved, "Перечень")["type"] == "voiceover"
    # The run stops at the reviewer's decision.
    assert _by_text(saved, "У меня в руках")["type"] == "voiceover"
    # Nothing was taken from the stand-up, so it stays a placeholder card.
    assert _by_text(saved, "ВЕДУЩИЙ № 2")["label_only"] is False
    # Other stand-ups are unaffected.
    assert _by_text(saved, "Эта скандальная")["type"] == "host"


def test_confirmed_label_is_not_overridden(tmp_path: Path) -> None:
    old = _as_saved_before_the_rule(_normalize(tmp_path))
    block = _by_text(old, "А вот фотоулика")
    block.update(original_label="Фотоулика", label_confirmed=True)
    normalized = tmp_path / "normalized.json"
    normalized.write_text(json.dumps(old, ensure_ascii=False), encoding="utf-8")

    saved = save_normalization(normalized, old, expected_revision=old["revision"])

    assert _by_text(saved, "А вот фотоулика")["type"] == "voiceover"
    assert _by_text(saved, "ВЕДУЩИЙ № 3")["label_only"] is False


def test_already_host_text_is_kept_and_header_is_absorbed_into_the_run(tmp_path: Path) -> None:
    old = _as_saved_before_the_rule(_normalize(tmp_path))
    block = _by_text(old, "Эти уникальные")
    block.update(
        type="host",
        source="manual",
        original_label="ВЕДУЩИЙ",
        label_confirmed=True,
        speaker="ВЕДУЩИЙ",
        manual_fields=["type", "original_label", "label_confirmed", "notes"],
        notes="моя заметка",
    )
    normalized = tmp_path / "normalized.json"
    normalized.write_text(json.dumps(old, ensure_ascii=False), encoding="utf-8")

    saved = save_normalization(normalized, old, expected_revision=old["revision"])

    kept = _by_text(saved, "Эти уникальные")
    assert (kept["type"], kept["source"], kept["notes"]) == ("host", "manual", "моя заметка")
    header = _by_text(saved, "ВЕДУЩИЙ № 4")
    assert header["label_only"] is True
    # The remark stays on the header when the text block's notes are the reviewer's.
    assert header["source_description"] == "смотрит видеохронику на мониторе"


def _timeline(document: dict, tmp_path: Path):
    document = {**document, "status": "confirmed"}
    return parse_normalized_timeline(
        document, tmp_path, tmp_path, "TEST", sync_matches={}, sync_sources={}
    )


def test_timeline_has_no_stub_for_a_header_with_text(tmp_path: Path) -> None:
    result = _normalize(tmp_path)
    timeline = _timeline(result, tmp_path)
    host = [s for s in timeline.segments if s.type == "host"]
    stubs = [s for s in host if s.host_standup]
    assert [s.host_standup for s in stubs] == ["№ 5"]
    texts = " ".join(s.text for s in host)
    assert "№ 1" not in texts and "№ 3" not in texts
    # Five stand-up texts (№1, №2 twice, №3, №4) plus the card of the text-less №5.
    assert len(host) == 6


def test_alignment_units_skip_header_labels(tmp_path: Path) -> None:
    result = _normalize(tmp_path)
    blocks = [b for b in result["blocks"] if b["type"] == "host"]
    units = api._sync_alignment_units(blocks, include_host_standup=True)
    unit_texts = [u["text"] for u in units]
    assert not any(t.startswith("№") for t in unit_texts), unit_texts
    standups = [u for u in units if u["block"]["host_standup"]]
    assert [u["block"]["host_standup"] for u in standups] == ["№ 5"]
    texts = normalization_unit_texts(result)
    assert not any(str(t).startswith("№") for t in texts.values())
