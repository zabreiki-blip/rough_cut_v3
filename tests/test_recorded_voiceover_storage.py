from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import unicodedata
import wave

import pytest

from project_service import ensure_project
from voiceover.models import RecordingSource, VoiceoverRange
from voiceover.storage import (
    asr_cache_key,
    asr_cache_path,
    load_recorded_voiceover,
    register_recording_file,
    save_recorded_voiceover,
    scan_recorded_voiceover,
)


def make_wav(path: Path, seconds: float = 2.0, rate: int = 8000, channels: int = 1) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = int(seconds * rate)
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(channels)
        stream.setsampwidth(2)
        stream.setframerate(rate)
        stream.writeframes(b"\x01\x00" * frames * channels)
    return path


def fake_converter(source: Path, destination: Path) -> None:
    make_wav(destination, seconds=3.0, rate=16000)


def recording_dict(**overrides: object) -> dict:
    payload = {
        "id": "rec_a",
        "asset": "speaker/recordings/aaa/montage.wav",
        "sha256": "a" * 64,
        "duration_sec": 100.0,
        "sample_rate": 48000,
        "channels": 1,
        "part_index": 0,
        "part_name": "part1.wav",
    }
    payload.update(overrides)
    return payload


def range_dict(uid: str, start: float, end: float, **overrides: object) -> dict:
    payload = {
        "block_uid": uid,
        "text_sha256": "t" * 64,
        "recording_id": "rec_a",
        "source_in_sec": start,
        "source_out_sec": end,
        "speech_in_sec": start + 0.1,
        "speech_out_sec": end - 0.1,
        "status": "confirmed",
        "confidence": 0.9,
        "manual_range": False,
    }
    payload.update(overrides)
    return payload


def write_map(paths: dict[str, Path], recordings: list[dict], ranges: list[dict]) -> None:
    for item in recordings:
        asset = paths["root"] / item["asset"]
        if not asset.exists():
            make_wav(asset, seconds=0.1)
    paths["recorded_voiceover"].parent.mkdir(parents=True, exist_ok=True)
    paths["recorded_voiceover"].write_text(
        json.dumps({"version": 1, "generation": 3, "recordings": recordings, "ranges": ranges}),
        encoding="utf-8",
    )


@pytest.fixture
def paths(tmp_path: Path) -> dict[str, Path]:
    return ensure_project("demo", tmp_path / "projects")


# --- paths / layout --------------------------------------------------------


def test_project_paths_expose_voiceover_locations(paths: dict[str, Path]) -> None:
    root = paths["root"]
    assert paths["inbox_voiceover"] == root / "ЗАГРУЗИТЬ" / "Закадр"
    assert paths["inbox_voiceover"].is_dir()
    assert paths["voiceover_recordings"] == root / "speaker" / "recordings"
    assert paths["recorded_voiceover"] == root / "work" / "recorded_voiceover.json"
    assert paths["recorded_voiceover_asr"] == root / "work" / "recorded_voiceover_asr"


# --- backward compatibility ------------------------------------------------


def test_old_project_without_files_gives_empty_result(paths: dict[str, Path]) -> None:
    data = load_recorded_voiceover(paths)
    assert data["recordings"] == []
    assert data["ranges"] == []
    assert data["generation"] == 0
    assert not paths["recorded_voiceover"].exists()


def test_corrupt_map_gives_empty_result(paths: dict[str, Path]) -> None:
    paths["recorded_voiceover"].write_text("{broken", encoding="utf-8")
    assert load_recorded_voiceover(paths)["ranges"] == []


def test_old_map_without_takes_and_selected_take_is_readable(paths: dict[str, Path]) -> None:
    write_map(paths, [recording_dict()], [range_dict("b1", 1.0, 5.0)])

    item = load_recorded_voiceover(paths)["ranges"][0]

    assert item["takes"] == []
    assert item["selected_take"] is None
    assert item["origin"] == "auto"
    assert item["voice_source"] == "recorded"


