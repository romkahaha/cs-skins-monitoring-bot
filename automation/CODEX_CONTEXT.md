# CS Skins Monitoring Automation Context

Этот файл нужен, чтобы продолжить работу в новом репозитории/новом чате Codex.

## Цель

Построить scheduled monitoring system для CS skins opportunities между Steam и внешней площадкой:

1. Ночью обновлять risk/filter/base слой.
2. Днем каждые 10-30 минут сканировать Steam listings батчами.
3. Считать fair price / edge / risk-adjusted opportunity.
4. Отправлять хорошие candidates в Telegram.

Это не HFT и не auto-trading bot. Это мониторинг, который заменяет ручной запуск скринера.

## Что скопировать в новый repo

Минимально скопировать:

```text
automation/
automation_runtime/
steam_listings/
base_screening_with_trades/
skin_homog/screener_preprocess_risk/risk_metrics.csv
skin_homog/data_skins_big/_summary.csv
skin_homog/data_skins_big/base.csv
skin_homog/data_skins_big/predicted.csv
steam_listings/data/float_fit_rel_curves.json
```

Возможно также нужны:

```text
lists/
skin_homog/screener_preprocess/preprocess_metrics.csv
skin_homog/screener_preprocess_risk/risk_preprocess.py
```

Если в новом repo сначала будет safe-mode без полного risk rebuild, достаточно существующего `risk_metrics.csv`.

## Текущая структура automation

```text
automation/
  configs/
    nightly.json
    monitoring.json
  nightly/
    build_monitor_list.py
    build_base_snapshot.py
    run_level1.py
  monitoring/
    run_batch.py
    build_opportunities.py
    send_telegram_alerts.py
  config.py
  listing_enrichment.py
  risk_filters.py
  state.py
  telegram_alerts.py
  CODEX_CONTEXT.md

automation_runtime/
  monitor_list_latest.csv
  monitor_list_latest.py
  base_snapshot_latest.csv
  steam_listings_latest.csv
  enriched_listings_latest.csv
  opportunities_latest.csv
  opportunities_report_latest.csv
  state.json
```

## Nightly level

Config:

```text
automation/configs/nightly.json
```

Runner:

```powershell
python automation\nightly\run_level1.py
```

Dry run:

```powershell
python automation\nightly\run_level1.py --skip-risk --skip-base --dry-run
```

Heavy risk rebuild is disabled by default:

```json
"risk_rebuild": {
  "enabled": false
}
```

Force heavy risk rebuild:

```powershell
python automation\nightly\run_level1.py --run-risk
```

Current intended nightly flow:

```text
existing or rebuilt risk_metrics.csv
  -> build monitor_list_latest.csv/.py from risk + high-CV filters
  -> build base_snapshot_latest.csv
```

`base_snapshot` has 429/rate-limit policy:

```json
"rate_limit_pause_sec": 900.0,
"rate_limit_stair_step_sec": 60.0,
"rate_limit_max_retries": 5
```

Meaning: on 429 wait 15 min, then retry same item. If 429 repeats, wait 16 min, then 17, etc. After 5 retries, leave item as error and continue.

The base snapshot runner writes partial CSV after every item, so progress is not lost on a long pause/crash.

## Monitoring level

Config:

```text
automation/configs/monitoring.json
```

Runner:

```powershell
python automation\monitoring\run_batch.py --batch-size 5 --telegram-dry-run
```

Real Telegram:

```powershell
python automation\monitoring\run_batch.py --batch-size 5 --send-telegram
```

Current monitoring flow:

```text
monitor_list_latest.py
  -> select batch using state.json pointer
  -> collect up to 200 Steam listings per item
  -> join base_snapshot_latest.csv
  -> join risk_metrics.csv
  -> load float_fit_rel_curves.json
  -> compute predictions/spreads/opportunity flags
  -> write enriched/opportunities CSV
  -> filter alerts
  -> send Telegram if enabled
  -> update state.json
```

The monitoring process is one-shot. It does not sleep between runs. Pause between runs belongs to GitHub Actions/cron/scheduler.

## Schedule

In `monitoring.json`:

```json
"schedule": {
  "enabled": true,
  "active_from": "08:00",
  "active_to": "23:00",
  "timezone": "Europe/Prague",
  "interval_minutes": 10,
  "github_actions_cron_utc": "*/10 6-21 * * *",
  "enforce_active_window": false
}
```

`enforce_active_window: false` means the runner prints whether it is inside the window but does not skip. For production, consider `true`.

## Steam SCM config

In `monitoring.json`:

