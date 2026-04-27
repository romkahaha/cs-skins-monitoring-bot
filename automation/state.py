"""Small JSON state store for scheduled monitoring batches."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def items_signature(items: list[str]) -> str:
    payload = "\n".join(str(x) for x in items)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def default_state(items: list[str]) -> dict[str, Any]:
    return {
        "version": 1,
        "items_signature": items_signature(items),
        "items_count": len(items),
        "batch_pointer": 0,
        "last_run_at_utc": None,
        "last_status": None,
        "last_batch_items": [],
        "last_error": None,
        "last_finished_at_utc": None,
        "last_successful_monitoring_at_utc": None,
        "last_failed_monitoring_at_utc": None,
        "consecutive_errors": 0,
        "last_listing_errors": [],
        "last_listing_error_count": 0,
        "last_listing_rows": 0,
        "last_enriched_rows": 0,
        "last_opportunities_rows": 0,
        "last_alert_stats": {},
    }


def load_state(path: Path, items: list[str]) -> dict[str, Any]:
    sig = items_signature(items)
    if not path.is_file():
        return default_state(items)
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default_state(items)
    if not isinstance(state, dict):
        return default_state(items)
    if state.get("items_signature") != sig or int(state.get("items_count") or -1) != len(items):
        return default_state(items)
    return state


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def select_batch(items: list[str], state: dict[str, Any], batch_size: int) -> tuple[list[str], int, int]:
    if not items:
        return [], 0, 0
    n = len(items)
    size = max(1, min(int(batch_size), n))
    start = int(state.get("batch_pointer") or 0) % n
    batch = [items[(start + offset) % n] for offset in range(size)]
    next_pointer = (start + size) % n
    return batch, start, next_pointer


def mark_run_started(state: dict[str, Any], batch: list[str], start_pointer: int) -> dict[str, Any]:
    out = dict(state)
    out["last_run_at_utc"] = utc_now_iso()
    out["last_status"] = "running"
    out["last_batch_start_pointer"] = int(start_pointer)
    out["last_batch_items"] = list(batch)
    out["last_error"] = None
    return out


def mark_run_finished(
    state: dict[str, Any],
    *,
    next_pointer: int,
    status: str,
    error: str | None = None,
    listing_errors: list[dict[str, Any]] | None = None,
    listing_rows: int | None = None,
    enriched_rows: int | None = None,
    opportunities_rows: int | None = None,
    alert_stats: dict[str, Any] | None = None,
) -> dict[str, Any]:
    out = dict(state)
    out["batch_pointer"] = int(next_pointer)
    finished_at = utc_now_iso()
    out["last_finished_at_utc"] = finished_at
    out["last_status"] = status
    out["last_error"] = error
    if status == "ok":
        out["last_successful_monitoring_at_utc"] = finished_at
        out["consecutive_errors"] = 0
    elif status == "error":
        out["last_failed_monitoring_at_utc"] = finished_at
        out["consecutive_errors"] = int(out.get("consecutive_errors") or 0) + 1
    if listing_errors is not None:
        out["last_listing_errors"] = listing_errors[:20]
        out["last_listing_error_count"] = len(listing_errors)
    if listing_rows is not None:
        out["last_listing_rows"] = int(listing_rows)
    if enriched_rows is not None:
        out["last_enriched_rows"] = int(enriched_rows)
    if opportunities_rows is not None:
        out["last_opportunities_rows"] = int(opportunities_rows)
    if alert_stats is not None:
        out["last_alert_stats"] = dict(alert_stats)
    return out
