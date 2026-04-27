"""
Lightweight CSFloat preprocess screener.

Goal:
- cheap first-pass scan over a large skin list;
- collect a stable base price, a capped liquidity proxy, and a simple discount metric;
- feed a filtering notebook that exports a smaller Python item list for the heavy screener.

Output:
- one CSV with rows=items and metrics columns.

Metrics:
- base_price: first non-null CSFloat reference base price
- n_listings: observed active buy-now listings, capped by LISTINGS_CAP
- cap_hit: whether observed listings reached the cap
- avg_discount: mean(1 - ask / predicted_price) on first DISCOUNT_SAMPLE_LIMIT rows

Run modes:
- create: wipe output CSV/log and rescan the full list from scratch
- merge: resume-style mode; skip items already present in the output CSV and only collect the rest
"""

from __future__ import annotations

import importlib.util
import json
import os
import random
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd


SCRIPT_DIR = Path(__file__).resolve().parent
SKIN_HOMOG_DIR = SCRIPT_DIR.parent
REPO_ROOT = SKIN_HOMOG_DIR.parent

DEFAULT_RUNTIME_JSON = SCRIPT_DIR / "preprocess_runtime.json"
DEFAULT_OUTPUT_CSV = SCRIPT_DIR / "preprocess_metrics.csv"
DEFAULT_PROGRESS_LOG = SCRIPT_DIR / "_preprocess_progress.log"

DEFAULT_SORT = "most_recent"
DEFAULT_LISTINGS_CAP = 20
DEFAULT_DISCOUNT_SAMPLE_LIMIT = 8
DEFAULT_CREATE_MODE = "create"
DEFAULT_ITEM_DELAY_MIN = 0.0
DEFAULT_ITEM_DELAY_MAX = 0.0


def _log(msg: str = "") -> None:
    print(msg, flush=True)
    try:
        sys.stdout.flush()
    except Exception:
        pass


def _runtime_path() -> Path:
    raw = os.environ.get("PREPROCESS_SCREENER_RUNTIME_CONFIG")
    if raw:
        return Path(raw).expanduser().resolve()
    return DEFAULT_RUNTIME_JSON


def _load_runtime() -> dict:
    path = _runtime_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        _log(f"[preprocess] broken JSON in {path}: {exc}")
        return {}


def _rt_str(cfg: dict, key: str, default: str) -> str:
    value = cfg.get(key, default)
    text = str(value).strip()
    return text or default


def _rt_int(cfg: dict, key: str, default: int) -> int:
    try:
        return max(1, int(cfg.get(key, default)))
    except (TypeError, ValueError):
        return default


def _rt_float(cfg: dict, key: str, default: float) -> float:
    try:
        return float(cfg.get(key, default))
    except (TypeError, ValueError):
        return default


def _resolve_path(raw: str | os.PathLike[str], base: Path) -> Path:
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = (base / path).resolve()
    return path


