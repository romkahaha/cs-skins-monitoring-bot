#!/usr/bin/env bash
set -Eeuo pipefail

BOT_ROOT="${GITHUB_WORKSPACE:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)}"
PYTHON_BIN="${PYTHON_BIN:-python}"
STATUS_FILE="$BOT_ROOT/automation_runtime/server_pipeline_status_latest.json"
HISTORY_FILE="$BOT_ROOT/automation_runtime/github_csfloat_worker_history.csv"
STAGES_FILE="${RUNNER_TEMP:-/tmp}/github_csfloat_worker_stages.jsonl"

cd "$BOT_ROOT"
mkdir -p "$BOT_ROOT/automation_runtime"
: >"$STAGES_FILE"

read_run_id() {
  "$PYTHON_BIN" - "$STATUS_FILE" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
if path.is_file():
    try:
        print(json.loads(path.read_text(encoding="utf-8")).get("run_id", ""))
    except Exception:
        print("")
else:
    print("")
PY
}

RUN_ID="$(read_run_id)"
if [[ -z "$RUN_ID" ]]; then
  RUN_ID="manual-${GITHUB_RUN_ID:-unknown}-$(date -u +%Y%m%dT%H%M%SZ)"
fi

START_EPOCH="$(date -u +%s)"
STARTED_AT_UTC="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
STATUS="success"
FAILED_STAGE=""

record_stage() {
  local name="$1"
  local started="$2"
  local finished="$3"
  local duration="$4"
  local exit_code="$5"
  "$PYTHON_BIN" - "$STAGES_FILE" "$name" "$started" "$finished" "$duration" "$exit_code" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
record = {
    "name": sys.argv[2],
    "started_at_utc": sys.argv[3],
    "finished_at_utc": sys.argv[4],
    "duration_sec": int(sys.argv[5]),
    "exit_code": int(sys.argv[6]),
}
with path.open("a", encoding="utf-8") as fh:
    fh.write(json.dumps(record, ensure_ascii=False) + "\n")
PY
}

run_stage() {
  local name="$1"
  shift
  if [[ "$STATUS" != "success" ]]; then
    echo "Skipping $name because previous stage failed: $FAILED_STAGE"
    return 0
  fi

  echo
  echo "=== $name ==="
  echo "$*"
  local started_epoch finished_epoch rc
  local started_at finished_at
  started_epoch="$(date -u +%s)"
  started_at="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
  set +e
  "$@"
  rc="$?"
  set -e
  finished_epoch="$(date -u +%s)"
  finished_at="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
  record_stage "$name" "$started_at" "$finished_at" "$((finished_epoch - started_epoch))" "$rc"
  if [[ "$rc" -ne 0 ]]; then
    STATUS="failure"
    FAILED_STAGE="$name"
  fi
}

write_status_files() {
  local finished_at="$1"
  local duration="$2"
  "$PYTHON_BIN" - "$STATUS_FILE" "$HISTORY_FILE" "$STAGES_FILE" \
    "$RUN_ID" "$STATUS" "$STARTED_AT_UTC" "$finished_at" "$duration" \
    "${GITHUB_RUN_ID:-}" "${GITHUB_RUN_ATTEMPT:-}" "${GITHUB_SHA:-}" "$FAILED_STAGE" <<'PY'
import csv
import json
import sys
from pathlib import Path

status_path = Path(sys.argv[1])
history_path = Path(sys.argv[2])
stages_path = Path(sys.argv[3])
run_id = sys.argv[4]
status = sys.argv[5]
started_at = sys.argv[6]
finished_at = sys.argv[7]
duration_sec = int(sys.argv[8])
github_run_id = sys.argv[9]
github_run_attempt = sys.argv[10]
github_sha = sys.argv[11]
failed_stage = sys.argv[12]

stages = []
if stages_path.is_file():
    for line in stages_path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            stages.append(json.loads(line))

payload = {
    "run_id": run_id,
    "status": status,
    "source": "github_actions",
    "started_at_utc": started_at,
    "finished_at_utc": finished_at,
    "duration_sec": duration_sec,
    "github_run_id": github_run_id,
    "github_run_attempt": github_run_attempt,
    "github_sha": github_sha,
    "failed_stage": failed_stage,
    "stages": stages,
}
status_path.parent.mkdir(parents=True, exist_ok=True)
status_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

history_path.parent.mkdir(parents=True, exist_ok=True)
exists = history_path.is_file()
stage_summary = "; ".join(f"{s['name']}={s['duration_sec']}s/{s['exit_code']}" for s in stages)
with history_path.open("a", encoding="utf-8", newline="") as fh:
    writer = csv.DictWriter(
        fh,
        fieldnames=[
            "run_id",
            "status",
            "started_at_utc",
            "finished_at_utc",
            "duration_sec",
            "github_run_id",
            "github_run_attempt",
            "github_sha",
            "failed_stage",
            "stage_summary",
        ],
    )
    if not exists:
        writer.writeheader()
    writer.writerow(
        {
            "run_id": run_id,
            "status": status,
            "started_at_utc": started_at,
            "finished_at_utc": finished_at,
            "duration_sec": duration_sec,
            "github_run_id": github_run_id,
            "github_run_attempt": github_run_attempt,
            "github_sha": github_sha,
            "failed_stage": failed_stage,
            "stage_summary": stage_summary,
        }
    )
PY
}

commit_outputs() {
  git config user.name "github-actions[bot]"
  git config user.email "41898282+github-actions[bot]@users.noreply.github.com"

  git add automation_runtime skin_homog/data_skins_big steam_listings/data/float_fit_rel_curves.json
  if git diff --cached --quiet; then
    echo "No CSFloat worker output changes to commit."
    return 0
  fi

  git commit -m "Update CSFloat nightly artifacts [skip ci]"
  git fetch origin main
  git rebase origin/main
  git push origin HEAD:main
}

echo "run_id=$RUN_ID"
echo "github_run_id=${GITHUB_RUN_ID:-}"
echo "github_sha=${GITHUB_SHA:-}"

if [[ -z "${CSFLOAT_API_KEY:-}" ]]; then
  STATUS="failure"
  FAILED_STAGE="secrets preflight"
  FINISHED_AT_UTC="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
  END_EPOCH="$(date -u +%s)"
  echo "Missing required GitHub secret: CSFLOAT_API_KEY" >&2
  write_status_files "$FINISHED_AT_UTC" "$((END_EPOCH - START_EPOCH))"
  commit_outputs
  exit 1
fi

run_stage "model backfill" "$PYTHON_BIN" -B automation/nightly/run_model_backfill.py
run_stage "model refit" "$PYTHON_BIN" -B automation/nightly/run_model_refit.py
run_stage "model backfill queue after refit" "$PYTHON_BIN" -B automation/nightly/build_model_backfill_queue.py
run_stage "monitor list" "$PYTHON_BIN" -B automation/nightly/build_monitor_list.py
run_stage "base snapshot" "$PYTHON_BIN" -B automation/nightly/build_base_snapshot.py

FINISHED_AT_UTC="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
END_EPOCH="$(date -u +%s)"
write_status_files "$FINISHED_AT_UTC" "$((END_EPOCH - START_EPOCH))"
commit_outputs

if [[ "$STATUS" != "success" ]]; then
  echo "CSFloat worker failed at stage: $FAILED_STAGE" >&2
  exit 1
fi

echo "CSFloat worker completed successfully"
