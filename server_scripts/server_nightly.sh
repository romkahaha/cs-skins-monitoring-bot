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
LOCAL_STASH_REF=""
LOCAL_STASH_LABEL=""
NIGHTLY_FAILOVER_CONFIG="automation/configs/monitoring.json"

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
  local payload
  payload="$(git show "origin/main:$PIPELINE_STATUS_FILE" 2>/dev/null || true)"
  "$PYTHON_BIN" -c '
import json
import sys

run_id = sys.argv[1]
try:
    payload = json.loads(sys.stdin.read())
except Exception:
    raise SystemExit(0)
if payload.get("run_id") == run_id:
    print(payload.get("status", ""))
' "$PIPELINE_RUN_ID" <<<"$payload"
}

stash_local_changes_if_needed() {
  local reason="$1"
  local status
  status="$(git status --porcelain --untracked-files=all)"
  if [[ -z "$status" ]]; then
    return 1
  fi
  LOCAL_STASH_LABEL="server_nightly:${PIPELINE_RUN_ID}:${reason}"
  echo "[$(timestamp)] stashing local changes before ${reason}"
  git stash push --include-untracked -m "$LOCAL_STASH_LABEL" -- . \
    ":(exclude)automation_runtime/precomputed_fit_plots" \
    ":(exclude)automation_runtime/telegram_queue" \
    ":(exclude)automation_runtime/state_telegram_alerts.json.lock" >/dev/null
  LOCAL_STASH_REF="stash@{0}"
  return 0
}

restore_local_changes_if_needed() {
  if [[ -z "$LOCAL_STASH_REF" ]]; then
    return 0
  fi
  local ref="$LOCAL_STASH_REF"
  local label="$LOCAL_STASH_LABEL"
  LOCAL_STASH_REF=""
  LOCAL_STASH_LABEL=""
  echo "[$(timestamp)] selectively restoring local changes after ${label}"

  local untracked_ref=""
  if git rev-parse -q --verify "${ref}^3" >/dev/null 2>&1; then
    untracked_ref="${ref}^3"
  fi

  should_skip_stash_restore_path() {
    local path="$1"
    case "$path" in
      automation_runtime/*latest*|\
automation_runtime/monitor_list_tier_*.py|\
automation_runtime/monitor_tiers_latest.json|\
automation_runtime/github_csfloat_worker_history.csv|\
automation_runtime/server_pipeline_status_latest.json|\
skin_homog/data_skins_big/*|\
steam_listings/data/*)
        return 0
        ;;
      *)
        return 1
        ;;
    esac
  }

  local -a stash_paths=()
  while IFS= read -r path; do
    [[ -n "$path" ]] || continue
    stash_paths+=("$path")
  done < <(git stash show --name-only --format= "$ref")
  if [[ -n "$untracked_ref" ]]; then
    while IFS= read -r path; do
      [[ -n "$path" ]] || continue
      stash_paths+=("$path")
    done < <(git ls-tree -r --name-only "$untracked_ref")
  fi
  if ((${#stash_paths[@]} == 0)); then
    git stash drop "$ref" >/dev/null || true
    return 0
  fi

  local -A seen_paths=()
  local restored_count=0
  local skipped_count=0
  local restore_failed=0
  local path source_ref=""
  for path in "${stash_paths[@]}"; do
    if [[ -n "${seen_paths[$path]:-}" ]]; then
      continue
    fi
    seen_paths["$path"]=1
    if should_skip_stash_restore_path "$path"; then
      skipped_count=$((skipped_count + 1))
      continue
    fi
    source_ref="$ref"
    if [[ -n "$untracked_ref" ]] && git cat-file -e "${untracked_ref}:${path}" 2>/dev/null; then
      source_ref="$untracked_ref"
    elif ! git cat-file -e "${ref}:${path}" 2>/dev/null; then
      continue
    fi
    if ! git checkout "$source_ref" -- "$path" >/dev/null 2>&1; then
      echo "[$(timestamp)] failed to restore stashed path: $path from $source_ref" >&2
      restore_failed=1
      continue
    fi
    git reset --quiet -- "$path" >/dev/null 2>&1 || true
    restored_count=$((restored_count + 1))
  done

  if (( restore_failed )); then
    echo "[$(timestamp)] failed to restore selected local changes from $ref" >&2
    echo "[$(timestamp)] stash was kept intact; resolve manually with: git stash list" >&2
    exit 1
  fi

  echo "[$(timestamp)] restored ${restored_count} local paths; skipped ${skipped_count} generated artifact paths"
  git stash drop "$ref" >/dev/null || true
}

pull_rebase_preserving_local_changes() {
  local stashed=0
  if stash_local_changes_if_needed "git pull --rebase origin main"; then
    stashed=1
  fi
  if ! git pull --rebase origin main; then
    local rc=$?
    if (( stashed )); then
      restore_local_changes_if_needed
    fi
    return "$rc"
  fi
  if (( stashed )); then
    restore_local_changes_if_needed
  fi
}

pull_ff_only_preserving_local_changes() {
  local stashed=0
  if stash_local_changes_if_needed "git pull --ff-only origin main"; then
    stashed=1
  fi
  if ! git pull --ff-only origin main; then
    local rc=$?
    if (( stashed )); then
      restore_local_changes_if_needed
    fi
    return "$rc"
  fi
  if (( stashed )); then
    restore_local_changes_if_needed
  fi
}

nightly_failover_lease_seconds() {
  "$PYTHON_BIN" - <<'PY'
from pathlib import Path

from automation.config import load_json_config, monitoring_defaults
from automation.failover_monitoring import load_failover_config

root = Path.cwd()
config = load_json_config(root / "automation" / "configs" / "monitoring.json", monitoring_defaults())
failover = load_failover_config(config, root)
if failover.enabled and failover.request_on_nightly_start:
    print(int(failover.nightly_lease_seconds))
else:
    print(0)
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
pull_rebase_preserving_local_changes
git push origin main

NIGHTLY_FAILOVER_LEASE_SECONDS="$(nightly_failover_lease_seconds)"
if [[ "$NIGHTLY_FAILOVER_LEASE_SECONDS" =~ ^[1-9][0-9]*$ ]]; then
  echo "[$(timestamp)] requesting monitoring failover for nightly window lease=${NIGHTLY_FAILOVER_LEASE_SECONDS}s"
  "$PYTHON_BIN" automation/failover_monitoring.py sync \
    --config "$NIGHTLY_FAILOVER_CONFIG" \
    --mode request \
    --lease-seconds "$NIGHTLY_FAILOVER_LEASE_SECONDS" \
    --reason "nightly pipeline started; keep monitoring on GitHub while VPS rebuilds risk"
fi

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

pull_ff_only_preserving_local_changes
echo "[$(timestamp)] GitHub worker finished with status=$worker_status"
if [[ "$worker_status" != "success" ]]; then
  echo "[$(timestamp)] nightly failed; inspect $PIPELINE_STATUS_FILE and GitHub Actions logs" >&2
  exit 1
fi

echo "[$(timestamp)] nightly completed; latest GitHub artifacts pulled"
