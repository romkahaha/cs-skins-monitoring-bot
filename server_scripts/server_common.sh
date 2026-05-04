#!/usr/bin/env bash
set -Eeuo pipefail

BOT_ROOT="${CS_SKINS_BOT_ROOT:-/home/roma/cs-arbitrage/cs-skins-monitoring-bot}"
CS_ARBITRAGE_ROOT="${CS_ARBITRAGE_ROOT:-/home/roma/cs-arbitrage}"
SECRETS_FILE="${CS_ARBITRAGE_SECRETS:-$CS_ARBITRAGE_ROOT/secrets.env}"
LOG_DIR="${CS_SKINS_LOG_DIR:-$CS_ARBITRAGE_ROOT/logs/cs-skins-monitoring-bot}"
LOCK_DIR="${CS_SKINS_LOCK_DIR:-$CS_ARBITRAGE_ROOT/locks}"
PYTHON_BIN="${CS_SKINS_PYTHON:-$BOT_ROOT/.venv/bin/python}"

umask 077
mkdir -p "$LOG_DIR" "$LOCK_DIR"

timestamp() {
  date +"%Y-%m-%dT%H:%M:%S%z"
}

log_file_for() {
  local name="$1"
  date +"$LOG_DIR/${name}_%Y%m%d_%H%M%S.log"
}

start_log() {
  local name="$1"
  local file
  file="$(log_file_for "$name")"
  exec >>"$file" 2>&1
  echo "[$(timestamp)] log=$file"
}

load_secrets() {
  if [[ ! -f "$SECRETS_FILE" ]]; then
    echo "[$(timestamp)] missing secrets file: $SECRETS_FILE" >&2
    return 2
  fi
  set -a
  # shellcheck source=/home/roma/cs-arbitrage/secrets.env
  source "$SECRETS_FILE"
  set +a
}

require_env() {
  local missing=0
  local key
  for key in "$@"; do
    if [[ -z "${!key:-}" ]]; then
      echo "[$(timestamp)] missing required environment variable: $key" >&2
      missing=1
    fi
  done
  if [[ "$missing" -ne 0 ]]; then
    return 2
  fi
}

enter_repo() {
  if [[ ! -x "$PYTHON_BIN" ]]; then
    echo "[$(timestamp)] missing python executable: $PYTHON_BIN" >&2
    return 2
  fi
  cd "$BOT_ROOT"
}

acquire_lock() {
  local name="$1"
  local lock_file="$LOCK_DIR/$name.lock"
  exec 9>"$lock_file"
  if ! flock -n 9; then
    echo "[$(timestamp)] another $name run is already active; exiting"
    exit 0
  fi
  echo "[$(timestamp)] acquired lock: $lock_file"
}

acquire_lock_wait() {
  local name="$1"
  local timeout_sec="${2:-0}"
  local lock_file="$LOCK_DIR/$name.lock"
  exec 9>"$lock_file"
  if flock -w "$timeout_sec" 9; then
    echo "[$(timestamp)] acquired lock: $lock_file"
    return 0
  fi
  echo "[$(timestamp)] could not acquire lock within ${timeout_sec}s: $lock_file" >&2
  exit 0
}

print_context() {
  echo "[$(timestamp)] bot_root=$BOT_ROOT"
  echo "[$(timestamp)] python=$PYTHON_BIN"
  echo "[$(timestamp)] secrets_file=$SECRETS_FILE"
}
