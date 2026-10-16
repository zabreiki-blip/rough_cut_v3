import json
from pathlib import Path

from stage2.timeline_parser import parse_normalized_timeline
from stage4.revision_assets import prepared_from_normalized

from stage1.normalizer import (
    _extract_sync_fragments,
    clean_voiceover_tts_text,
    confirm_normalization,
    generate_normalization,
    save_normalization,
    validate_for_confirmation,
)


def _normalize(tmp_path: Path, text: str) -> dict:
    source = tmp_path / "scenario.txt"
    source.write_text(text + "\n", encoding="utf-8")
    return generate_normalization(
        source,
        tmp_path / "normalized.json",
        memory_path=tmp_path / "memory.sqlite3",
        use_llm=False,
    )["blocks"][0]


def test_decade_is_not_a_timecode_or_fragment_boundary() -> None:
    fragments = _extract_sync_fragments(
        "30:57 Луэлла в 50-60ые годы помогала Лиле. 00:31:57 Они были родные."
    )
    assert len(fragments) == 2
    assert "50-60ые годы" in fragments[0].text
    assert [item.timecode_start for item in fragments] == ["30:57", "00:31:57"]


def test_splice_marker_removes_parentheses_without_empty_fragments() -> None:
    fragments = _extract_sync_fragments("00:11:19 Первая часть. (склейка) 00:12:25 Вторая часть.")
    assert [item.text for item in fragments] == ["Первая часть.", "Вторая часть."]


def test_speaker_credit_wins_over_prose_before_timecode(tmp_path: Path) -> None:
    block = _normalize(
        tmp_path,
        "ВЕРА СТОРОЖЕВА: То, что получилось у нас, это живая история. "
        "00:03:04 Мы делали кино, которое мы старались делать, что это живые люди.",
    )
    assert block["type"] == "sync"
    assert block["speaker"] == "ВЕРА СТОРОЖЕВА"
    assert block["role"] == ""
    assert len(block["sync_fragments"]) == 2


def test_named_interview_credit_is_sync_after_voiceover(tmp_path: Path) -> None:
    source = tmp_path / "scenario.txt"
    source.write_text(
        "ЗК: Вступительный текст диктора.\n"
        "Дана Курская, поэт, издатель: Она была энергетическим вампиром. "
        "А рядом — поэт. Ему нужна любовь, ей — энергия.\n",
        encoding="utf-8",
    )
    blocks = generate_normalization(
        source,
        tmp_path / "normalized.json",
        memory_path=tmp_path / "memory.sqlite3",
        use_llm=False,
    )["blocks"]
    assert [item["type"] for item in blocks] == ["voiceover", "sync"]
    assert blocks[1]["speaker"] == "Дана Курская"


def test_editorial_cues_and_empty_credits_are_not_spoken(tmp_path: Path) -> None:
    for text in (
        "НА КАДРАХ ПРОЦЕССА СЪЕМОК СЕРИАЛА – БЕКСТЕЙДЖ – НАЙТИ – ПОЛУЧИТЬ!",
        "ЗТМ.",
        "ЗК. (фотография Ланг)",
        "ЗК. «Голос Маяковского»:",
        "ФРАГМЕНТ СЕРИАЛА — С ЛИЛЕЙ, КРАСНОЩЕКОВЫМ И ЛУЭЛЛОЙ",
        "ЗДЕСЬ БЫ НУЖЕН ФРАГМЕНТ ИЗ СЕРИАЛА – БРИК ЗА РУЛЁМ.",
        "ЗДЕСЬ НАДО БЫ ДАТЬ ФРАГМЕНТ ИЗ СЕРИАЛА. ВСТРЕЧА НА ИППОДРОМЕ.",
        "Протоирей Андрей Дударев, куратор дачи Маяковского в Пушкино:",
    ):
        block = _normalize(tmp_path, text)
        assert block["type"] == "technical"
        assert not block["text_for_tts"]


def test_confirmation_rejects_corrupt_sync_metadata(tmp_path: Path) -> None:
    source = tmp_path / "scenario.txt"
    source.write_text("ВЕРА СТОРОЖЕВА: 00:03:04 Мы делали кино.\n", encoding="utf-8")
    scenario = generate_normalization(
        source,
        tmp_path / "normalized.json",
        memory_path=tmp_path / "memory.sqlite3",
        use_llm=False,
    )
    block = scenario["blocks"][0]
    block["speaker"] = "То"
    block["sync_fragments"].append({"text": ")", "timecode_start": "50:60"})
    errors = validate_for_confirmation(scenario)
    assert any("speaker conflicts" in error for error in errors)
    assert any("empty fragment" in error for error in errors)
    assert any("invalid timecode" in error for error in errors)


