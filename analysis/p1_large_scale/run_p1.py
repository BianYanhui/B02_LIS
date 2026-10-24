#!/usr/bin/env python3
"""Run the P1 matrix. Completed run directories are left in place."""
from __future__ import annotations

import argparse
import csv
import json
import subprocess
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

from emulator import (
    build_trace,
    gateway_sha256,
    run_replay,
    service_fps_for_mbps,
)

ROOT = Path("/home/byh/B02")
OUT = ROOT / "analysis/p1_large_scale"
RUNS = OUT / "runs"
SEEDS = (2026102301, 2026102302, 2026102303)
MAIN = ("FullSync", "RateFIFO", "LatestOnly", "AgeCov-Greedy", "KVSelect")
BOUNDED = ("BoundedFIFO", "BoundedPrio", "BoundedSemantic", "KVSelect")
NS = (4, 64, 256, 1024, 4096)
LOGIC = "p1-v1"


def git_commit() -> str:
    try:
        return subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True).strip()
    except (subprocess.CalledProcessError, OSError):
        return "unknown"


def run_id(spec: dict) -> str:
    mbps = "na" if spec.get("mbps") is None else f"{spec['mbps']:g}"
    rho = "na" if spec.get("rho") is None else f"{spec['rho']:.1f}"
    return (
        f"{LOGIC}_{spec['experiment']}_n{spec['n']}_p{spec['policy']}"
        f"_k{spec['k']}_m{mbps}_r{rho}_s{spec['seed']}"
    )


def matrix(stage: str) -> list[dict]:
    specs: list[dict] = []
    if stage == "smoke":
        for policy in MAIN:
            specs.append({"experiment": "S", "n": 4, "policy": policy, "k": 16, "mbps": 10, "rho": None, "seed": SEEDS[0]})
        for policy in ("FullSync", "KVSelect"):
            specs.append({"experiment": "S", "n": 64, "policy": policy, "k": 16, "mbps": 10, "rho": None, "seed": SEEDS[0]})
        return specs
    if stage == "calibrate":
        for policy in ("FullSync", "RateFIFO", "LatestOnly", "AgeCov-Greedy", "KVSelect"):
            specs.append({
                "experiment": "CAL", "n": 4, "policy": policy, "k": 16, "mbps": 10, "rho": None,
                "seed": 2026072600, "heterogeneous": False,
            })
        return specs
    for n in NS:
        for seed in SEEDS:
            for policy in MAIN:
                for mbps in (10, 5, 2):
                    specs.append({"experiment": "A", "n": n, "policy": policy, "k": 16, "mbps": mbps, "rho": None, "seed": seed})
                for rho in (0.8, 1.0, 1.2):
                    specs.append({"experiment": "B", "n": n, "policy": policy, "k": 16, "mbps": None, "rho": rho, "seed": seed})
    for n in (256, 1024, 4096):
        for seed in SEEDS:
            for k in (16, 32, 64, 128):
                for policy in BOUNDED:
                    specs.append({"experiment": "C", "n": n, "policy": policy, "k": k, "mbps": None, "rho": 1.2, "seed": seed})
    return specs


def execute(spec: dict) -> dict:
    spec = dict(spec)
    spec.setdefault("heterogeneous", True)
    spec["run_id"] = run_id(spec)
    out = RUNS / spec["run_id"]
    summary_path = out / "summary.json"
    if summary_path.exists():
        return json.loads(summary_path.read_text())
    out.mkdir(parents=True, exist_ok=True)
    started = time.time()
    trace = build_trace(spec["n"], spec["seed"], heterogeneous=spec["heterogeneous"], trace_dir=out / "trace")
    if spec["mbps"] is not None:
        service = service_fps_for_mbps(spec["mbps"])
    else:
        service = trace.offered_fps / float(spec["rho"])
    result = run_replay(trace, spec["policy"], spec["k"], service)
    summary = result["summary"]
    summary.update({
        "schema": LOGIC,
        "run_id": spec["run_id"],
        "experiment": spec["experiment"],
        "n_workers": spec["n"],
        "seed": spec["seed"],
        "mbps": spec["mbps"],
        "rho_target": spec["rho"],
        "heterogeneous": spec["heterogeneous"],
        "groups": trace.groups,
        "git_commit": git_commit(),
        "gateway_sha256": gateway_sha256(),
        "link_model": "virtual_bfifo_scaled_from_9407.55_frames_per_s_at_10Mbit",
        "wall_total_s": time.time() - started,
    })
    routing = result["routing"]
    if routing:
        with (out / "routing_log.csv").open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(routing[0].keys()), lineterminator="\n")
            writer.writeheader()
            writer.writerows(routing)
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    print(
        f"done {spec['run_id']} rho={summary['rho_observed']:.3f} "
        f"fn={summary['decision_fn_rate']} digest_p95={summary['digest_p95_censored_s']} "
        f"balance={summary['balance_ok']} wall={summary['wall_s']:.1f}s",
        flush=True,
    )
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", choices=("smoke", "calibrate", "formal"), required=True)
    parser.add_argument("--workers", type=int, default=6)
    args = parser.parse_args()
    specs = matrix(args.stage if args.stage != "formal" else "formal")
    if args.stage == "formal":
        specs = matrix("formal")
    RUNS.mkdir(parents=True, exist_ok=True)
    pending = [spec for spec in specs if not (RUNS / run_id(spec) / "summary.json").exists()]
    print(f"{args.stage}: {len(specs)} specs, {len(pending)} pending", flush=True)
    if not pending:
        return
    workers = 1 if args.stage != "formal" else max(1, args.workers)
    if workers == 1:
        for spec in pending:
            execute(spec)
        return
    failed: list[str] = []
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(execute, spec) for spec in pending]
        for future in as_completed(futures):
            try:
                summary = future.result()
            except Exception as exc:
                failed.append(repr(exc))
                print(f"FAIL {exc!r}", flush=True)
                continue
            if not summary.get("balance_ok", False):
                failed.append(str(summary.get("run_id")))
                print(f"FAIL balance {summary.get('run_id')}", flush=True)
    if failed:
        raise SystemExit("failed runs: " + ", ".join(failed[:20]))


if __name__ == "__main__":
    main()
