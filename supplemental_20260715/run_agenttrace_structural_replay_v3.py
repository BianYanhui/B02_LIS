#!/usr/bin/env python3
"""Trace-derived structural replay from public AgentTrace tool executions.

Raw prompts, reasoning and tool outputs are read only to derive lengths and
hashes, then are intentionally excluded from every output artifact.  The
result therefore measures the state-interface trade-off under real recorded
agent turn structure, not semantic model quality and not live vLLM latency.
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
from collections import Counter, defaultdict, deque
from dataclasses import dataclass
from pathlib import Path


ENTRY_BYTES = 64
BASE_LOAD_BYTES = 96
SOURCE_NAME = "pagarsky/agent-trace: nl2bash_1_7B_20260403T211347Z"
SOURCE_URL = "https://huggingface.co/datasets/pagarsky/agent-trace"
SOURCE_LICENSE = "Apache-2.0"


def stable_int(*parts: object) -> int:
    return int.from_bytes(hashlib.blake2b("|".join(map(str, parts)).encode(), digest_size=8).digest(), "big")


def digest_text(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:20]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


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


@dataclass(frozen=True)
class AgentRequest:
    request_id: int
    session_hash: str
    step_number: int
    prefix_chain: tuple[tuple[str, int], ...]
    prefix_tokens: int
    tool_span_count_before: int
    tool_latency_ms_before: float
    discard: bool


def text_length(value: object) -> int:
    return len(value) if isinstance(value, str) else 0


def session_steps(row: dict, source_hash: str) -> list[AgentRequest]:
    """Derive token/lineage structure without returning any raw text."""
    trace_id = str(row.get("trace_id", ""))
    session_hash = digest_text(f"{source_hash}:{trace_id}")
    metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    template_hash = str(metadata.get("chat_template", "unknown-template"))
    system_digest = digest_text(f"template:{template_hash}")
    task_digest = digest_text(f"task:{session_hash}")
    prompt_chars = text_length(row.get("prompt"))
    spans = row.get("spans") if isinstance(row.get("spans"), list) else []
    steps = row.get("llm_steps") if isinstance(row.get("llm_steps"), list) else []
    # The source exposes a chat-template hash but not a normalized template
    # token count.  Keep a fixed 256-token common prefix explicit in metadata.
    system_tokens = 256
    history_chars = prompt_chars
    prior_nodes: list[tuple[str, int]] = []
    output: list[AgentRequest] = []
    span_cursor = 0
    for index, step in enumerate(steps, start=1):
        prefix_tokens = min(4096, max(system_tokens, system_tokens + math.ceil(history_chars / 4)))
        chain = tuple([(system_digest, system_tokens), (task_digest, min(prefix_tokens, system_tokens + math.ceil(prompt_chars / 4)))] + prior_nodes)
        prior_span_count = min(len(spans), span_cursor)
        prior_latency = sum(float(span.get("duration_ms", 0.0) or 0.0) for span in spans[:prior_span_count] if isinstance(span, dict))
        output.append(AgentRequest(-1, session_hash, index, chain, prefix_tokens, prior_span_count, prior_latency, False))
        reasoning_chars = text_length(step.get("reasoning_content")) if isinstance(step, dict) else 0
        model_chars = text_length(step.get("model_output")) if isinstance(step, dict) else 0
        tool_chars = 0
        if span_cursor < len(spans) and isinstance(spans[span_cursor], dict):
            tool_chars = text_length(spans[span_cursor].get("tool_output")) + text_length(spans[span_cursor].get("tool_input"))
            span_cursor += 1
        history_chars += reasoning_chars + model_chars + tool_chars
        prior_nodes.append((digest_text(f"{session_hash}:step:{index}"), prefix_tokens))
    return output


def interleave_sessions(source: Path, rep: int, warmup: int, max_requests: int, seed: int) -> tuple[list[AgentRequest], list[dict]]:
    source_hash = sha256_file(source)
    sessions: list[list[AgentRequest]] = []
    with source.open() as handle:
        for line in handle:
            row = json.loads(line)
            derived = session_steps(row, source_hash)
            if derived:
                sessions.append(derived)
    rng = random.Random(stable_int(seed, "agenttrace-schedule", rep, source_hash))
    positions = [0 for _ in sessions]
    available = [index for index, session in enumerate(sessions) if session]
    trace: list[AgentRequest] = []
    while available and len(trace) < max_requests:
        session_index = available[rng.randrange(len(available))]
        request = sessions[session_index][positions[session_index]]
        request = AgentRequest(len(trace), request.session_hash, request.step_number, request.prefix_chain, request.prefix_tokens, request.tool_span_count_before, request.tool_latency_ms_before, len(trace) < warmup)
        trace.append(request)
        positions[session_index] += 1
        if positions[session_index] >= len(sessions[session_index]):
            available.remove(session_index)
    structural_rows = [
        {
            "request_id": request.request_id,
            "session_hash": request.session_hash,
            "step_number": request.step_number,
            "prefix_tokens": request.prefix_tokens,
            "prefix_chain_depth": len(request.prefix_chain),
            "tool_span_count_before": request.tool_span_count_before,
            "tool_latency_ms_before": request.tool_latency_ms_before,
            "discard": request.discard,
        }
        for request in trace
    ]
    return trace, structural_rows


class StructuralDispatcher:
    def __init__(self, policy: str, k: int | None, capacity: int, j: int) -> None:
        self.policy = policy
        self.k = k
        self.capacity = capacity
        self.j = j
        self.entries: list[dict[str, int]] = [dict() for _ in range(4)]
        self.lru: list[deque[str]] = [deque() for _ in range(4)]
        self.advertised: list[dict[str, int]] = [dict() for _ in range(4)]
        self.demand: Counter[str] = Counter()
        self.loads = [0, 0, 0, 0]
        self.rr = 0

    def _least_loaded(self, candidates: list[int]) -> int:
        least = min(self.loads[index] for index in candidates)
        tied = [index for index in candidates if self.loads[index] == least]
        target = tied[self.rr % len(tied)]
        self.rr += 1
        return target

    def _best_coverage(self, index: int, chain: tuple[tuple[str, int], ...], visible: dict[str, int]) -> int:
        return max((tokens for digest, tokens in chain if digest in visible), default=0)

    def choose(self, request: AgentRequest) -> tuple[int, int, int, int]:
        for digest, _ in request.prefix_chain:
            self.demand[digest] += 1
        native = self._least_loaded([0, 1, 2, 3])
        if self.policy == "load_only":
            # Load-only cannot expose or rank affinity candidates, but a
            # coincidental native placement may still land on reusable state.
            return native, 0, 0, self._best_coverage(native, request.prefix_chain, self.entries[native])
        source = self.entries if self.policy == "exact" else self.advertised
        candidates = [(index, self._best_coverage(index, request.prefix_chain, source[index])) for index in range(4)]
        candidates = [(index, coverage) for index, coverage in candidates if coverage > 0]
        candidates.sort(key=lambda item: (-item[1], self.loads[item[0]], item[0]))
        raw = len(candidates)
        evaluated = candidates[: self.j]
        if evaluated:
            best_coverage = evaluated[0][1]
            best = [index for index, coverage in evaluated if coverage == best_coverage]
            return self._least_loaded(best), raw, len(evaluated), best_coverage
        return native, raw, 0, 0

    def observe(self, target: int, request: AgentRequest) -> None:
        for digest, tokens in request.prefix_chain:
            if digest in self.entries[target]:
                try:
                    self.lru[target].remove(digest)
                except ValueError:
                    pass
            self.entries[target][digest] = tokens
            self.lru[target].append(digest)
        while len(self.lru[target]) > self.capacity:
            digest = self.lru[target].popleft()
            self.entries[target].pop(digest, None)
        if self.policy == "exact":
            self.advertised[target] = dict(self.entries[target])
        elif self.policy.startswith("sketch"):
            ranked = sorted(self.entries[target], key=lambda digest: (self.demand[digest], self.entries[target][digest], digest), reverse=True)
            self.advertised[target] = {digest: self.entries[target][digest] for digest in ranked[: self.k]}

    def metadata_bytes(self) -> int:
        if self.policy == "load_only":
            return 4 * BASE_LOAD_BYTES
        return 4 * BASE_LOAD_BYTES + sum(len(entries) for entries in self.advertised) * ENTRY_BYTES


def seed_snapshot(trace: list[AgentRequest], capacity: int) -> list[dict[str, int]]:
    state = [dict() for _ in range(4)]
    for request in trace:
        if not request.discard:
            continue
        owner = stable_int("agenttrace-snapshot", request.session_hash, request.step_number) % 4
        for digest, tokens in request.prefix_chain:
            if len(state[owner]) < capacity or digest in state[owner]:
                state[owner][digest] = tokens
    return state


def run_cell(trace: list[AgentRequest], policy: str, k: int | None, capacity: int, j: int, mode: str) -> dict:
    dispatcher = StructuralDispatcher(policy, k, capacity, j)
    if mode == "frozen":
        dispatcher.entries = seed_snapshot(trace, capacity)
        for request in trace:
            if request.discard:
                for digest, _ in request.prefix_chain:
                    dispatcher.demand[digest] += 1
        for index in range(4):
            dispatcher.observe(index, AgentRequest(-1, "seed", 0, tuple(), 0, 0, 0.0, False))
            if policy == "exact":
                dispatcher.advertised[index] = dict(dispatcher.entries[index])
            elif policy.startswith("sketch"):
                ranked = sorted(dispatcher.entries[index], key=lambda digest: (dispatcher.demand[digest], dispatcher.entries[index][digest], digest), reverse=True)
                dispatcher.advertised[index] = {digest: dispatcher.entries[index][digest] for digest in ranked[:k]}
    raw, evaluated, coverage, reuse, saved_tokens = [], [], [], 0, 0.0
    requests = [request for request in trace if not request.discard]
    for request in trace:
        target, raw_fanout, evaluated_fanout, covered_tokens = dispatcher.choose(request)
        if not request.discard:
            raw.append(raw_fanout)
            evaluated.append(evaluated_fanout)
            coverage.append(covered_tokens / request.prefix_tokens if request.prefix_tokens else 0.0)
            reuse += int(covered_tokens > 0)
            saved_tokens += covered_tokens
        if mode == "closed_loop":
            dispatcher.observe(target, request)
    n = len(requests)
    return {
        "request_count": n,
        "observed_reuse_hit_rate": reuse / n if n else 0.0,
        "mean_reuse_coverage": statistics.mean(coverage) if coverage else 0.0,
        "saved_prefill_tokens_total": saved_tokens,
        "raw_candidate_fanout_p95": percentile(raw, 95),
        "evaluated_candidate_fanout_p95": percentile(evaluated, 95),
        "dispatcher_index_bytes": dispatcher.metadata_bytes(),
    }


def aggregate(cells: list[dict], seed: int) -> list[dict]:
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in cells:
        groups[(row["mode"], row["policy"])].append(row)
    output: list[dict] = []
    for (mode, policy), rows in sorted(groups.items()):
        summary = {"experiment": "agenttrace_structural_replay_v3", "evidence_type": "trace_derived_simulation", "mode": mode, "policy": policy, "K": rows[0]["K"], "n_reps": len(rows), "status": "Current"}
        for metric in ("observed_reuse_hit_rate", "mean_reuse_coverage", "saved_prefill_tokens_total", "dispatcher_index_bytes"):
            values = [float(row[metric]) for row in rows]
            mean, low, high = bootstrap_ci(values, stable_int(seed, mode, policy, metric))
            summary[f"{metric}_mean"] = mean
            summary[f"{metric}_ci95_low"] = low
            summary[f"{metric}_ci95_high"] = high
        output.append(summary)
    return output


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-jsonl", required=True)
    parser.add_argument("--out-dir", default="/home/byh/B02/supplemental_20260715/agenttrace_structural_v3")
    parser.add_argument("--seed", type=int, default=20260715)
    parser.add_argument("--repetitions", type=int, default=10)
    parser.add_argument("--max-requests", type=int, default=512)
    parser.add_argument("--warmup", type=int, default=128)
    parser.add_argument("--cache-capacity", type=int, default=128)
    parser.add_argument("--j", type=int, default=8)
    args = parser.parse_args()
    source = Path(args.source_jsonl)
    if not source.is_file() or args.max_requests <= args.warmup or args.repetitions < 2:
        raise ValueError("source must exist; use measured requests and at least two repetitions")
    started = time.time()
    source_hash = sha256_file(source)
    root = Path(args.out_dir)
    cells: list[dict] = []
    structural_rows: list[dict] = []
    for rep in range(args.repetitions):
        trace, structure = interleave_sessions(source, rep, args.warmup, args.max_requests, args.seed)
        trace_path = root / "derived_traces" / f"agenttrace_structure_rep{rep}.csv"
        write_csv(trace_path, structure)
        trace_hash = sha256_file(trace_path)
        structural_rows.extend({"rep": rep, **row} for row in structure)
        for mode in ("frozen", "closed_loop"):
            for policy, k in (("load_only", None), ("sketch_k8", 8), ("sketch_k16", 16), ("exact", None)):
                metrics = run_cell(trace, policy, k, args.cache_capacity, args.j, mode)
                cells.append({
                    "experiment_id": f"20260715_agenttrace_{mode}_{policy}_rep{rep}",
                    "experiment": "agenttrace_structural_replay_v3",
                    "evidence_type": "trace_derived_simulation",
                    "code_commit": git_commit(),
                    "source_dataset": SOURCE_NAME,
                    "source_url": SOURCE_URL,
                    "source_license": SOURCE_LICENSE,
                    "source_file_sha256": source_hash,
                    "source_raw_text_retained": False,
                    "model": "AgentTrace Qwen/Qwen3-1.7B source; B02 dispatcher simulation",
                    "hardware": "CPU-only; no live vLLM request",
                    "mode": mode,
                    "policy": policy,
                    "K": "inf" if policy in {"load_only", "exact"} else k,
                    "J": args.j,
                    "rep": rep,
                    "repetitions": args.repetitions,
                    "seed": args.seed,
                    "workload_trace_hash": trace_hash,
                    "request_count_total": len(trace),
                    "warmup_request_count": args.warmup,
                    "cache_capacity": args.cache_capacity,
                    "metric_scope": "trace-derived agent turn structure; no raw text and no live vLLM latency",
                    "status": "Current",
                    **metrics,
                })
    summary = aggregate(cells, args.seed)
    frozen = [row for row in cells if row["mode"] == "frozen"]
    for rep in range(args.repetitions):
        rep_rows = [row for row in frozen if row["rep"] == rep]
        exact = next(row for row in rep_rows if row["policy"] == "exact")
        if any(float(exact["saved_prefill_tokens_total"]) + 1e-9 < float(row["saved_prefill_tokens_total"]) for row in rep_rows):
            raise RuntimeError("Exact must remain the frozen information upper bound")
    write_csv(root / "agenttrace_structural_cells.csv", cells)
    write_csv(root / "agenttrace_structural_summary.csv", summary)
    manifest = {
        "source_dataset": SOURCE_NAME,
        "source_url": SOURCE_URL,
        "source_license": SOURCE_LICENSE,
        "source_file_sha256": source_hash,
        "raw_source_retained_in_output": False,
        "derivation": "Only text lengths, step counts, tool-span latency, common chat-template hash, and SHA-256 lineage identifiers are used. No prompt, reasoning, tool input, or tool output is written to output files.",
    }
    (root / "agenttrace_source_manifest.json").write_text(json.dumps(manifest, indent=2))
    checks = [
        {"check_name": "source file hash recorded", "status": "PASS", "offending_rows": 0, "suggested_fix": "record source SHA-256 in manifest and every cell"},
        {"check_name": "derived trace contains no raw text columns", "status": "PASS" if all(not {"prompt", "reasoning_content", "tool_input", "tool_output"}.intersection(row) for row in structural_rows) else "FAIL", "offending_rows": 0, "suggested_fix": "write only structural/hash fields"},
        {"check_name": "frozen Exact is information upper bound", "status": "PASS", "offending_rows": 0, "suggested_fix": "inspect interface visibility"},
        {"check_name": "evaluated fanout <= J", "status": "PASS" if all(float(row["evaluated_candidate_fanout_p95"]) <= args.j for row in cells) else "FAIL", "offending_rows": 0, "suggested_fix": "truncate candidate list before selection"},
    ]
    write_csv(root / "agenttrace_structural_sanity_checks.csv", checks)
    metadata = {"started_at_unix": started, "finished_at_unix": time.time(), "duration_s": time.time() - started, "arguments": vars(args), "cells": len(cells), "structural_requests": len(structural_rows), "source_sha256": source_hash}
    (root / "run_metadata.json").write_text(json.dumps(metadata, indent=2))
    (root / "README.md").write_text(
        "# AgentTrace-derived structural replay v3\n\n"
        "This is a trace-derived dispatcher simulation based on recorded tool-using agent turn structure. "
        "It is not semantic evaluation, a live vLLM cache counter, or an end-to-end latency benchmark.\n"
    )
    print(json.dumps(metadata, indent=2))


if __name__ == "__main__":
    main()
