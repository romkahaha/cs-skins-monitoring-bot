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
NIGHTLY_RISK_TIMEOUT_MINUTES="${NIGHTLY_RISK_TIMEOUT_MINUTES:-420}"
NIGHTLY_BACKUP_DIR="automation_runtime/nightly_backups/$PIPELINE_RUN_ID"
LOCAL_STASH_REF=""
LOCAL_STASH_LABEL=""
NIGHTLY_FAILOVER_CONFIG="automation/configs/monitoring.json"
ACTIVE_CHILD_PID=""

cleanup_active_child() {
  local pid="$ACTIVE_CHILD_PID"
  if [[ -z "$pid" ]]; then
    return 0
  fi
  if kill -0 "$pid" 2>/dev/null; then
    echo "[$(timestamp)] stopping active nightly child process group: $pid" >&2
    kill -TERM "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
    sleep 2
    kill -KILL "-$pid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null || true
  fi
}

trap cleanup_active_child EXIT INT TERM

run_tracked() {
  setsid "$@" &
  ACTIVE_CHILD_PID="$!"
  set +e
  wait "$ACTIVE_CHILD_PID"
  local rc="$?"
  set -e
  ACTIVE_CHILD_PID=""
  return "$rc"
}

run_tracked_timeout() {
  local timeout_minutes="$1"
  shift
  local deadline=$((SECONDS + timeout_minutes * 60))
  setsid "$@" &
  ACTIVE_CHILD_PID="$!"
  local rc=0
  while kill -0 "$ACTIVE_CHILD_PID" 2>/dev/null; do
    if (( SECONDS >= deadline )); then
      echo "[$(timestamp)] nightly child timed out after ${timeout_minutes}m: $*" >&2
      cleanup_active_child
      set +e
      wait "$ACTIVE_CHILD_PID"
      set -e
      ACTIVE_CHILD_PID=""
      return 124
    fi
    sleep 5
  done
  set +e
  wait "$ACTIVE_CHILD_PID"
  rc="$?"
  set -e
  ACTIVE_CHILD_PID=""
  return "$rc"
}

send_nightly_status() {
  local title="$1"
  local status="$2"
  local message="$3"
  "$PYTHON_BIN" -B automation/nightly/notify_status.py \
    --title "$title" \
    --status "$status" \
    --message "$message" || true
}

nightly_artifact_paths() {
  cat <<'EOF'
automation_runtime/risk_metrics_latest.csv
automation_runtime/risk_progress_latest.log
automation_runtime/risk_runtime_latest.json
automation_runtime/risk_candidates_latest.csv
automation_runtime/model_coverage_latest.csv
automation_runtime/model_backfill_queue_latest.csv
automation_runtime/model_backfill_queue_latest.py
automation_runtime/model_backfill_batch_latest.py
automation_runtime/model_backfill_runtime_latest.json
automation_runtime/model_backfill_progress_latest.log
automation_runtime/model_refit_progress_latest.log
automation_runtime/monitor_list_latest.csv
automation_runtime/monitor_list_latest.py
automation_runtime/monitor_list_tier_a.py
automation_runtime/monitor_list_tier_b.py
automation_runtime/monitor_list_tier_c.py
automation_runtime/monitor_tiers_latest.json
automation_runtime/base_snapshot_latest.csv
skin_homog/data_skins_big/_summary.csv
steam_listings/data/float_fit_rel_curves.json
EOF
}

backup_nightly_artifacts() {
  mkdir -p "$NIGHTLY_BACKUP_DIR"
  nightly_artifact_paths >"$NIGHTLY_BACKUP_DIR/managed_paths.txt"
  while IFS= read -r path; do
    [[ -n "$path" ]] || continue
    if [[ -e "$path" ]]; then
      mkdir -p "$NIGHTLY_BACKUP_DIR/$(dirname "$path")"
      cp -p "$path" "$NIGHTLY_BACKUP_DIR/$path"
    fi
  done <"$NIGHTLY_BACKUP_DIR/managed_paths.txt"
  echo "[$(timestamp)] backed up nightly artifacts: $NIGHTLY_BACKUP_DIR"
}

