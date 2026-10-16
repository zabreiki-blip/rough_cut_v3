from pathlib import Path
from types import SimpleNamespace

import pytest

from api_stage1_routes import _revision_speaker_dir
from project_service import resolve_path


def test_restored_job_cannot_write_current_scenario_into_previous_revision(tmp_path: Path) -> None:
    root = tmp_path / "BRIK"
    speaker = root / "speaker"
    paths = {"root": root, "speaker": speaker}

    def abs_path(value: str, default: Path, _root: Path) -> Path:
        return Path(value) if value else default

    services = SimpleNamespace(
        _abs_path=abs_path,
        _revision_speaker_dir=lambda _paths, revision: speaker / "revisions" / revision,
    )
    old_revision = speaker / "revisions" / "rev_0004"
    assert (
        _revision_speaker_dir(services, {"speaker_dir": str(old_revision)}, paths, "rev_0005")
        == speaker / "revisions" / "rev_0005"
    )


def test_custom_speaker_directory_is_preserved(tmp_path: Path) -> None:
    root = tmp_path / "BRIK"
    speaker = root / "speaker"
    paths = {"root": root, "speaker": speaker}
    custom = root / "speaker" / "draft"
    services = SimpleNamespace(
        _abs_path=lambda value, default, _root: Path(value) if value else default,
        _revision_speaker_dir=lambda _paths, revision: speaker / "revisions" / revision,
    )
    assert (
        _revision_speaker_dir(services, {"speaker_dir": str(custom)}, paths, "rev_0005") == custom
    )


def test_host_speaker_path_maps_into_container_project(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "projects" / "BRIK"
    speaker = root / "speaker"
    paths = {"root": root, "speaker": speaker}
    host_projects = Path("/Users/mac/Documents/AI/rough_cut_v3/projects")
    monkeypatch.setenv("ROUGH_CUT_HOST_PROJECTS_ROOT", str(host_projects))
    services = SimpleNamespace(
        _abs_path=lambda value, default, allowed: resolve_path(
            value, default, root_dir=tmp_path, allowed_root=allowed
        ),
        _revision_speaker_dir=lambda _paths, revision: speaker / "revisions" / revision,
    )

    assert (
        _revision_speaker_dir(
            services, {"speaker_dir": str(host_projects / "BRIK" / "speaker")}, paths, "rev_0005"
        )
        == speaker / "revisions" / "rev_0005"
    )
    assert (
        _revision_speaker_dir(
            services,
            {"speaker_dir": str(host_projects / "BRIK" / "speaker" / "draft")},
            paths,
            "rev_0005",
        )
        == speaker / "draft"
    )
    with pytest.raises(ValueError, match="Path must be inside"):
        _revision_speaker_dir(
            services,
            {"speaker_dir": str(host_projects / "OTHER" / "speaker")},
            paths,
            "rev_0005",
        )