def _load_skin_screener(runtime_json: Path):
    os.environ["SKIN_SCREENER_RUNTIME_CONFIG"] = str(runtime_json)
    module_path = SKIN_HOMOG_DIR / "skin_screener.py"
    spec = importlib.util.spec_from_file_location("skin_screener_preprocess_base", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot import {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _cents_to_major(value: object) -> float | None:
    try:
        return float(value) / 100.0
    except (TypeError, ValueError):
        return None


def _listing_metrics(listing: dict) -> tuple[float | None, float | None, float | None, str | None]:
    ref = listing.get("reference", {}) or {}
    ask = _cents_to_major(listing.get("price"))
    predicted = _cents_to_major(ref.get("predicted_price"))
    base = _cents_to_major(ref.get("base_price"))
    currency = ref.get("currency") or listing.get("currency")
    return ask, predicted, base, currency


def _summarise_item(
    *,
    name: str,
    listings: list[dict],
    sample_limit: int,
    listings_cap: int,
    sort_by: str,
    key_trace: str,
    error: str | None,
) -> dict:
    base_price = None
    currency = None
    discounts: list[float] = []
    asks: list[float] = []
    preds: list[float] = []

    for listing in listings:
        ask, predicted, base, listing_currency = _listing_metrics(listing)
        if base_price is None and base is not None:
            base_price = base
        if currency is None and listing_currency is not None:
            currency = str(listing_currency)

    for listing in listings[:sample_limit]:
        ask, predicted, _, _ = _listing_metrics(listing)
        if ask is None or predicted is None or predicted <= 0:
            continue
        asks.append(ask)
        preds.append(predicted)
        discounts.append(1.0 - ask / predicted)

    return {
        "item": name,
        "status": "ok" if error is None else "error",
        "error": error,
        "sort_by": sort_by,
        "collected_at_utc": pd.Timestamp.utcnow().isoformat(),
        "base_price": base_price,
        "currency": currency,
        "n_listings": len(listings),
        "cap_hit": len(listings) >= listings_cap,
        "discount_sample_n": len(discounts),
        "avg_discount": (sum(discounts) / len(discounts)) if discounts else None,
        "median_discount": float(np.median(discounts)) if discounts else None,
        "avg_ask": (sum(asks) / len(asks)) if asks else None,
        "avg_predicted": (sum(preds) / len(preds)) if preds else None,
        "key_trace": key_trace or None,
    }


def _load_existing(output_csv: Path) -> pd.DataFrame:
    if not output_csv.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(output_csv)
    except Exception:
        return pd.DataFrame()


def _save_rows(output_csv: Path, rows: list[dict], *, mode: str) -> pd.DataFrame:
    new_df = pd.DataFrame(rows)
    if mode == "merge" and output_csv.exists():
        old_df = _load_existing(output_csv)
        if not old_df.empty and "item" in old_df.columns:
            old_df = old_df[~old_df["item"].isin(new_df["item"])]
        merged = pd.concat([old_df, new_df], ignore_index=True)
    else:
        merged = new_df
    merged = merged.sort_values("item").reset_index(drop=True)
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    merged.to_csv(output_csv, index=False)
    return merged


def _existing_item_names(output_csv: Path) -> set[str]:
    df = _load_existing(output_csv)
    if df.empty or "item" not in df.columns:
        return set()
    return {str(x) for x in df["item"].dropna().astype(str)}


def _write_progress(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def parse_cli(argv: list[str], cfg: dict) -> tuple[str, str, int, int, Path, Path, Path]:
    mode = _rt_str(cfg, "DEFAULT_RUN_MODE", DEFAULT_CREATE_MODE)
    sort_by = _rt_str(cfg, "DEFAULT_SORT", DEFAULT_SORT)
    listings_cap = _rt_int(cfg, "LISTINGS_CAP", DEFAULT_LISTINGS_CAP)
    sample_limit = _rt_int(cfg, "DISCOUNT_SAMPLE_LIMIT", DEFAULT_DISCOUNT_SAMPLE_LIMIT)
    list_path = _resolve_path(_rt_str(cfg, "DEFAULT_LIST_PATH", "../lists/skins_normal.py"), SCRIPT_DIR)
    output_csv = _resolve_path(_rt_str(cfg, "OUTPUT_CSV", "preprocess_metrics.csv"), SCRIPT_DIR)
    progress_log = _resolve_path(_rt_str(cfg, "PROGRESS_LOG", "_preprocess_progress.log"), SCRIPT_DIR)

    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--create":
            mode = "create"
        elif arg == "--merge":
            mode = "merge"
        elif arg == "--sort":
            i += 1
            sort_by = argv[i]
        elif arg == "--cap":
            i += 1
            listings_cap = max(1, int(argv[i]))
        elif arg == "--sample":
            i += 1
            sample_limit = max(1, int(argv[i]))
        elif arg == "--output":
            i += 1
            output_csv = _resolve_path(argv[i], Path.cwd())
        elif arg == "--progress-log":
            i += 1
            progress_log = _resolve_path(argv[i], Path.cwd())
        else:
            list_path = _resolve_path(arg, Path.cwd())
        i += 1

    sample_limit = min(sample_limit, listings_cap)
    return mode, sort_by, listings_cap, sample_limit, list_path, output_csv, progress_log


def main() -> int:
    cfg = _load_runtime()
    runtime_json = _runtime_path()
    skin_screener = _load_skin_screener(runtime_json)

    mode, sort_by, listings_cap, sample_limit, list_path, output_csv, progress_log = parse_cli(sys.argv[1:], cfg)
    item_delay_min = _rt_float(cfg, "PREPROCESS_ITEM_DELAY_MIN", DEFAULT_ITEM_DELAY_MIN)
    item_delay_max = _rt_float(cfg, "PREPROCESS_ITEM_DELAY_MAX", DEFAULT_ITEM_DELAY_MAX)
    if item_delay_max < item_delay_min:
        item_delay_max = item_delay_min

    items = skin_screener.load_items(str(list_path))
    _log(f"[preprocess] mode={mode} sort={sort_by} cap={listings_cap} sample={sample_limit}")
    _log(f"[preprocess] items={len(items)} from {list_path}")
    _log(f"[preprocess] runtime={runtime_json}")
    _log(f"[preprocess] output={output_csv}")

    if mode == "create":
        if output_csv.exists():
            output_csv.unlink()
        if progress_log.exists():
            progress_log.unlink()

    saved_rows = _load_existing(output_csv).to_dict("records") if mode == "merge" else []
    current_by_item = {row["item"]: row for row in saved_rows if "item" in row}
    skip_items: set[str] = set()
    if mode == "merge":
        skip_items = _existing_item_names(output_csv)
        if skip_items:
            _log(f"[preprocess] merge resume: skip existing items already in CSV = {len(skip_items)}")

    items_to_run = [name for name in items if name not in skip_items]
    total = len(items_to_run)
    if mode == "merge":
        _log(f"[preprocess] merge resume: remaining items to collect = {total}")
    merged_df = pd.DataFrame(list(current_by_item.values()))
    for idx, name in enumerate(items_to_run, start=1):
        started = time.perf_counter()
        listings, error, key_trace_list = skin_screener.fetch_listings(
            name,
            sort_by=sort_by,
            cap=listings_cap,
        )
        key_trace = "->".join(key_trace_list)
        row = _summarise_item(
            name=name,
            listings=listings,
            sample_limit=sample_limit,
            listings_cap=listings_cap,
            sort_by=sort_by,
            key_trace=key_trace,
            error=error,
        )
        current_by_item[name] = row
        merged_df = _save_rows(output_csv, list(current_by_item.values()), mode="create")
        elapsed = time.perf_counter() - started
        line = (
            f'{idx}/{total} "{name}"  status={row["status"]}  '
            f'n={row["n_listings"]}/{listings_cap}  '
            f'base={row["base_price"]}  '
            f'discount_n={row["discount_sample_n"]}  '
            f'avg_discount={row["avg_discount"]}  '
            f'{elapsed:.1f}s'
        )
        _log(line)
        _write_progress(progress_log, line)
        if idx < total and item_delay_max > 0:
            time.sleep(random.uniform(item_delay_min, item_delay_max))

    _log("")
    _log(f"[preprocess] saved {len(merged_df)} rows -> {output_csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
