from __future__ import annotations

from pathlib import Path
import wave

import pytest

import inbox_service
from project_service import ensure_project
from voiceover.storage import (
    StaleMapError,
    load_recorded_voiceover,
    register_recording_file,
    save_recorded_voiceover,
)


def make_wav(path: Path, seconds: float = 2.0, rate: int = 8000) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(rate)
        stream.writeframes(b"\x01\x00" * int(seconds * rate))
    return path


@pytest.fixture
def paths(tmp_path: Path) -> dict[str, Path]:
    return ensure_project("demo", tmp_path / "projects")


def test_voiceover_hashes_are_cached_by_path_size_and_mtime(
    paths: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    first = make_wav(paths["inbox_voiceover"] / "a.wav", 2.0)
    make_wav(paths["inbox_voiceover"] / "b.wav", 3.0)
    calls: list[str] = []
    original = inbox_service._sha256

    def counting(path: Path) -> str:
        calls.append(path.name)
        return original(path)

    monkeypatch.setattr(inbox_service, "_sha256", counting)
    root = paths["root"]
    one = inbox_service.discover_project_inbox(root)["voiceover_files"]
    assert sorted(calls) == ["a.wav", "b.wav"]
    calls.clear()
    two = inbox_service.discover_project_inbox(root)["voiceover_files"]
    assert calls == [] and [i["fingerprint"] for i in two] == [i["fingerprint"] for i in one]
    assert (root / "work" / "inbox_hash_cache.json").is_file()
    make_wav(first, 4.0)  # size changed: only this file is hashed again
    three = inbox_service.discover_project_inbox(root)["voiceover_files"]
    assert calls == ["a.wav"]
    assert {i["fingerprint"] for i in three} != {i["fingerprint"] for i in two}


def test_registration_does_not_lose_a_recording_registered_during_conversion(
    paths: dict[str, Path],
) -> None:
    slow = paths["inbox_voiceover"] / "slow.mp3"
    slow.parent.mkdir(parents=True, exist_ok=True)
    slow.write_bytes(b"not really mp3")
    other = make_wav(paths["inbox_voiceover"] / "other.wav", 3.0)

    def converter(source: Path, destination: Path) -> None:
        register_recording_file(paths, other)  # another request finishes meanwhile
        make_wav(destination, 2.0)

    register_recording_file(paths, slow, converter=converter)

    names = sorted(item["part_name"] for item in load_recorded_voiceover(paths)["recordings"])
    assert names == ["other.wav", "slow.mp3"]
    store = Path(paths["voiceover_recordings"])
    assert not [p for p in store.iterdir() if p.name.startswith(".tmp-")]


def test_conversion_runs_in_a_temporary_directory_not_in_the_store(
    paths: dict[str, Path],
) -> None:
    source = paths["inbox_voiceover"] / "x.mp3"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"x")
    seen: list[Path] = []

    def converter(origin: Path, destination: Path) -> None:
        seen.append(destination)
        make_wav(destination, 1.0)

    recording = register_recording_file(paths, source, converter=converter)
    assert seen and seen[0].parent.name.startswith(".tmp-")
    assert (paths["root"] / recording.asset).is_file()
    assert not seen[0].exists()


def test_stale_generation_write_is_rejected(paths: dict[str, Path]) -> None:
    register_recording_file(paths, make_wav(paths["inbox_voiceover"] / "a.wav"))
    stale = load_recorded_voiceover(paths)
    save_recorded_voiceover(paths, stale)  # somebody else saved first
    with pytest.raises(StaleMapError):
        save_recorded_voiceover(paths, stale, expected_generation=stale["generation"])
    fresh = load_recorded_voiceover(paths)
    save_recorded_voiceover(paths, fresh, expected_generation=fresh["generation"])
