"""Run monitoring batches continuously inside one GitHub Actions job."""

from __future__ import annotations

import argparse
import os
import random
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from automation.config import load_json_config, monitoring_defaults, path_from_config
from automation.failover_monitoring import (
    import_runtime_state_from_failover,
    load_failover_config,
    sync_monitoring_failover,
)
from automation.listing_enrichment import load_items_py
from automation.monitoring.runtime_integrity import ensure_monitor_runtime_integrity
from automation.monitoring.tier_scheduler import (
    alert_state_json_from_config,
    batch_sizes_from_config,
    listing_caps_from_config,
    load_scheduler_state,
    load_tier_items,
    mark_scheduler_run_finished,
    mark_scheduler_run_started,
    queue_pattern_from_config,
    save_scheduler_state,
    select_next_tier,
    tier_item_paths_from_config,
    tier_items_match_full_list,
    tier_mode_enabled,
    tier_state_paths_from_config,
)
from automation.risk_filters import repo_root_from
from automation.state import load_state, select_batch

RECOVERABLE_BATCH_ERROR_PATTERNS = (
    "all Steam listing fetches failed for this batch",
    "Steam listings fetch returned 0 rows with",
    "Steam rate limit (429)",
    "429",
    "Too Many Requests",
)


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def parse_args() -> argparse.Namespace:
    root = repo_root_from(Path(__file__))
    parser = argparse.ArgumentParser(description="Run sequential monitoring batches in a long cycle.")
    parser.add_argument(
        "--config",
        type=Path,
        default=root / "automation" / "configs" / "monitoring.json",
        help="Monitoring automation JSON config.",
    )
    parser.add_argument("--batch-size", type=int, default=None, help="Items per batch.")
    parser.add_argument("--send-telegram", action="store_true", help="Send real Telegram alerts.")
    parser.add_argument("--telegram-dry-run", action="store_true", help="Print Telegram alerts instead of sending.")
    parser.add_argument("--no-telegram", action="store_true", help="Disable Telegram for this cycle.")
    parser.add_argument("--max-runtime-minutes", type=float, default=None, help="Stop before this runtime is exceeded.")
    parser.add_argument("--max-batches", type=int, default=None, help="Stop after this many batches.")
    parser.add_argument("--max-cycles", type=int, default=None, help="Stop after this many full monitor-list cycles.")
    parser.add_argument("--commit-every-batches", type=int, default=None, help="Git checkpoint frequency.")
    parser.add_argument("--cycle-sleep-sec", type=float, default=None, help="Sleep after a full monitor-list cycle.")
    parser.add_argument("--no-git", action="store_true", help="Do not commit/push runtime checkpoints.")
    parser.add_argument("--ignore-schedule", action="store_true", help="Run even outside configured active window.")
    parser.add_argument("--dry-run", action="store_true", help="Print cycle plan without running batches.")
    return parser.parse_args()


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


def seconds_until_active_window_end(schedule_cfg: dict, *, now_local: datetime | None = None) -> float:
    tz = ZoneInfo(str(schedule_cfg.get("timezone", "Europe/Prague")))
    now = now_local or datetime.now(tz)
    start_h, start_m = parse_hhmm(str(schedule_cfg.get("active_from", "00:00")))
    end_h, end_m = parse_hhmm(str(schedule_cfg.get("active_to", "23:59")))
    cur = minutes_since_midnight(now)
    start = start_h * 60 + start_m
    end = end_h * 60 + end_m
    end_dt = now.replace(hour=end_h, minute=end_m, second=0, microsecond=0)
    if start <= end:
        if cur > end:
            return 0.0
    else:
        if cur >= start:
            end_dt = (now + timedelta(days=1)).replace(hour=end_h, minute=end_m, second=0, microsecond=0)
    return max(0.0, (end_dt - now).total_seconds())


def run_cmd(cmd: list[str], cwd: Path, *, check: bool = True) -> subprocess.CompletedProcess:
    print(" ".join(cmd), flush=True)
    return subprocess.run(cmd, cwd=str(cwd), check=check)


