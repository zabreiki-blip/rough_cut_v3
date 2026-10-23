"""Speaker-name comparison for source eligibility (inflection, labels, date prefixes)."""

from __future__ import annotations

import pytest

from stage2.sync_matcher import SyncMatcher, speaker_name_tokens
from stage2.sync_models import AlignmentRequest, SyncSource, WordIndex, WordTimestamp


def _matcher(*sources: SyncSource) -> SyncMatcher:
    indexes = [
        WordIndex(source_id=source.id, words=[WordTimestamp("слово", 0.0, 0.5)])
        for source in sources
    ]
    return SyncMatcher(sources, indexes)


def _source(source_id: str, speaker: str, **kwargs: object) -> SyncSource:
    return SyncSource(
        id=source_id, speaker=speaker, premiere_sequence=f"SYNC_{source_id.upper()}", **kwargs
    )


def _eligible(matcher: SyncMatcher, speaker: str) -> list[str]:
    sources, _filtered = matcher._eligible_sources(
        AlignmentRequest(segment_id="seg", text="слово", speaker=speaker)
    )
    return [source.id for source in sources]


def test_labelled_inflected_surname_matches_dated_source() -> None:
    matcher = _matcher(_source("max", "29-626 Максимов"), _source("other", "Иван Петров"))
    assert _eligible(matcher, "СИНХ МАКСИМОВА") == ["max"]


@pytest.mark.parametrize("label", ["СИНХ", "Синх", "СНХ", "sync", "Sync"])
def test_service_label_is_not_part_of_speaker_name(label: str) -> None:
    matcher = _matcher(_source("max", "Максимов"), _source("other", "Иван Петров"))
    assert _eligible(matcher, f"{label} Максимов") == ["max"]


@pytest.mark.parametrize(
    "speaker",
    ["Максимов", "Максимова", "Максимову", "Максимовым", "Максимове", "Максимовой"],
)
def test_surname_case_and_gender_forms_match(speaker: str) -> None:
    matcher = _matcher(_source("max", "Максимов"), _source("other", "Иван Петров"))
    assert _eligible(matcher, speaker) == ["max"]


@pytest.mark.parametrize("speaker", ["Сторожева", "Сторожевой", "Сторожеву", "Сторожевым"])
def test_surname_adjectival_forms_match(speaker: str) -> None:
    matcher = _matcher(_source("st", "Сторожевой"), _source("other", "Иван Петров"))
    assert _eligible(matcher, speaker) == ["st"]


def test_yo_and_ye_are_equivalent_in_surname() -> None:
    matcher = _matcher(_source("kr", "Краснощёкова"), _source("other", "Иван Петров"))
    assert _eligible(matcher, "Краснощекова") == ["kr"]
    other = _matcher(_source("kr", "Краснощекова"), _source("o", "Иван Петров"))
    assert _eligible(other, "Краснощёкову") == ["kr"]


@pytest.mark.parametrize(
    ("source_name", "speaker"),
    [
        ("220626_Краснова", "Краснова"),
        ("220626_Краснова", "СИНХ Краснову"),
        ("40726 Стычкин", "Стычкин"),
        ("40726 Стычкин", "Стычкина"),
        ("40726 Стычкин", "Алексей Стычкин"),
    ],
)
def test_date_prefix_in_source_name_is_ignored(source_name: str, speaker: str) -> None:
    matcher = _matcher(_source("src", source_name), _source("other", "Иван Петров"))
    assert _eligible(matcher, speaker) == ["src"]


def test_inflected_full_name_with_label_matches() -> None:
    matcher = _matcher(_source("max", "Иван Максимов"), _source("other", "Иван Петров"))
    assert _eligible(matcher, "СНХ Ивана Максимова") == ["max"]


@pytest.mark.parametrize(
    ("source_name", "speaker"),
    [
        ("Максимович", "Максимов"),
        ("Максимов", "Максимович"),
        ("Максим", "Максимов"),
        ("Максимов", "Максимова-Орлова"),
    ],
)
def test_different_surnames_do_not_merge(source_name: str, speaker: str) -> None:
    matcher = _matcher(_source("src", source_name), _source("other", "Иван Петров"))
    assert "src" not in _eligible(matcher, speaker)


def test_label_and_digits_alone_do_not_match_a_named_source() -> None:
    matcher = _matcher(_source("max", "Максимов"), _source("other", "Иван Петров"))
    # A label-only speaker carries no name: every source stays searchable.
    assert sorted(_eligible(matcher, "СИНХ")) == ["max", "other"]


def test_same_surname_with_different_first_names_is_not_picked_at_random() -> None:
    matcher = _matcher(
        _source("anna", "Анна Максимова"),
        _source("petr", "Пётр Максимов"),
    )
    assert _eligible(matcher, "Анна Максимова") == ["anna"]
    assert _eligible(matcher, "СИНХ Пётр Максимов") == ["petr"]


def test_ambiguous_bare_surname_keeps_all_candidates() -> None:
    matcher = _matcher(
        _source("anna", "Анна Максимова"),
        _source("petr", "Пётр Максимов"),
    )
    assert sorted(_eligible(matcher, "Максимов")) == ["anna", "petr"]


def test_role_description_after_comma_is_ignored_and_label_comma_falls_through() -> None:
    matcher = _matcher(_source("max", "Максимов"), _source("other", "Иван Петров"))
    assert _eligible(matcher, "Максимова, мать героя") == ["max"]
    assert _eligible(matcher, "СНХ, Максимова") == ["max"]


def test_speaker_name_tokens_drops_labels_and_numbers() -> None:
    assert speaker_name_tokens("СИНХ 29-626 Максимов") == speaker_name_tokens("Максимова")
    assert speaker_name_tokens("220626_Краснова") == speaker_name_tokens("Краснову")
    assert speaker_name_tokens("40726") == []


def test_setting_suffix_and_kinship_labels_still_match() -> None:
    matcher = _matcher(_source("dacha", "Сторожева дача"), _source("intv", "Мать интервью"))
    assert _eligible(matcher, "Вера Сторожева") == ["dacha"]
    matcher = _matcher(_source("mom", "Мама Иванова"), _source("other", "Иван Петров"))
    assert _eligible(matcher, "Мама") == ["mom"]
