"""
Skin homogeneity screener -- CSFloat only.

Fetches paginated CSFloat listings per skin and saves panel CSVs (cols=skins, rows=listings).

Modes:
  --create [list]   Wipe data_skins/, fetch, save fresh
  --merge  [list]   Append a new block of rows to existing panels
  (no flag)         Same as --create

Usage:
  python skin_screener.py --create batch_01_ak1.py
  python skin_screener.py --merge  --mix batch_ak2.py
  python skin_screener.py --create --mix batch_01_ak1.py   # blend sorts (wider float/price slice)
  python skin_screener.py --create --sort lowest_price ...

Default sort comes from runtime JSON; mix mode is enabled by default.

Keys: CSFLOAT_API_KEY + optional CSFLOAT_API_KEY_2 in local_secrets.py or env —
if both set, keys rotate round-robin among keys **not in cooldown**. A key that gets
HTTP 429 or 403 is paused (see KEY_COOLDOWN_* below); the other key keeps working.

Pauses / cooldowns / retries: defaults in this file; override without editing code via
**skin_screener_runtime.json** next to this script (or path in env SKIN_SCREENER_RUNTIME_CONFIG).
On file change, values reload (mtime); keys starting with ``__`` are comments only.
Transient API errors (429/403/5xx/net): **RETRY_LADDER_MIN_SEC / STEP / MAX_SEC** — пауза растёт
подряд идущими фейлами, на потолке max повторяется; успех сбрасывает счётчик (без лимита попыток).

Output folder: default ``skin_homog/data_skins``; override with env **SKIN_SCREENER_OUTPUT_DIR**
(absolute or relative path; resolved at startup).
"""

from __future__ import annotations

import io
import json
import math
import random
import sys, os, time, threading, importlib.util, shutil

import requests
from requests import exceptions as req_exc

import pandas as pd

# Exit codes for automation (e.g. run_all_batches.ipynb stops the chain)
EXIT_RATE_LIMIT = 2   # 429 / site "too many requests"
EXIT_NETWORK = 3    # timeout / connection
EXIT_SERVER = 4     # HTTP 5xx
EXIT_CLIENT = 5     # other HTTP 4xx

# -- Config -------------------------------------------------------------------
API_MAX_PAGE_LIMIT = 50
TARGET_UNIQUE = 400
PAGE_LIMIT = 50
# Паузы с jitter: между запросами внутри одного skin (mix), между skin, и «slow» после 429/403.
# Balanced baseline (~7–10 skins/h при mix×2 + сеть); подкрутите min/max под свои лимиты.
INNER_DELAY_MIN = 1.0
INNER_DELAY_MAX = 1.8
ITEM_DELAY_MIN = 1.0
ITEM_DELAY_MAX = 2.0
# После любого 429/403 на ключе — на SLOW_MODE_EXTEND_SEC включаются диапазоны ниже.
SLOW_INNER_MIN = 6.0
SLOW_INNER_MAX = 10.0
SLOW_ITEM_MIN = 10.0
SLOW_ITEM_MAX = 16.0
SLOW_MODE_EXTEND_SEC = 30 * 60.0
# After 429/403, do not send this key again until time.monotonic() passes (other keys still used).
KEY_COOLDOWN_429_SEC = 480.0
KEY_COOLDOWN_403_SEC = 1800.0
# Transient errors (429/403/5xx/net): см. RETRY_LADDER_* в JSON — бесконечные повторы, без abort
RETRY_LADDER_MIN_SEC = 90.0
RETRY_LADDER_STEP_SEC = 150.0
RETRY_LADDER_MAX_SEC = 900.0
# CSFloat API sort_by values (see docs.csfloat.com)
DEFAULT_SORT = "most_recent"
VALID_SORTS = (
    "best_deal", "lowest_price", "highest_price", "most_recent", "expires_soon",
    "lowest_float", "highest_float", "highest_discount", "float_rank", "num_bids",
)
DEFAULT_MIX_OVERFETCH_FACTOR = 1.15
DEFAULT_MIX_SHARDS = (
    {"sort_by": "most_recent", "share": 0.50},
    {"sort_by": "lowest_float", "share": 0.25},
    {"sort_by": "highest_float", "share": 0.25},
)
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_SCRIPT_DIR)


def _resolve_save_dir() -> str:
    out_env = (os.environ.get("SKIN_SCREENER_OUTPUT_DIR") or "").strip()
    if out_env:
        return os.path.abspath(os.path.expanduser(out_env))
    return os.path.join(_SCRIPT_DIR, "data_skins")


SAVE_DIR = _resolve_save_dir()
CANDS_DIR = os.path.join(_SCRIPT_DIR, "skin_cands")


def _refresh_runtime_paths() -> None:
    """Re-read output-related paths from env so notebooks can set them after import."""
    global SAVE_DIR, CANDS_DIR
    SAVE_DIR = _resolve_save_dir()
    CANDS_DIR = os.path.join(_SCRIPT_DIR, "skin_cands")

CSFLOAT_API_KEY = os.environ.get("CSFLOAT_API_KEY")
CSFLOAT_API_KEY_2 = os.environ.get("CSFLOAT_API_KEY_2")
try:
    sys.path.insert(0, os.path.join(_SCRIPT_DIR, ".."))
    import local_secrets as _ls
    _k1 = getattr(_ls, "CSFLOAT_API_KEY", None)
    if _k1:
        CSFLOAT_API_KEY = _k1
    _k2 = getattr(_ls, "CSFLOAT_API_KEY_2", None)
    if _k2:
        CSFLOAT_API_KEY_2 = _k2
except ImportError:
    pass

_RUNTIME_LOCK = threading.Lock()
_runtime_mtime: float | None = None
_runtime_data: dict = {}
_runtime_warned_missing: bool = False
_runtime_loaded_path: str | None = None
# Подряд идущие retryable-ошибки HTTP; сброс в 0 при успешном ответе
_retry_fail_streak: int = 0


def _log(msg: str = "") -> None:
    """Сообщения в консоль. В подпроцессе Jupyter иногда буферизует — см. _skin_done_line + файл."""
    print(msg, flush=True)
    try:
        sys.stdout.flush()
    except Exception:
        pass


def _skin_done_line(
    idx1: int,
    total: int,
    name: str,
    *,
    ok: bool,
    n_listings: int,
    lim_cap: int,
    dt_s: float,
    err: str | None = None,
    tag: str = "",
    key_trace: str = "",
) -> str:
    """Одна строка после завершения скина: 2/N \"…\" собран — …"""
    q = '"'
    base = f"{idx1}/{total}  {q}{name}{q}"
    if ok:
        extra = f"  собран  — {n_listings}/{lim_cap} листингов, {dt_s:.1f}с"
        if tag:
            extra += f", {tag}"
        if key_trace:
            extra += f", {key_trace}"
        return base + extra
    e = err or "?"
    return f"{base}  ОШИБКА {e}  ({n_listings}/{lim_cap} лист., {dt_s:.1f}с)"


def _write_progress_file(line: str) -> None:
    """Дублирует строку в SAVE_DIR/_screener_progress.log (видно в IDE даже если ячейка не обновляет вывод)."""
    try:
        path = os.path.join(SAVE_DIR, "_screener_progress.log")
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
            try:
                os.fsync(f.fileno())
            except Exception:
                pass
    except Exception:
        pass


