from io import BytesIO
from unittest.mock import patch

import pytest
from docx import Document
import api


@pytest.fixture
def client(tmp_path):
    projects = tmp_path / "projects"
    projects.mkdir()
    with patch.object(api, "ROOT_DIR", tmp_path), patch.object(api, "PROJECTS_DIR", projects):
        api.app.config.update(TESTING=True)
        api.app.config.pop("API_TOKEN", None)
        yield api.app.test_client()


def document(text):
    doc = Document()
    doc.add_paragraph(f"[00:00:01 - 00:00:03] {text}")
    data = BytesIO()
    doc.save(data)
    data.seek(0)
    return data


def test_import_docx_preserves_valid_import_after_invalid_upload(client):
    assert (
        client.post(
            "/stage2/sync-sources",
            json={
                "project": "demo",
                "source": {"id": "s", "premiere_sequence": "S", "duration_sec": 10},
            },
        ).status_code
        == 201
    )
    url = "/stage2/sync-sources/s/transcript-docx"
    result = client.post(url, data={"project": "demo", "file": (document("Привет мир"), "a.docx")})
    assert result.status_code == 200
    assert result.json["result"]["cues"][0]["russian_text"] == "Привет мир"
    invalid = client.post(url, data={"project": "demo", "file": (BytesIO(b"bad"), "bad.docx")})
    assert invalid.status_code == 400
    assert (
        client.get(url, query_string={"project": "demo"}).json["result"]["cues"][0]["russian_text"]
        == "Привет мир"
    )


def test_match_imported_cues_without_asr_and_preserve_confirmed(client, tmp_path):
    import json

    client.post(
        "/stage2/sync-sources",
        json={
            "project": "demo",
            "source": {"id": "s", "premiere_sequence": "S", "duration_sec": 10},
        },
    )
    normalized = tmp_path / "projects/demo/work/scenario_normalized.json"
    normalized.write_text(
        json.dumps(
            {
                "source": "",
                "source_hash": "",
                "status": "confirmed",
                "revision": 1,
                "blocks": [
                    {
                        "uid": "b",
                        "order": 1,
                        "type": "sync",
                        "normalized_text": "Привет мир",
                        "original_text": "Привет мир",
                    }
                ],
            },
            ensure_ascii=False,
        )
    )
    client.post(
        "/stage2/sync-sources/s/transcript-docx",
        data={"project": "demo", "file": (document("Привет мир"), "a.docx")},
    )
    response = client.post("/stage2/translated-sync/match", json={"project": "demo"})
    assert response.status_code == 200, response.json
    assert response.json["result"]["matches"][0]["source_in_sec"] == 1
    store = api.SyncMatchStore(tmp_path / "projects/demo/work/sync_matches.json")
    item = store.get("b")
    item.status = "confirmed"
    item.source_in_sec = 1.5
    store.save(item)
    client.post("/stage2/translated-sync/match", json={"project": "demo"})
    assert store.get("b").source_in_sec == 1.5


def test_match_replaces_prior_failed_full_transcript_row(client, tmp_path):
    import json
    from stage2.sync_models import SyncMatch

    client.post(
        "/stage2/sync-sources",
        json={
            "project": "demo",
            "source": {"id": "s", "premiere_sequence": "S", "duration_sec": 10},
        },
    )
    root = tmp_path / "projects/demo/work"
    (root / "scenario_normalized.json").write_text(
        json.dumps(
            {
                "source": "",
                "source_hash": "",
                "status": "confirmed",
                "revision": 1,
                "blocks": [
                    {
                        "uid": "b",
                        "order": 1,
                        "type": "sync",
                        "normalized_text": "Привет мир",
                        "original_text": "Привет мир",
                    }
                ],
            },
            ensure_ascii=False,
        )
    )
    client.post(
        "/stage2/sync-sources/s/transcript-docx",
        data={"project": "demo", "file": (document("Привет мир"), "a.docx")},
    )
    store = api.SyncMatchStore(root / "sync_matches.json")
    store.save(SyncMatch("b", "Привет мир", status="not_found", strategy="full_transcript"))

    response = client.post("/stage2/translated-sync/match", json={"project": "demo"})
    assert response.status_code == 200, response.json
    match = store.get("b")
    assert match.status == "needs_review"
    assert match.source_id == "s"
    assert (match.source_in_sec, match.source_out_sec) == (1, 3)


def test_display_import_override_ranks_duplicate_using_scenario_display_time(client, tmp_path):
    import json

    client.post(
        "/stage2/sync-sources",
        json={
            "project": "demo",
            "source": {"id": "s", "premiere_sequence": "S", "duration_sec": 120},
        },
    )
    normalized = tmp_path / "projects/demo/work/scenario_normalized.json"
    normalized.write_text(
        json.dumps(
            {
                "source": "",
                "source_hash": "",
                "status": "confirmed",
                "revision": 1,
                "blocks": [
                    {
                        "uid": "b",
                        "order": 1,
                        "type": "sync",
                        "normalized_text": "Повтор текста",
                        "original_text": "Повтор текста",
                        "timecode_start": "01:00:50",
                    }
                ],
            },
            ensure_ascii=False,
        )
    )
    doc = Document()
    doc.add_paragraph("[01:00:05] Повтор текста")
    doc.add_paragraph("[01:00:50] Повтор текста")
    data = BytesIO()
    doc.save(data)
    upload_url = "/stage2/sync-sources/s/transcript-docx"
    imported = client.post(
        upload_url,
        data={
            "project": "demo",
            "timecode_mode": "display",
            "sequence_start_tc": "01:00:00",
            "file": (BytesIO(data.getvalue()), "display.docx"),
        },
    )
    assert imported.status_code == 200, imported.json
    response = client.post("/stage2/translated-sync/match", json={"project": "demo"})
    assert response.status_code == 200, response.json
    assert response.json["result"]["matches"][0]["source_in_sec"] == 50
    source = client.get("/stage2/sync-sources", query_string={"project": "demo"}).json["result"][
        "sources"
    ][0]
    assert source["sequence_start_tc"] == "01:00:00"

    invalid = client.post(
        upload_url,
        data={
            "project": "demo",
            "timecode_mode": "display",
            "sequence_start_tc": "bad",
            "file": (BytesIO(data.getvalue()), "invalid.docx"),
        },
    )
    assert invalid.status_code == 400
    source = client.get("/stage2/sync-sources", query_string={"project": "demo"}).json["result"][
        "sources"
    ][0]
    assert source["sequence_start_tc"] == "01:00:00"


