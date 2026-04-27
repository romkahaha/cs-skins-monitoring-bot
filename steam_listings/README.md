# `steam_listings`

Listing-level Steam extraction from `/render/` with normalized CSV output and optional alignment to CSFloat-derived signals.

This folder is the Steam-side listing collector in the repository. It is used when a single Steam ask is too coarse and the analysis needs the actual distribution of Steam listings for an item.

## Technical Scope

This folder works at listing granularity.

For each `market_hash_name`, it can collect up to `N` Steam listings and normalize them into tabular rows suitable for CSV and notebook analysis.

It is conceptually parallel to `skin_homog/`, but the source is Steam rather than CSFloat.

## Source System

- Steam `market/listings/.../render/`

The script reads:

- listing-level seller net and buyer-visible price fields
- asset metadata
- float from `asset_properties`
- paint seed when exposed
- total listing count

## Main Entry Point

- [`steam_scm_listings.py`](./steam_scm_listings.py)

Important public-style functions:

- `fetch_steam_scm_top_listings(...)`
- `run_batch_to_csv(...)`
- `load_items_from_module(...)`

## CLI Modes

Main usage patterns:

- single item mode
- batch mode with `--batch`
- custom item list path via `--items-py`
- per-item page size with `--limit`
- per-item collection cap with `--max-listings`

## Price Semantics

Important detail:

- `converted_price` is seller net
- `converted_fee` is Steam fee
- `ask` is buyer-visible total price, derived as `converted_price + converted_fee`
- `ask_seller_net` is seller net only

The script preserves both because they serve different analytical purposes.

## Listing-Level Schema

Typical normalized output fields:

- `market_hash_name`
- `listing_id`
- `asset_id`
- `ask`
- `ask_seller_net`
- `float_value`
- `paint_seed`
- `converted_price`
- `converted_fee`
- `converted_currencyid`
- `scm_total_listings`
- `asset_properties_json` when enabled

Semantics:

- `ask` matches buyer-visible Steam price
- `scm_total_listings` is item-level listing depth from the Steam response
- `float_value` is parsed from `asset_properties`
- `paint_seed` is extracted heuristically from Steam asset metadata when available

## Pagination Model

Steam `/render/` returns up to 100 listings per request.

`fetch_steam_scm_top_listings(...)` supports:

- configurable per-request chunk size
- configurable total cap per item
- multiple requests using `start=0,100,200,...`
- deduplication by `listing_id`
- page-to-page delay control

## Retry and Stability Behavior

Implemented behaviors include:

- multiple attempts per page
- retry on request failures
- retry on `success=false`
- support for partial fetch continuation
- configurable pauses between pages
- configurable pauses between skins

Batch outputs can therefore be partially complete rather than fully failing, so fetch metadata matters.

## Runtime Configuration

Adjacent runtime config:

- `steam_scm_runtime.json`
- override path via `STEAM_SCM_RUNTIME_CONFIG`

This controls:

- page size
- retry attempts
- retry sleep windows
- delays between pages
- delays between skins
- batch logging
- default item-list source
- whether to include `asset_properties_json`

The config is reloaded dynamically on mtime change.

## Item Universe Resolution

Default item source:

- `../lists/screening_sub.py`

The script can also load:

- a custom Python file with `ITEMS`
- a Python module path
- a runtime-config-defined source

## Batch Output

Default batch output target commonly resolves to:

- `steam_listings/data/scm_listings_batch.csv`

`run_batch_to_csv(...)` writes one row per listing, not one row per item.

This file is appropriate for:

- listing-level modeling
- Steam-side depth analysis
- joining with float-based models
- downstream enrichment in notebooks

## Supporting Files

Related files in this folder:

- `steam_scm_batch.ipynb`
- `enrich_listings_from_batch.ipynb`
- `fit_rel_models_from_data_skins_big.py`
- `data/float_fit_rel_curves.json`

These support:

- float-vs-price fitting
- float->relative deviation fitting (`predicted/base - 1`) with smooth/segmented/hybrid curves
- enrichment of Steam listing rows with CSFloat-side predictions
- comparison of Steam listing distribution to CSFloat-derived expected value

## Where This Fits In The Full Project

Use this folder when the Steam side needs to be represented as a listing dataset, not as a single ask.

## Recommended Engineering Extension Points

- add new listing-level fields in `parse_render_payload(...)`
- add alternative export formats beside CSV
- add batch metadata output alongside listing rows
- add schema/version tagging for downstream jobs
