"""Strategy-level and boundary tests for ``stage1.analyzer``.

DOCX parsing is replaced by ``DocxParagraph`` fixtures and the LLM client by a
stub, so nothing here touches the network or real office files.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from stage1 import analyzer
from stage1.analyzer import (
    _attach_source_spans,
    _cache_llm_blocks,
    _canonical_raw_projection,
    _chunk_for_llm,
    _cleanup_blocks,
    _deserialize_llm_blocks,
    _fallback_blocks,
    _from_docx_formatting,
    _from_marked_docx,
    _is_sync_or_interview,
    _is_technical,
    _load_llm_cache,
    _merge_blocks_to_limit,
    _merge_short_blocks,
    _split_long_text,
    _strip_narrator_markers,
    _strip_speaker_cues,
    _strip_technical_cues,
    analyze_script,
)
from stage1.io_utils import DocxParagraph
from stage1.models import NarrationBlock


def _block(text: str, **kwargs) -> NarrationBlock:
    return NarrationBlock(id="", text_original=text, text_for_tts=text, **kwargs)


def _paras(*items: tuple[str, bool, bool]) -> list[DocxParagraph]:
    return [
        DocxParagraph(text=t, is_bold=b, has_highlight=h, index=i)
        for i, (t, b, h) in enumerate(items)
    ]


# --- classifiers ----------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "[06:34] Реплика героя",
        "06:34 Реплика героя",
        "(…) обрывок интервью",
        "(...) обрывок интервью",
        "(В кадре… пауза) ремарка",
    ],
)
def test_sync_or_interview_detects_timecodes_and_ellipsis(text: str) -> None:
    assert _is_sync_or_interview(text) is True


def test_plain_narration_is_not_sync() -> None:
    assert _is_sync_or_interview("Обычный дикторский текст про Арктику.") is False


def test_technical_detection_covers_prefix_and_short_all_caps() -> None:
    assert _is_technical("ВИДЕО 3 общий план") is True
    assert _is_technical("НАЗВАНИЕ ПРОГРАММЫ") is True
    # A narrator marker in caps is not "technical" even though it is short.
    assert _is_technical("ЗК5") is False
    assert _is_technical("Обычное предложение для диктора.") is False
    long_caps = "ОЧЕНЬ ДЛИННАЯ СТРОКА ЗАГЛАВНЫМИ БУКВАМИ БОЛЬШЕ ПЯТИДЕСЯТИ СИМВОЛОВ ПОДРЯД"
    assert _is_technical(long_caps) is False


# --- splitting / chunking / fallback ---------------------------------------------


def test_split_long_text_boundaries() -> None:
    assert list(_split_long_text("   ")) == []
    assert list(_split_long_text("Коротко.")) == ["Коротко."]
    exact = "а" * 300
    assert list(_split_long_text(exact)) == [exact]

    text = " ".join(["Первое предложение здесь." for _ in range(30)])
    chunks = list(_split_long_text(text, max_len=100))
    assert len(chunks) > 1
    assert all(len(c) <= 100 for c in chunks)
    assert " ".join(chunks) == text


def test_split_long_text_keeps_oversized_sentence_whole() -> None:
    sentence = "б" * 500
    assert list(_split_long_text(sentence + ". Ещё одно.", max_len=100)) == [
        sentence + ".",
        "Ещё одно.",
    ]


def test_chunk_for_llm_groups_paragraphs_and_respects_limit() -> None:
    assert _chunk_for_llm("") == []
    assert _chunk_for_llm(" \n\n \n") == []
    assert _chunk_for_llm("Один\n\nДва") == ["Один\n\nДва"]

    paragraphs = [f"Абзац {i} " + "я" * 90 for i in range(10)]
    chunks = _chunk_for_llm("\n\n".join(paragraphs), max_chars=300)
    assert len(chunks) > 1
    assert all(len(c) <= 300 for c in chunks)
    assert "\n\n".join(chunks) == "\n\n".join(paragraphs)


def test_chunk_for_llm_splits_single_oversized_paragraph() -> None:
    paragraph = " ".join(["Предложение номер один." for _ in range(100)])
    chunks = _chunk_for_llm(paragraph, max_chars=500)

    assert len(chunks) > 1
    assert all(len(c) <= 500 for c in chunks)


def test_chunk_for_llm_falls_back_to_lines_when_no_blank_separators() -> None:
    # No blank line: whole text is one paragraph, but must still be returned.
    assert _chunk_for_llm("Строка один\nСтрока два") == ["Строка один\nСтрока два"]


def test_fallback_uses_explicit_zk_markers_when_present() -> None:
    blocks = _fallback_blocks("zk7: Первый текст\nlorem\nZK9 - Второй текст")

    assert [b.id for b in blocks] == ["zk1", "zk2"]
    assert [b.text_for_tts for b in blocks] == ["Первый текст", "Второй текст"]


def test_fallback_drops_speaker_technical_and_sync_paragraphs() -> None:
    text = "\n\n".join(
        [
            "Это настоящий закадровый текст диктора.",
            "СНХ Иван Иванов. Файл 12",
            "ШАПКА",
            "[06:34] Реплика героя в интервью",
            "Второй абзац нужного текста.",
        ]
    )

    blocks = _fallback_blocks(text)

    assert [b.text_for_tts for b in blocks] == [
        "Это настоящий закадровый текст диктора.",
        "Второй абзац нужного текста.",
    ]
    assert [b.id for b in blocks] == ["zk1", "zk2"]


def test_fallback_without_blank_lines_and_empty_input() -> None:
    blocks = _fallback_blocks("Строка один тут.\nСтрока два тут.")
    assert [b.text_for_tts for b in blocks] == ["Строка один тут.\nСтрока два тут."]
    assert _fallback_blocks("") == []
    assert _fallback_blocks("   \n \n") == []


# --- DOCX strategies -----------------------------------------------------------------


def test_docx_formatting_ignores_non_docx_and_empty(tmp_path: Path) -> None:
    assert _from_docx_formatting(tmp_path / "a.txt") == ([], "")
    with patch.object(analyzer, "extract_paragraphs_from_docx", return_value=[]):
        assert _from_docx_formatting(tmp_path / "a.docx") == ([], "")


def test_docx_formatting_needs_enough_bold_contrast(tmp_path: Path) -> None:
    only_plain = _paras(*[(f"Текст абзаца номер {i} тут", False, False) for i in range(5)])
    two_bold = _paras(
        ("Заголовок 1", True, False),
        ("Заголовок 2", True, False),
        *[(f"Текст абзаца номер {i} тут", False, False) for i in range(5)],
    )
    for paragraphs in (only_plain, two_bold):
        with patch.object(analyzer, "extract_paragraphs_from_docx", return_value=paragraphs):
            assert _from_docx_formatting(tmp_path / "a.docx") == ([], "")


def test_docx_formatting_keeps_only_plain_narration(tmp_path: Path) -> None:
    paragraphs = _paras(
        ("ВЕДУЩИЙ-1. Подводка", True, False),
        ("СНХ Иван Иванов", True, False),
        ("Титры", True, False),
        ("Нормальный закадровый текст номер один.", False, False),
        ("[06:34] Реплика из интервью без смысла", False, False),
        ("ШАПКА НАЗВАНИЕ", False, False),
        ("СНХ. Не жирный спикер, но метка", False, False),
        ("(…) обрывок интервью", False, False),
        ("Коротко", False, False),
        ("   ", False, False),
        ("Нормальный закадровый текст номер два.", False, False),
    )
    with patch.object(analyzer, "extract_paragraphs_from_docx", return_value=paragraphs):
        blocks, method = _from_docx_formatting(tmp_path / "a.docx")

    assert method == "docx_formatting"
    assert [b.text_for_tts for b in blocks] == [
        "Нормальный закадровый текст номер один.",
        "Нормальный закадровый текст номер два.",
    ]
    assert {b.notes for b in blocks} == {"docx_formatting"}


def test_marked_docx_ignores_non_docx(tmp_path: Path) -> None:
    assert _from_marked_docx(tmp_path / "a.txt") == []


def test_marked_docx_prefers_zk_markers_inside_highlighted_paragraphs(tmp_path: Path) -> None:
    paragraphs = _paras(
        ("ЗК1: Подсвеченный закадровый текст.", False, True),
        ("Неподсвеченный текст.", False, False),
    )
    with (
        patch.object(analyzer, "extract_paragraphs_from_docx", return_value=paragraphs),
        patch.object(analyzer, "extract_marked_blocks_from_docx") as marked,
    ):
        blocks = _from_marked_docx(tmp_path / "a.docx")

    marked.assert_not_called()
    assert [b.text_for_tts for b in blocks] == ["Подсвеченный закадровый текст."]
    assert blocks[0].notes == "manual_highlight_marker"


def test_marked_docx_falls_back_to_raw_marked_runs(tmp_path: Path) -> None:
    paragraphs = _paras(("Просто подсвеченный текст без маркера.", False, True))
    with (
        patch.object(analyzer, "extract_paragraphs_from_docx", return_value=paragraphs),
        patch.object(
            analyzer, "extract_marked_blocks_from_docx", return_value=[" Первый ", "", "Второй"]
        ),
    ):
        blocks = _from_marked_docx(tmp_path / "a.docx")

    assert [b.text_for_tts for b in blocks] == ["Первый", "Второй"]
    assert {b.notes for b in blocks} == {"manual_highlight"}


def test_marked_docx_without_highlights_still_asks_marked_runs(tmp_path: Path) -> None:
    paragraphs = _paras(("Текст.", False, False))
    with (
        patch.object(analyzer, "extract_paragraphs_from_docx", return_value=paragraphs),
        patch.object(analyzer, "extract_marked_blocks_from_docx", return_value=[]),
    ):
        assert _from_marked_docx(tmp_path / "a.docx") == []


def _run(tmp_path: Path, text: str, *, suffix: str = ".docx", use_llm: bool = False) -> dict:
    source = tmp_path / f"scenario{suffix}"
    source.write_text("placeholder", encoding="utf-8")
    with patch.object(analyzer, "extract_text", return_value=text):
        return analyze_script(source, tmp_path / "out.json", tmp_path / "raw.txt", use_llm=use_llm)


def test_analyze_script_prefers_highlight_strategy_over_formatting(tmp_path: Path) -> None:
    paragraphs = _paras(("Выделенный закадровый текст тут.", False, True))
    with (
        patch.object(analyzer, "extract_paragraphs_from_docx", return_value=paragraphs),
        patch.object(
            analyzer,
            "extract_marked_blocks_from_docx",
            return_value=["Выделенный закадровый текст тут."],
        ),
    ):
        result = _run(tmp_path, "Выделенный закадровый текст тут.")

    assert result["extraction_method"] == "highlight"
    assert result["manual_markers_used"] is True


def test_analyze_script_uses_docx_formatting_when_no_markers(tmp_path: Path) -> None:
    paragraphs = _paras(
        ("Заголовок один", True, False),
        ("Заголовок два", True, False),
        ("Заголовок три", True, False),
        ("Первый закадровый абзац достаточной длины.", False, False),
        ("Второй закадровый абзац достаточной длины.", False, False),
        ("Третий закадровый абзац достаточной длины.", False, False),
    )
    text = "\n\n".join(p.text for p in paragraphs)
    with (
        patch.object(analyzer, "extract_paragraphs_from_docx", return_value=paragraphs),
        patch.object(analyzer, "extract_marked_blocks_from_docx", return_value=[]),
    ):
        result = _run(tmp_path, text)

    assert result["extraction_method"] == "docx_formatting"
    assert result["manual_markers_used"] is False
    payload = json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))
    assert payload["items"][0]["id"] == "zk1"


def test_analyze_script_missing_input_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="Input file not found"):
        analyze_script(tmp_path / "none.docx", tmp_path / "o.json", tmp_path / "r.txt")


def test_analyze_script_without_api_key_uses_fallback_even_if_llm_requested(tmp_path: Path) -> None:
    with (
        patch.dict(os.environ, {"OPENROUTER_API_KEY": ""}),
        patch.object(analyzer, "analyze_with_openrouter") as llm,
    ):
        result = _run(
            tmp_path, "Это достаточно длинный абзац для диктора.", suffix=".txt", use_llm=True
        )

    llm.assert_not_called()
    assert result["extraction_method"] == "fallback"
    assert result["used_llm"] is False


def test_llm_failure_for_one_chunk_falls_back_and_is_reported(tmp_path: Path) -> None:
    paragraphs = [f"Абзац {i}. " + "слово " * 150 for i in range(1, 15)]
    text = "\n\n".join(paragraphs)
    calls: list[str] = []

    def fake(settings, chunk, *args):
        calls.append(chunk)
        if len(calls) == 1:
            raise RuntimeError("HTTP 500")
        return [NarrationBlock(id="x", text_original=chunk, text_for_tts=chunk)]

    with (
        patch.dict(os.environ, {"OPENROUTER_API_KEY": "k", "ANALYZER_LLM_WORKERS": "1"}),
        patch.object(analyzer, "analyze_with_openrouter", side_effect=fake),
    ):
        result = _run(tmp_path, text, suffix=".txt", use_llm=True)

    assert len(calls) > 1
    assert result["llm_errors_count"] == 1
    assert result["used_llm"] is True
    assert result["extraction_method"] == "llm"


def test_all_llm_chunks_failing_marks_method_as_fallback(tmp_path: Path) -> None:
    with (
        patch.dict(os.environ, {"OPENROUTER_API_KEY": "k"}),
        patch.object(analyzer, "analyze_with_openrouter", side_effect=RuntimeError("down")),
    ):
        result = _run(
            tmp_path, "Достаточно длинный абзац для диктора здесь.", suffix=".txt", use_llm=True
        )

    assert result["used_llm"] is False
    assert result["extraction_method"] == "fallback"
    assert result["llm_errors_count"] == 1
    assert result["blocks_count"] >= 1


def test_invalid_worker_env_and_usage_callback_are_supported(tmp_path: Path) -> None:
    seen: list[tuple] = []

    def fake(settings, chunk, usage_callback=None):
        seen.append((chunk, usage_callback))
        return [NarrationBlock(id="x", text_original=chunk, text_for_tts=chunk)]

    callback = lambda _event: None  # noqa: E731
    source = tmp_path / "s.txt"
    source.write_text("Достаточно длинный абзац для диктора здесь.", encoding="utf-8")
    with (
        patch.dict(os.environ, {"OPENROUTER_API_KEY": "k", "ANALYZER_LLM_WORKERS": "many"}),
        patch.object(analyzer, "analyze_with_openrouter", side_effect=fake),
    ):
        result = analyze_script(
            source, tmp_path / "o.json", tmp_path / "r.txt", usage_callback=callback
        )

    assert result["used_llm"] is True
    assert seen[0][1] is callback


# --- text cleanup -----------------------------------------------------------------


def test_strip_helpers_return_empty_input_unchanged() -> None:
    for fn in (_strip_speaker_cues, _strip_narrator_markers, _strip_technical_cues):
        assert fn("") == ""


def test_strip_speaker_cues_removes_labels_and_inline_prefixes() -> None:
    text = "ВЕДУЩИЙ-1. Подводка\nЭкспертов слушаем. СПИКЕР 2: слова диктора\n\nПросто текст"

    cleaned = _strip_speaker_cues(text)

    assert "Подводка" not in cleaned
    assert "СПИКЕР" not in cleaned
    assert "слова диктора" in cleaned and "Просто текст" in cleaned


def test_strip_technical_cues_removes_prefix_lines_and_inline_links() -> None:
    text = "ШАПКА\nТекст перед. ВОТ ССЫЛКА: http://x.test Текст после\n[06:34] реплика"

    cleaned = _strip_technical_cues(text)

    assert "ШАПКА" not in cleaned and "реплика" not in cleaned
    assert "ССЫЛКА" not in cleaned
    assert "Текст перед." in cleaned


def test_cleanup_drops_blocks_that_become_too_short_and_keeps_notes_and_spans() -> None:
    kept = NarrationBlock(
        id="a",
        text_original="ЗК1: Достаточно длинный текст",
        text_for_tts="ЗК1: Достаточно длинный текст",
        pause_ms=700,
        notes="n",
        source_spans=[(1, 5)],
    )
    dropped = _block("ЗК2: ok")

    cleaned = _cleanup_blocks([kept, dropped])

    assert len(cleaned) == 1
    assert cleaned[0].text_for_tts == "Достаточно длинный текст"
    assert (cleaned[0].pause_ms, cleaned[0].notes, cleaned[0].source_spans) == (700, "n", [(1, 5)])


# --- merging -------------------------------------------------------------------------


def test_merge_short_blocks_pairs_short_with_next_and_keeps_tail() -> None:
    assert _merge_short_blocks([]) == []
    blocks = [_block("коротко", pause_ms=100), _block("и ещё", pause_ms=900), _block("x" * 60)]

    merged = _merge_short_blocks(blocks)

    assert [b.text_for_tts for b in merged] == ["коротко и ещё", "x" * 60]
    assert merged[0].pause_ms == 900

    assert [b.text_for_tts for b in _merge_short_blocks([_block("tail")])] == ["tail"]


def test_merge_to_limit_ignores_invalid_limits_and_merges_to_exact_count() -> None:
    blocks = [_block(f"Блок номер {i} " + "я" * 100) for i in range(6)]

    assert _merge_blocks_to_limit(blocks, 0) is blocks
    assert _merge_blocks_to_limit(blocks, 10) is blocks
    merged = _merge_blocks_to_limit(blocks, 2)
    assert len(merged) == 2
    assert merged[0].text_for_tts.startswith("Блок номер 0")
    assert merged[1].text_for_tts.endswith("я" * 100)
    joined = " ".join(b.text_for_tts for b in merged)
    assert joined == " ".join(b.text_for_tts for b in blocks)


def test_merge_to_limit_prefers_merging_continuation_pairs() -> None:
    blocks = [
        _block("Закончено полностью и без вопросов."),
        _block("Начало, которое продолжается дальше,"),
        _block("и тут продолжение мысли."),
    ]

    merged = _merge_blocks_to_limit(blocks, 2)

    assert merged[0].text_for_tts == "Закончено полностью и без вопросов."
    assert "продолжается дальше, и тут продолжение" in merged[1].text_for_tts


# --- source spans / raw projection --------------------------------------------------------


def test_attach_source_spans_preserves_duplicate_order_and_skips_unknown() -> None:
    text = "Повтор. Середина. Повтор."
    blocks = [_block("Повтор."), _block("Повтор."), _block("Нет такого"), _block("")]

    _attach_source_spans(blocks, text)

    assert blocks[0].source_spans == [(0, 7)]
    assert blocks[1].source_spans == [(18, 25)]
    assert blocks[2].source_spans == [] and blocks[3].source_spans == []


def test_attach_source_spans_falls_back_to_earlier_occurrence() -> None:
    text = "Нужная фраза. Хвост."
    first = _block("Хвост.")
    second = _block("Нужная фраза.")

    _attach_source_spans([first, second], text)

    assert second.source_spans == [(0, 13)]


def test_attach_source_spans_advances_cursor_past_prelocated_spans() -> None:
    text = "AAA BBB AAA"
    pre = _block("AAA", source_spans=[(0, 3)])
    later = _block("AAA")

    _attach_source_spans([pre, later], text)

    assert later.source_spans == [(8, 11)]


def test_raw_projection_replaces_spans_and_appends_unlocated_blocks() -> None:
    text = "Начало. Вставка первая. Конец."
    first = NarrationBlock(
        id="zk1", text_original="Один", text_for_tts="Один", source_spans=[(8, 23)]
    )
    lost = NarrationBlock(id="zk2", text_original="", text_for_tts="Потерян")
    overlapping = NarrationBlock(
        id="zk3", text_original="Три", text_for_tts="Три", source_spans=[(10, 15)]
    )

    raw, missing = _canonical_raw_projection(text, [first, lost, overlapping])

    assert raw.startswith("Начало. ЗК1: Один Конец.")
    assert raw.rstrip().endswith("ЗК2: Потерян\nЗК3: Три")
    assert missing == ["ЗК2: Потерян", "ЗК3: Три"]


def test_raw_projection_multi_span_block_collapses_extra_spans() -> None:
    text = "AA 11 BB 22 CC"
    block = NarrationBlock(
        id="zk1", text_original="X", text_for_tts="X", source_spans=[(9, 11), (0, 2)]
    )

    raw, missing = _canonical_raw_projection(text, [block])

    assert missing == []
    # The first span carries the rendered text; the second one is cut out.
    assert raw == "ЗК1: X 11 BB  CC"


def test_raw_projection_ignores_out_of_range_spans() -> None:
    block = NarrationBlock(
        id="zk1", text_original="T", text_for_tts="T", source_spans=[(5, 999), (-1, 2), (3, 3)]
    )

    raw, missing = _canonical_raw_projection("abc", [block])

    assert missing == ["ЗК1: T"]
    assert raw.startswith("abc")


# --- LLM cache primitives --------------------------------------------------------------------


def test_load_cache_rejects_wrong_version_shape_and_garbage(tmp_path: Path) -> None:
    empty = {"version": analyzer._LLM_CACHE_VERSION, "entries": {}}
    path = tmp_path / "cache.json"

    assert _load_llm_cache(path) == empty  # missing file
    path.write_text("{broken", encoding="utf-8")
    assert _load_llm_cache(path) == empty
    path.write_text(json.dumps({"version": 999, "entries": {"k": {}}}), encoding="utf-8")
    assert _load_llm_cache(path) == empty
    path.write_text(
        json.dumps({"version": analyzer._LLM_CACHE_VERSION, "entries": []}), encoding="utf-8"
    )
    assert _load_llm_cache(path) == empty
    path.write_text("[]", encoding="utf-8")
    assert _load_llm_cache(path) == empty


def test_deserialize_rejects_malformed_payloads() -> None:
    assert _deserialize_llm_blocks("x") is None
    assert _deserialize_llm_blocks([1]) is None
    assert _deserialize_llm_blocks([{"pause_ms": "fast"}]) is None
    blocks = _deserialize_llm_blocks([{"id": "zk1", "text_original": "a", "text_for_tts": "b"}])
    assert blocks is not None and blocks[0].pause_ms == 300


def test_cache_write_evicts_oldest_entries_over_the_limit(tmp_path: Path) -> None:
    cache = {
        "version": analyzer._LLM_CACHE_VERSION,
        "entries": {
            "old": {"created_at": "2000-01-01T00:00:00+00:00", "blocks": []},
            "newer": {"created_at": "2001-01-01T00:00:00+00:00", "blocks": []},
        },
    }
    with patch.object(analyzer, "_LLM_CACHE_MAX_ENTRIES", 2):
        ok = _cache_llm_blocks(tmp_path / "c.json", cache, "fresh", [_block("текст блока")])

    assert ok is True
    assert set(cache["entries"]) == {"newer", "fresh"}
    assert set(json.loads((tmp_path / "c.json").read_text(encoding="utf-8"))["entries"]) == {
        "newer",
        "fresh",
    }


def test_cache_write_failure_rolls_back_entry(tmp_path: Path) -> None:
    cache: dict = {"version": analyzer._LLM_CACHE_VERSION, "entries": {}}
    with patch.object(analyzer, "write_json", side_effect=OSError("disk full")):
        ok = _cache_llm_blocks(tmp_path / "c.json", cache, "k", [_block("текст блока")])

    assert ok is False
    assert cache["entries"] == {}


def test_cache_skips_unserialisable_blocks(tmp_path: Path) -> None:
    class Broken:
        def to_dict(self):
            raise TypeError("nope")

    cache: dict = {"entries": {}}

    assert _cache_llm_blocks(tmp_path / "c.json", cache, "k", [Broken()]) is False  # type: ignore[list-item]
    assert not (tmp_path / "c.json").exists()


def test_llm_cache_path_depends_on_work_directory(tmp_path: Path) -> None:
    assert (
        analyzer._llm_cache_path(tmp_path / "work" / "p.json")
        == tmp_path / "work" / "analysis_llm_cache.json"
    )
    assert (
        analyzer._llm_cache_path(tmp_path / "p.json")
        == tmp_path / "work" / "analysis_llm_cache.json"
    )