def _retry_ladder_sec() -> float:
    """Текущая пауза: min + (streak-1)*step, не выше max (JSON перечитывается)."""
    mn = _runtime_float("RETRY_LADDER_MIN_SEC", RETRY_LADDER_MIN_SEC)
    st = _runtime_float("RETRY_LADDER_STEP_SEC", RETRY_LADDER_STEP_SEC)
    mx = _runtime_float("RETRY_LADDER_MAX_SEC", RETRY_LADDER_MAX_SEC)
    if mn < 1.0:
        mn = 1.0
    if mx < mn:
        mx = mn
    k = max(1, _retry_fail_streak)
    raw = mn + (k - 1) * st
    return min(max(raw, mn), mx)


def _runtime_config_path() -> str:
    env = os.environ.get("SKIN_SCREENER_RUNTIME_CONFIG")
    if env:
        return os.path.abspath(os.path.expanduser(env))
    return os.path.join(_SCRIPT_DIR, "skin_screener_runtime.json")


def _load_runtime_config() -> dict:
    """Reload JSON when path or mtime changes (same idea as fetchers.py)."""
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
            print(
                f"  [skin_screener] нет {os.path.basename(path)} — тайминги из констант в skin_screener.py",
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
            print(f"  [skin_screener] {path}: JSON битый — оставляем предыдущие значения ({e})", flush=True)
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


def _runtime_int(key: str, default: int) -> int:
    cfg = _load_runtime_config()
    if key not in cfg:
        return default
    try:
        return int(cfg[key])
    except (TypeError, ValueError):
        return default


def _runtime_str(key: str, default: str) -> str:
    cfg = _load_runtime_config()
    if key not in cfg:
        return default
    v = cfg[key]
    if v is None:
        return default
    s = str(v).strip()
    return s if s else default


def _runtime_bool(key: str, default: bool) -> bool:
    cfg = _load_runtime_config()
    if key not in cfg:
        return default
    return _runtime_bool_from_value(cfg[key], default)


def _runtime_bool_from_value(v: object, default: bool) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if v is None:
        return default
    s = str(v).strip().lower()
    if s in {"1", "true", "yes", "y", "on"}:
        return True
    if s in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _normalize_fraction(v: object, default: float = 0.0) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return default
    if math.isnan(x):
        return default
    return min(max(x, 0.0), 0.95)


def _effective_page_limit() -> int:
    return max(1, min(API_MAX_PAGE_LIMIT, _runtime_int("PAGE_LIMIT", PAGE_LIMIT)))


def _target_unique(target_override: int | None = None) -> int:
    if target_override is not None:
        return max(1, int(target_override))
    return max(1, _runtime_int("TARGET_UNIQUE", TARGET_UNIQUE))


def _runtime_mix_shards() -> list[dict]:
    cfg = _load_runtime_config()
    raw = cfg.get("MIX_SHARDS")
    if raw is None or not isinstance(raw, list):
        raw = list(DEFAULT_MIX_SHARDS)
    out: list[dict] = []
    for idx, row in enumerate(raw):
        if not isinstance(row, dict):
            continue
        if not _runtime_bool_from_value(row.get("enabled"), True):
            continue
        sort_by = str(row.get("sort_by") or "").strip()
        if sort_by not in VALID_SORTS:
            continue
        share = _normalize_fraction(row.get("share"), 0.0)
        if share <= 0:
            continue
        low_trim = _normalize_fraction(row.get("drop_lowest_price_fraction"), 0.0)
        high_trim = _normalize_fraction(row.get("drop_highest_price_fraction"), 0.0)
        if low_trim + high_trim >= 0.95:
            high_trim = max(0.0, 0.95 - low_trim)
        out.append(
            {
                "index": idx,
                "sort_by": sort_by,
                "share": share,
                "drop_lowest_price_fraction": low_trim,
                "drop_highest_price_fraction": high_trim,
            }
        )
    return out or [dict(x) for x in DEFAULT_MIX_SHARDS]


def _describe_mix(shards: list[dict] | None = None) -> str:
    shards = shards or _runtime_mix_shards()
    total = sum(float(s.get("share") or 0.0) for s in shards) or 1.0
    parts: list[str] = []
    for shard in shards:
        pct = 100.0 * float(shard.get("share") or 0.0) / total
        parts.append(f"{shard['sort_by']}:{pct:.0f}%")
    return "mix(" + "+".join(parts) + ")"


PANELS = {
    "listing_id":         "listing_id.csv",
    "created_at":         "created_at.csv",
    "state":              "state.csv",
    "source_sort":        "source_sort.csv",
    "price":              "ask.csv",
    "predicted_price":    "predicted.csv",
    "base_price":         "base.csv",
    "currency":           "currency.csv",
    "quantity":           "quantity.csv",
    "float_value":        "float_value.csv",
    "sticker_count":      "sticker_count.csv",
    "sticker_value_usd":  "sticker_value.csv",
    "watchers":           "watchers.csv",
}
STRUCTURAL_GAP_SENTINEL = -1337
_STRING_PANELS = frozenset({"created_at", "state", "source_sort", "currency"})

# -- CSFloat ------------------------------------------------------------------

_CSFLOAT_KEY_RR_LOCK = threading.Lock()
_csfloat_key_rr_i = [0]
# key index -> time.monotonic() when the key may be used again
_key_cooldown_mono: dict[int, float] = {}
# Last Authorization key label after each HTTP build (for logs); set in _pick_csfloat_headers.
_last_cf_key_tag: str = ""
# After 429/403: use SLOW_* jitter ranges until this time.monotonic().
_slow_until_mono: float = 0.0


def _csfloat_api_keys() -> tuple[str, ...]:
    out: list[str] = []
    for raw in (CSFLOAT_API_KEY, CSFLOAT_API_KEY_2):
        if not raw:
            continue
        s = str(raw).strip()
        if s and s not in out:
            out.append(s)
    return tuple(out)


def _try_pick_key_index(keys: tuple[str, ...]) -> int | None:
    """First available key in round-robin order; None if all in cooldown."""
    mono = time.monotonic()
    n = len(keys)
    start = _csfloat_key_rr_i[0] % n
    for step in range(n):
        i = (start + step) % n
        if mono >= _key_cooldown_mono.get(i, 0.0):
            _csfloat_key_rr_i[0] = i + 1
            return i
    return None


def _apply_key_cooldown(key_index: int | None, err: str) -> None:
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
    _log(f"  [COOLDOWN] ключ {key_index + 1}: пауза ~{sec:.0f}s ({err})")
    _enter_slow_mode()


def _in_slow_delay_mode() -> bool:
    return time.monotonic() < _slow_until_mono


def _enter_slow_mode() -> None:
    """Extend slow jitter window (called on 429/403 cooldown)."""
    global _slow_until_mono
    ext = _runtime_float("SLOW_MODE_EXTEND_SEC", SLOW_MODE_EXTEND_SEC)
    nxt = time.monotonic() + ext
    if nxt > _slow_until_mono:
        _slow_until_mono = nxt
        _log(f"  [SLOW] увеличенные паузы (jitter) ~{ext / 60:.0f} мин")


def _sleep_inner() -> None:
    """Between HTTP chunks inside one skin (mix)."""
    if _in_slow_delay_mode():
        lo = _runtime_float("SLOW_INNER_MIN", SLOW_INNER_MIN)
        hi = _runtime_float("SLOW_INNER_MAX", SLOW_INNER_MAX)
    else:
        lo = _runtime_float("INNER_DELAY_MIN", INNER_DELAY_MIN)
        hi = _runtime_float("INNER_DELAY_MAX", INNER_DELAY_MAX)
    if hi <= lo:
        hi = lo + 0.1
    time.sleep(random.uniform(lo, hi))


def _sleep_item() -> None:
    """After one skin, before the next."""
    if _in_slow_delay_mode():
        lo = _runtime_float("SLOW_ITEM_MIN", SLOW_ITEM_MIN)
        hi = _runtime_float("SLOW_ITEM_MAX", SLOW_ITEM_MAX)
    else:
        lo = _runtime_float("ITEM_DELAY_MIN", ITEM_DELAY_MIN)
        hi = _runtime_float("ITEM_DELAY_MAX", ITEM_DELAY_MAX)
    if hi <= lo:
        hi = lo + 0.1
    time.sleep(random.uniform(lo, hi))


def _pick_csfloat_headers() -> tuple[dict, int | None]:
    """
    Pick Authorization for the next request: round-robin over keys not in cooldown.
    If only one key or all cooling, block until a key is free (then return).
    Returns (headers, key_index) for cooldown on 429/403; key_index None if no API key.
    """
    global _last_cf_key_tag
    h = {"User-Agent": "Mozilla/5.0"}
    keys = _csfloat_api_keys()
    if not keys:
        _last_cf_key_tag = ""
        return h, None

    while True:
        mono = time.monotonic()
        with _CSFLOAT_KEY_RR_LOCK:
            if len(keys) == 1:
                if mono >= _key_cooldown_mono.get(0, 0.0):
                    h["Authorization"] = keys[0]
                    _last_cf_key_tag = "1/1"
                    return h, 0
                wake = _key_cooldown_mono[0]
            else:
                picked = _try_pick_key_index(keys)
                if picked is not None:
                    h["Authorization"] = keys[picked]
                    _last_cf_key_tag = f"{picked + 1}/{len(keys)}"
                    return h, picked
                wake = min(_key_cooldown_mono.get(j, 0.0) for j in range(len(keys)))

        wait = max(0.05, wake - time.monotonic())
        _log(f"  [COOLDOWN] все ключи в паузе, ждём {wait:.0f} с…")
        time.sleep(wait)


def _api_msg_rate_limited(msg: str) -> bool:
    m = msg.lower()
    return "too many" in m or "rate" in m or "429" in m


_RETRYABLE = frozenset({"429", "403", "5xx", "net"})


def _fetch_listings_once(
    name: str,
    sort_by: str,
    lim: int,
    headers: dict,
    *,
    cursor: str | None = None,
) -> tuple[list[dict], str | None, str | None]:
    """Single HTTP attempt."""
    url = "https://csfloat.com/api/v1/listings"
    params = {
        "market_hash_name": name,
        "sort_by": sort_by,
        "limit": lim,
        "type": "buy_now",
    }
    if cursor:
        params["cursor"] = cursor
    try:
        r = requests.get(url, params=params, headers=headers, timeout=20)
        if r.status_code == 429:
            _log("  [WARN] HTTP 429 — rate limited.")
            return [], None, "429"
        if r.status_code == 403:
            _log("  [WARN] HTTP 403 — forbidden (key / IP?).")
            return [], None, "403"
        if r.status_code >= 500:
            _log(f"  [WARN] HTTP {r.status_code} — server error.")
            return [], None, "5xx"
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict) and ("error" in data or "message" in data):
            msg = str(data.get("error") or data.get("message") or "")
            _log(f"  [ERR] {name}: {msg}")
            if _api_msg_rate_limited(msg):
                return [], None, "429"
            return [], None, None
        if isinstance(data, list):
            return data, None, None
        out = data.get("data", []) if isinstance(data, dict) else []
        next_cursor = None
        if isinstance(data, dict):
            next_cursor = data.get("cursor") or data.get("next_cursor")
        return out, next_cursor, None
    except req_exc.Timeout:
        _log("  [WARN] Request timeout.")
        return [], None, "net"
    except req_exc.ConnectionError as e:
        _log(f"  [WARN] Connection error: {e}")
        return [], None, "net"
    except req_exc.HTTPError as e:
        _log(f"  [FATAL] HTTP error: {e}")
        return [], None, "http"
    except Exception as e:
        _log(f"  [ERR] {name}: {e}")
        return [], None, None


