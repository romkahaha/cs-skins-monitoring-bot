"""Run one monitoring batch: fetch fresh listings/base, enrich, and update state."""

from __future__ import annotations

import argparse
import os
import shlex
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from automation.config import load_json_config, monitoring_defaults, path_from_config
from automation.listing_enrichment import OpportunityConfig, build_enriched_listings, load_items_py, write_opportunity_outputs
from automation.monitoring.runtime_integrity import ensure_monitor_runtime_integrity
from automation.monitoring.send_telegram_alerts import bootstrap_alert_state
from automation.monitoring.tier_scheduler import (
    alert_monitor_items_py_from_config,
    alert_state_json_from_config,
    resolve_batch_state_path,
)
from automation.risk_filters import repo_root_from
from automation.state import load_state, mark_run_finished, mark_run_started, save_state, select_batch
from automation.telegram_alerts import send_opportunity_alerts


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _load_steam_scm_listings(repo_root: Path):
    steam_dir = repo_root / "steam_listings"
    steam_dir_str = str(steam_dir)
    if steam_dir_str not in sys.path:
        sys.path.insert(0, steam_dir_str)
    import importlib.util

    module_path = steam_dir / "steam_scm_listings.py"
    spec = importlib.util.spec_from_file_location("automation_steam_scm_listings", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot import {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _apply_steam_scm_config(
    module,
    monitoring_cfg: dict,
    steam_scm_cfg: dict,
    *,
    max_listings_override: int | None = None,
) -> None:
    config = getattr(module, "CONFIG", None)
    if not isinstance(config, dict):
        return

    max_listings = (
        max_listings_override
        if max_listings_override is not None
        else steam_scm_cfg.get("max_listings_per_item", monitoring_cfg.get("max_listings_per_item"))
    )
    if max_listings is not None:
        config["max_listings_per_skin"] = int(max_listings)

    direct_keys = [
        "listings_per_request",
        "request_timeout_sec",
        "retry_attempts",
        "retry_sleep_min_sec",
        "retry_sleep_max_sec",
        "delay_between_skins_min_sec",
        "delay_between_skins_max_sec",
        "delay_between_render_pages_min_sec",
        "delay_between_render_pages_max_sec",
        "batch_log_progress",
    ]
    for key in direct_keys:
        if key in steam_scm_cfg and steam_scm_cfg[key] is not None:
            config[key] = steam_scm_cfg[key]


def parse_args() -> argparse.Namespace:
    root = repo_root_from(Path(__file__))
    parser = argparse.ArgumentParser(description="Run one scheduled monitoring batch.")
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
        help="Python item list with ITEMS = [...].",
    )
    parser.add_argument(
        "--state-json",
        type=Path,
        default=None,
        help="Batch pointer state file.",
    )
    parser.add_argument(
        "--alert-state-json",
        type=Path,
        default=None,
        help="Shared Telegram alert dedupe state JSON.",
    )
    parser.add_argument("--batch-size", type=int, default=None, help="Number of items to scan in this run.")
    parser.add_argument(
        "--listings-out-csv",
        type=Path,
        default=None,
        help="Fresh Steam listings output CSV for this batch.",
    )
    parser.add_argument(
        "--base-out-csv",
        type=Path,
        default=None,
        help="Existing CSFloat base snapshot CSV. Build it first with automation/nightly/build_base_snapshot.py.",
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
    parser.add_argument(
        "--max-listings-per-item",
        type=int,
        default=None,
        help="Override max Steam listings depth for this batch.",
    )
    parser.add_argument("--send-telegram", action="store_true", help="Send Telegram alerts after building opportunities.")
    parser.add_argument("--telegram-dry-run", action="store_true", help="Print Telegram messages instead of sending.")
    parser.add_argument("--ignore-schedule", action="store_true", help="Run even outside the configured active window.")
    return parser.parse_args()


def file_age_hours(path: Path) -> float:
    modified = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    return (datetime.now(timezone.utc) - modified).total_seconds() / 3600.0


def require_file(path: Path, label: str) -> None:
    if not path.is_file():
        raise FileNotFoundError(f"{label} not found: {path}")


def check_age(path: Path, label: str, max_age_hours: float | None, *, fail_on_stale: bool) -> None:
    if max_age_hours is None:
        return
    age = file_age_hours(path)
    if age <= float(max_age_hours):
        return
    msg = f"{label} is stale: {age:.1f}h > {float(max_age_hours):.1f}h ({path})"
    if fail_on_stale:
        raise RuntimeError(msg)
    print(f"warning: {msg}")


def queue_alert_snapshot(opportunities_csv: Path, *, start_pointer: int) -> tuple[Path, Path]:
    queue_dir = opportunities_csv.parent / "telegram_queue"
    queue_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    stem = f"{opportunities_csv.stem}_{stamp}_p{start_pointer}_pid{os.getpid()}"
    snapshot_csv = queue_dir / f"{stem}.csv"
    log_path = queue_dir / f"{stem}.log"
    shutil.copy2(opportunities_csv, snapshot_csv)
    return snapshot_csv, log_path


def spawn_telegram_sender(
    *,
    repo_root: Path,
    config_path: Path,
    opportunities_csv: Path,
    state_json: Path,
    monitor_items_py: Path,
    alert_state_json: Path,
    dry_run: bool,
    log_path: Path,
) -> int:
    sender_cmd = [
        sys.executable,
        str(repo_root / "automation" / "monitoring" / "send_telegram_alerts.py"),
        "--config",
        str(config_path),
        "--opportunities-csv",
        str(opportunities_csv),
        "--state-json",
        str(state_json),
        "--alert-state-json",
        str(alert_state_json),
        "--monitor-items-py",
        str(monitor_items_py),
        "--delete-input-after",
    ]
    if dry_run:
        sender_cmd.append("--dry-run")

    secrets_file = os.environ.get("CS_ARBITRAGE_SECRETS") or str(repo_root.parent / "secrets.env")
    if os.path.isfile(secrets_file):
        bash_cmd = (
            f"set -a; source {shlex.quote(secrets_file)}; set +a; "
            f"exec {shlex.join(sender_cmd)}"
        )
        cmd = ["/bin/bash", "-lc", bash_cmd]
    else:
        cmd = sender_cmd
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab") as handle:
        proc = subprocess.Popen(
            cmd,
            cwd=str(repo_root),
            stdout=handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    return int(proc.pid)


def parse_hhmm(value: str) -> tuple[int, int]:
    hh, mm = str(value).split(":", 1)
    return int(hh), int(mm)


def minutes_since_midnight(dt: datetime) -> int:
    return dt.hour * 60 + dt.minute


def is_active_now(schedule_cfg: dict) -> tuple[bool, datetime]:
    tz = ZoneInfo(str(schedule_cfg.get("timezone", "Europe/Prague")))
    now = datetime.now(tz)
    start_h, start_m = parse_hhmm(str(schedule_cfg.get("active_from", "00:00")))
    end_h, end_m = parse_hhmm(str(schedule_cfg.get("active_to", "23:59")))
    cur = minutes_since_midnight(now)
    start = start_h * 60 + start_m
    end = end_h * 60 + end_m
    if start <= end:
        return start <= cur <= end, now
    return cur >= start or cur <= end, now


def run_preflight(
    config: dict,
    *,
    monitor_items_py: Path,
    base_snapshot_csv: Path,
    risk_csv: Path,
    fit_json: Path,
) -> None:
    preflight = config.get("preflight", {})
    fail_on_stale = bool(preflight.get("fail_on_stale_inputs", False))

    if preflight.get("require_monitor_items_py", True):
        require_file(monitor_items_py, "monitor item list")
        check_age(
            monitor_items_py,
            "monitor item list",
            preflight.get("max_monitor_items_age_hours"),
            fail_on_stale=fail_on_stale,
        )
    if preflight.get("require_base_snapshot_csv", True):
        require_file(base_snapshot_csv, "base snapshot CSV")
        check_age(
            base_snapshot_csv,
            "base snapshot CSV",
            preflight.get("max_base_snapshot_age_hours"),
            fail_on_stale=fail_on_stale,
        )
    if preflight.get("require_risk_csv", True):
        require_file(risk_csv, "risk CSV")
        check_age(risk_csv, "risk CSV", preflight.get("max_risk_csv_age_hours"), fail_on_stale=fail_on_stale)
    if preflight.get("require_fit_json", True):
        require_file(fit_json, "fit JSON")
        check_age(fit_json, "fit JSON", preflight.get("max_fit_json_age_hours"), fail_on_stale=fail_on_stale)


def main() -> int:
    configure_stdio()
    args = parse_args()
    config = load_json_config(args.config.resolve() if args.config else None, monitoring_defaults())
    schedule_cfg = config.get("schedule", {})
    preflight_cfg = config.get("preflight", {})
    monitoring_cfg = config.get("monitoring", {})
    steam_scm_cfg = config.get("steam_scm", {})
    opp_cfg = config.get("opportunity_filter", {})
    alerts_cfg = config.get("alerts", {})
    telegram_cfg = config.get("telegram", {})
    plot_cfg = config.get("model_plot", {})
    alert_enrichment_cfg = config.get("alert_enrichment", {})

    monitor_items_py = args.monitor_items_py.resolve() if args.monitor_items_py else path_from_config(config, "monitor_items_py")
    state_path = args.state_json.resolve() if args.state_json else resolve_batch_state_path(config, monitor_items_py)
    alert_state_path = (
        args.alert_state_json.resolve()
        if args.alert_state_json
        else alert_state_json_from_config(config, fallback_state_json=state_path)
    )
    alert_monitor_items_py = alert_monitor_items_py_from_config(config)
    listings_out_csv = args.listings_out_csv.resolve() if args.listings_out_csv else path_from_config(config, "steam_listings_csv")
    base_snapshot_csv = args.base_out_csv.resolve() if args.base_out_csv else path_from_config(config, "base_snapshot_csv")
    fit_json = args.fit_json.resolve() if args.fit_json else path_from_config(config, "fit_json")
    risk_csv = args.risk_csv.resolve() if args.risk_csv else path_from_config(config, "risk_csv")
    enriched_csv = args.out_enriched_csv.resolve() if args.out_enriched_csv else path_from_config(config, "enriched_listings_csv")
    opportunities_csv = args.out_opportunities_csv.resolve() if args.out_opportunities_csv else path_from_config(config, "opportunities_csv")
    report_csv = args.out_report_csv.resolve() if args.out_report_csv else path_from_config(config, "opportunities_report_csv")
    batch_size = int(args.batch_size if args.batch_size is not None else monitoring_cfg.get("batch_size", 5))
    config_path = args.config.resolve() if args.config else repo_root_from(Path(__file__)) / "automation" / "configs" / "monitoring.json"
    repo_root = repo_root_from(Path(__file__))

    print(f"config: {config_path}")
    print(
        "schedule: "
        f"{schedule_cfg.get('active_from', '<unset>')}..{schedule_cfg.get('active_to', '<unset>')} "
        f"{schedule_cfg.get('timezone', '<unset>')} every {schedule_cfg.get('interval_minutes', '<unset>')}m "
        f"(github cron UTC: {schedule_cfg.get('github_actions_cron_utc', '<unset>')})"
    )
    if not bool(monitoring_cfg.get("enabled", True)):
        print("monitoring disabled by config")
        return 0

    if bool(schedule_cfg.get("enabled", True)) and not args.ignore_schedule:
        active, now_local = is_active_now(schedule_cfg)
        print(f"local schedule time: {now_local.isoformat(timespec='seconds')} active={active}")
        if not active and bool(schedule_cfg.get("enforce_active_window", False)):
            print("outside active window; skipping run")
            return 0

    integrity_report = ensure_monitor_runtime_integrity(config, repo_root)
    for action in integrity_report.actions:
        print(f"runtime integrity: {action}")
    if integrity_report.warnings:
        for warning in integrity_report.warnings:
            print(f"runtime integrity warning: {warning}")
        raise RuntimeError("monitor runtime integrity check failed")

    run_preflight(
        config,
        monitor_items_py=monitor_items_py,
        base_snapshot_csv=base_snapshot_csv,
        risk_csv=risk_csv,
        fit_json=fit_json,
    )

    opp_config = OpportunityConfig(
        steam_sales_n_min=int(opp_cfg.get("steam_sales_n_min", 50)),
        downside_risk_max=float(opp_cfg.get("downside_risk_max", 10.0)),
        tail_ratio_min=float(opp_cfg.get("tail_ratio_min", 0.9)),
        downside_14d_max=float(opp_cfg.get("downside_14d_max", 0.12)),
        continuity_ratio_max=float(opp_cfg.get("continuity_ratio_max", 3.5)),
        spread_hybrid_disc_max=float(opp_cfg.get("spread_hybrid_disc_max", 0.17)),
    )

    items = load_items_py(monitor_items_py)
    if not items:
        print(f"no items in {monitor_items_py}")
        return 1
    min_monitor_items = int(preflight_cfg.get("min_monitor_items", 1))
    if len(items) < min_monitor_items:
        raise RuntimeError(f"monitor item list too small: {len(items)} < {min_monitor_items}")

    state = load_state(state_path, items)
    batch, start_pointer, next_pointer = select_batch(items, state, batch_size)
    state = mark_run_started(state, batch, start_pointer)
    save_state(state_path, state)

    print(f"monitor items: {len(items)} from {monitor_items_py}")
    print(f"batch start pointer: {start_pointer}")
    print(f"batch size: {len(batch)}")
    for idx, item in enumerate(batch, start=1):
        print(f"  {idx}. {item}")

    listing_errors: list[dict] = []
    listing_rows = 0
    alert_stats: dict[str, int] = {}

    try:
        if not base_snapshot_csv.is_file():
            raise FileNotFoundError(
                f"{base_snapshot_csv} not found; run python automation/nightly/build_base_snapshot.py first"
            )

        steam_scm_listings = _load_steam_scm_listings(repo_root)
        _apply_steam_scm_config(
            steam_scm_listings,
            monitoring_cfg,
            steam_scm_cfg,
            max_listings_override=args.max_listings_per_item,
        )
        if args.max_listings_per_item is not None:
            print(f"steam depth override: max_listings_per_item={int(args.max_listings_per_item)}")
        listings_path, listing_errors, listings_df = steam_scm_listings.run_batch_to_csv(
            batch,
            out_csv=listings_out_csv,
        )
        listing_rows = len(listings_df)
        print(f"fresh listings: {listings_path} rows={len(listings_df)} errors={len(listing_errors)}")
        print(f"base snapshot: {base_snapshot_csv}")
        if (
            bool(monitoring_cfg.get("fail_if_all_listing_fetches_error", True))
            and listing_errors
            and len(listing_errors) >= len(batch)
        ):
            failed_items = ", ".join(str(err.get("market_hash_name", "?")) for err in listing_errors[:3])
            raise RuntimeError(
                f"all Steam listing fetches failed for this batch ({len(listing_errors)} item errors: {failed_items})"
            )
        if listings_df.empty and listing_errors:
            failed_items = ", ".join(str(err.get("market_hash_name", "?")) for err in listing_errors[:3])
            raise RuntimeError(
                f"Steam listings fetch returned 0 rows with {len(listing_errors)} item errors"
                f" ({failed_items})"
            )

        enriched, opportunities, report = build_enriched_listings(
            listings_out_csv,
            base_snapshot_csv,
            fit_json,
            risk_csv,
            monitor_items_py=monitor_items_py,
            cfg=opp_config,
        )
        write_opportunity_outputs(
            enriched,
            opportunities,
            report,
            enriched_csv=enriched_csv,
            opportunities_csv=opportunities_csv,
            report_csv=report_csv,
        )

        telegram_enabled = bool(telegram_cfg.get("enabled", False)) or args.send_telegram or args.telegram_dry_run
        force_inline_sender = bool(telegram_cfg.get("force_inline_sender", False))
        if telegram_enabled:
            if args.telegram_dry_run:
                stats = send_opportunity_alerts(
                    opportunities_csv,
                    alert_state_path,
                    alert_monitor_items_py,
                    config_path=config_path,
                    cooldown_hours=float(telegram_cfg.get("cooldown_hours", 12.0)),
                    dry_run=True,
                    sleep_sec=float(telegram_cfg.get("sleep_sec", 0.6)),
                    max_alerts=telegram_cfg.get("max_alerts"),
                    alerts_cfg=alerts_cfg,
                    plot_cfg=plot_cfg,
                    alert_enrichment_cfg=alert_enrichment_cfg,
                )
                alert_stats = stats
                print(
                    "telegram alerts: "
                    f"loaded={stats['loaded']} filtered={stats['filtered']} "
                    f"considered={stats['considered']} sent={stats['sent']} skipped={stats['skipped']}"
                )
            elif force_inline_sender:
                bootstrap_alert_state(alert_state_path, state_path)
                stats = send_opportunity_alerts(
                    opportunities_csv,
                    alert_state_path,
                    alert_monitor_items_py,
                    config_path=config_path,
                    cooldown_hours=float(telegram_cfg.get("cooldown_hours", 12.0)),
                    dry_run=False,
                    sleep_sec=float(telegram_cfg.get("sleep_sec", 0.6)),
                    max_alerts=telegram_cfg.get("max_alerts"),
                    alerts_cfg=alerts_cfg,
                    plot_cfg=plot_cfg,
                    alert_enrichment_cfg=alert_enrichment_cfg,
                )
                alert_stats = stats
                print(
                    "telegram alerts inline: "
                    f"loaded={stats['loaded']} filtered={stats['filtered']} "
                    f"considered={stats['considered']} sent={stats['sent']} skipped={stats['skipped']}"
                )
            else:
                snapshot_csv, alert_log = queue_alert_snapshot(opportunities_csv, start_pointer=start_pointer)
                bootstrap_alert_state(alert_state_path, state_path)
                try:
                    sender_pid = spawn_telegram_sender(
                        repo_root=repo_root,
                        config_path=config_path,
                        opportunities_csv=snapshot_csv,
                        state_json=state_path,
                        monitor_items_py=alert_monitor_items_py,
                        alert_state_json=alert_state_path,
                        dry_run=False,
                        log_path=alert_log,
                    )
                except Exception as exc:
                    print(f"telegram background sender failed to start, falling back to inline send: {exc}")
                    stats = send_opportunity_alerts(
                        snapshot_csv,
                        alert_state_path,
                        alert_monitor_items_py,
                        config_path=config_path,
                        cooldown_hours=float(telegram_cfg.get("cooldown_hours", 12.0)),
                        dry_run=False,
                        sleep_sec=float(telegram_cfg.get("sleep_sec", 0.6)),
                        max_alerts=telegram_cfg.get("max_alerts"),
                        alerts_cfg=alerts_cfg,
                        plot_cfg=plot_cfg,
                        alert_enrichment_cfg=alert_enrichment_cfg,
                    )
                    alert_stats = stats
                    print(
                        "telegram alerts: "
                        f"loaded={stats['loaded']} filtered={stats['filtered']} "
                        f"considered={stats['considered']} sent={stats['sent']} skipped={stats['skipped']}"
                    )
                    try:
                        snapshot_csv.unlink()
                    except FileNotFoundError:
                        pass
                else:
                    alert_stats = {
                        "mode": "background",
                        "queued_rows": int(len(opportunities)),
                        "sender_pid": int(sender_pid),
                    }
                    print(
                        "telegram alerts queued: "
                        f"rows={len(opportunities)} pid={sender_pid} snapshot={snapshot_csv} log={alert_log}"
                    )

    except Exception as exc:
        state = mark_run_finished(
            state,
            next_pointer=start_pointer,
            status="error",
            error=str(exc),
            listing_errors=listing_errors,
            listing_rows=listing_rows,
            alert_stats=alert_stats,
        )
        save_state(state_path, state)
        print(f"monitoring failed: {exc}", file=sys.stderr)
        return 1

    state = mark_run_finished(
        state,
        next_pointer=next_pointer,
        status="ok",
        listing_errors=listing_errors,
        listing_rows=listing_rows,
        enriched_rows=len(enriched),
        opportunities_rows=len(opportunities),
        alert_stats=alert_stats,
    )
    save_state(state_path, state)

    print(f"enriched rows: {len(enriched)}")
    print(f"opportunity rows: {len(opportunities)}")
    print(f"next batch pointer: {next_pointer}")
    print(f"saved state: {state_path}")
    print(f"saved opportunities: {opportunities_csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
