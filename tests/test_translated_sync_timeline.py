import hashlib
from pathlib import Path

import pytest

from stage2.timeline_parser import parse_normalized_timeline


def config(duration=14):
    return {
        "active": "tts",
        "delay_sec": 0.8,
        "duck_db": -18.0,
        "gain_db": 0.0,
        "fade_sec": 0.2,
        "variants": {
            "tts": {
                "path": "/tmp/translation.wav",
                "source_in_sec": 0.0,
                "source_out_sec": duration,
                "duration_sec": duration,
                "sha256": "a" * 64,
                "text_sha256": hashlib.sha256("hello".encode()).hexdigest(),
            }
        },
    }


def timeline(cfg):
    norm = {
        "status": "confirmed",
        "blocks": [
            {"uid": "a", "type": "sync", "normalized_text": "hello", "order": 1},
            {"uid": "b", "type": "sync", "normalized_text": "next", "order": 2},
        ],
    }
    matches = {
        "a": {
            "status": "confirmed",
            "source_id": "s",
            "source_in_sec": 5,
            "source_out_sec": 15,
            "query_text": "hello",
            "metadata": {"speech_in_sec": 5, "speech_out_sec": 15, "translation_audio": cfg},
        },
        "b": {
            "status": "confirmed",
            "source_id": "s",
            "source_in_sec": 20,
            "source_out_sec": 23,
            "query_text": "next",
            "metadata": {},
        },
    }
    return parse_normalized_timeline(norm, Path("/tmp"), Path("/tmp"), "test", sync_matches=matches)


def test_translation_extends_slot_without_changing_source_and_rebuilds():
    first = timeline(config())
    assert first.segments[0].source_in_sec == 5
    assert first.segments[0].source_out_sec == 15
    assert first.segments[0].duration_sec == pytest.approx(14.8)
    assert first.segments[0].translation_audio["timeline_offset_sec"] == pytest.approx(0.8)
    assert first.segments[1].start_sec == pytest.approx(14.8)
    assert first.total_duration_sec == pytest.approx(17.8)
    assert timeline(config()).to_dict() == first.to_dict()
    assert timeline(config(4)).segments[1].start_sec == pytest.approx(10)


def test_stale_translation_is_rejected():
    cfg = config()
    cfg["variants"]["tts"]["text_sha256"] = "b" * 64
    with pytest.raises(ValueError, match="text"):
        timeline(cfg)


