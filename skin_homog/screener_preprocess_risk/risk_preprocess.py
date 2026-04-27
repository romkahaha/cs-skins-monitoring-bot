"""
Second-stage preprocess: enrich shortlist items with Steam trade/risk metrics.

Consumes:
- a shortlist Python file with `ITEMS = [...]`
- the CSV from the cheap CSFloat preprocess

Writes a richer shortlist CSV with:
- copied stage-1 cheap metrics for each shortlist item
- Steam 7d trade stats from /market/pricehistory/
- derived risk metrics

This stage is intentionally separate because Steam pricehistory is slower and more fragile.

Run modes:
- create: wipe output CSV/log and rescan the full shortlist
- merge: resume-style mode; skip items already present in the risk CSV and only collect the rest
"""

from __future__ import annotations

import importlib.util
import json
import math
import os
import random
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd


SCRIPT_DIR = Path(__file__).resolve().parent
SKIN_HOMOG_DIR = SCRIPT_DIR.parent
REPO_ROOT = SKIN_HOMOG_DIR.parent
DEFAULT_RUNTIME_JSON = SCRIPT_DIR / "risk_runtime.json"
DEFAULT_STEAM_COOKIE_REFRESH_SCRIPT = REPO_ROOT / "refresh_steam_cookies.ps1"
RET_3D_DAYS = 3
RET_7D_DAYS = 7
SLOPE_7D_DAYS = 7
EMA_FAST_SPAN = 3
EMA_SLOW_SPAN = 14
RANGE_14D_DAYS = 14
DOWNSIDE_14D_DAYS = 14
TREND_FETCH_DAYS = max(RET_7D_DAYS, SLOPE_7D_DAYS, EMA_SLOW_SPAN, RANGE_14D_DAYS, DOWNSIDE_14D_DAYS)


def _log(msg: str = "") -> None:
    print(msg, flush=True)
    try:
        sys.stdout.flush()
    except Exception:
        pass


def _runtime_path() -> Path:
    raw = os.environ.get("RISK_PREPROCESS_RUNTIME_CONFIG")
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
        _log(f"[risk_preprocess] broken JSON in {path}: {exc}")
        return {}


def _rt_str(cfg: dict, key: str, default: str) -> str:
    value = cfg.get(key, default)
    text = str(value).strip()
    return text or default


def _rt_int(cfg: dict, key: str, default: int) -> int:
    try:
        return int(cfg.get(key, default))
    except (TypeError, ValueError):
        return default


def _rt_float(cfg: dict, key: str, default: float) -> float:
    try:
        return float(cfg.get(key, default))
    except (TypeError, ValueError):
        return default


