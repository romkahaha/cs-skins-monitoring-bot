"""Promote locally prepared nightly inputs into the production repo."""

from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd


FILE_MAPPINGS = [
    ("lists/skins_normal_filtered1.py", "lists/skins_normal_filtered1.py"),
    ("lists/skins_normal2.py", "lists/skins_normal2.py"),
    ("skin_homog/screener_preprocess/preprocess_metrics.csv", "skin_homog/screener_preprocess/preprocess_metrics.csv"),
    ("steam_listings/data/float_fit_rel_curves.json", "steam_listings/data/float_fit_rel_curves.json"),
]

DIR_MAPPINGS = [
    ("skin_homog/data_skins_big", "skin_homog/data_skins_big"),
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Promote prepared nightly source inputs.")
    parser.add_argument("--source-repo", type=Path, required=True)
    parser.add_argument("--target-repo", type=Path, default=Path.cwd())
    parser.add_argument("--manifest", type=Path, default=Path("automation_runtime/nightly_source_promote_latest.json"))
    parser.add_argument("--check-only", action="store_true")
    return parser.parse_args()


def load_items(path: Path) -> list[str]:
    spec = importlib.util.spec_from_file_location("_nightly_source_items", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load item list: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    raw_items = getattr(module, "SKINS", None)
    if raw_items is None:
        raw_items = getattr(module, "ITEMS", None)
    if raw_items is None:
        raise RuntimeError(f"{path} must define SKINS or ITEMS")
    items = [str(item) for item in raw_items]
    if len(items) != len(set(items)):
        raise RuntimeError(f"Item list contains duplicates: {path}")
    return items


def validate_source(source_repo: Path) -> dict:
    item_path = source_repo / "lists" / "skins_normal_filtered1.py"
    preprocess_path = source_repo / "skin_homog" / "screener_preprocess" / "preprocess_metrics.csv"
    summary_path = source_repo / "skin_homog" / "data_skins_big" / "_summary.csv"
    fit_path = source_repo / "steam_listings" / "data" / "float_fit_rel_curves.json"

    items = load_items(item_path)
    item_set = set(items)

    preprocess = pd.read_csv(preprocess_path, usecols=["item", "status"])
    preprocess_items = set(preprocess["item"].dropna().astype(str))
    preprocess_missing = sorted(item_set - preprocess_items)
    if preprocess_missing:
        raise RuntimeError(f"Source preprocess is missing {len(preprocess_missing)} listed items")
    bad_status = preprocess.loc[preprocess["item"].astype(str).isin(item_set) & (preprocess["status"].fillna("") != "ok")]
    if not bad_status.empty:
        raise RuntimeError(f"Source preprocess has non-ok rows for listed items: {len(bad_status)}")

    summary = pd.read_csv(summary_path, usecols=["item"])
    summary_items = set(summary["item"].dropna().astype(str))
    summary_missing = sorted(item_set - summary_items)
    if summary_missing:
        raise RuntimeError(f"Source model summary is missing {len(summary_missing)} listed items")

    fit = json.loads(fit_path.read_text(encoding="utf-8"))
    per_skin = fit.get("per_skin") if isinstance(fit, dict) else None
    if not isinstance(per_skin, dict):
        raise RuntimeError(f"Fit JSON has no per_skin object: {fit_path}")
    fit_items = set(str(item) for item in per_skin.keys())
    fit_missing = sorted(item_set - fit_items)
    if fit_missing:
        raise RuntimeError(f"Source fit JSON is missing {len(fit_missing)} listed items")
    skipped = fit.get("skipped", [])
    if skipped:
        raise RuntimeError(f"Source fit JSON has skipped items: {len(skipped)}")

    return {
        "source_repo": str(source_repo),
        "items": len(items),
        "preprocess_rows": int(len(preprocess)),
        "preprocess_unique_items": int(preprocess["item"].nunique()),
        "summary_rows": int(len(summary)),
        "summary_unique_items": int(summary["item"].nunique()),
        "fit_per_skin": int(len(per_skin)),
        "fit_skipped": int(len(skipped)),
    }


def copy_file(src: Path, dst: Path) -> None:
    if not src.is_file():
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def copy_dir(src: Path, dst: Path) -> None:
    if not src.is_dir():
        raise FileNotFoundError(src)
    if dst.exists():
        shutil.rmtree(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dst)


def main() -> int:
    args = parse_args()
    source_repo = args.source_repo.resolve()
    target_repo = args.target_repo.resolve()
    manifest_path = args.manifest if args.manifest.is_absolute() else target_repo / args.manifest

    payload = validate_source(source_repo)
    payload["target_repo"] = str(target_repo)
    payload["promoted_at_utc"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    payload["check_only"] = bool(args.check_only)

    if not args.check_only:
        for src_rel, dst_rel in FILE_MAPPINGS:
            copy_file(source_repo / src_rel, target_repo / dst_rel)
        for src_rel, dst_rel in DIR_MAPPINGS:
            copy_dir(source_repo / src_rel, target_repo / dst_rel)
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
