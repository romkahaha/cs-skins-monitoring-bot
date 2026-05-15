"""Validate Steam cookies and alert Telegram when they stop working."""

from __future__ import annotations

import argparse
import html
import os
import sys
from pathlib import Path
from typing import Any

import requests

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from automation.config import health_defaults, load_json_config
from automation.risk_filters import repo_root_from
from automation.telegram_alerts import send_message, telegram_credentials


DEFAULT_BAD_COOKIES = "sessionid=codex-bad-cookie-test; steamLoginSecure=codex-bad-cookie-test"


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
    spec = importlib.util.spec_from_file_location("health_steam_scm_listings", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot import {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_fetchers(repo_root: Path):
    fetchers_dir = repo_root / "base_screening_with_trades"
    fetchers_dir_str = str(fetchers_dir)
    if fetchers_dir_str not in sys.path:
        sys.path.insert(0, fetchers_dir_str)

    import importlib.util

    module_path = fetchers_dir / "fetchers.py"
    spec = importlib.util.spec_from_file_location("health_fetchers", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot import {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def parse_args() -> argparse.Namespace:
    root = repo_root_from(Path(__file__))
    parser = argparse.ArgumentParser(description="Check Steam cookies and alert Telegram on failure.")
    parser.add_argument(
        "--config",
        type=Path,
        default=root / "automation" / "configs" / "health.json",
        help="Health automation JSON config.",
    )
    parser.add_argument("--steam-cookies", default=None, help="Override STEAM_COOKIES for this check.")
    parser.add_argument(
        "--force-bad-cookies",
        action="store_true",
        help="Use intentionally invalid cookies to test the alert path.",
    )
    parser.add_argument(
        "--force-failure-for-test",
        action="store_true",
        help="Skip network checks and force a health-check failure for Telegram testing.",
    )
    parser.add_argument("--dry-run-telegram", action="store_true", help="Print Telegram alert instead of sending it.")
    parser.add_argument(
        "--exit-zero-on-failure",
        action="store_true",
        help="For local smoke tests only: return 0 even when the health check intentionally fails.",
    )
    return parser.parse_args()


def effective_cookies(args: argparse.Namespace) -> str:
    if args.force_bad_cookies:
        return DEFAULT_BAD_COOKIES
    if args.steam_cookies is not None:
        return str(args.steam_cookies)
    return os.environ.get("STEAM_COOKIES", "")


def build_session(cookies: str, timeout_sec: float) -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        }
    )
    session.request_timeout_sec = timeout_sec  # type: ignore[attr-defined]
    if cookies.strip():
        session.headers["Cookie"] = cookies.strip()
    return session


def validate_steam_login(cookies: str, cfg: dict[str, Any]) -> tuple[bool, str]:
    if not cookies.strip() and cfg.get("fail_if_missing_cookies", True):
        return False, "STEAM_COOKIES is empty or missing"

    if not cfg.get("check_login_endpoint", True):
        return True, "login endpoint check disabled"

    timeout_sec = float(cfg.get("request_timeout_sec", 45.0))
    session = build_session(cookies, timeout_sec)
    url = str(cfg.get("login_check_url", "https://steamcommunity.com/my/"))
    try:
        response = session.get(url, timeout=timeout_sec, allow_redirects=True)
    except requests.RequestException as exc:
        return False, f"Steam login check request failed: {exc}"

    final_url = str(response.url or "").lower()
    text_head = response.text[:8000].lower()
    if response.status_code >= 400:
        return False, f"Steam login check returned HTTP {response.status_code}"
    if "/login" in final_url or "login/home" in final_url:
        return False, f"Steam redirected to login page: {response.url}"
    if "sign in" in text_head and "steamcommunity" in final_url:
        return False, "Steam login page detected in response body"

    return True, f"Steam login endpoint accepted cookies: {response.url}"


def apply_steam_scm_config(module: Any, cfg: dict[str, Any], cookies: str) -> None:
    module_cfg = getattr(module, "CONFIG", None)
    if not isinstance(module_cfg, dict):
        return

    module_cfg["steam_cookies"] = cookies
    direct_keys = [
        "listings_per_request",
        "request_timeout_sec",
        "retry_attempts",
        "retry_sleep_min_sec",
        "retry_sleep_max_sec",
        "delay_between_render_pages_min_sec",
        "delay_between_render_pages_max_sec",
        "batch_log_progress",
    ]
    for key in direct_keys:
        if key in cfg and cfg[key] is not None:
            module_cfg[key] = cfg[key]


def validate_listing_fetch(repo_root: Path, cookies: str, cfg: dict[str, Any]) -> tuple[bool, str, int, dict[str, Any]]:
    if not cfg.get("check_listing_endpoint", True):
        return True, "listing endpoint check disabled", 0, {}

    module = _load_steam_scm_listings(repo_root)
    apply_steam_scm_config(module, cfg, cookies)
    rows, meta = module.fetch_steam_scm_top_listings(
        str(cfg.get("item", "Dreams & Nightmares Case")),
        limit=int(cfg.get("limit", 10)),
        max_listings=int(cfg.get("max_listings", 10)),
        currency=int(cfg.get("currency", 3)),
        retry_attempts=int(cfg.get("retry_attempts", 2)),
        retry_sleep_min_sec=float(cfg.get("retry_sleep_min_sec", 2.0)),
        retry_sleep_max_sec=float(cfg.get("retry_sleep_max_sec", 5.0)),
        log_skin_label="steam-cookie-health-check",
    )
    min_rows = int(cfg.get("min_rows", 1))
    if not bool(meta.get("success")):
        return False, f"Steam listing fetch did not report success: {meta.get('note')}", len(rows), meta
    if len(rows) < min_rows:
        return False, f"Steam listing fetch returned {len(rows)} rows, expected at least {min_rows}", len(rows), meta
    return True, "Steam listing endpoint returned listings", len(rows), meta


def validate_pricehistory_fetch(repo_root: Path, cookies: str, cfg: dict[str, Any]) -> tuple[bool, str, int, dict[str, Any]]:
    if not cfg.get("check_pricehistory_endpoint", True):
        return True, "pricehistory endpoint check disabled", 0, {}

    module = _load_fetchers(repo_root)
    module.STEAM_COOKIES = cookies
    os.environ["STEAM_COOKIES"] = cookies
    if "request_timeout_sec" in cfg and cfg["request_timeout_sec"] is not None:
        module.STEAM_429_RETRY_WAIT_SEC = float(cfg.get("steam_429_retry_wait_sec", module.STEAM_429_RETRY_WAIT_SEC))
    item = str(cfg.get("item", "Dreams & Nightmares Case"))
    days = int(cfg.get("pricehistory_days", 14))
    currency = int(cfg.get("currency", 3))
    points = module.get_scm_trade_points(item, currency=currency, days=days)
    rows = len(points or [])
    meta = {"success": bool(points), "points": rows, "days": days}
    min_points = int(cfg.get("min_pricehistory_points", 1))
    if not points:
        return False, "Steam pricehistory returned no trade points", rows, meta
    if rows < min_points:
        return False, f"Steam pricehistory returned {rows} points, expected at least {min_points}", rows, meta
    return True, f"Steam pricehistory returned {rows} trade points", rows, meta


def format_failure_alert(*, item: str, reason: str, rows: int | None, meta: dict[str, Any] | None, checked_endpoint: str) -> str:
    lines = [
        "<b>Steam cookies health check failed</b>",
        f"Item: <code>{html.escape(item)}</code>",
        f"Checked endpoint: <code>{html.escape(checked_endpoint)}</code>",
        f"Reason: <code>{html.escape(reason)}</code>",
    ]
    if rows is not None:
        lines.append(f"Observed rows: <code>{rows}</code>")
    if meta:
        note = meta.get("note")
        total_count = meta.get("total_count")
        pages = meta.get("pages_fetched")
        points = meta.get("points")
        days = meta.get("days")
        if note is not None:
            lines.append(f"Steam note: <code>{html.escape(str(note))}</code>")
        if total_count is not None:
            lines.append(f"Steam total_count: <code>{html.escape(str(total_count))}</code>")
        if pages is not None:
            lines.append(f"Pages fetched: <code>{html.escape(str(pages))}</code>")
        if points is not None:
            lines.append(f"Trade points: <code>{html.escape(str(points))}</code>")
        if days is not None:
            lines.append(f"Trade days: <code>{html.escape(str(days))}</code>")
    lines.append("Action: update <code>STEAM_COOKIES</code> in <code>/home/roma/cs-arbitrage/secrets.env</code>.")
    return "\n".join(lines)


def notify_failure(message: str, cfg: dict[str, Any], *, dry_run: bool) -> None:
    telegram_cfg = cfg.get("telegram", {})
    if not telegram_cfg.get("enabled", True):
        print("telegram disabled; alert text follows")
        print(message)
        return
    if dry_run:
        print("telegram dry run; alert text follows")
        print(message)
        return
    token, chat = telegram_credentials()
    send_message(
        message,
        bot_token=token,
        chat_id=chat,
        timeout=int(float(telegram_cfg.get("timeout_sec", 120))),
    )


def run_check(args: argparse.Namespace) -> int:
    repo_root = repo_root_from(Path(__file__))
    cfg = load_json_config(args.config, health_defaults())
    steam_cfg = cfg.get("steam", {})
    cookies = effective_cookies(args)
    item = str(steam_cfg.get("item", "Dreams & Nightmares Case"))

    print(f"steam cookie health check: item={item!r}, cookies_present={bool(cookies.strip())}")

    failed_reason: str | None = None
    checked_endpoint = "pricehistory"
    rows: int | None = None
    meta: dict[str, Any] | None = None

    if args.force_failure_for_test:
        failed_reason = "forced failure for Telegram alert test"
        rows = 0
        meta = {"success": False, "note": "forced_failure_for_test"}
    else:
        if bool(steam_cfg.get("check_login_endpoint", False)):
            checked_endpoint = "login"
            ok, reason = validate_steam_login(cookies, steam_cfg)
            print(f"login check: {reason}")
            if not ok:
                failed_reason = reason
        if failed_reason is None and bool(steam_cfg.get("check_listing_endpoint", False)):
            checked_endpoint = "listing"
            ok, reason, rows, meta = validate_listing_fetch(repo_root, cookies, steam_cfg)
            print(f"listing check: {reason}; rows={rows}; meta_success={meta.get('success') if meta else None}")
            if not ok:
                failed_reason = reason
        if failed_reason is None and bool(steam_cfg.get("check_pricehistory_endpoint", True)):
            checked_endpoint = "pricehistory"
            ok, reason, rows, meta = validate_pricehistory_fetch(repo_root, cookies, steam_cfg)
            print(f"pricehistory check: {reason}; rows={rows}; meta_success={meta.get('success') if meta else None}")
            if not ok:
                failed_reason = reason

    if failed_reason is None:
        print("steam cookie health check ok")
        return 0

    alert = format_failure_alert(
        item=item,
        reason=failed_reason,
        rows=rows,
        meta=meta,
        checked_endpoint=checked_endpoint,
    )
    notify_failure(alert, cfg, dry_run=args.dry_run_telegram)
    print(f"steam cookie health check failed: {failed_reason}")
    return 0 if args.exit_zero_on_failure else 1


def main() -> None:
    configure_stdio()
    args = parse_args()
    raise SystemExit(run_check(args))


if __name__ == "__main__":
    main()