def _fetch_page_retry(
    name: str,
    sort_by: str,
    lim: int,
    *,
    cursor: str | None = None,
) -> tuple[list[dict], str | None, str | None]:
    """Retry one page until success or non-retryable error."""
    global _retry_fail_streak

    while True:
        headers, key_idx = _pick_csfloat_headers()
        out, next_cursor, err = _fetch_listings_once(
            name,
            sort_by,
            lim,
            headers,
            cursor=cursor,
        )
        if err in ("429", "403"):
            _apply_key_cooldown(key_idx, err)
        if not err:
            _retry_fail_streak = 0
            return out, next_cursor, None
        if err not in _RETRYABLE:
            return [], None, err
        _retry_fail_streak += 1
        wait_s = _retry_ladder_sec()
        _log(
            f"  [RETRY #{_retry_fail_streak}] {err}: pause {wait_s:.0f}s "
            "(retry ladder from runtime JSON)..."
        )
        time.sleep(wait_s)


def _listing_key(listing: dict) -> str:
    lid = listing.get("id")
    if lid is not None:
        return f"id:{lid}"
    item = listing.get("item", {})
    asset_id = item.get("asset_id") or listing.get("asset_id")
    if asset_id is not None:
        return f"asset:{asset_id}"
    inspect_link = item.get("inspect_link") or listing.get("inspect_link")
    if inspect_link:
        return f"inspect:{inspect_link}"
    return f"anon:{id(listing)}"


def _append_unique_rows(
    dst: list[dict],
    seen: set[str],
    batch: list[dict],
    *,
    source_sort: str,
    limit: int | None = None,
) -> int:
    added = 0
    for listing in batch:
        key = _listing_key(listing)
        if key in seen:
            continue
        seen.add(key)
        row = dict(listing)
        row["_source_sort"] = source_sort
        dst.append(row)
        added += 1
        if limit is not None and len(dst) >= limit:
            break
    return added


def _trim_rows_by_price(
    rows: list[dict],
    *,
    drop_lowest_fraction: float = 0.0,
    drop_highest_fraction: float = 0.0,
) -> list[dict]:
    if not rows:
        return []
    n = len(rows)
    drop_low = min(n - 1, int(math.floor(n * max(0.0, drop_lowest_fraction))))
    drop_high = min(n - 1 - drop_low, int(math.floor(n * max(0.0, drop_highest_fraction))))
    if drop_low <= 0 and drop_high <= 0:
        return list(rows)
    ordered = sorted(rows, key=lambda x: (x.get("price") or 0, str(x.get("id") or "")))
    hi_idx = len(ordered) - drop_high if drop_high > 0 else len(ordered)
    return ordered[drop_low:hi_idx]


