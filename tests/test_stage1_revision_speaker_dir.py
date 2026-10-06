from pathlib import Path
from types import SimpleNamespace

from api_stage1_routes import _revision_speaker_dir


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
