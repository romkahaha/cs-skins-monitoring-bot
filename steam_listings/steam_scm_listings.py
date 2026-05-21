"""
Steam Community Market: листинги /render/ + float из asset_properties, цена ask.

В ``listinginfo`` поле ``converted_price`` — сумма **к получению продавцом** (в минорных единицах
валюты кошелька). В клиенте Steam покупатель видит **итог с комиссией** (CS2 ≈ +15%):
``converted_price + converted_fee``. Колонка ``ask`` — сумма покупателя (как в клиенте).

Базовые значения — словарь CONFIG в этом файле. Их перекрывает **steam_scm_runtime.json**
(рядом со скриптом) или путь в env **STEAM_SCM_RUNTIME_CONFIG**: файл перечитывается при
изменении на диске (mtime), в том числе во время длинного батча — можно крутить паузы
без перезапуска процесса. См. steam_scm_runtime.example.json.

Список имён для батча по умолчанию читается **из файла** ``lists/screening_sub.py`` (корень репо
на уровень выше ``steam_listings/``), переменная ``ITEMS`` — без ``import lists`` и без PYTHONPATH.
Иначе: свой путь в ``items_py_path`` (JSON/CLI ``--items-py``). Только ``items_module``: в JSON
``"items_py_path": "-"`` (без импорта по файлу).

Программно: ``CONFIG.update(...)`` задаёт базу; JSON перекрывает при каждом чтении параметра.

CLI:
  python steam_scm_listings.py "AK-47 | Redline (Field-Tested)"
  python steam_scm_listings.py --batch --out data/scm.csv
"""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import json
import os
from json import JSONDecodeError
from decimal import ROUND_HALF_UP, Decimal
import random
import sys
import threading
import time
from datetime import datetime, timezone
import urllib.parse
from pathlib import Path
from typing import Any

import requests

# Корень репозитория: .../steam_listings/steam_scm_listings.py → родитель родителя
_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_ITEMS_PY = str(_REPO_ROOT / "lists" / "screening_sub.py")

# =============================================================================
# Дефолты (если ключа нет в steam_scm_runtime.json)
# =============================================================================
CONFIG: dict[str, Any] = {
    # Один запрос к .../render/ (Steam отдаёт максимум 100 листингов за вызов)
    "listings_per_request": 100,
    # currency= в URL: 1 USD, 2 GBP, 3 EUR (см. Steam ECurrency)
    "steam_currency": 3,
    "request_timeout_sec": 45.0,
    # Повторы одного и того же запроса при сетевой ошибке / success=false / пустой listinginfo
    "retry_attempts": 3,
    "retry_sleep_min_sec": 2.0,
    "retry_sleep_max_sec": 5.0,
    # Между разными скинами в батче (чтобы не ловить временный бан)
    "delay_between_skins_min_sec": 4.0,
    "delay_between_skins_max_sec": 10.0,
    # Батч: список имён. Если items_py_path непустой — читаем этот .py (без import по имени модуля)
    "items_module": "lists.skins_normal",
    "items_variable": "ITEMS",
    "items_py_path": _DEFAULT_ITEMS_PY,
    # Listing fetches must stay public/cookie-less. Steam cookies are for risk/pricehistory,
    # not for monitoring listings; logged-in cookies can make routeAction redirect to /market/.
    "steam_cookies": "",
    "use_steam_cookies_for_listings": False,
    # Steam SSR route action id for the Market listing Search action.
    # If Steam rotates route ids again, this can be overridden in steam_scm_runtime.json.
    "steam_market_route_id": "4OPT6VBA",
    # Steam's old /render endpoint now often returns a large HTML page. Prefer the
    # lighter routeAction JSON response and keep /render only as a non-429 fallback.
    "prefer_route_action": False,
    "route_action_fallback_to_render": True,
    # Куда писать CSV при --batch
    "batch_out_csv": "data/scm_listings_batch.csv",
    # Ограничить число скинов с начала списка (None = все)
    "batch_max_skins": None,
    # Float из API: знаков после запятой (убрать «хвост» точности)
    "float_decimal_places": 14,
    # Сколько листингов собрать на один market_hash_name (несколько запросов по ≤100)
    # None — один запрос, объём = min(listings_per_request, 100)
    "max_listings_per_skin": None,
    # Пауза между страницами /render/ (start=0,100,…) при max_listings_per_skin > 100
    "delay_between_render_pages_min_sec": 2.0,
    "delay_between_render_pages_max_sec": 5.0,
    # 1 = print прогресс батча / ретраи / паузы
    "batch_log_progress": 1,
    # Stop deep paging once the next page starts above first_ask * multiplier.
    # None/0 disables the cutoff.
    "tail_stop_ask_multiplier": None,
    # 1 = колонка asset_properties_json (все свойства предмета, может раздувать CSV)
    "include_asset_properties_json": 0,
}

APP_ID = 730
CONTEXT_ID = "2"
FLOAT_PROPERTY_ID = 2
# Pattern / paint seed в asset_properties (CS2): «Pattern Template» — propertyid 1, int_value.
# Не путать с propertyid 6 (Item Certificate — длинная hex string_value, не seed).
_PATTERN_NAME_HINTS = ("pattern", "template", "seed")
_PATTERN_PROPERTY_ID = 1
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

_RUNTIME_LOCK = threading.Lock()
_runtime_mtime: float | None = None
_runtime_data: dict = {}
_runtime_warned_missing: bool = False
_runtime_loaded_path: str | None = None


class SteamRateLimitError(RuntimeError):
    """Raised when Steam starts returning HTTP 429 for render requests."""

_INT_KEYS = frozenset(
    {
        "listings_per_request",
        "steam_currency",
        "retry_attempts",
        "float_decimal_places",
        "max_listings_per_skin",
        "batch_log_progress",
        "include_asset_properties_json",
    }
)


