#!/usr/bin/env python3
"""Difficult, input-identical live K sweep on four T4 vLLM workers.

Unlike the V4 high-locality pilot, this runner spreads demand over a larger
active-prefix pool.  A per-instance bounded advertisement therefore has to
choose which reusable prefixes it exposes: K=4 cannot represent the pool,
K=8 and K=16 represent progressively more of it, and K=32 is compared with
the full Exact interface.  All policies receive the same trace within a
replica, use cache_salt outside the prompt, and generate the same fixed number
of output tokens.

It reports two separate notions of reuse value:
* estimated_saved_prefill_tokens: dispatcher-selected reusable coverage above
  the 256-token minimum, normalized to Exact per replica;
* vllm_cached_tokens: physical response telemetry returned by vLLM, also
  normalized to Exact per replica.
Neither metric is called a generic cache-hit rate.
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
from collections import defaultdict
from dataclasses import asdict
from pathlib import Path

from run_fixed_prompt_t4_replay_v4 import (
    ENTRY_BYTES,
    MODEL_ID,
    URLS,
    TraceRequest,
    bootstrap_ci,
    check_endpoints,
    ensure_dir,
    git_commit,
    percentile,
    run_cell,
    sha256_file,
    stable_int,
    validate_records,
    write_csv,
)


def flat_zipf_cdf(alpha: float, n: int) -> list[float]:
    weights = [1.0 / ((rank + 1) ** alpha) for rank in range(n)]
    total = sum(weights)
    running, out = 0.0, []
    for weight in weights:
        running += weight / total
        out.append(running)
    out[-1] = 1.0
    return out


def make_difficult_trace(
    path: Path,
    rep: int,
    n_requests: int,
    warmup: int,
    prefix_tokens: int,
    active_prefixes: int,
    alpha: float,
    seed: int,
) -> list[TraceRequest]:
    rng = random.Random(stable_int(seed, "v5-live-k", rep, n_requests, prefix_tokens, active_prefixes, alpha))
    cdf = flat_zipf_cdf(alpha, active_prefixes)
    trace: list[TraceRequest] = []
    for request_id in range(n_requests):
        prefix_id = bisect.bisect_left(cdf, rng.random())
        trace.append(TraceRequest(
            request_id=request_id,
            arrival_time=request_id * 0.02,
            tenant=f"tenant-{prefix_id % 12}",
            digest=f"hard-p{prefix_id:04d}",
            prefix_token_target=prefix_tokens,
            discard=request_id < warmup,
        ))
    ensure_dir(path.parent)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(asdict(trace[0]).keys()))
        writer.writeheader()
        writer.writerows(asdict(request) for request in trace)
    return trace


def enrich_metrics(metrics: dict, records: list[dict]) -> dict:
    success = [record for record in records if record["ok"]]
    metrics = dict(metrics)
    metrics["mean_ttft_ms"] = statistics.mean(float(record["ttft_ms"]) for record in success) if success else 0.0
    metrics["estimated_saved_prefill_tokens_total"] = sum(
        max(0.0, float(record["selected_coverage_tokens"]) - 256.0) for record in records
    )
    metrics["physical_cached_tokens_total"] = metrics["vllm_cached_tokens_total"]
    return metrics


def make_pairs(cells: list[dict], policies: list[tuple[str, int | None]]) -> list[dict]:
    by_key = {(int(row["rep"]), row["policy"]): row for row in cells}
    treatments = [policy for policy, _ in policies if policy != "load_only"]
    pairs: list[dict] = []
    for rep in sorted({int(row["rep"]) for row in cells}):
        baseline = by_key[(rep, "load_only")]
        exact = by_key[(rep, "exact")]
        exact_saved = float(exact["estimated_saved_prefill_tokens_total"])
        exact_cached = float(exact["physical_cached_tokens_total"])
        for policy in treatments:
            current = by_key[(rep, policy)]
            row = {
                "experiment_id": current["experiment_id"],
                "rep": rep,
                "baseline_policy": "load_only",
                "treatment_policy": policy,
                "workload_trace_hash": baseline["workload_trace_hash"],
                "status": "Current",
                "exact_normalized_estimated_saved_prefill": (
                    float(current["estimated_saved_prefill_tokens_total"]) / exact_saved if exact_saved else 0.0
                ),
                "exact_normalized_physical_cached_tokens": (
                    float(current["physical_cached_tokens_total"]) / exact_cached if exact_cached else 0.0
                ),
            }
            for metric in (
                "mean_ttft_ms", "ttft_p50_ms", "ttft_p95_ms",
                "dispatcher_index_bytes", "estimated_saved_prefill_tokens_total",
                "physical_cached_tokens_total", "mean_selected_coverage_tokens",
            ):
                row[f"delta_{metric}"] = float(current[metric]) - float(baseline[metric])
                row[f"treatment_{metric}"] = current[metric]
            pairs.append(row)
    return pairs


def make_summary(pairs: list[dict], seed: int) -> list[dict]:
    rows: list[dict] = []
    metrics = (
        "exact_normalized_estimated_saved_prefill",
        "exact_normalized_physical_cached_tokens",
        "delta_mean_ttft_ms", "delta_ttft_p50_ms", "delta_ttft_p95_ms",
        "treatment_dispatcher_index_bytes",
        "treatment_estimated_saved_prefill_tokens_total",
        "treatment_physical_cached_tokens_total",
    )
    for policy in sorted({row["treatment_policy"] for row in pairs}):
        group = [row for row in pairs if row["treatment_policy"] == policy]
        row = {
            "experiment": "difficult_live_k_tradeoff_v5",
            "evidence_type": "live_t4_vllm",
            "treatment_policy": policy,
            "baseline_policy": "load_only",
            "n_reps": len(group),
            "status": "Current",
        }
        for metric in metrics:
            values = [float(item[metric]) for item in group]
            mean, low, high = bootstrap_ci(values, stable_int(seed, "summary", policy, metric))
            row[f"{metric}_mean"] = mean
            row[f"{metric}_ci95_low"] = low
            row[f"{metric}_ci95_high"] = high
            row[f"{metric}_median"] = statistics.median(values)
            row[f"{metric}_iqr"] = percentile(values, 75) - percentile(values, 25)
        rows.append(row)
    return rows


def extra_checks(cells: list[dict], raw: list[dict], policies: list[tuple[str, int | None]]) -> list[dict]:
    expected = {policy for policy, _ in policies}
    by_rep: dict[int, list[dict]] = defaultdict(list)
    for row in cells:
        by_rep[int(row["rep"])].append(row)
    bad_trace = sum(
        len(rows) != len(expected)
        or {row["policy"] for row in rows} != expected
        or len({row["workload_trace_hash"] for row in rows}) != 1
        for rows in by_rep.values()
    )
    fanout_violations = sum(
        float(row["evaluated_candidate_fanout"]) > float(row["J"]) for row in raw
    )
    return [
        {
            "check_name": "same difficult trace hash and policy set within every paired repetition",
            "status": "PASS" if bad_trace == 0 else "FAIL",
            "offending_rows": bad_trace,
            "suggested_fix": "regenerate one shared trace per repetition before policy replay",
        },
        {
            "check_name": "evaluated candidate fanout obeys J for every live request",
            "status": "PASS" if fanout_violations == 0 else "FAIL",
            "offending_rows": fanout_violations,
            "suggested_fix": "truncate ranked candidates before owner request",
        },
    ]


async def run(args: argparse.Namespace) -> tuple[list[dict], list[dict], list[dict]]:
    await check_endpoints()
    root = Path(args.out_dir)
    ks = [int(item) for item in args.k_values.split(",") if item]
    policies: list[tuple[str, int | None]] = [("load_only", None)]
    policies.extend((f"sketch_coverage_k{k}", k) for k in ks)
    policies.append(("exact", None))
    cells: list[dict] = []
    raw: list[dict] = []
    for rep in range(args.repetitions):
        trace_path = root / "traces" / f"difficult_k_trace_rep{rep}.csv"
        trace = make_difficult_trace(
            trace_path, rep, args.n_requests, args.warmup, args.prefix_tokens,
            args.active_prefixes, args.zipf_alpha, args.seed,
        )
        trace_hash = sha256_file(trace_path)
        order = list(policies)
        random.Random(stable_int(args.seed, "policy-order", rep)).shuffle(order)
        for order_index, (policy, k) in enumerate(order):
            salt = f"b02-v5:difficult-k:rep{rep}:{policy}"
            metrics, records = await run_cell(trace, policy, k, salt, args)
            metrics = enrich_metrics(metrics, records)
            row = {
                "experiment_id": f"20260716_live_k_{policy}_rep{rep}",
                "experiment": "difficult_live_k_tradeoff_v5",
                "evidence_type": "live_t4_vllm",
                "code_commit": git_commit(),
                "model": MODEL_ID,
                "hardware": "4x Tesla T4; Qwen2.5-1.5B; one vLLM instance/GPU",
                "policy": policy,
                "K": "inf" if k is None else k,
                "J": args.j,
                "admission": "online coverage-first (demand x coverage)" if k is not None else "n/a",
                "routing_guard": "net_prefill_benefit",
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
                "experiment_id": row["experiment_id"], "rep": rep, "policy": policy,
                "J": args.j, **record,
            } for record in records)
            print(json.dumps({
                "completed": row["experiment_id"], "p50_ttft_ms": row["ttft_p50_ms"],
                "saved_prefill": row["estimated_saved_prefill_tokens_total"],
                "cached_tokens": row["physical_cached_tokens_total"], "errors": row["request_error_rate"],
            }), flush=True)
            await asyncio.sleep(args.cooldown_s)
    return cells, make_pairs(cells, policies), raw


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", default="/home/byh/B02/supplemental_20260715/live_k_tradeoff_v5")
    parser.add_argument("--seed", type=int, default=20260716)
    parser.add_argument("--k-values", default="4,8,16,32")
    parser.add_argument("--repetitions", type=int, default=12)
    parser.add_argument("--n-requests", type=int, default=192)
    parser.add_argument("--warmup", type=int, default=64)
    parser.add_argument("--active-prefixes", type=int, default=96)
    parser.add_argument("--zipf-alpha", type=float, default=0.55)
    parser.add_argument("--cache-capacity", type=int, default=128)
    parser.add_argument("--j", type=int, default=4)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--prefix-tokens", type=int, default=2048)
    parser.add_argument("--output-tokens", type=int, default=16)
    parser.add_argument("--prefill-tokens-per-ms", type=float, default=50.0)
    parser.add_argument("--queue-penalty-ms", type=float, default=2.0)
    parser.add_argument("--guard-ms", type=float, default=0.5)
    parser.add_argument("--max-request-attempts", type=int, default=3)
    parser.add_argument("--cooldown-s", type=float, default=0.1)
    args = parser.parse_args()
    if args.n_requests - args.warmup < 128:
        raise ValueError("this primary K experiment requires at least 128 measured requests per policy/repetition")
    if args.repetitions < 10:
        raise ValueError("this primary K experiment requires at least 10 paired repetitions")
    if args.active_prefixes <= max(int(item) for item in args.k_values.split(",") if item):
        raise ValueError("active-prefix pool must exceed the largest K")
    started = time.time()
    cells, pairs, raw = asyncio.run(run(args))
    if any(float(row["request_error_rate"]) > 0 for row in cells):
        raise RuntimeError("live request error observed; do not use this run")
    checks = validate_records(raw, args.output_tokens)
    ks = [int(item) for item in args.k_values.split(",") if item]
    policies = [("load_only", None), *((f"sketch_coverage_k{k}", k) for k in ks), ("exact", None)]
    checks.extend(extra_checks(cells, raw, list(policies)))
    if any(check["status"] != "PASS" for check in checks):
        raise RuntimeError(f"live K sanity checks failed: {checks}")
    root = Path(args.out_dir)
    write_csv(root / "live_k_cells.csv", cells)
    write_csv(root / "live_k_pairs.csv", pairs)
    write_csv(root / "live_k_summary.csv", make_summary(pairs, args.seed))
    write_csv(root / "live_k_sanity_checks.csv", checks)
    (root / "live_k_raw.json").write_text(json.dumps(raw))
    metadata = {
        "started_at_unix": started, "finished_at_unix": time.time(),
        "duration_s": time.time() - started, "arguments": vars(args),
        "cells": len(cells), "pairs": len(pairs), "raw_requests": len(raw),
    }
    (root / "run_metadata.json").write_text(json.dumps(metadata, indent=2))
    (root / "README.md").write_text(
        "# Difficult live K trade-off (V5)\n\n"
        "The active prefix pool is intentionally larger than per-instance K. "
        "Each paired repetition uses one byte-identical trace across Load-only, K=4/8/16/32, and Exact. "
        "Use Exact-normalized estimated saved-prefill and physical cached-token telemetry as distinct metrics.\n"
    )
    print(json.dumps(metadata, indent=2))


if __name__ == "__main__":
    main()
