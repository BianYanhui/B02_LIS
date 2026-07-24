#!/usr/bin/env python3
"""Hybrid live shared-link experiment v3: REAL kernel networking state channel.

v3 replaces v2's in-process simulated FIFO with real kernel networking (see
net/ for the platform).  DEVIATION from the letter of the reviewer plan:
vLLM stays on the HOST (GPU simplicity); ALL signaling and background
traffic goes through the real kernel path:

  harness instance-agents (host) --TCP--> gateway container (b02-net,
  NET_ADMIN) --[tc HTB on eth0 egress]--TCP--> dispatcher endpoint (host,
  bridge IP).  iperf3 background traffic shares the link in class 1:20.

Ads/tombstones are fixed 64-byte binary frames (format in net/gateway_relay.py)
sent over real TCP to the gateway relay; the dispatcher index is updated ONLY
on receipt from the relay's downstream connection (real kernel-timed
delivery).  Delivery delay is measured cross-process with wall-clock
timestamps (same host => shared clock): delay = dispatcher recv time -
msg.t_send embedded by the agent.  The `ideal` policy bypasses the network
(synchronous index update, as v2).

The offered ad rate is MEASURED during an `ideal` cell (messages/s x
WIRE_BYTES_PER_MSG on-wire bytes) and the shared-link HTB rate for each cell
is computed as offered/rho, so cells specify --rho, not absolute rates.

Shared-link model (see net/cell_rate.sh): parent HTB class = the shared link
(rate = offered/rho).  Signaling class 1:10 (dst port 9701) is guaranteed
half and may borrow to the full link; background class 1:20 (iperf3, dst
port 5201) likewise.  With no background traffic signaling gets the full
rate; with saturating background it falls toward its guarantee.

Inherited from v2: lineage coverage-growth workload (3-step chains, Zipf
alpha=0.55, disjoint phase shift at 50%), per-rep cell order rotation,
per-instance LRU shadow model for tombstones (capacity from server logs),
physical vllm_cached_tokens as the ONLY reuse ground truth, cache_salt cell
isolation, fixed-length outputs, V4-style integrity checks.

Policies: ideal, exact_fifo (relay passthrough), local_topk (source-side
bounded view), gateway mechanism ablations, agg_static (merge+priority
+dedup2), agg_full (agg_static + adaptive utility gate), and hybrid
(local_topk + agg_full).
"""
from __future__ import annotations

import argparse
import asyncio
import bisect
import csv
import hashlib
import itertools
import json
import random
import re
import socket
import statistics
import struct
import subprocess
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path

import aiohttp


URLS = [f"http://127.0.0.1:{8000 + index}" for index in range(4)]
MODEL_ID = "/home/byh/.cache/modelscope/qwen/Qwen2.5-1.5B-Instruct"
NET_DIR = Path(__file__).resolve().parent / "net"
RELAY_PORT = 9700          # gateway relay, published on 127.0.0.1
DISPATCH_PORT = 9701       # harness dispatcher endpoint, bound on the bridge IP
FRAME = 64
HDR = struct.Struct(">BBHIqQd")
CFG = struct.Struct(">BBBBHI")
STATS = struct.Struct(">IIIIIIII")
K_UP, K_TOMB, K_RESET, K_STATS_REQ, K_CONFIG, K_ACK, K_STATS, K_RESET_DONE = 1, 2, 3, 4, 5, 6, 7, 8
WIRE_BYTES_PER_MSG = 104   # 64 payload + ~40 IP/TCP header; on-wire offered-load convention
STALE_COVERAGE_THRESHOLD = 512
DEFAULT_LINK_POLICIES = ["exact_fifo", "local_topk", "agg_static", "agg_full", "hybrid"]
POLICY_FLAGS = {
    "exact_fifo": dict(merge=0, priority=0, adaptive=0, dedup=0, global_topk=0),
    "local_topk": dict(merge=0, priority=0, adaptive=0, dedup=0, global_topk=0),
    "merge_only": dict(merge=1, priority=0, adaptive=0, dedup=0, global_topk=0),
    "priority_only": dict(merge=0, priority=1, adaptive=0, dedup=0, global_topk=0),
    "dedup_only": dict(merge=0, priority=0, adaptive=0, dedup=2, global_topk=0),
    "merge_priority": dict(merge=1, priority=1, adaptive=0, dedup=0, global_topk=0),
    "agg_static": dict(merge=1, priority=1, adaptive=0, dedup=2, global_topk=1),
    "agg_full": dict(merge=1, priority=1, adaptive=1, dedup=2, global_topk=1),
    "hybrid": dict(merge=1, priority=1, adaptive=1, dedup=2, global_topk=1),
}
LOCAL_TOPK_POLICIES = {"local_topk", "hybrid"}
MAX_QUEUE = 200            # relay passthrough drop-oldest cap (exact_fifo)
DRAIN_TIMEOUT_S = 60.0
BASE_WORDS = 2048
STEP_WORDS = 512


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


