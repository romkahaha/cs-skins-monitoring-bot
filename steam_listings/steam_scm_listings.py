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
from decimal import ROUND_HALF_UP, Decimal
import random
import sys
import threading
import time
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
    # Куки браузера (альтернатива — env STEAM_COOKIES)
    "steam_cookies": "",
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
    if key in ("items_module", "items_variable", "items_py_path", "batch_out_csv", "steam_cookies"):
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
    return r.json()


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

    def _retry_sleep() -> tuple[float, float]:
        lo = float(retry_sleep_min_sec if retry_sleep_min_sec is not None else _effective("retry_sleep_min_sec"))
        hi = float(retry_sleep_max_sec if retry_sleep_max_sec is not None else _effective("retry_sleep_max_sec"))
        return lo, hi

    meta: dict[str, Any] = {
        "total_count": None,
        "success": False,
        "note": None,
        "pages_fetched": 0,
        "listings_target_cap": total_cap,
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
        need = min(chunk, 100, total_cap - len(merged))
        last_err: str | None = None
        data: dict[str, Any] | None = None
        for attempt in range(tries):
            try:
                _batch_log(
                    f'  [steam_scm] "{label}": render start={start_offset} count={need} '
                    f"(got {len(merged)}/{total_cap})"
                )
                data = fetch_render_raw(
                    market_hash_name,
                    start=start_offset,
                    count=need,
                    currency=cur,
                    session=sess,
                )
            except requests.RequestException as e:
                last_err = str(e)
                if attempt + 1 < tries:
                    lo, hi = _retry_sleep()
                    w = random.uniform(lo, hi)
                    _batch_log(
                        f'  [steam_scm] "{label}": retry {attempt + 1}/{tries}, '
                        f"sleep {w:.1f}s (retry_sleep_*): {last_err}"
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
            return [], meta

        assert data is not None
        meta["success"] = True
        if data.get("total_count") is not None:
            meta["total_count"] = data.get("total_count")
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

        for row in page_rows:
            lid = str(row.get("listing_id") or "")
            if not lid or lid in seen_ids:
                continue
            seen_ids.add(lid)
            merged.append(row)
            if len(merged) >= total_cap:
                break

        if not page_rows or len(page_rows) < need:
            break
        if len(merged) >= total_cap:
            break

        start_offset += len(page_rows)
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


def run_batch_to_csv(
    items: list[str],
    out_csv: str | Path | None = None,
    *,
    session: requests.Session | None = None,
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
    for i, name in enumerate(items):
        t0 = time.monotonic()
        label = f"{i + 1}/{n} {name}"
        _batch_log(f'  [steam_scm] >> батч {label} (delay_between_skins_* после предмета)')
        rows, meta = fetch_steam_scm_top_listings(
            name, session=sess, log_skin_label=label
        )
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
        if i + 1 < n:
            d_lo = float(_effective("delay_between_skins_min_sec"))
            d_hi = float(_effective("delay_between_skins_max_sec"))
            time.sleep(random.uniform(d_lo, d_hi))

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
