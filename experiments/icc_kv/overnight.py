"""Run the ICC matrix in order, resuming from whatever is already on disk.

Capacity is left alone if a sweep is already running. Later stages start only
after capacity_summary.json exists. A failed stage is logged and the next
stage still starts, so one cell cannot stop the night.
"""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

from experiments.icc_kv.burst import run_burst
from experiments.icc_kv.e2e import run_e2e
from experiments.icc_kv.replay import run_scale
from experiments.icc_kv.runtime import OUT
import asyncio

ROOT = Path("/home/byh/B02")
PY = ROOT / "poc/.venv/bin/python"
SUMMARY = OUT / "capacity" / "capacity_summary.json"
LOG = OUT / "overnight.log"


def log(message: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}"
    print(line, flush=True)
    with LOG.open("a") as handle:
        handle.write(line + "\n")


def capacity_pids() -> list[int]:
    try:
        text = subprocess.check_output(["pgrep", "-af", "experiments.icc_kv capacity"], text=True)
    except subprocess.CalledProcessError:
        return []
    pids = []
    for line in text.splitlines():
        if "overnight" in line or "pgrep" in line:
            continue
        pid = int(line.split()[0])
        pids.append(pid)
    return pids


def wait_for_capacity() -> float:
    while not SUMMARY.exists():
        if not capacity_pids():
            log("capacity process missing; starting the fixed-capacity sweep")
            subprocess.check_call([
                str(PY), "-m", "experiments.icc_kv", "capacity",
                "--rates", "500,1000,2000,4000,8000,16000,32000,64000",
                "--reps", "3", "--seconds", "120",
                "--out-dir", str(OUT / "capacity"),
            ], cwd=ROOT)
        else:
            log(f"waiting for capacity sweep pids={capacity_pids()}")
            time.sleep(30)
    payload = json.loads(SUMMARY.read_text())
    capacity = float(payload.get("capacity_events_per_s") or 0)
    if capacity <= 0:
        stable = [row["rate_target"] for row in payload.get("rows", []) if row.get("gap_ratio", 1) == 0]
        capacity = float(max(stable) if stable else 0)
    if capacity <= 0:
        raise RuntimeError(f"no usable capacity in {SUMMARY}")
    log(f"capacity C={capacity} events/s")
    return capacity


def stage(name: str, fn) -> None:
    try:
        log(f"start {name}")
        fn()
        log(f"done {name}")
    except Exception as exc:
        log(f"FAILED {name}: {exc!r}")


def main() -> None:
    log("overnight queue starting")
    capacity = wait_for_capacity()
    trace = OUT / "trace" / "events.csv"
    if not trace.exists():
        raise RuntimeError(f"missing trace {trace}")

    def scale(correlated: bool, folder: str) -> None:
        asyncio.run(run_scale(
            trace, capacity, [0.5, 0.8, 1.0, 1.2],
            ["FullSync", "RateFIFO", "StaticSemantic", "Adaptive"],
            4, correlated, 5, 180.0, OUT / folder,
        ))

    stage("scale-independent", lambda: scale(False, "scale_independent"))
    stage("scale-correlated", lambda: scale(True, "scale_correlated"))
    stage("burst", lambda: asyncio.run(run_burst(
        trace, capacity, [5.0, 10.0],
        ["FullSync", "RateFIFO", "StaticSemantic", "Adaptive"],
        5, OUT / "burst",
    )))
    log("starting four vLLM servers for end-to-end runs")
    subprocess.check_call([str(ROOT / "experiments/4t4/restart_4t4.sh"), "0.40", "6144"], cwd=ROOT)
    stage("e2e", lambda: asyncio.run(run_e2e(
        trace, capacity, ["normal", "near", "burst"],
        ["FullSync", "StaticSemantic", "Adaptive", "Ideal"],
        5, 500, 104544, OUT / "e2e",
    )))
    stage("ablation", lambda: asyncio.run(run_e2e(
        trace, capacity, ["near", "burst"],
        ["FullSync", "StaticSemantic", "AdaptiveNoPriority", "Adaptive"],
        5, 500, 104544, OUT / "ablation",
    )))
    log("overnight queue finished")


if __name__ == "__main__":
    main()
