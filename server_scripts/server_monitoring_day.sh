#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/server_common.sh"

MONITORING_MAX_RUNTIME_MINUTES="${MONITORING_MAX_RUNTIME_MINUTES:-895}"

start_log "monitoring_day"
acquire_lock "cs-skins-monitoring-day"
load_secrets
require_env STEAM_COOKIES TELEGRAM_BOT_TOKEN TELEGRAM_CHAT_ID
enter_repo
print_context

echo "[$(timestamp)] starting daytime monitoring for ${MONITORING_MAX_RUNTIME_MINUTES} minutes"
"$PYTHON_BIN" -B automation/monitoring/run_cycle.py \
  --send-telegram \
  --ignore-schedule \
  --no-git \
  --max-runtime-minutes "$MONITORING_MAX_RUNTIME_MINUTES"
echo "[$(timestamp)] daytime monitoring completed"
