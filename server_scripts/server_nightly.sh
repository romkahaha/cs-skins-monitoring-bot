#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/server_common.sh"

start_log "nightly"
acquire_lock "cs-skins-main-pipeline"
load_secrets
require_env STEAM_COOKIES
enter_repo
print_context

PIPELINE_WAIT_TIMEOUT_MINUTES="${PIPELINE_WAIT_TIMEOUT_MINUTES:-180}"
PIPELINE_POLL_SECONDS="${PIPELINE_POLL_SECONDS:-60}"
PIPELINE_STATUS_FILE="automation_runtime/server_pipeline_status_latest.json"
PIPELINE_RUN_ID="$(date -u +"%Y%m%dT%H%M%SZ")-vps-$$"
PIPELINE_STARTED_AT_UTC="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"

write_vps_status() {
  local status="$1"
  local message="$2"
  "$PYTHON_BIN" - "$PIPELINE_STATUS_FILE" "$PIPELINE_RUN_ID" "$status" "$PIPELINE_STARTED_AT_UTC" "$message" <<'PY'
import json
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path

path = Path(sys.argv[1])
payload = {
    "run_id": sys.argv[2],
    "status": sys.argv[3],
    "source": "vps",
    "started_at_utc": sys.argv[4],
    "updated_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    "message": sys.argv[5],
    "host": socket.gethostname(),
}
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY
}

remote_pipeline_status() {
  git show "origin/main:$PIPELINE_STATUS_FILE" 2>/dev/null | "$PYTHON_BIN" - "$PIPELINE_RUN_ID" <<'PY'
import json
import sys

run_id = sys.argv[1]
try:
    payload = json.load(sys.stdin)
except Exception:
    raise SystemExit(0)
if payload.get("run_id") == run_id:
    print(payload.get("status", ""))
PY
}

checkpoint_local_monitoring_runtime() {
  git add \
    automation_runtime/state.json \
    automation_runtime/steam_listings_latest.csv \
    automation_runtime/enriched_listings_latest.csv \
    automation_runtime/opportunities_latest.csv \
    automation_runtime/opportunities_report_latest.csv
  if git diff --cached --quiet; then
    git reset --quiet
    return 0
  fi
  git commit -m "Capture server monitoring runtime [skip ci]"
}

echo "[$(timestamp)] starting server-orchestrated nightly run_id=$PIPELINE_RUN_ID"

checkpoint_local_monitoring_runtime
git pull --rebase origin main
git push origin main

echo "[$(timestamp)] rebuilding VPS risk inputs"
"$PYTHON_BIN" -B automation/nightly/build_risk_metrics.py --create
"$PYTHON_BIN" -B automation/nightly/build_risk_candidates.py
"$PYTHON_BIN" -B automation/nightly/build_model_backfill_queue.py

write_vps_status "risk_ready" "VPS risk and model backfill queue are ready; GitHub CSFloat worker should start from this push."

git add \
  automation_runtime/risk_metrics_latest.csv \
  automation_runtime/risk_progress_latest.log \
  automation_runtime/risk_runtime_latest.json \
  automation_runtime/risk_candidates_latest.csv \
  automation_runtime/model_coverage_latest.csv \
  automation_runtime/model_backfill_queue_latest.csv \
  automation_runtime/model_backfill_queue_latest.py \
  "$PIPELINE_STATUS_FILE"

git commit --allow-empty -m "Prepare CSFloat nightly inputs"
RISK_COMMIT="$(git rev-parse HEAD)"
git push origin main
echo "[$(timestamp)] pushed risk-ready commit: $RISK_COMMIT"

echo "[$(timestamp)] waiting for GitHub CSFloat worker, timeout=${PIPELINE_WAIT_TIMEOUT_MINUTES}m poll=${PIPELINE_POLL_SECONDS}s"
deadline=$((SECONDS + PIPELINE_WAIT_TIMEOUT_MINUTES * 60))
worker_status=""
while (( SECONDS < deadline )); do
  git fetch --quiet origin main
  worker_status="$(remote_pipeline_status || true)"
  if [[ "$worker_status" == "success" || "$worker_status" == "failure" ]]; then
    break
  fi
  echo "[$(timestamp)] GitHub worker status: ${worker_status:-pending}; sleeping ${PIPELINE_POLL_SECONDS}s"
  sleep "$PIPELINE_POLL_SECONDS"
done

if [[ "$worker_status" != "success" && "$worker_status" != "failure" ]]; then
  echo "[$(timestamp)] GitHub CSFloat worker timed out waiting for run_id=$PIPELINE_RUN_ID" >&2
  exit 1
fi

git pull --ff-only origin main
echo "[$(timestamp)] GitHub worker finished with status=$worker_status"
if [[ "$worker_status" != "success" ]]; then
  echo "[$(timestamp)] nightly failed; inspect $PIPELINE_STATUS_FILE and GitHub Actions logs" >&2
  exit 1
fi

echo "[$(timestamp)] nightly completed; latest GitHub artifacts pulled"
