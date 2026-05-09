from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from automation.config import path_from_config
from automation.listing_enrichment import load_items_py
from automation.monitoring.send_telegram_alerts import alert_state_path_from
from automation.state import items_signature


TIER_ORDER = ("A", "B", "C")
DEFAULT_QUEUE_PATTERN = ("A", "A", "B", "A", "A", "B", "C")
TIER_ITEM_PATH_KEYS = {
    "A": "monitor_tier_a_items_py",
    "B": "monitor_tier_b_items_py",
    "C": "monitor_tier_c_items_py",
}
TIER_STATE_PATH_KEYS = {
    "A": "state_tier_a_json",
    "B": "state_tier_b_json",
    "C": "state_tier_c_json",
}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def tier_mode_enabled(config: dict[str, Any]) -> bool:
    return bool(config.get("cycle", {}).get("tiers", {}).get("enabled", False))


def tier_item_paths_from_config(config: dict[str, Any]) -> dict[str, Path]:
    return {tier: path_from_config(config, key) for tier, key in TIER_ITEM_PATH_KEYS.items()}


def tier_state_paths_from_config(config: dict[str, Any]) -> dict[str, Path]:
    return {tier: path_from_config(config, key) for tier, key in TIER_STATE_PATH_KEYS.items()}


def tiers_metadata_path_from_config(config: dict[str, Any]) -> Path:
    return path_from_config(config, "monitor_tiers_json")


def alert_state_json_from_config(config: dict[str, Any], *, fallback_state_json: Path) -> Path:
    configured = config.get("paths", {}).get("alert_state_json")
    if configured:
        return path_from_config(config, "alert_state_json")
    return alert_state_path_from(fallback_state_json)


def alert_monitor_items_py_from_config(config: dict[str, Any]) -> Path:
    return path_from_config(config, "monitor_items_py")


def resolve_batch_state_path(config: dict[str, Any], monitor_items_py: Path) -> Path:
    monitor_items_py = monitor_items_py.resolve()
    full_monitor_items_py = path_from_config(config, "monitor_items_py").resolve()
    tier_item_paths = {tier: path.resolve() for tier, path in tier_item_paths_from_config(config).items()}
    tier_state_paths = tier_state_paths_from_config(config)
    for tier, tier_path in tier_item_paths.items():
        if monitor_items_py == tier_path:
            return tier_state_paths[tier]
    if monitor_items_py == full_monitor_items_py:
        if tier_mode_enabled(config) and config.get("paths", {}).get("default_batch_state_json"):
            return path_from_config(config, "default_batch_state_json")
        return path_from_config(config, "state_json")
    return path_from_config(config, "state_json")


def queue_pattern_from_config(config: dict[str, Any]) -> list[str]:
    raw = config.get("cycle", {}).get("tiers", {}).get("queue_pattern", list(DEFAULT_QUEUE_PATTERN))
    pattern = [str(value).upper() for value in raw if str(value).upper() in TIER_ORDER]
    return pattern or list(DEFAULT_QUEUE_PATTERN)


def batch_sizes_from_config(config: dict[str, Any], *, default_batch_size: int) -> dict[str, int]:
    raw = config.get("cycle", {}).get("tiers", {}).get("batch_sizes", {})
    out: dict[str, int] = {}
    for tier in TIER_ORDER:
        value = raw.get(tier, default_batch_size)
        try:
            out[tier] = max(1, int(value))
        except Exception:
            out[tier] = max(1, int(default_batch_size))
    return out


def listing_caps_from_config(config: dict[str, Any], *, default_max_listings: int) -> dict[str, int]:
    raw = config.get("cycle", {}).get("tiers", {}).get("max_listings_per_item", {})
    out: dict[str, int] = {}
    for tier in TIER_ORDER:
        value = raw.get(tier, default_max_listings)
        try:
            out[tier] = max(1, int(value))
        except Exception:
            out[tier] = max(1, int(default_max_listings))
    return out


