"""Experiment 2: fixed capacity, unique short noise, a few long prefixes.

Offered noise rate is rho * C. Each noise digest is new and coverage is 256,
so semantic merge cannot collapse it. Long prefixes use the same digest
strings and 4096-token coverage as the end-to-end requests, and they go out
the foreground connection. The recorded 64-prefix trace is not the load.
"""
from __future__ import annotations

import asyncio
import csv
import hashlib
import json
import random
import time
from pathlib import Path

from experiments.icc_kv.runtime import OUT, PathRuntime, prepare_fixed_gateway, write_json
from experiments.icc_kv.wire import K_TOMB, K_UP

METHODS = ("FullSync", "RateFIFO", "StaticSemantic", "Adaptive")


def load_events(path: Path) -> list[dict]:
    with path.open() as handle:
        rows = list(csv.DictReader(handle))
    rows.sort(key=lambda row: int(row["timestamp_ns"]))
    if not rows:
        raise RuntimeError(f"empty trace: {path}")
    return rows


def expand(events: list[dict], copies: int, correlated: bool, seed: int) -> list[dict]:
    span = int(events[-1]["timestamp_ns"]) - int(events[0]["timestamp_ns"])
    rng = random.Random(seed)
    out: list[dict] = []
    for copy in range(copies):
        shift = 0 if correlated or span <= 0 else rng.randrange(span)
        for row in events:
            worker = int(row["worker_id"]) + copy * 8
            if worker > 255:
                raise RuntimeError("logical worker id does not fit in one byte")
            out.append({
                **row,
                "worker_id": str(worker),
                "timestamp_ns": int(row["timestamp_ns"]) + shift,
            })
    out.sort(key=lambda row: int(row["timestamp_ns"]))
    return out


async def replay(runtime: PathRuntime, events: list[dict], cell: int, speed: float, seconds: float) -> int:
    """Replay the trace, repeating it until `seconds` elapse."""
    assert runtime.agent is not None
    origin = int(events[0]["timestamp_ns"])
    span_s = max(1e-3, (int(events[-1]["timestamp_ns"]) - origin) / 1e9)
    started = time.perf_counter()
    seq = 1
    sent = 0
    cycle = 0
    while time.perf_counter() - started < seconds:
        base = started + cycle * (span_s / max(speed, 1e-6))
        for row in events:
            if time.perf_counter() - started >= seconds:
                break
            due = base + ((int(row["timestamp_ns"]) - origin) / 1e9) / max(speed, 1e-6)
            delay = due - time.perf_counter()
            if delay > 0:
                await asyncio.sleep(delay)
            kind = K_TOMB if row["event_type"] in {"evict", "invalidate"} else K_UP
            await runtime.send_event(
                kind, int(row["worker_id"]), cell, seq, int(row["coverage"]),
                int.from_bytes(__import__("hashlib").blake2b(row["prefix_id"].encode(), digest_size=8).digest(), "big"),
                time.time(),
            )
            seq += 1
            sent += 1
            if sent % 200 == 0:
                await runtime.drain_agent()
        cycle += 1
    await runtime.drain_agent()
    await asyncio.sleep(1.0)
    return sent


def summarize(method: str, rho: float, capacity: float, sent: int, applied: list, seconds: float) -> dict:
    lags = [max(0.0, row.applied_at - row.generated_at) for row in applied]
    ordered = sorted(lags)

    def pct(p: float) -> float:
        if not ordered:
            return 0.0
        return ordered[min(len(ordered) - 1, round(p / 100 * (len(ordered) - 1)))]

    return {
        "method": method,
        "rho": rho,
        "capacity_events_per_s": capacity,
        "offered_events_per_s": sent / seconds if seconds else 0.0,
        "measured_utilization": (sent / seconds / capacity) if capacity and seconds else 0.0,
        "sent": sent,
        "applied": len(applied),
        "delivery_lag_p50_s": pct(50),
        "delivery_lag_p95_s": pct(95),
        "delivery_lag_p99_s": pct(99),
    }


def useful_digest(slot: int) -> int:
    return int.from_bytes(hashlib.blake2b(f"U{slot:04d}".encode(), digest_size=8).digest(), "big")


