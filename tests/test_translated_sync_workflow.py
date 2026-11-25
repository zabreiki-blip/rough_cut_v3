"""Browser-facing workflow contract from a Word cue through Stage 3 XML."""

import json
import wave
from io import BytesIO
from unittest.mock import patch
from xml.etree import ElementTree as ET

from docx import Document
import api
from stage2.sync_models import SyncMatch
from stage2.models import FullTimeline
from stage3.nested_xml import MediaSource, build_nested_xml


def test_word_match_confirm_recording_builds_extended_nested_timeline(tmp_path):
    projects = tmp_path / "projects"
    projects.mkdir()
    with patch.object(api, "ROOT_DIR", tmp_path), patch.object(api, "PROJECTS_DIR", projects):
        api.app.config.update(TESTING=True)
        api.app.config.pop("API_TOKEN", None)
        client = api.app.test_client()
        assert (
            client.post(
                "/stage2/sync-sources",
                json={
                    "project": "P",
                    "source": {"id": "cam", "premiere_sequence": "Camera", "duration_sec": 60},
                },
            ).status_code
            == 201
        )
        doc = Document()
        doc.add_paragraph("[00:00:01 - 00:00:11] Привет мир")
        content = BytesIO()
        doc.save(content)
        content.seek(0)
        response = client.post(
            "/stage2/sync-sources/cam/transcript-docx",
            data={"project": "P", "file": (content, "lines.docx")},
        )
        assert response.status_code == 200 and response.json["result"]["cue_count"] == 1
        root = projects / "P" / "work"
        root.mkdir(parents=True, exist_ok=True)
        normalized = {
            "source": "",
            "source_hash": "",
            "status": "confirmed",
            "revision": 1,
            "blocks": [
                {
                    "uid": "block",
                    "order": 1,
                    "type": "sync",
                    "normalized_text": "Привет мир",
                    "original_text": "Привет мир",
                    "timecode_start": "00:00:01",
                },
                {
                    "uid": "next",
                    "order": 2,
                    "type": "sync",
                    "normalized_text": "Дальше",
                    "original_text": "Дальше",
                },
            ],
        }
        (root / "scenario_normalized.json").write_text(json.dumps(normalized, ensure_ascii=False))
        assert api._confirmed_normalization("P")[1] == "confirmed"
        matched = client.post(
            "/stage2/translated-sync/match", json={"project": "P", "source_ids": ["cam"]}
        )
        assert matched.status_code == 200, matched.json
        segment = matched.json["result"]["matches"][0]["segment_id"]
        confirmation = client.patch(
            f"/stage2/sync-matches/{segment}",
            json={
                "project": "P",
                "status": "confirmed",
                "source_id": "cam",
                "source_in_sec": 1,
                "source_out_sec": 11,
            },
        )
        assert confirmation.status_code == 200, confirmation.json

        def synthesize(**kwargs):
            output = kwargs["speaker_dir"] / kwargs["file_name"]
            output.parent.mkdir(parents=True, exist_ok=True)
            with wave.open(str(output), "wb") as out:
                out.setnchannels(1)
                out.setsampwidth(2)
                out.setframerate(8000)
                out.writeframes(b"\0\0" * (14 * 8000))
            return {"file": str(output)}

        with patch.object(api, "synthesize_phrase", synthesize):
            generated = client.post(
                f"/stage2/translated-sync/{segment}/tts",
                json={"project": "P", "provider": "macos", "voice_name": "fixture"},
            )
        assert generated.status_code == 200, generated.json
        tts_config = generated.json["result"]["translation_audio"]
        tts_config.update(delay_sec=0.8, duck_db=-18, gain_db=0, fade_sec=0)
        store = api.SyncMatchStore(root / "sync_matches.json")
        persisted = store.get(segment)
        persisted.metadata.update(speech_in_sec=1, speech_out_sec=11)
        store.save(persisted)
        next_match = SyncMatch(
            "next",
            "Дальше",
            source_id="cam",
            source_in_sec=20,
            source_out_sec=23,
            status="confirmed",
            metadata={"normalization_revision": 1},
        )
        store.save(next_match)
        generated_build = client.post("/stage2/build", json={"project": "P"})
        assert generated_build.status_code == 200, generated_build.json
        generated_timeline = FullTimeline.from_dict(
            json.loads((root / "full_timeline.json").read_text())
        )
        assert generated_timeline.segments[0].duration_sec == 14.8
        assert generated_timeline.segments[1].start_sec == 14.8
        media = {"cam": MediaSource("cam", "/tmp/camera.mov", 60, 25, 1920, 1080)}
        long_xml = ET.fromstring(
            build_nested_xml(generated_timeline, media).split("\n", 2)[2]
        ).find("./project/children/sequence")
        assert long_xml.findtext("duration") == "445"
        assert long_xml.findtext("./media/audio/track[3]/clipitem/end") == "370"
        wav = tmp_path / "voice.wav"
        with wave.open(str(wav), "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(8000)
            out.writeframes(b"\0\0" * (6 * 8000))
        with wav.open("rb") as audio:
            uploaded = client.post(
                f"/stage2/translated-sync/{segment}/recording",
                data={
                    "project": "P",
                    "source_in_sec": "0",
                    "source_out_sec": "6",
                    "audio": (audio, "ru.wav"),
                },
            )
        assert uploaded.status_code == 200, uploaded.json
        audio_cfg = uploaded.json["result"]["translation_audio"]
        audio_cfg.update(active="recorded", delay_sec=0.8, duck_db=-18, gain_db=0, fade_sec=0)
        rebuilt = client.post("/stage2/build", json={"project": "P"})
        assert rebuilt.status_code == 200, rebuilt.json
        timeline = FullTimeline.from_dict(json.loads((root / "full_timeline.json").read_text()))
        assert timeline.segments[0].duration_sec == 10
        assert timeline.segments[1].start_sec == 10
        xml = build_nested_xml(timeline, media)
        final = ET.fromstring(xml.split("\n", 2)[2]).find("./project/children/sequence")
        assert final.find("./media/audio/track[3]/clipitem") is not None
