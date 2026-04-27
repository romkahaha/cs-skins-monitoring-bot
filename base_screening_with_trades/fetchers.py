"""
Steam + CSFloat price fetchers.
Используется из ноутбуков: from fetchers import fetch_all_prices, fetch_all_prices_with_trades

fetch_all_prices_with_trades: те же колонки + статистика сделок SCM за SCM_TRADE_DAYS по /market/pricehistory/
(валюта = та же, что steam_ask: при PRICES_IN_EUR — EUR). Куки: env STEAM_COOKIES или local_steam_cookies.STEAM_COOKIES (fallback: local_secrets)
(sessionid, steamLoginSecure, …). Регион: STEAM_MARKET_COUNTRY (default DE для EUR).

CSFloat: CSFLOAT_API_KEY + опционально CSFLOAT_API_KEY_2 (local_secrets или env) —
при двух ключах round-robin только по ключам не в cooldown; HTTP 429/403 ставит ключ
на паузу (KEY_COOLDOWN_*) и сразу берётся другой; если все в паузе — ждём до истечения
(как в skin_homog/skin_screener.py).

Валюты / расхождения с клиентом Steam:
  • priceoverview отдаёт цену в выбранной валюте; USD и EUR — разные запросы (разные курсы/округление Steam).
  • Сравнивать «доллары из API» с «евро в клиенте × курс банка» нельзя — у Steam свой FX.
  • CSFloat listings API считает цены в USD (центы); «Float в евро» из API официально нет — только умножить на свой курс.

STEAM_FETCH_EUR_ALSO: второй запрос в EUR → колонка steam_ask_eur (сверка с EU клиентом).
Спреды spread_*% считаются в одной валюте: steam_ask (primary STEAM_CURRENCY) и Float USD.

Паузы STEAM_DELAY / FLOAT_DELAY — базовые секунды; между запросами sleep случайный в диапазоне 0.5×…1.5× от базы.

PRICES_IN_EUR: Steam в EUR + Float USD × курс USD→EUR. Курс: api.frankfurter.app (ECB),
при сбое — XML ЕЦБ eurofxref-daily. В CSV добавляется колонка fx_usd_to_eur.

Опционально: файл fetchers_runtime.json рядом с fetchers.py (старое имя fetcher_runtime.json тоже ищется).
Путь: переменная FETCHERS_RUNTIME_CONFIG или устар. FETCHER_RUNTIME_CONFIG.
При изменении файла на диске подхватываются паузы и cooldown без перезапуска ядра.
См. fetchers_runtime.example.json — скопировать в fetchers_runtime.json и править числа.
"""

from __future__ import annotations

import base64
import importlib
import json
import math
import os
import random
import re
import sys
import time
import threading
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import quote, unquote

import requests
import numpy as np
import pandas as pd
from datetime import datetime, timedelta, timezone

# ---------------------------------------------------------------------------
#  Config (можно перезаписать перед вызовом fetch_all_prices)
# ---------------------------------------------------------------------------
STEAM_CURRENCY = 1       # 1=USD, 3=EUR — primary steam_ask + основа для spread_% (с Float в USD)
STEAM_FETCH_EUR_ALSO = False  # True: доп. запрос EUR → колонка steam_ask_eur (если primary USD)
# True: Steam priceoverview в EUR; CSFloat (USD) переводится в EUR по курсу (Frankfurter → ECB XML fallback)
PRICES_IN_EUR = False
# Окно для агрегатов сделок Steam Community Market (pricehistory)
SCM_TRADE_DAYS = 7
# Доп. окно для дневных/трендовых метрик по trade points (median/EMA/ret/etc.)
SCM_TREND_DAYS = 14
SCM_RET_3D_DAYS = 3
SCM_RET_7D_DAYS = 7
SCM_SLOPE_7D_DAYS = 7
SCM_EMA_FAST_SPAN = 3
SCM_EMA_SLOW_SPAN = 14
SCM_RANGE_14D_DAYS = 14
# Страна для pricehistory (влияет на валюту/отображение; с currency=3 — EUR)
STEAM_PRICEHISTORY_COUNTRY = os.environ.get("STEAM_MARKET_COUNTRY", "DE")
# Базовые секунды; фактическая пауза — random.uniform(base*0.5, base*1.5)
STEAM_DELAY    = 10.0
FLOAT_DELAY    = 10.0
FLOAT_MAX_WORKERS = 1
# После 429/403 на ключе — не слать этим ключом до time.monotonic() (см. skin_screener).
KEY_COOLDOWN_429_SEC = 600.0
KEY_COOLDOWN_403_SEC = 900.0
# Steam: при 429 подождать и повторить (секунды между попытками). Переопределение: fetchers_runtime.json.
STEAM_429_RETRY_WAIT_SEC = 90.0
# 0 = ждать бесконечно. Если >0 и включен STEAM_RETURN_MISS_ON_429, то после N ответов 429 вернуть MISS.
# Куки Steam: env STEAM_COOKIES > local_steam_cookies.py > local_secrets.STEAM_COOKIES > эта строка (не коммитить секреты).
STEAM_COOKIES = ""
# --- runtime overrides, перечитывается при изменении mtime ---
_RUNTIME_LOCK = threading.Lock()
_runtime_mtime: float | None = None
_runtime_data: dict = {}
_runtime_warned_missing: bool = False
_runtime_loaded_path: str | None = None


def _runtime_config_path() -> str:
    """Путь к JSON: env, иначе fetchers_runtime.json, иначе (legacy) fetcher_runtime.json."""
    env = os.environ.get("FETCHERS_RUNTIME_CONFIG") or os.environ.get("FETCHER_RUNTIME_CONFIG")
    if env:
        return env
    base = Path(__file__).resolve().parent
    p_new = base / "fetchers_runtime.json"
    p_old = base / "fetcher_runtime.json"
    if p_new.is_file():
        return str(p_new)
    if p_old.is_file():
        return str(p_old)
    return str(p_new)


def _load_runtime_config() -> dict:
    """Следующий вызов после сохранения JSON на диске подхватит новые значения."""
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
            base = Path(__file__).resolve().parent
            print(
                "  [fetchers] нет fetchers_runtime.json (или fetcher_runtime.json) — "
                "тайминги из констант в fetchers.py "
                f"(скопируй {base / 'fetchers_runtime.example.json'} → {base / 'fetchers_runtime.json'})",
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
                f"  [fetchers] {path}: JSON битый — оставляем предыдущие значения ({e})",
                flush=True,
            )
            _runtime_mtime = mtime
        return _runtime_data


def _runtime_float(key: str, default: float) -> float:
    cfg = _load_runtime_config()
    if key not in cfg:
        return default
    try:
        return float(cfg[key])
    except (TypeError, ValueError):
        return default
def _random_delay(base: float) -> None:
    """Пауза в секундах: uniform(0.5×base … 1.5×base)."""
    lo = max(0.0, base * 0.5)
    hi = base * 1.5
    time.sleep(random.uniform(lo, hi))


def _inter_request_delay(base_key: str, fallback: float) -> None:
    """Пауза между предметами; base из fetchers_runtime.json если ключ задан."""
    _random_delay(_runtime_float(base_key, fallback))


CSFLOAT_API_KEY = os.environ.get("CSFLOAT_API_KEY")
CSFLOAT_API_KEY_2 = os.environ.get("CSFLOAT_API_KEY_2")
try:
    import local_secrets as _ls
    _k1 = getattr(_ls, "CSFLOAT_API_KEY", None)
    if _k1:
        CSFLOAT_API_KEY = _k1
    _k2 = getattr(_ls, "CSFLOAT_API_KEY_2", None)
    if _k2:
        CSFLOAT_API_KEY_2 = _k2
except ImportError:
    pass

_CSFLOAT_KEY_RR_LOCK = threading.Lock()
_csfloat_key_rr_i = [0]
# key index -> time.monotonic() когда ключ снова можно использовать (429/403)
_key_cooldown_mono: dict[int, float] = {}
# После get_csfloat_prices в том же потоке — какой ключ использовался (для лога, в т.ч. при MISS)
_tls_cf_key_tag = threading.local()


