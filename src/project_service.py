from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Callable


PROJECT_NAME_RE = re.compile(r"^[A-Za-zА-Яа-яЁё0-9_-]{1,64}$")


def normalize_project_name(name: str) -> str:
    project = (name or "").strip()
    if not PROJECT_NAME_RE.fullmatch(project):
        raise ValueError("project may contain 1-64 Latin/Cyrillic letters, digits, '_' or '-'")
    return project


def project_paths(project: str, projects_dir: Path) -> dict[str, Path]:
    root = projects_dir / project
    inbox = root / "ЗАГРУЗИТЬ"
    work = root / "work"
    media = root / "media"
    versions = root / "versions"
    return {
        "root": root,
        "inbox": inbox,
        "inbox_scenarios": inbox / "Сценарии",
        "inbox_reviews": inbox / "Просмотровки",
        "input": root / "input",
        "work": work,
        "speaker": root / "speaker",
        "voiceover_assets": root / "speaker" / "assets",
        "media": media,
        "media_reviews": media / "reviews",
        "media_archive": media / "archive",
        "media_films": media / "films",
        "media_photos": media / "photos",
        "media_graphics": media / "graphics",
        "versions": versions,
        "scenario_versions": versions / "scenarios",
        "scenario_diffs": versions / "diffs",
        "timeline_versions": versions / "timelines",
        "normalized": work / "scenario_normalized.json",
        "prepared": work / "scenario_prepared.json",
        "raw": work / "scenario_raw.txt",
        "sync_sources": work / "sync_sources.json",
        "sync_matches": work / "sync_matches.json",
        "sync_job_status": work / "sync_job_status.json",
        "final_review_audio": work / "final_review.wav",
        "final_review_report": work / "final_review_report.json",
        "final_review_words": work / "final_review.words.json",
        "final_review_job_status": work / "final_review_job_status.json",
        "assembly_manifest": work / "assembly_manifest.json",
        "final_review_iterations": work / "final_review_iterations.json",
        "normalization_job_status": work / "normalization_job_status.json",
        "tts_job_status": work / "tts_job_status.json",
        "openrouter_usage": work / "openrouter_usage.json",
        "active_voiceover_manifest": work / "voiceover_manifest.json",
        "sync_audio": work / "sync_audio",
        "sync_transcripts": work / "transcripts",
        "timeline": work / "full_timeline.json",
    }


def ensure_project(project: str, projects_dir: Path) -> dict[str, Path]:
    paths = project_paths(project, projects_dir)
    directory_keys = (
        "inbox_scenarios",
        "inbox_reviews",
        "input",
        "work",
        "speaker",
        "voiceover_assets",
        "sync_audio",
        "sync_transcripts",
        "media_reviews",
        "media_archive",
        "media_films",
        "media_photos",
        "media_graphics",
        "scenario_versions",
        "scenario_diffs",
        "timeline_versions",
    )
    for key in directory_keys:
        paths[key].mkdir(parents=True, exist_ok=True)
    return paths


def list_projects(projects_dir: Path) -> list[str]:
    if not projects_dir.exists():
        return []
    return sorted(
        path.name
        for path in projects_dir.iterdir()
        if path.is_dir() and PROJECT_NAME_RE.fullmatch(path.name)
    )


def read_json_safe(
    path: Path,
    *,
    on_error: Callable[[Path, Exception], None] | None = None,
) -> dict:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        if on_error is not None:
            on_error(path, exc)
        return {}
    return payload if isinstance(payload, dict) else {}


def discover_input_files(input_dir: Path, root_dir: Path) -> list[str]:
    if not input_dir.exists():
        return []
    return [
        str(path.relative_to(root_dir))
        for path in sorted(input_dir.iterdir())
        if path.is_file() and path.suffix.lower() in {".doc", ".docx", ".txt", ".md"}
    ]


def resolve_project_name(raw: str | None, projects_dir: Path) -> str:
    if raw:
        return normalize_project_name(raw)
    projects = list_projects(projects_dir)
    return projects[0] if projects else "default"


def resolve_path(
    value: str,
    fallback: Path,
    *,
    root_dir: Path,
    allowed_root: Path,
) -> Path:
    if not value:
        return fallback
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = root_dir / candidate
    resolved = candidate.resolve()
    if not resolved.is_relative_to(allowed_root):
        raise ValueError(f"Path must be inside {allowed_root.name}/")
    return resolved
