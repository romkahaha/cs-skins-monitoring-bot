"""Render saved float fit curves for Telegram alerts."""

from __future__ import annotations

import io
import json
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


def load_numeric_panel(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    df = df.apply(pd.to_numeric, errors="coerce")
    return df.replace(STRUCTURAL_GAP, np.nan)


def build_item_df(item: str, panels: dict[str, pd.DataFrame]) -> pd.DataFrame:
    df = pd.DataFrame(
        {
            "float_value": panels["float_value"][item],
            "base": panels["base"][item],
            "predicted": panels["predicted"][item],
            "sticker_count": panels["sticker_count"][item],
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

    panels = {name: load_numeric_panel(data_dir / filename) for name, filename in PANEL_FILES.items()}
    with fit_json.open(encoding="utf-8") as f:
        fit_payload: dict[str, Any] = json.load(f)
    fit_per_skin = fit_payload.get("per_skin", {})
    if item not in fit_per_skin:
        raise ValueError(f"Item not found in fit JSON: {item}")
    missing_panels = [name for name, df in panels.items() if item not in df.columns]
    if missing_panels:
        raise ValueError(f"Item not found in panels {missing_panels}: {item}")

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
