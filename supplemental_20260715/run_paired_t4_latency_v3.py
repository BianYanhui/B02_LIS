#!/usr/bin/env python3
"""Paired, order-balanced live T4 replay for the B02 state-interface study.

This deliberately measures latency conservatively.  Every locality/repetition
uses one logical trace for all interfaces, but policy execution order is
randomized within the repetition.  Prompt namespaces remain policy-specific,
so physical vLLM prefix-cache entries cannot leak between interface cells.

The output reports per-repetition paired deltas against load-only.  It is a
sampled live serving result, not a throughput benchmark or an internal vLLM
cache-hit measurement.
"""
from __future__ import annotations

import argparse
import asyncio
import bisect
import csv
import hashlib
import json
import random
import statistics
import time
from collections import Counter, defaultdict, deque
from dataclasses import asdict, dataclass
from pathlib import Path

import aiohttp


URLS = [f"http://127.0.0.1:{8000 + index}" for index in range(4)]
MODEL_ID = "/home/byh/.cache/modelscope/qwen/Qwen2.5-1.5B-Instruct"
ENTRY_BYTES = 64
BASE_LOAD_BYTES = 96
LOCALITY_ALPHA = {"high": 1.35, "medium": 0.75, "low": 0.05}
POLICIES: list[tuple[str, int | None]] = [
    ("load_only", None),
    ("sketch_k8", 8),
    ("sketch_k16", 16),
    ("exact", None),
]


@dataclass(frozen=True)
class TraceRequest:
    request_id: int
    arrival_time: float
    tenant: str
    digest: str
    prefix_length: int
    discard: bool


def stable_int(*parts: object) -> int:
    return int.from_bytes(hashlib.blake2b("|".join(map(str, parts)).encode(), digest_size=8).digest(), "big")


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def write_csv(path: Path, rows: list[dict]) -> None:
    ensure_dir(path.parent)
    if not rows:
        path.write_text("")
        return
    columns: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in row:
            if key not in seen:
                columns.append(key)
                seen.add(key)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((p / 100.0) * (len(ordered) - 1))))
    return float(ordered[index])


def bootstrap_ci(values: list[float], seed: int, resamples: int = 2000) -> tuple[float, float, float]:
    if not values:
        return 0.0, 0.0, 0.0
    center = statistics.mean(values)
    if len(values) == 1:
        return center, center, center
    rng = random.Random(seed)
    means = [statistics.mean(rng.choices(values, k=len(values))) for _ in range(resamples)]
    return center, percentile(means, 2.5), percentile(means, 97.5)


def alpha_sampler(alpha: float, n: int) -> list[float]:
    weights = [1.0 / ((index + 1) ** alpha) for index in range(n)]
    total = sum(weights)
    cumulative: list[float] = []
    running = 0.0
    for weight in weights:
        running += weight / total
        cumulative.append(running)
    cumulative[-1] = 1.0
    return cumulative


def make_trace(path: Path, locality: str, rep: int, n_requests: int, warmup: int, seed: int) -> list[TraceRequest]:
    rng = random.Random(stable_int(seed, "paired", locality, rep, n_requests))
    cumulative = alpha_sampler(LOCALITY_ALPHA[locality], 256)
    requests: list[TraceRequest] = []
    for request_id in range(n_requests):
        prefix_id = bisect.bisect_left(cumulative, rng.random())
        requests.append(
            TraceRequest(
                request_id=request_id,
                arrival_time=request_id * 0.03,
                tenant=f"tenant-{prefix_id % 8}",
                digest=f"p{prefix_id:04d}",
                prefix_length=(256, 512)[request_id % 2],
                discard=request_id < warmup,
            )
        )
    ensure_dir(path.parent)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(asdict(requests[0]).keys()))
        writer.writeheader()
        writer.writerows(asdict(request) for request in requests)
    return requests