def _csfloat_api_keys() -> tuple[str, ...]:
    out: list[str] = []
    for raw in (CSFLOAT_API_KEY, CSFLOAT_API_KEY_2):
        if not raw:
            continue
        s = str(raw).strip()
        if s and s not in out:
            out.append(s)
    return tuple(out)


def _tag_for_explicit_key(api_key: str) -> str:
    keys = _csfloat_api_keys()
    for j, k in enumerate(keys):
        if k == api_key:
            return f"{j + 1}/{len(keys)}"
    return "fixed"


def _explicit_key_index(api_key: str) -> int:
    keys = _csfloat_api_keys()
    for j, k in enumerate(keys):
        if k == api_key:
            return j
    return 0


def _try_pick_key_index(keys: tuple[str, ...]) -> int | None:
    """Следующий доступный ключ в порядке round-robin; None если все в cooldown."""
    mono = time.monotonic()
    n = len(keys)
    start = _csfloat_key_rr_i[0] % n
    for step in range(n):
        i = (start + step) % n
        if mono >= _key_cooldown_mono.get(i, 0.0):
            _csfloat_key_rr_i[0] = i + 1
            return i
    return None


def _apply_csfloat_key_cooldown(key_index: int | None, err: str) -> None:
    if key_index is None or err not in ("429", "403"):
        return
    if err == "429":
        sec = _runtime_float("KEY_COOLDOWN_429_SEC", KEY_COOLDOWN_429_SEC)
    else:
        sec = _runtime_float("KEY_COOLDOWN_403_SEC", KEY_COOLDOWN_403_SEC)
    until = time.monotonic() + sec
    with _CSFLOAT_KEY_RR_LOCK:
        prev = _key_cooldown_mono.get(key_index, 0.0)
        _key_cooldown_mono[key_index] = max(prev, until)
    print(
        f"  [CSFloat] COOLDOWN ключ {key_index + 1}: ~{sec:.0f}s ({err})",
        flush=True,
    )


def _api_msg_rate_limited(msg: str) -> bool:
    m = msg.lower()
    return "too many" in m or "rate" in m or "429" in m


def _wait_pick_csfloat_key(api_key_explicit: str | None) -> tuple[str | None, str, int | None]:
    """
    Ключ для следующего запроса; если все в cooldown — блокируемся до освобождения.
    (api_key, tag '1/2', key_index для cooldown).
    """
    keys = _csfloat_api_keys()
    if not keys:
        return None, "", None

    if api_key_explicit is not None:
        ex = str(api_key_explicit).strip()
        if not ex:
            return None, "", None
        key_idx = _explicit_key_index(ex)
        while True:
            with _CSFLOAT_KEY_RR_LOCK:
                until = _key_cooldown_mono.get(key_idx, 0.0)
                if time.monotonic() >= until:
                    tag = _tag_for_explicit_key(ex)
                    return ex, tag, key_idx
                wake = until
            wait = max(0.05, wake - time.monotonic())
            print(f"  [CSFloat] COOLDOWN: ждём {wait:.0f}s (ключ {key_idx + 1})…", flush=True)
            time.sleep(wait)

    while True:
        with _CSFLOAT_KEY_RR_LOCK:
            if len(keys) == 1:
                if time.monotonic() >= _key_cooldown_mono.get(0, 0.0):
                    return keys[0], "1/1", 0
                wake = _key_cooldown_mono[0]
            else:
                picked = _try_pick_key_index(keys)
                if picked is not None:
                    return keys[picked], f"{picked + 1}/{len(keys)}", picked
                wake = min(_key_cooldown_mono.get(j, 0.0) for j in range(len(keys)))

        wait = max(0.05, wake - time.monotonic())
        print(f"  [CSFloat] COOLDOWN: все ключи в паузе, ждём {wait:.0f}s…", flush=True)
        time.sleep(wait)


# ---------------------------------------------------------------------------
#  Steam
# ---------------------------------------------------------------------------
def parse_steam_price(price_str: str | None) -> float | None:
    if not price_str:
        return None
    cleaned = re.sub(r'[^\d,.]', '', price_str)
    if not cleaned:
        return None
    if ',' in cleaned and '.' in cleaned:
        if cleaned.index('.') < cleaned.index(','):
            cleaned = cleaned.replace('.', '').replace(',', '.')
        else:
            cleaned = cleaned.replace(',', '')
    elif ',' in cleaned:
        cleaned = cleaned.replace(',', '.')
    try:
        return float(cleaned)
    except ValueError:
        return None


_STEAM_SYM = {1: "$", 3: "€", 5: "₽"}

FRANKFURTER_LATEST = "https://api.frankfurter.app/latest"
ECB_DAILY_XML = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"


def fetch_usd_to_eur_multiplier() -> tuple[float, str]:
    """
    Множитель: amount_eur = amount_usd * multiplier (сколько EUR за 1 USD).
    Источник: Frankfurter (курсы ECB); при ошибке — парсинг ECB daily XML.
    """
    err_ff: Exception | None = None
    try:
        r = requests.get(
            FRANKFURTER_LATEST,
            params={"from": "USD", "to": "EUR"},
            timeout=20,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        r.raise_for_status()
        data = r.json()
        m = float(data["rates"]["EUR"])
        day = data.get("date", "?")
        return m, f"Frankfurter {day} (ECB)"
    except Exception as e:
        err_ff = e
    try:
        r = requests.get(ECB_DAILY_XML, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        root = ET.fromstring(r.content)
        usd_per_1_eur: float | None = None
        for elem in root.iter():
            if elem.attrib.get("currency") == "USD":
                usd_per_1_eur = float(elem.attrib["rate"])
                break
        if usd_per_1_eur is None or usd_per_1_eur <= 0:
            raise ValueError("ECB XML: no USD rate")
        eur_per_usd = 1.0 / usd_per_1_eur
        return eur_per_usd, "ECB eurofxref-daily.xml (fallback)"
    except Exception as e2:
        raise RuntimeError(
            f"USD→EUR: Frankfurter failed ({err_ff!r}); ECB fallback failed ({e2!r})"
        ) from e2


def get_steam_price(market_hash_name: str, currency: int = STEAM_CURRENCY) -> float | None:
    url = "https://steamcommunity.com/market/priceoverview/"
    params = {"appid": 730, "currency": currency, "market_hash_name": market_hash_name}
    net_n = 0
    while True:
        try:
            r = requests.get(url, params=params, timeout=15)
            if r.status_code == 429:
                w = _runtime_float("STEAM_429_RETRY_WAIT_SEC", STEAM_429_RETRY_WAIT_SEC)
                print(
                    f"  [Steam] {market_hash_name}: HTTP 429 — пауза ~{w:.0f}s (до успеха)",
                    flush=True,
                )
                time.sleep(w)
                continue
            r.raise_for_status()
            data = r.json()
            if not data.get("success"):
                return None
            return parse_steam_price(data.get("lowest_price")) or parse_steam_price(
                data.get("median_price")
            )
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            net_n += 1
            print(
                f"  [Steam] {market_hash_name}: сеть — повтор #{net_n} (до успеха): {e}",
                flush=True,
            )
            time.sleep(
                random.uniform(
                    _runtime_float("STEAM_NET_SLEEP_MIN", 3.0),
                    _runtime_float("STEAM_NET_SLEEP_MAX", 12.0),
                )
            )
            continue
        except Exception as e:
            print(f"  [Steam] {market_hash_name}: {e}")
            return None


def _steam_cookie_header() -> str | None:
    c = os.environ.get("STEAM_COOKIES")
    if not c:
        try:
            if "local_steam_cookies" in sys.modules:
                _sc = importlib.reload(sys.modules["local_steam_cookies"])
            else:
                import local_steam_cookies as _sc
            c = getattr(_sc, "STEAM_COOKIES", None)
        except ImportError:
            c = None
    if not c:
        try:
            if "local_secrets" in sys.modules:
                _ls = importlib.reload(sys.modules["local_secrets"])
            else:
                import local_secrets as _ls
            c = getattr(_ls, "STEAM_COOKIES", None)
        except ImportError:
            c = None
    if not c:
        c = globals().get("STEAM_COOKIES")
    if not c:
        return None
    s = str(c).strip()
    if s.lower().startswith("cookie:"):
        s = s[7:].strip()
    return s or None


def _steam_sessionid_from_cookie_header(cookie_header: str | None) -> str | None:
    """sessionid из Cookie — часто нужен в query для /market/pricehistory/ (иначе 400)."""
    if not cookie_header:
        return None
    m = re.search(r"(?:^|;\s*)sessionid=([^;]+)", cookie_header, flags=re.I)
    if not m:
        return None
    return m.group(1).strip().strip('"') or None


def _steam_cookie_value(cookie_header: str | None, name: str) -> str | None:
    if not cookie_header:
        return None
    m = re.search(rf"(?:^|;\s*){re.escape(name)}=([^;]+)", cookie_header, flags=re.I)
    if not m:
        return None
    return m.group(1).strip().strip('"') or None


def _steam_loginsecure_expiry(cookie_header: str | None) -> datetime | None:
    """
    Пытается вытащить exp из steamLoginSecure (JWT-подобная часть после '||').
    Нужен только для диагностики: показать, что кука протухла, а не "просто нет".
    """
    raw = _steam_cookie_value(cookie_header, "steamLoginSecure")
    if not raw:
        return None
    try:
        decoded = unquote(raw)
        parts = decoded.split("||", 1)
        if len(parts) != 2:
            return None
        jwt = parts[1]
        seg = jwt.split(".")[1]
        seg += "=" * (-len(seg) % 4)
        payload = json.loads(base64.urlsafe_b64decode(seg.encode("ascii")))
        exp = payload.get("exp")
        if exp is None:
            return None
        return datetime.fromtimestamp(int(exp), tz=timezone.utc)
    except Exception:
        return None


def _steam_browser_headers(*, referer: str | None = None) -> dict[str, str]:
    h = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "en-US,en;q=0.9",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": "https://steamcommunity.com",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
    }
    if referer:
        h["Referer"] = referer
    ck = _steam_cookie_header()
    if ck:
        h["Cookie"] = ck
    return h


def _steam_pricehistory_headers_light(*, referer: str | None = None) -> dict[str, str]:
    """Минимум заголовков (как у вкладки в браузере); если полный набор даёт 400 — пробуем этот."""
    h = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "en-US,en;q=0.9",
    }
    if referer:
        h["Referer"] = referer
    ck = _steam_cookie_header()
    if ck:
        h["Cookie"] = ck
    return h


