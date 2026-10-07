"""Block runner for the coverage-priority admission matrix.

Does not start until --confirm is passed, and does not resume the old
e2e_full / scale / burst / ablation directories. --blocks is not part of the
resume identity; every other recorded flag must match.
"""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

ROOT = Path("/home/byh/B02")
PY = ROOT / "poc/.venv/bin/python"
OUT = ROOT / "analysis/icc_kv"

USEFUL_MIX = "1024:1,2048:1,4096:1"
NOISE_MIX = {
    "base": "",
    "ov0": "256:1",
    "ov10": "256:90,1024:4,2048:3,4096:3",
    "ov30": "256:70,1024:10,2048:10,4096:10",
    "ov60": "256:40,1024:20,2048:20,4096:20",
}
BLOCKS = {
    "main": {
        "scenarios": "xhigh,ultrahigh,burst",
        "methods": "FullSync,RateFIFO,BoundedFIFO16,BoundedPrio16,BoundedSemantic16,StaticSemantic,StaticTopK16,Ideal",
        "seeds": 3,
        "workload": "base",
    },
    "ksweep": {
        "scenarios": "ultrahigh",
        "methods": "StaticTopK4,StaticTopK8,StaticTopK16,StaticTopK64,BoundedSemantic4,BoundedSemantic16,BoundedSemantic64,Ideal",
        "seeds": 2,
        "workload": "ov30",
    },
    "overlap": {
        "scenarios": "ultrahigh",
        "methods": "StaticTopK16,BoundedSemantic16,BoundedPrio16,StaticSemantic,Ideal",
        "seeds": 2,
        "workloads": ("ov0", "ov10", "ov30", "ov60"),
    },
    "ablation": {
        "scenarios": "ultrahigh,burst",
        "methods": "StaticTopK16,StaticTopK16NoMerge,StaticTopK16NoDedup,StaticTopK16NoPriority,BoundedSemantic16,Adaptive,AdaptiveNoPriority",
        "seeds": 2,
        "workload": "base",
    },
    "rhoscan": {
        "scenarios": "normal,near,high,xhigh,ultrahigh",
        "methods": "FullSync,BoundedFIFO16,BoundedSemantic16,StaticTopK16,Ideal",
        "seeds": 1,
        "workload": "base",
    },
}


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


