"""Send non-fatal Telegram status messages for nightly automation."""

from __future__ import annotations

import argparse
import html
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from automation.telegram_alerts import send_message, telegram_credentials


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Send a nightly status notification to Telegram.")
    parser.add_argument("--title", required=True)
    parser.add_argument("--status", choices=["ok", "warning", "error", "info"], default="info")
    parser.add_argument("--message", required=True)
    parser.add_argument("--timeout", type=int, default=20)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    prefix = {
        "ok": "[OK]",
        "warning": "[WARN]",
        "error": "[FAIL]",
        "info": "[INFO]",
    }[args.status]
    text = (
        f"<b>{html.escape(prefix)} {html.escape(args.title)}</b>\n"
        f"{html.escape(args.message)}"
    )

    if args.dry_run:
        print(text)
        return 0

    bot_token, chat_id = telegram_credentials()
    if not bot_token or not chat_id:
        print("[notify_status] Telegram credentials are missing; notification skipped", file=sys.stderr)
        return 0
    try:
        send_message(text, bot_token=bot_token, chat_id=chat_id, timeout=args.timeout)
    except Exception as exc:
        print(f"[notify_status] Telegram notification failed: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