def _scm_listing_referer(appid: int, market_hash_name: str) -> str:
    """Как страница предмета в браузере — без этого Steam часто отвечает 400 на pricehistory."""
    seg = quote(market_hash_name, safe="")
    return f"https://steamcommunity.com/market/listings/{appid}/{seg}"


def _parse_pricehistory_timestamp(s: str) -> datetime | None:
    m = re.match(r"^([A-Za-z]{3}\s+\d{1,2}\s+\d{4})", str(s).strip())
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%b %d %Y").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _fetch_scm_pricehistory_raw(
    market_hash_name: str,
    *,
    currency: int,
    country: str | None = None,
    appid: int = 730,
) -> list | None:
    """
    Low-level helper for Steam /market/pricehistory/.

    Returns the raw `prices` array from Steam or None on failure.
    """
    country = country or STEAM_PRICEHISTORY_COUNTRY
    ref = _scm_listing_referer(appid, market_hash_name)
    ck = _steam_cookie_header()
    sid = _steam_sessionid_from_cookie_header(ck)

    param_variants: list[dict[str, str | int]] = []
    bases: list[dict[str, str | int]] = [
        {"appid": appid, "market_hash_name": market_hash_name},
        {"appid": appid, "market_hash_name": market_hash_name, "currency": currency},
    ]
    if country:
        bases.append(
            {
                "appid": appid,
                "market_hash_name": market_hash_name,
                "currency": currency,
                "country": country,
            }
        )
    for b in bases:
        if sid:
            param_variants.append({**b, "sessionid": sid})
        param_variants.append(dict(b))

    ph_urls = (
        "https://steamcommunity.com/market/pricehistory",
        "https://steamcommunity.com/market/pricehistory/",
    )
    header_factories = (
        _steam_pricehistory_headers_light,
        _steam_browser_headers,
    )

    raw: list | None = None
    last_http_err: str | None = None
    for hdr_fn in header_factories:
        for url in ph_urls:
            for params in param_variants:
                net_n = 0
                while True:
                    try:
                        r = requests.get(
                            url,
                            params=params,
                            headers=hdr_fn(referer=ref),
                            timeout=30,
                        )
                        if r.status_code == 429:
                            w = _runtime_float("STEAM_429_RETRY_WAIT_SEC", STEAM_429_RETRY_WAIT_SEC)
                            print(
                                f"  [Steam PH] {market_hash_name}: HTTP 429 â€” Ð¿Ð°ÑƒÐ·Ð° ~{w:.0f}s",
                                flush=True,
                            )
                            time.sleep(w)
                            continue
                        if r.status_code == 400:
                            t = (r.text or "").strip()
                            if t == "[]":
                                exp = _steam_loginsecure_expiry(ck)
                                if exp and exp <= datetime.now(timezone.utc):
                                    last_http_err = (
                                        "400 [] â€” steamLoginSecure Ð¸ÑÑ‚ÐµÐº "
                                        f"({exp.isoformat()}); Ð¾Ð±Ð½Ð¾Ð²Ð¸Ñ‚Ðµ STEAM_COOKIES Ð¸Ð· Ð±Ñ€Ð°ÑƒÐ·ÐµÑ€Ð°"
                                    )
                                else:
                                    last_http_err = (
                                        "400 [] â€” Ð½ÑƒÐ¶Ð½Ñ‹ Ð²Ð°Ð»Ð¸Ð´Ð½Ñ‹Ðµ STEAM_COOKIES Ñ sessionid Ð¸ steamLoginSecure "
                                        "(ÑÐºÐ¾Ð¿Ð¸Ñ€ÑƒÐ¹Ñ‚Ðµ Ð·Ð°Ð³Ð¾Ð»Ð¾Ð²Ð¾Ðº Cookie Ñ Ð·Ð°Ð¿Ñ€Ð¾ÑÐ° Ðº steamcommunity.com)"
                                    )
                            else:
                                last_http_err = t[:120] if t else "400"
                            break
                        r.raise_for_status()
                        try:
                            data = r.json()
                        except ValueError:
                            last_http_err = "non-json body"
                            break
                        if not data.get("success"):
                            last_http_err = "success=false"
                            break
                        rp = data.get("prices")
                        raw = list(rp) if isinstance(rp, list) else []
                        last_http_err = None
                        break
                    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                        net_n += 1
                        print(
                            f"  [Steam PH] {market_hash_name}: ÑÐµÑ‚ÑŒ â€” Ð¿Ð¾Ð²Ñ‚Ð¾Ñ€ #{net_n}: {e}",
                            flush=True,
                        )
                        time.sleep(
                            random.uniform(
                                _runtime_float("STEAM_NET_SLEEP_MIN", 3.0),
                                _runtime_float("STEAM_NET_SLEEP_MAX", 12.0),
                            )
                        )
                        continue
                    except requests.HTTPError as e:
                        last_http_err = str(e)
                        break
                    except Exception as e:
                        print(f"  [Steam PH] {market_hash_name}: {e}", flush=True)
                        return None
                if raw is not None:
                    break
            if raw is not None:
                break
        if raw is not None:
            break

    if raw is None:
        if last_http_err:
            extra = ""
            if ck and not sid:
                extra = " Ð’ STEAM_COOKIES Ð½ÐµÑ‚ sessionid= â€” Ð¾Ñ‚ÐºÑ€Ð¾Ð¹Ñ‚Ðµ DevTools â†’ Network â†’ Ð»ÑŽÐ±Ð¾Ð¹ Ð·Ð°Ð¿Ñ€Ð¾Ñ Ðº steamcommunity.com â†’ ÑÐºÐ¾Ð¿Ð¸Ñ€ÑƒÐ¹Ñ‚Ðµ Cookie Ñ†ÐµÐ»Ð¸ÐºÐ¾Ð¼."
            print(
                f"  [Steam PH] {market_hash_name}: Ð½ÐµÑ‚ Ð´Ð°Ð½Ð½Ñ‹Ñ… ({last_http_err}).{extra}",
                flush=True,
            )
        return None
    return raw


