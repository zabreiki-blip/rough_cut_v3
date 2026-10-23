#!/usr/bin/env python3
"""Оценка аудио-проверки синхрона (sync_audio_verify) на текущих матчах проекта.

Только оценка: решения никак не применяются к матчам, projects/* читается.

Наборы:
  P — confirmed (ручные) + auto_matched с confidence >= 0.95 (позитивы);
  N — синтетические негативы: тот же источник и цитата, диапазон той же длины,
      сдвинутый минимум на 60 с от диапазона цитаты (детерминированный seed);
  R — needs_review (реальная зона проверки).

Метрика: precision "match" = P_match / (P_match + N_match).

Примеры:
  .venv/bin/python scripts/eval_sync_audio_verify.py --project BRIK --dry-run
  .venv/bin/python scripts/eval_sync_audio_verify.py --project BRIK --max-usd 1.0 \
      --out /tmp/rc_eval/BRIK.json
Ключ берётся из окружения (SYNC_CLOUD_ASR_API_KEY / OPENROUTER_API_KEY).
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import tempfile
import wave
from collections import Counter
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from cost_budget import CostBudget, CostBudgetExceeded  # noqa: E402
from project_service import normalize_project_name, project_paths  # noqa: E402
from stage1.normalizer import load_normalization  # noqa: E402
from stage2 import sync_audio_verify as sav  # noqa: E402
from stage2.sync_storage import SyncMatchStore, SyncSourceStore  # noqa: E402
from stage_common import current_sync_matches  # noqa: E402

AUTO_CONFIDENCE_MIN = 0.95
NEGATIVE_MIN_GAP_SEC = 60.0
DEFAULT_SEED = 20261023
DEFAULT_NEG_RATIO = 0.5
# Запасная оценка $/мин, если ROUGH_CUT_CLOUD_ASR_USD_PER_MINUTE_ESTIMATE не задана
# (по факту прогона BRIK: ~$0.0032/мин на аудио-модели облачного ASR).
FALLBACK_USD_PER_MINUTE = Decimal("0.0035")


@dataclass
class Fragment:
    set_name: str  # "P" | "N" | "R"
    segment_id: str
    source_id: str
    audio_path: str
    start_sec: float
    end_sec: float
    quote: str
    speaker: str
    matcher_confidence: float
    matcher_status: str

    @property
    def duration_sec(self) -> float:
        return self.end_sec - self.start_sec


def map_container_path(value: str, repo_root: Path = REPO_ROOT) -> Path:
    """Путь контейнера (/app/...) -> корень репозитория на хосте."""
    path = Path(value)
    if path.is_absolute():
        try:
            return repo_root / path.relative_to("/app")
        except ValueError:
            return path
    return path


def resolve_audio_path(source: Any, paths: dict[str, Path], repo_root: Path = REPO_ROOT) -> Path:
    """Guide WAV источника: канонический sync_audio/<id>.wav, иначе guide_audio."""
    canonical = paths["sync_audio"] / f"{source.id}.wav"
    if canonical.is_file():
        return canonical
    value = str(source.guide_audio or "").strip()
    if not value:
        return canonical
    configured = map_container_path(value, repo_root)
    candidates = (
        [configured] if configured.is_absolute() else [paths["root"] / configured, paths["work"] / configured]
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return candidates[0]


def audio_duration_sec(path: Path, fallback: float = 0.0) -> float:
    try:
        with wave.open(str(path), "rb") as stream:
            return stream.getnframes() / float(stream.getframerate())
    except (OSError, EOFError, wave.Error, ZeroDivisionError):
        return fallback


def negative_range(
    *,
    seed: int,
    segment_id: str,
    pos_start: float,
    pos_end: float,
    source_duration: float,
    min_gap_sec: float = NEGATIVE_MIN_GAP_SEC,
) -> tuple[float, float] | None:
    """Диапазон той же длины без пересечения с речью цитаты (зазор >= min_gap_sec).

    Детерминирован для (seed, segment_id). None — если места в источнике нет.
    """
    length = pos_end - pos_start
    if length <= 0:
        return None
    zones: list[tuple[float, float]] = []  # допустимые интервалы для НАЧАЛА диапазона
    left_hi = pos_start - min_gap_sec - length
    if left_hi >= 0:
        zones.append((0.0, left_hi))
    right_lo = pos_end + min_gap_sec
    right_hi = source_duration - length
    if right_hi >= right_lo:
        zones.append((right_lo, right_hi))
    if not zones:
        return None
    rng = random.Random(f"{seed}:{segment_id}")
    widths = [hi - lo for lo, hi in zones]
    lo, hi = rng.choices(zones, weights=[w + 1e-6 for w in widths], k=1)[0]
    start = round(rng.uniform(lo, hi), 3)
    return start, round(start + length, 3)


def collect_fragments(
    matches: list[Any],
    sources: dict[str, Any],
    paths: dict[str, Path],
    *,
    seed: int = DEFAULT_SEED,
    neg_ratio: float = DEFAULT_NEG_RATIO,
    repo_root: Path = REPO_ROOT,
) -> list[Fragment]:
    fragments: list[Fragment] = []
    rng_select = random.Random(f"{seed}:select")
    durations: dict[str, float] = {}
    for match in sorted(matches, key=lambda item: item.segment_id):
        if match.source_in_sec is None or match.source_out_sec is None:
            continue
        source = sources.get(match.source_id)
        if source is None:
            continue
        positive = match.status == "confirmed" or (
            match.status == "auto_matched" and match.confidence >= AUTO_CONFIDENCE_MIN
        )
        review = match.status == "needs_review"
        if not (positive or review):
            continue
        audio_path = resolve_audio_path(source, paths, repo_root)

        def fragment(set_name: str, start: float, end: float) -> Fragment:
            return Fragment(
                set_name=set_name,
                segment_id=match.segment_id,
                source_id=match.source_id,
                audio_path=str(audio_path),
                start_sec=start,
                end_sec=end,
                quote=match.query_text,
                speaker=match.speaker,
                matcher_confidence=match.confidence,
                matcher_status=match.status,
            )

        fragments.append(
            fragment("P" if positive else "R", match.source_in_sec, match.source_out_sec)
        )
        if positive and rng_select.random() < neg_ratio:
            if match.source_id not in durations:
                durations[match.source_id] = audio_duration_sec(
                    audio_path, float(source.duration_sec or 0.0)
                ) or float(source.duration_sec or 0.0)
            neg = negative_range(
                seed=seed,
                segment_id=match.segment_id,
                pos_start=match.source_in_sec,
                pos_end=match.source_out_sec,
                source_duration=durations[match.source_id],
            )
            if neg is not None:
                fragments.append(fragment("N", neg[0], neg[1]))
    return fragments


def plan_summary(
    fragments: list[Fragment], usd_per_minute: Decimal, margin_sec: float = sav.DEFAULT_MARGIN_SEC
) -> dict[str, Any]:
    by_set: dict[str, dict[str, Any]] = {}
    total_sec = 0.0
    for name in ("P", "N", "R"):
        subset = [item for item in fragments if item.set_name == name]
        seconds = sum(item.duration_sec + 2 * margin_sec for item in subset)
        total_sec += seconds
        by_set[name] = {"fragments": len(subset), "duration_sec": round(seconds, 1)}
    return {
        "fragments": len(fragments),
        "duration_sec": round(total_sec, 1),
        "usd_per_minute": str(usd_per_minute),
        "estimated_cost_usd": float(Decimal(str(total_sec)) * usd_per_minute / Decimal(60)),
        "by_set": by_set,
    }


def limit_fragments(fragments: list[Fragment], limit: int | None) -> list[Fragment]:
    """Не более `limit` фрагментов на каждый набор; порядок — по кругу P, N, R."""
    queues = {name: [f for f in fragments if f.set_name == name] for name in ("P", "N", "R")}
    if limit is not None:
        queues = {name: items[:limit] for name, items in queues.items()}
    ordered: list[Fragment] = []
    while any(queues.values()):
        for name in ("R", "N", "P"):
            if queues[name]:
                ordered.append(queues[name].pop(0))
    return ordered


def _share(counter: Counter, total: int, key: str) -> float:
    return round(counter.get(key, 0) / total, 4) if total else 0.0


def summarize_results(rows: list[dict[str, Any]]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for name in ("P", "N", "R"):
        subset = [row for row in rows if row["set"] == name]
        counts = Counter(row["verdict"] for row in subset)
        summary[name] = {"n": len(subset), "verdicts": dict(counts)}
        if name == "P":
            for key in ("match", "partial", "no_match"):
                summary[name][f"{key}_share"] = _share(counts, len(subset), key)
        elif name == "N":
            summary[name]["false_accept_share"] = _share(counts, len(subset), "match")
    p_match = summary["P"]["verdicts"].get("match", 0)
    n_match = summary["N"]["verdicts"].get("match", 0)
    summary["precision_match"] = round(p_match / (p_match + n_match), 4) if p_match + n_match else None
    summary["review_items"] = [
        {
            "segment_id": row["segment_id"],
            "matcher_confidence": row["matcher_confidence"],
            "verdict": row["verdict"],
            "coverage": row["coverage"],
            "heard_text": row["heard_text"],
            "quote": row["quote"],
        }
        for row in rows
        if row["set"] == "R"
    ]
    return summary


def run_eval(
    fragments: list[Fragment],
    *,
    budget: CostBudget,
    cache_dir: Path,
    request_fn: Any = None,
    settings: Any = None,
) -> tuple[list[dict[str, Any]], bool]:
    rows: list[dict[str, Any]] = []
    exhausted = False
    for item in fragments:
        try:
            verdict = sav.verify_clip(
                audio_path=item.audio_path,
                start_sec=item.start_sec,
                end_sec=item.end_sec,
                quote=item.quote,
                speaker=item.speaker,
                settings=settings,
                request_fn=request_fn,
                budget=budget,
                cache_dir=cache_dir,
            )
        except CostBudgetExceeded:
            exhausted = True
            break
        row = {"set": item.set_name, **asdict(item), **verdict.to_dict()}
        row.pop("set_name", None)
        rows.append(row)
    return rows, exhausted


def _default_out() -> Path:
    return Path(tempfile.gettempdir()) / "rough_cut_sync_audio_verify" / "eval.json"


def _check_out_outside_repo(path: Path) -> None:
    resolved = path.resolve()
    if resolved.is_relative_to(REPO_ROOT.resolve()):
        raise SystemExit(f"--out must be outside the repository (projects/): {path}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--project", required=True)
    parser.add_argument("--max-usd", type=Decimal, default=Decimal("1.0"))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=None, help="максимум фрагментов на набор")
    parser.add_argument("--out", type=Path, default=None, help="файл отчёта JSON (вне репозитория)")
    parser.add_argument("--cache-dir", type=Path, default=None)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--neg-ratio", type=float, default=DEFAULT_NEG_RATIO)
    parser.add_argument("--usd-per-minute", type=Decimal, default=None)
    args = parser.parse_args(argv)

    out = args.out or _default_out()
    _check_out_outside_repo(out)
    cache_dir = args.cache_dir or out.parent / "cache"

    project = normalize_project_name(args.project)
    paths = project_paths(project, REPO_ROOT / "projects")
    normalization = load_normalization(paths["normalized"])
    matches, _stale = current_sync_matches(SyncMatchStore(paths["sync_matches"]).list(), normalization)
    sources = {item.id: item for item in SyncSourceStore(paths["sync_sources"]).list()}

    fragments = limit_fragments(
        collect_fragments(matches, sources, paths, seed=args.seed, neg_ratio=args.neg_ratio),
        args.limit,
    )
    env_rate = Decimal(os.getenv("ROUGH_CUT_CLOUD_ASR_USD_PER_MINUTE_ESTIMATE", "0") or "0")
    rate = args.usd_per_minute or (env_rate if env_rate > 0 else FALLBACK_USD_PER_MINUTE)
    plan = plan_summary(fragments, rate)

    if args.dry_run:
        print(json.dumps({"project": project, "dry_run": True, **plan}, ensure_ascii=False, indent=2))
        return 0

    budget = CostBudget(args.max_usd, rate)
    rows, exhausted = run_eval(fragments, budget=budget, cache_dir=cache_dir)
    snapshot = budget.snapshot()
    report = {
        "project": project,
        "model": sav.resolve_model(),
        "prompt_version": sav.PROMPT_VERSION,
        "plan": plan,
        "budget_exhausted": exhausted,
        "cost_usd": snapshot["spent_usd"],
        "cost_pending": budget.has_pending_reservations,
        "summary": summarize_results(rows),
        "items": rows,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    brief = {k: v for k, v in report["summary"].items() if k != "review_items"}
    print(
        json.dumps(
            {"report": str(out), "done": len(rows), "of": len(fragments),
             "budget_exhausted": exhausted, "cost_usd": report["cost_usd"], **brief},
            ensure_ascii=False, indent=2,
        )
    )  # fmt: skip
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
