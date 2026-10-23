from __future__ import annotations

from unittest.mock import patch

import api
from stage2.sync_models import SyncMatch
import test_stage2_sync_api as _sync_api_tests
from test_stage2_sync_api import _post_source, _write_confirmed_normalization

sync_api = _sync_api_tests.sync_api  # reuse the pytest fixture


def _match(segment_id: str, status: str, reason: str = "", speaker: str = "") -> SyncMatch:
    return SyncMatch(
        segment_id=segment_id,
        query_text="текст",
        speaker=speaker,
        status=status,
        metadata={"unmatched_reason": reason} if reason else {},
    )


def test_sync_summary_separates_asr_failed_no_speaker_and_no_file() -> None:
    summary = api._sync_summary(
        [
            _match("1", "auto_matched", speaker="Иван Петров"),
            _match("2", "needs_review", speaker="Иван Петров"),
            _match("3", "not_found", "asr_failed", speaker="Иван Петров"),
            _match("4", "not_found", "no_eligible_source", speaker="Иван Петров"),
            _match("5", "not_found", "no_eligible_source", speaker="Мария Сидорова"),
            _match("6", "not_found", "no_eligible_source"),
            _match("7", "not_found", "below_review_threshold", speaker="Иван Петров"),
        ]
    )
    assert summary["asr_failed"] == 1
    assert summary["no_file"] == 2
    assert summary["no_speaker"] == 1
    assert summary["no_source"] == summary["no_file"] + summary["no_speaker"] == 3
    # asr_failed stays in unmatched (compat) but has its own counter and is not no_source.
    assert summary["unmatched"] == 2
    assert summary["not_found"] == 5
    assert summary["unmatched"] + summary["no_source"] == summary["not_found"]


def test_sync_summary_asr_failed_is_not_counted_as_no_source() -> None:
    summary = api._sync_summary([_match("1", "not_found", "asr_failed", speaker="Иван Петров")])
    assert summary["asr_failed"] == 1
    assert summary["no_source"] == 0
    assert summary["unmatched"] == 1
    assert summary["not_found"] == 1


def test_build_response_counts_come_from_assembly_plan_summary(sync_api) -> None:
    client, projects = sync_api
    _write_confirmed_normalization(projects, scenario_revision_id="rev_0002")
    assert _post_source(client).status_code == 201
    words = [
        {"word": w, "start": 10.0 + i * 0.5, "end": 10.5 + i * 0.5}
        for i, w in enumerate(["маленькая", "женщина", "покоряла", "великих", "людей"])
    ]
    assert (
        client.put(
            "/stage2/sync-sources/vasilieva_01/words",
            json={"project": "demo", "words": words},
        ).status_code
        == 200
    )

    class _Immediate:
        def __init__(self, *, target, kwargs, **_unused):
            self.target, self.kwargs = target, kwargs

        def start(self) -> None:
            self.target(**self.kwargs)

    with patch.object(api.threading, "Thread", _Immediate):
        assert client.post("/stage2/sync-jobs/start", json={"project": "demo"}).status_code == 200
    with patch.object(api, "generate_placeholder", return_value=None):
        built = client.post("/stage2/build", json={"project": "demo"})
    assert built.status_code == 200, built.get_json()
    result = built.get_json()["result"]

    plan = result["plan_summary"]
    by_reason = plan["placeholders"]["by_reason"]
    sync_total = result["snh"] + result["host"]
    sync_reasons = ("not_found", "asr_failed", "no_speaker", "no_file")
    assert result["review"] == by_reason.get("needs_review", {}).get("segments", 0)
    assert result["unmatched"] == sum(by_reason.get(k, {}).get("segments", 0) for k in sync_reasons)
    assert result["matched"] + result["review"] + result["unmatched"] == sync_total
    assert result["sync_summary"] == {
        "matched": result["matched"],
        "review": result["review"],
        "unmatched": result["unmatched"],
    }
    assert plan["items"] == result["segments"]
