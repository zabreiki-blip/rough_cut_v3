from pathlib import Path

from stage1.normalizer import generate_normalization, validate_for_confirmation


def _normalize(tmp_path: Path, paragraphs: list[str]) -> dict:
    source = tmp_path / "scenario.txt"
    source.write_text("\n\n".join(paragraphs), encoding="utf-8")
    return generate_normalization(
        source, tmp_path / "out.json", memory_path=tmp_path / "memory.sqlite3", use_llm=False
    )


def test_named_presenter_caption_and_splice_are_not_spoken(tmp_path: Path) -> None:
    result = _normalize(
        tmp_path, ["СТЕНДАП МАРИЯ У РЕКИ Первый рассказ. + СТЕНДАП МАРИЯ У ХРАМА Второй рассказ."]
    )
    block = result["blocks"][0]
    assert block["speaker"] == "МАРИЯ"
    assert block["normalized_text"] == "Первый рассказ. Второй рассказ."
    assert [part["text"] for part in block["sync_fragments"]] == [
        "Первый рассказ.",
        "Второй рассказ.",
    ]
    assert "У РЕКИ" in block["source_description"]
    assert "У ХРАМА" in block["source_description"]
    assert not block["warnings"]
    assert validate_for_confirmation(result) == []


def test_named_leadin_retains_time_range_without_unknown_speaker_warning(tmp_path: Path) -> None:
    result = _normalize(
        tmp_path, ["ПОДВОДКА МАРИЯ У СОБОРА [29:41.93 - 30:42.20] Мы привыкли видеть его таким."]
    )
    block = result["blocks"][0]
    assert block["speaker"] == "МАРИЯ"
    assert block["source_description"] == "У СОБОРА"
    assert block["normalized_text"] == "Мы привыкли видеть его таким."
    assert (block["timecode_start"], block["timecode_end"]) == ("29:41", "30:42")
    assert not block["warnings"]
    assert validate_for_confirmation(result) == []


def test_different_presenters_remain_for_review_without_discarding_text(tmp_path: Path) -> None:
    result = _normalize(tmp_path, ["СТЕНДАП МАРИЯ Первый рассказ. + СТЕНДАП ОЛЬГА Второй рассказ."])
    block = result["blocks"][0]
    assert block["speaker"] == ""
    assert "Второй рассказ." in block["normalized_text"]
    assert block["warnings"]
    assert validate_for_confirmation(result)


def test_timed_life_dialogue_keeps_only_speech_and_timecode(tmp_path: Path) -> None:
    result = _normalize(
        tmp_path,
        [
            "ЛАЙФ САД (00:34:00) Начинаем работу.",
            "- (00:34:21) Ну давайте обрежем розу.",
            "+ 00.35.18 Вот здесь?",
            "- Да.",
        ],
    )
    blocks = result["blocks"]
    assert [block["type"] for block in blocks] == ["life"] * 4
    assert blocks[1]["normalized_text"] == "Ну давайте обрежем розу."
    assert blocks[2]["normalized_text"] == "Вот здесь?"
    assert blocks[1]["timecode_start"] == "00:34:21"
    assert blocks[2]["timecode_start"] == "00:35:18"
    assert all(block["scene_uid"] == blocks[0]["uid"] for block in blocks[1:])
    assert not any("no previous" in w for block in blocks for w in block["warnings"])
