from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable, Literal
import unicodedata

from atomic_io import atomic_write_json


SCENARIO_EXTENSIONS = frozenset({".doc", ".docx", ".md", ".txt"})
VIDEO_EXTENSIONS = frozenset(
    {
        ".3gp",
        ".avi",
        ".m2ts",
        ".m4v",
        ".mkv",
        ".mov",
        ".mp4",
        ".mpeg",
        ".mpg",
        ".mts",
        ".mxf",
        ".webm",
    }
)
AUDIO_EXTENSIONS = frozenset({".mp3", ".wav"})
REVIEW_EXTENSIONS = VIDEO_EXTENSIONS | AUDIO_EXTENSIONS

VOICEOVER_EXTENSIONS = AUDIO_EXTENSIONS

InboxKind = Literal["scenario", "video", "audio", "voiceover"]


@dataclass(frozen=True)
class InboxFile:
    """A safe, JSON-serializable reference to a file supplied by a user."""

    file_id: str
    fingerprint: str
    kind: InboxKind
    name: str
    relative_path: str
    extension: str
    size_bytes: int
    mtime_ns: int
    source: Literal["upload", "legacy"]
    duplicate_paths: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["duplicate_paths"] = list(self.duplicate_paths)
        return payload


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _nfc_fold(name: str) -> str:
    return unicodedata.normalize("NFC", name).casefold()


def resolve_inbox_dir(root: Path, *names: str) -> Path:
    """Return the existing directory ``root/names`` tolerating NFC/NFD spelling.

    macOS volumes (and archives made there) may store Cyrillic folder names in
    NFD, while Linux compares names byte-wise. When the literal path is absent,
    each component is matched by its NFC form; if nothing matches the literal
    (NFC) path is returned so callers may still create it.
    """

    current = root
    for name in names:
        literal = current / name
        if literal.exists():
            current = literal
            continue
        match = None
        if current.is_dir():
            try:
                for child in sorted(current.iterdir(), key=lambda item: item.name):
                    if _nfc_fold(child.name) == _nfc_fold(name):
                        match = child
                        break
            except OSError:
                match = None
        current = match if match is not None else literal
    return current


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _cached_sha256(
    path: Path,
    relative_path: str,
    stat: Any,
    cache: dict[str, Any] | None,
    cache_out: dict[str, Any] | None,
) -> str:
    """Content hash reused while (path, size, mtime_ns) are unchanged."""

    if cache is None or cache_out is None:
        return _sha256(path)
    entry = cache.get(relative_path)
    if (
        isinstance(entry, dict)
        and entry.get("size") == stat.st_size
        and entry.get("mtime_ns") == stat.st_mtime_ns
        and isinstance(entry.get("sha256"), str)
    ):
        digest = entry["sha256"]
    else:
        digest = _sha256(path)
    cache_out[relative_path] = {
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "sha256": digest,
    }
    return digest


