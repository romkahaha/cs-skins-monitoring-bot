"""Precompute Telegram fit plots for a list of items."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from automation.listing_enrichment import load_items_py
from automation.model_fit_plot import precomputed_plot_path, render_item_fit_plot


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Precompute fit plot PNGs for item alerts.")
    parser.add_argument("--items-py", type=Path, required=True, help="Python file with ITEMS = [...].")
    parser.add_argument("--out-dir", type=Path, required=True, help="Directory for precomputed PNG files.")
    parser.add_argument("--data-dir", type=Path, default=Path("skin_homog/data_skins_big"))
    parser.add_argument("--fit-json", type=Path, default=Path("steam_listings/data/float_fit_rel_curves.json"))
    parser.add_argument("--dpi", type=int, default=120)
    parser.add_argument("--limit", type=int, default=None, help="Optional limit for testing.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    items = load_items_py(args.items_py.resolve())
    if args.limit is not None:
        items = items[: max(0, int(args.limit))]
    out_dir = args.out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    total = len(items)
    saved = 0
    skipped = 0
    failed = 0

    for idx, item in enumerate(items, start=1):
        out_path = precomputed_plot_path(item, out_dir)
        if out_path.is_file():
            skipped += 1
            print(f"{idx}/{total} skip {item}")
            continue
        try:
            image_bytes = render_item_fit_plot(
                item,
                data_dir=args.data_dir.resolve(),
                fit_json=args.fit_json.resolve(),
                dpi=int(args.dpi),
            )
        except Exception as exc:
            failed += 1
            print(f"{idx}/{total} fail {item}: {exc}")
            continue
        out_path.write_bytes(image_bytes)
        saved += 1
        print(f"{idx}/{total} saved {item} -> {out_path.name}")

    print(f"done total={total} saved={saved} skipped={skipped} failed={failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