def test_mixed_import_modes_require_explicit_match_timecode_mode(client, tmp_path):
    import json
    from stage2.sync_models import SyncMatch

    for source_id in ("relative", "display"):
        client.post(
            "/stage2/sync-sources",
            json={
                "project": "demo",
                "source": {"id": source_id, "premiere_sequence": source_id, "duration_sec": 120},
            },
        )
    root = tmp_path / "projects/demo/work"
    (root / "scenario_normalized.json").write_text(
        json.dumps(
            {
                "source": "",
                "source_hash": "",
                "status": "confirmed",
                "revision": 1,
                "blocks": [
                    {
                        "uid": "b",
                        "order": 1,
                        "type": "sync",
                        "normalized_text": "Одинаковая фраза",
                        "original_text": "Одинаковая фраза",
                        "timecode_start": "01:00:01",
                    }
                ],
            },
            ensure_ascii=False,
        )
    )
    relative_doc = Document()
    relative_doc.add_paragraph("[00:00:01] Одинаковая фраза")
    relative_bytes = BytesIO()
    relative_doc.save(relative_bytes)
    display_doc = Document()
    display_doc.add_paragraph("[01:00:01] Одинаковая фраза")
    display_bytes = BytesIO()
    display_doc.save(display_bytes)
    client.post(
        "/stage2/sync-sources/relative/transcript-docx",
        data={
            "project": "demo",
            "timecode_mode": "relative",
            "file": (BytesIO(relative_bytes.getvalue()), "relative.docx"),
        },
    )
    client.post(
        "/stage2/sync-sources/display/transcript-docx",
        data={
            "project": "demo",
            "timecode_mode": "display",
            "sequence_start_tc": "01:00:00",
            "file": (BytesIO(display_bytes.getvalue()), "display.docx"),
        },
    )
    store = api.SyncMatchStore(root / "sync_matches.json")
    store.save(SyncMatch("b", "Одинаковая фраза", status="not_found", strategy="full_transcript"))
    before = store.get("b").to_dict()

    ambiguous = client.post("/stage2/translated-sync/match", json={"project": "demo"})
    assert ambiguous.status_code == 400
    assert store.get("b").to_dict() == before

    explicit = client.post(
        "/stage2/translated-sync/match", json={"project": "demo", "timecode_mode": "display"}
    )
    assert explicit.status_code == 200, explicit.json
    assert store.get("b").source_id == "display"


def test_recorded_audio_is_shared_with_validated_ranges(client, tmp_path):
    import json, wave
    from stage2.sync_models import SyncMatch

    client.post(
        "/stage2/sync-sources",
        json={
            "project": "demo",
            "source": {"id": "s", "premiere_sequence": "S", "duration_sec": 10},
        },
    )
    normalized = tmp_path / "projects/demo/work/scenario_normalized.json"
    normalized.write_text(
        json.dumps(
            {
                "source": "",
                "source_hash": "",
                "status": "confirmed",
                "revision": 1,
                "blocks": [
                    {
                        "uid": "b",
                        "order": 1,
                        "type": "sync",
                        "normalized_text": "Привет мир",
                        "original_text": "Привет мир",
                    }
                ],
            },
            ensure_ascii=False,
        )
    )
    api.SyncMatchStore(tmp_path / "projects/demo/work/sync_matches.json").save(
        SyncMatch(
            "b",
            "Привет мир",
            source_id="s",
            source_in_sec=1,
            source_out_sec=3,
            status="confirmed",
            metadata={"normalization_revision": 1},
        )
    )
    wav = BytesIO()
    with wave.open(wav, "wb") as f:
        f.setparams((1, 2, 8000, 8000, "NONE", "not compressed"))
        f.writeframes(b"\0\0" * 8000)
    wav.seek(0)
    response = client.post(
        "/stage2/translated-sync/b/recording",
        data={
            "project": "demo",
            "audio": (wav, "voice.wav"),
            "source_in_sec": "0.2",
            "source_out_sec": "0.8",
        },
    )
    assert response.status_code == 200, response.json
    recording = response.json["result"]["translation_audio"]["variants"]["recorded"]
    assert recording["duration_sec"] == 1
    assert recording["source_in_sec"] == 0.2
    assert client.get("/stage2/translated-sync/b/audio-file?project=demo").status_code == 200
    invalid = client.patch(
        "/stage2/translated-sync/b/audio",
        json={"project": "demo", "recording_id": recording["recording_id"], "source_out_sec": 2},
    )
    assert invalid.status_code == 400


def test_tts_uses_current_unit_and_selects_verified_wav(client, tmp_path):
    import json, wave
    from stage2.sync_models import SyncMatch

    client.post(
        "/stage2/sync-sources",
        json={
            "project": "demo",
            "source": {"id": "s", "premiere_sequence": "S", "duration_sec": 10},
        },
    )
    normalized = tmp_path / "projects/demo/work/scenario_normalized.json"
    normalized.write_text(
        json.dumps(
            {
                "source": "",
                "source_hash": "",
                "status": "confirmed",
                "revision": 1,
                "blocks": [
                    {
                        "uid": "b",
                        "order": 1,
                        "type": "sync",
                        "normalized_text": "Привет мир",
                        "original_text": "Привет мир",
                    }
                ],
            },
            ensure_ascii=False,
        )
    )
    api.SyncMatchStore(tmp_path / "projects/demo/work/sync_matches.json").save(
        SyncMatch(
            "b",
            "Привет мир",
            source_id="s",
            source_in_sec=1,
            source_out_sec=3,
            status="confirmed",
            metadata={"normalization_revision": 1},
        )
    )

    def synthesize(**kwargs):
        assert kwargs["text"] == "Привет мир"
        output = kwargs["speaker_dir"] / kwargs["file_name"]
        output.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(output), "wb") as f:
            f.setparams((1, 2, 8000, 8000, "NONE", "not compressed"))
            f.writeframes(b"\0\0" * 8000)
        return {"file": str(output)}

    with patch.object(api, "synthesize_phrase", synthesize):
        response = client.post(
            "/stage2/translated-sync/b/tts",
            json={"project": "demo", "provider": "macos", "text": "ignored"},
        )
    assert response.status_code == 200, response.json
    assert response.json["result"]["translation_audio"]["active"] == "tts"
    assert client.get("/stage2/translated-sync/b/audio-file?project=demo").status_code == 200


