"""
Collect a realtime CSFloat base-price snapshot per item into a standalone CSV.

This stays separate from Steam listing rows on purpose:
- `steam_scm_listings.py` writes Steam SCM listing-level data
- this module writes item-level CSFloat reference context used as the
  absolute anchor for relative float curves during Steam listing analysis
"""

from __future__ import annotations

from queue import Queue
import threading
import time
from pathlib import Path
import random
from typing import Any

import pandas as pd

import steam_scm_listings as scm


DEFAULT_OUT_CSV = Path(__file__).resolve().parent / "data" / "scm_realtime_base.csv"


def _load_fetchers_module():
    import sys

    repo_root = Path(__file__).resolve().parent.parent
    repo_root_str = str(repo_root)
    if repo_root_str not in sys.path:
        sys.path.insert(0, repo_root_str)

    from base_screening_with_trades import fetchers

    return fetchers


def _one_row(item: str, payload: dict | None) -> dict:
    row = {
        "item": item,
        "base_usd": None,
        "base_eur": None,
        "predicted_usd": None,
        "predicted_eur": None,
        "quantity": None,
        "reference_currency": "USD",
        "fx_usd_to_eur": None,
        "fx_source": None,
        "base_collected_at_utc": pd.Timestamp.utcnow().isoformat(),
        "status": "ok",
        "error": None,
    }
    if not payload:
        row["status"] = "error"
        row["error"] = "empty_csfloat_payload"
        return row

    row["base_usd"] = payload.get("base")
    row["predicted_usd"] = payload.get("predicted")
    row["quantity"] = payload.get("quantity")
    if row["base_usd"] is None:
        row["status"] = "error"
        row["error"] = "missing_base_price"
    return row


def _fetch_one_base_row(
    item: str,
    *,
    fetchers: Any,
    fx_usd_to_eur: float,
    fx_source: str,
) -> dict:
    try:
        payload = fetchers.get_csfloat_prices(item)
        row = _one_row(item, payload)
    except Exception as exc:
        row = {
            "item": item,
            "base_usd": None,
            "base_eur": None,
            "predicted_usd": None,
            "predicted_eur": None,
            "quantity": None,
            "reference_currency": "USD",
            "fx_usd_to_eur": fx_usd_to_eur,
            "fx_source": fx_source,
            "base_collected_at_utc": pd.Timestamp.utcnow().isoformat(),
            "status": "error",
            "error": str(exc),
        }

    row["fx_usd_to_eur"] = fx_usd_to_eur
    row["fx_source"] = fx_source
    if row.get("base_usd") is not None:
        row["base_eur"] = float(row["base_usd"]) * float(fx_usd_to_eur)
    if row.get("predicted_usd") is not None:
        row["predicted_eur"] = float(row["predicted_usd"]) * float(fx_usd_to_eur)
    return row


def _steam_rows_for_item(item: str, rows: list[dict], meta: dict[str, Any]) -> list[dict[str, Any]]:
    total_count = meta.get("total_count")
    out: list[dict[str, Any]] = []
    for r in rows:
        out.append(
            {
                "market_hash_name": item,
                "listing_id": r.get("listing_id"),
                "asset_id": r.get("asset_id"),
                "ask": r.get("ask"),
                "ask_seller_net": r.get("ask_seller_net"),
                "float_value": r.get("float_value"),
                "paint_seed": r.get("paint_seed"),
                "asset_properties_json": r.get("asset_properties_json"),
                "converted_price": r.get("converted_price"),
                "converted_fee": r.get("converted_fee"),
                "converted_currencyid": r.get("converted_currencyid"),
                "scm_total_listings": total_count,
            }
        )
    return out


def run_items_to_csv(
    items: list[str],
    out_csv: str | Path | None = None,
    *,
    delay_min_sec: float = 0.0,
    delay_max_sec: float = 0.0,
) -> tuple[Path, list[dict], pd.DataFrame]:
    fetchers = _load_fetchers_module()
    out = Path(out_csv) if out_csv is not None else DEFAULT_OUT_CSV
    out.parent.mkdir(parents=True, exist_ok=True)
    fx_usd_to_eur, fx_source = fetchers.fetch_usd_to_eur_multiplier()

    rows: list[dict] = []
    errors: list[dict] = []
    total = len(items)

    for idx, item in enumerate(items, start=1):
        started = time.perf_counter()
        try:
            payload = fetchers.get_csfloat_prices(item)
            row = _one_row(item, payload)
        except Exception as exc:
            row = {
                "item": item,
                "base_usd": None,
                "base_eur": None,
                "predicted_usd": None,
                "predicted_eur": None,
                "quantity": None,
                "reference_currency": "USD",
                "fx_usd_to_eur": fx_usd_to_eur,
                "fx_source": fx_source,
                "base_collected_at_utc": pd.Timestamp.utcnow().isoformat(),
                "status": "error",
                "error": str(exc),
            }

        row["fx_usd_to_eur"] = fx_usd_to_eur
        row["fx_source"] = fx_source
        if row.get("base_usd") is not None:
            row["base_eur"] = float(row["base_usd"]) * float(fx_usd_to_eur)
        if row.get("predicted_usd") is not None:
            row["predicted_eur"] = float(row["predicted_usd"]) * float(fx_usd_to_eur)
        rows.append(row)
        if row["status"] != "ok":
            errors.append({"item": item, "error": row["error"]})

        elapsed = time.perf_counter() - started
        print(
            f'[scm_realtime_base] {idx}/{total} "{item}"  '
            f'base_usd={row.get("base_usd")} base_eur={row.get("base_eur")} '
            f'status={row["status"]} {elapsed:.1f}s',
            flush=True,
        )

        if idx < total and max(delay_min_sec, delay_max_sec) > 0:
            lo = min(delay_min_sec, delay_max_sec)
            hi = max(delay_min_sec, delay_max_sec)
            time.sleep(random.uniform(lo, hi))

    df = pd.DataFrame(rows).sort_values("item").reset_index(drop=True)
    df.to_csv(out, index=False)
    return out, errors, df


