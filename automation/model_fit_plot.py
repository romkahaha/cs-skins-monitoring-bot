"""Render saved float fit curves for Telegram alerts."""

from __future__ import annotations

import io
import json
import re
from hashlib import sha1
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


STRUCTURAL_GAP = -1337.0
PANEL_FILES = {
    "base": "base.csv",
    "predicted": "predicted.csv",
    "float_value": "float_value.csv",
    "sticker_count": "sticker_count.csv",
}
MODEL_COLORS = {
    "smooth": "tab:blue",
    "segmented": "tab:orange",
    "hybrid": "tab:green",
}

_FIT_PAYLOAD_CACHE: dict[tuple[str, int, int], dict[str, Any]] = {}
_PANEL_COLUMN_CACHE: dict[tuple[str, int, int, str], pd.Series] = {}


def _path_signature(path: Path) -> tuple[str, int, int]:
    resolved = path.resolve()
    stat = resolved.stat()
    return str(resolved), int(stat.st_mtime_ns), int(stat.st_size)


def plot_filename_for_item(item: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", item).strip("._-")
    if not slug:
        slug = "item"
    digest = sha1(item.encode("utf-8")).hexdigest()[:12]
    return f"{digest}__{slug}.png"


def precomputed_plot_path(item: str, directory: Path) -> Path:
    return directory / plot_filename_for_item(item)


def load_numeric_panel_column(path: Path, item: str) -> pd.Series:
    key = (*_path_signature(path), item)
    cached = _PANEL_COLUMN_CACHE.get(key)
    if cached is not None:
        return cached.copy()

    try:
        df = pd.read_csv(path, usecols=[item])
    except ValueError as exc:
        raise ValueError(f"Item not found in panel {path.name}: {item}") from exc
    series = pd.to_numeric(df[item], errors="coerce").replace(STRUCTURAL_GAP, np.nan)
    _PANEL_COLUMN_CACHE[key] = series
    return series.copy()


def load_fit_payload(path: Path) -> dict[str, Any]:
    key = _path_signature(path)
    cached = _FIT_PAYLOAD_CACHE.get(key)
    if cached is not None:
        return cached

    with path.open(encoding="utf-8") as handle:
        payload: dict[str, Any] = json.load(handle)
    _FIT_PAYLOAD_CACHE[key] = payload
    return payload


def build_item_df(item: str, panels: dict[str, pd.Series]) -> pd.DataFrame:
    df = pd.DataFrame(
        {
            "float_value": panels["float_value"],
            "base": panels["base"],
            "predicted": panels["predicted"],
            "sticker_count": panels["sticker_count"],
        }
    )
    df["pred_rel_dev"] = df["predicted"] / df["base"] - 1.0
    df = df.replace([np.inf, -np.inf], np.nan)
    df = df.dropna(subset=["float_value", "base", "predicted", "pred_rel_dev"])
    return df.sort_values("float_value").reset_index(drop=True)


def render_item_fit_plot(
    item: str,
    *,
    data_dir: Path,
    fit_json: Path,
    dpi: int = 120,
) -> bytes:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    panels = {name: load_numeric_panel_column(data_dir / filename, item) for name, filename in PANEL_FILES.items()}
    fit_payload = load_fit_payload(fit_json)
    fit_per_skin = fit_payload.get("per_skin", {})
    if item not in fit_per_skin:
        raise ValueError(f"Item not found in fit JSON: {item}")

    fit = fit_per_skin[item]
    item_df = build_item_df(item, panels)
    if item_df.empty:
        raise ValueError(f"No usable panel rows for item: {item}")

    x = item_df["float_value"].to_numpy(dtype=float)
    base = item_df["base"].to_numpy(dtype=float)
    pred = item_df["predicted"].to_numpy(dtype=float)
    y_rel = item_df["pred_rel_dev"].to_numpy(dtype=float)
    x_grid = np.asarray(fit["x_grid"], dtype=float)
    base_grid = np.interp(x_grid, x, base)
    splits = fit.get("splits", [])

    curves = {}
    for name in ("smooth", "segmented", "hybrid"):
        if name in fit:
            curves[name] = np.asarray(fit[name], dtype=float)

    fig, axes = plt.subplots(1, 2, figsize=(15, 5))
    ax1, ax2 = axes

    ax1.scatter(x, y_rel, s=20, alpha=0.8)
    for model_name, curve in curves.items():
        ax1.plot(x_grid, curve, color=MODEL_COLORS[model_name], linewidth=2, label=model_name)
    for split_x in splits:
        ax1.axvline(split_x, color="gray", linestyle="--", alpha=0.7)
    ax1.set_title(f"{item} - rel dev")
    ax1.set_xlabel("float_value")
    ax1.set_ylabel("predicted / base - 1")
    ax1.grid(True, alpha=0.25)
    ax1.legend()

    ax2.scatter(x, pred, s=20, alpha=0.8)
    for model_name, curve in curves.items():
        ax2.plot(
            x_grid,
            base_grid * (1.0 + curve),
            color=MODEL_COLORS[model_name],
            linewidth=2,
            label=model_name,
        )
    for split_x in splits:
        ax2.axvline(split_x, color="gray", linestyle="--", alpha=0.7)
    ax2.set_title(f"{item} - predicted price")
    ax2.set_xlabel("float_value")
    ax2.set_ylabel("predicted")
    ax2.grid(True, alpha=0.25)
    ax2.legend()

    plt.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=dpi, bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()