def get_scm_trade_points(
    market_hash_name: str,
    *,
    currency: int,
    country: str | None = None,
    days: int | None = None,
    appid: int = 730,
) -> list[tuple[datetime, float, int]] | None:
    """
    Returns parsed Steam trade points as (timestamp_utc, price, volume).
    """
    raw = _fetch_scm_pricehistory_raw(
        market_hash_name,
        currency=currency,
        country=country,
        appid=appid,
    )
    if raw is None:
        return None

    cutoff = None
    if days is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    points: list[tuple[datetime, float, int]] = []
    for row in raw:
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            continue
        dt = _parse_pricehistory_timestamp(str(row[0]))
        if dt is None or (cutoff is not None and dt < cutoff):
            continue
        try:
            price = float(row[1])
        except (TypeError, ValueError):
            continue
        vol = 1
        if len(row) >= 3:
            try:
                vol = max(1, int(str(row[2]).strip()))
            except ValueError:
                vol = 1
        points.append((dt, price, vol))
    return points or None


def _summarise_trade_points(
    points: list[tuple[datetime, float, int]] | None,
    *,
    days: int,
) -> dict[str, float | int] | None:
    if not points:
        return None
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    expanded: list[float] = []
    for dt, price, vol in points:
        if dt < cutoff:
            continue
        expanded.extend([float(price)] * max(1, int(vol)))
    if not expanded:
        return None
    arr = np.array(expanded, dtype=float)
    return {
        "mean": float(arr.mean()),
        "median": float(np.median(arr)),
        "p10": float(np.percentile(arr, 10)),
        "p25": float(np.percentile(arr, 25)),
        "p75": float(np.percentile(arr, 75)),
        "p90": float(np.percentile(arr, 90)),
        "min": float(arr.min()),
        "max": float(arr.max()),
        "n": int(len(expanded)),
    }


def _derive_trade_risk_metrics(tr: dict[str, float | int] | None) -> dict[str, float | None]:
    out: dict[str, float | None] = {
        "steam_sales_7d_iqr_risk%": None,
        "steam_sales_7d_band_risk%": None,
        "steam_sales_7d_downside_risk%": None,
        "steam_sales_7d_tail_ratio": None,
    }
    if not tr:
        return out

    mean = float(tr.get("mean", 0.0) or 0.0)
    if mean > 0:
        p25 = tr.get("p25")
        p75 = tr.get("p75")
        p10 = tr.get("p10")
        p90 = tr.get("p90")
        median = tr.get("median")
        if p25 is not None and p75 is not None:
            out["steam_sales_7d_iqr_risk%"] = ((float(p75) - float(p25)) / mean) * 100.0
        if p10 is not None and p90 is not None:
            out["steam_sales_7d_band_risk%"] = ((float(p90) - float(p10)) / mean) * 100.0
        if median is not None and p10 is not None:
            out["steam_sales_7d_downside_risk%"] = ((float(median) - float(p10)) / mean) * 100.0

    median = tr.get("median")
    p10 = tr.get("p10")
    if median is not None and float(median) > 0 and p10 is not None:
        out["steam_sales_7d_tail_ratio"] = float(p10) / float(median)
    return out


def _daily_median_series(
    points: list[tuple[datetime, float, int]] | None,
    *,
    days: int,
) -> pd.Series:
    if not points:
        return pd.Series(dtype=float)
    rows: list[dict[str, object]] = []
    for dt, price, vol in points:
        day = pd.Timestamp(dt).tz_convert("UTC").normalize()
        rows.extend({"day": day, "price": float(price)} for _ in range(max(1, int(vol))))
    if not rows:
        return pd.Series(dtype=float)
    df = pd.DataFrame(rows)
    daily = df.groupby("day", sort=True)["price"].median()
    latest_day = daily.index.max()
    cutoff_day = latest_day - pd.Timedelta(days=max(0, int(days) - 1))
    return daily[daily.index >= cutoff_day].sort_index()


def _latest_value_at_or_before(series: pd.Series, target_day: pd.Timestamp) -> float | None:
    if series.empty:
        return None
    eligible = series[series.index <= target_day]
    if eligible.empty:
        return None
    try:
        value = float(eligible.iloc[-1])
    except (TypeError, ValueError):
        return None
    return value


def _log_slope_per_day(series: pd.Series) -> float | None:
    if len(series) < 3:
        return None
    values: list[tuple[float, float]] = []
    t0 = series.index.min()
    for idx, value in series.items():
        try:
            price = float(value)
        except (TypeError, ValueError):
            continue
        if price <= 0:
            continue
        x = float((idx - t0).days)
        y = math.log(price)
        values.append((x, y))
    if len(values) < 3:
        return None
    xs = [x for x, _ in values]
    ys = [y for _, y in values]
    mean_x = sum(xs) / len(xs)
    mean_y = sum(ys) / len(ys)
    var_x = sum((x - mean_x) ** 2 for x in xs)
    if var_x <= 0:
        return None
    cov_xy = sum((x - mean_x) * (y - mean_y) for x, y in values)
    return cov_xy / var_x


def _derive_trade_trend_metrics(points: list[tuple[datetime, float, int]] | None) -> dict[str, float | None]:
    out: dict[str, float | None] = {
        "steam_daily_ret_3d": None,
        "steam_daily_ret_7d": None,
        "steam_daily_slope_7d": None,
        "steam_daily_ema_gap_3_14": None,
        "steam_daily_range_14d_pct": None,
        "steam_daily_downside_14d_pct": None,
    }
    daily = _daily_median_series(points, days=SCM_TREND_DAYS)
    if daily.empty:
        return out

    latest_day = daily.index.max()
    p0 = _latest_value_at_or_before(daily, latest_day)
    if p0 is None or p0 <= 0:
        return out

    p3 = _latest_value_at_or_before(daily, latest_day - pd.Timedelta(days=SCM_RET_3D_DAYS))
    if p3 is not None and p3 > 0:
        out["steam_daily_ret_3d"] = (p0 / p3) - 1.0

    p7 = _latest_value_at_or_before(daily, latest_day - pd.Timedelta(days=SCM_RET_7D_DAYS))
    if p7 is not None and p7 > 0:
        out["steam_daily_ret_7d"] = (p0 / p7) - 1.0

    slope_cutoff = latest_day - pd.Timedelta(days=SCM_SLOPE_7D_DAYS - 1)
    slope_series = daily[daily.index >= slope_cutoff]
    slope = _log_slope_per_day(slope_series)
    if slope is not None:
        out["steam_daily_slope_7d"] = float(slope)

    if len(daily) >= SCM_EMA_SLOW_SPAN:
        ema_fast = daily.ewm(span=SCM_EMA_FAST_SPAN, adjust=False).mean().iloc[-1]
        ema_slow = daily.ewm(span=SCM_EMA_SLOW_SPAN, adjust=False).mean().iloc[-1]
        if pd.notna(ema_fast) and pd.notna(ema_slow) and float(ema_slow) > 0:
            out["steam_daily_ema_gap_3_14"] = (float(ema_fast) / float(ema_slow)) - 1.0

    range_cutoff = latest_day - pd.Timedelta(days=SCM_RANGE_14D_DAYS - 1)
    range_series = daily[daily.index >= range_cutoff]
    if len(range_series) >= 2:
        out["steam_daily_range_14d_pct"] = (float(range_series.max()) - float(range_series.min())) / float(p0)
        out["steam_daily_downside_14d_pct"] = (float(p0) - float(range_series.min())) / float(p0)

    return out


