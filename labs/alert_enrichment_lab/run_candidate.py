from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from automation.alert_enrichment import queue_enrichment_job, run_enrichment_job
from automation.config import load_json_config, monitoring_defaults
from automation.risk_filters import repo_root_from
from automation.telegram_alerts import (
    format_alert,
    maybe_render_fit_plot,
    send_message,
    send_photo,
    telegram_credentials,
)


def repo_root() -> Path:
    return repo_root_from(Path(__file__))


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def parse_args() -> argparse.Namespace:
    root = repo_root()
    parser = argparse.ArgumentParser(description="Replay one historical opportunity through alert + plot + AI note.")
    parser.add_argument(
        "--config",
        type=Path,
        default=root / "automation" / "configs" / "monitoring.json",
        help="Monitoring config JSON.",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=root / "labs" / "alert_enrichment_lab" / "fixtures" / "manifest.json",
        help="Fixture manifest JSON.",
    )
    parser.add_argument("--fixture-id", type=str, default=None, help="Fixture id from manifest.")
    parser.add_argument("--list", action="store_true", help="List fixtures and exit.")
    parser.add_argument("--send-telegram", action="store_true", help="Send base alert, plot, and AI note to Telegram.")
    parser.add_argument(
        "--latest-sales-json",
        type=Path,
        default=None,
        help="Optional saved latest_sales JSON to seed the lab cache and skip live CSFloat dependency.",
    )
    return parser.parse_args()


def load_manifest(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    fixtures = payload.get("fixtures")
    if not isinstance(fixtures, list):
        raise ValueError(f"{path} must contain fixtures=[...]")
    out: list[dict[str, Any]] = []
    for entry in fixtures:
        if isinstance(entry, dict) and str(entry.get("id") or "").strip():
            out.append(entry)
    return out


def find_fixture(fixtures: list[dict[str, Any]], fixture_id: str) -> dict[str, Any]:
    for entry in fixtures:
        if str(entry.get("id")) == fixture_id:
            return entry
    raise KeyError(f"fixture not found: {fixture_id}")


def load_fixture_row(fixture: dict[str, Any], *, root: Path) -> dict[str, Any]:
    source_csv = root / str(fixture["source_csv"])
    row_index = int(fixture.get("row_index", 0))
    df = pd.read_csv(source_csv, low_memory=False)
    if row_index < 0 or row_index >= len(df):
        raise IndexError(f"row_index={row_index} out of range for {source_csv} (rows={len(df)})")
    row = df.iloc[row_index].to_dict()
    row["_fixture_source_csv"] = str(source_csv)
    row["_fixture_row_index"] = row_index
    return row


def run_dir_for_fixture(fixture_id: str) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    return repo_root() / "labs" / "alert_enrichment_lab" / "runs" / f"{stamp}_{fixture_id}"


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def build_lab_enrichment_config(config: dict[str, Any], *, run_dir: Path) -> dict[str, Any]:
    merged = dict(config.get("alert_enrichment", {}))
    merged["enabled"] = True
    merged["background"] = False
    merged["log_dir"] = str(run_dir / "alert_enrichment")
    return {"alert_enrichment": merged}


def build_lab_plot_config(config: dict[str, Any], *, root: Path) -> dict[str, Any]:
    merged = dict(config.get("model_plot", {}))
    for key in ("data_dir", "fit_json", "precomputed_dir"):
        value = merged.get(key)
        if value in (None, ""):
            continue
        path = Path(str(value))
        merged[key] = str(path if path.is_absolute() else (root / path).resolve())
    return merged


def seed_manual_latest_sales(item: str, latest_sales_json: Path, *, enrich_config: dict[str, Any], run_dir: Path) -> None:
    payload = json.loads(latest_sales_json.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{latest_sales_json} must contain a JSON object")
    rows = payload.get("sales_rows")
    if not isinstance(rows, list) or not rows:
        raise ValueError(f"{latest_sales_json} must contain sales_rows=[...]")
    seeded = dict(payload)
    seeded["item"] = item
    seeded["source"] = "manual_fixture"
    seeded["fetched_at_utc"] = datetime.now(timezone.utc).isoformat()
    write_json(run_dir / "manual_latest_sales.json", seeded)
    log_dir = Path(str(enrich_config["alert_enrichment"]["log_dir"]))
    cache_file = log_dir / "cache" / f"{hashlib.sha256(item.encode('utf-8')).hexdigest()[:24]}.json"
    write_json(cache_file, seeded)


def main() -> int:
    configure_stdio()
    args = parse_args()
    config = load_json_config(args.config.resolve(), monitoring_defaults())
    fixtures = load_manifest(args.manifest.resolve())

    if args.list:
        for entry in fixtures:
            print(f"{entry['id']}: {entry.get('label') or entry['id']}")
        return 0

    if not args.fixture_id:
        raise SystemExit("--fixture-id is required unless --list is used")

    fixture = find_fixture(fixtures, args.fixture_id)
    root = repo_root()
    run_dir = run_dir_for_fixture(args.fixture_id)
    run_dir.mkdir(parents=True, exist_ok=True)

    row = load_fixture_row(fixture, root=root)
    row_series = pd.Series(row)
    base_alert = format_alert(row_series)
    write_json(run_dir / "fixture.json", fixture)
    write_json(run_dir / "row.json", row)
    write_text(run_dir / "base_alert.html", base_alert)

    plot_cfg = build_lab_plot_config(config, root=root)
    image_bytes = maybe_render_fit_plot(row_series, plot_cfg)
    if image_bytes:
        (run_dir / "fit_plot.png").write_bytes(image_bytes)

    primary_message_id = None
    chat_id = None
    if args.send_telegram:
        token, chat_id = telegram_credentials()
        primary = send_message(base_alert, bot_token=token, chat_id=chat_id)
        result_payload = primary.get("result") if isinstance(primary, dict) else None
        if isinstance(result_payload, dict):
            try:
                primary_message_id = int(result_payload.get("message_id"))
            except Exception:
                primary_message_id = None
        write_json(run_dir / "telegram_primary.json", primary)
        if image_bytes:
            photo_result = send_photo(
                image_bytes,
                bot_token=token,
                chat_id=chat_id,
                caption=str(row.get("item") or "Model fit")[:1024],
            )
            write_json(run_dir / "telegram_photo.json", photo_result)

    enrich_config = build_lab_enrichment_config(config, run_dir=run_dir)
    if args.latest_sales_json is not None:
        seed_manual_latest_sales(
            str(row.get("item") or ""),
            args.latest_sales_json.resolve(),
            enrich_config=enrich_config,
            run_dir=run_dir,
        )
    job_json = queue_enrichment_job(
        row=row,
        primary_message_id=primary_message_id,
        config=enrich_config,
        config_path=args.config.resolve(),
        chat_id=chat_id,
    )
    if job_json is None:
        raise RuntimeError("failed to create enrichment job")

    ok = run_enrichment_job(job_json, enrich_config, dry_run=not args.send_telegram)
    status_path = job_json.parent / "status.json"
    result_path = job_json.parent / "result.json"
    if status_path.is_file():
        print(status_path.read_text(encoding="utf-8").strip())
    if result_path.is_file():
        result_payload = json.loads(result_path.read_text(encoding="utf-8"))
        message_preview = str(result_payload.get("message_preview") or "").strip()
        if message_preview:
            write_text(run_dir / "ai_note.html", message_preview)
            print("\n=== AI NOTE ===\n")
            print(message_preview)

    print(f"\nrun_dir: {run_dir}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
