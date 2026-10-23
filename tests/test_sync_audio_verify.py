from __future__ import annotations

import base64
import importlib.util
import json
import shutil
import struct
import sys
import wave
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from cost_budget import CostBudget, CostBudgetExceeded
from stage2 import sync_audio_verify as sav

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is required")

_SPEC = importlib.util.spec_from_file_location(
    "eval_sync_audio_verify",
    Path(__file__).resolve().parent.parent / "scripts" / "eval_sync_audio_verify.py",
)
evalmod = importlib.util.module_from_spec(_SPEC)
sys.modules["eval_sync_audio_verify"] = evalmod
_SPEC.loader.exec_module(evalmod)


def _write_wav(path: Path, seconds: float, rate: int = 16000) -> Path:
    frames = b"".join(struct.pack("<h", (i * 37) % 2000 - 1000) for i in range(int(seconds * rate)))
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(rate)
        stream.writeframes(frames)
    return path


class FakeRequest:
    def __init__(self, content: str | None = None, cost: float | None = 0.002, error=None):
        self.content = content or json.dumps(
            {"verdict": "match", "coverage": 0.9, "heard_text": "слышу цитату", "confidence": 0.8}
        )
        self.cost = cost
        self.error = error
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.content, self.cost, {"id": "gen-1"}


def _verify(wav: Path, request: FakeRequest, **kwargs):
    params = dict(
        audio_path=wav,
        start_sec=1.0,
        end_sec=3.0,
        quote="Цитата из сценария",
        speaker="Иванов",
        settings={"SYNC_AUDIO_VERIFY_MODEL": "test/audio-model"},
        request_fn=request,
    )
    params.update(kwargs)
    return sav.verify_clip(**params)


def test_request_has_audio_prompt_and_json_is_parsed(tmp_path):
    wav = _write_wav(tmp_path / "a.wav", 6)
    request = FakeRequest()
    usage: list[dict] = []
    result = _verify(wav, request, usage_callback=usage.append)

    assert result.verdict == "match"
    assert result.coverage == 0.9 and result.confidence == 0.8
    assert result.heard_text == "слышу цитату"
    assert result.model == "test/audio-model" and result.cost_usd == 0.002
    (call,) = request.calls
    assert call["audio_format"] == "mp3" and call["model"] == "test/audio-model"
    audio = base64.b64decode(call["audio_b64"])
    assert audio[:3] == b"ID3" or audio[0] == 0xFF  # mp3
    assert "Цитата из сценария" in call["prompt"] and "Иванов" in call["prompt"]
    assert "JSON" in call["prompt"]
    # 2 c фрагмента + 2 * 0.5 c поля
    assert call["duration_sec"] == pytest.approx(3.0, abs=0.05)
    assert usage == [{"model": "test/audio-model", "cost_usd": 0.002, "generation_id": "gen-1"}]


def test_fenced_json_and_clamping(tmp_path):
    wav = _write_wav(tmp_path / "a.wav", 6)
    content = '```json\n{"verdict": "Partial", "coverage": 3, "heard_text": "x"}\n```'
    result = _verify(wav, FakeRequest(content))
    assert (result.verdict, result.coverage, result.confidence) == ("partial", 1.0, 0.0)


def test_cache_hit_skips_request_and_key_depends_on_quote(tmp_path):
    wav = _write_wav(tmp_path / "a.wav", 6)
    cache = tmp_path / "cache"
    first_request = FakeRequest()
    first = _verify(wav, first_request, cache_dir=cache)
    second_request = FakeRequest()
    second = _verify(wav, second_request, cache_dir=cache)

    assert not first.cached and second.cached
    assert second_request.calls == []
    assert second.verdict == first.verdict and second.heard_text == first.heard_text
    other = FakeRequest()
    _verify(wav, other, cache_dir=cache, quote="Другая цитата")
    assert len(other.calls) == 1


