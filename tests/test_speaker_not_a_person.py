"""A discourse word before a comma is never a speaker label; narration after ЗК stays narration."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from stage1.confirmation_checks import is_non_person_word, role_is_prose
from stage1.normalizer import generate_normalization, validate_for_confirmation_detailed

SYNC_HEAD = "СНХ Дмитрий ВИННИК, аналитик — ZOOM0002_Tr1.WAV 00:06:08 Это не просто так."
PROSE = {
    "Кажется": (
        "Кажется, что контроль над ядерным оружием жёсткий в любой стране. "
        "12 июня 1968 года Генассамблея ООН в Нью-Йорке одобрила документ."
    ),
    "Проверим": (
        "Проверим, можно ли найти слабый источник радиации под водой, когда его "
        "разносит течением. Возьмём калийное удобрение — хлористый калий."
    ),
    "Кстати": (
        "Кстати, в нашей стране подобное попросту не могло случиться. "
        "Ещё в 1947 году, когда первая бомба только создавалась."
    ),
}


def _run(tmp_path: Path, paragraphs: list[str]) -> list[dict[str, Any]]:
    source = tmp_path / "scenario.txt"
    source.write_text("\n\n".join(paragraphs) + "\n", encoding="utf-8")
    return generate_normalization(
        source, tmp_path / "out.json", memory_path=tmp_path / "memory.sqlite3", use_llm=False
    )["blocks"]


@pytest.mark.parametrize("word", list(PROSE))
@pytest.mark.parametrize("zk", ["ЗК1.7", "ЗК 2.10", "ЗК."])
def test_discourse_word_after_zk_is_voiceover_not_sync(tmp_path: Path, word: str, zk: str) -> None:
    blocks = _run(tmp_path, [SYNC_HEAD, zk, PROSE[word]])
    assert blocks[0]["type"] == "sync"
    block = blocks[2]
    assert block["type"] == "voiceover"
    assert block["speaker"] == ""
    assert not block["original_label"]
    assert block["text_for_tts"].startswith(word)


@pytest.mark.parametrize("word", list(PROSE))
def test_discourse_word_without_zk_is_not_a_speaker(tmp_path: Path, word: str) -> None:
    block = _run(tmp_path, [SYNC_HEAD, PROSE[word]])[1]
    assert block["speaker"] != word
    assert block["type"] != "sync" or block["speaker"] == "Дмитрий ВИННИК"


def test_full_name_with_role_stays_sync(tmp_path: Path) -> None:
    text = (
        "СНХ Виктор ЛИТОВКИН, военный обозреватель, полковник в отставке — "
        "ZOOM0002_Tr1.WAV 00:08:10 Это важная вещь, о ней нужно говорить."
    )
    block = _run(tmp_path, ["ЗК1.7", text])[1]
    assert block["type"] == "sync" and block["speaker"] == "Виктор ЛИТОВКИН"


@pytest.mark.parametrize("zk", [None, "ЗК2.10"])
def test_surname_with_role_and_colon_stays_sync(tmp_path: Path, zk: str | None) -> None:
    text = "Смольникова, искусствовед: это было очень необычное время для всей страны."
    paragraphs = [text] if zk is None else [zk, text]
    block = _run(tmp_path, paragraphs)[-1]
    assert block["type"] == "sync" and block["speaker"] == "Смольникова"


def test_surname_with_role_and_period_stays_sync_without_zk(tmp_path: Path) -> None:
    text = "Смольникова, искусствовед. Это было очень необычное время для всей страны."
    block = _run(tmp_path, [text])[0]
    assert block["type"] == "sync" and block["speaker"] == "Смольникова"


def test_known_speaker_with_comma_sentence_stays_sync(tmp_path: Path) -> None:
    known = "СНХ Иван Иванов, историк — a.wav 00:01:00 Мы жили там долго."
    text = "Иванов, он рассказал, что мы пришли домой. Было очень поздно, и мы все устали."
    blocks = _run(tmp_path, [known, text])
    assert blocks[1]["type"] == "sync" and blocks[1]["speaker"] == "Иванов"


def test_unknown_surname_with_comma_sentence_is_not_a_speaker(tmp_path: Path) -> None:
    text = "Иванов, он рассказал, что мы пришли домой. Было очень поздно, и мы все устали."
    block = _run(tmp_path, [text])[0]
    assert block["speaker"] != "Иванов"
    assert block["type"] != "sync"


def test_period_credit_after_zk_is_voiceover(tmp_path: Path) -> None:
    text = "Смольникова, искусствовед. Это было очень необычное время для всей страны."
    block = _run(tmp_path, ["ЗК3.1", text])[1]
    assert block["type"] == "voiceover" and block["speaker"] == ""


@pytest.mark.parametrize("word", ["Кажется", "Кстати", "Итак", "Представьте", "Обратимся"])
def test_non_person_words(word: str) -> None:
    assert is_non_person_word(word)


@pytest.mark.parametrize("word", ["Смольникова", "Иванов", "Отец", "Максим", "Вадим", "ЛИТОВКИН"])
def test_person_words(word: str) -> None:
    assert not is_non_person_word(word)


@pytest.mark.parametrize(
    "role",
    [
        "что контроль над ядерным оружием жёсткий",
        "можно ли найти слабый источник радиации",
        "в нашей стране подобное попросту не могло случиться",
    ],
)
def test_prose_roles(role: str) -> None:
    assert role_is_prose(role)


@pytest.mark.parametrize(
    "role", ["искусствовед", "военный обозреватель", "военный обозреватель, полковник в отставке"]
)
def test_credit_roles(role: str) -> None:
    assert not role_is_prose(role)


def _scenario(blocks: list[dict[str, Any]]) -> dict[str, Any]:
    return {"source": "s", "source_hash": "h", "blocks": blocks, "status": "draft"}


def _sync(order: int, speaker: str, text: str, **kwargs: Any) -> dict[str, Any]:
    return {
        "uid": f"b{order}",
        "order": order,
        "original_text": text,
        "normalized_text": text,
        "type": "sync",
        "speaker": speaker,
        "source": "builtin_rule",
        **kwargs,
    }


def _issues(blocks: list[dict[str, Any]], severity: str = "error") -> list[tuple[int, str]]:
    return [
        (i["order"], i["code"])
        for i in validate_for_confirmation_detailed(_scenario(blocks))
        if i["severity"] == severity
    ]


def test_sync_with_discourse_speaker_is_error() -> None:
    block = _sync(1, "Кстати", "Кстати, в нашей стране подобное не могло случиться.")
    assert (1, "speaker_not_a_person") in _issues([block])


def test_sync_with_prose_after_single_word_speaker_is_error() -> None:
    text = "Иванов, он рассказал, что мы пришли домой очень поздно."
    assert (1, "speaker_not_a_person") in _issues([_sync(1, "Иванов", text)])


def test_known_speaker_elsewhere_clears_the_check() -> None:
    text = "Иванов, он рассказал, что мы пришли домой очень поздно."
    blocks = [_sync(1, "Иванов", text), _sync(2, "Иван Иванов", "СНХ Иван Иванов 00:01:00 Да.")]
    assert (1, "speaker_not_a_person") not in _issues(blocks)


def test_manual_speaker_is_not_flagged() -> None:
    block = _sync(1, "Кстати", "Кстати, мы там были.", manual_fields=["speaker"])
    assert (1, "speaker_not_a_person") not in _issues([block])


@pytest.mark.parametrize("speaker", ["Отец", "Смольникова", "Дмитрий ВИННИК"])
def test_ordinary_speakers_are_not_flagged(speaker: str) -> None:
    block = _sync(1, speaker, f"{speaker}, искусствовед: речь была длинной и интересной.")
    assert (1, "speaker_not_a_person") not in _issues([block])


def test_saved_json_roundtrip(tmp_path: Path) -> None:
    blocks = _run(tmp_path, [SYNC_HEAD, "ЗК1.7", PROSE["Кажется"]])
    issues = validate_for_confirmation_detailed(json.loads((tmp_path / "out.json").read_text()))
    assert not [i for i in issues if i["code"] == "speaker_not_a_person"]
    assert blocks[2]["type"] == "voiceover"