def current_branch(repo_root: Path) -> str:
    env_branch = (os.environ.get("GITHUB_REF_NAME") or "").strip()
    result = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=str(repo_root),
        check=True,
        text=True,
        capture_output=True,
    )
    branch = result.stdout.strip()
    if branch == "HEAD":
        return env_branch or "main"
    return branch


def commit_runtime(repo_root: Path, message: str) -> bool:
    run_cmd(["git", "add", "automation_runtime"], repo_root)
    diff = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=str(repo_root))
    if diff.returncode == 0:
        print("runtime checkpoint: no changes to commit")
        return False
    if diff.returncode != 1:
        raise RuntimeError(f"git diff --cached --quiet failed with exit {diff.returncode}")

    run_cmd(["git", "commit", "-m", message], repo_root)
    push = run_cmd(["git", "push"], repo_root, check=False)
    if push.returncode == 0:
        print("runtime checkpoint: committed and pushed")
        return True

    branch = current_branch(repo_root)
    print("runtime checkpoint: push rejected; rebasing and retrying")
    run_cmd(["git", "pull", "--rebase", "origin", branch], repo_root)
    run_cmd(["git", "push"], repo_root)
    print("runtime checkpoint: committed and pushed after rebase")
    return True


def batch_command(
    root: Path,
    config_path: Path,
    batch_size: int,
    telegram_mode: str,
    *,
    monitor_items_py: Path | None = None,
    state_json: Path | None = None,
    alert_state_json: Path | None = None,
    max_listings_per_item: int | None = None,
    tier: str | None = None,
) -> list[str]:
    cmd = [
        sys.executable,
        "-B",
        str(root / "automation" / "monitoring" / "run_batch.py"),
        "--config",
        str(config_path),
        "--batch-size",
        str(batch_size),
        "--ignore-schedule",
    ]
    if monitor_items_py is not None:
        cmd.extend(["--monitor-items-py", str(monitor_items_py)])
    if state_json is not None:
        cmd.extend(["--state-json", str(state_json)])
    if alert_state_json is not None:
        cmd.extend(["--alert-state-json", str(alert_state_json)])
    if max_listings_per_item is not None:
        cmd.extend(["--max-listings-per-item", str(max_listings_per_item)])
    if tier:
        cmd.extend(["--tier", str(tier)])
    if telegram_mode == "real":
        cmd.append("--send-telegram")
    elif telegram_mode == "dry-run":
        cmd.append("--telegram-dry-run")
    elif telegram_mode != "off":
        raise ValueError(f"Unknown telegram mode: {telegram_mode}")
    return cmd


def value_or_config(value, cfg: dict, key: str, default):
    if value is not None:
        return value
    raw = cfg.get(key, default)
    return default if raw is None else raw


def is_recoverable_batch_error(state: dict, cycle_cfg: dict) -> bool:
    error = str(state.get("last_error") or "")
    patterns = cycle_cfg.get("recoverable_batch_error_patterns", RECOVERABLE_BATCH_ERROR_PATTERNS)
    return any(str(pattern) and str(pattern) in error for pattern in patterns)


def is_rate_limit_batch_error(state: dict, cycle_cfg: dict) -> bool:
    error = str(state.get("last_error") or "")
    patterns = cycle_cfg.get("failover_trigger_error_patterns", ["Steam rate limit (429)", "429", "Too Many Requests"])
    return any(str(pattern) and str(pattern) in error for pattern in patterns)


def partial_rate_limit_listing_errors(state: dict, cycle_cfg: dict) -> list[str]:
    patterns = [str(pattern).lower() for pattern in cycle_cfg.get("failover_trigger_error_patterns", ["Steam rate limit (429)", "429", "too many requests"]) if str(pattern).strip()]
    out: list[str] = []
    for entry in state.get("last_listing_errors") or []:
        if not isinstance(entry, dict):
            continue
        item = str(entry.get("market_hash_name") or "?")
        meta = entry.get("meta")
        haystack = str(meta).lower()
        if any(pattern in haystack for pattern in patterns):
            out.append(item)
    return out


