"""Task 4: timeline build, freshness and readiness for the published recorded voiceover."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

import api
from atomic_io import atomic_write_json
from stage_common import _timeline_voiceover_is_current
from test_recorded_voiceover_api import Env
from test_recorded_voiceover_api import env as env  # fixture re-export
from voiceover.readiness import voiceover_readiness


@pytest.fixture(autouse=True)
def _doc_as_normalization(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api, "load_normalization", lambda _path: env.doc)
    monkeypatch.setattr(api, "repair_active_revision_metadata", lambda _paths: {"repaired": False})


def _alias_tts(env: Env, ids: tuple[str, ...]) -> None:
    """Stage 1 exposes TTS as ``zkN.wav`` aliases in the revision speaker dir."""
    speaker_dir = api._revision_speaker_dir(env.paths, env.doc["scenario_revision_id"])
    speaker_dir.mkdir(parents=True, exist_ok=True)
    for segment_id in ids:
        shutil.copyfile(
            env.paths["voiceover_assets"] / f"b{segment_id[2:]}" / "tts.wav",
            speaker_dir / f"{segment_id}.wav",
        )


def _publish_mixed(env: Env) -> None:
    """b1 recorded, b2 manual TTS (WAV exists), b3 TTS fallback, b4 nothing."""
    env.register()
    env.add_tts("b2", "b3")
    _alias_tts(env, ("zk2", "zk3"))
    env.run_job()
    assert env.edit("PATCH", "/ranges/b1", status="confirmed").status_code == 200
    assert env.edit("PATCH", "/blocks/b2/voice-source", voice_source="tts").status_code == 200
    assert env.edit("POST", "/apply").status_code == 200
    atomic_write_json(env.paths["normalized"], env.doc)


def _build(env: Env) -> dict:
    response = env.client.post("/stage2/build", json={"project": "demo"})
    assert response.status_code == 200, response.get_json()
    return json.loads(env.paths["timeline"].read_text(encoding="utf-8"))


def test_build_mixed_timeline_and_status_use_one_readiness(env: Env) -> None:
    _publish_mixed(env)
    timeline = _build(env)
    zk = [s for s in timeline["segments"] if s["type"] == "zk"]
    assert [s["voiceover_kind"] for s in zk] == ["recorded", "tts", "tts", "none"]
    assert zk[0]["source_id"] and zk[0]["wav_path"].endswith(".wav")
    assert zk[0]["duration_sec"] == zk[0]["source_out_sec"] - zk[0]["source_in_sec"]
    assert timeline["recorded_voiceover_sha256"]
    status = env.client.get("/stage1/status", query_string={"project": "demo"}).get_json()["result"]
    by_id = {row["segment_id"]: row for row in status["voiceover_blocks"]}
    assert by_id["zk1"]["ready"] and by_id["zk1"]["voice_source"] == "recorded"
    assert not by_id["zk4"]["ready"]
    assert status["voiceover"]["recorded"] == 1
    assert status["voiceover"]["missing"] == [
        row["segment_id"] for row in status["voiceover_blocks"] if not row["ready"]
    ]
    assert status["timeline_ready"] is True


def test_new_published_map_makes_timeline_stale_for_read_export_and_stage3(env: Env) -> None:
    _publish_mixed(env)
    _build(env)
    assert env.client.get("/stage2/timeline", query_string={"project": "demo"}).status_code == 200
    # Same normalization, new published map (voice_source of b1 changed).
    assert env.edit("PATCH", "/blocks/b1/voice-source", voice_source="tts").status_code == 200
    assert env.edit("POST", "/apply").status_code == 200
    stale = env.client.get("/stage2/timeline", query_string={"project": "demo"})
    assert stale.status_code == 409
    assert "timeline_rebuild_required" in stale.get_json()["error"]
    export = env.client.get("/stage2/export/xml", query_string={"project": "demo"})
    assert export.status_code == 409
    prepare = env.client.post("/stage3/timeline/prepare", json={"project": "demo"})
    assert prepare.status_code == 409
    status = env.client.get("/stage1/status", query_string={"project": "demo"}).get_json()["result"]
    assert status["timeline_ready"] is False
    _build(env)  # rebuild adopts the new map
    assert env.client.get("/stage2/timeline", query_string={"project": "demo"}).status_code == 200


def test_legacy_timeline_without_map_stays_current(env: Env) -> None:
    atomic_write_json(env.paths["normalized"], env.doc)
    assert _timeline_voiceover_is_current({"segments": []}, env.doc, env.paths) is True
    assert (
        _timeline_voiceover_is_current({"recorded_voiceover_sha256": "abc"}, env.doc, env.paths)
        is False
    )


def test_readiness_recording_missing_changed_text_and_range(env: Env, tmp_path: Path) -> None:
    _publish_mixed(env)
    speaker_dir = env.paths["speaker"]
    ready = voiceover_readiness(env.paths, env.doc, speaker_dir)
    assert ready["blocks"][0]["voice_source"] == "recorded"
    published = json.loads(env.paths["recorded_voiceover_published"].read_text(encoding="utf-8"))
    recording = env.paths["root"] / published["recordings"][0]["asset"]
    # changed text of the block: the recorded range is not used
    changed = json.loads(json.dumps(env.doc))
    changed["blocks"][0]["text_for_tts"] = "Совсем другой текст блока"
    changed["blocks"][0]["normalized_text"] = "Совсем другой текст блока"
    row = voiceover_readiness(env.paths, changed, speaker_dir)["blocks"][0]
    assert (row["voice_source"], row["ready"], row["reason"]) == ("none", False, "text_changed")
    # recording file gone: not ready (and no TTS for b1)
    backup = recording.read_bytes()
    recording.unlink()
    row = voiceover_readiness(env.paths, env.doc, speaker_dir)["blocks"][0]
    assert (row["voice_source"], row["ready"], row["reason"]) == (
        "none",
        False,
        "recording_missing",
    )
    recording.write_bytes(backup)
    # range beyond recording duration
    published["blocks"][0]["source_out_sec"] = published["recordings"][0]["duration_sec"] + 5
    atomic_write_json(env.paths["recorded_voiceover_published"], published)
    row = voiceover_readiness(env.paths, env.doc, speaker_dir)["blocks"][0]
    assert (row["ready"], row["reason"]) == (False, "range_invalid")
