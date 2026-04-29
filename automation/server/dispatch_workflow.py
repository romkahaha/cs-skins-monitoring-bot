"""Dispatch a GitHub Actions workflow from an external scheduler."""

from __future__ import annotations

import argparse
import os
from typing import Any

import requests


def parse_key_value(values: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for value in values:
        if "=" not in value:
            raise ValueError(f"Expected key=value input, got: {value}")
        key, raw = value.split("=", 1)
        key = key.strip()
        if not key:
            raise ValueError(f"Empty input key in: {value}")
        out[key] = raw
    return out


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Dispatch a GitHub Actions workflow.")
    parser.add_argument("--repo", default="romkahaha/books", help="GitHub repository as owner/name.")
    parser.add_argument("--workflow", default="library-nightly.yml", help="Workflow file name or workflow id.")
    parser.add_argument("--ref", default="main", help="Git ref to run.")
    parser.add_argument("--token-env", default="GITHUB_DISPATCH_TOKEN", help="Environment variable containing token.")
    parser.add_argument("--input", action="append", default=[], help="Workflow input as key=value.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    token = os.environ.get(args.token_env)
    if not token:
        raise RuntimeError(f"Missing {args.token_env}")

    inputs = parse_key_value(args.input)
    payload: dict[str, Any] = {"ref": args.ref}
    if inputs:
        payload["inputs"] = inputs

    url = f"https://api.github.com/repos/{args.repo}/actions/workflows/{args.workflow}/dispatches"
    response = requests.post(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        json=payload,
        timeout=60,
    )
    if response.status_code != 204:
        raise RuntimeError(f"Dispatch failed: {response.status_code} {response.text[:800]}")
    print(f"dispatched {args.repo}/{args.workflow} ref={args.ref} inputs={inputs}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
