#!/usr/bin/env python3
"""Live net-benefit / abstention guard ablation on four T4 vLLM workers.

Supplementary experiment S2 for the B02 paper (2026-07-19).

Motivation.  The paper's live K sweep (run_live_k_tradeoff_v5.py primary)
showed that an undersized K=4 Sketch *increases* mean TTFT by +44.4 ms, and
Section 4.3 concludes that "a bounded interface needs an abstention mode when
its admitted set is too sparse or concentrates affinity on already busy
owners".  Section 4.4's queue-affinity conflict evidence is a *modeled*
dispatcher replay.  What the paper does not show live is:

  1. the counterfactual - affinity-first routing with no protective guard;
  2. an abstention mode actually recovering the K=4 harm while preserving
     the K=16 benefit.

Why the stock net-benefit guard is not enough here.  In this workload every
advertised entry has uniform 2048-token coverage, so the stock guard's
net estimate (2048/50 ms - 2 ms per extra queued request, guard 0.5 ms)
passes whenever any coverage advantage exists; it cannot veto the
queue-concentration harm observed at K=4.  This runner therefore compares
three routing rules on the identical primary workload (96 active prefixes,
Zipf alpha=0.55, 2048-token prefixes, concurrency 4):

* affinity_first : always take the best advertised coverage above native
                   (hit-rate maximizing, no guard);
* abstain        : take affinity only when (a) incremental coverage reaches
                   --abstain-min-increment-tokens AND (b) the affinity owner
                   is not strictly busier than the native choice; otherwise
                   fall back to the native load decision;
* (anchors)      : load_only and exact (exact uses the stock net-benefit
                   guard, as in the primary).

Cells: load_only, sketch_k4_affinity_first, sketch_k4_abstain,
sketch_k16_affinity_first, sketch_k16_abstain, exact - 12 paired repetitions,
byte-identical traces, cache_salt isolation, fixed output length.

All stock V4/V5 files are left untouched; this script subclasses the V4
Dispatcher and reuses the V5 trace/metric helpers.
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
    enrich_metrics,
    extra_checks,
    make_difficult_trace,
    make_pairs,
    make_summary,
)

POLICIES: list[tuple[str, int | None, str]] = [
    ("load_only", None, "net_benefit"),
    ("sketch_k4_affinity_first", 4, "affinity_first"),
    ("sketch_k4_abstain", 4, "abstain"),
    ("sketch_k16_affinity_first", 16, "affinity_first"),
    ("sketch_k16_abstain", 16, "abstain"),
    ("exact", None, "net_benefit"),
]


class GuardAblatedDispatcher(Dispatcher):
    """V4 dispatcher plus ``affinity_first`` and ``abstain`` guard modes."""

    def __init__(self, *args, guard_mode: str = "net_benefit", abstain_min_increment_tokens: int = 1024, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        if guard_mode not in ("net_benefit", "affinity_first", "abstain"):
            raise ValueError(f"unknown guard_mode: {guard_mode}")
        self.guard_mode = guard_mode
        self.abstain_min_increment_tokens = abstain_min_increment_tokens

    def choose(self, request):  # noqa: D102 - mirrors parent contract + guard note
        self.demand[request.digest] += 1
        native = self._least_loaded(list(range(len(URLS))))
        native_coverage = self.entries[native].get(request.digest, 0)
        if self.policy == "load_only":
            return native, 0, 0, False, native_coverage, 0.0, "native"
        visible = self.entries if self.policy == "exact" else self.advertised
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
            if incremental_tokens > 0:
                if self.guard_mode == "affinity_first":
                    return target, raw, len(evaluated), True, best_coverage, estimated_net_ms, "affinity"
                if self.guard_mode == "abstain":
                    if incremental_tokens < self.abstain_min_increment_tokens:
                        return native, raw, len(evaluated), False, native_coverage, 0.0, "abstain_sparse"
                    if self.loads[target] > self.loads[native]:
                        return native, raw, len(evaluated), False, native_coverage, 0.0, "abstain_busy"
                    return target, raw, len(evaluated), True, best_coverage, estimated_net_ms, "affinity"
                if estimated_net_ms > self.guard_ms:
                    return target, raw, len(evaluated), True, best_coverage, estimated_net_ms, "affinity"
        return native, raw, len(evaluated), False, native_coverage, 0.0, "native"


async def run_cell(trace, policy, k, guard_mode, cache_salt, args):
    """V4 run_cell with the guard-ablated dispatcher (copied, stock untouched)."""
    import aiohttp

    dispatcher = GuardAblatedDispatcher(
        policy, k, args.cache_capacity, args.j,
        args.prefill_tokens_per_ms, args.queue_penalty_ms, args.guard_ms,
        guard_mode=guard_mode, abstain_min_increment_tokens=args.abstain_min_increment_tokens,
    )
    records: list[dict] = []
    async with aiohttp.ClientSession() as session:
        before = await metric_snapshot(session)
        for offset in range(0, len(trace), args.concurrency):
            wave = trace[offset:offset + args.concurrency]
            decisions = []
            for request in wave:
                target, raw, evaluated, selected_affinity, coverage, expected_net, guard_note = dispatcher.choose(request)
                dispatcher.loads[target] += 1
                decisions.append((request, target, raw, evaluated, selected_affinity, coverage, expected_net, guard_note))
            responses = await asyncio.gather(*[
                one_request(session, URLS[target], prompt_for(request), cache_salt, args.output_tokens, args.max_request_attempts)
                for request, target, *_ in decisions
            ])
            for (request, target, raw, evaluated, selected_affinity, coverage, expected_net, guard_note), response in zip(decisions, responses):
                dispatcher.loads[target] -= 1
                prior = dispatcher.observe(target, request)
                if request.discard:
                    continue
                prompt = prompt_for(request)
                records.append({
                    "request_id": request.request_id,
                    "selected_instance": target,
                    "candidate_hit": selected_affinity,
                    "guard_note": guard_note,
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
        "abstain_rate": sum(row["guard_note"].startswith("abstain") for row in records) / n if n else 0.0,
        "dispatcher_reuse_rate": sum(float(row["dispatcher_prior_coverage_tokens"]) > 0 for row in records) / n if n else 0.0,
        "mean_selected_coverage_tokens": statistics.mean(float(row["selected_coverage_tokens"]) for row in records) if records else 0.0,
        "vllm_cached_token_rate": sum(float(row["vllm_cached_tokens"] or 0) > 0 for row in records) / n if n else 0.0,
        "vllm_cached_tokens_total": sum(float(row["vllm_cached_tokens"] or 0) for row in records),
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


async def run(args: argparse.Namespace):
    await check_endpoints()
    root = Path(args.out_dir)
    cells: list[dict] = []
    raw: list[dict] = []
    for rep in range(args.repetitions):
        trace_path = root / "traces" / f"guard_ablation_trace_rep{rep}.csv"
        trace = make_difficult_trace(
            trace_path, rep, args.n_requests, args.warmup, args.prefix_tokens,
            args.active_prefixes, args.zipf_alpha, args.seed,
        )
        trace_hash = sha256_file(trace_path)
        order = list(POLICIES)
        random.Random(stable_int(args.seed, "guard-policy-order", rep)).shuffle(order)
        for order_index, (policy, k, guard_mode) in enumerate(order):
            salt = f"b02-s2:guard-ablation:rep{rep}:{policy}"
            metrics, records = await run_cell(trace, policy, k, guard_mode, salt, args)
            metrics = enrich_metrics(metrics, records)
            row = {
                "experiment_id": f"20260719_guard_ablation_{policy}_rep{rep}",
                "experiment": "live_guard_ablation_v1",
                "evidence_type": "live_t4_vllm",
                "code_commit": git_commit(),
                "model": MODEL_ID,
                "hardware": "4x Tesla T4; Qwen2.5-1.5B; one vLLM instance/GPU",
                "policy": policy,
                "guard_mode": guard_mode,
                "abstain_min_increment_tokens": args.abstain_min_increment_tokens,
                "K": "inf" if k is None else k,
                "J": args.j,
                "admission": "online coverage-first (demand x coverage)" if k is not None else "n/a",
                "routing_guard": guard_mode,
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
                "metric_scope": "Live T4 TTFT and vLLM cached-token telemetry. Estimated saved prefill is dispatcher coverage, reported separately.",
                "status": "Current",
                **metrics,
            }
            cells.append(row)
            raw.extend({
                "experiment_id": row["experiment_id"], "locality": "difficult", "rep": rep, "policy": policy,
                "J": args.j, **record,
            } for record in records)
            write_csv(root / "_checkpoint_cells.csv", cells)
            (root / "_checkpoint_raw.json").write_text(json.dumps(raw))
            print(json.dumps({
                "completed": row["experiment_id"], "p50_ttft_ms": row["ttft_p50_ms"],
                "candidate_hit_rate": row["candidate_hit_rate"], "abstain_rate": row["abstain_rate"],
                "saved_prefill": row["estimated_saved_prefill_tokens_total"],
                "cached_tokens": row["physical_cached_tokens_total"], "errors": row["request_error_rate"],
            }), flush=True)
            await asyncio.sleep(args.cooldown_s)
    pair_policies = [(policy, k) for policy, k, _ in POLICIES]
    return cells, make_pairs(cells, pair_policies), raw


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", default="/home/byh/B02/supplemental_20260719/live_guard_ablation")
    parser.add_argument("--seed", type=int, default=20260719)
    parser.add_argument("--repetitions", type=int, default=12)
    parser.add_argument("--n-requests", type=int, default=192)
    parser.add_argument("--warmup", type=int, default=64)
    parser.add_argument("--active-prefixes", type=int, default=96)
    parser.add_argument("--zipf-alpha", type=float, default=0.55)
    parser.add_argument("--cache-capacity", type=int, default=128)
    parser.add_argument("--j", type=int, default=4)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--prefix-tokens", type=int, default=2048)
    parser.add_argument("--output-tokens", type=int, default=4)
    parser.add_argument("--prefill-tokens-per-ms", type=float, default=50.0)
    parser.add_argument("--queue-penalty-ms", type=float, default=2.0)
    parser.add_argument("--guard-ms", type=float, default=0.5)
    parser.add_argument("--abstain-min-increment-tokens", type=int, default=1024)
    parser.add_argument("--max-request-attempts", type=int, default=3)
    parser.add_argument("--cooldown-s", type=float, default=0.05)
    parser.add_argument("--smoke", action="store_true", help="relax paired-run validation for a quick pipeline check")
    args = parser.parse_args()
    if not args.smoke:
        if args.n_requests - args.warmup < 128:
            raise ValueError("this primary guard ablation requires at least 128 measured requests per policy/repetition")
        if args.repetitions < 10:
            raise ValueError("this primary guard ablation requires at least 10 paired repetitions")
    started = time.time()
    cells, pairs, raw = asyncio.run(run(args))
    if any(float(row["request_error_rate"]) > 0 for row in cells):
        raise RuntimeError("live request error observed; do not use this run")
    checks = validate_records(raw, args.output_tokens)
    checks.extend(extra_checks(cells, raw, [(policy, k) for policy, k, _ in POLICIES]))
    if any(check["status"] != "PASS" for check in checks):
        raise RuntimeError(f"guard ablation sanity checks failed: {checks}")
    root = Path(args.out_dir)
    ensure_dir(root)
    write_csv(root / "guard_ablation_cells.csv", cells)
    write_csv(root / "guard_ablation_pairs.csv", pairs)
    write_csv(root / "guard_ablation_summary.csv", make_summary(pairs, args.seed))
    write_csv(root / "guard_ablation_sanity_checks.csv", checks)
    (root / "guard_ablation_raw.json").write_text(json.dumps(raw))
    metadata = {
        "started_at_unix": started, "finished_at_unix": time.time(),
        "duration_s": time.time() - started, "arguments": vars(args),
        "cells": len(cells), "pairs": len(pairs), "raw_requests": len(raw),
    }
    (root / "run_metadata.json").write_text(json.dumps(metadata, indent=2))
    (root / "README.md").write_text(
        "# Live net-benefit / abstention guard ablation (S2, 2026-07-19)\n\n"
        "Same difficult workload as the V5 primary K sweep (alpha=0.55, 96 prefixes,\n"
        "2048-token prefixes, concurrency 4). Compares affinity-first routing (no\n"
        "guard) with an abstention mode that falls back to the native load decision\n"
        "when incremental coverage is below 1024 tokens or the affinity owner is\n"
        "strictly busier than the native choice. Tests the paper's claim that a\n"
        "bounded interface needs an abstention mode (Section 4.3) and gives live\n"
        "evidence for the net-benefit principle (Section 4.4, Eq. 10).\n"
    )
    print(json.dumps(metadata, indent=2))


if __name__ == "__main__":
    main()
