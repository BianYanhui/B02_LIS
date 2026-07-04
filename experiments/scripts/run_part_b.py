"""B02 Part B: scalable state maintenance stress test (logical emulator mode).

Per design.md §1.5-1.6: replay / synthesize state view payloads at scale
N=4,16,64,128,256 with f=1,10,50Hz and view=Coarse/Rich/Sketch.

The "logical Instance" is a Python object that periodically constructs
a state view of the configured type and pushes it to a single dispatcher
that maintains the same state table as Part A.

This part does NOT need vLLM running.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import random
import sys
import time
from dataclasses import dataclass

import orjson

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("part_b")

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
RESULTS_DIR = os.path.expanduser("~/B02/experiments/results")
VENV = os.path.expanduser("~/B02/poc/.venv")


# ---------------------------------------------------------------------------
# Synthetic state view generator (matches Part A's measured size distribution)
# ---------------------------------------------------------------------------

def make_workflow_records(k: int, n_instances_logical: int) -> list[dict]:
    """Create K workflow records for the rich view."""
    out = []
    for i in range(k):
        progress = random.random()
        out.append({
            "workflow_id": f"wf_{i:05d}",
            "current_step_id": int(progress * 8),
            "total_steps": 8,
            "workflow_progress": progress,
            "last_assigned_instance": f"instance_{random.randint(0, n_instances_logical-1)}",
            "assigned_instance_history": [
                f"instance_{random.randint(0, n_instances_logical-1)}" for _ in range(random.randint(1, 3))
            ],
            "tool_execution_status": random.choice(["idle", "running", "done"]),
            "last_tool_name": random.choice(["search", "calc", "code", "db"]),
            "last_tool_latency_ms": random.uniform(50, 500),
            "tool_result_context_size": random.randint(0, 4096),
            "tool_result_context_type": random.choice(["text", "code", "json"]),
            "tool_result_hash": "deadbeef",
            "workflow_to_instance_affinity": {
                f"instance_{j}": random.randint(0, 5) for j in range(n_instances_logical)
            },
            "workflow_start_time_ns": 0,
            "last_step_finish_time_ns": 0,
        })
    return out


def make_coarse_view(instance_id: str, ts_ns: int) -> dict:
    return {
        "instance_id": instance_id,
        "timestamp_ns": ts_ns,
        "num_requests_waiting": random.randint(0, 50),
        "num_requests_running": random.randint(0, 20),
        "kv_cache_usage_perc": random.random() * 0.9,
        "gpu_cache_usage_perc": random.random() * 0.9,
        "prompt_tokens_total": random.randint(1000, 1000000),
        "generation_tokens_total": random.randint(1000, 1000000),
        "prefix_cache_hits_total": random.randint(100, 10000),
        "prefix_cache_queries_total": random.randint(100, 10000),
        "request_success_total": random.randint(0, 100000),
        "num_preemptions_total": random.randint(0, 100),
    }


def make_rich_view(instance_id: str, ts_ns: int, n_workflows: int,
                   n_instances_logical: int) -> dict:
    coarse = make_coarse_view(instance_id, ts_ns)
    workflows = make_workflow_records(n_workflows, n_instances_logical)
    return {
        "instance_id": instance_id,
        "timestamp_ns": ts_ns,
        "runtime": {
            **coarse,
            "latency_summary": {
                "ttft_p50": random.uniform(20, 200),
                "ttft_p95": random.uniform(200, 1000),
                "tpot_p50": random.uniform(10, 50),
                "tpot_p95": random.uniform(50, 200),
                "queue_time_p95": random.uniform(5, 100),
                "prefill_time_p95": random.uniform(50, 300),
                "decode_time_p95": random.uniform(200, 2000),
            },
        },
        "workflows": workflows,
    }


def make_sketch_view(instance_id: str, ts_ns: int, n_workflows: int,
                     n_instances_logical: int) -> dict:
    return {
        "instance_id": instance_id,
        "timestamp_ns": ts_ns,
        "runtime": {
            "kv_cache_usage_q": random.randint(0, 90),
            "prefix_cache_hit_rate_q": random.randint(0, 100),
            "load_q": random.randint(0, 100),
        },
        "workflow_sketch": {
            "active_workflow_count": n_workflows,
            "avg_progress_q": random.randint(0, 100),
            "max_progress_q": random.randint(0, 100),
            "tool_status_bitset": random.randint(0, 0xFFFF),
            "tool_context_avail_bitmap": random.randint(0, 0xFFFF),
            "affinity_hot_instance_counts": [random.randint(0, 50) for _ in range(n_instances_logical)],
            "recent_workflow_hashes": [random.randint(0, 2**32) for _ in range(4)],
        },
    }


# ---------------------------------------------------------------------------
# Stress cell: N logical instances × f Hz × view × duration
# ---------------------------------------------------------------------------

async def stress_cell(n: int, f_hz: float, view: str, rep: int, duration_s: float,
                      out_dir: str, cell_id: str):
    os.makedirs(out_dir, exist_ok=True)
    log.info("=== cell %s n=%d f=%g view=%s ===", cell_id, n, f_hz, view)
    t_start = time.time()

    # Build N logical instances; each is a coroutine that publishes at f_hz
    instances = [f"instance_{i}" for i in range(n)]
    state_views: dict[str, dict] = {}
    metrics_log = []
    n_workflows_per_instance = 8  # how many workflows each instance reports

    stop = asyncio.Event()

    async def instance_loop(inst_id: str):
        next_t = time.time()
        period = 1.0 / f_hz
        while not stop.is_set():
            ts = time.time_ns()
            t_build0 = time.perf_counter_ns()
            if view == "coarse":
                v = make_coarse_view(inst_id, ts)
            elif view == "rich":
                v = make_rich_view(inst_id, ts, n_workflows_per_instance, n)
            elif view == "sketch":
                v = make_sketch_view(inst_id, ts, n_workflows_per_instance, n)
            else:
                raise ValueError(view)
            t_build1 = time.perf_counter_ns()
            t_ser0 = time.perf_counter_ns()
            blob = orjson.dumps(v)
            t_ser1 = time.perf_counter_ns()
            t_de0 = time.perf_counter_ns()
            orjson.loads(blob)
            t_de1 = time.perf_counter_ns()
            t_merg0 = time.perf_counter_ns()
            state_views[inst_id] = v
            t_merg1 = time.perf_counter_ns()
            t_end = time.perf_counter_ns()
            rec = {
                "ts_ns": ts,
                "instance_id": inst_id,
                "size_bytes": len(blob),
                "build_us": (t_build1 - t_build0) / 1e3,
                "ser_us": (t_ser1 - t_ser0) / 1e3,
                "deser_us": (t_de1 - t_de0) / 1e3,
                "merge_us": (t_merg1 - t_merg0) / 1e3,
                "total_us": (t_end - t_build0) / 1e3,
            }
            metrics_log.append(rec)
            next_t += period
            sleep_for = next_t - time.time()
            if sleep_for > 0:
                await asyncio.sleep(sleep_for)
            else:
                next_t = time.time()

    tasks = [asyncio.create_task(instance_loop(i)) for i in instances]
    # also do dispatch queries (probe dispatcher CPU under load)
    dispatch_latencies = []
    n_dispatch = 0
    async def dispatch_loop():
        nonlocal n_dispatch
        nonlocal dispatch_latencies
        while not stop.is_set():
            await asyncio.sleep(0.005)  # 200 QPS dispatch probes
            t0 = time.perf_counter_ns()
            # simulate a simple coarse-score policy
            if state_views:
                scores = []
                for inst in instances:
                    sv = state_views.get(inst, {})
                    rt = sv.get("runtime", sv)
                    w = rt.get("num_requests_waiting", rt.get("load_q", 0))
                    r = rt.get("num_requests_running", 0)
                    kv = rt.get("kv_cache_usage_perc", rt.get("kv_cache_usage_q", 0) / 100.0)
                    scores.append((w + r + kv, inst))
                scores.sort()
                _ = scores[0][1]  # chosen instance
            t1 = time.perf_counter_ns()
            dispatch_latencies.append((t1 - t0) / 1e3)  # us
            n_dispatch += 1
    dl = asyncio.create_task(dispatch_loop())

    await asyncio.sleep(duration_s)
    stop.set()
    await asyncio.gather(*tasks, return_exceptions=True)
    dl.cancel()
    try:
        await dl
    except asyncio.CancelledError:
        pass

    # Aggregate
    sizes = [m["size_bytes"] for m in metrics_log]
    n_skipped = sum(1 for m in metrics_log if m["total_us"] > 1e6 / f_hz)  # missed deadlines
    if sizes:
        sizes_sorted = sorted(sizes)
        size_summary = {
            "avg": sum(sizes) / len(sizes),
            "p50": sizes_sorted[len(sizes) // 2],
            "p95": sizes_sorted[int(len(sizes) * 0.95)],
            "p99": sizes_sorted[int(len(sizes) * 0.99)] if len(sizes) > 100 else 0,
            "min": min(sizes),
            "max": max(sizes),
        }
    else:
        size_summary = {"avg": 0, "p50": 0, "p95": 0, "p99": 0, "min": 0, "max": 0}
    build_us = [m["build_us"] for m in metrics_log]
    ser_us = [m["ser_us"] for m in metrics_log]
    deser_us = [m["deser_us"] for m in metrics_log]
    merge_us = [m["merge_us"] for m in metrics_log]
    total_us = [m["total_us"] for m in metrics_log]
    def q(xs, p):
        if not xs: return 0
        xs = sorted(xs)
        return xs[max(0, min(len(xs) - 1, int(round(p/100*(len(xs)-1)))))]
    dispatch_q = sorted(dispatch_latencies)
    def qd(p): return dispatch_q[max(0, min(len(dispatch_q)-1, int(round(p/100*(len(dispatch_q)-1)))))] if dispatch_q else 0

    summary = {
        "cell_id": cell_id,
        "n": n,
        "f_hz": f_hz,
        "view": view,
        "rep": rep,
        "duration_s": duration_s,
        "actual_elapsed_s": time.time() - t_start,
        "n_state_updates": len(metrics_log),
        "n_dispatch_queries": n_dispatch,
        "n_deadline_missed": n_skipped,
        "size": size_summary,
        "build_us_p95": q(build_us, 95),
        "ser_us_p95": q(ser_us, 95),
        "deser_us_p95": q(deser_us, 95),
        "merge_us_p95": q(merge_us, 95),
        "total_us_p95": q(total_us, 95),
        "dispatch_latency_us_p50": qd(50),
        "dispatch_latency_us_p95": qd(95),
        "dispatch_latency_us_p99": qd(99),
    }
    with open(os.path.join(out_dir, "summary.json"), "w") as f:
        json.dump(summary, f, indent=2)
    with open(os.path.join(out_dir, "metrics.jsonl"), "w") as f:
        for m in metrics_log:
            f.write(orjson.dumps(m).decode() + "\n")
    log.info("cell %s done: %d updates, %d dispatches, %d missed, p95 size=%d",
             cell_id, len(metrics_log), n_dispatch, n_skipped, size_summary["p95"])
    return summary


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

NS = [4, 16, 64, 128, 256]
FS = [1, 10, 50]
VIEWS = ["coarse", "rich", "sketch"]
REPS = [1, 2]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--single", nargs=4, metavar=("N", "F", "VIEW", "REP"))
    ap.add_argument("--duration-s", type=float, default=30.0)
    args = ap.parse_args()

    if args.single:
        n, f, view, rep = args.single
        cell_id = f"n{n}_f{f}_v{view}_r{rep}"
        cd = os.path.join(RESULTS_DIR, "part_b", cell_id)
        asyncio.run(stress_cell(int(n), float(f), view, int(rep),
                                args.duration_s, cd, cell_id))
        return

    if args.all:
        summaries = []
        for n in NS:
            for f in FS:
                for v in VIEWS:
                    for rep in REPS:
                        cell_id = f"n{n}_f{f}_v{v}_r{rep}"
                        cd = os.path.join(RESULTS_DIR, "part_b", cell_id)
                        try:
                            s = asyncio.run(stress_cell(n, f, v, rep,
                                                        args.duration_s, cd, cell_id))
                            summaries.append(s)
                        except Exception as e:
                            log.exception("cell %s failed", cell_id)
                            summaries.append({"cell_id": cell_id, "error": str(e)})
        with open(os.path.join(RESULTS_DIR, "part_b_summary.json"), "w") as f:
            json.dump(summaries, f, indent=2)
        log.info("Part B done: %d cells", len(summaries))


if __name__ == "__main__":
    main()