# --- round trip ------------------------------------------------------------


def test_round_trip_preserves_ranges_takes_and_selection(paths: dict[str, Path]) -> None:
    take = {
        "source_in_sec": 10.0,
        "source_out_sec": 14.0,
        "speech_in_sec": 10.2,
        "speech_out_sec": 13.8,
        "confidence": 0.8,
        "partial": False,
    }
    second = {
        **take,
        "source_in_sec": 20.0,
        "source_out_sec": 24.0,
        "speech_in_sec": 20.2,
        "speech_out_sec": 23.8,
        "partial": True,
    }
    write_map(paths, [recording_dict()], [])
    data = load_recorded_voiceover(paths)
    data["ranges"] = [
        range_dict(
            "b1",
            20.0,
            24.0,
            takes=[take, second],
            selected_take=1,
            origin="manual",
            voice_source="tts",
            manual_range=True,
        )
    ]

    saved = save_recorded_voiceover(paths, data)
    loaded = load_recorded_voiceover(paths)

    assert saved["generation"] == loaded["generation"] == 4
    item = loaded["ranges"][0]
    assert item["takes"] == [take, second]
    assert item["selected_take"] == 1
    assert item["origin"] == "manual"
    assert item["voice_source"] == "tts"
    assert item["manual_range"] is True
    assert loaded["recordings"][0]["asset"] == "speaker/recordings/aaa/montage.wav"


def test_models_roundtrip_via_dict() -> None:
    source = RecordingSource.from_dict(recording_dict())
    assert RecordingSource.from_dict(source.to_dict()) == source
    item = VoiceoverRange.from_dict(range_dict("b1", 1, 2), duration_sec=100.0)
    assert VoiceoverRange.from_dict(item.to_dict(), duration_sec=100.0) == item


# --- validation ------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"source_in_sec": float("nan")},
        {"source_out_sec": float("inf")},
        {"source_in_sec": -1.0},
        {"source_in_sec": 5.0, "source_out_sec": 5.0},
        {"source_in_sec": 6.0, "source_out_sec": 5.0},
        {"source_out_sec": 100.5},
        {"speech_in_sec": float("-inf")},
        {"speech_out_sec": 200.0},
        {"confidence": float("nan")},
        {"status": "bogus"},
        {"takes": [{"source_in_sec": 90.0, "source_out_sec": 120.0}]},
        {"takes": [{"source_in_sec": float("nan"), "source_out_sec": 3.0}]},
        {"selected_take": 0},
    ],
)
def test_invalid_ranges_are_rejected_on_load(
    paths: dict[str, Path], overrides: dict[str, object]
) -> None:
    write_map(paths, [recording_dict()], [range_dict("b1", 1.0, 5.0, **overrides)])

    data = load_recorded_voiceover(paths)

    assert data["ranges"] == []
    assert [item["block_uid"] for item in data["rejected"]] == ["b1"]


def test_json_with_nan_literal_is_rejected(paths: dict[str, Path]) -> None:
    write_map(paths, [recording_dict()], [range_dict("b1", 1.0, 5.0)])
    text = paths["recorded_voiceover"].read_text(encoding="utf-8")
    paths["recorded_voiceover"].write_text(
        text.replace('"source_in_sec": 1.0', '"source_in_sec": NaN'), encoding="utf-8"
    )
    assert load_recorded_voiceover(paths)["ranges"] == []


def test_range_for_unknown_recording_is_rejected(paths: dict[str, Path]) -> None:
    write_map(paths, [recording_dict()], [range_dict("b1", 1.0, 5.0, recording_id="nope")])
    assert load_recorded_voiceover(paths)["ranges"] == []


