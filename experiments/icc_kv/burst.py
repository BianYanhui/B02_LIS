"""Experiment 3: the end-to-end burst shape, without GPU requests.

Baseline is 0.9C of unique 256-token noise. In the last 5 seconds of each
30 second cycle the noise rate is 5x. Long 4096-token prefixes ride the
foreground connection the whole time. The recorded trace is not the load.
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from experiments.icc_kv.runtime import OUT, PathRuntime, prepare_fixed_gateway, write_json

METHODS = ("FullSync", "RateFIFO", "StaticSemantic", "Adaptive")


async def offer_burst_noise(runtime: PathRuntime, cell: int, capacity: float, seq0: int, cycles: int = 5) -> int:
    """Same shape as the end-to-end burst scenario: 0.9C, with the last 5s of each 30s at 5x."""
    from experiments.icc_kv.e2e import offer_noise

    seq = seq0
    started = time.perf_counter()
    deadline = started + cycles * 30
    while time.perf_counter() < deadline:
        elapsed = time.perf_counter() - started
        if (elapsed % 30) >= 25:
            level, window = 0.9 * capacity * 5, 5.0
        else:
            level, window = 0.9 * capacity, 5.0
        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            break
        sent = await offer_noise(runtime, cell, level, min(window, remaining), seq)
        seq += sent
    return seq - seq0


async def run_burst(capacity: float, methods: list[str], seeds: int, out_dir: Path) -> None:
    from experiments.icc_kv.e2e import NOISE_COVERAGE, USEFUL_COVERAGE
    from experiments.icc_kv.replay import kind_lag, long_lag, offer_long

    prepare_fixed_gateway()
    runtime = PathRuntime()
    await runtime.start()
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / "burst_summary.json"
        rows: list[dict] = json.loads(path.read_text()).get("rows", []) if path.exists() else []
        done = {(row["seed"], row["method"]) for row in rows}
        cell = 1 + len(rows)
        seconds = 150.0
        for seed in range(seeds):
            for method in methods:
                if (seed, method) in done:
                    continue
                rate = capacity if method == "RateFIFO" else 0.0
                await runtime.configure(cell, method, rate)
                noise_seq = 1_000_000_000 + seed * 100_000_000_000
                noise_sent, long_counts = await asyncio.gather(
                    offer_burst_noise(runtime, cell, capacity, noise_seq),
                    offer_long(runtime, cell, seconds, 1 + seed * 1_000_000),
                )
                long_sent, invalidate_sent = long_counts
                with runtime.dispatcher.lock:
                    frames = runtime.dispatcher.frames
                    foreground = runtime.dispatcher.foreground_frames
                    applied = list(runtime.dispatcher.applied)
                lag_p50, lag_p95, long_applied = long_lag(applied)
                inv_p50, inv_p95, inv_applied = kind_lag(applied, "invalidate")
                row = {
                    "seed": seed,
                    "method": method,
                    "cell": cell,
                    "seconds": seconds,
                    "workload": "unique_noise_burst",
                    "baseline_rho": 0.9,
                    "spike_multiplier": 5,
                    "noise_coverage": NOISE_COVERAGE,
                    "useful_coverage": USEFUL_COVERAGE,
                    "noise_sent": noise_sent,
                    "noise_applied": max(0, frames - foreground),
                    "long_sent": long_sent,
                    "long_applied": long_applied,
                    "long_lag_p50_s": lag_p50,
                    "long_lag_p95_s": lag_p95,
                    "invalidate_sent": invalidate_sent,
                    "invalidate_applied": inv_applied,
                    "invalidate_lag_p50_s": inv_p50,
                    "invalidate_lag_p95_s": inv_p95,
                    "capacity_events_per_s": capacity,
                    **(await runtime.fetch_stats()),
                }
                rows.append(row)
                write_json(path, {**runtime.meta(), "rows": rows})
                print(row, flush=True)
                cell += 1
    finally:
        await runtime.stop()


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--capacity", type=float, required=True)
    parser.add_argument("--methods", default=",".join(METHODS))
    parser.add_argument("--seeds", type=int, default=5)
    parser.add_argument("--out-dir", type=Path, default=OUT / "burst")
    args = parser.parse_args()
    asyncio.run(run_burst(
        args.capacity,
        [item for item in args.methods.split(",") if item],
        args.seeds, args.out_dir,
    ))


if __name__ == "__main__":
    main()
