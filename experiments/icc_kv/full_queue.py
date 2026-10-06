"""Overload matrix runner.

Writes a new directory and does not resume e2e_full, scale_c13000,
burst_c13000, or ablation_c13000. Does not start until --confirm is passed.
Capacity is an argument: pass the measured drain, not a leftover nominal
value. This file does not launch a run by being imported.
"""
from __future__ import annotations

import subprocess
import time
from pathlib import Path

ROOT = Path("/home/byh/B02")
PY = ROOT / "poc/.venv/bin/python"
OUT = ROOT / "analysis/icc_kv"


def git_commit() -> str:
    try:
        return subprocess.check_output(
            ["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"], text=True,
        ).strip()
    except subprocess.CalledProcessError:
        return "unknown"


def log(path: Path, message: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}"
    print(line, flush=True)
    with path.open("a") as handle:
        handle.write(line + "\n")


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument("--capacity", type=float)
    parser.add_argument("--out-dir", type=Path)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--requests", type=int, default=500)
    parser.add_argument("--warmup-requests", type=int, default=50)
    parser.add_argument("--seeds", type=int, default=5)
    parser.add_argument("--arrival-rate", type=float, default=1.25)
    parser.add_argument("--invalidate-every", type=int, default=8)
    parser.add_argument("--noise-workers", type=int, default=4)
    parser.add_argument("--noise-senders", type=int, default=4)
    args = parser.parse_args()
    if not args.confirm or args.capacity is None:
        raise SystemExit(
            "overload runner is idle. Pass --confirm and --capacity measured "
            "for this path. It will not resume the old result directories."
        )
    latest = OUT / "overload_latest.txt"
    if args.resume:
        if not latest.exists():
            raise SystemExit(f"no run to resume; {latest} is missing")
        out = Path(latest.read_text().strip())
    elif args.out_dir is not None:
        out = args.out_dir
    else:
        stamp = time.strftime("%Y%m%d_%H%M%S")
        out = OUT / f"overload_{stamp}_{git_commit()}"
    latest.parent.mkdir(parents=True, exist_ok=True)
    latest.write_text(str(out) + "\n")
    log_path = out / "runner.log"
    common = [
        "--capacity", str(args.capacity),
        "--seeds", str(args.seeds),
        "--requests", str(args.requests),
        "--warmup-requests", str(args.warmup_requests),
        "--arrival-rate", str(args.arrival_rate),
        "--invalidate-every", str(args.invalidate_every),
        "--noise-workers", str(args.noise_workers),
        "--noise-senders", str(args.noise_senders),
        "--concurrency", "4",
        "--kv-cache-tokens", "104544",
    ]

    def stage(name: str, argv: list[str]) -> None:
        log(log_path, f"start {name}: {' '.join(argv)}")
        completed = subprocess.run(argv, cwd=ROOT, check=False)
        log(log_path, f"done {name}: exit {completed.returncode}")
        if completed.returncode != 0:
            raise SystemExit(completed.returncode)

    log(log_path, f"overload runner writing {out}")
    stage("e2e", [
        str(PY), "-u", "-m", "experiments.icc_kv.e2e",
        "--scenarios", "xhigh,ultrahigh,burst",
        "--methods", "FullSync,StaticSemantic,Adaptive,Ideal,RateFIFO",
        "--out-dir", str(out / "e2e"),
        *common,
    ])
    stage("ablation", [
        str(PY), "-u", "-m", "experiments.icc_kv.e2e",
        "--scenarios", "ultrahigh,burst",
        "--methods", "FullSync,StaticSemantic,AdaptiveNoPriority,Adaptive",
        "--out-dir", str(out / "ablation"),
        *common,
    ])
    log(log_path, "overload runner finished")


if __name__ == "__main__":
    main()
