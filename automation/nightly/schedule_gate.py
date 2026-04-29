"""GitHub Actions schedule gate helpers."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Emit GitHub output flags for scheduled automation gates.")
    parser.add_argument("--mode", choices=["nightly"], required=True)
    parser.add_argument("--timezone", default="Europe/Prague")
    parser.add_argument("--state-json", type=Path, default=Path("automation_runtime/nightly_schedule_state.json"))
    parser.add_argument("--start-hour", type=int, default=0)
    parser.add_argument("--end-hour", type=int, default=5)
    return parser.parse_args()


def load_last_success_date(path: Path) -> str | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("last_success_local_date")
    except Exception:
        return None


def main() -> int:
    args = parse_args()
    now = datetime.now(ZoneInfo(args.timezone))
    last_success = load_last_success_date(args.state_json)

    allowed_window = args.start_hour <= now.hour <= args.end_hour
    already_done = last_success == now.date().isoformat()
    run_task = allowed_window and not already_done

    output_name = "run_nightly" if args.mode == "nightly" else "run_task"
    print(f"{output_name}={'true' if run_task else 'false'}")
    print(f"local_time={now.isoformat()}")
    print(f"allowed_window={'true' if allowed_window else 'false'}")
    print(f"already_done={'true' if already_done else 'false'}")
    print(f"last_success_local_date={last_success or ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
