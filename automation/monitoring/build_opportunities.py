"""CLI for building enriched Steam listing opportunities from local artifacts."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from automation.config import load_json_config, monitoring_defaults, path_from_config
from automation.listing_enrichment import (
    OpportunityConfig,
    build_enriched_listings,
    write_opportunity_outputs,
)
from automation.risk_filters import repo_root_from


def parse_args() -> argparse.Namespace:
    root = repo_root_from(Path(__file__))
    parser = argparse.ArgumentParser(description="Build opportunities_latest.csv from existing Steam listings/model/risk files.")
    parser.add_argument(
        "--config",
        type=Path,
        default=root / "automation" / "configs" / "monitoring.json",
        help="Monitoring automation JSON config.",
    )
    parser.add_argument(
        "--monitor-items-py",
        type=Path,
        default=None,
        help="Python item list used to restrict listings before enrichment.",
    )
    parser.add_argument(
        "--listings-csv",
        type=Path,
        default=None,
        help="Steam listing-level CSV.",
    )
    parser.add_argument(
        "--realtime-base-csv",
        type=Path,
        default=None,
        help="Realtime CSFloat base snapshot CSV.",
    )
    parser.add_argument(
        "--fit-json",
        type=Path,
        default=None,
        help="Saved float-fit curves JSON.",
    )
    parser.add_argument(
        "--risk-csv",
        type=Path,
        default=None,
        help="Risk metrics CSV.",
    )
    parser.add_argument(
        "--out-enriched-csv",
        type=Path,
        default=None,
        help="Output enriched listing-level CSV.",
    )
    parser.add_argument(
        "--out-opportunities-csv",
        type=Path,
        default=None,
        help="Output filtered opportunities CSV.",
    )
    parser.add_argument(
        "--out-report-csv",
        type=Path,
        default=None,
        help="Output opportunity filter report CSV.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = load_json_config(args.config.resolve() if args.config else None, monitoring_defaults())
    opp_cfg = config.get("opportunity_filter", {})
    cfg = OpportunityConfig(
        steam_sales_n_min=int(opp_cfg.get("steam_sales_n_min", 50)),
        downside_risk_max=float(opp_cfg.get("downside_risk_max", 10.0)),
        tail_ratio_min=float(opp_cfg.get("tail_ratio_min", 0.9)),
        downside_14d_max=float(opp_cfg.get("downside_14d_max", 0.12)),
        continuity_ratio_max=float(opp_cfg.get("continuity_ratio_max", 3.5)),
        spread_hybrid_disc_max=float(opp_cfg.get("spread_hybrid_disc_max", 0.17)),
    )
    monitor_items_py = args.monitor_items_py.resolve() if args.monitor_items_py else path_from_config(config, "monitor_items_py")
    listings_csv = args.listings_csv.resolve() if args.listings_csv else path_from_config(config, "steam_listings_csv")
    base_csv = args.realtime_base_csv.resolve() if args.realtime_base_csv else path_from_config(config, "base_snapshot_csv")
    fit_json = args.fit_json.resolve() if args.fit_json else path_from_config(config, "fit_json")
    risk_csv = args.risk_csv.resolve() if args.risk_csv else path_from_config(config, "risk_csv")
    enriched_csv = args.out_enriched_csv.resolve() if args.out_enriched_csv else path_from_config(config, "enriched_listings_csv")
    opportunities_csv = args.out_opportunities_csv.resolve() if args.out_opportunities_csv else path_from_config(config, "opportunities_csv")
    report_csv = args.out_report_csv.resolve() if args.out_report_csv else path_from_config(config, "opportunities_report_csv")
    enriched, opportunities, report = build_enriched_listings(
        listings_csv,
        base_csv,
        fit_json,
        risk_csv,
        monitor_items_py=monitor_items_py,
        cfg=cfg,
    )
    write_opportunity_outputs(
        enriched,
        opportunities,
        report,
        enriched_csv=enriched_csv,
        opportunities_csv=opportunities_csv,
        report_csv=report_csv,
    )

    print(f"config: {args.config.resolve() if args.config else '<defaults>'}")
    print(f"monitor items: {monitor_items_py}")
    print(f"listings rows enriched: {len(enriched)}")
    print(f"opportunities rows: {len(opportunities)}")
    print(f"unique enriched items: {enriched['item'].nunique() if 'item' in enriched.columns else 0}")
    print(f"unique opportunity items: {opportunities['item'].nunique() if not opportunities.empty and 'item' in opportunities.columns else 0}")
    print(f"saved enriched csv: {enriched_csv}")
    print(f"saved opportunities csv: {opportunities_csv}")
    print(f"saved report csv: {report_csv}")
    if opportunities.empty:
        print("warning: no rows passed opportunity filters")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