def run_steam_and_base_pipeline(
    items: list[str],
    *,
    listings_out_csv: str | Path | None = None,
    base_out_csv: str | Path | None = None,
    session: Any | None = None,
) -> tuple[Path, list[dict], pd.DataFrame, Path, list[dict], pd.DataFrame]:
    fetchers = _load_fetchers_module()
    fx_usd_to_eur, fx_source = fetchers.fetch_usd_to_eur_multiplier()

    listings_out = Path(listings_out_csv or scm._effective("batch_out_csv"))
    listings_out.parent.mkdir(parents=True, exist_ok=True)
    base_out = Path(base_out_csv) if base_out_csv is not None else DEFAULT_OUT_CSV
    base_out.parent.mkdir(parents=True, exist_ok=True)

    sess = session or scm._session()

    listing_rows: list[dict[str, Any]] = []
    listing_errors: list[dict[str, Any]] = []
    base_rows: list[dict[str, Any]] = []
    base_errors: list[dict[str, Any]] = []
    base_rows_lock = threading.Lock()
    base_queue: Queue[tuple[int, str] | None] = Queue(maxsize=1)

    def _base_worker() -> None:
        while True:
            task = base_queue.get()
            if task is None:
                base_queue.task_done()
                break
            idx, item = task
            t0 = time.perf_counter()
            row = _fetch_one_base_row(
                item,
                fetchers=fetchers,
                fx_usd_to_eur=fx_usd_to_eur,
                fx_source=fx_source,
            )
            dt = time.perf_counter() - t0
            with base_rows_lock:
                base_rows.append({"_idx": idx, **row})
                if row["status"] != "ok":
                    base_errors.append({"item": item, "error": row["error"]})
            print(
                f'[scm_realtime_base] {idx + 1}/{len(items)} "{item}"  '
                f'base_usd={row.get("base_usd")} base_eur={row.get("base_eur")} '
                f'status={row["status"]} {dt:.1f}s',
                flush=True,
            )
            base_queue.task_done()

    worker = threading.Thread(target=_base_worker, name="scm-realtime-base", daemon=True)
    worker.start()

    n = len(items)
    for i, item in enumerate(items):
        t0 = time.monotonic()
        label = f"{i + 1}/{n} {item}"
        scm._batch_log(f'  [steam_scm] >> батч {label} (delay_between_skins_* после предмета)')
        rows, meta = scm.fetch_steam_scm_top_listings(item, session=sess, log_skin_label=label)
        dt = time.monotonic() - t0
        scm._batch_log(
            f'  [steam_scm] ok батч {label}: {len(rows)} строк за {dt:.1f}s '
            f"(pages={meta.get('pages_fetched')}, cap={meta.get('listings_target_cap')})"
        )
        if not rows and meta.get("note") != "no_offers":
            listing_errors.append({"market_hash_name": item, "meta": meta})
        listing_rows.extend(_steam_rows_for_item(item, rows, meta))

        # bounded queue: if CSFloat lags, Steam waits here instead of building a burst
        base_queue.put((i, item))

        if i + 1 < n:
            d_lo = float(scm._effective("delay_between_skins_min_sec"))
            d_hi = float(scm._effective("delay_between_skins_max_sec"))
            time.sleep(random.uniform(d_lo, d_hi))

    base_queue.put(None)
    worker.join()

    listings_df = pd.DataFrame(listing_rows)
    listings_df.to_csv(listings_out, index=False)

    base_df = pd.DataFrame(base_rows)
    if not base_df.empty and "_idx" in base_df.columns:
        base_df = base_df.sort_values("_idx").drop(columns="_idx").reset_index(drop=True)
    base_df.to_csv(base_out, index=False)

    return listings_out, listing_errors, listings_df, base_out, base_errors, base_df


__all__ = ["DEFAULT_OUT_CSV", "run_items_to_csv", "run_steam_and_base_pipeline"]
