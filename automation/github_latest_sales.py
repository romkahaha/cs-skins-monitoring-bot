from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from automation.alert_enrichment import fetch_latest_sales, load_enrichment_config, write_json
from automation.config import load_json_config, monitoring_defaults
from automation.risk_filters import repo_root_from
from automation.state import utc_now_iso


REQUEST_REL = Path("automation_runtime/latest_sales_request_latest.json")
RESULT_REL = Path("automation_runtime/latest_sales_result_latest.json")
REQUESTS_DIR_REL = Path("automation_runtime/latest_sales/requests")
RESULTS_DIR_REL = Path("automation_runtime/latest_sales/results")
WORKFLOW_REL = Path(".github/workflows/latest_sales_fetch.yml")


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def run_git(repo: Path, args: list[str], *, check: bool = True, capture_output: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], check=check, text=True, capture_output=capture_output)


def ensure_repo(repo_path: Path | None, remote_url: str) -> Path:
    if repo_path is None:
        raise RuntimeError("github latest-sales repo_path is not configured")
    if not (repo_path / ".git").is_dir():
        raise RuntimeError(f"github latest-sales repo is not a git checkout: {repo_path}")
    if remote_url:
        remotes = run_git(repo_path, ["remote", "-v"], capture_output=True).stdout
        if "origin" not in remotes:
            run_git(repo_path, ["remote", "add", "origin", remote_url])
    return repo_path


def load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def maybe_pull(repo: Path, branch: str) -> None:
    remotes = {line.strip() for line in run_git(repo, ["remote"], capture_output=True).stdout.splitlines() if line.strip()}
    if "origin" not in remotes:
        return
    run_git(repo, ["pull", "--ff-only", "origin", branch])


def workflow_text() -> str:
    return """name: Latest Sales Fetch

on:
  push:
    branches:
      - main
  workflow_dispatch:

permissions:
  contents: write

concurrency:
  group: latest-sales-fetch
  cancel-in-progress: false

jobs:
  run-fetch:
    runs-on: ubuntu-latest
    timeout-minutes: 20
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Check latest-sales request
        id: request
        run: |
          python - <<'PY'
          import json
          import os
          from pathlib import Path
          request_path = Path("automation_runtime/latest_sales_request_latest.json")
          active = "false"
          mode = "missing"
          paths = [request_path]
          request_dir = Path("automation_runtime/latest_sales/requests")
          if request_dir.is_dir():
              paths.extend(sorted(request_dir.glob("*.json")))
          for path in paths:
              if not path.is_file():
                  continue
              payload = json.loads(path.read_text(encoding="utf-8"))
              mode = str(payload.get("mode", "unknown"))
              if payload.get("trigger_run"):
                  active = "true"
                  break
          with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as handle:
              handle.write(f"active={active}\\n")
              handle.write(f"mode={mode}\\n")
          print(f"latest-sales request mode={mode} active={active}")
          PY

      - name: Install dependencies
        if: steps.request.outputs.active == 'true'
        run: |
          python -m pip install --upgrade pip
          python -m pip install -r requirements.txt

      - name: Run latest-sales request
        if: steps.request.outputs.active == 'true'
        run: |
          python automation/latest_sales_worker.py run-request
"""


def build_request_payload(*, item: str, max_sales_rows: int) -> dict[str, Any]:
    request_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ") + f"-{platform.node()}"
    return {
        "version": 1,
        "mode": "request",
        "trigger_run": True,
        "request_id": request_id,
        "requested_at_utc": utc_now_iso(),
        "requested_by": platform.node(),
        "item": item,
        "max_sales_rows": int(max(1, max_sales_rows)),
    }