def _runtime_config_path() -> str:
    env = (os.environ.get("STEAM_SCM_RUNTIME_CONFIG") or "").strip()
    if env:
        return os.path.abspath(os.path.expanduser(env))
    return str(Path(__file__).resolve().parent / "steam_scm_runtime.json")


def _load_runtime_config() -> dict:
    """Следующий вызов после сохранения JSON подхватит новые значения (mtime)."""
    global _runtime_mtime, _runtime_data, _runtime_warned_missing, _runtime_loaded_path
    path = _runtime_config_path()
    if path != _runtime_loaded_path:
        _runtime_loaded_path = path
        _runtime_mtime = None
    try:
        mtime = os.stat(path).st_mtime
    except OSError:
        if not _runtime_warned_missing:
            _runtime_warned_missing = True
            ex = Path(__file__).resolve().parent / "steam_scm_runtime.example.json"
            print(
                f"  [steam_scm] нет {path} — только CONFIG в коде "
                f"(пример: {ex} → steam_scm_runtime.json)",
                flush=True,
            )
        return {}
    with _RUNTIME_LOCK:
        if _runtime_mtime is not None and mtime == _runtime_mtime:
            return _runtime_data
        try:
            with open(path, encoding="utf-8") as f:
                raw = json.load(f)
            if not isinstance(raw, dict):
                raw = {}
            _runtime_data = raw
            _runtime_mtime = mtime
        except json.JSONDecodeError as e:
            print(
                f"  [steam_scm] {path}: JSON битый — оставляем предыдущие значения ({e})",
                flush=True,
            )
            _runtime_mtime = mtime
        return _runtime_data


def _effective(key: str, override: Any = None) -> Any:
    """Дефолт из CONFIG; перекрытие из steam_scm_runtime.json (если ключ есть и не __*)."""
    if override is not None:
        return override
    base = CONFIG.get(key)
    rt = _load_runtime_config()
    if not isinstance(rt, dict) or key not in rt or str(key).startswith("__"):
        return base
    v = rt[key]
    if v is None:
        if key == "batch_max_skins":
            return None
        return base
    if key == "batch_max_skins":
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return base
    if key in _INT_KEYS:
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return base
    if key in (
        "items_module",
        "items_variable",
        "items_py_path",
        "batch_out_csv",
        "steam_cookies",
        "steam_market_route_id",
    ):
        if key == "items_py_path" and (v is None or str(v).strip() == ""):
            return base
        return str(v)
    try:
        return float(v)
    except (TypeError, ValueError):
        return base


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": DEFAULT_UA, "Accept-Language": "en-US,en;q=0.9"})
    if bool(_effective("use_steam_cookies_for_listings")):
        raw = (_effective("steam_cookies") or os.environ.get("STEAM_COOKIES") or "").strip()
        if raw:
            s.headers["Cookie"] = raw
    return s


def _listing_path(market_hash_name: str) -> str:
    seg = urllib.parse.quote(market_hash_name, safe="")
    return f"https://steamcommunity.com/market/listings/{APP_ID}/{seg}/render/"


def _asset_map(assets_blob: Any) -> dict[str, dict]:
    if not isinstance(assets_blob, dict):
        return {}
    app = assets_blob.get(str(APP_ID))
    if not isinstance(app, dict):
        return {}
    ctx = app.get(CONTEXT_ID)
    if not isinstance(ctx, dict):
        return {}
    out: dict[str, dict] = {}
    for aid, adata in ctx.items():
        if isinstance(adata, dict):
            out[str(aid)] = adata
    return out


def _batch_log(msg: str) -> None:
    try:
        if int(float(_effective("batch_log_progress"))) == 0:
            return
    except (TypeError, ValueError):
        return
    print(msg, flush=True)


def _paint_seed_from_asset(asset: dict) -> int | None:
    """Paint seed / pattern index из asset_properties (если Steam отдал в /render/)."""
    for p in asset.get("asset_properties") or []:
        if not isinstance(p, dict):
            continue
        name = str(p.get("name") or "").lower()
        if not any(h in name for h in _PATTERN_NAME_HINTS):
            continue
        if "sticker" in name or "patch" in name or "keychain" in name:
            continue
        raw = p.get("int_value")
        if raw is None or raw == "":
            continue
        try:
            return int(raw)
        except (TypeError, ValueError):
            try:
                return int(float(str(raw).replace(",", ".").strip()))
            except (TypeError, ValueError):
                continue
    for p in asset.get("asset_properties") or []:
        if not isinstance(p, dict):
            continue
        if p.get("propertyid") != _PATTERN_PROPERTY_ID:
            continue
        raw = p.get("int_value")
        if raw is None or raw == "":
            raw = p.get("string_value")
        if raw is None or raw == "":
            continue
        try:
            return int(raw)
        except (TypeError, ValueError):
            try:
                return int(float(str(raw).replace(",", ".").strip()))
            except (TypeError, ValueError):
                continue
    return None


def _asset_properties_json_column(asset: dict) -> str | None:
    if int(_effective("include_asset_properties_json")) == 0:
        return None
    props = asset.get("asset_properties") or []
    slim: list[dict[str, Any]] = []
    for p in props:
        if not isinstance(p, dict):
            continue
        slim.append(
            {
                "propertyid": p.get("propertyid"),
                "name": p.get("name"),
                "int_value": p.get("int_value"),
                "float_value": p.get("float_value"),
                "string_value": p.get("string_value"),
            }
        )
    return json.dumps(slim, ensure_ascii=False) if slim else None


