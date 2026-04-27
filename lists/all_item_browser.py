import json
from pathlib import Path

import requests

BASE = "https://raw.githubusercontent.com/ByMykel/CSGO-API/main/public/api/en"

# Категории, у которых есть market_hash_name
ENDPOINTS = [
    "skins_not_grouped.json",  # <- wear/state as separate items
    "stickers.json",
    "sticker_slabs.json",
    "keychains.json",
    "crates.json",
    "keys.json",
    "agents.json",
    "patches.json",
    "graffiti.json",
    "music_kits.json",
    "highlights.json",
    "collectibles.json",  # часть будет с null market_hash_name
]

OUTFILE = "screening_super_full.py"
TIMEOUT = 60


def fetch_json(url: str):
    r = requests.get(url, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def iter_market_hash_names(obj):
    """
    Рекурсивно проходит по JSON и достает все непустые market_hash_name.
    Это делает код устойчивым к небольшим изменениям структуры.
    """
    if isinstance(obj, dict):
        mh = obj.get("market_hash_name")
        if isinstance(mh, str) and mh.strip():
            yield mh.strip()
        for v in obj.values():
            yield from iter_market_hash_names(v)
    elif isinstance(obj, list):
        for x in obj:
            yield from iter_market_hash_names(x)


def collect_items():
    seen = set()

    for endpoint in ENDPOINTS:
        url = f"{BASE}/{endpoint}"
        print(f"Fetching {url}")
        data = fetch_json(url)

        before = len(seen)
        for name in iter_market_hash_names(data):
            seen.add(name)
        added = len(seen) - before

        print(f"  added {added} unique names from {endpoint}")

    items = sorted(seen)
    return items


def write_python_list(items, outfile: str = OUTFILE):
    path = Path(outfile)
    with path.open("w", encoding="utf-8") as f:
        f.write("# Auto-generated from ByMykel CSGO-API\n")
        f.write("# market_hash_name values for CS2 items\n\n")
        f.write("ITEMS = [\n")
        for item in items:
            f.write(f"    {json.dumps(item, ensure_ascii=False)},\n")
        f.write("]\n")

    print(f"Saved {len(items)} items to {path.resolve()}")


if __name__ == "__main__":
    items = collect_items()
    write_python_list(items)