async def offer_long(runtime: PathRuntime, cell: int, seconds: float, seq0: int, per_s: float = 2.0) -> tuple[int, int]:
    """Steady 4096-token updates on the foreground connection.

    Every eighth update is preceded by a tomb for that same prefix, so invalidate
    lag can be compared with update lag. The digest strings stay U0000–U0015.
    """
    from experiments.icc_kv.e2e import USEFUL_COVERAGE, USEFUL_POOL

    updates = 0
    tombs = 0
    seq = seq0
    started = time.perf_counter()
    while time.perf_counter() - started < seconds:
        target = started + (updates + tombs) / per_s
        delay = target - time.perf_counter()
        if delay > 0:
            await asyncio.sleep(delay)
            if time.perf_counter() - started >= seconds:
                break
        slot = updates % USEFUL_POOL
        if updates > 0 and updates % 8 == 0:
            await runtime.send_foreground(
                K_TOMB, slot % 4, cell, seq, 0, useful_digest(slot), time.time(),
            )
            seq += 1
            tombs += 1
        await runtime.send_foreground(
            K_UP, slot % 4, cell, seq, USEFUL_COVERAGE, useful_digest(slot), time.time(),
        )
        seq += 1
        updates += 1
    return updates, tombs


def kind_lag(applied: list, kind: str) -> tuple[float, float, int]:
    lags = sorted(
        max(0.0, row.applied_at - row.generated_at)
        for row in applied
        if getattr(row, "kind", "") == kind
    )
    if not lags:
        return 0.0, 0.0, 0

    def pct(p: float) -> float:
        return lags[min(len(lags) - 1, round(p / 100 * (len(lags) - 1)))]

    return pct(50), pct(95), len(lags)


def long_lag(applied: list) -> tuple[float, float, int]:
    from experiments.icc_kv.e2e import USEFUL_COVERAGE

    lags = sorted(max(0.0, row.applied_at - row.generated_at) for row in applied if row.coverage >= USEFUL_COVERAGE)
    if not lags:
        return 0.0, 0.0, 0

    def pct(p: float) -> float:
        return lags[min(len(lags) - 1, round(p / 100 * (len(lags) - 1)))]

    return pct(50), pct(95), len(lags)


async def run_scale(capacity: float, rhos: list[float], methods: list[str], seeds: int, seconds: float, out_dir: Path, workers: int = 4) -> None:
    from experiments.icc_kv.e2e import NOISE_COVERAGE, USEFUL_COVERAGE, offer_noise

    prepare_fixed_gateway()
    runtime = PathRuntime()
    await runtime.start()
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        summary_path = out_dir / "scale_summary.json"
        rows: list[dict] = json.loads(summary_path.read_text()).get("rows", []) if summary_path.exists() else []
        done = {(row["seed"], row["rho"], row["method"], row.get("noise_workers", 4)) for row in rows}
        cell = 1 + len(rows)
        for seed in range(seeds):
            for rho in rhos:
                for method in methods:
                    if (seed, rho, method, workers) in done:
                        continue
                    rate = capacity if method == "RateFIFO" else 0.0
                    await runtime.configure(cell, method, rate)
                    noise_seq = 1_000_000_000 + seed * 100_000_000_000 + int(rho * 1000) * 1_000_000
                    noise_sent, long_counts = await asyncio.gather(
                        offer_noise(runtime, cell, rho * capacity, seconds, noise_seq, workers),
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
                        "rho": rho,
                        "method": method,
                        "cell": cell,
                        "seconds": seconds,
                        "workload": "unique_noise",
                        "noise_workers": workers,
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
                        "offered_noise_per_s": noise_sent / seconds if seconds else 0.0,
                        **(await runtime.fetch_stats()),
                    }
                    rows.append(row)
                    write_json(summary_path, {**runtime.meta(), "rows": rows})
                    print(row, flush=True)
                    cell += 1
    finally:
        await runtime.stop()


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--capacity", type=float, required=True)
    parser.add_argument("--rhos", default="0.5,0.9,1.2,1.5,2.0")
    parser.add_argument("--methods", default=",".join(METHODS))
    parser.add_argument("--seeds", type=int, default=5)
    parser.add_argument("--seconds", type=float, default=120.0)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--out-dir", type=Path, default=OUT / "scale")
    args = parser.parse_args()
    asyncio.run(run_scale(
        args.capacity,
        [float(item) for item in args.rhos.split(",") if item],
        [item for item in args.methods.split(",") if item],
        args.seeds, args.seconds, args.out_dir, args.workers,
    ))


if __name__ == "__main__":
    main()
