"""CLI for sending Telegram alerts from opportunities_latest.csv."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from automation.config import load_json_config, monitoring_defaults, path_from_config
from automation.risk_filters import repo_root_from
from automation.telegram_alerts import send_opportunity_alerts


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def parse_args() -> argparse.Namespace:
    root = repo_root_from(Path(__file__))
    parser = argparse.ArgumentParser(description="Send Telegram alerts for new opportunity rows.")
    parser.add_argument(
        "--config",
        type=Path,
        default=root / "automation" / "configs" / "monitoring.json",
        help="Monitoring automation JSON config.",
    )
    parser.add_argument(
        "--opportunities-csv",
        type=Path,
        default=None,
        help="Filtered opportunities CSV.",
    )
    parser.add_argument(
        "--state-json",
        type=Path,
        default=None,
        help="State JSON used for alert dedupe/cooldown.",
    )
    parser.add_argument(
        "--monitor-items-py",
        type=Path,
        default=None,
        help="Current monitor item list, used to validate/reset state shape.",
    )
    parser.add_argument("--bot-token", type=str, default=None, help="Telegram bot token; otherwise env.")
    parser.add_argument("--chat-id", type=str, default=None, help="Telegram chat/channel id; otherwise env.")
    parser.add_argument("--cooldown-hours", type=float, default=12.0, help="Repeat alert cooldown per listing id.")
    parser.add_argument("--sleep-sec", type=float, default=0.6, help="Pause between Telegram messages.")
    parser.add_argument("--max-alerts", type=int, default=None, help="Optional cap for one run.")
    parser.add_argument("--dry-run", action="store_true", help="Print messages instead of sending and do not update state.")
    return parser.parse_args()


def main() -> int:
    configure_stdio()
    args = parse_args()
    config = load_json_config(args.config.resolve() if args.config else None, monitoring_defaults())
    alerts_cfg = config.get("alerts", {})
    telegram_cfg = config.get("telegram", {})
    plot_cfg = config.get("model_plot", {})
    opportunities_csv = args.opportunities_csv.resolve() if args.opportunities_csv else path_from_config(config, "opportunities_csv")
    state_json = args.state_json.resolve() if args.state_json else path_from_config(config, "state_json")
    monitor_items_py = args.monitor_items_py.resolve() if args.monitor_items_py else path_from_config(config, "monitor_items_py")
    max_alerts = args.max_alerts if args.max_alerts is not None else telegram_cfg.get("max_alerts")
    stats = send_opportunity_alerts(
        opportunities_csv,
        state_json,
        monitor_items_py,
        bot_token=args.bot_token,
        chat_id=args.chat_id,
        cooldown_hours=float(args.cooldown_hours if args.cooldown_hours != 12.0 else telegram_cfg.get("cooldown_hours", 12.0)),
        dry_run=args.dry_run,
        sleep_sec=float(args.sleep_sec if args.sleep_sec != 0.6 else telegram_cfg.get("sleep_sec", 0.6)),
        max_alerts=max_alerts,
        alerts_cfg=alerts_cfg,
        plot_cfg=plot_cfg,
    )
    print(f"opportunity rows loaded: {stats['loaded']}")
    print(f"alerts filtered out: {stats['filtered']}")
    print(f"alerts considered: {stats['considered']}")
    print(f"alerts sent: {stats['sent']}")
    print(f"alerts skipped: {stats['skipped']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
