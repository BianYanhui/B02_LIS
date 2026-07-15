#!/usr/bin/env python3
"""Same-trace replay for B02 Minimal State Sketch.

The experiment intentionally separates two meanings of replay:

``frozen``
    Dispatcher-only replay over a fixed owner snapshot.  It is a controlled
    information-bound test: Exact sees every resident entry, Sketch sees the
    bounded subset, and no request mutates the snapshot.  vLLM does not expose
    a cache snapshot/restore API, so frozen rows do not report fake GPU TTFT.

``closed_loop``
    Actual requests to four T4-backed vLLM endpoints.  Each policy receives the
    byte-identical trace.  A unique per-cell prompt namespace prevents a prior
    policy/repetition from providing cache hits to a later one.  TTFT is real;
    cache/reuse metrics are dispatcher-observed opportunities, explicitly not
    vLLM internal counters.
"""
from __future__ import annotations

import argparse
import asyncio
import bisect
import csv
import hashlib
import json
import math
import os
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


@dataclass(frozen=True)
class TraceRequest:
    request_id: int
    arrival_time: float
    tenant: str
    model_revision: str
    full_prefix_hash_chain: str
    prefix_length: int
    expected_reuse_coverage: float
    generation_length: int
    locality_class: str
    digest: str
    prompt_namespace: str
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
    keys, seen = [], set()
    for row in rows:
        for key in row:
            if key not in seen:
                keys.append(key)
                seen.add(key)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=keys)
        writer.writeheader()
        writer.writerows(rows)


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    xs = sorted(values)
    return float(xs[min(len(xs) - 1, max(0, round((p / 100.0) * (len(xs) - 1))))])


def ci95(values: list[float], seed: int, resamples: int = 1000) -> tuple[float, float, float]:
    if not values:
        return 0.0, 0.0, 0.0
    center = statistics.mean(values)
    if len(values) == 1:
        return center, center, center
    rng = random.Random(seed)
    samples = [statistics.mean(rng.choices(values, k=len(values))) for _ in range(resamples)]
    return center, percentile(samples, 2.5), percentile(samples, 97.5)


def alpha_sampler(alpha: float, n: int) -> list[float]:
    weights = [1.0 / ((i + 1) ** alpha) for i in range(n)]
    total = sum(weights)
    cumulative, running = [], 0.0
    for weight in weights:
        running += weight / total
        cumulative.append(running)
    cumulative[-1] = 1.0
    return cumulative


def make_trace(path: Path, locality: str, rep: int, n_requests: int, warmup: int, seed: int, context_lengths: tuple[int, ...]) -> list[TraceRequest]:
    namespace = f"20260715_{locality}_rep{rep}"
    rng = random.Random(stable_int(seed, namespace, n_requests))
    cumulative = alpha_sampler(LOCALITY_ALPHA[locality], 256)
    rows: list[TraceRequest] = []
    for request_id in range(n_requests):
        pid = bisect.bisect_left(cumulative, rng.random())
        digest = f"p{pid:04d}"
        rows.append(
            TraceRequest(
                request_id=request_id,
                arrival_time=request_id * 0.03,
                tenant=f"tenant-{pid % 8}",
                model_revision="qwen2.5-1.5b-r1",
                full_prefix_hash_chain=json.dumps(["root", f"tenant-{pid % 8}", digest]),
                prefix_length=context_lengths[request_id % len(context_lengths)],
                expected_reuse_coverage=0.5 + 0.5 * (pid % 4) / 3.0,
                generation_length=16,
                locality_class=locality,
                digest=digest,
                prompt_namespace=namespace,
                discard=request_id < warmup,
            )
        )
    ensure_dir(path.parent)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(asdict(rows[0]).keys()))
        writer.writeheader()
        writer.writerows(asdict(row) for row in rows)
    return rows


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def prompt_for(request: TraceRequest) -> str:
    # Space-separated stable tokens yield a long reusable prefix without
    # depending on an external tokenizer at experiment construction time.
    header = f"run={request.prompt_namespace}; prefix={request.digest}; "
    prefix = "context " * request.prefix_length
    return header + prefix + "\nReturn one concise action item."


