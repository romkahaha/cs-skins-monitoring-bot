# `skin_homog`

Listing-level CSFloat collection for float-driven segmentation, quasi-homogeneity analysis, and shortlist generation.

This folder exists because one `market_hash_name` is not truly homogeneous. A skin can contain a wide value distribution driven by `float`, listing placement, and current market microstructure. The workflow here is split into:

- a heavy listing-level collector for deep float analysis
- a cheap first-pass preprocess for broad universe filtering
- a second-stage Steam risk enrich for shortlist filtering

## Main Components

Deep collector:

- [`skin_screener.py`](./skin_screener.py)
- [`skin_screener_runtime.json`](./skin_screener_runtime.json)
- [`skin_screener_runner.ipynb`](./skin_screener_runner.ipynb)
- [`skin_screener_analysis.ipynb`](./skin_screener_analysis.ipynb)

Cheap preprocess:

- [`screener_preprocess/preprocess_screener.py`](./screener_preprocess/preprocess_screener.py)
- [`screener_preprocess/preprocess_runtime.json`](./screener_preprocess/preprocess_runtime.json)
- [`screener_preprocess/preprocess_runner.ipynb`](./screener_preprocess/preprocess_runner.ipynb)
- [`screener_preprocess/preprocess_analysis.ipynb`](./screener_preprocess/preprocess_analysis.ipynb)

Steam risk enrich:

- [`screener_preprocess_risk/risk_preprocess.py`](./screener_preprocess_risk/risk_preprocess.py)
- [`screener_preprocess_risk/risk_runtime.json`](./screener_preprocess_risk/risk_runtime.json)
- [`screener_preprocess_risk/risk_runner.ipynb`](./screener_preprocess_risk/risk_runner.ipynb)
- [`screener_preprocess_risk/risk_analysis.ipynb`](./screener_preprocess_risk/risk_analysis.ipynb)

Supporting analysis:

- [`float_price_fit.py`](./float_price_fit.py)
- [`run_all_batches.ipynb`](./run_all_batches.ipynb)

## Heavy Collector Scope

`skin_screener.py` works at listing granularity, not at item snapshot granularity.

For each skin it pulls multiple CSFloat listings and stores panel-style datasets:

- columns = skins
- rows = listing slots within a run block

This is optimized for exploratory notebooks and float-dispersion fitting.

## Heavy Collector Features

Implemented in [`skin_screener.py`](./skin_screener.py):

- create mode and merge mode
- weighted mix mode across multiple CSFloat sorts
- pagination beyond the first 50 listings using `cursor`
- decoupled `PAGE_LIMIT` and `TARGET_UNIQUE`
- per-run shared row block instead of one block per item
- incremental persistence during `create`
- optional skip of known listing ids across runs
- optional skip of already existing items for resume-style reruns
- one or two CSFloat API keys with round-robin and cooldown
- retry ladder for `429`, `403`, `5xx`, and network failures

## Heavy Collector Modes

- `--create`
  Wipes the output folder, writes the first successful item as a fresh dataset, then appends all next items into the same run block.
- `--merge`
  Appends a new run block under existing data.
- `--mix`
  Uses weighted multi-sort collection from runtime config.
- `--no-mix --sort <sort_name>`
  Forces a single CSFloat sort.
- `--skip-known-ids`
  Filters out listing ids that already exist for the same item in saved history.
- `--ignore-existing-items`
  Skips an item entirely if its column already exists in saved data.

Default behavior is driven by runtime JSON.

## Heavy Collector Output Panels

Panel mapping is defined in `PANELS`. Current saved panels include:

- `ask.csv`
- `predicted.csv`
- `base.csv`
- `currency.csv`
- `float_value.csv`
- `listing_id.csv`
- `created_at.csv`
- `state.csv`
- `source_sort.csv`
- `quantity.csv`
- `sticker_count.csv`
- `sticker_value.csv`
- `watchers.csv`

Aggregate output:

- `_summary.csv`

Progress/debug artifact:

- `_screener_progress.log`

## Heavy Collector Listing Schema

Each parsed listing may contribute:

- `listing_id`
- `created_at`
- `state`
- `source_sort`
- `price`
- `predicted_price`
- `base_price`
- `currency`
- `float_value`
- `quantity`
- `sticker_count`
- `sticker_value_usd`
- `watchers`

Notes:

- `currency` may be empty even when prices are effectively USD from CSFloat.
- `sticker_count` is useful.
- `sticker_value_usd` currently depends on sticker `scm.price`; in observed payloads this often comes through as missing, so the value column is not yet reliable.

## Summary-Level Schema

`_summary.csv` stores per-skin aggregates such as:

