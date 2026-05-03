#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/server_common.sh"

start_log "smoke_tests"
acquire_lock "cs-skins-smoke-tests"
enter_repo
print_context

echo "[$(timestamp)] smoke: nightly dry-run"
"$PYTHON_BIN" automation/nightly/run_level1.py \
  --dry-run \
  --run-risk \
  --skip-base \
  --skip-model-backfill

echo "[$(timestamp)] smoke: monitoring dry-run"
"$PYTHON_BIN" -B automation/monitoring/run_cycle.py \
  --dry-run \
  --ignore-schedule \
  --no-telegram \
  --no-git

echo "[$(timestamp)] smoke: forced health alert dry-run"
"$PYTHON_BIN" -B automation/health/check_steam_cookies.py \
  --force-failure-for-test \
  --dry-run-telegram \
  --exit-zero-on-failure

echo "[$(timestamp)] smoke tests completed"
