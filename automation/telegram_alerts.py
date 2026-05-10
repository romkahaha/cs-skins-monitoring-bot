"""Telegram alert formatting and sending for opportunity rows."""

from __future__ import annotations

import html
import os
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import requests

from automation.state import load_state, save_state, utc_now_iso
from automation.listing_enrichment import load_items_py


LEVEL_ICON = {
    "excellent": "🔵",
    "very_good": "🟢",
    "good": "🟡",
    "ok": "🟠",
    "bad": "🔴",
    "awful": "⚫",
    "unknown": "⚪",
}


BANDS: dict[str, dict[str, Any]] = {
    "spread_hybrid_disc": {
        "lower_is_better": True,
        "fmt": "pct",
        "bands": [
            {"label": "excellent", "lo": None, "hi": 0.00},
            {"label": "very_good", "lo": 0.00, "hi": 0.05},
            {"label": "good", "lo": 0.05, "hi": 0.10},
            {"label": "ok", "lo": 0.10, "hi": 0.17},
            {"label": "bad", "lo": 0.17, "hi": 0.25},
            {"label": "awful", "lo": 0.25, "hi": None},
        ],
    },
    "steam_sales_7d_n": {
        "fmt": "int",
        "bands": [
            {"label": "awful", "lo": None, "hi": 20},
            {"label": "bad", "lo": 20, "hi": 50},
            {"label": "ok", "lo": 50, "hi": 100},
            {"label": "good", "lo": 100, "hi": 200},
            {"label": "very_good", "lo": 200, "hi": 400},
            {"label": "excellent", "lo": 400, "hi": None},
        ],
    },
    "steam_sales_7d_downside_risk%": {
        "lower_is_better": True,
        "fmt": "pct_points",
        "bands": [
            {"label": "excellent", "lo": None, "hi": 2.5},
            {"label": "very_good", "lo": 2.5, "hi": 4.0},
            {"label": "good", "lo": 4.0, "hi": 6.0},
            {"label": "ok", "lo": 6.0, "hi": 10.0},
            {"label": "bad", "lo": 10.0, "hi": 12.0},
            {"label": "awful", "lo": 12.0, "hi": None},
        ],
    },
    "steam_sales_7d_tail_ratio": {
        "fmt": "ratio",
        "bands": [
            {"label": "awful", "lo": None, "hi": 0.85},
            {"label": "bad", "lo": 0.85, "hi": 0.90},
            {"label": "ok", "lo": 0.90, "hi": 0.93},
            {"label": "good", "lo": 0.93, "hi": 0.95},
            {"label": "very_good", "lo": 0.95, "hi": 0.97},
            {"label": "excellent", "lo": 0.97, "hi": None},
        ],
    },
    "steam_daily_downside_14d_pct": {
        "lower_is_better": True,
        "fmt": "pct",
        "bands": [
            {"label": "excellent", "lo": None, "hi": 0.02},
            {"label": "very_good", "lo": 0.02, "hi": 0.05},
            {"label": "good", "lo": 0.05, "hi": 0.08},
            {"label": "ok", "lo": 0.08, "hi": 0.12},
            {"label": "bad", "lo": 0.12, "hi": 0.18},
            {"label": "awful", "lo": 0.18, "hi": None},
        ],
    },
    "continuity_ratio": {
        "lower_is_better": True,
        "fmt": "ratio",
        "bands": [
            {"label": "excellent", "lo": None, "hi": 1.5},
            {"label": "very_good", "lo": 1.5, "hi": 1.7},
            {"label": "good", "lo": 1.7, "hi": 2.0},
            {"label": "ok", "lo": 2.0, "hi": 3.5},
            {"label": "bad", "lo": 3.5, "hi": 5.0},
            {"label": "awful", "lo": 5.0, "hi": None},
        ],
    },
    "steam_turnover_proxy": {
        "fmt": "ratio",
        "bands": [
            {"label": "awful", "lo": None, "hi": 0.5},
            {"label": "bad", "lo": 0.5, "hi": 1.0},
            {"label": "ok", "lo": 1.0, "hi": 2.0},
            {"label": "good", "lo": 2.0, "hi": 4.0},
            {"label": "very_good", "lo": 4.0, "hi": 8.0},
            {"label": "excellent", "lo": 8.0, "hi": None},
        ],
    },
    "scm_total_listings": {
        "fmt": "int",
        "bands": [
            {"label": "awful", "lo": None, "hi": 20},
            {"label": "bad", "lo": 20, "hi": 50},
            {"label": "ok", "lo": 50, "hi": 100},
            {"label": "good", "lo": 100, "hi": 200},
            {"label": "very_good", "lo": 200, "hi": 400},
            {"label": "excellent", "lo": 400, "hi": None},
        ],
    },
    "steam_daily_range_14d_pct": {
        "lower_is_better": True,
        "fmt": "pct",
        "bands": [
            {"label": "excellent", "lo": None, "hi": 0.05},
            {"label": "very_good", "lo": 0.05, "hi": 0.08},
            {"label": "good", "lo": 0.08, "hi": 0.12},
            {"label": "ok", "lo": 0.12, "hi": 0.18},
            {"label": "bad", "lo": 0.18, "hi": 0.25},
            {"label": "awful", "lo": 0.25, "hi": None},
        ],
    },
    "steam_daily_ret_3d": {
        "fmt": "pct",
        "bands": [
            {"label": "bad", "lo": None, "hi": -0.05},
            {"label": "ok", "lo": -0.05, "hi": -0.02},
            {"label": "good", "lo": -0.02, "hi": 0.02},
            {"label": "very_good", "lo": 0.02, "hi": 0.05},
            {"label": "excellent", "lo": 0.05, "hi": None},
        ],
    },
    "steam_daily_ret_7d": {
        "fmt": "pct",
        "bands": [
            {"label": "bad", "lo": None, "hi": -0.10},
            {"label": "ok", "lo": -0.10, "hi": -0.03},
            {"label": "good", "lo": -0.03, "hi": 0.03},
            {"label": "very_good", "lo": 0.03, "hi": 0.08},
            {"label": "excellent", "lo": 0.08, "hi": None},
        ],
    },
    "steam_daily_slope_7d": {
        "fmt": "pct",
        "bands": [
            {"label": "bad", "lo": None, "hi": -0.02},
            {"label": "ok", "lo": -0.02, "hi": -0.005},
            {"label": "good", "lo": -0.005, "hi": 0.005},
            {"label": "very_good", "lo": 0.005, "hi": 0.02},
            {"label": "excellent", "lo": 0.02, "hi": None},
        ],
    },
    "steam_daily_ema_gap_3_14": {
        "fmt": "pct",
        "bands": [
            {"label": "bad", "lo": None, "hi": -0.05},
            {"label": "ok", "lo": -0.05, "hi": -0.02},
            {"label": "good", "lo": -0.02, "hi": 0.02},
            {"label": "very_good", "lo": 0.02, "hi": 0.05},
            {"label": "excellent", "lo": 0.05, "hi": None},
        ],
    },
    "steam_sales_7d_iqr_risk%": {
        "lower_is_better": True,
        "fmt": "pct_points",
        "bands": [
            {"label": "excellent", "lo": None, "hi": 5.0},
            {"label": "very_good", "lo": 5.0, "hi": 8.0},
            {"label": "good", "lo": 8.0, "hi": 12.0},
            {"label": "ok", "lo": 12.0, "hi": 18.0},
            {"label": "bad", "lo": 18.0, "hi": 25.0},
            {"label": "awful", "lo": 25.0, "hi": None},
        ],
    },
    "steam_sales_7d_band_risk%": {
        "lower_is_better": True,
        "fmt": "pct_points",
        "bands": [
            {"label": "excellent", "lo": None, "hi": 8.0},
            {"label": "very_good", "lo": 8.0, "hi": 12.0},
            {"label": "good", "lo": 12.0, "hi": 18.0},
            {"label": "ok", "lo": 18.0, "hi": 25.0},
            {"label": "bad", "lo": 25.0, "hi": 35.0},
            {"label": "awful", "lo": 35.0, "hi": None},
        ],
    },
}


