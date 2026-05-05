#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/server_common.sh"

MONITORING_MAX_RUNTIME_MINUTES="${MONITORING_MAX_RUNTIME_MINUTES:-895}"
MONITORING_LOCK_WAIT_MINUTES="${MONITORING_LOCK_WAIT_MINUTES:-360}"
MONITORING_END_HOUR="${MONITORING_END_HOUR:-23}"
MONITORING_END_MINUTE="${MONITORING_END_MINUTE:-0}"
MONITORING_END_GUARD_MINUTES="${MONITORING_END_GUARD_MINUTES:-5}"

compute_runtime_budget_minutes() {
  local now_ts end_ts remaining_minutes guarded_minutes

  now_ts="$(date +%s)"
  end_ts="$(date -d "today ${MONITORING_END_HOUR}:${MONITORING_END_MINUTE}" +%s)"
  remaining_minutes=$(((end_ts - now_ts) / 60))
  guarded_minutes=$((remaining_minutes - MONITORING_END_GUARD_MINUTES))

  if (( guarded_minutes <= 0 )); then
    echo 0
    return 0
  fi

  if (( guarded_minutes < MONITORING_MAX_RUNTIME_MINUTES )); then
    echo "$guarded_minutes"
  else
    echo "$MONITORING_MAX_RUNTIME_MINUTES"
  fi
}

start_log "monitoring_day"
echo "[$(timestamp)] waiting up to ${MONITORING_LOCK_WAIT_MINUTES} minutes for nightly/main lock"
acquire_lock_wait "cs-skins-main-pipeline" "$((MONITORING_LOCK_WAIT_MINUTES * 60))"
load_secrets
require_env STEAM_COOKIES TELEGRAM_BOT_TOKEN TELEGRAM_CHAT_ID
enter_repo
print_context

RUNTIME_BUDGET_MINUTES="$(compute_runtime_budget_minutes)"
echo "[$(timestamp)] monitoring runtime budget=${RUNTIME_BUDGET_MINUTES}m cap=${MONITORING_MAX_RUNTIME_MINUTES}m end=${MONITORING_END_HOUR}:$(printf '%02d' "$MONITORING_END_MINUTE") guard=${MONITORING_END_GUARD_MINUTES}m"
if (( RUNTIME_BUDGET_MINUTES <= 0 )); then
  echo "[$(timestamp)] no daytime runtime budget remains; exiting without starting monitoring"
  exit 0
fi

echo "[$(timestamp)] starting daytime monitoring for ${RUNTIME_BUDGET_MINUTES} minutes"
"$PYTHON_BIN" -B automation/monitoring/run_cycle.py \
  --send-telegram \
  --ignore-schedule \
  --no-git \
  --max-runtime-minutes "$RUNTIME_BUDGET_MINUTES"
echo "[$(timestamp)] daytime monitoring completed"
