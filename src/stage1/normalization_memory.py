from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
import re
import sqlite3
from typing import Any, Iterator

from .normalization_models import VALID_BLOCK_TYPES


DEFAULT_MEMORY_PATH = Path("data/normalization_memory.sqlite3")


def normalize_marker(marker: str) -> str:
    value = marker.casefold().replace("ё", "е").strip()
    value = re.sub(r"[\s.:;,_/\\|\-–—]+", " ", value)
    value = re.sub(r"\s+\d+[a-zа-я]?\s*$", "", value)
    return value.strip()


class NormalizationMemory:
    def __init__(self, path: Path | str | None = None):
        self.path = Path(path or DEFAULT_MEMORY_PATH)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA busy_timeout = 5000")
        self._create_schema()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "NormalizationMemory":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        try:
            yield
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def _create_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS aliases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                marker TEXT NOT NULL,
                normalized_marker TEXT NOT NULL,
                target_type TEXT NOT NULL,
                source TEXT NOT NULL,
                status TEXT NOT NULL,
                confirmations INTEGER NOT NULL DEFAULT 0,
                corrections INTEGER NOT NULL DEFAULT 0,
                confidence REAL NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(normalized_marker, target_type, source)
            );
            CREATE INDEX IF NOT EXISTS aliases_marker_idx ON aliases(normalized_marker);
            CREATE TABLE IF NOT EXISTS alias_confirmations (
                alias_id INTEGER NOT NULL REFERENCES aliases(id) ON DELETE CASCADE,
                source_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(alias_id, source_hash)
            );
            """
        )
        self._conn.commit()

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    def list_rules(self, *, status: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM aliases"
        params: tuple[Any, ...] = ()
        if status:
            sql += " WHERE status = ?"
            params = (status,)
        sql += " ORDER BY normalized_marker, source, id"
        return [dict(row) for row in self._conn.execute(sql, params)]

    def get_rule(self, rule_id: int) -> dict[str, Any] | None:
        return self._row(self._conn.execute("SELECT * FROM aliases WHERE id = ?", (rule_id,)).fetchone())

    def create_rule(
        self,
        marker: str,
        target_type: str,
        *,
        source: str = "manual",
        status: str | None = None,
    ) -> dict[str, Any]:
        if target_type not in VALID_BLOCK_TYPES or target_type == "unknown":
            raise ValueError(f"Invalid learned block type: {target_type}")
        normalized = normalize_marker(marker)
        if not normalized:
            raise ValueError("Marker cannot be empty")
        if source not in {"manual", "llm"}:
            raise ValueError("Memory source must be manual or llm")
        now = _utcnow()
        rule_status = status or ("active" if source == "manual" else "candidate")
        confirmations = 1 if source == "manual" else 0
        confidence = 1.0 if source == "manual" else 0.5
        with self.transaction():
            self._conn.execute(
                """INSERT INTO aliases
                   (marker, normalized_marker, target_type, source, status,
                    confirmations, corrections, confidence, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
                   ON CONFLICT(normalized_marker, target_type, source) DO UPDATE SET
                     marker=excluded.marker,
                     status=CASE
                       WHEN aliases.status='disabled' THEN 'disabled'
                       WHEN aliases.status IN ('active', 'conflict') AND excluded.status='candidate'
                         THEN aliases.status
                       ELSE excluded.status
                     END,
                     confidence=MAX(aliases.confidence, excluded.confidence),
                     updated_at=excluded.updated_at""",
                (marker.strip(), normalized, target_type, source, rule_status,
                 confirmations, confidence, now, now),
            )
            self._reconcile_conflicts(normalized)
        row = self._conn.execute(
            "SELECT * FROM aliases WHERE normalized_marker=? AND target_type=? AND source=?",
            (normalized, target_type, source),
        ).fetchone()
        return dict(row)

    def update_rule(self, rule_id: int, **changes: Any) -> dict[str, Any]:
        allowed = {"marker", "target_type", "status"}
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError(f"Unsupported memory fields: {', '.join(sorted(unknown))}")
        current = self.get_rule(rule_id)
        if current is None:
            raise KeyError(rule_id)
        marker = str(changes.get("marker", current["marker"]))
        normalized = normalize_marker(marker)
        target_type = str(changes.get("target_type", current["target_type"]))
        status = str(changes.get("status", current["status"]))
        if target_type not in VALID_BLOCK_TYPES or target_type == "unknown":
            raise ValueError(f"Invalid learned block type: {target_type}")
        if status not in {"candidate", "active", "conflict", "disabled"}:
            raise ValueError(f"Invalid memory status: {status}")
        with self.transaction():
            self._conn.execute(
                "UPDATE aliases SET marker=?, normalized_marker=?, target_type=?, status=?, updated_at=? WHERE id=?",
                (marker.strip(), normalized, target_type, status, _utcnow(), rule_id),
            )
            self._reconcile_conflicts(str(current["normalized_marker"]))
            self._reconcile_conflicts(normalized)
        result = self.get_rule(rule_id)
        assert result is not None
        return result

    def delete_rule(self, rule_id: int) -> bool:
        current = self.get_rule(rule_id)
        if current is None:
            return False
        with self.transaction():
            cur = self._conn.execute("DELETE FROM aliases WHERE id=?", (rule_id,))
            self._reconcile_conflicts(str(current["normalized_marker"]))
        return cur.rowcount > 0

    def record_manual(self, marker: str, target_type: str) -> dict[str, Any]:
        return self.create_rule(marker, target_type, source="manual", status="active")

    def record_llm_confirmation(
        self, marker: str, target_type: str, source_hash: str
    ) -> dict[str, Any]:
        rule = self.create_rule(marker, target_type, source="llm", status="candidate")
        now = _utcnow()
        with self.transaction():
            inserted = self._conn.execute(
                "INSERT OR IGNORE INTO alias_confirmations(alias_id, source_hash, created_at) VALUES (?, ?, ?)",
                (rule["id"], source_hash, now),
            ).rowcount
            if inserted:
                count = self._conn.execute(
                    "SELECT COUNT(*) FROM alias_confirmations WHERE alias_id=?", (rule["id"],)
                ).fetchone()[0]
                status = "active" if count >= 2 else "candidate"
                confidence = min(0.95, 0.5 + 0.2 * count)
                self._conn.execute(
                    """UPDATE aliases SET confirmations=?,
                       status=CASE WHEN status='disabled' THEN 'disabled' ELSE ? END,
                       confidence=?, updated_at=? WHERE id=?""",
                    (count, status, confidence, now, rule["id"]),
                )
            self._reconcile_conflicts(normalize_marker(marker))
        result = self.get_rule(int(rule["id"]))
        assert result is not None
        return result

    def record_correction(self, rule_id: int) -> dict[str, Any]:
        with self.transaction():
            self._conn.execute(
                """UPDATE aliases SET corrections=corrections+1,
                   confidence=MAX(0, confidence-0.25), updated_at=? WHERE id=?""",
                (_utcnow(), rule_id),
            )
        result = self.get_rule(rule_id)
        if result is None:
            raise KeyError(rule_id)
        return result

    def resolve_exact(self, marker: str) -> dict[str, Any] | None:
        normalized = normalize_marker(marker)
        rows = [dict(row) for row in self._conn.execute(
            """SELECT * FROM aliases WHERE normalized_marker=? AND status IN ('active', 'conflict')
               ORDER BY CASE source WHEN 'manual' THEN 0 ELSE 1 END, confidence DESC, id""",
            (normalized,),
        )]
        if not rows:
            return None
        manual = [row for row in rows if row["source"] == "manual"]
        if manual:
            manual_types = {row["target_type"] for row in manual}
            active_manual = [row for row in manual if row["status"] == "active"]
            if len(manual_types) == 1 and active_manual:
                return {"conflict": False, "rule": active_manual[0]}
            return {"conflict": True, "normalized_marker": normalized, "rules": manual}
        if any(row["status"] == "conflict" for row in rows):
            return {"conflict": True, "normalized_marker": normalized, "rules": rows}
        preferred = manual or rows
        if len({row["target_type"] for row in preferred}) != 1:
            return {"conflict": True, "normalized_marker": normalized, "rules": preferred}
        return {"conflict": False, "rule": preferred[0]}

    def suggest(self, marker: str, *, cutoff: float = 0.82) -> dict[str, Any] | None:
        normalized = normalize_marker(marker)
        if not normalized:
            return None
        best: tuple[float, dict[str, Any]] | None = None
        for rule in self.list_rules(status="active"):
            if rule["normalized_marker"] == normalized:
                continue
            score = SequenceMatcher(None, normalized, rule["normalized_marker"]).ratio()
            if score >= cutoff and (best is None or score > best[0]):
                best = (score, rule)
        if best is None:
            return None
        return {"score": round(best[0], 3), "rule": best[1]}

    def _reconcile_conflicts(self, normalized_marker: str) -> None:
        rows = list(self._conn.execute(
            """SELECT id, target_type, source, status, confirmations
               FROM aliases WHERE normalized_marker=? AND status != 'disabled'""",
            (normalized_marker,),
        ))
        manual_types = {row["target_type"] for row in rows if row["source"] == "manual"}
        if len(manual_types) > 1:
            self._conn.execute(
                "UPDATE aliases SET status='conflict', updated_at=? WHERE normalized_marker=? AND status!='disabled'",
                (_utcnow(), normalized_marker),
            )
            return
        if len(manual_types) == 1:
            manual_target = next(iter(manual_types))
            now = _utcnow()
            self._conn.execute(
                """UPDATE aliases SET status='active', updated_at=?
                   WHERE normalized_marker=? AND source='manual'""",
                (now, normalized_marker),
            )
            self._conn.execute(
                """UPDATE aliases SET status=CASE
                     WHEN target_type != ? THEN 'conflict'
                     WHEN confirmations >= 2 THEN 'active'
                     ELSE 'candidate' END,
                   updated_at=?
                   WHERE normalized_marker=? AND source='llm' AND status!='disabled'""",
                (manual_target, now, normalized_marker),
            )
            return
        active_types = {row["target_type"] for row in rows if row["status"] in {"active", "conflict"}}
        if len(active_types) > 1:
            self._conn.execute(
                "UPDATE aliases SET status='conflict', updated_at=? WHERE normalized_marker=? AND status!='disabled'",
                (_utcnow(), normalized_marker),
            )
        elif len(active_types) == 1:
            target = next(iter(active_types))
            self._conn.execute(
                """UPDATE aliases SET status='active', updated_at=?
                   WHERE normalized_marker=? AND status='conflict' AND target_type=?""",
                (_utcnow(), normalized_marker, target),
            )


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()
