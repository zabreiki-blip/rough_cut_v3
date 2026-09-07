from __future__ import annotations

from io import BytesIO
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import time
from unittest.mock import patch
import wave

import pytest

import api
from stage2.sync_llm_review import SemanticDecision
from stage2.sync_matcher import SyncMatcher
from stage2.sync_models import SyncAlternative, SyncMatch, SyncSource, WordIndex, WordTimestamp


class _ImmediateThread:
    def __init__(self, *, target, kwargs, **_unused):
        self.target = target
        self.kwargs = kwargs

    def start(self) -> None:
        self.target(**self.kwargs)


def test_matching_fingerprint_is_stable_and_tracks_word_content() -> None:
    source = SyncSource(id="source", speaker="A", premiere_sequence="Sequence")
    units = [
        {
            "segment_id": "block-1",
            "parent_block_uid": "block-1",
            "text": "точная цитата",
            "speaker": "A",
            "timecode_start": "00:10",
        }
    ]
    index = WordIndex(
        source_id="source",
        words=[WordTimestamp("цитата", 10.0, 10.5)],
        generated_at="stable",
    )
    arguments = {
        "units": units,
        "sources": [source],
        "indexes": [index],
        "normalization_revision": 3,
        "timecode_mode": "relative",
        "search_window_sec": 180.0,
        "llm_enabled": True,
        "llm_model": "model",
        "llm_threshold": 0.8,
        "llm_limit": 30,
    }
    first = api._sync_matching_fingerprint(**arguments)
    assert first == api._sync_matching_fingerprint(**arguments)

    changed = dict(arguments)
    changed["indexes"] = [
        WordIndex(
            source_id="source",
            words=[WordTimestamp("другая", 10.0, 10.5)],
            generated_at="stable",
        )
    ]
    assert api._sync_matching_fingerprint(**changed) != first

    changed = dict(arguments, scenario_revision_id="rev_0002")
    assert api._sync_matching_fingerprint(**changed) != first


@pytest.mark.parametrize(
    ("cue", "role"),
    [
        ("СНХ", "Киркоров"),
        ("ЛАЙФ", "Киркоров"),
        ("СНХ", "Киркоров: начало реплики"),
    ],
)
def test_editorial_cue_role_selects_the_named_interview_source(cue: str, role: str) -> None:
    quote = "точная реплика из интервью"
    unit = api._sync_alignment_units(
        [
            {
                "uid": "block-1",
                "type": "sync",
                "speaker": cue,
                "role": role,
                "normalized_text": f"{cue}, Киркоров: {quote}",
            }
        ],
        source_speakers={("киркор",)},
    )[0]

    assert unit["speaker"] == "Киркоров"
    result = SyncMatcher(
        [SyncSource(id="kirkorov", speaker="Киркоров", premiere_sequence="Киркоров")],
        [
            WordIndex(
                source_id="kirkorov",
                words=[
                    WordTimestamp(word, index, index + 0.5)
                    for index, word in enumerate(quote.split())
                ],
            )
        ],
    ).match(
        api.AlignmentRequest(
            segment_id=str(unit["segment_id"]),
            text=str(unit["text"]),
            speaker=str(unit["speaker"]),
        )
    )

    assert result.source_id == "kirkorov"
    assert unit["text"] == quote


def test_regular_speaker_role_does_not_replace_the_person_name() -> None:
    unit = api._sync_alignment_units(
        [
            {
                "uid": "block-1",
                "type": "sync",
                "speaker": "Киркоров",
                "role": "певец",
                "normalized_text": "Киркоров, певец: точная реплика",
            }
        ]
    )[0]

    assert unit["speaker"] == "Киркоров"


def test_unknown_editorial_role_keeps_the_search_unrestricted() -> None:
    unit = api._sync_alignment_units(
        [
            {
                "uid": "block-1",
                "type": "sync",
                "speaker": "СНХ",
                "role": "архив",
                "normalized_text": "СНХ, архив: точная реплика",
            }
        ],
        source_speakers={("киркор",)},
    )[0]

    assert unit["speaker"] == ""


