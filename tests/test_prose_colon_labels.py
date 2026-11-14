"""A colon in narration must not turn its opening phrase into a speaker label."""

from pathlib import Path

import pytest

from stage1.normalizer import generate_normalization


PROSE = [
    "План был прост: мы начинаем рассказ с самого начала и ничего не пропускаем.",
    "Цель была двойной: изучить реку и объяснить результаты нашим зрителям.",
    "Наша гипотеза: река меняет своё течение и постепенно выходит из берегов.",
    "Наше предположение: мост построили намного раньше, чем считали исследователи.",
    "НАША гипотеза: река меняет своё течение и постепенно выходит из берегов.",
    "ПЛАН был прост: мы начинаем рассказ с самого начала и ничего не пропускаем.",
]


@pytest.mark.parametrize("text", PROSE)
def test_prose_caption_after_voiceover_marker_preserves_whole_sentence(
    tmp_path: Path, text: str
) -> None:
    source = tmp_path / "scenario.txt"
    source.write_text(f"ЗК-8\n{text}\n", encoding="utf-8")
    blocks = generate_normalization(
        source,
        tmp_path / "out.json",
        memory_path=tmp_path / "memory.sqlite3",
        use_llm=False,
    )["blocks"]
    block = blocks[1]
    assert block["type"] == "voiceover"
    assert block["original_label"] == ""
    assert block["normalized_text"] == text
    assert block["text_for_tts"] == text


@pytest.mark.parametrize("text", PROSE)
def test_llm_marker_flag_cannot_drop_prose_opening_phrase(tmp_path: Path, text: str) -> None:
    source = tmp_path / "scenario.txt"
    source.write_text(text + "\n", encoding="utf-8")

    def classifier(batch, _hint):
        return [
            dict(uid=item["uid"], type="voiceover", confidence=0.99, label_is_marker=True)
            for item in batch
        ]

    block = generate_normalization(
        source,
        tmp_path / "out.json",
        memory_path=tmp_path / "memory.sqlite3",
        classifier=classifier,
    )["blocks"][0]
    assert block["original_label"] == ""
    assert block["normalized_text"] == text
    assert block["text_for_tts"] == text
