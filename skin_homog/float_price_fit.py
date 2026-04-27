"""
Цена/рейшио от float: квадратичная регрессия (OLS) или k-NN по одному признаку float.

Панели `data_skins` (строки = листинги, колонки = скины).

Поддерживаемые target_mode:
- `price`: таргет = price_csv (по умолчанию `predicted.csv`)
- `ratio`: таргет = price_csv / base_csv (по умолчанию `predicted.csv / base.csv`)
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import numpy as np
import pandas as pd

# Скринер тянет до 50 листингов; для устойчивости квадратики — минимум 5 пар (float, price).
# По умолчанию y — CSFloat `predicted_price` (`predicted.csv`), не ask.
DEFAULT_PRICE_CSV = "predicted.csv"
DEFAULT_BASE_CSV = "base.csv"
DEFAULT_TARGET_MODE = "price"
DEFAULT_MIN_LISTINGS = 5
DEFAULT_MAX_LISTINGS = 50
_MIN_DISTINCT_FLOATS = 3
STRUCTURAL_GAP_SENTINEL = -1337.0
TargetMode = Literal["price", "ratio"]


@dataclass(frozen=True)
class QuadFloatPriceFit:
    """Результат OLS по столбцу [1, x, x²]."""

    beta0: float
    beta1: float
    beta2: float
    n: int
    floats: np.ndarray
    prices: np.ndarray
    fitted: np.ndarray

    def predict(self, x: float) -> float:
        xf = float(x)
        return self.beta0 + self.beta1 * xf + self.beta2 * xf * xf


@dataclass(frozen=True)
class KnnFloatPriceFit:
    """k ближайших соседей по |float − x|; in-sample `fitted` — leave-one-out."""

    k: int
    weights: Literal["uniform", "distance"]
    n: int
    floats: np.ndarray
    prices: np.ndarray
    fitted: np.ndarray

    def predict(self, x: float) -> float:
        return _knn_predict_1d(
            self.floats, self.prices, float(x), self.k, exclude_idx=None, weights=self.weights
        )


def _default_data_skins_dir() -> Path:
    here = Path(__file__).resolve().parent
    return here / "data_skins"


def align_float_price(
    floats: np.ndarray | list | pd.Series,
    prices: np.ndarray | list | pd.Series,
    *,
    max_listings: int | None = DEFAULT_MAX_LISTINGS,
) -> tuple[np.ndarray, np.ndarray]:
    """Оставляет только валидные пары (float, target), отбрасывая structural gaps и мусор."""
    x = np.asarray(floats, dtype=float).ravel()
    y = np.asarray(prices, dtype=float).ravel()
    n = min(x.size, y.size)
    x, y = x[:n], y[:n]
    if max_listings is not None and n > int(max_listings):
        x, y = x[: int(max_listings)], y[: int(max_listings)]
    ok = (
        np.isfinite(x)
        & np.isfinite(y)
        & (x != STRUCTURAL_GAP_SENTINEL)
        & (y != STRUCTURAL_GAP_SENTINEL)
        & (x >= 0.0)
        & (x <= 1.0)
        & (y > 0.0)
    )
    return x[ok], y[ok]


def _knn_predict_1d(
    x_train: np.ndarray,
    y_train: np.ndarray,
    x_query: float,
    k: int,
    *,
    exclude_idx: int | None,
    weights: Literal["uniform", "distance"],
) -> float:
    x_train = np.asarray(x_train, dtype=float).ravel()
    y_train = np.asarray(y_train, dtype=float).ravel()
    d = np.abs(x_train - x_query)
    if exclude_idx is not None:
        d = d.copy()
        d[int(exclude_idx)] = np.inf
    n_avail = int(np.isfinite(d).sum())
    if n_avail < 1:
        raise ValueError("k-NN: нет доступных соседей")
    kk = min(max(1, int(k)), n_avail)
    idx = np.argpartition(d, kk - 1)[:kk]
    if weights == "uniform":
        return float(np.mean(y_train[idx]))
    w = 1.0 / (d[idx] + 1e-9)
    return float(np.sum(w * y_train[idx]) / np.sum(w))


def _knn_fitted_loo(
    x: np.ndarray, y: np.ndarray, k: int, weights: Literal["uniform", "distance"]
) -> np.ndarray:
    n = len(x)
    out = np.empty(n, dtype=float)
    for i in range(n):
        out[i] = _knn_predict_1d(x, y, float(x[i]), k, exclude_idx=i, weights=weights)
    return out


def fit_float_knn_price(
    floats: np.ndarray | list | pd.Series,
    prices: np.ndarray | list | pd.Series,
    *,
    k: int = 5,
    weights: Literal["uniform", "distance"] = "distance",
    min_listings: int = DEFAULT_MIN_LISTINGS,
    max_listings: int | None = DEFAULT_MAX_LISTINGS,
) -> KnnFloatPriceFit:
    """k-NN по float; fitted — среднее (или веса 1/|Δf|) по k соседям, LOO на обучающих точках."""
    x, y = align_float_price(floats, prices, max_listings=max_listings)
    if x.size < int(min_listings):
        raise ValueError(
            f"нужно ≥{min_listings} валидных пар (float, price); получилось {x.size}"
        )
    if x.size < 2:
        raise ValueError("k-NN нужен хотя бы 2 листинга")
    k_eff = min(max(1, int(k)), x.size)
    fitted = _knn_fitted_loo(x, y, k_eff, weights)
    return KnnFloatPriceFit(
        k=k_eff,
        weights=weights,
        n=int(len(x)),
        floats=x,
        prices=y,
        fitted=fitted,
    )


def fit_float_quad_price(
    floats: np.ndarray | list | pd.Series,
    prices: np.ndarray | list | pd.Series,
    *,
    min_listings: int = DEFAULT_MIN_LISTINGS,
    max_listings: int | None = DEFAULT_MAX_LISTINGS,
    rcond: float | None = None,
) -> QuadFloatPriceFit:
    """
    OLS: y ~ 1 + x + x². Возвращает коэффициенты и in-sample предикт `fitted`.
    """
    x, y = align_float_price(floats, prices, max_listings=max_listings)
    if x.size < int(min_listings):
        raise ValueError(
            f"нужно ≥{min_listings} валидных пар (float, price); получилось {x.size}"
        )
    if len(np.unique(x)) < _MIN_DISTINCT_FLOATS:
        raise ValueError(
            f"для квадратики нужно ≥{_MIN_DISTINCT_FLOATS} различных float; получилось {len(np.unique(x))}"
        )
    X = np.column_stack([np.ones(len(x)), x, x * x])
    beta, *_ = np.linalg.lstsq(X, y, rcond=rcond)
    b0, b1, b2 = float(beta[0]), float(beta[1]), float(beta[2])
    fitted = X @ beta
    return QuadFloatPriceFit(
        beta0=b0,
        beta1=b1,
        beta2=b2,
        n=int(len(x)),
        floats=x,
        prices=y,
        fitted=fitted.astype(float),
    )


def load_skin_float_and_target(
    skin_name: str,
    data_dir: str | Path | None = None,
    *,
    target_mode: TargetMode = DEFAULT_TARGET_MODE,
    price_csv: str = DEFAULT_PRICE_CSV,
    base_csv: str = DEFAULT_BASE_CSV,
    float_csv: str = "float_value.csv",
    max_listings: int | None = DEFAULT_MAX_LISTINGS,
) -> tuple[np.ndarray, np.ndarray]:
    """Читает float_value и выбранный таргет; возвращает (float, target) по одному скину."""
    root = Path(data_dir) if data_dir is not None else _default_data_skins_dir()
    p_path = root / price_csv
    f_path = root / float_csv
    if not p_path.is_file():
        raise FileNotFoundError(p_path)
    if not f_path.is_file():
        raise FileNotFoundError(f_path)
    price_df = pd.read_csv(p_path)
    fl = pd.read_csv(f_path)
    if skin_name not in price_df.columns:
        raise KeyError(skin_name)
    if skin_name not in fl.columns:
        raise KeyError(skin_name)
    pa = price_df[skin_name]
    fa = fl[skin_name]
    if target_mode == "price":
        target = pd.to_numeric(pa, errors="coerce")
    elif target_mode == "ratio":
        b_path = root / base_csv
        if not b_path.is_file():
            raise FileNotFoundError(b_path)
        base_df = pd.read_csv(b_path)
        if skin_name not in base_df.columns:
            raise KeyError(skin_name)
        ba = pd.to_numeric(base_df[skin_name], errors="coerce")
        pa_num = pd.to_numeric(pa, errors="coerce")
        ba_np = ba.to_numpy(dtype=float)
        pa_np = pa_num.to_numpy(dtype=float)
        valid_base = (ba_np > 0.0) & (ba_np != STRUCTURAL_GAP_SENTINEL)
        valid_price = np.isfinite(pa_np) & (pa_np != STRUCTURAL_GAP_SENTINEL)
        target = np.where(valid_base & valid_price, pa_np / ba_np, np.nan)
    else:
        raise ValueError("target_mode ожидает 'price' или 'ratio'")
    x, y = align_float_price(fa.values, target, max_listings=max_listings)
    return x, y


def load_skin_float_and_price(
    skin_name: str,
    data_dir: str | Path | None = None,
    *,
    price_csv: str = DEFAULT_PRICE_CSV,
    float_csv: str = "float_value.csv",
    max_listings: int | None = DEFAULT_MAX_LISTINGS,
) -> tuple[np.ndarray, np.ndarray]:
    """Обратносуместимый alias для абсолютной цены."""
    return load_skin_float_and_target(
        skin_name,
        data_dir,
        target_mode="price",
        price_csv=price_csv,
        float_csv=float_csv,
        max_listings=max_listings,
    )


def fit_skin_float_quad(
    skin_name: str,
    data_dir: str | Path | None = None,
    *,
    target_mode: TargetMode = DEFAULT_TARGET_MODE,
    price_csv: str = DEFAULT_PRICE_CSV,
    base_csv: str = DEFAULT_BASE_CSV,
    float_csv: str = "float_value.csv",
    min_listings: int = DEFAULT_MIN_LISTINGS,
    max_listings: int | None = DEFAULT_MAX_LISTINGS,
) -> QuadFloatPriceFit:
    """Загружает target + float_value и строит квадратичный фит."""
    x, y = load_skin_float_and_target(
        skin_name,
        data_dir,
        target_mode=target_mode,
        price_csv=price_csv,
        base_csv=base_csv,
        float_csv=float_csv,
        max_listings=max_listings,
    )
    return fit_float_quad_price(
        x, y, min_listings=min_listings, max_listings=None
    )


def fit_skin_float_knn(
    skin_name: str,
    data_dir: str | Path | None = None,
    *,
    k: int = 5,
    weights: Literal["uniform", "distance"] = "distance",
    target_mode: TargetMode = DEFAULT_TARGET_MODE,
    price_csv: str = DEFAULT_PRICE_CSV,
    base_csv: str = DEFAULT_BASE_CSV,
    float_csv: str = "float_value.csv",
    min_listings: int = DEFAULT_MIN_LISTINGS,
    max_listings: int | None = DEFAULT_MAX_LISTINGS,
) -> KnnFloatPriceFit:
    """Те же панели, что у квадратики; фит k-NN по float."""
    x, y = load_skin_float_and_target(
        skin_name,
        data_dir,
        target_mode=target_mode,
        price_csv=price_csv,
        base_csv=base_csv,
        float_csv=float_csv,
        max_listings=max_listings,
    )
    return fit_float_knn_price(
        x,
        y,
        k=k,
        weights=weights,
        min_listings=min_listings,
        max_listings=None,
    )


def predict_skin_target_at_float(
    skin_name: str,
    float_val: float,
    data_dir: str | Path | None = None,
    *,
    method: Literal["quad", "knn"] = "quad",
    knn_k: int = 5,
    knn_weights: Literal["uniform", "distance"] = "distance",
    target_mode: TargetMode = DEFAULT_TARGET_MODE,
    price_csv: str = DEFAULT_PRICE_CSV,
    base_csv: str = DEFAULT_BASE_CSV,
    float_csv: str = "float_value.csv",
    min_listings: int = DEFAULT_MIN_LISTINGS,
    max_listings: int | None = DEFAULT_MAX_LISTINGS,
) -> float:
    """Загрузка панелей → фит → предикт таргета при float (квадратика или k-NN)."""
    if method == "quad":
        fit = fit_skin_float_quad(
            skin_name,
            data_dir,
            target_mode=target_mode,
            price_csv=price_csv,
            base_csv=base_csv,
            float_csv=float_csv,
            min_listings=min_listings,
            max_listings=max_listings,
        )
    elif method == "knn":
        fit = fit_skin_float_knn(
            skin_name,
            data_dir,
            k=knn_k,
            weights=knn_weights,
            target_mode=target_mode,
            price_csv=price_csv,
            base_csv=base_csv,
            float_csv=float_csv,
            min_listings=min_listings,
            max_listings=max_listings,
        )
    else:
        raise ValueError("method ожидает 'quad' или 'knn'")
    return fit.predict(float_val)


def predict_skin_price_at_float(
    skin_name: str,
    float_val: float,
    data_dir: str | Path | None = None,
    *,
    method: Literal["quad", "knn"] = "quad",
    knn_k: int = 5,
    knn_weights: Literal["uniform", "distance"] = "distance",
    price_csv: str = DEFAULT_PRICE_CSV,
    float_csv: str = "float_value.csv",
    min_listings: int = DEFAULT_MIN_LISTINGS,
    max_listings: int | None = DEFAULT_MAX_LISTINGS,
) -> float:
    """Обратносуместимый alias для абсолютной цены."""
    return predict_skin_target_at_float(
        skin_name,
        float_val,
        data_dir,
        method=method,
        knn_k=knn_k,
        knn_weights=knn_weights,
        target_mode="price",
        price_csv=price_csv,
        float_csv=float_csv,
        min_listings=min_listings,
        max_listings=max_listings,
    )


def predict_skin_ratio_at_float(
    skin_name: str,
    float_val: float,
    data_dir: str | Path | None = None,
    *,
    method: Literal["quad", "knn"] = "quad",
    knn_k: int = 5,
    knn_weights: Literal["uniform", "distance"] = "distance",
    price_csv: str = DEFAULT_PRICE_CSV,
    base_csv: str = DEFAULT_BASE_CSV,
    float_csv: str = "float_value.csv",
    min_listings: int = DEFAULT_MIN_LISTINGS,
    max_listings: int | None = DEFAULT_MAX_LISTINGS,
) -> float:
    """Предсказывает ratio = price_csv / base_csv при заданном float."""
    return predict_skin_target_at_float(
        skin_name,
        float_val,
        data_dir,
        method=method,
        knn_k=knn_k,
        knn_weights=knn_weights,
        target_mode="ratio",
        price_csv=price_csv,
        base_csv=base_csv,
        float_csv=float_csv,
        min_listings=min_listings,
        max_listings=max_listings,
    )


__all__ = [
    "DEFAULT_PRICE_CSV",
    "DEFAULT_BASE_CSV",
    "DEFAULT_TARGET_MODE",
    "DEFAULT_MIN_LISTINGS",
    "DEFAULT_MAX_LISTINGS",
    "QuadFloatPriceFit",
    "KnnFloatPriceFit",
    "TargetMode",
    "align_float_price",
    "fit_float_quad_price",
    "fit_float_knn_price",
    "load_skin_float_and_target",
    "load_skin_float_and_price",
    "fit_skin_float_quad",
    "fit_skin_float_knn",
    "predict_skin_target_at_float",
    "predict_skin_price_at_float",
    "predict_skin_ratio_at_float",
]