@pytest.mark.parametrize(
    "asset",
    [
        "../outside.wav",
        "/etc/passwd",
        "speaker/../../outside.wav",
        "speaker\\..\\x.wav",
        "",
    ],
)
def test_unsafe_recording_paths_are_rejected(paths: dict[str, Path], asset: str) -> None:
    (paths["root"].parent / "outside.wav").write_bytes(b"x")
    write_map(paths, [recording_dict(asset=asset)], [range_dict("b1", 1.0, 5.0)])
    # write_map would create the asset for unsafe paths; recreate map without touching disk
    data = load_recorded_voiceover(paths)
    assert data["recordings"] == []
    assert data["ranges"] == []


def test_symlink_escaping_project_is_rejected(paths: dict[str, Path], tmp_path: Path) -> None:
    outside = make_wav(tmp_path / "outside" / "x.wav")
    link = paths["root"] / "speaker" / "link"
    link.symlink_to(outside.parent, target_is_directory=True)
    write_map(paths, [recording_dict(asset="speaker/link/x.wav")], [range_dict("b1", 1.0, 5.0)])
    assert load_recorded_voiceover(paths)["recordings"] == []


def test_missing_asset_drops_recording_and_ranges(paths: dict[str, Path]) -> None:
    write_map(paths, [recording_dict()], [range_dict("b1", 1.0, 5.0)])
    (paths["root"] / "speaker/recordings/aaa/montage.wav").unlink()
    data = load_recorded_voiceover(paths)
    assert data["recordings"] == []
    assert data["ranges"] == []


def test_overlapping_confirmed_ranges_are_rejected(paths: dict[str, Path]) -> None:
    write_map(
        paths,
        [recording_dict()],
        [
            range_dict("b1", 1.0, 5.0),
            range_dict("b2", 4.0, 9.0),
            range_dict("b3", 5.0, 8.0),
            range_dict("b4", 4.5, 9.5, status="needs_review"),
        ],
    )

    data = load_recorded_voiceover(paths)

    assert [item["block_uid"] for item in data["ranges"]] == ["b1", "b3", "b4"]
    assert [item["block_uid"] for item in data["rejected"]] == ["b2"]
    assert "overlap" in data["rejected"][0]["reason"]


def test_same_range_in_two_parts_is_independent_but_block_cannot_be_confirmed_twice(
    paths: dict[str, Path],
) -> None:
    second = recording_dict(
        id="rec_b",
        asset="speaker/recordings/bbb/montage.wav",
        sha256="b" * 64,
        part_index=1,
        part_name="part2.wav",
    )
    write_map(
        paths,
        [recording_dict(), second],
        [
            range_dict("b1", 1.0, 5.0),
            range_dict("b2", 1.0, 5.0, recording_id="rec_b"),
            range_dict("b1", 10.0, 12.0, recording_id="rec_b"),
        ],
    )

    data = load_recorded_voiceover(paths)

    assert [(r["block_uid"], r["recording_id"]) for r in data["ranges"]] == [
        ("b1", "rec_a"),
        ("b2", "rec_b"),
    ]
    assert len(data["rejected"]) == 1


# --- import / scan ---------------------------------------------------------


def test_register_wav_uses_file_directly_without_zk_files(paths: dict[str, Path]) -> None:
    source = make_wav(paths["inbox_voiceover"] / "Закадр 1.wav", seconds=2.5, rate=22050)

    recording = register_recording_file(paths, source)

    assert recording.duration_sec == pytest.approx(2.5, abs=1e-3)
    assert recording.sample_rate == 22050
    assert recording.channels == 1
    assert recording.part_index == 0
    assert recording.part_name == "Закадр 1.wav"
    assert len(recording.sha256) == 64
    asset = paths["root"] / recording.asset
    assert asset.is_file()
    assert asset.is_relative_to(paths["voiceover_recordings"] / recording.sha256)
    assert not list(paths["root"].rglob("zk*.wav"))
    assert source.is_file()  # user file is never moved or removed
    assert load_recorded_voiceover(paths)["recordings"][0]["id"] == recording.id


