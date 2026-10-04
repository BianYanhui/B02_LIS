"""Experiment 1: stable full-path capacity at a fixed CPU quota."""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

from experiments.icc_kv.runtime import OUT, PathRuntime, prepare_fixed_gateway, write_json
from experiments.icc_kv.wire import K_UP

RATES = (500, 1000, 2000, 4000, 8000, 16000)


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, round((p / 100.0) * (len(ordered) - 1)))
    return ordered[index]


def slope_per_s(samples: list[tuple[float, int]]) -> float:
    if len(samples) < 2:
        return 0.0
    dt = samples[-1][0] - samples[0][0]
    if dt <= 0:
        return 0.0
    return (samples[-1][1] - samples[0][1]) / dt


async def drive(runtime: PathRuntime, rate: float, seconds: float, cell: int, seq0: int) -> int:
    """Offer `rate` real 64-byte updates/s. Returns the number generated."""
    assert runtime.agent is not None
    count = int(rate * seconds)
    batch = max(1, int(rate / 200))
    started = time.perf_counter()
    seq = seq0
    for begin in range(0, count, batch):
        target = started + begin / rate
        delay = target - time.perf_counter()
        if delay > 0:
            await asyncio.sleep(delay)
        now = time.time()
        for offset in range(min(batch, count - begin)):
            seq = seq0 + begin + offset
            digest = (seq % 4096) + 1
            coverage = (1024, 2048, 4096)[seq % 3]
            await runtime.send_event(K_UP, seq % 8, cell, seq, coverage, digest, now)
        if begin % (batch * 20) == 0:
            await runtime.drain_agent()
    await runtime.drain_agent()
    return count


async def one_point(runtime: PathRuntime, rate: int, seconds: float, rep: int, cell: int) -> dict:
    await runtime.configure(cell, "FullSync", 0.0)
    warmup = 30.0 if seconds >= 120 else seconds * 0.25
    measure = seconds - warmup
    loop = asyncio.get_running_loop()
    samples: list[tuple[float, int]] = []
    stop = asyncio.Event()

    async def probe() -> None:
        while not stop.is_set():
            samples.append((time.perf_counter(), len(runtime.dispatcher.applied)))
            await asyncio.sleep(0.1)

    task = asyncio.create_task(probe())
    t0 = time.perf_counter()
    generated = await drive(runtime, rate, seconds, cell, 1)
    # Allow a short tail so in-flight frames can apply before the snapshot.
    await asyncio.sleep(2.0)
    stop.set()
    await task
    applied = list(runtime.dispatcher.applied)
    run_wall0 = time.time() - (time.perf_counter() - t0)
    window = [row for row in applied if run_wall0 + warmup <= row.applied_at <= run_wall0 + seconds]
    lags = [max(0.0, row.applied_at - row.generated_at) for row in window]
    mid_cut = run_wall0 + warmup + measure / 2
    last_cut = run_wall0 + seconds - min(30.0, measure / 3)
    mid = [lag for row, lag in zip(window, lags) if row.applied_at < mid_cut]
    last = [lag for row, lag in zip(window, lags) if row.applied_at >= last_cut]
    last_samples = [(ts, n) for ts, n in samples if ts >= t0 + max(warmup, seconds - 60)]
    generated_window = generated
    output_window = len(applied)
    gap = abs(output_window - generated_window) / generated_window if generated_window else 1.0
    deficit = [(ts, max(0.0, rate * (ts - t0) - n)) for ts, n in last_samples]
    deficit_slope = slope_per_s([(ts, int(value)) for ts, value in deficit])
    p95_mid = percentile(mid, 95)
    p95_last = percentile(last, 95)
    lag_rising = bool(mid and last and p95_last > max(0.05, p95_mid) * 1.5 + 0.02)
    # Gap and lag are the stability tests. The deficit slope is recorded because
    # batch pacing makes it noisy on a path that still delivers every frame.
    stable = gap <= 0.01 and not lag_rising
    return {
        "rate_target": rate,
        "rep": rep,
        "seconds": seconds,
        "warmup_s": warmup,
        "generated": generated,
        "applied_total": len(applied),
        "generated_window": generated_window,
        "applied_window": output_window,
        "gap_ratio": gap,
        "deficit_slope_per_s": deficit_slope,
        "lag_p50_s": percentile(lags, 50),
        "lag_p95_s": percentile(lags, 95),
        "lag_p99_s": percentile(lags, 99),
        "lag_p95_mid_s": p95_mid,
        "lag_p95_last_s": p95_last,
        "stable": stable,
        "cell": cell,
    }


async def run_capacity(rates: list[int], reps: int, seconds: float, out_dir: Path, stop_at_unstable: bool = True) -> dict:
    prepare_fixed_gateway()
    runtime = PathRuntime()
    await runtime.start()
    try:
        rows = []
        cell = 1
        for rate in rates:
            point_rows = []
            for rep in range(reps):
                row = await one_point(runtime, rate, seconds, rep, cell)
                cell += 1
                point_rows.append(row)
                rows.append(row)
                print(row, flush=True)
            if stop_at_unstable and not all(row["stable"] for row in point_rows):
                break
        stable_rates = sorted({row["rate_target"] for row in rows if row["stable"]})
        capacity = max(stable_rates) if stable_rates else 0
        summary = {
            **runtime.meta(),
            "experiment": "capacity",
            "capacity_events_per_s": capacity,
            "rule": "largest offered rate with gap<=1%, deficit slope<=1% of rate, and P95 lag not rising",
            "rows": rows,
        }
        out_dir.mkdir(parents=True, exist_ok=True)
        write_json(out_dir / "capacity_summary.json", summary)
        return summary
    finally:
        await runtime.stop()


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--rates", default=",".join(str(r) for r in RATES))
    parser.add_argument("--reps", type=int, default=3)
    parser.add_argument("--seconds", type=float, default=120.0)
    parser.add_argument("--out-dir", type=Path, default=OUT / "capacity")
    parser.add_argument("--all-rates", action="store_true", help="keep measuring after the first unstable rate")
    args = parser.parse_args()
    rates = [int(item) for item in args.rates.split(",") if item]
    asyncio.run(run_capacity(rates, args.reps, args.seconds, args.out_dir, stop_at_unstable=not args.all_rates))


if __name__ == "__main__":
    main()
