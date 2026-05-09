"""Post-alert enrichment: CSFloat latest sales + Gemini trade note."""

from __future__ import annotations

import hashlib
import html
import json
import math
import os
import re
import shlex
import subprocess
import sys
import time
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

from automation.risk_filters import repo_root_from
from automation.state import utc_now_iso


DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/136.0.0.0 Safari/537.36"
)

FRANKFURTER_LATEST = "https://api.frankfurter.app/latest"
ECB_DAILY_XML = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"


@dataclass(frozen=True)
class EnrichmentConfig:
    enabled: bool
    background: bool
    provider: str
    gemini_model: str
    prompt_template_path: Path
    fee_pct: float
    max_sales_rows: int
    cache_ttl_minutes: float
    use_stale_cache_on_error: bool
    use_cache: bool
    persist_cache: bool
    github_fetch_enabled: bool
    github_fetch_required: bool
    github_fetch_repo_path: Path | None
    github_fetch_remote_url: str
    github_fetch_branch: str
    github_fetch_timeout_sec: float
    github_fetch_poll_interval_sec: float
    log_dir: Path
    csfloat_base_url: str
    csfloat_timeout_sec: float
    gemini_timeout_sec: float
    telegram_timeout_sec: int
    user_agent: str


def repo_root() -> Path:
    return repo_root_from(Path(__file__))


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _resolve_path(root: Path, value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else (root / path).resolve()


def load_enrichment_config(config: dict[str, Any], *, root: Path | None = None) -> EnrichmentConfig:
    cfg = config.get("alert_enrichment", {})
    github_cfg = cfg.get("github_fetch", {})
    if not isinstance(github_cfg, dict):
        github_cfg = {}
    base_root = root or repo_root()
    github_repo_path_raw = github_cfg.get("repo_path")
    github_repo_path = None
    if github_repo_path_raw not in (None, ""):
        github_repo_path = _resolve_path(base_root, github_repo_path_raw)
    return EnrichmentConfig(
        enabled=bool(cfg.get("enabled", False)),
        background=bool(cfg.get("background", True)),
        provider=str(cfg.get("provider", "gemini")).strip() or "gemini",
        gemini_model=str(cfg.get("gemini_model", "gemini-2.5-flash")).strip() or "gemini-2.5-flash",
        prompt_template_path=_resolve_path(
            base_root,
            cfg.get("prompt_template_path", "automation/prompts/alert_enrichment_gemini.txt"),
        ),
        fee_pct=float(cfg.get("fee_pct", 0.02)),
        max_sales_rows=max(1, int(cfg.get("max_sales_rows", 30))),
        cache_ttl_minutes=max(0.0, float(cfg.get("cache_ttl_minutes", 15.0))),
        use_stale_cache_on_error=bool(cfg.get("use_stale_cache_on_error", True)),
        use_cache=bool(cfg.get("use_cache", True)),
        persist_cache=bool(cfg.get("persist_cache", True)),
        github_fetch_enabled=bool(github_cfg.get("enabled", False)),
        github_fetch_required=bool(github_cfg.get("required", False)),
        github_fetch_repo_path=github_repo_path,
        github_fetch_remote_url=str(github_cfg.get("remote_url", "")).strip(),
        github_fetch_branch=str(github_cfg.get("branch", "main")).strip() or "main",
        github_fetch_timeout_sec=max(5.0, float(github_cfg.get("timeout_sec", 180.0))),
        github_fetch_poll_interval_sec=max(1.0, float(github_cfg.get("poll_interval_sec", 5.0))),
        log_dir=_resolve_path(base_root, cfg.get("log_dir", "automation_runtime/alert_enrichment")),
        csfloat_base_url=str(cfg.get("csfloat_base_url", "https://csfloat.com")).rstrip("/"),
        csfloat_timeout_sec=max(5.0, float(cfg.get("csfloat_timeout_sec", 30.0))),
        gemini_timeout_sec=max(5.0, float(cfg.get("gemini_timeout_sec", 45.0))),
        telegram_timeout_sec=max(5, int(cfg.get("telegram_timeout_sec", 20))),
        user_agent=str(cfg.get("user_agent", DEFAULT_USER_AGENT)).strip() or DEFAULT_USER_AGENT,
    )


def jobs_dir(cfg: EnrichmentConfig) -> Path:
    return cfg.log_dir / "jobs"


def cache_dir(cfg: EnrichmentConfig) -> Path:
    return cfg.log_dir / "cache"


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def _slug(value: str, *, max_len: int = 48) -> str:
    text = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return text[:max_len] or "item"


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if hasattr(value, "item") and not isinstance(value, (str, bytes, bytearray)):
        try:
            value = value.item()
        except Exception:
            pass
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return value
    try:
        if value != value:
            return None
    except Exception:
        pass
    return str(value)


def sanitize_row(row: dict[str, Any]) -> dict[str, Any]:
    return {str(key): _json_safe(value) for key, value in row.items()}


def write_json(path: Path, payload: dict[str, Any] | list[Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def append_status(job_dir: Path, status: str, **extra: Any) -> None:
    status_path = job_dir / "status.json"
    payload: dict[str, Any]
    if status_path.is_file():
        try:
            payload = json.loads(status_path.read_text(encoding="utf-8"))
        except Exception:
            payload = {}
    else:
        payload = {}
    payload.update({"status": status, "updated_at_utc": utc_now_iso()})
    for key, value in extra.items():
        payload[key] = _json_safe(value)
    write_json(status_path, payload)


def _build_job_id(row: dict[str, Any]) -> str:
    stamp = _now_utc().strftime("%Y%m%dT%H%M%S_%fZ")
    listing_id = str(row.get("listing_id") or "no-listing")
    item = _slug(str(row.get("item") or "item"))
    return f"{stamp}_{listing_id}_{item}"


def queue_enrichment_job(
    *,
    row: dict[str, Any],
    primary_message_id: int | None,
    config: dict[str, Any],
    config_path: Path | None = None,
    chat_id: str | None = None,
) -> Path | None:
    cfg = load_enrichment_config(config)
    if not cfg.enabled:
        return None
    row_payload = sanitize_row(row)
    job_id = _build_job_id(row_payload)
    job_dir = jobs_dir(cfg) / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "job_id": job_id,
        "queued_at_utc": utc_now_iso(),
        "config_path": str(config_path.resolve()) if config_path else None,
        "primary_message_id": int(primary_message_id) if primary_message_id is not None else None,
        "chat_id": chat_id,
        "item": row_payload.get("item"),
        "listing_id": row_payload.get("listing_id"),
        "row": row_payload,
    }
    job_path = job_dir / "job.json"
    write_json(job_path, payload)
    append_status(job_dir, "queued")
    return job_path


def _load_secrets_file() -> None:
    secrets_file = os.environ.get("CS_ARBITRAGE_SECRETS") or str(repo_root().parent / "secrets.env")
    path = Path(secrets_file)
    if not path.is_file():
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key or key in os.environ:
            continue
        if value and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ[key] = value


def spawn_enrichment_worker(*, config_path: Path, job_json: Path) -> int:
    worker = repo_root() / "automation" / "monitoring" / "run_alert_enrichment.py"
    cmd = [sys.executable, str(worker), "--config", str(config_path), "--job-json", str(job_json)]
    secrets_file = os.environ.get("CS_ARBITRAGE_SECRETS") or str(repo_root().parent / "secrets.env")
    if os.path.isfile(secrets_file):
        bash_cmd = f"set -a; source {shlex.quote(secrets_file)}; set +a; exec {shlex.join(cmd)}"
        launch_cmd = ["/bin/bash", "-lc", bash_cmd]
    else:
        launch_cmd = cmd
    log_path = job_json.parent / "worker.log"
    with log_path.open("ab") as handle:
        proc = subprocess.Popen(
            launch_cmd,
            cwd=str(repo_root()),
            stdout=handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    return int(proc.pid)


def _cache_key(item: str) -> str:
    return hashlib.sha256(item.encode("utf-8")).hexdigest()[:24]


def _cache_path(cfg: EnrichmentConfig, item: str) -> Path:
    return cache_dir(cfg) / f"{_cache_key(item)}.json"


def _load_cache(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _cache_age_minutes(payload: dict[str, Any]) -> float | None:
    ts = _parse_iso(payload.get("fetched_at_utc"))
    if ts is None:
        return None
    return (_now_utc() - ts).total_seconds() / 60.0


def _is_cache_fresh(payload: dict[str, Any], ttl_minutes: float) -> bool:
    age = _cache_age_minutes(payload)
    return age is not None and age <= max(0.0, ttl_minutes)


def fetch_usd_to_eur_multiplier(*, user_agent: str, timeout_sec: float) -> tuple[float, str]:
    err_ff: Exception | None = None
    try:
        response = requests.get(
            FRANKFURTER_LATEST,
            params={"from": "USD", "to": "EUR"},
            timeout=timeout_sec,
            headers={"User-Agent": user_agent},
        )
        response.raise_for_status()
        payload = response.json()
        multiplier = float(payload["rates"]["EUR"])
        day = payload.get("date", "?")
        return multiplier, f"Frankfurter {day} (ECB)"
    except Exception as exc:
        err_ff = exc
    response = requests.get(ECB_DAILY_XML, timeout=timeout_sec, headers={"User-Agent": user_agent})
    response.raise_for_status()
    root = ET.fromstring(response.content)
    usd_per_1_eur: float | None = None
    for elem in root.iter():
        if elem.attrib.get("currency") == "USD":
            usd_per_1_eur = float(elem.attrib["rate"])
            break
    if usd_per_1_eur is None or usd_per_1_eur <= 0:
        raise RuntimeError(f"USD->EUR: Frankfurter failed ({err_ff!r}); ECB fallback has no USD rate")
    return 1.0 / usd_per_1_eur, "ECB eurofxref-daily.xml (fallback)"


def _normalize_latest_sale(row: dict[str, Any], *, fx_usd_to_eur: float, fx_source: str) -> dict[str, Any]:
    item = row.get("item")
    item_payload = item if isinstance(item, dict) else {}
    stickers = item_payload.get("stickers")
    sticker_names: list[str] = []
    if isinstance(stickers, list):
        for entry in stickers:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or "").strip()
            if name:
                sticker_names.append(name)
    notes: list[str] = []
    low_rank = item_payload.get("low_rank")
    high_rank = item_payload.get("high_rank")
    phase = item_payload.get("phase")
    fade = item_payload.get("fade")
    blue_gem = item_payload.get("blue_gem")
    if low_rank not in (None, ""):
        notes.append(f"low #{low_rank}")
    if high_rank not in (None, ""):
        notes.append(f"high #{high_rank}")
    if phase not in (None, ""):
        notes.append(f"phase {phase}")
    if fade not in (None, ""):
        notes.append(f"fade {fade}")
    if blue_gem not in (None, ""):
        notes.append(f"blue_gem {blue_gem}")
    if sticker_names:
        notes.append(f"{len(sticker_names)} stickers")
    price_usd = None
    price_eur = None
    raw_price = row.get("price")
    try:
        price_minor = float(raw_price)
    except Exception:
        pass
    else:
        if math.isfinite(price_minor):
            price_usd = round(price_minor / 100.0, 2)
            price_eur = round(price_usd * float(fx_usd_to_eur), 2)
    return {
        "sale_id": _json_safe(row.get("id")),
        "sold_at": _json_safe(row.get("sold_at")),
        "price_usd": price_usd,
        "price_eur": price_eur,
        "reference_currency": "USD",
        "fx_usd_to_eur": _json_safe(fx_usd_to_eur),
        "fx_source": fx_source,
        "float_value": _json_safe(item_payload.get("float_value")),
        "paint_seed": _json_safe(item_payload.get("paint_seed")),
        "stickers": sticker_names[:5],
        "notes": notes,
    }


def fetch_latest_sales(item: str, cfg: EnrichmentConfig, *, job_dir: Path) -> dict[str, Any]:
    url = f"{cfg.csfloat_base_url}/api/v1/history/{urllib.parse.quote(item, safe='')}/sales"
    fx_usd_to_eur, fx_source = fetch_usd_to_eur_multiplier(
        user_agent=cfg.user_agent,
        timeout_sec=cfg.csfloat_timeout_sec,
    )
    response = requests.get(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": cfg.user_agent,
        },
        timeout=cfg.csfloat_timeout_sec,
    )
    content_type = response.headers.get("content-type", "")
    if "application/json" in content_type:
        raw_payload = response.json()
    else:
        raw_payload = {"text": response.text[:4000]}
    write_json(
        job_dir / "latest_sales_raw.json",
        {
            "fetched_at_utc": utc_now_iso(),
            "url": url,
            "http_status": response.status_code,
            "payload": _json_safe(raw_payload),
        },
    )
    if not response.ok:
        raise RuntimeError(f"CSFloat latest-sales HTTP {response.status_code}")
    if isinstance(raw_payload, dict) and raw_payload.get("error"):
        raise RuntimeError(f"CSFloat latest-sales error: {raw_payload.get('error')}")
    if isinstance(raw_payload, list):
        rows_raw = raw_payload
    elif isinstance(raw_payload, dict):
        rows_raw = raw_payload.get("data") or raw_payload.get("rows") or raw_payload.get("sales") or []
    else:
        rows_raw = []
    if not isinstance(rows_raw, list):
        raise RuntimeError("CSFloat latest-sales payload shape is not a list")
    rows = [
        _normalize_latest_sale(entry, fx_usd_to_eur=fx_usd_to_eur, fx_source=fx_source)
        for entry in rows_raw
        if isinstance(entry, dict)
    ]
    rows = rows[: cfg.max_sales_rows]
    if not rows:
        raise RuntimeError("CSFloat latest-sales returned no sales rows")
    return {
        "version": 1,
        "source": "network",
        "item": item,
        "url": url,
        "fetched_at_utc": utc_now_iso(),
        "raw_row_count": len(rows_raw),
        "reference_currency": "USD",
        "fx_usd_to_eur": fx_usd_to_eur,
        "fx_source": fx_source,
        "sales_rows": rows,
    }


def load_latest_sales(item: str, cfg: EnrichmentConfig, *, job_dir: Path) -> dict[str, Any]:
    if cfg.github_fetch_enabled:
        try:
            from automation.github_latest_sales import fetch_latest_sales_via_github

            payload = fetch_latest_sales_via_github(
                item=item,
                repo_path=cfg.github_fetch_repo_path,
                remote_url=cfg.github_fetch_remote_url,
                branch=cfg.github_fetch_branch,
                timeout_sec=cfg.github_fetch_timeout_sec,
                poll_interval_sec=cfg.github_fetch_poll_interval_sec,
                max_sales_rows=cfg.max_sales_rows,
                job_dir=job_dir,
            )
            if cfg.persist_cache:
                cache_path = _cache_path(cfg, item)
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                write_json(cache_path, payload)
            write_json(job_dir / "latest_sales.json", payload)
            return payload
        except Exception as exc:
            if cfg.github_fetch_required:
                write_json(job_dir / "latest_sales_error.json", {"error": str(exc), "item": item, "at_utc": utc_now_iso()})
                raise
            write_json(
                job_dir / "latest_sales_github_fallback_error.json",
                {"error": str(exc), "item": item, "at_utc": utc_now_iso()},
            )

    cache_path = _cache_path(cfg, item)
    cached = _load_cache(cache_path) if cfg.use_cache else None
    if cached and _is_cache_fresh(cached, cfg.cache_ttl_minutes):
        payload = dict(cached)
        payload["source"] = "fresh_cache"
        age = _cache_age_minutes(cached)
        if age is not None:
            payload["cache_age_minutes"] = round(age, 2)
        write_json(job_dir / "latest_sales.json", payload)
        return payload

    try:
        fresh = fetch_latest_sales(item, cfg, job_dir=job_dir)
    except Exception as exc:
        if cached and cfg.use_stale_cache_on_error:
            payload = dict(cached)
            payload["source"] = "stale_cache"
            payload["stale_reason"] = str(exc)
            age = _cache_age_minutes(cached)
            if age is not None:
                payload["cache_age_minutes"] = round(age, 2)
            write_json(job_dir / "latest_sales.json", payload)
            return payload
        write_json(job_dir / "latest_sales_error.json", {"error": str(exc), "item": item, "at_utc": utc_now_iso()})
        raise

    if cfg.persist_cache:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        write_json(cache_path, fresh)
    write_json(job_dir / "latest_sales.json", fresh)
    return fresh


def _num(value: Any) -> float | None:
    try:
        out = float(value)
    except Exception:
        return None
    return out if math.isfinite(out) else None


def _pct_or_none(value: float | None) -> float | None:
    return None if value is None or not math.isfinite(value) else value * 100.0


def _net_exit_pct(gross_sale_eur: float | None, steam_ask: float | None, *, fee_pct: float) -> float | None:
    if gross_sale_eur is None or steam_ask is None or steam_ask <= 0:
        return None
    return ((gross_sale_eur * (1.0 - fee_pct) / steam_ask) - 1.0) * 100.0


def _gross_target(steam_ask: float | None, target_net_pct: float, *, fee_pct: float) -> float | None:
    if steam_ask is None or steam_ask <= 0 or fee_pct >= 1.0:
        return None
    return steam_ask * (1.0 + target_net_pct / 100.0) / (1.0 - fee_pct)


def _dispersion_pct(values: list[float | None]) -> float | None:
    clean = [float(v) for v in values if v is not None and math.isfinite(float(v))]
    if len(clean) < 2:
        return None
    mean = sum(clean) / len(clean)
    if not math.isfinite(mean) or mean <= 0:
        return None
    return ((max(clean) - min(clean)) / mean) * 100.0


def _compact_alert_payload(row: dict[str, Any]) -> dict[str, Any]:
    steam_ask = _num(row.get("ask"))
    smooth_fair = _num(row.get("pred_smooth_eur"))
    segmented_fair = _num(row.get("pred_segmented_eur"))
    hybrid_fair = _num(row.get("pred_hybrid_eur"))
    smooth_disc_fair = _num(row.get("pred_smooth_eur_disc"))
    segmented_disc_fair = _num(row.get("pred_segmented_eur_disc"))
    hybrid_disc_fair = _num(row.get("pred_hybrid_eur_disc"))
    return {
        "item": row.get("item"),
        "listing_id": row.get("listing_id"),
        "steam_ask": steam_ask,
        "float_value": row.get("float_value"),
        "paint_seed": row.get("paint_seed"),
        "hybrid_disc_spread": row.get("spread_hybrid_disc"),
        "smooth_fair_eur": smooth_fair,
        "segmented_fair_eur": segmented_fair,
        "hybrid_fair_eur": hybrid_fair,
        "smooth_disc_fair_eur": smooth_disc_fair,
        "segmented_disc_fair_eur": segmented_disc_fair,
        "hybrid_disc_fair_eur": hybrid_disc_fair,
        "model_fair_dispersion_pct": _dispersion_pct([smooth_fair, segmented_fair, hybrid_fair]),
        "model_disc_dispersion_pct": _dispersion_pct([smooth_disc_fair, segmented_disc_fair, hybrid_disc_fair]),
        "continuity_ratio": row.get("continuity_ratio"),
        "steam_sales_7d_n": row.get("steam_sales_7d_n"),
        "steam_sales_7d_downside_risk_pct": row.get("steam_sales_7d_downside_risk%"),
        "steam_sales_7d_tail_ratio": row.get("steam_sales_7d_tail_ratio"),
        "steam_turnover_proxy": row.get("steam_turnover_proxy"),
        "scm_total_listings": row.get("scm_total_listings"),
        "steam_daily_ret_3d": row.get("steam_daily_ret_3d"),
        "steam_daily_ret_7d": row.get("steam_daily_ret_7d"),
        "steam_daily_slope_7d": row.get("steam_daily_slope_7d"),
        "steam_daily_ema_gap_3_14": row.get("steam_daily_ema_gap_3_14"),
        "steam_daily_range_14d_pct": row.get("steam_daily_range_14d_pct"),
        "steam_daily_downside_14d_pct": row.get("steam_daily_downside_14d_pct"),
        "steam_sales_7d_iqr_risk_pct": row.get("steam_sales_7d_iqr_risk%"),
        "steam_sales_7d_band_risk_pct": row.get("steam_sales_7d_band_risk%"),
        "breakeven_gross_eur": _gross_target(steam_ask, 0.0, fee_pct=0.02),
        "gross_for_minus_5pct": _gross_target(steam_ask, -5.0, fee_pct=0.02),
        "gross_for_minus_10pct": _gross_target(steam_ask, -10.0, fee_pct=0.02),
        "gross_for_minus_15pct": _gross_target(steam_ask, -15.0, fee_pct=0.02),
    }


def _load_prompt_template(cfg: EnrichmentConfig) -> str:
    path = cfg.prompt_template_path
    if path.is_file():
        return path.read_text(encoding="utf-8")
    return (
        "You are a careful CS2 skins trading assistant. "
        "Be conservative and do not hype trades. "
        "Use the alert metrics plus recent CSFloat latest sales comps to produce a short preliminary note. "
        "Prefer MAYBE or PASS over BUY when the edge is thin or the comps are mixed.\n\n"
        "Return valid JSON only with these keys:\n"
        "{\n"
        '  "verdict": "BUY|MAYBE|PASS",\n'
        '  "confidence": "low|medium|high",\n'
        '  "breakeven_gross_eur": number|null,\n'
        '  "gross_for_minus_5pct": number|null,\n'
        '  "gross_for_minus_10pct": number|null,\n'
        '  "gross_for_minus_15pct": number|null,\n'
        '  "fast_sale_range_eur": [number, number]|null,\n'
        '  "realistic_sale_range_eur": [number, number]|null,\n'
        '  "patient_sale_range_eur": [number, number]|null,\n'
        '  "start_listing_range_eur": [number, number]|null,\n'
        '  "fast_floor_range_eur": [number, number]|null,\n'
        '  "best_comps": [{"price_eur": number|null, "float_value": number|null, "paint_seed": number|null, "why": string}],\n'
        '  "risks": [string],\n'
        '  "summary": string\n'
        "}\n\n"
        "Keep best_comps to at most 3 entries and risks to at most 3 entries. "
        "Use only the given data. If something is unclear, say so briefly in summary.\n\n"
        "INPUT_JSON:\n__INPUT_JSON__"
    )


def _gemini_prompt(row: dict[str, Any], latest_sales: dict[str, Any], cfg: EnrichmentConfig) -> str:
    payload = {
        "fee_pct": cfg.fee_pct,
        "alert": _compact_alert_payload(row),
        "latest_sales_source": latest_sales.get("source"),
        "latest_sales_count": len(latest_sales.get("sales_rows") or []),
        "latest_sales": latest_sales.get("sales_rows") or [],
    }
    template = _load_prompt_template(cfg)
    return template.replace("__INPUT_JSON__", json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def _strip_json_fence(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    return stripped.strip()


def _extract_text(payload: dict[str, Any]) -> str:
    candidates = payload.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise RuntimeError(f"Gemini returned no candidates: {payload}")
    parts = candidates[0].get("content", {}).get("parts", [])
    if not isinstance(parts, list):
        raise RuntimeError(f"Gemini returned malformed parts: {payload}")
    chunks = [str(part.get("text") or "") for part in parts if isinstance(part, dict)]
    text = "\n".join(chunk for chunk in chunks if chunk.strip())
    if not text.strip():
        raise RuntimeError(f"Gemini returned empty text: {payload}")
    return text


def _coerce_range(value: Any) -> list[float] | None:
    if not isinstance(value, list) or len(value) != 2:
        return None
    left = _num(value[0])
    right = _num(value[1])
    if left is None or right is None:
        return None
    return [left, right]


def _coerce_label(value: Any, allowed: set[str], default: str) -> str:
    text = str(value or "").strip().lower()
    return text if text in allowed else default


def _post_validate_note(note: dict[str, Any]) -> dict[str, Any]:
    out = dict(note)
    verdict = str(out.get("verdict") or "MAYBE").upper()
    confidence = str(out.get("confidence") or "medium").lower()
    target_15 = str(out.get("target_15pct_fast") or "maybe").lower()
    float_liq = str(out.get("float_liquidity") or "medium").lower()
    model_agreement = str(out.get("model_agreement") or "mixed").lower()
    risk_level = str(out.get("risk_level") or "medium").lower()
    fast_sale_range = out.get("fast_sale_range_eur")
    gross_for_minus_15 = _num(out.get("gross_for_minus_15pct"))

    if verdict == "BUY" and target_15 != "yes":
        verdict = "MAYBE"
    if verdict == "BUY" and float_liq == "low":
        verdict = "MAYBE"
    if verdict == "BUY" and model_agreement == "divergent" and confidence != "high":
        verdict = "MAYBE"
    if verdict == "BUY" and risk_level == "high":
        verdict = "MAYBE"
    if verdict == "BUY" and isinstance(fast_sale_range, list) and len(fast_sale_range) == 2 and gross_for_minus_15 is not None:
        fast_low = _num(fast_sale_range[0])
        if fast_low is not None and fast_low < gross_for_minus_15:
            verdict = "MAYBE"
    out["verdict"] = verdict
    return out


def _call_gemini_once(
    *,
    prompt: str,
    model: str,
    cfg: EnrichmentConfig,
    job_dir: Path,
    attempt_tag: str,
    max_output_tokens: int,
) -> tuple[dict[str, Any], int]:
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("Missing GEMINI_API_KEY")
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent?key={urllib.parse.quote(api_key, safe='')}"
    )
    request_payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.2,
            "topP": 0.9,
            "maxOutputTokens": int(max_output_tokens),
            "responseMimeType": "application/json",
            "thinkingConfig": {
                "thinkingBudget": 0,
            },
        },
    }
    write_json(job_dir / f"gemini_request_{attempt_tag}.json", request_payload)
    response = requests.post(url, json=request_payload, timeout=cfg.gemini_timeout_sec)
    raw_payload = response.json() if "application/json" in response.headers.get("content-type", "") else {"text": response.text[:4000]}
    write_json(
        job_dir / f"gemini_response_raw_{attempt_tag}.json",
        {
            "http_status": response.status_code,
            "received_at_utc": utc_now_iso(),
            "model": model,
            "payload": _json_safe(raw_payload),
        },
    )
    if not response.ok:
        return raw_payload if isinstance(raw_payload, dict) else {"payload": raw_payload}, response.status_code
    text = _extract_text(raw_payload if isinstance(raw_payload, dict) else {})
    parsed = json.loads(_strip_json_fence(text))
    if not isinstance(parsed, dict):
        raise RuntimeError(f"Gemini returned non-object JSON: {parsed!r}")
    return parsed, response.status_code


def call_gemini(row: dict[str, Any], latest_sales: dict[str, Any], cfg: EnrichmentConfig, *, job_dir: Path) -> dict[str, Any]:
    prompt = _gemini_prompt(row, latest_sales, cfg)
    models: list[str] = []
    for model in (cfg.gemini_model, "gemini-2.5-flash-lite"):
        model_name = str(model).strip()
        if model_name and model_name not in models:
            models.append(model_name)
    last_error = "Gemini call failed"
    parsed: dict[str, Any] | None = None
    for model_index, model in enumerate(models, start=1):
        for attempt in range(1, 3):
            attempt_tag = f"m{model_index}_a{attempt}"
            max_output_tokens = 1400 if attempt == 1 else 2200
            try:
                parsed_payload, http_status = _call_gemini_once(
                    prompt=prompt,
                    model=model,
                    cfg=cfg,
                    job_dir=job_dir,
                    attempt_tag=attempt_tag,
                    max_output_tokens=max_output_tokens,
                )
            except Exception as exc:
                last_error = f"{model} parse/transport error: {exc}"
                time.sleep(0.5)
                continue
            if http_status >= 500 or http_status in {429, 503}:
                error_info = parsed_payload.get("error") if isinstance(parsed_payload, dict) else parsed_payload
                last_error = f"{model} HTTP {http_status}: {error_info}"
                time.sleep(1.0)
                continue
            if http_status != 200:
                raise RuntimeError(f"{model} HTTP {http_status}: {parsed_payload}")
            parsed = parsed_payload
            break
        if parsed is not None:
            break
    if parsed is None:
        raise RuntimeError(last_error)
    result = {
        "verdict": str(parsed.get("verdict") or "MAYBE").upper(),
        "confidence": str(parsed.get("confidence") or "medium").lower(),
        "risk_level": _coerce_label(parsed.get("risk_level"), {"low", "medium", "high"}, "medium"),
        "item_liquidity": _coerce_label(parsed.get("item_liquidity"), {"low", "medium", "high"}, "medium"),
        "float_liquidity": _coerce_label(parsed.get("float_liquidity"), {"low", "medium", "high"}, "medium"),
        "model_agreement": _coerce_label(parsed.get("model_agreement"), {"strong", "mixed", "divergent"}, "mixed"),
        "target_15pct_fast": _coerce_label(parsed.get("target_15pct_fast"), {"yes", "maybe", "no"}, "maybe"),
        "breakeven_gross_eur": _num(parsed.get("breakeven_gross_eur")),
        "gross_for_minus_5pct": _num(parsed.get("gross_for_minus_5pct")),
        "gross_for_minus_10pct": _num(parsed.get("gross_for_minus_10pct")),
        "gross_for_minus_15pct": _num(parsed.get("gross_for_minus_15pct")),
        "fast_sale_range_eur": _coerce_range(parsed.get("fast_sale_range_eur")),
        "realistic_sale_range_eur": _coerce_range(parsed.get("realistic_sale_range_eur")),
        "patient_sale_range_eur": _coerce_range(parsed.get("patient_sale_range_eur")),
        "start_listing_range_eur": _coerce_range(parsed.get("start_listing_range_eur")),
        "fast_floor_range_eur": _coerce_range(parsed.get("fast_floor_range_eur")),
        "fast_net_exit_pct": _coerce_range(parsed.get("fast_net_exit_pct")),
        "realistic_net_exit_pct": _coerce_range(parsed.get("realistic_net_exit_pct")),
        "patient_net_exit_pct": _coerce_range(parsed.get("patient_net_exit_pct")),
        "best_comps": [],
        "risks": [],
        "summary": str(parsed.get("summary") or "").strip(),
    }
    best_comps = parsed.get("best_comps")
    if isinstance(best_comps, list):
        for entry in best_comps[:3]:
            if not isinstance(entry, dict):
                continue
            result["best_comps"].append(
                {
                    "price_eur": _num(entry.get("price_eur")),
                    "float_value": _num(entry.get("float_value")),
                    "paint_seed": _num(entry.get("paint_seed")),
                    "why": str(entry.get("why") or "").strip(),
                }
            )
    risks = parsed.get("risks")
    if isinstance(risks, list):
        result["risks"] = [str(entry).strip() for entry in risks[:3] if str(entry).strip()]
    for key_prefix in ("fast", "realistic", "patient"):
        if result.get(f"{key_prefix}_net_exit_pct") is None:
            sale_range = result.get(f"{key_prefix}_sale_range_eur")
            if isinstance(sale_range, list) and len(sale_range) == 2:
                ask = _num(row.get("ask"))
                lo = _net_exit_pct(_num(sale_range[0]), ask, fee_pct=cfg.fee_pct)
                hi = _net_exit_pct(_num(sale_range[1]), ask, fee_pct=cfg.fee_pct)
                if lo is not None and hi is not None:
                    result[f"{key_prefix}_net_exit_pct"] = [lo, hi]
    result = _post_validate_note(result)
    write_json(job_dir / "gemini_result.json", result)
    return result


def _fmt_money(value: float | None) -> str:
    return "-" if value is None else f"€{value:.2f}"


def _fmt_range(value: list[float] | None) -> str:
    if not value:
        return "-"
    return f"€{value[0]:.2f}-{value[1]:.2f}"


def _fmt_pct_range(value: list[float] | None) -> str:
    if not value:
        return "-"
    return f"{value[0]:+.1f}%..{value[1]:+.1f}%"


def _fmt_comp(comp: dict[str, Any]) -> str:
    parts = [_fmt_money(_num(comp.get("price_eur")))]
    flt = _num(comp.get("float_value"))
    if flt is not None:
        parts.append(f"@ {flt:.6f}")
    seed = _num(comp.get("paint_seed"))
    if seed is not None:
        parts.append(f"seed {seed:.0f}")
    why = str(comp.get("why") or "").strip()
    if why:
        parts.append(f"— {why}")
    return " ".join(parts)


def format_ai_note_message(row: dict[str, Any], latest_sales: dict[str, Any], note: dict[str, Any]) -> str:
    latest_source = str(latest_sales.get("source") or "unknown")
    source_map = {
        "network": "fresh CSFloat sales",
        "fresh_cache": "cached latest sales",
        "stale_cache": "stale cached sales",
    }
    lines = [
        "<b>AI note</b>",
        f"Verdict: <b>{html.escape(str(note.get('verdict') or 'MAYBE'))}</b> / {html.escape(str(note.get('confidence') or 'medium'))}",
        (
            "Risk / liquidity: "
            f"<code>risk={html.escape(str(note.get('risk_level') or 'medium'))}</code> "
            f"<code>item_liq={html.escape(str(note.get('item_liquidity') or 'medium'))}</code> "
            f"<code>float_liq={html.escape(str(note.get('float_liquidity') or 'medium'))}</code>"
        ),
        (
            "Model / fast -15%: "
            f"<code>models={html.escape(str(note.get('model_agreement') or 'mixed'))}</code> "
            f"<code>target_15_fast={html.escape(str(note.get('target_15pct_fast') or 'maybe'))}</code>"
        ),
        f"Sales source: <code>{html.escape(source_map.get(latest_source, latest_source))}</code>",
        f"Fast sale: <code>{html.escape(_fmt_range(note.get('fast_sale_range_eur')))}</code>",
        f"Realistic: <code>{html.escape(_fmt_range(note.get('realistic_sale_range_eur')))}</code>",
        f"Patient: <code>{html.escape(_fmt_range(note.get('patient_sale_range_eur')))}</code>",
        f"Start listing: <code>{html.escape(_fmt_range(note.get('start_listing_range_eur')))}</code>",
        f"Fast floor: <code>{html.escape(_fmt_range(note.get('fast_floor_range_eur')))}</code>",
        f"Fast net exit: <code>{html.escape(_fmt_pct_range(note.get('fast_net_exit_pct')))}</code>",
        f"Realistic net exit: <code>{html.escape(_fmt_pct_range(note.get('realistic_net_exit_pct')))}</code>",
        f"Patient net exit: <code>{html.escape(_fmt_pct_range(note.get('patient_net_exit_pct')))}</code>",
        "",
        "<b>Breakeven math</b>",
        f"Gross 0%: <code>{html.escape(_fmt_money(note.get('breakeven_gross_eur')))}</code>",
        f"Gross -5%: <code>{html.escape(_fmt_money(note.get('gross_for_minus_5pct')))}</code>",
        f"Gross -10%: <code>{html.escape(_fmt_money(note.get('gross_for_minus_10pct')))}</code>",
        f"Gross -15%: <code>{html.escape(_fmt_money(note.get('gross_for_minus_15pct')))}</code>",
    ]
    comps = note.get("best_comps") or []
    if comps:
        lines.extend(["", "<b>Relevant comps</b>"])
        for comp in comps[:3]:
            if isinstance(comp, dict):
                lines.append(f"• {html.escape(_fmt_comp(comp))}")
    risks = note.get("risks") or []
    if risks:
        lines.extend(["", "<b>Risks</b>"])
        for risk in risks[:3]:
            text = str(risk).strip()
            if text:
                lines.append(f"• {html.escape(text)}")
    summary = str(note.get("summary") or "").strip()
    if summary:
        lines.extend(["", html.escape(summary)])
    return "\n".join(lines)[:4000]


def run_enrichment_job(job_json: Path, config: dict[str, Any], *, dry_run: bool = False) -> bool:
    configure_stdio()
    _load_secrets_file()
    cfg = load_enrichment_config(config)
    if not cfg.enabled:
        print("alert enrichment disabled")
        return False
    payload = json.loads(job_json.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError(f"Malformed enrichment job: {job_json}")
    row = sanitize_row(payload.get("row") or {})
    item = str(row.get("item") or "").strip()
    if not item:
        raise RuntimeError("Enrichment job row is missing item")
    job_dir = job_json.parent
    append_status(job_dir, "started", started_at_utc=utc_now_iso())
    try:
        latest_sales = load_latest_sales(item, cfg, job_dir=job_dir)
        note = call_gemini(row, latest_sales, cfg, job_dir=job_dir)
        message = format_ai_note_message(row, latest_sales, note)
        write_json(
            job_dir / "result.json",
            {
                "sent_at_utc": None,
                "item": item,
                "listing_id": row.get("listing_id"),
                "latest_sales_source": latest_sales.get("source"),
                "ai_note": note,
                "message_preview": message,
            },
        )
        if dry_run:
            print(message)
            append_status(job_dir, "dry_run_ok", finished_at_utc=utc_now_iso())
            return True
        from automation.telegram_alerts import send_message, telegram_credentials

        token, chat = telegram_credentials(chat_id=str(payload.get("chat_id") or "") or None)
        sent = send_message(
            message,
            bot_token=token,
            chat_id=chat,
            timeout=cfg.telegram_timeout_sec,
            reply_to_message_id=payload.get("primary_message_id"),
        )
        result_path = job_dir / "result.json"
        result_payload = json.loads(result_path.read_text(encoding="utf-8"))
        result_payload["sent_at_utc"] = utc_now_iso()
        result_payload["telegram_result"] = _json_safe(sent)
        write_json(result_path, result_payload)
        append_status(
            job_dir,
            "sent",
            finished_at_utc=utc_now_iso(),
            latest_sales_source=latest_sales.get("source"),
            latest_sales_count=len(latest_sales.get("sales_rows") or []),
        )
        print(f"alert enrichment sent for {item}")
        return True
    except Exception as exc:
        write_json(
            job_dir / "failure.json",
            {
                "failed_at_utc": utc_now_iso(),
                "item": item,
                "listing_id": row.get("listing_id"),
                "error": str(exc),
            },
        )
        append_status(job_dir, "failed", error=str(exc), finished_at_utc=utc_now_iso())
        print(f"alert enrichment failed for {item}: {exc}", file=sys.stderr)
        return False