THRESHOLDS = {
    "spread_hybrid_disc": "<= 12.00%",
    "steam_sales_7d_n": ">= 50",
    "steam_sales_7d_downside_risk%": "<= 10.00%",
    "steam_sales_7d_tail_ratio": ">= 0.900",
    "steam_daily_downside_14d_pct": "<= 12.00%",
    "continuity_ratio": "<= 3.50",
}


def _as_float(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if pd.isna(out):
        return None
    return out


def _in_band(value: float, lo: float | None, hi: float | None) -> bool:
    left_ok = True if lo is None else value >= float(lo)
    right_ok = True if hi is None else value < float(hi)
    return left_ok and right_ok


def level_for(metric: str, value: Any) -> str:
    val = _as_float(value)
    if val is None:
        return "unknown"
    spec = BANDS.get(metric)
    if spec is None:
        return "unknown"
    for band in spec["bands"]:
        if _in_band(val, band.get("lo"), band.get("hi")):
            return str(band["label"])
    return "unknown"


def icon_for(metric: str, value: Any) -> str:
    return LEVEL_ICON.get(level_for(metric, value), LEVEL_ICON["unknown"])


def fmt_value(metric: str, value: Any) -> str:
    val = _as_float(value)
    if val is None:
        return "-"
    fmt = BANDS.get(metric, {}).get("fmt")
    if fmt == "pct":
        return f"{val:.2%}"
    if fmt == "pct_points":
        return f"{val:.2f}%"
    if fmt == "int":
        return f"{val:.0f}"
    if fmt == "ratio":
        return f"{val:.3f}"
    return f"{val:.4g}"


def fmt_money(value: Any) -> str:
    val = _as_float(value)
    return "-" if val is None else f"€{val:.2f}"


def fmt_float(value: Any) -> str:
    val = _as_float(value)
    return "-" if val is None else f"{val:.6f}"


def fmt_seed(value: Any) -> str:
    val = _as_float(value)
    return "-" if val is None else f"{val:.0f}"


def fmt_pct(value: Any) -> str:
    val = _as_float(value)
    return "-" if val is None else f"{val:.2%}"


def steam_item_url(item: str) -> str:
    return "https://steamcommunity.com/market/listings/730/" + urllib.parse.quote(item, safe="")


def alert_key(row: pd.Series) -> str:
    listing_id = str(row.get("listing_id") or "").strip()
    if listing_id:
        return f"listing:{listing_id}"
    item = str(row.get("item") or "")
    ask = row.get("ask")
    flt = row.get("float_value")
    return f"fallback:{item}:{ask}:{flt}"


def metric_line(row: pd.Series, metric: str, label: str | None = None) -> str:
    label = label or metric
    value = fmt_value(metric, row.get(metric))
    threshold = THRESHOLDS.get(metric)
    suffix = f" / {threshold}" if threshold else ""
    return f"{icon_for(metric, row.get(metric))} {html.escape(label)}: <code>{html.escape(value)}</code>{html.escape(suffix)}"


def model_line(row: pd.Series, model: str, label: str) -> str:
    fair = fmt_money(row.get(f"pred_{model}_eur"))
    disc = fmt_money(row.get(f"pred_{model}_eur_disc"))
    spread = fmt_pct(row.get(f"spread_{model}"))
    spread_disc = fmt_pct(row.get(f"spread_{model}_disc"))
    return (
        f"{html.escape(label)}: "
        f"<code>{html.escape(fair)}</code> / "
        f"<code>{html.escape(disc)}</code> | "
        f"sp <code>{html.escape(spread)}</code> / "
        f"disc <code>{html.escape(spread_disc)}</code>"
    )


def format_alert(row: pd.Series) -> str:
    item = str(row.get("item") or "-")
    link = steam_item_url(item)
    spread = row.get("spread_hybrid_disc")
    spread_text = fmt_value("spread_hybrid_disc", spread)
    fair_gap = None
    spread_val = _as_float(spread)
    if spread_val is not None:
        fair_gap = -spread_val / max(1.0 - spread_val, 1e-12)

    lines = [
        f"<b>{icon_for('spread_hybrid_disc', spread)} Opportunity</b>",
        "",
        f"<b>{html.escape(item)}</b>",
        f"<a href=\"{html.escape(link)}\">Open Steam market page</a>",
        "",
        f"Ask: <code>{html.escape(fmt_money(row.get('ask')))}</code>",
        f"Float: <code>{html.escape(fmt_float(row.get('float_value')))}</code>",
        f"Seed: <code>{html.escape(fmt_seed(row.get('paint_seed')))}</code>",
        f"Tier: <code>{html.escape(str(row.get('tier') or '-'))}</code>",
        f"Listing ID: <code>{html.escape(str(row.get('listing_id') or '-'))}</code>",
        "",
        "<b>Model table</b>",
        model_line(row, "smooth", "Smooth"),
        model_line(row, "segmented", "Segmented"),
        model_line(row, "hybrid", "Hybrid"),
        f"Hybrid disc spread: <b>{html.escape(spread_text)}</b> / max 10.00%",
        f"Hybrid fair/ask gap: <code>{'-' if fair_gap is None else html.escape(f'{fair_gap:.2%}')}</code>",
        "",
        "<b>Risk checks</b>",
        metric_line(row, "steam_sales_7d_n", "sales 7d"),
        metric_line(row, "steam_sales_7d_downside_risk%", "downside risk"),
        metric_line(row, "steam_sales_7d_tail_ratio", "tail ratio"),
        metric_line(row, "steam_daily_downside_14d_pct", "downside 14d"),
        metric_line(row, "continuity_ratio", "continuity"),
        metric_line(row, "steam_turnover_proxy", "turnover"),
        metric_line(row, "scm_total_listings", "SCM listings"),
        metric_line(row, "steam_daily_ret_3d", "ret 3d"),
        metric_line(row, "steam_daily_ret_7d", "ret 7d"),
        metric_line(row, "steam_daily_slope_7d", "slope 7d"),
        metric_line(row, "steam_daily_ema_gap_3_14", "EMA gap 3/14"),
        metric_line(row, "steam_daily_range_14d_pct", "range 14d"),
        metric_line(row, "steam_sales_7d_iqr_risk%", "IQR risk"),
        metric_line(row, "steam_sales_7d_band_risk%", "band risk"),
    ]
    return "\n".join(lines)


def load_opportunities(path: Path) -> pd.DataFrame:
    if not path.is_file():
        raise FileNotFoundError(path)
    df = pd.read_csv(path, low_memory=False)
    if "item" not in df.columns:
        raise KeyError(f"{path} must contain an 'item' column")
    if "opportunity_pass" in df.columns:
        df = df[df["opportunity_pass"].astype(str).str.lower().isin(["true", "1"])].copy()
    return df.reset_index(drop=True)


def _passes_min(frame: pd.DataFrame, column: str, threshold: Any) -> pd.Series:
    if threshold is None or column not in frame.columns:
        return pd.Series(True, index=frame.index)
    return pd.to_numeric(frame[column], errors="coerce") >= float(threshold)


def _passes_max(frame: pd.DataFrame, column: str, threshold: Any) -> pd.Series:
    if threshold is None or column not in frame.columns:
        return pd.Series(True, index=frame.index)
    return pd.to_numeric(frame[column], errors="coerce") <= float(threshold)


def apply_alert_filters(frame: pd.DataFrame, alerts_cfg: dict[str, Any] | None = None) -> tuple[pd.DataFrame, dict[str, int]]:
    cfg = alerts_cfg or {}
    if not bool(cfg.get("enabled", True)):
        return frame.iloc[0:0].copy(), {"input": len(frame), "passed": 0, "filtered": len(frame)}

    mask = pd.Series(True, index=frame.index)
    mask &= _passes_max(frame, "spread_hybrid_disc", cfg.get("spread_hybrid_disc_max"))
    mask &= _passes_min(frame, "ask", cfg.get("ask_min"))
    mask &= _passes_max(frame, "ask", cfg.get("ask_max"))
    mask &= _passes_min(frame, "steam_sales_7d_n", cfg.get("steam_sales_7d_n_min"))
    mask &= _passes_max(frame, "steam_sales_7d_downside_risk%", cfg.get("steam_sales_7d_downside_risk_max"))
    mask &= _passes_min(frame, "steam_sales_7d_tail_ratio", cfg.get("steam_sales_7d_tail_ratio_min"))
    mask &= _passes_max(frame, "steam_daily_downside_14d_pct", cfg.get("steam_daily_downside_14d_pct_max"))
    mask &= _passes_max(frame, "continuity_ratio", cfg.get("continuity_ratio_max"))

    exclude_any = [str(x).lower() for x in cfg.get("exclude_any", []) if str(x).strip()]
    if exclude_any and "item" in frame.columns:
        names = frame["item"].fillna("").astype(str).str.lower()
        exclude_mask = pd.Series(False, index=frame.index)
        for token in exclude_any:
            exclude_mask |= names.str.contains(token, regex=False)
        mask &= ~exclude_mask

    out = frame[mask.fillna(False)].copy().reset_index(drop=True)
    return out, {"input": len(frame), "passed": len(out), "filtered": len(frame) - len(out)}


def _parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def should_send(key: str, state: dict[str, Any], cooldown_hours: float) -> bool:
    sent = state.get("sent_alerts")
    if not isinstance(sent, dict):
        return True
    prev = sent.get(key)
    if not isinstance(prev, dict):
        return True
    if cooldown_hours < 0:
        return False
    ts = _parse_iso(prev.get("sent_at_utc"))
    if ts is None:
        return True
    return datetime.now(timezone.utc) - ts >= timedelta(hours=float(cooldown_hours))


def mark_sent(state: dict[str, Any], key: str, row: pd.Series) -> dict[str, Any]:
    out = dict(state)
    sent = out.get("sent_alerts")
    if not isinstance(sent, dict):
        sent = {}
    sent[key] = {
        "sent_at_utc": utc_now_iso(),
        "item": str(row.get("item") or ""),
        "listing_id": str(row.get("listing_id") or ""),
        "spread_hybrid_disc": _as_float(row.get("spread_hybrid_disc")),
    }
    out["sent_alerts"] = sent
    out["last_alert_sent_at_utc"] = utc_now_iso()
    return out


def telegram_credentials(bot_token: str | None = None, chat_id: str | None = None) -> tuple[str, str]:
    token = bot_token or os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ.get("TG_BOT_TOKEN")
    chat = chat_id or os.environ.get("TELEGRAM_CHAT_ID") or os.environ.get("TG_CHAT_ID") or os.environ.get("TELEGRAM_CHANNEL")
    if not token:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN or TG_BOT_TOKEN")
    if not chat:
        raise RuntimeError("Missing TELEGRAM_CHAT_ID, TG_CHAT_ID, or TELEGRAM_CHANNEL")
    return token, chat


def send_message(
    text: str,
    *,
    bot_token: str,
    chat_id: str,
    timeout: int = 20,
    reply_to_message_id: int | None = None,
) -> dict[str, Any]:
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    data = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }
    if reply_to_message_id is not None:
        data["reply_to_message_id"] = str(int(reply_to_message_id))
        data["allow_sending_without_reply"] = "true"
    response = requests.post(
        url,
        data=data,
        timeout=timeout,
    )
    if not response.ok:
        raise RuntimeError(f"sendMessage failed: {response.status_code} {response.text[:800]}")
    payload = response.json()
    if not payload.get("ok"):
        raise RuntimeError(f"sendMessage failed: {payload}")
    return payload


