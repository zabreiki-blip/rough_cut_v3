from __future__ import annotations

from pathlib import Path

import pytest

from project_service import (
    discover_input_files,
    ensure_project,
    list_projects,
    normalize_project_name,
    project_paths,
    read_json_safe,
    resolve_path,
    resolve_project_name,
)


def test_project_service_builds_expected_layout(tmp_path: Path) -> None:
    paths = ensure_project("Монтаж_01", tmp_path / "projects")

    assert paths == project_paths("Монтаж_01", tmp_path / "projects")
    for key in (
        "input",
        "work",
        "speaker",
        "sync_audio",
        "sync_transcripts",
        "media_reviews",
        "media_archive",
        "media_films",
        "media_photos",
        "media_graphics",
        "scenario_versions",
        "scenario_diffs",
        "timeline_versions",
    ):
        assert paths[key].is_dir()
    assert paths["timeline"] == tmp_path / "projects/Монтаж_01/work/full_timeline.json"
    assert paths["media_archive"] == tmp_path / "projects/Монтаж_01/media/archive"
    assert paths["scenario_versions"] == tmp_path / "projects/Монтаж_01/versions/scenarios"


def test_project_names_are_validated_and_listed_deterministically(tmp_path: Path) -> None:
    projects = tmp_path / "projects"
    projects.mkdir()
    for name in ("zeta", "alpha_02", "Русский-проект", "invalid project"):
        (projects / name).mkdir()

    assert normalize_project_name(" Русский-проект ") == "Русский-проект"
    assert list_projects(projects) == ["alpha_02", "zeta", "Русский-проект"]
    assert resolve_project_name(None, projects) == "alpha_02"
    with pytest.raises(ValueError):
        normalize_project_name("../escape")


def test_json_and_input_discovery_have_explicit_boundaries(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    input_dir = root / "projects/demo/input"
    input_dir.mkdir(parents=True)
    (input_dir / "scenario.docx").write_bytes(b"docx")
    (input_dir / "notes.md").write_text("notes", encoding="utf-8")
    (input_dir / "ignore.wav").write_bytes(b"wav")

    assert discover_input_files(input_dir, root) == [
        "projects/demo/input/notes.md",
        "projects/demo/input/scenario.docx",
    ]
    broken = root / "broken.json"
    broken.write_text("{", encoding="utf-8")
    errors: list[tuple[Path, Exception]] = []
    assert read_json_safe(broken, on_error=lambda path, exc: errors.append((path, exc))) == {}
    assert errors and errors[0][0] == broken


def test_resolve_path_rejects_paths_outside_project_root(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    allowed = root / "projects/demo"
    fallback = allowed / "work/default.json"

    assert resolve_path("", fallback, root_dir=root, allowed_root=allowed) == fallback
    assert (
        resolve_path(
            "projects/demo/input/scenario.txt",
            fallback,
            root_dir=root,
            allowed_root=allowed,
        )
        == allowed / "input/scenario.txt"
    )
    with pytest.raises(ValueError):
        resolve_path(
            "projects/other/input/scenario.txt",
            fallback,
            root_dir=root,
            allowed_root=allowed,
        )
