#!/usr/bin/env python3
"""Hybrid live shared-link experiment v2 on four T4 vLLM workers.

v2 changes over v1 (run_live_shared_link_v1.py), driven by v1 findings:

1. DE-CONFOUNDED ORDERING: v1 ran cells in a fixed order within each rep
   while a global time drift inflated TTFT over the run, aliasing policy
   order with drift.  v2 shuffles the per-rep cell order with a per-rep
   seed and records the order in every row and in the run metadata.  The
   paired trace per logical request across cells within a rep is unchanged.

2. COVERAGE-GROWTH WORKLOAD (agentic lineage): v1's fixed 2048-token
   coverage per prefix meant merge-superseded never fired.  v2 uses
   lineages: each lineage is a chain of steps where step k's prompt is step
   k-1's prompt plus a ~512-token extension (coverage target grows
   2048 -> 2560 -> 3072).  Repeated completions of the same lineage produce
   repeated upserts for the same digest with growing coverage, so
   merge-superseded and freshness actually matter.  Step prompts are literal
   string prefixes of later steps so physical prefix caching chains.

3. DEEPER CONGESTION: v1's tightest tier (util ~0.95) only reached ad queue
   p95 ~0.9 s, far below tau=30 s.  v2 tiers target util ~0.5 / 1.0 / 1.75
   of the measured raw ad rate (remeasured under the lineage workload).
   Over-capacity tiers would grow the backlog without bound, so exact_fifo
   (only) drops the oldest queued message beyond a max backlog of 200
   messages; backlog drops are recorded as a metric.  agg policies
   merge/drop and should rarely approach the cap.  Cell-end link drain is
   capped at 60 s; undeliverable remainder is counted as drain overflow.

Everything else is inherited from v1: real vLLM inference, real TTFT,
physical vllm_cached_tokens as the ONLY reuse ground truth, per-instance
LRU shadow model (capacity from server logs) for tombstones only, cache_salt
cell isolation, fixed-length outputs, V4-style integrity checks.
"""
from __future__ import annotations

import argparse
import asyncio
import bisect
import csv
import hashlib
import itertools
import json
import math
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
MSG_BYTES = 64
STALE_COVERAGE_THRESHOLD = 512
LINK_POLICIES = ["exact_fifo", "local_topk", "agg_static", "agg_adaptive"]
TAU_S = 30.0
UTILITY_GATE_S = 0.050
# Utility drop fires when exp(-(age+Dq)/TAU_S)*coverage <= LAMBDA*MSG_BYTES,
# i.e. (with coverage>=2048) once age+Dq exceeds ~4 s: only under congestion.
UTILITY_LAMBDA = 28.0
BPS_TIERS = ("low", "med", "high")  # target link utilization ~0.5 / 1.0 / 1.75
MAX_BACKLOG = 200  # exact_fifo only: drop-oldest beyond this queue depth
DRAIN_TIMEOUT_S = 60.0
BASE_WORDS = 2048  # lineage step-0 prompt length in words (~2094 tokens)
STEP_WORDS = 512   # per-step extension in words (~525 tokens)


@dataclass(frozen=True)
class TraceRequest:
    request_id: int
    phase: int
    lineage_id: int
    step: int
    tenant: str
    digest: str
    coverage_tokens: int
    discard: bool


@dataclass(eq=False)
class Message:
    kind: str  # "upsert" | "tombstone"
    instance: int
    digest: str
    coverage: int
    seq: int
    enqueued_at: float
    priority: bool = False


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


def zipf_cdf(alpha: float, n: int) -> list[float]:
    weights = [1.0 / ((rank + 1) ** alpha) for rank in range(n)]
    total = sum(weights)
    running, out = 0.0, []
    for weight in weights:
        running += weight / total
        out.append(running)
    out[-1] = 1.0
    return out


