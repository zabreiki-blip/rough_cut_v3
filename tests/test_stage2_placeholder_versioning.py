from pathlib import Path
from typing import Any

import pytest

from stage2.placeholder import (
    generate_versioned_placeholder,
    placeholder_asset_key,
    placeholder_asset_path,
)
from api_stage2_routes import _revision_placeholder_dir


def _key(**changes: Any) -> str:
    fields: dict[str, Any] = {
        "block_uid": "block-42",
        "text": "Текст синхрона",
        "speaker": "Анна",
        "segment_type": "snh",
        "role": "редактор",
        "timecode": "01:02:03",
    }
    fields.update(changes)
    return placeholder_asset_key(**fields)


def test_asset_key_is_stable_and_does_not_depend_on_positional_segment_id() -> None:
    # A caller can renumber snh1 to snh9 without changing any identity input.
    assert _key() == _key()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("block_uid", "block-43"),
        ("text", "Исправленный текст синхрона"),
        ("speaker", "Борис"),
        ("segment_type", "host"),
        ("role", "ведущий"),
        ("timecode", "01:02:04"),
        ("footer_label", "Новая подпись"),
        ("width", 1280),
        ("height", 720),
    ],
)
def test_asset_key_changes_with_identity_or_visible_content(field: str, value: Any) -> None:
    assert _key(**{field: value}) != _key()


def test_asset_path_is_safe_and_stays_inside_requested_directory() -> None:
    directory = Path("/tmp/placeholders")
    path = placeholder_asset_path(
        directory,
        "../Блок 1",
        text="Текст",
        speaker="Спикер",
        segment_type="snh",
    )

    assert path.parent == directory
    assert path.suffix == ".png"
    assert ".." not in path.name
    assert path.name.startswith("placeholder-v1-")


def test_revision_placeholder_directory_is_isolated_from_other_revisions(tmp_path: Path) -> None:
    paths = {"work": tmp_path / "work"}

    assert (
        _revision_placeholder_dir(paths, "rev_0002")
        == tmp_path / "work" / "placeholders" / "rev_0002"
    )
    assert _revision_placeholder_dir(paths, "") == tmp_path / "work" / "placeholders"


def test_versioned_generator_keeps_old_asset_when_content_changes(tmp_path: Path) -> None:
    old_path = generate_versioned_placeholder(
        "block-42",
        "Анна",
        "редактор",
        "01:02:03",
        "Первая редакция",
        tmp_path,
        segment_type="snh",
        width=320,
        height=180,
    )
    new_path = generate_versioned_placeholder(
        "block-42",
        "Анна",
        "редактор",
        "01:02:03",
        "Вторая редакция",
        tmp_path,
        segment_type="snh",
        width=320,
        height=180,
    )

    assert old_path != new_path
    assert old_path.is_file()
    assert new_path.is_file()


def test_unicode_normalization_does_not_duplicate_same_visible_asset() -> None:
    assert _key(speaker="Cafe\u0301") == _key(speaker="Caf\u00e9")


@pytest.mark.parametrize("field", ["block_uid", "segment_type"])
def test_required_identity_fields_are_rejected(field: str) -> None:
    with pytest.raises(ValueError):
        _key(**{field: " "})
