"""ICC fixed-capacity experiment entrypoints."""
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from experiments.icc_kv.capacity import RATES, run_capacity
from experiments.icc_kv.collect import collect
from experiments.icc_kv.replay import run_scale
from experiments.icc_kv.runtime import OUT


def main() -> None:
    parser = argparse.ArgumentParser(description="ICC KV coordination experiments at fixed capacity C")
    sub = parser.add_subparsers(dest="cmd", required=True)
    cap = sub.add_parser("capacity")
    cap.add_argument("--rates", default=",".join(str(item) for item in RATES))
    cap.add_argument("--reps", type=int, default=3)
    cap.add_argument("--seconds", type=float, default=120.0)
    cap.add_argument("--out-dir", type=Path, default=OUT / "capacity")
    tr = sub.add_parser("trace")
    tr.add_argument("--seconds", type=float, default=600.0)
    tr.add_argument("--max-requests", type=int, default=2000)
    tr.add_argument("--kv-cache-tokens", type=int, default=104544)
    tr.add_argument("--out-dir", type=Path, default=OUT / "trace")
    sc = sub.add_parser("scale")
    sc.add_argument("--capacity", type=float, required=True)
    sc.add_argument("--rhos", default="0.5,0.9,1.2,1.5,2.0")
    sc.add_argument("--methods", default="FullSync,RateFIFO,StaticSemantic,Adaptive")
    sc.add_argument("--seeds", type=int, default=5)
    sc.add_argument("--seconds", type=float, default=120.0)
    sc.add_argument("--workers", type=int, default=4)
    sc.add_argument("--out-dir", type=Path, default=OUT / "scale")
    args = parser.parse_args()
    if args.cmd == "capacity":
        rates = [int(item) for item in args.rates.split(",") if item]
        asyncio.run(run_capacity(rates, args.reps, args.seconds, args.out_dir))
    elif args.cmd == "trace":
        asyncio.run(collect(args.out_dir, args.seconds, args.max_requests, args.kv_cache_tokens))
    else:
        asyncio.run(run_scale(
            args.capacity,
            [float(item) for item in args.rhos.split(",") if item],
            [item for item in args.methods.split(",") if item],
            args.seeds, args.seconds, args.out_dir, args.workers,
        ))


if __name__ == "__main__":
    main()
