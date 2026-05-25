"""Validate and repair generated monitor runtime files before monitoring runs."""

from __future__ import annotations

import argparse
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from automation.config import load_json_config, monitoring_defaults, path_from_config
from automation.listing_enrichment import load_items_py
from automation.monitoring.tier_scheduler import (
    tier_item_paths_from_config,
    tier_items_match_full_list,
    tier_mode_enabled,
    tiers_metadata_path_from_config,
    write_tier_outputs,
)
from automation.risk_filters import repo_root_from, write_items_py


CONFLICT_MARKERS = ("<<<<<<< ", "=======", ">>>>>>> ")


@dataclass
class IntegrityReport:
    actions: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _has_conflict_markers(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return False
    return any(marker in text for marker in CONFLICT_MARKERS)


def _load_items_if_valid(path: Path) -> list[str] | None:
    if not path.is_file() or _has_conflict_markers(path):
        return None
    try:
        items = load_items_py(path)
    except Exception:
        return None
    if not items:
        return None
    return items


def _load_monitor_frame(path: Path) -> pd.DataFrame | None:
    if not path.is_file() or _has_conflict_markers(path):
        return None
    try:
        frame = pd.read_csv(path, low_memory=False)
    except Exception:
        return None
    if "item" not in frame.columns:
        return None
    frame = frame.copy()
    frame["item"] = frame["item"].astype(str)
    return frame


def _load_risk_candidates_frame(path: Path) -> pd.DataFrame | None:
    if not path.is_file() or _has_conflict_markers(path):
        return None
    try:
        frame = pd.read_csv(path, low_memory=False)
    except Exception:
        return None
    if "item" not in frame.columns:
        return None
    frame = frame.copy()
    frame["item"] = frame["item"].astype(str)
    return frame


def _monitor_has_only_risk_candidates(monitor_frame: pd.DataFrame, candidates_frame: pd.DataFrame) -> bool:
    candidate_items = set(candidates_frame["item"].dropna().astype(str))
    if not candidate_items:
        return False
    if "monitor_pass" in monitor_frame.columns:
        active = monitor_frame.loc[monitor_frame["monitor_pass"].astype(str).str.lower().isin(["true", "1", "yes"])]
    else:
        active = monitor_frame
    monitor_items = set(active["item"].dropna().astype(str))
    return bool(monitor_items) and monitor_items.issubset(candidate_items)


def _rebuild_monitor_list_from_risk_candidates(repo_root: Path) -> bool:
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            str(repo_root / "automation" / "nightly" / "build_monitor_list.py"),
            "--config",
            str(repo_root / "automation" / "configs" / "nightly.json"),
        ],
        cwd=str(repo_root),
        check=False,
    )
    return result.returncode == 0