def _rt_bool(cfg: dict, key: str, default: bool) -> bool:
    value = cfg.get(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        s = value.strip().lower()
        if s in {"1", "true", "yes", "on"}:
            return True
        if s in {"0", "false", "no", "off"}:
            return False
    return default


def _resolve_path(raw: str | os.PathLike[str], base: Path) -> Path:
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = (base / path).resolve()
    return path


def _load_fetchers_module():
    repo_root_str = str(REPO_ROOT)
    if repo_root_str not in sys.path:
        # Make repo-root helper modules like local_steam_cookies/local_secrets importable
        # even when this script is launched from a nested notebook directory.
        sys.path.insert(0, repo_root_str)
    module_path = REPO_ROOT / "base_screening_with_trades" / "fetchers.py"
    spec = importlib.util.spec_from_file_location("risk_preprocess_fetchers", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot import {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _refresh_steam_cookies(refresh_script: Path) -> bool:
    if not refresh_script.exists():
        _log(f"[risk_preprocess] steam cookie refresh script not found: {refresh_script}")
        return False
    try:
        proc = subprocess.run(
            [
                "powershell",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(refresh_script),
            ],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
    except Exception as exc:
        _log(f"[risk_preprocess] steam cookie refresh failed to start: {exc}")
        return False

    if proc.stdout.strip():
        _log(proc.stdout.strip())
    if proc.stderr.strip():
        _log(proc.stderr.strip())
    if proc.returncode != 0:
        _log(f"[risk_preprocess] steam cookie refresh exited with code {proc.returncode}")
        return False
    return True


def _ensure_steam_cookies(fetchers_module, cfg: dict) -> None:
    if not _rt_bool(cfg, "AUTO_REFRESH_STEAM_COOKIES", True):
        return

    ck = fetchers_module._steam_cookie_header()
    sid = fetchers_module._steam_sessionid_from_cookie_header(ck)
    exp = fetchers_module._steam_loginsecure_expiry(ck)
    now = datetime.now(timezone.utc)
    needs_refresh = False

    if not ck:
        _log("[risk_preprocess] no Steam cookies found, attempting auto-refresh...")
        needs_refresh = True
    elif not sid:
        _log("[risk_preprocess] Steam cookies missing sessionid, attempting auto-refresh...")
        needs_refresh = True
    elif exp is not None and exp <= now:
        _log(f"[risk_preprocess] steamLoginSecure expired at {exp.isoformat()}, attempting auto-refresh...")
        needs_refresh = True

    if not needs_refresh:
        return

    refresh_script = _resolve_path(
        _rt_str(cfg, "STEAM_COOKIE_REFRESH_SCRIPT", str(DEFAULT_STEAM_COOKIE_REFRESH_SCRIPT)),
        SCRIPT_DIR,
    )
    ok = _refresh_steam_cookies(refresh_script)
    if not ok:
        _log("[risk_preprocess] auto-refresh did not succeed; Steam requests may still fail.")
        return

    ck2 = fetchers_module._steam_cookie_header()
    sid2 = fetchers_module._steam_sessionid_from_cookie_header(ck2)
    exp2 = fetchers_module._steam_loginsecure_expiry(ck2)
    if ck2 and sid2 and (exp2 is None or exp2 > datetime.now(timezone.utc)):
        _log("[risk_preprocess] Steam cookies refreshed successfully.")
    else:
        _log("[risk_preprocess] Steam cookie refresh finished, but cookies still look incomplete.")


def _load_items(list_path: Path) -> list[str]:
    spec = importlib.util.spec_from_file_location("risk_preprocess_item_list", list_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot import {list_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    items = getattr(module, "ITEMS", None)
    if not isinstance(items, list):
        raise ValueError(f"{list_path} must define ITEMS = [...]")
    return [str(x) for x in items]


def _trade_row(fetchers_module, tr: dict | None) -> dict:
    return fetchers_module._scm_trade_row(tr)


def _safe_ratio(num: float | None, den: float | None) -> float | None:
    if num is None or den is None or den == 0:
        return None
    return num / den


def _derive_risk_metrics(row: dict) -> dict:
    mean = row.get("steam_sales_7d_mean")
    median = row.get("steam_sales_7d_median")
    p10 = row.get("steam_sales_7d_p10")
    p25 = row.get("steam_sales_7d_p25")
    p75 = row.get("steam_sales_7d_p75")
    p90 = row.get("steam_sales_7d_p90")
    n_sales = row.get("steam_sales_7d_n")
    observed = row.get("n_listings")

    out: dict[str, float | None] = {
        "steam_sales_7d_iqr_risk%": None,
        "steam_sales_7d_band_risk%": None,
        "steam_sales_7d_downside_risk%": None,
        "steam_sales_7d_tail_ratio": None,
        "steam_turnover_proxy": None,
        "steam_discount_risk_score": None,
    }
    if mean and mean > 0:
        if p25 is not None and p75 is not None:
            out["steam_sales_7d_iqr_risk%"] = ((p75 - p25) / mean) * 100.0
        if p10 is not None and p90 is not None:
            out["steam_sales_7d_band_risk%"] = ((p90 - p10) / mean) * 100.0
        if median is not None and p10 is not None:
            out["steam_sales_7d_downside_risk%"] = ((median - p10) / mean) * 100.0
    if median and median > 0 and p10 is not None:
        out["steam_sales_7d_tail_ratio"] = p10 / median
    if observed and observed > 0 and n_sales is not None:
        out["steam_turnover_proxy"] = float(n_sales) / float(observed)

    avg_discount = row.get("avg_discount")
    downside = out["steam_sales_7d_downside_risk%"]
    turnover = out["steam_turnover_proxy"]
    if avg_discount is not None:
        score = float(avg_discount)
        if downside is not None:
            score -= 0.01 * float(downside)
        if turnover is not None and turnover > 0:
            score += 0.02 * math.log10(max(turnover, 1e-9) + 1.0)
        out["steam_discount_risk_score"] = score

    return out


def _summarise_trade_points(
    points: list[tuple[datetime, float, int]] | None,
    *,
    days: int,
) -> dict | None:
    if not points:
        return None
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    expanded: list[float] = []
    for dt, price, vol in points:
        if dt < cutoff:
            continue
        expanded.extend([float(price)] * max(1, int(vol)))
    if not expanded:
        return None
    arr = pd.Series(expanded, dtype=float)
    return {
        "mean": float(arr.mean()),
        "median": float(arr.median()),
        "p10": float(arr.quantile(0.10)),
        "p25": float(arr.quantile(0.25)),
        "p75": float(arr.quantile(0.75)),
        "p90": float(arr.quantile(0.90)),
        "min": float(arr.min()),
        "max": float(arr.max()),
        "n": int(arr.size),
    }


def _daily_median_series(
    points: list[tuple[datetime, float, int]] | None,
    *,
    days: int,
) -> pd.Series:
    if not points:
        return pd.Series(dtype=float)
    rows: list[dict[str, object]] = []
    for dt, price, vol in points:
        day = pd.Timestamp(dt).tz_convert("UTC").normalize()
        rows.extend({"day": day, "price": float(price)} for _ in range(max(1, int(vol))))
    if not rows:
        return pd.Series(dtype=float)
    df = pd.DataFrame(rows)
    daily = df.groupby("day", sort=True)["price"].median()
    latest_day = daily.index.max()
    cutoff_day = latest_day - pd.Timedelta(days=max(0, int(days) - 1))
    return daily[daily.index >= cutoff_day].sort_index()


def _latest_value_at_or_before(series: pd.Series, target_day: pd.Timestamp) -> float | None:
    if series.empty:
        return None
    eligible = series[series.index <= target_day]
    if eligible.empty:
        return None
    try:
        value = float(eligible.iloc[-1])
    except (TypeError, ValueError):
        return None
    return value


def _log_slope_per_day(series: pd.Series) -> float | None:
    if len(series) < 3:
        return None
    values: list[tuple[float, float]] = []
    t0 = series.index.min()
    for idx, value in series.items():
        try:
            price = float(value)
        except (TypeError, ValueError):
            continue
        if price <= 0:
            continue
        x = float((idx - t0).days)
        y = math.log(price)
        values.append((x, y))
    if len(values) < 3:
        return None
    xs = [x for x, _ in values]
    ys = [y for _, y in values]
    mean_x = sum(xs) / len(xs)
    mean_y = sum(ys) / len(ys)
    var_x = sum((x - mean_x) ** 2 for x in xs)
    if var_x <= 0:
        return None
    cov_xy = sum((x - mean_x) * (y - mean_y) for x, y in values)
    return cov_xy / var_x


def _derive_trend_metrics(points: list[tuple[datetime, float, int]] | None) -> dict:
    out: dict[str, float | None] = {
        "steam_daily_ret_3d": None,
        "steam_daily_ret_7d": None,
        "steam_daily_slope_7d": None,
        "steam_daily_ema_gap_3_14": None,
        "steam_daily_range_14d_pct": None,
        "steam_daily_downside_14d_pct": None,
    }
    daily = _daily_median_series(points, days=TREND_FETCH_DAYS)
    if daily.empty:
        return out

    latest_day = daily.index.max()
    p0 = _latest_value_at_or_before(daily, latest_day)
    if p0 is None or p0 <= 0:
        return out

    p3 = _latest_value_at_or_before(daily, latest_day - pd.Timedelta(days=RET_3D_DAYS))
    if p3 is not None and p3 > 0:
        out["steam_daily_ret_3d"] = (p0 / p3) - 1.0

    p7 = _latest_value_at_or_before(daily, latest_day - pd.Timedelta(days=RET_7D_DAYS))
    if p7 is not None and p7 > 0:
        out["steam_daily_ret_7d"] = (p0 / p7) - 1.0

    slope_cutoff = latest_day - pd.Timedelta(days=SLOPE_7D_DAYS - 1)
    slope_series = daily[daily.index >= slope_cutoff]
    slope = _log_slope_per_day(slope_series)
    if slope is not None:
        out["steam_daily_slope_7d"] = float(slope)

    if len(daily) >= EMA_SLOW_SPAN:
        ema_fast = daily.ewm(span=EMA_FAST_SPAN, adjust=False).mean().iloc[-1]
        ema_slow = daily.ewm(span=EMA_SLOW_SPAN, adjust=False).mean().iloc[-1]
        if pd.notna(ema_fast) and pd.notna(ema_slow) and float(ema_slow) > 0:
            out["steam_daily_ema_gap_3_14"] = (float(ema_fast) / float(ema_slow)) - 1.0

    range_cutoff = latest_day - pd.Timedelta(days=RANGE_14D_DAYS - 1)
    range_series = daily[daily.index >= range_cutoff]
    if len(range_series) >= 2:
        out["steam_daily_range_14d_pct"] = (float(range_series.max()) - float(range_series.min())) / float(p0)
        out["steam_daily_downside_14d_pct"] = (float(p0) - float(range_series.min())) / float(p0)

    return out


def parse_cli(argv: list[str], cfg: dict) -> tuple[str, int, int, Path, Path, Path, Path]:
    mode = _rt_str(cfg, "DEFAULT_RUN_MODE", "create")
    trade_days = _rt_int(cfg, "TRADE_DAYS", 7)
    min_discount_sample_n = _rt_int(cfg, "MIN_DISCOUNT_SAMPLE_N", 3)
    list_path = _resolve_path(
        _rt_str(cfg, "DEFAULT_LIST_PATH", "../../lists/skins_preprocess_filtered.py"),
        SCRIPT_DIR,
    )
    stage1_csv = _resolve_path(
        _rt_str(cfg, "STAGE1_CSV", "../screener_preprocess/preprocess_metrics.csv"),
        SCRIPT_DIR,
    )
    output_csv = _resolve_path(_rt_str(cfg, "OUTPUT_CSV", "risk_metrics.csv"), SCRIPT_DIR)
    progress_log = _resolve_path(_rt_str(cfg, "PROGRESS_LOG", "_risk_progress.log"), SCRIPT_DIR)

    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--create":
            mode = "create"
        elif arg == "--merge":
            mode = "merge"
        elif arg == "--days":
            i += 1
            trade_days = max(1, int(argv[i]))
        elif arg == "--min-discount-sample":
            i += 1
            min_discount_sample_n = max(0, int(argv[i]))
        elif arg == "--stage1-csv":
            i += 1
            stage1_csv = _resolve_path(argv[i], Path.cwd())
        elif arg == "--output":
            i += 1
            output_csv = _resolve_path(argv[i], Path.cwd())
        elif arg == "--progress-log":
            i += 1
            progress_log = _resolve_path(argv[i], Path.cwd())
        else:
            list_path = _resolve_path(arg, Path.cwd())
        i += 1
    return mode, trade_days, min_discount_sample_n, list_path, stage1_csv, output_csv, progress_log


def _load_stage1(stage1_csv: Path) -> pd.DataFrame:
    df = pd.read_csv(stage1_csv)
    if "item" not in df.columns:
        raise KeyError(f"{stage1_csv} has no 'item' column")
    return df


def _load_existing(output_csv: Path) -> pd.DataFrame:
    if not output_csv.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(output_csv)
    except Exception:
        return pd.DataFrame()


def _existing_item_names(output_csv: Path) -> set[str]:
    df = _load_existing(output_csv)
    if df.empty or "item" not in df.columns:
        return set()
    return {str(x) for x in df["item"].dropna().astype(str)}


def _save_rows(output_csv: Path, rows: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(rows).sort_values("item").reset_index(drop=True)
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(output_csv, index=False)
    return df


def _write_progress(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def main() -> int:
    cfg = _load_runtime()
    fetchers = _load_fetchers_module()
    mode, trade_days, min_discount_sample_n, list_path, stage1_csv, output_csv, progress_log = parse_cli(sys.argv[1:], cfg)
    steam_delay_min = _rt_float(cfg, "STEAM_ITEM_DELAY_MIN", 1.0)
    steam_delay_max = _rt_float(cfg, "STEAM_ITEM_DELAY_MAX", 2.0)
    if steam_delay_max < steam_delay_min:
        steam_delay_max = steam_delay_min
    steam_currency = _rt_int(cfg, "STEAM_CURRENCY", 3)
    require_ok = _rt_bool(cfg, "REQUIRE_STAGE1_OK", True)
    _ensure_steam_cookies(fetchers, cfg)

    shortlist_items = _load_items(list_path)
    stage1 = _load_stage1(stage1_csv)
    if require_ok and "status" in stage1.columns:
        stage1 = stage1[stage1["status"].fillna("") == "ok"].copy()
    if "discount_sample_n" in stage1.columns:
        stage1 = stage1[stage1["discount_sample_n"].fillna(0) >= min_discount_sample_n].copy()
    stage1 = stage1.sort_values("item").reset_index(drop=True)
    stage1_by_item = (
        stage1.drop_duplicates(subset=["item"], keep="last")
        .set_index("item", drop=False)
    )

    _log(f"[risk_preprocess] mode={mode} trade_days={trade_days} steam_currency={steam_currency}")
    _log(f"[risk_preprocess] shortlist={list_path} items={len(shortlist_items)}")
    _log(f"[risk_preprocess] stage1_csv={stage1_csv} rows={len(stage1)}")
    _log(f"[risk_preprocess] runtime={_runtime_path()}")
    _log(f"[risk_preprocess] output={output_csv}")

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
            _log(f"[risk_preprocess] merge resume: skip existing items already in CSV = {len(skip_items)}")

    missing_in_stage1: list[str] = [item for item in shortlist_items if item not in stage1_by_item.index]
    if missing_in_stage1:
        _log(f"[risk_preprocess] warning: items missing in stage1 CSV = {len(missing_in_stage1)}")

    items_to_run = [item for item in shortlist_items if item in stage1_by_item.index and item not in skip_items]
    total = len(items_to_run)
    if mode == "merge":
        _log(f"[risk_preprocess] merge resume: remaining items to collect = {total}")

    merged_df = pd.DataFrame(list(current_by_item.values()))
    for idx, item in enumerate(items_to_run, start=1):
        src = stage1_by_item.loc[item].to_dict()
        started = time.perf_counter()
        fetch_days = max(trade_days, TREND_FETCH_DAYS)
        trade_points = fetchers.get_scm_trade_points(item, currency=steam_currency, days=fetch_days)
        tr = _summarise_trade_points(trade_points, days=trade_days)
        row = dict(src)
        row.update(_trade_row(fetchers, tr))
        row.update(_derive_risk_metrics(row))
        row.update(_derive_trend_metrics(trade_points))
        row["trade_days"] = trade_days
        row["steam_trade_currency"] = steam_currency
        row["risk_collected_at_utc"] = pd.Timestamp.utcnow().isoformat()
        current_by_item[item] = row
        merged_df = _save_rows(output_csv, list(current_by_item.values()))
        elapsed = time.perf_counter() - started
        line = (
            f'{idx}/{total} "{item}"  '
            f'steam_n={row.get("steam_sales_7d_n", 0)}  '
            f'median={row.get("steam_sales_7d_median")}  '
            f'iqr_risk={row.get("steam_sales_7d_iqr_risk%")}  '
            f'downside={row.get("steam_sales_7d_downside_risk%")}  '
            f'ret7={row.get("steam_daily_ret_7d")}  '
            f'{elapsed:.1f}s'
        )
        _log(line)
        _write_progress(progress_log, line)
        if idx < total and steam_delay_max > 0:
            time.sleep(random.uniform(steam_delay_min, steam_delay_max))

    _log("")
    _log(f"[risk_preprocess] saved {len(merged_df)} rows -> {output_csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
