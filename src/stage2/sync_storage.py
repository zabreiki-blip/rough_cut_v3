"""Atomic JSON stores for Stage 2 sync sources and alignment results."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from atomic_io import atomic_write_json as _atomic_write_json

from .sync_models import SyncMatch, SyncSource

# Review-media support is an additive extension of the existing schema.  Keep
# writing version 1 so older clients that ignore unknown source fields continue
# to accept the document; ``SyncSource.from_dict`` supplies defaults for older
# records that do not contain them.
SCHEMA_VERSION = 1


def _read_json(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON in {path}: {exc}") from exc


def _items(payload: Any, key: str) -> list[dict[str, Any]]:
    if payload is None:
        return []
    # Accept the initial prototype's bare-list files as well as the versioned
    # envelope used for newly written data.
    values = payload if isinstance(payload, list) else payload.get(key, [])
    if not isinstance(values, list):
        raise ValueError(f"{key} must be a JSON list")
    if not all(isinstance(item, dict) for item in values):
        raise ValueError(f"every {key} item must be an object")
    return values


class SyncSourceStore:
    def __init__(self, path: Path | str):
        self.path = Path(path)

    def list(self) -> list[SyncSource]:
        return [
            SyncSource.from_dict(item)
            for item in _items(_read_json(self.path), "sources")
        ]

    def get(self, source_id: str) -> SyncSource | None:
        return next((item for item in self.list() if item.id == source_id), None)

    def save(self, source: SyncSource) -> SyncSource:
        sources = self.list()
        by_id = {item.id: item for item in sources}
        by_id[source.id] = source
        ordered = [by_id[key] for key in sorted(by_id)]
        _atomic_write_json(
            self.path,
            {"schema_version": SCHEMA_VERSION, "sources": [item.to_dict() for item in ordered]},
        )
        return source

    def replace_all(self, sources: list[SyncSource]) -> None:
        if len({item.id for item in sources}) != len(sources):
            raise ValueError("duplicate sync source id")
        _atomic_write_json(
            self.path,
            {
                "schema_version": SCHEMA_VERSION,
                "sources": [item.to_dict() for item in sources],
            },
        )

    def delete(self, source_id: str) -> bool:
        sources = self.list()
        remaining = [item for item in sources if item.id != source_id]
        if len(remaining) == len(sources):
            return False
        self.replace_all(remaining)
        return True


class SyncMatchStore:
    def __init__(self, path: Path | str):
        self.path = Path(path)

    def list(self) -> list[SyncMatch]:
        return [
            SyncMatch.from_dict(item)
            for item in _items(_read_json(self.path), "matches")
        ]

    def get(self, segment_id: str) -> SyncMatch | None:
        return next((item for item in self.list() if item.segment_id == segment_id), None)

    def save(self, match: SyncMatch) -> SyncMatch:
        matches = self.list()
        by_id = {item.segment_id: item for item in matches}
        by_id[match.segment_id] = match
        ordered = [by_id[key] for key in sorted(by_id)]
        _atomic_write_json(
            self.path,
            {"schema_version": SCHEMA_VERSION, "matches": [item.to_dict() for item in ordered]},
        )
        return match

    def replace_all(self, matches: list[SyncMatch]) -> None:
        if len({item.segment_id for item in matches}) != len(matches):
            raise ValueError("duplicate segment_id in sync matches")
        _atomic_write_json(
            self.path,
            {
                "schema_version": SCHEMA_VERSION,
                "matches": [item.to_dict() for item in matches],
            },
        )

    def delete(self, segment_id: str) -> bool:
        matches = self.list()
        remaining = [item for item in matches if item.segment_id != segment_id]
        if len(remaining) == len(matches):
            return False
        self.replace_all(remaining)
        return True
