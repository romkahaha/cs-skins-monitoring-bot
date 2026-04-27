"""
Fit float->relative deviation curves from skin_homog/data_skins_big.

Target y is the same as in steam_listings/panel_float_scatter.ipynb:
    y = predicted/base - 1

We replicate the notebook's cleaning:
- replace STRUCTURAL_GAP (-1337) with NaN
- drop NaNs/infs
- local outlier removal (the "x" points)

Then we fit and save 3 models per item:
- smooth: robust local linear regression
- segmented: smooth per segment, where segments are split by detected jumps
- hybrid: convex combination of smooth and segmented curves

Output is a single JSON with per-item grids/curves for fast interpolation later.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

STRUCTURAL_GAP = -1337.0

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_DATA_DIR = _REPO_ROOT / "skin_homog" / "data_skins_big"
_DEFAULT_OUT_JSON = Path(__file__).resolve().parent / "data" / "float_fit_rel_curves.json"


def load_numeric_panel(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    df = df.apply(pd.to_numeric, errors="coerce")
    return df.replace(STRUCTURAL_GAP, np.nan)


def _tricube(u: np.ndarray) -> np.ndarray:
    u = np.asarray(u, dtype=float)
    out = np.zeros_like(u)
    m = np.abs(u) < 1.0
    out[m] = (1.0 - np.abs(u[m]) ** 3) ** 3
    return out


def _local_linear_predict(
    x: np.ndarray,
    y: np.ndarray,
    x_query: float,
    *,
    frac: float = 0.25,
    min_pts: int = 12,
    robust_weights: np.ndarray | None = None,
) -> float:
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    n = len(x)
    if n == 0:
        return float("nan")
    if n == 1:
        return float(y[0])

    k = max(int(math.ceil(float(frac) * n)), int(min_pts))
    k = min(max(2, k), n)

    d = np.abs(x - float(x_query))
    # bandwidth = k-th nearest distance
    h = float(np.partition(d, k - 1)[k - 1])
    if not np.isfinite(h) or h <= 0:
        return float(np.median(y))

    w = _tricube(d / h)
    if robust_weights is not None:
        w = w * np.asarray(robust_weights, dtype=float)

    ok = w > 1e-12
    if ok.sum() < 2:
        return float(np.median(y[ok]) if ok.any() else np.median(y))

    xk = x[ok]
    yk = y[ok]
    wk = w[ok]

    # Weighted local linear fit: y = a + b*(x - x_query) (stabilizes conditioning)
    dx = xk - float(x_query)
    s0 = float(np.sum(wk))
    s1 = float(np.sum(wk * dx))
    s2 = float(np.sum(wk * dx * dx))
    t0 = float(np.sum(wk * yk))
    t1 = float(np.sum(wk * yk * dx))

    denom = s0 * s2 - s1 * s1
    if abs(denom) < 1e-12:
        return float(t0 / s0) if s0 > 0 else float(np.median(yk))

    a = (t0 * s2 - t1 * s1) / denom
    return float(a)


def robust_local_linear_curve(
    x: np.ndarray,
    y: np.ndarray,
    x_grid: np.ndarray,
    *,
    frac: float = 0.25,
    min_pts: int = 12,
    robust_iters: int = 2,
) -> np.ndarray:
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    robust_w = np.ones(len(x), dtype=float)
    for _ in range(max(0, int(robust_iters))):
        fitted_train = np.array(
            [
                _local_linear_predict(x, y, xi, frac=frac, min_pts=min_pts, robust_weights=robust_w)
                for xi in x
            ],
            dtype=float,
        )
        resid = y - fitted_train
        mad = float(np.median(np.abs(resid)))
        scale = max(1.4826 * mad, 1e-6)
        u = resid / (6.0 * scale)
        robust_w = np.where(np.abs(u) < 1.0, (1.0 - u**2) ** 2, 0.0)

    return np.array(
        [
            _local_linear_predict(x, y, float(xq), frac=frac, min_pts=min_pts, robust_weights=robust_w)
            for xq in np.asarray(x_grid, dtype=float)
        ],
        dtype=float,
    )


def detect_jump_splits(
    x: np.ndarray,
    y: np.ndarray,
    *,
    min_seg: int = 12,
    z_thresh: float = 4.0,
    max_jumps: int = 2,
) -> list[float]:
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    n = len(x)
    if n < 2 * min_seg:
        return []

    ord_idx = np.argsort(x)
    x = x[ord_idx]
    y = y[ord_idx]

    dy = np.abs(np.diff(y))
    med = float(np.median(dy))
    mad = float(np.median(np.abs(dy - med)))
    scale = max(1.4826 * mad, 1e-6)
    scores = (dy - med) / scale

    cand = [
        i
        for i, s in enumerate(scores)
        if s >= z_thresh and (i + 1) >= min_seg and (n - i - 1) >= min_seg
    ]
    cand = sorted(cand, key=lambda i: scores[i], reverse=True)

    chosen: list[int] = []
    for idx in cand:
        if any(abs(idx - j) < min_seg for j in chosen):
            continue
        chosen.append(idx)
        if len(chosen) >= max_jumps:
            break

    return [float((x[i] + x[i + 1]) / 2.0) for i in sorted(chosen)]


def segmented_robust_curve(
    x: np.ndarray,
    y: np.ndarray,
    x_grid: np.ndarray,
    *,
    min_seg: int = 12,
    frac: float = 0.25,
    robust_iters: int = 2,
    z_thresh: float = 4.0,
    max_jumps: int = 2,
) -> tuple[np.ndarray, list[float]]:
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    x_grid = np.asarray(x_grid, dtype=float)

    splits = detect_jump_splits(x, y, min_seg=min_seg, z_thresh=z_thresh, max_jumps=max_jumps)
    y_pred = np.full(len(x_grid), np.nan, dtype=float)
    bounds = [-np.inf] + splits + [np.inf]
    for lo, hi in zip(bounds[:-1], bounds[1:]):
        seg_mask = (x > lo) & (x <= hi)
        grid_mask = (x_grid > lo) & (x_grid <= hi)
        if not np.any(grid_mask):
            continue
        x_seg = x[seg_mask]
        y_seg = y[seg_mask]
        if len(x_seg) < max(4, min_seg // 2):
            y_pred[grid_mask] = np.median(y_seg) if len(y_seg) else np.nan
            continue
        seg_min_pts = min(max(4, min_seg // 2), len(x_seg))
        seg_frac = max(frac, seg_min_pts / max(len(x_seg), 1))
        y_pred[grid_mask] = robust_local_linear_curve(
            x_seg,
            y_seg,
            x_grid[grid_mask],
            frac=seg_frac,
            min_pts=seg_min_pts,
            robust_iters=robust_iters,
        )
    return y_pred, splits


def local_outlier_mask(
    x: np.ndarray,
    y: np.ndarray,
    *,
    neighbors: int = 6,
    z_thresh: float = 3.5,
    min_abs_dev: float = 0.03,
) -> np.ndarray:
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    n = len(x)
    if n < max(5, neighbors):
        return np.zeros(n, dtype=bool)

    mask = np.zeros(n, dtype=bool)
    k = min(max(3, neighbors), n - 1)
    for i in range(n):
        d = np.abs(x - x[i])
        d[i] = np.inf
        idx = np.argpartition(d, k)[:k]
        y_loc = y[idx]
        med = float(np.median(y_loc))
        mad = float(np.median(np.abs(y_loc - med)))
        scale = max(1.4826 * mad, 1e-6)
        resid = float(y[i] - med)
        z = abs(resid) / scale
        if abs(resid) >= float(min_abs_dev) and z >= float(z_thresh):
            left = y[idx][x[idx] < x[i]]
            right = y[idx][x[idx] > x[i]]
            if len(left) > 0 and len(right) > 0:
                left_med = float(np.median(left))
                right_med = float(np.median(right))
                side_gap = abs(left_med - right_med)
                if side_gap < abs(resid) * 0.6:
                    mask[i] = True
            else:
                mask[i] = True
    return mask


@dataclass(frozen=True)
class FitCurves:
    x_grid: np.ndarray
    smooth: np.ndarray
    segmented: np.ndarray
    hybrid: np.ndarray
    splits: list[float]


def _finite_xy(x: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    ok = np.isfinite(x) & np.isfinite(y)
    return x[ok], y[ok]


def _interp_on_train(x_grid: np.ndarray, y_grid: np.ndarray, x_query: np.ndarray) -> np.ndarray:
    x_grid = np.asarray(x_grid, dtype=float)
    y_grid = np.asarray(y_grid, dtype=float)
    x_query = np.asarray(x_query, dtype=float)
    out = np.full(len(x_query), np.nan, dtype=float)
    ok = np.isfinite(x_query)
    if len(x_grid) >= 2 and np.isfinite(x_grid).all() and np.isfinite(y_grid).all():
        out[ok] = np.interp(x_query[ok], x_grid, y_grid, left=np.nan, right=np.nan)
    return out


def fit_item_curves(
    x: np.ndarray,
    y: np.ndarray,
    *,
    grid_n: int = 300,
    smooth_frac: float = 0.25,
    supersmooth_frac: float = 0.35,
    smooth_min_pts: int = 12,
    smooth_robust_iters: int = 2,
    seg_min_seg: int = 12,
    seg_z_thresh: float = 4.0,
    seg_max_jumps: int = 2,
    hybrid_alpha: float = 0.7,
    outlier_neighbors: int = 6,
    outlier_z: float = 3.5,
    outlier_min_abs_dev: float = 0.03,
) -> tuple[FitCurves | None, dict[str, Any]]:
    x, y = _finite_xy(x, y)
    if len(x) < 2:
        return None, {"reason": "too_few_points", "n_raw": int(len(x))}

    # Sort by float (important for split logic and consistency).
    ord_idx = np.argsort(x)
    x = x[ord_idx]
    y = y[ord_idx]

    out_mask = local_outlier_mask(
        x,
        y,
        neighbors=outlier_neighbors,
        z_thresh=outlier_z,
        min_abs_dev=outlier_min_abs_dev,
    )
    x_clean = x[~out_mask]
    y_clean = y[~out_mask]

    info = {
        "n_raw": int(len(x)),
        "n_clean": int(len(x_clean)),
        "outlier_n": int(np.sum(out_mask)),
    }
    if len(x_clean) < 2:
        info["reason"] = "too_few_points_after_outlier"
        return None, info

    x_min = float(np.min(x_clean))
    x_max = float(np.max(x_clean))
    if not np.isfinite(x_min) or not np.isfinite(x_max) or x_min == x_max:
        info["reason"] = "degenerate_x_range"
        return None, info

    grid_n = int(max(10, grid_n))
    x_grid = np.linspace(x_min, x_max, grid_n).astype(float)

    smooth = robust_local_linear_curve(
        x_clean,
        y_clean,
        x_grid,
        frac=smooth_frac,
        min_pts=smooth_min_pts,
        robust_iters=smooth_robust_iters,
    )
    supersmooth = robust_local_linear_curve(
        x_clean,
        y_clean,
        x_grid,
        frac=max(float(supersmooth_frac), float(smooth_frac)),
        min_pts=smooth_min_pts,
        robust_iters=smooth_robust_iters,
    )
    seg, splits = segmented_robust_curve(
        x_clean,
        y_clean,
        x_grid,
        min_seg=seg_min_seg,
        frac=smooth_frac,
        robust_iters=smooth_robust_iters,
        z_thresh=seg_z_thresh,
        max_jumps=seg_max_jumps,
    )
    a = float(np.clip(hybrid_alpha, 0.0, 1.0))
    hybrid = (1.0 - a) * smooth + a * seg

    curves = FitCurves(x_grid=x_grid, smooth=smooth, segmented=seg, hybrid=hybrid, splits=splits)
    smooth_train = _interp_on_train(x_grid, smooth, x_clean)
    supersmooth_train = _interp_on_train(x_grid, supersmooth, x_clean)
    seg_train = _interp_on_train(x_grid, seg, x_clean)
    mae_smooth_clean = float(np.mean(np.abs(y_clean - smooth_train))) if len(y_clean) else float("nan")
    mae_supersmooth_clean = float(np.mean(np.abs(y_clean - supersmooth_train))) if len(y_clean) else float("nan")
    mae_segmented_clean = float(np.mean(np.abs(y_clean - seg_train))) if len(y_clean) else float("nan")
    continuity_ratio = (
        float(mae_smooth_clean / max(mae_segmented_clean, 1e-12))
        if np.isfinite(mae_smooth_clean) and np.isfinite(mae_segmented_clean)
        else float("nan")
    )
    continuity_ratio2 = (
        float(mae_supersmooth_clean / max(mae_segmented_clean, 1e-12))
        if np.isfinite(mae_supersmooth_clean) and np.isfinite(mae_segmented_clean)
        else float("nan")
    )
    info.update(
        {
            "x_min": x_min,
            "x_max": x_max,
            "splits_n": int(len(splits)),
            "mae_smooth_clean": mae_smooth_clean,
            "mae_supersmooth_clean": mae_supersmooth_clean,
            "mae_segmented_clean": mae_segmented_clean,
            "continuity_ratio": continuity_ratio,
            "continuity_ratio2": continuity_ratio2,
        }
    )
    return curves, info


def run_fit_all(
    data_dir: Path,
    *,
    base_csv: str = "base.csv",
    pred_csv: str = "predicted.csv",
    float_csv: str = "float_value.csv",
    out_json: Path,
    min_points: int = 5,
    max_skins: int | None = None,
    grid_n: int = 300,
    smooth_frac: float = 0.25,
    supersmooth_frac: float = 0.35,
    smooth_min_pts: int = 12,
    smooth_robust_iters: int = 2,
    seg_min_seg: int = 12,
    seg_z_thresh: float = 4.0,
    seg_max_jumps: int = 2,
    hybrid_alpha: float = 0.7,
    outlier_neighbors: int = 6,
    outlier_z: float = 3.5,
    outlier_min_abs_dev: float = 0.03,
) -> dict[str, Any]:
    base = load_numeric_panel(data_dir / base_csv)
    pred = load_numeric_panel(data_dir / pred_csv)
    flt = load_numeric_panel(data_dir / float_csv)

    items = sorted(set(base.columns) & set(pred.columns) & set(flt.columns))
    if max_skins is not None:
        items = items[: int(max_skins)]

    out_json.parent.mkdir(parents=True, exist_ok=True)

    per_skin: dict[str, Any] = {}
    ok_n = 0
    skipped: list[dict[str, Any]] = []

    for i, item in enumerate(items, start=1):
        x = flt[item].to_numpy(dtype=float)
        b = base[item].to_numpy(dtype=float)
        p = pred[item].to_numpy(dtype=float)

        y = p / b - 1.0
        # Remove infs from division by 0; keep NaNs as missing.
        y = np.where(np.isfinite(y), y, np.nan)

        # Drop anything missing (including former -1337).
        ok = np.isfinite(x) & np.isfinite(b) & np.isfinite(p) & np.isfinite(y)
        x2 = x[ok]
        y2 = y[ok]

        if len(x2) < int(min_points):
            skipped.append({"item": item, "reason": "too_few_points", "n": int(len(x2))})
            continue

        curves, info = fit_item_curves(
            x2,
            y2,
            grid_n=grid_n,
            smooth_frac=smooth_frac,
            supersmooth_frac=supersmooth_frac,
            smooth_min_pts=smooth_min_pts,
            smooth_robust_iters=smooth_robust_iters,
            seg_min_seg=seg_min_seg,
            seg_z_thresh=seg_z_thresh,
            seg_max_jumps=seg_max_jumps,
            hybrid_alpha=hybrid_alpha,
            outlier_neighbors=outlier_neighbors,
            outlier_z=outlier_z,
            outlier_min_abs_dev=outlier_min_abs_dev,
        )
        if curves is None:
            skipped.append({"item": item, **info})
            continue

        ok_n += 1
        per_skin[item] = {
            "n_raw": int(info.get("n_raw", 0)),
            "n_clean": int(info.get("n_clean", 0)),
            "outlier_n": int(info.get("outlier_n", 0)),
            "x_min": float(info["x_min"]),
            "x_max": float(info["x_max"]),
            "mae_smooth_clean": float(info.get("mae_smooth_clean", float("nan"))),
            "mae_supersmooth_clean": float(info.get("mae_supersmooth_clean", float("nan"))),
            "mae_segmented_clean": float(info.get("mae_segmented_clean", float("nan"))),
            "continuity_ratio": float(info.get("continuity_ratio", float("nan"))),
            "continuity_ratio2": float(info.get("continuity_ratio2", float("nan"))),
            "splits": [float(s) for s in curves.splits],
            "x_grid": curves.x_grid.astype(float).tolist(),
            "smooth": curves.smooth.astype(float).tolist(),
            "segmented": curves.segmented.astype(float).tolist(),
            "hybrid": curves.hybrid.astype(float).tolist(),
        }

        if i % 25 == 0 or i == len(items):
            print(f"[fit_rel] {i}/{len(items)} ok={ok_n} last='{item}'", flush=True)

    meta = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "data_dir": str(data_dir.resolve()),
        "files": {"base": base_csv, "predicted": pred_csv, "float_value": float_csv},
        "target": "predicted/base - 1",
        "structural_gap_value": STRUCTURAL_GAP,
        "items_total": len(items),
        "items_ok": ok_n,
        "items_skipped": len(skipped),
        "grid_n": int(grid_n),
        "smooth": {"frac": float(smooth_frac), "min_pts": int(smooth_min_pts), "robust_iters": int(smooth_robust_iters)},
        "supersmooth": {"frac": float(supersmooth_frac), "min_pts": int(smooth_min_pts), "robust_iters": int(smooth_robust_iters)},
        "segmented": {"min_seg": int(seg_min_seg), "z_thresh": float(seg_z_thresh), "max_jumps": int(seg_max_jumps)},
        "hybrid": {"alpha": float(hybrid_alpha)},
        "outliers": {
            "neighbors": int(outlier_neighbors),
            "z_thresh": float(outlier_z),
            "min_abs_dev": float(outlier_min_abs_dev),
        },
        "min_points_after_nan_drop": int(min_points),
    }

    payload = {"meta": meta, "per_skin": per_skin, "skipped": skipped}
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Fit smooth/segmented/hybrid float->rel_dev curves from data_skins_big.")
    p.add_argument("--data-dir", type=str, default=str(_DEFAULT_DATA_DIR))
    p.add_argument("--base-csv", type=str, default="base.csv")
    p.add_argument("--pred-csv", type=str, default="predicted.csv")
    p.add_argument("--float-csv", type=str, default="float_value.csv")
    p.add_argument("--out-json", type=str, default=str(_DEFAULT_OUT_JSON))
    p.add_argument("--min-points", type=int, default=5)
    p.add_argument("--max-skins", type=int, default=None, help="for debugging: only fit first N items")
    p.add_argument("--grid-n", type=int, default=300)

    # smooth
    p.add_argument("--smooth-frac", type=float, default=0.25)
    p.add_argument("--supersmooth-frac", type=float, default=0.35)
    p.add_argument("--smooth-min-pts", type=int, default=12)
    p.add_argument("--smooth-robust-iters", type=int, default=2)

    # segmented
    p.add_argument("--seg-min-seg", type=int, default=12)
    p.add_argument("--seg-z-thresh", type=float, default=4.0)
    p.add_argument("--seg-max-jumps", type=int, default=2)

    # hybrid
    p.add_argument("--hybrid-alpha", type=float, default=0.7)

    # outliers
    p.add_argument("--outlier-neighbors", type=int, default=6)
    p.add_argument("--outlier-z", type=float, default=3.5)
    p.add_argument("--outlier-min-abs-dev", type=float, default=0.03)

    args = p.parse_args(argv)

    try:
        payload = run_fit_all(
            Path(args.data_dir),
            base_csv=args.base_csv,
            pred_csv=args.pred_csv,
            float_csv=args.float_csv,
            out_json=Path(args.out_json),
            min_points=args.min_points,
            max_skins=args.max_skins,
            grid_n=args.grid_n,
            smooth_frac=args.smooth_frac,
            supersmooth_frac=args.supersmooth_frac,
            smooth_min_pts=args.smooth_min_pts,
            smooth_robust_iters=args.smooth_robust_iters,
            seg_min_seg=args.seg_min_seg,
            seg_z_thresh=args.seg_z_thresh,
            seg_max_jumps=args.seg_max_jumps,
            hybrid_alpha=args.hybrid_alpha,
            outlier_neighbors=args.outlier_neighbors,
            outlier_z=args.outlier_z,
            outlier_min_abs_dev=args.outlier_min_abs_dev,
        )
    except FileNotFoundError as e:
        print(str(e), file=sys.stderr)
        return 2
    except Exception as e:
        print(str(e), file=sys.stderr)
        return 1

    m = payload["meta"]
    print(f"saved: {args.out_json}")
    print(f"items ok: {m['items_ok']}/{m['items_total']} | skipped: {m['items_skipped']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