@pytest.mark.parametrize(
    "request_kwargs",
    [
        {"error": RuntimeError("network down")},
    ],
)
def test_errors_become_error_verdict_and_are_not_cached(tmp_path, request_kwargs):
    wav = _write_wav(tmp_path / "a.wav", 6)
    cache = tmp_path / "cache"
    result = _verify(wav, FakeRequest(**request_kwargs), cache_dir=cache)
    assert result.verdict == "error" and result.reason
    assert not list(cache.glob("*.json")) if cache.exists() else True


def test_missing_audio_is_error_verdict(tmp_path):
    result = _verify(tmp_path / "missing.wav", FakeRequest())
    assert result.verdict == "error" and "extract" in result.reason


def test_budget_is_reserved_and_settled(tmp_path):
    wav = _write_wav(tmp_path / "a.wav", 6)
    budget = CostBudget(Decimal("1"), Decimal("0.0035"))
    seen: dict = {}

    def request(**kwargs):
        seen["reserved"] = budget.snapshot()["reserved_usd"]
        return FakeRequest()(**kwargs)

    _verify(wav, request, budget=budget)
    assert seen["reserved"] > 0
    snapshot = budget.snapshot()
    assert snapshot["reserved_usd"] == 0 and snapshot["spent_usd"] == pytest.approx(0.002)


def test_budget_exhaustion_propagates(tmp_path):
    wav = _write_wav(tmp_path / "a.wav", 6)
    budget = CostBudget(Decimal("0.0000001"), Decimal("1"))
    request = FakeRequest()
    with pytest.raises(CostBudgetExceeded):
        _verify(wav, request, budget=budget)
    assert request.calls == []


def test_unknown_cost_reserves_and_books_the_estimate(tmp_path):
    wav = _write_wav(tmp_path / "a.wav", 6)
    budget = CostBudget.for_llm_tts({"ROUGH_CUT_MAX_USD_PER_JOB": "1"})  # ASR rate is zero
    seen: dict = {}

    def request(**kwargs):
        seen["reserved"] = budget.snapshot()["reserved_usd"]
        return FakeRequest(cost=None)(**kwargs)

    result = _verify(wav, request, budget=budget)
    assert seen["reserved"] > 0  # not the zero ASR estimate of the LLM/TTS budget
    assert result.cost_estimated and result.cost_usd == pytest.approx(seen["reserved"])
    snapshot = budget.snapshot()
    assert snapshot["reserved_usd"] == 0
    assert snapshot["spent_usd"] == pytest.approx(result.cost_usd)
    assert snapshot["estimated_spent_usd"] == pytest.approx(result.cost_usd)


def test_estimate_rate_uses_env_or_doubled_default():
    assert sav.clip_usd_per_minute({}) == Decimal("0.007")
    assert sav.clip_usd_per_minute({sav.RATE_ENV: "0.02"}) == Decimal("0.02")
    assert sav.clip_usd_per_minute({sav.RATE_ENV: "junk"}) == Decimal("0.007")


def test_request_exception_after_send_books_estimate_and_clears_reservation(tmp_path):
    wav = _write_wav(tmp_path / "a.wav", 6)
    budget = CostBudget.for_llm_tts({})
    result = _verify(wav, FakeRequest(error=RuntimeError("read timeout")), budget=budget)
    assert result.verdict == "error" and result.cost_estimated and result.cost_usd > 0
    snapshot = budget.snapshot()
    assert snapshot["reserved_usd"] == 0
    assert snapshot["spent_usd"] == pytest.approx(result.cost_usd)


def test_unsent_request_is_released_without_cost(tmp_path):
    import httpx

    wav = _write_wav(tmp_path / "a.wav", 6)
    budget = CostBudget.for_llm_tts({})
    result = _verify(wav, FakeRequest(error=httpx.ConnectError("refused")), budget=budget)
    assert result.verdict == "error" and not result.cost_usd
    assert budget.snapshot()["reserved_usd"] == 0 and budget.snapshot()["spent_usd"] == 0


def test_cancellation_after_send_settles_the_reservation(tmp_path):
    wav = _write_wav(tmp_path / "a.wav", 6)
    budget = CostBudget.for_llm_tts({})
    with pytest.raises(KeyboardInterrupt):
        _verify(wav, FakeRequest(error=KeyboardInterrupt()), budget=budget)
    assert budget.snapshot()["reserved_usd"] == 0


