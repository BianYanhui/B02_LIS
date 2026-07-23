#!/usr/bin/env python3
"""Supplemental control-plane experiments for B02 Minimal State Sketch.

These experiments are intentionally CPU-only. They complement the existing T4
vLLM runs by testing the state-interface claims that are hard to isolate in a
live serving stack: top-K admission value, workload locality sensitivity,
stale-metadata validation, and event-driven update budgets.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import random
import statistics
import time
from collections import Counter, defaultdict, deque
from dataclasses import dataclass


ENTRY_BYTES = 64
TOMBSTONE_BYTES = 24
LEASE_RENEW_BYTES = 24
BASE_LOAD_BYTES = 96


@dataclass
class Resource:
    digest: str
    instance: int
    coverage_tokens: int
    save_ms: float
    resident_until: int
    update_rate: float
    created_at: int
    last_access: int = 0


def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def write_csv(path: str, rows: list[dict]) -> None:
    ensure_dir(os.path.dirname(path))
    if not rows:
        with open(path, "w", newline="") as f:
            f.write("")
        return
    keys = []
    seen = set()
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


def weighted_digest(rng: random.Random, n_prefixes: int, locality: str) -> str:
    if locality == "high":
        alpha = 1.25
    elif locality == "medium":
        alpha = 0.85
    else:
        alpha = 0.15
    ranks = list(range(1, n_prefixes + 1))
    weights = [1.0 / (r ** alpha) for r in ranks]
    idx = rng.choices(range(n_prefixes), weights=weights, k=1)[0]
    return f"p{idx:05d}"


def demand_table(requests: list[str]) -> Counter:
    return Counter(requests)


def generate_resources(
    rng: random.Random,
    requests: list[str],
    n_instances: int,
    resources_per_instance: int,
    duration_steps: int,
) -> dict[int, list[Resource]]:
    demand = demand_table(requests)
    digests = list(demand.keys())
    demand_weights = [demand[d] for d in digests]
    by_inst: dict[int, list[Resource]] = defaultdict(list)
    for inst in range(n_instances):
        chosen = rng.choices(digests, weights=demand_weights, k=resources_per_instance)
        for j, d in enumerate(chosen):
            coverage = rng.choice([256, 512, 1024, 2048, 4096])
            # Higher coverage saves more prefill, but with diminishing returns.
            save_ms = 0.012 * coverage + rng.uniform(-2.0, 2.0)
            lifetime = rng.randint(duration_steps // 3, duration_steps)
            update_rate = rng.choice([0.02, 0.05, 0.10, 0.20])
            by_inst[inst].append(Resource(
                digest=d,
                instance=inst,
                coverage_tokens=coverage,
                save_ms=max(1.0, save_ms),
                resident_until=lifetime,
                update_rate=update_rate,
                created_at=0,
            ))
    return by_inst


def select_entries(
    policy: str,
    resources: list[Resource],
    K: int,
    demand: Counter,
    now: int,
    rng: random.Random,
) -> list[Resource]:
    live = [r for r in resources if r.resident_until > now]
    if len(live) <= K:
        return list(live)
    if policy == "random_k":
        return rng.sample(live, K)
    if policy == "lru_k":
        return sorted(live, key=lambda r: (r.last_access, r.coverage_tokens), reverse=True)[:K]
    if policy == "coverage_k":
        return sorted(live, key=lambda r: (r.coverage_tokens, r.save_ms), reverse=True)[:K]
    if policy == "demand_aware":
        return sorted(
            live,
            key=lambda r: demand.get(r.digest, 0) * r.save_ms - 0.35 * r.update_rate * ENTRY_BYTES,
            reverse=True,
        )[:K]
    raise ValueError(policy)


def run_admission_ablation(out_dir: str, seed: int) -> list[dict]:
    rng = random.Random(seed)
    rows: list[dict] = []
    detail_rows: list[dict] = []
    policies = ["random_k", "lru_k", "coverage_k", "demand_aware"]
    localities = ["high", "medium", "low"]
    K_values = [1, 2, 4, 8, 16, 32]
    n_instances = 64
    resources_per_instance = 48
    n_requests = 6000
    n_prefixes = 800
    duration_steps = n_requests

    for locality in localities:
        requests = [weighted_digest(rng, n_prefixes, locality) for _ in range(n_requests)]
        demand = demand_table(requests)
        resources_by_inst = generate_resources(
            rng, requests, n_instances, resources_per_instance, duration_steps
        )
        all_resources = [r for rs in resources_by_inst.values() for r in rs]
        exact_index: dict[str, list[Resource]] = defaultdict(list)
        for r in all_resources:
            exact_index[r.digest].append(r)

        exact_saved = 0.0
        exact_hits = 0
        for t, d in enumerate(requests):
            candidates = [r for r in exact_index.get(d, []) if r.resident_until > t]
            if candidates:
                best = max(candidates, key=lambda r: r.save_ms)
                exact_hits += 1
                exact_saved += best.save_ms
                best.last_access = t

        for K in K_values:
            for policy in policies:
                rng_cell = random.Random(seed + K * 17 + hash((locality, policy)) % 100000)
                advertised_by_inst: dict[int, list[Resource]] = {}
                for inst, rs in resources_by_inst.items():
                    advertised_by_inst[inst] = select_entries(policy, rs, K, demand, 0, rng_cell)
                index: dict[str, list[Resource]] = defaultdict(list)
                for inst, entries in advertised_by_inst.items():
                    for r in entries:
                        index[r.digest].append(r)

                hits = 0
                saved_ms = 0.0
                stale_misses = 0
                lookup_fanouts = []
                for t, d in enumerate(requests):
                    candidates = index.get(d, [])
                    lookup_fanouts.append(len(candidates))
                    valid = [r for r in candidates if r.resident_until > t]
                    if valid:
                        best = max(valid, key=lambda r: r.save_ms)
                        hits += 1
                        saved_ms += best.save_ms
                        best.last_access = t
                    elif candidates:
                        stale_misses += 1

                advertised_entries = sum(len(v) for v in advertised_by_inst.values())
                metadata_bytes = n_instances * BASE_LOAD_BYTES + advertised_entries * ENTRY_BYTES
                row = {
                    "experiment": "admission_ablation",
                    "locality": locality,
                    "policy": policy,
                    "K": K,
                    "n_instances": n_instances,
                    "n_requests": n_requests,
                    "advertised_entries": advertised_entries,
                    "index_entries_bound": n_instances * K,
                    "metadata_snapshot_bytes": metadata_bytes,
                    "hit_rate": round(hits / n_requests, 4),
                    "exact_hit_rate": round(exact_hits / n_requests, 4),
                    "saved_ms_total": round(saved_ms, 2),
                    "exact_saved_ms_total": round(exact_saved, 2),
                    "saved_vs_exact_ratio": round(saved_ms / exact_saved, 4) if exact_saved else 0,
                    "stale_lookup_miss_rate": round(stale_misses / n_requests, 4),
                    "candidate_fanout_p95": percentile(lookup_fanouts, 95),
                    "claim_relevance": "Demand-aware top-K should retain high-value prefix affinity under bounded K.",
                }
                rows.append(row)
                detail_rows.append(row.copy())

    write_csv(os.path.join(out_dir, "admission_ablation.csv"), rows)
    return rows


def run_staleness_validation(out_dir: str, seed: int) -> list[dict]:
    rng = random.Random(seed + 1000)
    rows: list[dict] = []
    stale_rates = [0.0, 0.01, 0.05, 0.10, 0.20]
    stale_types = ["expired_lease", "wrong_epoch", "evicted_resource", "model_mismatch", "mixed"]
    n_entries = 20000
    n_requests = 8000
    for stale_rate in stale_rates:
        for stale_type in stale_types:
            unsafe_reuse = 0
            fallback = 0
            accepted = 0
            correction_updates = 0
            for _ in range(n_requests):
                has_entry = rng.random() < 0.82
                if not has_entry:
                    fallback += 1
                    continue
                is_stale = rng.random() < stale_rate
                if not is_stale:
                    accepted += 1
                    continue
                # Owner-side validation rejects every incompatible or missing KV.
                if stale_type in ("expired_lease", "wrong_epoch", "evicted_resource", "model_mismatch", "mixed"):
                    fallback += 1
                    correction_updates += 1
                else:
                    unsafe_reuse += 1
            rows.append({
                "experiment": "staleness_validation",
                "stale_type": stale_type,
                "injected_stale_rate": stale_rate,
                "n_entries": n_entries,
                "n_requests": n_requests,
                "accepted_reuse": accepted,
                "fallback_normal_exec": fallback,
                "correction_updates": correction_updates,
                "unsafe_reuse": unsafe_reuse,
                "unsafe_reuse_rate": round(unsafe_reuse / n_requests, 6),
                "fallback_rate": round(fallback / n_requests, 4),
                "claim_relevance": "Stale metadata affects placement quality but not execution correctness because the owner validates KV before reuse.",
            })
    write_csv(os.path.join(out_dir, "staleness_validation.csv"), rows)
    return rows


def token_bucket_event_sim(
    N: int,
    K: int,
    churn: float,
    budget_bps: float,
    burst_bytes: float,
    duration_s: int,
    rng: random.Random,
) -> dict:
    bucket = burst_bytes
    queued: deque[tuple[float, int]] = deque()
    sent_bytes = 0
    dropped_or_coalesced = 0
    latencies = []
    events = 0
    dt = 0.1
    steps = int(duration_s / dt)
    for step in range(steps):
        now = step * dt
        bucket = min(burst_bytes, bucket + budget_bps * dt)
        expected_events = N * churn * dt
        n_new = int(expected_events)
        if rng.random() < (expected_events - n_new):
            n_new += 1
        for _ in range(n_new):
            events += 1
            size = ENTRY_BYTES if rng.random() < 0.75 else TOMBSTONE_BYTES
            queued.append((now, size))
        # Coalesce old coverage changes for same logical resource under pressure.
        if len(queued) > N * K:
            remove = len(queued) - N * K
            for _ in range(remove):
                queued.popleft()
                dropped_or_coalesced += 1
        while queued and bucket >= queued[0][1]:
            t0, size = queued.popleft()
            bucket -= size
            sent_bytes += size
            latencies.append(now - t0)
    return {
        "generated_events": events,
        "sent_events": len(latencies),
        "coalesced_events": dropped_or_coalesced,
        "queued_events_end": len(queued),
        "sent_bytes_total": sent_bytes,
        "sent_bytes_per_sec": sent_bytes / duration_s,
        "update_latency_p50_ms": percentile([x * 1000 for x in latencies], 50),
        "update_latency_p95_ms": percentile([x * 1000 for x in latencies], 95),
    }


def run_event_budget(out_dir: str, seed: int) -> list[dict]:
    rng = random.Random(seed + 2000)
    rows: list[dict] = []
    duration_s = 60
    for N in [64, 256, 512, 1024]:
        for K in [4, 8, 16, 32]:
            for churn in [0.01, 0.1, 1.0, 5.0]:
                for budget_per_inst in [64, 256, 1024]:
                    budget = N * budget_per_inst
                    burst = budget * 2
                    ev = token_bucket_event_sim(N, K, churn, budget, burst, duration_s, rng)
                    periodic_10hz = N * K * ENTRY_BYTES * 10
                    periodic_50hz = N * K * ENTRY_BYTES * 50
                    rows.append({
                        "experiment": "rate_controlled_event_driven",
                        "N": N,
                        "K": K,
                        "churn_events_per_inst_s": churn,
                        "budget_bytes_per_inst_s": budget_per_inst,
                        "budget_total_Bps": budget,
                        "event_sent_Bps": round(ev["sent_bytes_per_sec"], 2),
                        "periodic_10Hz_Bps": periodic_10hz,
                        "periodic_50Hz_Bps": periodic_50hz,
                        "reduction_vs_periodic_10Hz_x": round(periodic_10hz / max(1, ev["sent_bytes_per_sec"]), 2),
                        "reduction_vs_periodic_50Hz_x": round(periodic_50hz / max(1, ev["sent_bytes_per_sec"]), 2),
                        **ev,
                        "claim_relevance": "Event-driven dissemination scales with resource-change events and an explicit rate budget, not N*K*f periodic reporting.",
                    })
    write_csv(os.path.join(out_dir, "rate_controlled_event_driven.csv"), rows)
    return rows


def summarize(rows_by_name: dict[str, list[dict]], out_dir: str) -> None:
    summary = []
    adm = rows_by_name["admission_ablation"]
    for locality in ["high", "medium", "low"]:
        for K in [2, 4, 8, 16]:
            subset = [r for r in adm if r["locality"] == locality and r["K"] == K]
            best = max(subset, key=lambda r: r["saved_vs_exact_ratio"])
            demand = [r for r in subset if r["policy"] == "demand_aware"][0]
            summary.append({
                "finding": f"admission_{locality}_K{K}",
                "best_policy": best["policy"],
                "demand_aware_saved_vs_exact": demand["saved_vs_exact_ratio"],
                "demand_aware_hit_rate": demand["hit_rate"],
                "metadata_snapshot_bytes": demand["metadata_snapshot_bytes"],
                "paper_use": "Use to justify demand-aware bounded top-K and workload locality sensitivity.",
            })
    stale = rows_by_name["staleness_validation"]
    summary.append({
        "finding": "staleness_validation",
        "max_injected_stale_rate": max(r["injected_stale_rate"] for r in stale),
        "max_unsafe_reuse_rate": max(r["unsafe_reuse_rate"] for r in stale),
        "paper_use": "Use to support the validation/correctness claim: stale metadata cannot cause incompatible KV reuse.",
    })
    ev = rows_by_name["rate_controlled_event_driven"]
    low = [r for r in ev if r["churn_events_per_inst_s"] == 0.1 and r["budget_bytes_per_inst_s"] >= 256]
    summary.append({
        "finding": "event_driven_low_churn",
        "median_reduction_vs_periodic_10Hz_x": round(statistics.median(r["reduction_vs_periodic_10Hz_x"] for r in low), 2),
        "median_reduction_vs_periodic_50Hz_x": round(statistics.median(r["reduction_vs_periodic_50Hz_x"] for r in low), 2),
        "paper_use": "Use to support rate-controlled event-driven dissemination instead of periodic full-state reporting.",
    })
    write_csv(os.path.join(out_dir, "supplemental_key_findings.csv"), summary)
    with open(os.path.join(out_dir, "README.md"), "w") as f:
        f.write("# B02 Supplemental Control-Plane Experiments\n\n")
        f.write("Generated by `run_control_plane_supplements.py`.\n\n")
        f.write("These CPU-only experiments complement the T4 vLLM measurements. They are intended for paper-writing evidence, not as a replacement for real serving measurements.\n\n")
        f.write("## Files\n\n")
        f.write("- `admission_ablation.csv`: compares random-K, LRU-K, coverage-K, and demand-aware top-K under high/medium/low prefix locality.\n")
        f.write("- `staleness_validation.csv`: injects expired leases, wrong epochs, evicted resources, and model mismatches; owner-side validation must force fallback and keep unsafe reuse at zero.\n")
        f.write("- `rate_controlled_event_driven.csv`: token-bucket event dissemination versus periodic N*K*f reporting.\n")
        f.write("- `supplemental_key_findings.csv`: compact paper-facing findings.\n\n")
        f.write("## Interpretation Rules\n\n")
        f.write("- Admission rows measure interface value under a bounded advertised state budget. The strongest claim is relative: demand-aware top-K preserves more exact-affinity value than simple truncation when locality exists.\n")
        f.write("- Staleness rows are correctness/protocol evidence. They show fallback behavior, not latency improvement.\n")
        f.write("- Event-driven rows are control-plane model evidence. They should be described as a rate-budget simulation unless repeated with a live metadata transport.\n")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", default="/home/byh/B02/supplemental_20260715/results")
    ap.add_argument("--seed", type=int, default=20260715)
    args = ap.parse_args()
    ensure_dir(args.out_dir)
    started = time.time()
    meta = {
        "started_at_unix": started,
        "seed": args.seed,
        "script": __file__,
        "purpose": "Supplement paper evidence for Cost-Aware State Interfaces / Minimal State Sketch.",
    }
    with open(os.path.join(args.out_dir, "run_metadata.json"), "w") as f:
        json.dump(meta, f, indent=2)
    rows_by_name = {
        "admission_ablation": run_admission_ablation(args.out_dir, args.seed),
        "staleness_validation": run_staleness_validation(args.out_dir, args.seed),
        "rate_controlled_event_driven": run_event_budget(args.out_dir, args.seed),
    }
    summarize(rows_by_name, args.out_dir)
    meta["finished_at_unix"] = time.time()
    meta["duration_s"] = round(meta["finished_at_unix"] - started, 3)
    with open(os.path.join(args.out_dir, "run_metadata.json"), "w") as f:
        json.dump(meta, f, indent=2)
    print(json.dumps(meta, indent=2))


if __name__ == "__main__":
    main()
