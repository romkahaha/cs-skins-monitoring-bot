"""Replay queued opportunity snapshots through the Telegram sender."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Replay queued opportunity snapshots to Telegram.")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--items-py", type=Path, required=True)
    parser.add_argument("--queue-dir", type=Path, required=True)
    parser.add_argument("--state-json", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    args.state_json.write_text(json.dumps({"version": 1, "items_signature": "", "items_count": 0}) + "\n", encoding="utf-8")
    py = sys.executable
    sender = _REPO_ROOT / "automation" / "monitoring" / "send_telegram_alerts.py"

    for csv_path in sorted(args.queue_dir.glob("*.csv")):
        df = pd.read_csv(csv_path, low_memory=False)
        if len(df) <= 0:
            continue
        print(f"REPLAY {csv_path.name} rows={len(df)}", flush=True)
        subprocess.run(
            [
                py,
                "-B",
                str(sender),
                "--config",
                str(args.config),
                "--opportunities-csv",
                str(csv_path),
                "--state-json",
                str(args.state_json),
                "--monitor-items-py",
                str(args.items_py),
            ],
            check=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
