# Alert Enrichment Lab

Small sandbox for replaying historical opportunities through the current alert stack:

1. Base Telegram alert text
2. Fit plot image
3. AI note from CSFloat latest sales + Gemini

This lab does not touch the live monitoring loop. It only replays saved opportunity rows.

## Fixtures

Fixtures live in `fixtures/manifest.json`.
Each fixture points at a historical `automation_runtime/telegram_queue/*.csv` snapshot and a row index.

## Usage

List fixtures:

```bash
/home/roma/cs-arbitrage/cs-skins-monitoring-bot/.venv/bin/python \
  labs/alert_enrichment_lab/run_candidate.py --list
```

Local dry-run for one candidate:

```bash
/home/roma/cs-arbitrage/cs-skins-monitoring-bot/.venv/bin/python \
  labs/alert_enrichment_lab/run_candidate.py --fixture-id poly_mag_fn
```

Force a live CSFloat fetch with no cache read/write:

```bash
/home/roma/cs-arbitrage/cs-skins-monitoring-bot/.venv/bin/python \
  labs/alert_enrichment_lab/run_candidate.py --fixture-id poly_mag_fn --live-no-cache
```

Fetch latest sales via the `digital_books` GitHub Actions runner:

```bash
/home/roma/cs-arbitrage/cs-skins-monitoring-bot/.venv/bin/python \
  labs/alert_enrichment_lab/run_candidate.py --fixture-id poly_mag_fn --github-live
```

Dry-run with manually saved latest sales:

```bash
/home/roma/cs-arbitrage/cs-skins-monitoring-bot/.venv/bin/python \
  labs/alert_enrichment_lab/run_candidate.py \
  --fixture-id poly_mag_fn \
  --latest-sales-json /abs/path/latest_sales.json
```

Send the three-step flow to Telegram:

```bash
/home/roma/cs-arbitrage/cs-skins-monitoring-bot/.venv/bin/python \
  labs/alert_enrichment_lab/run_candidate.py --fixture-id poly_mag_fn --send-telegram
```

Outputs are written under `runs/<timestamp>_<fixture-id>/`:

- `row.json`
- `base_alert.html`
- `fit_plot.png` when available
- `alert_enrichment/jobs/...` with latest sales, Gemini request/response, and final AI note
- `manual_latest_sales.json` when you injected saved sales for offline prompt work

## Notes

- The lab reuses the production prompt and enrichment code from `automation/alert_enrichment.py`.
- `--send-telegram` sends the base alert first, then the plot, then the AI note reply.
- `--live-no-cache` forces a fresh CSFloat latest-sales request and does not read/write the enrichment cache.
- `--github-live` routes the latest-sales fetch through the `digital_books` GitHub Actions runner.
- Latest sales still depend on current CSFloat reachability from the running machine/IP.
- If CSFloat is rate-limiting the VPS, use `--latest-sales-json` to keep iterating on the AI note with a manually saved sales snapshot.
