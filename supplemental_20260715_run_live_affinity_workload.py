#!/usr/bin/env python3
"""Short live-vLLM affinity workload for B02.

This uses the already running vLLM endpoints on yhs1 ports 8000-8007. It is a
small T4-backed measurement designed to support the paper's interface claim:
when prefix locality exists, bounded affinity state lets a dispatcher route to
instances with reusable KV while keeping advertised metadata bounded.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import os
import random
import statistics
import time
from collections import Counter, defaultdict, deque

import aiohttp


MODEL_ID = "/home/byh/.cache/modelscope/qwen/Qwen2.5-1.5B-Instruct"
URLS = [f"http://127.0.0.1:{8000+i}" for i in range(8)]
ENTRY_BYTES = 64
LOAD_BYTES_PER_INSTANCE = 96


def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def write_csv(path: str, rows: list[dict]) -> None:
    ensure_dir(os.path.dirname(path))
    if not rows:
        return
    keys = []
    seen = set()
    for r in rows:
        for k in r:
            if k not in seen:
                keys.append(k)
                seen.add(k)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        w.writerows(rows)


def percentile(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    xs = sorted(xs)
    idx = max(0, min(len(xs) - 1, round((p / 100.0) * (len(xs) - 1))))
    return xs[idx]


def prefix_id(rng: random.Random, locality: str, n_prefixes: int) -> int:
    alpha = {"high": 1.35, "medium": 0.75, "low": 0.05}[locality]
    weights = [1.0 / ((i + 1) ** alpha) for i in range(n_prefixes)]
    return rng.choices(range(n_prefixes), weights=weights, k=1)[0]


def make_prefix(pid: int, prefix_tokens: int) -> str:
    # Stable repeated content makes prefix caching possible on the same endpoint.
    base = f"Workflow prefix {pid:04d}. Analyze logs, preserve facts, and answer consistently. "
    repeat = max(1, prefix_tokens // 12)
    return base * repeat


def digest(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()[:16]


class Dispatcher:
    def __init__(self, policy: str, K: int | None, n_instances: int):
        self.policy = policy
        self.K = K
        self.n_instances = n_instances
        self.rr = 0
        self.resident: list[set[str]] = [set() for _ in range(n_instances)]
        self.advertised: list[set[str]] = [set() for _ in range(n_instances)]
        self.lru: list[deque[str]] = [deque() for _ in range(n_instances)]
        self.demand = Counter()
        self.hits = 0
        self.misses = 0
        self.reuse_candidates = []

    def _rr_pick(self) -> int:
        inst = self.rr % self.n_instances
        self.rr += 1
        return inst

    def choose(self, h: str) -> tuple[int, bool, int]:
        self.demand[h] += 1
        if self.policy == "coarse":
            return self._rr_pick(), False, 0
        if self.policy == "exact":
            candidates = [i for i in range(self.n_instances) if h in self.resident[i]]
        else:
            candidates = [i for i in range(self.n_instances) if h in self.advertised[i]]
        if candidates:
            # Spread among candidates but keep deterministic choice for locality.
            inst = min(candidates, key=lambda i: len(self.resident[i]))
            return inst, True, len(candidates)
        return self._rr_pick(), False, 0

    def observe(self, inst: int, h: str) -> None:
        if h in self.resident[inst]:
            self.hits += 1
        else:
            self.misses += 1
        self.resident[inst].add(h)
        if h in self.lru[inst]:
            try:
                self.lru[inst].remove(h)
            except ValueError:
                pass
        self.lru[inst].append(h)
        if self.policy == "exact":
            self.advertised[inst] = set(self.resident[inst])
        elif self.policy.startswith("sketch"):
            K = self.K or len(self.resident[inst])
            ranked = sorted(self.resident[inst], key=lambda x: (self.demand[x], x), reverse=True)
            self.advertised[inst] = set(ranked[:K])

    def metadata_bytes(self) -> int:
        return self.n_instances * LOAD_BYTES_PER_INSTANCE + sum(len(s) for s in self.advertised) * ENTRY_BYTES

    def advertised_entries(self) -> int:
        return sum(len(s) for s in self.advertised)


async def one_request(session: aiohttp.ClientSession, url: str, prefix: str, step: int, max_tokens: int) -> dict:
    t0 = time.perf_counter_ns()
    first = 0
    chunks = 0
    ok = True
    err = ""
    try:
        async with session.post(
            f"{url}/v1/chat/completions",
            json={
                "model": MODEL_ID,
                "messages": [
                    {"role": "system", "content": prefix},
                    {"role": "user", "content": f"Step {step}: summarize the next action in one short sentence."},
                ],
                "max_tokens": max_tokens,
                "temperature": 0.0,
                "stream": True,
            },
            timeout=aiohttp.ClientTimeout(total=30),
        ) as r:
            if r.status != 200:
                ok = False
                err = f"http_{r.status}"
            else:
                async for raw in r.content:
                    line = raw.decode(errors="ignore").strip()
                    if not line.startswith("data: "):
                        continue
                    payload = line[len("data: "):].strip()
                    if payload == "[DONE]":
                        break
                    chunks += 1
                    if first == 0:
                        first = time.perf_counter_ns()
    except Exception as e:
        ok = False
        err = repr(e)[:120]
    t1 = time.perf_counter_ns()
    return {
        "ok": ok,
        "err": err,
        "ttft_ms": ((first or t1) - t0) / 1e6,
        "latency_ms": (t1 - t0) / 1e6,
        "chunks": chunks,
    }


async def run_cell(policy: str, K: int | None, locality: str, rep: int, args) -> dict:
    rng = random.Random(args.seed + rep * 1000 + hash((policy, K, locality)) % 100000)
    disp = Dispatcher(policy, K, len(URLS))
    rows = []
    async with aiohttp.ClientSession() as session:
        for step in range(args.n_requests):
            pid = prefix_id(rng, locality, args.n_prefixes)
            prefix = make_prefix(pid, args.prefix_tokens)
            h = digest(prefix)
            t_dec0 = time.perf_counter_ns()
            inst, candidate_hit, fanout = disp.choose(h)
            decision_us = (time.perf_counter_ns() - t_dec0) / 1e3
            res = await one_request(session, URLS[inst], prefix, step, args.max_tokens)
            disp.observe(inst, h)
            rows.append({
                "ok": res["ok"],
                "ttft_ms": res["ttft_ms"],
                "latency_ms": res["latency_ms"],
                "decision_us": decision_us,
                "candidate_hit": candidate_hit,
                "candidate_fanout": fanout,
                "inst": inst,
                "digest": h,
            })
            if args.sleep_ms:
                await asyncio.sleep(args.sleep_ms / 1000)
    oks = [r for r in rows if r["ok"]]
    ttfts = [r["ttft_ms"] for r in oks]
    lats = [r["latency_ms"] for r in oks]
    decisions = [r["decision_us"] for r in rows]
    fanouts = [r["candidate_fanout"] for r in rows]
    unique_prefixes = len(set(r["digest"] for r in rows))
    return {
        "experiment": "live_vllm_affinity_workload",
        "policy": policy,
        "K": "full" if K is None else K,
        "locality": locality,
        "rep": rep,
        "n_requests": args.n_requests,
        "n_success": len(oks),
        "success_rate": round(len(oks) / max(1, len(rows)), 4),
        "unique_prefixes": unique_prefixes,
        "observed_reuse_hit_rate": round(disp.hits / max(1, disp.hits + disp.misses), 4),
        "candidate_hit_rate": round(sum(1 for r in rows if r["candidate_hit"]) / max(1, len(rows)), 4),
        "candidate_fanout_p95": percentile(fanouts, 95),
        "ttft_p50_ms": round(percentile(ttfts, 50), 3),
        "ttft_p95_ms": round(percentile(ttfts, 95), 3),
        "latency_p50_ms": round(percentile(lats, 50), 3),
        "latency_p95_ms": round(percentile(lats, 95), 3),
        "decision_p50_us": round(percentile(decisions, 50), 3),
        "decision_p95_us": round(percentile(decisions, 95), 3),
        "advertised_entries_end": disp.advertised_entries(),
        "metadata_snapshot_bytes_end": disp.metadata_bytes(),
        "claim_relevance": "Live T4/vLLM check that bounded affinity state helps route repeated prefixes while bounding advertised metadata.",
    }


async def main_async(args) -> None:
    ensure_dir(args.out_dir)
    cells = []
    for locality in args.localities:
        for policy_spec in args.policies:
            if policy_spec == "coarse":
                policy, K = "coarse", None
            elif policy_spec == "exact":
                policy, K = "exact", None
            else:
                policy, K = "sketch", int(policy_spec.split("=", 1)[1])
            for rep in range(1, args.reps + 1):
                print(f"cell locality={locality} policy={policy_spec} rep={rep}", flush=True)
                row = await run_cell(policy, K, locality, rep, args)
                cells.append(row)
                write_csv(os.path.join(args.out_dir, "live_vllm_affinity_workload.csv"), cells)
    with open(os.path.join(args.out_dir, "live_vllm_affinity_workload.json"), "w") as f:
        json.dump(cells, f, indent=2)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", default="/home/byh/B02/supplemental_20260715/results")
    ap.add_argument("--seed", type=int, default=20260715)
    ap.add_argument("--n-requests", type=int, default=80)
    ap.add_argument("--n-prefixes", type=int, default=80)
    ap.add_argument("--prefix-tokens", type=int, default=640)
    ap.add_argument("--max-tokens", type=int, default=12)
    ap.add_argument("--sleep-ms", type=int, default=0)
    ap.add_argument("--reps", type=int, default=2)
    ap.add_argument("--localities", nargs="+", default=["high", "medium", "low"])
    ap.add_argument("--policies", nargs="+", default=["coarse", "sketch=2", "sketch=4", "sketch=8", "exact"])
    args = ap.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