def _collect_sort_rows(
    name: str,
    *,
    sort_by: str,
    target_rows: int,
    seen: set[str] | None = None,
) -> tuple[list[dict], str | None, list[str]]:
    seen_local = seen if seen is not None else set()
    rows: list[dict] = []
    key_trace: list[str] = []
    cursor: str | None = None
    page_limit = _effective_page_limit()
    empty_pages = 0

    while len(rows) < target_rows:
        remaining = target_rows - len(rows)
        lim = min(page_limit, max(1, remaining))
        batch, next_cursor, err = _fetch_page_retry(
            name,
            sort_by,
            lim,
            cursor=cursor,
        )
        key_trace.append(_last_cf_key_tag or "?")
        if err:
            return [], err, key_trace
        before = len(rows)
        _append_unique_rows(rows, seen_local, batch, source_sort=sort_by)
        if len(batch) < lim:
            empty_pages += 1
        else:
            empty_pages = 0
        if len(rows) >= target_rows:
            break
        if not next_cursor:
            break
        cursor = next_cursor
        if len(rows) == before and not batch:
            break
        if empty_pages >= 2:
            break
        _sleep_inner()
    return rows, None, key_trace


def fetch_listings(
    name: str, sort_by: str = DEFAULT_SORT, cap: int | None = None
) -> tuple[list[dict], str | None, list[str]]:
    """
    Returns (listings, fatal_error_tag).
    Transient errors (429, 403, 5xx, net): пауза по «лесенке» RETRY_LADDER_* в JSON,
    повтор без лимита попыток (останов — только Ctrl+C / kill).
    """
    target_rows = max(1, int(cap if cap is not None else _target_unique()))
    rows, err, key_trace = _collect_sort_rows(
        name,
        sort_by=sort_by,
        target_rows=target_rows,
    )
    return rows, err, key_trace

    while True:
        headers, key_idx = _pick_csfloat_headers()
        out, err = _fetch_listings_once(name, sort_by, lim, headers)
        if err in ("429", "403"):
            _apply_key_cooldown(key_idx, err)
        if not err:
            _retry_fail_streak = 0
            return out, None
        if err not in _RETRYABLE:
            return [], err
        _retry_fail_streak += 1
        wait_s = _retry_ladder_sec()
        _log(
            f"  [RETRY #{_retry_fail_streak}] {err}: пауза {wait_s:.0f}s "
            f"(лесенка min/step/max из runtime JSON)…"
        )
        time.sleep(wait_s)


def _mix_final_targets(target_unique: int, shards: list[dict]) -> list[int]:
    total_share = sum(float(s.get("share") or 0.0) for s in shards) or 1.0
    raw = [target_unique * float(s.get("share") or 0.0) / total_share for s in shards]
    base = [max(1, int(math.floor(v))) for v in raw]
    diff = target_unique - sum(base)
    order = sorted(
        range(len(shards)),
        key=lambda i: raw[i] - math.floor(raw[i]),
        reverse=True,
    )
    idx = 0
    while diff > 0 and order:
        base[order[idx % len(order)]] += 1
        diff -= 1
        idx += 1
    return base


def fetch_listings_mixed(name: str, *, target_unique: int | None = None) -> tuple[list[dict], str | None, str]:
    """
    Several sort orders + dedupe by listing id (API has no random sort).
    Spreads sample across deals, expensive tail, and recency.
    Each chunk is a separate API call — keys rotate per call (see key_trace in return).
    """
    shards = _runtime_mix_shards()
    target_unique = _target_unique(target_unique)
    overfetch = max(1.0, _runtime_float("MIX_OVERFETCH_FACTOR", DEFAULT_MIX_OVERFETCH_FACTOR))
    shard_targets = _mix_final_targets(target_unique, shards)
    seen: set[str] = set()
    out: list[dict] = []
    key_parts: list[str] = []

    for idx, shard in enumerate(shards):
        raw_target = shard_targets[idx]
        shard_goal = max(raw_target, int(math.ceil(raw_target * overfetch)))
        batch, err, trace = _collect_sort_rows(
            name,
            sort_by=str(shard["sort_by"]),
            target_rows=shard_goal,
            seen=set(seen),
        )
        key_parts.extend(trace)
        if err:
            return [], err, "->".join(key_parts)
        trimmed = _trim_rows_by_price(
            batch,
            drop_lowest_fraction=float(shard.get("drop_lowest_price_fraction") or 0.0),
            drop_highest_fraction=float(shard.get("drop_highest_price_fraction") or 0.0),
        )
        if raw_target > 0 and len(trimmed) > raw_target:
            trimmed = trimmed[:raw_target]
        _append_unique_rows(
            out,
            seen,
            trimmed,
            source_sort=str(shard["sort_by"]),
            limit=target_unique,
        )
        if len(out) >= target_unique:
            return out[:target_unique], None, "->".join(key_parts)
        if idx < len(shards) - 1:
            _sleep_inner()

    if len(out) < target_unique and shards:
        fill_sort = str(shards[0]["sort_by"])
        batch, err, trace = _collect_sort_rows(
            name,
            sort_by=fill_sort,
            target_rows=target_unique,
            seen=set(seen),
        )
        key_parts.extend(trace)
        if err:
            return [], err, "->".join(key_parts)
        _append_unique_rows(out, seen, batch, source_sort=fill_sort, limit=target_unique)
    return out[:target_unique], None, "->".join(key_parts)

    chunks = (
        ("best_deal", 30),
        ("most_recent", 30),
    )
    seen: set[str | int] = set()
    out: list[dict] = []
    key_parts: list[str] = []
    nchunks = len(chunks)
    for ci, (sort, n) in enumerate(chunks):
        batch, err = fetch_listings(name, sort_by=sort, cap=n)
        key_parts.append(_last_cf_key_tag or "?")
        if err:
            return [], err, "->".join(key_parts)
        for L in batch:
            lid = L.get("id")
            if lid is None or lid in seen:
                continue
            seen.add(lid)
            out.append(L)
            if len(out) >= _runtime_int("LIMIT", LIMIT):
                return out, None, "->".join(key_parts)
        if ci < nchunks - 1:
            _sleep_inner()
    return out, None, "->".join(key_parts)


def _cents_to_major(v: object) -> float | None:
    try:
        return float(v) / 100.0
    except (TypeError, ValueError):
        return None


def parse_listing(listing: dict) -> dict:
    item = listing.get("item", {})
    ref = listing.get("reference", {})
    stickers = item.get("stickers", [])
    cur = ref.get("currency") or listing.get("currency")
    return {
        "listing_id": listing.get("id"),
        "created_at": listing.get("created_at"),
        "state": listing.get("state"),
        "source_sort": listing.get("_source_sort"),
        "price": _cents_to_major(listing.get("price")) or 0.0,
        "float_value": item.get("float_value"),
        "predicted_price": _cents_to_major(ref.get("predicted_price")),
        "base_price": _cents_to_major(ref.get("base_price")),
        "currency": cur if cur is not None else None,
        "quantity": ref.get("quantity"),
        "sticker_count": len(stickers),
        "sticker_value_usd": sum(
            (_cents_to_major(s.get("scm", {}).get("price")) or 0.0) for s in stickers
        ),
        "watchers": listing.get("watchers", 0),
    }