- `item`
- `base_price`
- `reference_currency`
- `float_qty`
- `n_listings`
- ask min / median / max
- predicted mean / std / cv
- predicted min / max
- factor mean / std / cv

This is the compact table for ranking skins by homogeneity and fit quality.

## Panel Alignment Model

Within one run, all items share one row block.

If one item has fewer listings than another within the same run:

- the shorter numeric columns are padded with the structural sentinel `-1337`
- string-like columns stay empty

This is a storage/alignment mechanism, not a real market value.

Across different runs:

- `merge` appends a new run block under the existing ones
- repeated listing ids can either be kept as history or skipped with `--skip-known-ids`

## Cheap Preprocess Stage

The folder [`screener_preprocess/`](./screener_preprocess) is the broad-universe filter before the expensive homog collector.

It pulls only a small CSFloat sample per item and writes one row per item with cheap metrics:

- `base_price`
- `n_listings`
- `cap_hit`
- `discount_sample_n`
- `avg_discount`
- `median_discount`
- `avg_ask`
- `avg_predicted`

Interpretation:

- `n_listings` is observed listings up to `LISTINGS_CAP`
- `cap_hit = True` means the true count is at least that cap, not necessarily equal to it
- this stage is intentionally cheap and usually uses one request per item

Run behavior:

- `create` wipes the preprocess CSV/log and rescans the full list
- `merge` is resume-style, not historical append
- in `merge`, the script reads the existing preprocess CSV, skips item names already present there, and only collects the remaining items from the input list

This makes stage-1 suitable for long interrupted scans over very large universes.

The paired analysis notebook filters by:

- base price band
- rough liquidity
- average or median discount

and exports a new Python item list in `lists/`.

## Steam Risk Preprocess Stage

The folder [`screener_preprocess_risk/`](./screener_preprocess_risk) is the second-stage enrich after the cheap preprocess.

It reads the stage-1 CSV and adds Steam `pricehistory` features via the existing `base_screening_with_trades/fetchers.py` helpers:

- `steam_sales_7d_mean`
- `steam_sales_7d_median`
- `steam_sales_7d_p10`
- `steam_sales_7d_p25`
- `steam_sales_7d_p75`
- `steam_sales_7d_p90`
- `steam_sales_7d_min`
- `steam_sales_7d_max`
- `steam_sales_7d_n`

Derived risk metrics:

- `steam_sales_7d_iqr_risk%`
- `steam_sales_7d_band_risk%`
- `steam_sales_7d_downside_risk%`
- `steam_sales_7d_tail_ratio`
- `steam_turnover_proxy`
- `steam_discount_risk_score`

This stage is slower and more fragile because it relies on Steam `pricehistory`, cookies, and Steam-side pacing.

## Runtime / Operational Controls

Heavy collector runtime:

- `skin_screener_runtime.json`
- override path via `SKIN_SCREENER_RUNTIME_CONFIG`

Cheap preprocess runtime:

- `screener_preprocess/preprocess_runtime.json`
- override path via `PREPROCESS_SCREENER_RUNTIME_CONFIG`

Risk preprocess runtime:

- `screener_preprocess_risk/risk_runtime.json`
- override path via `RISK_PREPROCESS_RUNTIME_CONFIG`

Configs are designed so long-running jobs can be retuned without touching code.

## Candidate Inputs

Candidate skin universes live in [`skin_cands/`](./skin_cands/) and in the project-wide `lists/` folder.

Typical pattern:

1. broad universe list
2. cheap preprocess
3. risk enrich
4. export filtered list
5. run heavy homog collector only on the shortlist

## Storage Defaults

Heavy collector default output:

- `skin_homog/data_skins`

Often overridden in notebooks to:

- `skin_homog/data_skins_big`

Overridable via:

- `SKIN_SCREENER_OUTPUT_DIR`

Cheap preprocess default output:

- `skin_homog/screener_preprocess/preprocess_metrics.csv`

Risk preprocess default output:

- `skin_homog/screener_preprocess_risk/risk_metrics.csv`

## Why This Split Exists

Running the heavy CSFloat collector on the full universe is expensive because mix sampling, pagination, and panel persistence are designed for research-quality listing coverage.

The two preprocess stages let you filter first by:

- price band
- observed liquidity
- cheap discount proxies
- Steam trade depth and risk

and only then spend deep CSFloat collection budget on the shortlist.

## Recommended Extension Points

- add new listing fields inside `parse_listing(...)`
- add new panels by extending `PANELS`
- add new summary stats inside `summarise(...)`
- add extra cheap preprocess metrics in `preprocess_screener.py`
- add new Steam risk features in `risk_preprocess.py`
- add parquet or long-table export if downstream analysis grows beyond notebooks