def make_trace(path: Path, rep: int, args: argparse.Namespace) -> list[TraceRequest]:
    """Zipf over lineages; each hit advances the lineage's step (mod steps).

    Step k's advertised coverage target is base + k*step_tokens.  At 50% of
    requests the active lineage pool rotates to a disjoint slice.
    """
    rng = random.Random(stable_int(args.seed, "shared-link-v2", rep, args.n_requests, args.pool_size, args.alpha))
    cdf = zipf_cdf(args.alpha, args.pool_size)
    shift_at = args.n_requests // 2
    next_step: dict[int, int] = {}
    trace: list[TraceRequest] = []
    for request_id in range(args.n_requests):
        phase = 0 if request_id < shift_at else 1
        slot = bisect.bisect_left(cdf, rng.random())
        lineage_id = phase * args.pool_size + slot
        step = next_step.get(lineage_id, 0)
        next_step[lineage_id] = (step + 1) % args.steps
        trace.append(TraceRequest(
            request_id=request_id,
            phase=phase,
            lineage_id=lineage_id,
            step=step,
            tenant=f"tenant-{lineage_id % 8}",
            digest=f"L{lineage_id:04d}",
            coverage_tokens=BASE_WORDS + STEP_WORDS * step,
            discard=request_id < args.warmup,
        ))
    ensure_dir(path.parent)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(asdict(trace[0]).keys()))
        writer.writeheader()
        writer.writerows(asdict(request) for request in trace)
    return trace


def prompt_for(request: TraceRequest) -> str:
    """Policy-independent lineage prompt.  Step k's prompt is a literal string
    prefix of step k+1's prompt, so physical prefix caching chains along the
    lineage.  cache_salt must never enter this text."""
    prompt = f"Shared reusable context for tenant {request.tenant} and lineage {request.digest}. " + ("context " * BASE_WORDS)
    for k in range(1, request.step + 1):
        prompt += f"Extension {k} for lineage {request.digest}. " + ("detail " * STEP_WORDS)
    return prompt


class Dispatcher:
    """Coverage-based candidate selection with the V4 net-benefit guard.

    The affinity index is updated ONLY by link deliveries (or synchronously
    for the ideal policy).  Staleness therefore shows up here exactly as it
    would in a real deployment.
    """

    def __init__(self, j: int, prefill_tokens_per_ms: float, queue_penalty_ms: float, guard_ms: float) -> None:
        self.j = j
        self.prefill_tokens_per_ms = prefill_tokens_per_ms
        self.queue_penalty_ms = queue_penalty_ms
        self.guard_ms = guard_ms
        self.index: list[dict[str, int]] = [dict() for _ in URLS]
        self.loads = [0 for _ in URLS]
        self.rr = 0

    def _least_loaded(self, candidates: list[int]) -> int:
        minimum = min(self.loads[index] for index in candidates)
        ties = [index for index in candidates if self.loads[index] == minimum]
        target = ties[self.rr % len(ties)]
        self.rr += 1
        return target

    def choose(self, request: TraceRequest) -> tuple[int, int, int, bool, int, float]:
        native = self._least_loaded(list(range(len(URLS))))
        native_coverage = self.index[native].get(request.digest, 0)
        candidates = [(index, self.index[index].get(request.digest, 0)) for index in range(len(URLS))]
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

    def apply(self, msg: Message) -> None:
        """Link delivery callback: the only path into the index for link policies."""
        if msg.kind == "upsert":
            self.index[msg.instance][msg.digest] = msg.coverage
        else:
            self.index[msg.instance].pop(msg.digest, None)