def stable_int(*parts: object) -> int:
    raw = "|".join(map(str, parts)).encode()
    return int.from_bytes(hashlib.blake2b(raw, digest_size=8).digest(), "big")


def digest64(digest: str) -> int:
    return int.from_bytes(hashlib.blake2b(digest.encode(), digest_size=8).digest(), "big")


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def write_csv(path: Path, rows: list[dict]) -> None:
    ensure_dir(path.parent)
    if not rows:
        path.write_text("")
        return
    columns = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, lineterminator="\n")
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
    rng = random.Random(stable_int(args.seed, "shared-link-v3", rep, args.n_requests, args.pool_size, args.alpha))
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
    prompt = f"Shared reusable context for tenant {request.tenant} and lineage {request.digest}. " + ("context " * BASE_WORDS)
    for k in range(1, request.step + 1):
        prompt += f"Extension {k} for lineage {request.digest}. " + ("detail " * STEP_WORDS)
    return prompt


class Dispatcher:
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


class ShadowCache:
    """Per-instance LRU shadow of vLLM's block pool (tombstones only; see README)."""

    def __init__(self, capacity_tokens: int) -> None:
        self.capacity = capacity_tokens
        self.entries: dict[str, int] = {}
        self.total = 0

    def insert(self, digest: str, tokens: int) -> list[str]:
        if digest in self.entries:
            old = self.entries.pop(digest)
            self.entries[digest] = max(old, tokens)
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


def sh(args: list[str]) -> str:
    return subprocess.check_output(args, text=True)


def bridge_ip() -> str:
    net_id = sh(["docker", "network", "inspect", "b02-net", "-f", "{{.Id}}"]).strip()
    out = sh(["bash", "-c", f"ip -4 -o addr show dev br-{net_id[:12]} | awk '{{print $4}}' | cut -d/ -f1"]).strip()
    if not out:
        raise RuntimeError("cannot determine b02-net bridge IP")
    return out


def parse_tc(text: str) -> dict:
    """Parse `tc -s qdisc/class` output: per-qdisc backlog bytes, per-class Sent bytes."""
    out: dict[str, float] = {}
    blocks = text.split("---")
    qdisc_text = blocks[0]
    for match in re.finditer(r"qdisc \S+ (\w+): [^\n]*\n Sent \d+ bytes \d+ pkt \(dropped (\d+)[^\n]*\n backlog (\d+)b", qdisc_text):
        handle, dropped, backlog = match.group(1), int(match.group(2)), int(match.group(3))
        out[f"qdisc_{handle}_backlog_bytes"] = backlog
        out[f"qdisc_{handle}_dropped"] = dropped
    class_text = blocks[1] if len(blocks) > 1 else ""
    for match in re.finditer(r"class htb (1:\d+) [^\n]*\n Sent (\d+) bytes", class_text):
        out[f"class_{match.group(1).replace(':', '')}_sent_bytes"] = int(match.group(2))
    return out


def tc_snapshot(tag: str, out_dir: Path) -> dict:
    text = sh(["bash", str(NET_DIR / "tc_stats.sh")])
    ensure_dir(out_dir / "tc")
    (out_dir / "tc" / f"tc_{tag}.txt").write_text(text)
    return parse_tc(text)