def test_audio_match_accepts_precomputed_signature_without_rereading(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    audio_path = tmp_path / "guide.wav"
    audio_path.write_bytes(b"audio")
    signature = {"size": 5, "mtime_ns": 1, "sample_sha256": "stable"}
    index = WordIndex(
        source_id="source",
        words=[WordTimestamp("слово", 0.0, 0.5)],
        metadata={
            api._TRANSCRIPTION_CACHE_KEY: {
                "version": 1,
                "audio": dict(signature),
            }
        },
    )
    monkeypatch.setattr(
        api,
        "_audio_cache_signature",
        lambda _path: (_ for _ in ()).throw(AssertionError("unexpected reread")),
    )

    assert api._word_index_audio_matches(
        index,
        audio_path,
        audio_signature=signature,
    )


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
                api._SYNC_CANCEL_EVENTS.clear()
            yield api.app.test_client(), projects


def _write_confirmed_normalization(
    projects: Path,
    revision: int = 7,
    scenario_revision_id: str = "",
) -> Path:
    path = projects / "demo" / "work" / "scenario_normalized.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "source": "",
                "source_hash": "",
                "status": "confirmed",
                "revision": revision,
                "scenario_revision_id": scenario_revision_id,
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
        json.dumps(
            {
                "source": "",
                "source_hash": "",
                "status": "confirmed",
                "revision": 3,
                "blocks": [
                    {
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
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def _write_shared_timeline_normalization(projects: Path) -> Path:
    path = projects / "demo" / "work" / "scenario_normalized.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
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
            },
            ensure_ascii=False,
        ),
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


def test_stale_upload_cleanup_preserves_recent_parts(tmp_path: Path) -> None:
    stale = tmp_path / ".stale.wav.dead.upload"
    recent = tmp_path / ".recent.wav.live.upload"
    stale.write_bytes(b"old")
    recent.write_bytes(b"new")
    old = time.time() - 48 * 60 * 60
    os.utime(stale, (old, old))

    removed = api._cleanup_stale_uploads(tmp_path)

    assert removed == 1
    assert not stale.exists()
    assert recent.exists()


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
    assert [item["id"] for item in listing.get_json()["result"]["sources"]] == ["vasilieva_01"]
    streamed = client.get("/stage2/sync-sources/vasilieva_01/audio?project=demo")
    assert streamed.status_code == 200
    assert streamed.mimetype in {"audio/x-wav", "audio/wav"}

    deleted = client.delete(
        "/stage2/sync-sources",
        json={"project": "demo", "source_id": "vasilieva_01"},
    )
    assert deleted.status_code == 200
    assert not fixed.exists()


def test_source_and_transcript_mutations_are_blocked_while_job_is_active(sync_api) -> None:
    client, _projects = sync_api
    assert _post_source(client).status_code == 201
    with api._SYNC_JOBS_LOCK:
        api._SYNC_ACTIVE_PROJECTS.add("demo")
    try:
        assert client.get("/stage2/sync-sources?project=demo").status_code == 200
        assert (
            client.post(
                "/stage2/sync-sources",
                json={
                    "project": "demo",
                    "id": "another",
                    "premiere_sequence": "SYNC_ANOTHER",
                },
            ).status_code
            == 409
        )
        assert (
            client.delete(
                "/stage2/sync-sources",
                json={
                    "project": "demo",
                    "source_id": "vasilieva_01",
                },
            ).status_code
            == 409
        )
        assert (
            client.put(
                "/stage2/sync-sources/vasilieva_01/audio",
                data={
                    "project": "demo",
                    "audio": (BytesIO(_wav_bytes()), "guide.wav"),
                },
                content_type="multipart/form-data",
            ).status_code
            == 409
        )
        assert (
            client.put(
                "/stage2/sync-sources/vasilieva_01/words",
                json={"project": "demo", "words": []},
            ).status_code
            == 409
        )
    finally:
        with api._SYNC_JOBS_LOCK:
            api._SYNC_ACTIVE_PROJECTS.discard("demo")


def test_review_media_registration_stores_opaque_path_without_reading_it(sync_api) -> None:
    client, projects = sync_api
    media_path = "/Volumes/L_BRICK/DOP/Брик_просмотровки/missing-review.mp4"

    created = client.post(
        "/stage2/sync-sources",
        json={
            "project": "demo",
            "id": "brick_review_01",
            "source_kind": "review_media",
            "media_path": media_path,
            "media_fingerprint": "size:1234:mtime:5678",
            "proxy_sequence_name": "RC_SRC_BRICK_REVIEW_01",
        },
    )

    assert created.status_code == 201, created.get_json()
    source = created.get_json()["result"]["source"]
    assert source["source_kind"] == "review_media"
    assert source["media_path"] == media_path
    assert source["sequence_name"] == "RC_SRC_BRICK_REVIEW_01"
    # Registration only persists client metadata.  In particular, it neither
    # requires the path to exist nor copies it into the project workspace.
    assert not (projects / "demo" / "work" / "missing-review.mp4").exists()

    updated = client.post(
        "/stage2/sync-sources",
        json={
            "project": "demo",
            "id": "brick_review_01",
            "proxy_sequence_id": "premiere-proxy-01",
        },
    )
    assert updated.status_code == 201, updated.get_json()
    updated_source = updated.get_json()["result"]["source"]
    assert updated_source["proxy_sequence_id"] == "premiere-proxy-01"
    assert updated_source["media_path"] == media_path


def test_review_media_registration_validates_kind_and_required_path(sync_api) -> None:
    client, _projects = sync_api
    missing_path = client.post(
        "/stage2/sync-sources",
        json={
            "project": "demo",
            "id": "review_missing_path",
            "source_kind": "review_media",
            "proxy_sequence_name": "RC_SRC_MISSING",
        },
    )
    assert missing_path.status_code == 400
    assert "media_path" in missing_path.get_json()["error"]

    unknown_kind = client.post(
        "/stage2/sync-sources",
        json={
            "project": "demo",
            "id": "unknown_kind",
            "source_kind": "external_url",
            "premiere_sequence": "SYNC_UNKNOWN",
        },
    )
    assert unknown_kind.status_code == 400
    assert "source kind" in unknown_kind.get_json()["error"]


def test_source_reregistration_preserves_guide_and_rejects_id_rebinding(sync_api) -> None:
    client, _projects = sync_api
    assert _post_source(client).status_code == 201
    assert (
        client.put(
            "/stage2/sync-sources/vasilieva_01/audio",
            data={
                "project": "demo",
                "audio": (BytesIO(_wav_bytes()), "guide.wav"),
            },
            content_type="multipart/form-data",
        ).status_code
        == 200
    )

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


def test_latest_sync_job_status_survives_in_memory_registry_reset(sync_api) -> None:
    client, projects = sync_api
    created = api._create_sync_job("demo")
    completed = api._update_sync_job(
        created["id"],
        status="completed",
        message="Completed",
        done=12,
        total=12,
        finished_at="2026-07-30T20:00:00+00:00",
    )
    assert completed is not None

    status_path = projects / "demo" / "work" / "sync_job_status.json"
    persisted = json.loads(status_path.read_text(encoding="utf-8"))
    assert persisted["id"] == created["id"]
    assert persisted["status"] == "completed"
    assert persisted["done"] == 12

    with api._SYNC_JOBS_LOCK:
        api._SYNC_JOBS.clear()

    response = client.get("/stage2/sync-jobs/status?project=demo")
    assert response.status_code == 200
    restored = response.get_json()["result"]
    assert restored["id"] == created["id"]
    assert restored["status"] == "completed"
    assert restored["done"] == 12
    assert client.get(f"/stage2/sync-jobs/{created['id']}").status_code == 404


def test_trim_sync_jobs_keeps_active_and_expires_terminal(sync_api) -> None:
    _client, _projects = sync_api
    old = "2020-01-01T00:00:00+00:00"
    with api._SYNC_JOBS_LOCK:
        api._SYNC_JOBS.update(
            {
                "queued": {"status": "queued", "created_at": old},
                "running": {"status": "running", "created_at": old},
                "done": {
                    "status": "completed",
                    "created_at": old,
                    "finished_at": old,
                },
            }
        )
        api._SYNC_CANCEL_EVENTS["done"] = api.threading.Event()
        api._trim_sync_jobs_locked()

    assert "queued" in api._SYNC_JOBS
    assert "running" in api._SYNC_JOBS
    assert "done" not in api._SYNC_JOBS
    assert "done" not in api._SYNC_CANCEL_EVENTS


def test_non_durable_sync_update_changes_memory_without_fsync(sync_api) -> None:
    _client, _projects = sync_api
    created = api._create_sync_job("demo")
    with patch.object(api, "_persist_sync_job") as persist:
        updated = api._update_sync_job(
            created["id"],
            persist=False,
            done=3,
        )

    assert updated is not None
    assert updated["done"] == 3
    assert api._get_sync_job(created["id"])["done"] == 3
    persist.assert_not_called()


def test_terminal_job_status_uses_live_match_summary_after_browser_review(
    sync_api,
) -> None:
    client, projects = sync_api
    match_path = projects / "demo" / "work" / "sync_matches.json"
    store = api.SyncMatchStore(match_path)
    pending = api.SyncMatch(
        segment_id="stable-sync-uid",
        query_text="Реплика для проверки",
        source_id="source-1",
        source_in_sec=10.0,
        source_out_sec=14.0,
        confidence=0.7,
        status="needs_review",
    )
    store.save(pending)
    created = api._create_sync_job("demo")
    api._update_sync_job(
        created["id"],
        status="completed",
        needs_review=1,
        confirmed=0,
    )

    before = client.get(f"/stage2/sync-jobs/{created['id']}").get_json()["result"]
    assert before["needs_review"] == 1

    pending.status = "confirmed"
    store.save(pending)

    by_id = client.get(f"/stage2/sync-jobs/{created['id']}").get_json()["result"]
    by_project = client.get("/stage2/sync-jobs/status?project=demo").get_json()["result"]
    assert by_id["needs_review"] == 0
    assert by_id["confirmed"] == 1
    assert by_project["needs_review"] == 0
    assert by_project["confirmed"] == 1


def test_corrupt_persisted_sync_job_status_is_ignored(sync_api) -> None:
    client, projects = sync_api
    status_path = projects / "demo" / "work" / "sync_job_status.json"
    status_path.parent.mkdir(parents=True, exist_ok=True)
    status_path.write_text("{not valid json", encoding="utf-8")

    response = client.get("/stage2/sync-jobs/status?project=demo")
    assert response.status_code == 404
    assert response.get_json()["error"] == "job not found"


def test_semantic_review_selects_only_a_real_candidate_and_stays_manual():
    original = SyncMatch(
        segment_id="block-1",
        query_text="длинная цитата сценария для смысловой проверки",
        speaker="Спикер",
        source_id="source-a",
        source_in_sec=10.0,
        source_out_sec=15.0,
        confidence=0.62,
        status="needs_review",
        matched_text="первый вариант",
        strategy="full_transcript",
        alternatives=[
            SyncAlternative("source-a", 30.0, 37.0, 0.57, "второй вариант"),
        ],
    )
    candidates = api._semantic_review_candidates(original)
    reviewed = api._apply_semantic_review(
        original,
        candidates,
        1,
        llm_confidence=0.93,
        reason="совпадает смысл",
        model="test/model",
    )
    assert reviewed.status == "needs_review"
    assert reviewed.strategy == "llm_semantic_review"
    assert reviewed.source_in_sec == 30.0
    assert reviewed.source_out_sec == 37.0
    assert reviewed.confidence == 0.57
    assert reviewed.metadata["llm_semantic_review"] == {
        "selected_candidate": 1,
        "confidence": 0.93,
        "reason": "совпадает смысл",
        "model": "test/model",
        "original_strategy": "full_transcript",
        "original_confidence": 0.62,
    }


def test_background_job_uses_llm_for_low_confidence_blocks_without_auto_confirming(
    sync_api,
) -> None:
    client, projects = sync_api
    _write_confirmed_normalization(projects)
    assert _post_source(client).status_code == 201
    assert (
        client.put(
            "/stage2/sync-sources/vasilieva_01/words",
            json={
                "project": "demo",
                "words": [
                    {"word": "тестовая", "start": 10.0, "end": 10.4},
                    {"word": "расшифровка", "start": 10.4, "end": 10.8},
                ],
            },
        ).status_code
        == 200
    )

    settings = api.get_settings()
    settings.openrouter_api_key = "test-key"

    def low_match(_matcher, request):
        return SyncMatch(
            segment_id=request.segment_id,
            query_text=request.text,
            speaker=request.speaker,
            source_id="vasilieva_01",
            source_in_sec=10.0,
            source_out_sec=15.0,
            confidence=0.63,
            status="needs_review",
            matched_text="первый буквальный кандидат",
            strategy="full_transcript",
            alternatives=[
                SyncAlternative(
                    "vasilieva_01",
                    20.0,
                    26.0,
                    0.55,
                    "второй смысловой кандидат",
                ),
            ],
        )

    with (
        patch.object(api, "get_settings", return_value=settings),
        patch.object(api.SyncMatcher, "match", low_match),
        patch.object(
            api,
            "review_candidates_with_llm",
            return_value=SemanticDecision(1, 0.92, "тот же смысл", "test/model"),
        ) as llm_review,
        patch.object(api.threading, "Thread", _ImmediateThread),
    ):
        response = client.post("/stage2/sync-jobs/start", json={"project": "demo"})

    assert response.status_code == 200, response.get_json()
    job = client.get(response.get_json()["result"]["status_url"]).get_json()["result"]
    assert job["status"] == "completed"
    assert job["llm_reviewed"] == 1
    assert job["needs_review"] == 2
    assert job["auto_matched"] == 0
    assert llm_review.call_count == 1

    matches = client.get("/stage2/sync-matches?project=demo").get_json()["result"]["matches"]
    assert len(matches) == 2
    assert all(item["status"] == "needs_review" for item in matches)
    reviewed = next(item for item in matches if item["strategy"] == "llm_semantic_review")
    assert reviewed["source_in_sec"] == 20.0
    assert reviewed["source_out_sec"] == 26.0


def test_persisted_running_sync_job_is_reported_as_interrupted_checkpoint(sync_api) -> None:
    client, projects = sync_api
    created = api._create_sync_job("demo")
    running = api._update_sync_job(
        created["id"],
        status="running",
        message="Matching scenario quotes",
        done=7,
        total=12,
        current_segment_id="stable-sync-uid",
    )
    assert running is not None

    with api._SYNC_JOBS_LOCK:
        api._SYNC_JOBS.clear()

    response = client.get("/stage2/sync-jobs/status?project=demo")
    assert response.status_code == 200
    restored = response.get_json()["result"]
    assert restored["id"] == created["id"]
    assert restored["status"] == "interrupted"
    assert restored["done"] == 7
    assert restored["total"] == 12
    assert restored["current_segment_id"] == ""
    assert "saved progress" in restored["message"]

    build = client.post("/stage2/build", json={"project": "demo"})
    assert build.status_code == 409
    assert build.get_json()["error"].startswith("sync_job_interrupted:")


def test_persisted_cancelling_sync_job_is_reported_as_interrupted_checkpoint(
    sync_api,
) -> None:
    client, _projects = sync_api
    created = api._create_sync_job("demo")
    updated = api._update_sync_job(
        created["id"],
        status="cancelling",
        message="Cancellation requested; saving completed work",
        done=3,
        total=8,
    )
    assert updated is not None

    with api._SYNC_JOBS_LOCK:
        api._SYNC_JOBS.clear()
        api._SYNC_ACTIVE_PROJECTS.clear()
        api._SYNC_CANCEL_EVENTS.clear()

    response = client.get("/stage2/sync-jobs/status?project=demo")
    assert response.status_code == 200
    restored = response.get_json()["result"]
    assert restored["id"] == created["id"]
    assert restored["status"] == "interrupted"
    assert restored["done"] == 3
    assert restored["total"] == 8


def test_sync_cancel_endpoint_sets_durable_cancelling_checkpoint(sync_api) -> None:
    client, projects = sync_api
    created = api._create_sync_job("demo")

    response = client.post(f"/stage2/sync-jobs/{created['id']}/cancel")

    assert response.status_code == 202
    result = response.get_json()["result"]
    assert result["status"] == "cancelling"
    with api._SYNC_JOBS_LOCK:
        assert api._SYNC_CANCEL_EVENTS[created["id"]].is_set()

    persisted = json.loads(
        (projects / "demo" / "work" / "sync_job_status.json").read_text(encoding="utf-8")
    )
    assert persisted["id"] == created["id"]
    assert persisted["status"] == "cancelling"


def test_cancelled_sync_checkpoint_blocks_partial_timeline_build(sync_api) -> None:
    client, _projects = sync_api
    created = api._create_sync_job("demo")
    updated = api._update_sync_job(
        created["id"],
        status="cancelled",
        message="Cancelled; saved indexes and matches can be resumed",
        finished_at=api._utc_now_iso(),
    )
    assert updated is not None

    response = client.post("/stage2/build", json={"project": "demo"})
    assert response.status_code == 409
    assert response.get_json()["error"].startswith("sync_job_interrupted:")


def test_cancelled_queued_sync_worker_releases_project_without_loading_inputs(
    sync_api,
) -> None:
    _client, _projects = sync_api
    assert api._reserve_sync_project("demo")
    created = api._create_sync_job("demo")
    with api._SYNC_JOBS_LOCK:
        api._SYNC_CANCEL_EVENTS[created["id"]].set()

    api._run_sync_job(
        job_id=created["id"],
        project="demo",
        backend=None,
        model=None,
        language="ru",
        timecode_mode="relative",
        rematch_confirmed=False,
        source_ids=(),
        source_scope="all",
        force_transcribe=False,
        search_window_sec=180.0,
    )

    state = api._get_sync_job(created["id"])
    assert state is not None
    assert state["status"] == "cancelled"
    with api._SYNC_JOBS_LOCK:
        assert "demo" not in api._SYNC_ACTIVE_PROJECTS
        assert created["id"] not in api._SYNC_CANCEL_EVENTS


def test_review_media_scope_excludes_legacy_sources_and_records_model(sync_api) -> None:
    client, projects = sync_api
    _write_confirmed_normalization(projects)
    assert _post_source(client, "legacy", "Татьяна Васильева").status_code == 201
    created = client.post(
        "/stage2/sync-sources",
        json={
            "project": "demo",
            "id": "review",
            "source_kind": "review_media",
            "media_path": "media/reviews/Васильева.mp4",
            "guide_audio": "media/reviews/Васильева.mp4",
            "premiere_sequence": "Васильева",
            "speaker": "Татьяна Васильева",
            "duration_sec": 100.0,
        },
    )
    assert created.status_code == 201, created.get_json()
    assert (
        client.put(
            "/stage2/sync-sources/review/words",
            json={
                "project": "demo",
                "words": [
                    {"word": "маленькая", "start": 10.0, "end": 10.5},
                    {"word": "женщина", "start": 10.5, "end": 11.0},
                    {"word": "покоряла", "start": 11.0, "end": 11.5},
                    {"word": "великих", "start": 11.5, "end": 12.0},
                    {"word": "людей", "start": 12.0, "end": 12.5},
                ],
            },
        ).status_code
        == 200
    )

    with patch.object(api.threading, "Thread", _ImmediateThread):
        response = client.post(
            "/stage2/sync-jobs/start",
            json={
                "project": "demo",
                "model": "small",
                "source_scope": "review_media",
            },
        )

    assert response.status_code == 200, response.get_json()
    job = client.get(response.get_json()["result"]["status_url"]).get_json()["result"]
    assert job["status"] == "completed"
    assert job["total_sources"] == 1
    assert job["reused_indexes"] == 1
    assert job["whisper_model"] == "small"
    assert job["source_scope"] == "review_media"


def test_sync_job_rejects_unknown_source_scope(sync_api) -> None:
    client, projects = sync_api
    _write_confirmed_normalization(projects)
    assert _post_source(client).status_code == 201

    response = client.post(
        "/stage2/sync-jobs/start",
        json={
            "project": "demo",
            "source_scope": "everything",
        },
    )

    assert response.status_code == 400
    assert "source_scope" in response.get_json()["error"]


def test_words_ingestion_match_patch_build_and_timeline(sync_api) -> None:
    client, projects = sync_api
    _write_confirmed_normalization(projects, scenario_revision_id="rev_0002")
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
    assert found["metadata"]["scenario_revision_id"] == "rev_0002"
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
    assert timeline.get_json()["result"]["scenario_revision_id"] == "rev_0002"
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


def test_build_writes_timeline_atomically_and_export_handles_corrupt_json(
    sync_api,
) -> None:
    client, projects = sync_api
    _write_confirmed_normalization(projects)
    written_paths: list[Path] = []
    original_atomic_write = api._atomic_write_json_file

    def track_atomic_write(path: Path, payload: dict) -> None:
        written_paths.append(path)
        original_atomic_write(path, payload)

    with (
        patch.object(api, "generate_placeholder", return_value=None),
        patch.object(api, "_atomic_write_json_file", side_effect=track_atomic_write),
    ):
        built = client.post("/stage2/build", json={"project": "demo"})

    assert built.status_code == 200, built.get_json()
    timeline_path = projects / "demo" / "work" / "full_timeline.json"
    assert timeline_path in written_paths

    timeline_path.write_text("{not valid json", encoding="utf-8")
    exported = client.get("/stage2/export/xml?project=demo")
    assert exported.status_code == 500
    assert "invalid full_timeline.json" in exported.get_json()["error"]


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
    assert (
        client.put(
            "/stage2/sync-sources/vasilieva_01/words",
            json={"project": "demo", "words": words},
        ).status_code
        == 200
    )

    original_match = api.SyncMatcher.match
    store = api.SyncMatchStore(projects / "demo" / "work" / "sync_matches.json")

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


def _seed_words(client) -> None:
    words = [
        {"word": "маленькая", "start": 10.0, "end": 10.5},
        {"word": "женщина", "start": 10.5, "end": 11.0},
        {"word": "покоряла", "start": 11.0, "end": 11.5},
        {"word": "великих", "start": 11.5, "end": 12.0},
        {"word": "людей", "start": 12.0, "end": 12.5},
    ]
    assert (
        client.put(
            "/stage2/sync-sources/vasilieva_01/words",
            json={"project": "demo", "words": words},
        ).status_code
        == 200
    )


def test_forced_rematch_replaces_a_stale_confirmed_match(sync_api) -> None:
    client, projects = sync_api
    _write_confirmed_normalization(projects)
    assert _post_source(client).status_code == 201
    _seed_words(client)

    store = api.SyncMatchStore(projects / "demo" / "work" / "sync_matches.json")
    stale_confirmed = api.SyncMatch(
        segment_id="stable-sync-uid",
        query_text="маленькая женщина покоряла великих людей",
        speaker="Татьяна Васильева",
        source_id="vasilieva_01",
        source_in_sec=900.0,
        source_out_sec=905.0,
        confidence=1.0,
        status="confirmed",
        matched_text="старый подтверждённый диапазон",
        metadata={
            # Predates the job by construction: a PATCH confirmation made long
            # before this rematch job was ever started.
            "manual_updated_at": "2000-01-01T00:00:00+00:00",
            "normalization_revision": 7,
        },
    )
    store.save(stale_confirmed)

    with patch.object(api.threading, "Thread", _ImmediateThread):
        response = client.post(
            "/stage2/sync-jobs/start",
            json={"project": "demo", "rematch_confirmed": True},
        )

    assert response.status_code == 200, response.get_json()
    updated = store.get("stable-sync-uid")
    assert updated is not None
    # A forced rematch must replace the old confirmed range with the freshly
    # computed one, not keep silently returning the stale confirmation.
    assert (updated.source_in_sec, updated.source_out_sec) != (900.0, 905.0)
    assert updated.metadata.get("manual_updated_at") != "2000-01-01T00:00:00+00:00"


def test_forced_rematch_still_defers_to_a_concurrent_manual_edit(sync_api) -> None:
    client, projects = sync_api
    _write_confirmed_normalization(projects)
    assert _post_source(client).status_code == 201
    _seed_words(client)

    original_match = api.SyncMatcher.match
    store = api.SyncMatchStore(projects / "demo" / "work" / "sync_matches.json")

    def confirm_while_matching(matcher, request):
        automatic = original_match(matcher, request)
        if request.segment_id == "stable-sync-uid":
            confirmed = api.SyncMatch.from_dict(automatic.to_dict())
            confirmed.status = "confirmed"
            confirmed.source_in_sec = 900.0
            confirmed.source_out_sec = 905.0
            confirmed.metadata["manual_updated_at"] = api._utc_now_iso()
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
    assert (preserved.source_in_sec, preserved.source_out_sec) == (900.0, 905.0)


def test_unforced_rematch_still_leaves_a_confirmed_match_untouched(sync_api) -> None:
    client, projects = sync_api
    _write_confirmed_normalization(projects)
    assert _post_source(client).status_code == 201
    _seed_words(client)

    store = api.SyncMatchStore(projects / "demo" / "work" / "sync_matches.json")
    stale_confirmed = api.SyncMatch(
        segment_id="stable-sync-uid",
        query_text="маленькая женщина покоряла великих людей",
        speaker="Татьяна Васильева",
        source_id="vasilieva_01",
        source_in_sec=900.0,
        source_out_sec=905.0,
        confidence=1.0,
        status="confirmed",
        matched_text="старый подтверждённый диапазон",
        metadata={
            "manual_updated_at": "2000-01-01T00:00:00+00:00",
            "normalization_revision": 7,
        },
    )
    store.save(stale_confirmed)

    with patch.object(api.threading, "Thread", _ImmediateThread):
        response = client.post(
            "/stage2/sync-jobs/start",
            json={"project": "demo", "rematch_confirmed": False},
        )

    assert response.status_code == 200, response.get_json()
    unchanged = store.get("stable-sync-uid")
    assert unchanged is not None
    assert unchanged.status == "confirmed"
    assert (unchanged.source_in_sec, unchanged.source_out_sec) == (900.0, 905.0)


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
    job = client.get(f"/stage2/sync-jobs/status?job_id={job_id}").get_json()["result"]
    assert job["status"] == "completed"
    assert job["transcribed"] == 1
    assert job["transcription_processed_sec"] == 5.0
    assert job["transcription_duration_sec"] == 10.0
    assert job["transcription_words"] == 3
    assert job["source_failures"][0]["source_id"] == "broken_01"
    assert client.get("/stage2/sync-matches?project=demo").get_json()["result"]["matched"] == 1


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
    assert (
        client.put(
            "/stage2/sync-sources/irina_01/words",
            json={"project": "demo", "words": words},
        ).status_code
        == 200
    )

    with patch.object(api.threading, "Thread", _ImmediateThread):
        response = client.post("/stage2/sync-jobs/start", json={"project": "demo"})
    assert response.status_code == 200, response.get_json()
    matches_result = client.get("/stage2/sync-matches?project=demo").get_json()["result"]
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
    segments = client.get("/stage2/timeline?project=demo").get_json()["result"]["segments"]
    assert [item["parent_block_uid"] for item in segments] == [
        "irina-block",
        "irina-block",
        "irina-block",
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
    assert (
        client.put(
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
        ).status_code
        == 200
    )

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


def test_scenario_revision_identity_flows_through_matches_and_timeline(
    sync_api,
) -> None:
    client, projects = sync_api
    _write_confirmed_normalization(projects, revision=7, scenario_revision_id="rev_0002")
    assert _post_source(client).status_code == 201

    patched = client.patch(
        "/stage2/sync-matches/stable-sync-uid",
        json={
            "project": "demo",
            "status": "confirmed",
            "source_id": "vasilieva_01",
            "source_in_sec": 1.0,
            "source_out_sec": 2.0,
        },
    )
    assert patched.status_code == 200
    assert (
        patched.get_json()["result"]["matches"][0]["metadata"]["scenario_revision_id"] == "rev_0002"
    )

    with patch.object(api, "generate_placeholder", return_value=None):
        built = client.post("/stage2/build", json={"project": "demo"})
    assert built.status_code == 200
    timeline = client.get("/stage2/timeline?project=demo")
    assert timeline.status_code == 200
    assert timeline.get_json()["result"]["scenario_revision_id"] == "rev_0002"


def test_explicit_other_scenario_revision_is_stale_but_legacy_match_is_accepted(
    sync_api,
) -> None:
    client, projects = sync_api
    _write_confirmed_normalization(projects, revision=7, scenario_revision_id="rev_0002")
    assert _post_source(client).status_code == 201
    match_path = projects / "demo" / "work" / "sync_matches.json"

    def write_match(scenario_revision_id: str | None) -> None:
        metadata = {"normalization_revision": 7}
        if scenario_revision_id is not None:
            metadata["scenario_revision_id"] = scenario_revision_id
        match_path.write_text(
            json.dumps(
                {
                    "matches": [
                        {
                            "segment_id": "stable-sync-uid",
                            "query_text": "цитата",
                            "source_id": "vasilieva_01",
                            "source_in_sec": 1.0,
                            "source_out_sec": 2.0,
                            "confidence": 1.0,
                            "status": "confirmed",
                            "metadata": metadata,
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

    write_match("rev_0001")
    assert client.get("/stage2/sync-matches?project=demo").get_json()["result"]["matches"] == []
    with patch.object(api, "generate_placeholder", return_value=None):
        stale_build = client.post("/stage2/build", json={"project": "demo"})
    assert stale_build.status_code == 200
    assert stale_build.get_json()["result"]["matched"] == 0

    write_match(None)
    listed = client.get("/stage2/sync-matches?project=demo").get_json()["result"]
    assert len(listed["matches"]) == 1
    with patch.object(api, "generate_placeholder", return_value=None):
        legacy_build = client.post("/stage2/build", json={"project": "demo"})
    assert legacy_build.status_code == 200
    assert legacy_build.get_json()["result"]["matched"] == 1


def test_timeline_rejects_explicit_other_scenario_revision(sync_api) -> None:
    client, projects = sync_api
    normalization_path = _write_confirmed_normalization(
        projects, revision=7, scenario_revision_id="rev_0002"
    )
    timeline_path = normalization_path.parent / "full_timeline.json"
    timeline_path.write_text(
        json.dumps(
            {
                "project": "demo",
                "fps": 25,
                "width": 1920,
                "height": 1080,
                "normalization_revision": 7,
                "scenario_revision_id": "rev_0001",
                "segments": [],
            }
        ),
        encoding="utf-8",
    )

    assert client.get("/stage2/timeline?project=demo").status_code == 409
    assert client.get("/stage2/export/xml?project=demo").status_code == 409

    # A timeline created before scenario IDs existed remains readable when its
    # numeric normalization revision is still current.
    legacy = json.loads(timeline_path.read_text(encoding="utf-8"))
    legacy.pop("scenario_revision_id")
    timeline_path.write_text(json.dumps(legacy), encoding="utf-8")
    assert client.get("/stage2/timeline?project=demo").status_code == 200


def test_job_can_force_selected_source_and_custom_search_window(sync_api) -> None:
    client, projects = sync_api
    _write_confirmed_normalization(projects)
    assert _post_source(client).status_code == 201
    assert _post_source(client, "other_01", "Другой герой").status_code == 201
    assert (
        client.put(
            "/stage2/sync-sources/vasilieva_01/audio",
            data={
                "project": "demo",
                "audio": (BytesIO(_wav_bytes()), "guide.wav"),
            },
            content_type="multipart/form-data",
        ).status_code
        == 200
    )
    assert (
        client.put(
            "/stage2/sync-sources/vasilieva_01/words",
            json={
                "project": "demo",
                "words": [
                    {"word": "старый", "start": 1.0, "end": 1.5},
                    {"word": "индекс", "start": 1.5, "end": 2.0},
                ],
            },
        ).status_code
        == 200
    )

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
    job = client.get(response.get_json()["result"]["status_url"]).get_json()["result"]
    assert job["transcribed"] == 1
    assert job["reused_indexes"] == 0


def test_selected_cep_export_still_matches_against_all_ready_indexes(sync_api) -> None:
    client, projects = sync_api
    _write_confirmed_normalization(projects)
    assert _post_source(client).status_code == 201
    assert _post_source(client, "other_01", "Татьяна Васильева").status_code == 201
    for source_id in ("vasilieva_01", "other_01"):
        assert (
            client.put(
                f"/stage2/sync-sources/{source_id}/audio",
                data={
                    "project": "demo",
                    "audio": (BytesIO(_wav_bytes(15.0)), "guide.wav"),
                },
                content_type="multipart/form-data",
            ).status_code
            == 200
        )
    assert (
        client.put(
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
        ).status_code
        == 200
    )
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
    found = next(item for item in matches["matches"] if item["block_uid"] == "stable-sync-uid")
    assert found["source_id"] == "other_01"


def test_failed_forced_transcription_preserves_previous_word_index(sync_api) -> None:
    client, projects = sync_api
    _write_confirmed_normalization(projects)
    assert _post_source(client).status_code == 201
    assert (
        client.put(
            "/stage2/sync-sources/vasilieva_01/audio",
            data={
                "project": "demo",
                "audio": (BytesIO(_wav_bytes()), "guide.wav"),
            },
            content_type="multipart/form-data",
        ).status_code
        == 200
    )
    assert (
        client.put(
            "/stage2/sync-sources/vasilieva_01/words",
            json={
                "project": "demo",
                "words": [
                    {"word": "сохранить", "start": 1.0, "end": 1.4},
                    {"word": "старый", "start": 1.4, "end": 1.8},
                    {"word": "индекс", "start": 1.8, "end": 2.2},
                ],
            },
        ).status_code
        == 200
    )
    index_path = projects / "demo" / "work" / "transcripts" / "vasilieva_01.words.json"
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
    assert job["source_failures"] == [{"source_id": "vasilieva_01", "error": "backend failed"}]
    assert index_path.read_bytes() == previous


def test_audio_change_during_transcription_preserves_previous_index(sync_api) -> None:
    client, projects = sync_api
    _write_confirmed_normalization(projects)
    assert _post_source(client).status_code == 201
    assert (
        client.put(
            "/stage2/sync-sources/vasilieva_01/audio",
            data={
                "project": "demo",
                "audio": (BytesIO(_wav_bytes()), "guide.wav"),
            },
            content_type="multipart/form-data",
        ).status_code
        == 200
    )
    assert (
        client.put(
            "/stage2/sync-sources/vasilieva_01/words",
            json={
                "project": "demo",
                "words": [{"word": "старый", "start": 1.0, "end": 1.5}],
            },
        ).status_code
        == 200
    )
    index_path = projects / "demo" / "work" / "transcripts" / "vasilieva_01.words.json"
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


def test_word_index_cache_survives_config_change_but_invalidates_on_audio_change(sync_api) -> None:
    client, projects = sync_api
    _write_confirmed_normalization(projects)
    assert _post_source(client).status_code == 201
    assert (
        client.put(
            "/stage2/sync-sources/vasilieva_01/audio",
            data={
                "project": "demo",
                "audio": (BytesIO(_wav_bytes()), "guide.wav"),
            },
            content_type="multipart/form-data",
        ).status_code
        == 200
    )
    calls: list[Path] = []
    match_calls: list[str] = []
    original_match = api.SyncMatcher.match

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

    def tracked_match(matcher, alignment):
        match_calls.append(alignment.segment_id)
        return original_match(matcher, alignment)

    def start_job() -> dict:
        response = client.post("/stage2/sync-jobs/start", json={"project": "demo"})
        assert response.status_code == 200, response.get_json()
        return client.get(response.get_json()["result"]["status_url"]).get_json()["result"]

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
        patch.object(api.SyncMatcher, "match", new=tracked_match),
        patch.object(api.threading, "Thread", _ImmediateThread),
    ):
        first = start_job()
        first_match_count = len(match_calls)
        second = start_job()
        assert first["transcribed"] == 1
        assert second["transcribed"] == 0
        assert second["reused_indexes"] == 1
        assert len(calls) == 1
        assert first_match_count > 0
        assert len(match_calls) == first_match_count

        audio_path = calls[-1]
        stat = audio_path.stat()
        os.utime(
            audio_path,
            ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000),
        )
        same_content_new_mtime = start_job()
        assert same_content_new_mtime["transcribed"] == 0
        assert same_content_new_mtime["reused_indexes"] == 1
        assert len(calls) == 1
        assert len(match_calls) == first_match_count

        api.os.environ["SYNC_WHISPER_BEAM_SIZE"] = "1"
        changed_config = start_job()
        assert changed_config["transcribed"] == 0
        assert changed_config["reused_indexes"] == 1
        assert len(calls) == 1
        assert len(match_calls) == first_match_count

        audio_path = calls[-1]
        audio_path.write_bytes(audio_path.read_bytes() + b"\0\0")
        changed_audio = start_job()
        assert changed_audio["transcribed"] == 1
        assert len(calls) == 2
        assert len(match_calls) == first_match_count * 2


def test_legacy_word_index_is_migrated_without_retranscription(sync_api) -> None:
    client, projects = sync_api
    _write_confirmed_normalization(projects)
    assert _post_source(client).status_code == 201
    assert (
        client.put(
            "/stage2/sync-sources/vasilieva_01/audio",
            data={
                "project": "demo",
                "audio": (BytesIO(_wav_bytes()), "guide.wav"),
            },
            content_type="multipart/form-data",
        ).status_code
        == 200
    )
    index_path = projects / "demo" / "work" / "transcripts" / "vasilieva_01.words.json"
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


def test_model_config_change_reuses_completed_same_audio_index(sync_api) -> None:
    client, projects = sync_api
    _write_confirmed_normalization(projects)
    assert _post_source(client).status_code == 201
    assert (
        client.put(
            "/stage2/sync-sources/vasilieva_01/audio",
            data={
                "project": "demo",
                "audio": (BytesIO(_wav_bytes(15.0)), "guide.wav"),
            },
            content_type="multipart/form-data",
        ).status_code
        == 200
    )

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
        patch.dict(api.os.environ, {"SYNC_WHISPER_BEAM_SIZE": "2"}),
        patch.object(
            api,
            "transcribe_audio",
            side_effect=AssertionError("completed transcript must be reused"),
        ),
        patch.object(api.threading, "Thread", _ImmediateThread),
    ):
        second = client.post("/stage2/sync-jobs/start", json={"project": "demo"})

    job = client.get(second.get_json()["result"]["status_url"]).get_json()["result"]
    matches = client.get("/stage2/sync-matches?project=demo").get_json()["result"]
    found = next(item for item in matches["matches"] if item["block_uid"] == "stable-sync-uid")
    assert job["transcribed"] == 0
    assert job["reused_indexes"] == 1
    assert job["source_failures"] == []
    assert found["source_id"] == "vasilieva_01"


def test_sync_job_start_rejects_unknown_asr_engine_with_clear_error(sync_api) -> None:
    client, projects = sync_api
    _write_confirmed_normalization(projects)
    assert _post_source(client).status_code == 201

    response = client.post(
        "/stage2/sync-jobs/start",
        json={"project": "demo", "asr_engine": "turbo"},
    )

    assert response.status_code == 400
    assert "asr_engine" in response.get_json()["error"]


def test_sync_job_start_accepts_cloud_engine_and_forwards_cloud_backend(
    sync_api,
) -> None:
    client, projects = sync_api
    _write_confirmed_normalization(projects)
    assert _post_source(client).status_code == 201
    assert (
        client.put(
            "/stage2/sync-sources/vasilieva_01/audio",
            data={
                "project": "demo",
                "audio": (BytesIO(_wav_bytes()), "guide.wav"),
            },
            content_type="multipart/form-data",
        ).status_code
        == 200
    )

    captured_backend: dict[str, object] = {}

    def fake_transcribe(path, backend=None, **_kwargs):
        captured_backend["backend"] = backend
        captured_backend["model"] = _kwargs.get("model")
        return {
            "backend": "cloud-audio",
            "model": "cloud-model",
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
    ):
        response = client.post(
            "/stage2/sync-jobs/start",
            # The UI historically sent its local Whisper setting even for
            # Cloud ASR.  Regression: it must not reach the provider.
            json={"project": "demo", "asr_engine": "cloud", "model": "medium"},
        )

    assert response.status_code == 200, response.get_json()
    job = client.get(response.get_json()["result"]["status_url"]).get_json()["result"]
    assert job["status"] == "completed"
    assert job["asr_engine"] == "cloud"
    assert captured_backend["backend"] == "cloud-audio"
    assert captured_backend["model"] is None


def test_sync_job_start_defaults_to_local_engine_without_activating_cloud_backend(
    sync_api,
) -> None:
    client, projects = sync_api
    _write_confirmed_normalization(projects)
    assert _post_source(client).status_code == 201
    assert (
        client.put(
            "/stage2/sync-sources/vasilieva_01/audio",
            data={
                "project": "demo",
                "audio": (BytesIO(_wav_bytes()), "guide.wav"),
            },
            content_type="multipart/form-data",
        ).status_code
        == 200
    )

    captured_backend: dict[str, object] = {}

    def fake_transcribe(path, backend=None, **_kwargs):
        captured_backend["backend"] = backend
        captured_backend["model"] = _kwargs.get("model")
        return {
            "backend": "faster-whisper",
            "model": "small",
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
    ):
        # No asr_engine in the payload: must default to "local" and never
        # implicitly switch to the paid cloud-audio backend.
        response = client.post(
            "/stage2/sync-jobs/start",
            json={"project": "demo", "model": "medium"},
        )

    assert response.status_code == 200, response.get_json()
    job = client.get(response.get_json()["result"]["status_url"]).get_json()["result"]
    assert job["asr_engine"] == "local"
    assert captured_backend["backend"] is None
    assert captured_backend["backend"] != "cloud-audio"
    assert captured_backend["model"] == "medium"


def test_sync_job_records_asr_cost_precision_and_wall_duration_metrics(
    sync_api,
) -> None:
    client, projects = sync_api
    _write_confirmed_normalization(projects)
    assert _post_source(client).status_code == 201
    assert (
        client.put(
            "/stage2/sync-sources/vasilieva_01/audio",
            data={
                "project": "demo",
                "audio": (BytesIO(_wav_bytes()), "guide.wav"),
            },
            content_type="multipart/form-data",
        ).status_code
        == 200
    )

    def fake_transcribe(path, **_kwargs):
        return {
            "backend": "cloud-audio",
            "model": "cloud-model",
            "language": "ru",
            "backend_metadata": {
                "cost_usd": 0.02,
                "timestamp_precision": "interpolated",
            },
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
    ):
        response = client.post(
            "/stage2/sync-jobs/start",
            json={"project": "demo", "asr_engine": "cloud"},
        )

    assert response.status_code == 200, response.get_json()
    job = client.get(response.get_json()["result"]["status_url"]).get_json()["result"]
    assert job["asr_backend_used"] == "cloud-audio"
    assert job["asr_cost_usd_total"] == pytest.approx(0.02)
    assert job["asr_timestamp_precision"] == "interpolated"
    assert job["transcription_wall_sec_total"] >= 0.0