class ShadowCache:
    """Per-instance LRU shadow of vLLM's block pool.

    APPROXIMATION: capacity comes from the server log's "GPU KV cache size"
    in tokens, each lineage occupies its longest requested prompt (step
    prompts are prefixes of later steps), and each cell's shadow starts empty
    while the physical pool carries LRU residue from earlier cells.  The
    shadow is used ONLY to decide when to emit tombstones; physical
    vllm_cached_tokens remains the reuse ground truth.
    """

    def __init__(self, capacity_tokens: int) -> None:
        self.capacity = capacity_tokens
        self.entries: dict[str, int] = {}  # insertion order == LRU order
        self.total = 0

    def insert(self, digest: str, tokens: int) -> list[str]:
        if digest in self.entries:
            old = self.entries.pop(digest)
            self.entries[digest] = max(old, tokens)  # cache keeps the longest prefix
            self.total += self.entries[digest] - old
            return []
        self.entries[digest] = tokens
        self.total += tokens
        evicted: list[str] = []
        while self.total > self.capacity and len(self.entries) > 1:
            victim = next(iter(self.entries))
            if victim == digest:
                break
            self.total -= self.entries.pop(victim)
            evicted.append(victim)
        return evicted


class SharedLink:
    """In-process byte-rate FIFO simulating the shared control link.

    Service time per 64-byte message is MSG_BYTES / bytes_per_s.  For the
    agg_* policies tombstones use a priority lane served first
    (non-preemptive), a newer upsert cancels a queued unsent older upsert
    for the same (instance, digest) (merge-superseded), and at most two
    instances may hold delivered-or-in-flight upserts per digest.

    Over-capacity backpressure: exact_fifo (only) drops the OLDEST queued
    message once the backlog exceeds MAX_BACKLOG, counted in
    dropped["backlog_cap"].  Cell-end drain is capped; any undeliverable
    remainder is counted in dropped["drain_overflow"].
    """

    def __init__(self, bytes_per_s: float, policy: str, deliver) -> None:
        self.bps = bytes_per_s
        self.policy = policy
        self.deliver = deliver
        self.regular: deque[Message] = deque()
        self.priority: deque[Message] = deque()
        self.queue_delays: dict[str, list[float]] = {"upsert": [], "tombstone": []}
        self.msgs_sent: Counter[str] = Counter()
        self.dropped: Counter[str] = Counter()
        self.bytes_sent = 0
        self.ewma_dq = 0.0
        self.max_depth = 0
        self.replicas: dict[str, set[int]] = defaultdict(set)
        self.max_backlog = MAX_BACKLOG if policy == "exact_fifo" else None
        self._event = asyncio.Event()
        self._closed = False
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._serve())

    def enqueue(self, msg: Message) -> bool:
        """Returns True if the message entered the link, False if filtered."""
        agg = self.policy in ("agg_static", "agg_adaptive")
        if msg.kind == "upsert" and agg:
            for queued in list(self.regular):
                if queued.kind == "upsert" and queued.instance == msg.instance and queued.digest == msg.digest:
                    self.regular.remove(queued)
                    self.dropped["superseded"] += 1
            if self.policy == "agg_adaptive" and self.ewma_dq > UTILITY_GATE_S:
                age = time.perf_counter() - msg.enqueued_at
                utility = math.exp(-(age + self.ewma_dq) / TAU_S) * msg.coverage - UTILITY_LAMBDA * MSG_BYTES
                if utility <= 0:
                    self.dropped["low_utility"] += 1
                    return False
            replicas = self.replicas[msg.digest]
            if msg.instance not in replicas and len(replicas) >= 2:
                self.dropped["replica_cap"] += 1
                return False
            replicas.add(msg.instance)
        if msg.kind == "tombstone" and agg:
            for queued in list(self.regular):
                if queued.kind == "upsert" and queued.instance == msg.instance and queued.digest == msg.digest:
                    self.regular.remove(queued)
                    self.dropped["superseded"] += 1
        (self.priority if msg.priority else self.regular).append(msg)
        if self.max_backlog is not None:
            while len(self.regular) > self.max_backlog:
                self.regular.popleft()  # drop-oldest backpressure
                self.dropped["backlog_cap"] += 1
        self.max_depth = max(self.max_depth, len(self.regular) + len(self.priority))
        self._event.set()
        return True

    async def _serve(self) -> None:
        service_s = MSG_BYTES / self.bps
        while True:
            if self.priority:
                msg = self.priority.popleft()
            elif self.regular:
                msg = self.regular.popleft()
            else:
                if self._closed:
                    return
                self._event.clear()
                if not self.priority and not self.regular and not self._closed:
                    await self._event.wait()
                continue
            await asyncio.sleep(service_s)
            delay = time.perf_counter() - msg.enqueued_at
            self.queue_delays[msg.kind].append(delay)
            self.ewma_dq = 0.8 * self.ewma_dq + 0.2 * delay
            self.bytes_sent += MSG_BYTES
            self.msgs_sent[msg.kind] += 1
            if msg.kind == "tombstone":
                self.replicas[msg.digest].discard(msg.instance)
            self.deliver(msg)

    async def drain(self, timeout_s: float = DRAIN_TIMEOUT_S) -> None:
        self._closed = True
        self._event.set()
        if self._task is None:
            return
        try:
            await asyncio.wait_for(self._task, timeout_s)
        except asyncio.TimeoutError:
            # Over-capacity tier: the remaining backlog is undeliverable
            # within the drain budget; count it and move on.
            self.dropped["drain_overflow"] += len(self.regular) + len(self.priority)
            self.regular.clear()
            self.priority.clear()
            self._task.cancel()


