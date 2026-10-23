"""Immutable, content-addressed Stage 3 assembly builds.

Layout (inside ``work/builds``)::

    <build_id>/build.json, assembly_plan.json, timeline.xml, source_mapping.json
    latest.json            pointer to the most recent successful build

``build_id`` is a hash of the build inputs and the mode (never of the clock), so preparing the
same inputs twice yields the same build.  A build directory is published with one ``rename`` of a
fully written temporary directory and is never modified afterwards.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shutil
import tempfile
import threading
import time
from typing import Any, Callable, Iterable
import uuid

from atomic_io import atomic_write_json
from stage2.assembly_manifest import sha256_file
from stage3.nested_xml import MediaSource
from stage_common import _scenario_revision_matches

BUILD_SCHEMA_VERSION = 1
EXPORTER_VERSION = 1
BUILDS_KEEP = 20
BUILD_ID_RE = re.compile(r"[0-9a-f]{16}")
LATEST_FILENAME = "latest.json"
BUILD_FILENAME = "build.json"
PLAN_FILENAME = "assembly_plan.json"
XML_FILENAME = "timeline.xml"
MAPPING_FILENAME = "source_mapping.json"
_TMP_PREFIX = ".tmp-"
_BROKEN_PREFIX = ".broken-"
STALE_DIR_MINUTES = 60
WAV_PROGRESS_EVERY = 10

_logger = logging.getLogger(__name__)
_wav_hash_cache: dict[tuple[str, int, int], str] = {}
_wav_hash_lock = threading.Lock()


class BuildInvariantError(RuntimeError):
    """The same build id was produced with different XML bytes (non-deterministic export)."""


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def compute_build_id(inputs: dict, mode: str) -> str:
    return sha256_text(canonical_json(inputs) + mode)[:16]


def _stat(path: str) -> tuple[int | None, int | None]:
    try:
        stat = Path(path).stat()
    except OSError:
        return None, None
    return stat.st_size, stat.st_mtime_ns


def _cached_wav_sha256(path: str, size: int, mtime_ns: int) -> str | None:
    key = (path, size, mtime_ns)
    with _wav_hash_lock:
        cached = _wav_hash_cache.get(key)
    if cached is not None:
        return cached
    try:
        digest = sha256_file(Path(path))
    except OSError:
        return None
    with _wav_hash_lock:
        _wav_hash_cache[key] = digest
    return digest


def _portable_path(path: str, project_root: Path | None) -> str:
    """Path relative to the project root when inside it, otherwise unchanged (absolute)."""

    if not path or project_root is None:
        return path
    try:
        return Path(path).relative_to(project_root).as_posix()
    except ValueError:
        return path


def wav_inputs(
    plan: dict,
    *,
    project_root: Path | None = None,
    progress: Callable[[int, str], None] | None = None,
) -> list[dict]:
    """Full-content identity of every WAV the plan references (missing files get nulls)."""

    wav_items = [
        (item, str(item["wav"].get("path") or ""))
        for item in plan.get("items") or []
        if isinstance(item, dict) and isinstance(item.get("wav"), dict)
    ]
    wav_items = [(item, path) for item, path in wav_items if path]
    result: list[dict] = []
    for index, (item, path) in enumerate(wav_items, start=1):
        size, mtime_ns = _stat(path)
        digest = (
            _cached_wav_sha256(path, size, mtime_ns)
            if size is not None and mtime_ns is not None
            else None
        )
        result.append(
            {
                "uid": str(item.get("uid") or ""),
                "path": _portable_path(path, project_root),
                "size": size,
                "sha256": digest,
            }
        )
        if progress is not None and index % WAV_PROGRESS_EVERY == 0:
            progress(
                85 + 10 * index // len(wav_items),
                f"Хэширование WAV: {index} из {len(wav_items)}",
            )
    return result


def _entry_value(entry: object, *keys: str) -> object:
    for key in keys:
        value = entry.get(key) if isinstance(entry, dict) else getattr(entry, key, None)
        if value not in (None, ""):
            return value
    return None


def _xml_fields_sha256(source_id: str, entry: object, project_root: Path | None) -> str:
    """Hash of every catalog field the XML exporter reads (paths made portable)."""

    fields = asdict(MediaSource.from_value(source_id, entry))
    for key in ("path", "placeholder_path"):
        fields[key] = _portable_path(str(fields.get(key) or ""), project_root)
    return sha256_text(canonical_json(fields))


def source_inputs(catalog: dict, *, project_root: Path | None = None) -> list[dict]:
    """Identity of every catalog source that reaches the XML (size + mtime + XML fields)."""

    result: list[dict] = []
    for source_id in sorted(catalog):
        entry = catalog[source_id]
        path = str(_entry_value(entry, "path", "container_path", "media_path") or "")
        size, mtime_ns = _stat(path) if path else (None, None)
        result.append(
            {
                "source_id": str(source_id),
                "path": _portable_path(path, project_root),
                "size": size,
                "mtime_ns": mtime_ns,
                "xml_fields_sha256": _xml_fields_sha256(str(source_id), entry, project_root),
            }
        )
    return result


def collect_inputs(
    *,
    timeline_data: dict,
    timeline_sha256: str,
    plan: dict,
    export_catalog: dict,
    unmapped_source_ids: Iterable[str],
    project_root: Path | None = None,
    progress: Callable[[int, str], None] | None = None,
) -> dict:
    """Build inputs; ``export_catalog`` is the exact catalog the XML is generated from."""

    sources = source_inputs(export_catalog, project_root=project_root)
    settings = {
        "fps": timeline_data.get("fps"),
        "width": timeline_data.get("width"),
        "height": timeline_data.get("height"),
        "edit_policy_version": timeline_data.get("edit_policy_version"),
        "exporter_version": EXPORTER_VERSION,
    }
    media_mapping = {
        "mapped": {item["source_id"]: item["path"] for item in sources},
        "unmapped": sorted(str(item) for item in unmapped_source_ids),
    }
    return {
        "scenario_revision_id": str(timeline_data.get("scenario_revision_id") or ""),
        "normalization_revision": timeline_data.get("normalization_revision"),
        "timeline_sha256": timeline_sha256,
        "sync_matches_fingerprint": str(timeline_data.get("sync_matches_fingerprint") or ""),
        "settings_sha256": sha256_text(canonical_json(settings)),
        "wav": wav_inputs(plan, project_root=project_root, progress=progress),
        "sources": sources,
        "media_mapping_sha256": sha256_text(canonical_json(media_mapping)),
    }


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def load_build(builds_dir: Path, build_id: str) -> dict | None:
    """Return build.json of an existing build or None (invalid ids count as missing)."""

    if not BUILD_ID_RE.fullmatch(build_id or ""):
        return None
    return _read_json(builds_dir / build_id / BUILD_FILENAME)


def load_latest_id(builds_dir: Path) -> str | None:
    """Build id from latest.json; None when there is no pointer; ValueError when it is broken."""

    path = builds_dir / LATEST_FILENAME
    if not path.is_file():
        return None
    data = _read_json(path)
    build_id = str(data.get("build_id") or "") if data else ""
    if not BUILD_ID_RE.fullmatch(build_id):
        raise ValueError("invalid builds/latest.json")
    return build_id


def verify_build_xml(builds_dir: Path, build: dict) -> Path:
    """Return the XML path after checking it against build.json; ValueError on mismatch."""

    path = builds_dir / str(build.get("build_id") or "") / XML_FILENAME
    expected = str((build.get("outputs") or {}).get("xml_sha256") or "")
    try:
        actual = sha256_file(path)
    except OSError as exc:
        raise ValueError(f"build XML is unreadable: {exc}") from exc
    if not expected or actual != expected:
        raise ValueError("build XML hash does not match build.json")
    return path


def build_is_current(build: dict, current: dict) -> bool:
    """True when the build still matches ``current`` (a subset of the build inputs).

    Only keys present in ``current`` are compared.  Empty values stored by legacy artifacts do
    not count as a mismatch (same rules as the pre-build download check).
    """

    stored = build.get("inputs") if isinstance(build.get("inputs"), dict) else {}
    if "normalization_revision" in current:
        value = stored.get("normalization_revision")
        if value not in (None, "") and value != current["normalization_revision"]:
            return False
    if "scenario_revision_id" in current and not _scenario_revision_matches(
        stored.get("scenario_revision_id"), current["scenario_revision_id"]
    ):
        return False
    for key in ("sync_matches_fingerprint", "timeline_sha256"):
        if key in current:
            value = str(stored.get(key) or "")
            if value and value != str(current[key] or ""):
                return False
    return True


def list_builds(builds_dir: Path) -> list[dict]:
    """All published builds, newest first."""

    builds: list[dict] = []
    if not builds_dir.is_dir():
        return builds
    for entry in builds_dir.iterdir():
        if not entry.is_dir() or not BUILD_ID_RE.fullmatch(entry.name):
            continue
        data = _read_json(entry / BUILD_FILENAME)
        if data and data.get("build_id") == entry.name:
            builds.append(data)
    builds.sort(key=lambda b: (str(b.get("created_at") or ""), str(b["build_id"])), reverse=True)
    return builds


def rotate_builds(builds_dir: Path, *, keep: int = BUILDS_KEEP) -> list[str]:
    """Delete builds beyond the newest ``keep``; the build latest.json points to is never removed."""

    try:
        protected = load_latest_id(builds_dir)
    except ValueError:
        return []  # a broken pointer: do not guess, delete nothing
    removed: list[str] = []
    for build in list_builds(builds_dir)[keep:]:
        build_id = str(build["build_id"])
        if build_id == protected:
            continue
        shutil.rmtree(builds_dir / build_id, ignore_errors=True)
        removed.append(build_id)
    return removed


def publish_build(
    builds_dir: Path,
    *,
    mode: str,
    complete: bool,
    inputs: dict,
    plan: dict,
    xml_text: str,
    xml_filename: str,
    mapping_payload: dict,
    project_root: str = "",
) -> dict:
    """Write and atomically publish a build directory; returns its build.json.

    latest.json is NOT touched here (see :func:`set_latest`), so a failure leaves the previous
    pointer intact.  An identical existing build is reused as is; a broken one (missing or
    corrupted files, or an XML built under another project root) is replaced.
    """

    build_id = compute_build_id(inputs, mode)
    xml_sha256 = sha256_text(xml_text)
    plan_sha256 = sha256_text(canonical_json(plan))
    build = {
        "schema_version": BUILD_SCHEMA_VERSION,
        "build_id": build_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "mode": mode,
        "complete": complete,
        "project_root": project_root,
        "inputs": inputs,
        "outputs": {
            "plan_sha256": plan_sha256,
            "xml_sha256": xml_sha256,
            "xml_filename": xml_filename,
        },
        "summary": plan.get("summary"),
    }
    builds_dir.mkdir(parents=True, exist_ok=True)
    cleanup_stale_dirs(builds_dir)
    target = builds_dir / build_id
    existing, replace = _inspect_existing(builds_dir, build_id, xml_sha256, project_root)
    if existing is not None:
        return existing

    tmp = Path(tempfile.mkdtemp(prefix=_TMP_PREFIX, dir=builds_dir))
    broken: Path | None = None
    try:
        (tmp / XML_FILENAME).write_text(xml_text, encoding="utf-8")
        atomic_write_json(tmp / PLAN_FILENAME, plan)
        atomic_write_json(tmp / MAPPING_FILENAME, mapping_payload)
        atomic_write_json(tmp / BUILD_FILENAME, build)
        if sha256_file(tmp / XML_FILENAME) != xml_sha256:
            raise OSError("build XML hash changed while it was being written")
        os.chmod(tmp, 0o755)  # mkdtemp creates 0o700; published builds are shared read-only
        if replace:
            broken = builds_dir / f"{_BROKEN_PREFIX}{build_id}-{uuid.uuid4().hex[:8]}"
            os.rename(target, broken)
        try:
            os.rename(tmp, target)
        except OSError:
            if broken is not None and not target.exists():
                os.rename(broken, target)  # put the previous directory back
                broken = None
            # A concurrent publisher may have created the same build meanwhile.
            existing, _ = _inspect_existing(builds_dir, build_id, xml_sha256, project_root)
            if existing is None:
                raise
            return existing
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        if broken is not None:
            shutil.rmtree(broken, ignore_errors=True)
    return build


def _inspect_existing(
    builds_dir: Path, build_id: str, xml_sha256: str, project_root: str
) -> tuple[dict | None, bool]:
    """Return ``(reusable build, False)``, ``(None, True)`` to replace a broken/foreign build,
    or ``(None, False)`` when there is no build.  Raises BuildInvariantError when an intact
    build of the same project holds a different XML.
    """

    target = builds_dir / build_id
    if not target.exists():
        return None, False
    existing = _read_json(target / BUILD_FILENAME)
    try:
        actual = sha256_file(target / XML_FILENAME)
    except OSError:
        return None, True
    recorded = str(((existing or {}).get("outputs") or {}).get("xml_sha256") or "")
    if existing is None or existing.get("build_id") != build_id or actual != recorded:
        return None, True  # unreadable build.json or the stored XML was damaged
    if actual == xml_sha256:
        return existing, False
    stored_root = str(existing.get("project_root") or "")
    if project_root and stored_root != project_root:
        return None, True  # same inputs, but the XML embeds paths of another project location
    raise BuildInvariantError(
        f"build {build_id} already exists with a different XML: "
        "the XML export is not deterministic for identical inputs "
        f"(stored {actual[:12]}, new {xml_sha256[:12]})"
    )


def cleanup_stale_dirs(builds_dir: Path, *, max_age_minutes: int = STALE_DIR_MINUTES) -> None:
    """Remove abandoned ``.tmp-*`` / ``.broken-*`` directories older than ``max_age_minutes``."""

    if not builds_dir.is_dir():
        return
    cutoff = time.time() - max_age_minutes * 60
    for entry in builds_dir.iterdir():
        if not entry.name.startswith((_TMP_PREFIX, _BROKEN_PREFIX)) or not entry.is_dir():
            continue
        try:
            if entry.stat().st_mtime < cutoff:
                shutil.rmtree(entry, ignore_errors=True)
        except OSError:
            continue


def set_latest(builds_dir: Path, build_id: str) -> None:
    atomic_write_json(
        builds_dir / LATEST_FILENAME,
        {"build_id": build_id, "updated_at": datetime.now(timezone.utc).isoformat()},
    )


def safe_rotate(builds_dir: Path, *, keep: int | None = None) -> None:
    try:
        cleanup_stale_dirs(builds_dir)
        rotate_builds(builds_dir, keep=BUILDS_KEEP if keep is None else keep)
    except OSError:
        _logger.warning("Stage 3 build rotation failed in %s", builds_dir, exc_info=True)
