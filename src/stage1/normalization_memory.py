from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
import re
import sqlite3
from typing import Any, Iterator

from .normalization_models import VALID_BLOCK_TYPES


DEFAULT_MEMORY_PATH = Path(__file__).resolve().parents[2] / "data" / "normalization_memory.sqlite3"

VALID_ALIAS_SCOPES = {"marker", "video_cue"}

# Service-cue prefixes that a speaker index is commonly glued directly to
# ("тех32", "зк5", "снх12"), as opposed to a person's name ("Сергей1",
# "Сергей2") where the glued digit is part of what distinguishes two
# different speakers and must NOT be stripped. Kept in sync (by hand) with
# `_BUILTIN_MARKERS`/`_TECH_MARKERS` in normalizer.py; duplicated here rather
# than imported to avoid a circular import between the two modules.
_GLUED_NUMBER_STRIPPABLE_PREFIXES = frozenset(
    {
        "тех",
        "техн",
        "зк",
        "з",
        "zk",
        "снх",
        "синхр",
        "синхрон",
        "лайф",
        "лайфы",
        "стендап",
        "вид",
        "видео",
        "титр",
        "vo",
        "sync",
    }
)

# Markers whose whitespace-separated trailing index ("диктор 1", "зк 5") is a
# speaker/cue index and is stripped. Superset of the glued-form prefixes: it
# also covers the multi-word and word-form built-in markers. Kept in sync (by
# hand) with `_BUILTIN_MARKERS`/`_TECH_MARKERS` in normalizer.py. A person's
# name ("Сергей 1" / "Сергей 2") is deliberately absent: the index is what
# tells two different speakers apart.
_SPACED_NUMBER_STRIPPABLE_PREFIXES = _GLUED_NUMBER_STRIPPABLE_PREFIXES | frozenset(
    {
        "narrator",
        "подводка",
        "з к",
        "закадр",
        "закадровый текст",
        "диктор",
        "голос диктора",
        "интервью",
        "ведущий",
        "ведущая",
        "life",
        "титры",
        "плашка",
        "графика",
        "карта",
        "фото",
        "архив",
        "отбивка",
        "шапка",
        "склейка",
        "ремарка",
        "техническое",
    }
)


def _validate_scope(scope: str) -> str:
    if scope not in VALID_ALIAS_SCOPES:
        raise ValueError(
            f"Invalid alias scope: {scope!r}. Expected one of {sorted(VALID_ALIAS_SCOPES)}"
        )
    return scope