class LinkRuntime:
    """Run-wide networking state: dispatcher endpoint server, agent conns,
    per-cell views.  Acts as (a) the dispatcher endpoint: a TCP server on the
    b02-net bridge IP:9701 that the gateway relay connects to, and (b) the
    four instance agents: TCP clients of the relay on 127.0.0.1:9700.  The
    dispatcher index is updated ONLY by frames received from the relay."""

    def __init__(self) -> None:
        self.dispatcher: Dispatcher | None = None
        self.cell_id = -1
        self.digest_map: dict[int, str] = {}
        self.down_writer: asyncio.StreamWriter | None = None
        self.agent_writers: list[asyncio.StreamWriter] = []
        self.agent_readers: list[asyncio.Task] = []
        self.delays: dict[str, list[float]] = {"upsert": [], "tombstone": []}
        self.sent = 0
        self.received = 0
        self.reset_done = asyncio.Event()
        self.stats_future: asyncio.Future | None = None

    async def start(self) -> None:
        ip = bridge_ip()
        server = await asyncio.start_server(self._on_downstream, ip, DISPATCH_PORT)
        self._server = server
        await self.open_agents()

    async def open_agents(self) -> None:
        self.agent_writers = []
        self.agent_readers = []
        for _ in URLS:
            reader, writer = await asyncio.open_connection("127.0.0.1", RELAY_PORT)
            sock = writer.get_extra_info("socket")
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.agent_writers.append(writer)
            self.agent_readers.append(asyncio.create_task(self._on_agent(reader)))

    async def _on_agent(self, reader: asyncio.StreamReader) -> None:
        """Receive unshaped relay-control replies on each agent connection."""
        try:
            while True:
                data = await reader.readexactly(FRAME)
                kind, _instance, cell, _seq, _coverage, _digest, _sent = HDR.unpack(data[:32])
                if kind == K_STATS and cell == self.cell_id and self.stats_future is not None and not self.stats_future.done():
                    self.stats_future.set_result(STATS.unpack(data[32:64]))
        except (asyncio.IncompleteReadError, ConnectionResetError):
            return

    async def _on_downstream(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.down_writer = writer
        try:
            while True:
                data = await reader.readexactly(FRAME)
                kind, instance, cell, seq, coverage, digest, t_send = HDR.unpack(data[:32])
                now = time.time()
                if kind == K_RESET_DONE:
                    if cell == self.cell_id:
                        self.reset_done.set()
                    continue
                if kind == K_STATS:
                    if self.stats_future is not None and not self.stats_future.done():
                        self.stats_future.set_result(STATS.unpack(data[32:64]))
                    continue
                if kind not in (K_UP, K_TOMB):
                    continue
                if cell != self.cell_id or self.dispatcher is None:
                    continue
                name = self.digest_map.get(digest)
                if name is None:
                    continue
                if kind == K_UP:
                    self.dispatcher.index[instance][name] = coverage
                    self.delays["upsert"].append(now - t_send)
                else:
                    self.dispatcher.index[instance].pop(name, None)
                    self.delays["tombstone"].append(now - t_send)
                self.received += 1
                # feedback ack so the relay's adaptive gate sees REAL delay
                writer.write(HDR.pack(K_ACK, 0, cell, seq, 0, 0, now) + b"\x00" * 32)
        except (asyncio.IncompleteReadError, ConnectionResetError):
            pass
        finally:
            self.down_writer = None
            writer.close()

    async def configure_cell(self, dispatcher: Dispatcher, cell_id: int, digest_map: dict[int, str], policy: str, global_topk: int) -> None:
        self.dispatcher = dispatcher
        self.cell_id = cell_id
        self.digest_map = digest_map
        self.delays = {"upsert": [], "tombstone": []}
        self.sent = 0
        self.received = 0
        self.reset_done.clear()
        self.stats_future = None
        if not self.agent_writers:
            await self.open_agents()
        flags = POLICY_FLAGS[policy]
        cfg_payload = CFG.pack(
            flags["merge"], flags["priority"], flags["adaptive"],
            global_topk if flags["global_topk"] else 0,
            flags["dedup"], MAX_QUEUE,
        )
        self.agent_writers[0].write(HDR.pack(K_CONFIG, 0, cell_id, 0, 0, 0, time.time()) + cfg_payload.ljust(32, b"\x00"))
        await self.agent_writers[0].drain()
        for attempt in range(3):
            self.agent_writers[0].write(HDR.pack(K_RESET, 0, cell_id, 0, 0, 0, time.time()) + b"\x00" * 32)
            await self.agent_writers[0].drain()
            try:
                await asyncio.wait_for(self.reset_done.wait(), timeout=15)
                return
            except asyncio.TimeoutError:
                self.reset_done.clear()
        raise RuntimeError("relay did not acknowledge cell reset")

    def send(self, kind: int, instance: int, digest: str, coverage: int) -> None:
        frame = HDR.pack(kind, instance, self.cell_id, self.sent, coverage, digest64(digest), time.time()) + b"\x00" * 32
        self.agent_writers[instance].write(frame)
        self.sent += 1

    async def drain(self, timeout_s: float = DRAIN_TIMEOUT_S) -> None:
        deadline = time.perf_counter() + timeout_s
        quiet_since = time.perf_counter()
        last_received = self.received
        while time.perf_counter() < deadline:
            await asyncio.sleep(0.25)
            if self.received != last_received:
                last_received = self.received
                quiet_since = time.perf_counter()
            elif time.perf_counter() - quiet_since > 5.0:
                return  # no deliveries for 5 s: remaining backlog is undeliverable

    async def fetch_stats(self) -> dict:
        self.stats_future = asyncio.get_running_loop().create_future()
        self.agent_writers[0].write(HDR.pack(K_STATS_REQ, 0, self.cell_id, 0, 0, 0, time.time()) + b"\x00" * 32)
        await self.agent_writers[0].drain()
        try:
            forwarded, sup, cap, util, backlog, global_topk, maxq, ewma_ms = await asyncio.wait_for(self.stats_future, timeout=15)
        except asyncio.TimeoutError:
            return {}
        return {
            "relay_forwarded": forwarded, "relay_drop_superseded": sup,
            "relay_drop_replica_cap": cap, "relay_drop_low_utility": util,
            "relay_drop_backlog_cap": backlog, "relay_drop_global_topk": global_topk,
            "relay_max_queue": maxq,
            "relay_ewma_dq_s": ewma_ms / 1000.0,
        }


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


async def run_cell(trace: list[TraceRequest], policy: str, rho: float | None, bg: bool, cell_tag: str,
                   cache_salt: str, link: LinkRuntime, cell_uid: int, rate_state: dict, args: argparse.Namespace) -> tuple[dict, list[dict]]:
    dispatcher = Dispatcher(args.j, args.prefill_tokens_per_ms, args.queue_penalty_ms, args.guard_ms)
    shadows = [ShadowCache(args.kv_cache_tokens) for _ in URLS]
    # `advertised` is the source's best-known dispatcher view.  For local
    # filtering it is reconciled after each real resource update, so entries
    # displaced from the local Top-K receive a withdrawal just like evictions.
    advertised: list[dict[str, int]] = [dict() for _ in URLS]
    local_entries: list[dict[str, int]] = [dict() for _ in URLS]
    local_recency: list[dict[str, int]] = [dict() for _ in URLS]
    local_clock = 0
    upserts_generated = 0
    source_upserts_sent = 0
    source_tombstones_sent = 0
    is_ideal = policy == "ideal"
    digest_map = {digest64(request.digest): request.digest for request in trace}
    sig_bit = 0
    tc_before: dict = {}
    tc_mid: dict = {}
    tc_mid_done = is_ideal
    if not is_ideal:
        sig_bit = max(64, int(rate_state["offered_bit_per_s"] / rho))
        print(sh(["bash", str(NET_DIR / "cell_rate.sh"), "--sig-bit", str(sig_bit)]).strip(), flush=True)
        if bg:
            subprocess.call(["docker", "exec", "-d", "gateway", "iperf3", "-c", "bgserver", "-t", "7200"])
            await asyncio.sleep(1.0)
        tc_before = tc_snapshot(f"{cell_tag}_before", Path(args.out_dir))
        await link.configure_cell(dispatcher, cell_uid % 60000, digest_map, policy, args.global_topk)
    records: list[dict] = []
    started = time.perf_counter()
    mid_at = int(len(trace) * 0.75)
    async with aiohttp.ClientSession() as session:
        for offset in range(0, len(trace), args.concurrency):
            if not tc_mid_done and offset >= mid_at:
                tc_mid = tc_snapshot(f"{cell_tag}_mid", Path(args.out_dir))
                tc_mid_done = True
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
                evicted = shadows[target].insert(request.digest, request.coverage_tokens)
                if is_ideal:
                    dispatcher.index[target][request.digest] = request.coverage_tokens
                    for victim in evicted:
                        dispatcher.index[target].pop(victim, None)
                    upserts_generated += 1
                else:
                    upserts_generated += 1
                    if policy in LOCAL_TOPK_POLICIES:
                        local_clock += 1
                        local_entries[target][request.digest] = request.coverage_tokens
                        local_recency[target][request.digest] = local_clock
                        for victim in evicted:
                            local_entries[target].pop(victim, None)
                            local_recency[target].pop(victim, None)
                        selected = dict(sorted(
                            local_entries[target].items(),
                            key=lambda item: (-item[1], -local_recency[target][item[0]], item[0]),
                        )[:args.topk])
                        for digest in set(advertised[target]) - set(selected):
                            link.send(K_TOMB, target, digest, 0)
                            source_tombstones_sent += 1
                        for digest, selected_coverage in selected.items():
                            if advertised[target].get(digest) != selected_coverage:
                                link.send(K_UP, target, digest, selected_coverage)
                                source_upserts_sent += 1
                        advertised[target] = selected
                    else:
                        link.send(K_UP, target, request.digest, request.coverage_tokens)
                        source_upserts_sent += 1
                        advertised[target][request.digest] = request.coverage_tokens
                        for victim in evicted:
                            if victim in advertised[target]:
                                link.send(K_TOMB, target, victim, 0)
                                source_tombstones_sent += 1
                                advertised[target].pop(victim, None)
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
    net_metrics: dict = {}
    if not is_ideal:
        await link.drain()
        stats = await link.fetch_stats()
        if bg:
            subprocess.call(["docker", "exec", "gateway", "pkill", "iperf3"])
        tc_after = tc_snapshot(f"{cell_tag}_after", Path(args.out_dir))
        up = link.delays["upsert"]
        tomb = link.delays["tombstone"]
        net_metrics = {
            "rho": rho, "sig_bit_per_s": sig_bit, "background_traffic": bg,
            "source_local_topk": policy in LOCAL_TOPK_POLICIES,
            "gateway_global_topk": args.global_topk if POLICY_FLAGS[policy]["global_topk"] else 0,
            "source_upserts_sent": source_upserts_sent,
            "source_tombstones_sent": source_tombstones_sent,
            "source_suppressed_upserts": max(0, upserts_generated - source_upserts_sent),
            "net_msgs_sent": link.sent, "net_msgs_delivered": link.received,
            "net_wire_bytes_sent": link.sent * WIRE_BYTES_PER_MSG,
            "net_undelivered_at_drain_end": link.sent - link.received,
            "ad_delivery_delay_mean_s": statistics.mean(up) if up else 0.0,
            "ad_delivery_delay_p50_s": percentile(up, 50),
            "ad_delivery_delay_p95_s": percentile(up, 95),
            "ad_delivery_delay_p99_s": percentile(up, 99),
            "tombstone_delay_p95_s": percentile(tomb, 95),
            "tc_sig_backlog_bytes_before": tc_before.get("qdisc_10_backlog_bytes", 0),
            "tc_sig_backlog_bytes_mid": tc_mid.get("qdisc_10_backlog_bytes", 0),
            "tc_sig_backlog_bytes_after": tc_after.get("qdisc_10_backlog_bytes", 0),
            "tc_sig_dropped_after": tc_after.get("qdisc_10_dropped", 0),
            "tc_class110_sent_bytes_after": tc_after.get("class_110_sent_bytes", 0),
            "tc_class120_sent_bytes_after": tc_after.get("class_120_sent_bytes", 0),
            "tc_class120_sent_bytes_before": tc_before.get("class_120_sent_bytes", 0),
            **stats,
        }
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
        "offered_wire_bit_per_s": (upserts_generated * WIRE_BYTES_PER_MSG * 8) / active_s if active_s > 0 else 0.0,
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
        **net_metrics,
    }
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
                "rho": row.get("rho", ""),
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