async def check_endpoints() -> None:
    async with aiohttp.ClientSession() as session:
        for url in URLS:
            async with session.get(f"{url}/v1/models", timeout=aiohttp.ClientTimeout(total=10)) as response:
                if response.status != 200:
                    raise RuntimeError(f"endpoint unavailable: {url} -> {response.status}")


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


async def run_cell(trace: list[TraceRequest], policy: str, bps_tier: str, bps: float | None, cache_salt: str, args: argparse.Namespace) -> tuple[dict, list[dict]]:
    dispatcher = Dispatcher(args.j, args.prefill_tokens_per_ms, args.queue_penalty_ms, args.guard_ms)
    shadows = [ShadowCache(args.kv_cache_tokens) for _ in URLS]
    advertised: list[set[str]] = [set() for _ in URLS]
    seq = itertools.count()
    upserts_generated = 0
    link: SharedLink | None = None
    if policy != "ideal":
        link = SharedLink(bps, policy, dispatcher.apply)
        link.start()
    records: list[dict] = []
    started = time.perf_counter()
    async with aiohttp.ClientSession() as session:
        for offset in range(0, len(trace), args.concurrency):
            wave = trace[offset:offset + args.concurrency]
            decisions = []
            for request in wave:
                target, raw_fanout, evaluated, affinity, coverage, expected_net = dispatcher.choose(request)
                dispatcher.loads[target] += 1
                decisions.append((request, target, raw_fanout, evaluated, affinity, coverage, expected_net))
            responses = await asyncio.gather(*[
                one_request(session, URLS[target], prompt_for(request), cache_salt, args.output_tokens, args.max_request_attempts)
                for request, target, _, _, _, _, _ in decisions
            ])
            for (request, target, raw_fanout, evaluated, affinity, coverage, expected_net), response in zip(decisions, responses):
                dispatcher.loads[target] -= 1
                now = time.perf_counter()
                evicted = shadows[target].insert(request.digest, request.coverage_tokens)
                if policy == "ideal":
                    dispatcher.index[target][request.digest] = request.coverage_tokens
                    for victim in evicted:
                        dispatcher.index[target].pop(victim, None)
                    upserts_generated += 1
                else:
                    send_ad = True
                    if policy == "local_topk":
                        topk = set(list(shadows[target].entries)[-args.topk:])
                        send_ad = request.digest in topk
                        # Tombstone anything previously advertised that fell out of top-K.
                        for digest in list(advertised[target]):
                            if digest not in topk:
                                link.enqueue(Message("tombstone", target, digest, 0, next(seq), now, priority=False))
                                advertised[target].discard(digest)
                    if send_ad:
                        msg = Message("upsert", target, request.digest, request.coverage_tokens, next(seq), now)
                        if link.enqueue(msg):
                            advertised[target].add(request.digest)
                            upserts_generated += 1
                    for victim in evicted:
                        if victim in advertised[target]:
                            priority = policy in ("agg_static", "agg_adaptive")
                            link.enqueue(Message("tombstone", target, victim, 0, next(seq), now, priority=priority))
                            advertised[target].discard(victim)
                if request.discard:
                    continue
                physical_cached = float(response["vllm_cached_tokens"] or 0)
                records.append({
                    "request_id": request.request_id,
                    "phase": request.phase,
                    "lineage_id": request.lineage_id,
                    "step": request.step,
                    "digest": request.digest,
                    "selected_instance": target,
                    "candidate_hit": affinity,
                    "expected_coverage_tokens": coverage,
                    "expected_net_prefill_ms": expected_net,
                    "raw_candidate_fanout": raw_fanout,
                    "evaluated_candidate_fanout": evaluated,
                    "stale_fallback": bool(coverage >= STALE_COVERAGE_THRESHOLD and response["ok"] and physical_cached < STALE_COVERAGE_THRESHOLD),
                    "coverage_shortfall_tokens": max(0.0, float(coverage) - physical_cached) if response["ok"] else None,
                    "prompt_sha256": hashlib.sha256(prompt_for(request).encode()).hexdigest(),
                    **response,
                })
    active_s = time.perf_counter() - started
    if link is not None:
        await link.drain()
    success = [row for row in records if row["ok"]]
    ttfts = [float(row["ttft_ms"]) for row in success]
    latencies = [float(row["latency_ms"]) for row in success]
    shortfalls = [float(row["coverage_shortfall_tokens"]) for row in success]
    n = len(records)
    metrics = {
        "request_count": n,
        "cell_active_s": active_s,
        "upserts_generated": upserts_generated,
        "upserts_per_s": upserts_generated / active_s if active_s > 0 else 0.0,
        "raw_ad_bytes_per_s": (upserts_generated * MSG_BYTES) / active_s if active_s > 0 else 0.0,
        "affinity_selection_rate": sum(bool(row["candidate_hit"]) for row in records) / n if n else 0.0,
        "vllm_cached_token_rate": sum(float(row["vllm_cached_tokens"] or 0) > 0 for row in records) / n if n else 0.0,
        "vllm_cached_tokens_total": sum(float(row["vllm_cached_tokens"] or 0) for row in records),
        "stale_fallback_count": sum(bool(row["stale_fallback"]) for row in records),
        "stale_fallback_rate": sum(bool(row["stale_fallback"]) for row in records) / n if n else 0.0,
        "mean_coverage_shortfall_tokens": statistics.mean(shortfalls) if shortfalls else 0.0,
        "ttft_mean_ms": statistics.mean(ttfts) if ttfts else 0.0,
        "ttft_p50_ms": percentile(ttfts, 50),
        "ttft_p95_ms": percentile(ttfts, 95),
        "latency_p50_ms": percentile(latencies, 50),
        "latency_p95_ms": percentile(latencies, 95),
        "retried_request_count": sum(int(row["attempt_count"]) > 1 for row in records),
        "request_error_rate": 1.0 - len(success) / n if n else 1.0,
    }
    if link is not None:
        metrics.update({
            "link_bps": link.bps,
            "link_bytes_sent": link.bytes_sent,
            "link_upserts_sent": link.msgs_sent["upsert"],
            "link_tombstones_sent": link.msgs_sent["tombstone"],
            "link_dropped_superseded": link.dropped["superseded"],
            "link_dropped_replica_cap": link.dropped["replica_cap"],
            "link_dropped_low_utility": link.dropped["low_utility"],
            "backlog_drop_count": link.dropped["backlog_cap"] + link.dropped["drain_overflow"],
            "link_max_backlog_depth": link.max_depth,
            "ad_queue_delay_mean_s": statistics.mean(link.queue_delays["upsert"]) if link.queue_delays["upsert"] else 0.0,
            "ad_queue_delay_p50_s": percentile(link.queue_delays["upsert"], 50),
            "ad_queue_delay_p95_s": percentile(link.queue_delays["upsert"], 95),
            "tombstone_delay_p95_s": percentile(link.queue_delays["tombstone"], 95),
            "ewma_dq_final_s": link.ewma_dq,
        })
    else:
        metrics.update({
            "link_bps": 0.0, "link_bytes_sent": 0, "link_upserts_sent": 0, "link_tombstones_sent": 0,
            "link_dropped_superseded": 0, "link_dropped_replica_cap": 0, "link_dropped_low_utility": 0,
            "backlog_drop_count": 0, "link_max_backlog_depth": 0,
            "ad_queue_delay_mean_s": 0.0, "ad_queue_delay_p50_s": 0.0, "ad_queue_delay_p95_s": 0.0,
            "tombstone_delay_p95_s": 0.0, "ewma_dq_final_s": 0.0,
        })
    return metrics, records


