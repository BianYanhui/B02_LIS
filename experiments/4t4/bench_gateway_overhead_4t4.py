#!/usr/bin/env python3
"""Single-core control-plane benchmark for the frozen 4xT4 gateway relay.

This measures only Relay.enqueue: frame construction, TCP/tc transmission,
and JSON-line telemetry are deliberately outside the timed region.  The live
formal experiment already measures the latter end-to-end path.  The relay is
configured with the frozen Adaptive parameters (merge, tombstone priority,
replica cap 2, global top-K 16, tau 30 s, lambda 16, delay gate 2 s, queue
gate 8 frames, and max queue 4096) so the benchmark quantifies the incremental
CPU/memory cost of semantic admission rather than a synthetic policy.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.util
import json
import os
import resource
import statistics
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any


def load_relay(path: Path) -> Any:
    spec = importlib.util.spec_from_file_location("gateway_relay_4t4_bench", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import relay: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    return ordered[min(len(ordered) - 1, round((q / 100.0) * (len(ordered) - 1)))]


def rss_kb() -> int:
    for line in Path("/proc/self/status").read_text().splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1])
    raise RuntimeError("VmRSS unavailable")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def make_relay(module: Any) -> Any:
    args = SimpleNamespace(
        max_queue=4096,
        tau=30.0,
        util_lambda=16.0,
        gate=2.0,
        adaptive_queue_gate=8,
    )
    relay = module.Relay(args)
    relay.current_cell = 1
    relay.mode = module.MODE_ADAPTIVE
    relay.merge = True
    relay.priority = True
    relay.adaptive = True
    relay.dedup = 2
    relay.global_topk = 16
    relay.max_inflight = 4
    # The formal run separately records JSON-line telemetry.  Disable it here
    # to isolate controller decision/metadata work from terminal/log-driver I/O.
    relay.emit_update = lambda *args, **kwargs: None
    return relay


def run_once(module: Any, update_count: int, repetition: int) -> dict[str, Any]:
    relay = make_relay(module)
    baseline_rss = rss_kb()
    latencies_ns: list[int] = []
    cpu_before = resource.getrusage(resource.RUSAGE_SELF)
    wall_before = time.perf_counter()
    coverages = (1024, 2048, 4096)
    tombstones = 0
    for seq in range(update_count):
        kind = module.K_TOMB if seq % 19 == 0 else module.K_UP
        tombstones += int(kind == module.K_TOMB)
        coverage = coverages[seq % len(coverages)]
        digest = seq % 4096
        generated_at = time.time()
        payload = module.frame(kind, seq % 4, 1, seq, coverage, digest, generated_at)
        started_ns = time.perf_counter_ns()
        relay.enqueue(payload, kind, seq % 4, seq, coverage, digest, generated_at)
        latencies_ns.append(time.perf_counter_ns() - started_ns)
    wall_s = time.perf_counter() - wall_before
    cpu_after = resource.getrusage(resource.RUSAGE_SELF)
    cpu_s = (cpu_after.ru_utime + cpu_after.ru_stime) - (cpu_before.ru_utime + cpu_before.ru_stime)
    pending = len(relay.queue) + len(relay.pqueue)
    return {
        "repetition": repetition,
        "input_updates": update_count,
        "tombstone_fraction": tombstones / update_count,
        "controller_mode": "Adaptive",
        "enqueue_p50_us": percentile(latencies_ns, 50) / 1_000.0,
        "enqueue_p95_us": percentile(latencies_ns, 95) / 1_000.0,
        "enqueue_p99_us": percentile(latencies_ns, 99) / 1_000.0,
        "achieved_updates_per_s": update_count / wall_s,
        "one_core_cpu_utilization_pct": 100.0 * cpu_s / wall_s,
        "rss_before_kb": baseline_rss,
        "rss_after_kb": rss_kb(),
        "rss_delta_kb": rss_kb() - baseline_rss,
        "pending_records_at_end": pending,
        "max_pending_records": relay.maxq,
        "queued_upserts_at_end": len(relay.queue),
        "queued_tombstones_at_end": len(relay.pqueue),
        "suppressed_superseded": relay.drops["superseded"],
        "suppressed_duplicate_holder": relay.drops["replica_cap"],
        "suppressed_low_utility": relay.drops["low_utility"],
    }


def summarize(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for update_count in sorted({int(row["input_updates"]) for row in rows}):
        group = [row for row in rows if int(row["input_updates"]) == update_count]
        record: dict[str, Any] = {"input_updates": update_count, "repetitions": len(group)}
        for field in (
            "enqueue_p50_us", "enqueue_p95_us", "enqueue_p99_us",
            "achieved_updates_per_s", "one_core_cpu_utilization_pct", "rss_after_kb",
            "rss_delta_kb", "max_pending_records", "pending_records_at_end",
        ):
            values = [float(row[field]) for row in group]
            record[f"{field}_mean"] = statistics.mean(values)
            record[f"{field}_min"] = min(values)
            record[f"{field}_max"] = max(values)
        result.append(record)
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--relay", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--updates", default="10000,50000,100000")
    parser.add_argument("--repetitions", type=int, default=3)
    args = parser.parse_args()
    if args.repetitions < 1:
        raise SystemExit("repetitions must be positive")
    counts = [int(value) for value in args.updates.split(",")]
    if any(value <= 0 for value in counts):
        raise SystemExit("all update counts must be positive")
    if args.out_dir.exists() and any(args.out_dir.iterdir()):
        raise SystemExit(f"refusing to overwrite nonempty output: {args.out_dir}")
    args.out_dir.mkdir(parents=True, exist_ok=False)
    try:
        os.sched_setaffinity(0, {0})
        affinity = "cpu0"
    except (AttributeError, OSError):
        affinity = "unavailable"
    module = load_relay(args.relay.resolve())
    rows = [run_once(module, count, rep) for count in counts for rep in range(args.repetitions)]
    write_csv(args.out_dir / "raw_runs.csv", rows)
    write_csv(args.out_dir / "summary.csv", summarize(rows))
    manifest = {
        "benchmark": "gateway_enqueue_overhead_4t4",
        "metric_scope": "Relay.enqueue controller work; excludes frame construction, TCP/tc transport, and JSON-line telemetry I/O",
        "controller_configuration": {
            "mode": "Adaptive", "merge": True, "tombstone_priority": True,
            "replica_cap": 2, "global_topk": 16, "tau_s": 30.0,
            "util_lambda": 16.0, "delay_gate_s": 2.0,
            "queue_gate_frames": 8, "max_queue_frames": 4096,
        },
        "workload": {
            "coverages_tokens": [1024, 2048, 4096],
            "owners": 4, "distinct_digest_cycle": 4096,
            "tombstone_every_n_updates": 19,
        },
        "single_core_affinity": affinity,
        "relay_sha256": sha256(args.relay.resolve()),
        "update_counts": counts,
        "repetitions": args.repetitions,
    }
    (args.out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps({"out_dir": str(args.out_dir), "rows": len(rows), "affinity": affinity}, indent=2))


if __name__ == "__main__":
    main()
