"""Tier-9 (stress test): logical emulator mode, scales beyond 8 instances.

Uses measured payload distributions from Part A (or B02 experiments) at scale.
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
from collections import defaultdict
from statistics import mean, median, stdev
import orjson

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("stress")

sys.path.insert(0, os.path.expanduser("~/B02/experiments/scripts"))
sys.path.insert(0, os.path.expanduser("~/B02/tradeoff_experiments/scripts"))

OUT = os.path.expanduser("~/B02/full_experiment/results")


def make_coarse(instance_id, ts_ns):
    return {
        "instance_id": instance_id, "timestamp_ns": ts_ns,
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
        "recent_request_latency_p50": random.uniform(50, 300),
        "recent_request_latency_p95": random.uniform(200, 1500),
    }


def make_rich(instance_id, ts_ns, n_workflows=8, n_instances_logical=4):
    coarse = make_coarse(instance_id, ts_ns)
    workflows = []
    for i in range(n_workflows):
        workflows.append({
            "workflow_id": f"wf_{i:05d}", "current_step_id": random.randint(0, 8),
            "total_steps": 8, "workflow_progress": random.random(),
            "last_assigned_instance": f"instance_{random.randint(0, n_instances_logical-1)}",
            "assigned_instance_history": [
                f"instance_{random.randint(0, n_instances_logical-1)}"
                for _ in range(random.randint(1, 5))],
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
            "latency_sensitive_flag": random.choice([0, 1]),
        })
    return {
        "instance_id": instance_id, "timestamp_ns": ts_ns,
        "runtime": {**coarse,
                    "latency_summary": {
                        "ttft_p50": random.uniform(20, 200),
                        "ttft_p95": random.uniform(200, 1000),
                        "tpot_p50": random.uniform(10, 50),
                        "tpot_p95": random.uniform(50, 200),
                    }},
        "workflows": workflows,
    }


def make_sketch(instance_id, ts_ns, n_workflows=8, n_instances_logical=4):
    K = n_workflows
    if K > 0:
        avg_progress_q = int(random.uniform(0, 1) * 100) & 0xFF
        max_progress_q = int(random.uniform(0, 1) * 100) & 0xFF
        ts_bits = random.randint(0, 0xFFFF)
        ctx_bits = random.randint(0, 0xFFFF)
        affinity = [random.randint(0, 50) for _ in range(n_instances_logical)]
    else:
        avg_progress_q = max_progress_q = ts_bits = ctx_bits = 0
        affinity = [0] * n_instances_logical
    return {
        "instance_id": instance_id, "timestamp_ns": ts_ns,
        "runtime": {
            "kv_cache_usage_q": int(random.random() * 100),
            "prefix_cache_hit_rate_q": int(random.uniform(0, 100)),
            "load_q": random.randint(0, 100),
        },
        "workflow_sketch": {
            "active_workflow_count": K,
            "avg_progress_q": avg_progress_q,
            "max_progress_q": max_progress_q,
            "tool_status_bitset": ts_bits & 0xFFFF,
            "tool_context_avail_bitmap": ctx_bits & 0xFFFF,
            "affinity_hot_instance_counts": affinity,
            "recent_workflow_hashes": [random.randint(0, 2**32) for _ in range(4)],
            "latency_sensitive_count": random.randint(0, 5),
        },
    }


async def stress_cell(n, f_hz, view, rep, duration_s, n_workflows_per_inst=8):
    os.makedirs(f"{OUT}/stress", exist_ok=True)
    cell_id = f"n{n}_f{f_hz}_v{view}_r{rep}"
    log.info("=== cell %s (N=%d, f=%g, view=%s) ===", cell_id, n, f_hz, view)
    t_start = time.time()

    instances = [f"instance_{i}" for i in range(n)]
    state_views = {}
    n_updates = 0
    sizes = []
    ser_us_l, deser_us_l, merge_us_l, total_us_l = [], [], [], []
    next_t = time.time()
    period = 1.0 / f_hz
    end = t_start + duration_s

    while time.time() < end:
        ts = time.time_ns()
        ts_build0 = time.perf_counter_ns()
        if view == "coarse":
            v = make_coarse(instances[0], ts)
        elif view == "rich":
            v = make_rich(instances[0], ts, n_workflows=n_workflows_per_inst,
                          n_instances_logical=n)
        else:  # sketch
            v = make_sketch(instances[0], ts, n_workflows=n_workflows_per_inst,
                            n_instances_logical=n)
        ts_build1 = time.perf_counter_ns()
        ts_ser0 = time.perf_counter_ns()
        blob = orjson.dumps(v)
        ts_ser1 = time.perf_counter_ns()
        ts_de0 = time.perf_counter_ns()
        orjson.loads(blob)
        ts_de1 = time.perf_counter_ns()
        ts_merg0 = time.perf_counter_ns()
        for i, inst in enumerate(instances):
            if view == "coarse":
                state_views[inst] = make_coarse(inst, ts)
            elif view == "rich":
                state_views[inst] = make_rich(inst, ts, n_workflows=n_workflows_per_inst,
                                              n_instances_logical=n)
            else:
                state_views[inst] = make_sketch(inst, ts, n_workflows=n_workflows_per_inst,
                                                n_instances_logical=n)
        ts_merg1 = time.perf_counter_ns()
        ts_end = time.perf_counter_ns()

        sz = len(blob)
        sizes.append(sz)
        ser_us_l.append((ts_ser1 - ts_ser0) / 1e3)
        deser_us_l.append((ts_de1 - ts_de0) / 1e3)
        merge_us_l.append((ts_merg1 - ts_merg0) / 1e3)
        total_us_l.append((ts_end - ts_build0) / 1e3)
        n_updates += 1

        # also serialize all instances to traffic
        # (we report the avg size × N × f as traffic)

        next_t += period
        sf = next_t - time.time()
        if sf > 0:
            await asyncio.sleep(sf)
        else:
            next_t = time.time()

    # Dispatch load
    dispatch_latencies = []
    while True:
        await asyncio.sleep(0.005)
        if time.time() > end + 0.5:
            break
        d0 = time.perf_counter_ns()
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
        d1 = time.perf_counter_ns()
        dispatch_latencies.append((d1 - d0) / 1e3)

    elapsed = time.time() - t_start
    avg_size = mean(sizes) if sizes else 0
    p95_size = sorted(sizes)[int(len(sizes)*0.95)] if sizes else 0
    p99_size = sorted(sizes)[int(len(sizes)*0.99)] if sizes else 0
    traffic = avg_size * f_hz * n

    def q(xs, p):
        if not xs: return 0
        xs = sorted(xs)
        idx = max(0, min(len(xs)-1, int(round(p / 100.0 * (len(xs) - 1)))))
        return xs[idx]
    summary = {
        "cell_id": cell_id, "n": n, "f_hz": f_hz, "view": view, "rep": rep,
        "duration_s": duration_s, "n_updates": n_updates,
        "size_avg": avg_size, "size_p95": p95_size, "size_p99": p99_size,
        "size_max": max(sizes) if sizes else 0,
        "traffic_Bps": traffic, "traffic_MBps": traffic / 1e6,
        "ser_us_p95": q(ser_us_l, 95),
        "deser_us_p95": q(deser_us_l, 95),
        "merge_us_p95": q(merge_us_l, 95),
        "total_us_p95": q(total_us_l, 95),
        "dispatch_p50": q(dispatch_latencies, 50),
        "dispatch_p95": q(dispatch_latencies, 95),
        "dispatch_p99": q(dispatch_latencies, 99),
        "dispatch_q_count": len(dispatch_latencies),
        "actual_elapsed_s": elapsed,
        "achieved_hz": n_updates / elapsed,
    }
    out_dir = f"{OUT}/stress/{cell_id}"
    os.makedirs(out_dir, exist_ok=True)
    with open(f"{out_dir}/summary.json", "w") as f:
        json.dump(summary, f, indent=2)
    log.info("stress %s: %d updates p95=%.0fus total_us=%.0fus, traffic=%.0f KB/s",
             cell_id, n_updates, summary["dispatch_p95"], summary["total_us_p95"],
             traffic / 1000)
    return summary


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--N", nargs="+", type=int, default=[4, 16, 64, 128, 256])
    ap.add_argument("--freq", nargs="+", type=float, default=[10, 50])
    ap.add_argument("--views", nargs="+", default=["coarse", "rich", "sketch"])
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--duration-s", type=int, default=30)
    args = ap.parse_args()

    cells = []
    t_start = time.time()
    total = len(args.N) * len(args.freq) * len(args.views) * args.reps
    i = 0
    for n in args.N:
        for f_hz in args.freq:
            for view in args.views:
                for rep in range(1, args.reps + 1):
                    i += 1
                    log.info("progress: %d/%d starting", i, total)
                    try:
                        s = asyncio.run(stress_cell(n, f_hz, view, rep, args.duration_s))
                        cells.append(s)
                    except Exception as e:
                        log.exception("stress cell failed")
                        cells.append({"error": str(e), "n": n, "f_hz": f_hz,
                                       "view": view, "rep": rep})
                    log.info("progress: %d/%d done, %.1f min",
                             i, total, (time.time() - t_start) / 60)
    with open(f"{OUT}/stress_summary.json", "w") as f:
        json.dump(cells, f, indent=2)
    log.info("Saved %d stress summaries", len(cells))


if __name__ == "__main__":
    main()