def get_scm_trade_stats(
    market_hash_name: str,
    *,
    currency: int,
    country: str | None = None,
    days: int | None = None,
    appid: int = 730,
) -> dict[str, float | int] | None:
    """
    История продаж с GET /market/pricehistory/ (как в InternalSteamWebAPI: в первую очередь
    только appid + market_hash_name). Лишние query (country+currency) часто дают HTTP 400.

    Валюта в ответе — как у веб-клиента Steam (поле price_suffix); при необходимости EUR
    пробуем вариант с currency=3 без country.

    Возвращает среднее, медиану, квантили по ценам сделок за последние `days` дней.
    """
    days = days if days is not None else SCM_TRADE_DAYS
    country = country or STEAM_PRICEHISTORY_COUNTRY
    ref = _scm_listing_referer(appid, market_hash_name)
    ck = _steam_cookie_header()
    sid = _steam_sessionid_from_cookie_header(ck)

    # Параметры: сначала с sessionid в query (как в клиенте Steam), затем без; wiki — только appid+name.
    param_variants: list[dict[str, str | int]] = []
    bases: list[dict[str, str | int]] = [
        {"appid": appid, "market_hash_name": market_hash_name},
        {"appid": appid, "market_hash_name": market_hash_name, "currency": currency},
    ]
    if country:
        bases.append(
            {
                "appid": appid,
                "market_hash_name": market_hash_name,
                "currency": currency,
                "country": country,
            }
        )
    for b in bases:
        if sid:
            param_variants.append({**b, "sessionid": sid})
        param_variants.append(dict(b))

    # Пример из wiki без завершающего / ; иногда отличается поведение CDN.
    ph_urls = (
        "https://steamcommunity.com/market/pricehistory",
        "https://steamcommunity.com/market/pricehistory/",
    )
    header_factories = (
        _steam_pricehistory_headers_light,
        _steam_browser_headers,
    )

    raw: list | None = None
    last_http_err: str | None = None
    for hdr_fn in header_factories:
        for url in ph_urls:
            for params in param_variants:
                net_n = 0
                while True:
                    try:
                        r = requests.get(
                            url,
                            params=params,
                            headers=hdr_fn(referer=ref),
                            timeout=30,
                        )
                        if r.status_code == 429:
                            w = _runtime_float("STEAM_429_RETRY_WAIT_SEC", STEAM_429_RETRY_WAIT_SEC)
                            print(
                                f"  [Steam PH] {market_hash_name}: HTTP 429 — пауза ~{w:.0f}s",
                                flush=True,
                            )
                            time.sleep(w)
                            continue
                        if r.status_code == 400:
                            t = (r.text or "").strip()
                            if t == "[]":
                                exp = _steam_loginsecure_expiry(ck)
                                if exp and exp <= datetime.now(timezone.utc):
                                    last_http_err = (
                                        "400 [] — steamLoginSecure истек "
                                        f"({exp.isoformat()}); обновите STEAM_COOKIES из браузера"
                                    )
                                else:
                                    last_http_err = (
                                        "400 [] — нужны валидные STEAM_COOKIES с sessionid и steamLoginSecure "
                                        "(скопируйте заголовок Cookie с запроса к steamcommunity.com)"
                                    )
                            else:
                                last_http_err = t[:120] if t else "400"
                            break  # next param variant
                        r.raise_for_status()
                        try:
                            data = r.json()
                        except ValueError:
                            last_http_err = "non-json body"
                            break
                        if not data.get("success"):
                            last_http_err = "success=false"
                            break
                        rp = data.get("prices")
                        raw = list(rp) if isinstance(rp, list) else []
                        last_http_err = None
                        break
                    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                        net_n += 1
                        print(
                            f"  [Steam PH] {market_hash_name}: сеть — повтор #{net_n}: {e}",
                            flush=True,
                        )
                        time.sleep(
                            random.uniform(
                                _runtime_float("STEAM_NET_SLEEP_MIN", 3.0),
                                _runtime_float("STEAM_NET_SLEEP_MAX", 12.0),
                            )
                        )
                        continue
                    except requests.HTTPError as e:
                        last_http_err = str(e)
                        break
                    except Exception as e:
                        print(f"  [Steam PH] {market_hash_name}: {e}", flush=True)
                        return None
                if raw is not None:
                    break
            if raw is not None:
                break
        if raw is not None:
            break

    if raw is None:
        if last_http_err:
            extra = ""
            if ck and not sid:
                extra = " В STEAM_COOKIES нет sessionid= — откройте DevTools → Network → любой запрос к steamcommunity.com → скопируйте Cookie целиком."
            print(
                f"  [Steam PH] {market_hash_name}: нет данных ({last_http_err}).{extra}",
                flush=True,
            )
        return None

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    expanded: list[float] = []
    for row in raw:
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            continue
        dt = _parse_pricehistory_timestamp(str(row[0]))
        if dt is None or dt < cutoff:
            continue
        try:
            price = float(row[1])
        except (TypeError, ValueError):
            continue
        vol = 1
        if len(row) >= 3:
            try:
                vol = max(1, int(str(row[2]).strip()))
            except ValueError:
                vol = 1
        expanded.extend([price] * vol)

    if not expanded:
        return None
    arr = np.array(expanded, dtype=float)
    return {
        "mean": float(arr.mean()),
        "median": float(np.median(arr)),
        "p10": float(np.percentile(arr, 10)),
        "p25": float(np.percentile(arr, 25)),
        "p75": float(np.percentile(arr, 75)),
        "p90": float(np.percentile(arr, 90)),
        "min": float(arr.min()),
        "max": float(arr.max()),
        "n": int(len(expanded)),
    }


# ---------------------------------------------------------------------------
#  CSFloat
# ---------------------------------------------------------------------------
def get_csfloat_prices(market_hash_name: str, api_key: str | None = None) -> dict | None:
    """
    Returns dict: ask, predicted, base, quantity — в USD (API CSFloat, центы/100).

    If api_key is None: CSFLOAT_API_KEY + CSFLOAT_API_KEY_2 — round-robin по ключам не в cooldown;
    при HTTP 429/403 ключ уходит в паузу, запрос повторяется с другим ключом или после ожидания.
    Паузы/cooldown можно крутить через fetchers_runtime.json без перезапуска.

    Pass api_key explicitly to pin a single key (при 429 ждём cooldown только этого ключа).
    """
    url = "https://csfloat.com/api/v1/listings"
    params = {
        "market_hash_name": market_hash_name,
        "sort_by": "lowest_price",
        "limit": 3,
        "type": "buy_now",
    }
    while True:
        k, key_tag, key_idx = _wait_pick_csfloat_key(api_key)
        if not k:
            print(f"  [CSFloat] {market_hash_name}: нет API ключа")
            return None
        _tls_cf_key_tag.label = key_tag
        headers = {"User-Agent": "Mozilla/5.0", "Authorization": k}
        try:
            r = requests.get(url, params=params, headers=headers, timeout=15)
            if r.status_code == 429:
                print(f"  [CSFloat] {market_hash_name}: HTTP 429 — другой ключ / пауза", flush=True)
                _apply_csfloat_key_cooldown(key_idx, "429")
                continue
            if r.status_code == 403:
                print(f"  [CSFloat] {market_hash_name}: HTTP 403 — другой ключ / пауза", flush=True)
                _apply_csfloat_key_cooldown(key_idx, "403")
                continue
            if r.status_code >= 500:
                print(
                    f"  [CSFloat] {market_hash_name}: HTTP {r.status_code} — пауза и повтор…",
                    flush=True,
                )
                time.sleep(
                    random.uniform(
                        _runtime_float("CSFLOAT_5XX_SLEEP_MIN", 5.0),
                        _runtime_float("CSFLOAT_5XX_SLEEP_MAX", 15.0),
                    )
                )
                continue
            r.raise_for_status()
            data = r.json()
            if isinstance(data, dict) and ("error" in data or "message" in data):
                msg = str(data.get("error") or data.get("message") or "")
                if _api_msg_rate_limited(msg):
                    print(f"  [CSFloat] {market_hash_name}: rate limit в теле ответа — cooldown", flush=True)
                    _apply_csfloat_key_cooldown(key_idx, "429")
                    continue
                print(f"  [CSFloat] {market_hash_name}: {msg}")
                return None
            listings = data if isinstance(data, list) else data.get("data", [])
            if not listings:
                print(f"  [CSFloat] {market_hash_name}: no listings")
                return None
            ask_prices = [l["price"] / 100 for l in listings if "price" in l]
            if not ask_prices:
                return None
            ref = listings[0].get("reference", {})
            predicted = ref.get("predicted_price")
            base = ref.get("base_price")
            return {
                "ask": ask_prices[0],
                "predicted": predicted / 100 if predicted else None,
                "base": base / 100 if base else None,
                "quantity": ref.get("quantity"),
                "_key": key_tag,
            }
        except requests.HTTPError as e:
            resp = getattr(e, "response", None)
            code = resp.status_code if resp is not None else None
            if code == 429:
                _apply_csfloat_key_cooldown(key_idx, "429")
                continue
            if code == 403:
                _apply_csfloat_key_cooldown(key_idx, "403")
                continue
            print(f"  [CSFloat] {market_hash_name}: {e}")
            return None
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            print(f"  [CSFloat] {market_hash_name}: сеть — повтор: {e}", flush=True)
            time.sleep(
                random.uniform(
                    _runtime_float("CSFLOAT_NET_SLEEP_MIN", 3.0),
                    _runtime_float("CSFLOAT_NET_SLEEP_MAX", 10.0),
                )
            )
            continue
        except Exception as e:
            print(f"  [CSFloat] {market_hash_name}: {e}")
            return None


