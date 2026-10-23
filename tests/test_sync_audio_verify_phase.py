"""Проверка кандидатов синхрона по звуку как пост-обработка (verify_clip замокан)."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import api
import sync_audio_verify_phase as phase
import test_stage2_sync_api as base
from cost_budget import CostBudget, CostBudgetExceeded
from stage2.sync_audio_verify import AudioVerdict
from stage2.sync_models import SyncMatch, WordIndex
from test_stage2_sync_api import _ImmediateThread, _post_source, _write_confirmed_normalization

sync_api = base.sync_api


def _match(segment_id: str, status: str, confidence: float = 0.95, **kwargs) -> SyncMatch:
    params = dict(
        segment_id=segment_id,
        query_text="цитата сценария",
        speaker="Иван",
        source_id="src",
        source_in_sec=1.0,
        source_out_sec=4.0,
        confidence=confidence,
        status=status,
    )
    params.update(kwargs)
    return SyncMatch(**params)


def _distinct(i: int) -> dict:
    """Different clip per match (identical clips are verified once)."""
    return {"source_in_sec": 1.0 + 10 * i, "source_out_sec": 4.0 + 10 * i}


def _index(source_id: str = "src", precision: str = "") -> WordIndex:
    metadata = {"timestamp_precision": precision} if precision else {}
    return WordIndex(source_id=source_id, metadata=metadata)


def _verdict(kind: str = "match", coverage: float = 1.0, confidence: float = 0.95, **kwargs):
    return AudioVerdict(
        kind, coverage=coverage, confidence=confidence, heard_text="слышно", model="m", **kwargs
    )


class FakeVerify:
    def __init__(self, verdict: AudioVerdict | None = None, error: Exception | None = None):
        self.verdict = verdict or _verdict()
        self.error = error
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        budget = kwargs.get("budget")
        if budget is not None and self.verdict.cost_usd:
            # Как настоящий verify_clip: резерв и фактическая стоимость в бюджете задачи.
            budget.settle(budget.reserve_attempt(1.0), self.verdict.cost_usd)
        return self.verdict


def _run(matches, verify, indexes=None, **kwargs):
    params = dict(
        resolve_audio_path=lambda source: Path("/nonexistent.wav"),
        cache_dir=Path("/nonexistent-cache"),
        budget=None,
        environ={},
        now="2026-01-01T00:00:00+00:00",
        verify=verify,
    )
    params.update(kwargs)
    return phase.run_audio_verification(
        matches, [SimpleNamespace(id="src")], indexes or [_index()], **params
    )


# --- отбор кандидатов -------------------------------------------------------------------


def test_selection_skips_confirmed_high_auto_and_unusable_matches() -> None:
    matches = [
        _match("confirmed", "confirmed", 0.5),
        _match("high_auto", "auto_matched", 0.97),
        _match("low_auto", "auto_matched", 0.8),
        _match("review", "needs_review", 0.6),
        _match(
            "not_found", "not_found", 0.0, source_id="", source_in_sec=None, source_out_sec=None
        ),
        _match("no_range", "needs_review", 0.6, source_in_sec=None, source_out_sec=None),
        _match("life", "needs_review", 0.6, metadata={"block_type": "life"}),
    ]
    ids = [m.segment_id for m in phase.select_candidates(matches, [_index()])]
    assert ids == ["review", "low_auto"]


def test_selection_threshold_is_configurable_and_boundary_is_exclusive() -> None:
    matches = [_match("a", "auto_matched", 0.9), _match("b", "auto_matched", 0.85)]
    assert [m.segment_id for m in phase.select_candidates(matches, [_index()])] == ["b"]
    assert [
        m.segment_id for m in phase.select_candidates(matches, [_index()], auto_below=0.95)
    ] == [
        "a",
        "b",
    ]


def test_interpolated_source_and_window_refined_are_verified_first() -> None:
    matches = [
        _match("review", "needs_review", 0.6, source_id="clean"),
        _match("interp_auto", "auto_matched", 0.99, source_id="interp"),
        _match(
            "refined_auto",
            "auto_matched",
            0.99,
            source_id="clean",
            metadata={"window_refined": {"a": 1}},
        ),
        _match("high_clean", "auto_matched", 0.99, source_id="clean"),
        _match("confirmed_interp", "confirmed", 0.99, source_id="interp"),
    ]
    indexes = [_index("clean"), _index("interp", "interpolated")]
    ids = [m.segment_id for m in phase.select_candidates(matches, indexes)]
    assert ids == ["interp_auto", "refined_auto", "review"]


def test_retimed_index_is_not_imprecise() -> None:
    matches = [_match("auto", "auto_matched", 0.99)]
    assert phase.select_candidates(matches, [_index("src", "retimed")]) == []


# --- решения ----------------------------------------------------------------------------


def test_match_promotes_needs_review_and_records_metadata() -> None:
    match = _match(
        "m", "needs_review", 0.7, metadata={"review_reason": "range_disagrees_with_local"}
    )
    result = _run([match], FakeVerify(_verdict("match", 0.95, 0.9)))
    assert match.status == "auto_matched"
    record = match.metadata["audio_verified"]
    assert record["verdict"] == "match" and record["coverage"] == 0.95
    assert record["confidence"] == 0.9 and record["heard_text"] == "слышно"
    assert record["model"] == "m" and record["at"] == "2026-01-01T00:00:00+00:00"
    assert "review_reason" not in match.metadata
    assert match.metadata["review_reason_before_audio"] == "range_disagrees_with_local"
    assert (result.verified, result.promoted, result.rejected) == (1, 1, 0)


def test_weak_match_verdict_keeps_status() -> None:
    review = _match("r", "needs_review", 0.7)
    auto = _match("a", "auto_matched", 0.8)
    _run([review, auto], FakeVerify(_verdict("match", 0.89, 0.95)))
    assert (review.status, auto.status) == ("needs_review", "auto_matched")
    _run([review], FakeVerify(_verdict("match", 0.95, 0.84)))
    assert review.status == "needs_review"
    assert review.metadata["audio_verified"]["verdict"] == "match"


def test_no_match_demotes_auto_with_audio_mismatch_and_heard_text() -> None:
    match = _match("a", "auto_matched", 0.8)
    result = _run([match], FakeVerify(_verdict("no_match", 0.0, 0.9)))
    assert match.status == "needs_review"
    assert match.metadata["review_reason"] == "audio_mismatch"
    assert match.metadata["audio_verified"]["heard_text"] == "слышно"
    assert (result.verified, result.rejected, result.promoted) == (1, 1, 0)


def test_partial_sends_to_review() -> None:
    match = _match("a", "auto_matched", 0.8)
    result = _run([match], FakeVerify(_verdict("partial", 0.5, 0.8)))
    assert match.status == "needs_review"
    assert match.metadata["review_reason"] == "audio_partial"
    assert result.partial == 1 and result.rejected == 0


def test_error_leaves_status_and_marks_metadata() -> None:
    match = _match("a", "needs_review", 0.8)
    result = _run([match], FakeVerify(AudioVerdict("error", reason="request: boom")))
    assert match.status == "needs_review"
    assert match.metadata["audio_verified"]["verdict"] == "error"
    assert match.metadata["audio_verified"]["reason"] == "request: boom"
    assert (result.errors, result.verified) == (1, 0)


def test_unexpected_exception_is_an_error_verdict() -> None:
    match = _match("a", "auto_matched", 0.8)
    result = _run([match], FakeVerify(error=RuntimeError("boom")))
    assert match.status == "auto_matched" and result.errors == 1


def test_confirmed_match_is_never_sent() -> None:
    verify = FakeVerify()
    match = _match("c", "confirmed", 0.1)
    _run([match], verify)
    assert verify.calls == [] and "audio_verified" not in match.metadata


def test_verify_receives_clip_quote_cache_dir_and_budget() -> None:
    verify = FakeVerify()
    budget = CostBudget(Decimal("0"), Decimal("0"), require_asr_rate=False)
    _run([_match("a", "needs_review")], verify, budget=budget, cache_dir=Path("/c/audio-verify"))
    call = verify.calls[0]
    assert (call["start_sec"], call["end_sec"]) == (1.0, 4.0)
    assert call["quote"] == "цитата сценария" and call["speaker"] == "Иван"
    assert call["budget"] is budget and call["cache_dir"] == Path("/c/audio-verify")


def test_cached_verdict_costs_nothing() -> None:
    result = _run([_match("a", "needs_review")], FakeVerify(_verdict(cost_usd=0.5, cached=True)))
    assert result.cost_usd == 0.0 and result.verified == 1


# --- бюджет -----------------------------------------------------------------------------


def test_audio_budget_exhaustion_stops_and_warns() -> None:
    matches = [_match(f"m{i}", "needs_review", 0.5, **_distinct(i)) for i in range(5)]
    verify = FakeVerify(_verdict("match", 0.3, 0.3, cost_usd=0.01))
    result = _run(
        matches,
        verify,
        environ={"SYNC_AUDIO_VERIFY_MAX_USD": "0.02", "SYNC_AUDIO_VERIFY_WORKERS": "1"},
    )
    assert len(verify.calls) == 2
    assert result.budget_exhausted and result.skipped == 3
    assert result.warnings and "исчерпан бюджет" in result.warnings[0]
    assert [bool(m.metadata.get("audio_verified")) for m in matches] == [
        True,
        True,
        False,
        False,
        False,
    ]
    assert result.cost_usd == pytest.approx(0.02)


def test_job_budget_exception_stops_remaining_without_changes() -> None:
    matches = [_match(f"m{i}", "needs_review", 0.5, **_distinct(i)) for i in range(3)]
    result = _run(
        matches,
        FakeVerify(error=CostBudgetExceeded("limit")),
        environ={"SYNC_AUDIO_VERIFY_WORKERS": "1"},
    )
    assert result.budget_exhausted and result.skipped == 3
    assert all(m.status == "needs_review" and "audio_verified" not in m.metadata for m in matches)


def test_unknown_cost_hits_the_audio_limit_by_estimate() -> None:
    # The provider reports no cost: verify_clip returns an estimated cost, the limit still holds.
    matches = [_match(f"m{i}", "needs_review", 0.5, **_distinct(i)) for i in range(6)]
    verdict = _verdict("match", 0.3, 0.3, cost_usd=0.0006, cost_estimated=True)
    verify = FakeVerify(verdict)
    result = _run(
        matches,
        verify,
        environ={"SYNC_AUDIO_VERIFY_MAX_USD": "0.001", "SYNC_AUDIO_VERIFY_WORKERS": "1"},
    )
    assert len(verify.calls) == 2 and result.budget_exhausted and result.skipped == 4
    assert result.cost_usd == pytest.approx(0.0012)


def test_limit_is_checked_before_every_clip_of_a_batch() -> None:
    # 4 workers, each clip ~ $0.00047 by estimate: only 3 are launched with a $0.001 limit.
    matches = [_match(f"m{i}", "needs_review", 0.5, **_distinct(i)) for i in range(8)]
    verify = FakeVerify(_verdict("match", 0.3, 0.3, cost_usd=0.0005))
    result = _run(
        matches,
        verify,
        environ={"SYNC_AUDIO_VERIFY_MAX_USD": "0.001", "SYNC_AUDIO_VERIFY_WORKERS": "4"},
    )
    assert len(verify.calls) == 3 and result.budget_exhausted
    assert result.cost_usd <= 0.001 + 0.0005  # overshoot below one clip


def test_clip_count_is_capped_per_run() -> None:
    matches = [_match(f"m{i}", "needs_review", 0.5, **_distinct(i)) for i in range(5)]
    verify = FakeVerify(_verdict("match", 0.3, 0.3))
    result = _run(
        matches, verify, environ={"SYNC_AUDIO_VERIFY_MAX_CLIPS": "2", "SYNC_AUDIO_VERIFY_WORKERS": "1"}
    )
    assert len(verify.calls) == 2 and result.clips_limited and result.skipped == 3
    assert "SYNC_AUDIO_VERIFY_MAX_CLIPS" in result.warnings[0]
    assert phase.audio_max_clips({}) == 200 and phase.audio_max_clips({"SYNC_AUDIO_VERIFY_MAX_CLIPS": "x"}) == 200


def test_identical_clips_are_verified_once_and_paid_once() -> None:
    matches = [_match(f"m{i}", "needs_review", 0.5) for i in range(3)]  # same range and quote
    verify = FakeVerify(_verdict("match", 0.3, 0.3, cost_usd=0.01))
    result = _run(matches, verify, environ={"SYNC_AUDIO_VERIFY_WORKERS": "1"})
    assert len(verify.calls) == 1 and result.checked == 3 and result.verified == 3
    assert result.cost_usd == pytest.approx(0.01)
    assert all("audio_verified" in m.metadata for m in matches)


def test_cancel_in_the_middle_keeps_paid_verdicts() -> None:
    matches = [_match(f"m{i}", "needs_review", 0.5, **_distinct(i)) for i in range(4)]
    calls = {"n": 0}

    def cancel() -> None:
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("cancelled")

    partial = phase.AudioVerifyResult()
    with pytest.raises(RuntimeError, match="cancelled"):
        _run(
            matches,
            FakeVerify(_verdict("no_match", 0.0, 0.9, cost_usd=0.01)),
            environ={"SYNC_AUDIO_VERIFY_WORKERS": "1"},
            ensure_not_cancelled=cancel,
            result=partial,
        )
    assert partial.changed_ids == ["m0", "m1"] and partial.cost_usd == pytest.approx(0.02)
    stored = [_match(f"m{i}", "needs_review", 0.5, **_distinct(i)) for i in range(4)]
    snapshot = phase.snapshot_matches(stored)
    assert phase.persist_audio_results(stored, matches, partial.changed_ids, snapshot)
    assert [m.metadata.get("review_reason") for m in stored] == [
        "audio_mismatch", "audio_mismatch", None, None,
    ]


def test_selection_skips_manual_matches() -> None:
    matches = [
        _match("edited", "needs_review", 0.5, metadata={"manual_updated_at": "2026-01-01"}),
        _match("plain", "needs_review", 0.5),
    ]
    assert [m.segment_id for m in phase.select_candidates(matches, [_index()])] == ["plain"]


def test_unrefined_unreliable_match_is_not_promoted_and_keeps_its_reason() -> None:
    match = _match("m", "needs_review", 0.7, metadata={"review_reason": "timing_unrefined"})
    result = _run([match], FakeVerify(_verdict("match", 0.97, 0.93)), [_index("src", "interpolated")])
    assert match.status == "needs_review" and result.promoted == 0
    assert match.metadata["review_reason"] == "timing_unrefined"
    assert match.metadata["audio_verified"]["verdict"] == "match"


def test_window_refined_match_may_be_promoted_on_interpolated_source() -> None:
    match = _match("m", "needs_review", 0.7, metadata={"window_refined": {"to": [1, 4]}})
    result = _run([match], FakeVerify(_verdict("match", 0.97, 0.93)), [_index("src", "interpolated")])
    assert match.status == "auto_matched" and result.promoted == 1


def test_wide_range_is_not_promoted_even_when_the_sound_matches() -> None:
    # 2 words: speech estimate 3 s; 30 s of range is far above 2 * 3 + 4.
    match = _match(
        "m", "needs_review", 0.7, source_out_sec=31.0, metadata={"review_reason": "range_disagrees_with_local"}
    )
    result = _run([match], FakeVerify(_verdict("match", 0.97, 0.93)))
    assert match.status == "needs_review" and result.promoted == 0
    assert match.metadata["review_reason"] == "audio_match_wide_range"
    assert match.metadata["review_reason_before_audio"] == "range_disagrees_with_local"
    boundary = _match("b", "needs_review", 0.7, source_out_sec=11.0)  # exactly 2 * 3 + 4 = 10 s
    _run([boundary], FakeVerify(_verdict("match", 0.97, 0.93)))
    assert boundary.status == "auto_matched"


def test_persist_writes_only_this_runs_matches_and_skips_manual() -> None:
    old = _match("old", "needs_review", 0.8, metadata={"audio_verified": {"verdict": "partial"}})
    fresh = _match("fresh", "auto_matched", 0.8)
    _run([fresh], FakeVerify(_verdict("no_match", 0.0, 0.9)))
    stored_old = _match("old", "needs_review", 0.8)
    stored_fresh = _match("fresh", "auto_matched", 0.8)
    stored_manual = _match("manual", "needs_review", 0.8, metadata={"manual_updated_at": "x"})
    mem_manual = _match("manual", "needs_review", 0.8, metadata={"audio_verified": {"verdict": "x"}})
    assert phase.persist_audio_results(
        [stored_old, stored_fresh, stored_manual], [old, fresh, mem_manual], ["fresh", "manual"]
    )
    assert "audio_verified" not in stored_old.metadata
    assert "audio_verified" not in stored_manual.metadata
    assert stored_fresh.status == "needs_review"


def test_manual_edit_during_verification_is_not_overwritten() -> None:
    mem = _match("a", "auto_matched", 0.8)
    stored = _match("a", "auto_matched", 0.8)
    snapshot = phase.snapshot_matches([stored])
    _run([mem], FakeVerify(_verdict("no_match", 0.0, 0.9)))
    # the user moves the range while the job is running
    stored.source_in_sec, stored.source_out_sec = 20.0, 24.0
    assert not phase.persist_audio_results([stored], [mem], ["a"], snapshot)
    assert stored.status == "auto_matched" and "audio_verified" not in stored.metadata
    # ... or changes the status
    stored.source_in_sec, stored.source_out_sec = 1.0, 4.0
    stored.status = "needs_review"
    assert not phase.persist_audio_results([stored], [mem], ["a"], snapshot)
    assert "audio_verified" not in stored.metadata


# --- включение --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("requested", "engine", "env", "expected"),
    [
        (None, "cloud", {"OPENROUTER_API_KEY": "k"}, True),
        (None, "cloud", {}, False),
        (None, "local", {"OPENROUTER_API_KEY": "k"}, False),
        (False, "cloud", {"OPENROUTER_API_KEY": "k"}, False),
        (True, "local", {"SYNC_CLOUD_ASR_API_KEY": "k"}, True),
        (None, "local", {"SYNC_AUDIO_VERIFY": "1", "OPENROUTER_API_KEY": "k"}, True),
        (None, "cloud", {"SYNC_AUDIO_VERIFY": "0", "OPENROUTER_API_KEY": "k"}, False),
        (True, "cloud", {"SYNC_AUDIO_VERIFY": "0", "OPENROUTER_API_KEY": "k"}, True),
    ],
)
def test_resolve_audio_verify(requested, engine, env, expected) -> None:
    assert phase.resolve_audio_verify(requested, engine, env)[0] is expected


def test_explicit_enable_without_key_reports_reason() -> None:
    enabled, reason = phase.resolve_audio_verify(True, "cloud", {})
    assert enabled is False and "ключ" in reason


def test_max_usd_default_and_invalid_values() -> None:
    assert phase.audio_max_usd({}) == Decimal("0.5")
    assert phase.audio_max_usd({"SYNC_AUDIO_VERIFY_MAX_USD": "abc"}) == Decimal("0.5")
    assert phase.audio_max_usd({"SYNC_AUDIO_VERIFY_MAX_USD": "1.25"}) == Decimal("1.25")


def test_persist_updates_stored_but_not_confirmed() -> None:
    mem = _match("a", "auto_matched", 0.8)
    _run([mem], FakeVerify(_verdict("no_match", 0.0, 0.9)))
    stored_a = _match("a", "auto_matched", 0.8)
    stored_c = _match("c", "confirmed", 0.8)
    mem_c = _match("c", "needs_review", 0.8, metadata={"audio_verified": {"verdict": "no_match"}})
    assert phase.persist_audio_results([stored_a, stored_c], [mem, mem_c], ["a", "c"])
    assert stored_a.status == "needs_review"
    assert stored_a.metadata["review_reason"] == "audio_mismatch"
    assert stored_c.status == "confirmed" and "audio_verified" not in stored_c.metadata


# --- интеграция: задача sync -----------------------------------------------------------


def _seed_interpolated(client, projects: Path) -> None:
    _write_confirmed_normalization(projects)
    assert _post_source(client).status_code == 201
    base._seed_words(client)
    path = projects / "demo" / "work" / "transcripts" / "vasilieva_01.words.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["backend_metadata"] = {"timestamp_precision": "interpolated"}
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _start(client, env: dict[str, str], fake: FakeVerify, **payload) -> dict:
    with (
        patch.object(api.threading, "Thread", _ImmediateThread),
        patch.object(phase, "verify_clip", fake),
        patch.object(api, "_resolve_sync_audio_path", lambda source, paths: Path("/x.wav")),
        # Thread подменён на _ImmediateThread: пул потоков в тесте недоступен.
        patch.dict("os.environ", {"SYNC_AUDIO_VERIFY_WORKERS": "1", **env}, clear=False),
    ):
        response = client.post("/stage2/sync-jobs/start", json={"project": "demo", **payload})
        assert response.status_code == 200, response.get_json()
        return client.get(response.get_json()["result"]["status_url"]).get_json()["result"]


def _stored(client) -> list[dict]:
    return client.get("/stage2/sync-matches?project=demo").get_json()["result"]["matches"]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api.get_settings(), "openrouter_api_key", "")
    for name in (
        "SYNC_AUDIO_VERIFY",
        "SYNC_AUDIO_VERIFY_MAX_USD",
        "SYNC_CLOUD_ASR_API_KEY",
        "OPENROUTER_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)


def test_job_promotes_interpolated_match_and_reports_counters(sync_api) -> None:
    client, projects = sync_api
    _seed_interpolated(client, projects)
    fake = FakeVerify(_verdict("match", 0.97, 0.93, cost_usd=0.0007))
    job = _start(client, {"OPENROUTER_API_KEY": "k"}, fake, audio_verify=True)
    assert len(fake.calls) == 1, job
    assert fake.calls[0]["cache_dir"] == (
        projects / "demo" / "work" / "transcripts" / "audio-verify"
    )
    # Interpolated source without a successful window refine: the sound matches, but the
    # imprecise range must not be promoted to auto_matched.
    assert job["audio_verified"] == 1 and job["audio_promoted"] == 0
    assert job["audio_rejected"] == 0
    assert job["audio_cost_usd"] == pytest.approx(0.0007)
    assert job["cost_usd"] >= 0.0007
    stored = [m for m in _stored(client) if m["source_id"] == "vasilieva_01"]
    assert stored[0]["status"] == "needs_review"
    assert stored[0]["metadata"]["audio_verified"]["verdict"] == "match"


def test_job_demotes_on_no_match_and_stores_heard_text(sync_api) -> None:
    client, projects = sync_api
    _seed_interpolated(client, projects)
    job = _start(
        client, {"OPENROUTER_API_KEY": "k"}, FakeVerify(_verdict("no_match")), audio_verify=True
    )
    stored = [m for m in _stored(client) if m["source_id"] == "vasilieva_01"][0]
    assert stored["status"] == "needs_review"
    assert stored["metadata"]["review_reason"] == "audio_mismatch"
    assert stored["metadata"]["audio_verified"]["heard_text"] == "слышно"
    assert job["audio_rejected"] == 1 and job["needs_review"] >= 1


def test_job_switch_off_never_calls_verifier(sync_api) -> None:
    client, projects = sync_api
    _seed_interpolated(client, projects)
    fake = FakeVerify()
    job = _start(client, {"OPENROUTER_API_KEY": "k"}, fake, audio_verify=False)
    assert fake.calls == [] and job["audio_verified"] == 0


def test_job_default_is_off_for_local_engine_even_with_key(sync_api) -> None:
    client, projects = sync_api
    _seed_interpolated(client, projects)
    fake = FakeVerify()
    _start(client, {"OPENROUTER_API_KEY": "k"}, fake)
    assert fake.calls == []


def test_job_enabled_without_key_warns_and_skips(sync_api) -> None:
    client, projects = sync_api
    _seed_interpolated(client, projects)
    fake = FakeVerify()
    job = _start(client, {}, fake, audio_verify=True)
    assert fake.calls == []
    assert any("ключ" in item["warning"] for item in job["source_warnings"])


def test_job_budget_exhausted_warns_and_leaves_rest(sync_api) -> None:
    client, projects = sync_api
    _seed_interpolated(client, projects)
    fake = FakeVerify(error=CostBudgetExceeded("limit"))
    job = _start(client, {"OPENROUTER_API_KEY": "k"}, fake, audio_verify=True)
    assert job["status"] == "completed"
    assert any("исчерпан бюджет" in item["warning"] for item in job["source_warnings"])
    stored = [m for m in _stored(client) if m["source_id"] == "vasilieva_01"][0]
    assert "audio_verified" not in stored["metadata"]


def test_audio_verify_payload_must_be_boolean(sync_api) -> None:
    client, projects = sync_api
    _seed_interpolated(client, projects)
    response = client.post(
        "/stage2/sync-jobs/start", json={"project": "demo", "audio_verify": "yes"}
    )
    assert response.status_code == 400


def test_selection_uses_same_unreliable_source_rule_as_window_refine(tmp_path: Path) -> None:
    # interpolated cloud index + rejected retime: reloaded from disk it stays unreliable.
    from stage2.sync_transcript import load_word_index
    from stage2.sync_window_refine import unrefined_source_ids

    path = tmp_path / "src.words.json"
    path.write_text(
        json.dumps(
            {
                "source_id": "src",
                "metadata": {
                    "timestamp_precision": "interpolated",
                    "backend_metadata": {"timestamp_precision": "interpolated"},
                },
                "words": [{"word": "а", "start": 0.0, "end": 1.0}],
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "src.retime_attempt.json").write_text('{"status": "rejected"}', encoding="utf-8")
    index = load_word_index(path, "src")
    matches = [
        _match("interp_auto", "auto_matched", 0.99),
        _match("high_other", "auto_matched", 0.99, source_id="other"),
    ]
    assert unrefined_source_ids([index]) == {"src"}
    assert [m.segment_id for m in phase.select_candidates(matches, [index])] == ["interp_auto"]


def test_refine_outputs_reach_audio_verify() -> None:
    # The refine marks ``window_refined`` (success) / review_reason=timing_unreliable
    # (demotion); both must be picked even if the source looks reliable.
    from stage2.sync_window_refine import PRECISION, TIMING_UNRELIABLE

    assert PRECISION == "window_refined"
    matches = [
        _match("refined", "auto_matched", 0.99, source_id="clean", metadata={PRECISION: {"a": 1}}),
        _match(
            "demoted",
            "needs_review",
            0.99,
            source_id="clean",
            metadata={"review_reason": TIMING_UNRELIABLE},
        ),
    ]
    ids = [m.segment_id for m in phase.select_candidates(matches, [_index("clean")])]
    assert ids == ["refined", "demoted"]


def test_job_survives_unexpected_guide_path_error_in_window_refine(sync_api) -> None:
    client, projects = sync_api
    _seed_interpolated(client, projects)

    calls: list[str] = []

    def boom(source, paths):
        calls.append(source.id)
        raise RuntimeError("path resolver exploded")

    with (
        patch.object(api.threading, "Thread", _ImmediateThread),
        patch.object(api, "_resolve_sync_audio_path", boom),
    ):
        response = client.post(
            "/stage2/sync-jobs/start", json={"project": "demo", "audio_verify": False}
        )
        assert response.status_code == 200, response.get_json()
        job = client.get(response.get_json()["result"]["status_url"]).get_json()["result"]
    assert job["status"] == "completed", job
    assert calls  # the refine really asked for the guide audio
