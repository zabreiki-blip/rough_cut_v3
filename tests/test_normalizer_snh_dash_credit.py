"""``СНХ Имя, роль — файл.wav 00:05:42 — текст`` credits (no comma after the marker)."""

from pathlib import Path

from stage1.normalizer import generate_normalization, validate_for_confirmation


def _normalize(tmp_path: Path, *paragraphs: str) -> dict:
    source = tmp_path / "scenario.txt"
    source.write_text("\n".join(paragraphs) + "\n", encoding="utf-8")
    return generate_normalization(
        source,
        tmp_path / "normalized.json",
        memory_path=tmp_path / "memory.sqlite3",
        use_llm=False,
    )


def test_name_role_and_media_file_in_one_paragraph(tmp_path: Path) -> None:
    result = _normalize(
        tmp_path,
        "СНХ Виктор ЛИТОВКИН, военный обозреватель, полковник в отставке — "
        "ZOOM0007_Tr1 (1).WAV 00:05:42 — Это сплошное лицемерие, "
        "то, что о чём говорит Дональд Трамп.",
    )
    assert len(result["blocks"]) == 1
    block = result["blocks"][0]
    assert block["type"] == "sync"
    assert block["speaker"] == "Виктор ЛИТОВКИН"
    assert block["role"] == "военный обозреватель, полковник в отставке"
    assert block["source_description"] == "ZOOM0007_Tr1 (1).WAV"
    assert "ZOOM" not in block["speaker"] + block["normalized_text"]
    assert [f["timecode_start"] for f in block["sync_fragments"]] == ["00:05:42"]
    assert block["sync_fragments"][0]["text"].startswith("Это сплошное лицемерие")
    assert block["timecode_start"] == "00:05:42"
    assert validate_for_confirmation(result) == []


def test_surname_only_credit(tmp_path: Path) -> None:
    result = _normalize(
        tmp_path, "СНХ ПОПОВ — ZOOM0004_Tr1.WAV 00:09:05 — Но угроза применению ядерного оружия."
    )
    block = result["blocks"][0]
    assert block["type"] == "sync"
    assert block["speaker"] == "ПОПОВ"
    assert block["role"] == ""
    assert block["source_description"] == "ZOOM0004_Tr1.WAV"
    assert block["sync_fragments"][0]["text"] == "Но угроза применению ядерного оружия."
    assert validate_for_confirmation(result) == []


def test_surname_and_short_role_with_cyrillic_media_name(tmp_path: Path) -> None:
    result = _normalize(
        tmp_path, "СНХ ЖАРЕНКОВ, инженер — Жаренков 1 часть.mp3 00:06:47 — Это ведёт к другому."
    )
    block = result["blocks"][0]
    assert block["speaker"] == "ЖАРЕНКОВ"
    assert block["role"] == "инженер"
    assert block["source_description"] == "Жаренков 1 часть.mp3"
    assert "mp3" not in block["normalized_text"]
    assert block["sync_fragments"][0]["timecode_start"] == "00:06:47"
    assert validate_for_confirmation(result) == []


def test_several_timecodes_become_fragments(tmp_path: Path) -> None:
    result = _normalize(
        tmp_path,
        "СНХ Дмитрий ВИННИК, аналитик — ZOOM0002_Tr1.WAV 00:03:48 — Вы знаете. "
        "00:04:39 — Ну, зато.",
    )
    block = result["blocks"][0]
    assert block["speaker"] == "Дмитрий ВИННИК"
    assert block["role"] == "аналитик"
    assert [f["timecode_start"] for f in block["sync_fragments"]] == ["00:03:48", "00:04:39"]
    assert [f["text"] for f in block["sync_fragments"]] == ["Вы знаете.", "Ну, зато."]
    assert validate_for_confirmation(result) == []


