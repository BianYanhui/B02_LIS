"""Experiment 0: record a replayable KV trace from four live vLLM workers.

Events follow the harness shadow of each real completion (create/update/evict).
Routing during capture is round-robin, so the trace is not itself a policy result.
Collection stops at 10 minutes or 2,000 completed requests, whichever comes first.
"""
from __future__ import annotations

import asyncio
import csv
import importlib.util
import time
from pathlib import Path

import aiohttp

from experiments.icc_kv.runtime import OUT, PathRuntime, prepare_fixed_gateway, write_json
from experiments.icc_kv.wire import K_TOMB, K_UP


def load_formal():
    path = Path("/home/byh/B02/experiments/4t4/run_formal4t4.py")
    spec = importlib.util.spec_from_file_location("formal4t4_icc", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("")
        return
    fields = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


async def collect(out_dir: Path, seconds: float, max_requests: int, kv_tokens: int) -> dict:
    formal = load_formal()
    prepare_fixed_gateway()
    runtime = PathRuntime()
    await runtime.start()
    await runtime.configure(1, "FullSync", 0.0)
    await formal.check_endpoints()
    shadows = [formal.ShadowCache(kv_tokens) for _ in range(4)]
    versions: dict[tuple[int, str], int] = {}
    seen: set[tuple[int, str]] = set()
    events: list[dict] = []
    requests: list[dict] = []
    seq = 1
    rng_seed = 20260928
    import random
    rng = random.Random(rng_seed)
    cdf = formal.zipf_cdf(1.2, 64)
    lengths = formal.reuse_prefix_lengths(64, rng_seed)
    started = time.perf_counter()
    completed = 0
    async with aiohttp.ClientSession() as session:
        request_id = 0
        while time.perf_counter() - started < seconds and completed < max_requests:
            wave = []
            for slot in range(4):
                if completed + slot >= max_requests:
                    break
                prefix_slot = __import__("bisect").bisect_left(cdf, rng.random())
                digest = f"L{prefix_slot:04d}"
                coverage = lengths[prefix_slot]
                worker = request_id % 4
                request_id += 1
                prompt = formal.prompt_for(formal.TraceRequest(
                    request_id=request_id, phase=0, lineage_id=prefix_slot, step=0,
                    tenant=f"tenant-{prefix_slot % 8}", digest=digest, coverage_tokens=coverage,
                    workload="reuse_intensive", discard=False,
                ))
                wave.append((request_id, worker, digest, coverage, prompt))
            responses = await asyncio.gather(*[
                formal.one_request(session, formal.URLS[worker], prompt, "icc-trace", 4, 3)
                for _rid, worker, _d, _c, prompt in wave
            ])
            for (rid, worker, digest, coverage, _prompt), response in zip(wave, responses):
                if not response["ok"]:
                    continue
                completed += 1
                now_ns = time.time_ns()
                evicted = shadows[worker].insert(digest, coverage)
                key = (worker, digest)
                versions[key] = versions.get(key, 0) + 1
                kind_name = "create" if key not in seen else "update"
                seen.add(key)
                digest_i = formal.digest64(digest)
                generated = time.time()
                await runtime.send_event(K_UP, worker, 1, seq, coverage, digest_i, generated)
                events.append({
                    "timestamp_ns": now_ns, "worker_id": str(worker), "prefix_id": digest,
                    "event_type": kind_name, "version": versions[key], "coverage": coverage,
                    "scope": "owner", "size_bytes": 64, "request_id": str(rid),
                    "resident_after": True, "seq": seq,
                })
                seq += 1
                for victim in evicted:
                    vkey = (worker, victim)
                    versions[vkey] = versions.get(vkey, 0) + 1
                    await runtime.send_event(K_TOMB, worker, 1, seq, 0, formal.digest64(victim), time.time())
                    events.append({
                        "timestamp_ns": time.time_ns(), "worker_id": str(worker), "prefix_id": victim,
                        "event_type": "evict", "version": versions[vkey], "coverage": 0,
                        "scope": "owner", "size_bytes": 64, "request_id": str(rid),
                        "resident_after": False, "seq": seq,
                    })
                    seq += 1
                requests.append({
                    "arrival_ns": now_ns, "prefix_id": digest, "prompt_tokens": response["input_tokens"],
                    "routed_worker": worker, "ttft_ms": response["ttft_ms"],
                    "prefill_tokens": max(0, int(response["input_tokens"] or 0) - int(response["vllm_cached_tokens"] or 0)),
                    "reused_tokens": int(response["vllm_cached_tokens"] or 0),
                    "request_id": rid, "coverage": coverage,
                })
            await runtime.drain_agent()
            print({"completed": completed, "events": len(events), "elapsed_s": round(time.perf_counter() - started, 1)}, flush=True)
    write_csv(out_dir / "events.csv", events)
    write_csv(out_dir / "requests.csv", requests)
    summary = {
        **runtime.meta(),
        "experiment": "trace",
        "completed_requests": completed,
        "events": len(events),
        "elapsed_s": time.perf_counter() - started,
        "seed": rng_seed,
        "note": "Round-robin capture. create/update/evict follow the per-worker shadow of real completions.",
    }
    write_json(out_dir / "trace_meta.json", summary)
    return summary


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=float, default=600.0)
    parser.add_argument("--max-requests", type=int, default=2000)
    parser.add_argument("--kv-cache-tokens", type=int, default=104544)
    parser.add_argument("--out-dir", type=Path, default=OUT / "trace")
    args = parser.parse_args()
    asyncio.run(collect(args.out_dir, args.seconds, args.max_requests, args.kv_cache_tokens))


if __name__ == "__main__":
    main()
