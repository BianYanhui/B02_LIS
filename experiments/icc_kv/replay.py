"""Experiments 2–3: replay one recorded trace at a fixed capacity C.

Offered rate is rho * C. The HTB ceiling and Gateway CPU quota stay at the
values used to measure C. Copies of the trace differ by worker id; correlated
mode keeps the original timing, independent mode shifts each copy.
"""
from __future__ import annotations

import asyncio
import csv
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


async def run_scale(trace: Path, capacity: float, rhos: list[float], methods: list[str], copies: int, correlated: bool, seeds: int, seconds: float, out_dir: Path) -> None:
    events = load_events(trace)
    prepare_fixed_gateway()
    runtime = PathRuntime()
    await runtime.start()
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        summary_path = out_dir / "scale_summary.json"
        rows: list[dict] = []
        if summary_path.exists():
            rows = json.loads(summary_path.read_text()).get("rows", [])
        done = {(row["seed"], row["rho"], row["method"], row["correlated"]) for row in rows}
        cell = 1 + len(rows)
        for seed in range(seeds):
            expanded = expand(events, copies, correlated, seed)
            for rho in rhos:
                span_s = max(1e-3, (int(expanded[-1]["timestamp_ns"]) - int(expanded[0]["timestamp_ns"])) / 1e9)
                natural = len(expanded) / span_s
                target = rho * capacity
                speed = target / natural if natural else 1.0
                for method in methods:
                    if (seed, rho, method, correlated) in done:
                        continue
                    rate = capacity if method == "RateFIFO" else 0.0
                    await runtime.configure(cell, method, rate)
                    sent = await replay(runtime, expanded, cell, speed, seconds)
                    row = summarize(method, rho, capacity, sent, list(runtime.dispatcher.applied), seconds)
                    row.update({"seed": seed, "copies": copies, "correlated": correlated, "cell": cell, "speed": speed})
                    rows.append(row)
                    write_json(summary_path, {**runtime.meta(), "rows": rows})
                    print(row, flush=True)
                    cell += 1
    finally:
        await runtime.stop()


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--trace", type=Path, default=OUT / "trace" / "events.csv")
    parser.add_argument("--capacity", type=float, required=True)
    parser.add_argument("--rhos", default="0.5,0.8,1.0,1.2")
    parser.add_argument("--methods", default=",".join(METHODS))
    parser.add_argument("--copies", type=int, default=4)
    parser.add_argument("--correlated", action="store_true")
    parser.add_argument("--seeds", type=int, default=5)
    parser.add_argument("--seconds", type=float, default=180.0)
    parser.add_argument("--out-dir", type=Path, default=OUT / "scale")
    args = parser.parse_args()
    asyncio.run(run_scale(
        args.trace, args.capacity,
        [float(item) for item in args.rhos.split(",")],
        [item for item in args.methods.split(",") if item],
        args.copies, args.correlated, args.seeds, args.seconds, args.out_dir,
    ))


if __name__ == "__main__":
    main()
