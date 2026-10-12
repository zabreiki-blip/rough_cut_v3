from __future__ import annotations

import pytest

from stage2.text_normalization import fold_russian_text


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", ""),
        ("   ", "   "),
        ("Привет, Мир!", "привет, мир!"),
        ("Ёлка", "елка"),
        ("ЁЖИК", "ежик"),
        ("всё", "все"),
        ("Дом 12 А", "дом 12 а"),
        ("a.b-c, d?", "a.b-c, d?"),
    ],
)
def test_fold_russian_text_cases(raw: str, expected: str) -> None:
    assert fold_russian_text(raw) == expected


def test_fold_russian_text_unifies_yo_and_ye_spellings() -> None:
    assert fold_russian_text("Всё ещё") == fold_russian_text("все еще")


def test_fold_russian_text_applies_nfkc_compatibility_forms() -> None:
    # Fullwidth digits/letters and the "fi" ligature collapse to plain forms.
    assert fold_russian_text("１２３") == "123"
    assert fold_russian_text("ＡＢＣ") == "abc"
    assert fold_russian_text("ﬁnal") == "final"
    # Non-breaking space becomes an ordinary space.
    assert fold_russian_text("а б") == "а б"


def test_fold_russian_text_composes_decomposed_yo() -> None:
    decomposed = "Ёлка"  # "Е" + combining diaeresis
    assert fold_russian_text(decomposed) == "елка"


def test_fold_russian_text_casefold_handles_german_sharp_s() -> None:
    assert fold_russian_text("Straße") == "strasse"


def test_fold_russian_text_does_not_stem_or_strip() -> None:
    assert fold_russian_text("  Бегущий  ") == "  бегущий  "


def test_fold_russian_text_is_idempotent() -> None:
    once = fold_russian_text("Ёлки-ПАЛКИ ３")
    assert fold_russian_text(once) == once