def test_tts_provider_runtime_error_returns_json_500_and_releases_reservation(client, tmp_path):
    import json
    from stage2.sync_models import SyncMatch

    client.post(
        "/stage2/sync-sources",
        json={
            "project": "demo",
            "source": {"id": "s", "premiere_sequence": "S", "duration_sec": 10},
        },
    )
    root = tmp_path / "projects/demo"
    (root / "work/scenario_normalized.json").write_text(
        json.dumps(
            {
                "source": "",
                "source_hash": "",
                "status": "confirmed",
                "revision": 1,
                "blocks": [
                    {
                        "uid": "b",
                        "order": 1,
                        "type": "sync",
                        "normalized_text": "Привет мир",
                        "original_text": "Привет мир",
                    }
                ],
            },
            ensure_ascii=False,
        )
    )
    api.SyncMatchStore(root / "work/sync_matches.json").save(
        SyncMatch(
            "b",
            "Привет мир",
            source_id="s",
            source_in_sec=1,
            source_out_sec=3,
            status="confirmed",
            metadata={"normalization_revision": 1},
        )
    )

    def fail_provider(**kwargs):
        raise RuntimeError("provider failed")

    with patch.object(api, "synthesize_phrase", fail_provider):
        response = client.post(
            "/stage2/translated-sync/b/tts", json={"project": "demo", "provider": "macos"}
        )
    assert response.status_code == 500
    assert response.json["ok"] is False
    assert api._reserve_tts_project("demo") is True
    api._release_tts_project("demo")


def test_translation_audio_can_be_disabled_and_reenabled_without_resynthesis(client, tmp_path):
    import json
    import wave
    from pathlib import Path
    from stage2.models import FullTimeline
    from stage2.sync_models import SyncMatch

    client.post(
        "/stage2/sync-sources",
        json={
            "project": "demo",
            "source": {"id": "s", "premiere_sequence": "S", "duration_sec": 10},
        },
    )
    root = tmp_path / "projects/demo"
    (root / "work/scenario_normalized.json").write_text(
        json.dumps(
            {
                "source": "",
                "source_hash": "",
                "status": "confirmed",
                "revision": 1,
                "blocks": [
                    {
                        "uid": "b",
                        "order": 1,
                        "type": "sync",
                        "normalized_text": "Привет мир",
                        "original_text": "Привет мир",
                    }
                ],
            },
            ensure_ascii=False,
        )
    )
    api.SyncMatchStore(root / "work/sync_matches.json").save(
        SyncMatch(
            "b",
            "Привет мир",
            source_id="s",
            source_in_sec=0,
            source_out_sec=10,
            status="confirmed",
            metadata={"normalization_revision": 1},
        )
    )
    calls = []

    def synthesize(**kwargs):
        calls.append(1)
        output = kwargs["speaker_dir"] / kwargs["file_name"]
        with wave.open(str(output), "wb") as wav:
            wav.setparams((1, 2, 8000, 14 * 8000, "NONE", "not compressed"))
            wav.writeframes(b"\0\0" * 14 * 8000)
        return {"file": str(output)}

    with patch.object(api, "synthesize_phrase", synthesize):
        generated = client.post(
            "/stage2/translated-sync/b/tts", json={"project": "demo", "provider": "macos"}
        )
    assert generated.status_code == 200, generated.json
    assert generated.json["result"]["translation_audio"]["enabled"] is True
    assert (
        client.patch(
            "/stage2/translated-sync/b/audio", json={"project": "demo", "delay_sec": 0.8}
        ).status_code
        == 200
    )

    def build_duration():
        built = client.post("/stage2/build", json={"project": "demo"})
        assert built.status_code == 200, built.json
        timeline = FullTimeline.from_dict(
            json.loads((root / "work/full_timeline.json").read_text())
        )
        return timeline.segments[0].duration_sec, timeline.segments[0].translation_audio

    duration, overlay = build_duration()
    assert duration == pytest.approx(14.8)
    assert overlay
    audio_before = client.get(
        "/stage2/translated-sync/b/audio", query_string={"project": "demo"}
    ).json["result"]["translation_audio"]
    tts_path = Path(audio_before["variants"]["tts"]["path"])
    valid_bytes = tts_path.read_bytes()
    tts_path.write_bytes(b"stale damaged asset")
    disabled = client.patch(
        "/stage2/translated-sync/b/audio", json={"project": "demo", "enabled": False}
    )
    assert disabled.status_code == 200, disabled.json
    config = disabled.json["result"]["translation_audio"]
    assert config["active"] == audio_before["active"]
    assert config["variants"] == audio_before["variants"]
    duration, overlay = build_duration()
    assert duration == pytest.approx(10)
    assert overlay is None
    tts_path.write_bytes(valid_bytes)

    invalid = client.patch(
        "/stage2/translated-sync/b/audio", json={"project": "demo", "enabled": "false"}
    )
    assert invalid.status_code == 400
    enabled = client.patch(
        "/stage2/translated-sync/b/audio", json={"project": "demo", "enabled": True}
    )
    assert enabled.status_code == 200, enabled.json
    assert enabled.json["result"]["translation_audio"]["variants"] == audio_before["variants"]
    duration, overlay = build_duration()
    assert duration == pytest.approx(14.8)
    assert overlay
    assert len(calls) == 1


def test_reimport_changed_cue_marks_confirmed_match_without_changing_range(client, tmp_path):
    from stage2.sync_models import SyncMatch

    client.post(
        "/stage2/sync-sources",
        json={
            "project": "demo",
            "source": {"id": "s", "premiere_sequence": "S", "duration_sec": 10},
        },
    )
    url = "/stage2/sync-sources/s/transcript-docx"
    data = document("Привет мир").getvalue()
    first = client.post(url, data={"project": "demo", "file": (BytesIO(data), "one.docx")}).json[
        "result"
    ]
    store = api.SyncMatchStore(tmp_path / "projects/demo/work/sync_matches.json")
    store.save(
        SyncMatch(
            "b",
            "Привет мир",
            source_id="s",
            source_in_sec=1.2,
            source_out_sec=2.5,
            status="confirmed",
            strategy="translated_docx",
            metadata={"document_cue_id": first["cues"][0]["id"]},
        )
    )
    client.post(url, data={"project": "demo", "file": (BytesIO(data), "one.docx")})
    assert "retime_pending_review" not in store.get("b").metadata
    client.post(url, data={"project": "demo", "file": (document("Другой текст"), "two.docx")})
    changed = store.get("b")
    assert changed.metadata["retime_pending_review"] is True
    assert (changed.source_in_sec, changed.source_out_sec) == (1.2, 2.5)


