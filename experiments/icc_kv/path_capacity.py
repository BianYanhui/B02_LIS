"""Short drain probe for one path.

The nominal 13000 events/s is not assumed to be the knee. This offers unique
256-token noise at fixed rates, with the same gateway CPU and 1 Gbit ceiling,
and records how many frames the dispatcher actually applies. vLLM may be up;
this probe does not send GPU requests. A separate end-to-end cell records
forwarded_per_s while requests are in flight.
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

from experiments.icc_kv.e2e import offer_noise
from experiments.icc_kv.runtime import OUT, PathRuntime, prepare_fixed_gateway, write_json


async def probe(rates: list[float], seconds: float, out_dir: Path) -> None:
    prepare_fixed_gateway()
    runtime = PathRuntime()
    await runtime.start()
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / "path_capacity.json"
        rows: list[dict] = []
        for index, rate in enumerate(rates):
            cell = index + 1
            await runtime.configure(cell, "FullSync", 0.0)
            before = runtime.dispatcher.frames
            started = time.perf_counter()
            sent = await offer_noise(runtime, cell, rate, seconds, 1_000_000_000 + index * 1_000_000_000)
            await asyncio.sleep(1.0)
            elapsed = max(time.perf_counter() - started, 1e-6)
            applied = runtime.dispatcher.frames - before
            stats = await runtime.fetch_stats()
            row = {
                "path": "control_vllm_resident",
                "rate_target": rate,
                "seconds": seconds,
                "sent": sent,
                "applied": applied,
                "applied_per_s": applied / elapsed,
                "sent_per_s": sent / elapsed,
                "gap_ratio": max(0.0, 1.0 - (applied / sent)) if sent else 0.0,
                **stats,
            }
            rows.append(row)
            print(row, flush=True)
        write_json(path, {**runtime.meta(), "rows": rows})
    finally:
        await runtime.stop()


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--rates", default="14000,20000")
    parser.add_argument("--seconds", type=float, default=12.0)
    parser.add_argument("--out-dir", type=Path, default=OUT / "path_capacity")
    args = parser.parse_args()
    asyncio.run(probe(
        [float(item) for item in args.rates.split(",") if item],
        args.seconds,
        args.out_dir,
    ))


if __name__ == "__main__":
    main()