def maybe_sync_failover(
    *,
    root: Path,
    config_path: Path,
    config: dict,
    mode: str,
    reason: str = "",
    lease_seconds: int | None = None,
    state_path: Path,
    monitor_items_py: Path,
    batch_pointer: int | None,
) -> bool:
    failover_cfg = load_failover_config(config, root)
    if not failover_cfg.enabled:
        return False
    try:
        return sync_monitoring_failover(
            repo_root=root,
            config_path=config_path,
            config=config,
            mode=mode,
            reason=reason,
            lease_seconds=lease_seconds,
            state_path=state_path,
            monitor_items_py=monitor_items_py,
            batch_pointer=batch_pointer,
        )
    except Exception as exc:
        print(f"warning: failover sync ({mode}) failed: {exc}", file=sys.stderr, flush=True)
        return False


def maybe_import_failover_runtime(*, root: Path, config: dict, quiet: bool = False) -> bool:
    failover_cfg = load_failover_config(config, root)
    if not failover_cfg.enabled:
        return False
    try:
        return import_runtime_state_from_failover(repo_root=root, config=config, quiet=quiet)
    except Exception as exc:
        print(f"warning: failover runtime import failed: {exc}", file=sys.stderr, flush=True)
        return False


def rate_limit_failover_lease_seconds(
    *,
    schedule_cfg: dict,
    failover_cfg,
    recoverable_error_sleep_sec: float,
    now_local: datetime | None = None,
) -> int:
    lease = max(60, int(recoverable_error_sleep_sec))
    if not getattr(failover_cfg, "request_on_nightly_start", False):
        return lease
    remaining_to_nightly_sec = int(seconds_until_active_window_end(schedule_cfg, now_local=now_local))
    if remaining_to_nightly_sec > 0:
        lease = min(lease, max(60, remaining_to_nightly_sec))
    return lease