def test_audio_stream_rejects_tampered_selected_asset(client, tmp_path):
    import json
    from stage2.sync_models import SyncMatch
    from translated_sync.audio import text_sha256

    client.post(
        "/stage2/sync-sources",
        json={
            "project": "demo",
            "source": {"id": "s", "premiere_sequence": "S", "duration_sec": 10},
        },
    )
    root = tmp_path / "projects/demo"
    (root / "work/scenario_normalized.json").write_text(
        json.dumps(
            {
                "source": "",
                "source_hash": "",
                "status": "confirmed",
                "revision": 1,
                "blocks": [
                    {
                        "uid": "b",
                        "order": 1,
                        "type": "sync",
                        "normalized_text": "Привет мир",
                        "original_text": "Привет мир",
                    }
                ],
            },
            ensure_ascii=False,
        )
    )
    config = {
        "active": "tts",
        "variants": {
            "tts": {
                "path": "../outside.wav",
                "sha256": "0" * 64,
                "text_sha256": text_sha256("Привет мир"),
                "duration_sec": 1,
                "source_in_sec": 0,
                "source_out_sec": 1,
            }
        },
    }
    api.SyncMatchStore(root / "work/sync_matches.json").save(
        SyncMatch(
            "b",
            "Привет мир",
            source_id="s",
            source_in_sec=1,
            source_out_sec=3,
            status="confirmed",
            metadata={"normalization_revision": 1, "translation_audio": config},
        )
    )
    (root.parent / "outside.wav").write_bytes(b"secret")
    assert client.get("/stage2/translated-sync/b/audio-file?project=demo").status_code == 400
    assert (
        client.patch(
            "/stage2/translated-sync/b/audio", json={"project": "demo", "active": "tts"}
        ).status_code
        == 400
    )


def test_invalid_recording_range_does_not_register_recording(client, tmp_path):
    import json, wave
    from stage2.sync_models import SyncMatch
    from voiceover.storage import load_recorded_voiceover

    client.post(
        "/stage2/sync-sources",
        json={
            "project": "demo",
            "source": {"id": "s", "premiere_sequence": "S", "duration_sec": 10},
        },
    )
    root = tmp_path / "projects/demo"
    (root / "work/scenario_normalized.json").write_text(
        json.dumps(
            {
                "source": "",
                "source_hash": "",
                "status": "confirmed",
                "revision": 1,
                "blocks": [
                    {
                        "uid": "b",
                        "order": 1,
                        "type": "sync",
                        "normalized_text": "Привет мир",
                        "original_text": "Привет мир",
                    }
                ],
            },
            ensure_ascii=False,
        )
    )
    api.SyncMatchStore(root / "work/sync_matches.json").save(
        SyncMatch(
            "b",
            "Привет мир",
            source_id="s",
            source_in_sec=1,
            source_out_sec=3,
            status="confirmed",
            metadata={"normalization_revision": 1},
        )
    )
    wav = BytesIO()
    with wave.open(wav, "wb") as f:
        f.setparams((1, 2, 8000, 8000, "NONE", "not compressed"))
        f.writeframes(b"\0\0" * 8000)
    wav.seek(0)
    response = client.post(
        "/stage2/translated-sync/b/recording",
        data={"project": "demo", "audio": (wav, "voice.wav"), "source_out_sec": "2"},
    )
    assert response.status_code == 400
    assert load_recorded_voiceover(api._project_paths("demo"))["recordings"] == []


def test_unknown_source_and_active_project_are_rejected(client):
    assert (
        client.get("/stage2/sync-sources/missing/transcript-docx?project=demo").status_code == 404
    )
    with patch.object(api, "_project_has_active_mutation", return_value=True):
        response = client.post("/stage2/translated-sync/match", json={"project": "demo"})
        assert response.status_code == 409


def test_translated_routes_require_global_auth(client):
    api.app.config["API_TOKEN"] = "example-token"
    try:
        assert (
            client.get("/stage2/sync-sources/missing/transcript-docx?project=demo").status_code
            == 401
        )
    finally:
        api.app.config.pop("API_TOKEN", None)


def test_display_import_validates_offset_and_source_duration(client):
    client.post(
        "/stage2/sync-sources",
        json={
            "project": "demo",
            "source": {"id": "s", "premiere_sequence": "S", "duration_sec": 10},
        },
    )
    url = "/stage2/sync-sources/s/transcript-docx"
    doc = Document()
    doc.add_paragraph("[01:00:02:00] Привет мир")
    data = BytesIO()
    doc.save(data)
    raw = data.getvalue()
    assert (
        client.post(url, data={"project": "demo", "file": (BytesIO(raw), "x.docx")}).status_code
        == 400
    )
    valid = client.post(
        url,
        data={
            "project": "demo",
            "timecode_mode": "display",
            "sequence_start_tc": "01:00:00:00",
            "file": (BytesIO(raw), "x.docx"),
        },
    )
    assert valid.status_code == 200
    assert valid.json["result"]["cues"][0]["start_sec"] == 2
    assert (
        client.post(
            url,
            data={
                "project": "demo",
                "timecode_mode": "display",
                "sequence_start_tc": "01:00:00:00.5",
                "file": (BytesIO(raw), "x.docx"),
            },
        ).status_code
        == 400
    )


