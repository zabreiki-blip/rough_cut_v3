"""Ошибки ASR не должны маскироваться под успешное завершение sync-задачи.

Источник, чьё распознавание упало, остаётся в ``source_failures``; итог задачи
обязан явно показывать неполноту (``complete=False`` + ``degraded_sources``), а
сегменты, которым этот источник мог бы подойти, получают причину ``asr_failed``,
отличимую от ``no_eligible_source`` («записи для спикера нет»).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

import api
import test_stage2_sync_api as base
from test_stage2_sync_api import (
    _ImmediateThread,
    _post_source,
    _sync_asr_setup,
    _write_confirmed_normalization,
)

sync_api = base.sync_api


@pytest.fixture(autouse=True)
def _no_llm_review(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api.get_settings(), "openrouter_api_key", "")


def _start(client, **payload) -> dict:
    with patch.object(api.threading, "Thread", _ImmediateThread):
        response = client.post("/stage2/sync-jobs/start", json={"project": "demo", **payload})
    assert response.status_code == 200, response.get_json()
    return client.get(response.get_json()["result"]["status_url"]).get_json()["result"]


def _matches(client) -> list[dict]:
    return client.get("/stage2/sync-matches?project=demo").get_json()["result"]["matches"]


def test_asr_failure_marks_job_incomplete_and_lists_degraded_sources(sync_api) -> None:
    client, projects = sync_api
    _sync_asr_setup(client, projects)

    with patch.object(api, "transcribe_audio", side_effect=RuntimeError("asr boom")):
        job = _start(client)

    # status остаётся терминальным "completed": от него зависят опрос UI и
    # terminal_statuses; неполнота выражена отдельными полями.
    assert job["status"] == "completed"
    assert job["complete"] is False
    assert job["degraded_sources"] == ["vasilieva_01"]
    assert job["unavailable_sources"] == []
    assert "ASR failed for 1 source(s)" in job["message"]
    assert job["source_failures"][0]["source_id"] == "vasilieva_01"


def test_clean_run_is_marked_complete(sync_api) -> None:
    client, projects = sync_api
    _write_confirmed_normalization(projects)
    assert _post_source(client).status_code == 201
    base._seed_words(client)

    job = _start(client)

    assert job["status"] == "completed"
    assert job["complete"] is True
    assert job["degraded_sources"] == []
    assert job["unavailable_sources"] == []
    assert job["message"] == "Completed"


def test_segment_of_failed_source_gets_asr_failed_not_no_eligible_source(sync_api) -> None:
    client, projects = sync_api
    _sync_asr_setup(client, projects)

    with patch.object(api, "transcribe_audio", side_effect=RuntimeError("asr boom")):
        job = _start(client)

    by_speaker = {item["speaker"]: item for item in _matches(client)}
    failed = by_speaker["Татьяна Васильева"]
    assert failed["status"] == "not_found"
    assert failed["metadata"]["unmatched_reason"] == "asr_failed"
    assert failed["metadata"]["asr_failed_source_ids"] == ["vasilieva_01"]
    # Ведущему этот источник не подходит: для него записи действительно нет.
    assert by_speaker["Ведущий"]["metadata"]["unmatched_reason"] == "no_eligible_source"
    # asr_failed - не «ждём загрузки записи»: идёт в unmatched, а не в no_source.
    assert job["no_source"] == 1
    assert job["unmatched"] == 1


def test_missing_recording_keeps_no_eligible_source_reason(sync_api) -> None:
    client, projects = sync_api
    _write_confirmed_normalization(projects)
    # Источник без аудио/индекса: ASR даже не запускался, записи реально нет.
    assert _post_source(client).status_code == 201

    with patch.object(api, "transcribe_audio", side_effect=AssertionError("ASR must not run")):
        job = _start(client)

    matches = _matches(client)
    assert matches
    assert all(item["metadata"]["unmatched_reason"] == "no_eligible_source" for item in matches)
    # Задача всё равно неполна (источник не удалось прочитать), но причина
    # сегментов не подменяется на asr_failed, а источник - не «ASR failed»:
    # он в отдельном поле unavailable_sources.
    assert job["complete"] is False
    assert job["degraded_sources"] == []
    assert job["unavailable_sources"] == ["vasilieva_01"]
    assert "ASR failed" not in job["message"]
    assert "guide file unavailable" in job["message"]
    assert job["no_source"] == len(matches)


# -- сопоставление имён: тот же принцип, что в SyncMatcher._eligible_sources --


def _unit_match(speaker: str):
    from types import SimpleNamespace

    return SimpleNamespace(
        segment_id="seg",
        speaker=speaker,
        status="not_found",
        metadata={"unmatched_reason": "no_eligible_source"},
    )


def _unit_source(speaker: str, **kwargs):
    from stage2.sync_models import SyncSource

    return SyncSource(id="src", speaker=speaker, premiere_sequence="SYNC_SRC", **kwargs)


@pytest.mark.parametrize(
    ("source_name", "speaker", "expected"),
    [
        ("Татьяна Васильева", "Татьяна Васильева", True),
        ("29-626 Васильева", "СИНХ Васильевой", True),  # дата, метка, склонение
        ("Васильева", "Татьяна Васильева, мать", True),  # роль после запятой не имя
        # Общая фамилия, разные имена - разные люди: ложного asr_failed нет.
        ("Анна Иванова", "Мария Иванова", False),
        ("Иван Петров", "Ведущий", False),
    ],
)
def test_asr_failed_relabel_uses_speaker_name_tokens(
    source_name: str, speaker: str, expected: bool
) -> None:
    from sync_asr_failures import mark_asr_failed_matches

    match = _unit_match(speaker)
    source = _unit_source(source_name)

    changed = mark_asr_failed_matches([match], [source], {"src"}, lambda value: value.split())

    assert bool(changed) is expected
    reason = "asr_failed" if expected else "no_eligible_source"
    assert match.metadata["unmatched_reason"] == reason


def test_asr_failed_relabel_all_speakers_source_covers_everyone() -> None:
    from sync_asr_failures import mark_asr_failed_matches

    match = _unit_match("Кто угодно")
    changed = mark_asr_failed_matches(
        [match], [_unit_source("Общий", all_speakers=True)], {"src"}, lambda value: value.split()
    )

    assert changed == ["seg"]