def workload_flags(name: str) -> list[str]:
    if name == "base":
        return []
    return [
        "--useful-pool", "32",
        "--useful-coverage-mix", USEFUL_MIX,
        "--noise-coverage-mix", NOISE_MIX[name],
    ]


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument("--capacity", type=float)
    parser.add_argument("--capacity-file", default="")
    parser.add_argument("--link-bit", type=int)
    parser.add_argument("--out-dir", type=Path)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--requests", type=int, default=0)
    parser.add_argument("--cell-seconds", type=float, default=600.0)
    parser.add_argument("--warmup-requests", type=int, default=10)
    parser.add_argument("--seeds", type=int, default=0)
    parser.add_argument("--arrival-rate", type=float)
    parser.add_argument("--gpu-rho", type=float, default=0.0)
    parser.add_argument("--burst-mult", type=float, default=5.0)
    parser.add_argument("--invalidate-every", type=int, default=8)
    parser.add_argument("--noise-workers", type=int, default=4)
    parser.add_argument("--noise-senders", type=int, default=8)
    parser.add_argument("--blocks", default="main,ksweep,overlap,ablation,rhoscan")
    parser.add_argument("--allow-dirty", action="store_true")
    parser.add_argument("--allow-outside-window", action="store_true")
    args = parser.parse_args()
    if not args.confirm or args.link_bit is None or (not args.capacity and not args.capacity_file):
        raise SystemExit(
            "block runner is idle. Pass --confirm, --link-bit, and --capacity-file "
            "(or --capacity). It will not resume the old result directories."
        )
    if args.arrival_rate is None and args.gpu_rho <= 0:
        raise SystemExit("pass --arrival-rate or --gpu-rho; the runner will not invent one")
    if args.capacity is None and args.capacity_file:
        window = json.loads(Path(args.capacity_file).read_text())
        args.capacity = float(window["c_drain_per_s"])
    blocks = [item for item in args.blocks.split(",") if item]
    unknown = [item for item in blocks if item not in BLOCKS]
    if unknown:
        raise SystemExit(f"unknown blocks: {unknown}")
    latest = OUT / "overload_latest.txt"
    if args.resume:
        if not latest.exists():
            raise SystemExit(f"no run to resume; {latest} is missing")
        out = Path(latest.read_text().strip())
    elif args.out_dir is not None:
        out = args.out_dir
    else:
        out = OUT / f"topk_{time.strftime('%Y%m%d_%H%M%S')}_{git_commit()}"
    out.mkdir(parents=True, exist_ok=True)
    latest.parent.mkdir(parents=True, exist_ok=True)
    latest.write_text(str(out) + "\n")
    identity = {
        "link_bit": args.link_bit,
        "capacity": args.capacity,
        "capacity_file": args.capacity_file,
        "arrival_rate": args.arrival_rate,
        "gpu_rho": args.gpu_rho,
        "cell_seconds": args.cell_seconds,
        "requests": args.requests,
        "warmup_requests": args.warmup_requests,
        "burst_mult": args.burst_mult,
        "invalidate_every": args.invalidate_every,
        "noise_workers": args.noise_workers,
        "noise_senders": args.noise_senders,
        "seeds_override": args.seeds,
    }
    config_path = out / "run_config.json"
    if args.resume:
        previous = json.loads(config_path.read_text())
        if previous != identity:
            raise SystemExit(f"resume flags differ from {config_path}")
    else:
        if config_path.exists():
            raise SystemExit(f"{config_path} already exists; pass --resume or a new --out-dir")
        config_path.write_text(json.dumps(identity, indent=2) + "\n")
    log_path = out / "runner.log"
    arrival = args.arrival_rate
    if args.gpu_rho > 0:
        calibration = out / "calibration.json"
        if not (args.resume and calibration.exists()):
            stage_argv = [
                str(PY), "-u", "-m", "experiments.icc_kv.e2e",
                "--calibrate-only", "--gpu-rho", str(args.gpu_rho),
                "--concurrency", "4", "--link-bit", str(args.link_bit),
                "--capacity", str(args.capacity or 1),
                "--calibration-out", str(calibration),
            ]
            if args.allow_dirty:
                stage_argv.append("--allow-dirty")
            log(log_path, "start calibration: " + " ".join(stage_argv))
            completed = subprocess.run(stage_argv, cwd=ROOT, check=False)
            log(log_path, f"done calibration: exit {completed.returncode}")
            if completed.returncode != 0:
                raise SystemExit(completed.returncode)
        arrival = float(json.loads(calibration.read_text())["arrival_rate"])
        log(log_path, f"arrival_rate {arrival}")
    common = [
        "--link-bit", str(args.link_bit),
        "--capacity", str(args.capacity or 1),
        "--warmup-requests", str(args.warmup_requests),
        "--arrival-rate", str(arrival),
        "--burst-mult", str(args.burst_mult),
        "--invalidate-every", str(args.invalidate_every),
        "--noise-workers", str(args.noise_workers),
        "--noise-senders", str(args.noise_senders),
        "--concurrency", "4",
        "--kv-cache-tokens", "104544",
    ]
    if args.capacity_file:
        common.extend(["--capacity-file", args.capacity_file])
    if args.cell_seconds > 0 and args.requests <= 0:
        common.extend(["--cell-seconds", str(args.cell_seconds), "--requests", "1"])
    else:
        common.extend(["--requests", str(args.requests or 500)])
    if args.allow_dirty:
        common.append("--allow-dirty")
    if args.allow_outside_window:
        common.append("--allow-outside-window")

    def stage(name: str, argv: list[str]) -> None:
        log(log_path, f"start {name}: {' '.join(argv)}")
        completed = subprocess.run(argv, cwd=ROOT, check=False)
        log(log_path, f"done {name}: exit {completed.returncode}")
        if completed.returncode != 0:
            raise SystemExit(completed.returncode)

    for block in blocks:
        spec = BLOCKS[block]
        names = spec.get("workloads") or (spec["workload"],)
        for workload in names:
            seeds = args.seeds or int(spec["seeds"])
            stage(f"{block}-{workload}", [
                str(PY), "-u", "-m", "experiments.icc_kv.e2e",
                "--scenarios", spec["scenarios"],
                "--methods", spec["methods"],
                "--seeds", str(seeds),
                "--out-dir", str(out / f"{block}_{workload}"),
                *workload_flags(workload),
                *common,
            ])
    log(log_path, "block runner finished")


if __name__ == "__main__":
    main()
