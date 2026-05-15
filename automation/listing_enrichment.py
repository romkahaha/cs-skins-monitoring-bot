"""Enrich Steam listing rows with float-fit fair values and opportunity flags."""

from __future__ import annotations

import importlib.util
import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd


MODEL_NAMES = ["smooth", "segmented", "hybrid"]


@dataclass(frozen=True)
class OpportunityConfig:
    steam_sales_n_min: int = 50
    downside_risk_max: float = 10.0
    tail_ratio_min: float = 0.9
    downside_14d_max: float = 0.12
    continuity_ratio_max: float = 3.5
    spread_hybrid_disc_max: float = 0.17


DISPLAY_COLUMNS = [
    "item",
    "tier",
    "listing_id",
    "asset_id",
    "ask",
    "ask_seller_net",
    "float_value",
    "paint_seed",
    "base_eur",
    "predicted_eur",
    "avg_discount",
    "pred_smooth_eur",
    "pred_segmented_eur",
    "pred_hybrid_eur",
    "pred_smooth_eur_disc",
    "pred_segmented_eur_disc",
    "pred_hybrid_eur_disc",
    "spread_smooth",
    "spread_segmented",
    "spread_hybrid",
    "spread_smooth_disc",
    "spread_segmented_disc",
    "spread_hybrid_disc",
    "opportunity_pass",
    "opportunity_fail_count",
    "opportunity_fail_reasons",
    "continuity_ratio",
    "n_fit_clean",
    "n_fit_raw",
    "fit_outlier_n",
    "fit_splits_n",
    "steam_turnover_proxy",
    "scm_total_listings",
    "steam_sales_7d_n",
    "steam_sales_7d_downside_risk%",
    "steam_sales_7d_tail_ratio",
    "steam_daily_downside_14d_pct",
]


SORT_COLUMNS = ["spread_hybrid_disc", "spread_segmented_disc", "spread_smooth_disc", "ask", "item"]
SORT_ASCENDING = [True, True, True, True, True]


def numeric_series(frame: pd.DataFrame, column: str) -> pd.Series:
    if column not in frame.columns:
        return pd.Series(np.nan, index=frame.index, dtype=float)
    return pd.to_numeric(frame[column], errors="coerce")


def finite_series(series: pd.Series) -> pd.Series:
    return pd.Series(np.isfinite(series), index=series.index).fillna(False)


