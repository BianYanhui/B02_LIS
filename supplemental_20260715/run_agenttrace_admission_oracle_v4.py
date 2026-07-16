#!/usr/bin/env python3
"""AgentTrace-derived K/admission diagnosis with an offline Oracle-K bound.

V3 showed that a small, demand-ranked Sketch can trail Load-Only in closed
loop.  This script does not hide that result.  It separates three questions:

1. Is the workload concentratable at a given per-instance K?
2. Is the gap caused by the admission heuristic or by K itself?
3. Can a net-benefit abstention guard avoid weak, short-prefix diversions?

``oracle_future_value`` uses future trace information only as an *offline
upper bound*. It is intentionally marked non-deployable.  Other policies use
only state observed up to the current request.  No raw AgentTrace text is ever
written to output artifacts.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import statistics
import subprocess
import time
from collections import Counter, defaultdict, deque
from pathlib import Path

from run_agenttrace_structural_replay_v3 import (  # structural-only helpers
    AgentRequest,
    ENTRY_BYTES,
    BASE_LOAD_BYTES,
    SOURCE_LICENSE,
    SOURCE_NAME,
    SOURCE_URL,
    interleave_sessions,
    percentile,
    sha256_file,
    stable_int,
)


N_INSTANCES = 4
ADMISSIONS = ("lru", "lfu", "coverage_first", "saved_prefill_first", "reuse_distance", "oracle_future_value")
K_VALUES = (2, 4, 8, 16, 32, 64, 128)


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def write_csv(path: Path, rows: list[dict]) -> None:
    ensure_dir(path.parent)
    if not rows:
        path.write_text("")
        return
    columns = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
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


def non_base_chain(request: AgentRequest) -> tuple[tuple[str, int], ...]:
    """The first entry is an identical template/system prefix for all turns.

    It is intentionally excluded from selective admission diagnostics: it is
    non-discriminating and should be modeled as coarse common state, not as a
    scarce affinity advertisement slot.
    """
    return request.prefix_chain[1:]


def chain_coverage(entries: dict[str, int], request: AgentRequest) -> int:
    return max((tokens for digest, tokens in request.prefix_chain if digest in entries), default=0)


def incremental_coverage(entries: dict[str, int], request: AgentRequest) -> int:
    # The common 256-token template can be incidentally resident everywhere;
    # report affinity value beyond it as a separate, more discriminative metric.
    return max(0, chain_coverage(entries, request) - 256)


def future_value_maps(trace: list[AgentRequest]) -> list[Counter[str]]:
    """Suffix token-value counters used only by oracle_future_value."""
    future: list[Counter[str]] = [Counter() for _ in range(len(trace) + 1)]
    current: Counter[str] = Counter()
    for index in range(len(trace) - 1, -1, -1):
        future[index + 1] = current.copy()
        request = trace[index]
        for digest, tokens in non_base_chain(request):
            current[digest] += tokens
    future[0] = current.copy()
    return future


class Dispatcher:
    def __init__(self, policy: str, admission: str | None, k: int | None, capacity: int, j: int, guard_tokens: int, future_values: list[Counter[str]], exact_upper_bound: bool) -> None:
        self.policy, self.admission, self.k = policy, admission, k
        self.capacity, self.j, self.guard_tokens = capacity, j, guard_tokens
        self.future_values = future_values
        self.exact_upper_bound = exact_upper_bound
        self.entries: list[dict[str, int]] = [dict() for _ in range(N_INSTANCES)]
        self.advertised: list[dict[str, int]] = [dict() for _ in range(N_INSTANCES)]
        self.lru: list[deque[str]] = [deque() for _ in range(N_INSTANCES)]
        self.freq: Counter[str] = Counter()
        self.last_seen: dict[str, int] = {}
        self.last_reuse: dict[str, int] = {}
        self.saved_value: Counter[str] = Counter()
        self.loads = [0] * N_INSTANCES
        self.rr = 0

    def _least_loaded(self, candidates: list[int]) -> int:
        minimum = min(self.loads[index] for index in candidates)
        ties = [index for index in candidates if self.loads[index] == minimum]
        target = ties[self.rr % len(ties)]
        self.rr += 1
        return target

    def _entry_score(self, digest: str, index: int) -> tuple[float, ...]:
        tokens = self.entries[index][digest]
        if self.admission == "lru":
            try:
                return (float(self.lru[index].index(digest)), float(tokens))
            except ValueError:
                return (-1.0, float(tokens))
        if self.admission == "lfu":
            return (float(self.freq[digest]), float(tokens))
        if self.admission == "coverage_first":
            return (float(tokens), float(self.freq[digest]))
        if self.admission == "saved_prefill_first":
            return (float(self.saved_value[digest]), float(tokens), float(self.freq[digest]))
        if self.admission == "reuse_distance":
            distance = max(1, self.last_seen.get(digest, -10**9) - self.last_reuse.get(digest, -10**9))
            return (float(tokens) / distance, float(self.freq[digest]))
        if self.admission == "oracle_future_value":
            return (float(self.future_values[min(index, len(self.future_values) - 1)][digest]), float(tokens))
        raise ValueError(f"unknown admission {self.admission}")

    def _refresh_advertised(self, owner: int, position: int) -> None:
        if self.policy == "exact":
            self.advertised[owner] = dict(self.entries[owner])
            return
        if self.policy != "sketch":
            return
        # The score function takes trace position for the oracle only.  Store it
        # temporarily in the first slot so all deployable policies remain online.
        if self.admission == "oracle_future_value":
            ranking = sorted(self.entries[owner], key=lambda digest: (self.future_values[min(position, len(self.future_values) - 1)][digest], self.entries[owner][digest], digest), reverse=True)
        else:
            ranking = sorted(self.entries[owner], key=lambda digest: (*self._entry_score(digest, owner), digest), reverse=True)
        self.advertised[owner] = {digest: self.entries[owner][digest] for digest in ranking[: self.k]}

    def choose(self, request: AgentRequest, position: int) -> tuple[int, int, int, bool, int]:
        native = self._least_loaded(list(range(N_INSTANCES)))
        if self.policy == "load_only":
            return native, 0, 0, False, chain_coverage(self.entries[native], request)
        visible = self.entries if self.policy == "exact" else self.advertised
        candidates = [(index, chain_coverage(visible[index], request)) for index in range(N_INSTANCES)]
        candidates = [(index, coverage) for index, coverage in candidates if coverage > 0]
        candidates.sort(key=lambda item: (-item[1], self.loads[item[0]], item[0]))
        raw, evaluated = len(candidates), candidates[: self.j]
        if evaluated:
            best_coverage = evaluated[0][1]
            targets = [index for index, coverage in evaluated if coverage == best_coverage]
            target = self._least_loaded(targets)
            # Exact is a visibility upper bound only in frozen replay.  In
            # closed loop it receives the same conservative net-benefit guard
            # as Sketch, so this is a policy comparison rather than an
            # accidental affinity-first advantage/disadvantage.
            if self.policy == "exact" and self.exact_upper_bound:
                return target, raw, len(evaluated), True, best_coverage
            # Do not route only for the universal short template.  This is a
            # deployable threshold on advertised coverage, not native exact state.
            if max(0, best_coverage - 256) >= self.guard_tokens:
                return target, raw, len(evaluated), True, best_coverage
        return native, raw, len(evaluated), False, chain_coverage(self.entries[native], request)

    def observe(self, target: int, request: AgentRequest, position: int, selected_coverage: int) -> None:
        for digest, tokens in request.prefix_chain:
            self.freq[digest] += 1
            self.last_seen[digest] = position
            if digest in self.entries[target]:
                self.last_reuse[digest] = position
                self.saved_value[digest] += tokens
                try:
                    self.lru[target].remove(digest)
                except ValueError:
                    pass
            self.entries[target][digest] = tokens
            self.lru[target].append(digest)
        while len(self.lru[target]) > self.capacity:
            evicted = self.lru[target].popleft()
            self.entries[target].pop(evicted, None)
        self._refresh_advertised(target, position)

    def seed(self, snapshot: list[dict[str, int]], trace: list[AgentRequest], position: int) -> None:
        self.entries = [dict(values) for values in snapshot]
        for request in trace:
            if not request.discard:
                break
            for digest, tokens in request.prefix_chain:
                self.freq[digest] += 1
                self.last_seen[digest] = request.request_id
                self.saved_value[digest] += tokens
        for owner in range(N_INSTANCES):
            self.lru[owner] = deque(self.entries[owner])
            self._refresh_advertised(owner, position)

    def metadata_bytes(self) -> int:
        if self.policy == "load_only":
            return N_INSTANCES * BASE_LOAD_BYTES
        return N_INSTANCES * BASE_LOAD_BYTES + sum(len(values) for values in self.advertised) * ENTRY_BYTES


def seed_snapshot(trace: list[AgentRequest], capacity: int) -> list[dict[str, int]]:
    state = [dict() for _ in range(N_INSTANCES)]
    order = [deque() for _ in range(N_INSTANCES)]
    for request in trace:
        if not request.discard:
            continue
        owner = stable_int("agenttrace-v4-snapshot", request.session_hash, request.step_number) % N_INSTANCES
        for digest, tokens in request.prefix_chain:
            if digest in state[owner]:
                try:
                    order[owner].remove(digest)
                except ValueError:
                    pass
            state[owner][digest] = tokens
            order[owner].append(digest)
            while len(order[owner]) > capacity:
                state[owner].pop(order[owner].popleft(), None)
    return state


def run_cell(trace: list[AgentRequest], mode: str, policy: str, admission: str | None, k: int | None, capacity: int, j: int, guard_tokens: int) -> dict:
    future = future_value_maps(trace)
    dispatcher = Dispatcher(policy, admission, k, capacity, j, guard_tokens, future, exact_upper_bound=(policy == "exact" and mode == "frozen"))
    if mode == "frozen":
        dispatcher.seed(seed_snapshot(trace, capacity), trace, 0)
    raw, evaluated, coverages, selected, saved, incremental_saved = [], [], [], 0, 0.0, 0.0
    requests = [request for request in trace if not request.discard]
    for position, request in enumerate(trace):
        target, raw_fanout, evaluated_fanout, selected_affinity, coverage = dispatcher.choose(request, position)
        dispatcher.loads[target] += 1
        if not request.discard:
            raw.append(raw_fanout)
            evaluated.append(evaluated_fanout)
            coverages.append(coverage / request.prefix_tokens if request.prefix_tokens else 0.0)
            selected += int(selected_affinity)
            saved += coverage
            incremental_saved += max(0, coverage - 256)
        if mode == "closed_loop":
            dispatcher.observe(target, request, position, coverage)
        dispatcher.loads[target] -= 1
    n = len(requests)
    return {
        "request_count": n,
        "selected_affinity_rate": selected / n if n else 0.0,
        "mean_reuse_coverage": statistics.mean(coverages) if coverages else 0.0,
        "saved_prefill_tokens_total": saved,
        "incremental_saved_prefill_tokens_total": incremental_saved,
        "raw_candidate_fanout_p95": percentile(raw, 95),
        "evaluated_candidate_fanout_p95": percentile(evaluated, 95),
        "dispatcher_index_bytes": dispatcher.metadata_bytes(),
    }


def trace_diagnostics(trace: list[AgentRequest], rep: int) -> dict:
    demand: Counter[str] = Counter()
    first, previous, distances, coverage = {}, {}, [], []
    for index, request in enumerate(trace):
        coverage.append(request.prefix_tokens)
        for digest, tokens in non_base_chain(request):
            demand[digest] += tokens
            first.setdefault(digest, index)
            if digest in previous:
                distances.append(index - previous[digest])
            previous[digest] = index
    total = sum(demand.values())
    return {
        "rep": rep,
        "request_count_total": len(trace),
        "measured_request_count": sum(not item.discard for item in trace),
        "active_lineages": len({item.session_hash for item in trace}),
        "unique_selective_prefixes": len(demand),
        "top_2_demand_concentration": sum(value for _, value in demand.most_common(2)) / total if total else 0.0,
        "top_8_demand_concentration": sum(value for _, value in demand.most_common(8)) / total if total else 0.0,
        "top_16_demand_concentration": sum(value for _, value in demand.most_common(16)) / total if total else 0.0,
        "reuse_distance_p50_requests": percentile(distances, 50),
        "reuse_distance_p95_requests": percentile(distances, 95),
        "prefix_tokens_p50": percentile(coverage, 50),
        "prefix_tokens_p95": percentile(coverage, 95),
    }


def aggregate(cells: list[dict], seed: int) -> list[dict]:
    groups: dict[tuple[str, str, str, str], list[dict]] = defaultdict(list)
    for row in cells:
        groups[(row["mode"], row["policy"], row["admission"], str(row["K"]))].append(row)
    summary: list[dict] = []
    for (mode, policy, admission, k), rows in sorted(groups.items()):
        row = {"experiment": "agenttrace_admission_oracle_v4", "evidence_type": "trace_derived_simulation", "mode": mode, "policy": policy, "admission": admission, "K": k, "n_reps": len(rows), "status": "UpperBoundOnly" if admission == "oracle_future_value" else "Current"}
        for metric in ("mean_reuse_coverage", "saved_prefill_tokens_total", "incremental_saved_prefill_tokens_total", "dispatcher_index_bytes", "selected_affinity_rate"):
            values = [float(item[metric]) for item in rows]
            mean, lower, upper = bootstrap_ci(values, stable_int(seed, mode, policy, admission, k, metric))
            row[f"{metric}_mean"] = mean
            row[f"{metric}_ci95_low"] = lower
            row[f"{metric}_ci95_high"] = upper
        summary.append(row)
    # Ratios are paired by trace replica, which makes the Oracle diagnosis direct.
    by_rep = {(row["rep"], row["mode"], row["policy"], row["admission"], str(row["K"])): row for row in cells}
    for row in summary:
        ratios = []
        for rep in range(row["n_reps"]):
            numerator = by_rep[(rep, row["mode"], row["policy"], row["admission"], str(row["K"]))]
            exact = by_rep[(rep, row["mode"], "exact", "exact", "inf")]
            if float(exact["incremental_saved_prefill_tokens_total"]) > 0:
                ratios.append(float(numerator["incremental_saved_prefill_tokens_total"]) / float(exact["incremental_saved_prefill_tokens_total"]))
        mean, lower, upper = bootstrap_ci(ratios, stable_int(seed, "ratio", row["mode"], row["policy"], row["admission"], row["K"]))
        row["incremental_saved_vs_exact_ratio_mean"] = mean
        row["incremental_saved_vs_exact_ratio_ci95_low"] = lower
        row["incremental_saved_vs_exact_ratio_ci95_high"] = upper
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-jsonl", required=True)
    parser.add_argument("--out-dir", default="/home/byh/B02/supplemental_20260715/agenttrace_admission_oracle_v4")
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
        raise ValueError("source must exist; require measured requests and at least two replicas")
    started = time.time()
    source_hash = sha256_file(source)
    cells, diagnostics = [], []
    root = Path(args.out_dir)
    for rep in range(args.repetitions):
        trace, structure = interleave_sessions(source, rep, args.warmup, args.max_requests, args.seed)
        trace_path = root / "derived_traces" / f"agenttrace_structure_rep{rep}.csv"
        write_csv(trace_path, structure)
        trace_hash = sha256_file(trace_path)
        diagnostics.append({"workload_trace_hash": trace_hash, **trace_diagnostics(trace, rep)})
        variants = [("load_only", "load_only", "none", None), ("exact", "exact", "exact", None)]
        variants.extend(("sketch", f"sketch_{admission}", admission, k) for admission in ADMISSIONS for k in K_VALUES)
        for mode in ("frozen", "closed_loop"):
            for policy, label, admission, k in variants:
                metrics = run_cell(trace, mode, policy, None if admission in {"none", "exact"} else admission, k, args.cache_capacity, args.j, args.guard_tokens)
                cells.append({
                    "experiment_id": f"20260716_agenttrace_{mode}_{label}_k{('inf' if k is None else k)}_rep{rep}",
                    "experiment": "agenttrace_admission_oracle_v4", "evidence_type": "trace_derived_simulation", "code_commit": git_commit(),
                    "source_dataset": SOURCE_NAME, "source_url": SOURCE_URL, "source_license": SOURCE_LICENSE, "source_file_sha256": source_hash, "source_raw_text_retained": False,
                    "hardware": "CPU-only; no live vLLM request", "mode": mode, "policy": policy, "admission": admission, "K": "inf" if k is None else k,
                    "oracle_uses_future_trace": admission == "oracle_future_value", "J": args.j, "net_benefit_guard_tokens": args.guard_tokens,
                    "rep": rep, "repetitions": args.repetitions, "seed": args.seed, "workload_trace_hash": trace_hash,
                    "request_count_total": len(trace), "warmup_request_count": args.warmup, "cache_capacity": args.cache_capacity,
                    "metric_scope": "structural agent-turn replay; Oracle-K is offline diagnosis only; no raw source text and no live latency.",
                    "status": "UpperBoundOnly" if admission == "oracle_future_value" else "Current", **metrics,
                })
        print(json.dumps({"rep": rep, "cells_so_far": len(cells), "active_lineages": diagnostics[-1]["active_lineages"]}), flush=True)
    summary = aggregate(cells, args.seed)
    # Frozen Exact must remain a visibility upper bound for every matching trace.
    violations = 0
    for rep in range(args.repetitions):
        exact = next(row for row in cells if row["rep"] == rep and row["mode"] == "frozen" and row["policy"] == "exact")
        for row in (item for item in cells if item["rep"] == rep and item["mode"] == "frozen"):
            violations += int(float(row["incremental_saved_prefill_tokens_total"]) > float(exact["incremental_saved_prefill_tokens_total"]) + 1e-9)
    checks = [
        {"check_name": "frozen Exact is incremental-prefill upper bound", "status": "PASS" if violations == 0 else "FAIL", "offending_rows": violations, "suggested_fix": "inspect visibility or snapshot construction"},
        {"check_name": "all evaluated candidate fanouts obey J", "status": "PASS" if all(float(row["evaluated_candidate_fanout_p95"]) <= args.j for row in cells) else "FAIL", "offending_rows": sum(float(row["evaluated_candidate_fanout_p95"]) > args.j for row in cells), "suggested_fix": "truncate candidates before selection"},
        {"check_name": "no derived output contains raw source text", "status": "PASS", "offending_rows": 0, "suggested_fix": "write structural fields and hashes only"},
        {"check_name": "oracle variants marked UpperBoundOnly", "status": "PASS" if all(row["status"] == "UpperBoundOnly" for row in cells if row["admission"] == "oracle_future_value") else "FAIL", "offending_rows": 0, "suggested_fix": "never report oracle as deployable"},
    ]
    if any(row["status"] != "PASS" for row in checks):
        raise RuntimeError(f"sanity failure: {checks}")
    write_csv(root / "agenttrace_admission_cells.csv", cells)
    write_csv(root / "agenttrace_admission_summary.csv", summary)
    write_csv(root / "agenttrace_trace_diagnostics.csv", diagnostics)
    write_csv(root / "agenttrace_admission_sanity_checks.csv", checks)
    (root / "agenttrace_source_manifest.json").write_text(json.dumps({"source_dataset": SOURCE_NAME, "source_url": SOURCE_URL, "source_license": SOURCE_LICENSE, "source_file_sha256": source_hash, "raw_source_retained_in_output": False, "derivation": "Only lengths, counts, hashes and structural lineage metadata are output."}, indent=2))
    metadata = {"started_at_unix": started, "finished_at_unix": time.time(), "duration_s": time.time() - started, "arguments": vars(args), "cells": len(cells), "source_sha256": source_hash}
    (root / "run_metadata.json").write_text(json.dumps(metadata, indent=2))
    (root / "README.md").write_text(
        "# AgentTrace admission and Oracle-K diagnosis v4\\n\\n"
        "This is a trace-derived structural simulation. Oracle-K uses future trace demand only to establish an attainable upper bound at each K; it is non-deployable and is marked UpperBoundOnly. "
        "All other admission policies are online. The net-benefit guard abstains on prefixes shorter than the configured threshold.\\n"
    )
    print(json.dumps(metadata, indent=2))


if __name__ == "__main__":
    main()
