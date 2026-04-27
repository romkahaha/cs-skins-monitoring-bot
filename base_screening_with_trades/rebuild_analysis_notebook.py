from __future__ import annotations

import json
from pathlib import Path
from textwrap import dedent


ROOT = Path(__file__).resolve().parent
NOTEBOOK = ROOT / "analysis_with_trades.ipynb"


def _lines(text: str) -> list[str]:
    return dedent(text).lstrip("\n").splitlines(keepends=True)


def md(text: str) -> dict:
    return {
        "cell_type": "markdown",
        "metadata": {},
        "source": _lines(text),
    }


def code(text: str) -> dict:
    return {
        "cell_type": "code",
        "execution_count": None,
        "metadata": {},
        "outputs": [],
        "source": _lines(text),
    }


cells = [
    md(
        """
        # Анализ `screening_with_trades`

        Цель ноутбука: смотреть арбитраж между Steam и CSFloat с учетом того, что после покупки в Steam предмет сидит в **7-дневном бане**. Поэтому фильтры здесь не про "красивый датасет", а про то, чтобы:

        - не заходить в слишком тонкие и шумные позиции
        - не переоценивать большой spread, если у Steam по истории есть плохой downside
        - сохранить удобный просмотр и **больших**, и **маленьких** spread'ов

        Структура намеренно простая:
        1. импорт и препроцесс
        2. risk-фильтры
        3. display risk-passed выборки по `spread_pred%` сначала по убыванию, потом по возрастанию
        """
    ),
    md(
        """
        ## Risk Metrics Cheatsheet

        - `steam_sales_7d_iqr_risk% = (p75 - p25) / mean * 100`
          Центральный разброс сделок за 7 дней. Больше = шире нормальный диапазон цен.

        - `steam_sales_7d_band_risk% = (p90 - p10) / mean * 100`
          Широкий диапазон сделок за 7 дней. Больше = выше общая турбулентность.

        - `steam_sales_7d_downside_risk% = (median - p10) / mean * 100`
          Насколько нижний хвост уходит вниз от типичной цены. Больше = хуже downside.

        - `steam_sales_7d_tail_ratio = p10 / median`
          Насколько низ хвоста близок к медиане. Ближе к `1` = лучше.

        - `steam_daily_ret_3d = current_daily_median / daily_median_3d_ago - 1`
          Изменение дневной медианы за 3 дня. Ниже = слабее краткосрок.

        - `steam_daily_ret_7d = current_daily_median / daily_median_7d_ago - 1`
          Изменение дневной медианы за 7 дней. Ниже = слабее фон на горизонте холда.

        - `steam_daily_slope_7d = slope(log(daily_median))`
          Наклон лог-цены за 7 дней. Ниже = слабее устойчивый тренд.

        - `steam_daily_ema_gap_3_14 = EMA(3) / EMA(14) - 1`
          Короткая EMA против длинной. Ниже `0` = локальный momentum слабый.

        - `steam_daily_range_14d_pct = (max_14d - min_14d) / current_daily_median`
          Полный диапазон дневной медианы за 14 дней. Больше = шире среда.

        - `steam_daily_downside_14d_pct = (current_daily_median - min_14d) / current_daily_median`
          Насколько текущая цена выше недавнего 14-day low. Больше = под текущей ценой уже был более глубокий низ.

        - `steam_sales_7d_n`
          Число сделок в weekly summary. Больше = risk-метрики надежнее.
        """
    ),
    code(
        """
        from __future__ import annotations

        from pathlib import Path
        import numpy as np
        import pandas as pd
        from IPython.display import display
        from matplotlib import colors

        pd.set_option("display.max_columns", 50)
        pd.set_option("display.max_colwidth", 120)

        USE_LATEST_SCREENING = True
        SCREENING_GLOBS = ("screening_full_trades_*.csv", "screening_sub_trades_*.csv")


        def resolve_project_root() -> Path:
            cwd = Path.cwd().resolve()
            if (cwd / "data_with_trades").is_dir():
                return cwd
            if (cwd.parent / "data_with_trades").is_dir():
                return cwd.parent
            raise FileNotFoundError(
                "Не удалось найти папку data_with_trades ни в cwd, ни уровнем выше"
            )


        PROJECT_ROOT = resolve_project_root()
        SCREENING_DATA_DIR = PROJECT_ROOT / "data_with_trades"
        CSV_FILE_MANUAL = SCREENING_DATA_DIR / "screening_full_trades_20260422_044314.csv"

        ITEM_INCLUDE_ANY: list[str] = []
        ITEM_EXCLUDE_ANY: list[str] = []
        TOP_N = 50

        RISK_FILTERS = {
            "steam_ask_min": 1.25,
            "steam_ask_max": 35.0,
            "float_qty_min": 80,
            "steam_sales_n_min": 50,
            "downside_risk_max": 10.0,
            "tail_ratio_min": 0.90,
            "downside_14d_max": 0.12,
        }


        def resolve_csv() -> Path:
            if not USE_LATEST_SCREENING:
                return CSV_FILE_MANUAL.resolve()

            found: list[Path] = []
            seen: set[Path] = set()
            for pattern in SCREENING_GLOBS:
                for path in SCREENING_DATA_DIR.glob(pattern):
                    rp = path.resolve()
                    if rp not in seen:
                        seen.add(rp)
                        found.append(path)
            found.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            if not found:
                raise FileNotFoundError(
                    f"Нет файлов {SCREENING_GLOBS} в {SCREENING_DATA_DIR.resolve()}"
                )
            return found[0].resolve()


        def contains_any(series: pd.Series, needles: list[str]) -> pd.Series:
            if not needles:
                return pd.Series(True, index=series.index)
            pattern = "|".join(map(str, needles))
            return series.str.contains(pattern, case=False, na=False, regex=True)
        """
    ),
    code(
        """
        csv_file = resolve_csv()
        df = pd.read_csv(csv_file)

        for col in df.columns:
            if col != "item":
                df[col] = pd.to_numeric(df[col], errors="coerce")

        df = df.copy()
        df["tail_gap%"] = (1.0 - df["steam_sales_7d_tail_ratio"]) * 100.0

        core_cols = [
            "item",
            "steam_ask",
            "float_ask",
            "float_pred",
            "float_qty",
            "spread_ask%",
            "spread_pred%",
            "steam_sales_7d_n",
            "steam_sales_7d_iqr_risk%",
            "steam_sales_7d_band_risk%",
            "steam_sales_7d_downside_risk%",
            "steam_sales_7d_tail_ratio",
            "tail_gap%",
            "steam_daily_ret_3d",
            "steam_daily_ret_7d",
            "steam_daily_slope_7d",
            "steam_daily_ema_gap_3_14",
            "steam_daily_range_14d_pct",
            "steam_daily_downside_14d_pct",
        ]

        print("CSV_FILE =", csv_file)
        print(f"rows={len(df)}, cols={len(df.columns)}")
        display(df[core_cols].head(10))
        """
    ),
    md(
        """
        ## Risk Filters

        Ниже только **risk / liquidity gate**. Спред здесь **не фильтруется**, чтобы после прохождения риска можно было отдельно посмотреть и лучшие большие spread'ы, и самые слабые маленькие.
        """
    ),
    code(
        """
        filter_specs: list[tuple[str, pd.Series]] = [
            (
                f"steam_ask in [{RISK_FILTERS['steam_ask_min']}, {RISK_FILTERS['steam_ask_max']}]",
                df["steam_ask"].between(RISK_FILTERS["steam_ask_min"], RISK_FILTERS["steam_ask_max"]),
            ),
            (
                f"float_qty >= {RISK_FILTERS['float_qty_min']}",
                df["float_qty"] >= RISK_FILTERS["float_qty_min"],
            ),
            (
                f"steam_sales_7d_n >= {RISK_FILTERS['steam_sales_n_min']}",
                df["steam_sales_7d_n"] >= RISK_FILTERS["steam_sales_n_min"],
            ),
            (
                f"steam_sales_7d_downside_risk% <= {RISK_FILTERS['downside_risk_max']}",
                df["steam_sales_7d_downside_risk%"] <= RISK_FILTERS["downside_risk_max"],
            ),
            (
                f"steam_sales_7d_tail_ratio >= {RISK_FILTERS['tail_ratio_min']}",
                df["steam_sales_7d_tail_ratio"] >= RISK_FILTERS["tail_ratio_min"],
            ),
            (
                f"steam_daily_downside_14d_pct <= {RISK_FILTERS['downside_14d_max']}",
                df["steam_daily_downside_14d_pct"] <= RISK_FILTERS["downside_14d_max"],
            ),
        ]

        if ITEM_INCLUDE_ANY:
            filter_specs.append((f"include_any={ITEM_INCLUDE_ANY}", contains_any(df["item"], ITEM_INCLUDE_ANY)))
        if ITEM_EXCLUDE_ANY:
            filter_specs.append((f"exclude_any={ITEM_EXCLUDE_ANY}", ~contains_any(df["item"], ITEM_EXCLUDE_ANY)))

        risk_masks = {label: mask.fillna(False).astype(bool) for label, mask in filter_specs}
        risk_pass = pd.Series(True, index=df.index)
        for mask in risk_masks.values():
            risk_pass &= mask

        fail_matrix = pd.DataFrame({label: ~mask for label, mask in risk_masks.items()}, index=df.index)
        df["risk_pass"] = risk_pass
        df["risk_fail_count"] = fail_matrix.sum(axis=1)
        df["risk_fail_reasons"] = fail_matrix.apply(
            lambda row: ", ".join([col for col, failed in row.items() if failed]) if row.any() else "-",
            axis=1,
        )

        risk_report = pd.DataFrame(
            [
                {
                    "rule": label,
                    "passed_rows": int(mask.sum()),
                    "failed_rows": int((~mask).sum()),
                }
                for label, mask in risk_masks.items()
            ]
        )

        risk_df = df.loc[df["risk_pass"]].copy()
        failed_df = df.loc[~df["risk_pass"]].copy()

        print(f"{len(df)} loaded -> {len(risk_df)} passed risk filters -> {len(failed_df)} failed")
        display(risk_report)
        display(
            failed_df[
                [
                    "item",
                    "spread_pred%",
                    "steam_sales_7d_n",
                    "steam_sales_7d_band_risk%",
                    "steam_sales_7d_downside_risk%",
                    "steam_sales_7d_tail_ratio",
                    "steam_daily_downside_14d_pct",
                    "risk_fail_count",
                    "risk_fail_reasons",
                ]
            ]
            .sort_values(["risk_fail_count", "spread_pred%"], ascending=[True, False])
            .head(20)
            .reset_index(drop=True)
        )
        """
    ),
    md(
        """
        ## Display By Spread

        Одна и та же `risk_df` показывается в двух срезах:
        - сначала где spread самый большой
        - потом где spread самый маленький
        """
    ),
    code(
        """
        display_cols = [
            "item",
            "steam_ask",
            "float_ask",
            "float_pred",
            "float_qty",
            "spread_ask%",
            "spread_pred%",
            "steam_sales_7d_n",
            "steam_sales_7d_iqr_risk%",
            "steam_sales_7d_band_risk%",
            "steam_sales_7d_downside_risk%",
            "steam_sales_7d_tail_ratio",
            "tail_gap%",
            "steam_daily_ret_3d",
            "steam_daily_ret_7d",
            "steam_daily_slope_7d",
            "steam_daily_ema_gap_3_14",
            "steam_daily_range_14d_pct",
            "steam_daily_downside_14d_pct",
            "risk_fail_reasons",
        ]

        color_specs = {
            "spread_ask%": "high_good",
            "spread_pred%": "high_good",
            "steam_sales_7d_n": "high_good",
            "steam_sales_7d_iqr_risk%": "low_good",
            "steam_sales_7d_band_risk%": "low_good",
            "steam_sales_7d_downside_risk%": "low_good",
            "steam_sales_7d_tail_ratio": "high_good",
            "tail_gap%": "low_good",
            "steam_daily_ret_3d": "high_good",
            "steam_daily_ret_7d": "high_good",
            "steam_daily_slope_7d": "high_good",
            "steam_daily_ema_gap_3_14": "high_good",
            "steam_daily_range_14d_pct": "low_good",
            "steam_daily_downside_14d_pct": "low_good",
        }


        def make_quantile_styler(frame: pd.DataFrame, overrides: dict[str, str] | None = None):
            cmap = colors.LinearSegmentedColormap.from_list(
                "risk_grid", ["#d65f5f", "#f2e6a7", "#5dbb63"]
            )
            active_specs = dict(color_specs)
            active_specs.update(overrides or {})

            def style_series(s: pd.Series):
                spec = active_specs.get(s.name)
                if spec is None or not pd.api.types.is_numeric_dtype(s):
                    return [""] * len(s)
                valid = s.dropna()
                if valid.nunique() <= 1:
                    return [""] * len(s)
                pct = valid.rank(method="average", pct=True)
                if spec == "low_good":
                    pct = 1.0 - pct
                out = []
                for idx in s.index:
                    if idx not in pct.index:
                        out.append("")
                        continue
                    out.append(
                        f"background-color: {colors.to_hex(cmap(float(pct.loc[idx])))}; color: black;"
                    )
                return out

            return (
                frame.style
                .format(
                    {
                        "steam_ask": "{:.2f}",
                        "float_ask": "{:.2f}",
                        "float_pred": "{:.2f}",
                        "float_qty": "{:.0f}",
                        "spread_ask%": "{:.2f}",
                        "spread_pred%": "{:.2f}",
                        "steam_sales_7d_n": "{:.0f}",
                        "steam_sales_7d_iqr_risk%": "{:.2f}",
                        "steam_sales_7d_band_risk%": "{:.2f}",
                        "steam_sales_7d_downside_risk%": "{:.2f}",
                        "steam_sales_7d_tail_ratio": "{:.4f}",
                        "tail_gap%": "{:.2f}",
                        "steam_daily_ret_3d": "{:.2%}",
                        "steam_daily_ret_7d": "{:.2%}",
                        "steam_daily_slope_7d": "{:.4f}",
                        "steam_daily_ema_gap_3_14": "{:.2%}",
                        "steam_daily_range_14d_pct": "{:.2%}",
                        "steam_daily_downside_14d_pct": "{:.2%}",
                    },
                    na_rep="-",
                )
                .apply(style_series, axis=0)
            )

        print(f"TOP {TOP_N} risk-passed rows by spread_pred% DESC")
        top_desc = (
            risk_df[display_cols]
            .sort_values(["spread_pred%", "spread_ask%"], ascending=[False, False])
            .head(TOP_N)
            .reset_index(drop=True)
        )
        display(make_quantile_styler(top_desc))
        """
    ),
    code(
        """
        print(f"TOP {TOP_N} risk-passed rows by spread_pred% ASC")
        top_asc = (
            risk_df[display_cols]
            .sort_values(["spread_pred%", "spread_ask%"], ascending=[True, True])
            .head(TOP_N)
            .reset_index(drop=True)
        )
        display(
            make_quantile_styler(
                top_asc,
                overrides={
                    "spread_ask%": "low_good",
                    "spread_pred%": "low_good",
                },
            )
        )
        """
    ),
    md(
        """
        ## Breakeven Spread

        Пара A→B:
        - на ноге **A** покупаем в Steam и потом выходим в Float
        - на ноге **B** покупаем в Float и потом выходим в Steam

        Если нога A слабая и дает маленький или отрицательный edge, можно посчитать, какой **минимальный spread на B** нужен, чтобы вся связка хотя бы вышла в ноль после комиссий.
        """
    ),
    code(
        """
        STEAM_FEE = 0.15
        FLOAT_FEE = 0.02

        A_SPREAD_INPUTS = [-20, -15, -10, -5, 0, 5, 10, 15, 20]


        def leg_a_after_fees_from_spread(spread_a_pct: float, float_fee: float = FLOAT_FEE) -> float:
            ratio_a = 1.0 - (spread_a_pct / 100.0)
            return ratio_a * (1.0 - float_fee)


        def breakeven_b_spread_from_a(
            spread_a_pct: float,
            *,
            steam_fee: float = STEAM_FEE,
            float_fee: float = FLOAT_FEE,
        ) -> float | None:
            leg_a = leg_a_after_fees_from_spread(spread_a_pct, float_fee=float_fee)
            if leg_a <= 0:
                return None
            required_ratio_b = 1.0 / (leg_a * (1.0 - steam_fee))
            if required_ratio_b <= 0:
                return None
            return (1.0 - (1.0 / required_ratio_b)) * 100.0


        rows = []
        for spread_a in A_SPREAD_INPUTS:
            leg_a = leg_a_after_fees_from_spread(spread_a)
            need_b = breakeven_b_spread_from_a(spread_a)
            rows.append(
                {
                    "A_spread%": spread_a,
                    "A_leg_after_float_fee": round(leg_a, 4),
                    "min_B_spread%_for_breakeven": round(need_b, 2) if need_b is not None else None,
                }
            )

        breakeven_df = pd.DataFrame(rows)
        display(breakeven_df)
        """
    ),
]


notebook = {
    "cells": cells,
    "metadata": {
        "kernelspec": {
            "display_name": "Python 3",
            "language": "python",
            "name": "python3",
        },
        "language_info": {
            "name": "python",
            "version": "3.14",
        },
    },
    "nbformat": 4,
    "nbformat_minor": 5,
}


NOTEBOOK.write_text(json.dumps(notebook, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
print(f"written {NOTEBOOK}")
