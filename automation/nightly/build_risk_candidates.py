"""Build the risk-only candidate list from the latest risk metrics CSV."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from automation.config import load_json_config, nightly_defaults, path_from_config
from automation.risk_filters import FilterConfig, apply_risk_filter, load_risk_metrics, repo_root_from


def parse_args() -> argparse.Namespace:
    root = repo_root_from(Path(__file__))
    parser = argparse.ArgumentParser(description="Build automation_runtime/risk_candidates_latest.csv.")
    parser.add_argument(
        "--config",
        type=Path,
        default=root / "automation" / "configs" / "nightly.json",
        help="Nightly automation JSON config.",
    )
    parser.add_argument("--risk-csv", type=Path, default=None, help="Input risk metrics CSV.")
    parser.add_argument("--out-csv", type=Path, default=None, help="Output risk-passed candidates CSV.")
    return parser.parse_args()


def filter_config(config: dict) -> FilterConfig:
    risk_filter = config.get("risk_filter", {})
    high_cv_filter = config.get("high_cv_filter", {})
    return FilterConfig(
        risk_ret_7d_min=float(risk_filter.get("ret_7d_min", -0.03)),
        risk_downside_14d_max=float(risk_filter.get("downside_14d_max", 0.17)),
        risk_sales_7d_n_min=int(risk_filter.get("sales_7d_n_min", 21)),
        risk_tail_ratio_min=float(risk_filter.get("tail_ratio_min", 0.85)),
        risk_n_listings_min=int(risk_filter.get("n_listings_min", 20)),
        cv_min_listings=int(high_cv_filter.get("min_listings", 3)),
        pred_cv_min=float(high_cv_filter.get("pred_cv_min", 0.075)),
        pred_range_over_mean_min=float(high_cv_filter.get("pred_range_over_mean_min", 0.3)),
    )


def main() -> int:
    args = parse_args()
    config_path = args.config.resolve()
    config = load_json_config(config_path, nightly_defaults())
    risk_csv = args.risk_csv.resolve() if args.risk_csv else path_from_config(config, "risk_csv")
    out_csv = args.out_csv.resolve() if args.out_csv else path_from_config(config, "risk_candidates_csv")

    frame = apply_risk_filter(load_risk_metrics(risk_csv), filter_config(config))
    candidates = frame.loc[frame["risk_pass"]].copy().reset_index(drop=True)
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    candidates.to_csv(out_csv, index=False)

    print(f"config: {config_path}")
    print(f"risk csv: {risk_csv}")
    print(f"total risk rows: {len(frame)}")
    print(f"risk candidates: {len(candidates)}")
    print(f"saved risk candidates: {out_csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