def test_parse_error_is_cached_as_paid_error_and_not_paid_twice(tmp_path):
    wav = _write_wav(tmp_path / "a.wav", 6)
    cache = tmp_path / "cache"
    first_request = FakeRequest(content="не JSON вообще")
    first = _verify(wav, first_request, cache_dir=cache)
    assert first.verdict == "error" and first.reason.startswith("parse:") and first.paid
    assert first.cost_usd == 0.002
    second_request = FakeRequest()
    second = _verify(wav, second_request, cache_dir=cache)
    assert second.verdict == "error" and second.cached and second_request.calls == []
    # an explicit flag pays again and replaces the cached error
    retry_request = FakeRequest()
    retried = _verify(wav, retry_request, cache_dir=cache, retry_parse_errors=True)
    assert retried.verdict == "match" and len(retry_request.calls) == 1
    again = _verify(wav, FakeRequest(content="x"), cache_dir=cache)
    assert again.verdict == "match" and again.cached


def test_cache_write_uses_unique_tmp_and_survives_parallel_writers(tmp_path):
    import threading

    path = tmp_path / "cache" / "k.json"
    errors: list[BaseException] = []

    def write() -> None:
        try:
            for _ in range(30):
                sav._write_cache(path, sav.AudioVerdict("match", coverage=1.0))
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=write) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors
    assert json.loads(path.read_text(encoding="utf-8"))["verdict"] == "match"
    assert [p.name for p in path.parent.iterdir()] == ["k.json"]


# --- eval script -----------------------------------------------------------


def test_negative_range_is_deterministic_and_far_from_speech():
    for index in range(200):
        seg = f"seg-{index}"
        args = dict(seed=7, segment_id=seg, pos_start=100.0, pos_end=112.0, source_duration=400.0)
        first = evalmod.negative_range(**args)
        assert first == evalmod.negative_range(**args)
        assert first is not None
        start, end = first
        assert end - start == pytest.approx(12.0)
        assert 0 <= start and end <= 400.0
        assert end <= 100.0 - 60.0 or start >= 112.0 + 60.0


def test_negative_range_none_when_no_room():
    assert (
        evalmod.negative_range(
            seed=1, segment_id="s", pos_start=10.0, pos_end=20.0, source_duration=80.0
        )
        is None
    )


def test_map_container_path(tmp_path):
    assert evalmod.map_container_path("/app/projects/BRIK/a.wav", tmp_path) == (
        tmp_path / "projects/BRIK/a.wav"
    )
    assert evalmod.map_container_path("/other/a.wav", tmp_path) == Path("/other/a.wav")


def _fixture_project(tmp_path):
    root = tmp_path / "projects" / "P"
    work = root / "work"
    (work / "sync_audio").mkdir(parents=True)
    _write_wav(work / "sync_audio" / "src1.wav", 400, rate=1000)
    paths = {"root": root, "work": work, "sync_audio": work / "sync_audio"}
    source = SimpleNamespace(id="src1", guide_audio="", duration_sec=400.0)

    def match(segment_id, status, confidence, start, end):
        return SimpleNamespace(
            segment_id=segment_id,
            status=status,
            confidence=confidence,
            source_id="src1",
            source_in_sec=start,
            source_out_sec=end,
            query_text=f"цитата {segment_id}",
            speaker="Иванов",
        )

    matches = [
        match("a", "confirmed", 1.0, 100.0, 110.0),
        match("b", "auto_matched", 0.97, 200.0, 210.0),
        match("c", "auto_matched", 0.90, 220.0, 230.0),  # ни P, ни R
        match("d", "needs_review", 0.6, 300.0, 308.0),
        match("e", "not_found", 0.0, 0.0, 0.0),
    ]
    return matches, {"src1": source}, paths


