"""Build a CSFloat base/predicted snapshot for the current monitor list."""

from __future__ import annotations

import argparse
import importlib.util
import random
import sys
import time
from pathlib import Path

import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from automation.config import load_json_config, nightly_defaults, path_from_config
from automation.listing_enrichment import load_items_py
from automation.risk_filters import repo_root_from


BASE_COLUMNS = [
    "item",
    "base_usd",
    "base_eur",
    "predicted_usd",
    "predicted_eur",
    "quantity",
    "reference_currency",
    "fx_usd_to_eur",
    "fx_source",
    "base_collected_at_utc",
    "status",
    "error",
]


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _load_scm_realtime_base(repo_root: Path):
    steam_dir = repo_root / "steam_listings"
    steam_dir_str = str(steam_dir)
    if steam_dir_str not in sys.path:
        sys.path.insert(0, steam_dir_str)
    module_path = steam_dir / "scm_realtime_base.py"
    spec = importlib.util.spec_from_file_location("automation_scm_realtime_base_snapshot", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot import {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def parse_args() -> argparse.Namespace:
    root = repo_root_from(Path(__file__))
    parser = argparse.ArgumentParser(description="Build base_snapshot_latest.csv for current monitor items.")
    parser.add_argument(
        "--config",
        type=Path,
        default=root / "automation" / "configs" / "nightly.json",
        help="Nightly automation JSON config.",
    )
    parser.add_argument(
        "--monitor-items-py",
        type=Path,
        default=None,
        help="Python item list with ITEMS = [...].",
    )
    parser.add_argument(
        "--out-csv",
        type=Path,
        default=None,
        help="Output CSFloat base snapshot CSV.",
    )
    parser.add_argument("--delay-min-sec", type=float, default=0.0, help="Optional delay between CSFloat base requests.")
    parser.add_argument("--delay-max-sec", type=float, default=0.0, help="Optional delay between CSFloat base requests.")
    return parser.parse_args()


def is_rate_limit_error(error: object, patterns: list[str]) -> bool:
    text = str(error or "").lower()
    return any(pattern.lower() in text for pattern in patterns)


def fetch_one_row_with_rate_limit_retry(
    item: str,
    *,
    scm_realtime_base,
    fetchers,
    fx_usd_to_eur: float,
    fx_source: str,
    rate_limit_pause_sec: float,
    rate_limit_stair_step_sec: float,
    rate_limit_max_retries: int | None,
    rate_limit_error_patterns: list[str],
) -> tuple[dict, int]:
    retry_n = 0
    while True:
        row = scm_realtime_base._fetch_one_base_row(
            item,
            fetchers=fetchers,
            fx_usd_to_eur=fx_usd_to_eur,
            fx_source=fx_source,
        )
        if row.get("status") == "ok":
            return row, retry_n
        if not is_rate_limit_error(row.get("error"), rate_limit_error_patterns):
            return row, retry_n
        if rate_limit_max_retries is not None and retry_n >= rate_limit_max_retries:
            return row, retry_n

        sleep_sec = float(rate_limit_pause_sec) + retry_n * float(rate_limit_stair_step_sec)
        print(
            f'[scm_realtime_base] 429/rate-limit for "{item}", '
            f"sleep {sleep_sec / 60.0:.1f} min before retry {retry_n + 1}",
            flush=True,
        )
        time.sleep(sleep_sec)
        retry_n += 1


def write_partial(out_csv: Path, rows: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    if df.empty:
        df = pd.DataFrame(columns=BASE_COLUMNS)
    else:
        df = df[[c for c in BASE_COLUMNS if c in df.columns] + [c for c in df.columns if c not in BASE_COLUMNS]]
        df = df.sort_values("item").reset_index(drop=True)
    df.to_csv(out_csv, index=False)
    return df


def run_items_to_csv_with_rate_limit_retry(
    scm_realtime_base,
    items: list[str],
    out_csv: Path,
    *,
    delay_min_sec: float,
    delay_max_sec: float,
    rate_limit_pause_sec: float,
    rate_limit_stair_step_sec: float,
    rate_limit_max_retries: int | None,
    rate_limit_error_patterns: list[str],
) -> tuple[Path, list[dict], pd.DataFrame]:
    fetchers = scm_realtime_base._load_fetchers_module()
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    fx_usd_to_eur, fx_source = fetchers.fetch_usd_to_eur_multiplier()

    rows: list[dict] = []
    errors: list[dict] = []
    total = len(items)

    for idx, item in enumerate(items, start=1):
        started = time.perf_counter()
        row, rate_limit_retries = fetch_one_row_with_rate_limit_retry(
            item,
            scm_realtime_base=scm_realtime_base,
            fetchers=fetchers,
            fx_usd_to_eur=fx_usd_to_eur,
            fx_source=fx_source,
            rate_limit_pause_sec=rate_limit_pause_sec,
            rate_limit_stair_step_sec=rate_limit_stair_step_sec,
            rate_limit_max_retries=rate_limit_max_retries,
            rate_limit_error_patterns=rate_limit_error_patterns,
        )
        if rate_limit_retries:
            row["rate_limit_retries"] = rate_limit_retries
        rows.append(row)
        if row.get("status") != "ok":
            errors.append({"item": item, "error": row.get("error")})

        df = write_partial(out_csv, rows)
        elapsed = time.perf_counter() - started
        print(
            f'[scm_realtime_base] {idx}/{total} "{item}"  '
            f'base_usd={row.get("base_usd")} base_eur={row.get("base_eur")} '
            f'status={row.get("status")} retries_429={rate_limit_retries} {elapsed:.1f}s',
            flush=True,
        )

        if idx < total and max(delay_min_sec, delay_max_sec) > 0:
            lo = min(delay_min_sec, delay_max_sec)
            hi = max(delay_min_sec, delay_max_sec)
            time.sleep(random.uniform(lo, hi))

    return out_csv, errors, df


def main() -> int:
    configure_stdio()
    args = parse_args()
    config = load_json_config(args.config.resolve() if args.config else None, nightly_defaults())
    repo_root = repo_root_from(Path(__file__))
    monitor_items_py = args.monitor_items_py.resolve() if args.monitor_items_py else path_from_config(config, "monitor_items_py")
    out_csv = args.out_csv.resolve() if args.out_csv else path_from_config(config, "base_snapshot_csv")
    base_cfg = config.get("base_snapshot", {})
    delay_min_sec = args.delay_min_sec if args.delay_min_sec != 0.0 else float(base_cfg.get("delay_min_sec", 0.0))
    delay_max_sec = args.delay_max_sec if args.delay_max_sec != 0.0 else float(base_cfg.get("delay_max_sec", 0.0))
    rate_limit_max_retries = base_cfg.get("rate_limit_max_retries", 5)
    if rate_limit_max_retries is not None:
        rate_limit_max_retries = int(rate_limit_max_retries)
    rate_limit_error_patterns = [str(x) for x in base_cfg.get("rate_limit_error_patterns", ["429"])]

    items = load_items_py(monitor_items_py)
    if not items:
        print(f"no items in {monitor_items_py}")
        return 1

    scm_realtime_base = _load_scm_realtime_base(repo_root)
    out_path, errors, df = run_items_to_csv_with_rate_limit_retry(
        scm_realtime_base,
        items,
        out_csv,
        delay_min_sec=delay_min_sec,
        delay_max_sec=delay_max_sec,
        rate_limit_pause_sec=float(base_cfg.get("rate_limit_pause_sec", 900.0)),
        rate_limit_stair_step_sec=float(base_cfg.get("rate_limit_stair_step_sec", 60.0)),
        rate_limit_max_retries=rate_limit_max_retries,
        rate_limit_error_patterns=rate_limit_error_patterns,
    )
    print(f"config: {args.config.resolve() if args.config else '<defaults>'}")
    print(f"monitor items: {len(items)} from {monitor_items_py}")
    print(f"base snapshot rows: {len(df)}")
    print(f"base snapshot errors: {len(errors)}")
    print(f"saved base snapshot: {out_path}")
    return 0 if not df.empty else 1


if __name__ == "__main__":
    raise SystemExit(main())