def _restore_file_from_head(repo_root: Path, path: Path) -> bool:
    try:
        rel = path.resolve().relative_to(repo_root.resolve())
    except Exception:
        return False
    result = subprocess.run(
        ["git", "-C", str(repo_root), "show", f"HEAD:{rel.as_posix()}"],
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(result.stdout)
    return True


def _best_effort_counts(frame: pd.DataFrame) -> dict[str, int]:
    total = int(len(frame))
    return {
        "total_risk_rows": total,
        "risk_passed": total,
        "high_cv_passed": total,
        "monitor_passed": total,
    }


def _rewrite_monitor_items_py(frame: pd.DataFrame, items_py_path: Path, *, source_csv: Path) -> None:
    items = frame["item"].dropna().astype(str).tolist()
    write_items_py(items, items_py_path, source_csv=source_csv, counts=_best_effort_counts(frame))


def _rewrite_tier_outputs(frame: pd.DataFrame, config: dict[str, Any], *, source_csv: Path) -> bool:
    if "tier" not in frame.columns:
        return False
    tier_frame = frame.loc[frame["tier"].isin(["A", "B", "C"])].copy().reset_index(drop=True)
    if tier_frame.empty:
        return False
    tier_frame["item"] = tier_frame["item"].astype(str)
    score_weights = config.get("monitor_tiers", {}).get("score_weights", {})
    shares = config.get("monitor_tiers", {}).get("shares", {})
    write_tier_outputs(
        tier_frame,
        tier_item_paths=tier_item_paths_from_config(config),
        metadata_path=tiers_metadata_path_from_config(config),
        source_csv=source_csv,
        counts=_best_effort_counts(frame),
        shares=shares,
        score_weights={
            "steam_sales_7d_n": float(score_weights.get("steam_sales_7d_n", 0.75)),
            "steam_turnover_proxy": float(score_weights.get("steam_turnover_proxy", 0.25)),
        },
    )
    return True


def ensure_monitor_runtime_integrity(config: dict[str, Any], repo_root: Path) -> IntegrityReport:
    report = IntegrityReport()

    paths_cfg = config.get("paths", {})
    monitor_csv_value = paths_cfg.get("monitor_csv") or str(repo_root / "automation_runtime" / "monitor_list_latest.csv")
    monitor_csv = Path(monitor_csv_value).resolve()
    risk_candidates_csv = repo_root / "automation_runtime" / "risk_candidates_latest.csv"
    monitor_items_py = path_from_config(config, "monitor_items_py")
    tier_item_paths = tier_item_paths_from_config(config)
    tiers_metadata_path = tiers_metadata_path_from_config(config)

    if _has_conflict_markers(monitor_csv) and _restore_file_from_head(repo_root, monitor_csv):
        report.actions.append(f"restored {monitor_csv.name} from HEAD because it contained merge markers")

    frame = _load_monitor_frame(monitor_csv)
    if frame is None and _restore_file_from_head(repo_root, monitor_csv):
        frame = _load_monitor_frame(monitor_csv)
        if frame is not None:
            report.actions.append(f"restored {monitor_csv.name} from HEAD because it was unreadable")

    candidates = _load_risk_candidates_frame(risk_candidates_csv)
    if frame is not None and candidates is not None and not _monitor_has_only_risk_candidates(frame, candidates):
        if _rebuild_monitor_list_from_risk_candidates(repo_root):
            report.actions.append(f"rebuilt {monitor_csv.name} from {risk_candidates_csv.name}")
            frame = _load_monitor_frame(monitor_csv)
        else:
            report.warnings.append(f"{monitor_csv.name} did not match {risk_candidates_csv.name} and rebuild failed")

    items = _load_items_if_valid(monitor_items_py)
    if items is None and frame is not None:
        _rewrite_monitor_items_py(frame, monitor_items_py, source_csv=monitor_csv)
        items = _load_items_if_valid(monitor_items_py)
        if items is not None:
            report.actions.append(f"rewrote {monitor_items_py.name} from {monitor_csv.name}")
    if items is None and _restore_file_from_head(repo_root, monitor_items_py):
        items = _load_items_if_valid(monitor_items_py)
        if items is not None:
            report.actions.append(f"restored {monitor_items_py.name} from HEAD")

    if items is None:
        report.warnings.append(f"monitor item list is still invalid: {monitor_items_py}")
        return report

    if not tier_mode_enabled(config):
        return report

    tier_items: dict[str, list[str]] = {}
    tier_invalid = False
    for tier, path in tier_item_paths.items():
        loaded = _load_items_if_valid(path)
        if loaded is None:
            tier_invalid = True
            tier_items[tier] = []
        else:
            tier_items[tier] = loaded

    if not tier_invalid and not tier_items_match_full_list(items, tier_items):
        tier_invalid = True
        report.warnings.append("tier item files did not match monitor_list_latest.py")

    if tier_invalid and frame is not None and _rewrite_tier_outputs(frame, config, source_csv=monitor_csv):
        report.actions.append(f"rewrote tier item files and {tiers_metadata_path.name} from {monitor_csv.name}")
        tier_invalid = False
        tier_items = {tier: (_load_items_if_valid(path) or []) for tier, path in tier_item_paths.items()}

    if tier_invalid:
        restored_any = False
        for path in [*tier_item_paths.values(), tiers_metadata_path]:
            if _restore_file_from_head(repo_root, path):
                restored_any = True
                report.actions.append(f"restored {path.name} from HEAD")
        if restored_any:
            tier_items = {tier: (_load_items_if_valid(path) or []) for tier, path in tier_item_paths.items()}
            tier_invalid = not tier_items_match_full_list(items, tier_items)

    if tier_invalid:
        report.warnings.append("tier item files are still invalid after repair attempts")

    return report


def parse_args() -> argparse.Namespace:
    root = repo_root_from(Path(__file__))
    parser = argparse.ArgumentParser(description="Validate and repair generated monitor runtime files.")
    parser.add_argument(
        "--config",
        type=Path,
        default=root / "automation" / "configs" / "monitoring.json",
        help="Monitoring config JSON path.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = load_json_config(args.config.resolve() if args.config else None, monitoring_defaults())
    root = repo_root_from(Path(__file__))
    report = ensure_monitor_runtime_integrity(config, root)
    for action in report.actions:
        print(f"runtime-integrity: {action}")
    for warning in report.warnings:
        print(f"runtime-integrity warning: {warning}")
    return 0 if not report.warnings else 1


if __name__ == "__main__":
    raise SystemExit(main())