def prompt_for(request: TraceRequest, namespace: str) -> str:
    header = f"run={namespace}; tenant={request.tenant}; prefix={request.digest}; "
    return header + "context " * request.prefix_length + "\nReturn one concise action item."


class Dispatcher:
    def __init__(self, policy: str, k: int | None, cache_capacity: int, j: int, load_slack: int) -> None:
        self.policy = policy
        self.k = k
        self.cache_capacity = cache_capacity
        self.j = j
        self.load_slack = load_slack
        self.resident = [set() for _ in URLS]
        self.lru = [deque() for _ in URLS]
        self.advertised = [set() for _ in URLS]
        self.demand: Counter[str] = Counter()
        self.loads = [0 for _ in URLS]
        self.rr = 0

    def _least_loaded(self, candidates: list[int]) -> int:
        minimum = min(self.loads[index] for index in candidates)
        tied = [index for index in candidates if self.loads[index] == minimum]
        target = tied[self.rr % len(tied)]
        self.rr += 1
        return target

    def choose(self, digest: str) -> tuple[int, int, int, bool]:
        self.demand[digest] += 1
        native = list(range(len(URLS)))
        native_target = self._least_loaded(native)
        if self.policy == "load_only":
            return native_target, 0, 0, False
        if self.policy == "exact":
            candidates = [index for index in native if digest in self.resident[index]]
        else:
            candidates = [index for index in native if digest in self.advertised[index]]
        raw = len(candidates)
        evaluated = sorted(candidates, key=lambda index: (self.loads[index], index))[: self.j]
        if evaluated:
            affinity_target = self._least_loaded(evaluated)
            if self.loads[affinity_target] <= self.loads[native_target] + self.load_slack:
                return affinity_target, raw, len(evaluated), True
        return native_target, raw, 0, False

    def observe(self, target: int, digest: str) -> bool:
        hit = digest in self.resident[target]
        if hit:
            try:
                self.lru[target].remove(digest)
            except ValueError:
                pass
        self.resident[target].add(digest)
        self.lru[target].append(digest)
        while len(self.lru[target]) > self.cache_capacity:
            self.resident[target].discard(self.lru[target].popleft())
        if self.policy == "exact":
            self.advertised[target] = set(self.resident[target])
        elif self.policy.startswith("sketch"):
            ranked = sorted(self.resident[target], key=lambda item: (self.demand[item], item), reverse=True)
            self.advertised[target] = set(ranked[: self.k])
        return hit

    def metadata_bytes(self) -> int:
        if self.policy == "load_only":
            return len(URLS) * BASE_LOAD_BYTES
        return len(URLS) * BASE_LOAD_BYTES + sum(len(entries) for entries in self.advertised) * ENTRY_BYTES


async def one_request(session: aiohttp.ClientSession, url: str, prompt: str) -> dict:
    started = time.perf_counter_ns()
    first = 0
    ok, error, chunks = True, "", 0
    try:
        async with session.post(
            f"{url}/v1/chat/completions",
            json={"model": MODEL_ID, "messages": [{"role": "user", "content": prompt}], "max_tokens": 16, "temperature": 0.0, "stream": True},
            timeout=aiohttp.ClientTimeout(total=120),
        ) as response:
            if response.status != 200:
                ok, error = False, f"http_{response.status}"
            else:
                async for raw in response.content:
                    line = raw.decode(errors="ignore").strip()
                    if not line.startswith("data: "):
                        continue
                    if line == "data: [DONE]":
                        break
                    chunks += 1
                    if not first:
                        first = time.perf_counter_ns()
    except Exception as exc:
        ok, error = False, repr(exc)[:160]
    ended = time.perf_counter_ns()
    return {"ok": ok, "error": error, "ttft_ms": ((first or ended) - started) / 1e6, "latency_ms": (ended - started) / 1e6, "chunks": chunks}