def test_recording_rejects_unit_changed_during_real_preparation(client, tmp_path):
    import json, wave
    from stage2.sync_models import SyncMatch
    from voiceover import storage

    client.post(
        "/stage2/sync-sources",
        json={
            "project": "demo",
            "source": {"id": "s", "premiere_sequence": "S", "duration_sec": 10},
        },
    )
    root = tmp_path / "projects/demo"
    normalized = root / "work/scenario_normalized.json"
    doc = {
        "source": "",
        "source_hash": "",
        "status": "confirmed",
        "revision": 1,
        "blocks": [
            {
                "uid": "b",
                "order": 1,
                "type": "sync",
                "normalized_text": "Привет мир",
                "original_text": "Привет мир",
            }
        ],
    }
    normalized.write_text(json.dumps(doc, ensure_ascii=False))
    api.SyncMatchStore(root / "work/sync_matches.json").save(
        SyncMatch(
            "b",
            "Привет мир",
            source_id="s",
            source_in_sec=1,
            source_out_sec=3,
            status="confirmed",
            metadata={"normalization_revision": 1},
        )
    )
    wav = BytesIO()
    with wave.open(wav, "wb") as f:
        f.setparams((1, 2, 8000, 8000, "NONE", "not compressed"))
        f.writeframes(b"\0\0" * 8000)
    wav.seek(0)
    prepare = storage.prepare_recording_file

    def mutate_after_prepare(*args, **kwargs):
        result = prepare(*args, **kwargs)
        doc["revision"] = 2
        normalized.write_text(json.dumps(doc, ensure_ascii=False))
        return result

    with patch.object(storage, "prepare_recording_file", mutate_after_prepare):
        response = client.post(
            "/stage2/translated-sync/b/recording",
            data={"project": "demo", "audio": (wav, "voice.wav")},
        )
    assert response.status_code == 409
    assert storage.load_recorded_voiceover(api._project_paths("demo"))["recordings"] == []


@pytest.mark.parametrize("kind", ["recording", "tts"])
def test_audio_api_output_builds_complete_translation_asset(client, tmp_path, kind):
    import json, wave
    from stage2.assembly_plan import build_assembly_plan, validate_plan_complete
    from stage2.sync_models import SyncMatch

    client.post(
        "/stage2/sync-sources",
        json={
            "project": "demo",
            "source": {"id": "s", "premiere_sequence": "S", "duration_sec": 10},
        },
    )
    root = tmp_path / "projects/demo"
    doc = {
        "source": "",
        "source_hash": "",
        "status": "confirmed",
        "revision": 1,
        "blocks": [
            {
                "uid": "b",
                "order": 1,
                "type": "sync",
                "normalized_text": "Привет мир",
                "original_text": "Привет мир",
            }
        ],
    }
    (root / "work/scenario_normalized.json").write_text(json.dumps(doc, ensure_ascii=False))
    api.SyncMatchStore(root / "work/sync_matches.json").save(
        SyncMatch(
            "b",
            "Привет мир",
            source_id="s",
            source_in_sec=1,
            source_out_sec=3,
            status="confirmed",
            metadata={"normalization_revision": 1},
        )
    )

    def make_wav(out):
        with wave.open(out, "wb") as f:
            f.setparams((1, 2, 8000, 8000, "NONE", "not compressed"))
            f.writeframes(b"\0\0" * 8000)

    if kind == "recording":
        wav = BytesIO()
        make_wav(wav)
        wav.seek(0)
        response = client.post(
            "/stage2/translated-sync/b/recording",
            data={"project": "demo", "audio": (wav, "voice.wav")},
        )
    else:

        def synthesize(**kwargs):
            path = kwargs["speaker_dir"] / kwargs["file_name"]
            make_wav(str(path))
            return {"file": str(path)}

        with patch.object(api, "synthesize_phrase", synthesize):
            response = client.post(
                "/stage2/translated-sync/b/tts", json={"project": "demo", "provider": "macos"}
            )
    assert response.status_code == 200, response.json
    built = client.post("/stage2/build", json={"project": "demo"})
    assert built.status_code == 200, built.json
    timeline = json.loads((root / "work/full_timeline.json").read_text())
    plan = build_assembly_plan(timeline)
    assert any(item.get("translation_audio") for item in plan["items"])
    assert not [
        b for b in validate_plan_complete(plan) if b["reason"].startswith("translation_audio")
    ]


def test_offset_word_import_preserves_raw_marks_and_end(client):
    client.post(
        "/stage2/sync-sources",
        json={
            "project": "demo",
            "source": {
                "id": "s",
                "premiere_sequence": "S",
                "duration_sec": 60,
                "guide_offset_sec": 20,
            },
        },
    )
    url = "/stage2/sync-sources/s/transcript-docx"
    result = client.post(url, data={"project": "demo", "file": (document("Привет мир"), "a.docx")})
    cue = result.json["result"]["cues"][0]
    assert (cue["start_sec"], cue["end_sec"]) == (21, 23)
    assert (cue["raw_start_tc"], cue["raw_end_tc"], cue["explicit_end"]) == (
        "00:00:01",
        "00:00:03",
        True,
    )


def test_semantic_flag_requires_boolean(client):
    result = client.post(
        "/stage2/translated-sync/match", json={"project": "demo", "semantic": "false"}
    )
    assert result.status_code == 400


def test_alternative_selection_binds_selected_document_cue(client, tmp_path):
    import json

    client.post(
        "/stage2/sync-sources",
        json={
            "project": "demo",
            "source": {"id": "s", "premiere_sequence": "S", "duration_sec": 60},
        },
    )
    root = tmp_path / "projects/demo/work"
    (root / "scenario_normalized.json").write_text(
        json.dumps(
            {
                "source": "",
                "source_hash": "",
                "status": "confirmed",
                "revision": 1,
                "blocks": [
                    {
                        "uid": "b",
                        "order": 1,
                        "type": "sync",
                        "normalized_text": "Привет мир",
                        "original_text": "Привет мир",
                    }
                ],
            },
            ensure_ascii=False,
        )
    )
    doc = Document()
    doc.add_paragraph("[00:00:01 - 00:00:03] Привет мир")
    doc.add_paragraph("[00:00:10 - 00:00:12] Привет мир")
    data = BytesIO()
    doc.save(data)
    data.seek(0)
    client.post(
        "/stage2/sync-sources/s/transcript-docx", data={"project": "demo", "file": (data, "a.docx")}
    )
    match = client.post("/stage2/translated-sync/match", json={"project": "demo"}).json["result"][
        "matches"
    ][0]
    alt = match["alternatives"][0]
    assert alt.get("document_cue_id")
    response = client.patch(
        "/stage2/sync-matches/b",
        json={
            "project": "demo",
            "status": "confirmed",
            "source_id": "s",
            "source_in_sec": 10,
            "source_out_sec": 12,
            "document_cue_id": alt["document_cue_id"],
        },
    )
    assert response.status_code == 200
    assert (
        api.SyncMatchStore(root / "sync_matches.json").get("b").metadata["document_cue_id"]
        == alt["document_cue_id"]
    )


