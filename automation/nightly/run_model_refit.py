"""Refit float-relative model curves from data_skins_big for automation."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from automation.config import load_json_config, nightly_defaults, path_from_config
from automation.risk_filters import repo_root_from


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def parse_args() -> argparse.Namespace:
    root = repo_root_from(Path(__file__))
    parser = argparse.ArgumentParser(description="Refit float-relative model curves from data_skins_big.")
    parser.add_argument(
        "--config",
        type=Path,
        default=root / "automation" / "configs" / "nightly.json",
        help="Nightly automation JSON config.",
    )
    parser.add_argument("--max-skins", type=int, default=None, help="Debug override for model_refit.max_skins.")
    parser.add_argument("--dry-run", action="store_true", help="Print command without running.")
    return parser.parse_args()


def require_dir(path: Path, label: str) -> None:
    if not path.is_dir():
        raise FileNotFoundError(f"{label} not found: {path}")


def require_file(path: Path, label: str) -> None:
    if not path.is_file():
        raise FileNotFoundError(f"{label} not found: {path}")


def add_arg(cmd: list[str], name: str, value: object) -> None:
    cmd.extend([name, str(value)])


def build_command(config: dict, *, max_skins_override: int | None = None) -> list[str]:
    root = repo_root_from(Path(__file__))
    cfg = config.get("model_refit", {})
    script = root / "steam_listings" / "fit_rel_models_from_data_skins_big.py"
    data_dir = path_from_config(config, "skin_data_dir")
    fit_json = path_from_config(config, "fit_json")

    cmd = [
        sys.executable,
        str(script),
        "--data-dir",
        str(data_dir),
        "--out-json",
        str(fit_json),
    ]
    add_arg(cmd, "--min-points", int(cfg.get("min_points", 5)))
    max_skins = max_skins_override if max_skins_override is not None else cfg.get("max_skins")
    if max_skins is not None:
        add_arg(cmd, "--max-skins", int(max_skins))
    add_arg(cmd, "--grid-n", int(cfg.get("grid_n", 300)))
    add_arg(cmd, "--smooth-frac", float(cfg.get("smooth_frac", 0.25)))
    add_arg(cmd, "--supersmooth-frac", float(cfg.get("supersmooth_frac", 0.35)))
    add_arg(cmd, "--smooth-min-pts", int(cfg.get("smooth_min_pts", 12)))
    add_arg(cmd, "--smooth-robust-iters", int(cfg.get("smooth_robust_iters", 2)))
    add_arg(cmd, "--seg-min-seg", int(cfg.get("seg_min_seg", 12)))
    add_arg(cmd, "--seg-z-thresh", float(cfg.get("seg_z_thresh", 4.0)))
    add_arg(cmd, "--seg-max-jumps", int(cfg.get("seg_max_jumps", 2)))
    add_arg(cmd, "--hybrid-alpha", float(cfg.get("hybrid_alpha", 0.7)))
    add_arg(cmd, "--outlier-neighbors", int(cfg.get("outlier_neighbors", 6)))
    add_arg(cmd, "--outlier-z", float(cfg.get("outlier_z", 3.5)))
    add_arg(cmd, "--outlier-min-abs-dev", float(cfg.get("outlier_min_abs_dev", 0.03)))
    return cmd


def run_logged(cmd: list[str], *, cwd: Path, log_path: Path) -> int:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8") as log:
        log.write("COMMAND:\n")
        log.write(" ".join(cmd) + "\n\n")
        log.flush()
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            print(line, end="")
            log.write(line)
            log.flush()
        return proc.wait()


def main() -> int:
    configure_stdio()
    args = parse_args()
    root = repo_root_from(Path(__file__))
    config_path = args.config.resolve()
    config = load_json_config(config_path, nightly_defaults())
    script = root / "steam_listings" / "fit_rel_models_from_data_skins_big.py"
    data_dir = path_from_config(config, "skin_data_dir")
    fit_json = path_from_config(config, "fit_json")
    progress_log = path_from_config(config, "model_refit_progress_log")

    require_file(script, "model refit script")
    require_dir(data_dir, "skin data dir")
    cmd = build_command(config, max_skins_override=args.max_skins)

    print(f"config: {config_path}")
    print(f"script: {script}")
    print(f"data dir: {data_dir}")
    print(f"fit json: {fit_json}")
    print(f"progress log: {progress_log}")
    print("command:")
    print(" ".join(cmd))
    if args.dry_run:
        return 0

    rc = run_logged(cmd, cwd=root, log_path=progress_log)
    if rc != 0:
        print(f"model refit failed with exit code {rc}", file=sys.stderr)
        return rc
    print("model refit completed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
