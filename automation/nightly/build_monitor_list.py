"""CLI for building the latest Steam monitoring list from risk-passed candidates."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from automation.config import load_json_config, nightly_defaults, path_from_config
from automation.monitoring.tier_scheduler import (
    DEFAULT_QUEUE_PATTERN,
    assign_monitor_tiers,
    tier_item_paths_from_config,
    tiers_metadata_path_from_config,
    write_tier_outputs,
)
from automation.risk_filters import FilterConfig, build_monitor_frame, repo_root_from, write_monitor_outputs


def parse_args() -> argparse.Namespace:
    root = repo_root_from(Path(__file__))
    parser = argparse.ArgumentParser(description="Build monitor_list_latest from risk metrics and high-CV panels.")
    parser.add_argument(
        "--config",
        type=Path,
        default=root / "automation" / "configs" / "nightly.json",
        help="Nightly automation JSON config.",
    )
    parser.add_argument(
        "--risk-csv",
        type=Path,
        default=None,
        help="Path to risk_candidates_latest.csv (already passed VPS risk filter).",
    )
    parser.add_argument(
        "--summary-csv",
        type=Path,
        default=None,
        help="Path to skin_homog data_skins_big _summary.csv.",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help="Directory with raw panel CSVs for CV recomputation.",
    )
    parser.add_argument(
        "--out-csv",
        type=Path,
        default=None,
        help="Output audit CSV.",
    )
    parser.add_argument(
        "--out-items-py",
        type=Path,
        default=None,
        help="Output Python item list with ITEMS = [...].",
    )
    parser.add_argument(
        "--model-coverage-csv",
        type=Path,
        default=None,
        help="Path to model_coverage_latest.csv; used to exclude items without summary/fit models.",
    )
    return parser.parse_args()


def apply_model_ready_filter(frame: pd.DataFrame, coverage_csv: Path, counts: dict[str, int]) -> pd.DataFrame:
    if not coverage_csv.is_file():
        raise FileNotFoundError(f"model coverage CSV not found: {coverage_csv}")

    coverage = pd.read_csv(coverage_csv)
    if "item" not in coverage.columns or "model_ready" not in coverage.columns:
        raise KeyError(f"{coverage_csv} must contain 'item' and 'model_ready' columns")

    keep = coverage[["item", "model_ready", "has_summary", "summary_ready", "has_fit", "fit_ready"]].copy()
    keep["item"] = keep["item"].astype(str)
    for col in ["model_ready", "has_summary", "summary_ready", "has_fit", "fit_ready"]:
        if col in keep.columns:
            keep[col] = keep[col].astype(str).str.lower().isin(["true", "1", "yes"])

    out = frame.merge(keep.drop_duplicates(subset=["item"], keep="last"), on="item", how="left")
    out["model_ready"] = out["model_ready"].map(lambda value: bool(value) if pd.notna(value) else False)
    out["monitor_pass_before_model_ready"] = out["monitor_pass"]
    out["monitor_pass"] = out["monitor_pass"] & out["model_ready"]
    counts["model_ready_passed"] = int(out["model_ready"].sum())
    counts["monitor_passed_before_model_ready"] = int(out["monitor_pass_before_model_ready"].sum())
    counts["monitor_passed"] = int(out["monitor_pass"].sum())
    return out


def main() -> int:
    args = parse_args()
    config = load_json_config(args.config.resolve() if args.config else None, nightly_defaults())
    risk_filter = config.get("risk_filter", {})
    high_cv_filter = config.get("high_cv_filter", {})
    cfg = FilterConfig(
        risk_ret_7d_min=float(risk_filter.get("ret_7d_min", -0.03)),
        risk_downside_14d_max=float(risk_filter.get("downside_14d_max", 0.17)),
        risk_sales_7d_n_min=int(risk_filter.get("sales_7d_n_min", 21)),
        risk_tail_ratio_min=float(risk_filter.get("tail_ratio_min", 0.85)),
        risk_n_listings_min=int(risk_filter.get("n_listings_min", 20)),
        cv_min_listings=int(high_cv_filter.get("min_listings", 3)),
        pred_cv_min=float(high_cv_filter.get("pred_cv_min", 0.075)),
        pred_range_over_mean_min=float(high_cv_filter.get("pred_range_over_mean_min", 0.3)),
    )
    risk_csv = args.risk_csv.resolve() if args.risk_csv else path_from_config(config, "risk_candidates_csv")
    summary_csv = args.summary_csv.resolve() if args.summary_csv else path_from_config(config, "summary_csv")
    data_dir = args.data_dir.resolve() if args.data_dir else path_from_config(config, "skin_data_dir")
    out_csv = args.out_csv.resolve() if args.out_csv else path_from_config(config, "monitor_csv")
    out_items_py = args.out_items_py.resolve() if args.out_items_py else path_from_config(config, "monitor_items_py")
    model_coverage_csv = (
        args.model_coverage_csv.resolve()
        if args.model_coverage_csv
        else path_from_config(config, "model_coverage_csv")
    )
    frame, counts = build_monitor_frame(
        risk_csv,
        summary_csv,
        data_dir,
        cfg,
        assume_risk_passed=True,
    )
    if bool(config.get("model_coverage", {}).get("require_model_ready_for_monitor", True)):
        frame = apply_model_ready_filter(frame, model_coverage_csv, counts)

    tier_cfg = config.get("monitor_tiers", {})
    if bool(tier_cfg.get("enabled", True)):
        score_weights = tier_cfg.get("score_weights", {})
        tier_frame, tier_counts, normalized_shares = assign_monitor_tiers(
            frame.loc[frame["monitor_pass"]].copy().reset_index(drop=True),
            shares=tier_cfg.get("shares", {}),
            sales_weight=float(score_weights.get("steam_sales_7d_n", 0.75)),
            turnover_weight=float(score_weights.get("steam_turnover_proxy", 0.25)),
        )
        if not tier_frame.empty:
            frame = frame.merge(
                tier_frame[["item", "tier", "liquidity_score", "liquidity_rank"]],
                on="item",
                how="left",
            )
        write_tier_outputs(
            tier_frame,
            tier_item_paths=tier_item_paths_from_config(config),
            metadata_path=tiers_metadata_path_from_config(config),
            source_csv=risk_csv,
            counts=counts,
            shares=normalized_shares,
            score_weights={
                "steam_sales_7d_n": float(score_weights.get("steam_sales_7d_n", 0.75)),
                "steam_turnover_proxy": float(score_weights.get("steam_turnover_proxy", 0.25)),
            },
        )
        counts["tier_a_items"] = int(tier_counts["A"])
        counts["tier_b_items"] = int(tier_counts["B"])
        counts["tier_c_items"] = int(tier_counts["C"])

    items = write_monitor_outputs(
        frame,
        out_csv,
        out_items_py,
        source_csv=risk_csv,
        counts=counts,
    )

    print(f"config: {args.config.resolve() if args.config else '<defaults>'}")
    print(f"risk csv: {risk_csv}")
    print(f"summary csv: {summary_csv}")
    print(f"data dir: {data_dir}")
    print(f"total input candidates: {counts['total_risk_rows']}")
    print(f"risk passed (assumed from VPS candidates): {counts['risk_passed']}")
    print(f"high-CV passed: {counts['high_cv_passed']}")
    if "monitor_passed_before_model_ready" in counts:
        print(f"model ready: {counts['model_ready_passed']}")
        print(f"monitor before model-ready filter: {counts['monitor_passed_before_model_ready']}")
    print(f"final monitor items: {counts['monitor_passed']}")
    if "tier_a_items" in counts:
        print(
            "tier split: "
            f"A={counts['tier_a_items']} "
            f"B={counts['tier_b_items']} "
            f"C={counts['tier_c_items']} "
            f"(queue pattern: {','.join(DEFAULT_QUEUE_PATTERN)})"
        )
    print(f"saved audit csv: {out_csv}")
    print(f"saved ITEMS py: {out_items_py}")

    if not items:
        print("warning: monitor list is empty")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