def test_semantic_opt_in_can_choose_a_no_overlap_imported_cue(client, tmp_path):
    import json
    from stage2.sync_llm_review import SemanticDecision

    client.post(
        "/stage2/sync-sources",
        json={
            "project": "demo",
            "source": {"id": "s", "premiere_sequence": "S", "duration_sec": 60},
        },
    )
    root = tmp_path / "projects/demo/work"
    (root / "scenario_normalized.json").write_text(
        json.dumps(
            {
                "source": "",
                "source_hash": "",
                "status": "confirmed",
                "revision": 1,
                "blocks": [
                    {
                        "uid": "b",
                        "order": 1,
                        "type": "sync",
                        "normalized_text": "Совсем иные слова",
                        "original_text": "Совсем иные слова",
                    }
                ],
            },
            ensure_ascii=False,
        )
    )
    client.post(
        "/stage2/sync-sources/s/transcript-docx",
        data={"project": "demo", "file": (document("Привет мир"), "a.docx")},
    )

    def review(**kwargs):
        import threading

        acquired = []

        def check_lock():
            locked = api._SYNC_DATA_LOCK.acquire(blocking=False)
            acquired.append(locked)
            if locked:
                api._SYNC_DATA_LOCK.release()

        thread = threading.Thread(target=check_lock)
        thread.start()
        thread.join(timeout=2)
        assert acquired == [True]
        assert kwargs["speaker"] == "" and len(kwargs["candidates"]) == 1
        return SemanticDecision(0, 0.8, "", "stub")

    with patch.object(api, "review_candidates_with_llm", side_effect=review):
        response = client.post(
            "/stage2/translated-sync/match", json={"project": "demo", "semantic": True}
        )
    match = response.json["result"]["matches"][0]
    assert (match["status"], match["source_in_sec"], match["source_out_sec"]) == (
        "needs_review",
        1,
        3,
    )
    assert match["metadata"]["document_cue_id"]