def test_reimport_of_same_content_registers_once(paths: dict[str, Path]) -> None:
    first = make_wav(paths["inbox_voiceover"] / "a.wav")
    copy = paths["inbox_voiceover"] / "b.wav"
    shutil.copy(first, copy)

    one = register_recording_file(paths, first)
    two = register_recording_file(paths, first)
    three = register_recording_file(paths, copy)

    assert one == two == three
    assert len(load_recorded_voiceover(paths)["recordings"]) == 1


def test_stored_source_is_immutable(paths: dict[str, Path]) -> None:
    source = make_wav(paths["inbox_voiceover"] / "a.wav")
    recording = register_recording_file(paths, source)
    asset = paths["root"] / recording.asset
    stamp = asset.stat().st_mtime_ns
    register_recording_file(paths, source)
    assert asset.stat().st_mtime_ns == stamp
    assert asset.stat().st_mode & 0o200 == 0  # read-only


def test_mp3_gets_one_shared_pcm_copy_via_converter(paths: dict[str, Path]) -> None:
    source = paths["inbox_voiceover"] / "voice.mp3"
    source.write_bytes(b"ID3 not really mp3")
    calls: list[tuple[Path, Path]] = []

    def converter(src: Path, dst: Path) -> None:
        calls.append((src, dst))
        fake_converter(src, dst)

    first = register_recording_file(paths, source, converter=converter)
    second = register_recording_file(paths, source, converter=converter)

    assert first == second
    assert len(calls) == 1
    assert first.asset.endswith(".wav")
    assert first.duration_sec == pytest.approx(3.0, abs=1e-3)
    assert first.sample_rate == 16000
    assert (paths["root"] / first.original_asset).read_bytes() == source.read_bytes()
    assert not list(paths["voiceover_recordings"].rglob("*.tmp"))


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is required")
def test_real_ffmpeg_converts_mp3_to_pcm_wav(paths: dict[str, Path], tmp_path: Path) -> None:
    wav = make_wav(tmp_path / "tone.wav", seconds=1.0, rate=22050)
    mp3 = paths["inbox_voiceover"] / "tone.mp3"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-i", str(wav), str(mp3)],
        check=True,
        capture_output=True,
    )

    recording = register_recording_file(paths, mp3)

    assert recording.sample_rate == 22050
    assert recording.duration_sec == pytest.approx(1.0, abs=0.15)
    with wave.open(str(paths["root"] / recording.asset), "rb") as stream:
        assert stream.getsampwidth() == 2


def test_failed_conversion_leaves_nothing_registered(paths: dict[str, Path]) -> None:
    source = paths["inbox_voiceover"] / "voice.mp3"
    source.write_bytes(b"broken")

    def failing(src: Path, dst: Path) -> None:
        raise RuntimeError("ffmpeg failed")

    with pytest.raises(RuntimeError):
        register_recording_file(paths, source, converter=failing)

    assert load_recorded_voiceover(paths)["recordings"] == []


def test_register_rejects_file_outside_project(paths: dict[str, Path], tmp_path: Path) -> None:
    outside = make_wav(tmp_path / "outside.wav")
    with pytest.raises(ValueError):
        register_recording_file(paths, outside)


def test_scan_registers_new_file_once(paths: dict[str, Path]) -> None:
    make_wav(paths["inbox_voiceover"] / "one.wav")

    first = scan_recorded_voiceover(paths)
    second = scan_recorded_voiceover(paths)

    assert [item.part_name for item in first] == ["one.wav"]
    assert second == []
    assert len(load_recorded_voiceover(paths)["recordings"]) == 1


def test_scan_empty_folder_means_no_recording(paths: dict[str, Path]) -> None:
    assert scan_recorded_voiceover(paths) == []
    assert load_recorded_voiceover(paths)["recordings"] == []
    assert not paths["recorded_voiceover"].exists()


