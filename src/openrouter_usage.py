from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
import logging
from pathlib import Path
import threading
from typing import Any
from uuid import uuid4

from atomic_io import atomic_write_json


LOGGER = logging.getLogger(__name__)
UsageCallback = Callable[[dict[str, Any]], None]
_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()
# События, по которым прямо сейчас идёт внешний запрос (claim). Хранится
# только в памяти: после падения процесса «зависшей» пометки не остаётся.
_CLAIMED: set[tuple[str, str]] = set()
_SCHEMA_VERSION = 2


class UsageLedgerError(RuntimeError):
    """The project usage ledger cannot be read without risking data loss."""


def _now() -> datetime:
    return datetime.now(UTC)


def _now_iso() -> str:
    return _now().isoformat()


def _lock_for(path: Path) -> threading.RLock:
    key = str(path.resolve())
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.RLock())


def _claim_key(path: Path, event: Mapping[str, Any]) -> tuple[str, str]:
    return (str(path.resolve()), str(event.get("event_id")))


def _is_due(event: Mapping[str, Any], now: datetime) -> bool:
    # record_usage always assigns an event_id. An event without one (hand-edited or
    # legacy ledger) cannot be claimed or written back unambiguously, so it is skipped
    # instead of colliding with every other id-less event under the key "None".
    return bool(
        str(event.get("event_id") or "").strip()
        and event.get("status") == "pending"
        and event.get("generation_id")
        and _parse_iso(event.get("next_retry_at")) <= now
    )


def _new_ledger() -> dict[str, Any]:
    return {
        "schema_version": _SCHEMA_VERSION,
        "currency": "USD",
        "started_at": _now_iso(),
        "total_cost_usd": "0",
        "events": [],
    }


def _load(path: Path, *, create: bool = False) -> dict[str, Any]:
    if not path.exists():
        ledger = _new_ledger()
        if create:
            atomic_write_json(path, ledger)
        return ledger
    try:
        import json

        ledger = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise UsageLedgerError(f"OpenRouter usage ledger is unreadable: {exc}") from exc
    if not isinstance(ledger, dict) or not isinstance(ledger.get("events"), list):
        raise UsageLedgerError("OpenRouter usage ledger has an invalid structure")
    if any(not isinstance(event, dict) for event in ledger["events"]):
        raise UsageLedgerError("OpenRouter usage ledger contains an invalid event")
    return ledger


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() and result >= 0 else None


def _total(events: list[dict[str, Any]]) -> Decimal:
    total = Decimal("0")
    for event in events:
        value = _decimal(event.get("cost_usd"))
        if value is not None:
            total += value
    return total


def _save(path: Path, ledger: dict[str, Any]) -> None:
    ledger["schema_version"] = _SCHEMA_VERSION
    ledger["total_cost_usd"] = str(_total(ledger["events"]))
    atomic_write_json(path, ledger)


def extract_cost(body: Mapping[str, Any]) -> Decimal | None:
    usage = body.get("usage")
    if not isinstance(usage, Mapping):
        return None
    for key in ("cost", "total_cost", "cost_usd", "total_usd"):
        value = _decimal(usage.get(key))
        if value is not None:
            return value
    return None


def report_json_usage(
    callback: UsageCallback | None,
    body: Mapping[str, Any],
    *,
    model: str,
) -> None:
    if callback is None:
        return
    cost = extract_cost(body)
    if cost is None:
        return
    callback(
        {
            "generation_id": str(body.get("id") or ""),
            "model": str(body.get("model") or model),
            "cost_usd": str(cost),
        }
    )


def make_usage_callback(
    path: Path,
    *,
    stage: str,
    operation: str,
    model: str = "",
    job_id: str = "",
    revision_id: str = "",
    base_url: str = "",
) -> UsageCallback:
    def callback(payload: dict[str, Any]) -> None:
        try:
            record_usage(
                path,
                stage=stage,
                operation=operation,
                model=str(payload.get("model") or model),
                cost_usd=payload.get("cost_usd"),
                reserved_cost_usd=payload.get("reserved_cost_usd"),
                generation_id=str(payload.get("generation_id") or ""),
                event_id=str(payload.get("event_id") or ""),
                job_id=job_id,
                revision_id=revision_id,
                pending=bool(payload.get("pending", False)),
                base_url=str(payload.get("base_url") or base_url),
            )
        except Exception:
            LOGGER.exception("Could not record OpenRouter usage in %s", path)

    return callback


