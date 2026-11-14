from __future__ import annotations

from pathlib import Path

from stage1.normalizer import generate_normalization, validate_for_confirmation_detailed


def _normalize(tmp_path: Path, *paragraphs: str) -> list[dict]:
    source = tmp_path / "scenario.txt"
    source.write_text("\n\n".join(paragraphs) + "\n", encoding="utf-8")
    result = generate_normalization(
        source,
        tmp_path / "normalized.json",
        memory_path=tmp_path / "memory.sqlite3",
        use_llm=False,
    )
    return result["blocks"]


_HEADER = "Руслан Каленчук, политолог, публицист, православный катохизатор.Руслан. Файл 1:"


def test_unmarked_file_credit_inherits_speaker_to_timed_paragraph(tmp_path: Path) -> None:
    header, quote = _normalize(tmp_path, _HEADER, "00:01:12 Первая реплика.")

    assert header["type"] == "sync" and header["label_only"] is True
    assert header["speaker"] == "Руслан Каленчук"
    assert header["role"] == "политолог, публицист, православный катохизатор"
    assert header["source_description"] == "1"
    assert quote["type"] == "sync" and quote["speaker"] == "Руслан Каленчук"
    assert quote["timecode_start"] == "00:01:12"


def test_unmarked_file_credit_inherits_through_initial_untimed_prose(tmp_path: Path) -> None:
    header, prose_and_quote, quote = _normalize(
        tmp_path,
        _HEADER,
        "мы начали разговор здесь. 00:02:18 Первая реплика.",
        "00:02:54 Продолжение ответа.",
    )

    assert header["label_only"] is True
    assert prose_and_quote["type"] == "sync" and prose_and_quote["speaker"] == "Руслан Каленчук"
    assert prose_and_quote["timecode_start"] == "00:02:18"
    assert prose_and_quote["source_description"] == "1"
    assert "мы начали разговор здесь." in prose_and_quote["normalized_text"]
    assert "мы начали разговор здесь." in " ".join(
        fragment["text"] for fragment in prose_and_quote["sync_fragments"]
    )
    assert quote["type"] == "sync" and quote["speaker"] == "Руслан Каленчук"
    assert quote["timecode_start"] == "00:02:54"
    assert quote["source_description"] == "1"


def test_explicit_voiceover_breaks_unmarked_file_credit_inheritance(tmp_path: Path) -> None:
    header, voiceover, quote = _normalize(
        tmp_path,
        _HEADER,
        "ЗК: За кадром звучит рассказ диктора.",
        "00:03:10 Реплика без подтверждённого спикера.",
    )

    assert header["speaker"] == "Руслан Каленчук"
    assert voiceover["type"] == "voiceover"
    assert quote["type"] == "sync" and quote["speaker"] == ""


def test_narrative_colon_is_not_a_person_file_header(tmp_path: Path) -> None:
    (block,) = _normalize(tmp_path, "История нашего города. Файл 1:")

    assert block["type"] != "sync"
    assert block["speaker"] == ""


def test_unmarked_file_header_has_no_confirmation_blocker(tmp_path: Path) -> None:
    blocks = _normalize(tmp_path, _HEADER, "00:01:12 Первая реплика.")

    assert (
        validate_for_confirmation_detailed(
            {"source": "scenario.txt", "source_hash": "a" * 64, "blocks": blocks}
        )
        == []
    )
