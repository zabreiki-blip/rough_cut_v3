from pathlib import Path

from stage1.normalizer import (
    _extract_sync_fragments,
    generate_normalization,
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