restore_nightly_artifacts() {
  if [[ ! -f "$NIGHTLY_BACKUP_DIR/managed_paths.txt" ]]; then
    echo "[$(timestamp)] no nightly backup manifest found at $NIGHTLY_BACKUP_DIR" >&2
    return 0
  fi
  while IFS= read -r path; do
    [[ -n "$path" ]] || continue
    rm -f "$path"
    if [[ -e "$NIGHTLY_BACKUP_DIR/$path" ]]; then
      mkdir -p "$(dirname "$path")"
      cp -p "$NIGHTLY_BACKUP_DIR/$path" "$path"
    fi
  done <"$NIGHTLY_BACKUP_DIR/managed_paths.txt"
  echo "[$(timestamp)] restored previous nightly artifacts from $NIGHTLY_BACKUP_DIR"
}

handled_nightly_failure() {
  local stage="$1"
  local rc="$2"
  local details="$3"
  restore_nightly_artifacts
  write_vps_status "failure" "$stage failed before producing a complete nightly artifact set; previous good artifacts remain in use. rc=$rc"
  send_nightly_status \
    "Nightly $stage failed" \
    "error" \
    "$details
run_id=$PIPELINE_RUN_ID
Previous good artifacts were restored. Day monitoring can start on the old risk/base."
  echo "[$(timestamp)] handled nightly ${stage} failure; previous artifacts restored; exiting 0 to release lock"
  exit 0
}

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

remote_pipeline_failed_stage() {
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
    print(payload.get("failed_stage", ""))
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
  if ! "$PYTHON_BIN" automation/failover_monitoring.py sync \
    --config "$NIGHTLY_FAILOVER_CONFIG" \
    --mode request \
    --lease-seconds "$NIGHTLY_FAILOVER_LEASE_SECONDS" \
    --reason "nightly pipeline started; keep monitoring on GitHub while VPS rebuilds risk"; then
    echo "[$(timestamp)] warning: monitoring failover request failed; continuing nightly risk/base rebuild" >&2
  fi
fi

echo "[$(timestamp)] rebuilding VPS risk inputs"
backup_nightly_artifacts
risk_rc=0
run_tracked_timeout "$NIGHTLY_RISK_TIMEOUT_MINUTES" "$PYTHON_BIN" -B automation/nightly/build_risk_metrics.py --create || risk_rc="$?"
if (( risk_rc != 0 )); then
  handled_nightly_failure "risk" "$risk_rc" "Risk rebuild did not finish cleanly. This is usually expired Steam cookies, Steam 429 throttling, or the risk quality gate rejecting a partial CSV."
fi
run_tracked "$PYTHON_BIN" -B automation/nightly/build_risk_candidates.py || risk_rc="$?"
if (( risk_rc != 0 )); then
  handled_nightly_failure "risk candidates" "$risk_rc" "Risk metrics finished, but candidate/coverage generation failed, so the new risk set was not promoted."
fi
run_tracked "$PYTHON_BIN" -B automation/nightly/build_model_backfill_queue.py || risk_rc="$?"
if (( risk_rc != 0 )); then
  handled_nightly_failure "model backfill queue" "$risk_rc" "Risk metrics finished, but model backfill queue generation failed, so the new risk set was not promoted."
fi

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
send_nightly_status \
  "Nightly risk OK" \
  "ok" \
  "New risk metrics, risk candidates, and CSFloat backfill queue were rebuilt and pushed.
run_id=$PIPELINE_RUN_ID
risk_commit=$RISK_COMMIT"

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
  handled_nightly_failure "base" "124" "GitHub CSFloat worker timed out before producing a complete base/monitor artifact set."
fi

echo "[$(timestamp)] GitHub worker finished with status=$worker_status"
if [[ "$worker_status" != "success" ]]; then
  failed_stage="$(remote_pipeline_failed_stage || true)"
  handled_nightly_failure "base" "1" "GitHub CSFloat worker failed at stage: ${failed_stage:-unknown}. Old base/monitor artifacts remain in use."
fi

pull_ff_only_preserving_local_changes
send_nightly_status \
  "Nightly base OK" \
  "ok" \
  "CSFloat worker finished successfully. Fresh model/base/monitor artifacts were pulled onto the server.
run_id=$PIPELINE_RUN_ID"
echo "[$(timestamp)] nightly completed; latest GitHub artifacts pulled"