def request_latest_sales(
    *,
    item: str,
    repo_path: Path | None,
    remote_url: str,
    branch: str,
    max_sales_rows: int,
) -> str:
    repo = ensure_repo(repo_path, remote_url)
    workflow_path = repo / WORKFLOW_REL
    workflow_path.parent.mkdir(parents=True, exist_ok=True)
    workflow_path.write_text(workflow_text(), encoding="utf-8")

    request = build_request_payload(item=item, max_sales_rows=max_sales_rows)
    write_json(repo / REQUEST_REL, request)
    write_json(repo / REQUESTS_DIR_REL / f"{request['request_id']}.json", request)
    write_json(
        repo / RESULT_REL,
        {
            "version": 1,
            "request_id": request["request_id"],
            "status": "pending",
            "requested_at_utc": request["requested_at_utc"],
            "item": item,
        },
    )
    write_json(
        repo / RESULTS_DIR_REL / f"{request['request_id']}.json",
        {
            "version": 1,
            "request_id": request["request_id"],
            "status": "pending",
            "requested_at_utc": request["requested_at_utc"],
            "item": item,
        },
    )

    run_git(
        repo,
        [
            "add",
            str(REQUEST_REL),
            str(RESULT_REL),
            str(REQUESTS_DIR_REL),
            str(RESULTS_DIR_REL),
            str(WORKFLOW_REL),
        ],
    )
    diff = subprocess.run(["git", "-C", str(repo), "diff", "--cached", "--quiet"])
    if diff.returncode != 1:
        if diff.returncode == 0:
            raise RuntimeError("latest-sales request produced no git changes")
        raise RuntimeError(f"latest-sales git diff failed with exit {diff.returncode}")
    run_git(repo, ["commit", "-m", f"Request latest-sales fetch for {item}"])
    remotes = {line.strip() for line in run_git(repo, ["remote"], capture_output=True).stdout.splitlines() if line.strip()}
    if "origin" not in remotes:
        raise RuntimeError("latest-sales repo origin remote is not configured")
    push = run_git(repo, ["push", "origin", f"HEAD:{branch}"], check=False)
    if push.returncode != 0:
        run_git(repo, ["pull", "--rebase", "origin", branch])
        run_git(repo, ["push", "origin", f"HEAD:{branch}"])
    return str(request["request_id"])


def wait_for_result(
    *,
    request_id: str,
    repo_path: Path | None,
    remote_url: str,
    branch: str,
    timeout_sec: float,
    poll_interval_sec: float,
) -> dict[str, Any]:
    repo = ensure_repo(repo_path, remote_url)
    deadline = time.time() + max(1.0, timeout_sec)
    last_error = ""
    while time.time() < deadline:
        try:
            maybe_pull(repo, branch)
        except Exception as exc:
            last_error = str(exc)
        payload = load_json(repo / RESULTS_DIR_REL / f"{request_id}.json")
        if not payload:
            payload = load_json(repo / RESULT_REL)
        if str(payload.get("request_id") or "") == request_id:
            status = str(payload.get("status") or "")
            if status == "success":
                return payload
            if status == "error":
                raise RuntimeError(str(payload.get("error") or "latest-sales fetch failed"))
        time.sleep(max(1.0, poll_interval_sec))
    if last_error:
        raise TimeoutError(f"timed out waiting for GitHub latest-sales result ({last_error})")
    raise TimeoutError("timed out waiting for GitHub latest-sales result")


def fetch_latest_sales_via_github(
    *,
    item: str,
    repo_path: Path | None,
    remote_url: str,
    branch: str,
    timeout_sec: float,
    poll_interval_sec: float,
    max_sales_rows: int,
    job_dir: Path,
) -> dict[str, Any]:
    request_id = request_latest_sales(
        item=item,
        repo_path=repo_path,
        remote_url=remote_url,
        branch=branch,
        max_sales_rows=max_sales_rows,
    )
    result = wait_for_result(
        request_id=request_id,
        repo_path=repo_path,
        remote_url=remote_url,
        branch=branch,
        timeout_sec=timeout_sec,
        poll_interval_sec=poll_interval_sec,
    )
    payload = result.get("latest_sales")
    if not isinstance(payload, dict):
        raise RuntimeError(f"GitHub latest-sales result missing payload for request {request_id}")
    payload = dict(payload)
    payload["source"] = "github_fetch"
    payload["request_id"] = request_id
    write_json(job_dir / "latest_sales_github_result.json", result)
    return payload


def finalize_request(request: dict[str, Any], *, status: str, error: str | None = None) -> dict[str, Any]:
    payload = dict(request)
    payload["mode"] = "clear"
    payload["trigger_run"] = False
    payload["last_status"] = status
    payload["last_completed_at_utc"] = utc_now_iso()
    if error:
        payload["last_error"] = error
    return payload