def test_plan_checks_translation_asset_and_build_hash(tmp_path):
    import wave
    from stage2.assembly_plan import build_assembly_plan, validate_plan_complete
    from stage3.builds import wav_inputs
    from stage2.assembly_manifest import sha256_file

    wav = tmp_path / "voice.wav"
    with wave.open(str(wav), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(8000)
        f.writeframes(b"\0\0" * 8000)
    cfg = config(1)
    cfg["variants"]["tts"].update(path=str(wav), sha256=sha256_file(wav))
    plan = build_assembly_plan(timeline(cfg))
    assert plan["items"][0]["translation_audio"]["path"] == str(wav)
    assert not [b for b in validate_plan_complete(plan) if b["segment_id"] == "snh1"]
    entries = wav_inputs(plan)
    assert entries[0]["sha256"] == sha256_file(wav)
    wav.write_bytes(b"changed")
    assert any(b["reason"] == "translation_audio_invalid" for b in validate_plan_complete(plan))
    assert wav_inputs(plan)[0]["sha256"] != entries[0]["sha256"]


def test_speech_padding_changes_offset_and_frame_ceiling():
    cfg = config(14.01)
    norm = {
        "status": "confirmed",
        "blocks": [{"uid": "a", "type": "sync", "normalized_text": "hello", "order": 1}],
    }
    match = {
        "status": "confirmed",
        "source_id": "s",
        "source_in_sec": 5,
        "source_out_sec": 15,
        "query_text": "hello",
        "metadata": {"speech_in_sec": 6, "speech_out_sec": 14, "translation_audio": cfg},
    }
    seg = parse_normalized_timeline(
        norm, Path("/tmp"), Path("/tmp"), "t", sync_matches={"a": match}
    ).segments[0]
    assert seg.translation_audio["timeline_offset_sec"] == pytest.approx(1.8)
    assert seg.duration_sec == pytest.approx(15.84)


def test_fingerprint_changes_for_translation_settings_and_text():
    from types import SimpleNamespace
    from stage_common import sync_matches_fingerprint

    m = SimpleNamespace(
        segment_id="a",
        status="confirmed",
        source_id="s",
        source_in_sec=5,
        source_out_sec=15,
        confidence=1.0,
        query_text="hello",
        metadata={"translation_audio": config()},
    )
    first = sync_matches_fingerprint([m])
    m.metadata["translation_audio"]["gain_db"] = 3
    assert sync_matches_fingerprint([m]) != first
    second = sync_matches_fingerprint([m])
    m.query_text = "changed"
    assert sync_matches_fingerprint([m]) != second


def test_translated_fragments_are_not_merged():
    from stage2.models import TimelineSegment
    from stage2.timeline_parser import _merge_adjacent_sync_fragments

    common = dict(
        type="snh",
        text="text",
        duration_sec=2,
        parent_block_uid="a",
        match_status="confirmed",
        source_id="s",
    )
    a = TimelineSegment(
        id="a",
        source_in_sec=0,
        source_out_sec=2,
        speech_in_sec=0.1,
        speech_out_sec=1.9,
        translation_audio={"path": "/tmp/a.wav"},
        **common,
    )
    b = TimelineSegment(
        id="b", source_in_sec=2, source_out_sec=4, speech_in_sec=2.1, speech_out_sec=3.9, **common
    )
    assert len(_merge_adjacent_sync_fragments([a, b])) == 2


def test_translation_host_path_resolves_for_readiness_and_hash(tmp_path, monkeypatch):
    import wave
    from api_stage3_routes import _host_timeline_paths, _container_resolver
    from stage2.assembly_plan import build_assembly_plan, validate_plan_complete
    from stage3.builds import wav_inputs
    from stage2.assembly_manifest import sha256_file

    root = tmp_path / "projects" / "P"
    root.mkdir(parents=True)
    wav = root / "translation.wav"
    with wave.open(str(wav), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(8000)
        f.writeframes(b"\0\0" * 8000)
    cfg = config(1)
    cfg["variants"]["tts"].update(path=str(wav), sha256=sha256_file(wav))
    data = timeline(cfg).to_dict()
    monkeypatch.setenv("ROUGH_CUT_HOST_PROJECTS_ROOT", "/external/projects")
    host = _host_timeline_paths(data, project="P", project_root=root)
    assert (
        host["segments"][0]["translation_audio"]["path"] == "/external/projects/P/translation.wav"
    )
    plan = build_assembly_plan(host)
    resolve = _container_resolver("P", root)
    blockers = validate_plan_complete(
        plan, path_exists=lambda p: Path(resolve(p)).exists(), path_resolver=resolve
    )
    assert not [b for b in blockers if b["segment_id"] == "snh1"]
    assert wav_inputs(plan, path_resolver=resolve)[0]["sha256"] == sha256_file(wav)


def test_frame_rounding_contains_translation_clip():
    cfg = config(14.019)
    cfg["delay_sec"] = 0.801
    cfg["variants"]["tts"]["source_in_sec"] = 0.02
    seg = timeline(cfg).segments[0]
    import math

    clip_end = (
        math.ceil(seg.translation_audio["timeline_offset_sec"] * 25)
        + math.ceil(14.019 * 25)
        - math.ceil(0.02 * 25)
    )
    assert seg.duration_sec * 25 >= clip_end


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("duck_db", -121),
        ("duck_db", 0.01),
        ("duck_db", float("nan")),
        ("gain_db", -121),
        ("gain_db", 24.01),
        ("gain_db", 100000),
        ("gain_db", float("nan")),
    ],
)
def test_invalid_db_settings_are_rejected(key, value):
    cfg = config()
    cfg[key] = value
    with pytest.raises(ValueError, match=key):
        timeline(cfg)