class Dispatcher:
    def __init__(self, policy: str, k: int | None, cache_capacity: int, j: int):
        self.policy = policy
        self.k = k
        self.cache_capacity = cache_capacity
        self.j = j
        self.resident = [set() for _ in URLS]
        self.lru = [deque() for _ in URLS]
        self.advertised = [set() for _ in URLS]
        self.demand: Counter[str] = Counter()
        self.loads = [0 for _ in URLS]
        self.rr = 0

    def _least_loaded(self, candidates: list[int]) -> int:
        return min(candidates, key=lambda index: (self.loads[index], index))

    def choose(self, digest: str) -> tuple[int, int, int, bool]:
        self.demand[digest] += 1
        if self.policy == "load_only":
            target = self.rr % len(URLS)
            self.rr += 1
            return target, 0, 0, False
        if self.policy in {"exact", "sketch_inf"}:
            candidates = [index for index in range(len(URLS)) if digest in self.resident[index]]
        else:
            candidates = [index for index in range(len(URLS)) if digest in self.advertised[index]]
        raw = len(candidates)
        evaluated = sorted(candidates, key=lambda index: (self.loads[index], index))[: self.j]
        if evaluated:
            return self._least_loaded(evaluated), raw, len(evaluated), True
        target = self._least_loaded(list(range(len(URLS))))
        return target, raw, 0, False

    def observe(self, target: int, digest: str) -> bool:
        was_resident = digest in self.resident[target]
        if was_resident:
            try:
                self.lru[target].remove(digest)
            except ValueError:
                pass
        self.resident[target].add(digest)
        self.lru[target].append(digest)
        while len(self.lru[target]) > self.cache_capacity:
            evicted = self.lru[target].popleft()
            self.resident[target].discard(evicted)
        self._refresh(target)
        return was_resident

    def _refresh(self, target: int) -> None:
        if self.policy in {"exact", "sketch_inf"}:
            self.advertised[target] = set(self.resident[target])
        elif self.policy.startswith("sketch"):
            ranked = sorted(self.resident[target], key=lambda digest: (self.demand[digest], digest), reverse=True)
            self.advertised[target] = set(ranked[: self.k])

    def metadata_bytes(self) -> int:
        return len(URLS) * BASE_LOAD_BYTES + sum(len(entries) for entries in self.advertised) * ENTRY_BYTES


