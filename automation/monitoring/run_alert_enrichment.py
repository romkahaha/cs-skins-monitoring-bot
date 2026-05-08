"""Run one post-alert enrichment job."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from automation.alert_enrichment import run_enrichment_job
from automation.config import load_json_config, monitoring_defaults
from automation.risk_filters import repo_root_from


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def parse_args() -> argparse.Namespace:
    root = repo_root_from(Path(__file__))
    parser = argparse.ArgumentParser(description="Run a queued Telegram alert enrichment job.")
    parser.add_argument(
        "--config",
        type=Path,
        default=root / "automation" / "configs" / "monitoring.json",
        help="Monitoring automation JSON config.",
    )
    parser.add_argument("--job-json", type=Path, required=True, help="Queued enrichment job JSON.")
    parser.add_argument("--dry-run", action="store_true", help="Render the AI note locally without Telegram send.")
    return parser.parse_args()


def main() -> int:
    configure_stdio()
    args = parse_args()
    config = load_json_config(args.config.resolve(), monitoring_defaults())
    ok = run_enrichment_job(args.job_json.resolve(), config, dry_run=args.dry_run)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
