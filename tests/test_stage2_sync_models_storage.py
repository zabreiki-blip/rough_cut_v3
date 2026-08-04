from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from stage2.sync_models import (
    SyncAlternative,
    SyncMatch,
    SyncSource,
    WordIndex,
    WordTimestamp,
)
from stage2.sync_storage import SyncMatchStore, SyncSourceStore


def _source(source_id: str = "vasilieva_01") -> SyncSource:
    return SyncSource(
        id=source_id,
        speaker="Татьяна Васильева",
        premiere_sequence="SYNC_ВАСИЛЬЕВА_01",
        sequence_id="premiere-123",
        guide_audio=f"sync_audio/{source_id}.wav",
        guide_offset_sec=2.5,
        duration_sec=1800.0,
    )


def test_sync_source_round_trip_and_legacy_aliases() -> None:
    source = _source()
    assert SyncSource.from_dict(source.to_dict()) == source

    legacy = SyncSource.from_dict(
        {
            "source_id": "legacy",
            "speaker": "Герой",
            "name": "SYNC_LEGACY",
            "audio_path": "legacy.wav",
        }
    )
    assert legacy.id == "legacy"
    assert legacy.premiere_sequence == "SYNC_LEGACY"
    assert legacy.guide_audio == "legacy.wav"
    assert legacy.source_kind == "premiere_sequence"

    # The additive fields must not shift the legacy positional arguments.
    positional = SyncSource("positional", "Герой", "SYNC_POSITIONAL", ("Гость",))
    assert positional.speakers == ("Гость",)
    assert positional.source_kind == "premiere_sequence"


def test_review_media_source_round_trip_and_proxy_name_alias() -> None:
    source = SyncSource.from_dict(
        {
            "id": "brick_review_01",
            "source_kind": "review_media",
            "media_path": "/Volumes/L_BRICK/DOP/Брик_просмотровки/episode_01.mp4",
            "media_fingerprint": "sha256:0123456789abcdef",
            "proxy_sequence_id": "premiere-proxy-01",
            "proxy_sequence_name": "RC_SRC_EPISODE_01",
        }
    )

    assert source.premiere_sequence == "RC_SRC_EPISODE_01"
    assert source.source_kind == "review_media"
    assert SyncSource.from_dict(source.to_dict()) == source


def test_review_media_source_requires_media_path_and_rejects_unknown_kind() -> None:
    with pytest.raises(ValueError, match="media_path"):
        SyncSource.from_dict(
            {
                "id": "review",
                "source_kind": "review_media",
                "proxy_sequence_name": "RC_SRC_REVIEW",
            }
        )
    with pytest.raises(ValueError, match="source kind"):
        SyncSource.from_dict(
            {
                "id": "future",
                "source_kind": "future_kind",
                "premiere_sequence": "SYNC_FUTURE",
            }
        )


def test_sync_source_round_trip_preserves_multi_speaker_scope() -> None:
    source = SyncSource(
        id="shared_interviews",
        speaker="Ирина Котова",
        speakers=("Алексей Иванов", "Мария Петрова"),
        premiere_sequence="SYNC_SHARED_INTERVIEWS",
    )

    restored = SyncSource.from_dict(source.to_dict())

    assert restored == source
    # ``speaker`` remains the backwards-compatible primary speaker while
    # ``speakers`` registers additional people present on the same timeline.
    assert restored.speaker == "Ирина Котова"
    assert restored.speakers == ("Алексей Иванов", "Мария Петрова")
    assert restored.all_speakers is False


def test_sync_source_round_trip_preserves_all_speakers_scope() -> None:
    source = SyncSource(
        id="master_interviews",
        speaker="",
        premiere_sequence="SYNC_MASTER_INTERVIEWS",
        all_speakers=True,
    )

    assert SyncSource.from_dict(source.to_dict()) == source
    assert source.speakers == ()
    assert source.all_speakers is True


def test_legacy_sync_source_defaults_to_one_speaker_only() -> None:
    source = SyncSource.from_dict(
        {
            "id": "legacy_single",
            "speaker": "Ирина Котова",
            "premiere_sequence": "SYNC_LEGACY_SINGLE",
        }
    )

    assert source.speaker == "Ирина Котова"
    assert source.speakers == ()
    assert source.all_speakers is False


def test_model_validation_rejects_broken_time_ranges() -> None:
    with pytest.raises(ValueError):
        WordTimestamp("слово", 3.0, 2.0)
    with pytest.raises(ValueError):
        SyncSource(id="", speaker="", premiere_sequence="SYNC")
    with pytest.raises(ValueError):
        SyncMatch(
            segment_id="snh1",
            query_text="текст",
            source_in_sec=2.0,
            source_out_sec=None,
        )


def test_word_index_is_sorted_and_serializable() -> None:
    index = WordIndex(
        source_id="s1",
        words=[
            WordTimestamp("второе", 2.0, 2.5),
            WordTimestamp("первое", 1.0, 1.5),
        ],
        model="whisper-large",
    )
    restored = WordIndex.from_dict(index.to_dict())
    assert [word.word for word in restored.words] == ["первое", "второе"]
    assert restored.duration_sec == 2.5