def record_usage(
    path: Path,
    *,
    stage: str,
    operation: str,
    model: str,
    cost_usd: Any = None,
    reserved_cost_usd: Any = None,
    generation_id: str = "",
    event_id: str = "",
    job_id: str = "",
    revision_id: str = "",
    pending: bool = False,
    base_url: str = "",
) -> None:
    cost = _decimal(cost_usd)
    reserved = _decimal(reserved_cost_usd)
    if cost is None and not pending:
        return
    generation_id = generation_id.strip()
    event_id = event_id.strip() or generation_id or uuid4().hex
    with _lock_for(path):
        ledger = _load(path, create=False)
        events = ledger["events"]
        existing = next((item for item in events if str(item.get("event_id")) == event_id), None)
        if existing is not None:
            if cost is not None and existing.get("cost_usd") is None:
                existing["cost_usd"] = str(cost)
                existing["status"] = "confirmed"
                existing["resolved_at"] = _now_iso()
                existing.pop("next_retry_at", None)
                existing.pop("reserved_cost_usd", None)
            elif existing.get("status") == "pending" and reserved is not None:
                existing["reserved_cost_usd"] = str(reserved)
            _save(path, ledger)
            return
        event: dict[str, Any] = {
            "event_id": event_id,
            "generation_id": generation_id,
            "recorded_at": _now_iso(),
            "stage": stage,
            "operation": operation,
            "model": model,
            "job_id": job_id,
            "revision_id": revision_id,
            "status": "pending" if cost is None else "confirmed",
            "cost_usd": str(cost) if cost is not None else None,
        }
        if cost is None:
            if reserved is not None:
                event["reserved_cost_usd"] = str(reserved)
            event.update(
                {
                    "base_url": base_url.rstrip("/"),
                    "retry_count": 0,
                    "next_retry_at": _now_iso(),
                }
            )
        events.append(event)
        _save(path, ledger)


def _parse_iso(value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return datetime.min.replace(tzinfo=UTC)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def reconcile_pending(
    path: Path,
    *,
    api_key: str,
    request_fn: Callable[..., Any],
    max_items: int = 4,
) -> None:
    if not api_key:
        return
    # Дедупликация claim-ов действует только внутри процесса: несколько воркеров
    # gunicorn могут продублировать безвредный запрос generation. Дубль не портит
    # леджер: закрытые события (status != pending) пропускаются при записи результата.
    due: list[dict[str, Any]] = []
    try:
        # Claim-ы набираются внутри try: любой сбой посреди набора освобождает
        # уже занятые ключи в finally, а не оставляет их навсегда.
        with _lock_for(path):
            ledger = _load(path, create=True)
            now = _now()
            with _LOCKS_GUARD:
                for event in ledger["events"]:
                    if len(due) >= max_items:
                        break
                    key = _claim_key(path, event)
                    if _is_due(event, now) and key not in _CLAIMED:
                        due.append(dict(event))
                        _CLAIMED.add(key)
        _reconcile_claimed(path, due, api_key=api_key, request_fn=request_fn)
    finally:
        with _LOCKS_GUARD:
            for event in due:
                _CLAIMED.discard(_claim_key(path, event))


def has_due_pending(path: Path) -> bool:
    """Есть ли pending-события, которые пора уточнять и которые никто не уточняет."""
    try:
        with _lock_for(path):
            if not path.exists():
                return False
            ledger = _load(path)
            now = _now()
            with _LOCKS_GUARD:
                return any(
                    _is_due(event, now) and _claim_key(path, event) not in _CLAIMED
                    for event in ledger["events"]
                )
    except UsageLedgerError:
        return False


def _reconcile_claimed(
    path: Path,
    due: list[dict[str, Any]],
    *,
    api_key: str,
    request_fn: Callable[..., Any],
) -> None:
    for event in due:
        generation_id = str(event["generation_id"])
        cost: Decimal | None = None
        try:
            response = request_fn(
                f"{str(event.get('base_url') or '').rstrip('/')}/generation",
                headers={"Authorization": f"Bearer {api_key}"},
                params={"id": generation_id},
                timeout=8,
            )
            response.raise_for_status()
            body = response.json()
            data = body.get("data") if isinstance(body, dict) else None
            if isinstance(data, Mapping):
                cost = _decimal(data.get("total_cost"))
                if cost is None:
                    cost = _decimal(data.get("usage"))
        except Exception:
            LOGGER.debug("OpenRouter generation cost is not ready: %s", generation_id)
        with _lock_for(path):
            ledger = _load(path)
            current = next(
                (
                    item
                    for item in ledger["events"]
                    if str(item.get("event_id")) == str(event.get("event_id"))
                ),
                None,
            )
            if current is None or current.get("status") != "pending":
                continue
            if cost is not None:
                current["cost_usd"] = str(cost)
                current["status"] = "confirmed"
                current["resolved_at"] = _now_iso()
                current.pop("next_retry_at", None)
                current.pop("reserved_cost_usd", None)
            else:
                attempts = int(current.get("retry_count") or 0) + 1
                current["retry_count"] = attempts
                delay = min(300, 5 * (2 ** min(attempts, 6)))
                current["next_retry_at"] = (_now() + timedelta(seconds=delay)).isoformat()
            _save(path, ledger)


def usage_summary(path: Path, *, create: bool = True) -> dict[str, Any]:
    try:
        with _lock_for(path):
            ledger = _load(path, create=create)
            events = ledger["events"]
            return {
                "status": "ok",
                "total_cost_usd": float(_total(events)),
                "pending_count": sum(item.get("status") == "pending" for item in events),
                "reserved_cost_usd": float(
                    sum(
                        (
                            _decimal(item.get("reserved_cost_usd")) or Decimal("0")
                            for item in events
                            if item.get("status") == "pending"
                        ),
                        Decimal("0"),
                    )
                ),
                "started_at": str(ledger.get("started_at") or ""),
            }
    except UsageLedgerError as exc:
        LOGGER.error("%s", exc)
        return {
            "status": "unavailable",
            "total_cost_usd": 0.0,
            "pending_count": 0,
            "reserved_cost_usd": 0.0,
            "started_at": "",
        }
