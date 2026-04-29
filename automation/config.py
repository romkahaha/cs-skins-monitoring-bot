"""JSON config loading for nightly and monitoring automation jobs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from automation.risk_filters import repo_root_from


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def resolve_path(repo_root: Path, value: str | Path | None) -> Path | None:
    if value is None:
        return None
    path = Path(value)
    return path if path.is_absolute() else (repo_root / path).resolve()


def load_json_config(path: Path | None, defaults: dict[str, Any]) -> dict[str, Any]:
    if path is None:
        return defaults
    if not path.is_file():
        raise FileNotFoundError(path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return deep_merge(defaults, raw)


def repo_root() -> Path:
    return repo_root_from(Path(__file__))


def monitoring_defaults() -> dict[str, Any]:
    root = repo_root()
    return {
        "schedule": {
            "enabled": True,
            "active_from": "08:00",
            "active_to": "23:00",
            "timezone": "Europe/Prague",
            "interval_minutes": 10,
            "github_actions_cron_utc": "0 6,12,18 * * *",
            "enforce_active_window": False,
        },
        "preflight": {
            "require_monitor_items_py": True,
            "require_base_snapshot_csv": True,
            "require_risk_csv": True,
            "require_fit_json": True,
            "max_monitor_items_age_hours": 30.0,
            "max_base_snapshot_age_hours": 30.0,
            "max_risk_csv_age_hours": 36.0,
            "max_fit_json_age_hours": None,
            "fail_on_stale_inputs": False,
            "min_monitor_items": 1,
        },
        "paths": {
            "monitor_items_py": str(root / "automation_runtime" / "monitor_list_latest.py"),
            "state_json": str(root / "automation_runtime" / "state.json"),
            "base_snapshot_csv": str(root / "automation_runtime" / "base_snapshot_latest.csv"),
            "steam_listings_csv": str(root / "automation_runtime" / "steam_listings_latest.csv"),
            "fit_json": str(root / "steam_listings" / "data" / "float_fit_rel_curves.json"),
            "risk_csv": str(root / "skin_homog" / "screener_preprocess_risk" / "risk_metrics.csv"),
            "enriched_listings_csv": str(root / "automation_runtime" / "enriched_listings_latest.csv"),
            "opportunities_csv": str(root / "automation_runtime" / "opportunities_latest.csv"),
            "opportunities_report_csv": str(root / "automation_runtime" / "opportunities_report_latest.csv"),
        },
        "monitoring": {
            "enabled": True,
            "batch_size": 5,
            "max_listings_per_item": 200,
            "fail_if_all_listing_fetches_error": True,
        },
        "cycle": {
            "enabled": True,
            "batch_size": None,
            "cycle_sleep_sec": 600.0,
            "commit_runtime": True,
            "commit_every_batches": 5,
            "max_runtime_minutes": 330.0,
            "max_batches_per_run": None,
            "max_cycles_per_run": None,
            "respect_active_window": True,
            "checkpoint_message": "Update monitoring runtime [skip ci]",
        },
        "steam_scm": {
            "listings_per_request": 100,
            "max_listings_per_item": 200,
            "request_timeout_sec": 45.0,
            "retry_attempts": 3,
            "retry_sleep_min_sec": 2.0,
            "retry_sleep_max_sec": 5.0,
            "delay_between_skins_min_sec": 4.0,
            "delay_between_skins_max_sec": 10.0,
            "delay_between_render_pages_min_sec": 2.0,
            "delay_between_render_pages_max_sec": 5.0,
            "batch_log_progress": 1,
        },
        "opportunity_filter": {
            "steam_sales_n_min": 50,
            "downside_risk_max": 10.0,
            "tail_ratio_min": 0.9,
            "downside_14d_max": 0.12,
            "continuity_ratio_max": 3.5,
            "spread_hybrid_disc_max": 0.17,
            "exclude_any": [],
        },
        "alerts": {
            "enabled": True,
            "spread_hybrid_disc_max": 0.17,
            "ask_min": None,
            "ask_max": None,
            "steam_sales_7d_n_min": 50,
            "steam_sales_7d_downside_risk_max": 10.0,
            "steam_sales_7d_tail_ratio_min": 0.9,
            "steam_daily_downside_14d_pct_max": 0.12,
            "continuity_ratio_max": 3.5,
            "exclude_any": [],
        },
        "model_plot": {
            "enabled": False,
            "data_dir": "skin_homog/data_skins_big",
            "fit_json": "steam_listings/data/float_fit_rel_curves.json",
            "dpi": 120,
            "fail_on_error": False,
        },
        "telegram": {
            "enabled": False,
            "cooldown_hours": 12.0,
            "sleep_sec": 0.6,
            "max_alerts": None,
            "send_empty_summary": False,
        },
    }


def health_defaults() -> dict[str, Any]:
    return {
        "enabled": True,
        "schedule": {
            "enabled": True,
            "timezone": "Europe/Prague",
            "interval_minutes": 60,
            "github_actions_cron_utc": "7 * * * *",
        },
        "steam": {
            "item": "Dreams & Nightmares Case",
            "currency": 3,
            "limit": 10,
            "max_listings": 10,
            "min_rows": 1,
            "fail_if_missing_cookies": True,
            "check_login_endpoint": True,
            "login_check_url": "https://steamcommunity.com/my/",
            "check_listing_endpoint": True,
            "listings_per_request": 10,
            "request_timeout_sec": 45.0,
            "retry_attempts": 2,
            "retry_sleep_min_sec": 2.0,
            "retry_sleep_max_sec": 5.0,
            "delay_between_render_pages_min_sec": 0.0,
            "delay_between_render_pages_max_sec": 0.0,
            "batch_log_progress": 1,
        },
        "telegram": {
            "enabled": True,
            "timeout_sec": 120,
        },
    }


def nightly_defaults() -> dict[str, Any]:
    root = repo_root()
    return {
        "paths": {
            "risk_csv": str(root / "skin_homog" / "screener_preprocess_risk" / "risk_metrics.csv"),
            "risk_script": str(root / "skin_homog" / "screener_preprocess_risk" / "risk_preprocess.py"),
            "risk_input_items_py": str(root / "lists" / "skins_preprocess_filtered.py"),
            "risk_stage1_csv": str(root / "skin_homog" / "screener_preprocess" / "preprocess_metrics.csv"),
            "risk_progress_log": str(root / "skin_homog" / "screener_preprocess_risk" / "_risk_progress.log"),
            "risk_runtime_json": str(root / "automation_runtime" / "risk_runtime_latest.json"),
            "risk_candidates_csv": str(root / "automation_runtime" / "risk_candidates_latest.csv"),
            "model_coverage_csv": str(root / "automation_runtime" / "model_coverage_latest.csv"),
            "model_backfill_queue_csv": str(root / "automation_runtime" / "model_backfill_queue_latest.csv"),
            "model_backfill_queue_items_py": str(root / "automation_runtime" / "model_backfill_queue_latest.py"),
            "model_backfill_batch_items_py": str(root / "automation_runtime" / "model_backfill_batch_latest.py"),
            "model_backfill_runtime_json": str(root / "automation_runtime" / "model_backfill_runtime_latest.json"),
            "model_backfill_progress_log": str(root / "automation_runtime" / "model_backfill_progress_latest.log"),
            "model_refit_progress_log": str(root / "automation_runtime" / "model_refit_progress_latest.log"),
            "summary_csv": str(root / "skin_homog" / "data_skins_big" / "_summary.csv"),
            "skin_data_dir": str(root / "skin_homog" / "data_skins_big"),
            "fit_json": str(root / "steam_listings" / "data" / "float_fit_rel_curves.json"),
            "monitor_csv": str(root / "automation_runtime" / "monitor_list_latest.csv"),
            "monitor_items_py": str(root / "automation_runtime" / "monitor_list_latest.py"),
            "base_snapshot_csv": str(root / "automation_runtime" / "base_snapshot_latest.csv"),
        },
        "schedule": {
            "intended_start": "00:17",
            "timezone": "Europe/Prague",
            "github_actions_cron_utc": (
                "temporary test: 50,55 5 * * * via night-clock.yml; "
                "schedule_gate.py allows 07:00-07:59 Prague"
            ),
        },
        "preflight": {
            "require_existing_risk_csv": True,
            "max_existing_risk_age_hours": None,
            "fail_on_stale_existing_risk_csv": False,
            "require_summary_csv": True,
            "require_skin_data_dir": True,
        },
        "risk_rebuild": {
            "enabled": False,
            "mode": "create",
            "trade_days": 7,
            "min_discount_sample": 3,
            "steam_currency": 3,
            "auto_refresh_steam_cookies": False,
            "require_stage1_ok": True,
            "steam_item_delay_min_sec": 6.0,
            "steam_item_delay_max_sec": 11.0,
        },
        "monitor_list": {
            "enabled": True,
            "expected_min_items": 100,
            "expected_max_items": 300,
            "fail_if_outside_expected_range": False,
        },
        "risk_filter": {
            "ret_7d_min": -0.03,
            "downside_14d_max": 0.17,
            "sales_7d_n_min": 21,
            "tail_ratio_min": 0.85,
            "n_listings_min": 20,
        },
        "high_cv_filter": {
            "pred_cv_min": 0.075,
            "pred_range_over_mean_min": 0.3,
            "min_listings": 3,
            "missing_cv_policy": "assume_high",
        },
        "model_coverage": {
            "min_summary_n_listings": 3,
            "min_fit_clean_points": 5,
            "require_model_ready_for_monitor": True,
        },
        "model_backfill": {
            "enabled": False,
            "max_items_per_run": 5,
            "target_unique": 400,
            "page_limit": 50,
            "mode": "merge",
            "use_mix": True,
            "sort_by": "most_recent",
            "skip_known_ids": True,
            "ignore_existing_items": True,
            "mix_overfetch_factor": 1.15,
            "mix_shards": [
                {
                    "sort_by": "most_recent",
                    "share": 0.5,
                    "drop_lowest_price_fraction": 0.0,
                    "drop_highest_price_fraction": 0.0,
                },
                {
                    "sort_by": "lowest_float",
                    "share": 0.25,
                    "drop_lowest_price_fraction": 0.0,
                    "drop_highest_price_fraction": 0.0,
                },
                {
                    "sort_by": "highest_float",
                    "share": 0.25,
                    "drop_lowest_price_fraction": 0.0,
                    "drop_highest_price_fraction": 0.0,
                },
            ],
            "inner_delay_min_sec": 11.0,
            "inner_delay_max_sec": 16.8,
            "item_delay_min_sec": 11.0,
            "item_delay_max_sec": 16.0,
            "slow_inner_delay_min_sec": 16.0,
            "slow_inner_delay_max_sec": 21.0,
            "slow_item_delay_min_sec": 16.0,
            "slow_item_delay_max_sec": 21.0,
            "slow_mode_extend_sec": 1800.0,
            "key_cooldown_429_sec": 1200.0,
            "key_cooldown_403_sec": 1800.0,
            "retry_ladder_min_sec": 90.0,
            "retry_ladder_step_sec": 150.0,
            "retry_ladder_max_sec": 900.0,
        },
        "model_refit": {
            "enabled": True,
            "min_points": 5,
            "max_skins": None,
            "grid_n": 300,
            "smooth_frac": 0.25,
            "supersmooth_frac": 0.35,
            "smooth_min_pts": 12,
            "smooth_robust_iters": 2,
            "seg_min_seg": 12,
            "seg_z_thresh": 4.0,
            "seg_max_jumps": 2,
            "hybrid_alpha": 0.7,
            "outlier_neighbors": 6,
            "outlier_z": 3.5,
            "outlier_min_abs_dev": 0.03,
        },
        "base_snapshot": {
            "enabled": True,
            "delay_min_sec": 0.0,
            "delay_max_sec": 0.0,
            "rate_limit_pause_sec": 900.0,
            "rate_limit_stair_step_sec": 60.0,
            "rate_limit_max_retries": 5,
            "rate_limit_error_patterns": [
                "429",
                "too many requests",
                "rate limit",
            ],
        },
    }


def path_from_config(cfg: dict[str, Any], key: str, *, section: str = "paths") -> Path:
    root = repo_root()
    value = cfg.get(section, {}).get(key)
    resolved = resolve_path(root, value)
    if resolved is None:
        raise KeyError(f"Missing config path {section}.{key}")
    return resolved