def _float_from_asset(asset: dict) -> float | None:
    for p in asset.get("asset_properties") or []:
        if not isinstance(p, dict):
            continue
        if p.get("propertyid") != FLOAT_PROPERTY_ID:
            continue
        v = p.get("float_value")
        if v is None or v == "":
            return None
        try:
            places = int(_effective("float_decimal_places"))
            places = max(0, min(places, 20))
            raw = str(v).strip()
            q = Decimal("1e-" + str(places))
            d = Decimal(raw).quantize(q, rounding=ROUND_HALF_UP)
            return float(d)
        except (TypeError, ValueError, ArithmeticError):
            try:
                return float(v)
            except (TypeError, ValueError):
                return None
    return None


def _iter_listings(listinginfo: Any) -> list[tuple[str, dict]]:
    if isinstance(listinginfo, dict):
        return [(str(k), v) for k, v in listinginfo.items() if isinstance(v, dict)]
    if isinstance(listinginfo, list):
        out: list[tuple[str, dict]] = []
        for x in listinginfo:
            if not isinstance(x, dict):
                continue
            lid = x.get("listingid")
            if lid is not None:
                out.append((str(lid), x))
        return out
    return []


def _positive_minor_units(value: Any) -> bool:
    try:
        return int(value) > 0
    except (TypeError, ValueError):
        return False


def parse_render_payload(data: dict) -> list[dict[str, Any]]:
    if not data.get("success"):
        return []
    amap = _asset_map(data.get("assets"))
    rows: list[dict[str, Any]] = []
    for listing_id, info in _iter_listings(data.get("listinginfo")):
        asset_part = info.get("asset") or {}
        aid = str(asset_part.get("id") or "")
        asset = amap.get(aid) or {}
        ccy = info.get("converted_currencyid")
        cprice = info.get("converted_price")
        cfee = info.get("converted_fee")
        if cfee is None and info.get("fee") is not None:
            cfee = info.get("fee")
        if not _positive_minor_units(cprice):
            continue
        row: dict[str, Any] = {
            "listing_id": listing_id,
            "asset_id": aid or None,
            "converted_price": cprice,
            "converted_fee": cfee,
            "converted_currencyid": ccy,
            "float_value": _float_from_asset(asset),
            "paint_seed": _paint_seed_from_asset(asset),
            "asset_properties_json": _asset_properties_json_column(asset),
            "market_hash_name": asset.get("market_hash_name"),
        }
        rows.append(row)
    return rows


def _route_action_payload_to_render_payload(data: dict | None) -> dict:
    """Normalize Steam's new SSR Market Search action response to the old /render/ shape."""
    if not isinstance(data, dict):
        raise RuntimeError(f"Steam Market Search action returned unexpected payload type: {type(data).__name__}")
    listings = data.get("listings")
    if not isinstance(listings, list):
        keys = ",".join(sorted(str(k) for k in data.keys())[:12])
        raise RuntimeError(f"Steam Market Search action returned payload without listings list; keys={keys}")

    listinginfo: list[dict[str, Any]] = []
    assets: dict[str, dict[str, dict[str, dict]]] = {str(APP_ID): {CONTEXT_ID: {}}}
    asset_bucket = assets[str(APP_ID)][CONTEXT_ID]

    for listing in listings:
        if not isinstance(listing, dict):
            continue
        listing_id = str(listing.get("listingid") or "")
        asset = listing.get("asset") if isinstance(listing.get("asset"), dict) else {}
        desc = listing.get("description") if isinstance(listing.get("description"), dict) else {}
        asset_id = str(asset.get("assetid") or asset.get("id") or "")
        if not listing_id or not asset_id:
            continue

        enriched_asset = dict(asset)
        for key in (
            "market_hash_name",
            "market_name",
            "name",
            "market_actions",
            "commodity",
            "type",
            "name_color",
        ):
            if key in desc and key not in enriched_asset:
                enriched_asset[key] = desc.get(key)
        asset_bucket[asset_id] = enriched_asset

        e_currency = listing.get("eCurrency")
        converted_currencyid = None
        try:
            converted_currencyid = 2000 + int(e_currency)
        except (TypeError, ValueError):
            pass
        converted_price = listing.get("unPricePerUnit", listing.get("unPrice"))
        if not _positive_minor_units(converted_price):
            continue

        listinginfo.append(
            {
                "listingid": listing_id,
                "converted_price": converted_price,
                "converted_fee": listing.get("unFeePerUnit", listing.get("unFee")),
                "converted_currencyid": converted_currencyid,
                "asset": {"id": asset_id},
            }
        )

    return {
        "success": True,
        "more": bool(data.get("more")),
        "start": data.get("start"),
        "total_count": data.get("total_count"),
        "listinginfo": listinginfo,
        "assets": assets,
        "__source": "steam_market_route_action",
    }


def fetch_market_search_raw(
    market_hash_name: str,
    *,
    start: int = 0,
    count: int | None = None,
    currency: int | None = None,
    session: requests.Session | None = None,
    timeout: float | None = None,
) -> dict:
    count = count if count is not None else int(_effective("listings_per_request"))
    timeout = timeout if timeout is not None else float(_effective("request_timeout_sec"))
    sess = session or _session()
    seg = urllib.parse.quote(market_hash_name, safe="")
    url = f"https://steamcommunity.com/market/listings/{APP_ID}/{seg}"
    route_id = str(_effective("steam_market_route_id") or "").strip()
    if not route_id:
        raise RuntimeError("steam_market_route_id is empty")
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json; charset=utf-8",
        "x-valve-request-type": "routeAction",
        "x-valve-action-type": f"{route_id}:Search",
        "Referer": url,
    }
    query = {
        "appid": APP_ID,
        "strItemName": market_hash_name,
        "filters": {},
        "accessoryFilters": {},
        "propertyFilters": {},
        "disableGrouping": True,
        "start": int(start),
        "count": min(max(1, int(count)), 100),
    }
    if currency is not None:
        query["currency"] = int(currency)
    r = sess.post(url, headers=headers, data=json.dumps([query]), timeout=timeout)
    r.raise_for_status()
    try:
        payload = r.json()
    except (ValueError, JSONDecodeError):
        _batch_log(
            f'  [steam_scm] "{market_hash_name}": Steam Market Search action returned non-JSON '
            f"({r.headers.get('content-type')}; final_url={r.url})"
        )
        raise RuntimeError(
            f"Steam Market Search action returned non-JSON ({r.headers.get('content-type')}; final_url={r.url})"
        )
    return _route_action_payload_to_render_payload(payload)