def _csfloat_key_suffix() -> str:
    tag = getattr(_tls_cf_key_tag, "label", "") or ""
    return f", key={tag}" if tag else ""


# ---------------------------------------------------------------------------
#  Batch fetcher
# ---------------------------------------------------------------------------
def fetch_all_prices(
    items: list[str],
    steam_delay: float = STEAM_DELAY,
    float_delay: float = FLOAT_DELAY,
    float_workers: int = FLOAT_MAX_WORKERS,
    *,
    steam_currency: int | None = None,
    steam_fetch_eur_also: bool | None = None,
    prices_in_eur: bool | None = None,
) -> pd.DataFrame:
    pie = PRICES_IN_EUR if prices_in_eur is None else prices_in_eur
    usd_eur: float | None = None
    fx_src = ""

    if pie:
        usd_eur, fx_src = fetch_usd_to_eur_multiplier()
        sc = 3
        fetch_eur = False
        print(
            f"PRICES_IN_EUR: Steam EUR + Float USD×{usd_eur:.6f} (€ per $1; EUR = USD×this) — {fx_src}\n"
        )
    else:
        sc = STEAM_CURRENCY if steam_currency is None else steam_currency
        fetch_eur = STEAM_FETCH_EUR_ALSO if steam_fetch_eur_also is None else steam_fetch_eur_also

    steam_prices: dict[str, float | None] = {}
    steam_prices_alt: dict[str, float | None] = {}  # EUR if primary USD, USD if primary EUR
    float_data: dict[str, dict | None] = {}
    lock = threading.Lock()
    total = len(items)
    nk = len(_csfloat_api_keys())
    if nk > 1:
        print(f"CSFloat: {nk} API keys — round-robin per request\n")

    sym = _STEAM_SYM.get(sc, "")
    want_eur_column = (not pie) and fetch_eur and sc == 1
    want_usd_column = (not pie) and fetch_eur and sc == 3
    if fetch_eur and sc not in (1, 3):
        print("Note: STEAM_FETCH_EUR_ALSO only adds EUR/USD pair when primary is USD (1) or EUR (3).\n")
    if want_eur_column or want_usd_column:
        print(
            "Note: extra Steam column for UI cross-check; spread_% still uses primary steam_ask vs Float USD.\n"
        )
    if (not pie) and sc != 1:
        print(
            "Note: steam_ask is not USD — spread_% vs CSFloat (USD) mixes currencies unless you convert.\n"
        )

    def steam_worker():
        for i, name in enumerate(items):
            price = get_steam_price(name, currency=sc)
            eur_p: float | None = None
            usd_p: float | None = None
            if want_eur_column:
                eur_p = get_steam_price(name, currency=3)
            elif want_usd_column:
                usd_p = get_steam_price(name, currency=1)
            with lock:
                steam_prices[name] = price
                if want_eur_column:
                    steam_prices_alt[name] = eur_p
                elif want_usd_column:
                    steam_prices_alt[name] = usd_p
                else:
                    steam_prices_alt[name] = None
                tag = f"{sym}{price:.2f}" if price else "MISS"
                if eur_p is not None:
                    tag += f"  (€{eur_p:.2f})"
                if usd_p is not None:
                    tag += f"  (${usd_p:.2f})"
                print(f"  ☁ Steam  [{i+1}/{total}] {name}: {tag}")
            if i < total - 1:
                _inter_request_delay("STEAM_DELAY", steam_delay)

    def float_fetch_one(pair):
        i, name = pair
        fd = get_csfloat_prices(name)
        ks = _csfloat_key_suffix()
        with lock:
            float_data[name] = fd
            if fd and fd.get("predicted") is not None:
                if pie and usd_eur is not None:
                    ae = fd["ask"] * usd_eur
                    pe = fd["predicted"] * usd_eur
                    print(
                        f"  🔷 Float  [{i+1}/{total}] {name}: ask=€{ae:.2f} (${fd['ask']:.2f})  "
                        f"pred=€{pe:.2f} (${fd['predicted']:.2f}){ks}"
                    )
                else:
                    print(
                        f"  🔷 Float  [{i+1}/{total}] {name}: ask=${fd['ask']:.2f}  "
                        f"pred=${fd['predicted']:.2f}{ks}"
                    )
            elif fd:
                if pie and usd_eur is not None:
                    ae = fd["ask"] * usd_eur
                    print(
                        f"  🔷 Float  [{i+1}/{total}] {name}: ask=€{ae:.2f} (${fd['ask']:.2f})  "
                        f"pred=n/a{ks}"
                    )
                else:
                    print(
                        f"  🔷 Float  [{i+1}/{total}] {name}: ask=${fd['ask']:.2f}  "
                        f"pred=n/a{ks}"
                    )
            else:
                print(f"  🔷 Float  [{i+1}/{total}] {name}: MISS{ks}")
        _inter_request_delay("FLOAT_DELAY", float_delay)

    def float_worker_parallel():
        with ThreadPoolExecutor(max_workers=max(1, float_workers)) as pool:
            list(pool.map(float_fetch_one, enumerate(items)))

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=2) as pool:
        fut_s = pool.submit(steam_worker)
        fut_f = pool.submit(float_worker_parallel)
        fut_s.result()
        fut_f.result()
    elapsed = time.time() - t0

    rows = []
    for name in items:
        s = steam_prices.get(name)
        fd = float_data.get(name)
        if s and fd and fd["ask"] and fd.get("predicted"):
            if pie and usd_eur is not None:
                f_ask = fd["ask"] * usd_eur
                f_pred = fd["predicted"] * usd_eur
                f_base = (fd["base"] * usd_eur) if fd.get("base") else None
                row = {
                    "item": name,
                    "steam_ask": round(s, 2),
                    "float_ask": round(f_ask, 2),
                    "float_pred": round(f_pred, 2),
                    "float_base": round(f_base, 2) if f_base is not None else None,
                    "float_qty": fd.get("quantity"),
                    "spread_ask%": round((s - f_ask) / s * 100, 2),
                    "spread_pred%": round((s - f_pred) / s * 100, 2),
                    "fx_usd_to_eur": round(usd_eur, 6),
                }
            else:
                row = {
                    "item": name,
                    "steam_ask": round(s, 2),
                    "float_ask": round(fd["ask"], 2),
                    "float_pred": round(fd["predicted"], 2),
                    "float_base": round(fd["base"], 2) if fd.get("base") else None,
                    "float_qty": fd.get("quantity"),
                    "spread_ask%": round((s - fd["ask"]) / s * 100, 2),
                    "spread_pred%": round((s - fd["predicted"]) / s * 100, 2),
                }
            alt = steam_prices_alt.get(name)
            if want_eur_column:
                row["steam_ask_eur"] = round(alt, 2) if alt is not None else None
            elif want_usd_column:
                row["steam_ask_usd"] = round(alt, 2) if alt is not None else None
            rows.append(row)  # fd["_key"] в CSV не попадает

    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values("spread_pred%", ascending=False).reset_index(drop=True)
    tail = f"  ({len(rows)}/{total} items with both prices)"
    if pie:
        tail += " — все цены и спреды в EUR"
    print(f"\n⏱ Done in {elapsed:.0f}s{tail}")
    return df


