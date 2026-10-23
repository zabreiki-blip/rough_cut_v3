from pathlib import Path
import json

from stage1.normalizer import (
    generate_normalization,
    reset_normalization,
    save_normalization,
    validate_for_confirmation,
)


def _generate(tmp_path: Path, source: Path, output: Path) -> dict:
    return generate_normalization(
        source, output, memory_path=tmp_path / "memory.sqlite3", use_llm=False
    )


def test_manual_resolution_survives_regeneration_and_keeps_other_warning(tmp_path: Path) -> None:
    source = tmp_path / "scenario.txt"
    output = tmp_path / "normalized.json"
    source.write_text("ЗК СНХ НИКИТА ВИХЛЯЕВ (00:30.00) Рассказ героя.\n", encoding="utf-8")

    first = _generate(tmp_path, source, output)
    block = first["blocks"][0]
    conflict = next(w for w in block["warnings"] if "Ambiguous label combination" in w)
    timecode = next(w for w in block["warnings"] if "00:30.00" in w)
    block["type"] = "sync"
    block["normalized_text"] = "Рассказ героя."
    block["speaker"] = "НИКИТА ВИХЛЯЕВ"
    block["timecode_start"] = "00:30:00"
    block["sync_fragments"] = [{"text": "Рассказ героя.", "timecode_start": "00:30:00"}]
    block["warnings"] = []
    saved = save_normalization(output, first, expected_revision=1)

    for _ in range(2):
        regenerated = _generate(tmp_path, source, output)
        after = regenerated["blocks"][0]
        assert after["uid"] == block["uid"]
        assert after["type"] == "sync"
        assert after["source"] == "manual"
        assert after["confidence"] == 1.0
        assert after["speaker"] == "НИКИТА ВИХЛЯЕВ"
        assert after["normalized_text"] == "Рассказ героя."
        assert after["timecode_start"] == "00:30:00"
        assert after["sync_fragments"] == block["sync_fragments"]
        assert not after["warnings"]
        assert conflict in after["reviewed_warnings"]
        assert timecode in after["reviewed_warnings"]
    assert validate_for_confirmation(regenerated) == []
    assert saved["blocks"][0]["source"] == "manual"


def test_metadata_edit_is_tracked_and_reset_discards_review_override(tmp_path: Path) -> None:
    source = tmp_path / "scenario.txt"
    output = tmp_path / "normalized.json"
    source.write_text("Авторский блок\n", encoding="utf-8")

    def initial_classifier(batch: list[dict], _hint: str) -> list[dict]:
        return [
            {
                "uid": batch[0]["uid"],
                "type": "sync",
                "confidence": 0.9,
                "speaker": "ИВАН",
                "timecode_start": "00:01:00",
            }
        ]

    def improved_classifier(batch: list[dict], _hint: str) -> list[dict]:
        return [
            {
                "uid": batch[0]["uid"],
                "type": "voiceover",
                "confidence": 0.9,
                "speaker": "НОВЫЙ ДИКТОР",
            }
        ]

    first = generate_normalization(
        source,
        output,
        memory_path=tmp_path / "memory.sqlite3",
        classifier=initial_classifier,
    )
    first["blocks"][0]["source_description"] = "У ХРАМА"
    first["blocks"][0]["manual_fields"] = ["type"]  # client data is ignored
    saved = save_normalization(output, first, expected_revision=1)
    assert saved["blocks"][0]["manually_edited"] is True
    assert saved["blocks"][0]["manual_fields"] == ["source_description"]
    regenerated = generate_normalization(
        source,
        output,
        memory_path=tmp_path / "memory.sqlite3",
        classifier=improved_classifier,
    )
    assert regenerated["blocks"][0]["source_description"] == "У ХРАМА"
    assert regenerated["blocks"][0]["type"] == "voiceover"
    assert regenerated["blocks"][0]["speaker"] == "НОВЫЙ ДИКТОР"
    assert regenerated["blocks"][0]["proposed_type"] == "voiceover"

    reset = reset_normalization(output)
    assert reset["blocks"] == []
    regenerated_after_reset = _generate(tmp_path, source, output)
    assert regenerated_after_reset["blocks"][0]["source_description"] != "У ХРАМА"