def load_items_py(path: Path) -> list[str]:
    spec = importlib.util.spec_from_file_location("_automation_monitor_items", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot import item list: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    items = getattr(module, "ITEMS", None)
    if not isinstance(items, list):
        raise ValueError(f"{path} must define ITEMS = [...]")
    return [str(x) for x in items]


def _infer_fixed_tier_from_items_py(path: Path | None) -> str | None:
    if path is None:
        return None
    stem = path.stem.lower()
    if stem.endswith("tier_a"):
        return "A"
    if stem.endswith("tier_b"):
        return "B"
    if stem.endswith("tier_c"):
        return "C"
    return None


def load_item_metadata(path: Path | None, items: list[str] | None = None) -> pd.DataFrame | None:
    if path is None:
        return None
    fixed_tier = _infer_fixed_tier_from_items_py(path)
    if fixed_tier is not None and items:
        return pd.DataFrame({"item": [str(x) for x in items], "tier": fixed_tier})

    csv_path = path.with_suffix(".csv")
    if not csv_path.is_file():
        return None
    try:
        meta = pd.read_csv(csv_path, low_memory=False)
    except pd.errors.EmptyDataError:
        return None
    if "item" not in meta.columns:
        return None
    keep = [c for c in ["item", "tier", "liquidity_score", "liquidity_rank"] if c in meta.columns]
    if "item" not in keep or len(keep) == 1:
        return None
    meta = meta[keep].copy()
    meta["item"] = meta["item"].astype(str)
    return meta.drop_duplicates(subset=["item"], keep="first").reset_index(drop=True)


def load_steam_listings(path: Path, items: list[str] | None = None) -> pd.DataFrame:
    try:
        df = pd.read_csv(path, low_memory=False)
    except pd.errors.EmptyDataError:
        df = pd.DataFrame(
            columns=[
                "item",
                "listing_id",
                "asset_id",
                "ask",
                "ask_seller_net",
                "float_value",
                "paint_seed",
                "asset_properties_json",
                "converted_price",
                "converted_fee",
                "converted_currencyid",
                "scm_total_listings",
            ]
        )
    if "market_hash_name" in df.columns and "item" not in df.columns:
        df = df.rename(columns={"market_hash_name": "item"})
    if "item" not in df.columns:
        raise KeyError(f"{path} must contain 'market_hash_name' or 'item'")
    df["item"] = df["item"].astype(str)
    if items is not None:
        df = df[df["item"].isin(set(items))].copy()
    for col in ["ask", "ask_seller_net", "float_value", "paint_seed", "scm_total_listings"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.reset_index(drop=True)


def load_realtime_base(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    if "item" not in df.columns:
        raise KeyError(f"{path} must contain an 'item' column")
    keep_cols = [
        "item",
        "base_usd",
        "base_eur",
        "predicted_usd",
        "predicted_eur",
        "quantity",
        "fx_usd_to_eur",
        "fx_source",
        "base_collected_at_utc",
        "status",
        "error",
    ]
    cols = [c for c in keep_cols if c in df.columns]
    out = df[cols].copy()
    out["item"] = out["item"].astype(str)
    for col in ["base_usd", "base_eur", "predicted_usd", "predicted_eur", "quantity", "fx_usd_to_eur"]:
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce")
    return out.drop_duplicates(subset=["item"], keep="last")


def load_risk_metrics(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, low_memory=False)
    if "item" not in df.columns:
        raise KeyError(f"{path} must contain an 'item' column")
    df["item"] = df["item"].astype(str)
    return df.drop_duplicates(subset=["item"], keep="last")


def load_fit_payload(path: Path) -> dict:
    with path.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    per_skin = payload.get("per_skin")
    if not isinstance(per_skin, dict):
        raise ValueError(f"{path} must contain a 'per_skin' object")
    return payload


def interp_curve(item_fit: dict | None, float_values: pd.Series, model_name: str) -> np.ndarray:
    if not isinstance(item_fit, dict):
        return np.full(len(float_values), np.nan, dtype=float)
    try:
        x_grid = np.asarray(item_fit["x_grid"], dtype=float)
        y_grid = np.asarray(item_fit[model_name], dtype=float)
    except Exception:
        return np.full(len(float_values), np.nan, dtype=float)

    xq = pd.to_numeric(float_values, errors="coerce").to_numpy(dtype=float)
    out = np.full(len(xq), np.nan, dtype=float)
    ok = np.isfinite(xq)
    if len(x_grid) >= 2 and np.isfinite(x_grid).all() and np.isfinite(y_grid).all():
        out[ok] = np.interp(xq[ok], x_grid, y_grid, left=np.nan, right=np.nan)
    return out


def add_model_predictions(df: pd.DataFrame, fit_payload: dict) -> pd.DataFrame:
    per_skin = fit_payload.get("per_skin", {})
    grouped: list[pd.DataFrame] = []
    for item, group in df.groupby("item", sort=False):
        item_fit = per_skin.get(item)
        gg = group.copy()
        base = numeric_series(gg, "base_eur")
        ask = numeric_series(gg, "ask")
        ask = ask.where((ask > 0) & finite_series(ask))
        avg_discount = numeric_series(gg, "avg_discount")

        for model_name in MODEL_NAMES:
            rel_col = f"rel_{model_name}"
            pred_col = f"pred_{model_name}_eur"
            disc_col = f"pred_{model_name}_eur_disc"
            spread_col = f"spread_{model_name}"
            spread_disc_col = f"spread_{model_name}_disc"

            gg[rel_col] = interp_curve(item_fit, gg["float_value"], model_name)
            gg[pred_col] = base * (1.0 + gg[rel_col])
            gg[disc_col] = gg[pred_col] * (1.0 - avg_discount)
            gg[spread_col] = 1.0 - gg[pred_col] / ask
            gg[spread_disc_col] = 1.0 - gg[disc_col] / ask

        if isinstance(item_fit, dict):
            gg["continuity_ratio"] = item_fit.get("continuity_ratio")
            gg["n_fit_clean"] = item_fit.get("n_clean")
            gg["n_fit_raw"] = item_fit.get("n_raw")
            gg["fit_outlier_n"] = item_fit.get("outlier_n")
            splits = item_fit.get("splits", [])
            gg["fit_splits_n"] = len(splits) if isinstance(splits, list) else np.nan
        else:
            gg["continuity_ratio"] = np.nan
            gg["n_fit_clean"] = np.nan
            gg["n_fit_raw"] = np.nan
            gg["fit_outlier_n"] = np.nan
            gg["fit_splits_n"] = np.nan
        grouped.append(gg)

    return pd.concat(grouped, ignore_index=True) if grouped else df.copy()


def build_opportunity_masks(frame: pd.DataFrame, cfg: OpportunityConfig) -> dict[str, pd.Series]:
    ask = numeric_series(frame, "ask")
    spread_hybrid_disc = numeric_series(frame, "spread_hybrid_disc")
    specs = [
        (
            "ask > 0",
            (ask > 0) & finite_series(ask),
        ),
        (
            f"steam_sales_7d_n >= {cfg.steam_sales_n_min}",
            numeric_series(frame, "steam_sales_7d_n") >= cfg.steam_sales_n_min,
        ),
        (
            f"steam_sales_7d_downside_risk% <= {cfg.downside_risk_max}",
            numeric_series(frame, "steam_sales_7d_downside_risk%") <= cfg.downside_risk_max,
        ),
        (
            f"steam_sales_7d_tail_ratio >= {cfg.tail_ratio_min}",
            numeric_series(frame, "steam_sales_7d_tail_ratio") >= cfg.tail_ratio_min,
        ),
        (
            f"steam_daily_downside_14d_pct <= {cfg.downside_14d_max}",
            numeric_series(frame, "steam_daily_downside_14d_pct") <= cfg.downside_14d_max,
        ),
        (
            f"continuity_ratio <= {cfg.continuity_ratio_max}",
            numeric_series(frame, "continuity_ratio") <= cfg.continuity_ratio_max,
        ),
        (
            f"spread_hybrid_disc <= {cfg.spread_hybrid_disc_max}",
            finite_series(spread_hybrid_disc) & (spread_hybrid_disc <= cfg.spread_hybrid_disc_max),
        ),
    ]
    return {label: mask.fillna(False).astype(bool) for label, mask in specs}


def apply_opportunity_flags(frame: pd.DataFrame, cfg: OpportunityConfig = OpportunityConfig()) -> tuple[pd.DataFrame, pd.DataFrame]:
    out = frame.copy()
    masks = build_opportunity_masks(out, cfg)
    opportunity_pass = pd.Series(True, index=out.index)
    for mask in masks.values():
        opportunity_pass &= mask

    out["opportunity_pass"] = opportunity_pass
    fail_matrix = pd.DataFrame({label: ~mask for label, mask in masks.items()}, index=out.index)
    if out.empty:
        out["opportunity_fail_count"] = pd.Series(dtype="int64")
        out["opportunity_fail_reasons"] = pd.Series(dtype="object")
    else:
        out["opportunity_fail_count"] = fail_matrix.sum(axis=1)
        out["opportunity_fail_reasons"] = fail_matrix.apply(
            lambda row: ", ".join([col for col, failed in row.items() if failed]) if row.any() else "-",
            axis=1,
        )
    report = pd.DataFrame(
        [
            {"rule": label, "passed_rows": int(mask.sum()), "failed_rows": int((~mask).sum())}
            for label, mask in masks.items()
        ]
    )
    return out, report


def build_enriched_listings(
    listings_csv: Path,
    realtime_base_csv: Path,
    fit_json: Path,
    risk_csv: Path,
    *,
    monitor_items_py: Path | None = None,
    cfg: OpportunityConfig = OpportunityConfig(),
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    items = load_items_py(monitor_items_py) if monitor_items_py is not None and monitor_items_py.is_file() else None
    item_meta = load_item_metadata(monitor_items_py, items)
    listings = load_steam_listings(listings_csv, items)
    base = load_realtime_base(realtime_base_csv)
    risk = load_risk_metrics(risk_csv)
    fit_payload = load_fit_payload(fit_json)

    df = listings.merge(base, on="item", how="left", suffixes=("", "_base"))
    df = df.merge(risk, on="item", how="left", suffixes=("", "_risk"))
    if item_meta is not None and not item_meta.empty:
        meta_cols = [c for c in item_meta.columns if c != "item" and c not in df.columns]
        if meta_cols:
            df = df.merge(item_meta[["item", *meta_cols]], on="item", how="left")
    df = add_model_predictions(df, fit_payload)
    df, report = apply_opportunity_flags(df, cfg)

    sort_cols = [c for c in SORT_COLUMNS if c in df.columns]
    if sort_cols:
        df = df.sort_values(sort_cols, ascending=SORT_ASCENDING[: len(sort_cols)], na_position="last")
    cols = [c for c in DISPLAY_COLUMNS if c in df.columns]
    cols += [c for c in df.columns if c not in cols]
    df = df[cols].reset_index(drop=True)

    opportunities = df.loc[df["opportunity_pass"]].copy().reset_index(drop=True)
    return df, opportunities, report


def write_opportunity_outputs(
    enriched: pd.DataFrame,
    opportunities: pd.DataFrame,
    report: pd.DataFrame,
    *,
    enriched_csv: Path,
    opportunities_csv: Path,
    report_csv: Path,
) -> None:
    enriched_csv.parent.mkdir(parents=True, exist_ok=True)
    opportunities_csv.parent.mkdir(parents=True, exist_ok=True)
    report_csv.parent.mkdir(parents=True, exist_ok=True)
    enriched.to_csv(enriched_csv, index=False)
    opportunities.to_csv(opportunities_csv, index=False)
    report.to_csv(report_csv, index=False)