def _scm_trade_row(tr: dict[str, float | int] | None) -> dict[str, float | int | None]:
    if not tr:
        return {
            "steam_sales_7d_mean": None,
            "steam_sales_7d_median": None,
            "steam_sales_7d_p10": None,
            "steam_sales_7d_p25": None,
            "steam_sales_7d_p75": None,
            "steam_sales_7d_p90": None,
            "steam_sales_7d_min": None,
            "steam_sales_7d_max": None,
            "steam_sales_7d_n": 0,
        }
    return {
        "steam_sales_7d_mean": round(tr["mean"], 4),
        "steam_sales_7d_median": round(tr["median"], 4),
        "steam_sales_7d_p10": round(tr["p10"], 4),
        "steam_sales_7d_p25": round(tr["p25"], 4),
        "steam_sales_7d_p75": round(tr["p75"], 4),
        "steam_sales_7d_p90": round(tr["p90"], 4),
        "steam_sales_7d_min": round(tr["min"], 4),
        "steam_sales_7d_max": round(tr["max"], 4),
        "steam_sales_7d_n": int(tr["n"]),
    }


def _expected_prices_with_trades_columns(
    *,
    prices_in_eur: bool,
    want_eur_column: bool,
    want_usd_column: bool,
) -> list[str]:
    cols = [
        "item",
        "steam_ask",
        "float_ask",
        "float_pred",
        "float_base",
        "float_qty",
        "spread_ask%",
        "spread_pred%",
    ]
    if prices_in_eur:
        cols.append("fx_usd_to_eur")
    cols.extend(
        [
            "steam_sales_7d_mean",
            "steam_sales_7d_median",
            "steam_sales_7d_p10",
            "steam_sales_7d_p25",
            "steam_sales_7d_p75",
            "steam_sales_7d_p90",
            "steam_sales_7d_min",
            "steam_sales_7d_max",
            "steam_sales_7d_n",
            "steam_sales_7d_iqr_risk%",
            "steam_sales_7d_band_risk%",
            "steam_sales_7d_downside_risk%",
            "steam_sales_7d_tail_ratio",
            "steam_daily_ret_3d",
            "steam_daily_ret_7d",
            "steam_daily_slope_7d",
            "steam_daily_ema_gap_3_14",
            "steam_daily_range_14d_pct",
            "steam_daily_downside_14d_pct",
        ]
    )
    if want_eur_column:
        cols.append("steam_ask_eur")
    elif want_usd_column:
        cols.append("steam_ask_usd")
    return cols


def _build_prices_with_trades_row(
    name: str,
    *,
    steam_price: float | None,
    steam_price_alt: float | None,
    trade_stats: dict[str, float | int] | None,
    trade_enrich: dict[str, float | None] | None,
    float_row: dict | None,
    prices_in_eur: bool,
    usd_eur: float | None,
    want_eur_column: bool,
    want_usd_column: bool,
) -> dict[str, float | int | str | None] | None:
    if not steam_price or not float_row or not float_row.get("ask") or not float_row.get("predicted"):
        return None

    if prices_in_eur and usd_eur is not None:
        f_ask = float(float_row["ask"]) * usd_eur
        f_pred = float(float_row["predicted"]) * usd_eur
        f_base_raw = float_row.get("base")
        f_base = (float(f_base_raw) * usd_eur) if f_base_raw else None
        row: dict[str, float | int | str | None] = {
            "item": name,
            "steam_ask": round(float(steam_price), 2),
            "float_ask": round(f_ask, 2),
            "float_pred": round(f_pred, 2),
            "float_base": round(f_base, 2) if f_base is not None else None,
            "float_qty": float_row.get("quantity"),
            "spread_ask%": round((float(steam_price) - f_ask) / float(steam_price) * 100, 2),
            "spread_pred%": round((float(steam_price) - f_pred) / float(steam_price) * 100, 2),
            "fx_usd_to_eur": round(float(usd_eur), 6),
        }
    else:
        row = {
            "item": name,
            "steam_ask": round(float(steam_price), 2),
            "float_ask": round(float(float_row["ask"]), 2),
            "float_pred": round(float(float_row["predicted"]), 2),
            "float_base": round(float(float_row["base"]), 2) if float_row.get("base") else None,
            "float_qty": float_row.get("quantity"),
            "spread_ask%": round((float(steam_price) - float(float_row["ask"])) / float(steam_price) * 100, 2),
            "spread_pred%": round(
                (float(steam_price) - float(float_row["predicted"])) / float(steam_price) * 100,
                2,
            ),
        }

    row.update(_scm_trade_row(trade_stats))
    row.update(trade_enrich or {})
    if want_eur_column:
        row["steam_ask_eur"] = round(float(steam_price_alt), 2) if steam_price_alt is not None else None
    elif want_usd_column:
        row["steam_ask_usd"] = round(float(steam_price_alt), 2) if steam_price_alt is not None else None
    return row


def _finalize_prices_with_trades_df(
    df: pd.DataFrame,
    *,
    prices_in_eur: bool,
    want_eur_column: bool,
    want_usd_column: bool,
) -> pd.DataFrame:
    if df.empty:
        return df
    if "spread_pred%" in df.columns:
        df = df.sort_values("spread_pred%", ascending=False).reset_index(drop=True)
    ordered = _expected_prices_with_trades_columns(
        prices_in_eur=prices_in_eur,
        want_eur_column=want_eur_column,
        want_usd_column=want_usd_column,
    )
    extra = [c for c in df.columns if c not in ordered]
    return df[[c for c in ordered if c in df.columns] + extra]


def _prepare_incremental_output(
    out_csv: str | os.PathLike[str] | None,
    *,
    write_mode: str,
    expected_columns: list[str],
) -> tuple[Path | None, set[str]]:
    if out_csv is None:
        return None, set()

    path = Path(out_csv).expanduser().resolve()
    mode = str(write_mode or "create").strip().lower()
    if mode not in {"create", "merge"}:
        raise ValueError(f"write_mode must be 'create' or 'merge', got {write_mode!r}")

    if mode == "create":
        if path.exists():
            raise FileExistsError(
                f"{path} already exists; create mode does not delete files. Use a new path or write_mode='merge'."
            )
        return path, set()

    if not path.exists():
        return path, set()

    try:
        existing = pd.read_csv(path, nrows=0)
    except Exception:
        return path, set()
    existing_columns = list(existing.columns)
    if existing_columns and existing_columns != expected_columns:
        raise ValueError(
            f"{path} has a different schema and cannot be resumed safely.\n"
            f"existing={existing_columns}\nexpected={expected_columns}"
        )

    try:
        items_df = pd.read_csv(path, usecols=["item"])
    except Exception:
        return path, set()
    known = {
        str(x).strip()
        for x in items_df["item"].dropna().astype(str).tolist()
        if str(x).strip()
    }
    return path, known


def _append_prices_with_trades_row(out_csv: Path, row: dict, columns: list[str]) -> None:
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    frame = pd.DataFrame([{col: row.get(col) for col in columns}], columns=columns)
    write_header = not out_csv.exists()
    frame.to_csv(out_csv, mode="a", header=write_header, index=False)