def send_photo(
    image_bytes: bytes,
    *,
    bot_token: str,
    chat_id: str,
    caption: str | None = None,
    timeout: int = 20,
) -> dict[str, Any]:
    url = f"https://api.telegram.org/bot{bot_token}/sendPhoto"
    data = {"chat_id": chat_id}
    if caption:
        data["caption"] = caption
        data["parse_mode"] = "HTML"
    response = requests.post(
        url,
        data=data,
        files={"photo": ("fit.png", image_bytes, "image/png")},
        timeout=timeout,
    )
    if not response.ok:
        raise RuntimeError(f"sendPhoto failed: {response.status_code} {response.text[:800]}")
    payload = response.json()
    if not payload.get("ok"):
        raise RuntimeError(f"sendPhoto failed: {payload}")
    return payload


def _plot_config_value(plot_cfg: dict[str, Any], key: str, default: Any) -> Any:
    value = plot_cfg.get(key, default)
    return default if value is None else value


def maybe_render_fit_plot(row: pd.Series, plot_cfg: dict[str, Any] | None) -> bytes | None:
    cfg = plot_cfg or {}
    if not bool(cfg.get("enabled", False)):
        return None
    item = str(row.get("item", "") or "").strip()
    if not item:
        return None

    from automation.model_fit_plot import precomputed_plot_path, render_item_fit_plot

    data_dir = Path(str(_plot_config_value(cfg, "data_dir", "skin_homog/data_skins_big")))
    fit_json = Path(str(_plot_config_value(cfg, "fit_json", "steam_listings/data/float_fit_rel_curves.json")))
    dpi = int(_plot_config_value(cfg, "dpi", 120))
    precomputed_dir_raw = _plot_config_value(cfg, "precomputed_dir", None)
    precomputed_dir = None if precomputed_dir_raw in (None, "") else Path(str(precomputed_dir_raw))
    if precomputed_dir is not None:
        precomputed_path = precomputed_plot_path(item, precomputed_dir)
        if precomputed_path.is_file():
            return precomputed_path.read_bytes()

    image_bytes = render_item_fit_plot(item, data_dir=data_dir, fit_json=fit_json, dpi=dpi)
    if precomputed_dir is not None and bool(cfg.get("write_precomputed_on_miss", True)):
        precomputed_path = precomputed_plot_path(item, precomputed_dir)
        precomputed_path.parent.mkdir(parents=True, exist_ok=True)
        precomputed_path.write_bytes(image_bytes)
    return image_bytes