def summarise(name: str, parsed: list[dict]) -> dict:
    if not parsed:
        return {"item": name, "reference_currency": None, "n_listings": 0}
    df = pd.DataFrame(parsed)
    pred = df["predicted_price"].dropna()
    ask  = df["price"].dropna()
    base = df["base_price"].dropna().iloc[0] if df["base_price"].notna().any() else None
    qty  = df["quantity"].dropna().iloc[0] if df["quantity"].notna().any() else None
    cur_col = df["currency"].dropna() if "currency" in df.columns else None
    ref_cur = cur_col.iloc[0] if cur_col is not None and len(cur_col) else None
    n = len(pred)

    if n > 1:
        pm, ps = pred.mean(), pred.std()
        pcv = ps / pm if pm > 0 else None
    elif n == 1:
        pm, ps, pcv = pred.iloc[0], 0.0, 0.0
    else:
        pm = ps = pcv = None

    if base and base > 0 and n:
        f = pred / base
        fm, fs = f.mean(), (f.std() if n > 1 else 0.0)
        fcv = fs / fm if fm > 0 else None
    else:
        fm = fs = fcv = None

    def r4(x): return round(x, 4) if x is not None else None
    return {
        "item": name, "base_price": base, "reference_currency": ref_cur, "float_qty": qty,
        "n_listings": len(parsed),
        "ask_min": ask.min() if len(ask) else None,
        "ask_median": ask.median() if len(ask) else None,
        "ask_max": ask.max() if len(ask) else None,
        "pred_mean": r4(pm), "pred_std": r4(ps), "pred_cv": r4(pcv),
        "pred_min": pred.min() if n else None, "pred_max": pred.max() if n else None,
        "factor_mean": r4(fm), "factor_std": r4(fs), "factor_cv": r4(fcv),
    }

# -- Load skin list -----------------------------------------------------------

def load_items(path: str) -> list[str]:
    spec = importlib.util.spec_from_file_location("skin_list", path)
    mod  = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.ITEMS


def resolve_list_path(raw: str) -> str:
    """Resolve path: absolute / cwd / repo / skin_cands / script dir. Adds .py if omitted."""
    variants = [raw]
    if not raw.lower().endswith(".py"):
        variants.append(raw + ".py")

    tried: list[str] = []
    for name in variants:
        if os.path.isabs(name) and os.path.isfile(name):
            return name
        if os.path.isfile(name):
            return os.path.abspath(name)
        for base in (CANDS_DIR, _SCRIPT_DIR, _REPO_ROOT):
            cand = os.path.join(base, name)
            tried.append(cand)
            if os.path.isfile(cand):
                return cand

    raise FileNotFoundError(
        f"Skin list not found: {raw!r}\n  Tried:\n    " + "\n    ".join(tried[-8:])
    )


def _default_list_path(fallback: str) -> str:
    raw = _runtime_str("DEFAULT_LIST_PATH", fallback)
    try:
        return resolve_list_path(raw)
    except FileNotFoundError:
        return fallback

# -- Fetch batch --------------------------------------------------------------

_FATAL_TO_EXIT = {
    "429": EXIT_RATE_LIMIT,
    "403": EXIT_RATE_LIMIT,
    "5xx": EXIT_SERVER,
    "net": EXIT_NETWORK,
    "http": EXIT_CLIENT,
}


def fetch_batch(
    items: list[str],
    *,
    use_mix: bool,
    sort_by: str,
    target_unique: int,
) -> tuple[dict[str, list[dict]], int]:
    """
    Returns (results, exit_code). exit_code 0 = ok, else fatal API/site error — do not save.
    Strictly sequential (one skin at a time, no thread pool) so two API keys alternate cleanly.
    """
    results: dict[str, list[dict]] = {}
    fatal_exit = 0
    total = len(items)
    t0 = time.time()

    _log(f"--- Прогон: {total} скинов подряд ---")
    _log(f"Прогресс также пишется в: {os.path.join(SAVE_DIR, '_screener_progress.log')}")
    try:
        plog = os.path.join(SAVE_DIR, "_screener_progress.log")
        with open(plog, "w", encoding="utf-8") as f:
            f.write(f"# старт прогона, всего скинов: {total}\n")
            f.flush()
    except Exception:
        pass

    lim_cap = target_unique
    for i, name in enumerate(items):
        if fatal_exit:
            break
        t_skin = time.time()
        if use_mix:
            raw, err, key_trace = fetch_listings_mixed(name, target_unique=target_unique)
        else:
            raw, err, trace = fetch_listings(name, sort_by=sort_by, cap=target_unique)
            key_trace = "->".join(trace)
        if err:
            fatal_exit = _FATAL_TO_EXIT.get(err, EXIT_CLIENT)
            raw = []
        parsed = [parse_listing(l) for l in raw]
        results[name] = parsed
        tag = "mix" if use_mix else sort_by
        dt = time.time() - t_skin
        line = _skin_done_line(
            i + 1,
            total,
            name,
            ok=not err,
            n_listings=len(raw),
            lim_cap=lim_cap,
            dt_s=dt,
            err=err,
            tag=tag,
            key_trace=key_trace,
        )
        _log(line)
        _write_progress_file(line)
        if i < total - 1:
            _sleep_item()

    elapsed = time.time() - t0
    code = fatal_exit
    rows = sum(len(v) for v in results.values())
    if code:
        _log(
            f"ABORT {elapsed:.0f}s — скинов: {len(results)}/{total}, строк листингов: {rows} (exit {code})."
        )
    else:
        _log(
            f"Готово: {total} скинов, ~{rows} строк листингов, {elapsed:.0f}s "
            f"({elapsed / max(total, 1):.1f}s/скин)."
        )
    return results, code


def _init_progress_log(total: int) -> None:
    _log(f"--- ÐŸÑ€Ð¾Ð³Ð¾Ð½: {total} ÑÐºÐ¸Ð½Ð¾Ð² Ð¿Ð¾Ð´Ñ€ÑÐ´ ---")
    _log(f"ÐŸÑ€Ð¾Ð³Ñ€ÐµÑÑ Ñ‚Ð°ÐºÐ¶Ðµ Ð¿Ð¸ÑˆÐµÑ‚ÑÑ Ð²: {os.path.join(SAVE_DIR, '_screener_progress.log')}")
    try:
        plog = os.path.join(SAVE_DIR, "_screener_progress.log")
        with open(plog, "w", encoding="utf-8") as f:
            f.write(f"# ÑÑ‚Ð°Ñ€Ñ‚ Ð¿Ñ€Ð¾Ð³Ð¾Ð½Ð°, Ð²ÑÐµÐ³Ð¾ ÑÐºÐ¸Ð½Ð¾Ð²: {total}\n")
            f.flush()
    except Exception:
        pass


def _fetch_single_item(
    name: str,
    *,
    use_mix: bool,
    sort_by: str,
    target_unique: int,
) -> tuple[list[dict], str | None, str, float]:
    t_skin = time.time()
    if use_mix:
        raw, err, key_trace = fetch_listings_mixed(name, target_unique=target_unique)
    else:
        raw, err, trace = fetch_listings(name, sort_by=sort_by, cap=target_unique)
        key_trace = "->".join(trace)
    parsed = [parse_listing(l) for l in raw]
    return parsed, err, key_trace, time.time() - t_skin