def test_source_store_upserts_atomically_and_accepts_bare_list() -> None:
    with TemporaryDirectory() as temporary:
        path = Path(temporary) / "work" / "sync_sources.json"
        store = SyncSourceStore(path)
        store.save(_source("b"))
        store.save(_source("a"))
        store.save(
            SyncSource(
                id="a",
                speaker="Обновлённый герой",
                premiere_sequence="SYNC_A",
            )
        )

        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["schema_version"] == 1
        assert [item["id"] for item in payload["sources"]] == ["a", "b"]
        assert store.get("a").speaker == "Обновлённый герой"  # type: ignore[union-attr]
        assert store.delete("missing") is False
        assert store.delete("b") is True

        path.write_text(
            json.dumps([_source("legacy").to_dict()], ensure_ascii=False),
            encoding="utf-8",
        )
        assert [item.id for item in store.list()] == ["legacy"]


def test_source_store_serializes_multi_speaker_scope_as_json() -> None:
    with TemporaryDirectory() as temporary:
        path = Path(temporary) / "sync_sources.json"
        source = SyncSource(
            id="shared",
            speaker="Ирина Котова",
            speakers=("Алексей Иванов", "Мария Петрова"),
            premiere_sequence="SYNC_SHARED",
            all_speakers=False,
        )

        store = SyncSourceStore(path)
        store.save(source)

        payload = json.loads(path.read_text(encoding="utf-8"))["sources"][0]
        assert payload["speakers"] == ["Алексей Иванов", "Мария Петрова"]
        assert payload["all_speakers"] is False
        assert store.get("shared") == source


def test_source_store_keeps_schema_v1_for_additive_review_media_fields() -> None:
    with TemporaryDirectory() as temporary:
        path = Path(temporary) / "sync_sources.json"
        source = SyncSource.from_dict(
            {
                "id": "review",
                "source_kind": "review_media",
                "media_path": "/Volumes/reviews/review.mp4",
                "proxy_sequence_name": "RC_SRC_REVIEW",
            }
        )

        SyncSourceStore(path).save(source)

        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["schema_version"] == 1
        assert payload["sources"][0]["source_kind"] == "review_media"
        assert SyncSourceStore(path).get("review") == source


def test_match_store_is_separate_and_preserves_scenario_file() -> None:
    with TemporaryDirectory() as temporary:
        work = Path(temporary)
        scenario = work / "normalization.json"
        original = '{"status":"confirmed","blocks":[{"id":"snh1"}]}\n'
        scenario.write_text(original, encoding="utf-8")
        store = SyncMatchStore(work / "sync_matches.json")

        match = SyncMatch(
            segment_id="snh1",
            query_text="Она была энергетическим вампиром",
            source_id="vasilieva_01",
            source_in_sec=12.0,
            source_out_sec=16.0,
            confidence=0.91,
            status="auto_matched",
            alternatives=[
                SyncAlternative("vasilieva_02", 40.0, 44.0, 0.61, "похожий текст")
            ],
        )
        store.save(match)

        assert scenario.read_text(encoding="utf-8") == original
        restored = store.get("snh1")
        assert restored is not None
        assert restored.status == "auto_matched"
        assert restored.alternatives[0].source_id == "vasilieva_02"

        restored.status = "confirmed"
        restored.metadata["confirmed_by"] = "editor"
        store.save(restored)
        confirmed = store.get("snh1")
        assert confirmed is not None
        assert confirmed.status == "confirmed"
        assert confirmed.metadata["confirmed_by"] == "editor"


def test_match_store_keeps_distinct_blocks_with_overlapping_ranges() -> None:
    with TemporaryDirectory() as temporary:
        store = SyncMatchStore(Path(temporary) / "sync_matches.json")
        store.replace_all([
            SyncMatch(
                segment_id="teaser-block",
                query_text="Короткий анонс",
                source_id="interview-1",
                source_in_sec=120.0,
                source_out_sec=124.0,
                confidence=0.98,
                status="confirmed",
            ),
            SyncMatch(
                segment_id="full-block",
                query_text="Полный ответ героя",
                source_id="interview-1",
                source_in_sec=120.0,
                source_out_sec=132.0,
                confidence=0.98,
                status="confirmed",
            ),
        ])

        restored = store.list()
        assert [item.segment_id for item in restored] == ["teaser-block", "full-block"]
        assert [item.source_in_sec for item in restored] == [120.0, 120.0]
        assert [item.source_out_sec for item in restored] == [124.0, 132.0]


def test_store_reports_malformed_json_instead_of_overwriting_it() -> None:
    with TemporaryDirectory() as temporary:
        path = Path(temporary) / "sync_sources.json"
        path.write_text("{broken", encoding="utf-8")
        with pytest.raises(ValueError, match="invalid JSON"):
            SyncSourceStore(path).save(_source())