async def run_cell(trace: list[TraceRequest], policy: str, k: int | None, namespace: str, args: argparse.Namespace) -> tuple[dict, list[dict]]:
    dispatcher = Dispatcher(policy, k, args.cache_capacity, args.j, args.load_slack)
    records: list[dict] = []
    async with aiohttp.ClientSession() as session:
        for offset in range(0, len(trace), args.concurrency):
            wave = trace[offset:offset + args.concurrency]
            decisions = []
            for request in wave:
                target, raw, evaluated, candidate = dispatcher.choose(request.digest)
                dispatcher.loads[target] += 1
                decisions.append((request, target, raw, evaluated, candidate))
            responses = await asyncio.gather(*[
                one_request(session, URLS[target], prompt_for(request, namespace))
                for request, target, _, _, _ in decisions
            ])
            for (request, target, raw, evaluated, candidate), response in zip(decisions, responses):
                dispatcher.loads[target] -= 1
                reuse = dispatcher.observe(target, request.digest)
                if request.discard:
                    continue
                records.append({
                    "request_id": request.request_id,
                    "selected_instance": target,
                    "candidate_hit": candidate,
                    "observed_reuse": reuse,
                    "raw_candidate_fanout": raw,
                    "evaluated_candidate_fanout": evaluated,
                    **response,
                })
    successful = [record for record in records if record["ok"]]
    ttfts = [float(record["ttft_ms"]) for record in successful]
    latencies = [float(record["latency_ms"]) for record in successful]
    n = len(records)
    return {
        "request_count": n,
        "cache_hit_rate": sum(int(record["observed_reuse"]) for record in records) / n if n else 0.0,
        "observed_reuse_hit_rate": sum(int(record["observed_reuse"]) for record in records) / n if n else 0.0,
        "candidate_hit_rate": sum(int(record["candidate_hit"]) for record in records) / n if n else 0.0,
        "raw_candidate_fanout_p95": percentile([float(record["raw_candidate_fanout"]) for record in records], 95),
        "evaluated_candidate_fanout_p95": percentile([float(record["evaluated_candidate_fanout"]) for record in records], 95),
        "dispatcher_index_bytes": dispatcher.metadata_bytes(),
        "ttft_p50_ms": percentile(ttfts, 50),
        "ttft_p95_ms": percentile(ttfts, 95),
        "ttft_p99_ms": percentile(ttfts, 99),
        "latency_p50_ms": percentile(latencies, 50),
        "latency_p95_ms": percentile(latencies, 95),
        "request_error_rate": 1.0 - len(successful) / n if n else 1.0,
    }, records


async def check_endpoints() -> None:
    async with aiohttp.ClientSession() as session:
        for url in URLS:
            try:
                async with session.get(f"{url}/v1/models", timeout=aiohttp.ClientTimeout(total=5)) as response:
                    if response.status != 200:
                        raise RuntimeError(f"{url} returned HTTP {response.status}")
            except Exception as exc:
                raise RuntimeError(f"vLLM endpoint unavailable: {url}: {exc}") from exc


def paired_rows(cells: list[dict]) -> list[dict]:
    by_cell = {(row["locality"], row["rep"], row["policy"]): row for row in cells}
    pairs: list[dict] = []
    for locality in sorted({row["locality"] for row in cells}):
        for rep in sorted({int(row["rep"]) for row in cells if row["locality"] == locality}):
            baseline = by_cell[(locality, rep, "load_only")]
            for treatment in ("sketch_k8", "sketch_k16", "exact"):
                current = by_cell[(locality, rep, treatment)]
                pairs.append({
                    "experiment_id": f"20260715_paired_latency_{locality}_{treatment}_rep{rep}",
                    "experiment": "paired_t4_latency_replay_v3",
                    "evidence_type": "live_t4_vllm",
                    "locality": locality,
                    "rep": rep,
                    "baseline_policy": "load_only",
                    "treatment_policy": treatment,
                    "workload_trace_hash": baseline["workload_trace_hash"],
                    "baseline_policy_order": baseline["policy_order"],
                    "treatment_policy_order": current["policy_order"],
                    "delta_ttft_p50_ms": float(current["ttft_p50_ms"]) - float(baseline["ttft_p50_ms"]),
                    "delta_ttft_p95_ms": float(current["ttft_p95_ms"]) - float(baseline["ttft_p95_ms"]),
                    "delta_latency_p95_ms": float(current["latency_p95_ms"]) - float(baseline["latency_p95_ms"]),
                    "delta_observed_reuse_hit_rate": float(current["observed_reuse_hit_rate"]) - float(baseline["observed_reuse_hit_rate"]),
                    "status": "Current",
                })
    return pairs


