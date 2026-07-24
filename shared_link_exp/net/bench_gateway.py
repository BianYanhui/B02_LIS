#!/usr/bin/env python3
"""Single-core microbenchmark for the v3 gateway semantic enqueue path.

This intentionally benchmarks Relay.enqueue rather than TCP or tc: the live
experiments measure those end-to-end.  Keeping the benchmark in-process lets
us report the incremental CPU/memory cost of the gateway's merge, priority,
replica-cap, adaptive, and global Top-K logic without confusing it with link
queueing.  It uses only the stdlib and imports the production relay module.
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import resource
import statistics
import time
from pathlib import Path
from types import SimpleNamespace


def load_relay(path: Path):
    spec = importlib.util.spec_from_file_location("gateway_relay_bench", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def rss_kb() -> int:
    for line in Path("/proc/self/status").read_text().splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1])
    return 0


def percentile(values: list[float], p: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, round((p / 100.0) * (len(ordered) - 1)))
    return ordered[index]


def run_count(module, count: int, batch: int, global_topk: int) -> dict:
    args = SimpleNamespace(max_queue=200, tau=30.0, util_lambda=28.0, gate=0.050)
    relay = module.Relay(args)
    relay.current_cell = 1
    relay.merge = True
    relay.priority = True
    relay.adaptive = True
    relay.dedup = 2
    relay.global_topk = global_topk
    before_cpu = resource.getrusage(resource.RUSAGE_SELF)
    started = time.perf_counter()
    batch_ns: list[float] = []
    for first in range(0, count, batch):
        stop = min(first + batch, count)
        batch_start = time.perf_counter_ns()
        for seq in range(first, stop):
            digest = seq % 4096
            kind = module.K_TOMB if seq % 19 == 0 else module.K_UP
            payload = module.frame(kind, seq % 4, 1, seq, 2048 + (seq % 3) * 512, digest, time.time())
            relay.enqueue(payload, kind, seq % 4, seq, 2048 + (seq % 3) * 512, digest, time.time())
        batch_ns.append((time.perf_counter_ns() - batch_start) / (stop - first))
    wall_s = time.perf_counter() - started
    after_cpu = resource.getrusage(resource.RUSAGE_SELF)
    cpu_s = (after_cpu.ru_utime + after_cpu.ru_stime) - (before_cpu.ru_utime + before_cpu.ru_stime)
    return {
        "input_updates": count,
        "achieved_updates_per_s": count / wall_s,
        "enqueue_p50_us": percentile(batch_ns, 50) / 1000.0,
        "enqueue_p95_us": percentile(batch_ns, 95) / 1000.0,
        "enqueue_p99_us": percentile(batch_ns, 99) / 1000.0,
        "one_core_cpu_utilization_pct": 100.0 * cpu_s / wall_s,
        "rss_kb": rss_kb(),
        "max_gateway_queue_depth": relay.maxq,
        "global_topk": global_topk,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--relay", type=Path, default=Path(__file__).with_name("gateway_relay.py"))
    parser.add_argument("--batch", type=int, default=1000)
    parser.add_argument("--global-topk", type=int, default=32)
    args = parser.parse_args()
    try:
        os.sched_setaffinity(0, {0})
        affinity = "cpu0"
    except (AttributeError, OSError):
        affinity = "unavailable"
    module = load_relay(args.relay)
    print("affinity," + affinity)
    print("input_updates,achieved_updates_per_s,enqueue_p50_us,enqueue_p95_us,enqueue_p99_us,one_core_cpu_utilization_pct,rss_kb,max_gateway_queue_depth,global_topk")
    for count in (1_000, 5_000, 10_000, 50_000, 100_000):
        row = run_count(module, count, args.batch, args.global_topk)
        print(",".join(str(row[key]) for key in (
            "input_updates", "achieved_updates_per_s", "enqueue_p50_us", "enqueue_p95_us",
            "enqueue_p99_us", "one_core_cpu_utilization_pct", "rss_kb",
            "max_gateway_queue_depth", "global_topk",
        )))


if __name__ == "__main__":
    main()
