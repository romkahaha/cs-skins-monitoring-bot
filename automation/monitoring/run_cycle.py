"""Run monitoring batches continuously inside one GitHub Actions job."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from automation.config import load_json_config, monitoring_defaults, path_from_config
from automation.listing_enrichment import load_items_py
from automation.risk_filters import repo_root_from
from automation.state import load_state

RECOVERABLE_BATCH_ERROR_PATTERNS = (
    "all Steam listing fetches failed for this batch",
    "Steam listings fetch returned 0 rows with",
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


def batch_command(root: Path, config_path: Path, batch_size: int, telegram_mode: str) -> list[str]:
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


def main() -> int:
    configure_stdio()
    args = parse_args()
    root = repo_root_from(Path(__file__))
    config_path = args.config.resolve()
    config = load_json_config(config_path, monitoring_defaults())
    schedule_cfg = config.get("schedule", {})
    monitoring_cfg = config.get("monitoring", {})
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
    items = load_items_py(monitor_items_py)
    if not items:
        raise RuntimeError(f"no monitor items in {monitor_items_py}")

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
    print(f"batch size: {batch_size}")
    print(f"telegram mode: {telegram_mode}")
    print(f"cycle sleep after full list: {cycle_sleep_sec:.1f}s")
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

        before = load_state(state_path, items)
        start_pointer = int(before.get("batch_pointer") or 0) % len(items)
        print(f"\n=== monitoring cycle batch {batches_run + 1} start_pointer={start_pointer} ===", flush=True)
        result = run_cmd(batch_command(root, config_path, batch_size, telegram_mode), root, check=False)
        after = load_state(state_path, items)
        next_pointer = int(after.get("batch_pointer") or 0) % len(items)
        batches_run += 1
        batches_since_commit += 1

        if result.returncode != 0:
            if is_recoverable_batch_error(after, cycle_cfg):
                print(
                    "recoverable batch failure; "
                    f"sleeping {recoverable_error_sleep_sec:.1f}s before retry: {after.get('last_error')}",
                    file=sys.stderr,
                )
                if commit_enabled and batches_since_commit > 0:
                    commit_runtime(root, checkpoint_message)
                    batches_since_commit = 0
                time.sleep(max(0.0, recoverable_error_sleep_sec))
                continue
            print(f"batch failed with exit {result.returncode}", file=sys.stderr)
            if commit_enabled:
                commit_runtime(root, checkpoint_message)
            return result.returncode

        full_cycle_done = batch_size >= len(items) or next_pointer <= start_pointer
        if full_cycle_done:
            cycles_done += 1
            print(f"full monitor-list cycle completed: cycles_done={cycles_done}")

        checkpoint_due = commit_enabled and (
            batches_since_commit >= max(1, commit_every_batches) or full_cycle_done
        )
        if checkpoint_due:
            commit_runtime(root, checkpoint_message)
            batches_since_commit = 0

        if full_cycle_done:
            if max_cycles is not None and cycles_done >= max_cycles:
                print(f"max cycles reached after completed cycle: {cycles_done}")
                break
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