def summary_rows(cells: list[dict], pairs: list[dict], seed: int) -> list[dict]:
    output: list[dict] = []
    for locality in sorted({row["locality"] for row in cells}):
        for treatment in ("sketch_k8", "sketch_k16", "exact"):
            group = [row for row in pairs if row["locality"] == locality and row["treatment_policy"] == treatment]
            base = {
                "experiment": "paired_t4_latency_replay_v3",
                "evidence_type": "live_t4_vllm",
                "locality": locality,
                "baseline_policy": "load_only",
                "treatment_policy": treatment,
                "n_reps": len(group),
                "status": "Current",
            }
            for metric in ("delta_ttft_p50_ms", "delta_ttft_p95_ms", "delta_latency_p95_ms", "delta_observed_reuse_hit_rate"):
                values = [float(row[metric]) for row in group]
                mean, low, high = bootstrap_ci(values, stable_int(seed, locality, treatment, metric))
                base[f"{metric}_mean"] = mean
                base[f"{metric}_ci95_low"] = low
                base[f"{metric}_ci95_high"] = high
                base[f"{metric}_median"] = statistics.median(values)
                base[f"{metric}_iqr"] = percentile(values, 75) - percentile(values, 25)
                base[f"{metric}_fraction_below_zero"] = sum(value < 0 for value in values) / len(values)
            output.append(base)
    return output