def paired_rows(cells: list[dict]) -> list[dict]:
    by_cell = {(row["rep"], row["cell_id"]): row for row in cells}
    out: list[dict] = []
    for rep in sorted({row["rep"] for row in cells}):
        baseline = by_cell.get((rep, "ideal"))
        if baseline is None:
            continue
        for row in cells:
            if row["rep"] != rep or row["cell_id"] == "ideal":
                continue
            ideal_total = float(baseline["vllm_cached_tokens_total"])
            out.append({
                "rep": rep,
                "cell_id": row["cell_id"],
                "policy": row["policy"],
                "bps_tier": row["bps_tier"],
                "saved_prefill_retention_vs_ideal": (float(row["vllm_cached_tokens_total"]) / ideal_total) if ideal_total > 0 else 0.0,
                "delta_ttft_mean_ms_vs_ideal": float(row["ttft_mean_ms"]) - float(baseline["ttft_mean_ms"]),
                "delta_ttft_p95_ms_vs_ideal": float(row["ttft_p95_ms"]) - float(baseline["ttft_p95_ms"]),
                "delta_stale_fallback_rate_vs_ideal": float(row["stale_fallback_rate"]) - float(baseline["stale_fallback_rate"]),
            })
    return out


def validate_records(raw: list[dict], output_tokens: int) -> list[dict]:
    groups: dict[tuple[int, int], list[dict]] = defaultdict(list)
    for row in raw:
        groups[(int(row["rep"]), int(row["request_id"]))].append(row)
    mismatched_inputs = sum(len({(item["prompt_sha256"], item["input_tokens"]) for item in rows}) != 1 for rows in groups.values())
    mismatched_outputs = sum(len({item["output_tokens"] for item in rows}) != 1 or next(iter({item["output_tokens"] for item in rows})) != output_tokens for rows in groups.values())
    missing_usage = sum(item["input_tokens"] is None or item["output_tokens"] is None for item in raw)
    retries = sum(int(item["attempt_count"]) > 1 for item in raw)
    return [
        {"check_name": "same logical request has byte-identical prompt and input token count across cells", "status": "PASS" if mismatched_inputs == 0 else "FAIL", "offending_rows": mismatched_inputs, "suggested_fix": "remove all policy data from semantic prompt"},
        {"check_name": "same logical request has fixed output token count across cells", "status": "PASS" if mismatched_outputs == 0 else "FAIL", "offending_rows": mismatched_outputs, "suggested_fix": "keep min_tokens=max_tokens and ignore_eos"},
        {"check_name": "vLLM usage telemetry present for every request", "status": "PASS" if missing_usage == 0 else "FAIL", "offending_rows": missing_usage, "suggested_fix": "restart vLLM with --enable-prompt-tokens-details"},
        {"check_name": "transient live retries recorded in raw data", "status": "PASS", "offending_rows": retries, "suggested_fix": "inspect prior_attempt_errors if retries are nonzero"},
    ]