def fetch_render_raw(
    market_hash_name: str,
    *,
    start: int = 0,
    count: int | None = None,
    currency: int | None = None,
    session: requests.Session | None = None,
    timeout: float | None = None,
) -> dict:
    count = count if count is not None else int(_effective("listings_per_request"))
    currency = currency if currency is not None else int(_effective("steam_currency"))
    timeout = timeout if timeout is not None else float(_effective("request_timeout_sec"))
    sess = session or _session()
    prefer_route_action = bool(_effective("prefer_route_action"))
    if prefer_route_action:
        try:
            _batch_log(
                f'  [steam_scm] "{market_hash_name}": routeAction start={start} count={min(int(count), 100)}'
            )
            return fetch_market_search_raw(
                market_hash_name,
                start=start,
                count=count,
                currency=currency,
                session=sess,
                timeout=timeout,
            )
        except requests.HTTPError:
            raise
        except Exception as exc:
            if not bool(_effective("route_action_fallback_to_render")):
                raise
            _batch_log(
                f'  [steam_scm] "{market_hash_name}": routeAction failed ({exc}) — trying /render/'
            )

    url = _listing_path(market_hash_name)
    params = {
        "query": "",
        "start": start,
        "count": min(int(count), 100),
        "currency": currency,
        "language": "english",
        "format": "json",
    }
    seg = urllib.parse.quote(market_hash_name, safe="")
    sess.headers["Referer"] = f"https://steamcommunity.com/market/listings/{APP_ID}/{seg}"
    r = sess.get(url, params=params, timeout=timeout)
    r.raise_for_status()
    try:
        return r.json()
    except (ValueError, JSONDecodeError) as exc:
        _batch_log(
            f'  [steam_scm] "{market_hash_name}": /render/ returned non-JSON '
            f"({r.headers.get('content-type')}; final_url={r.url}) — trying Steam Market Search action"
        )
        try:
            return fetch_market_search_raw(
                market_hash_name,
                start=start,
                count=count,
                currency=currency,
                session=sess,
                timeout=timeout,
            )
        except Exception as fallback_exc:
            raise RuntimeError(
                f"Steam /render/ returned non-JSON and Market Search action fallback failed: {fallback_exc}"
            ) from exc



def _minor_units_major(
    minor_total: int, converted_currencyid: Any
) -> float | None:
    """minor (центах/евроцентах) → основные единицы для id 2001–2010."""
    cid = int(converted_currencyid) if converted_currencyid is not None else None
    if cid in (2001, 2002, 2003, 2004, 2005, 2006, 2007, 2008, 2009, 2010):
        return round(minor_total / 100.0, 2)
    return None


def _seller_net_major_units(converted_price: Any, converted_currencyid: Any) -> float | None:
    """Только net продавца: ``converted_price`` / 100 (без комиссий)."""
    if converted_price is None:
        return None
    try:
        minor = int(converted_price)
    except (TypeError, ValueError):
        return None
    return _minor_units_major(minor, converted_currencyid)


def _buyer_pays_major_units(
    converted_price: Any,
    converted_currencyid: Any,
    converted_fee: Any,
) -> float | None:
    """
    Сумма покупателя в валюте кошелька: (converted_price + converted_fee) / 100.
    Совпадает с отображением в клиенте Steam (цена с комиссией).
    """
    if converted_price is None:
        return None
    try:
        p = int(converted_price)
        f = int(converted_fee) if converted_fee is not None else 0
    except (TypeError, ValueError):
        return None
    return _minor_units_major(p + f, converted_currencyid)


