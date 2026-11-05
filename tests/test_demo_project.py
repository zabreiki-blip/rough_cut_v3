"""The real helper must populate a fresh project without changing prior work."""

from __future__ import annotations

import os
import shutil
import subprocess
import wave
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def demo_root(tmp_path: Path) -> Path:
    root = tmp_path / "copy with spaces"
    (root / "scripts").mkdir(parents=True)
    helper = ROOT / "scripts/create-demo"
    assert helper.is_file(), "scripts/create-demo must exist"
    shutil.copy2(helper, root / "scripts/create-demo")
    shutil.copytree(ROOT / "examples/first-project", root / "examples/first-project")
    return root


def run_demo(root: Path) -> tuple[Path, str]:
    before = set((root / "projects").glob("*"))
    result = subprocess.run(
        ["bash", str(root / "scripts/create-demo")],
        cwd=root.parent,
        text=True,
        capture_output=True,
        check=True,
        env={**os.environ, "ROUGH_CUT_PORT": "8000"},
    )
    created = set((root / "projects").glob("*")) - before
    assert len(created) == 1
    return created.pop(), result.stdout


def test_demo_populates_three_inboxes_with_valid_speech_wavs(demo_root: Path) -> None:
    project, output = run_demo(demo_root)
    assert project.name.startswith("demo_")
    assert f"http://127.0.0.1:8000/project/{project.name}" in output
    assert "Готовая запись" in output
    assert (project / "ЗАГРУЗИТЬ/Сценарии/scenario.txt").read_bytes() == (
        ROOT / "examples/first-project/scenario.txt"
    ).read_bytes()
    for folder, name in (("Закадр", "narrator.wav"), ("Просмотровки", "review.wav")):
        target = project / "ЗАГРУЗИТЬ" / folder / name
        assert target.read_bytes() == (ROOT / "examples/first-project" / name).read_bytes()
        with wave.open(str(target), "rb") as wav:
            assert wav.getnchannels() == 1
            assert wav.getsampwidth() == 2
            assert wav.getframerate() >= 16000
            assert 2 < wav.getnframes() / wav.getframerate() < 60
    for directory in (
        "input",
        "work/sync_audio",
        "work/transcripts",
        "speaker/assets",
        "media/reviews",
        "media/archive",
        "media/films",
        "media/photos",
        "media/graphics",
        "versions/scenarios",
        "versions/diffs",
        "versions/timelines",
    ):
        assert (project / directory).is_dir()
    assert not list((project / "work").rglob("*.json"))


def test_repeat_run_preserves_existing_projects_and_creates_unique_names(demo_root: Path) -> None:
    existing = demo_root / "projects/demo_existing"
    existing.mkdir(parents=True)
    sentinel = existing / "notes.txt"
    sentinel.write_text("keep my edit", encoding="utf-8")
    first, _ = run_demo(demo_root)
    first_scenario = first / "ЗАГРУЗИТЬ/Сценарии/scenario.txt"
    first_scenario.write_text("edited scenario", encoding="utf-8")
    second, _ = run_demo(demo_root)
    assert first != second
    assert sentinel.read_text(encoding="utf-8") == "keep my edit"
    assert first_scenario.read_text(encoding="utf-8") == "edited scenario"
    assert len(list((demo_root / "projects").iterdir())) == 3
