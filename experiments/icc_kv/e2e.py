"""Experiments 4 and 5: four real vLLM workers share the control path with replay.

Background events use worker ids 16 and above. Adaptive only sees ordinary
frames, so it cannot tell a real completion from a replayed one. Ideal applies
the real worker's update to the local view immediately and does not send it.
"""
from __future__ import annotations

import asyncio
import csv
import importlib.util
import json
import sys
import time
from pathlib import Path

import aiohttp

from experiments.icc_kv.burst import offer
from experiments.icc_kv.replay import load_events
from experiments.icc_kv.runtime import OUT, PathRuntime, prepare_fixed_gateway, write_json
from experiments.icc_kv.wire import K_TOMB, K_UP


def load_formal():
    path = Path("/home/byh/B02/experiments/4t4/run_formal4t4.py")
    spec = importlib.util.spec_from_file_location("formal4t4_icc_e2e", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def choose(view: dict[tuple[int, int], int], digest: int) -> tuple[int, int]:
    best_worker, best = 0, -1
    for worker in range(4):
        coverage = view.get((worker, digest), 0)
        if coverage > best:
            best_worker, best = worker, coverage
    return best_worker, best


async def run_e2e(
    trace: Path, capacity: float, scenarios: list[str], methods: list[str],
    seeds: int, requests_per_run: int, kv_tokens: int, out_dir: Path,
) -> None:
    formal = load_formal()
    events = load_events(trace)
    prepare_fixed_gateway()
    runtime = PathRuntime()
    await runtime.start()
    await formal.check_endpoints()
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "e2e_summary.json"
    rows: list[dict] = json.loads(path.read_text()).get("rows", []) if path.exists() else []
    done = {(row["seed"], row["scenario"], row["method"]) for row in rows}
    cell = 10 + len(rows)
    lock = asyncio.Lock()
    import random
    cdf = formal.zipf_cdf(1.2, 64)
    lengths = formal.reuse_prefix_lengths(64, 20260928)

    async def emit(kind: int, worker: int, seq: int, coverage: int, digest: int) -> None:
        async with lock:
            await runtime.send_event(kind, worker, cell_id, seq, coverage, digest, time.time())

    for seed in range(seeds):
        for scenario in scenarios:
            for method in methods:
                if (seed, scenario, method) in done:
                    continue
                rho = {"normal": 0.5, "near": 0.9, "burst": 0.65}[scenario]
                rate = capacity if method == "RateFIFO" else 0.0
                cell_id = cell
                if method != "Ideal":
                    await runtime.configure(cell_id, method, rate)
                else:
                    await runtime.configure(cell_id, "FullSync", 0.0)
                shadows = [formal.ShadowCache(kv_tokens) for _ in range(4)]
                truth: dict[tuple[int, int], int] = {}
                seq = {"n": 1}
                stop = asyncio.Event()
                background_events = [{**row, "worker_id": str(int(row["worker_id"]) + 16)} for row in events]

                async def background() -> None:
                    bg_seq = 1_000_000_000
                    started = time.perf_counter()
                    while not stop.is_set():
                        elapsed = time.perf_counter() - started
                        if scenario == "burst" and (elapsed % 30) >= 25:
                            level, window = rho * capacity * 5, 5.0
                        else:
                            level, window = rho * capacity, 5.0
                        sent = await offer(runtime, background_events, cell_id, level, window, bg_seq)
                        bg_seq += sent

                bg = asyncio.create_task(background())
                rng = random.Random(seed)
                records = []
                async with aiohttp.ClientSession() as session:
                    for request_id in range(requests_per_run):
                        slot = __import__("bisect").bisect_left(cdf, rng.random())
                        digest_s = f"L{slot:04d}"
                        digest = formal.digest64(digest_s)
                        coverage = lengths[slot]
                        ideal, ideal_cov = choose(truth, digest)
                        routed, view_cov = choose(runtime.dispatcher.tested, digest) if method != "Ideal" else (ideal, ideal_cov)
                        prompt = formal.prompt_for(formal.TraceRequest(
                            request_id=request_id, phase=0, lineage_id=slot, step=0,
                            tenant=f"tenant-{slot % 8}", digest=digest_s, coverage_tokens=coverage,
                            workload="reuse_intensive", discard=False,
                        ))
                        response = await formal.one_request(session, formal.URLS[routed], prompt, f"icc-e2e-{cell}", 4, 2)
                        if not response["ok"]:
                            continue
                        regret = max(0, ideal_cov - truth.get((routed, digest), 0))
                        evicted = shadows[routed].insert(digest_s, coverage)
                        truth[(routed, digest)] = coverage
                        if method == "Ideal":
                            runtime.dispatcher.tested[(routed, digest)] = coverage
                        else:
                            await emit(K_UP, routed, seq["n"], coverage, digest)
                            seq["n"] += 1
                            for victim in evicted:
                                truth.pop((routed, formal.digest64(victim)), None)
                                await emit(K_TOMB, routed, seq["n"], 0, formal.digest64(victim))
                                seq["n"] += 1
                        records.append({
                            "ttft_ms": response["ttft_ms"],
                            "reused_tokens": int(response["vllm_cached_tokens"] or 0),
                            "prefill_tokens": max(0, int(response["input_tokens"] or 0) - int(response["vllm_cached_tokens"] or 0)),
                            "wrong_placement": int(routed != ideal and ideal_cov > 0),
                            "coverage_regret": regret,
                        })
                stop.set()
                bg.cancel()
                try:
                    await bg
                except asyncio.CancelledError:
                    pass
                ttfts = [row["ttft_ms"] for row in records]
                ordered = sorted(ttfts)

                def pct(p: float) -> float:
                    if not ordered:
                        return 0.0
                    return ordered[min(len(ordered) - 1, round(p / 100 * (len(ordered) - 1)))]

                summary = {
                    "seed": seed, "scenario": scenario, "method": method, "cell": cell,
                    "requests": len(records),
                    "ttft_mean_ms": sum(ttfts) / len(ttfts) if ttfts else 0.0,
                    "ttft_p95_ms": pct(95),
                    "slo_violation_2s": sum(value > 2000 for value in ttfts) / len(ttfts) if ttfts else 0.0,
                    "prefill_tokens_mean": sum(row["prefill_tokens"] for row in records) / len(records) if records else 0.0,
                    "reused_tokens_mean": sum(row["reused_tokens"] for row in records) / len(records) if records else 0.0,
                    "wrong_placement_rate": sum(row["wrong_placement"] for row in records) / len(records) if records else 0.0,
                    "coverage_regret_mean": sum(row["coverage_regret"] for row in records) / len(records) if records else 0.0,
                    "capacity_events_per_s": capacity,
                    "background_rho": rho,
                }
                rows.append(summary)
                write_json(path, {**runtime.meta(), "rows": rows})
                if records:
                    csv_path = out_dir / f"requests_{scenario}_{method}_seed{seed}.csv"
                    with csv_path.open("w", newline="") as handle:
                        writer = csv.DictWriter(handle, fieldnames=list(records[0]), lineterminator="\n")
                        writer.writeheader()
                        writer.writerows(records)
                print(summary, flush=True)
                cell += 1


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--trace", type=Path, default=OUT / "trace" / "events.csv")
    parser.add_argument("--capacity", type=float, required=True)
    parser.add_argument("--scenarios", default="normal,near,burst")
    parser.add_argument("--methods", default="FullSync,StaticSemantic,Adaptive,Ideal")
    parser.add_argument("--seeds", type=int, default=5)
    parser.add_argument("--requests", type=int, default=500)
    parser.add_argument("--kv-cache-tokens", type=int, default=104544)
    parser.add_argument("--out-dir", type=Path, default=OUT / "e2e")
    args = parser.parse_args()
    asyncio.run(run_e2e(
        args.trace, args.capacity,
        [item for item in args.scenarios.split(",") if item],
        [item for item in args.methods.split(",") if item],
        args.seeds, args.requests, args.kv_cache_tokens, args.out_dir,
    ))


if __name__ == "__main__":
    main()