def fetch_all_prices_with_trades(
    items: list[str],
    steam_delay: float = STEAM_DELAY,
    float_delay: float = FLOAT_DELAY,
    float_workers: int = FLOAT_MAX_WORKERS,
    *,
    steam_currency: int | None = None,
    steam_fetch_eur_also: bool | None = None,
    prices_in_eur: bool | None = None,
    trade_days: int | None = None,
    out_csv: str | os.PathLike[str] | None = None,
    write_mode: str = "create",
) -> pd.DataFrame:
    """
    Как fetch_all_prices, плюс колонки по сделкам SCM за trade_days (по умолчанию SCM_TRADE_DAYS)
    из /market/pricehistory/ в валюте steam_ask (для PRICES_IN_EUR — евро из Steam, как priceoverview).
    Float по-прежнему USD×Frankfurter/ECB.
    """
    pie = PRICES_IN_EUR if prices_in_eur is None else prices_in_eur
    usd_eur: float | None = None
    fx_src = ""
    td = trade_days if trade_days is not None else SCM_TRADE_DAYS

    if pie:
        usd_eur, fx_src = fetch_usd_to_eur_multiplier()
        sc = 3
        fetch_eur = False
        print(
            f"PRICES_IN_EUR: Steam EUR + Float USD×{usd_eur:.6f} — {fx_src}\n"
            f"SCM trades: последние {td} дн. из pricehistory (currency={sc}, country={STEAM_PRICEHISTORY_COUNTRY})\n"
        )
    else:
        sc = STEAM_CURRENCY if steam_currency is None else steam_currency
        fetch_eur = STEAM_FETCH_EUR_ALSO if steam_fetch_eur_also is None else steam_fetch_eur_also
        print(
            f"SCM trades: последние {td} дн. из pricehistory (currency={sc}, country={STEAM_PRICEHISTORY_COUNTRY})\n"
        )

    steam_prices: dict[str, float | None] = {}
    steam_prices_alt: dict[str, float | None] = {}
    steam_trade_stats: dict[str, dict | None] = {}
    steam_trade_enrich: dict[str, dict[str, float | None]] = {}
    float_data: dict[str, dict | None] = {}
    lock = threading.Lock()
    total = len(items)
    nk = len(_csfloat_api_keys())
    if nk > 1:
        print(f"CSFloat: {nk} API keys — round-robin per request\n")

    sym = _STEAM_SYM.get(sc, "")
    want_eur_column = (not pie) and fetch_eur and sc == 1
    want_usd_column = (not pie) and fetch_eur and sc == 3
    expected_columns = _expected_prices_with_trades_columns(
        prices_in_eur=pie,
        want_eur_column=want_eur_column,
        want_usd_column=want_usd_column,
    )
    out_path, known_items = _prepare_incremental_output(
        out_csv,
        write_mode=write_mode,
        expected_columns=expected_columns,
    )
    if known_items:
        original_total = total
        items = [name for name in items if name not in known_items]
        total = len(items)
        print(f"merge resume: skipped {original_total - total} known items, remaining {total}\n")
    elif out_path is not None:
        print(f"{str(write_mode).strip().lower()} output: {out_path}\n")
    if total == 0:
        if out_path is not None and out_path.exists():
            return _finalize_prices_with_trades_df(
                pd.read_csv(out_path),
                prices_in_eur=pie,
                want_eur_column=want_eur_column,
                want_usd_column=want_usd_column,
            )
        return pd.DataFrame(columns=expected_columns)

    steam_done: set[str] = set()
    float_done: set[str] = set()
    resolved_items: set[str] = set()
    rows: list[dict[str, float | int | str | None]] = []

    def try_finalize_item(name: str) -> None:
        if name in resolved_items or name not in steam_done or name not in float_done:
            return
        row = _build_prices_with_trades_row(
            name,
            steam_price=steam_prices.get(name),
            steam_price_alt=steam_prices_alt.get(name),
            trade_stats=steam_trade_stats.get(name),
            trade_enrich=steam_trade_enrich.get(name),
            float_row=float_data.get(name),
            prices_in_eur=pie,
            usd_eur=usd_eur,
            want_eur_column=want_eur_column,
            want_usd_column=want_usd_column,
        )
        resolved_items.add(name)
        if row is None:
            return
        rows.append(row)
        if out_path is not None:
            _append_prices_with_trades_row(out_path, row, expected_columns)

    def steam_worker():
        for i, name in enumerate(items):
            eur_p: float | None = None
            usd_p: float | None = None
            price = get_steam_price(name, currency=sc)
            if want_eur_column:
                eur_p = get_steam_price(name, currency=3)
            elif want_usd_column:
                usd_p = get_steam_price(name, currency=1)
            fetch_days = max(int(td), int(SCM_TREND_DAYS))
            pts = get_scm_trade_points(name, currency=sc, days=fetch_days)
            tr = _summarise_trade_points(pts, days=td)
            enrich = {}
            enrich.update(_derive_trade_risk_metrics(tr))
            enrich.update(_derive_trade_trend_metrics(pts))
            with lock:
                steam_prices[name] = price
                if want_eur_column:
                    steam_prices_alt[name] = eur_p
                elif want_usd_column:
                    steam_prices_alt[name] = usd_p
                else:
                    steam_prices_alt[name] = None
                steam_trade_stats[name] = tr
                steam_trade_enrich[name] = enrich
                steam_done.add(name)
                try_finalize_item(name)
                tag = f"{sym}{price:.2f}" if price else "MISS"
                if eur_p is not None:
                    tag += f"  (€{eur_p:.2f})"
                if usd_p is not None:
                    tag += f"  (${usd_p:.2f})"
                extra = f"  | SCM n={tr['n']}" if tr else "  | SCM —"
                print(f"  ☁ Steam  [{i+1}/{total}] {name}: {tag}{extra}")
            if i < total - 1:
                _inter_request_delay("STEAM_DELAY", steam_delay)

    def float_fetch_one(pair):
        i, name = pair
        fd = get_csfloat_prices(name)
        ks = _csfloat_key_suffix()
        with lock:
            float_data[name] = fd
            float_done.add(name)
            try_finalize_item(name)
            if fd and fd.get("predicted") is not None:
                if pie and usd_eur is not None:
                    ae = fd["ask"] * usd_eur
                    pe = fd["predicted"] * usd_eur
                    print(
                        f"  🔷 Float  [{i+1}/{total}] {name}: ask=€{ae:.2f} (${fd['ask']:.2f})  "
                        f"pred=€{pe:.2f} (${fd['predicted']:.2f}){ks}"
                    )
                else:
                    print(
                        f"  🔷 Float  [{i+1}/{total}] {name}: ask=${fd['ask']:.2f}  "
                        f"pred=${fd['predicted']:.2f}{ks}"
                    )
            elif fd:
                if pie and usd_eur is not None:
                    ae = fd["ask"] * usd_eur
                    print(
                        f"  🔷 Float  [{i+1}/{total}] {name}: ask=€{ae:.2f} (${fd['ask']:.2f})  "
                        f"pred=n/a{ks}"
                    )
                else:
                    print(
                        f"  🔷 Float  [{i+1}/{total}] {name}: ask=${fd['ask']:.2f}  "
                        f"pred=n/a{ks}"
                    )
            else:
                print(f"  🔷 Float  [{i+1}/{total}] {name}: MISS{ks}")
        _inter_request_delay("FLOAT_DELAY", float_delay)

    def float_worker_parallel():
        with ThreadPoolExecutor(max_workers=max(1, float_workers)) as pool:
            list(pool.map(float_fetch_one, enumerate(items)))

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=2) as pool:
        fut_s = pool.submit(steam_worker)
        fut_f = pool.submit(float_worker_parallel)
        fut_s.result()
        fut_f.result()
    elapsed = time.time() - t0

    if out_path is not None and out_path.exists():
        df = pd.read_csv(out_path)
    else:
        df = pd.DataFrame(rows, columns=expected_columns)
    df = _finalize_prices_with_trades_df(
        df,
        prices_in_eur=pie,
        want_eur_column=want_eur_column,
        want_usd_column=want_usd_column,
    )
    tail = f"  ({len(rows)}/{total} items with both prices)"
    if pie:
        tail += " — Steam/Float в EUR; SCM sales stats в валюте Steam (EUR)"
    print(f"\n⏱ Done in {elapsed:.0f}s{tail}")
    return df
