"""Experiment 3: correlated bursts against the same fixed capacity C."""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from experiments.icc_kv.replay import load_events, summarize
from experiments.icc_kv.runtime import OUT, PathRuntime, prepare_fixed_gateway, write_json
from experiments.icc_kv.wire import K_TOMB, K_UP

METHODS = ("FullSync", "RateFIFO", "StaticSemantic", "Adaptive")


async def offer(runtime: PathRuntime, events: list[dict], cell: int, rate: float, seconds: float, seq0: int) -> int:
    assert runtime.agent is not None
    count = max(1, int(rate * seconds))
    batch = max(1, int(rate / 200)) if rate >= 200 else 1
    started = time.perf_counter()
    sent = 0
    while sent < count and time.perf_counter() - started < seconds + 1:
        target = started + sent / max(rate, 1e-6)
        delay = target - time.perf_counter()
        if delay > 0:
            await asyncio.sleep(delay)
        now = time.time()
        step = min(batch, count - sent)
        for offset in range(step):
            row = events[(sent + offset) % len(events)]
            kind = K_TOMB if row["event_type"] in {"evict", "invalidate"} else K_UP
            await runtime.send_event(
                kind, int(row["worker_id"]) % 256, cell, seq0 + sent + offset,
                int(row["coverage"]),
                int.from_bytes(__import__("hashlib").blake2b(str(row["prefix_id"]).encode(), digest_size=8).digest(), "big"),
                now,
            )
        sent += step
        if sent % 400 == 0:
            await runtime.drain_agent()
    await runtime.drain_agent()
    return sent


async def run_burst(trace: Path, capacity: float, multipliers: list[float], methods: list[str], seeds: int, out_dir: Path) -> None:
    events = load_events(trace)
    prepare_fixed_gateway()
    runtime = PathRuntime()
    await runtime.start()
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / "burst_summary.json"
        rows: list[dict] = json.loads(path.read_text()).get("rows", []) if path.exists() else []
        done = {(row["seed"], row["multiplier"], row["method"]) for row in rows}
        cell = 1 + len(rows)
        base = 0.65 * capacity
        for seed in range(seeds):
            for multiplier in multipliers:
                for method in methods:
                    if (seed, multiplier, method) in done:
                        continue
                    rate = capacity if method == "RateFIFO" else 0.0
                    await runtime.configure(cell, method, rate)
                    sent = 0
                    seq = 1
                    for _cycle in range(5):
                        sent += await offer(runtime, events, cell, base, 25.0, seq + sent)
                        sent += await offer(runtime, events, cell, base * multiplier, 5.0, seq + sent)
                    before = len(runtime.dispatcher.applied)
                    await asyncio.sleep(5.0)
                    row = summarize(method, base * (25 + 5 * multiplier) / 30 / capacity, capacity, sent, list(runtime.dispatcher.applied), 150.0)
                    row.update({
                        "seed": seed, "multiplier": multiplier, "cell": cell,
                        "applied_after_tail": len(runtime.dispatcher.applied),
                        "applied_at_burst_end": before,
                    })
                    rows.append(row)
                    write_json(path, {**runtime.meta(), "rows": rows})
                    print(row, flush=True)
                    cell += 1
    finally:
        await runtime.stop()


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--trace", type=Path, default=OUT / "trace" / "events.csv")
    parser.add_argument("--capacity", type=float, required=True)
    parser.add_argument("--multipliers", default="5,10")
    parser.add_argument("--methods", default=",".join(METHODS))
    parser.add_argument("--seeds", type=int, default=5)
    parser.add_argument("--out-dir", type=Path, default=OUT / "burst")
    args = parser.parse_args()
    asyncio.run(run_burst(
        args.trace, args.capacity,
        [float(item) for item in args.multipliers.split(",")],
        [item for item in args.methods.split(",") if item],
        args.seeds, args.out_dir,
    ))


if __name__ == "__main__":
    main()
