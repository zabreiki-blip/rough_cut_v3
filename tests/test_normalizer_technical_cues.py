from __future__ import annotations

from pathlib import Path
import tempfile

import pytest

from stage1.normalizer import generate_normalization


def _classify_block(text: str, *, first: bool = False) -> dict:
    """Run the normalizer (no LLM) and return the block's dict.

    Mirrors the helper pattern used by ``test_normalizer_title.py`` and
    ``test_normalizer_video_cue.py``: write a scenario to a temp file and
    classify it with ``use_llm=False`` so only builtin rules and memory
    can produce a decision.

    Args:
        text: The line to classify.
        first: If True, the line is the first block (no leading paragraph).
               If False, prepend a leading voiceover paragraph before the test line.

    Returns:
        The block dict containing the test line (or the first block if first=True).
    """
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        source = root / "scenario.txt"

        if first:
            scenario = f"{text}\n"
        else:
            leading_paragraph = "З.к. В том году она впервые вышла на большую сцену и запела."
            scenario = f"{leading_paragraph}\n\n{text}\n"

        source.write_text(scenario, encoding="utf-8")
        blocks = generate_normalization(
            source,
            root / "out.json",
            memory_path=root / "memory.sqlite3",
            use_llm=False,
        )["blocks"]

        if first:
            return blocks[0]
        else:
            # Find the block containing the test text
            for block in blocks:
                if text in block.get("original_text", ""):
                    return block
            assert False, f"Block containing '{text}' not found"


# A. Lines containing a URL/link.
LINK_TEXTS = (
    "https://vkvideo.ru/video-104917606_456239404",
    "https://www.youtube.com/watch?v=hZ58-4jCVZ0 Чао бамбино",
    "https://youtu.be/vF3vgENr5-w?t=19272 плачет",
    "• Цветы https://www.youtube.com/results?search_query=Цветы",
)

# B. Bulleted list items.
BULLET_TEXTS = (
    "• Лечу в темноту",
    "• Ла-ла-ла",
    "• Мама Мария",
    "• Портофолио",
    "• Ты рядом (feat. Джиган)",
)

# C. Titles with a year in parentheses (including a bullet/dash prefix).
YEAR_TITLE_TEXTS = (
    "• Ночной дозор (2004)",
    "• О чём говорят мужчины (2010)",
    "- Белым снегом (Новогодний Огонёк 2001)",
)

# D. Section headings and production notes.
SECTION_HEADING_TEXTS = (
    "Сольные клипы",
    "Клипы группы «Блестящие»",
    "Художественные фильмы",
    "Последний герой",
    "ФОТЫ ЖАННЫ",
    "Хроника Жанны -НТВ и",
    "Фрагмент «Ледникового периода».",
    "Вставить синхрон Жанны",
    "Сценарий. Жанна Фриске. Неба коснуться рукой…",
    "-Жанна Фриске",
)

# E. Anti-regression: these must NOT become technical via the builtin rule.
NON_TECHNICAL_TEXTS = (
    "- Я тогда совершенно не знала, что мне делать дальше, и просто позвонила маме.",
    "З.к. В 1974 году она впервые вышла на сцену и запела.",
    "Она приехала в Москву и поступила в институт, где встретила будущих подруг.",
    # First-person singular -у/-ю verbs ("хочу", "знаю") are not covered by
    # _PERSONAL_VERB_RE (adding them would misfire on accusative nouns like
    # "на сцену"), so the dash-line/section-heading detectors must instead
    # rely on punctuation shape (comma, sentence-ending punctuation) to keep
    # live speech like this out of technical.
    "- Не знаю, что сказать.",
    "- Я хочу домой.",
    "Хочу показать всем самое важное в моей жизни.",
    "- Мы сидим и вспоминаем.",
)


@pytest.mark.parametrize("text", LINK_TEXTS, ids=[f"link-{i}" for i in range(len(LINK_TEXTS))])
def test_link_lines_are_technical_by_builtin_rule(text: str) -> None:
    block = _classify_block(text)
    assert block["type"] == "technical"
    assert block["source"] == "builtin_rule"


@pytest.mark.parametrize(
    "text", BULLET_TEXTS, ids=[f"bullet-{i}" for i in range(len(BULLET_TEXTS))]
)
def test_bullet_list_items_are_technical_by_builtin_rule(text: str) -> None:
    block = _classify_block(text)
    assert block["type"] == "technical"
    assert block["source"] == "builtin_rule"


@pytest.mark.parametrize(
    "text", YEAR_TITLE_TEXTS, ids=[f"year-title-{i}" for i in range(len(YEAR_TITLE_TEXTS))]
)
def test_titles_with_year_in_parentheses_are_technical_by_builtin_rule(text: str) -> None:
    block = _classify_block(text)
    assert block["type"] == "technical"
    assert block["source"] == "builtin_rule"


@pytest.mark.parametrize(
    "text",
    SECTION_HEADING_TEXTS,
    ids=[f"section-heading-{i}" for i in range(len(SECTION_HEADING_TEXTS))],
)
def test_section_headings_and_production_notes_are_technical_by_builtin_rule(text: str) -> None:
    # "Сценарий..." is actually the first block in a real scenario; others are in middle
    first = text == "Сценарий. Жанна Фриске. Неба коснуться рукой…"
    block = _classify_block(text, first=first)
    assert block["type"] == "technical"
    assert block["source"] == "builtin_rule"


@pytest.mark.parametrize(
    "text",
    NON_TECHNICAL_TEXTS,
    ids=[f"non-technical-{i}" for i in range(len(NON_TECHNICAL_TEXTS))],
)
def test_speech_and_voiceover_lines_stay_non_technical(text: str) -> None:
    block = _classify_block(text)
    assert block["type"] != "technical"
