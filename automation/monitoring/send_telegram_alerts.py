"""CLI for sending Telegram alerts from opportunities_latest.csv."""

from __future__ import annotations

import argparse
import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import fcntl

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
        "--alert-state-json",
        type=Path,
        default=None,
        help="Explicit Telegram dedupe state JSON; defaults to <state-json stem>_telegram_alerts.json.",
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
    parser.add_argument("--delete-input-after", action="store_true", help="Delete the opportunities CSV after sending.")
    parser.add_argument("--dry-run", action="store_true", help="Print messages instead of sending and do not update state.")
    return parser.parse_args()


def alert_state_path_from(path: Path) -> Path:
    if path.stem.endswith("_telegram_alerts"):
        return path
    return path.with_name(f"{path.stem}_telegram_alerts{path.suffix}")


def bootstrap_alert_state(alert_state_json: Path, batch_state_json: Path) -> None:
    if alert_state_json.exists() or not batch_state_json.exists():
        return
    try:
        payload = json.loads(batch_state_json.read_text(encoding="utf-8"))
    except Exception:
        return
    if not isinstance(payload, dict):
        return
    if "sent_alerts" not in payload:
        return
    alert_state_json.parent.mkdir(parents=True, exist_ok=True)
    alert_state_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


@contextmanager
def advisory_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def main() -> int:
    configure_stdio()
    args = parse_args()
    config = load_json_config(args.config.resolve() if args.config else None, monitoring_defaults())
    alerts_cfg = config.get("alerts", {})
    telegram_cfg = config.get("telegram", {})
    plot_cfg = config.get("model_plot", {})
    alert_enrichment_cfg = config.get("alert_enrichment", {})
    opportunities_csv = args.opportunities_csv.resolve() if args.opportunities_csv else path_from_config(config, "opportunities_csv")
    batch_state_json = args.state_json.resolve() if args.state_json else path_from_config(config, "state_json")
    state_json = args.alert_state_json.resolve() if args.alert_state_json else alert_state_path_from(batch_state_json)
    monitor_items_py = args.monitor_items_py.resolve() if args.monitor_items_py else path_from_config(config, "monitor_items_py")
    max_alerts = args.max_alerts if args.max_alerts is not None else telegram_cfg.get("max_alerts")
    bootstrap_alert_state(state_json, batch_state_json)
    lock_path = state_json.with_suffix(f"{state_json.suffix}.lock")
    with advisory_lock(lock_path):
        stats = send_opportunity_alerts(
            opportunities_csv,
            state_json,
            monitor_items_py,
            config_path=args.config.resolve() if args.config else None,
            bot_token=args.bot_token,
            chat_id=args.chat_id,
            cooldown_hours=float(args.cooldown_hours if args.cooldown_hours != 12.0 else telegram_cfg.get("cooldown_hours", 12.0)),
            dry_run=args.dry_run,
            sleep_sec=float(args.sleep_sec if args.sleep_sec != 0.6 else telegram_cfg.get("sleep_sec", 0.6)),
            max_alerts=max_alerts,
            alerts_cfg=alerts_cfg,
            plot_cfg=plot_cfg,
            alert_enrichment_cfg=alert_enrichment_cfg,
        )
    if args.delete_input_after:
        try:
            os.unlink(opportunities_csv)
        except FileNotFoundError:
            pass
    print(f"opportunity rows loaded: {stats['loaded']}")
    print(f"alerts filtered out: {stats['filtered']}")
    print(f"alerts considered: {stats['considered']}")
    print(f"alerts sent: {stats['sent']}")
    print(f"alerts skipped: {stats['skipped']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