async def run(args: argparse.Namespace) -> dict:
    await check_endpoints()
    root = Path(args.out_dir)
    link = LinkRuntime()
    await link.start()
    rate_state = {"offered_bit_per_s": args.initial_offered_bit_per_s}
    cells: list[dict] = []
    raw: list[dict] = []
    cell_uid = 0
    order_by_rep: dict[int, list[str]] = {}

    async def do_cell(rep: int, policy: str, rho: float | None, bg: bool, trace: list[TraceRequest], trace_hash: str, order_index: int, order_seq: str, suffix: str = "") -> dict:
        nonlocal cell_uid
        cell_uid += 1
        cell_id = "ideal" if policy == "ideal" else f"{policy}@rho{rho}" + ("+bg" if bg else "") + suffix
        cache_salt = f"sharedlink-v3:{args.tag}:{cell_id}:rep{rep}"
        metrics, records = await run_cell(trace, policy, rho, bg, f"rep{rep}_{cell_id}_{args.tag}", cache_salt, link, cell_uid, rate_state, args)
        if policy == "ideal" and metrics["offered_wire_bit_per_s"] > 0:
            rate_state["offered_bit_per_s"] = metrics["offered_wire_bit_per_s"]
        row = {
            "experiment_id": f"20260723_shared_link_live_v3_{cell_id}_rep{rep}",
            "experiment": "shared_link_live_v3",
            "evidence_type": "hybrid_live_vllm_real_kernel_link",
            "code_commit": git_commit(),
            "model": MODEL_ID,
            "hardware": "4x Tesla T4; Qwen2.5-1.5B-Instruct; one vLLM instance/GPU on host; docker gateway+tc state channel",
            "cell_id": cell_id, "policy": policy,
            "wire_frame_bytes": FRAME, "wire_bytes_per_msg_assumed": WIRE_BYTES_PER_MSG,
            "rep": rep, "repetitions": args.repetitions, "seed": args.seed,
            "workload_trace_hash": trace_hash,
            "zipf_alpha": args.alpha, "pool_size": args.pool_size,
            "lineage_steps": args.steps,
            "distinct_lineages_total": 2 * args.pool_size,
            "phase_shift_at_request": args.n_requests // 2,
            "n_requests": args.n_requests, "warmup_request_count": args.warmup,
            "concurrency": args.concurrency,
            "fixed_output_tokens": args.output_tokens,
            "kv_cache_tokens_per_instance_shadow": args.kv_cache_tokens,
            "max_queue_relay": MAX_QUEUE, "drain_timeout_s": DRAIN_TIMEOUT_S,
            "topk": args.topk, "j": args.j,
            "guard_ms": args.guard_ms, "prefill_tokens_per_ms": args.prefill_tokens_per_ms,
            "queue_penalty_ms": args.queue_penalty_ms,
            "cell_order_index": order_index,
            "cell_order_sequence": order_seq,
            "generation_mode": "greedy_temperature0_min_tokens_eq_max_tokens_ignore_eos",
            "vllm_cache_salt": cache_salt, "semantic_prompt_contains_policy": False,
            "metric_scope": "TTFT and cached-token telemetry are live vLLM measurements; the state channel is real kernel networking (docker + tc HTB).",
            "status": "Current", **metrics,
        }
        cells.append(row)
        raw.extend({"experiment_id": row["experiment_id"], "rep": rep, "cell_id": cell_id, "policy": policy, **record} for record in records)
        print(json.dumps({
            "completed": row["experiment_id"], "order": order_index,
            "ttft_mean_ms": round(row["ttft_mean_ms"], 1),
            "cached_tokens": row["vllm_cached_tokens_total"],
            "stale_fallback_rate": round(row["stale_fallback_rate"], 3),
            "ad_p95_s": round(row.get("ad_delivery_delay_p95_s", 0.0), 3),
            "tc_backlog_after": row.get("tc_sig_backlog_bytes_after", 0),
            "undelivered": row.get("net_undelivered_at_drain_end", 0),
            "errors": row["request_error_rate"], "cell_active_s": round(row["cell_active_s"], 1),
        }), flush=True)
        await asyncio.sleep(args.cooldown_s)
        return row

    if args.smoke:
        # Smoke: calibration-capable ideal cell first (also a recorded cell),
        # then the rho grid, then the background-sharing A/B.  Fixed order.
        rep = 0
        trace_path = root / "traces" / f"shared_link_v3_trace_rep{rep}.csv"
        trace = make_trace(trace_path, rep, args)
        trace_hash = sha256_file(trace_path)
        order_by_rep[rep] = ["ideal", "exact_fifo@rho0.5", "exact_fifo@rho1.3", "agg_full@rho0.5", "agg_full@rho1.3", "exact_fifo@rho0.5+bgoff", "exact_fifo@rho0.5+bg"]
        await do_cell(rep, "ideal", None, False, trace, trace_hash, 0, ",".join(order_by_rep[rep]))
        for idx, (policy, rho, bg, suffix) in enumerate([
            ("exact_fifo", 0.5, False, ""), ("exact_fifo", 1.3, False, ""),
            ("agg_full", 0.5, False, ""), ("agg_full", 1.3, False, ""),
            # At rho=0.5, a saturating peer consumes borrowable bandwidth and
            # changes the signal service rate from full link to its guarantee.
            ("exact_fifo", 0.5, False, "+bgoff"), ("exact_fifo", 0.5, True, ""),
        ], start=1):
            await do_cell(rep, policy, rho, bg, trace, trace_hash, idx, ",".join(order_by_rep[rep]), suffix)
    else:
        rhos = [float(value) for value in args.rhos.split(",")]
        policies = [value.strip() for value in args.policies.split(",") if value.strip()]
        unknown = sorted(set(policies) - set(POLICY_FLAGS))
        if unknown:
            raise ValueError(f"unknown policies: {','.join(unknown)}")
        for rep in range(args.repetitions):
            trace_path = root / "traces" / f"shared_link_v3_trace_rep{rep}.csv"
            trace = make_trace(trace_path, rep, args)
            trace_hash = sha256_file(trace_path)
            plan: list[tuple[str, float | None, bool]] = [("ideal", None, False)]
            plan += [(policy, rho, args.background) for policy in policies for rho in rhos]
            if rep == 0:
                # rep0's ideal cell doubles as the offered-rate calibration and
                # must run before any link cell; the rest of rep0 is shuffled.
                head, tail = plan[:1], plan[1:]
                random.Random(stable_int(args.seed, "cell-order", rep)).shuffle(tail)
                order = head + tail
            else:
                order = list(plan)
                random.Random(stable_int(args.seed, "cell-order", rep)).shuffle(order)
            order_by_rep[rep] = ["ideal" if policy == "ideal" else f"{policy}@rho{rho}" for policy, rho, _ in order]
            for order_index, (policy, rho, bg) in enumerate(order):
                await do_cell(rep, policy, rho, bg, trace, trace_hash, order_index, ",".join(order_by_rep[rep]))

    return {"cells": cells, "raw": raw, "order_by_rep": order_by_rep}


