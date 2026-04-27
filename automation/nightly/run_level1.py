"""Run the level-1 nightly automation sequence.

By default this uses the latest existing risk_metrics CSV, rebuilds the monitor
list, then refreshes the CSFloat base snapshot. The Steam risk refresh is routed
through automation/nightly/build_risk_metrics.py so runtime outputs stay under
automation_runtime/.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from automation.config import load_json_config, nightly_defaults, path_from_config
from automation.listing_enrichment import load_items_py
from automation.risk_filters import repo_root_from


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def parse_args() -> argparse.Namespace:
    root = repo_root_from(Path(__file__))
    parser = argparse.ArgumentParser(description="Run nightly level-1 automation in sequence.")
    parser.add_argument(
        "--config",
        type=Path,
        default=root / "automation" / "configs" / "nightly.json",
        help="Nightly automation JSON config.",
    )
    parser.add_argument("--run-risk", action="store_true", help="Force the heavy risk rebuild for this run.")
    parser.add_argument("--skip-risk", action="store_true", help="Skip the heavy risk rebuild for this run.")
    parser.add_argument("--skip-base", action="store_true", help="Skip CSFloat base snapshot refresh.")
    parser.add_argument("--dry-run", action="store_true", help="Print the sequence without running steps.")
    return parser.parse_args()


def run_step(name: str, cmd: list[str], cwd: Path, *, dry_run: bool = False) -> None:
    print(f"\n=== {name} ===")
    print(" ".join(cmd))
    if dry_run:
        return
    subprocess.run(cmd, cwd=str(cwd), check=True)


def file_age_hours(path: Path) -> float:
    modified = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    return (datetime.now(timezone.utc) - modified).total_seconds() / 3600.0


def require_file(path: Path, label: str) -> None:
    if not path.is_file():
        raise FileNotFoundError(f"{label} not found: {path}")


def require_dir(path: Path, label: str) -> None:
    if not path.is_dir():
        raise FileNotFoundError(f"{label} not found: {path}")


def run_preflight(config: dict, *, risk_enabled: bool) -> None:
    preflight = config.get("preflight", {})

    if risk_enabled:
        require_file(path_from_config(config, "risk_script"), "risk script")
        require_file(path_from_config(config, "risk_input_items_py"), "risk input item list")
        require_file(path_from_config(config, "risk_stage1_csv"), "risk stage1 CSV")
    elif preflight.get("require_existing_risk_csv", True):
        risk_csv = path_from_config(config, "risk_csv")
        require_file(risk_csv, "existing risk CSV")
        max_age = preflight.get("max_existing_risk_age_hours")
        if max_age is not None:
            age = file_age_hours(risk_csv)
            if age > float(max_age):
                msg = f"existing risk CSV is stale: {age:.1f}h > {float(max_age):.1f}h ({risk_csv})"
                if preflight.get("fail_on_stale_existing_risk_csv", False):
                    raise RuntimeError(msg)
                print(f"warning: {msg}")

    if preflight.get("require_summary_csv", True):
        require_file(path_from_config(config, "summary_csv"), "high-CV summary CSV")
    if preflight.get("require_skin_data_dir", True):
        require_dir(path_from_config(config, "skin_data_dir"), "high-CV raw panel directory")


def validate_monitor_list(config: dict) -> None:
    monitor_cfg = config.get("monitor_list", {})
    items_path = path_from_config(config, "monitor_items_py")
    items = load_items_py(items_path)
    n_items = len(items)
    expected_min = monitor_cfg.get("expected_min_items")
    expected_max = monitor_cfg.get("expected_max_items")
    fail = bool(monitor_cfg.get("fail_if_outside_expected_range", False))

    print(f"monitor list items: {n_items}")
    if expected_min is None and expected_max is None:
        return

    too_low = expected_min is not None and n_items < int(expected_min)
    too_high = expected_max is not None and n_items > int(expected_max)
    if too_low or too_high:
        bounds = f"{expected_min if expected_min is not None else '-inf'}..{expected_max if expected_max is not None else '+inf'}"
        msg = f"monitor list size outside expected range: {n_items} not in {bounds}"
        if fail:
            raise RuntimeError(msg)
        print(f"warning: {msg}")


def risk_command(config: dict, config_path: Path) -> list[str]:
    risk_cfg = config.get("risk_rebuild", {})
    mode = str(risk_cfg.get("mode", "create")).lower()
    mode_flag = "--create" if mode == "create" else "--merge"
    return [
        sys.executable,
        str(repo_root_from(Path(__file__)) / "automation" / "nightly" / "build_risk_metrics.py"),
        "--config",
        str(config_path),
        mode_flag,
    ]


def main() -> int:
    configure_stdio()
    args = parse_args()
    root = repo_root_from(Path(__file__))
    config_path = args.config.resolve()
    config = load_json_config(config_path, nightly_defaults())

    risk_enabled = bool(config.get("risk_rebuild", {}).get("enabled", False))
    if args.run_risk:
        risk_enabled = True
    if args.skip_risk:
        risk_enabled = False

    base_enabled = bool(config.get("base_snapshot", {}).get("enabled", True)) and not args.skip_base
    monitor_list_enabled = bool(config.get("monitor_list", {}).get("enabled", True))
    schedule = config.get("schedule", {})

    print(f"config: {config_path}")
    print(
        "schedule: "
        f"{schedule.get('intended_start', '<unset>')} {schedule.get('timezone', '<unset>')} "
        f"(github cron UTC: {schedule.get('github_actions_cron_utc', '<unset>')})"
    )
    print(f"risk rebuild: {'on' if risk_enabled else 'off'}")
    print(f"monitor list: {'on' if monitor_list_enabled else 'off'}")
    print(f"base snapshot: {'on' if base_enabled else 'off'}")
    print(f"dry run: {'on' if args.dry_run else 'off'}")
    run_preflight(config, risk_enabled=risk_enabled)

    if risk_enabled:
        run_step("risk rebuild", risk_command(config, config_path), root, dry_run=args.dry_run)
    else:
        risk_csv = path_from_config(config, "risk_csv")
        if not risk_csv.is_file():
            raise FileNotFoundError(f"{risk_csv} not found; enable risk_rebuild or provide an existing risk CSV")
        print(f"using existing risk csv: {risk_csv}")

    run_step(
        "risk candidates",
        [sys.executable, str(root / "automation" / "nightly" / "build_risk_candidates.py"), "--config", str(config_path)],
        root,
        dry_run=args.dry_run,
    )
    run_step(
        "model backfill queue",
        [
            sys.executable,
            str(root / "automation" / "nightly" / "build_model_backfill_queue.py"),
            "--config",
            str(config_path),
        ],
        root,
        dry_run=args.dry_run,
    )

    if monitor_list_enabled:
        run_step(
            "monitor list",
            [sys.executable, str(root / "automation" / "nightly" / "build_monitor_list.py"), "--config", str(config_path)],
            root,
            dry_run=args.dry_run,
        )
        if not args.dry_run:
            validate_monitor_list(config)

    if base_enabled:
        run_step(
            "base snapshot",
            [sys.executable, str(root / "automation" / "nightly" / "build_base_snapshot.py"), "--config", str(config_path)],
            root,
            dry_run=args.dry_run,
        )

    print("\nnightly level-1 completed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