def push_runtime(repo_root: Path, *, branch: str) -> None:
    run_git(repo_root, ["config", "user.name", "github-actions[bot]"])
    run_git(repo_root, ["config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"])
    run_git(repo_root, ["add", str(REQUEST_REL), str(RESULT_REL)])
    diff = subprocess.run(["git", "-C", str(repo_root), "diff", "--cached", "--quiet"])
    if diff.returncode == 0:
        return
    if diff.returncode != 1:
        raise RuntimeError(f"latest-sales runtime diff failed with exit {diff.returncode}")
    run_git(repo_root, ["commit", "-m", "Update latest-sales fetch runtime [skip ci]"])
    push = run_git(repo_root, ["push", "origin", f"HEAD:{branch}"], check=False)
    if push.returncode != 0:
        run_git(repo_root, ["pull", "--rebase", "origin", branch])
        run_git(repo_root, ["push", "origin", f"HEAD:{branch}"])


def run_request(*, repo_root: Path, request_json: Path, config_path: Path) -> int:
    request = load_json(request_json)
    if not bool(request.get("trigger_run")):
        print(f"latest-sales request inactive (mode={request.get('mode')}); nothing to do")
        return 0

    config = load_json_config(config_path, monitoring_defaults())
    config.setdefault("alert_enrichment", {})
    config["alert_enrichment"]["enabled"] = True
    config["alert_enrichment"]["use_cache"] = False
    config["alert_enrichment"]["persist_cache"] = False
    config["alert_enrichment"]["use_stale_cache_on_error"] = False
    if request.get("max_sales_rows") is not None:
        config["alert_enrichment"]["max_sales_rows"] = int(request["max_sales_rows"])
    config["alert_enrichment"]["log_dir"] = str(repo_root / "automation_runtime" / "latest_sales_fetch")
    enrich_cfg = load_enrichment_config(config, root=repo_root)

    item = str(request.get("item") or "").strip()
    if not item:
        raise RuntimeError("latest-sales request is missing item")
    job_dir = repo_root / "automation_runtime" / "latest_sales_fetch" / str(request.get("request_id") or "request")
    job_dir.mkdir(parents=True, exist_ok=True)

    exit_code = 0
    try:
        latest_sales = fetch_latest_sales(item, enrich_cfg, job_dir=job_dir)
        write_json(
            repo_root / RESULT_REL,
            {
                "version": 1,
                "request_id": request.get("request_id"),
                "status": "success",
                "completed_at_utc": utc_now_iso(),
                "item": item,
                "latest_sales": latest_sales,
            },
        )
        request = finalize_request(request, status="success")
    except Exception as exc:
        exit_code = 1
        write_json(
            repo_root / RESULT_REL,
            {
                "version": 1,
                "request_id": request.get("request_id"),
                "status": "error",
                "completed_at_utc": utc_now_iso(),
                "item": item,
                "error": str(exc),
            },
        )
        request = finalize_request(request, status="error", error=str(exc))
    write_json(request_json, request)
    branch = str(os.environ.get("GITHUB_REF_NAME") or "main").strip() or "main"
    push_runtime(repo_root, branch=branch)
    return exit_code


def parse_args() -> argparse.Namespace:
    root = repo_root_from(Path(__file__))
    parser = argparse.ArgumentParser(description="GitHub Actions bridge for CSFloat latest-sales fetches.")
    sub = parser.add_subparsers(dest="command", required=True)

    run_req = sub.add_parser("run-request", help="Run latest-sales request inside the GitHub repo.")
    run_req.add_argument("--request-json", type=Path, default=root / REQUEST_REL)
    run_req.add_argument("--config", type=Path, default=root / "automation" / "configs" / "monitoring.json")
    run_req.add_argument("--root", type=Path, default=root)
    return parser.parse_args()


def main() -> int:
    configure_stdio()
    args = parse_args()
    if args.command == "run-request":
        return run_request(
            repo_root=args.root.resolve(),
            request_json=args.request_json.resolve(),
            config_path=args.config.resolve(),
        )
    raise RuntimeError(f"Unsupported command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