def test_warning_only_acknowledgement_is_tracked_without_metadata_edit(tmp_path: Path) -> None:
    source = tmp_path / "scenario.txt"
    output = tmp_path / "normalized.json"
    source.write_text("ЗК СНХ ИВАН ПЕТРОВ (00:30.00) Ответ.\n", encoding="utf-8")
    first = _generate(tmp_path, source, output)
    block = first["blocks"][0]
    conflict = next(w for w in block["warnings"] if "Ambiguous label combination" in w)
    timecode = next(w for w in block["warnings"] if "00:30.00" in w)
    block["type"] = "sync"
    block["speaker"] = "ИВАН ПЕТРОВ"
    block["warnings"].remove(conflict)
    saved = save_normalization(output, first, expected_revision=1)
    assert saved["blocks"][0]["manually_edited"] is True
    assert saved["blocks"][0]["source"] == "manual"
    assert saved["blocks"][0]["speaker"] == "ИВАН ПЕТРОВ"
    regenerated = _generate(tmp_path, source, output)
    assert conflict not in regenerated["blocks"][0]["warnings"]
    assert timecode in regenerated["blocks"][0]["warnings"]
    assert {"type", "original_label", "label_confirmed"} <= set(
        regenerated["blocks"][0]["manual_fields"]
    )