```json
"steam_scm": {
  "listings_per_request": 100,
  "max_listings_per_item": 200,
  "request_timeout_sec": 45.0,
  "retry_attempts": 3,
  "retry_sleep_min_sec": 2.0,
  "retry_sleep_max_sec": 5.0,
  "delay_between_skins_min_sec": 10.0,
  "delay_between_skins_max_sec": 20.0,
  "delay_between_render_pages_min_sec": 5.0,
  "delay_between_render_pages_max_sec": 10.0,
  "batch_log_progress": 1
}
```

This controls Steam Community Market `/render/` listing fetches.

`max_listings_per_item: 200` means up to two Steam pages per item: `start=0` and `start=100`.

## Opportunity vs Alerts

`opportunity_filter` controls what goes into `opportunities_latest.csv`.

`alerts` controls what actually goes to Telegram.

This separation is intentional: keep the CSV broad for audit/debug, make Telegram stricter.

Current alerts block:

```json
"alerts": {
  "enabled": true,
  "spread_hybrid_disc_max": 0.17,
  "ask_min": null,
  "ask_max": null,
  "steam_sales_7d_n_min": 50,
  "steam_sales_7d_downside_risk_max": 10.0,
  "steam_sales_7d_tail_ratio_min": 0.9,
  "steam_daily_downside_14d_pct_max": 0.12,
  "continuity_ratio_max": 3.5,
  "exclude_any": []
}
```

Possible stricter production example:

```json
"spread_hybrid_disc_max": 0.10,
"ask_min": 5,
"ask_max": 200
```

## Telegram

Telegram sender uses env/secrets:

```text
TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID
```

Alternative env names supported:

```text
TG_BOT_TOKEN
TG_CHAT_ID
TELEGRAM_CHANNEL
```

Do not hardcode secrets in repo.

Telegram messages are HTML formatted and include:

- item name
- Steam market link
- ask
- discounted fair value
- model spread
- fair/ask gap
- float
- listing id
- risk metrics with colored emoji bands

## State

State lives in:

```text
automation_runtime/state.json
```

It stores:

```text
batch_pointer
items_signature
last_run_at_utc
last_status
last_successful_monitoring_at_utc
last_failed_monitoring_at_utc
consecutive_errors
last_listing_errors
last_listing_error_count
last_listing_rows
last_enriched_rows
last_opportunities_rows
last_alert_stats
sent_alerts
```

`sent_alerts` dedupes/cooldowns already sent listing alerts.

Important: if using GitHub Actions, `state.json` must persist between runs. Easiest temporary approach is to commit `automation_runtime/` back to the automation repo after each workflow run.

## Current tested commands

Dry checks passed:

```powershell
python -B automation\nightly\run_level1.py --skip-risk --skip-base --dry-run
python -B automation\monitoring\send_telegram_alerts.py --dry-run
python -B -c "import automation.telegram_alerts, automation.monitoring.run_batch, automation.monitoring.send_telegram_alerts, automation.state, automation.config; print('imports ok')"
```

Live monitoring smoke test passed:

```powershell
python automation\monitoring\run_batch.py --batch-size 2 --telegram-dry-run
```

Last observed smoke result:

```text
batch start pointer: 8
items:
  Glock-18 | Steel Disruption (Factory New)
  M4A4 | The Emperor (Field-Tested)
Steam listings rows: 135
listing errors: 0
enriched rows: 135
opportunity rows: 0
telegram loaded/filtered/considered/sent/skipped: 0/0/0/0/0
next batch pointer: 10
```

## Important caveats

1. `automation_runtime/monitor_list_latest.py` currently may be a compact/manual test list with many commented items. Running nightly monitor-list builder regenerates the full list and overwrites this.

2. GitHub Actions starts from fresh checkout. Runtime files do not persist unless we commit them back, use cache/artifacts, or external storage.

3. Separate automation repo is recommended, so runtime commits do not pollute the research repo.

4. Steam cookies are unresolved. Steam listings seem to work without cookies in local smoke tests. Full risk rebuild may require cookies or be more fragile. If needed, use GitHub Secrets such as `STEAM_COOKIES`.

5. Current `telegram.enabled` is false in config. Use CLI `--send-telegram` or set config to true for real sends.

## Suggested next steps in new repo

1. Install/check dependencies and run import smoke.
2. Run monitoring smoke with `--telegram-dry-run`.
3. Decide how to persist `automation_runtime/state.json` in GitHub Actions.
4. Add `.github/workflows/monitoring.yml`.
5. Add `.github/workflows/nightly-level1.yml`.
6. Start workflows in safe mode:
   - monitoring with dry-run or Telegram disabled;
   - nightly without `--run-risk`.
7. Add GitHub Secrets:
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
   - maybe `STEAM_COOKIES`.
8. Turn on real Telegram only after dry-run workflow is stable.

