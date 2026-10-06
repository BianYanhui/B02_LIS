"""Drain probe with several noise processes.

The nominal 13000 events/s is not assumed to be the knee. Senders run in
their own processes so a single interpreter is not the rate limit. The probe
records how much of the target rate was actually offered, what the gateway
still held, and whether the gateway core was busy. It does not send GPU
requests.
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

from experiments.icc_kv.e2e import NOISE_COVERAGE
from experiments.icc_kv.runtime import (
    OUT, gateway_pid, prepare_fixed_gateway, process_cpu_seconds, write_json,
)
from experiments.icc_kv.split_path import SplitControl


async def probe(rates: list[float], seconds: float, out_dir: Path, senders: int, policies: list[str]) -> None:
    prepare_fixed_gateway()
    control = SplitControl(senders=senders)
    control.start()
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / "path_capacity.json"
        rows: list[dict] = []
        gateway = gateway_pid()
        pairs = [(policy, rate) for policy in policies for rate in rates]
        for index, (policy, rate) in enumerate(pairs):
            cell = index + 1
            control.begin_cell()
            control.configure(cell, policy, 0.0)
            before = int(control.counts().get("frames") or 0)
            cpu_mark = process_cpu_seconds(gateway)
            started = time.perf_counter()
            sent = control.offer_for({
                "cell": cell,
                "level": rate,
                "seconds": seconds,
                "scenario": "flat",
                "coverage": NOISE_COVERAGE,
                "seq0": 100_000_000,
                "workers": 4,
            })
            offer_elapsed = max(time.perf_counter() - started, 1e-6)
            gateway_cpu = (process_cpu_seconds(gateway) - cpu_mark) / offer_elapsed
            previous = -1
            stats: dict = {}
            for _settle in range(12):
                stats = control.stats()
                got = int(stats.get("relay_received") or 0)
                if got == previous:
                    break
                previous = got
                time.sleep(0.4)
            elapsed = offer_elapsed
            applied = int(control.counts().get("frames") or 0) - before
            received = int(stats.get("relay_received") or 0)
            row = {
                "path": "control_multiprocess",
                "policy": policy,
                "senders": senders,
                "rate_target": rate,
                "seconds": seconds,
                "sent": sent,
                "applied": applied,
                "applied_per_s": applied / elapsed,
                "sent_per_s": sent / elapsed,
                "sender_fraction": (sent / elapsed) / rate if rate else 0.0,
                "gap_ratio": max(0.0, 1.0 - (applied / sent)) if sent else 0.0,
                "ledger_ingress_gap": abs(sent - received) / max(sent, received) if max(sent, received) else 0.0,
                "gateway_cpu": gateway_cpu,
                **stats,
            }
            rows.append(row)
            print(row, flush=True)
        write_json(path, {**control.meta(), "rows": rows})
    finally:
        control.stop()


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--policy", default="FullSync",
                        help="comma-separated gateway policies; FullSync measures drain, Adaptive measures ingress")
    parser.add_argument("--rates", default="14000,20000")
    parser.add_argument("--seconds", type=float, default=12.0)
    parser.add_argument("--senders", type=int, default=4)
    parser.add_argument("--out-dir", type=Path, default=OUT / "path_capacity")
    args = parser.parse_args()
    asyncio.run(probe(
        [float(item) for item in args.rates.split(",") if item],
        args.seconds,
        args.out_dir,
        args.senders,
        [item for item in args.policy.split(",") if item],
    ))


if __name__ == "__main__":
    main()
