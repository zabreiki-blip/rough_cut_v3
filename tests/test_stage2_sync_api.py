from __future__ import annotations

from io import BytesIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import wave

import pytest

import api


class _ImmediateThread:
    def __init__(self, *, target, kwargs, **_unused):
        self.target = target
        self.kwargs = kwargs

    def start(self) -> None:
        self.target(**self.kwargs)


def _wav_bytes(duration_sec: float = 1.0, sample_rate: int = 8000) -> bytes:
    output = BytesIO()
    with wave.open(output, "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(sample_rate)
        stream.writeframes(b"\0\0" * int(duration_sec * sample_rate))
    return output.getvalue()


@pytest.fixture()
def sync_api():
    with TemporaryDirectory() as temporary:
        base = Path(temporary).resolve()
        projects = base / "projects"
        projects.mkdir()
        with (
            patch.object(api, "ROOT_DIR", base),
            patch.object(api, "PROJECTS_DIR", projects),
        ):
            api.app.config.update(TESTING=True)
            api.app.config.pop("API_TOKEN", None)
            with api._SYNC_JOBS_LOCK:
                api._SYNC_JOBS.clear()
                api._SYNC_ACTIVE_PROJECTS.clear()
            yield api.app.test_client(), projects


def _write_confirmed_normalization(projects: Path, revision: int = 7) -> Path:
    path = projects / "demo" / "work" / "scenario_normalized.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "source": "",
                "source_hash": "",
                "status": "confirmed",
                "revision": revision,
                "blocks": [
                    {
                        "uid": "stable-sync-uid",
                        "order": 1,
                        "type": "sync",
                        "original_text": (
                            "Татьяна Васильева, актриса: 00:10 — 00:14 "
                            "Маленькая женщина покоряла великих людей"
                        ),
                        "normalized_text": (
                            "Татьяна Васильева, актриса: 00:10 — 00:14 "
                            "Маленькая женщина покоряла великих людей"
                        ),
                        "speaker": "Татьяна Васильева",
                        "role": "актриса",
                        "timecode_start": "00:10",
                        "timecode_end": "00:14",
                    },
                    {
                        "uid": "stable-host-uid",
                        "order": 2,
                        "type": "host",
                        "original_text": "Ведущий: 00:20 Следующая реплика ведущего",
                        "normalized_text": "Ведущий: 00:20 Следующая реплика ведущего",
                        "speaker": "Ведущий",
                        "timecode_start": "00:20",
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def _write_multi_fragment_normalization(projects: Path) -> Path:
    path = projects / "demo" / "work" / "scenario_normalized.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({
            "source": "",
            "source_hash": "",
            "status": "confirmed",
            "revision": 3,
            "blocks": [{
                "uid": "irina-block",
                "order": 1,
                "type": "sync",
                "original_text": "ИРИНА КОТОВА, ИСТОРИК: три фрагмента",
                "normalized_text": "Шодэ приехал Архитектуру любил Особняки строил",
                "speaker": "ИРИНА КОТОВА",
                "role": "ИСТОРИК",
                "timecode_start": "00:10",
                "sync_fragments": [
                    {"timecode_start": "00:10", "text": "Шодэ приехал"},
                    {"timecode_start": "00:30", "text": "Архитектуру любил"},
                    {"timecode_start": "00:50", "text": "Особняки строил"},
                ],
            }],
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def _write_shared_timeline_normalization(projects: Path) -> Path:
    path = projects / "demo" / "work" / "scenario_normalized.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({
            "source": "",
            "source_hash": "",
            "status": "confirmed",
            "revision": 4,
            "blocks": [
                {
                    "uid": "irina-shared",
                    "order": 1,
                    "type": "sync",
                    "original_text": "Ирина: первая общая цитата",
                    "normalized_text": "первая общая цитата",
                    "speaker": "Ирина Котова",
                    "timecode_start": "00:10",
                },
                {
                    "uid": "alexey-shared",
                    "order": 2,
                    "type": "sync",
                    "original_text": "Алексей: вторая общая цитата",
                    "normalized_text": "вторая общая цитата",
                    "speaker": "Алексей Иванов",
                    "timecode_start": "00:30",
                },
            ],
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def _post_source(client, source_id: str = "vasilieva_01", speaker: str = "Татьяна Васильева"):
    return client.post(
        "/stage2/sync-sources",
        json={
            "project": "demo",
            "id": source_id,
            "speaker": speaker,
            "premiere_sequence": f"SYNC_{source_id.upper()}",
            "sequence_id": f"premiere-{source_id}",
            "duration_sec": 100.0,
        },
    )


def test_actual_stream_bytes_are_limited_without_trusting_content_length(tmp_path) -> None:
    destination = tmp_path / "upload.wav"

    with pytest.raises(api.AudioUploadTooLargeError, match="too large"):
        api._copy_stream_limited(
            BytesIO(b"123456"),
            destination,
            max_bytes=5,
            chunk_size=2,
        )

    assert destination.exists()
    assert destination.stat().st_size <= 4


def test_source_crud_and_server_named_validated_audio(sync_api) -> None:
    client, projects = sync_api
    created = _post_source(client)
    assert created.status_code == 201
    source = created.get_json()["result"]["source"]
    assert source["sequence_name"] == "SYNC_VASILIEVA_01"

    bad_id = client.post(
        "/stage2/sync-sources",
        json={
            "project": "demo",
            "id": "../escape",
            "premiere_sequence": "SYNC_BAD",
        },
    )
    assert bad_id.status_code == 400

    invalid_audio = client.put(
        "/stage2/sync-sources/vasilieva_01/audio",
        data={
            "project": "demo",
            "audio": (BytesIO(b"not a wav"), "guide.wav"),
        },
        content_type="multipart/form-data",
    )
    assert invalid_audio.status_code == 415

    audio = client.put(
        "/stage2/sync-sources/vasilieva_01/audio",
        data={
            "project": "demo",
            "audio": (BytesIO(_wav_bytes(1.25)), "../../client-name.wav"),
        },
        content_type="multipart/form-data",
    )
    assert audio.status_code == 200, audio.get_json()
    fixed = projects / "demo" / "work" / "sync_audio" / "vasilieva_01.wav"
    assert fixed.is_file()
    assert audio.get_json()["result"]["source"]["guide_audio"] == (
        "work/sync_audio/vasilieva_01.wav"
    )

    listing = client.get("/stage2/sync-sources?project=demo")
    assert [item["id"] for item in listing.get_json()["result"]["sources"]] == [
        "vasilieva_01"
    ]
    streamed = client.get(
        "/stage2/sync-sources/vasilieva_01/audio?project=demo"
    )
    assert streamed.status_code == 200
    assert streamed.mimetype in {"audio/x-wav", "audio/wav"}

    deleted = client.delete(
        "/stage2/sync-sources",
        json={"project": "demo", "source_id": "vasilieva_01"},
    )
    assert deleted.status_code == 200
    assert not fixed.exists()


def test_source_reregistration_preserves_guide_and_rejects_id_rebinding(sync_api) -> None:
    client, _projects = sync_api
    assert _post_source(client).status_code == 201
    assert client.put(
        "/stage2/sync-sources/vasilieva_01/audio",
        data={
            "project": "demo",
            "audio": (BytesIO(_wav_bytes()), "guide.wav"),
        },
        content_type="multipart/form-data",
    ).status_code == 200

    refreshed = client.post(
        "/stage2/sync-sources",
        json={
            "project": "demo",
            "id": "vasilieva_01",
            "premiere_sequence": "SYNC_VASILIEVA_01",
            "sequence_id": "premiere-vasilieva_01",
            "guide_audio": "",
        },
    )
    assert refreshed.status_code == 201
    assert refreshed.get_json()["result"]["source"]["guide_audio"] == (
        "work/sync_audio/vasilieva_01.wav"
    )

    rebound = client.post(
        "/stage2/sync-sources",
        json={
            "project": "demo",
            "id": "vasilieva_01",
            "premiere_sequence": "OTHER",
            "sequence_id": "another-premiere-id",
        },
    )
    assert rebound.status_code == 409


def test_words_ingestion_match_patch_build_and_timeline(sync_api) -> None:
    client, projects = sync_api
    _write_confirmed_normalization(projects)
    assert _post_source(client).status_code == 201
    words = [
        {"word": "маленькая", "start": 10.0, "end": 10.5},
        {"word": "женщина", "start": 10.5, "end": 11.0},
        {"word": "покоряла", "start": 11.0, "end": 11.5},
        {"word": "великих", "start": 11.5, "end": 12.0},
        {"word": "людей", "start": 12.0, "end": 12.5},
    ]
    ingested = client.put(
        "/stage2/sync-sources/vasilieva_01/words",
        json={"project": "demo", "words": words},
    )
    assert ingested.status_code == 200, ingested.get_json()
    assert ingested.get_json()["result"]["word_count"] == 5

    with patch.object(api.threading, "Thread", _ImmediateThread):
        started = client.post(
            "/stage2/sync-jobs/start",
            json={"project": "demo"},
        )
    assert started.status_code == 200, started.get_json()
    job_id = started.get_json()["result"]["job_id"]
    job = client.get(f"/stage2/sync-jobs/{job_id}").get_json()["result"]
    assert job["status"] == "completed"
    assert job["reused_indexes"] == 1

    matches = client.get("/stage2/sync-matches?project=demo").get_json()["result"]
    by_uid = {item["block_uid"]: item for item in matches["matches"]}
    found = by_uid["stable-sync-uid"]
    assert found["segment_id"] == "stable-sync-uid"
    assert found["query_text"] == "Маленькая женщина покоряла великих людей"
    assert found["metadata"]["normalization_revision"] == 7
    assert found["status"] == "auto_matched"

    patched = client.patch(
        "/stage2/sync-matches/stable-sync-uid",
        json={
            "project": "demo",
            "status": "confirmed",
            "source_in_sec": 9.75,
            "source_out_sec": 13.25,
        },
    )
    assert patched.status_code == 200, patched.get_json()
    assert patched.get_json()["result"]["matches"][0]["status"] == "confirmed"
    unknown_block = client.patch(
        "/stage2/sync-matches/not-a-block",
        json={"project": "demo", "status": "confirmed"},
    )
    assert unknown_block.status_code == 400
    outside_duration = client.patch(
        "/stage2/sync-matches/stable-sync-uid",
        json={
            "project": "demo",
            "status": "confirmed",
            "source_out_sec": 999.0,
        },
    )
    assert outside_duration.status_code == 400

    with patch.object(api, "generate_placeholder", return_value=None):
        built = client.post("/stage2/build", json={"project": "demo"})
    assert built.status_code == 200, built.get_json()
    result = built.get_json()["result"]
    assert result["matched"] == 1
    assert result["unmatched"] == 1

    timeline = client.get("/stage2/timeline?project=demo")
    assert timeline.status_code == 200
    segments = timeline.get_json()["result"]["segments"]
    first = segments[0]
    assert first["block_uid"] == "stable-sync-uid"
    assert first["sequence_name"] == "SYNC_VASILIEVA_01"
    assert first["duration_sec"] == 3.5
    normalization_path = projects / "demo" / "work" / "scenario_normalized.json"
    changed = json.loads(normalization_path.read_text(encoding="utf-8"))
    changed["revision"] = 8
    normalization_path.write_text(json.dumps(changed), encoding="utf-8")
    assert client.get("/stage2/timeline?project=demo").status_code == 409


def test_background_match_never_overwrites_concurrent_confirmation(sync_api) -> None:
    client, projects = sync_api
    _write_confirmed_normalization(projects)
    assert _post_source(client).status_code == 201
    words = [
        {"word": "маленькая", "start": 10.0, "end": 10.5},
        {"word": "женщина", "start": 10.5, "end": 11.0},
        {"word": "покоряла", "start": 11.0, "end": 11.5},
        {"word": "великих", "start": 11.5, "end": 12.0},
        {"word": "людей", "start": 12.0, "end": 12.5},
    ]
    assert client.put(
        "/stage2/sync-sources/vasilieva_01/words",
        json={"project": "demo", "words": words},
    ).status_code == 200

    original_match = api.SyncMatcher.match
    store = api.SyncMatchStore(
        projects / "demo" / "work" / "sync_matches.json"
    )

    def confirm_while_matching(matcher, request):
        automatic = original_match(matcher, request)
        if request.segment_id == "stable-sync-uid":
            confirmed = api.SyncMatch.from_dict(automatic.to_dict())
            confirmed.status = "confirmed"
            confirmed.metadata["confirmed_by"] = "editor-during-job"
            store.save(confirmed)
        return automatic

    with (
        patch.object(api.SyncMatcher, "match", confirm_while_matching),
        patch.object(api.threading, "Thread", _ImmediateThread),
    ):
        response = client.post(
            "/stage2/sync-jobs/start",
            json={"project": "demo", "rematch_confirmed": True},
        )

    assert response.status_code == 200, response.get_json()
    preserved = store.get("stable-sync-uid")
    assert preserved is not None
    assert preserved.status == "confirmed"
    assert preserved.metadata["confirmed_by"] == "editor-during-job"


def test_job_transcribes_only_missing_indexes_and_isolates_source_failure(sync_api) -> None:
    client, projects = sync_api
    _write_confirmed_normalization(projects)
    assert _post_source(client).status_code == 201
    assert _post_source(client, "broken_01", "Другой герой").status_code == 201

    audio = client.put(
        "/stage2/sync-sources/vasilieva_01/audio",
        data={
            "project": "demo",
            "audio": (BytesIO(_wav_bytes()), "guide.wav"),
        },
        content_type="multipart/form-data",
    )
    assert audio.status_code == 200

    def fake_transcribe(path, progress_callback=None, **_kwargs):
        if Path(path).name == "vasilieva_01.wav":
            assert progress_callback is not None
            progress_callback(5.0, 10.0, 3)
            return {
                "model": "mock-whisper",
                "language": "ru",
                "words": [
                    {"word": "маленькая", "start": 10.0, "end": 10.2},
                    {"word": "женщина", "start": 10.2, "end": 10.4},
                    {"word": "покоряла", "start": 10.4, "end": 10.6},
                    {"word": "великих", "start": 10.6, "end": 10.8},
                    {"word": "людей", "start": 10.8, "end": 11.0},
                ],
            }
        raise AssertionError("unexpected audio")

    with (
        patch.object(api, "transcribe_audio", side_effect=fake_transcribe),
        patch.object(api.threading, "Thread", _ImmediateThread),
    ):
        response = client.post("/stage2/sync-jobs/start", json={"project": "demo"})
    job_id = response.get_json()["result"]["job_id"]
    job = client.get(
        f"/stage2/sync-jobs/status?job_id={job_id}"
    ).get_json()["result"]
    assert job["status"] == "completed"
    assert job["transcribed"] == 1
    assert job["transcription_processed_sec"] == 5.0
    assert job["transcription_duration_sec"] == 10.0
    assert job["transcription_words"] == 3
    assert job["source_failures"][0]["source_id"] == "broken_01"
    assert client.get("/stage2/sync-matches?project=demo").get_json()["result"][
        "matched"
    ] == 1


def test_job_matches_and_builds_each_fragment_of_one_speaker_block(sync_api) -> None:
    client, projects = sync_api
    _write_multi_fragment_normalization(projects)
    assert _post_source(client, "irina_01", "ИРИНА КОТОВА").status_code == 201
    words = [
        {"word": "Шодэ", "start": 10.0, "end": 10.4},
        {"word": "приехал", "start": 10.4, "end": 10.8},
        {"word": "Архитектуру", "start": 30.0, "end": 30.5},
        {"word": "любил", "start": 30.5, "end": 30.9},
        {"word": "Особняки", "start": 50.0, "end": 50.4},
        {"word": "строил", "start": 50.4, "end": 50.8},
    ]
    assert client.put(
        "/stage2/sync-sources/irina_01/words",
        json={"project": "demo", "words": words},
    ).status_code == 200

    with patch.object(api.threading, "Thread", _ImmediateThread):
        response = client.post("/stage2/sync-jobs/start", json={"project": "demo"})
    assert response.status_code == 200, response.get_json()
    matches_result = client.get(
        "/stage2/sync-matches?project=demo"
    ).get_json()["result"]
    assert matches_result["matched"] == 3
    assert [item["segment_id"] for item in matches_result["matches"]] == [
        "irina-block::fragment:1",
        "irina-block::fragment:2",
        "irina-block::fragment:3",
    ]
    assert all(item["block_uid"] == "irina-block" for item in matches_result["matches"])

    with patch.object(api, "generate_placeholder", return_value=None):
        built = client.post("/stage2/build", json={"project": "demo"})
    assert built.status_code == 200, built.get_json()
    assert built.get_json()["result"]["snh"] == 3
    segments = client.get(
        "/stage2/timeline?project=demo"
    ).get_json()["result"]["segments"]
    assert [item["parent_block_uid"] for item in segments] == [
        "irina-block", "irina-block", "irina-block",
    ]


def test_one_registered_timeline_can_supply_multiple_speakers(sync_api) -> None:
    client, projects = sync_api
    _write_shared_timeline_normalization(projects)
    created = client.post(
        "/stage2/sync-sources",
        json={
            "project": "demo",
            "id": "shared_interviews",
            "speaker": "Ирина Котова",
            "speakers": ["Ирина Котова", "Алексей Иванов"],
            "premiere_sequence": "SYNC_SHARED_INTERVIEWS",
            "sequence_id": "premiere-shared",
            "duration_sec": 100.0,
        },
    )
    assert created.status_code == 201, created.get_json()
    source = created.get_json()["result"]["source"]
    assert source["speaker"] == "Ирина Котова"
    assert source["speakers"] == ["Алексей Иванов"]
    assert client.put(
        "/stage2/sync-sources/shared_interviews/words",
        json={
            "project": "demo",
            "words": [
                {"word": "первая", "start": 10.0, "end": 10.3},
                {"word": "общая", "start": 10.3, "end": 10.6},
                {"word": "цитата", "start": 10.6, "end": 10.9},
                {"word": "вторая", "start": 30.0, "end": 30.3},
                {"word": "общая", "start": 30.3, "end": 30.6},
                {"word": "цитата", "start": 30.6, "end": 30.9},
            ],
        },
    ).status_code == 200

    with patch.object(api.threading, "Thread", _ImmediateThread):
        response = client.post("/stage2/sync-jobs/start", json={"project": "demo"})
    assert response.status_code == 200, response.get_json()
    matches = client.get("/stage2/sync-matches?project=demo").get_json()["result"]
    assert matches["matched"] == 2
    assert {item["source_id"] for item in matches["matches"]} == {"shared_interviews"}

    with patch.object(api, "generate_placeholder", return_value=None):
        built = client.post("/stage2/build", json={"project": "demo"})
    assert built.status_code == 200, built.get_json()
    assert built.get_json()["result"]["matched"] == 2


def test_build_ignores_match_from_another_normalization_revision(sync_api) -> None:
    client, projects = sync_api
    _write_confirmed_normalization(projects, revision=8)
    assert _post_source(client).status_code == 201
    match_path = projects / "demo" / "work" / "sync_matches.json"
    match_path.write_text(
        json.dumps(
            {
                "matches": [
                    {
                        "segment_id": "stable-sync-uid",
                        "query_text": "цитата",
                        "source_id": "vasilieva_01",
                        "source_in_sec": 1.0,
                        "source_out_sec": 3.0,
                        "confidence": 1.0,
                        "status": "confirmed",
                        "metadata": {"normalization_revision": 7},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    with patch.object(api, "generate_placeholder", return_value=None):
        built = client.post("/stage2/build", json={"project": "demo"})
    assert built.status_code == 200
    result = built.get_json()["result"]
    assert result["matched"] == 0
    assert result["unmatched"] == 2
    assert any("stable-sync-uid" in item for item in result["warnings"])

    listed = client.get("/stage2/sync-matches?project=demo").get_json()["result"]
    assert listed["matches"] == []
    assert listed["matched"] == 0


def test_job_can_force_selected_source_and_custom_search_window(sync_api) -> None:
    client, projects = sync_api
    _write_confirmed_normalization(projects)
    assert _post_source(client).status_code == 201
    assert _post_source(client, "other_01", "Другой герой").status_code == 201
    assert client.put(
        "/stage2/sync-sources/vasilieva_01/audio",
        data={
            "project": "demo",
            "audio": (BytesIO(_wav_bytes()), "guide.wav"),
        },
        content_type="multipart/form-data",
    ).status_code == 200
    assert client.put(
        "/stage2/sync-sources/vasilieva_01/words",
        json={
            "project": "demo",
            "words": [
                {"word": "старый", "start": 1.0, "end": 1.5},
                {"word": "индекс", "start": 1.5, "end": 2.0},
            ],
        },
    ).status_code == 200

    calls: list[str] = []

    def fake_transcribe(path, **_kwargs):
        calls.append(Path(path).name)
        return {
            "model": "mock",
            "language": "ru",
            "words": [
                {"word": "маленькая", "start": 10.0, "end": 10.2},
                {"word": "женщина", "start": 10.2, "end": 10.4},
                {"word": "покоряла", "start": 10.4, "end": 10.6},
                {"word": "великих", "start": 10.6, "end": 10.8},
                {"word": "людей", "start": 10.8, "end": 11.0},
            ],
        }

    with (
        patch.object(api, "transcribe_audio", side_effect=fake_transcribe),
        patch.object(api.threading, "Thread", _ImmediateThread),
        patch.object(api, "MatcherConfig", wraps=api.MatcherConfig) as config,
    ):
        response = client.post(
            "/stage2/sync-jobs/start",
            json={
                "project": "demo",
                "source_ids": ["vasilieva_01"],
                "force_transcribe": True,
                "search_window_sec": 42,
            },
        )
    assert response.status_code == 200, response.get_json()
    assert calls == ["vasilieva_01.wav"]
    config.assert_called_once_with(search_window_sec=42.0)
    job = client.get(
        response.get_json()["result"]["status_url"]
    ).get_json()["result"]
    assert job["transcribed"] == 1
    assert job["reused_indexes"] == 0


def test_selected_cep_export_still_matches_against_all_ready_indexes(sync_api) -> None:
    client, projects = sync_api
    _write_confirmed_normalization(projects)
    assert _post_source(client).status_code == 201
    assert _post_source(client, "other_01", "Татьяна Васильева").status_code == 201
    for source_id in ("vasilieva_01", "other_01"):
        assert client.put(
            f"/stage2/sync-sources/{source_id}/audio",
            data={
                "project": "demo",
                "audio": (BytesIO(_wav_bytes(15.0)), "guide.wav"),
            },
            content_type="multipart/form-data",
        ).status_code == 200
    assert client.put(
        "/stage2/sync-sources/other_01/words",
        json={
            "project": "demo",
            "words": [
                {"word": "маленькая", "start": 10.0, "end": 10.2},
                {"word": "женщина", "start": 10.2, "end": 10.4},
                {"word": "покоряла", "start": 10.4, "end": 10.6},
                {"word": "великих", "start": 10.6, "end": 10.8},
                {"word": "людей", "start": 10.8, "end": 11.0},
            ],
        },
    ).status_code == 200
    # Make the non-selected source stale. A scoped CEP job must still reuse its
    # last good index instead of unexpectedly transcribing it.
    other_audio = projects / "demo" / "work" / "sync_audio" / "other_01.wav"
    other_audio.write_bytes(other_audio.read_bytes() + b"\0\0")

    transcribed_paths: list[str] = []

    def fake_transcribe(path, **_kwargs):
        transcribed_paths.append(Path(path).name)
        return {
            "model": "mock",
            "language": "ru",
            "words": [
                {"word": "совсем", "start": 1.0, "end": 1.2},
                {"word": "другой", "start": 1.2, "end": 1.4},
                {"word": "текст", "start": 1.4, "end": 1.6},
            ],
        }

    with (
        patch.object(api, "transcribe_audio", side_effect=fake_transcribe),
        patch.object(api.threading, "Thread", _ImmediateThread),
    ):
        response = client.post(
            "/stage2/sync-jobs/start",
            json={
                "project": "demo",
                "source_ids": ["vasilieva_01"],
                "force_transcribe": True,
            },
        )

    assert response.status_code == 200, response.get_json()
    assert transcribed_paths == ["vasilieva_01.wav"]
    matches = client.get("/stage2/sync-matches?project=demo").get_json()["result"]
    found = next(
        item for item in matches["matches"]
        if item["block_uid"] == "stable-sync-uid"
    )
    assert found["source_id"] == "other_01"


def test_failed_forced_transcription_preserves_previous_word_index(sync_api) -> None:
    client, projects = sync_api
    _write_confirmed_normalization(projects)
    assert _post_source(client).status_code == 201
    assert client.put(
        "/stage2/sync-sources/vasilieva_01/audio",
        data={
            "project": "demo",
            "audio": (BytesIO(_wav_bytes()), "guide.wav"),
        },
        content_type="multipart/form-data",
    ).status_code == 200
    assert client.put(
        "/stage2/sync-sources/vasilieva_01/words",
        json={
            "project": "demo",
            "words": [
                {"word": "сохранить", "start": 1.0, "end": 1.4},
                {"word": "старый", "start": 1.4, "end": 1.8},
                {"word": "индекс", "start": 1.8, "end": 2.2},
            ],
        },
    ).status_code == 200
    index_path = (
        projects / "demo" / "work" / "transcripts" / "vasilieva_01.words.json"
    )
    previous = index_path.read_bytes()

    with (
        patch.object(api, "transcribe_audio", side_effect=RuntimeError("backend failed")),
        patch.object(api.threading, "Thread", _ImmediateThread),
    ):
        response = client.post(
            "/stage2/sync-jobs/start",
            json={
                "project": "demo",
                "source_ids": ["vasilieva_01"],
                "force_transcribe": True,
            },
        )

    assert response.status_code == 200, response.get_json()
    job = client.get(response.get_json()["result"]["status_url"]).get_json()["result"]
    assert job["status"] == "completed"
    assert job["transcribed"] == 0
    assert job["source_failures"] == [
        {"source_id": "vasilieva_01", "error": "backend failed"}
    ]
    assert index_path.read_bytes() == previous


def test_audio_change_during_transcription_preserves_previous_index(sync_api) -> None:
    client, projects = sync_api
    _write_confirmed_normalization(projects)
    assert _post_source(client).status_code == 201
    assert client.put(
        "/stage2/sync-sources/vasilieva_01/audio",
        data={
            "project": "demo",
            "audio": (BytesIO(_wav_bytes()), "guide.wav"),
        },
        content_type="multipart/form-data",
    ).status_code == 200
    assert client.put(
        "/stage2/sync-sources/vasilieva_01/words",
        json={
            "project": "demo",
            "words": [{"word": "старый", "start": 1.0, "end": 1.5}],
        },
    ).status_code == 200
    index_path = (
        projects / "demo" / "work" / "transcripts" / "vasilieva_01.words.json"
    )
    previous = index_path.read_bytes()

    def mutate_audio(path, **_kwargs):
        audio_path = Path(path)
        audio_path.write_bytes(audio_path.read_bytes() + b"\0\0")
        return {
            "model": "mock",
            "language": "ru",
            "words": [{"word": "новый", "start": 1.0, "end": 1.5}],
        }

    with (
        patch.object(api, "transcribe_audio", side_effect=mutate_audio),
        patch.object(api.threading, "Thread", _ImmediateThread),
    ):
        response = client.post(
            "/stage2/sync-jobs/start",
            json={
                "project": "demo",
                "source_ids": ["vasilieva_01"],
                "force_transcribe": True,
            },
        )

    job = client.get(response.get_json()["result"]["status_url"]).get_json()["result"]
    assert "changed during transcription" in job["source_failures"][0]["error"]
    assert index_path.read_bytes() == previous


def test_word_index_cache_invalidates_on_config_and_audio_changes(sync_api) -> None:
    client, projects = sync_api
    _write_confirmed_normalization(projects)
    assert _post_source(client).status_code == 201
    assert client.put(
        "/stage2/sync-sources/vasilieva_01/audio",
        data={
            "project": "demo",
            "audio": (BytesIO(_wav_bytes()), "guide.wav"),
        },
        content_type="multipart/form-data",
    ).status_code == 200
    calls: list[Path] = []

    def fake_transcribe(path, **_kwargs):
        calls.append(Path(path))
        return {
            "backend": "faster-whisper",
            "model": "medium",
            "language": "ru",
            "words": [
                {"word": "маленькая", "start": 10.0, "end": 10.2},
                {"word": "женщина", "start": 10.2, "end": 10.4},
                {"word": "покоряла", "start": 10.4, "end": 10.6},
                {"word": "великих", "start": 10.6, "end": 10.8},
                {"word": "людей", "start": 10.8, "end": 11.0},
            ],
        }

    def start_job() -> dict:
        response = client.post("/stage2/sync-jobs/start", json={"project": "demo"})
        assert response.status_code == 200, response.get_json()
        return client.get(
            response.get_json()["result"]["status_url"]
        ).get_json()["result"]

    environment = {
        "SYNC_WHISPER_BACKEND": "faster-whisper",
        "SYNC_WHISPER_MODEL": "medium",
        "SYNC_WHISPER_LANGUAGE": "ru",
        "SYNC_WHISPER_BEAM_SIZE": "5",
        "SYNC_WHISPER_BEST_OF": "5",
        "SYNC_WHISPER_BATCH_SIZE": "0",
        "SYNC_WHISPER_DEVICE": "cpu",
        "SYNC_WHISPER_COMPUTE_TYPE": "int8",
    }
    with (
        patch.dict(api.os.environ, environment),
        patch.object(api, "transcribe_audio", side_effect=fake_transcribe),
        patch.object(api.threading, "Thread", _ImmediateThread),
    ):
        first = start_job()
        second = start_job()
        assert first["transcribed"] == 1
        assert second["transcribed"] == 0
        assert second["reused_indexes"] == 1
        assert len(calls) == 1

        api.os.environ["SYNC_WHISPER_BEAM_SIZE"] = "1"
        changed_config = start_job()
        assert changed_config["transcribed"] == 1
        assert len(calls) == 2

        audio_path = calls[-1]
        audio_path.write_bytes(audio_path.read_bytes() + b"\0\0")
        changed_audio = start_job()
        assert changed_audio["transcribed"] == 1
        assert len(calls) == 3


def test_legacy_word_index_is_migrated_without_retranscription(sync_api) -> None:
    client, projects = sync_api
    _write_confirmed_normalization(projects)
    assert _post_source(client).status_code == 201
    assert client.put(
        "/stage2/sync-sources/vasilieva_01/audio",
        data={
            "project": "demo",
            "audio": (BytesIO(_wav_bytes()), "guide.wav"),
        },
        content_type="multipart/form-data",
    ).status_code == 200
    index_path = (
        projects / "demo" / "work" / "transcripts" / "vasilieva_01.words.json"
    )
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text(
        json.dumps(
            {
                "source_id": "vasilieva_01",
                "language": "ru",
                "model": "legacy-medium",
                "metadata": {},
                "words": [
                    {"word": "ручная", "start": 1.0, "end": 1.4},
                    {"word": "правка", "start": 1.4, "end": 1.8},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    with (
        patch.object(
            api,
            "transcribe_audio",
            side_effect=AssertionError("legacy index must be reused"),
        ),
        patch.object(api.threading, "Thread", _ImmediateThread),
    ):
        response = client.post("/stage2/sync-jobs/start", json={"project": "demo"})

    job = client.get(response.get_json()["result"]["status_url"]).get_json()["result"]
    migrated = json.loads(index_path.read_text(encoding="utf-8"))
    assert job["transcribed"] == 0
    assert job["reused_indexes"] == 1
    assert migrated["words"][0]["word"] == "ручная"
    assert migrated["metadata"]["transcription_cache"]["origin"] == "legacy"


def test_failed_config_refresh_uses_same_audio_index_as_fallback(sync_api) -> None:
    client, projects = sync_api
    _write_confirmed_normalization(projects)
    assert _post_source(client).status_code == 201
    assert client.put(
        "/stage2/sync-sources/vasilieva_01/audio",
        data={
            "project": "demo",
            "audio": (BytesIO(_wav_bytes(15.0)), "guide.wav"),
        },
        content_type="multipart/form-data",
    ).status_code == 200

    transcript = {
        "backend": "faster-whisper",
        "model": "medium",
        "language": "ru",
        "words": [
            {"word": "маленькая", "start": 10.0, "end": 10.2},
            {"word": "женщина", "start": 10.2, "end": 10.4},
            {"word": "покоряла", "start": 10.4, "end": 10.6},
            {"word": "великих", "start": 10.6, "end": 10.8},
            {"word": "людей", "start": 10.8, "end": 11.0},
        ],
    }
    with (
        patch.object(api, "transcribe_audio", return_value=transcript),
        patch.object(api.threading, "Thread", _ImmediateThread),
    ):
        first = client.post("/stage2/sync-jobs/start", json={"project": "demo"})
    assert first.status_code == 200

    with (
        patch.dict(api.os.environ, {"SYNC_WHISPER_BEAM_SIZE": "1"}),
        patch.object(
            api, "transcribe_audio", side_effect=RuntimeError("temporary failure")
        ),
        patch.object(api.threading, "Thread", _ImmediateThread),
    ):
        second = client.post("/stage2/sync-jobs/start", json={"project": "demo"})

    job = client.get(second.get_json()["result"]["status_url"]).get_json()["result"]
    matches = client.get("/stage2/sync-matches?project=demo").get_json()["result"]
    found = next(
        item for item in matches["matches"]
        if item["block_uid"] == "stable-sync-uid"
    )
    assert job["transcribed"] == 0
    assert job["reused_indexes"] == 1
    assert job["source_failures"][0]["error"] == "temporary failure"
    assert found["source_id"] == "vasilieva_01"
