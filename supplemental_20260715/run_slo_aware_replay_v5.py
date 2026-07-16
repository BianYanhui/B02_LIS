#!/usr/bin/env python3
"""Paired SLO-aware affinity fallback replay on structural AgentTrace data.

This is a dispatcher-level simulation, not a live SLO benchmark.  The native
policy predicts each candidate's virtual finish time and chooses the earliest
one.  Sketch/Exact may override it only when the affinity candidate satisfies
the same request deadline and its reusable prefix repays the predicted queue
penalty.  The resulting rows establish interface compatibility with an
SLO-aware policy family while making the modeled nature of TTFT explicit.
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
from collections import Counter, defaultdict, deque
from pathlib import Path

from run_agenttrace_structural_replay_v3 import (
    AgentRequest,
    BASE_LOAD_BYTES,
    ENTRY_BYTES,
    SOURCE_LICENSE,
    SOURCE_NAME,
    SOURCE_URL,
    interleave_sessions,
    percentile,
    sha256_file,
    stable_int,
)


N_INSTANCES = 4
INTERFACES: tuple[tuple[str, int | None], ...] = (
    ("slo_load_only", None),
    ("slo_sketch_coverage", 8),
    ("slo_sketch_coverage", 16),
    ("slo_sketch_coverage", 32),
    ("slo_exact", None),
)


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def write_csv(path: Path, rows: list[dict]) -> None:
    ensure_dir(path.parent)
    if not rows:
        path.write_text("")
        return
    fields = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def git_commit() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    except Exception:
        return "TO_BE_FINALIZED"


def bootstrap_ci(values: list[float], seed: int, resamples: int = 1500) -> tuple[float, float, float]:
    if not values:
        return 0.0, 0.0, 0.0
    center = statistics.mean(values)
    if len(values) == 1:
        return center, center, center
    rng = random.Random(seed)
    samples = [statistics.mean(rng.choices(values, k=len(values))) for _ in range(resamples)]
    return center, percentile(samples, 2.5), percentile(samples, 97.5)


def chain_coverage(entries: dict[str, int], request: AgentRequest) -> int:
    return max((tokens for digest, tokens in request.prefix_chain if digest in entries), default=0)


def request_arrival_ms(request: AgentRequest, position: int, burst: bool) -> float:
    # Structural traces do not expose production arrival timestamps.  This
    # explicit synthetic schedule creates reproducible steady and bursty SLO
    # regimes without inventing semantic workload claims.
    base = position * 11.0
    if burst and position % 40 < 12:
        return base - (position % 12) * 8.0
    return base


def request_deadline_ms(request: AgentRequest) -> float:
    # Longer context gets a proportionally wider, but still finite, budget.
    return 85.0 + min(95.0, request.prefix_tokens / 48.0)


def service_ms(request: AgentRequest, coverage: int) -> float:
    remaining = max(0, request.prefix_tokens - coverage)
    return 7.0 + remaining / 52.0


class SloRouter:
    def __init__(self, interface: str, k: int | None, capacity: int, j: int, guard_ms: float) -> None:
        self.interface, self.k = interface, k
        self.capacity, self.j, self.guard_ms = capacity, j, guard_ms
        self.entries = [dict() for _ in range(N_INSTANCES)]
        self.advertised = [dict() for _ in range(N_INSTANCES)]
        self.lru = [deque() for _ in range(N_INSTANCES)]
        self.demand: Counter[str] = Counter()
        self.saved_value: Counter[str] = Counter()
        self.available_ms = [0.0] * N_INSTANCES

    def _refresh(self, owner: int) -> None:
        if self.interface == "slo_exact":
            self.advertised[owner] = dict(self.entries[owner])
        elif self.interface == "slo_sketch_coverage":
            ranked = sorted(
                self.entries[owner],
                key=lambda digest: (
                    self.entries[owner][digest], self.saved_value[digest],
                    self.demand[digest], digest,
                ),
                reverse=True,
            )
            self.advertised[owner] = {
                digest: self.entries[owner][digest] for digest in ranked[: self.k]
            }

    def _finish(self, owner: int, arrival_ms: float, request: AgentRequest, coverage: int) -> tuple[float, float]:
        start = max(arrival_ms, self.available_ms[owner])
        finish = start + service_ms(request, coverage)
        return start, finish

    def choose(self, request: AgentRequest, arrival_ms: float) -> tuple[int, int, int, bool, bool, int, float]:
        # The native SLO policy sees coarse queue/deadline information only.
        # It must not inspect private KV coverage; that visibility is the
        # treatment supplied only by the Exact or Sketch interface.
        native_options = []
        for owner in range(N_INSTANCES):
            _, finish = self._finish(owner, arrival_ms, request, 0)
            native_options.append((finish, owner, 0))
        native_finish, native, native_coverage = min(native_options)
        if self.interface == "slo_load_only":
            return native, 0, 0, False, False, native_coverage, native_finish

        visible = self.entries if self.interface == "slo_exact" else self.advertised
        candidates = [(owner, chain_coverage(visible[owner], request)) for owner in range(N_INSTANCES)]
        candidates = [(owner, coverage) for owner, coverage in candidates if coverage > 256]
        candidates.sort(key=lambda item: (-item[1], item[0]))
        raw, evaluated = len(candidates), candidates[: self.j]
        deadline = arrival_ms + request_deadline_ms(request)
        viable: list[tuple[float, int, int]] = []
        for owner, advertised_coverage in evaluated:
            # The owner-side private state may only be used for the modeled
            # completion estimate after its advertised candidate passed routing.
            coverage = chain_coverage(self.entries[owner], request)
            _, finish = self._finish(owner, arrival_ms, request, coverage)
            if finish <= deadline and native_finish - finish > self.guard_ms:
                viable.append((finish, owner, coverage))
        if viable:
            finish, owner, coverage = min(viable)
            return owner, raw, len(evaluated), True, False, coverage, finish
        deadline_abstain = bool(evaluated)
        return native, raw, len(evaluated), False, deadline_abstain, native_coverage, native_finish

    def observe(self, owner: int, request: AgentRequest, finish_ms: float) -> None:
        self.available_ms[owner] = finish_ms
        for digest, tokens in request.prefix_chain:
            self.demand[digest] += 1
            if digest in self.entries[owner]:
                self.saved_value[digest] += tokens
                try:
                    self.lru[owner].remove(digest)
                except ValueError:
                    pass
            self.entries[owner][digest] = tokens
            self.lru[owner].append(digest)
        while len(self.lru[owner]) > self.capacity:
            self.entries[owner].pop(self.lru[owner].popleft(), None)
        self._refresh(owner)

    def metadata_bytes(self) -> int:
        if self.interface == "slo_load_only":
            return N_INSTANCES * BASE_LOAD_BYTES
        return N_INSTANCES * BASE_LOAD_BYTES + sum(len(entries) for entries in self.advertised) * ENTRY_BYTES


def run_cell(trace: list[AgentRequest], interface: str, k: int | None, capacity: int, j: int, guard_ms: float, burst: bool) -> dict:
    router = SloRouter(interface, k, capacity, j, guard_ms)
    modeled_ttft, raw_fanouts, evaluated_fanouts = [], [], []
    saved, selected, abstained, misses = 0.0, 0, 0, 0
    measured = [request for request in trace if not request.discard]
    for position, request in enumerate(trace):
        arrival = request_arrival_ms(request, position, burst)
        owner, raw, evaluated, selected_affinity, deadline_abstain, coverage, finish = router.choose(request, arrival)
        ttft = finish - arrival
        if not request.discard:
            modeled_ttft.append(ttft)
            raw_fanouts.append(raw)
            evaluated_fanouts.append(evaluated)
            saved += max(0, coverage - 256)
            selected += int(selected_affinity)
            abstained += int(deadline_abstain)
            misses += int(ttft > request_deadline_ms(request))
        router.observe(owner, request, finish)
    count = len(measured)
    return {
        "request_count": count,
        "modeled_ttft_mean_ms": statistics.mean(modeled_ttft) if modeled_ttft else 0.0,
        "modeled_ttft_p50_ms": percentile(modeled_ttft, 50),
        "modeled_ttft_p95_ms": percentile(modeled_ttft, 95),
        "slo_miss_rate": misses / count if count else 0.0,
        "saved_prefill_tokens_total": saved,
        "affinity_selected_rate": selected / count if count else 0.0,
        "deadline_abstention_rate": abstained / count if count else 0.0,
        "raw_candidate_fanout_p95": percentile(raw_fanouts, 95),
        "evaluated_candidate_fanout_p95": percentile(evaluated_fanouts, 95),
        "dispatcher_index_bytes": router.metadata_bytes(),
    }


def aggregate(cells: list[dict], seed: int) -> list[dict]:
    groups: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for row in cells:
        groups[(row["load_regime"], row["interface"], str(row["K"]))].append(row)
    lookup = {(row["load_regime"], row["interface"], str(row["K"]), row["rep"]): row for row in cells}
    summary: list[dict] = []
    metrics = (
        "saved_prefill_tokens_total", "slo_miss_rate", "modeled_ttft_mean_ms",
        "modeled_ttft_p50_ms", "modeled_ttft_p95_ms", "dispatcher_index_bytes",
        "affinity_selected_rate", "deadline_abstention_rate",
    )
    for (regime, interface, k), rows in sorted(groups.items()):
        row = {
            "experiment": "slo_aware_interface_replay_v5",
            "evidence_type": "dispatcher_level_simulation",
            "load_regime": regime, "interface": interface, "K": k,
            "n_reps": len(rows), "status": "Current",
        }
        for metric in metrics:
            values = [float(item[metric]) for item in rows]
            mean, low, high = bootstrap_ci(values, stable_int(seed, regime, interface, k, metric))
            row[f"{metric}_mean"] = mean
            row[f"{metric}_ci95_low"] = low
            row[f"{metric}_ci95_high"] = high
        for rep in sorted({int(item["rep"]) for item in rows}):
            baseline = lookup[(regime, "slo_load_only", "inf", rep)]
            current = lookup[(regime, interface, k, rep)]
            exact = lookup[(regime, "slo_exact", "inf", rep)]
            row.setdefault("incremental_saved_vs_exact_ratio_values", []).append(
                float(current["saved_prefill_tokens_total"]) / float(exact["saved_prefill_tokens_total"])
                if float(exact["saved_prefill_tokens_total"]) else 0.0
            )
            row.setdefault("delta_slo_miss_vs_load_values", []).append(
                float(current["slo_miss_rate"]) - float(baseline["slo_miss_rate"])
            )
        for label in ("incremental_saved_vs_exact_ratio", "delta_slo_miss_vs_load"):
            values = row.pop(f"{label}_values")
            mean, low, high = bootstrap_ci(values, stable_int(seed, regime, interface, k, label))
            row[f"{label}_mean"] = mean
            row[f"{label}_ci95_low"] = low
            row[f"{label}_ci95_high"] = high
        summary.append(row)
    return summary


def sanity(cells: list[dict]) -> list[dict]:
    groups: dict[tuple[str, int], list[dict]] = defaultdict(list)
    for row in cells:
        groups[(row["load_regime"], int(row["rep"]))].append(row)
    same_hash = sum(
        len(rows) != len(INTERFACES) or len({row["workload_trace_hash"] for row in rows}) != 1
        for rows in groups.values()
    )
    fanout = sum(float(row["evaluated_candidate_fanout_p95"]) > float(row["J"]) for row in cells)
    negative = sum(float(row["saved_prefill_tokens_total"]) < 0 for row in cells)
    return [
        {"check_name": "same AgentTrace-derived trace within each SLO paired cell", "status": "PASS" if same_hash == 0 else "FAIL", "offending_rows": same_hash, "suggested_fix": "replay one trace for all interfaces"},
        {"check_name": "SLO replay evaluated fanout obeys J", "status": "PASS" if fanout == 0 else "FAIL", "offending_rows": fanout, "suggested_fix": "truncate candidate list before prediction"},
        {"check_name": "saved-prefill accounting is nonnegative", "status": "PASS" if negative == 0 else "FAIL", "offending_rows": negative, "suggested_fix": "inspect coverage accounting"},
    ]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", default="/home/byh/B02/supplemental_20260715/agenttrace_source_v3/agenttrace_nl2bash_1_7B.jsonl")
    parser.add_argument("--out-dir", default="/home/byh/B02/supplemental_20260715/slo_aware_replay_v5")
    parser.add_argument("--repetitions", type=int, default=20)
    parser.add_argument("--warmup", type=int, default=100)
    parser.add_argument("--max-requests", type=int, default=1000)
    parser.add_argument("--capacity", type=int, default=128)
    parser.add_argument("--j", type=int, default=4)
    parser.add_argument("--guard-ms", type=float, default=2.0)
    parser.add_argument("--seed", type=int, default=20260716)
    args = parser.parse_args()
    source = Path(args.source)
    if not source.is_file():
        raise FileNotFoundError(source)
    started = time.time()
    cells: list[dict] = []
    trace_rows: list[dict] = []
    for rep in range(args.repetitions):
        trace, stats = interleave_sessions(source, rep, args.warmup, args.max_requests, args.seed)
        trace_path = Path(args.out_dir) / "derived_traces" / f"slo_structure_rep{rep}.csv"
        ensure_dir(trace_path.parent)
        # Structural-only artifact: no raw tool arguments or natural language.
        with trace_path.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=["request_id", "session_hash", "step_number", "prefix_tokens", "discard"])
            writer.writeheader()
            writer.writerows({
                "request_id": request.request_id, "session_hash": request.session_hash,
                "step_number": request.step_number, "prefix_tokens": request.prefix_tokens,
                "discard": request.discard,
            } for request in trace)
        trace_hash = sha256_file(trace_path)
        trace_rows.append({
            "rep": rep,
            "workload_trace_hash": trace_hash,
            "interleaved_session_count": len(stats),
            "structural_session_stats_json": json.dumps(stats, sort_keys=True),
        })
        for regime, burst in (("steady", False), ("burst", True)):
            for interface, k in INTERFACES:
                metrics = run_cell(trace, interface, k, args.capacity, args.j, args.guard_ms, burst)
                cells.append({
                    "experiment_id": f"20260716_slo_{regime}_{interface}_{k if k is not None else 'inf'}_rep{rep}",
                    "experiment": "slo_aware_interface_replay_v5",
                    "evidence_type": "dispatcher_level_simulation",
                    "code_commit": git_commit(), "model": "structural AgentTrace-derived state replay",
                    "hardware": "CPU-only deterministic dispatcher simulation",
                    "load_regime": regime, "interface": interface, "K": "inf" if k is None else k,
                    "J": args.j, "guard_ms": args.guard_ms, "rep": rep,
                    "repetitions": args.repetitions, "seed": args.seed,
                    "workload_trace_hash": trace_hash, "source_name": SOURCE_NAME,
                    "source_url": SOURCE_URL, "source_license": SOURCE_LICENSE,
                    "metric_scope": "Modeled TTFT/deadline outcomes; not live serving latency.",
                    "status": "Current", **metrics,
                })
    checks = sanity(cells)
    if any(row["status"] != "PASS" for row in checks):
        raise RuntimeError(f"SLO replay sanity checks failed: {checks}")
    root = Path(args.out_dir)
    write_csv(root / "slo_aware_cells.csv", cells)
    write_csv(root / "slo_aware_summary.csv", aggregate(cells, args.seed))
    write_csv(root / "slo_aware_sanity_checks.csv", checks)
    write_csv(root / "slo_aware_trace_manifest.csv", trace_rows)
    (root / "source_manifest.json").write_text(json.dumps({
        "source": str(source), "sha256": sha256_file(source), "source_name": SOURCE_NAME,
        "source_url": SOURCE_URL, "source_license": SOURCE_LICENSE,
        "raw_text_included": False,
    }, indent=2))
    metadata = {"started_at_unix": started, "finished_at_unix": time.time(), "duration_s": time.time() - started, "arguments": vars(args), "cells": len(cells)}
    (root / "run_metadata.json").write_text(json.dumps(metadata, indent=2))
    (root / "README.md").write_text(
        "# SLO-aware paired replay (V5)\n\n"
        "This is a trace-derived dispatcher simulation. SLO deadlines and virtual queue timing are explicit model inputs. "
        "Do not cite modeled TTFT or miss rate as live vLLM serving results.\n"
    )
    print(json.dumps(metadata, indent=2))


if __name__ == "__main__":
    main()
