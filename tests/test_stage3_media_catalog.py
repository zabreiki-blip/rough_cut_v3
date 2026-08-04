from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

from stage3.media_catalog import MediaCatalogError, resolve_media_directory, scan_media_directory


def _probe_result(*, duration: str = "12.5", rate: str = "25/1"):
    return subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout=json.dumps({
            "format": {"duration": duration},
            "streams": [{
                "codec_type": "video",
                "avg_frame_rate": rate,
                "r_frame_rate": rate,
                "width": 1920,
                "height": 1080,
            }],
        }),
        stderr="",
    )


def test_scan_filters_extensions_and_is_non_recursive_by_default(tmp_path: Path) -> None:
    root = tmp_path / "review-media"
    folder = root / "BRIK"
    nested = folder / "nested"
    nested.mkdir(parents=True)
    video = folder / "Просмотровка 01.MP4"
    video.write_bytes(b"video")
    (folder / "notes.txt").write_text("not media", encoding="utf-8")
    (nested / "nested.mov").write_bytes(b"nested")
    calls: list[Path] = []

    def fake_run(command, **_kwargs):
        calls.append(Path(command[-1]))
        return _probe_result()

    result = scan_media_directory(str(folder), media_root=root, run=fake_run)

    assert [item["name"] for item in result["sources"]] == [video.name]
    assert calls == [video]
    source = result["sources"][0]
    assert source["duration_sec"] == 12.5
    assert source["fps"] == 25.0
    assert source["width"] == 1920
    assert source["height"] == 1080
    assert source["size_bytes"] == 5
    assert source["mtime"] > 0
    assert source["relative_path"] == "BRIK/Просмотровка 01.MP4"


def test_recursive_scan_has_stable_id_and_change_fingerprint(tmp_path: Path) -> None:
    root = tmp_path / "media"
    folder = root / "project"
    folder.mkdir(parents=True)
    video = folder / "clip.mkv"
    video.write_bytes(b"one")

    first = scan_media_directory(
        str(folder), recursive=True, media_root=root, run=lambda *_a, **_k: _probe_result()
    )["sources"][0]
    second = scan_media_directory(
        str(folder), recursive=True, media_root=root, run=lambda *_a, **_k: _probe_result()
    )["sources"][0]
    assert second["source_id"] == first["source_id"]
    assert second["fingerprint"] == first["fingerprint"]

    video.write_bytes(b"changed-content")
    changed = scan_media_directory(
        str(folder), recursive=True, media_root=root, run=lambda *_a, **_k: _probe_result()
    )["sources"][0]
    assert changed["source_id"] == first["source_id"]
    assert changed["fingerprint"] != first["fingerprint"]


@pytest.mark.parametrize("directory", ["/etc", "/review-media/../../etc"])
def test_outside_directory_is_rejected(tmp_path: Path, directory: str) -> None:
    root = tmp_path / "review-media"
    root.mkdir()
    with pytest.raises(MediaCatalogError):
        resolve_media_directory(directory, media_root=root)


def test_symlink_to_file_outside_root_is_never_probed(tmp_path: Path) -> None:
    root = tmp_path / "review-media"
    folder = root / "project"
    folder.mkdir(parents=True)
    outside = tmp_path / "secret.mp4"
    outside.write_bytes(b"secret")
    (folder / "linked.mp4").symlink_to(outside)

    def must_not_run(*_args, **_kwargs):
        raise AssertionError("ffprobe must not read an outside-root symlink")

    result = scan_media_directory(str(folder), media_root=root, run=must_not_run)
    assert result["sources"] == []