def test_confirmation_rejects_sync_without_speaker(tmp_path: Path) -> None:
    source = tmp_path / "scenario.txt"
    source.write_text("СНХ: Фраза без указания героя.\n", encoding="utf-8")
    scenario = generate_normalization(
        source,
        tmp_path / "normalized.json",
        memory_path=tmp_path / "memory.sqlite3",
        use_llm=False,
    )
    assert "Sync block 1 has no speaker" in validate_for_confirmation(scenario)


def test_confirmation_rejects_reading_direction_as_speaker(tmp_path: Path) -> None:
    source = tmp_path / "scenario.txt"
    source.write_text("СНХ, Черкасов читает: Первая строка.\n", encoding="utf-8")
    scenario = generate_normalization(
        source,
        tmp_path / "normalized.json",
        memory_path=tmp_path / "memory.sqlite3",
        use_llm=False,
    )
    assert any(
        "direction instead of a speaker" in error for error in validate_for_confirmation(scenario)
    )


def test_sync_does_not_inherit_speaker_across_narration(tmp_path: Path) -> None:
    source = tmp_path / "scenario.txt"
    source.write_text(
        "Курская: 00:03 Первый ответ.\nЗК: Рассказ ведущего.\nСНХ: Следующий ответ.\n",
        encoding="utf-8",
    )
    blocks = generate_normalization(
        source,
        tmp_path / "normalized.json",
        memory_path=tmp_path / "memory.sqlite3",
        use_llm=False,
    )["blocks"]
    assert [block["type"] for block in blocks] == ["sync", "voiceover", "sync"]
    assert blocks[-1]["speaker"] == ""


def test_absorbed_voiceover_is_kept_in_scenario_but_not_spoken_twice(tmp_path: Path) -> None:
    source = tmp_path / "scenario.txt"
    source.write_text("ЗК: Первая строка.\nЗК: Вторая строка.\n", encoding="utf-8")
    normalized = tmp_path / "normalized.json"
    draft = generate_normalization(
        source, normalized, memory_path=tmp_path / "memory.sqlite3", use_llm=False
    )
    first, second = draft["blocks"]
    first["text_for_tts"] = "Первая строка. Вторая строка."
    second["absorbed_by_uid"] = first["uid"]
    second["text_for_tts"] = ""
    assert validate_for_confirmation(draft) == []
    saved = save_normalization(normalized, draft, expected_revision=draft["revision"])
    confirmed = confirm_normalization(
        normalized,
        tmp_path / "prepared.json",
        tmp_path / "raw.txt",
        memory_path=tmp_path / "memory.sqlite3",
        expected_revision=saved["revision"],
    )
    prepared = json.loads((tmp_path / "prepared.json").read_text())
    raw = (tmp_path / "raw.txt").read_text()
    timeline = parse_normalized_timeline(confirmed, tmp_path, tmp_path, "TEST")
    assert len(confirmed["blocks"]) == 2
    assert [item["block_uid"] for item in prepared["items"]] == [first["uid"]]
    assert [item["block_uid"] for item in prepared_from_normalized(confirmed)["items"]] == [
        first["uid"]
    ]
    assert raw.count("ЗК") == 1
    assert [(segment.id, segment.block_uid) for segment in timeline.segments] == [
        ("zk1", first["uid"])
    ]


def test_absorbed_voiceover_requires_valid_target_and_empty_tts(tmp_path: Path) -> None:
    source = tmp_path / "scenario.txt"
    source.write_text("ЗК: Первая строка.\nЗК: Вторая строка.\n", encoding="utf-8")
    draft = generate_normalization(
        source, tmp_path / "normalized.json", memory_path=tmp_path / "memory.sqlite3", use_llm=False
    )
    draft["blocks"][1]["absorbed_by_uid"] = "missing"
    errors = validate_for_confirmation(draft)
    assert any("invalid absorbed-by link" in error for error in errors)
    assert any("absorbed but still has TTS text" in error for error in errors)


def test_voice_credit_and_inline_directorial_cue_are_not_spoken() -> None:
    assert (
        clean_voiceover_tts_text("«Голос Маяковского»- Перевел сто долларов.")
        == "Перевел сто долларов."
    )
    assert (
        clean_voiceover_tts_text("Голос за кадром читает воспоминания Лили: «Я была там»")
        == "«Я была там»"
    )
    assert (
        clean_voiceover_tts_text(
            "Его книги выходили с посвящением. (показываем издания Маяковского)"
        )
        == "Его книги выходили с посвящением."
    )
    assert clean_voiceover_tts_text("Голос Лили. Дорогой товарищ Сталин<…>") == (
        "Дорогой товарищ Сталин…"
    )


