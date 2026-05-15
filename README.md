# CS Skins Monitoring Bot

End-to-end pipeline for:

- nightly risk rebuild
- model backfill/refit
- monitor list generation
- daytime Steam monitoring
- Telegram opportunity alerts
- GitHub Actions failover when VPS monitoring is unavailable or sleeping on Steam `429`

This README is meant as the operational map of the whole project, not just one submodule.

## High-level flow

There are two main modes:

1. `Nightly`
2. `Daytime monitoring`

There is also a third piece:

3. `Failover monitoring on GitHub`

### Nightly

Nightly starts on the VPS and does the heavy rebuild work:

1. checkpoint current monitoring runtime
2. optionally request GitHub monitoring failover for the night window
3. rebuild local risk inputs on VPS:
   - `build_risk_metrics.py`
   - `build_risk_candidates.py`
   - `build_model_backfill_queue.py`
4. push `risk_ready` inputs to `main`
5. wait for GitHub worker
6. GitHub worker runs:
   - model backfill
   - model refit
   - model backfill queue again
   - monitor list
   - base snapshot
7. VPS pulls the fresh artifacts back

Important: the final `monitor_list_latest` and `base_snapshot_latest` are produced by the GitHub worker, not by the local VPS nightly step.

### Daytime monitoring

Day monitoring runs on the VPS and loops over `monitor_list_latest.py` in batches:

1. load current monitor list, state, base snapshot, risk CSV, fit JSON
2. fetch fresh Steam listings for one batch
3. enrich listings with:
   - base snapshot
   - risk metrics
   - fit curves
4. compute `opportunity_pass`
5. send Telegram alerts for new listings
6. save runtime state
7. continue until runtime budget is exhausted or the end-of-day guard is reached

### Failover

Failover is a separate monitoring copy in a sibling repo:

- main repo: `cs-skins-monitoring-bot`
- failover repo: `digital_books`

Failover is used in two cases:

1. daytime monitoring hits Steam `429`
2. nightly starts and we want monitoring continuity while VPS is busy rebuilding risk

The main repo syncs code + runtime bundle into `digital_books`, pushes a request file, and GitHub Actions there runs temporary monitoring on GitHub-hosted runners.

## Repo layout

Top-level important directories/files:

- `automation/`
  - orchestration and pipeline code
- `automation/configs/`
  - JSON configs for nightly and monitoring
- `automation/nightly/`
  - nightly build scripts
- `automation/monitoring/`
  - batch/cycle monitoring scripts
- `automation_runtime/`
  - generated runtime artifacts used by both nightly and monitoring
- `server_scripts/`
  - server entrypoints used by cron and GitHub worker
- `skin_homog/`
  - research/modeling data + risk preprocess logic
- `steam_listings/`
  - fit curves and Steam-related collectors

## Main entrypoints

### Server scripts

- `server_scripts/server_nightly.sh`
  - main nightly orchestrator on VPS
- `server_scripts/server_monitoring_day.sh`
  - main daytime monitoring launcher on VPS
- `server_scripts/github_csfloat_worker.sh`
  - GitHub Actions worker stage runner
- `server_scripts/server_check_steam_cookies.sh`
  - hourly cookie check

### Nightly Python scripts

- `automation/nightly/build_risk_metrics.py`
- `automation/nightly/build_risk_candidates.py`
- `automation/nightly/build_model_backfill_queue.py`
- `automation/nightly/run_model_backfill.py`
- `automation/nightly/run_model_refit.py`
- `automation/nightly/build_monitor_list.py`
- `automation/nightly/build_base_snapshot.py`

### Monitoring Python scripts

- `automation/monitoring/run_cycle.py`
  - outer loop, handles sleep, runtime budget, failover triggers
- `automation/monitoring/run_batch.py`
  - one batch fetch + enrich + opportunities + Telegram
- `automation/monitoring/send_telegram_alerts.py`
  - standalone Telegram sender

### Failover Python script

- `automation/failover_monitoring.py`
  - sync bundle to `digital_books`
  - create `standby`, `request`, `clear`
  - run failover request on GitHub side
  - sync state/dedupe back

## Runtime artifacts

Most important files in `automation_runtime/`:

- `risk_metrics_latest.csv`
  - full risk universe
- `risk_candidates_latest.csv`
  - items that passed the nightly risk filter
- `model_coverage_latest.csv`
  - summary/fit readiness table
- `model_backfill_queue_latest.csv`
  - items needing more model data
- `monitor_list_latest.csv`
  - final monitoring audit CSV
- `monitor_list_latest.py`
  - final `ITEMS = [...]` list used by live monitoring