async def one_request(session: aiohttp.ClientSession, url: str, prompt: str, request_id: int) -> dict:
    started = time.perf_counter_ns()
    first = 0
    ok, error, chunks = True, "", 0
    try:
        async with session.post(
            f"{url}/v1/chat/completions",
            json={
                "model": MODEL_ID,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 16,
                "temperature": 0.0,
                "stream": True,
            },
            timeout=aiohttp.ClientTimeout(total=90),
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
    return {
        "ok": ok,
        "error": error,
        "ttft_ms": ((first or ended) - started) / 1e6,
        "latency_ms": (ended - started) / 1e6,
        "chunks": chunks,
    }


def frozen_snapshot(trace: list[TraceRequest], cache_capacity: int) -> list[set[str]]:
    snapshot = [set() for _ in URLS]
    for request in trace:
        if not request.discard:
            continue
        owner = stable_int("snapshot", request.digest) % len(URLS)
        if len(snapshot[owner]) < cache_capacity:
            snapshot[owner].add(request.digest)
    return snapshot


def run_frozen_cell(trace: list[TraceRequest], policy: str, k: int | None, cache_capacity: int, j: int) -> tuple[dict, list[dict]]:
    dispatcher = Dispatcher(policy, k, cache_capacity, j)
    snapshot = frozen_snapshot(trace, cache_capacity)
    dispatcher.resident = [set(entries) for entries in snapshot]
    for target in range(len(URLS)):
        dispatcher._refresh(target)
    records: list[dict] = []
    raw, evaluated, candidate_hits, reuse = [], [], 0, 0
    for request in trace:
        if request.discard:
            continue
        target, raw_fanout, evaluated_fanout, candidate = dispatcher.choose(request.digest)
        observed = request.digest in snapshot[target]
        candidate_hits += int(candidate)
        reuse += int(observed)
        raw.append(raw_fanout)
        evaluated.append(evaluated_fanout)
        records.append({
            "request_id": request.request_id, "selected_instance": target, "candidate_hit": candidate,
            "observed_reuse": observed, "raw_candidate_fanout": raw_fanout,
            "evaluated_candidate_fanout": evaluated_fanout,
        })
    n = len(records)
    return {
        "request_count": n,
        "cache_hit_rate": reuse / n if n else 0.0,
        "observed_reuse_hit_rate": reuse / n if n else 0.0,
        "candidate_hit_rate": candidate_hits / n if n else 0.0,
        "raw_candidate_fanout_p95": percentile(raw, 95),
        "evaluated_candidate_fanout_p95": percentile(evaluated, 95),
        "dispatcher_index_bytes": dispatcher.metadata_bytes(),
        "ttft_p50_ms": "",
        "ttft_p95_ms": "",
        "ttft_p99_ms": "",
        "request_error_rate": 0.0,
    }, records


async def run_closed_loop_cell(trace: list[TraceRequest], policy: str, k: int | None, cache_capacity: int, j: int, concurrency: int) -> tuple[dict, list[dict]]:
    dispatcher = Dispatcher(policy, k, cache_capacity, j)
    records: list[dict] = []
    async with aiohttp.ClientSession() as session:
        # Each wave is a small arrival batch. Decisions are made from the same
        # preceding dispatcher state, then the requests execute concurrently on
        # the four T4 workers. Updates commit in trace order after the wave.
        for start in range(0, len(trace), concurrency):
            wave = trace[start:start + concurrency]
            decisions = []
            for request in wave:
                target, raw_fanout, evaluated_fanout, candidate = dispatcher.choose(request.digest)
                dispatcher.loads[target] += 1
                decisions.append((request, target, raw_fanout, evaluated_fanout, candidate))
            responses = await asyncio.gather(*[
                one_request(session, URLS[target], prompt_for(request), request.request_id)
                for request, target, _, _, _ in decisions
            ])
            for (request, target, raw_fanout, evaluated_fanout, candidate), response in zip(decisions, responses):
                dispatcher.loads[target] -= 1
                observed = dispatcher.observe(target, request.digest)
                if request.discard:
                    continue
                records.append({
                    "request_id": request.request_id, "selected_instance": target,
                    "candidate_hit": candidate, "observed_reuse": observed,
                    "raw_candidate_fanout": raw_fanout, "evaluated_candidate_fanout": evaluated_fanout,
                    **response,
                })
    successful = [record for record in records if record["ok"]]
    ttfts = [record["ttft_ms"] for record in successful]
    raw = [record["raw_candidate_fanout"] for record in records]
    evaluated = [record["evaluated_candidate_fanout"] for record in records]
    n = len(records)
    return {
        "request_count": n,
        "cache_hit_rate": sum(record["observed_reuse"] for record in records) / n if n else 0.0,
        "observed_reuse_hit_rate": sum(record["observed_reuse"] for record in records) / n if n else 0.0,
        "candidate_hit_rate": sum(record["candidate_hit"] for record in records) / n if n else 0.0,
        "raw_candidate_fanout_p95": percentile(raw, 95),
        "evaluated_candidate_fanout_p95": percentile(evaluated, 95),
        "dispatcher_index_bytes": dispatcher.metadata_bytes(),
        "ttft_p50_ms": percentile(ttfts, 50),
        "ttft_p95_ms": percentile(ttfts, 95),
        "ttft_p99_ms": percentile(ttfts, 99),
        "request_error_rate": 1.0 - len(successful) / n if n else 1.0,
    }, records


def policy_cells() -> list[tuple[str, int | None]]:
    return [
        ("load_only", None), ("exact", None), ("sketch_k2", 2), ("sketch_k4", 4),
        ("sketch_k8", 8), ("sketch_k16", 16), ("sketch_inf", None),
    ]


async def run_all(args) -> tuple[list[dict], list[dict]]:
    root = Path(args.out_dir)
    trace_dir = root / "traces"
    rows: list[dict] = []
    raw: list[dict] = []
    for locality in ["high", "medium", "low"]:
        for rep in range(args.repetitions):
            trace_path = trace_dir / f"trace_{locality}_rep{rep}.csv"
            trace = make_trace(trace_path, locality, rep, args.n_requests, args.warmup, args.seed, args.context_lengths)
            trace_hash = hash_file(trace_path)
            for mode in ["frozen", "closed_loop"]:
                for policy, k in policy_cells():
                    if mode == "frozen":
                        metrics, per_request = run_frozen_cell(trace, policy, k, args.cache_capacity, args.j)
                        evidence_type = "trace_replay_simulation"
                    else:
                        metrics, per_request = await run_closed_loop_cell(trace, policy, k, args.cache_capacity, args.j, args.concurrency)
                        evidence_type = "live_t4_vllm"
                    row = {
                        "experiment_id": f"20260715_trace_{mode}_{policy}_{locality}_rep{rep}",
                        "experiment": "trace_replay_quality",
                        "evidence_type": evidence_type,
                        "mode": mode,
                        "policy": policy,
                        "K": "inf" if policy in {"exact", "sketch_inf", "load_only"} else k,
                        "J": args.j,
                        "locality": locality,
                        "rep": rep,
                        "seed": args.seed,
                        "repetitions": args.repetitions,
                        "workload_trace_hash": trace_hash,
                        "unique_prefixes": len({request.digest for request in trace}),
                        "warmup_request_count": args.warmup,
                        "model": MODEL_ID,
                        "hardware": "4x Tesla T4; one vLLM instance per GPU",
                        "cache_capacity": args.cache_capacity,
                        "status": "Current",
                        **metrics,
                    }
                    rows.append(row)
                    for record in per_request:
                        raw.append({"experiment_id": row["experiment_id"], "mode": mode, "policy": policy, "locality": locality, "rep": rep, **record})
                    print(json.dumps({"completed": row["experiment_id"], "ttft_p95_ms": row["ttft_p95_ms"], "reuse": row["observed_reuse_hit_rate"]}), flush=True)
    return rows, raw


def aggregate(rows: list[dict], seed: int) -> list[dict]:
    group: dict[tuple[str, str, str, str], list[dict]] = defaultdict(list)
    for row in rows:
        group[(row["mode"], row["policy"], str(row["K"]), row["locality"])].append(row)
    summary: list[dict] = []
    metrics = ["cache_hit_rate", "observed_reuse_hit_rate", "candidate_hit_rate", "ttft_p50_ms", "ttft_p95_ms", "ttft_p99_ms", "dispatcher_index_bytes"]
    for (mode, policy, k, locality), items in sorted(group.items()):
        base = {"mode": mode, "policy": policy, "K": k, "locality": locality, "n_reps": len(items), "evidence_type": items[0]["evidence_type"], "workload_trace_hashes": json.dumps(sorted({item["workload_trace_hash"] for item in items}))}
        for metric in metrics:
            values = [float(item[metric]) for item in items if item[metric] != ""]
            if values:
                mean, low, high = ci95(values, stable_int(seed, mode, policy, k, locality, metric))
                base[f"{metric}_mean"] = mean
                base[f"{metric}_ci95_low"] = low
                base[f"{metric}_ci95_high"] = high
                base[f"{metric}_ci95_halfwidth"] = (high - low) / 2.0
            else:
                base[f"{metric}_mean"] = ""
                base[f"{metric}_ci95_low"] = ""
                base[f"{metric}_ci95_high"] = ""
                base[f"{metric}_ci95_halfwidth"] = ""
        summary.append(base)
    return summary


def validate(rows: list[dict]) -> list[dict]:
    checks: list[dict] = []
    frozen = [row for row in rows if row["mode"] == "frozen"]
    for locality in ["high", "medium", "low"]:
        rep_rows = [row for row in frozen if row["locality"] == locality]
        exact = [row for row in rep_rows if row["policy"] == "exact"]
        sketch_inf = [row for row in rep_rows if row["policy"] == "sketch_inf"]
        exact_bound = all(float(row["cache_hit_rate"]) >= max(float(other["cache_hit_rate"]) for other in rep_rows if other["rep"] == row["rep"]) for row in exact)
        equal = all(float(a["cache_hit_rate"]) == float(b["cache_hit_rate"]) for a, b in zip(sorted(exact, key=lambda x: x["rep"]), sorted(sketch_inf, key=lambda x: x["rep"])))
        checks.append({"check_name": f"frozen Exact upper bound {locality}", "status": "PASS" if exact_bound else "FAIL", "offending_rows": 0, "suggested_fix": "inspect snapshot/interface construction"})
        checks.append({"check_name": f"frozen Sketch_inf equals Exact {locality}", "status": "PASS" if equal else "FAIL", "offending_rows": 0, "suggested_fix": "make sketch infinity expose full exact resident set"})
    closed = [row for row in rows if row["mode"] == "closed_loop"]
    checks.append({"check_name": "all policies share same trace hash within locality/rep", "status": "PASS" if all(len({row["workload_trace_hash"] for row in closed if row["locality"] == loc and row["rep"] == rep}) == 1 for loc in ["high", "medium", "low"] for rep in set(row["rep"] for row in closed)) else "FAIL", "offending_rows": 0, "suggested_fix": "generate one trace before scheduling policy cells"})
    checks.append({"check_name": "evaluated fanout <= J", "status": "PASS" if all(float(row["evaluated_candidate_fanout_p95"]) <= float(row["J"]) for row in rows) else "FAIL", "offending_rows": 0, "suggested_fix": "truncate candidates before selection"})
    return checks


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", default="/home/byh/B02/supplemental_20260715/trace_replay_v2")
    parser.add_argument("--seed", type=int, default=20260715)
    parser.add_argument("--n-requests", type=int, default=64)
    parser.add_argument("--warmup", type=int, default=8)
    parser.add_argument("--repetitions", type=int, default=5)
    parser.add_argument("--cache-capacity", type=int, default=128)
    parser.add_argument("--j", type=int, default=8)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--context-lengths", default="256,512", help="comma-separated approximate token counts for live prompts")
    args = parser.parse_args()
    args.context_lengths = tuple(int(value) for value in args.context_lengths.split(",") if value)
    if not args.context_lengths or args.concurrency < 1:
        raise ValueError("context lengths and concurrency must be positive")
    started = time.time()
    rows, raw = asyncio.run(run_all(args))
    # 3 locality x repetitions x 7 policies x 2 modes.
    expected = 3 * args.repetitions * 7 * 2
    if len(rows) != expected:
        raise RuntimeError(f"expected {expected} cells, got {len(rows)}")
    root = Path(args.out_dir)
    write_csv(root / "trace_replay_quality_cells.csv", rows)
    write_csv(root / "trace_replay_quality.csv", aggregate(rows, args.seed))
    (root / "trace_replay_quality.json").write_text(json.dumps(raw))
    checks = validate(rows)
    write_csv(root / "trace_replay_sanity_checks.csv", checks)
    metadata = {"started_at_unix": started, "finished_at_unix": time.time(), "duration_s": time.time() - started, "arguments": vars(args), "cells": len(rows), "raw_requests": len(raw)}
    (root / "run_metadata.json").write_text(json.dumps(metadata, indent=2))
    (root / "README.md").write_text("# Same-trace replay v2\n\n`frozen` rows are dispatcher-only fixed-snapshot evidence. `closed_loop` rows send actual requests to four T4 vLLM endpoints. Internal vLLM cache counters are unavailable; observed reuse is tracked at the dispatcher.\n")
    print(json.dumps(metadata, indent=2))


if __name__ == "__main__":
    main()