def run_batch_incremental(
    items: list[str],
    *,
    mode: str,
    use_mix: bool,
    sort_by: str,
    target_unique: int,
    skip_known_ids: bool,
    ignore_existing_items: bool,
) -> tuple[pd.DataFrame, int]:
    fatal_exit = 0
    total = len(items)
    t0 = time.time()
    lim_cap = target_unique
    last_sdf = pd.DataFrame()
    run_results: dict[str, list[dict]] = {}
    known_ids_by_item = _known_listing_ids_by_item() if skip_known_ids else {}
    existing_items = _existing_item_names() if ignore_existing_items else set()
    base_frames = {} if mode == "create" else _load_existing_panel_frames()
    base_summary_df = pd.DataFrame() if mode == "create" else _load_existing_summary_df()

    _init_progress_log(total)

    for i, name in enumerate(items):
        if ignore_existing_items and name in existing_items:
            tag = "skip_existing_item"
            if skip_known_ids:
                tag += ",skip_known_ids=on"
            line = f'{i + 1}/{total}  "{name}"  skipped  - already exists, {tag}'
            _log(line)
            _write_progress_file(line)
            continue
        parsed, err, key_trace, dt = _fetch_single_item(
            name,
            use_mix=use_mix,
            sort_by=sort_by,
            target_unique=target_unique,
        )
        skipped_known = 0
        if not err and skip_known_ids:
            parsed, skipped_known = _filter_known_listing_ids(name, parsed, known_ids_by_item)
        if not err:
            run_results[name] = parsed
            last_sdf = _write_incremental_state(base_frames, base_summary_df, run_results)
            existing_items.add(name)
        else:
            fatal_exit = _FATAL_TO_EXIT.get(err, EXIT_CLIENT)

        tag = "mix" if use_mix else sort_by
        if not err:
            write_tag = "create_block" if mode == "create" else "merge_block"
            tag = f"{tag},{write_tag}"
        if not err and skip_known_ids:
            tag = f"{tag},skip_known_ids={skipped_known}"
        line = _skin_done_line(
            i + 1,
            total,
            name,
            ok=not err,
            n_listings=len(parsed),
            lim_cap=lim_cap,
            dt_s=dt,
            err=err,
            tag=tag,
            key_trace=key_trace,
        )
        _log(line)
        _write_progress_file(line)
        if fatal_exit:
            break
        if i < total - 1:
            _sleep_item()

    elapsed = time.time() - t0
    if fatal_exit:
        _log(
            f"ABORT {elapsed:.0f}s â€” ÑÐ¾Ñ…Ñ€Ð°Ð½ÐµÐ½Ð¾ ÑÐºÐ¸Ð½Ð¾Ð²: "
            f"{len(last_sdf) if not last_sdf.empty else 0}/{total} (exit {fatal_exit})."
        )
    else:
        _log(
            f"Ð“Ð¾Ñ‚Ð¾Ð²Ð¾: {total} ÑÐºÐ¸Ð½Ð¾Ð², {elapsed:.0f}s "
            f"({elapsed / max(total, 1):.1f}s/ÑÐºÐ¸Ð½)."
        )

    if last_sdf.empty:
        summary_path = os.path.join(SAVE_DIR, "_summary.csv")
        if os.path.isfile(summary_path):
            last_sdf = pd.read_csv(summary_path)
    return last_sdf, fatal_exit

# -- Save / merge panels ------------------------------------------------------

def build_new_panels(items, results):
    panels = {m: {} for m in PANELS}
    summary = []
    for name in items:
        parsed = results.get(name, [])
        for metric in PANELS:
            panels[metric][name] = [row.get(metric) for row in parsed]
        summary.append(summarise(name, parsed))
    return panels, summary


def save_panels(panels, summary):
    for metric, fname in PANELS.items():
        cols = panels[metric]
        max_len = max((len(v) for v in cols.values()), default=0)
        if max_len == 0:
            continue
        df = pd.DataFrame({k: _pad_values(metric, list(v), max_len) for k, v in cols.items()})
        df.to_csv(os.path.join(SAVE_DIR, fname), index=False)

    sdf = pd.DataFrame(summary)
    sdf.to_csv(os.path.join(SAVE_DIR, "_summary.csv"), index=False)
    return sdf


def _load_existing_summary_df() -> pd.DataFrame:
    path = os.path.join(SAVE_DIR, "_summary.csv")
    if os.path.isfile(path):
        try:
            return pd.read_csv(path)
        except Exception:
            pass
    return pd.DataFrame()


def _structural_pad_value(metric: str):
    return None if metric in _STRING_PANELS else STRUCTURAL_GAP_SENTINEL


def _pad_values(metric: str, values: list[object], target_len: int) -> list[object]:
    if len(values) >= target_len:
        return list(values)
    return list(values) + [_structural_pad_value(metric)] * (target_len - len(values))


def _is_structural_gap(metric: str, value: object) -> bool:
    if metric in _STRING_PANELS:
        return False
    if value is None or pd.isna(value):
        return False
    try:
        return float(value) == float(STRUCTURAL_GAP_SENTINEL)
    except (TypeError, ValueError):
        return False


def _load_existing_panel_frames() -> dict[str, pd.DataFrame]:
    frames: dict[str, pd.DataFrame] = {}
    for metric, fname in PANELS.items():
        path = os.path.join(SAVE_DIR, fname)
        if os.path.isfile(path):
            frames[metric] = pd.read_csv(path, low_memory=False)
    return frames


def _rows_from_panel_frames(item: str, frames: dict[str, pd.DataFrame]) -> dict[str, list[object]]:
    out: dict[str, list[object]] = {}
    for metric in PANELS:
        df = frames.get(metric)
        if df is None or item not in df.columns:
            out[metric] = []
            continue
        out[metric] = df[item].tolist()
    return out


def _known_listing_ids_by_item() -> dict[str, set[str]]:
    path = os.path.join(SAVE_DIR, PANELS["listing_id"])
    if not os.path.isfile(path):
        return {}
    try:
        df = pd.read_csv(path, low_memory=False)
    except Exception:
        return {}
    out: dict[str, set[str]] = {}
    for col in df.columns:
        vals = {
            str(v)
            for v in df[col].tolist()
            if not pd.isna(v)
            and str(v).strip() not in {"", "None", "nan", str(STRUCTURAL_GAP_SENTINEL), f"{STRUCTURAL_GAP_SENTINEL}.0"}
        }
        out[col] = vals
    return out


def _existing_item_names() -> set[str]:
    path = os.path.join(SAVE_DIR, PANELS["listing_id"])
    if os.path.isfile(path):
        try:
            return set(pd.read_csv(path, nrows=0).columns)
        except Exception:
            pass
    summary_path = os.path.join(SAVE_DIR, "_summary.csv")
    if os.path.isfile(summary_path):
        try:
            sdf = pd.read_csv(summary_path, usecols=["item"])
            return {str(v) for v in sdf["item"].dropna().tolist()}
        except Exception:
            pass
    return set()


def _filter_known_listing_ids(
    item: str,
    parsed: list[dict],
    known_ids_by_item: dict[str, set[str]],
) -> tuple[list[dict], int]:
    known = known_ids_by_item.get(item, set())
    if not known:
        fresh_ids = {
            str(row["listing_id"])
            for row in parsed
            if row.get("listing_id") is not None and str(row.get("listing_id")).strip()
        }
        if fresh_ids:
            known_ids_by_item[item] = set(fresh_ids)
        return parsed, 0

    kept: list[dict] = []
    skipped = 0
    seen_new = set(known)
    for row in parsed:
        lid = row.get("listing_id")
        lid_s = str(lid).strip() if lid is not None else ""
        if lid_s and lid_s in seen_new:
            skipped += 1
            continue
        kept.append(row)
        if lid_s:
            seen_new.add(lid_s)
    known_ids_by_item[item] = seen_new
    return kept, skipped