- `base_snapshot_latest.csv`
  - base prices for enrichment
- `steam_listings_latest.csv`
  - last batch of fresh Steam listings
- `enriched_listings_latest.csv`
  - listings enriched with risk/base/model outputs
- `opportunities_latest.csv`
  - rows that passed `opportunity_filter`
- `opportunities_report_latest.csv`
  - rule-by-rule pass/fail report for the latest batch
- `state.json`
  - monitoring pointer + run status
- `state_telegram_alerts.json`
  - dedupe state for sent alerts
- `server_pipeline_status_latest.json`
  - nightly pipeline status shared with GitHub worker

## Scheduling

Server cron lives in `server_scripts/crontab.cs-skins-monitoring`.

Current schedule:

- cookie check: hourly
- nightly: `23:00 Europe/Berlin`
- daytime monitoring launcher: `08:00 Europe/Berlin`

Important nuance:

- `automation/configs/monitoring.json` has schedule metadata, but the real server timing is enforced by `server_monitoring_day.sh`
- `server_monitoring_day.sh` computes the runtime budget to stop before the nightly window

## Current config behavior

### Nightly filters (`automation/configs/nightly.json`)

Current risk filter:

- `steam_daily_ret_7d >= -0.05`
- `steam_daily_downside_14d_pct <= 0.15`
- `steam_sales_7d_n >= 50`
- `steam_sales_7d_tail_ratio >= 0.8`
- `n_listings >= 20`

Current high-CV filter:

- `cv_n_listings >= 3`
- `pred_cv > 0.05`
- `pred_range_over_mean > -1`
- `missing_cv_policy = assume_high`

Current model readiness gate:

- `summary_n_listings >= 3`
- `fit_n_clean >= 5`
- `require_model_ready_for_monitor = true`

Final nightly monitor-list logic:

`monitor_pass = risk_pass AND high_cv_pass AND model_ready`

### Monitoring opportunity filters (`automation/configs/monitoring.json`)

Current `opportunity_filter`:

- `steam_sales_7d_n >= 50`
- `steam_sales_7d_downside_risk% <= 10.0`
- `steam_sales_7d_tail_ratio >= 0.8`
- `steam_daily_downside_14d_pct <= 0.15`
- `continuity_ratio <= 3.5`
- `spread_hybrid_disc <= 0.12`

Current `alerts` filter:

- `spread_hybrid_disc <= 0.12`
- `ask_min = null`
- `ask_max = null`
- `steam_sales_7d_n >= 50`
- `steam_sales_7d_downside_risk% <= 10.0`
- `steam_sales_7d_tail_ratio >= 0.8`
- `steam_daily_downside_14d_pct <= 0.15`
- `continuity_ratio <= 3.5`
- `exclude_any = ["Fade", "Case Hardened", "Heat Treated"]`

Telegram dedupe:

- keyed primarily by `listing_id`
- cooldown: `12 hours`

## Failover design

### Why there are two repos

`digital_books` is not a nested repo inside the main repo. It is a sibling repo used as a GitHub Actions execution surface.

Layout:

```text
/home/roma/cs-arbitrage/
  cs-skins-monitoring-bot/
  digital_books/
```

### Failover modes

- `standby`
  - update bundle only
  - GitHub workflow wakes up, sees no active request, exits
- `request`
  - GitHub should start monitoring temporarily
- `clear`
  - active request is over; GitHub should stop launching new failover runs

### What gets synced

Failover sync copies, at minimum:

- `automation/`
- `steam_listings/`
- `requirements.txt`
- `automation_runtime/monitor_list_latest.py`
- `automation_runtime/monitor_list_latest.csv`
- `automation_runtime/base_snapshot_latest.csv`
- `automation_runtime/risk_metrics_latest.csv`
- `automation_runtime/state.json`
- `automation_runtime/state_telegram_alerts.json`
- `steam_listings/data/float_fit_rel_curves.json`
- optionally precomputed fit plots

### Important failover rules

- daytime `429` failover lease defaults to `cycle.recoverable_error_sleep_sec`
- if a `429` happens shortly before nightly, the daytime failover lease is clipped to the remaining time until `23:00`
- nightly starts its own failover request for `nightly_lease_seconds`
- failover sends Telegram inline so dedupe state is definitely persisted before sync-back

### Current configured failover

From `monitoring.json`:

- `enabled = true`
- `push_on_cycle_start = true`
- `request_on_rate_limit = true`
- `lease_seconds = 5400`
- `request_on_nightly_start = true`
- `nightly_lease_seconds = 19800`

## Important implementation details and gotchas

### 1. Nightly is split across VPS and GitHub

This is the single most important operational nuance.

