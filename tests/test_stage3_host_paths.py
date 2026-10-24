"""Docker: XML carries macOS host paths, but existence checks must run on container paths."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from urllib.request import pathname2url
import wave

import pytest

import api_stage3_routes
from stage3 import builds as stage3_builds
from stage3.nested_xml import build_nested_xml
from stage2.models import FullTimeline, TimelineSegment
from test_stage3_api import _nested_prepare_setup, _segment
from test_stage3_api import client as stage3_client  # noqa: F401

HOST_ROOT = "/nonexistent/host/projects"
_REAL_HOST_CATALOG = api_stage3_routes._host_catalog


def _quote(path: str) -> str:
    """Same percent-encoding that nested_xml applies to `<pathurl>`."""
    return pathname2url(path)


@pytest.fixture()
def client(stage3_client):  # noqa: F811
    return stage3_client


def _host_setup(client, *, media_name="v1.mp4", wav_name="zk.wav", wav=True, source=True):
    """Nested project like `_complete_setup`, but with the real host-path translation."""
    http, app, monkeypatch = client
    monkeypatch.setenv("ROUGH_CUT_HOST_PROJECTS_ROOT", HOST_ROOT)
    root = app.config["TEST_STAGE3_SERVICES"]._ensure_project("BRIK")["root"]
    media = root / "media" / media_name
    wav_file = root / "work" / wav_name
    media.parent.mkdir(parents=True, exist_ok=True)
    wav_file.parent.mkdir(parents=True, exist_ok=True)
    if source:
        media.write_bytes(b"x")
    if wav:
        wav_file.write_bytes(b"x")
    segments = [
        _segment("z1", "zk", 0.0, 2.0, wav_path=str(wav_file)),
        _segment(
            "m1",
            "snh",
            2.0,
            2.0,
            match_status="confirmed",
            source_id="v1",
            source_in_sec=1.0,
            source_out_sec=3.0,
            speech_in_sec=1.2,
            speech_out_sec=2.8,
        ),
    ]
    http, paths = _nested_prepare_setup(client, segments)
    video = {
        "source_id": "v1",
        "container_path": str(media),
        "duration": 100.0,
        "fps": 25,
        "media_kind": "video",
    }
    monkeypatch.setattr(
        api_stage3_routes, "_scan_project_media", lambda *_a, **_k: {"sources": [video]}
    )
    monkeypatch.setattr(api_stage3_routes, "_host_catalog", _REAL_HOST_CATALOG)
    monkeypatch.setattr(
        api_stage3_routes,
        "map_catalog_to_timeline",
        lambda _timeline, _sync, host_catalog: SimpleNamespace(
            catalog={"v1": host_catalog["sources"][0]},
            details={},
            unmapped_source_ids=[],
            to_dict=lambda: {},
        ),
    )
    return http, paths


def _prepare_complete(http):
    return http.post("/stage3/timeline/prepare", json={"project": "BRIK", "mode": "complete"})


@pytest.mark.parametrize(
    ("media_name", "wav_name"), [("v1.mp4", "zk.wav"), ("Свиблова 1.mp4", "голос 1.wav")]
)
def test_existing_files_do_not_block_when_xml_paths_are_host_paths(
    client, media_name, wav_name
) -> None:
    http, paths = _host_setup(client, media_name=media_name, wav_name=wav_name)

    response = _prepare_complete(http)

    assert response.status_code == 200, response.get_json()
    result = response.get_json()["result"]
    assert result["complete"] is True
    assert result["blocker_count"] == 0
    xml = (paths["work"] / result["filename"]).read_text(encoding="utf-8")
    assert f"<pathurl>file://{_quote(HOST_ROOT + '/BRIK/work/' + wav_name)}</pathurl>" in xml
    assert f"<pathurl>file://{_quote(HOST_ROOT + '/BRIK/media/' + media_name)}</pathurl>" in xml


def test_plan_endpoint_does_not_block_existing_files_with_host_paths(client) -> None:
    http, _paths = _host_setup(client)

    body = http.get("/stage3/plan?project=BRIK").get_json()

    assert body["blockers"] == []
    assert body["complete_ready"] is True


def test_deleted_source_still_blocks_and_message_keeps_host_path(client) -> None:
    http, _paths = _host_setup(client, source=False)

    response = _prepare_complete(http)

    assert response.status_code == 409
    blockers = response.get_json()["blockers"]
    assert [(b["uid"], b["reason"]) for b in blockers] == [("m1", "source_missing")]
    assert f"{HOST_ROOT}/BRIK/media/v1.mp4" in blockers[0]["detail"]


def test_deleted_wav_still_blocks_and_message_keeps_host_path(client) -> None:
    http, _paths = _host_setup(client, wav=False)

    response = _prepare_complete(http)

    assert response.status_code == 409
    blockers = response.get_json()["blockers"]
    assert [(b["uid"], b["reason"]) for b in blockers] == [("z1", "missing_wav")]
    assert f"{HOST_ROOT}/BRIK/work/zk.wav" in blockers[0]["detail"]


def test_container_path_inverts_host_path_only_for_project_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ROUGH_CUT_HOST_PROJECTS_ROOT", HOST_ROOT)
    root = tmp_path / "projects" / "BRIK"
    kwargs = {"project": "BRIK", "project_root": root}
    host = api_stage3_routes._host_path(str(root / "media" / "Файл 1.mp4"), **kwargs)
    assert host == f"{HOST_ROOT}/BRIK/media/Файл 1.mp4"
    assert api_stage3_routes._container_path(host, **kwargs) == str(root / "media" / "Файл 1.mp4")
    for foreign in (
        "/Volumes/L_BRICK/a.mp4",
        f"{HOST_ROOT}/OTHER/a.mp4",
        f"{HOST_ROOT}/BRIKX/a.mp4",
        str(root / "media" / "a.mp4"),
        "",
    ):
        assert api_stage3_routes._container_path(foreign, **kwargs) == foreign


def test_container_path_rejects_parent_traversal_under_project_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ROUGH_CUT_HOST_PROJECTS_ROOT", "/h/projects")
    root = tmp_path / "projects" / "BRIK"
    kwargs = {"project": "BRIK", "project_root": root}
    escaping = "/h/projects/BRIK/../OTHER/x.wav"
    assert api_stage3_routes._container_path(escaping, **kwargs) == escaping


def test_container_path_round_trips_with_trailing_slash_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ROUGH_CUT_HOST_PROJECTS_ROOT", "/h/projects/")
    root = tmp_path / "projects" / "BRIK"
    kwargs = {"project": "BRIK", "project_root": root}
    path = str(root / "media" / "Файл 1.mp4")
    host = api_stage3_routes._host_path(path, **kwargs)
    assert host == "/h/projects/BRIK/media/Файл 1.mp4"
    assert api_stage3_routes._container_path(host, **kwargs) == path


def test_container_path_without_env_is_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ROUGH_CUT_HOST_PROJECTS_ROOT", raising=False)
    root = tmp_path / "projects" / "BRIK"
    path = str(root / "media" / "a.mp4")
    kwargs = {"project": "BRIK", "project_root": root}
    assert api_stage3_routes._host_path(path, **kwargs) == path
    assert api_stage3_routes._container_path(path, **kwargs) == path


def _write_wav(path: Path, rate: int) -> None:
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(2)
        stream.setsampwidth(3)
        stream.setframerate(rate)
        stream.writeframes(b"\x00" * 6 * 10)


def test_nested_xml_reads_wav_params_through_path_resolver(tmp_path: Path) -> None:
    real = tmp_path / "голос.wav"
    _write_wav(real, 48000)
    host = "/nonexistent/host/projects/BRIK/голос.wav"
    timeline = FullTimeline(
        project="P",
        fps=25,
        segments=[
            TimelineSegment(
                id="zk1", type="zk", text="t", duration_sec=2, start_sec=0, wav_path=host
            )
        ],
    )

    def resolve(path: str) -> str:
        return str(real) if path == host else path

    default_xml = build_nested_xml(timeline, {})
    xml = build_nested_xml(timeline, {}, path_resolver=resolve)

    assert "<samplerate>24000</samplerate>" in default_xml
    assert "<samplerate>48000</samplerate>" in xml
    assert "<depth>24</depth>" in xml
    assert "<channelcount>2</channelcount>" in xml
    assert (
        "<pathurl>file:///nonexistent/host/projects/BRIK/%D0%B3%D0%BE%D0%BB%D0%BE%D1%81.wav"
        "</pathurl>"
    ) in xml


def test_build_inputs_stat_and_hash_through_path_resolver(tmp_path: Path) -> None:
    media = tmp_path / "Свиблова.mp4"
    media.write_bytes(b"abc")
    wav = tmp_path / "zk.wav"
    wav.write_bytes(b"wav-bytes")
    host_media = "/nonexistent/host/projects/BRIK/Свиблова.mp4"
    host_wav = "/nonexistent/host/projects/BRIK/zk.wav"
    mapping = {host_media: str(media), host_wav: str(wav)}

    def resolve(path: str) -> str:
        return mapping.get(path, path)

    catalog = {"v1": {"source_id": "v1", "container_path": host_media, "duration": 5.0}}
    plan = {"items": [{"uid": "z1", "wav": {"path": host_wav, "kind": "tts"}}]}

    blind = stage3_builds.source_inputs(catalog)
    seen = stage3_builds.source_inputs(catalog, path_resolver=resolve)
    assert blind[0]["size"] is None
    assert seen[0]["size"] == 3 and seen[0]["mtime_ns"] is not None
    assert seen[0]["path"] == host_media  # identity path stays the host path

    assert stage3_builds.wav_inputs(plan)[0]["sha256"] is None
    wav_entry = stage3_builds.wav_inputs(plan, path_resolver=resolve)[0]
    assert wav_entry["size"] == len(b"wav-bytes")
    assert wav_entry["sha256"] == stage3_builds.sha256_file(wav)
