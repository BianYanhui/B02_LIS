#!/usr/bin/env python3
"""Reduced cross-system state-view comparison on four T4 vLLM workers.

Supplementary experiment (item 1, 2026-07-20) for the B02 paper.

Question (reviewer): the paper critiques the cache-state semantics of SGLang
Router and Preble but never compares latency against them.  Whole-stack
head-to-head would confound backend differences (paper Section 4.5), so this
runner locks the backend (the same four vLLM 0.10.2 workers), locks the native
policy (least-loaded placement, identical for every arm), and varies ONLY the
state view, in the spirit of the paper's own Section 2.1 ("Load-Only, Exact
Affinity, and Sketch should be compared under the same policy logic"):

* load_only            - no affinity state (anchor)
* sglang_approx        - SGLang-Router-style router-side learned prefix state:
                         the router keeps its own prefix->worker map built only
                         from its own routing history with an ASSUMED capacity
                         larger than the real one; it receives no eviction
                         notification and runs no version/lifetime validation.
                         When the real LRU evicts, its belief goes stale.
* preble_global        - Preble-style global prefix view: eviction-aware global
                         residency (advertised = full entries) with aggressive
                         longest-match-first scheduling (load only tie-breaks);
                         no net-benefit guard.
* sketch_coverage_k16  - Minimal State Sketch K=16 + net-benefit guard (ours)
* exact                - full affinity directory + net-benefit guard (anchor)

Two workload variants expose the semantic difference:

* normal    (alpha=0.55, 96 prefixes, util 0.85, real capacity 128): no
            capacity pressure, so approximate state never goes stale;
* eviction  (alpha=0.55, 384 prefixes, util 0.50, real capacity 64, assumed
            capacity 256): physical KV capacity binds, real LRU evicts, and
            router-side approximate belief measurably diverges from the
            physical cached tokens vLLM reports.

Per-request records carry believed coverage (selected_coverage_tokens) and
physical truth (vllm_cached_tokens), so stale-belief rate is computed from
raw data.  Stock harness files are untouched; the dispatch core subclasses
the V4 Dispatcher and reuses V5 trace/metric helpers.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import random
import statistics
import sys
import time
from collections import deque
from pathlib import Path

sys.path.insert(0, "/home/byh/B02/supplemental_20260715")

from run_fixed_prompt_t4_replay_v4 import (  # noqa: E402
    URLS,
    Dispatcher,
    check_endpoints,
    ensure_dir,
    git_commit,
    metric_snapshot,
    one_request,
    percentile,
    prompt_for,
    sha256_file,
    stable_int,
    validate_records,
    write_csv,
    MODEL_ID,
)
from run_live_k_tradeoff_v5 import (  # noqa: E402
    extra_checks,
    make_difficult_trace,
    make_pairs,
    make_summary,
)

POLICIES: list[tuple[str, int | None, str]] = [
    ("load_only", None, "net_benefit"),
    ("sglang_approx", None, "sglang_approx"),
    ("preble_global", None, "preble_global"),
    ("sketch_coverage_k16", 16, "net_benefit"),
    ("exact", None, "net_benefit"),
]

STALE_GAP_TOKENS = 1024


class CrossSystemDispatcher(Dispatcher):
    """V4 dispatcher plus SGLang-style approximate and Preble-style views."""

    def __init__(self, *args, view_mode: str = "net_benefit", assumed_capacity: int = 512, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        if view_mode not in ("net_benefit", "sglang_approx", "preble_global"):
            raise ValueError(f"unknown view_mode: {view_mode}")
        self.view_mode = view_mode
        self.assumed_capacity = assumed_capacity
        # Router-side belief state (only used by sglang_approx).
        self.belief: list[dict[str, int]] = [dict() for _ in URLS]
        self.belief_lru: list[deque[str]] = [deque() for _ in URLS]

    def choose(self, request):  # noqa: D102 - mirrors parent contract
        self.demand[request.digest] += 1
        native = self._least_loaded(list(range(len(URLS))))
        if self.view_mode == "sglang_approx":
            native_belief = self.belief[native].get(request.digest, 0)
            candidates = [(index, self.belief[index].get(request.digest, 0)) for index in range(len(URLS))]
            candidates = [(index, coverage) for index, coverage in candidates if coverage > 0]
            candidates.sort(key=lambda item: (-item[1], self.loads[item[0]], item[0]))
            raw = len(candidates)
            evaluated = candidates[: self.j]
            if evaluated:
                best_coverage = evaluated[0][1]
                best = [index for index, coverage in evaluated if coverage == best_coverage]
                target = self._least_loaded(best)
                if best_coverage > native_belief:
                    # No guard, no validation: the router trusts its learned map.
                    return target, raw, len(evaluated), True, best_coverage, 0.0
            return native, raw, len(evaluated), False, native_belief, 0.0
        native_coverage = self.entries[native].get(request.digest, 0)
        if self.policy == "load_only":
            return native, 0, 0, False, native_coverage, 0.0
        visible = self.entries  # preble_global and exact both see full residency
        if self.policy.startswith("sketch"):
            visible = self.advertised
        candidates = [(index, visible[index].get(request.digest, 0)) for index in range(len(URLS))]
        candidates = [(index, coverage) for index, coverage in candidates if coverage > 0]
        candidates.sort(key=lambda item: (-item[1], self.loads[item[0]], item[0]))
        raw = len(candidates)
        evaluated = candidates[: self.j]
        if evaluated:
            best_coverage = evaluated[0][1]
            best = [index for index, coverage in evaluated if coverage == best_coverage]
            target = self._least_loaded(best)
            incremental_tokens = max(0, best_coverage - native_coverage)
            estimated_net_ms = incremental_tokens / self.prefill_tokens_per_ms
            estimated_net_ms -= max(0, self.loads[target] - self.loads[native]) * self.queue_penalty_ms
            if self.view_mode == "preble_global":
                if incremental_tokens > 0:
                    # Preble-style: locality first, no net-benefit guard.
                    return target, raw, len(evaluated), True, best_coverage, estimated_net_ms
            elif estimated_net_ms > self.guard_ms:
                return target, raw, len(evaluated), True, best_coverage, estimated_net_ms
        return native, raw, len(evaluated), False, native_coverage, 0.0

    def observe(self, target: int, request) -> int:
        if self.view_mode == "sglang_approx":
            prior = self.belief[target].get(request.digest, 0)
            if request.digest in self.belief[target]:
                try:
                    self.belief_lru[target].remove(request.digest)
                except ValueError:
                    pass
            self.belief[target][request.digest] = request.prefix_token_target
            self.belief_lru[target].append(request.digest)
            while len(self.belief_lru[target]) > self.assumed_capacity:
                evicted = self.belief_lru[target].popleft()
                self.belief[target].pop(evicted, None)
            return prior
        return super().observe(target, request)

    def metadata_bytes(self) -> int:
        if self.view_mode == "sglang_approx":
            return len(URLS) * 96 + sum(len(values) for values in self.belief) * 64
        if self.view_mode == "preble_global":
            # Preble's global view tracks full residency, same volume as Exact.
            return len(URLS) * 96 + sum(len(values) for values in self.entries) * 64
        return super().metadata_bytes()


async def run_cell(trace, policy, k, view_mode, cache_salt, args):
    """V4 run_cell with the cross-system dispatcher (copied, stock untouched)."""
    import aiohttp

    dispatcher = CrossSystemDispatcher(
        policy, k, args.cache_capacity, args.j,
        args.prefill_tokens_per_ms, args.queue_penalty_ms, args.guard_ms,
        view_mode=view_mode, assumed_capacity=args.sglang_assumed_capacity,
    )
    records: list[dict] = []
    async with aiohttp.ClientSession() as session:
        before = await metric_snapshot(session)
        for offset in range(0, len(trace), args.concurrency):
            wave = trace[offset:offset + args.concurrency]
            decisions = []
            for request in wave:
                target, raw, evaluated, selected_affinity, coverage, expected_net = dispatcher.choose(request)
                dispatcher.loads[target] += 1
                decisions.append((request, target, raw, evaluated, selected_affinity, coverage, expected_net))
            responses = await asyncio.gather(*[
                one_request(session, URLS[target], prompt_for(request), cache_salt, args.output_tokens, args.max_request_attempts)
                for request, target, *_ in decisions
            ])
            for (request, target, raw, evaluated, selected_affinity, coverage, expected_net), response in zip(decisions, responses):
                dispatcher.loads[target] -= 1
                prior = dispatcher.observe(target, request)
                if request.discard:
                    continue
                prompt = prompt_for(request)
                records.append({
                    "request_id": request.request_id,
                    "selected_instance": target,
                    "candidate_hit": selected_affinity,
                    "dispatcher_prior_coverage_tokens": prior,
                    "selected_coverage_tokens": coverage,
                    "expected_net_prefill_ms": expected_net,
                    "raw_candidate_fanout": raw,
                    "evaluated_candidate_fanout": evaluated,
                    "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
                    **response,
                })
        after = await metric_snapshot(session)
    success = [row for row in records if row["ok"]]
    ttfts = [float(row["ttft_ms"]) for row in success]
    latencies = [float(row["latency_ms"]) for row in success]
    metric_delta = {key: after[key] - before[key] for key in before}
    n = len(records)
    return {
        "request_count": n,
        "candidate_hit_rate": sum(bool(row["candidate_hit"]) for row in records) / n if n else 0.0,
        "dispatcher_reuse_rate": sum(float(row["dispatcher_prior_coverage_tokens"]) > 0 for row in records) / n if n else 0.0,
        "mean_selected_coverage_tokens": statistics.mean(float(row["selected_coverage_tokens"]) for row in records) if records else 0.0,
        "vllm_cached_token_rate": sum(float(row["vllm_cached_tokens"] or 0) > 0 for row in records) / n if n else 0.0,
        "vllm_cached_tokens_total": sum(float(row["vllm_cached_tokens"] or 0) for row in records),
        "stale_belief_rate": sum(
            float(row["selected_coverage_tokens"]) - float(row["vllm_cached_tokens"] or 0) > STALE_GAP_TOKENS
            for row in records
        ) / n if n else 0.0,
        "retried_request_count": sum(int(row["attempt_count"]) > 1 for row in records),
        "metric_prefix_cache_hits_delta": metric_delta["vllm:prefix_cache_hits_total"],
        "metric_prefix_cache_queries_delta": metric_delta["vllm:prefix_cache_queries_total"],
        "metric_prompt_tokens_delta": metric_delta["vllm:prompt_tokens_total"],
        "raw_candidate_fanout_p95": percentile([float(row["raw_candidate_fanout"]) for row in records], 95),
        "evaluated_candidate_fanout_p95": percentile([float(row["evaluated_candidate_fanout"]) for row in records], 95),
        "dispatcher_index_bytes": dispatcher.metadata_bytes(),
        "ttft_p50_ms": percentile(ttfts, 50),
        "ttft_p95_ms": percentile(ttfts, 95),
        "ttft_p99_ms": percentile(ttfts, 99),
        "latency_p50_ms": percentile(latencies, 50),
        "latency_p95_ms": percentile(latencies, 95),
        "request_error_rate": 1.0 - len(success) / n if n else 1.0,
    }, records


def enrich_metrics(metrics: dict, records: list[dict]) -> dict:
    success = [record for record in records if record["ok"]]
    metrics = dict(metrics)
    metrics["mean_ttft_ms"] = statistics.mean(float(record["ttft_ms"]) for record in success) if success else 0.0
    metrics["estimated_saved_prefill_tokens_total"] = sum(
        max(0.0, float(record["selected_coverage_tokens"]) - 256.0) for record in records
    )
    metrics["physical_cached_tokens_total"] = metrics["vllm_cached_tokens_total"]
    return metrics


async def run(args: argparse.Namespace):
    await check_endpoints()
    root = Path(args.out_dir)
    cells: list[dict] = []
    raw: list[dict] = []
    for rep in range(args.repetitions):
        trace_path = root / "traces" / f"crosssystem_trace_rep{rep}.csv"
        trace = make_difficult_trace(
            trace_path, rep, args.n_requests, args.warmup, args.prefix_tokens,
            args.active_prefixes, args.zipf_alpha, args.seed,
        )
        trace_hash = sha256_file(trace_path)
        order = list(POLICIES)
        random.Random(stable_int(args.seed, "crosssystem-policy-order", rep)).shuffle(order)
        for order_index, (policy, k, view_mode) in enumerate(order):
            salt = f"b02-x1:crosssystem:{args.variant}:rep{rep}:{policy}"
            metrics, records = await run_cell(trace, policy, k, view_mode, salt, args)
            metrics = enrich_metrics(metrics, records)
            row = {
                "experiment_id": f"20260720_crosssystem_{args.variant}_{policy}_rep{rep}",
                "experiment": "live_crosssystem_v1",
                "evidence_type": "live_t4_vllm",
                "code_commit": git_commit(),
                "model": MODEL_ID,
                "hardware": "4x Tesla T4; Qwen2.5-1.5B; one vLLM instance/GPU",
                "variant": args.variant,
                "policy": policy,
                "view_mode": view_mode,
                "K": "inf" if k is None else k,
                "J": args.j,
                "admission": "router-side learned map (assumed capacity)" if view_mode == "sglang_approx" else (
                    "global exact view" if view_mode == "preble_global" else (
                        "online coverage-first (demand x coverage)" if k is not None else "n/a")),
                "routing_guard": "none" if view_mode in ("sglang_approx", "preble_global") else "net_prefill_benefit",
                "sglang_assumed_capacity": args.sglang_assumed_capacity,
                "rep": rep,
                "repetitions": args.repetitions,
                "seed": args.seed,
                "workload_trace_hash": trace_hash,
                "request_count_total": args.n_requests,
                "warmup_request_count": args.warmup,
                "measured_request_count": args.n_requests - args.warmup,
                "active_prefixes": args.active_prefixes,
                "zipf_alpha": args.zipf_alpha,
                "cache_capacity": args.cache_capacity,
                "concurrency": args.concurrency,
                "prefix_token_target": args.prefix_tokens,
                "fixed_output_tokens": args.output_tokens,
                "generation_mode": "greedy_temperature0_min_tokens_eq_max_tokens_ignore_eos",
                "policy_order": order_index,
                "policy_order_sequence": ",".join(item[0] for item in order),
                "vllm_cache_salt": salt,
                "semantic_prompt_contains_policy": False,
                "metric_scope": "Live T4 TTFT and vLLM cached-token telemetry. Believed coverage (selected_coverage_tokens) vs physical cached tokens yields stale-belief rate.",
                "status": "Current",
                **metrics,
            }
            cells.append(row)
            raw.extend({
                "experiment_id": row["experiment_id"], "locality": "crosssystem", "rep": rep, "policy": policy,
                "J": args.j, **record,
            } for record in records)
            write_csv(root / "_checkpoint_cells.csv", cells)
            (root / "_checkpoint_raw.json").write_text(json.dumps(raw))
            print(json.dumps({
                "completed": row["experiment_id"], "p50_ttft_ms": row["ttft_p50_ms"],
                "candidate_hit_rate": row["candidate_hit_rate"], "stale_belief_rate": row["stale_belief_rate"],
                "cached_tokens": row["physical_cached_tokens_total"], "errors": row["request_error_rate"],
            }), flush=True)
            await asyncio.sleep(args.cooldown_s)
    pair_policies = [(policy, k) for policy, k, _ in POLICIES]
    return cells, make_pairs(cells, pair_policies), raw


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", default="/home/byh/B02/supplemental_20260720/crosssystem_normal")
    parser.add_argument("--variant", default="normal")
    parser.add_argument("--seed", type=int, default=20260720)
    parser.add_argument("--repetitions", type=int, default=12)
    parser.add_argument("--n-requests", type=int, default=192)
    parser.add_argument("--warmup", type=int, default=64)
    parser.add_argument("--active-prefixes", type=int, default=96)
    parser.add_argument("--zipf-alpha", type=float, default=0.55)
    parser.add_argument("--cache-capacity", type=int, default=128)
    parser.add_argument("--sglang-assumed-capacity", type=int, default=512)
    parser.add_argument("--j", type=int, default=4)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--prefix-tokens", type=int, default=2048)
    parser.add_argument("--output-tokens", type=int, default=4)
    parser.add_argument("--prefill-tokens-per-ms", type=float, default=50.0)
    parser.add_argument("--queue-penalty-ms", type=float, default=2.0)
    parser.add_argument("--guard-ms", type=float, default=0.5)
    parser.add_argument("--max-request-attempts", type=int, default=3)
    parser.add_argument("--cooldown-s", type=float, default=0.05)
    parser.add_argument("--smoke", action="store_true")
    args = parser.parse_args()
    if not args.smoke:
        if args.n_requests - args.warmup < 128:
            raise ValueError("requires at least 128 measured requests per policy/repetition")
        if args.repetitions < 10:
            raise ValueError("requires at least 10 paired repetitions")
    started = time.time()
    cells, pairs, raw = asyncio.run(run(args))
    if any(float(row["request_error_rate"]) > 0 for row in cells):
        raise RuntimeError("live request error observed; do not use this run")
    checks = validate_records(raw, args.output_tokens)
    checks.extend(extra_checks(cells, raw, [(policy, k) for policy, k, _ in POLICIES]))
    if any(check["status"] != "PASS" for check in checks):
        raise RuntimeError(f"cross-system sanity checks failed: {checks}")
    root = Path(args.out_dir)
    ensure_dir(root)
    write_csv(root / "crosssystem_cells.csv", cells)
    write_csv(root / "crosssystem_pairs.csv", pairs)
    write_csv(root / "crosssystem_summary.csv", make_summary(pairs, args.seed))
    write_csv(root / "crosssystem_sanity_checks.csv", checks)
    (root / "crosssystem_raw.json").write_text(json.dumps(raw))
    metadata = {
        "started_at_unix": started, "finished_at_unix": time.time(),
        "duration_s": time.time() - started, "arguments": vars(args),
        "cells": len(cells), "pairs": len(pairs), "raw_requests": len(raw),
    }
    (root / "run_metadata.json").write_text(json.dumps(metadata, indent=2))
    (root / "README.md").write_text(
        "# Reduced cross-system state-view comparison (item 1, 2026-07-20)\n\n"
        "Same backend (4x vLLM 0.10.2 on T4), same native least-loaded policy; only the\n"
        "state view varies: load_only / sglang_approx (router-side learned map, no\n"
        "eviction notice, no validation) / preble_global (eviction-aware global view,\n"
        "locality-first scheduling) / sketch_coverage_k16 (bounded + guard) / exact.\n"
        "Variants: normal (no capacity pressure) and eviction (physical KV binds).\n"
    )
    print(json.dumps(metadata, indent=2))


if __name__ == "__main__":
    main()