def test_scan_without_folder_is_noop(paths: dict[str, Path]) -> None:
    paths["inbox_voiceover"].rmdir()
    assert scan_recorded_voiceover(paths) == []


def test_scan_sorts_parts_by_name_and_reindexes_late_arrivals(paths: dict[str, Path]) -> None:
    make_wav(paths["inbox_voiceover"] / "Часть 2.wav", seconds=1.0)
    make_wav(paths["inbox_voiceover"] / "Часть 3.wav", seconds=1.5)
    make_wav(paths["inbox_voiceover"] / "часть 10.wav", seconds=2.0)  # natural: 10 after 3

    scan_recorded_voiceover(paths)
    names = [
        (r["part_index"], r["part_name"]) for r in load_recorded_voiceover(paths)["recordings"]
    ]
    assert names == [(0, "Часть 2.wav"), (1, "Часть 3.wav"), (2, "часть 10.wav")]

    make_wav(paths["inbox_voiceover"] / "Часть 1.wav", seconds=2.5)
    scan_recorded_voiceover(paths)
    names = [
        (r["part_index"], r["part_name"]) for r in load_recorded_voiceover(paths)["recordings"]
    ]
    assert names == [
        (0, "Часть 1.wav"),
        (1, "Часть 2.wav"),
        (2, "Часть 3.wav"),
        (3, "часть 10.wav"),
    ]


def test_scan_handles_nfd_file_and_part_names(paths: dict[str, Path]) -> None:
    nfd = unicodedata.normalize("NFD", "Дубль й.wav")
    make_wav(paths["inbox_voiceover"] / nfd)

    scan_recorded_voiceover(paths)
    scan_recorded_voiceover(paths)

    recordings = load_recorded_voiceover(paths)["recordings"]
    assert len(recordings) == 1
    assert recordings[0]["part_name"] == unicodedata.normalize("NFC", "Дубль й.wav")


def test_scan_finds_nfd_named_folder(paths: dict[str, Path]) -> None:
    root = paths["root"]
    shutil.rmtree(paths["inbox_voiceover"])
    inbox_name = unicodedata.normalize("NFD", "ЗАГРУЗИТЬ")
    folder_name = unicodedata.normalize("NFD", "Закадр")
    make_wav(root / inbox_name / folder_name / "a.wav")

    assert len(scan_recorded_voiceover(paths)) == 1


def test_scan_ignores_symlinks_and_other_extensions(paths: dict[str, Path], tmp_path: Path) -> None:
    outside = make_wav(tmp_path / "outside.wav")
    (paths["inbox_voiceover"] / "link.wav").symlink_to(outside)
    (paths["inbox_voiceover"] / "notes.txt").write_text("x", encoding="utf-8")
    assert scan_recorded_voiceover(paths) == []


def test_scan_keeps_registered_recording_after_user_removes_file(paths: dict[str, Path]) -> None:
    source = make_wav(paths["inbox_voiceover"] / "a.wav")
    scan_recorded_voiceover(paths)
    source.unlink()
    assert scan_recorded_voiceover(paths) == []
    assert len(load_recorded_voiceover(paths)["recordings"]) == 1


# --- ASR cache key ---------------------------------------------------------


def test_asr_cache_key_depends_on_file_hash_and_config_only(paths: dict[str, Path]) -> None:
    config = {"backend": "local", "model": "large-v3", "language": "ru"}
    key = asr_cache_key("a" * 64, config)

    assert key == asr_cache_key("a" * 64, dict(reversed(list(config.items()))))
    assert key != asr_cache_key("b" * 64, config)
    assert key != asr_cache_key("a" * 64, {**config, "model": "small"})
    path = asr_cache_path(paths, "a" * 64, config)
    assert path.parent == paths["recorded_voiceover_asr"]
    assert path.name == f"{key}.json"
    assert not path.exists()  # the key is only computed; ASR is not run here