async def run(args: argparse.Namespace) -> tuple[list[dict], list[dict], list[dict], dict]:
    await check_endpoints()
    root = Path(args.out_dir)
    bps_map = dict(item.split("=", 1) for item in args.bps.split(","))
    bps_map = {tier: float(bps_map[tier]) for tier in BPS_TIERS}
    cell_kinds: list[tuple[str, str, float | None]] = [("ideal", "none", None)]
    if not args.ideal_only:
        cell_kinds += [(policy, tier, bps_map[tier]) for policy in LINK_POLICIES for tier in BPS_TIERS]
    cells: list[dict] = []
    raw: list[dict] = []
    order_by_rep: dict[int, list[str]] = {}
    for rep in range(args.repetitions):
        trace_path = root / "traces" / f"shared_link_v2_trace_rep{rep}.csv"
        trace = make_trace(trace_path, rep, args)
        trace_hash = sha256_file(trace_path)
        # v2: rotate the per-rep cell order with a per-rep seed so policy
        # order is not aliased with global time drift (v1 confound).
        order = list(cell_kinds)
        random.Random(stable_int(args.seed, "cell-order", rep)).shuffle(order)
        order_by_rep[rep] = ["ideal" if policy == "ideal" else f"{policy}@{tier}" for policy, tier, _ in order]
        for order_index, (policy, tier, bps) in enumerate(order):
            cell_id = "ideal" if policy == "ideal" else f"{policy}@{tier}"
            cache_salt = f"sharedlink-v2:{args.tag}:{cell_id}:rep{rep}"
            metrics, records = await run_cell(trace, policy, tier, bps, cache_salt, args)
            row = {
                "experiment_id": f"20260723_shared_link_live_v2_{cell_id}_rep{rep}",
                "experiment": "shared_link_live_v2",
                "evidence_type": "hybrid_live_vllm_simulated_link",
                "code_commit": git_commit(),
                "model": MODEL_ID,
                "hardware": "4x Tesla T4; Qwen2.5-1.5B-Instruct; one vLLM instance/GPU",
                "cell_id": cell_id, "policy": policy, "bps_tier": tier,
                "link_msg_bytes": MSG_BYTES,
                "rep": rep, "repetitions": args.repetitions, "seed": args.seed,
                "workload_trace_hash": trace_hash,
                "zipf_alpha": args.alpha, "pool_size": args.pool_size,
                "lineage_steps": args.steps,
                "base_prefix_tokens": BASE_WORDS,
                "step_tokens": STEP_WORDS,
                "distinct_lineages_total": 2 * args.pool_size,
                "phase_shift_at_request": args.n_requests // 2,
                "n_requests": args.n_requests, "warmup_request_count": args.warmup,
                "concurrency": args.concurrency,
                "fixed_output_tokens": args.output_tokens,
                "kv_cache_tokens_per_instance_shadow": args.kv_cache_tokens,
                "utility_tau_s": TAU_S, "utility_lambda": UTILITY_LAMBDA, "utility_gate_s": UTILITY_GATE_S,
                "max_backlog_exact_fifo": MAX_BACKLOG, "drain_timeout_s": DRAIN_TIMEOUT_S,
                "topk": args.topk, "j": args.j,
                "guard_ms": args.guard_ms, "prefill_tokens_per_ms": args.prefill_tokens_per_ms,
                "queue_penalty_ms": args.queue_penalty_ms,
                "cell_order_index": order_index,
                "cell_order_sequence": ",".join(order_by_rep[rep]),
                "generation_mode": "greedy_temperature0_min_tokens_eq_max_tokens_ignore_eos",
                "vllm_cache_salt": cache_salt, "semantic_prompt_contains_policy": False,
                "metric_scope": "TTFT and cached-token telemetry are live vLLM measurements; only the shared control link is simulated.",
                "status": "Current", **metrics,
            }
            cells.append(row)
            raw.extend({"experiment_id": row["experiment_id"], "rep": rep, "cell_id": cell_id, "policy": policy, **record} for record in records)
            print(json.dumps({
                "completed": row["experiment_id"], "order": order_index,
                "ttft_mean_ms": round(row["ttft_mean_ms"], 1),
                "ttft_p95_ms": round(row["ttft_p95_ms"], 1), "cached_tokens": row["vllm_cached_tokens_total"],
                "stale_fallback_rate": round(row["stale_fallback_rate"], 3),
                "ad_q_mean_s": round(row["ad_queue_delay_mean_s"], 3),
                "ad_q_p95_s": round(row["ad_queue_delay_p95_s"], 3),
                "backlog_drops": row["backlog_drop_count"],
                "errors": row["request_error_rate"], "cell_active_s": round(row["cell_active_s"], 1),
            }), flush=True)
            await asyncio.sleep(args.cooldown_s)
    return cells, paired_rows(cells), raw, order_by_rep


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", default="/home/byh/B02/shared_link_exp/live_v2")
    parser.add_argument("--tag", default="v2")
    parser.add_argument("--seed", type=int, default=20260723)
    parser.add_argument("--repetitions", type=int, default=4)
    parser.add_argument("--n-requests", type=int, default=128)
    parser.add_argument("--warmup", type=int, default=32)
    parser.add_argument("--pool-size", type=int, default=96)
    parser.add_argument("--alpha", type=float, default=0.55)
    parser.add_argument("--steps", type=int, default=3)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--output-tokens", type=int, default=4)
    parser.add_argument("--kv-cache-tokens", type=int, required=True,
                        help="per-instance GPU KV cache size in tokens, read from vLLM server logs")
    parser.add_argument("--bps", default="low=520,med=260,high=150",
                        help="link capacities per tier, e.g. low=520,med=260,high=150")
    parser.add_argument("--j", type=int, default=4)
    parser.add_argument("--topk", type=int, default=8)
    parser.add_argument("--prefill-tokens-per-ms", type=float, default=50.0)
    parser.add_argument("--queue-penalty-ms", type=float, default=2.0)
    parser.add_argument("--guard-ms", type=float, default=0.5)
    parser.add_argument("--max-request-attempts", type=int, default=3)
    parser.add_argument("--cooldown-s", type=float, default=1.0)
    parser.add_argument("--ideal-only", action="store_true", help="calibration: run only ideal cells")
    args = parser.parse_args()
    if args.n_requests <= args.warmup or args.repetitions < 1 or args.output_tokens < 1:
        raise ValueError("need measured requests and positive output tokens")
    started = time.time()
    cells, pairs, raw, order_by_rep = asyncio.run(run(args))
    if any(float(row["request_error_rate"]) > 0 for row in cells):
        raise RuntimeError("live request error observed; do not use this run")
    checks = validate_records(raw, args.output_tokens)
    if any(row["status"] != "PASS" for row in checks):
        raise RuntimeError(f"live comparability checks failed: {checks}")
    root = Path(args.out_dir)
    results = root / "results"
    write_csv(results / f"cells_{args.tag}.csv", cells)
    write_csv(results / f"pairs_{args.tag}.csv", pairs)
    write_csv(results / f"sanity_checks_{args.tag}.csv", checks)
    (results / f"raw_{args.tag}.json").write_text(json.dumps(raw))
    metadata = {
        "started_at_unix": started, "finished_at_unix": time.time(),
        "duration_s": time.time() - started, "arguments": vars(args),
        "cell_order_by_rep": {str(rep): order for rep, order in order_by_rep.items()},
        "cells": len(cells), "raw_requests": len(raw),
    }
    (results / f"run_metadata_{args.tag}.json").write_text(json.dumps(metadata, indent=2))
    print(json.dumps(metadata, indent=2))


if __name__ == "__main__":
    main()