def fetch_steam_scm_top_listings(
    market_hash_name: str,
    *,
    limit: int | None = None,
    max_listings: int | None = None,
    currency: int | None = None,
    session: requests.Session | None = None,
    retry_attempts: int | None = None,
    retry_sleep_min_sec: float | None = None,
    retry_sleep_max_sec: float | None = None,
    log_skin_label: str | None = None,
    tail_stop_ask_multiplier: float | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """
    Листинги с /render/: не больше 100 за один HTTP-запрос; при ``max_listings_per_skin`` > 100
    делается несколько запросов (start=0, 100, 200, …), пауза из JSON между страницами.
    """
    chunk = limit if limit is not None else int(_effective("listings_per_request"))
    chunk = max(1, min(int(chunk), 100))

    cap_raw = max_listings if max_listings is not None else _effective("max_listings_per_skin")
    if cap_raw is None:
        total_cap = chunk
    else:
        try:
            ci = int(cap_raw)
        except (TypeError, ValueError):
            total_cap = chunk
        else:
            total_cap = chunk if ci <= 0 else ci

    tries = retry_attempts if retry_attempts is not None else int(_effective("retry_attempts"))
    tries = max(1, tries)
    cur = currency if currency is not None else int(_effective("steam_currency"))
    label = log_skin_label or market_hash_name
    tail_raw = tail_stop_ask_multiplier if tail_stop_ask_multiplier is not None else _effective("tail_stop_ask_multiplier")
    try:
        tail_multiplier = float(tail_raw) if tail_raw is not None else 0.0
    except (TypeError, ValueError):
        tail_multiplier = 0.0
    if tail_multiplier <= 0:
        tail_multiplier = 0.0
    tail_first_ask: float | None = None

    def _retry_sleep() -> tuple[float, float]:
        lo = float(retry_sleep_min_sec if retry_sleep_min_sec is not None else _effective("retry_sleep_min_sec"))
        hi = float(retry_sleep_max_sec if retry_sleep_max_sec is not None else _effective("retry_sleep_max_sec"))
        return lo, hi

    def _is_rate_limited(err_text: str | None) -> bool:
        text = str(err_text or "").lower()
        return "429" in text or "too many requests" in text

    meta: dict[str, Any] = {
        "total_count": None,
        "success": False,
        "note": None,
        "pages_fetched": 0,
        "listings_target_cap": total_cap,
        "source": None,
    }
    sess = session or _session()
    merged: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    start_offset = 0

    def _buyer_minor(r: dict[str, Any]) -> int:
        if r.get("converted_price") is None:
            return 0
        try:
            p = int(r["converted_price"])
            f = int(r["converted_fee"]) if r.get("converted_fee") is not None else 0
            return p + f
        except (TypeError, ValueError):
            return 0

    while len(merged) < total_cap:
        remaining = total_cap - len(merged)
        request_count = min(chunk, 100)
        if chunk < 100:
            request_count = min(request_count, remaining)
        last_err: str | None = None
        data: dict[str, Any] | None = None
        for attempt in range(tries):
            try:
                request_mode = "routeAction" if bool(_effective("prefer_route_action")) else "render"
                _batch_log(
                    f'  [steam_scm] "{label}": {request_mode} start={start_offset} count={request_count} '
                    f"(got {len(merged)}/{total_cap})"
                )
                data = fetch_render_raw(
                    market_hash_name,
                    start=start_offset,
                    count=request_count,
                    currency=cur,
                    session=sess,
                )
            except requests.RequestException as e:
                last_err = str(e)
                if _is_rate_limited(last_err):
                    _batch_log(
                        f'  [steam_scm] "{label}": immediate batch abort on Steam rate limit: {last_err}'
                    )
                    raise SteamRateLimitError(f'Steam rate limit (429) for "{label}": {last_err}') from e
                if attempt + 1 < tries:
                    lo, hi = _retry_sleep()
                    w = random.uniform(lo, hi)
                    _batch_log(
                        f'  [steam_scm] "{label}": retry {attempt + 1}/{tries}, '
                        f"sleep {w:.1f}s (retry_sleep_*): {last_err}"
                    )
                    time.sleep(w)
                continue
            except Exception as e:
                last_err = str(e)
                if _is_rate_limited(last_err):
                    _batch_log(
                        f'  [steam_scm] "{label}": immediate batch abort on Steam rate limit: {last_err}'
                    )
                    raise SteamRateLimitError(f'Steam rate limit (429) for "{label}": {last_err}') from e
                if attempt + 1 < tries:
                    lo, hi = _retry_sleep()
                    w = random.uniform(lo, hi)
                    _batch_log(
                        f'  [steam_scm] "{label}": retry {attempt + 1}/{tries}, '
                        f"sleep {w:.1f}s (steam transient): {last_err}"
                    )
                    time.sleep(w)
                continue
            if not data.get("success"):
                last_err = "success=false"
                if attempt + 1 < tries:
                    lo, hi = _retry_sleep()
                    w = random.uniform(lo, hi)
                    _batch_log(
                        f'  [steam_scm] "{label}": retry {attempt + 1}/{tries}, '
                        f"sleep {w:.1f}s: {last_err}"
                    )
                    time.sleep(w)
                continue
            break
        else:
            # Все попытки страницы провалились — не выкидываем уже собранные с прошлых страниц
            meta["note"] = last_err or "failed"
            if merged:
                meta["partial_fetch"] = True
                meta["last_page_failed"] = last_err
                _batch_log(
                    f'  [steam_scm] "{label}": страница start={start_offset} не загрузилась после ретраев '
                    f"({last_err!r}) — сохраняем уже собранные {len(merged)} строк"
                )
                break
            meta["last_page_failed"] = last_err
            return [], meta

        assert data is not None
        meta["success"] = True
        if data.get("total_count") is not None:
            meta["total_count"] = data.get("total_count")
        if data.get("__source") is not None:
            meta["source"] = data.get("__source")
        lis = _iter_listings(data.get("listinginfo"))
        if not lis:
            if (data.get("total_count") or 0) == 0:
                meta["note"] = "no_offers"
                break
            last_err = "empty listinginfo"
            meta["note"] = last_err
            break

        page_rows = parse_render_payload(data)
        meta["pages_fetched"] = int(meta.get("pages_fetched") or 0) + 1

        page_asks: list[float] = []
        for row in page_rows:
            ask_value = row.get("ask")
            if ask_value is None:
                ask_value = _buyer_pays_major_units(
                    row.get("converted_price"),
                    row.get("converted_currencyid"),
                    row.get("converted_fee"),
                )
            try:
                ask = float(ask_value)
            except (TypeError, ValueError):
                continue
            if ask > 0:
                page_asks.append(ask)
        if page_asks and tail_first_ask is None:
            tail_first_ask = min(page_asks)
            meta["tail_cutoff_first_ask"] = tail_first_ask
        if tail_multiplier and tail_first_ask and page_asks and start_offset > 0:
            page_min_ask = min(page_asks)
            cutoff_ask = tail_first_ask * tail_multiplier
            if page_min_ask > cutoff_ask:
                meta["tail_cutoff"] = True
                meta["tail_cutoff_multiplier"] = tail_multiplier
                meta["tail_cutoff_ask"] = page_min_ask
                meta["tail_cutoff_limit_ask"] = cutoff_ask
                _batch_log(
                    f'  [steam_scm] "{label}": tail cutoff at start={start_offset} '
                    f"ask={page_min_ask:.2f} first_ask={tail_first_ask:.2f} multiplier={tail_multiplier:.2f}"
                )
                break

        for row in page_rows:
            lid = str(row.get("listing_id") or "")
            if not lid or lid in seen_ids:
                continue
            seen_ids.add(lid)
            merged.append(row)
            if len(merged) >= total_cap:
                break

        if not page_rows:
            break
        if len(page_rows) < request_count and not data.get("more"):
            break
        if len(merged) >= total_cap:
            break

        start_offset += len(lis)
        if len(merged) < total_cap:
            d_lo = float(_effective("delay_between_render_pages_min_sec"))
            d_hi = float(_effective("delay_between_render_pages_max_sec"))
            w = random.uniform(d_lo, d_hi)
            _batch_log(
                f'  [steam_scm] "{label}": пауза между страницами {w:.1f}s '
                f"(delay_between_render_pages_*; следующий start={start_offset})"
            )
            time.sleep(w)

    merged.sort(key=lambda r: (r.get("converted_price") is None, _buyer_minor(r)))
    for row in merged:
        row["ask_seller_net"] = _seller_net_major_units(
            row.get("converted_price"), row.get("converted_currencyid")
        )
        row["ask"] = _buyer_pays_major_units(
            row.get("converted_price"),
            row.get("converted_currencyid"),
            row.get("converted_fee"),
        )
    merged = [
        row
        for row in merged
        if row.get("ask") is not None and _positive_minor_units(row.get("converted_price"))
    ]
    return merged[:total_cap], meta


def load_items_from_module(
    module_name: str | None = None,
    variable: str | None = None,
    items_py_path: str | None = None,
) -> list[str]:
    var = variable or str(_effective("items_variable"))
    raw_path = items_py_path if items_py_path is not None else _effective("items_py_path")
    trimmed = str(raw_path or "").strip()
    force_module = False
    if trimmed.lower() in ("-", "__module__"):
        trimmed = ""
        force_module = True
    # Старый импорт модуля без ключа items_py_path в CONFIG → raw_path None; не падаем в import lists
    if not trimmed and not force_module and os.path.isfile(_DEFAULT_ITEMS_PY):
        trimmed = _DEFAULT_ITEMS_PY
    if trimmed:
        path = os.path.abspath(os.path.expanduser(trimmed))
        if not os.path.isfile(path):
            raise FileNotFoundError(
                f"items_py_path: file not found: {path}\n"
                f"  (clear items_py_path in CONFIG/runtime JSON to use items_module instead)"
            )
        mod_name = f"steam_scm_items_{Path(path).stem}"
        spec = importlib.util.spec_from_file_location(mod_name, path)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load items from {path!r}")
        m = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = m
        spec.loader.exec_module(m)
        items = getattr(m, var)
        src = f"{path}.{var}"
    else:
        mod = module_name or str(_effective("items_module"))
        m = importlib.import_module(mod)
        items = getattr(m, var)
        src = f"{mod}.{var}"
    if not isinstance(items, (list, tuple)):
        raise TypeError(f"{src} must be a list")
    return list(items)



def _parse_utc_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        text = str(value).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _age_minutes(value: Any, now: datetime) -> float | None:
    dt = _parse_utc_datetime(value)
    if dt is None:
        return None
    return max(0.0, (now - dt).total_seconds() / 60.0)


def _strategy_tier_minutes(strategy: dict[str, Any], key: str, tier: str | None, default: float) -> float:
    raw = strategy.get(key, default)
    if isinstance(raw, dict):
        raw = raw.get(str(tier or "default"), raw.get("default", default))
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return float(default)


def _strategy_int(strategy: dict[str, Any], key: str, default: int) -> int:
    try:
        return max(1, int(float(strategy.get(key, default))))
    except (TypeError, ValueError):
        return int(default)


def _strategy_float(strategy: dict[str, Any], key: str, default: float) -> float:
    try:
        return float(strategy.get(key, default))
    except (TypeError, ValueError):
        return float(default)


def _page0_fingerprint(rows: list[dict[str, Any]], meta: dict[str, Any], *, page0_limit: int) -> dict[str, Any]:
    listing_ids: list[str] = []
    asks: list[float] = []
    for row in rows[:page0_limit]:
        listing_id = str(row.get("listing_id") or "")
        if listing_id:
            listing_ids.append(listing_id)
        try:
            ask = float(row.get("ask"))
        except (TypeError, ValueError):
            continue
        if ask > 0:
            asks.append(ask)
    total_raw = meta.get("total_count")
    try:
        total_count = int(total_raw) if total_raw is not None else None
    except (TypeError, ValueError):
        total_count = None
    return {
        "page0_listing_ids": listing_ids,
        "total_count": total_count,
        "first_ask": min(asks) if asks else None,
    }


def _sentinel_change_reasons(entry: dict[str, Any], fp: dict[str, Any], strategy: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    if bool(strategy.get("deep_on_page0_ids_change", True)):
        if list(entry.get("page0_listing_ids") or []) != list(fp.get("page0_listing_ids") or []):
            reasons.append("page0_ids_changed")
    if bool(strategy.get("deep_on_total_count_change", True)):
        if entry.get("total_count") != fp.get("total_count"):
            reasons.append("total_count_changed")
    return reasons


def _update_listing_fetch_entry(
    entry: dict[str, Any],
    fp: dict[str, Any],
    *,
    now_iso: str,
    day_key: str | None,
    deep: bool,
    reason: str,
    pending: bool | None = None,
) -> None:
    entry["page0_listing_ids"] = list(fp.get("page0_listing_ids") or [])
    entry["total_count"] = fp.get("total_count")
    entry["first_ask"] = fp.get("first_ask")
    entry["last_page0_at_utc"] = now_iso
    if day_key:
        entry["day_key"] = day_key
    if deep:
        entry["last_deep_at_utc"] = now_iso
        entry["last_deep_reason"] = reason
        entry["deep_pending"] = False
        entry.pop("deep_pending_since_utc", None)
        entry.pop("deep_pending_reason", None)
    elif pending is not None:
        entry["deep_pending"] = bool(pending)
        if pending:
            entry.setdefault("deep_pending_since_utc", now_iso)
            entry["deep_pending_reason"] = reason


def _bump_stat(stats: dict[str, Any], key: str, reason: str | None = None) -> None:
    stats[key] = int(stats.get(key) or 0) + 1
    if reason:
        by_reason = stats.setdefault(f"{key}_by_reason", {})
        by_reason[reason] = int(by_reason.get(reason) or 0) + 1


def _fetch_monitoring_item(
    name: str,
    *,
    max_listings_per_item: int | None,
    session: requests.Session,
    log_skin_label: str,
    fetch_strategy: dict[str, Any] | None,
    fetch_entry: dict[str, Any] | None,
    tier: str | None,
    day_key: str | None,
    stats: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    strategy = fetch_strategy if isinstance(fetch_strategy, dict) else {}
    if not bool(strategy.get("enabled", False)):
        return fetch_steam_scm_top_listings(
            name,
            max_listings=max_listings_per_item,
            session=session,
            log_skin_label=log_skin_label,
        )

    entry = fetch_entry if isinstance(fetch_entry, dict) else {}
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    page0_limit = _strategy_int(strategy, "page0_listings", 20)
    periodic_minutes = _strategy_tier_minutes(strategy, "periodic_deep_minutes_by_tier", tier, 120.0)
    cooldown_minutes = _strategy_tier_minutes(strategy, "changed_deep_cooldown_minutes_by_tier", tier, 20.0)
    tail_multiplier = _strategy_float(strategy, "tail_stop_ask_multiplier", 3.0)

    def fetch_deep(reason: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        _batch_log(
            f'  [steam_scm] "{log_skin_label}": sentinel deep reason={reason} '
            f"cap={max_listings_per_item} tail_multiplier={tail_multiplier:.2f}"
        )
        rows, meta = fetch_steam_scm_top_listings(
            name,
            max_listings=max_listings_per_item,
            session=session,
            log_skin_label=log_skin_label,
            tail_stop_ask_multiplier=tail_multiplier,
        )
        fp = _page0_fingerprint(rows, meta, page0_limit=page0_limit)
        _update_listing_fetch_entry(entry, fp, now_iso=now_iso, day_key=day_key, deep=True, reason=reason)
        if meta.get("tail_cutoff"):
            _bump_stat(stats, "tail_cutoffs")
        _bump_stat(stats, "deep", reason)
        return rows, meta

    last_deep_age = _age_minutes(entry.get("last_deep_at_utc"), now)
    if not entry.get("last_deep_at_utc"):
        return fetch_deep("missing_sentinel")
    if bool(strategy.get("deep_on_new_day", True)) and day_key and entry.get("day_key") != day_key:
        return fetch_deep("new_day")
    if bool(entry.get("deep_pending")) and (last_deep_age is None or last_deep_age >= cooldown_minutes):
        return fetch_deep(str(entry.get("deep_pending_reason") or "pending_sentinel_changed"))
    if periodic_minutes > 0 and (last_deep_age is None or last_deep_age >= periodic_minutes):
        return fetch_deep(f"periodic_{periodic_minutes:g}m")

    _batch_log(f'  [steam_scm] "{log_skin_label}": sentinel page0 check limit={page0_limit}')
    rows, meta = fetch_steam_scm_top_listings(
        name,
        max_listings=page0_limit,
        session=session,
        log_skin_label=log_skin_label,
    )
    fp = _page0_fingerprint(rows, meta, page0_limit=page0_limit)
    reasons = _sentinel_change_reasons(entry, fp, strategy)
    if reasons:
        reason = "+".join(reasons)
        if last_deep_age is None or last_deep_age >= cooldown_minutes:
            return fetch_deep(f"sentinel_changed:{reason}")
        _batch_log(
            f'  [steam_scm] "{log_skin_label}": sentinel changed ({reason}) but deep cooldown active '
            f"age={last_deep_age:.1f}m cooldown={cooldown_minutes:.1f}m"
        )
        _update_listing_fetch_entry(entry, fp, now_iso=now_iso, day_key=day_key, deep=False, reason=reason, pending=True)
        _bump_stat(stats, "page0_changed_cooldown", reason)
    else:
        _batch_log(f'  [steam_scm] "{log_skin_label}": sentinel unchanged -> page0 only')
        _update_listing_fetch_entry(entry, fp, now_iso=now_iso, day_key=day_key, deep=False, reason="page0_unchanged")
        _bump_stat(stats, "page0_only", "unchanged")
    return rows, meta


def run_batch_to_csv(
    items: list[str],
    out_csv: str | Path | None = None,
    *,
    max_listings_per_item: int | None = None,
    session: requests.Session | None = None,
    fetch_strategy: dict[str, Any] | None = None,
    fetch_state: dict[str, Any] | None = None,
    tier: str | None = None,
    day_key: str | None = None,
) -> tuple[Path, list[dict[str, Any]], Any]:
    """
    Для каждого market_hash_name — запрос листингов; в каждой строке CSV колонка
    ``scm_total_listings`` (одно число на весь предмет, из ответа Steam ``total_count``).
    """
    import pandas as pd

    out = Path(out_csv or _effective("batch_out_csv"))
    out.parent.mkdir(parents=True, exist_ok=True)
    sess = session or _session()

    all_rows: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    n = len(items)
    strategy = fetch_strategy if isinstance(fetch_strategy, dict) else {}
    strategy_enabled = bool(strategy.get("enabled", False))
    state_root = fetch_state if isinstance(fetch_state, dict) else None
    item_fetch_state: dict[str, Any] = {}
    stats: dict[str, Any] = {
        "enabled": strategy_enabled,
        "tier": tier,
        "day_key": day_key,
        "items": n,
        "deep": 0,
        "page0_only": 0,
        "page0_changed_cooldown": 0,
        "tail_cutoffs": 0,
    }
    if state_root is not None:
        state_root["version"] = 1
        item_fetch_state = state_root.setdefault("items", {})
        state_root["last_batch_started_at_utc"] = datetime.now(timezone.utc).isoformat()
        state_root["last_batch_stats"] = stats

    for i, name in enumerate(items):
        t0 = time.monotonic()
        label = f"{i + 1}/{n} {name}"
        _batch_log(f'  [steam_scm] >> батч {label} (delay_between_skins_* после предмета)')
        entry = None
        if strategy_enabled and state_root is not None:
            raw_entry = item_fetch_state.get(name)
            if not isinstance(raw_entry, dict):
                raw_entry = {}
                item_fetch_state[name] = raw_entry
            entry = raw_entry
        try:
            rows, meta = _fetch_monitoring_item(
                name,
                max_listings_per_item=max_listings_per_item,
                session=sess,
                log_skin_label=label,
                fetch_strategy=strategy,
                fetch_entry=entry,
                tier=tier,
                day_key=day_key,
                stats=stats,
            )
        except Exception as exc:
            rows = []
            meta = {
                "success": False,
                "note": "exception",
                "error": str(exc),
                "pages_fetched": 0,
                "listings_target_cap": max_listings_per_item,
            }
            _batch_log(f'  [steam_scm] error батч {label}: {exc}')
        dt = time.monotonic() - t0
        _batch_log(
            f'  [steam_scm] ok батч {label}: {len(rows)} строк за {dt:.1f}s '
            f"(pages={meta.get('pages_fetched')}, cap={meta.get('listings_target_cap')})"
        )
        tc = meta.get("total_count")
        if not rows and meta.get("note") != "no_offers":
            errors.append({"market_hash_name": name, "meta": meta})
        for r in rows:
            all_rows.append(
                {
                    "market_hash_name": name,
                    "listing_id": r.get("listing_id"),
                    "asset_id": r.get("asset_id"),
                    "ask": r.get("ask"),
                    "ask_seller_net": r.get("ask_seller_net"),
                    "float_value": r.get("float_value"),
                    "paint_seed": r.get("paint_seed"),
                    "asset_properties_json": r.get("asset_properties_json"),
                    "converted_price": r.get("converted_price"),
                    "converted_fee": r.get("converted_fee"),
                    "converted_currencyid": r.get("converted_currencyid"),
                    "scm_total_listings": tc,
                }
            )
        if state_root is not None:
            state_root["last_batch_stats"] = stats
        if i + 1 < n:
            d_lo = float(_effective("delay_between_skins_min_sec"))
            d_hi = float(_effective("delay_between_skins_max_sec"))
            time.sleep(random.uniform(d_lo, d_hi))

    if state_root is not None:
        state_root["last_batch_finished_at_utc"] = datetime.now(timezone.utc).isoformat()
        state_root["last_batch_stats"] = stats
    df = pd.DataFrame(all_rows)
    df.to_csv(out, index=False)
    return out, errors, df

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Steam SCM listings + float")
    p.add_argument("market_hash_name", nargs="?", help="один предмет (без --batch)")
    p.add_argument("--batch", action="store_true", help="пройти ITEMS (items_py_path или items_module) и сохранить CSV")
    p.add_argument(
        "--items-py",
        default=None,
        metavar="PATH",
        help="путь к .py с ITEMS (как items_py_path в JSON; перекрывает до вызова load)",
    )
    p.add_argument("--out", default=None, help="путь CSV для --batch")
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="размер одного запроса /render/ (≤100, перекрывает listings_per_request)",
    )
    p.add_argument(
        "--max-listings",
        type=int,
        default=None,
        metavar="N",
        help="всего листингов на предмет; >100 — несколько запросов (как max_listings_per_skin в JSON)",
    )
    p.add_argument("--max-skins", type=int, default=None, help="только первые N имён из списка")
    p.add_argument("--json", action="store_true", help="JSON в stdout (один предмет)")
    args = p.parse_args(argv)

    if args.batch:
        if args.items_py is not None:
            CONFIG["items_py_path"] = args.items_py
        items = load_items_from_module()
        if args.max_skins is not None:
            items = items[: args.max_skins]
        elif _effective("batch_max_skins") is not None:
            items = items[: int(_effective("batch_max_skins"))]
        if args.limit is not None:
            CONFIG["listings_per_request"] = args.limit
        if args.max_listings is not None:
            CONFIG["max_listings_per_skin"] = args.max_listings
        path, errs, df = run_batch_to_csv(items, out_csv=args.out)
        print(f"saved: {path}  listing_rows={len(df)}  skins_with_errors={len(errs)}")
        if errs:
            print(f"errors: {len(errs)} skins (see stderr detail)")
            for e in errs[:20]:
                print(e)
        return 0

    if not args.market_hash_name:
        p.error("укажи market_hash_name или --batch")
    if args.limit is not None:
        CONFIG["listings_per_request"] = args.limit
    if args.max_listings is not None:
        CONFIG["max_listings_per_skin"] = args.max_listings
    rows, meta = fetch_steam_scm_top_listings(args.market_hash_name)
    if args.json:
        print(json.dumps({"meta": meta, "listings": rows}, indent=2))
        return 0 if meta.get("success") or meta.get("note") == "no_offers" else 2
    print("meta:", meta)
    print(f"rows: {len(rows)}")
    for r in rows[:15]:
        print(r)
    if len(rows) > 15:
        print(f"... and {len(rows) - 15} more")
    return 0 if rows or meta.get("note") == "no_offers" else 2


if __name__ == "__main__":
    sys.exit(main())
