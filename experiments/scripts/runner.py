"""B02 Part A runner: drive all (workload × view × freq × rep) cells.

Per experiments/design.md §1.5-1.6.

Usage:
    python runner.py --pilot
    python runner.py --part-a
    python runner.py --part-b
    python runner.py --all
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from dataclasses import asdict

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("runner")

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
RESULTS_DIR = os.path.expanduser("~/B02/experiments/results")
VENV = os.path.expanduser("~/B02/poc/.venv")

WORKLOADS = ["chatbot", "agentic"]
STATE_VIEWS = ["none", "coarse", "rich", "sketch"]
FREQS = [1, 10, 50]
REPS = [1, 2]
POLICIES = {
    "none": "round-robin",
    "coarse": "coarse",
    "rich": "rich",
    "sketch": "sketch",
}

INSTANCES = ["instance_0", "instance_1", "instance_2", "instance_3"]
URLS = {f"instance_{i}": f"http://127.0.0.1:{8000+i}" for i in range(4)}


def cell_dir(cell_id: str) -> str:
    d = os.path.join(RESULTS_DIR, "part_a", cell_id)
    os.makedirs(d, exist_ok=True)
    return d


def part_b_dir(cell_id: str) -> str:
    d = os.path.join(RESULTS_DIR, "part_b", cell_id)
    os.makedirs(d, exist_ok=True)
    return d


def write_dispatcher_config(out_path: str, *, state_view: str, freq_hz: float,
                            duration_s: float, cell_id: str, workload: str,
                            rep: int, policy: str, out_dir: str):
    """Emit a python file that builds a Dispatcher instance for the workload to import."""
    instances_repr = repr(INSTANCES)
    urls_repr = repr(URLS)
    body = f'''
import sys, os
sys.path.insert(0, {SCRIPTS_DIR!r})
from dispatcher import Dispatcher, DispatcherConfig

cfg = DispatcherConfig(
    instances={instances_repr},
    instance_urls={urls_repr},
    state_view={state_view!r},
    update_freq_hz={freq_hz},
    duration_s={duration_s},
    out_dir={out_dir!r},
    cell_id={cell_id!r},
    workload={workload!r},
    rep={rep},
    policy={policy!r},
)
dispatcher = Dispatcher(cfg)
'''
    with open(out_path, "w") as f:
        f.write(body)


def run_state_collector(cell_id: str, *, state_view: str, freq_hz: float,
                        duration_s: float, workload: str, rep: int,
                        policy: str) -> dict:
    """Run dispatcher.py as a background subprocess that collects state at freq_hz."""
    cd = cell_dir(cell_id)
    cmd = [
        os.path.join(VENV, "bin", "python"),
        os.path.join(SCRIPTS_DIR, "dispatcher.py"),
        "--state-view", state_view,
        "--freq-hz", str(freq_hz),
        "--duration-s", str(duration_s + 5),  # +5s buffer
        "--out-dir", cd,
        "--cell-id", cell_id,
        "--workload", workload,
        "--rep", str(rep),
        "--policy", policy,
    ]
    log.info("[collector] starting: %s", " ".join(cmd))
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    time.sleep(2.0)  # let it initialize
    return {"proc": proc, "cell_id": cell_id, "cmd": cmd}


def run_workload(cell_id: str, *, workload: str, duration_s: float, n_requests: int,
                 n_workflows: int, step_counts: list[int], tool_delay_ms: int) -> dict:
    cd = cell_dir(cell_id)
    cfg_path = os.path.join(cd, "dispatcher_config.py")
    if workload == "chatbot":
        target_rps = max(1.0, n_requests / duration_s)
        cmd = [
            os.path.join(VENV, "bin", "python"),
            os.path.join(SCRIPTS_DIR, "workloads.py"),
            "--mode", "chatbot",
            "--n-requests", str(n_requests),
            "--target-rps", str(target_rps),
            "--duration-s", str(duration_s),
            "--out-dir", cd,
            "--out-prefix", cell_id,
            "--dispatcher-config", cfg_path,
        ]
    else:
        cmd = [
            os.path.join(VENV, "bin", "python"),
            os.path.join(SCRIPTS_DIR, "workloads.py"),
            "--mode", "agentic",
            "--n-workflows", str(n_workflows),
            "--step-counts"] + [str(s) for s in step_counts] + [
            "--tool-delay-ms", str(tool_delay_ms),
            "--duration-s", str(duration_s * 2),  # agentic may run longer
            "--out-dir", cd,
            "--out-prefix", cell_id,
            "--dispatcher-config", cfg_path,
        ]
    log.info("[workload] starting: %s", " ".join(cmd))
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return {"proc": proc, "cell_id": cell_id, "cmd": cmd}


def run_part_a_cell(workload: str, state_view: str, freq_hz: float, rep: int) -> dict:
    """Run one Part A cell: collector + workload in parallel, both for the same duration."""
    cell_id = f"{workload}_{state_view}_f{freq_hz}_r{rep}"
    cd = cell_dir(cell_id)
    duration_s = 60.0  # measurement window
    warmup_s = 30.0    # warmup window
    total_s = warmup_s + duration_s

    policy = POLICIES[state_view]
    write_dispatcher_config(
        os.path.join(cd, "dispatcher_config.py"),
        state_view=state_view, freq_hz=freq_hz,
        duration_s=duration_s, cell_id=cell_id,
        workload=workload, rep=rep, policy=policy, out_dir=cd,
    )
    # 1) Start state collector (will run for total_s)
    coll = run_state_collector(
        cell_id, state_view=state_view, freq_hz=freq_hz,
        duration_s=total_s, workload=workload, rep=rep, policy=policy,
    )
    # 2) Warmup: 30s of workload that we discard
    log.info("[%s] WARMUP %ds", cell_id, warmup_s)
    if workload == "chatbot":
        n_warmup_reqs = int(25 * warmup_s)  # ~25 RPS × 30s = 750 reqs
        wk_warmup = run_workload(
            cell_id, workload=workload, duration_s=warmup_s,
            n_requests=n_warmup_reqs, n_workflows=0,
            step_counts=[], tool_delay_ms=0,
        )
    else:
        n_warmup_wf = max(2, int(0.5 * warmup_s))  # very few warmup workflows
        wk_warmup = run_workload(
            cell_id, workload=workload, duration_s=warmup_s,
            n_requests=0, n_workflows=n_warmup_wf,
            step_counts=[4, 8], tool_delay_ms=200,
        )
    rc = wk_warmup["proc"].wait()
    if rc != 0:
        log.warning("[%s] warmup returned %d, stdout:", cell_id, rc)
        log.warning(wk_warmup["proc"].stdout.read().decode()[-1000:])
    # 3) Measurement: 60s window
    log.info("[%s] MEASUREMENT %ds", cell_id, duration_s)
    if workload == "chatbot":
        n_reqs = int(25 * duration_s)
        wk = run_workload(
            cell_id, workload=workload, duration_s=duration_s,
            n_requests=n_reqs, n_workflows=0,
            step_counts=[], tool_delay_ms=0,
        )
    else:
        n_wf = 40
        wk = run_workload(
            cell_id, workload=workload, duration_s=duration_s,
            n_requests=0, n_workflows=n_wf,
            step_counts=[4, 8, 16], tool_delay_ms=200,
        )
    rc2 = wk["proc"].wait()
    log.info("[%s] measurement workload rc=%d", cell_id, rc2)
    # 4) Wait for collector to finish (allow up to total_s + 30s buffer)
    rc1 = coll["proc"].wait(timeout=total_s + 30)
    if rc1 is None:
        log.warning("[%s] collector timed out, killing", cell_id)
        coll["proc"].kill()
        rc1 = -1
    log.info("[%s] collector rc=%d", cell_id, rc1)
    summary = {
        "cell_id": cell_id,
        "workload": workload,
        "state_view": state_view,
        "freq_hz": freq_hz,
        "rep": rep,
        "policy": policy,
        "warmup_rc": rc,
        "measure_rc": rc2,
        "collector_rc": rc1,
    }
    summary_path = os.path.join(cd, "summary.json")
    with open(summary_path, "w") as f:
        json.dump(summary, f, indent=2)
    return summary


def run_part_a():
    cells = []
    failed = []
    for workload in WORKLOADS:
        for sv in STATE_VIEWS:
            for freq in FREQS:
                for rep in REPS:
                    try:
                        s = run_part_a_cell(workload, sv, freq, rep)
                        cells.append(s)
                    except Exception as e:
                        log.exception("cell failed")
                        failed.append({"workload": workload, "sv": sv, "freq": freq, "rep": rep, "error": str(e)})
    summary_path = os.path.join(RESULTS_DIR, "part_a_summary.json")
    with open(summary_path, "w") as f:
        json.dump({"cells": cells, "failed": failed}, f, indent=2)
    log.info("Part A: %d cells done, %d failed", len(cells), len(failed))


def run_pilot():
    """Small-scale pilot to confirm setup works. 1 workload × 1 view × 1 freq × 1 rep."""
    log.info("=== PILOT ===")
    s = run_part_a_cell("chatbot", "coarse", 1.0, 1)
    log.info("pilot result: %s", s)
    return s


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--pilot", action="store_true")
    ap.add_argument("--part-a", action="store_true")
    args = ap.parse_args()
    if args.pilot:
        run_pilot()
    elif args.part_a:
        run_part_a()
    else:
        run_pilot()