Local VPS nightly does not produce the final monitor list alone.

The pipeline is:

1. local VPS builds risk-related files
2. pushes `risk_ready`
3. GitHub worker runs `build_monitor_list.py` and `build_base_snapshot.py`
4. VPS pulls artifacts back

If you change `nightly.json` only locally and do not get those changes into the GitHub worker path, the final monitor list can still reflect older settings.

### 2. Failover and nightly use different execution surfaces

- night rebuild: local VPS + GitHub worker
- failover monitoring: GitHub Actions in `digital_books`
- daytime monitoring: local VPS

### 3. Partial `429` handling exists

Failover is not only triggered on total batch failure.

`run_cycle.py` also reacts to partial item-level `429` patterns inside otherwise successful batches if enough item errors match the rate-limit patterns.

### 4. Dirty worktree protection in nightly

`server_nightly.sh` stashes local changes before `git pull` and restores them after, so nightly does not die immediately on a dirty working tree.

### 5. Coverage/model-ready can lag a changed filter universe

If you dry-run `build_monitor_list.py` against a changed risk filter but still rely on an old `model_coverage_latest.csv`, the result can understate the true next-night output.

The real next-night result is only correct after:

- `build_risk_candidates.py`
- `build_model_backfill_queue.py`
- `build_monitor_list.py`

have all run for that same filter set.

## Common operational questions

### Is the monitoring alive?

Check:

```bash
pgrep -af 'server_monitoring_day.sh|automation/monitoring/run_cycle.py|automation/monitoring/run_batch.py'
```

Latest log:

```bash
ls -1t /home/roma/cs-arbitrage/logs/cs-skins-monitoring-bot/monitoring_day_*.log | head -n 1
```

### Did nightly finish?

Check:

```bash
sed -n '1,220p' automation_runtime/server_pipeline_status_latest.json
```

Latest nightly log:

```bash
ls -1t /home/roma/cs-arbitrage/logs/cs-skins-monitoring-bot/nightly_*.log | head -n 1
```

### Why are there no Telegram alerts?

Typical reasons:

1. `opportunity rows = 0`
2. `alerts skipped > 0` because dedupe already sent those listing IDs
3. listings are being collected but the batch produces no rows that pass current risk/opportunity filters

### Why can good listings disappear before alerting?

Main practical reason: revisit interval.

If the monitor list gets larger, but the loop speed stays similar, the scan returns to hot/liquid items too slowly and good listings are bought before the next pass.

## Current pain points / future design ideas

The main product problem is freshness, not just coverage.

Natural next steps:

1. tier the monitor list by liquidity / actionability
2. scan hot tiers more often than cold tiers
3. maybe use shallower page depth for hot tiers
4. reserve failover or a second lane for the hottest subset instead of the whole universe

## Quick command checklist

### Dry-run nightly monitor list

```bash
cd /home/roma/cs-arbitrage/cs-skins-monitoring-bot
.venv/bin/python -B automation/nightly/build_monitor_list.py --config automation/configs/nightly.json
```

### Dry-run one monitoring batch

```bash
cd /home/roma/cs-arbitrage/cs-skins-monitoring-bot
.venv/bin/python -B automation/monitoring/run_batch.py --config automation/configs/monitoring.json --batch-size 5 --ignore-schedule --send-telegram
```

### Request failover manually

```bash
cd /home/roma/cs-arbitrage/cs-skins-monitoring-bot
.venv/bin/python automation/failover_monitoring.py sync --config automation/configs/monitoring.json --mode request --lease-seconds 5400 --reason "manual test"
```

### Standby sync manually

```bash
cd /home/roma/cs-arbitrage/cs-skins-monitoring-bot
.venv/bin/python automation/failover_monitoring.py sync --config automation/configs/monitoring.json --mode standby
```

## Related docs

- [skin_homog/README.md](/home/roma/cs-arbitrage/cs-skins-monitoring-bot/skin_homog/README.md)
- [steam_listings/README.md](/home/roma/cs-arbitrage/cs-skins-monitoring-bot/steam_listings/README.md)
- [lists/README.md](/home/roma/cs-arbitrage/cs-skins-monitoring-bot/lists/README.md)
- [server_nightly.sh](/home/roma/cs-arbitrage/cs-skins-monitoring-bot/server_scripts/server_nightly.sh)
- [server_monitoring_day.sh](/home/roma/cs-arbitrage/cs-skins-monitoring-bot/server_scripts/server_monitoring_day.sh)
- [github_csfloat_worker.sh](/home/roma/cs-arbitrage/cs-skins-monitoring-bot/server_scripts/github_csfloat_worker.sh)

