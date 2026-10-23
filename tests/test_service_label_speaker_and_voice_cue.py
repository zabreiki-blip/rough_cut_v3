"""Voice-reading cue ends sync-speaker inheritance; a service label is never a speaker."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from stage1.confirmation_checks import has_speaker_name, looks_like_caps_source_note
from stage1.normalizer import generate_normalization, validate_for_confirmation_detailed


def _run(tmp_path: Path, paragraphs: list[str], **kwargs: Any) -> list[dict[str, Any]]:
    source = tmp_path / "scenario.txt"
    source.write_text("\n\n".join(paragraphs) + "\n", encoding="utf-8")
    return generate_normalization(
        source,
        tmp_path / "out.json",
        memory_path=tmp_path / "memory.sqlite3",
        use_llm=kwargs.pop("use_llm", False),
        **kwargs,
    )["blocks"]


SYNC = "ИВАНОВА 00:11:00 СКЛ его обзывают попутчиком, и он обижается."


@pytest.mark.parametrize(
    "cue", ["Голос «Поэта»:", "Голос Поэта:", "Читает Иван Петров:", "Голос за кадром:"]
)
def test_reading_cue_stops_sync_speaker_inheritance(tmp_path: Path, cue: str) -> None:
    blocks = _run(tmp_path, [SYNC, cue, "... кому я, к черту, попутчик!"])
    assert blocks[0]["type"] == "sync" and blocks[0]["speaker"] == "ИВАНОВА"
    assert blocks[1]["type"] == "technical"
    quote = blocks[2]
    assert quote["type"] == "voiceover"
    assert quote["speaker"] == ""
    assert quote["text_for_tts"]


def test_reading_cue_beats_llm_sync_with_inherited_speaker(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def classifier(batch: list[dict[str, Any]], hint: str) -> list[dict[str, Any]]:
        calls.append([item["uid"] for item in batch])
        return [
            {"uid": i["uid"], "type": "sync", "speaker": "ИВАНОВА", "confidence": 0.95}
            for i in batch
        ]

    blocks = _run(
        tmp_path,
        [SYNC, "Голос «Поэта»:", "... кому я, к черту, попутчик!"],
        use_llm=True,
        classifier=classifier,
    )
    assert blocks[2]["type"] == "voiceover" and blocks[2]["speaker"] == ""


def test_timecoded_quote_after_cue_is_not_voiceover(tmp_path: Path) -> None:
    blocks = _run(tmp_path, [SYNC, "Голос «Поэта»:", "00:12:00 Он так сказал."])
    assert blocks[2]["type"] != "voiceover"


NOTE = "СИНХ ЭРНСТА – ИЗ ФАЙЛОВ, КОТОРЫЙ ДАЛ ПЕРВЫЙ КАНАЛ – съемки в центр зотов"


def test_caps_source_note_after_sync_label_is_technical(tmp_path: Path) -> None:
    block = _run(tmp_path, [NOTE])[0]
    assert block["type"] == "technical"
    assert block["speaker"] == ""


def test_ordinary_sync_with_name_is_untouched(tmp_path: Path) -> None:
    block = _run(tmp_path, ["СНХ. Отец: очень похож и талантлив, как мама."])[0]
    assert block["type"] == "sync" and block["speaker"] == "Отец"


@pytest.mark.parametrize("speaker", ["СИНХ", "СНХ", "ЗК", "синх.", "СНХ 2", ""])
def test_service_label_is_not_a_speaker_name(speaker: str) -> None:
    assert not has_speaker_name(speaker)


@pytest.mark.parametrize("speaker", ["Отец", "КРАСНОВА", "Анна Лесная"])
def test_name_is_a_speaker_name(speaker: str) -> None:
    assert has_speaker_name(speaker)


def test_caps_source_note_detector() -> None:
    assert looks_like_caps_source_note("ЭРНСТА – ИЗ ФАЙЛОВ, КОТОРЫЙ ДАЛ ПЕРВЫЙ КАНАЛ – съемки")
    assert not looks_like_caps_source_note("00:01:00 ИЗ ФАЙЛОВ КОТОРЫЙ ДАЛ ПЕРВЫЙ КАНАЛ")
    assert not looks_like_caps_source_note("Он рассказал, как жил в доме у канала.")


def _scenario(blocks: list[dict[str, Any]]) -> dict[str, Any]:
    return {"source": "s", "source_hash": "h", "blocks": blocks, "status": "draft"}


def _sync_block(**kwargs: Any) -> dict[str, Any]:
    base = {
        "uid": "b1",
        "order": 1,
        "original_text": "СИНХ Он сказал это тихо.",
        "normalized_text": "Он сказал это тихо.",
        "type": "sync",
        "speaker": "СИНХ",
        "source": "builtin_rule",
    }
    return {**base, **kwargs}


def _codes(blocks: list[dict[str, Any]], severity: str = "error") -> list[str]:
    return [
        i["code"]
        for i in validate_for_confirmation_detailed(_scenario(blocks))
        if i["severity"] == severity
    ]


def test_sync_with_service_label_speaker_is_error() -> None:
    assert "sync_no_speaker" in _codes([_sync_block()])


def test_sync_with_manual_speaker_is_not_flagged_as_service_label() -> None:
    assert "sync_no_speaker" not in _codes([_sync_block(manual_fields=["speaker"])])


def test_saved_sync_with_caps_source_note_is_error() -> None:
    block = _sync_block(
        original_text=NOTE,
        normalized_text="ЭРНСТА – ИЗ ФАЙЛОВ, КОТОРЫЙ ДАЛ ПЕРВЫЙ КАНАЛ – съемки в центр зотов",
        speaker="ЭРНСТА",
    )
    assert "sync_source_note" in _codes([block])
    assert "sync_source_note" in _codes([{**block, "manual_fields": ["type"]}], "warning")


def test_bare_service_label_sync_gets_no_speaker_and_blocks_confirmation(tmp_path: Path) -> None:
    out = tmp_path / "out.json"
    block = _run(tmp_path, ["СИНХ Он сказал это очень тихо и очень долго, никто не слышал."])[0]
    assert block["type"] == "sync" and block["speaker"] == ""
    import json

    issues = validate_for_confirmation_detailed(json.loads(out.read_text(encoding="utf-8")))
    assert any(i["code"] == "sync_no_speaker" and i["severity"] == "error" for i in issues)
