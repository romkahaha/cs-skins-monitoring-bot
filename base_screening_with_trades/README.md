# `base_screening_with_trades`

Snapshot-level screening enriched with recent Steam trade-history statistics.

This folder extends the base spread screen by joining current asks with realized Steam trade behavior from `pricehistory`. For data work, this is the main decision-grade snapshot layer because it combines current cross-market pricing with recent Steam execution context.

## Technical Scope

This layer keeps the same one-row-per-item grain as `base_screening_and_anal`, but adds a second Steam source:

- current ask from `market/priceoverview`
- recent realized sales from `market/pricehistory`
- current ask and reference values from CSFloat

This is still snapshot-level data. It does not go down to listing-level dispersion inside one item.

## Source Systems

- Steam `market/priceoverview`
- Steam `market/pricehistory`
- CSFloat listings API

## Main Entry Points

- [`fetchers.py`](./fetchers.py)
- `screening_with_trades.ipynb`

## Main Functions

Key callables in [`fetchers.py`](./fetchers.py):

- `fetch_all_prices(...)`
- `fetch_all_prices_with_trades(...)`
- `get_scm_trade_stats(...)`

For a new engineer, `fetch_all_prices_with_trades(...)` is the main production-facing function in this folder.

## Grain and Data Contract

Output grain:

- one row per item
- row is emitted only when both Steam current price and CSFloat reference are available

The trades component is an enrichment on top of that row.

## Output Schema

Base columns:

- `item`
- `steam_ask`
- `float_ask`
- `float_pred`
- `float_base`
- `float_qty`
- `spread_ask%`
- `spread_pred%`

Trade-history columns:

- `steam_sales_7d_mean`
- `steam_sales_7d_median`
- `steam_sales_7d_p10`
- `steam_sales_7d_p25`
- `steam_sales_7d_p75`
- `steam_sales_7d_p90`
- `steam_sales_7d_min`
- `steam_sales_7d_max`
- `steam_sales_7d_n`

Derived from trade history (optional enrichment):

- `steam_sales_7d_iqr_risk%`
- `steam_sales_7d_band_risk%`
- `steam_sales_7d_downside_risk%`
- `steam_sales_7d_tail_ratio`
- `steam_daily_ret_3d`
- `steam_daily_ret_7d`
- `steam_daily_slope_7d`
- `steam_daily_ema_gap_3_14`
- `steam_daily_range_14d_pct`
- `steam_daily_downside_14d_pct`

Optional columns:

- `steam_ask_eur`
- `steam_ask_usd`
- `fx_usd_to_eur`

The actual time window is configurable even though the exported naming follows the default 7-day convention.

## Steam Trade-History Semantics

`pricehistory` is used as a compact liquidity and execution proxy.

The aggregation step summarizes recent realized Steam trades into:

- count
- central tendency
- lower and upper quantiles
- observed min/max

This layer therefore combines:

- quote-level state
- transaction-level recent summary

That matters because a raw spread can look attractive while realized Steam trades tell a different story.

## Cookies and Authentication

Trade-history fetching requires Steam cookies.

Supported sources include:

- `STEAM_COOKIES` environment variable
- `local_steam_cookies.py`
- `local_secrets`

## Runtime / Operational Controls

Adjacent runtime-config support:

- `fetchers_runtime.json`
- legacy fallback: `fetcher_runtime.json`
- override path: `FETCHERS_RUNTIME_CONFIG` or `FETCHER_RUNTIME_CONFIG`

Reload behavior:

- config is re-read on mtime change
- long notebook runs can be throttled live without restart

Typical knobs:

- Steam delay
- CSFloat delay
- retry windows
- cooldowns for CSFloat keys
- Steam `429` retry wait

## Retry, Cooldown, and Throughput Behavior

Implemented behaviors include:

- indefinite retry on Steam `429`
- retry on Steam network errors
- CSFloat key rotation across two keys when available
- cooldown after CSFloat `429` or `403`
- retry on CSFloat network and `5xx`
- concurrent Steam and CSFloat acquisition with controlled worker count

This layer is designed for stable batch completion under API pressure, not for low-latency serving.

## Currency Handling

- Steam current ask can be fetched in USD or EUR
- Steam trade-history uses the same selected Steam currency context
- CSFloat values remain USD-native unless explicit EUR normalization is enabled
- when `PRICES_IN_EUR=True`, CSFloat values are converted via FX and the snapshot becomes EUR-consistent

## Storage and Output Convention

Typical output path:

- `../data_with_trades/`

Typical artifact:

- one CSV per screening run
- one row per item with both spreads and trade-history aggregates
- optional incremental append/resume via `fetch_all_prices_with_trades(..., out_csv=..., write_mode="create|merge")`

Resume semantics:

- `write_mode="create"` requires a new target path and never deletes an existing CSV
- `write_mode="merge"` reads existing `item` values from the target CSV, skips already-saved items, and appends only new completed rows
- a row is appended only after both Steam and CSFloat work for that item have finished; there is no partial-row write

This makes the output suitable for:

- ranking and filtering in notebooks
- feeding later pair-selection logic
- auditing market assumptions used in a run

## Where This Folder Fits In The Full Project

Use this layer when:

- base spreads are available but still too noisy
- you need recent realized Steam behavior before acting
- you want a more execution-aware shortlist before deeper float analysis

## Recommended Engineering Extension Points

- add new row-level enrichments after `get_scm_trade_stats(...)`
- add more trade-history summary statistics inside the aggregation layer
- capture explicit run metadata such as currency mode, cookie source, or config hash
- add parquet output alongside CSV if the pipeline grows
