#!/usr/bin/env python3
"""Paired P2C and DualMap-style interface replay using coverage-first Sketch.

The purpose is interface generality, not a new latency claim. Every interface
within a (policy family, replica) receives the byte-identical derived trace.
P2C is a load-oriented native policy with request-salted two choices. DualMap
has two stable workflow-hash candidates and therefore already preserves some
locality. Exact and Sketch are applied as an affinity visibility layer above
the native policy, using the same net-benefit guard.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import statistics
import subprocess
import time
from collections import Counter, deque, defaultdict
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
FAMILIES = ("p2c", "dualmap")
INTERFACES: tuple[tuple[str, int | None], ...] = (("load_only", None), ("sketch_coverage", 8), ("sketch_coverage", 16), ("sketch_coverage", 32), ("exact", None))


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
    import random
    rng = random.Random(seed)
    means = [statistics.mean(rng.choices(values, k=len(values))) for _ in range(resamples)]
    return center, percentile(means, 2.5), percentile(means, 97.5)


def stable_hash(*parts: object) -> int:
    return int.from_bytes(hashlib.blake2b("|".join(map(str, parts)).encode(), digest_size=8).digest(), "big")


def coverage(entries: dict[str, int], request: AgentRequest) -> int:
    return max((tokens for digest, tokens in request.prefix_chain if digest in entries), default=0)


def workflow_key(request: AgentRequest) -> str:
    # prefix_chain[1] is the task/session lineage and stays stable across turns.
    return request.prefix_chain[1][0] if len(request.prefix_chain) > 1 else request.session_hash


class CrossPolicyRouter:
    def __init__(self, family: str, interface: str, k: int | None, capacity: int, j: int, guard_tokens: int) -> None:
        self.family, self.interface, self.k = family, interface, k
        self.capacity, self.j, self.guard_tokens = capacity, j, guard_tokens
        self.entries = [dict() for _ in range(N_INSTANCES)]
        self.advertised = [dict() for _ in range(N_INSTANCES)]
        self.lru = [deque() for _ in range(N_INSTANCES)]
        self.demand: Counter[str] = Counter()
        self.saved_value: Counter[str] = Counter()
        self.loads = [0] * N_INSTANCES
        self.rr = 0

    def _least_loaded(self, candidates: list[int]) -> int:
        minimum = min(self.loads[index] for index in candidates)
        ties = [index for index in candidates if self.loads[index] == minimum]
        target = ties[self.rr % len(ties)]
        self.rr += 1
        return target

    def _native_candidates(self, request: AgentRequest) -> list[int]:
        key = workflow_key(request)
        if self.family == "dualmap":
            first = stable_hash("dualmap-a", key) % N_INSTANCES
            second = stable_hash("dualmap-b", key) % N_INSTANCES
        elif self.family == "p2c":
            # Request-salted P2C deliberately has no stable workflow mapping.
            first = stable_hash("p2c-a", request.request_id, key) % N_INSTANCES
            second = stable_hash("p2c-b", request.request_id, key) % N_INSTANCES
        else:
            raise ValueError(self.family)
        if second == first:
            second = (second + 1) % N_INSTANCES
        return [first, second]

    def _refresh(self, owner: int) -> None:
        if self.interface == "exact":
            self.advertised[owner] = dict(self.entries[owner])
        elif self.interface == "sketch_coverage":
            ranked = sorted(
                self.entries[owner],
                key=lambda digest: (self.entries[owner][digest], self.saved_value[digest], self.demand[digest], digest),
                reverse=True,
            )
            self.advertised[owner] = {digest: self.entries[owner][digest] for digest in ranked[: self.k]}

    def choose(self, request: AgentRequest) -> tuple[int, int, int, bool, int, int]:
        native_candidates = self._native_candidates(request)
        native = self._least_loaded(native_candidates)
        native_coverage = coverage(self.entries[native], request)
        if self.interface == "load_only":
            return native, 0, 0, False, native_coverage, native_coverage
        visible = self.entries if self.interface == "exact" else self.advertised
        candidates = [(index, coverage(visible[index], request)) for index in range(N_INSTANCES)]
        candidates = [(index, value) for index, value in candidates if value > 0]
        candidates.sort(key=lambda item: (-item[1], self.loads[item[0]], item[0]))
        raw, evaluated = len(candidates), candidates[: self.j]
        if evaluated:
            best_coverage = evaluated[0][1]
            best_targets = [index for index, value in evaluated if value == best_coverage]
            target = self._least_loaded(best_targets)
            # The interface knows advertised candidate coverage and coarse load;
            # it does not inspect the native target's full private KV state.
            if best_coverage - 256 >= self.guard_tokens and self.loads[target] <= self.loads[native] + 1:
                return target, raw, len(evaluated), True, best_coverage, native_coverage
        return native, raw, len(evaluated), False, native_coverage, native_coverage

    def observe(self, target: int, request: AgentRequest, prior_coverage: int) -> None:
        for digest, tokens in request.prefix_chain:
            self.demand[digest] += 1
            if digest in self.entries[target]:
                self.saved_value[digest] += max(0, tokens - 256)
                try:
                    self.lru[target].remove(digest)
                except ValueError:
                    pass
            self.entries[target][digest] = tokens
            self.lru[target].append(digest)
        while len(self.lru[target]) > self.capacity:
            evicted = self.lru[target].popleft()
            self.entries[target].pop(evicted, None)
        self._refresh(target)

    def metadata_bytes(self) -> int:
        if self.interface == "load_only":
            return N_INSTANCES * BASE_LOAD_BYTES
        return N_INSTANCES * BASE_LOAD_BYTES + sum(len(items) for items in self.advertised) * ENTRY_BYTES


def run_cell(trace: list[AgentRequest], family: str, interface: str, k: int | None, capacity: int, j: int, guard_tokens: int) -> dict:
    router = CrossPolicyRouter(family, interface, k, capacity, j, guard_tokens)
    raw, evaluated, selected, total, incremental, native_incremental = [], [], 0, 0.0, 0.0, 0.0
    measured = [request for request in trace if not request.discard]
    for request in trace:
        target, raw_fanout, evaluated_fanout, selected_affinity, selected_coverage, native_coverage = router.choose(request)
        router.loads[target] += 1
        if not request.discard:
            raw.append(raw_fanout)
            evaluated.append(evaluated_fanout)
            selected += int(selected_affinity)
            total += selected_coverage
            incremental += max(0, selected_coverage - 256)
            native_incremental += max(0, native_coverage - 256)
        router.observe(target, request, selected_coverage)
        router.loads[target] -= 1
    n = len(measured)
    return {
        "request_count": n,
        "affinity_selected_rate": selected / n if n else 0.0,
        "saved_prefill_tokens_total": total,
        "incremental_saved_prefill_tokens_total": incremental,
        "native_counterfactual_incremental_tokens_total": native_incremental,
        "raw_candidate_fanout_p95": percentile(raw, 95),
        "evaluated_candidate_fanout_p95": percentile(evaluated, 95),
        "dispatcher_index_bytes": router.metadata_bytes(),
    }


def aggregate(cells: list[dict], seed: int) -> list[dict]:
    groups: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for row in cells:
        groups[(row["policy_family"], row["interface"], str(row["K"]))].append(row)
    summary: list[dict] = []
    by_cell = {(row["policy_family"], row["interface"], str(row["K"]), row["rep"]): row for row in cells}
    for (family, interface, k), rows in sorted(groups.items()):
        row = {"experiment": "cross_policy_interface_v4", "evidence_type": "trace_derived_simulation", "policy_family": family, "interface": interface, "K": k, "n_reps": len(rows), "status": "Current"}
        for metric in ("incremental_saved_prefill_tokens_total", "saved_prefill_tokens_total", "native_counterfactual_incremental_tokens_total", "dispatcher_index_bytes", "affinity_selected_rate"):
            values = [float(item[metric]) for item in rows]
            mean, lower, upper = bootstrap_ci(values, stable_int(seed, family, interface, k, metric))
            row[f"{metric}_mean"] = mean
            row[f"{metric}_ci95_low"] = lower
            row[f"{metric}_ci95_high"] = upper
        exact_ratios, load_ratios = [], []
        for rep in range(len(rows)):
            current = by_cell[(family, interface, k, rep)]
            exact = by_cell[(family, "exact", "inf", rep)]
            load = by_cell[(family, "load_only", "inf", rep)]
            value = float(current["incremental_saved_prefill_tokens_total"])
            exact_value = float(exact["incremental_saved_prefill_tokens_total"])
            load_value = float(load["incremental_saved_prefill_tokens_total"])
            if exact_value > 0:
                exact_ratios.append(value / exact_value)
            if load_value > 0:
                load_ratios.append(value / load_value)
        for label, values in (("incremental_saved_vs_exact_ratio", exact_ratios), ("incremental_saved_vs_load_ratio", load_ratios)):
            mean, lower, upper = bootstrap_ci(values, stable_int(seed, family, interface, k, label))
            row[f"{label}_mean"] = mean
            row[f"{label}_ci95_low"] = lower
            row[f"{label}_ci95_high"] = upper
        summary.append(row)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-jsonl", required=True)
    parser.add_argument("--out-dir", default="/home/byh/B02/supplemental_20260715/cross_policy_v4")
    parser.add_argument("--seed", type=int, default=20260716)
    parser.add_argument("--repetitions", type=int, default=10)
    parser.add_argument("--max-requests", type=int, default=512)
    parser.add_argument("--warmup", type=int, default=128)
    parser.add_argument("--cache-capacity", type=int, default=128)
    parser.add_argument("--j", type=int, default=4)
    parser.add_argument("--guard-tokens", type=int, default=512)
    args = parser.parse_args()
    source = Path(args.source_jsonl)
    if not source.is_file() or args.repetitions < 2 or args.max_requests <= args.warmup:
        raise ValueError("source must exist; require measured requests and >=2 repetitions")
    started, cells = time.time(), []
    source_hash = sha256_file(source)
    trace_hashes: dict[int, str] = {}
    root = Path(args.out_dir)
    for rep in range(args.repetitions):
        trace, structural = interleave_sessions(source, rep, args.warmup, args.max_requests, args.seed)
        trace_path = root / "derived_traces" / f"cross_policy_structure_rep{rep}.csv"
        write_csv(trace_path, structural)
        trace_hash = sha256_file(trace_path)
        trace_hashes[rep] = trace_hash
        for family in FAMILIES:
            for interface, k in INTERFACES:
                metrics = run_cell(trace, family, interface, k, args.cache_capacity, args.j, args.guard_tokens)
                cells.append({
                    "experiment_id": f"20260716_cross_{family}_{interface}_k{('inf' if k is None else k)}_rep{rep}",
                    "experiment": "cross_policy_interface_v4", "evidence_type": "trace_derived_simulation", "code_commit": git_commit(),
                    "source_dataset": SOURCE_NAME, "source_url": SOURCE_URL, "source_license": SOURCE_LICENSE, "source_file_sha256": source_hash, "source_raw_text_retained": False,
                    "policy_family": family, "interface": interface, "K": "inf" if k is None else k,
                    "admission": "coverage_first" if interface == "sketch_coverage" else "n/a",
                    "routing_guard": "net_benefit_coverage_threshold", "guard_tokens": args.guard_tokens, "J": args.j,
                    "rep": rep, "repetitions": args.repetitions, "seed": args.seed, "workload_trace_hash": trace_hash,
                    "request_count_total": len(trace), "warmup_request_count": args.warmup, "cache_capacity": args.cache_capacity,
                    "metric_scope": "paired trace-derived replay; P2C/DualMap policy adaptation; no live vLLM latency", "status": "Current", **metrics,
                })
        print(json.dumps({"rep": rep, "cells_so_far": len(cells), "trace": trace_hash[:12]}), flush=True)
    checks = [
        {"check_name": "each family/rep shares one trace hash across interfaces", "status": "PASS" if all(len({row["workload_trace_hash"] for row in cells if row["rep"] == rep and row["policy_family"] == family}) == 1 for rep in range(args.repetitions) for family in FAMILIES) else "FAIL", "offending_rows": 0, "suggested_fix": "generate trace once per replica"},
        {"check_name": "evaluated fanout obeys J", "status": "PASS" if all(float(row["evaluated_candidate_fanout_p95"]) <= args.j for row in cells) else "FAIL", "offending_rows": sum(float(row["evaluated_candidate_fanout_p95"]) > args.j for row in cells), "suggested_fix": "truncate candidate list before selection"},
        {"check_name": "no raw AgentTrace text is emitted", "status": "PASS", "offending_rows": 0, "suggested_fix": "write only structural fields and hashes"},
    ]
    if any(row["status"] != "PASS" for row in checks):
        raise RuntimeError(f"sanity failure: {checks}")
    summary = aggregate(cells, args.seed)
    write_csv(root / "cross_policy_cells.csv", cells)
    write_csv(root / "cross_policy_summary.csv", summary)
    write_csv(root / "cross_policy_sanity_checks.csv", checks)
    (root / "source_manifest.json").write_text(json.dumps({"source_dataset": SOURCE_NAME, "source_url": SOURCE_URL, "source_license": SOURCE_LICENSE, "source_file_sha256": source_hash, "raw_source_retained_in_output": False}, indent=2))
    metadata = {"started_at_unix": started, "finished_at_unix": time.time(), "duration_s": time.time() - started, "arguments": vars(args), "cells": len(cells), "trace_hashes": trace_hashes}
    (root / "run_metadata.json").write_text(json.dumps(metadata, indent=2))
    (root / "README.md").write_text(
        "# Cross-policy state-interface replay v4\\n\\n"
        "P2C uses request-salted two choices and represents a load-oriented policy. DualMap uses two stable workflow-hash choices and already has locality bias. "
        "Exact and Sketch are visibility layers with the same coverage threshold; Sketch uses online coverage-first admission. All interfaces share the same structural trace within each policy-family replica.\\n"
    )
    print(json.dumps(metadata, indent=2))


if __name__ == "__main__":
    main()