def main() -> int:
    configure_stdio()
    args = parse_args()
    root = repo_root_from(Path(__file__))
    config_path = args.config.resolve()
    config = load_json_config(config_path, monitoring_defaults())
    schedule_cfg = config.get("schedule", {})
    monitoring_cfg = config.get("monitoring", {})
    steam_scm_cfg = config.get("steam_scm", {})
    cycle_cfg = config.get("cycle", {})

    if not bool(monitoring_cfg.get("enabled", True)):
        print("monitoring disabled by config")
        return 0
    if not bool(cycle_cfg.get("enabled", True)):
        print("monitoring cycle disabled by config")
        return 0

    cycle_batch_size = cycle_cfg.get("batch_size")
    batch_size = int(args.batch_size if args.batch_size is not None else (cycle_batch_size or monitoring_cfg.get("batch_size", 5)))
    cycle_sleep_sec = float(value_or_config(args.cycle_sleep_sec, cycle_cfg, "cycle_sleep_sec", 600.0))
    commit_every_batches = int(value_or_config(args.commit_every_batches, cycle_cfg, "commit_every_batches", 5))
    max_runtime_minutes = float(value_or_config(args.max_runtime_minutes, cycle_cfg, "max_runtime_minutes", 350.0))
    max_batches = value_or_config(args.max_batches, cycle_cfg, "max_batches_per_run", None)
    max_cycles = value_or_config(args.max_cycles, cycle_cfg, "max_cycles_per_run", None)
    max_batches = None if max_batches is None else int(max_batches)
    max_cycles = None if max_cycles is None else int(max_cycles)
    recoverable_error_sleep_sec = float(value_or_config(args.cycle_sleep_sec, cycle_cfg, "recoverable_error_sleep_sec", cycle_sleep_sec))
    batch_sleep_min_sec = max(0.0, float(cycle_cfg.get("batch_sleep_min_sec", 0.0) or 0.0))
    batch_sleep_max_sec = max(batch_sleep_min_sec, float(cycle_cfg.get("batch_sleep_max_sec", batch_sleep_min_sec) or 0.0))
    commit_enabled = bool(cycle_cfg.get("commit_runtime", True)) and not args.no_git
    respect_active_window = bool(cycle_cfg.get("respect_active_window", True)) and not args.ignore_schedule
    checkpoint_message = str(cycle_cfg.get("checkpoint_message", "Update monitoring runtime [skip ci]"))

    if args.no_telegram:
        telegram_mode = "off"
    elif args.telegram_dry_run:
        telegram_mode = "dry-run"
    elif args.send_telegram:
        telegram_mode = "real"
    elif bool(config.get("telegram", {}).get("enabled", False)):
        telegram_mode = "real"
    else:
        telegram_mode = "off"

    monitor_items_py = path_from_config(config, "monitor_items_py")
    state_path = path_from_config(config, "state_json")
    alert_state_path = alert_state_json_from_config(config, fallback_state_json=state_path)
    items = load_items_py(monitor_items_py)
    if not items:
        raise RuntimeError(f"no monitor items in {monitor_items_py}")
    failover_cfg = load_failover_config(config, root)
    if failover_cfg.enabled:
        maybe_import_failover_runtime(root=root, config=config, quiet=True)

    integrity_report = ensure_monitor_runtime_integrity(config, root)
    for action in integrity_report.actions:
        print(f"runtime integrity: {action}")
    if integrity_report.warnings:
        for warning in integrity_report.warnings:
            print(f"runtime integrity warning: {warning}", file=sys.stderr, flush=True)
        raise RuntimeError("monitor runtime integrity check failed")

    use_tiers = False
    queue_pattern: list[str] = []
    tier_item_paths = tier_item_paths_from_config(config)
    tier_state_paths = tier_state_paths_from_config(config)
    tier_items: dict[str, list[str]] = {}
    tier_batch_sizes: dict[str, int] = {}
    default_max_listings = int(
        steam_scm_cfg.get("max_listings_per_item", monitoring_cfg.get("max_listings_per_item", 200))
    )
    tier_listing_caps: dict[str, int] = {}
    if tier_mode_enabled(config):
        tier_items = load_tier_items(tier_item_paths)
        if any(tier_items.values()):
            if tier_items_match_full_list(items, tier_items):
                use_tiers = True
                queue_pattern = queue_pattern_from_config(config)
                tier_batch_sizes = batch_sizes_from_config(config, default_batch_size=batch_size)
                tier_listing_caps = listing_caps_from_config(config, default_max_listings=default_max_listings)
            else:
                print("warning: tier files do not match monitor_list_latest.py; falling back to single-list mode")
        else:
            print("warning: tiered monitoring enabled but tier item files are missing/empty; falling back to single-list mode")

    active, now_local = is_active_now(schedule_cfg)
    print(f"config: {config_path}")
    print(
        "cycle schedule: "
        f"{schedule_cfg.get('active_from')}..{schedule_cfg.get('active_to')} "
        f"{schedule_cfg.get('timezone')} "
        f"(github cron UTC: {schedule_cfg.get('github_actions_cron_utc')})"
    )
    print(f"local schedule time: {now_local.isoformat(timespec='seconds')} active={active}")
    print(f"monitor items: {len(items)} from {monitor_items_py}")
    if use_tiers:
        print(
            "tiered monitoring: on "
            f"queue={queue_pattern} "
            f"A={len(tier_items.get('A', []))}/b{tier_batch_sizes['A']}/d{tier_listing_caps['A']} "
            f"B={len(tier_items.get('B', []))}/b{tier_batch_sizes['B']}/d{tier_listing_caps['B']} "
            f"C={len(tier_items.get('C', []))}/b{tier_batch_sizes['C']}/d{tier_listing_caps['C']}"
        )
        print("cycle sleep after queue rounds: skipped in tiered mode")
    else:
        print(f"batch size: {batch_size}")
        print(f"max listings per item: {default_max_listings}")
        print(f"cycle sleep after full list: {cycle_sleep_sec:.1f}s")
    print(f"telegram mode: {telegram_mode}")
    print(f"batch sleep: {batch_sleep_min_sec:.1f}..{batch_sleep_max_sec:.1f}s")
    print(f"runtime checkpoint: {'on' if commit_enabled else 'off'} every {commit_every_batches} batches")
    print(f"max runtime: {max_runtime_minutes:.1f} minutes")
    print(f"dry run: {'on' if args.dry_run else 'off'}")

    if respect_active_window and not active:
        print("outside active window; cycle will not start")
        return 0
    if args.dry_run:
        return 0

    started = time.monotonic()
    batches_run = 0
    cycles_done = 0
    batches_since_commit = 0
    failover_request_active = False

    if failover_cfg.enabled and failover_cfg.push_on_cycle_start:
        maybe_sync_failover(
            root=root,
            config_path=config_path,
            config=config,
            mode="standby",
            reason="main monitoring cycle started",
            lease_seconds=failover_cfg.lease_seconds,
            state_path=state_path,
            monitor_items_py=monitor_items_py,
            batch_pointer=(
                int(load_scheduler_state(state_path, items, tier_items, queue_pattern).get("queue_pointer") or 0)
                if use_tiers
                else int(load_state(state_path, items).get("batch_pointer") or 0)
            ),
        )

    partial_rate_limit_min_item_errors = max(
        1,
        int(cycle_cfg.get("partial_rate_limit_failover_min_item_errors", 2)),
    )

    while True:
        elapsed_minutes = (time.monotonic() - started) / 60.0
        if elapsed_minutes >= max_runtime_minutes:
            print(f"max runtime reached before next batch: {elapsed_minutes:.1f}m")
            break
        if max_batches is not None and batches_run >= max_batches:
            print(f"max batches reached: {batches_run}")
            break
        if max_cycles is not None and cycles_done >= max_cycles:
            print(f"max cycles reached: {cycles_done}")
            break
        if respect_active_window:
            active, now_local = is_active_now(schedule_cfg)
            if not active:
                print(f"outside active window before next batch: {now_local.isoformat(timespec='seconds')}")
                break

        selected_tier = None
        queue_index = None
        next_queue_pointer = None
        run_items = items
        run_items_path = monitor_items_py
        run_state_path = state_path
        effective_batch_size = batch_size
        effective_max_listings = default_max_listings
        cycle_boundary_done = False

        if use_tiers:
            scheduler_before = load_scheduler_state(state_path, items, tier_items, queue_pattern)
            selected_tier, queue_index, next_queue_pointer = select_next_tier(
                scheduler_before,
                queue_pattern,
                tier_items,
            )
            run_items = tier_items[selected_tier]
            run_items_path = tier_item_paths[selected_tier]
            run_state_path = tier_state_paths[selected_tier]
            effective_batch_size = tier_batch_sizes[selected_tier]
            effective_max_listings = tier_listing_caps[selected_tier]
            tier_before = load_state(run_state_path, run_items)
            batch_preview, start_pointer, _ = select_batch(run_items, tier_before, effective_batch_size)
            scheduler_before = mark_scheduler_run_started(
                scheduler_before,
                tier=selected_tier,
                queue_index=queue_index,
                batch_items=batch_preview,
                tier_start_pointer=start_pointer,
            )
            save_scheduler_state(state_path, scheduler_before)
            print(
                f"\n=== monitoring cycle batch {batches_run + 1} "
                f"tier={selected_tier} queue_index={queue_index} "
                f"start_pointer={start_pointer} batch_size={effective_batch_size} "
                f"max_listings={effective_max_listings} ===",
                flush=True,
            )
        else:
            before = load_state(run_state_path, run_items)
            start_pointer = int(before.get("batch_pointer") or 0) % len(run_items)
            print(
                f"\n=== monitoring cycle batch {batches_run + 1} "
                f"start_pointer={start_pointer} max_listings={effective_max_listings} ===",
                flush=True,
            )

        result = run_cmd(
            batch_command(
                root,
                config_path,
                effective_batch_size,
                telegram_mode,
                monitor_items_py=run_items_path,
                state_json=run_state_path,
                alert_state_json=alert_state_path,
                max_listings_per_item=effective_max_listings,
                tier=str(selected_tier) if selected_tier else None,
            ),
            root,
            check=False,
        )
        after = load_state(run_state_path, run_items)
        next_pointer = int(after.get("batch_pointer") or 0) % len(run_items)
        if use_tiers:
            scheduler_after = mark_scheduler_run_finished(
                load_scheduler_state(state_path, items, tier_items, queue_pattern),
                tier=str(selected_tier),
                queue_index=int(queue_index),
                next_queue_pointer=int(next_queue_pointer),
                status="ok" if result.returncode == 0 else "error",
                tier_state=after,
                error=str(after.get("last_error") or "") or None,
            )
            save_scheduler_state(state_path, scheduler_after)
            cycle_boundary_done = bool(next_queue_pointer is not None and queue_index is not None and next_queue_pointer <= queue_index)
        else:
            cycle_boundary_done = effective_batch_size >= len(run_items) or next_pointer <= start_pointer
        batches_run += 1
        batches_since_commit += 1

        if result.returncode != 0:
            if is_recoverable_batch_error(after, cycle_cfg):
                remaining_runtime_sec = max(0.0, max_runtime_minutes * 60.0 - (time.monotonic() - started))
                if failover_cfg.enabled and failover_cfg.request_on_rate_limit and is_rate_limit_batch_error(after, cycle_cfg):
                    failover_lease_seconds = rate_limit_failover_lease_seconds(
                        schedule_cfg=schedule_cfg,
                        failover_cfg=failover_cfg,
                        recoverable_error_sleep_sec=recoverable_error_sleep_sec,
                    )
                    maybe_sync_failover(
                        root=root,
                        config_path=config_path,
                        config=config,
                        mode="request",
                        reason=(
                            f"[tier {selected_tier}] {after.get('last_error')}"
                            if use_tiers and selected_tier
                            else str(after.get("last_error") or "")
                        ),
                        lease_seconds=failover_lease_seconds,
                        state_path=state_path,
                        monitor_items_py=monitor_items_py,
                        batch_pointer=int(queue_index if use_tiers and queue_index is not None else start_pointer),
                    )
                    failover_request_active = True
                if remaining_runtime_sec <= recoverable_error_sleep_sec:
                    print(
                        "recoverable batch failure near runtime deadline; "
                        f"remaining={remaining_runtime_sec:.1f}s sleep={recoverable_error_sleep_sec:.1f}s; "
                        "stopping now to release lock",
                        file=sys.stderr,
                    )
                    if commit_enabled and batches_since_commit > 0:
                        commit_runtime(root, checkpoint_message)
                        batches_since_commit = 0
                    break
                print(
                    "recoverable batch failure; "
                    f"sleeping {recoverable_error_sleep_sec:.1f}s before retry: {after.get('last_error')}",
                    file=sys.stderr,
                )
                if commit_enabled and batches_since_commit > 0:
                    commit_runtime(root, checkpoint_message)
                    batches_since_commit = 0
                time.sleep(max(0.0, recoverable_error_sleep_sec))
                if failover_request_active:
                    maybe_import_failover_runtime(root=root, config=config, quiet=False)
                continue
            print(f"batch failed with exit {result.returncode}", file=sys.stderr)
            if commit_enabled:
                commit_runtime(root, checkpoint_message)
            return result.returncode

        partial_rate_limited_items = partial_rate_limit_listing_errors(after, cycle_cfg)
        if (
            failover_cfg.enabled
            and failover_cfg.request_on_rate_limit
            and len(partial_rate_limited_items) >= partial_rate_limit_min_item_errors
        ):
            remaining_runtime_sec = max(0.0, max_runtime_minutes * 60.0 - (time.monotonic() - started))
            failover_lease_seconds = rate_limit_failover_lease_seconds(
                schedule_cfg=schedule_cfg,
                failover_cfg=failover_cfg,
                recoverable_error_sleep_sec=recoverable_error_sleep_sec,
            )
            sample = ", ".join(partial_rate_limited_items[:3])
            reason = (
                f"partial Steam rate limit (429): {len(partial_rate_limited_items)} item errors "
                f"in successful batch "
                f"{'(tier ' + str(selected_tier) + ') ' if use_tiers and selected_tier else ''}"
                f"starting at pointer {start_pointer} ({sample})"
            )
            maybe_sync_failover(
                root=root,
                config_path=config_path,
                config=config,
                mode="request",
                reason=reason,
                lease_seconds=failover_lease_seconds,
                state_path=state_path,
                monitor_items_py=monitor_items_py,
                batch_pointer=int(queue_index if use_tiers and queue_index is not None else start_pointer),
            )
            failover_request_active = True
            if remaining_runtime_sec <= recoverable_error_sleep_sec:
                print(
                    "partial rate-limit batch near runtime deadline; "
                    f"remaining={remaining_runtime_sec:.1f}s sleep={recoverable_error_sleep_sec:.1f}s; "
                    "stopping now to release lock",
                    file=sys.stderr,
                )
                if commit_enabled and batches_since_commit > 0:
                    commit_runtime(root, checkpoint_message)
                    batches_since_commit = 0
                break
            print(
                "partial rate-limit batch; "
                f"sleeping {recoverable_error_sleep_sec:.1f}s before retry/failover handoff: {reason}",
                file=sys.stderr,
            )
            if commit_enabled and batches_since_commit > 0:
                commit_runtime(root, checkpoint_message)
                batches_since_commit = 0
            time.sleep(max(0.0, recoverable_error_sleep_sec))
            maybe_import_failover_runtime(root=root, config=config, quiet=False)
            continue

        if cycle_boundary_done:
            cycles_done += 1
            if use_tiers:
                print(f"tier queue round completed: cycles_done={cycles_done}")
            else:
                print(f"full monitor-list cycle completed: cycles_done={cycles_done}")

        if failover_request_active:
            maybe_sync_failover(
                root=root,
                config_path=config_path,
                config=config,
                mode="clear",
                reason="main monitoring recovered after successful batch",
                lease_seconds=failover_cfg.lease_seconds,
                state_path=state_path,
                monitor_items_py=monitor_items_py,
                batch_pointer=int(next_queue_pointer if use_tiers and next_queue_pointer is not None else next_pointer),
            )
            failover_request_active = False

        checkpoint_due = commit_enabled and (
            batches_since_commit >= max(1, commit_every_batches) or cycle_boundary_done
        )
        if checkpoint_due:
            commit_runtime(root, checkpoint_message)
            batches_since_commit = 0

        should_batch_sleep = batch_sleep_max_sec > 0 and not (cycle_boundary_done and not use_tiers)
        if should_batch_sleep:
            sleep_sec = random.uniform(batch_sleep_min_sec, batch_sleep_max_sec)
            remaining_runtime_sec = max(0.0, max_runtime_minutes * 60.0 - (time.monotonic() - started))
            if remaining_runtime_sec <= sleep_sec:
                print(
                    "not sleeping between batches because max runtime is near: "
                    f"remaining={remaining_runtime_sec:.1f}s sleep={sleep_sec:.1f}s"
                )
                break
            print(f"sleeping between batches: {sleep_sec:.1f}s")
            time.sleep(sleep_sec)

        if cycle_boundary_done:
            if max_cycles is not None and cycles_done >= max_cycles:
                print(f"max cycles reached after completed cycle: {cycles_done}")
                break
            if use_tiers:
                continue
            elapsed_minutes = (time.monotonic() - started) / 60.0
            if elapsed_minutes + (cycle_sleep_sec / 60.0) >= max_runtime_minutes:
                print("not sleeping after full cycle because max runtime is near")
                break
            if respect_active_window:
                active, now_local = is_active_now(schedule_cfg)
                if not active:
                    print(f"outside active window after completed cycle: {now_local.isoformat(timespec='seconds')}")
                    break
            print(f"sleeping after full cycle: {cycle_sleep_sec:.1f}s")
            time.sleep(max(0.0, cycle_sleep_sec))

    if commit_enabled and batches_since_commit > 0:
        commit_runtime(root, checkpoint_message)

    print(f"monitoring cycle completed: batches={batches_run}, full_cycles={cycles_done}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
