"""Run the standalone CSFloat base snapshot job."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from automation.risk_filters import repo_root_from


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def parse_args() -> argparse.Namespace:
    root = repo_root_from(Path(__file__))
    parser = argparse.ArgumentParser(description="Run standalone CSFloat base snapshot collection.")
    parser.add_argument(
        "--config",
        type=Path,
        default=root / "automation" / "configs" / "base.json",
        help="Base snapshot JSON config.",
    )
    parser.add_argument("--delay-min-sec", type=float, default=None, help="Override delay minimum.")
    parser.add_argument("--delay-max-sec", type=float, default=None, help="Override delay maximum.")
    parser.add_argument(
        "--respect-schedule",
        action="store_true",
        help="Run only when the current local hour is allowed by config schedule.run_hours_local.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print the command without running it.")
    return parser.parse_args()


def should_run_now(config_path: Path) -> tuple[bool, str]:
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{config_path} must contain a JSON object")
    schedule = raw.get("schedule", {})
    if not isinstance(schedule, dict):
        schedule = {}

    if not bool(schedule.get("enabled", True)):
        return False, "base schedule disabled"

    tz = ZoneInfo(str(schedule.get("timezone", "Europe/Prague")))
    now = datetime.now(tz)
    allowed_hours = schedule.get("run_hours_local")
    if allowed_hours is None:
        return True, f"no run_hours_local configured; running at {now.isoformat(timespec='seconds')}"
    allowed = {int(hour) for hour in allowed_hours}
    if now.hour in allowed:
        return True, f"local hour {now.hour:02d} is allowed ({sorted(allowed)})"
    return False, f"local hour {now.hour:02d} is not in allowed hours {sorted(allowed)}"


def main() -> int:
    configure_stdio()
    args = parse_args()
    root = repo_root_from(Path(__file__))
    config_path = args.config.resolve()
    if args.respect_schedule:
        ok, reason = should_run_now(config_path)
        print(f"base schedule gate: {reason}")
        if not ok:
            return 0

    cmd = [
        sys.executable,
        "-B",
        str(root / "automation" / "nightly" / "build_base_snapshot.py"),
        "--config",
        str(config_path),
    ]
    if args.delay_min_sec is not None:
        cmd.extend(["--delay-min-sec", str(args.delay_min_sec)])
    if args.delay_max_sec is not None:
        cmd.extend(["--delay-max-sec", str(args.delay_max_sec)])

    print("base snapshot command:")
    print(" ".join(cmd))
    if args.dry_run:
        return 0
    return subprocess.run(cmd, cwd=str(root)).returncode


if __name__ == "__main__":
    raise SystemExit(main())
