#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/server_common.sh"

start_log "nightly"
acquire_lock "cs-skins-nightly"
load_secrets
require_env STEAM_COOKIES CSFLOAT_API_KEY
enter_repo
print_context

echo "[$(timestamp)] starting nightly rebuild"
"$PYTHON_BIN" automation/nightly/run_level1.py --run-risk
echo "[$(timestamp)] nightly rebuild completed"