def test_collect_fragments_sets_and_negatives(tmp_path):
    matches, sources, paths = _fixture_project(tmp_path)
    fragments = evalmod.collect_fragments(matches, sources, paths, seed=3, neg_ratio=1.0)
    again = evalmod.collect_fragments(matches, sources, paths, seed=3, neg_ratio=1.0)
    assert fragments == again

    by_set = {name: [f for f in fragments if f.set_name == name] for name in "PNR"}
    assert {f.segment_id for f in by_set["P"]} == {"a", "b"}
    assert {f.segment_id for f in by_set["R"]} == {"d"}
    assert {f.segment_id for f in by_set["N"]} == {"a", "b"}
    positives = {f.segment_id: f for f in by_set["P"]}
    for neg in by_set["N"]:
        pos = positives[neg.segment_id]
        assert neg.quote == pos.quote and neg.audio_path == pos.audio_path
        assert neg.duration_sec == pytest.approx(pos.duration_sec)
        assert neg.end_sec <= pos.start_sec - 60 or neg.start_sec >= pos.end_sec + 60


def test_dry_run_counts_duration_and_cost_without_network(tmp_path, monkeypatch, capsys):
    matches, sources, paths = _fixture_project(tmp_path)
    fragments = evalmod.collect_fragments(matches, sources, paths, seed=3, neg_ratio=1.0)
    plan = evalmod.plan_summary(fragments, Decimal("0.006"), margin_sec=0.5)
    # 5 фрагментов: 10 + 10 + 10 + 10 + 8 c + по 1 c полей
    assert plan["fragments"] == 5
    assert plan["duration_sec"] == pytest.approx(48 + 5.0)
    assert plan["estimated_cost_usd"] == pytest.approx(53.0 / 60 * 0.006)
    assert plan["by_set"]["R"]["fragments"] == 1

    def boom(*_a, **_k):
        raise AssertionError("network/model call in dry-run")

    monkeypatch.setattr(sav, "verify_clip", boom)
    monkeypatch.setattr(sav, "make_default_request_fn", boom)
    monkeypatch.setattr(evalmod, "SyncMatchStore", lambda _p: SimpleNamespace(list=lambda: matches))
    monkeypatch.setattr(evalmod, "SyncSourceStore", lambda _p: SimpleNamespace(list=lambda: list(sources.values())))
    monkeypatch.setattr(evalmod, "load_normalization", lambda _p: {})
    monkeypatch.setattr(evalmod, "current_sync_matches", lambda m, _n: (m, []))
    full_paths = {**paths, "normalized": None, "sync_matches": None, "sync_sources": None}
    monkeypatch.setattr(evalmod, "project_paths", lambda _n, _d: full_paths)
    assert evalmod.main(["--project", "P", "--dry-run", "--usd-per-minute", "0.006"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["dry_run"] is True and printed["fragments"] == 5


def test_run_eval_summary_and_budget_stop(tmp_path):
    matches, sources, paths = _fixture_project(tmp_path)
    fragments = evalmod.limit_fragments(
        evalmod.collect_fragments(matches, sources, paths, seed=3, neg_ratio=1.0), None
    )
    assert fragments[0].set_name == "R"  # круговой порядок R, N, P

    def request(**kwargs):
        return FakeRequest()(**kwargs)

    budget = CostBudget(Decimal("5"), Decimal("0.006"))
    rows, exhausted = evalmod.run_eval(
        fragments, budget=budget, cache_dir=tmp_path / "cache", request_fn=request,
        settings={"SYNC_AUDIO_VERIFY_MODEL": "m"},
    )  # fmt: skip
    assert not exhausted and len(rows) == 5
    summary = evalmod.summarize_results(rows)
    assert summary["P"]["match_share"] == 1.0 and summary["N"]["false_accept_share"] == 1.0
    assert summary["precision_match"] == 0.5
    assert summary["review_items"][0]["segment_id"] == "d"

    tiny = CostBudget(Decimal("0.0000001"), Decimal("1"))
    rows, exhausted = evalmod.run_eval(
        fragments, budget=tiny, cache_dir=tmp_path / "cache2", request_fn=request,
        settings={"SYNC_AUDIO_VERIFY_MODEL": "m"},
    )  # fmt: skip
    assert exhausted and rows == []