def send_opportunity_alerts(
    opportunities_csv: Path,
    state_json: Path,
    monitor_items_py: Path,
    *,
    config_path: Path | None = None,
    bot_token: str | None = None,
    chat_id: str | None = None,
    cooldown_hours: float = 12.0,
    dry_run: bool = False,
    sleep_sec: float = 0.6,
    max_alerts: int | None = None,
    alerts_cfg: dict[str, Any] | None = None,
    plot_cfg: dict[str, Any] | None = None,
    alert_enrichment_cfg: dict[str, Any] | None = None,
) -> dict[str, int]:
    raw_df = load_opportunities(opportunities_csv)
    df, filter_stats = apply_alert_filters(raw_df, alerts_cfg)
    items = load_items_py(monitor_items_py) if monitor_items_py.is_file() else []
    state = load_state(state_json, items)
    plot_cache: dict[str, bytes | None] = {}
    enrichment_cfg = dict(alert_enrichment_cfg or {})
    if config_path is None:
        config_path = repo_root_from(Path(__file__)) / "automation" / "configs" / "monitoring.json"
    token = chat = None
    if not dry_run:
        token, chat = telegram_credentials(bot_token=bot_token, chat_id=chat_id)

    sent_n = 0
    skipped_n = 0
    considered_n = 0
    for _, row in df.iterrows():
        considered_n += 1
        key = alert_key(row)
        if not should_send(key, state, cooldown_hours):
            skipped_n += 1
            continue
        message = format_alert(row)
        if dry_run:
            print("=" * 70)
            print(message)
        else:
            assert token is not None and chat is not None
            item = str(row.get("item", "") or "").strip()
            image_bytes = plot_cache.get(item) if item else None
            try:
                primary_payload = send_message(message, bot_token=token, chat_id=chat)
            except Exception as exc:
                if bool((plot_cfg or {}).get("fail_on_error", False)):
                    raise
                print(f"telegram text send failed: {exc}")
                continue
            primary_message_id = None
            result_payload = primary_payload.get("result") if isinstance(primary_payload, dict) else None
            if isinstance(result_payload, dict):
                try:
                    primary_message_id = int(result_payload.get("message_id"))
                except Exception:
                    primary_message_id = None
            try:
                if item and item not in plot_cache:
                    image_bytes = maybe_render_fit_plot(row, plot_cfg)
                    plot_cache[item] = image_bytes
                if image_bytes:
                    send_photo(
                        image_bytes,
                        bot_token=token,
                        chat_id=chat,
                        caption=(item or "Model fit")[:1024],
                    )
            except Exception as exc:
                if bool((plot_cfg or {}).get("fail_on_error", False)):
                    raise
                print(f"fit plot skipped: {exc}")
            if bool(enrichment_cfg.get("enabled", False)):
                try:
                    from automation.alert_enrichment import queue_enrichment_job, run_enrichment_job, spawn_enrichment_worker

                    job_json = queue_enrichment_job(
                        row=row.to_dict(),
                        primary_message_id=primary_message_id,
                        config={"alert_enrichment": enrichment_cfg},
                        config_path=config_path,
                        chat_id=chat,
                    )
                    if job_json is not None:
                        if bool(enrichment_cfg.get("background", True)):
                            spawn_enrichment_worker(config_path=config_path, job_json=job_json)
                        else:
                            run_enrichment_job(job_json, {"alert_enrichment": enrichment_cfg}, dry_run=False)
                except Exception as exc:
                    print(f"alert enrichment failed to start: {exc}")
            state = mark_sent(state, key, row)
            save_state(state_json, state)
            time.sleep(max(0.0, float(sleep_sec)))
        sent_n += 1
        if max_alerts is not None and sent_n >= max_alerts:
            break

    return {
        "loaded": int(filter_stats["input"]),
        "filtered": int(filter_stats["filtered"]),
        "considered": considered_n,
        "sent": sent_n,
        "skipped": skipped_n,
    }