def normalize_marker(marker: str) -> str:
    value = marker.casefold().replace("ё", "е").strip()
    value = re.sub(r"[\s.:;,_/\\|\-–—]+", " ", value)
    # A speaker index separated by whitespace ("диктор 1") is stripped only
    # for the closed set of known service-cue markers. For arbitrary markers
    # (a person's name: "Сергей 1" vs "Сергей 2") the index distinguishes
    # different speakers and must be kept, otherwise learned rules and
    # suppressions collide on the same key.
    spaced_match = re.search(r"\s+\d+[a-zа-я]?\s*$", value)
    if spaced_match:
        candidate = value[: spaced_match.start()].strip()
        if candidate in _SPACED_NUMBER_STRIPPABLE_PREFIXES:
            value = candidate
    else:
        # A glued digit ("тех32", "зк5") is only stripped for the closed set
        # of known service-cue prefixes. Stripping it for arbitrary markers
        # would merge distinct speakers who happen to share a name prefix
        # and a trailing index, e.g. "Сергей1" vs "Сергей2".
        glued_match = re.match(r"^([a-zа-я]+)(\d+[a-zа-я]?)$", value)
        if glued_match and glued_match.group(1) in _GLUED_NUMBER_STRIPPABLE_PREFIXES:
            value = glued_match.group(1)
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
        self._rekey_stale_normalized_markers()

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
                scope TEXT NOT NULL DEFAULT 'marker',
                UNIQUE(normalized_marker, target_type, source, scope)
            );
            CREATE INDEX IF NOT EXISTS aliases_marker_idx ON aliases(normalized_marker);
            CREATE TABLE IF NOT EXISTS alias_confirmations (
                alias_id INTEGER NOT NULL REFERENCES aliases(id) ON DELETE CASCADE,
                source_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(alias_id, source_hash)
            );
            CREATE TABLE IF NOT EXISTS alias_suppressions (
                normalized_marker TEXT PRIMARY KEY,
                marker TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        self._conn.commit()
        self._migrate_scope_column_if_needed()

    def _migrate_scope_column_if_needed(self) -> None:
        """Rebuild the ``aliases`` table for databases created before the
        ``scope`` column existed. Idempotent: no-ops once ``scope`` is present.
        """
        columns = [row[1] for row in self._conn.execute("PRAGMA table_info(aliases)")]
        if "scope" in columns:
            return
        before_count = self._conn.execute("SELECT COUNT(*) FROM aliases").fetchone()[0]
        previous_foreign_keys = self._conn.execute("PRAGMA foreign_keys").fetchone()[0]
        try:
            self._conn.execute("PRAGMA foreign_keys = OFF")
            self._conn.execute("BEGIN")
            self._conn.execute(
                """
                CREATE TABLE aliases_new (
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
                    scope TEXT NOT NULL DEFAULT 'marker',
                    UNIQUE(normalized_marker, target_type, source, scope)
                )
                """
            )
            self._conn.execute(
                """INSERT INTO aliases_new
                   (id, marker, normalized_marker, target_type, source, status,
                    confirmations, corrections, confidence, created_at, updated_at, scope)
                   SELECT id, marker, normalized_marker, target_type, source, status,
                          confirmations, corrections, confidence, created_at, updated_at, 'marker'
                   FROM aliases"""
            )
            self._conn.execute("DROP TABLE aliases")
            self._conn.execute("ALTER TABLE aliases_new RENAME TO aliases")
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS aliases_marker_idx ON aliases(normalized_marker)"
            )
            after_count = self._conn.execute("SELECT COUNT(*) FROM aliases").fetchone()[0]
            if after_count != before_count:
                raise RuntimeError(
                    f"Alias migration row count mismatch: before={before_count} after={after_count}"
                )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        finally:
            self._conn.execute(f"PRAGMA foreign_keys = {'ON' if previous_foreign_keys else 'OFF'}")

    def _rekey_stale_normalized_markers(self) -> None:
        """Recompute ``normalized_marker`` from the stored raw ``marker``.

        Older versions stripped a spaced numeric index from every marker, so
        "Сергей 2" was stored under the key "сергей". The raw marker is always
        stored next to the key, so the key is rebuilt from it. If the rebuilt
        key already exists for the same (target_type, source, scope) the stale
        duplicate row is dropped. There is deliberately no fallback to the old
        ambiguous key. Idempotent: no-ops once every key matches its marker.
        """
        try:
            stale = []
            for row in self._conn.execute(
                "SELECT id, marker, normalized_marker, scope FROM aliases"
            ):
                new_key = normalize_marker(str(row["marker"]))
                if new_key and new_key != row["normalized_marker"]:
                    stale.append((row["id"], new_key, row["normalized_marker"], row["scope"]))
            stale_suppressions = []
            for row in self._conn.execute(
                "SELECT rowid AS rid, marker, normalized_marker FROM alias_suppressions"
            ):
                new_key = normalize_marker(str(row["marker"]))
                if new_key and new_key != row["normalized_marker"]:
                    stale_suppressions.append((row["rid"], new_key))
            if not stale and not stale_suppressions:
                return
            touched: set[tuple[str, str]] = set()
            with self.transaction():
                # Phase 1: park every stale row under a unique temporary key so
                # that a stale row's target key can never collide with another
                # stale row's *old* key (chains like "Сергей 2" / "Сергей 2 2").
                # Phase 2 then moves each row, addressed by its own id/rowid;
                # only a genuine clash with an already-correct row drops the
                # stale row itself. Rows that are not stale are never touched.
                for rule_id, _new_key, _old_key, _scope in stale:
                    self._conn.execute(
                        "UPDATE aliases SET normalized_marker=? WHERE id=?",
                        (f"\x00rekey:{rule_id}", rule_id),
                    )
                for rid, _new_key in stale_suppressions:
                    self._conn.execute(
                        "UPDATE alias_suppressions SET normalized_marker=? WHERE rowid=?",
                        (f"\x00rekey:{rid}", rid),
                    )
                for rule_id, new_key, old_key, scope in stale:
                    cur = self._conn.execute(
                        "UPDATE OR IGNORE aliases SET normalized_marker=? WHERE id=?",
                        (new_key, rule_id),
                    )
                    if cur.rowcount == 0:
                        self._conn.execute("DELETE FROM aliases WHERE id=?", (rule_id,))
                    touched.add((new_key, scope))
                    touched.add((old_key, scope))
                for rid, new_key in stale_suppressions:
                    cur = self._conn.execute(
                        "UPDATE OR IGNORE alias_suppressions SET normalized_marker=? WHERE rowid=?",
                        (new_key, rid),
                    )
                    if cur.rowcount == 0:
                        self._conn.execute("DELETE FROM alias_suppressions WHERE rowid=?", (rid,))
                for key, scope in touched:
                    self._reconcile_conflicts(key, scope)
        except sqlite3.Error:
            self._conn.rollback()
            raise

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    def list_rules(
        self, *, status: str | None = None, scope: str | None = None
    ) -> list[dict[str, Any]]:
        conditions: list[str] = []
        params: list[Any] = []
        if status:
            conditions.append("status = ?")
            params.append(status)
        if scope is not None:
            _validate_scope(scope)
            conditions.append("scope = ?")
            params.append(scope)
        sql = "SELECT * FROM aliases"
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        sql += " ORDER BY normalized_marker, source, id"
        return [dict(row) for row in self._conn.execute(sql, tuple(params))]

    def get_rule(self, rule_id: int) -> dict[str, Any] | None:
        return self._row(
            self._conn.execute("SELECT * FROM aliases WHERE id = ?", (rule_id,)).fetchone()
        )

    def create_rule(
        self,
        marker: str,
        target_type: str,
        *,
        source: str = "manual",
        status: str | None = None,
        scope: str = "marker",
    ) -> dict[str, Any]:
        _validate_scope(scope)
        if target_type not in VALID_BLOCK_TYPES or target_type == "unknown":
            raise ValueError(f"Invalid learned block type: {target_type}")
        normalized = normalize_marker(marker)
        if not normalized:
            raise ValueError("Marker cannot be empty")
        if source not in {"manual", "llm", "correction"}:
            raise ValueError("Memory source must be manual, llm or correction")
        now = _utcnow()
        rule_status = status or ("active" if source == "manual" else "candidate")
        confirmations = 1 if source == "manual" else 0
        confidence = 1.0 if source == "manual" else 0.5
        with self.transaction():
            self._conn.execute(
                """INSERT INTO aliases
                   (marker, normalized_marker, target_type, source, status,
                    confirmations, corrections, confidence, created_at, updated_at, scope)
                   VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)
                   ON CONFLICT(normalized_marker, target_type, source, scope) DO UPDATE SET
                     marker=excluded.marker,
                     status=CASE
                       WHEN aliases.status='disabled' THEN 'disabled'
                       WHEN aliases.status IN ('active', 'conflict') AND excluded.status='candidate'
                         THEN aliases.status
                       ELSE excluded.status
                     END,
                     confidence=MAX(aliases.confidence, excluded.confidence),
                     updated_at=excluded.updated_at""",
                (
                    marker.strip(),
                    normalized,
                    target_type,
                    source,
                    rule_status,
                    confirmations,
                    confidence,
                    now,
                    now,
                    scope,
                ),
            )
            self._reconcile_conflicts(normalized, scope)
        row = self._conn.execute(
            "SELECT * FROM aliases WHERE normalized_marker=? AND target_type=? AND source=? AND scope=?",
            (normalized, target_type, source, scope),
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
            self._reconcile_conflicts(str(current["normalized_marker"]), str(current["scope"]))
            self._reconcile_conflicts(normalized, str(current["scope"]))
        result = self.get_rule(rule_id)
        assert result is not None
        return result

    def delete_rule(self, rule_id: int) -> bool:
        current = self.get_rule(rule_id)
        if current is None:
            return False
        with self.transaction():
            cur = self._conn.execute("DELETE FROM aliases WHERE id=?", (rule_id,))
            self._reconcile_conflicts(str(current["normalized_marker"]), str(current["scope"]))
        return cur.rowcount > 0

    def record_manual(
        self, marker: str, target_type: str, *, scope: str = "marker"
    ) -> dict[str, Any]:
        return self.create_rule(marker, target_type, source="manual", status="active", scope=scope)

    def record_llm_confirmation(
        self, marker: str, target_type: str, source_hash: str, *, scope: str = "marker"
    ) -> dict[str, Any]:
        return self.record_candidate_confirmation(
            marker, target_type, source_hash, source="llm", scope=scope
        )

    def record_correction_confirmation(
        self, marker: str, target_type: str, source_hash: str, *, scope: str = "marker"
    ) -> dict[str, Any]:
        return self.record_candidate_confirmation(
            marker, target_type, source_hash, source="correction", scope=scope
        )

    def record_candidate_confirmation(
        self, marker: str, target_type: str, source_hash: str, *, source: str, scope: str = "marker"
    ) -> dict[str, Any]:
        _validate_scope(scope)
        if source not in {"llm", "correction"}:
            raise ValueError("Candidate source must be llm or correction")
        rule = self.create_rule(marker, target_type, source=source, status="candidate", scope=scope)
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
            self._reconcile_conflicts(normalize_marker(marker), scope)
        result = self.get_rule(int(rule["id"]))
        assert result is not None
        return result

    def suppress_marker(self, marker: str) -> dict[str, Any]:
        normalized = normalize_marker(marker)
        if not normalized:
            raise ValueError("Marker cannot be empty")
        now = _utcnow()
        with self.transaction():
            self._conn.execute(
                """INSERT INTO alias_suppressions(normalized_marker, marker, created_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(normalized_marker) DO UPDATE SET marker=excluded.marker""",
                (normalized, marker.strip(), now),
            )
        return {"marker": marker.strip(), "normalized_marker": normalized, "created_at": now}

    def is_suppressed(self, marker: str) -> bool:
        normalized = normalize_marker(marker)
        if not normalized:
            return False
        row = self._conn.execute(
            "SELECT 1 FROM alias_suppressions WHERE normalized_marker=?", (normalized,)
        ).fetchone()
        return row is not None

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

    def resolve_exact(self, marker: str, *, scope: str = "marker") -> dict[str, Any] | None:
        _validate_scope(scope)
        normalized = normalize_marker(marker)
        rows = [
            dict(row)
            for row in self._conn.execute(
                """SELECT * FROM aliases WHERE normalized_marker=? AND scope=? AND status IN ('active', 'conflict')
               ORDER BY CASE source WHEN 'manual' THEN 0 ELSE 1 END, confidence DESC, id""",
                (normalized, scope),
            )
        ]
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

    def suggest(
        self, marker: str, *, cutoff: float = 0.82, scope: str = "marker"
    ) -> dict[str, Any] | None:
        _validate_scope(scope)
        normalized = normalize_marker(marker)
        if not normalized:
            return None
        best: tuple[float, dict[str, Any]] | None = None
        for rule in self.list_rules(status="active", scope=scope):
            if rule["normalized_marker"] == normalized:
                continue
            score = SequenceMatcher(None, normalized, rule["normalized_marker"]).ratio()
            if score >= cutoff and (best is None or score > best[0]):
                best = (score, rule)
        if best is None:
            return None
        return {"score": round(best[0], 3), "rule": best[1]}

    def _reconcile_conflicts(self, normalized_marker: str, scope: str = "marker") -> None:
        rows = list(
            self._conn.execute(
                """SELECT id, target_type, source, status, confirmations
               FROM aliases WHERE normalized_marker=? AND scope=? AND status != 'disabled'""",
                (normalized_marker, scope),
            )
        )
        manual_types = {row["target_type"] for row in rows if row["source"] == "manual"}
        if len(manual_types) > 1:
            self._conn.execute(
                "UPDATE aliases SET status='conflict', updated_at=? WHERE normalized_marker=? AND scope=? AND status!='disabled'",
                (_utcnow(), normalized_marker, scope),
            )
            return
        if len(manual_types) == 1:
            manual_target = next(iter(manual_types))
            now = _utcnow()
            self._conn.execute(
                """UPDATE aliases SET status='active', updated_at=?
                   WHERE normalized_marker=? AND scope=? AND source='manual'""",
                (now, normalized_marker, scope),
            )
            self._conn.execute(
                """UPDATE aliases SET status=CASE
                     WHEN target_type != ? THEN 'conflict'
                     WHEN confirmations >= 2 THEN 'active'
                     ELSE 'candidate' END,
                   updated_at=?
                   WHERE normalized_marker=? AND scope=? AND source IN ('llm', 'correction') AND status!='disabled'""",
                (manual_target, now, normalized_marker, scope),
            )
            return
        active_types = {
            row["target_type"] for row in rows if row["status"] in {"active", "conflict"}
        }
        if len(active_types) > 1:
            self._conn.execute(
                "UPDATE aliases SET status='conflict', updated_at=? WHERE normalized_marker=? AND scope=? AND status!='disabled'",
                (_utcnow(), normalized_marker, scope),
            )
        elif len(active_types) == 1:
            target = next(iter(active_types))
            self._conn.execute(
                """UPDATE aliases SET status='active', updated_at=?
                   WHERE normalized_marker=? AND scope=? AND status='conflict' AND target_type=?""",
                (_utcnow(), normalized_marker, scope, target),
            )


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()