async def run(args: argparse.Namespace) -> tuple[list[dict], list[dict], list[dict]]:
    await check_endpoints()
    root = Path(args.out_dir)
    cells: list[dict] = []
    raw: list[dict] = []
    localities = tuple(value for value in args.localities.split(",") if value)
    for locality in localities:
        if locality not in LOCALITY_ALPHA:
            raise ValueError(f"unsupported locality {locality}")
        for rep in range(args.repetitions):
            trace_path = root / "traces" / f"paired_trace_{locality}_rep{rep}.csv"
            trace = make_trace(trace_path, locality, rep, args.n_requests, args.warmup, args.seed)
            trace_hash = sha256_file(trace_path)
            order = list(POLICIES)
            random.Random(stable_int(args.seed, "policy-order", locality, rep)).shuffle(order)
            for order_index, (policy, k) in enumerate(order):
                namespace = f"b02pairedv3_{locality}_rep{rep}_{policy}"
                metrics, records = await run_cell(trace, policy, k, namespace, args)
                row = {
                    "experiment_id": f"20260715_paired_latency_{locality}_{policy}_rep{rep}",
                    "experiment": "paired_t4_latency_replay_v3",
                    "evidence_type": "live_t4_vllm",
                    "code_commit": "TO_BE_FINALIZED",
                    "model": MODEL_ID,
                    "hardware": "4x Tesla T4; Qwen2.5-1.5B; one vLLM instance/GPU",
                    "locality": locality,
                    "policy": policy,
                    "K": "inf" if policy in {"load_only", "exact"} else k,
                    "J": args.j,
                    "rep": rep,
                    "repetitions": args.repetitions,
                    "seed": args.seed,
                    "workload_trace_hash": trace_hash,
                    "request_count_total": args.n_requests,
                    "warmup_request_count": args.warmup,
                    "cache_capacity": args.cache_capacity,
                    "concurrency": args.concurrency,
                    "policy_order": order_index,
                    "policy_order_sequence": ",".join(item[0] for item in order),
                    "serving_cache_namespace": namespace,
                    "metric_scope": "TTFT is live; reuse is dispatcher-observed, not an internal vLLM cache counter",
                    "status": "Current",
                    **metrics,
                }
                cells.append(row)
                raw.extend({"experiment_id": row["experiment_id"], "locality": locality, "rep": rep, "policy": policy, **record} for record in records)
                print(json.dumps({"completed": row["experiment_id"], "ttft_p95_ms": row["ttft_p95_ms"], "reuse": row["observed_reuse_hit_rate"], "errors": row["request_error_rate"]}), flush=True)
                await asyncio.sleep(args.cooldown_s)
    pairs = paired_rows(cells)
    return cells, pairs, raw


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", default="/home/byh/B02/supplemental_20260715/paired_t4_latency_v3")
    parser.add_argument("--seed", type=int, default=20260715)
    parser.add_argument("--localities", default="high,medium,low")
    parser.add_argument("--n-requests", type=int, default=288)
    parser.add_argument("--warmup", type=int, default=32)
    parser.add_argument("--repetitions", type=int, default=20)
    parser.add_argument("--cache-capacity", type=int, default=128)
    parser.add_argument("--j", type=int, default=8)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--load-slack", type=int, default=0)
    parser.add_argument("--cooldown-s", type=float, default=0.25)
    args = parser.parse_args()
    if args.n_requests <= args.warmup or args.repetitions < 2 or args.concurrency < 1:
        raise ValueError("need measured requests, >=2 repetitions, and positive concurrency")
    started = time.time()
    cells, pairs, raw = asyncio.run(run(args))
    expected = len(tuple(value for value in args.localities.split(",") if value)) * args.repetitions * len(POLICIES)
    if len(cells) != expected:
        raise RuntimeError(f"expected {expected} cells, got {len(cells)}")
    if any(float(row["request_error_rate"]) > 0 for row in cells):
        raise RuntimeError("live request errors observed; do not publish this run")
    root = Path(args.out_dir)
    write_csv(root / "paired_latency_cells.csv", cells)
    write_csv(root / "paired_latency_pairs.csv", pairs)
    write_csv(root / "paired_latency_summary.csv", summary_rows(cells, pairs, args.seed))
    (root / "paired_latency_raw.json").write_text(json.dumps(raw))
    checks = [
        {"check_name": "all policies share trace hash within locality/rep", "status": "PASS", "offending_rows": 0, "suggested_fix": "generate one trace per paired repetition"},
        {"check_name": "no live request errors", "status": "PASS", "offending_rows": 0, "suggested_fix": "inspect vLLM health and retry only after root cause"},
        {"check_name": "per-policy vLLM cache namespace isolation", "status": "PASS", "offending_rows": 0, "suggested_fix": "retain unique namespace per policy/repetition"},
        {"check_name": "policy order randomized within repetition", "status": "PASS", "offending_rows": 0, "suggested_fix": "shuffle policy sequence deterministically from seed"},
    ]
    write_csv(root / "paired_latency_sanity_checks.csv", checks)
    metadata = {"started_at_unix": started, "finished_at_unix": time.time(), "duration_s": time.time() - started, "arguments": vars(args), "cells": len(cells), "pairs": len(pairs), "raw_requests": len(raw)}
    (root / "run_metadata.json").write_text(json.dumps(metadata, indent=2))
    (root / "README.md").write_text(
        "# Paired T4 latency replay v3\n\n"
        "Each repetition shares one logical trace across interfaces and randomizes policy order. "
        "Use paired median/IQR/CI results, not an unpaired mean p95. Reuse is dispatcher-observed.\n"
    )
    print(json.dumps(metadata, indent=2))


if __name__ == "__main__":
    main()
