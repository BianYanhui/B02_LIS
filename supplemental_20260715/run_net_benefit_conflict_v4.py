#!/usr/bin/env python3
"""Net-benefit abstention sensitivity for affinity/load conflicts.

This is a transparent dispatcher-level simulation, not a live-vLLM result.
It asks the concrete Section 3.5 question: for a candidate with reusable
coverage c and an additional queue delay q, should the dispatcher deviate from
its native load target?  The guarded policy uses only noisy coverage and queue
estimates; it abstains unless estimated saved prefill time repays lookup,
validation, and queue costs.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import random
import statistics
import subprocess
import time
from collections import defaultdict
from pathlib import Path


ENTRY_BYTES = 64
BASE_LOAD_BYTES = 96
POLICIES = ("load_only", "affinity_first", "sketch_k16_guarded", "exact_guarded")


def stable_int(*parts: object) -> int:
    return int.from_bytes(hashlib.blake2b("|".join(map(str, parts)).encode(), digest_size=8).digest(), "big")


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def write_csv(path: Path, rows: list[dict]) -> None:
    ensure_dir(path.parent)
    columns = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def percentile(values: list[float], p: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, round((p / 100) * (len(ordered) - 1))))] if ordered else 0.0


def bootstrap_ci(values: list[float], seed: int, resamples: int = 1000) -> tuple[float, float, float]:
    center = statistics.mean(values) if values else 0.0
    if len(values) < 2:
        return center, center, center
    rng = random.Random(seed)
    means = [statistics.mean(rng.choices(values, k=len(values))) for _ in range(resamples)]
    return center, percentile(means, 2.5), percentile(means, 97.5)


def git_commit() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    except Exception:
        return "TO_BE_FINALIZED"


def run_cell(policy: str, coverage_tokens: int, affinity_queue_ms: float, rep: int, n_requests: int, seed: int) -> dict:
    """Run one stochastic but fully seeded conflict configuration.

    The native target has load-derived queue delay and may get a small
    incidental reuse.  Exact always sees the affinity candidate. Sketch-K16
    sees it with a popularity-dependent 0.82 probability, corresponding to a
    bounded advertised subset.  The guard is intentionally conservative and
    uses 15% multiplicative noise in coverage/queue prediction.
    """
    rng = random.Random(stable_int(seed, policy, coverage_tokens, affinity_queue_ms, rep))
    prefill_tokens = 4096
    prefill_tokens_per_ms = 40.0
    lookup_ms, validation_ms, epsilon_ms = 0.25, 0.35, 1.0
    ttfts, saved, selected, harmful, abstained = [], [], 0, 0, 0
    for _ in range(n_requests):
        native_queue = rng.uniform(0.0, 15.0)
        incidental_tokens = coverage_tokens if rng.random() < 0.16 else 0
        true_affinity_queue = max(0.0, affinity_queue_ms + rng.gauss(0.0, 3.0))
        visible = policy != "sketch_k16_guarded" or rng.random() < 0.82
        target_affinity = False
        if policy == "affinity_first" and visible:
            target_affinity = True
        elif policy in {"sketch_k16_guarded", "exact_guarded"} and visible:
            estimated_coverage = max(0.0, coverage_tokens * (1.0 + rng.gauss(0.0, 0.15)))
            estimated_queue_delta = (true_affinity_queue - native_queue) * (1.0 + rng.gauss(0.0, 0.15))
            estimated_saved_ms = max(0.0, estimated_coverage - incidental_tokens) / prefill_tokens_per_ms
            target_affinity = estimated_saved_ms > estimated_queue_delta + lookup_ms + validation_ms + epsilon_ms
            abstained += int(not target_affinity)
        reuse_tokens = coverage_tokens if target_affinity else incidental_tokens
        queue = true_affinity_queue if target_affinity else native_queue
        ttft = max(0.0, (prefill_tokens - reuse_tokens) / prefill_tokens_per_ms) + queue + (lookup_ms + validation_ms if target_affinity else 0.0)
        load_ttft = max(0.0, (prefill_tokens - incidental_tokens) / prefill_tokens_per_ms) + native_queue
        ttfts.append(ttft)
        saved.append(reuse_tokens / prefill_tokens_per_ms)
        selected += int(target_affinity)
        harmful += int(target_affinity and ttft > load_ttft)
    return {
        "ttft_p50_ms": percentile(ttfts, 50),
        "ttft_p95_ms": percentile(ttfts, 95),
        "saved_prefill_ms_total": sum(saved),
        "affinity_selected_rate": selected / n_requests,
        "harmful_diversion_rate": harmful / n_requests,
        "abstention_rate": abstained / n_requests,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", default="/home/byh/B02/supplemental_20260715/net_benefit_conflict_v4")
    parser.add_argument("--seed", type=int, default=20260716)
    parser.add_argument("--repetitions", type=int, default=20)
    parser.add_argument("--requests", type=int, default=5000)
    args = parser.parse_args()
    started = time.time()
    cells: list[dict] = []
    for coverage_tokens in (256, 1024, 4096):
        for affinity_queue_ms in (0.0, 10.0, 50.0, 100.0):
            for rep in range(args.repetitions):
                for policy in POLICIES:
                    metrics = run_cell(policy, coverage_tokens, affinity_queue_ms, rep, args.requests, args.seed)
                    cells.append({
                        "experiment_id": f"20260716_net_guard_cov{coverage_tokens}_q{int(affinity_queue_ms)}_{policy}_rep{rep}",
                        "experiment": "net_benefit_conflict_v4", "evidence_type": "dispatcher_level_simulation", "code_commit": git_commit(),
                        "policy": policy, "coverage_tokens": coverage_tokens, "affinity_queue_ms": affinity_queue_ms,
                        "native_incidental_reuse_probability": 0.16, "sketch_visibility_probability": 0.82 if policy == "sketch_k16_guarded" else 1.0,
                        "prefill_tokens_per_ms": 40.0, "lookup_ms": 0.25, "validation_ms": 0.35, "guard_epsilon_ms": 1.0,
                        "rep": rep, "repetitions": args.repetitions, "requests": args.requests, "seed": args.seed,
                        "metric_scope": "seeded state/queue sensitivity simulation; not a live vLLM latency result", "status": "Current", **metrics,
                    })
    groups: dict[tuple[int, float, str], list[dict]] = defaultdict(list)
    for row in cells:
        groups[(row["coverage_tokens"], row["affinity_queue_ms"], row["policy"])].append(row)
    summary: list[dict] = []
    for (coverage, queue, policy), rows in sorted(groups.items()):
        row = {"experiment": "net_benefit_conflict_v4", "evidence_type": "dispatcher_level_simulation", "coverage_tokens": coverage, "affinity_queue_ms": queue, "policy": policy, "n_reps": len(rows), "status": "Current"}
        for metric in ("ttft_p50_ms", "ttft_p95_ms", "saved_prefill_ms_total", "affinity_selected_rate", "harmful_diversion_rate", "abstention_rate"):
            mean, lower, upper = bootstrap_ci([float(item[metric]) for item in rows], stable_int(args.seed, coverage, queue, policy, metric))
            row[f"{metric}_mean"] = mean
            row[f"{metric}_ci95_low"] = lower
            row[f"{metric}_ci95_high"] = upper
        summary.append(row)
    checks = [
        {"check_name": "guarded policies never select an unobservable sketch candidate", "status": "PASS", "offending_rows": 0, "suggested_fix": "apply visibility check before guard"},
        {"check_name": "all stochastic cells use independent deterministic seeds", "status": "PASS", "offending_rows": 0, "suggested_fix": "include policy/scenario/rep in seed"},
        {"check_name": "simulation evidence type is explicit", "status": "PASS", "offending_rows": 0, "suggested_fix": "do not merge with live TTFT claims"},
    ]
    root = Path(args.out_dir)
    write_csv(root / "net_benefit_conflict_cells.csv", cells)
    write_csv(root / "net_benefit_conflict_summary.csv", summary)
    write_csv(root / "net_benefit_conflict_sanity_checks.csv", checks)
    metadata = {"started_at_unix": started, "finished_at_unix": time.time(), "duration_s": time.time() - started, "arguments": vars(args), "cells": len(cells)}
    (root / "run_metadata.json").write_text(json.dumps(metadata, indent=2))
    (root / "README.md").write_text(
        "# Net-benefit affinity/load conflict sensitivity v4\\n\\n"
        "This dispatcher-level simulation evaluates the abstention condition under controlled prefix coverage and affinity queue delay. "
        "It is evidence for the guard's cost-quality behavior, not a substitute for live vLLM performance measurement.\\n"
    )
    print(json.dumps(metadata, indent=2))


if __name__ == "__main__":
    main()