def test_guide_duration_and_display_origin_with_offset(client, tmp_path):
    import wave

    client.post(
        "/stage2/sync-sources",
        json={
            "project": "demo",
            "source": {
                "id": "s",
                "premiere_sequence": "S",
                "duration_sec": 60,
                "guide_offset_sec": 20,
                "guide_audio": "guide.wav",
                "sequence_start_tc": "01:00:00",
            },
        },
    )
    root = tmp_path / "projects/demo"
    with wave.open(str(root / "guide.wav"), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(8000)
        f.writeframes(b"\0\0" * 96000)
    doc = Document()
    doc.add_paragraph("[01:00:21] Привет мир")
    data = BytesIO()
    doc.save(data)
    data.seek(0)
    result = client.post(
        "/stage2/sync-sources/s/transcript-docx",
        data={"project": "demo", "timecode_mode": "display", "file": (data, "a.docx")},
    )
    assert result.status_code == 200, result.json
    cue = result.json["result"]["cues"][0]
    assert (cue["start_sec"], cue["end_sec"]) == (21, 32)
    assert cue["raw_start_tc"] == "01:00:21" and cue["explicit_end"] is False


def test_selected_alternative_reimport_requires_review_and_forgery_fails(client, tmp_path):
    import json

    client.post(
        "/stage2/sync-sources",
        json={
            "project": "demo",
            "source": {"id": "s", "premiere_sequence": "S", "duration_sec": 60},
        },
    )
    root = tmp_path / "projects/demo/work"
    (root / "scenario_normalized.json").write_text(
        json.dumps(
            {
                "source": "",
                "source_hash": "",
                "status": "confirmed",
                "revision": 1,
                "blocks": [
                    {
                        "uid": "b",
                        "order": 1,
                        "type": "sync",
                        "normalized_text": "Привет мир",
                        "original_text": "Привет мир",
                    }
                ],
            },
            ensure_ascii=False,
        )
    )

    def upload(second):
        doc = Document()
        doc.add_paragraph("[00:00:01 - 00:00:03] Привет мир")
        doc.add_paragraph("[00:00:10 - 00:00:12] " + second)
        data = BytesIO()
        doc.save(data)
        data.seek(0)
        return client.post(
            "/stage2/sync-sources/s/transcript-docx",
            data={"project": "demo", "file": (data, "a.docx")},
        )

    upload("Привет мир")
    match = client.post("/stage2/translated-sync/match", json={"project": "demo"}).json["result"][
        "matches"
    ][0]
    alt = match["alternatives"][0]
    payload = {
        "project": "demo",
        "status": "confirmed",
        "source_id": "s",
        "source_in_sec": 10.5,
        "source_out_sec": 11.5,
        "document_cue_id": "forged",
    }
    assert client.patch("/stage2/sync-matches/b", json=payload).status_code == 400
    payload["document_cue_id"] = alt["document_cue_id"]
    assert client.patch("/stage2/sync-matches/b", json=payload).status_code == 200
    upload("Привет новый мир")
    item = api.SyncMatchStore(root / "sync_matches.json").get("b")
    assert item.metadata["retime_pending_review"] is True
    assert (item.source_in_sec, item.source_out_sec) == (10.5, 11.5)


def test_semantic_default_and_stale_result_guard(client, tmp_path):
    import json
    from stage2.sync_llm_review import SemanticDecision

    client.post(
        "/stage2/sync-sources",
        json={
            "project": "demo",
            "source": {"id": "s", "premiere_sequence": "S", "duration_sec": 60},
        },
    )
    root = tmp_path / "projects/demo/work"
    normalization = root / "scenario_normalized.json"
    doc = {
        "source": "",
        "source_hash": "",
        "status": "confirmed",
        "revision": 1,
        "blocks": [
            {
                "uid": "b",
                "order": 1,
                "type": "sync",
                "normalized_text": "Совсем иные слова",
                "original_text": "Совсем иные слова",
            }
        ],
    }
    normalization.write_text(json.dumps(doc, ensure_ascii=False))
    client.post(
        "/stage2/sync-sources/s/transcript-docx",
        data={"project": "demo", "file": (document("Привет мир"), "a.docx")},
    )
    with patch.object(
        api, "review_candidates_with_llm", side_effect=AssertionError("must not call")
    ):
        assert (
            client.post("/stage2/translated-sync/match", json={"project": "demo"}).json["result"][
                "matches"
            ][0]["status"]
            == "not_found"
        )

    def mutate(**kwargs):
        doc["revision"] = 2
        normalization.write_text(json.dumps(doc, ensure_ascii=False))
        return SemanticDecision(0, 0.8, "", "stub")

    with patch.object(api, "review_candidates_with_llm", side_effect=mutate):
        result = client.post(
            "/stage2/translated-sync/match", json={"project": "demo", "semantic": True}
        )
    assert result.status_code == 409
    assert api.SyncMatchStore(root / "sync_matches.json").get("b").status == "not_found"


def test_semantic_rejection_invalid_index_and_provider_error_leave_not_found(client, tmp_path):
    import json
    from stage2.sync_llm_review import SemanticDecision

    client.post(
        "/stage2/sync-sources",
        json={
            "project": "demo",
            "source": {"id": "s", "premiere_sequence": "S", "duration_sec": 60},
        },
    )
    root = tmp_path / "projects/demo/work"
    (root / "scenario_normalized.json").write_text(
        json.dumps(
            {
                "source": "",
                "source_hash": "",
                "status": "confirmed",
                "revision": 1,
                "blocks": [
                    {
                        "uid": "b",
                        "order": 1,
                        "type": "sync",
                        "normalized_text": "Совсем иные слова",
                        "original_text": "Совсем иные слова",
                    }
                ],
            },
            ensure_ascii=False,
        )
    )
    client.post(
        "/stage2/sync-sources/s/transcript-docx",
        data={"project": "demo", "file": (document("Привет мир"), "a.docx")},
    )
    for result, expected in [
        (None, "rejected_or_unavailable"),
        (SemanticDecision(5, 0.8, "", "stub"), "error"),
        (RuntimeError("provider unavailable"), "error"),
    ]:
        with patch.object(
            api,
            "review_candidates_with_llm",
            side_effect=result if isinstance(result, Exception) else None,
            return_value=None if isinstance(result, Exception) else result,
        ):
            response = client.post(
                "/stage2/translated-sync/match", json={"project": "demo", "semantic": True}
            )
        assert response.status_code == 200
        assert response.json["result"]["matches"][0]["status"] == "not_found"
        assert response.json["result"]["semantic"]["statuses"][0]["status"] == expected
        assert "demo" not in api._TTS_ACTIVE_PROJECTS
        assert "demo" not in api._SYNC_ACTIVE_PROJECTS


def test_source_playback_uses_existing_guide_without_recognition(client, tmp_path):
    import wave

    client.post(
        "/stage2/sync-sources",
        json={
            "project": "demo",
            "source": {
                "id": "s",
                "premiere_sequence": "S",
                "duration_sec": 60,
                "guide_audio": "guide.wav",
            },
        },
    )
    path = tmp_path / "projects/demo/guide.wav"
    with wave.open(str(path), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(8000)
        f.writeframes(b"\0\0" * 8000)
    response = client.get("/stage2/sync-sources/s/audio?project=demo")
    assert response.status_code == 200
    assert response.data[:4] == b"RIFF"


def test_cross_source_selection_invalidates_only_selected_source(client, tmp_path):
    import json

    for source in ("a", "b"):
        client.post(
            "/stage2/sync-sources",
            json={
                "project": "demo",
                "source": {"id": source, "premiere_sequence": source, "duration_sec": 60},
            },
        )
    root = tmp_path / "projects/demo/work"
    (root / "scenario_normalized.json").write_text(
        json.dumps(
            {
                "source": "",
                "source_hash": "",
                "status": "confirmed",
                "revision": 1,
                "blocks": [
                    {
                        "uid": "b1",
                        "order": 1,
                        "type": "sync",
                        "normalized_text": "Привет мир",
                        "original_text": "Привет мир",
                    }
                ],
            },
            ensure_ascii=False,
        )
    )

    def upload(source, text):
        return client.post(
            f"/stage2/sync-sources/{source}/transcript-docx",
            data={"project": "demo", "file": (document(text), "a.docx")},
        )

    upload("a", "Привет мир")
    upload("b", "Привет мир")
    match = client.post("/stage2/translated-sync/match", json={"project": "demo"}).json["result"][
        "matches"
    ][0]
    alt = match["alternatives"][0]
    assert alt["source_id"] == "b"
    response = client.patch(
        "/stage2/sync-matches/b1",
        json={
            "project": "demo",
            "status": "confirmed",
            "source_id": "b",
            "source_in_sec": 1,
            "source_out_sec": 3,
            "document_cue_id": alt["document_cue_id"],
        },
    )
    assert response.status_code == 200
    store = api.SyncMatchStore(root / "sync_matches.json")
    upload("a", "Привет новый мир")
    assert not store.get("b1").metadata.get("retime_pending_review")
    upload("b", "Привет новый мир")
    assert store.get("b1").metadata.get("retime_pending_review")


def test_semantic_limits_and_budget_failure_are_visible(client, tmp_path):
    import json
    from cost_budget import CostBudgetExceeded

    client.post(
        "/stage2/sync-sources",
        json={
            "project": "demo",
            "source": {"id": "s", "premiere_sequence": "S", "duration_sec": 100},
        },
    )
    root = tmp_path / "projects/demo/work"
    blocks = [
        {
            "uid": f"b{i}",
            "order": i,
            "type": "sync",
            "normalized_text": f"Совсем иные слова {i}",
            "original_text": f"Совсем иные слова {i}",
        }
        for i in range(9)
    ]
    (root / "scenario_normalized.json").write_text(
        json.dumps(
            {
                "source": "",
                "source_hash": "",
                "status": "confirmed",
                "revision": 1,
                "blocks": blocks,
            },
            ensure_ascii=False,
        )
    )
    doc = Document()
    for i in range(25):
        doc.add_paragraph(f"[00:00:{i:02d} - 00:00:{i + 1:02d}] Привет мир")
    data = BytesIO()
    doc.save(data)
    data.seek(0)
    client.post(
        "/stage2/sync-sources/s/transcript-docx", data={"project": "demo", "file": (data, "a.docx")}
    )
    with patch.object(api, "review_candidates_with_llm", return_value=None) as helper:
        response = client.post(
            "/stage2/translated-sync/match", json={"project": "demo", "semantic": True}
        )
    details = response.json["result"]["semantic"]
    assert details["calls"] == 8 and details["limited"] and helper.call_count == 8
    assert all(
        len(call.kwargs["candidates"]) == 24 and call.kwargs["timeout_seconds"] == 45
        for call in helper.call_args_list
    )
    assert details["statuses"][-1]["status"] == "call_limit"
    with patch.object(
        api, "review_candidates_with_llm", side_effect=CostBudgetExceeded("budget exhausted")
    ) as helper:
        response = client.post(
            "/stage2/translated-sync/match", json={"project": "demo", "semantic": True}
        )
    details = response.json["result"]["semantic"]
    assert details["calls"] == 1 and helper.call_count == 1
    assert details["statuses"][-1]["status"] == "budget_exhausted"


def test_semantic_respects_active_job_guard(client):
    for reserve, release in (
        (api._reserve_tts_project, api._release_tts_project),
        (api._reserve_sync_project, api._release_sync_project),
    ):
        assert reserve("demo")
        try:
            with patch.object(
                api,
                "review_candidates_with_llm",
                side_effect=AssertionError("must not run"),
            ):
                response = client.post(
                    "/stage2/translated-sync/match",
                    json={"project": "demo", "semantic": True},
                )
            assert response.status_code == 409
        finally:
            release("demo")


def test_semantic_reservation_is_reported_as_sync_during_revision_import(client, tmp_path):
    import json

    client.post(
        "/stage2/sync-sources",
        json={
            "project": "demo",
            "source": {"id": "s", "premiere_sequence": "S", "duration_sec": 60},
        },
    )
    root = tmp_path / "projects/demo/work"
    (root / "scenario_normalized.json").write_text(
        json.dumps(
            {
                "source": "",
                "source_hash": "",
                "status": "confirmed",
                "revision": 1,
                "blocks": [
                    {
                        "uid": "b",
                        "order": 1,
                        "type": "sync",
                        "normalized_text": "Совсем иные слова",
                        "original_text": "Совсем иные слова",
                    }
                ],
            },
            ensure_ascii=False,
        )
    )
    client.post(
        "/stage2/sync-sources/s/transcript-docx",
        data={"project": "demo", "file": (document("Привет мир"), "a.docx")},
    )

    busy_errors = []

    def review(**kwargs):
        response = client.post("/stage4/revisions/import", json={"project": "demo"})
        assert response.status_code == 409
        busy_errors.append(response.get_json()["error"])
        return None

    with patch.object(api, "review_candidates_with_llm", side_effect=review):
        result = client.post(
            "/stage2/translated-sync/match",
            json={"project": "demo", "semantic": True},
        )

    assert result.status_code == 200
    assert len(busy_errors) == 1
    assert "сопоставления СНХ" in busy_errors[0]
    assert "озвучки" not in busy_errors[0]
    assert "demo" not in api._TTS_ACTIVE_PROJECTS
    assert "demo" not in api._SYNC_ACTIVE_PROJECTS


def test_offset_change_then_reimport_marks_match_without_changing_trim(client, tmp_path):
    from dataclasses import replace
    from stage2.sync_models import SyncMatch

    client.post(
        "/stage2/sync-sources",
        json={
            "project": "demo",
            "source": {
                "id": "s",
                "premiere_sequence": "S",
                "duration_sec": 60,
                "guide_offset_sec": 20,
            },
        },
    )
    root = tmp_path / "projects/demo/work"
    url = "/stage2/sync-sources/s/transcript-docx"
    first = client.post(
        url, data={"project": "demo", "file": (document("Привет мир"), "a.docx")}
    ).json["result"]["cues"][0]
    matches = api.SyncMatchStore(root / "sync_matches.json")
    matches.save(
        SyncMatch(
            "b",
            "Привет мир",
            source_id="s",
            source_in_sec=21.2,
            source_out_sec=22.8,
            status="confirmed",
            strategy="translated_docx",
            metadata={"document_cue_id": first["id"]},
        )
    )
    sources, _ = api._sync_stores(api._ensure_project("demo"))
    sources.save(replace(sources.get("s"), guide_offset_sec=25))
    second = client.post(
        url, data={"project": "demo", "file": (document("Привет мир"), "a.docx")}
    ).json["result"]["cues"][0]
    item = matches.get("b")
    assert second["id"] != first["id"] and second["start_sec"] == 26
    assert item.metadata["retime_pending_review"] and (item.source_in_sec, item.source_out_sec) == (
        21.2,
        22.8,
    )


@pytest.mark.parametrize("bound", [True, False])
def test_reconfirm_after_document_change_keeps_manual_trim(client, tmp_path, bound):
    import json
    from stage2.sync_models import SyncMatch

    client.post(
        "/stage2/sync-sources",
        json={
            "project": "demo",
            "source": {"id": "s", "premiere_sequence": "S", "duration_sec": 60},
        },
    )
    root = tmp_path / "projects/demo/work"
    (root / "scenario_normalized.json").write_text(
        json.dumps(
            {
                "source": "",
                "source_hash": "",
                "status": "confirmed",
                "revision": 1,
                "blocks": [
                    {
                        "uid": "b",
                        "order": 1,
                        "type": "sync",
                        "normalized_text": "Привет мир",
                        "original_text": "Привет мир",
                    }
                ],
            },
            ensure_ascii=False,
        )
    )
    url = "/stage2/sync-sources/s/transcript-docx"
    cue = client.post(
        url, data={"project": "demo", "file": (document("Привет мир"), "a.docx")}
    ).json["result"]["cues"][0]
    store = api.SyncMatchStore(root / "sync_matches.json")
    metadata = {"document_cue_id": cue["id"]} if bound else {"document_cue_unbound": True}
    store.save(
        SyncMatch(
            "b",
            "Привет мир",
            source_id="s",
            source_in_sec=1.2,
            source_out_sec=2.8,
            status="confirmed",
            strategy="translated_docx",
            metadata=metadata,
        )
    )
    client.post(url, data={"project": "demo", "file": (document("Другой перевод"), "b.docx")})
    assert store.get("b").metadata["retime_pending_review"]
    response = client.patch(
        "/stage2/sync-matches/b",
        json={
            "project": "demo",
            "status": "confirmed",
            "source_id": "s",
            "source_in_sec": 1.2,
            "source_out_sec": 2.8,
        },
    )
    assert response.status_code == 200, response.json
    updated = store.get("b")
    assert not updated.metadata.get("retime_pending_review")
    assert updated.metadata.get("document_cue_unbound")
    assert not updated.metadata.get("document_cue_id")
    assert (updated.source_in_sec, updated.source_out_sec) == (1.2, 2.8)
    client.post(url, data={"project": "demo", "file": (document("Третья версия"), "c.docx")})
    assert store.get("b").metadata["retime_pending_review"]