def _load_hash_cache(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _safe_files(
    directory: Path,
    *,
    project_root: Path,
    extensions: frozenset[str],
) -> Iterable[Path]:
    """Yield direct child files without following user-created symbolic links."""

    if not directory.exists() or not directory.is_dir():
        return
    safe_root = project_root.resolve()
    safe_directory = directory.resolve()
    if not _is_relative_to(safe_directory, safe_root):
        return
    try:
        candidates = sorted(directory.iterdir(), key=lambda path: path.name.casefold())
    except OSError:
        return
    for candidate in candidates:
        if candidate.is_symlink() or candidate.suffix.lower() not in extensions:
            continue
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            continue
        if (
            resolved.is_file()
            and _is_relative_to(resolved, safe_directory)
            and _is_relative_to(resolved, safe_root)
        ):
            yield resolved


def _discover_category(
    locations: tuple[tuple[Path, Literal["upload", "legacy"]], ...],
    *,
    project_root: Path,
    extensions: frozenset[str],
    default_kind: InboxKind,
    hash_content: bool = True,
    hash_cache: dict[str, Any] | None = None,
    hash_cache_out: dict[str, Any] | None = None,
) -> list[InboxFile]:
    # Locations are ordered by preference. This makes a copy placed in the new,
    # user-facing folder canonical while preserving visibility of legacy inputs.
    by_fingerprint: dict[str, InboxFile] = {}
    duplicates: dict[str, list[str]] = {}
    safe_root = project_root.resolve()

    for directory, source in locations:
        for path in _safe_files(
            directory,
            project_root=safe_root,
            extensions=extensions,
        ):
            try:
                stat = path.stat()
                relative_path = path.relative_to(safe_root).as_posix()
                if hash_content:
                    identity_hash = _cached_sha256(
                        path, relative_path, stat, hash_cache, hash_cache_out
                    )
                else:
                    identity = json.dumps(
                        {
                            "path": relative_path,
                            "size": stat.st_size,
                            "mtime_ns": stat.st_mtime_ns,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    identity_hash = hashlib.sha256(identity.encode("utf-8")).hexdigest()
            except (OSError, ValueError):
                # A concurrently removed file, permission error, or unexpected
                # path must not break discovery of the rest of the inbox.
                continue
            fingerprint = f"sha256:{identity_hash}"
            if fingerprint in by_fingerprint:
                duplicates[fingerprint].append(relative_path)
                continue
            extension = path.suffix.lower()
            kind: InboxKind = default_kind
            if default_kind not in ("scenario", "voiceover"):
                kind = "audio" if extension in AUDIO_EXTENSIONS else "video"
            by_fingerprint[fingerprint] = InboxFile(
                file_id=f"inbox_{identity_hash[:20]}",
                fingerprint=fingerprint,
                kind=kind,
                name=path.name,
                relative_path=relative_path,
                extension=extension,
                size_bytes=stat.st_size,
                mtime_ns=stat.st_mtime_ns,
                source=source,
            )
            duplicates[fingerprint] = []

    files = []
    for fingerprint, item in by_fingerprint.items():
        files.append(
            InboxFile(
                **{
                    **asdict(item),
                    "duplicate_paths": tuple(duplicates[fingerprint]),
                }
            )
        )
    return sorted(
        files,
        key=lambda item: (
            item.source != "upload",
            item.name.casefold(),
            item.relative_path.casefold(),
        ),
    )


def discover_project_inbox(project_root: Path) -> dict[str, Any]:
    """Discover supported files in the friendly and legacy project folders.

    The function is read-only. It never moves, copies, renames, or deletes a
    supplied file. Identical content is returned once, with alternate locations
    listed in ``duplicate_paths``.
    """

    root = project_root.resolve()
    scenarios = _discover_category(
        (
            (resolve_inbox_dir(root, "ЗАГРУЗИТЬ", "Сценарии"), "upload"),
            (root / "input", "legacy"),
        ),
        project_root=root,
        extensions=SCENARIO_EXTENSIONS,
        default_kind="scenario",
    )
    reviews = _discover_category(
        (
            (resolve_inbox_dir(root, "ЗАГРУЗИТЬ", "Просмотровки"), "upload"),
            (root / "media" / "reviews", "legacy"),
        ),
        project_root=root,
        extensions=REVIEW_EXTENSIONS,
        default_kind="video",
        hash_content=False,
    )
    # Recordings are large: their hashes are cached by (path, size, mtime_ns).
    cache_path = root / "work" / "inbox_hash_cache.json"
    hash_cache = _load_hash_cache(cache_path)
    hash_cache_out: dict[str, Any] = {}
    voiceovers = _discover_category(
        ((resolve_inbox_dir(root, "ЗАГРУЗИТЬ", "Закадр"), "upload"),),
        project_root=root,
        extensions=VOICEOVER_EXTENSIONS,
        default_kind="voiceover",
        hash_cache=hash_cache,
        hash_cache_out=hash_cache_out,
    )
    if hash_cache_out != hash_cache:
        try:
            atomic_write_json(cache_path, hash_cache_out)
        except OSError:
            pass  # the cache is an optimization only
    return {
        "scenario_files": [item.to_dict() for item in scenarios],
        "review_files": [item.to_dict() for item in reviews],
        "voiceover_files": [item.to_dict() for item in voiceovers],
    }