def test_header_paragraph_merges_with_timecoded_quote(tmp_path: Path) -> None:
    result = _normalize(
        tmp_path,
        "СНХ Виктор ЛИТОВКИН, военный обозреватель, полковник в отставке — ZOOM0005_Tr1 (1).WAV",
        "00:00:25 — Контроль должен был сработать на каждом этапе.",
    )
    assert len(result["blocks"]) == 1
    block = result["blocks"][0]
    assert block["type"] == "sync"
    assert block["speaker"] == "Виктор ЛИТОВКИН"
    assert block["role"] == "военный обозреватель, полковник в отставке"
    assert block["source_description"] == "ZOOM0005_Tr1 (1).WAV"
    assert block["sync_fragments"][0]["timecode_start"] == "00:00:25"
    assert block["sync_fragments"][0]["text"] == "Контроль должен был сработать на каждом этапе."
    assert validate_for_confirmation(result) == []


def test_next_quote_after_merged_header_inherits_speaker(tmp_path: Path) -> None:
    result = _normalize(
        tmp_path,
        "СНХ ПОПОВ — ZOOM0004_Tr1.WAV",
        "00:09:05 — Первая реплика.",
        "00:10:10 — Вторая реплика.",
    )
    assert [b["speaker"] for b in result["blocks"]] == ["ПОПОВ", "ПОПОВ"]
    assert validate_for_confirmation(result) == []


def test_comma_form_is_unchanged(tmp_path: Path) -> None:
    result = _normalize(tmp_path, "СНХ, Иван Петров: 00:01:02 — Обычный синхрон.")
    block = result["blocks"][0]
    assert block["type"] == "sync"
    assert block["speaker"] == "Иван Петров"
    assert validate_for_confirmation(result) == []


_CREDIT = (
    "СНХ Александр ШИРОКОРАД, историк — ZOOM0001_Tr1 (1).WAV 00:08:18 — "
    "Тринадцатого февраля была встреча."
)


def test_speaker_inherited_through_splice_markers_chain(tmp_path: Path) -> None:
    result = _normalize(
        tmp_path,
        _CREDIT,
        "СКЛЕЙКА",
        "00:09:09 — 11 апреля 1950 года была другая встреча.",
        "склейка:",
        "00:10:03 — Тринадцатого июня всё закончилось.",
    )
    blocks = result["blocks"]
    assert [b["type"] for b in blocks] == ["sync", "technical", "sync", "technical", "sync"]
    assert [b["original_text"] for b in blocks[1:4:2]] == ["СКЛЕЙКА", "склейка:"]
    for block in blocks[::2]:
        assert block["speaker"] == "Александр ШИРОКОРАД"
        assert block["role"] == "историк"
    assert blocks[1]["speaker"] == blocks[3]["speaker"] == ""
    assert not [e for e in validate_for_confirmation(result) if "has no speaker" in e]


def test_consecutive_splice_markers_and_trailing_punctuation(tmp_path: Path) -> None:
    result = _normalize(
        tmp_path,
        _CREDIT,
        "СКЛЕЙКА.",
        "  Склейка :  ",
        "00:09:09 — 11 апреля 1950 года была другая встреча.",
    )
    last = result["blocks"][-1]
    assert last["type"] == "sync"
    assert last["speaker"] == "Александр ШИРОКОРАД"
    assert len([b for b in result["blocks"] if b["speaker"]]) == 2
    assert not [e for e in validate_for_confirmation(result) if "has no speaker" in e]


def test_other_technical_block_between_blocks_inheritance_forbidden(tmp_path: Path) -> None:
    for blocker in ("ЗА КАДРОМ: текст диктора.", "ГРАФИКА"):
        result = _normalize(
            tmp_path,
            _CREDIT,
            "СКЛЕЙКА",
            blocker,
            "00:09:09 — 11 апреля 1950 года была другая встреча.",
        )
        last = result["blocks"][-1]
        assert last["speaker"] == "", blocker
        assert any("has no speaker" in e for e in validate_for_confirmation(result)), blocker


def test_splice_inheritance_requires_unambiguous_timecode(tmp_path: Path) -> None:
    result = _normalize(
        tmp_path,
        _CREDIT,
        "СКЛЕЙКА",
        "Тринадцатого февраля 1950 года была другая встреча.",
    )
    assert result["blocks"][-1]["speaker"] == ""