def _parsed_rows_from_frames(item: str, frames: dict[str, pd.DataFrame]) -> list[dict]:
    cols = _rows_from_panel_frames(item, frames)
    nrows = max((len(v) for v in cols.values()), default=0)
    rows: list[dict] = []
    for idx in range(nrows):
        row: dict[str, object] = {}
        any_value = False
        for metric in PANELS:
            values = cols.get(metric, [])
            value = values[idx] if idx < len(values) else None
            if pd.isna(value):
                value = None
            elif _is_structural_gap(metric, value):
                value = None
            if value is not None:
                any_value = True
            row[metric] = value
        if any_value:
            rows.append(row)
    return rows


def _is_available_listing_slot(value: object) -> bool:
    return _is_structural_gap("listing_id", value)


def _base_column_values(
    metric: str,
    item: str,
    base_frames: dict[str, pd.DataFrame],
    old_block_len: int,
) -> list[object]:
    df = base_frames.get(metric)
    if df is not None and item in df.columns:
        vals = df[item].tolist()
        if len(vals) < old_block_len:
            vals = vals + [_structural_pad_value(metric)] * (old_block_len - len(vals))
        return vals
    return [_structural_pad_value(metric)] * old_block_len


def _merge_fill_positions(
    item: str,
    base_frames: dict[str, pd.DataFrame],
    old_block_len: int,
) -> list[int]:
    listing_df = base_frames.get("listing_id")
    if listing_df is not None and item in listing_df.columns:
        listing_vals = listing_df[item].tolist()
        if len(listing_vals) < old_block_len:
            listing_vals = listing_vals + [STRUCTURAL_GAP_SENTINEL] * (old_block_len - len(listing_vals))
        return [idx for idx, value in enumerate(listing_vals[:old_block_len]) if _is_available_listing_slot(value)]
    return list(range(old_block_len))


def _compact_merge_columns(
    all_items: list[str],
    base_frames: dict[str, pd.DataFrame],
    new_panels: dict[str, dict[str, list[object]]],
) -> tuple[dict[str, dict[str, list[object]]], int]:
    old_block_len = 0
    for df in base_frames.values():
        old_block_len = max(old_block_len, len(df))

    fill_positions_by_item = {
        item: _merge_fill_positions(item, base_frames, old_block_len)
        for item in all_items
    }

    merged_by_metric: dict[str, dict[str, list[object]]] = {metric: {} for metric in PANELS}
    final_len = old_block_len

    for item in all_items:
        fill_positions = fill_positions_by_item[item]
        for metric in PANELS:
            old_vals = _base_column_values(metric, item, base_frames, old_block_len)
            new_vals = list(new_panels.get(metric, {}).get(item, []))
            merged_vals = list(old_vals)

            n_fill = min(len(new_vals), len(fill_positions))
            for pos, value in zip(fill_positions[:n_fill], new_vals[:n_fill]):
                merged_vals[pos] = value
            if n_fill < len(new_vals):
                merged_vals.extend(new_vals[n_fill:])

            merged_by_metric[metric][item] = merged_vals
            if len(merged_vals) > final_len:
                final_len = len(merged_vals)

    for metric in PANELS:
        for item in all_items:
            merged_by_metric[metric][item] = _pad_values(
                metric,
                merged_by_metric[metric][item],
                final_len,
            )

    return merged_by_metric, final_len


def merge_panels(new_panels, new_summary):
    existing_frames = _load_existing_panel_frames()
    old_items: list[str] = []
    for df in existing_frames.values():
        for col in df.columns:
            if col not in old_items:
                old_items.append(col)
    new_items: list[str] = []
    for cols in new_panels.values():
        for col in cols:
            if col not in new_items:
                new_items.append(col)
    all_items = list(old_items)
    for item in new_items:
        if item not in all_items:
            all_items.append(item)

    merged_cols_by_metric, _ = _compact_merge_columns(all_items, existing_frames, new_panels)

    for metric, fname in PANELS.items():
        path = os.path.join(SAVE_DIR, fname)
        pd.DataFrame(merged_cols_by_metric[metric]).to_csv(path, index=False)

    summary_path = os.path.join(SAVE_DIR, "_summary.csv")
    impacted_items = [row["item"] for row in new_summary]
    combined_frames = _load_existing_panel_frames()
    combined_summary = [
        summarise(item, _parsed_rows_from_frames(item, combined_frames))
        for item in impacted_items
    ]
    if os.path.isfile(summary_path):
        old_sdf = pd.read_csv(summary_path)
        old_sdf = old_sdf[~old_sdf["item"].isin(impacted_items)]
        sdf = pd.concat([old_sdf, pd.DataFrame(combined_summary)], ignore_index=True)
    else:
        sdf = pd.DataFrame(combined_summary)
    sdf.to_csv(summary_path, index=False)
    return sdf


def _write_incremental_state(
    base_frames: dict[str, pd.DataFrame],
    base_summary_df: pd.DataFrame,
    run_results: dict[str, list[dict]],
) -> pd.DataFrame:
    run_items = list(run_results.keys())
    run_panels, _ = build_new_panels(run_items, run_results)

    base_items: list[str] = []
    for df in base_frames.values():
        for col in df.columns:
            if col not in base_items:
                base_items.append(col)
    all_items = list(base_items)
    for item in run_items:
        if item not in all_items:
            all_items.append(item)

    merged_cols_by_metric, _ = _compact_merge_columns(all_items, base_frames, run_panels)

    for metric, fname in PANELS.items():
        path = os.path.join(SAVE_DIR, fname)
        merged_cols = merged_cols_by_metric.get(metric, {})
        if merged_cols:
            pd.DataFrame(merged_cols).to_csv(path, index=False)

    combined_frames = _load_existing_panel_frames()
    impacted_items = run_items
    combined_summary = [
        summarise(item, _parsed_rows_from_frames(item, combined_frames))
        for item in impacted_items
    ]
    if base_summary_df.empty:
        sdf = pd.DataFrame(combined_summary)
    else:
        old_sdf = base_summary_df[~base_summary_df["item"].isin(impacted_items)]
        sdf = pd.concat([old_sdf, pd.DataFrame(combined_summary)], ignore_index=True)
    sdf.to_csv(os.path.join(SAVE_DIR, "_summary.csv"), index=False)
    return sdf

    for metric, fname in PANELS.items():
        path = os.path.join(SAVE_DIR, fname)
        new_cols = new_panels[metric]
        if not new_cols:
            continue

        if os.path.exists(path):
            old = pd.read_csv(path)
            # drop columns that will be replaced
            old = old.drop(columns=[c for c in new_cols if c in old.columns], errors="ignore")
        else:
            old = pd.DataFrame()

        max_new = max(len(v) for v in new_cols.values())
        new_df = pd.DataFrame({k: v + [None]*(max_new - len(v)) for k, v in new_cols.items()})

        # align row count (rebuild DataFrames — pandas rejects assigning a longer list than index)
        n_old, n_new = len(old), len(new_df)
        if n_old > n_new and len(new_df.columns) > 0:
            pad = n_old - n_new
            new_df = pd.DataFrame(
                {c: list(new_df[c]) + [None] * pad for c in new_df.columns}
            )
        elif n_new > n_old and not old.empty:
            pad = n_new - n_old
            old = pd.DataFrame(
                {c: list(old[c]) + [None] * pad for c in old.columns}
            )

        merged = pd.concat([old, new_df], axis=1) if not old.empty else new_df
        merged.to_csv(path, index=False)

    # merge summary
    summary_path = os.path.join(SAVE_DIR, "_summary.csv")
    new_sdf = pd.DataFrame(new_summary)
    if os.path.exists(summary_path):
        old_sdf = pd.read_csv(summary_path)
        old_sdf = old_sdf[~old_sdf["item"].isin(new_sdf["item"])]
        sdf = pd.concat([old_sdf, new_sdf], ignore_index=True)
    else:
        sdf = new_sdf
    sdf.to_csv(summary_path, index=False)
    return sdf


