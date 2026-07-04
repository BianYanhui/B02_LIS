"""B02 Part A grid runner: iterate (workload × view × freq × rep).

Replaces runner.py --part-a. Uses run_cell.py for each cell.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("part_a")

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
RESULTS_DIR = os.path.expanduser("~/B02/experiments/results")
VENV = os.path.expanduser("~/B02/poc/.venv")

WORKLOADS = ["chatbot", "agentic"]
STATE_VIEWS = ["none", "coarse", "rich", "sketch"]
FREQS = [1.0, 10.0, 50.0]
REPS = [1, 2]


def run_one_cell(workload: str, state_view: str, freq_hz: float, rep: int,
                 warmup_s: float = 30.0, duration_s: float = 60.0) -> dict:
    cell_id = f"{workload}_{state_view}_f{freq_hz:g}_r{rep}"
    cd = os.path.join(RESULTS_DIR, "part_a", cell_id)
    cmd = [
        os.path.join(VENV, "bin", "python"),
        os.path.join(SCRIPTS_DIR, "run_cell.py"),
        "--cell-id", cell_id,
        "--out-dir", cd,
        "--workload", workload,
        "--state-view", state_view,
        "--freq-hz", str(freq_hz),
        "--rep", str(rep),
        "--warmup-s", str(warmup_s),
        "--duration-s", str(duration_s),
    ]
    log.info("[cell %s] starting", cell_id)
    t0 = time.time()
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=duration_s + warmup_s + 300)
    elapsed = time.time() - t0
    if proc.returncode != 0:
        log.error("[cell %s] rc=%d elapsed=%.1fs", cell_id, proc.returncode, elapsed)
        log.error("STDOUT tail: %s", proc.stdout[-1000:])
        log.error("STDERR tail: %s", proc.stderr[-1000:])
        return {"cell_id": cell_id, "rc": proc.returncode, "elapsed_s": elapsed, "error": "nonzero rc"}
    summary = {"cell_id": cell_id, "rc": 0, "elapsed_s": elapsed}
    summary_path = os.path.join(cd, "summary.json")
    if os.path.exists(summary_path):
        with open(summary_path) as f:
            summary.update(json.load(f))
    log.info("[cell %s] OK rc=0 elapsed=%.1fs", cell_id, elapsed)
    return summary


def run_pilot():
    """One pilot cell to confirm setup works."""
    log.info("=== PILOT ===")
    s = run_one_cell("chatbot", "coarse", 1.0, 1)
    log.info("pilot: %s", s)
    return s


def run_part_a():
    cells = []
    failed = []
    t_start = time.time()
    for workload in WORKLOADS:
        for sv in STATE_VIEWS:
            for freq in FREQS:
                for rep in REPS:
                    try:
                        s = run_one_cell(workload, sv, freq, rep)
                        if s.get("rc") == 0:
                            cells.append(s)
                        else:
                            failed.append(s)
                    except subprocess.TimeoutExpired as e:
                        log.error("cell timed out: %s", e)
                        failed.append({"cell_id": f"{workload}_{sv}_f{freq:g}_r{rep}", "error": "timeout"})
                    except Exception as e:
                        log.exception("cell failed")
                        failed.append({"cell_id": f"{workload}_{sv}_f{freq:g}_r{rep}", "error": str(e)})
                    log.info("=== progress: %d cells done, %d failed, elapsed %.1f min ===",
                             len(cells), len(failed), (time.time() - t_start) / 60)
    summary_path = os.path.join(RESULTS_DIR, "part_a_summary.json")
    with open(summary_path, "w") as f:
        json.dump({"cells": cells, "failed": failed, "total_elapsed_s": time.time() - t_start}, f, indent=2)
    log.info("Part A done: %d ok, %d failed", len(cells), len(failed))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--pilot", action="store_true")
    ap.add_argument("--part-a", action="store_true")
    ap.add_argument("--single", nargs=5, metavar=("WORKLOAD", "SV", "FREQ", "REP", "DUR"),
                    help="run a single cell: workload sv freq rep duration_s")
    args = ap.parse_args()
    if args.pilot:
        run_pilot()
    elif args.single:
        wl, sv, freq, rep, dur = args.single
        run_one_cell(wl, sv, float(freq), int(rep), duration_s=float(dur))
    elif args.part_a:
        run_part_a()
    else:
        ap.print_help()