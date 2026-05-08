"""Build a Steam monitoring list from risk metrics and high-CV skin panels.

This module is intentionally offline-only: it reads existing CSV/panel outputs
and does not call Steam or CSFloat APIs.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd


STRUCTURAL_GAP = -1337.0


@dataclass(frozen=True)
class FilterConfig:
    risk_ret_7d_min: float = -0.03
    risk_downside_14d_max: float = 0.17
    risk_sales_7d_n_min: int = 21
    risk_tail_ratio_min: float = 0.85
    risk_n_listings_min: int = 20
    cv_min_listings: int = 3
    pred_cv_min: float = 0.075
    pred_range_over_mean_min: float = 0.3


RISK_AUDIT_COLUMNS = [
    "base_price",
    "n_listings",
    "avg_discount",
    "median_discount",
    "steam_sales_7d_n",
    "steam_sales_7d_iqr_risk%",
    "steam_sales_7d_downside_risk%",
    "steam_sales_7d_tail_ratio",
    "steam_turnover_proxy",
    "steam_daily_ret_7d",
    "steam_daily_slope_7d",
    "steam_daily_ema_gap_3_14",
    "steam_daily_range_14d_pct",
    "steam_daily_downside_14d_pct",
    "steam_discount_risk_score",
    "risk_collected_at_utc",
]


SORT_COLUMNS = [
    "median_discount",
    "steam_turnover_proxy",
    "steam_daily_ret_7d",
    "steam_daily_slope_7d",
    "steam_daily_downside_14d_pct",
    "steam_sales_7d_iqr_risk%",
    "pred_cv",
    "item",
]
SORT_ASCENDING = [False, False, False, False, True, True, False, True]
SORT_ASCENDING_BY_COLUMN = dict(zip(SORT_COLUMNS, SORT_ASCENDING))


def repo_root_from(path: Path) -> Path:
    path = path.resolve()
    for candidate in [path, *path.parents]:
        if (candidate / "skin_homog").is_dir() and (candidate / "lists").is_dir():
            return candidate
    return Path.cwd().resolve()


def numeric_series(frame: pd.DataFrame, column: str) -> pd.Series:
    if column not in frame.columns:
        return pd.Series(np.nan, index=frame.index, dtype=float)
    return pd.to_numeric(frame[column], errors="coerce")


def load_risk_metrics(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    if "item" not in df.columns:
        raise KeyError(f"{path} must contain an 'item' column")
    df = df.copy()
    df["item"] = df["item"].astype(str)
    return df


def load_summary_cv(summary_csv: Path) -> pd.DataFrame:
    if not summary_csv.is_file():
        return pd.DataFrame(columns=["item", "pred_cv", "pred_range_over_mean", "cv_n_listings", "cv_source"])

    df = pd.read_csv(summary_csv)
    if "item" not in df.columns:
        raise KeyError(f"{summary_csv} must contain an 'item' column")

    out = pd.DataFrame({"item": df["item"].astype(str)})
    out["pred_cv"] = numeric_series(df, "pred_cv")
    pred_mean = numeric_series(df, "pred_mean")
    pred_min = numeric_series(df, "pred_min")
    pred_max = numeric_series(df, "pred_max")
    out["pred_range_over_mean"] = np.where(pred_mean > 0, (pred_max - pred_min) / pred_mean, np.nan)
    out["cv_n_listings"] = numeric_series(df, "n_listings")
    out["cv_source"] = "summary"
    return out


def _read_numeric_panel(path: Path) -> pd.DataFrame | None:
    if not path.is_file():
        return None
    df = pd.read_csv(path, low_memory=False)
    df = df.apply(pd.to_numeric, errors="coerce")
    return df.replace(STRUCTURAL_GAP, np.nan)


def recompute_cv_from_panels(data_dir: Path, items: Iterable[str] | None = None) -> pd.DataFrame:
    pred = _read_numeric_panel(data_dir / "predicted.csv")
    if pred is None:
        return pd.DataFrame(columns=["item", "pred_cv", "pred_range_over_mean", "cv_n_listings", "cv_source"])

    wanted = set(str(x) for x in items) if items is not None else set(pred.columns.astype(str))
    rows: list[dict[str, object]] = []
    for item in sorted(wanted):
        if item not in pred.columns:
            continue
        values = pd.to_numeric(pred[item], errors="coerce").dropna()
        values = values[np.isfinite(values)]
        if values.empty:
            continue

        mean = float(values.mean())
        std = float(values.std(ddof=0))
        pmin = float(values.min())
        pmax = float(values.max())
        rows.append(
            {
                "item": item,
                "pred_cv": (std / mean) if mean > 0 else np.nan,
                "pred_range_over_mean": ((pmax - pmin) / mean) if mean > 0 else np.nan,
                "cv_n_listings": int(values.size),
                "cv_source": "recomputed",
            }
        )
    return pd.DataFrame(rows)


def load_cv_metrics(summary_csv: Path, data_dir: Path, risk_items: Iterable[str]) -> pd.DataFrame:
    summary = load_summary_cv(summary_csv)
    risk_items_set = set(str(x) for x in risk_items)

    need_recompute = set(risk_items_set)
    if not summary.empty:
        summary_items = set(summary["item"].astype(str))
        ok_summary = summary[
            summary["pred_cv"].notna()
            & summary["pred_range_over_mean"].notna()
            & summary["cv_n_listings"].notna()
        ]
        need_recompute = (risk_items_set - summary_items) | (risk_items_set - set(ok_summary["item"].astype(str)))

    recomputed = recompute_cv_from_panels(data_dir, need_recompute)
    if summary.empty:
        combined = recomputed.copy()
    elif recomputed.empty:
        combined = summary.copy()
    else:
        summary_keep = summary[~summary["item"].isin(set(recomputed["item"].astype(str)))]
        combined = pd.concat([summary_keep, recomputed], ignore_index=True)

    if combined.empty:
        combined = pd.DataFrame(columns=["item", "pred_cv", "pred_range_over_mean", "cv_n_listings", "cv_source"])

    existing = set(combined["item"].astype(str))
    missing_rows = [
        {
            "item": item,
            "pred_cv": np.nan,
            "pred_range_over_mean": np.nan,
            "cv_n_listings": np.nan,
            "cv_source": "missing_assumed_high",
        }
        for item in sorted(risk_items_set - existing)
    ]
    if missing_rows:
        combined = pd.concat([combined, pd.DataFrame(missing_rows)], ignore_index=True)

    combined["item"] = combined["item"].astype(str)
    return combined.drop_duplicates(subset=["item"], keep="last")


def apply_risk_filter(df: pd.DataFrame, cfg: FilterConfig = FilterConfig()) -> pd.DataFrame:
    out = df.copy()
    out["risk_rule_trend"] = numeric_series(out, "steam_daily_ret_7d") >= cfg.risk_ret_7d_min
    out["risk_rule_price"] = numeric_series(out, "steam_daily_downside_14d_pct") <= cfg.risk_downside_14d_max
    out["risk_rule_liquidity"] = numeric_series(out, "steam_sales_7d_n") >= cfg.risk_sales_7d_n_min
    out["risk_rule_tail"] = numeric_series(out, "steam_sales_7d_tail_ratio") >= cfg.risk_tail_ratio_min
    out["risk_rule_listings"] = numeric_series(out, "n_listings") >= cfg.risk_n_listings_min

    rule_cols = [
        "risk_rule_trend",
        "risk_rule_price",
        "risk_rule_liquidity",
        "risk_rule_tail",
        "risk_rule_listings",
    ]
    out["risk_pass"] = out[rule_cols].fillna(False).all(axis=1)
    return out


def apply_high_cv_filter(df: pd.DataFrame, cfg: FilterConfig = FilterConfig()) -> pd.DataFrame:
    out = df.copy()
    assumed = out["cv_source"].fillna("") == "missing_assumed_high"
    numeric_pass = (
        (numeric_series(out, "cv_n_listings") >= cfg.cv_min_listings)
        & (numeric_series(out, "pred_cv") > cfg.pred_cv_min)
        & (numeric_series(out, "pred_range_over_mean") > cfg.pred_range_over_mean_min)
    )
    out["high_cv_pass"] = assumed | numeric_pass.fillna(False)
    return out


def build_monitor_frame(
    risk_csv: Path,
    summary_csv: Path,
    data_dir: Path,
    cfg: FilterConfig = FilterConfig(),
    *,
    assume_risk_passed: bool = False,
) -> tuple[pd.DataFrame, dict[str, int]]:
    risk = load_risk_metrics(risk_csv)
    if assume_risk_passed:
        risk = risk.copy()
        for col in (
            "risk_rule_trend",
            "risk_rule_price",
            "risk_rule_liquidity",
            "risk_rule_tail",
            "risk_rule_listings",
        ):
            risk[col] = True
        risk["risk_pass"] = True
    else:
        risk = apply_risk_filter(risk, cfg)
    cv = load_cv_metrics(summary_csv, data_dir, risk["item"])

    out = risk.merge(cv, on="item", how="left")
    out["cv_source"] = out["cv_source"].fillna("missing_assumed_high")
    out = apply_high_cv_filter(out, cfg)
    out["monitor_pass"] = out["risk_pass"] & out["high_cv_pass"]

    counts = {
        "total_risk_rows": int(len(out)),
        "risk_passed": int(out["risk_pass"].sum()),
        "high_cv_passed": int(out["high_cv_pass"].sum()),
        "monitor_passed": int(out["monitor_pass"].sum()),
    }

    sort_cols = [c for c in SORT_COLUMNS if c in out.columns]
    sort_ascending = [SORT_ASCENDING_BY_COLUMN[c] for c in sort_cols]
    if sort_cols:
        out = out.sort_values(sort_cols, ascending=sort_ascending, na_position="last")

    leading = [
        "item",
        "risk_pass",
        "high_cv_pass",
        "monitor_pass",
        "pred_cv",
        "pred_range_over_mean",
        "cv_n_listings",
        "cv_source",
    ]
    rules = [
        "risk_rule_trend",
        "risk_rule_price",
        "risk_rule_liquidity",
        "risk_rule_tail",
        "risk_rule_listings",
    ]
    cols = leading + rules + [c for c in RISK_AUDIT_COLUMNS if c in out.columns]
    cols += [c for c in out.columns if c not in cols]
    return out[cols].reset_index(drop=True), counts


def write_items_py(items: list[str], path: Path, *, source_csv: Path, counts: dict[str, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Auto-generated by automation/nightly/build_monitor_list.py",
        f"# SOURCE_CSV = {str(source_csv)!r}",
        "# PROFILE = 'risk_plus_high_cv'",
        f"# TOTAL_RISK_ROWS = {counts['total_risk_rows']}",
        f"# RISK_PASSED = {counts['risk_passed']}",
        f"# HIGH_CV_PASSED = {counts['high_cv_passed']}",
        f"# N_OUTPUT_ITEMS = {len(items)}",
        "",
        "ITEMS = [",
    ]
    lines.extend(f"    {json.dumps(item, ensure_ascii=False)}," for item in items)
    lines.append("]")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_monitor_outputs(
    frame: pd.DataFrame,
    csv_path: Path,
    items_py_path: Path,
    *,
    source_csv: Path,
    counts: dict[str, int],
) -> list[str]:
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    monitor_frame = frame.loc[frame["monitor_pass"]].copy().reset_index(drop=True)
    monitor_frame.to_csv(csv_path, index=False)
    items = monitor_frame["item"].dropna().astype(str).tolist()
    write_items_py(items, items_py_path, source_csv=source_csv, counts=counts)
    return items
