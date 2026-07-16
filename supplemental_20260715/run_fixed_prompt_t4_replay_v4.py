#!/usr/bin/env python3
"""Input-identical, cache-isolated live replay for B02.

This replaces the V3 live treatment whose cache namespace was embedded in the
semantic prompt.  V4 puts isolation in vLLM's ``cache_salt`` request field,
uses an identical prompt byte sequence for the same logical request under all
policies, and fixes generation length.  It records vLLM cached-token telemetry
and Prometheus prefix-cache counters alongside TTFT.

The experiment is deliberately a sampled, paired live evaluation.  It does
not infer a cache hit from dispatcher state: ``vllm_cached_tokens`` is returned
by vLLM's OpenAI usage object and ``metric_prefix_cache_hits_delta`` is sampled
from each endpoint's /metrics counter.
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
import subprocess
import time
from collections import Counter, defaultdict, deque
from dataclasses import asdict, dataclass
from pathlib import Path

import aiohttp


URLS = [f"http://127.0.0.1:{8000 + index}" for index in range(4)]
MODEL_ID = "/home/byh/.cache/modelscope/qwen/Qwen2.5-1.5B-Instruct"
ENTRY_BYTES = 64
BASE_LOAD_BYTES = 96
LOCALITY_ALPHA = {"high": 1.35, "medium": 0.75, "low": 0.05}
POLICIES: list[tuple[str, int | None]] = [
    ("load_only", None),
    ("sketch_coverage_k8", 8),
    ("sketch_coverage_k16", 16),
    ("exact", None),
]


@dataclass(frozen=True)
class TraceRequest:
    request_id: int
    arrival_time: float
    tenant: str
    digest: str
    prefix_token_target: int
    discard: bool


def stable_int(*parts: object) -> int:
    raw = "|".join(map(str, parts)).encode()
    return int.from_bytes(hashlib.blake2b(raw, digest_size=8).digest(), "big")


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


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((p / 100.0) * (len(ordered) - 1))))
    return float(ordered[index])


def bootstrap_ci(values: list[float], seed: int, resamples: int = 2000) -> tuple[float, float, float]:
    if not values:
        return 0.0, 0.0, 0.0
    center = statistics.mean(values)
    if len(values) == 1:
        return center, center, center
    rng = random.Random(seed)
    means = [statistics.mean(rng.choices(values, k=len(values))) for _ in range(resamples)]
    return center, percentile(means, 2.5), percentile(means, 97.5)


def alpha_sampler(alpha: float, n: int) -> list[float]:
    weights = [1.0 / ((index + 1) ** alpha) for index in range(n)]
    total = sum(weights)
    out, current = [], 0.0
    for weight in weights:
        current += weight / total
        out.append(current)
    out[-1] = 1.0
    return out


def make_trace(path: Path, locality: str, rep: int, n_requests: int, warmup: int, prefix_tokens: int, seed: int) -> list[TraceRequest]:
    rng = random.Random(stable_int(seed, "v4-live", locality, rep, n_requests, prefix_tokens))
    cumulative = alpha_sampler(LOCALITY_ALPHA[locality], 64)
    trace: list[TraceRequest] = []
    for request_id in range(n_requests):
        prefix_id = bisect.bisect_left(cumulative, rng.random())
        trace.append(TraceRequest(
            request_id=request_id,
            arrival_time=request_id * 0.03,
            tenant=f"tenant-{prefix_id % 8}",
            digest=f"p{prefix_id:04d}",
            prefix_token_target=prefix_tokens,
            discard=request_id < warmup,
        ))
    ensure_dir(path.parent)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(asdict(trace[0]).keys()))
        writer.writeheader()
        writer.writerows(asdict(request) for request in trace)
    return trace


def prompt_for(request: TraceRequest) -> str:
    """Return policy-independent content.  cache_salt must never enter this text."""
    common = f"Shared reusable context for tenant {request.tenant} and lineage {request.digest}. "
    return common + ("context " * request.prefix_token_target) + "\nReturn a concise deterministic action."


class Dispatcher:
    """Coverage-value admission plus a net-benefit abstention guard.

    The guard only deviates from native load placement when advertised coverage
    exceeds the native instance's known coverage by enough estimated prefill
    time to repay the current queue difference.  This is an implementation of
    the paper's net-benefit condition, not a hit-count bonus.
    """

    def __init__(self, policy: str, k: int | None, capacity: int, j: int, prefill_tokens_per_ms: float, queue_penalty_ms: float, guard_ms: float) -> None:
        self.policy, self.k, self.capacity, self.j = policy, k, capacity, j
        self.prefill_tokens_per_ms = prefill_tokens_per_ms
        self.queue_penalty_ms, self.guard_ms = queue_penalty_ms, guard_ms
        self.entries: list[dict[str, int]] = [dict() for _ in URLS]
        self.lru: list[deque[str]] = [deque() for _ in URLS]
        self.advertised: list[dict[str, int]] = [dict() for _ in URLS]
        self.demand: Counter[str] = Counter()
        self.loads = [0 for _ in URLS]
        self.rr = 0

    def _least_loaded(self, candidates: list[int]) -> int:
        minimum = min(self.loads[index] for index in candidates)
        ties = [index for index in candidates if self.loads[index] == minimum]
        target = ties[self.rr % len(ties)]
        self.rr += 1
        return target

    def choose(self, request: TraceRequest) -> tuple[int, int, int, bool, int, float]:
        self.demand[request.digest] += 1
        native = self._least_loaded(list(range(len(URLS))))
        native_coverage = self.entries[native].get(request.digest, 0)
        if self.policy == "load_only":
            return native, 0, 0, False, native_coverage, 0.0
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
            if estimated_net_ms > self.guard_ms:
                return target, raw, len(evaluated), True, best_coverage, estimated_net_ms
        return native, raw, len(evaluated), False, native_coverage, 0.0

    def observe(self, target: int, request: TraceRequest) -> int:
        prior = self.entries[target].get(request.digest, 0)
        if request.digest in self.entries[target]:
            try:
                self.lru[target].remove(request.digest)
            except ValueError:
                pass
        self.entries[target][request.digest] = request.prefix_token_target
        self.lru[target].append(request.digest)
        while len(self.lru[target]) > self.capacity:
            evicted = self.lru[target].popleft()
            self.entries[target].pop(evicted, None)
        if self.policy == "exact":
            self.advertised[target] = dict(self.entries[target])
        elif self.policy.startswith("sketch"):
            ranked = sorted(
                self.entries[target],
                key=lambda digest: (self.demand[digest] * self.entries[target][digest], self.entries[target][digest], digest),
                reverse=True,
            )
            self.advertised[target] = {digest: self.entries[target][digest] for digest in ranked[: self.k]}
        return prior

    def metadata_bytes(self) -> int:
        if self.policy == "load_only":
            return len(URLS) * BASE_LOAD_BYTES
        return len(URLS) * BASE_LOAD_BYTES + sum(len(values) for values in self.advertised) * ENTRY_BYTES


async def metric_snapshot(session: aiohttp.ClientSession) -> dict[str, float]:
    keys = ("vllm:prefix_cache_hits_total", "vllm:prefix_cache_queries_total", "vllm:prompt_tokens_total")
    totals = {key: 0.0 for key in keys}
    for url in URLS:
        async with session.get(f"{url}/metrics", timeout=aiohttp.ClientTimeout(total=10)) as response:
            response.raise_for_status()
            for line in (await response.text()).splitlines():
                for key in keys:
                    if line.startswith(key + "{") or line.startswith(key + " "):
                        try:
                            totals[key] += float(line.rsplit(" ", 1)[1])
                        except (IndexError, ValueError):
                            pass
    return totals


async def one_request(session: aiohttp.ClientSession, url: str, prompt: str, cache_salt: str, output_tokens: int, max_attempts: int) -> dict:
    payload = {
        "model": MODEL_ID,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": output_tokens,
        "min_tokens": output_tokens,
        "ignore_eos": True,
        "temperature": 0.0,
        # temperature=0 plus fixed min/max length is deterministic for this
        # greedy decode. Do not pass vLLM's optional seed: this server build
        # has an illegal-memory-access failure on long prompts with some seeds.
        "cache_salt": cache_salt,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    errors: list[str] = []
    for attempt in range(1, max_attempts + 1):
        started, first, chunks, parse_errors = time.perf_counter_ns(), 0, 0, 0
        usage: dict = {}
        ok, error = True, ""
        try:
            async with session.post(f"{url}/v1/chat/completions", json=payload, timeout=aiohttp.ClientTimeout(total=180)) as response:
                if response.status != 200:
                    ok, error = False, f"http_{response.status}: {(await response.text())[:120]}"
                else:
                    buffer = ""
                    async for part in response.content.iter_any():
                        buffer += part.decode(errors="ignore")
                        while "\n\n" in buffer:
                            event, buffer = buffer.split("\n\n", 1)
                            data = next((line[6:] for line in event.splitlines() if line.startswith("data: ")), "")
                            if not data or data == "[DONE]":
                                continue
                            try:
                                parsed = json.loads(data)
                            except json.JSONDecodeError:
                                # A malformed transient SSE fragment must not
                                # discard an otherwise valid streamed response.
                                parse_errors += 1
                                continue
                            chunks += 1
                            if not first and parsed.get("choices"):
                                first = time.perf_counter_ns()
                            if parsed.get("usage"):
                                usage = parsed["usage"]
        except Exception as exc:
            ok, error = False, repr(exc)[:180]
        ended = time.perf_counter_ns()
        if ok and usage.get("prompt_tokens") is None:
            ok, error = False, "missing_final_usage"
        if ok:
            details = usage.get("prompt_tokens_details") or {}
            return {
                "ok": True, "error": "", "prior_attempt_errors": "|".join(errors),
                "attempt_count": attempt, "sse_parse_errors": parse_errors,
                "ttft_ms": ((first or ended) - started) / 1e6,
                "latency_ms": (ended - started) / 1e6, "chunks": chunks,
                "input_tokens": usage.get("prompt_tokens"), "output_tokens": usage.get("completion_tokens"),
                "vllm_cached_tokens": details.get("cached_tokens", 0),
            }
        errors.append(error)
        if attempt < max_attempts:
            await asyncio.sleep(0.05 * attempt)
    return {
        "ok": False, "error": errors[-1] if errors else "unknown_request_failure", "prior_attempt_errors": "|".join(errors),
        "attempt_count": max_attempts, "sse_parse_errors": 0, "ttft_ms": 0.0, "latency_ms": 0.0,
        "chunks": 0, "input_tokens": None, "output_tokens": None, "vllm_cached_tokens": 0,
    }


async def run_cell(trace: list[TraceRequest], policy: str, k: int | None, cache_salt: str, args: argparse.Namespace) -> tuple[dict, list[dict]]:
    dispatcher = Dispatcher(policy, k, args.cache_capacity, args.j, args.prefill_tokens_per_ms, args.queue_penalty_ms, args.guard_ms)
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
                for request, target, _, _, _, _, _ in decisions
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


def paired_rows(cells: list[dict]) -> list[dict]:
    by_cell = {(row["locality"], row["rep"], row["policy"]): row for row in cells}
    out: list[dict] = []
    for locality, rep, _ in sorted((row["locality"], row["rep"], row["policy"]) for row in cells if row["policy"] == "load_only"):
        baseline = by_cell[(locality, rep, "load_only")]
        for treatment in ("sketch_coverage_k8", "sketch_coverage_k16", "exact"):
            current = by_cell[(locality, rep, treatment)]
            row = {"experiment_id": f"20260716_fixed_t4_{locality}_{treatment}_rep{rep}", "locality": locality, "rep": rep, "baseline_policy": "load_only", "treatment_policy": treatment, "workload_trace_hash": baseline["workload_trace_hash"], "status": "Current"}
            for metric in ("ttft_p50_ms", "ttft_p95_ms", "vllm_cached_tokens_total", "vllm_cached_token_rate", "metric_prefix_cache_hits_delta", "mean_selected_coverage_tokens"):
                row[f"delta_{metric}"] = float(current[metric]) - float(baseline[metric])
            out.append(row)
    return out


def summary_rows(pairs: list[dict], seed: int) -> list[dict]:
    out: list[dict] = []
    for locality in sorted({row["locality"] for row in pairs}):
        for treatment in ("sketch_coverage_k8", "sketch_coverage_k16", "exact"):
            group = [row for row in pairs if row["locality"] == locality and row["treatment_policy"] == treatment]
            base = {"experiment": "fixed_prompt_t4_replay_v4", "evidence_type": "live_t4_vllm", "locality": locality, "baseline_policy": "load_only", "treatment_policy": treatment, "n_reps": len(group), "status": "Current"}
            for metric in ("delta_ttft_p50_ms", "delta_ttft_p95_ms", "delta_vllm_cached_tokens_total", "delta_vllm_cached_token_rate", "delta_metric_prefix_cache_hits_delta", "delta_mean_selected_coverage_tokens"):
                values = [float(row[metric]) for row in group]
                mean, low, high = bootstrap_ci(values, stable_int(seed, locality, treatment, metric))
                base[f"{metric}_mean"] = mean
                base[f"{metric}_ci95_low"] = low
                base[f"{metric}_ci95_high"] = high
                base[f"{metric}_median"] = statistics.median(values)
                base[f"{metric}_iqr"] = percentile(values, 75) - percentile(values, 25)
                base[f"{metric}_fraction_below_zero"] = sum(value < 0 for value in values) / len(values)
            out.append(base)
    return out


def validate_records(raw: list[dict], output_tokens: int) -> list[dict]:
    groups: dict[tuple[str, int, int], list[dict]] = defaultdict(list)
    for row in raw:
        groups[(row["locality"], int(row["rep"]), int(row["request_id"]))].append(row)
    mismatched_inputs = sum(len({(item["prompt_sha256"], item["input_tokens"]) for item in rows}) != 1 for rows in groups.values())
    mismatched_outputs = sum(len({item["output_tokens"] for item in rows}) != 1 or next(iter({item["output_tokens"] for item in rows})) != output_tokens for rows in groups.values())
    missing_usage = sum(item["input_tokens"] is None or item["output_tokens"] is None for item in raw)
    retries = sum(int(item["attempt_count"]) > 1 for item in raw)
    return [
        {"check_name": "same logical request has byte-identical prompt and input token count across policies", "status": "PASS" if mismatched_inputs == 0 else "FAIL", "offending_rows": mismatched_inputs, "suggested_fix": "remove all policy data from semantic prompt"},
        {"check_name": "same logical request has fixed output token count across policies", "status": "PASS" if mismatched_outputs == 0 else "FAIL", "offending_rows": mismatched_outputs, "suggested_fix": "keep min_tokens=max_tokens and ignore_eos"},
        {"check_name": "vLLM usage telemetry present for every request", "status": "PASS" if missing_usage == 0 else "FAIL", "offending_rows": missing_usage, "suggested_fix": "restart vLLM with --enable-prompt-tokens-details"},
        {"check_name": "all output token counts equal requested fixed length", "status": "PASS" if mismatched_outputs == 0 else "FAIL", "offending_rows": mismatched_outputs, "suggested_fix": "inspect server generation settings"},
        {"check_name": "transient live retries recorded in raw data", "status": "PASS", "offending_rows": retries, "suggested_fix": "inspect prior_attempt_errors if retries are nonzero"},
    ]


async def check_endpoints() -> None:
    async with aiohttp.ClientSession() as session:
        for url in URLS:
            async with session.get(f"{url}/v1/models", timeout=aiohttp.ClientTimeout(total=10)) as response:
                if response.status != 200:
                    raise RuntimeError(f"endpoint unavailable: {url} -> {response.status}")


async def run(args: argparse.Namespace) -> tuple[list[dict], list[dict], list[dict]]:
    await check_endpoints()
    root = Path(args.out_dir)
    cells: list[dict] = []
    raw: list[dict] = []
    for locality in (value for value in args.localities.split(",") if value):
        if locality not in LOCALITY_ALPHA:
            raise ValueError(f"unsupported locality: {locality}")
        for rep in range(args.repetitions):
            trace_path = root / "traces" / f"fixed_trace_{locality}_rep{rep}.csv"
            trace = make_trace(trace_path, locality, rep, args.n_requests, args.warmup, args.prefix_tokens, args.seed)
            trace_hash = sha256_file(trace_path)
            order = list(POLICIES)
            random.Random(stable_int(args.seed, "order", locality, rep)).shuffle(order)
            for order_index, (policy, k) in enumerate(order):
                cache_salt = f"b02-v4:{locality}:rep{rep}:{policy}"
                metrics, records = await run_cell(trace, policy, k, cache_salt, args)
                row = {
                    "experiment_id": f"20260716_fixed_t4_{locality}_{policy}_rep{rep}",
                    "experiment": "fixed_prompt_t4_replay_v4",
                    "evidence_type": "live_t4_vllm",
                    "code_commit": git_commit(),
                    "model": MODEL_ID,
                    "hardware": "4x Tesla T4; Qwen2.5-1.5B; one vLLM instance/GPU",
                    "locality": locality, "policy": policy, "K": "inf" if k is None else k, "J": args.j,
                    "admission": "coverage_value" if policy.startswith("sketch") else "n/a",
                    "routing_guard": "net_prefill_benefit", "guard_ms": args.guard_ms,
                    "prefill_tokens_per_ms": args.prefill_tokens_per_ms, "queue_penalty_ms": args.queue_penalty_ms,
                    "rep": rep, "repetitions": args.repetitions, "seed": args.seed, "workload_trace_hash": trace_hash,
                    "request_count_total": args.n_requests, "warmup_request_count": args.warmup, "cache_capacity": args.cache_capacity,
                    "concurrency": args.concurrency, "prefix_token_target": args.prefix_tokens, "fixed_output_tokens": args.output_tokens,
                    "generation_mode": "greedy_temperature0_min_tokens_eq_max_tokens_ignore_eos", "max_request_attempts": args.max_request_attempts,
                    "policy_order": order_index, "policy_order_sequence": ",".join(item[0] for item in order),
                    "vllm_cache_salt": cache_salt, "semantic_prompt_contains_policy": False,
                    "metric_scope": "TTFT and cached-token telemetry are live vLLM measurements; total latency is comparable because generation length is fixed.",
                    "status": "Current", **metrics,
                }
                cells.append(row)
                raw.extend({"experiment_id": row["experiment_id"], "locality": locality, "rep": rep, "policy": policy, **record} for record in records)
                print(json.dumps({"completed": row["experiment_id"], "ttft_p95_ms": row["ttft_p95_ms"], "cached_tokens": row["vllm_cached_tokens_total"], "errors": row["request_error_rate"]}), flush=True)
                await asyncio.sleep(args.cooldown_s)
    return cells, paired_rows(cells), raw


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", default="/home/byh/B02/supplemental_20260715/fixed_prompt_t4_v4")
    parser.add_argument("--seed", type=int, default=20260716)
    parser.add_argument("--localities", default="high")
    parser.add_argument("--n-requests", type=int, default=96)
    parser.add_argument("--warmup", type=int, default=32)
    parser.add_argument("--repetitions", type=int, default=20)
    parser.add_argument("--cache-capacity", type=int, default=128)
    parser.add_argument("--j", type=int, default=4)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--prefix-tokens", type=int, default=2048)
    parser.add_argument("--output-tokens", type=int, default=16)
    parser.add_argument("--prefill-tokens-per-ms", type=float, default=50.0)
    parser.add_argument("--queue-penalty-ms", type=float, default=2.0)
    parser.add_argument("--guard-ms", type=float, default=0.5)
    parser.add_argument("--max-request-attempts", type=int, default=3)
    parser.add_argument("--cooldown-s", type=float, default=0.2)
    args = parser.parse_args()
    if args.n_requests <= args.warmup or args.repetitions < 2 or args.output_tokens < 1 or args.max_request_attempts < 1:
        raise ValueError("need measured requests, at least two repetitions, and positive output tokens")
    started = time.time()
    cells, pairs, raw = asyncio.run(run(args))
    if any(float(row["request_error_rate"]) > 0 for row in cells):
        raise RuntimeError("live request error observed; do not use this run")
    checks = validate_records(raw, args.output_tokens)
    if any(row["status"] != "PASS" for row in checks):
        raise RuntimeError(f"live comparability checks failed: {checks}")
    root = Path(args.out_dir)
    write_csv(root / "fixed_prompt_cells.csv", cells)
    write_csv(root / "fixed_prompt_pairs.csv", pairs)
    write_csv(root / "fixed_prompt_summary.csv", summary_rows(pairs, args.seed))
    write_csv(root / "fixed_prompt_sanity_checks.csv", checks)
    (root / "fixed_prompt_raw.json").write_text(json.dumps(raw))
    metadata = {"started_at_unix": started, "finished_at_unix": time.time(), "duration_s": time.time() - started, "arguments": vars(args), "cells": len(cells), "pairs": len(pairs), "raw_requests": len(raw)}
    (root / "run_metadata.json").write_text(json.dumps(metadata, indent=2))
    (root / "README.md").write_text(
        "# Fixed-prompt paired T4 replay v4\\n\\n"
        "Every logical request has the same prompt SHA-256, input token count, and fixed output token count across policies. "
        "Policy-specific cache isolation uses vLLM cache_salt, which is not part of prompt text. "
        "Use per-request cached-token telemetry and paired TTFT/latency distributions.\\n"
    )
    print(json.dumps(metadata, indent=2))


if __name__ == "__main__":
    main()