def test_announce_paragraph_without_timecode_or_label_is_not_sync(tmp_path: Path) -> None:
    block = _normalize(
        tmp_path,
        "Девятисерийный фильм «Лиля» сняла режиссёр Вера Сторожева.",
    )
    assert block["type"] != "sync"


def test_label_only_sync_marker_without_colon_sets_speaker(tmp_path: Path) -> None:
    block = _normalize(tmp_path, "Синх Сторожевой")
    assert block["type"] == "sync"
    assert block["speaker"] == "Сторожевой"
    assert block["text_for_tts"] == ""


def test_three_paragraph_sync_pattern_inherits_speaker_from_label_marker(tmp_path: Path) -> None:
    source = tmp_path / "scenario.txt"
    source.write_text(
        "Девятисерийный фильм «Лиля» сняла режиссёр Вера Сторожева.\n"
        "Синх Сторожевой\n"
        "То, что получилось у нас, это живая история... "
        "00:03:04 Мы делали кино, которое мы старались делать, что это живые люди.\n",
        encoding="utf-8",
    )
    blocks = generate_normalization(
        source,
        tmp_path / "normalized.json",
        memory_path=tmp_path / "memory.sqlite3",
        use_llm=False,
    )["blocks"]

    assert blocks[0]["type"] != "sync"
    assert blocks[1]["type"] == "sync"
    assert blocks[1]["speaker"] == "Сторожевой"
    assert blocks[2]["type"] == "sync"
    assert blocks[2]["speaker"] == "Сторожевой"
    assert blocks[2]["original_label"] == ""
    assert "Мы делали кино" in blocks[2]["normalized_text"]
    assert blocks[2]["sync_fragments"]
    assert blocks[2]["sync_fragments"][-1]["timecode_start"] == "00:03:04"
    assert "Мы делали кино" in blocks[2]["sync_fragments"][-1]["text"]


def test_voiceover_with_mid_sentence_timecode_after_sync_does_not_inherit_speaker(
    tmp_path: Path,
) -> None:
    # Regression: a bare, unlabeled voiceover/cue paragraph that merely
    # contains a timecode somewhere in its text must not be swept into the
    # preceding sync block just because that sync has a known speaker. Only
    # a label-only "Синх <Фамилия>" carrier (handled separately) should
    # trigger that inheritance.
    source = tmp_path / "scenario.txt"
    source.write_text(
        "ИВАНОВ: 00:00:05 Мы завершили стройку в срок.\n"
        "Архивные кадры пожара на фабрике. Оператор снял эвакуацию жителей "
        "соседних домов 00:15:22.\n",
        encoding="utf-8",
    )
    blocks = generate_normalization(
        source,
        tmp_path / "normalized.json",
        memory_path=tmp_path / "memory.sqlite3",
        use_llm=False,
    )["blocks"]

    assert blocks[0]["type"] == "sync"
    assert blocks[0]["speaker"]
    assert blocks[1]["type"] != "sync"
    assert not blocks[1]["speaker"]


def test_llm_sync_suggestion_without_signal_is_downgraded(tmp_path: Path) -> None:
    source = tmp_path / "scenario.txt"
    source.write_text(
        "Девятисерийный фильм «Лиля» сняла режиссёр Вера Сторожева.\n",
        encoding="utf-8",
    )

    def classifier(items, _hint):
        return [{"uid": items[0]["uid"], "type": "sync", "confidence": 0.95}]

    result = generate_normalization(
        source,
        tmp_path / "normalized.json",
        memory_path=tmp_path / "memory.sqlite3",
        classifier=classifier,
    )
    assert result["blocks"][0]["type"] != "sync"


def test_confirm_relocated_source_with_matching_hash(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    source.write_text("ЗК: Текст диктора.\n", encoding="utf-8")
    normalized = tmp_path / "normalized.json"
    scenario = generate_normalization(
        source, normalized, memory_path=tmp_path / "memory.sqlite3", use_llm=False
    )
    moved = tmp_path / "moved.txt"
    source.rename(moved)
    confirmed = confirm_normalization(
        normalized,
        tmp_path / "prepared.json",
        tmp_path / "raw.txt",
        memory_path=tmp_path / "memory.sqlite3",
        source_path_override=moved,
    )
    assert confirmed["status"] == "confirmed"
    assert confirmed["source"] == scenario["source"]
