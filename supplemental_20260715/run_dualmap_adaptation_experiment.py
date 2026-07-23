#!/usr/bin/env python3
"""DualMap-style policy adaptation experiments for B02.

This script tests whether replacing the toy weighted dispatcher with a
DualMap-style router changes the paper conclusion. It has two parts:

1) CPU trace simulation:
   - least_load
   - dualmap_load
   - exact_affinity_dualmap
   - sketch_affinity_dualmap_K{2,4,8,16}

2) Optional live vLLM sanity check against ports 8000-8007:
   - least_load
   - dualmap_load
   - sketch_affinity_dualmap_K{2,8}
   - exact_affinity_dualmap

The implementation follows the DualMap idea at the policy level: two stable
prefix hash mappings produce two load-balanced cache-affinity candidates; the
router chooses among them and any visible affinity candidates under a simple
load-envelope rule instead of a hand-tuned weighted score.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import os
import random
import time
from collections import Counter, defaultdict

try:
    import aiohttp
except Exception:  # CPU-only mode does not need aiohttp.
    aiohttp = None


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
    keys, seen = [], set()
    for row in rows:
        for k in row:
            if k not in seen:
                seen.add(k)
                keys.append(k)
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


def h64(s: str) -> int:
    return int(hashlib.blake2b(s.encode(), digest_size=8).hexdigest(), 16)


def dual_candidates(prefix_digest: str, n_instances: int) -> tuple[int, int]:
    a = h64("dualmap_a:" + prefix_digest) % n_instances
    b = h64("dualmap_b:" + prefix_digest) % n_instances
    if b == a:
        b = (b + 1) % n_instances
    return a, b


def weighted_prefix(rng: random.Random, locality: str, n_prefixes: int) -> str:
    alpha = {"high": 1.35, "medium": 0.75, "low": 0.05}[locality]
    weights = [1.0 / ((i + 1) ** alpha) for i in range(n_prefixes)]
    idx = rng.choices(range(n_prefixes), weights=weights, k=1)[0]
    return f"p{idx:05d}"


class DualMapRouter:
    def __init__(self, policy: str, n_instances: int, K: int | None = None, load_slack: int = 2):
        self.policy = policy
        self.n_instances = n_instances
        self.K = K
        self.load_slack = load_slack
        self.loads = [0] * n_instances
        self.resident: list[set[str]] = [set() for _ in range(n_instances)]
        self.advertised: list[set[str]] = [set() for _ in range(n_instances)]
        self.demand = Counter()
        self.rr = 0
        self.reuse_hits = 0
        self.requests = 0
        self.candidate_hits = 0
        self.dual_hash_hits = 0
        self.affinity_visible_hits = 0
        self.fallback_to_load = 0
        self.fanouts = []
        self.load_samples = []

    def _least_load_round_robin_tie(self) -> int:
        min_load = min(self.loads)
        tied = [i for i, x in enumerate(self.loads) if x == min_load]
        chosen = tied[self.rr % len(tied)]
        self.rr += 1
        return chosen

    def _least_loaded(self, candidates: list[int] | None = None) -> int:
        cands = candidates if candidates else list(range(self.n_instances))
        return min(cands, key=lambda i: (self.loads[i], i))

    def _visible_affinity(self, d: str) -> list[int]:
        if self.policy.startswith("exact"):
            return [i for i in range(self.n_instances) if d in self.resident[i]]
        if self.policy.startswith("sketch"):
            return [i for i in range(self.n_instances) if d in self.advertised[i]]
        return []

    def choose(self, d: str) -> int:
        self.requests += 1
        self.demand[d] += 1
        if self.policy == "least_load":
            # A load-only balancer should not get accidental prefix affinity from
            # deterministic index tie-breaking when every instance is idle.
            chosen = self._least_load_round_robin_tie()
            self.fanouts.append(0)
            return chosen

        a, b = dual_candidates(d, self.n_instances)
        dual = [a, b]
        if d in self.resident[a] or d in self.resident[b]:
            self.dual_hash_hits += 1

        if self.policy == "dualmap_load":
            chosen = self._least_loaded(dual)
            self.fanouts.append(2)
            return chosen

        visible = self._visible_affinity(d)
        if visible:
            self.affinity_visible_hits += 1
        cands = sorted(set(dual + visible))
        self.fanouts.append(len(cands))

        min_load = min(self.loads)
        valid_affinity = [i for i in visible if self.loads[i] <= min_load + self.load_slack]
        if valid_affinity:
            self.candidate_hits += 1
            return self._least_loaded(valid_affinity)

        self.fallback_to_load += 1
        return self._least_loaded(dual)

    def finish(self, inst: int, d: str) -> None:
        if d in self.resident[inst]:
            self.reuse_hits += 1
        self.resident[inst].add(d)
        self._refresh_advertisements(inst)
        self.load_samples.append(max(self.loads) - min(self.loads))

    def _refresh_advertisements(self, inst: int) -> None:
        if self.policy.startswith("exact"):
            self.advertised[inst] = set(self.resident[inst])
        elif self.policy.startswith("sketch"):
            K = self.K or len(self.resident[inst])
            ranked = sorted(self.resident[inst], key=lambda d: (self.demand[d], d), reverse=True)
            self.advertised[inst] = set(ranked[:K])

    def metadata_bytes(self) -> int:
        return self.n_instances * LOAD_BYTES_PER_INSTANCE + sum(len(x) for x in self.advertised) * ENTRY_BYTES

    def advertised_entries(self) -> int:
        return sum(len(x) for x in self.advertised)


def run_cpu_trace(out_dir: str, seed: int) -> list[dict]:
    rows = []
    n_instances = 64
    n_requests = 12000
    n_prefixes = 1200
    policies = [
        ("least_load", None),
        ("dualmap_load", None),
        ("exact_affinity_dualmap", None),
        ("sketch_affinity_dualmap", 2),
        ("sketch_affinity_dualmap", 4),
        ("sketch_affinity_dualmap", 8),
        ("sketch_affinity_dualmap", 16),
    ]
    for locality in ["high", "medium", "low"]:
        for policy, K in policies:
            rng = random.Random(seed + hash((locality, policy, K)) % 100000)
            r = DualMapRouter(policy, n_instances, K)
            saved_ms = 0.0
            for _ in range(n_requests):
                d = weighted_prefix(rng, locality, n_prefixes)
                inst = r.choose(d)
                # Simulate short service time/load envelope. Prefix reuse avoids prefill.
                hit = d in r.resident[inst]
                if hit:
                    saved_ms += 45.0
                r.loads[inst] += 1
                # Immediate completion with mild random overlap to keep load meaningful.
                if rng.random() < 0.85:
                    r.loads[inst] -= 1
                else:
                    # Drain one random busy instance.
                    busy = [i for i, x in enumerate(r.loads) if x > 0]
                    if busy:
                        r.loads[rng.choice(busy)] -= 1
                r.finish(inst, d)
            rows.append({
                "experiment": "dualmap_cpu_trace",
                "policy": policy,
                "K": "full" if K is None else K,
                "locality": locality,
                "n_instances": n_instances,
                "n_requests": n_requests,
                "reuse_hit_rate": round(r.reuse_hits / n_requests, 4),
                "dual_hash_hit_rate": round(r.dual_hash_hits / n_requests, 4),
                "visible_affinity_hit_rate": round(r.affinity_visible_hits / n_requests, 4),
                "candidate_affinity_chosen_rate": round(r.candidate_hits / n_requests, 4),
                "fallback_to_dual_load_rate": round(r.fallback_to_load / n_requests, 4),
                "saved_ms_total": round(saved_ms, 2),
                "candidate_fanout_p95": percentile(r.fanouts, 95),
                "load_imbalance_p95": percentile(r.load_samples, 95),
                "advertised_entries_end": r.advertised_entries(),
                "metadata_snapshot_bytes_end": r.metadata_bytes(),
                "claim_relevance": "Tests whether DualMap-style routing preserves the Minimal State Sketch conclusion under a non-toy policy.",
            })
    write_csv(os.path.join(out_dir, "dualmap_cpu_trace.csv"), rows)
    return rows


def make_prefix_text(d: str, prefix_tokens: int) -> str:
    base = f"Shared workflow prefix {d}. Maintain the same task context and answer briefly. "
    return base * max(1, prefix_tokens // 12)


async def one_vllm_request(session, url: str, prefix: str, step: int, max_tokens: int) -> dict:
    t0 = time.perf_counter_ns()
    first = 0
    ok = True
    err = ""
    try:
        async with session.post(
            f"{url}/v1/chat/completions",
            json={
                "model": MODEL_ID,
                "messages": [
                    {"role": "system", "content": prefix},
                    {"role": "user", "content": f"Step {step}: produce one concise next action."},
                ],
                "max_tokens": max_tokens,
                "temperature": 0.0,
                "stream": True,
            },
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            if resp.status != 200:
                ok = False
                err = f"http_{resp.status}"
            else:
                async for raw in resp.content:
                    line = raw.decode(errors="ignore").strip()
                    if not line.startswith("data: "):
                        continue
                    payload = line[len("data: "):].strip()
                    if payload == "[DONE]":
                        break
                    if first == 0:
                        first = time.perf_counter_ns()
    except Exception as e:
        ok = False
        err = repr(e)[:120]
    t1 = time.perf_counter_ns()
    return {"ok": ok, "err": err, "ttft_ms": ((first or t1) - t0) / 1e6, "latency_ms": (t1 - t0) / 1e6}


async def run_live(out_dir: str, seed: int, n_requests: int, prefix_tokens: int, max_tokens: int) -> list[dict]:
    if aiohttp is None:
        raise RuntimeError("aiohttp is required for --live")
    rows = []
    policies = [
        ("least_load", None),
        ("dualmap_load", None),
        ("exact_affinity_dualmap", None),
        ("sketch_affinity_dualmap", 2),
        ("sketch_affinity_dualmap", 8),
    ]
    async with aiohttp.ClientSession() as session:
        for locality in ["high", "medium", "low"]:
            for policy, K in policies:
                print(f"live cell locality={locality} policy={policy} K={K}", flush=True)
                rng = random.Random(seed + hash(("live", locality, policy, K)) % 100000)
                r = DualMapRouter(policy, len(URLS), K)
                ttfts, lats, decisions = [], [], []
                ok_count = 0
                unique_prefixes = set()
                for step in range(n_requests):
                    d = weighted_prefix(rng, locality, max(32, n_requests * 2))
                    unique_prefixes.add(d)
                    t_dec = time.perf_counter_ns()
                    inst = r.choose(d)
                    decisions.append((time.perf_counter_ns() - t_dec) / 1e3)
                    res = await one_vllm_request(session, URLS[inst], make_prefix_text(d, prefix_tokens), step, max_tokens)
                    if res["ok"]:
                        ok_count += 1
                        ttfts.append(res["ttft_ms"])
                        lats.append(res["latency_ms"])
                    r.finish(inst, d)
                rows.append({
                    "experiment": "dualmap_live_vllm",
                    "policy": policy,
                    "K": "full" if K is None else K,
                    "locality": locality,
                    "n_requests": n_requests,
                    "n_success": ok_count,
                    "success_rate": round(ok_count / n_requests, 4),
                    "unique_prefixes": len(unique_prefixes),
                    "reuse_hit_rate": round(r.reuse_hits / n_requests, 4),
                    "dual_hash_hit_rate": round(r.dual_hash_hits / n_requests, 4),
                    "visible_affinity_hit_rate": round(r.affinity_visible_hits / n_requests, 4),
                    "candidate_affinity_chosen_rate": round(r.candidate_hits / n_requests, 4),
                    "ttft_p50_ms": round(percentile(ttfts, 50), 3),
                    "ttft_p95_ms": round(percentile(ttfts, 95), 3),
                    "latency_p50_ms": round(percentile(lats, 50), 3),
                    "latency_p95_ms": round(percentile(lats, 95), 3),
                    "decision_p50_us": round(percentile(decisions, 50), 3),
                    "decision_p95_us": round(percentile(decisions, 95), 3),
                    "candidate_fanout_p95": percentile(r.fanouts, 95),
                    "advertised_entries_end": r.advertised_entries(),
                    "metadata_snapshot_bytes_end": r.metadata_bytes(),
                    "claim_relevance": "Live T4 sanity check for DualMap-style router adapted to exact/sketch affinity state.",
                })
                write_csv(os.path.join(out_dir, "dualmap_live_vllm.csv"), rows)
    return rows


def write_summary(out_dir: str, cpu_rows: list[dict], live_rows: list[dict]) -> None:
    rows = []
    for locality in ["high", "medium", "low"]:
        subset = [r for r in cpu_rows if r["locality"] == locality]
        by = {(r["policy"], str(r["K"])): r for r in subset}
        exact = by.get(("exact_affinity_dualmap", "full"), {})
        sk8 = by.get(("sketch_affinity_dualmap", "8"), {})
        dm = by.get(("dualmap_load", "full"), {})
        rows.append({
            "finding": f"cpu_{locality}",
            "dualmap_load_reuse": dm.get("reuse_hit_rate"),
            "exact_reuse": exact.get("reuse_hit_rate"),
            "sketchK8_reuse": sk8.get("reuse_hit_rate"),
            "sketchK8_vs_exact_reuse_ratio": round(float(sk8.get("reuse_hit_rate", 0)) / max(1e-9, float(exact.get("reuse_hit_rate", 0))), 4) if exact else "",
            "sketchK8_metadata_bytes": sk8.get("metadata_snapshot_bytes_end"),
            "exact_metadata_bytes": exact.get("metadata_snapshot_bytes_end"),
            "interpretation": "If Sketch remains near Exact with much less metadata, the paper conclusion survives replacing toy weighted routing with DualMap.",
        })
    if live_rows:
        for locality in ["high", "medium", "low"]:
            subset = [r for r in live_rows if r["locality"] == locality]
            by = {(r["policy"], str(r["K"])): r for r in subset}
            rows.append({
                "finding": f"live_{locality}",
                "dualmap_load_ttft_p95": by.get(("dualmap_load", "full"), {}).get("ttft_p95_ms"),
                "exact_ttft_p95": by.get(("exact_affinity_dualmap", "full"), {}).get("ttft_p95_ms"),
                "sketchK8_ttft_p95": by.get(("sketch_affinity_dualmap", "8"), {}).get("ttft_p95_ms"),
                "least_load_ttft_p95": by.get(("least_load", "full"), {}).get("ttft_p95_ms"),
                "interpretation": "Live numbers are a small sanity check; CPU trace is better for isolating policy behavior.",
            })
    write_csv(os.path.join(out_dir, "dualmap_key_findings.csv"), rows)
    with open(os.path.join(out_dir, "DUALMAP_ADAPTATION_README.md"), "w") as f:
        f.write("# DualMap Adaptation Experiment\n\n")
        f.write("This experiment replaces the earlier hand-weighted dispatcher with a DualMap-style policy.\n\n")
        f.write("Policy definitions:\n\n")
        f.write("- `least_load`: load-only baseline.\n")
        f.write("- `dualmap_load`: two stable prefix hash candidates, choose less loaded candidate.\n")
        f.write("- `exact_affinity_dualmap`: DualMap candidates plus exact visible resident-prefix candidates.\n")
        f.write("- `sketch_affinity_dualmap_K`: DualMap candidates plus bounded Sketch-visible prefix candidates.\n\n")
        f.write("The important paper question is whether Sketch remains close to Exact while using fewer advertised entries. If yes, replacing the toy weighted policy does not break the Minimal State Sketch conclusion.\n")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", default="/home/byh/B02/supplemental_20260715/dualmap_results")
    ap.add_argument("--seed", type=int, default=20260715)
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--live-n-requests", type=int, default=20)
    ap.add_argument("--prefix-tokens", type=int, default=256)
    ap.add_argument("--max-tokens", type=int, default=8)
    args = ap.parse_args()
    ensure_dir(args.out_dir)
    cpu_rows = run_cpu_trace(args.out_dir, args.seed)
    live_rows = []
    if args.live:
        live_rows = asyncio.run(run_live(args.out_dir, args.seed, args.live_n_requests, args.prefix_tokens, args.max_tokens))
    write_summary(args.out_dir, cpu_rows, live_rows)
    print(json.dumps({"out_dir": args.out_dir, "cpu_rows": len(cpu_rows), "live_rows": len(live_rows)}, indent=2))


if __name__ == "__main__":
    main()