def smoke_report(cells: list[dict], checks: list[dict]) -> list[dict]:
    by_id = {row["cell_id"]: row for row in cells}
    report: list[dict] = []

    def add(name: str, passed: bool, detail: str) -> None:
        report.append({"check": name, "status": "PASS" if passed else "FAIL", "detail": detail})

    ex13 = by_id.get("exact_fifo@rho1.3", {})
    ex05 = by_id.get("exact_fifo@rho0.5", {})
    backlog_mid = float(ex13.get("tc_sig_backlog_bytes_mid", 0))
    backlog_after = float(ex13.get("tc_sig_backlog_bytes_after", 0))
    add("a: real tc backlog at rho=1.3", max(backlog_mid, backlog_after) > 0,
        f"tc_sig_backlog mid={backlog_mid:.0f}B after={backlog_after:.0f}B relay_max_queue={ex13.get('relay_max_queue')}")
    p95_13 = float(ex13.get("ad_delivery_delay_p95_s", 0))
    p95_05 = float(ex05.get("ad_delivery_delay_p95_s", 0))
    add("b: delivery delay p95 materially larger at rho=1.3", p95_13 >= max(1.0, 3.0 * p95_05),
        f"p95 rho1.3={p95_13:.3f}s rho0.5={p95_05:.3f}s")
    ideal = by_id.get("ideal", {})
    add("c: ideal bypasses the network", "ad_delivery_delay_p95_s" not in ideal and float(ideal.get("upserts_per_s", 0)) > 0,
        f"ideal net fields absent, upserts_per_s={ideal.get('upserts_per_s')}")
    add("d: integrity checks", all(row["status"] == "PASS" for row in checks),
        "; ".join(f"{row['check_name']}={row['status']}" for row in checks))
    bg_on = by_id.get("exact_fifo@rho0.5+bg", {})
    bg_off = by_id.get("exact_fifo@rho0.5+bgoff", {})
    p95_on = float(bg_on.get("ad_delivery_delay_p95_s", 0))
    p95_off = float(bg_off.get("ad_delivery_delay_p95_s", 0))
    bg_bytes_delta = int(bg_on.get("tc_class120_sent_bytes_after", 0)) - int(bg_on.get("tc_class120_sent_bytes_before", 0))
    add("e: iperf3 background sharing raises signaling delay", p95_on > p95_off and bg_bytes_delta > 0,
        f"p95 bg_on={p95_on:.3f}s bg_off={p95_off:.3f}s class1:20_bytes_during_bg_cell={bg_bytes_delta}")
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", default="/home/byh/B02/shared_link_exp/live_v3")
    parser.add_argument("--tag", default="v3")
    parser.add_argument("--seed", type=int, default=20260723)
    parser.add_argument("--repetitions", type=int, default=4)
    parser.add_argument("--n-requests", type=int, default=128)
    parser.add_argument("--warmup", type=int, default=32)
    parser.add_argument("--pool-size", type=int, default=64)
    parser.add_argument("--alpha", type=float, default=0.55)
    parser.add_argument("--steps", type=int, default=3)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--output-tokens", type=int, default=4)
    parser.add_argument("--kv-cache-tokens", type=int, required=True)
    parser.add_argument("--rhos", default="0.5,1.0,1.75")
    parser.add_argument("--policies", default=",".join(DEFAULT_LINK_POLICIES),
                        help="comma-separated link policies; includes gateway ablations")
    parser.add_argument("--global-topk", type=int, default=16,
                        help="distinct queued prefixes retained by gateway static/full policies")
    parser.add_argument("--background", action="store_true",
                        help="run every non-ideal cell with saturating iperf3 traffic")
    parser.add_argument("--initial-offered-bit-per-s", type=float, default=1248.0,
                        help="used until the first ideal cell measures the real offered rate")
    parser.add_argument("--j", type=int, default=4)
    parser.add_argument("--topk", type=int, default=8)
    parser.add_argument("--prefill-tokens-per-ms", type=float, default=50.0)
    parser.add_argument("--queue-penalty-ms", type=float, default=2.0)
    parser.add_argument("--guard-ms", type=float, default=0.5)
    parser.add_argument("--max-request-attempts", type=int, default=3)
    parser.add_argument("--cooldown-s", type=float, default=1.0)
    parser.add_argument("--smoke", action="store_true")
    args = parser.parse_args()
    if args.smoke:
        args.repetitions = 1
        args.n_requests = 48
        args.warmup = 16
        args.tag = args.tag if args.tag != "v3" else "v3smoke"
    if args.n_requests <= args.warmup or args.output_tokens < 1:
        raise ValueError("need measured requests and positive output tokens")
    started = time.time()
    result = asyncio.run(run(args))
    cells, raw = result["cells"], result["raw"]
    if any(float(row["request_error_rate"]) > 0 for row in cells):
        raise RuntimeError("live request error observed; do not use this run")
    checks = validate_records(raw, args.output_tokens)
    if any(row["status"] != "PASS" for row in checks):
        raise RuntimeError(f"live comparability checks failed: {checks}")
    root = Path(args.out_dir)
    results = root / "results"
    write_csv(results / f"cells_{args.tag}.csv", cells)
    write_csv(results / f"pairs_{args.tag}.csv", paired_rows(cells))
    write_csv(results / f"sanity_checks_{args.tag}.csv", checks)
    (results / f"raw_{args.tag}.json").write_text(json.dumps(raw))
    metadata = {
        "started_at_unix": started, "finished_at_unix": time.time(),
        "duration_s": time.time() - started, "arguments": vars(args),
        "cell_order_by_rep": {str(rep): order for rep, order in result["order_by_rep"].items()},
        "cells": len(cells), "raw_requests": len(raw),
    }
    (results / f"run_metadata_{args.tag}.json").write_text(json.dumps(metadata, indent=2))
    if args.smoke:
        report = smoke_report(cells, checks)
        write_csv(results / f"smoke_report_{args.tag}.csv", report)
        print("SMOKE_V3_REPORT " + json.dumps(report, indent=2))
        if any(row["status"] != "PASS" for row in report):
            raise SystemExit("smoke checks FAILED")
    print(json.dumps(metadata, indent=2))


if __name__ == "__main__":
    main()
