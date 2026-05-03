#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/server_common.sh"

start_log "steam_cookies"
acquire_lock "cs-skins-steam-cookies"
load_secrets
require_env STEAM_COOKIES TELEGRAM_BOT_TOKEN TELEGRAM_CHAT_ID
enter_repo
print_context

echo "[$(timestamp)] starting Steam cookies health check"
"$PYTHON_BIN" -B automation/health/check_steam_cookies.py
echo "[$(timestamp)] Steam cookies health check completed"
