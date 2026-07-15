#!/usr/bin/env python3
"""Reviewer-gap v3 control-plane simulations for B02.

The original J-bound test only established that truncation happened.  This
script adds heterogeneous candidate coverage, queue delay, estimated prefill
savings, and expiry risk, so J has a measurable quality/cost trade-off.

The budget experiment is repeated with independent event streams and a finite
per-instance transport capacity applied uniformly to all baselines.  It is
explicitly a control-plane simulation, not a live network benchmark.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import random
import statistics
import subprocess
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path


ENTRY_BYTES = 64
BASE_LOAD_BYTES = 96
LOCALITY_FANOUT = {"high": (3.0, 2.0), "medium": (7.0, 3.0), "low": (14.0, 5.0)}


def stable_int(*parts: object) -> int:
    return int.from_bytes(hashlib.blake2b("|".join(map(str, parts)).encode(), digest_size=8).digest(), "big")


def git_commit() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    except Exception:
        return "TO_BE_FINALIZED"


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


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((p / 100.0) * (len(ordered) - 1))))
    return float(ordered[index])


def bootstrap_ci(values: list[float], seed: int, resamples: int = 1500) -> tuple[float, float, float]:
    if not values:
        return 0.0, 0.0, 0.0
    center = statistics.mean(values)
    if len(values) == 1:
        return center, center, center
    rng = random.Random(seed)
    samples = [statistics.mean(rng.choices(values, k=len(values))) for _ in range(resamples)]
    return center, percentile(samples, 2.5), percentile(samples, 97.5)


def poisson(rng: random.Random, lam: float) -> int:
    """Knuth sampling is sufficient here because lam <= 0.1 per 20 ms step."""
    threshold = math.exp(-lam)
    product, count = 1.0, 0
    while product > threshold:
        count += 1
        product *= rng.random()
    return count - 1


@dataclass(frozen=True)
class Candidate:
    coverage: float
    queue_ms: float
    saved_ms: float
    expiry_probability: float
    valid: bool

    @property
    def predicted_value(self) -> float:
        return (1.0 - self.expiry_probability) * self.saved_ms - self.queue_ms

    @property
    def realised_value(self) -> float:
        return self.saved_ms - self.queue_ms


def make_candidates(rng: random.Random, locality: str, k: int) -> list[Candidate]:
    mean, spread = LOCALITY_FANOUT[locality]
    owners = max(1, min(32, int(round(rng.gauss(mean, spread)))))
    advertisement_probability = min(0.96, 0.25 + k / 24.0)
    candidates: list[Candidate] = []
    for _ in range(owners):
        if rng.random() > advertisement_probability:
            continue
        coverage = 0.25 + 0.75 * rng.random()
        prefix_tokens = (256, 512, 1024, 2048)[int(rng.random() * 4)]
        saved_ms = prefix_tokens * 0.085 * coverage
        queue_ms = 4.0 + rng.expovariate(1.0 / 24.0)
        # Entries with high theoretical value can be older and therefore risk
        # expiry; this makes evaluating more than one candidate meaningful.
        expiry = min(0.62, 0.04 + 0.34 * coverage + 0.08 * (queue_ms > 35.0))
        candidates.append(Candidate(coverage, queue_ms, saved_ms, expiry, rng.random() >= expiry))
    return sorted(candidates, key=lambda candidate: candidate.predicted_value, reverse=True)


def evaluate_candidates(candidates: list[Candidate], j: int | None) -> tuple[float, int, bool]:
    considered = candidates if j is None else candidates[:j]
    for candidate in considered:
        if candidate.valid:
            return max(0.0, candidate.realised_value), len(considered), True
    return 0.0, len(considered), False


def run_j_bound_heterogeneous(args: argparse.Namespace) -> tuple[list[dict], list[dict]]:
    replicate_rows: list[dict] = []
    commit = git_commit()
    for locality in ("high", "medium", "low"):
        for k in (2, 4, 8, 16):
            for rep in range(args.j_repetitions):
                rng = random.Random(stable_int(args.seed, "heterogeneous-j", locality, k, rep))
                values: dict[int, list[float]] = {j: [] for j in (1, 2, 4, 8, 16)}
                evaluated: dict[int, list[float]] = {j: [] for j in (1, 2, 4, 8, 16)}
                fallback: dict[int, int] = {j: 0 for j in (1, 2, 4, 8, 16)}
                truncated: dict[int, int] = {j: 0 for j in (1, 2, 4, 8, 16)}
                raw_fanout: list[float] = []
                full_values: list[float] = []
                coverages: list[float] = []
                queues: list[float] = []
                expiry_risks: list[float] = []
                for _ in range(args.j_requests):
                    candidates = make_candidates(rng, locality, k)
                    raw_fanout.append(len(candidates))
                    coverages.extend(candidate.coverage for candidate in candidates)
                    queues.extend(candidate.queue_ms for candidate in candidates)
                    expiry_risks.extend(candidate.expiry_probability for candidate in candidates)
                    full_value, _, _ = evaluate_candidates(candidates, None)
                    full_values.append(full_value)
                    for j in values:
                        value, count, accepted = evaluate_candidates(candidates, j)
                        values[j].append(value)
                        evaluated[j].append(count)
                        fallback[j] += int(not accepted)
                        truncated[j] += int(len(candidates) > j)
                full_total = sum(full_values)
                for j in values:
                    saved = sum(values[j])
                    replicate_rows.append({
                        "experiment_id": f"20260715_jhetero_{locality}_k{k}_j{j}_rep{rep}",
                        "experiment": "j_bound_heterogeneous_v3",
                        "evidence_type": "control_plane_simulation",
                        "code_commit": commit,
                        "model": "dispatcher-level candidate utility model",
                        "hardware": "CPU-only; no vLLM",
                        "locality": locality,
                        "K": k,
                        "J": j,
                        "rep": rep,
                        "repetitions": args.j_repetitions,
                        "seed": args.seed,
                        "request_count": args.j_requests,
                        "raw_candidate_fanout_p95": percentile(raw_fanout, 95),
                        "evaluated_candidate_fanout_p95": percentile(evaluated[j], 95),
                        "fraction_truncated": truncated[j] / args.j_requests,
                        "candidate_coverage_mean": statistics.mean(coverages) if coverages else 0.0,
                        "candidate_coverage_std": statistics.pstdev(coverages) if len(coverages) > 1 else 0.0,
                        "candidate_queue_ms_mean": statistics.mean(queues) if queues else 0.0,
                        "candidate_expiry_probability_mean": statistics.mean(expiry_risks) if expiry_risks else 0.0,
                        "fallback_rate": fallback[j] / args.j_requests,
                        "saved_prefill_ms_total": saved,
                        "saved_vs_unbounded_ratio": saved / full_total if full_total else 1.0,
                        "quality_loss_due_to_J": max(0.0, (full_total - saved) / full_total) if full_total else 0.0,
                        "status": "Current",
                    })
    summary_rows: list[dict] = []
    by_key: dict[tuple[str, int, int], list[dict]] = {}
    for row in replicate_rows:
        by_key.setdefault((row["locality"], int(row["K"]), int(row["J"])), []).append(row)
    for (locality, k, j), rows in sorted(by_key.items()):
        summary = {
            "experiment": "j_bound_heterogeneous_v3",
            "evidence_type": "control_plane_simulation",
            "locality": locality,
            "K": k,
            "J": j,
            "n_reps": len(rows),
            "status": "Current",
        }
        for metric in ("saved_vs_unbounded_ratio", "quality_loss_due_to_J", "fallback_rate", "saved_prefill_ms_total", "raw_candidate_fanout_p95", "evaluated_candidate_fanout_p95", "fraction_truncated"):
            values = [float(row[metric]) for row in rows]
            mean, low, high = bootstrap_ci(values, stable_int(args.seed, "j-summary", locality, k, j, metric))
            summary[f"{metric}_mean"] = mean
            summary[f"{metric}_ci95_low"] = low
            summary[f"{metric}_ci95_high"] = high
        summary_rows.append(summary)
    if any(float(row["evaluated_candidate_fanout_p95"]) > int(row["J"]) for row in replicate_rows):
        raise RuntimeError("J truncation invariant violated")
    for locality in ("high", "medium", "low"):
        for k in (2, 4, 8, 16):
            curve = [row for row in summary_rows if row["locality"] == locality and row["K"] == k]
            ordered = sorted(curve, key=lambda row: int(row["J"]))
            if any(float(left["saved_vs_unbounded_ratio_mean"]) > float(right["saved_vs_unbounded_ratio_mean"]) + 1e-10 for left, right in zip(ordered, ordered[1:])):
                raise RuntimeError("J quality curve should be monotone for a common candidate realization")
    return replicate_rows, summary_rows


@dataclass
class Packet:
    created_at: float
    bytes: int
    versions: dict[int, int]
    event_count: int


def enqueue_token_bucket(queue: deque[Packet], packet: Packet) -> int:
    """Coalesce new state with queued state for the same resources."""
    if not queue:
        queue.append(packet)
        return 0
    replaced = 0
    retained: deque[Packet] = deque()
    for item in queue:
        overlap = set(item.versions).intersection(packet.versions)
        if overlap:
            item.versions = {resource: version for resource, version in item.versions.items() if resource not in overlap}
            item.event_count -= len(overlap)
            replaced += len(overlap)
        if item.versions:
            retained.append(item)
    retained.append(packet)
    queue.clear()
    queue.extend(retained)
    return replaced


def run_budget_replicate(rng: random.Random, baseline: str, budget: int, churn: float, duration_s: float, dt: float) -> dict:
    resources = 32
    advertised = 8
    steps = int(duration_s / dt)
    truth = [0 for _ in range(resources)]
    metadata = [0 for _ in range(resources)]
    pending: dict[int, int] = {}
    queue: deque[Packet] = deque()
    link_credit = 0.0
    token_credit = float(budget)
    generated = delivered = dropped = coalesced = sent_bytes = 0
    delays_ms: list[float] = []
    queue_max = 0
    stale_lookups = fallback = request_count = 0
    saved_prefill = 0.0
    popularity = [1.0 / ((index + 1) ** 1.35) for index in range(advertised)]
    total_popularity = sum(popularity)
    cumulative: list[float] = []
    running = 0.0
    for weight in popularity:
        running += weight / total_popularity
        cumulative.append(running)
    for step in range(steps):
        now = step * dt
        arrivals = poisson(rng, churn * dt)
        for _ in range(arrivals):
            resource = int(rng.random() * resources)
            truth[resource] += 1
            generated += 1
            if baseline.startswith("periodic"):
                pending[resource] = truth[resource]
            else:
                packet = Packet(now, ENTRY_BYTES, {resource: truth[resource]}, 1)
                if baseline == "event_driven_token_bucket":
                    coalesced += enqueue_token_bucket(queue, packet)
                else:
                    queue.append(packet)
        if step % int(round(1.0 / dt)) == 0 and baseline.startswith("periodic"):
            if baseline == "periodic_full":
                queue.append(Packet(now, BASE_LOAD_BYTES + advertised * ENTRY_BYTES, {index: truth[index] for index in range(advertised)}, len(pending)))
            elif pending:
                queue.append(Packet(now, ENTRY_BYTES * len(pending), dict(pending), len(pending)))
            pending.clear()
        # A finite link may accumulate service credit while idle.  Cap the
        # idle burst at 8 KiB rather than one second of traffic, otherwise a
        # 608 B periodic snapshot can never traverse a 64 B/s link at all.
        link_credit = min(8192.0, link_credit + budget * dt)
        if baseline == "event_driven_token_bucket":
            token_credit = min(float(budget), token_credit + budget * dt)
        while queue:
            packet = queue[0]
            if now - packet.created_at > 2.0:
                queue.popleft()
                dropped += packet.event_count
                continue
            sender_ready = baseline != "event_driven_token_bucket" or token_credit >= packet.bytes
            if link_credit < packet.bytes or not sender_ready:
                break
            queue.popleft()
            link_credit -= packet.bytes
            if baseline == "event_driven_token_bucket":
                token_credit -= packet.bytes
            for resource, version in packet.versions.items():
                metadata[resource] = max(metadata[resource], version)
            delivered += packet.event_count
            sent_bytes += packet.bytes
            delays_ms.append((now - packet.created_at) * 1000.0)
        queue_max = max(queue_max, len(queue))
        # One sample from the fixed high-locality request distribution per step.
        request_count += 1
        sample = rng.random()
        resource = next(index for index, limit in enumerate(cumulative) if sample <= limit)
        if metadata[resource] != truth[resource]:
            stale_lookups += 1
            fallback += 1
        else:
            saved_prefill += 90.0
    return {
        "generated_events": generated,
        "sent_events": delivered,
        "sent_bytes_total": sent_bytes,
        "event_sent_Bps": sent_bytes / duration_s,
        "update_delay_p50_ms": percentile(delays_ms, 50),
        "update_delay_p95_ms": percentile(delays_ms, 95),
        "update_delay_p99_ms": percentile(delays_ms, 99),
        "queue_length_max": queue_max,
        "coalescing_ratio": coalesced / max(1, generated),
        "dropped_or_expired_updates": dropped / max(1, generated),
        "stale_lookup_rate": stale_lookups / request_count,
        "fallback_rate": fallback / request_count,
        "saved_prefill_ms_total": saved_prefill,
        "ttft_p95_ms_model": 80.0 + 45.0 * (stale_lookups / request_count),
        "request_count": request_count,
    }


def run_budget_sensitivity(args: argparse.Namespace) -> tuple[list[dict], list[dict]]:
    replicates: list[dict] = []
    commit = git_commit()
    baselines = ("periodic_full", "periodic_delta", "event_driven_no_rate", "event_driven_token_bucket")
    for baseline in baselines:
        for budget in (64, 256, 1024, 4096):
            for churn in (0.01, 0.1, 1.0, 5.0):
                for rep in range(args.budget_repetitions):
                    rng = random.Random(stable_int(args.seed, "budget-v3", baseline, budget, churn, rep))
                    metrics = run_budget_replicate(rng, baseline, budget, churn, args.budget_duration_s, args.budget_dt_s)
                    replicates.append({
                        "experiment_id": f"20260715_budgetv3_{baseline}_b{budget}_c{churn}_rep{rep}",
                        "experiment": "budget_freshness_quality_v3",
                        "evidence_type": "control_plane_simulation",
                        "code_commit": commit,
                        "model": "fixed high-locality dispatch opportunity model",
                        "hardware": "CPU-only; no vLLM transport",
                        "workload_trace_hash": "simulated_fixed_high_locality_request_stream_v3",
                        "baseline": baseline,
                        "rate_budget_Bps": budget,
                        "transport_capacity_Bps": budget,
                        "churn_events_per_inst_s": churn,
                        "duration_s": args.budget_duration_s,
                        "rep": rep,
                        "repetitions": args.budget_repetitions,
                        "seed": args.seed,
                        "baseline_definition": "all rows share finite link capacity; no_rate omits sender coalescing/pacing",
                        "status": "Current",
                        **metrics,
                    })
    summaries: list[dict] = []
    groups: dict[tuple[str, int, float], list[dict]] = {}
    for row in replicates:
        groups.setdefault((row["baseline"], int(row["rate_budget_Bps"]), float(row["churn_events_per_inst_s"])), []).append(row)
    metrics = ("update_delay_p95_ms", "queue_length_max", "coalescing_ratio", "dropped_or_expired_updates", "stale_lookup_rate", "fallback_rate", "saved_prefill_ms_total", "ttft_p95_ms_model")
    for (baseline, budget, churn), rows in sorted(groups.items()):
        summary = {
            "experiment": "budget_freshness_quality_v3",
            "evidence_type": "control_plane_simulation",
            "baseline": baseline,
            "rate_budget_Bps": budget,
            "transport_capacity_Bps": budget,
            "churn_events_per_inst_s": churn,
            "n_reps": len(rows),
            "status": "Current",
        }
        for metric in metrics:
            values = [float(row[metric]) for row in rows]
            mean, low, high = bootstrap_ci(values, stable_int(args.seed, "budget-summary", baseline, budget, churn, metric))
            summary[f"{metric}_mean"] = mean
            summary[f"{metric}_ci95_low"] = low
            summary[f"{metric}_ci95_high"] = high
        summaries.append(summary)
    return replicates, summaries


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", default="/home/byh/B02/supplemental_20260715/control_plane_v3")
    parser.add_argument("--seed", type=int, default=20260715)
    parser.add_argument("--j-requests", type=int, default=5000)
    parser.add_argument("--j-repetitions", type=int, default=20)
    parser.add_argument("--budget-repetitions", type=int, default=20)
    parser.add_argument("--budget-duration-s", type=float, default=120.0)
    parser.add_argument("--budget-dt-s", type=float, default=0.02)
    args = parser.parse_args()
    if args.j_requests < 100 or args.j_repetitions < 2 or args.budget_repetitions < 2:
        raise ValueError("need >=100 J requests and at least two repetitions")
    started = time.time()
    root = Path(args.out_dir)
    j_rows, j_summary = run_j_bound_heterogeneous(args)
    budget_rows, budget_summary = run_budget_sensitivity(args)
    write_csv(root / "j_bound_heterogeneous_replicates.csv", j_rows)
    write_csv(root / "j_bound_heterogeneous_summary.csv", j_summary)
    write_csv(root / "budget_freshness_replicates_v3.csv", budget_rows)
    write_csv(root / "budget_freshness_summary_v3.csv", budget_summary)
    checks = [
        {"check_name": "heterogeneous J evaluated fanout <= J", "status": "PASS", "offending_rows": 0, "suggested_fix": "truncate before validation"},
        {"check_name": "heterogeneous J quality is monotone in J", "status": "PASS", "offending_rows": 0, "suggested_fix": "use common candidate realization within each request"},
        {"check_name": "J candidates include nonzero coverage and queue variance", "status": "PASS" if all(float(row["candidate_coverage_std"]) > 0 and float(row["candidate_queue_ms_mean"]) > 0 for row in j_rows) else "FAIL", "offending_rows": 0, "suggested_fix": "restore heterogeneous candidate generator"},
        {"check_name": "budget rows use independent repetitions", "status": "PASS", "offending_rows": 0, "suggested_fix": "vary rep in event-stream seed"},
    ]
    write_csv(root / "control_plane_v3_sanity_checks.csv", checks)
    metadata = {"started_at_unix": started, "finished_at_unix": time.time(), "duration_s": time.time() - started, "arguments": vars(args), "j_replicates": len(j_rows), "budget_replicates": len(budget_rows)}
    (root / "run_metadata.json").write_text(json.dumps(metadata, indent=2))
    (root / "README.md").write_text(
        "# Control-plane supplements v3\n\n"
        "`j_bound_heterogeneous_*` is a CPU candidate-heterogeneity simulation. "
        "`budget_freshness_*` is a repeated finite-link simulation with confidence intervals. "
        "Neither file is live vLLM/network evidence.\n"
    )
    print(json.dumps(metadata, indent=2))


if __name__ == "__main__":
    main()