def load_tier_items(item_paths: dict[str, Path]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for tier, path in item_paths.items():
        out[tier] = load_items_py(path) if path.is_file() else []
    return out


def tier_items_match_full_list(all_items: list[str], tier_items: dict[str, list[str]]) -> bool:
    flattened: list[str] = []
    for tier in TIER_ORDER:
        flattened.extend(tier_items.get(tier, []))
    return len(flattened) == len(all_items) and set(flattened) == set(all_items)


def _normalize_shares(raw: dict[str, Any] | None) -> dict[str, float]:
    raw = raw or {}
    out = {}
    total = 0.0
    for tier in TIER_ORDER:
        try:
            value = max(0.0, float(raw.get(tier, 0.0)))
        except Exception:
            value = 0.0
        out[tier] = value
        total += value
    if total <= 0:
        out = {"A": 0.2, "B": 0.3, "C": 0.5}
        total = 1.0
    return {tier: value / total for tier, value in out.items()}


def _allocate_tier_counts(total: int, shares: dict[str, float]) -> dict[str, int]:
    if total <= 0:
        return {tier: 0 for tier in TIER_ORDER}
    shares = _normalize_shares(shares)
    exact = {tier: total * shares[tier] for tier in TIER_ORDER}
    base = {tier: int(np.floor(exact[tier])) for tier in TIER_ORDER}
    remaining = int(total - sum(base.values()))
    order = sorted(
        TIER_ORDER,
        key=lambda tier: (exact[tier] - base[tier], shares[tier], -TIER_ORDER.index(tier)),
        reverse=True,
    )
    for tier in order[:remaining]:
        base[tier] += 1
    return base


def _log_rank_pct(frame: pd.DataFrame, column: str) -> pd.Series:
    values = pd.to_numeric(frame.get(column), errors="coerce")
    values = values.clip(lower=0)
    logged = np.log1p(values)
    rank = logged.rank(method="average", pct=True)
    return rank.fillna(0.0)


def assign_monitor_tiers(
    frame: pd.DataFrame,
    *,
    shares: dict[str, Any] | None = None,
    sales_weight: float = 0.75,
    turnover_weight: float = 0.25,
) -> tuple[pd.DataFrame, dict[str, int], dict[str, float]]:
    out = frame.copy().reset_index(drop=True)
    if out.empty:
        out["liquidity_score"] = pd.Series(dtype=float)
        out["liquidity_rank"] = pd.Series(dtype=int)
        out["tier"] = pd.Series(dtype=str)
        return out, {tier: 0 for tier in TIER_ORDER}, _normalize_shares(shares)

    sales_rank = _log_rank_pct(out, "steam_sales_7d_n")
    turnover_rank = _log_rank_pct(out, "steam_turnover_proxy")
    total_weight = max(1e-9, float(sales_weight) + float(turnover_weight))
    out["liquidity_score"] = (
        float(sales_weight) * sales_rank + float(turnover_weight) * turnover_rank
    ) / total_weight

    sort_cols = ["liquidity_score"]
    ascending = [False]
    if "steam_sales_7d_n" in out.columns:
        sort_cols.append("steam_sales_7d_n")
        ascending.append(False)
    if "steam_turnover_proxy" in out.columns:
        sort_cols.append("steam_turnover_proxy")
        ascending.append(False)
    sort_cols.append("item")
    ascending.append(True)
    out = out.sort_values(sort_cols, ascending=ascending, na_position="last").reset_index(drop=True)
    out["liquidity_rank"] = np.arange(1, len(out) + 1, dtype=int)

    normalized_shares = _normalize_shares(shares)
    counts = _allocate_tier_counts(len(out), normalized_shares)
    tier_labels: list[str] = []
    for tier in TIER_ORDER:
        tier_labels.extend([tier] * counts[tier])
    if len(tier_labels) < len(out):
        tier_labels.extend(["C"] * (len(out) - len(tier_labels)))
    out["tier"] = tier_labels[: len(out)]
    return out, counts, normalized_shares


def write_tier_outputs(
    tiered_monitor_frame: pd.DataFrame,
    *,
    tier_item_paths: dict[str, Path],
    metadata_path: Path,
    source_csv: Path,
    counts: dict[str, int],
    shares: dict[str, float],
    score_weights: dict[str, float],
) -> dict[str, int]:
    from automation.risk_filters import write_items_py

    metadata = {
        "generated_at_utc": utc_now_iso(),
        "items_total": int(len(tiered_monitor_frame)),
        "shares": {tier: float(shares.get(tier, 0.0)) for tier in TIER_ORDER},
        "score_weights": {
            "steam_sales_7d_n": float(score_weights.get("steam_sales_7d_n", 0.75)),
            "steam_turnover_proxy": float(score_weights.get("steam_turnover_proxy", 0.25)),
        },
        "counts": {},
    }
    out_counts: dict[str, int] = {}
    for tier in TIER_ORDER:
        items = tiered_monitor_frame.loc[tiered_monitor_frame["tier"] == tier, "item"].dropna().astype(str).tolist()
        write_items_py(items, tier_item_paths[tier], source_csv=source_csv, counts=counts)
        out_counts[tier] = len(items)
        metadata["counts"][tier] = len(items)
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return out_counts


def scheduler_queue_signature(tier_items: dict[str, list[str]], queue_pattern: list[str]) -> str:
    payload = {
        "queue_pattern": list(queue_pattern),
        "tier_signatures": {tier: items_signature(tier_items.get(tier, [])) for tier in TIER_ORDER},
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def default_scheduler_state(
    all_items: list[str],
    tier_items: dict[str, list[str]],
    queue_pattern: list[str],
) -> dict[str, Any]:
    return {
        "version": 1,
        "mode": "tiered_weighted_sequence",
        "items_signature": items_signature(all_items),
        "items_count": len(all_items),
        "queue_signature": scheduler_queue_signature(tier_items, queue_pattern),
        "queue_pattern": list(queue_pattern),
        "queue_pointer": 0,
        "last_tier": None,
        "last_tier_queue_index": None,
        "last_batch_start_pointer": None,
        "last_batch_items": [],
        "last_error": None,
        "last_run_at_utc": None,
        "last_finished_at_utc": None,
        "last_successful_monitoring_at_utc": None,
        "last_failed_monitoring_at_utc": None,
        "last_status": None,
        "consecutive_errors": 0,
        "last_listing_errors": [],
        "last_listing_error_count": 0,
        "last_listing_rows": 0,
        "last_enriched_rows": 0,
        "last_opportunities_rows": 0,
        "last_alert_stats": {},
        "tiers": {
            tier: {
                "items_signature": items_signature(tier_items.get(tier, [])),
                "items_count": len(tier_items.get(tier, [])),
            }
            for tier in TIER_ORDER
        },
    }


def load_scheduler_state(
    path: Path,
    all_items: list[str],
    tier_items: dict[str, list[str]],
    queue_pattern: list[str],
) -> dict[str, Any]:
    expected_items_sig = items_signature(all_items)
    expected_queue_sig = scheduler_queue_signature(tier_items, queue_pattern)
    if not path.is_file():
        return default_scheduler_state(all_items, tier_items, queue_pattern)
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default_scheduler_state(all_items, tier_items, queue_pattern)
    if not isinstance(state, dict):
        return default_scheduler_state(all_items, tier_items, queue_pattern)
    if str(state.get("items_signature") or "") != expected_items_sig:
        return default_scheduler_state(all_items, tier_items, queue_pattern)
    if str(state.get("queue_signature") or "") != expected_queue_sig:
        return default_scheduler_state(all_items, tier_items, queue_pattern)
    return state


def save_scheduler_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def select_next_tier(
    state: dict[str, Any],
    queue_pattern: list[str],
    tier_items: dict[str, list[str]],
) -> tuple[str, int, int]:
    if not queue_pattern:
        raise RuntimeError("tier queue pattern is empty")
    start = int(state.get("queue_pointer") or 0) % len(queue_pattern)
    for offset in range(len(queue_pattern)):
        queue_index = (start + offset) % len(queue_pattern)
        tier = queue_pattern[queue_index]
        if tier_items.get(tier):
            next_pointer = (queue_index + 1) % len(queue_pattern)
            return tier, queue_index, next_pointer
    raise RuntimeError("all monitoring tiers are empty")


def mark_scheduler_run_started(
    state: dict[str, Any],
    *,
    tier: str,
    queue_index: int,
    batch_items: list[str],
    tier_start_pointer: int,
) -> dict[str, Any]:
    out = dict(state)
    out["last_run_at_utc"] = utc_now_iso()
    out["last_status"] = "running"
    out["last_tier"] = str(tier)
    out["last_tier_queue_index"] = int(queue_index)
    out["last_batch_start_pointer"] = int(tier_start_pointer)
    out["last_batch_items"] = list(batch_items)
    out["last_error"] = None
    return out


def mark_scheduler_run_finished(
    state: dict[str, Any],
    *,
    tier: str,
    queue_index: int,
    next_queue_pointer: int,
    status: str,
    tier_state: dict[str, Any],
    error: str | None = None,
) -> dict[str, Any]:
    out = dict(state)
    out["last_tier"] = str(tier)
    out["last_tier_queue_index"] = int(queue_index)
    out["queue_pointer"] = int(next_queue_pointer if status == "ok" else queue_index)
    finished_at = utc_now_iso()
    out["last_finished_at_utc"] = finished_at
    out["last_status"] = status
    out["last_error"] = error
    out["last_batch_start_pointer"] = int(tier_state.get("last_batch_start_pointer") or out.get("last_batch_start_pointer") or 0)
    out["last_batch_items"] = list(tier_state.get("last_batch_items") or [])
    if status == "ok":
        out["last_successful_monitoring_at_utc"] = finished_at
        out["consecutive_errors"] = 0
    elif status == "error":
        out["last_failed_monitoring_at_utc"] = finished_at
        out["consecutive_errors"] = int(out.get("consecutive_errors") or 0) + 1
    out["last_listing_errors"] = list(tier_state.get("last_listing_errors") or [])[:20]
    out["last_listing_error_count"] = int(tier_state.get("last_listing_error_count") or len(out["last_listing_errors"]))
    out["last_listing_rows"] = int(tier_state.get("last_listing_rows") or 0)
    out["last_enriched_rows"] = int(tier_state.get("last_enriched_rows") or 0)
    out["last_opportunities_rows"] = int(tier_state.get("last_opportunities_rows") or 0)
    out["last_alert_stats"] = dict(tier_state.get("last_alert_stats") or {})
    return out
