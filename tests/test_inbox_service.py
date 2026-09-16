from __future__ import annotations

from pathlib import Path

from inbox_service import discover_project_inbox
from project_service import ensure_project


def test_project_creates_friendly_upload_folders_and_keeps_legacy_layout(tmp_path: Path) -> None:
    paths = ensure_project("Телефильм", tmp_path / "projects")

    assert paths["inbox_scenarios"].is_dir()
    assert paths["inbox_reviews"].is_dir()
    assert paths["inbox_scenarios"] == paths["root"] / "ЗАГРУЗИТЬ" / "Сценарии"
    assert paths["inbox_reviews"] == paths["root"] / "ЗАГРУЗИТЬ" / "Просмотровки"
    assert paths["input"].is_dir()
    assert paths["media_reviews"].is_dir()


def test_discovery_supports_unicode_case_insensitive_extensions_and_legacy(tmp_path: Path) -> None:
    paths = ensure_project("Новости", tmp_path / "projects")
    (paths["inbox_scenarios"] / "Сценарий №1.DOCX").write_bytes(b"scenario")
    (paths["input"] / "заметки.Md").write_text("notes", encoding="utf-8")
    (paths["inbox_reviews"] / "Просмотровка.MP4").write_bytes(b"video")
    (paths["inbox_reviews"] / "Интервью.WaV").write_bytes(b"wave")
    (paths["media_reviews"] / "Архив.mp3").write_bytes(b"audio")
    (paths["inbox_scenarios"] / "не сценарий.pdf").write_bytes(b"pdf")

    result = discover_project_inbox(paths["root"])

    assert [item["name"] for item in result["scenario_files"]] == [
        "Сценарий №1.DOCX",
        "заметки.Md",
    ]
    assert {item["name"] for item in result["review_files"]} == {
        "Архив.mp3",
        "Интервью.WaV",
        "Просмотровка.MP4",
    }
    assert {item["kind"] for item in result["review_files"]} == {"audio", "video"}
    assert all(not Path(item["relative_path"]).is_absolute() for item in result["review_files"])


def test_discovery_deduplicates_by_content_and_prefers_upload_folder(tmp_path: Path) -> None:
    paths = ensure_project("demo", tmp_path / "projects")
    preferred = paths["inbox_scenarios"] / "Новая редакция.docx"
    legacy = paths["input"] / "scenario.docx"
    preferred.write_bytes(b"same document")
    legacy.write_bytes(b"same document")

    result = discover_project_inbox(paths["root"])

    assert len(result["scenario_files"]) == 1
    item = result["scenario_files"][0]
    assert item["source"] == "upload"
    assert item["relative_path"] == "ЗАГРУЗИТЬ/Сценарии/Новая редакция.docx"
    assert item["duplicate_paths"] == ["input/scenario.docx"]
    assert item["fingerprint"].startswith("sha256:")
    assert len(item["fingerprint"]) == len("sha256:") + 64


def test_discovery_ignores_symlinks_even_when_their_target_is_supported(tmp_path: Path) -> None:
    paths = ensure_project("demo", tmp_path / "projects")
    outside = tmp_path / "outside.docx"
    outside.write_bytes(b"secret")
    (paths["inbox_scenarios"] / "unsafe.docx").symlink_to(outside)
    internal = paths["root"] / "hidden.docx"
    internal.write_bytes(b"internal")
    (paths["inbox_scenarios"] / "alias.docx").symlink_to(internal)

    assert discover_project_inbox(paths["root"])["scenario_files"] == []


def test_discovery_is_read_only_and_empty_folders_are_valid(tmp_path: Path) -> None:
    paths = ensure_project("demo", tmp_path / "projects")
    before = sorted(path.relative_to(paths["root"]) for path in paths["root"].rglob("*"))

    assert discover_project_inbox(paths["root"]) == {
        "scenario_files": [],
        "review_files": [],
    }
    after = sorted(path.relative_to(paths["root"]) for path in paths["root"].rglob("*"))
    assert after == before