def test_accepted_timecode_pins_interpretation_even_without_value_change(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "scenario.txt"
    output = tmp_path / "normalized.json"
    source.write_text("ЛАЙФ САД (00:30.00) Всё готово.\n", encoding="utf-8")
    draft = _generate(tmp_path, source, output)
    block = draft["blocks"][0]
    warning = next(w for w in block["warnings"] if w.startswith("Ambiguous timecode"))
    accepted = block["timecode_start"]
    assert accepted == "00:30:00"
    block["warnings"].remove(warning)
    saved = save_normalization(output, draft, expected_revision=1)
    assert set(saved["blocks"][0]["manual_fields"]) >= {
        "timecode_start",
        "timecode_end",
        "sync_fragments",
    }

    from stage1 import normalizer

    original_extract = normalizer._extract_block_metadata

    def changed_parser_interpretation(item) -> None:
        original_extract(item)
        if item.original_text.startswith("ЛАЙФ САД"):
            item.timecode_start = "00:00:30"
            for fragment in item.sync_fragments:
                fragment.timecode_start = "00:00:30"

    monkeypatch.setattr(normalizer, "_extract_block_metadata", changed_parser_interpretation)
    regenerated = _generate(tmp_path, source, output)
    assert regenerated["blocks"][0]["timecode_start"] == accepted
    assert regenerated["blocks"][0]["sync_fragments"] == []
    assert warning in regenerated["blocks"][0]["reviewed_warnings"]


def test_invalid_manual_unknown_decision_still_blocks_confirmation(tmp_path: Path) -> None:
    source = tmp_path / "scenario.txt"
    output = tmp_path / "normalized.json"
    source.write_text("ЗК СНХ ИВАН ПЕТРОВ Ответ.\n", encoding="utf-8")
    draft = _generate(tmp_path, source, output)
    draft["blocks"][0]["notes"] = "Reviewer left the type unresolved"
    save_normalization(output, draft, expected_revision=1)
    regenerated = _generate(tmp_path, source, output)
    assert regenerated["blocks"][0]["type"] == "unknown"
    assert validate_for_confirmation(regenerated)


def test_legacy_manual_block_keeps_classification_after_metadata_save(tmp_path: Path) -> None:
    source = tmp_path / "scenario.txt"
    output = tmp_path / "normalized.json"
    source.write_text("ЗК СНХ ИВАН ПЕТРОВ Ответ.\n", encoding="utf-8")
    draft = _generate(tmp_path, source, output)
    draft["blocks"][0]["type"] = "sync"
    draft["blocks"][0]["speaker"] = "ИВАН ПЕТРОВ"
    saved = save_normalization(output, draft, expected_revision=1)

    legacy = json.loads(output.read_text(encoding="utf-8"))
    legacy_block = legacy["blocks"][0]
    legacy_block.pop("manual_fields", None)
    legacy_block.pop("manually_edited", None)
    legacy_block.pop("reviewed_warnings", None)
    output.write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")
    legacy_block["notes"] = "Checked by reviewer"
    after_save = save_normalization(output, legacy, expected_revision=saved["revision"])

    regenerated = _generate(tmp_path, source, output)
    assert after_save["blocks"][0]["source"] == "manual"
    assert regenerated["blocks"][0]["type"] == "sync"
    assert regenerated["blocks"][0]["speaker"] == "ИВАН ПЕТРОВ"
    assert regenerated["blocks"][0]["notes"] == "Checked by reviewer"


def test_acknowledged_scene_opener_warning_stays_closed(tmp_path: Path) -> None:
    source = tmp_path / "scenario.txt"
    output = tmp_path / "normalized.json"
    source.write_text("ЛАЙФ САД (00:30:00) Начинаем.\n- Продолжаем.\n", encoding="utf-8")
    draft = _generate(tmp_path, source, output)
    opener, reply = draft["blocks"]
    opener["type"] = "technical"
    first_saved = save_normalization(output, draft, expected_revision=1)
    reply = next(block for block in first_saved["blocks"] if block["uid"] == reply["uid"])
    warning = "scene opener changed: check reply type"
    assert warning in reply["warnings"]
    reply["warnings"].remove(warning)
    save_normalization(output, first_saved, expected_revision=first_saved["revision"])

    regenerated = _generate(tmp_path, source, output)
    regenerated_reply = next(
        block for block in regenerated["blocks"] if block["uid"] == reply["uid"]
    )
    assert regenerated_reply["type"] == "life"
    assert warning in regenerated_reply["reviewed_warnings"]
    assert warning not in regenerated_reply["warnings"]


def test_improved_classifier_drops_old_unknown_warning_and_refreshes_proposal(
    tmp_path: Path,
) -> None:
    source = tmp_path / "scenario.txt"
    output = tmp_path / "normalized.json"
    source.write_text("Авторский блок\n", encoding="utf-8")

    def incomplete_classifier(_batch: list[dict], _hint: str) -> list[dict]:
        return []

    def improved_classifier(batch: list[dict], _hint: str) -> list[dict]:
        return [{"uid": batch[0]["uid"], "type": "technical", "confidence": 0.8}]

    first = generate_normalization(
        source, output, memory_path=tmp_path / "memory.sqlite3", classifier=incomplete_classifier
    )
    assert any("did not return" in warning for warning in first["blocks"][0]["warnings"])
    improved = generate_normalization(
        source, output, memory_path=tmp_path / "memory.sqlite3", classifier=improved_classifier
    )
    assert not any("did not return" in warning for warning in improved["blocks"][0]["warnings"])
    assert improved["blocks"][0]["proposed_type"] == "technical"
    assert improved["blocks"][0]["type"] == "technical"


def test_duplicate_paragraphs_keep_occurrence_uids_and_edits(tmp_path: Path) -> None:
    source = tmp_path / "scenario.txt"
    output = tmp_path / "normalized.json"
    source.write_text("Одинаковая строка.\n\nОдинаковая строка.\n", encoding="utf-8")
    first = _generate(tmp_path, source, output)
    uids = [block["uid"] for block in first["blocks"]]
    first["blocks"][1]["normalized_text"] = "Вторая строка после проверки."
    save_normalization(output, first, expected_revision=1)

    regenerated = _generate(tmp_path, source, output)
    assert [block["uid"] for block in regenerated["blocks"]] == uids
    assert regenerated["blocks"][0]["normalized_text"] != "Вторая строка после проверки."
    assert regenerated["blocks"][1]["normalized_text"] == "Вторая строка после проверки."


def test_changed_source_does_not_inherit_manual_override(tmp_path: Path) -> None:
    source = tmp_path / "scenario.txt"
    output = tmp_path / "normalized.json"
    source.write_text("СТЕНДАП МАРИЯ У РЕКИ Добрый день.\n", encoding="utf-8")
    first = _generate(tmp_path, source, output)
    first["blocks"][0]["type"] = "technical"
    save_normalization(output, first, expected_revision=1)

    source.write_text("СТЕНДАП МАРИЯ У РЕКИ Добрый день.\nНовый абзац.\n", encoding="utf-8")
    regenerated = _generate(tmp_path, source, output)
    assert regenerated["blocks"][0]["type"] != "technical"


def _counting_classifier(counter: list[int], block_type: str = "other"):
    def classifier(batch: list[dict], _hint: str) -> list[dict]:
        counter.append(len(batch))
        return [{"uid": item["uid"], "type": block_type, "confidence": 0.9} for item in batch]

    return classifier


def test_same_input_reuses_llm_cache(tmp_path: Path) -> None:
    source = tmp_path / "scenario.txt"
    output = tmp_path / "normalized.json"
    source.write_text("Авторский блок\n", encoding="utf-8")
    calls: list[int] = []
    classifier = _counting_classifier(calls)
    memory = tmp_path / "memory.sqlite3"

    generate_normalization(source, output, memory_path=memory, classifier=classifier)
    again = generate_normalization(source, output, memory_path=memory, classifier=classifier)

    assert len(calls) == 1
    assert again["blocks"][0]["type"] == "other"


def test_new_structure_parser_version_does_not_reuse_stale_cache(
    tmp_path: Path, monkeypatch
) -> None:
    import stage1.normalizer as normalizer_module

    source = tmp_path / "scenario.txt"
    output = tmp_path / "normalized.json"
    source.write_text("Авторский блок\n", encoding="utf-8")
    calls: list[int] = []
    classifier = _counting_classifier(calls)
    memory = tmp_path / "memory.sqlite3"

    generate_normalization(source, output, memory_path=memory, classifier=classifier)
    monkeypatch.setattr(
        normalizer_module,
        "STRUCTURE_PARSER_VERSION",
        normalizer_module.STRUCTURE_PARSER_VERSION + 1,
    )
    generate_normalization(source, output, memory_path=memory, classifier=classifier)
    assert len(calls) == 2

    # The same (new) version is cached again.
    generate_normalization(source, output, memory_path=memory, classifier=classifier)
    assert len(calls) == 2


def test_new_scene_context_version_does_not_reuse_stale_cache(tmp_path: Path, monkeypatch) -> None:
    import stage1.normalizer as normalizer_module

    source = tmp_path / "scenario.txt"
    output = tmp_path / "normalized.json"
    source.write_text("Авторский блок\n", encoding="utf-8")
    calls: list[int] = []
    classifier = _counting_classifier(calls)
    memory = tmp_path / "memory.sqlite3"

    generate_normalization(source, output, memory_path=memory, classifier=classifier)
    monkeypatch.setattr(
        normalizer_module,
        "SCENE_CONTEXT_VERSION",
        normalizer_module.SCENE_CONTEXT_VERSION + 1,
    )
    generate_normalization(source, output, memory_path=memory, classifier=classifier)
    assert len(calls) == 2


def test_manual_edit_survives_reparse_after_parser_version_bump(
    tmp_path: Path, monkeypatch
) -> None:
    import stage1.normalizer as normalizer_module

    source = tmp_path / "scenario.txt"
    output = tmp_path / "normalized.json"
    source.write_text("Авторский блок\n", encoding="utf-8")
    calls: list[int] = []
    classifier = _counting_classifier(calls, "other")
    memory = tmp_path / "memory.sqlite3"

    first = generate_normalization(source, output, memory_path=memory, classifier=classifier)
    first["blocks"][0]["type"] = "technical"
    save_normalization(output, first, expected_revision=1)

    monkeypatch.setattr(
        normalizer_module,
        "STRUCTURE_PARSER_VERSION",
        normalizer_module.STRUCTURE_PARSER_VERSION + 1,
    )
    again = generate_normalization(source, output, memory_path=memory, classifier=classifier)

    assert len(calls) == 2  # re-asked the classifier, not the stale cache
    block = again["blocks"][0]
    assert block["type"] == "technical"
    assert block["source"] == "manual"
    assert "type" in block["manual_fields"]
    assert block["uid"] == first["blocks"][0]["uid"]