def parse_cli(argv: list[str]):
    """Returns (mode, use_mix, sort_by, target_override, skip_known_ids, ignore_existing_items, rest_args)."""
    mode = "create"
    use_mix = _runtime_bool("DEFAULT_USE_MIX", True)
    sort_by = _runtime_str("DEFAULT_SORT", DEFAULT_SORT)
    target_override: int | None = None
    skip_known_ids = _runtime_bool("DEFAULT_SKIP_KNOWN_IDS", False)
    ignore_existing_items = _runtime_bool("DEFAULT_IGNORE_EXISTING_ITEMS", False)
    rest = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--merge":
            mode = "merge"
        elif a == "--create":
            mode = "create"
        elif a == "--mix":
            use_mix = True
        elif a == "--no-mix":
            use_mix = False
        elif a == "--sort":
            if i + 1 >= len(argv):
                sys.exit("--sort needs a value, e.g. --sort highest_price")
            sort_by = argv[i + 1]
            i += 1
        elif a == "--target":
            if i + 1 >= len(argv):
                sys.exit("--target needs a value, e.g. --target 400")
            try:
                target_override = max(1, int(argv[i + 1]))
            except ValueError:
                sys.exit("--target must be an integer")
            i += 1
        elif a == "--skip-known-ids":
            skip_known_ids = True
        elif a == "--allow-known-ids":
            skip_known_ids = False
        elif a == "--ignore-existing-items":
            ignore_existing_items = True
        elif a == "--allow-existing-items":
            ignore_existing_items = False
        else:
            rest.append(a)
        i += 1
    if sort_by not in VALID_SORTS:
        sys.exit(f"Unknown --sort {sort_by!r}. Allowed: {', '.join(VALID_SORTS)}")
    return mode, use_mix, sort_by, target_override, skip_known_ids, ignore_existing_items, rest

# -- Main ---------------------------------------------------------------------

def _configure_stdio_utf8() -> None:
    """Windows cp1252 / Jupyter pipes: avoid UnicodeEncodeError on print."""
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name)
        reconf = getattr(stream, "reconfigure", None)
        if reconf:
            try:
                # line_buffering: иначе pipe от Jupyter буферит вывод до ~8K или конца процесса
                lb = True  # stdout и stderr — сразу видно прогресс
                reconf(
                    encoding="utf-8",
                    errors="replace",
                    line_buffering=lb,
                    write_through=lb,
                )
                continue
            except Exception:
                pass
        buf = getattr(stream, "buffer", None)
        if buf is not None:
            try:
                setattr(
                    sys,
                    name,
                    io.TextIOWrapper(
                        buf,
                        encoding="utf-8",
                        errors="replace",
                        line_buffering=True,
                        write_through=True,
                    ),
                )
            except Exception:
                pass


def main():
    _refresh_runtime_paths()
    _configure_stdio_utf8()
    args = sys.argv[1:]
    _defaults = (
        os.path.join(CANDS_DIR, "batch_01_ak1.py"),
        os.path.join(CANDS_DIR, "batch_01_ak47.py"),
        os.path.join(_SCRIPT_DIR, "skins_candidates.py"),
    )
    fallback_list = next((p for p in _defaults if os.path.isfile(p)), _defaults[0])
    default_list = _default_list_path(fallback_list)

    mode, use_mix, sort_by, target_override, skip_known_ids, ignore_existing_items, args = parse_cli(args)
    target_unique = _target_unique(target_override)

    raw = args[0] if args else default_list
    list_path = resolve_list_path(raw)

    _log(f"[skin_screener] загрузка списка (import {os.path.basename(list_path)})…")
    items = load_items(list_path)
    sort_desc = _describe_mix() if use_mix else sort_by
    n_items = len(items)
    _log(f"Mode: {mode} | sort: {sort_desc}")
    _log(f"Target unique/listings per skin: {target_unique} | page_limit: {_effective_page_limit()}")
    _log(f"Skip known listing ids across runs: {skip_known_ids}")
    _log(f"Ignore items already present in data: {ignore_existing_items}")
    _log(f"Список: {n_items} скинов из {list_path}")
    _rp = _runtime_config_path()
    if os.path.isfile(_rp):
        _log(f"Timing JSON: {_rp}")
    nkeys = len(_csfloat_api_keys())
    if nkeys > 1:
        _log(f"CSFloat: {nkeys} API keys — round-robin per request")
    elif nkeys == 0:
        _log("CSFloat: нет API ключа — проверь local_secrets / CSFLOAT_API_KEY")
    _log("")

    if mode == "create":
        if os.path.exists(SAVE_DIR):
            _log(f"[skin_screener] create: удаляю {SAVE_DIR} (может занять время)…")
            shutil.rmtree(SAVE_DIR)
    os.makedirs(SAVE_DIR, exist_ok=True)

    summary_path = os.path.join(SAVE_DIR, "_summary.csv")
    if mode == "merge" and os.path.isfile(summary_path):
        try:
            prev_df = pd.read_csv(summary_path)
            prev_n = len(prev_df)
            _log(
                f"Merge: в {SAVE_DIR} уже есть _summary.csv ({prev_n} строк) — "
                "колонки по текущему списку будут добавлены/обновлены."
            )
        except Exception:
            pass

    sdf, err_code = run_batch_incremental(
        items,
        mode=mode,
        use_mix=use_mix,
        sort_by=sort_by,
        target_unique=target_unique,
        skip_known_ids=skip_known_ids,
        ignore_existing_items=ignore_existing_items,
    )
    if err_code:
        sys.exit(err_code)
    _log(f"Saved incrementally into {SAVE_DIR}/")

    valid = sdf.dropna(subset=["pred_cv"]).sort_values("pred_cv")
    if not valid.empty:
        _log("-- Most homogeneous (lowest pred_cv) --")
        _log(valid.head(20)[["item", "base_price", "pred_mean", "pred_cv",
                              "factor_mean", "factor_cv", "float_qty", "n_listings"]].to_string(index=False))


def run_cli(argv: list[str]) -> int:
    """
    Запуск из Jupyter/IDE без subprocess: print сразу попадает в вывод ячейки.
    Передай argv как у интерпретатора: [путь/к/skin_screener.py, флаги..., список_скинов.py].
    Возвращает код выхода (0 = ок).
    """
    old_argv = sys.argv[:]
    try:
        sys.argv = argv[:]
        _refresh_runtime_paths()
        _configure_stdio_utf8()
        _log("[skin_screener] in-process: заходим в main…")
        main()
        return 0
    except SystemExit as e:
        c = e.code
        if c is None:
            return 0
        if isinstance(c, int):
            return c
        return 1
    except BaseException:
        import traceback

        traceback.print_exc()
        return 1
    finally:
        sys.argv = old_argv


if __name__ == "__main__":
    try:
        _refresh_runtime_paths()
        _configure_stdio_utf8()
        # До этого момента тишина = грузятся pandas/requests и т.д. (в Jupyter может быть 30–120 с)
        _log("[skin_screener] модули загружены, заходим в main…")
        main()
    except SystemExit:
        raise
    except BaseException:
        import traceback

        traceback.print_exc()
        sys.exit(1)
