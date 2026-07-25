#!/usr/bin/env python3
"""Frozen-manifest 4xT4 live baseline experiment.

The 4xT4 study reuses the legacy platform's real kernel signaling path while
placing its code, Docker network, containers, logs, raw data, and figures in
an isolated directory.  vLLM stays on the host; all signaling and background
traffic traverse:

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

Formal policies: Ideal (upper bound), FullSync, RateFIFO, LatestOnly,
AgeCov-Greedy, StaticSemantic, and Adaptive.  RateFIFO has the identical
physical HTB capacity as Adaptive; it differs only by using a non-semantic
token bucket at admission.  The policy definition is written into every
manifest and CSV row.
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
RELAY_PORT = 9710          # isolated 4T4 gateway relay, published on localhost
DISPATCH_PORT = 9711       # dispatcher endpoint on the isolated Docker bridge
FRAME = 64
HDR = struct.Struct(">BBHIqQd")
CFG = struct.Struct(">BBBBHIIIII")
STATS = struct.Struct(">IIIIIIII")
K_UP, K_TOMB, K_RESET, K_STATS_REQ, K_CONFIG, K_ACK, K_STATS, K_RESET_DONE = 1, 2, 3, 4, 5, 6, 7, 8
WIRE_BYTES_PER_MSG = 104   # 64 payload + ~40 IP/TCP header; on-wire offered-load convention
STALE_COVERAGE_THRESHOLD = 512
MODE_FULLSYNC, MODE_RATEFIFO, MODE_LATEST, MODE_AGECOV, MODE_STATIC, MODE_ADAPTIVE = range(6)
POLICY_DEFS = {
    "FullSync": dict(mode=MODE_FULLSYNC, merge=0, priority=0, adaptive=0, dedup=0, global_topk=0,
                     definition="All events forward in FIFO order; no suppression, replacement, deduplication, priority, or congestion admission."),
    "RateFIFO": dict(mode=MODE_RATEFIFO, merge=0, priority=0, adaptive=0, dedup=0, global_topk=0,
                     definition="FIFO plus only a token bucket at the same physical signaling rate; no KV-state semantics."),
    "LatestOnly": dict(mode=MODE_LATEST, merge=1, priority=0, adaptive=0, dedup=0, global_topk=0,
                        definition="For an unsent (owner,prefix) update retain only the latest one; no priority, deduplication, greedy score, or adaptive admission."),
    "AgeCov-Greedy": dict(mode=MODE_AGECOV, merge=0, priority=0, adaptive=0, dedup=0, global_topk=0,
                           definition="Choose pending updates by age_s * max(coverage_tokens,1) / 64 bytes; no invalidation priority, replica suppression, or adaptive gate."),
    "StaticSemantic": dict(mode=MODE_STATIC, merge=1, priority=1, adaptive=0, dedup=2, global_topk=0,
                            definition="Static semantic aggregation: latest-update replacement, non-preemptive invalidation priority, and at-most-two queued/advertised owners per prefix."),
    "Adaptive": dict(mode=MODE_ADAPTIVE, merge=1, priority=1, adaptive=1, dedup=2, global_topk=16,
                     definition="StaticSemantic plus EWMA-delay/queue-aware utility admission and dynamic tightening of the global useful-prefix set."),
}
DEFAULT_LINK_POLICIES = list(POLICY_DEFS)
IDEAL_POLICY = "Ideal"
MAX_QUEUE = 4096           # safety only; FullSync is not deliberately capped
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
    workload: str
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


def reuse_prefix_lengths(pool_size: int, seed: int) -> list[int]:
    """Fixed 35/40/25 mixture across a pool: 1024/2048/4096-token contexts."""
    n_1024 = round(pool_size * 0.35)
    n_2048 = round(pool_size * 0.40)
    lengths = [1024] * n_1024 + [2048] * n_2048
    lengths += [4096] * (pool_size - len(lengths))
    rng = random.Random(seed)
    rng.shuffle(lengths)
    return lengths


def make_trace(path: Path, rep: int, args: argparse.Namespace) -> list[TraceRequest]:
    rng = random.Random(stable_int(args.seed, "formal4t4", args.workload, rep, args.n_requests, args.pool_size, args.alpha))
    cdf = zipf_cdf(args.alpha, args.pool_size)
    shift_at = args.n_requests // 2
    next_step: dict[int, int] = {}
    prefix_lengths = {
        phase: reuse_prefix_lengths(args.pool_size, stable_int(args.seed, args.workload, rep, phase, "prefix-lengths"))
        for phase in (0, 1)
    }
    trace: list[TraceRequest] = []
    for request_id in range(args.n_requests):
        phase = 0 if request_id < shift_at else 1
        slot = bisect.bisect_left(cdf, rng.random())
        lineage_id = phase * args.pool_size + slot
        step = next_step.get(lineage_id, 0)
        next_step[lineage_id] = (step + 1) % args.steps
        coverage = (BASE_WORDS + STEP_WORDS * step) if args.workload == "original_compatible" else prefix_lengths[phase][slot]
        trace.append(TraceRequest(
            request_id=request_id,
            phase=phase,
            lineage_id=lineage_id,
            step=step,
            tenant=f"tenant-{lineage_id % 8}",
            digest=f"L{lineage_id:04d}",
            coverage_tokens=coverage,
            workload=args.workload,
            discard=request_id < args.warmup,
        ))
    ensure_dir(path.parent)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(asdict(trace[0]).keys()))
        writer.writeheader()
        writer.writerows(asdict(request) for request in trace)
    return trace


def prompt_for(request: TraceRequest) -> str:
    if request.workload == "original_compatible":
        prefix = f"Shared reusable context for tenant {request.tenant} and lineage {request.digest}. " + ("context " * BASE_WORDS)
        for k in range(1, request.step + 1):
            prefix += f"Extension {k} for lineage {request.digest}. " + ("detail " * STEP_WORDS)
    else:
        # The leading context is byte-identical for every request of one
        # logical prefix; only the suffix changes.  The vLLM usage telemetry
        # records the exact post-chat-template token count per request.
        prefix = (
            f"[KV shared prefix id={request.digest} tenant={request.tenant}]\n"
            + ("context " * request.coverage_tokens)
        )
    return prefix + f"\nTask turn {request.request_id}, variant {request.step}: respond with OK."


class Dispatcher:
    def __init__(self, j: int, prefill_tokens_per_ms: float, queue_penalty_ms: float, guard_ms: float) -> None:
        self.j = j
        self.prefill_tokens_per_ms = prefill_tokens_per_ms
        self.queue_penalty_ms = queue_penalty_ms
        self.guard_ms = guard_ms
        self.index: list[dict[str, int]] = [dict() for _ in URLS]
        # Source-generation and arrival times are retained separately: a
        # delivered-only delay percentile is not a freshness metric.
        self.generated_at: list[dict[str, float]] = [dict() for _ in URLS]
        self.received_at: list[dict[str, float]] = [dict() for _ in URLS]
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

    def apply_upsert(self, instance: int, digest: str, coverage: int, generated_at: float, received_at: float) -> None:
        self.index[instance][digest] = coverage
        self.generated_at[instance][digest] = generated_at
        self.received_at[instance][digest] = received_at

    def apply_tombstone(self, instance: int, digest: str) -> None:
        self.index[instance].pop(digest, None)
        self.generated_at[instance].pop(digest, None)
        self.received_at[instance].pop(digest, None)


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

    def reset(self) -> list[str]:
        """Mirror a test-only physical owner cache reset."""
        evicted = list(self.entries)
        self.entries.clear()
        self.total = 0
        return evicted


def sh(args: list[str]) -> str:
    return subprocess.check_output(args, text=True)


def bridge_ip() -> str:
    net_id = sh(["docker", "network", "inspect", "b02-4t4-net", "-f", "{{.Id}}"]).strip()
    out = sh(["bash", "-c", f"ip -4 -o addr show dev br-{net_id[:12]} | awk '{{print $4}}' | cut -d/ -f1"]).strip()
    if not out:
        raise RuntimeError("cannot determine b02-4t4-net bridge IP")
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
    text = sh(["bash", str(NET_DIR / "tc_stats_4t4.sh")])
    ensure_dir(out_dir / "tc")
    (out_dir / "tc" / f"tc_{tag}.txt").write_text(text)
    return parse_tc(text)


def gateway_update_events(cell_ids: set[int]) -> list[dict]:
    """Read structured update events emitted by this isolated gateway.

    Cell IDs are tag/seed-derived, so filtering avoids mixing earlier smoke or
    calibration logs with a frozen formal run.  A later analysis pass joins
    these enqueue/suppress/forward events with dispatcher-arrival events on
    ``(relay_cell_id, update_id)``.
    """
    try:
        output = subprocess.check_output(["docker", "logs", "b02-gateway4t4"], text=True, stderr=subprocess.STDOUT)
    except Exception as exc:
        raise RuntimeError(f"cannot collect b02-gateway4t4 update telemetry: {exc!r}") from exc
    events: list[dict] = []
    for line in output.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("event") == "update" and int(event.get("cell", -1)) in cell_ids:
            events.append(event)
    return events


def enforce_frozen_manifest(args: argparse.Namespace) -> None:
    """Reject any baseline invocation that diverges from its frozen record."""
    if args.stage != "baseline":
        return
    if not args.frozen_manifest:
        raise ValueError("baseline stage requires --frozen-manifest")
    manifest = json.loads(Path(args.frozen_manifest).read_text())
    if manifest.get("model") != MODEL_ID or manifest.get("hardware", {}).get("instances") != 4:
        raise ValueError("manifest model or four-instance mapping mismatch")
    workload = manifest.get("workloads", {}).get(args.workload)
    if not workload:
        raise ValueError(f"workload not present in frozen manifest: {args.workload}")
    expected = {
        "n_requests": workload["requests"], "warmup": workload["warmup"],
        "concurrency": workload["concurrency"], "pool_size": workload["pool_size"],
        "alpha": workload["alpha"], "overlap": workload["replica_overlap"],
        "repetitions": manifest["formal"]["repetitions"],
        "rate_burst_frames": manifest["network"]["ratefifo_burst_frames"],
    }
    for name, value in expected.items():
        if getattr(args, name) != value:
            raise ValueError(f"baseline parameter differs from frozen manifest: {name}={getattr(args, name)} expected={value}")
    manifest_rhos = [float(value) for value in manifest["network"]["rhos"]]
    supplied_rhos = [float(value) for value in args.rhos.split(",")]
    if supplied_rhos != manifest_rhos:
        raise ValueError(f"baseline rho grid differs from frozen manifest: {supplied_rhos} != {manifest_rhos}")
    supplied_policies = [value.strip() for value in args.policies.split(",") if value.strip()]
    if supplied_policies != [policy for policy in POLICY_DEFS]:
        raise ValueError("baseline policy order/content differs from frozen manifest")


class LinkRuntime:
    """Run-wide networking state: dispatcher endpoint server, agent conns,
    per-cell views.  Acts as (a) the dispatcher endpoint: a TCP server on the
    b02-4t4-net bridge IP:9711 that the gateway relay connects to, and (b) the
    four instance agents: TCP clients of the relay on 127.0.0.1:9710.  The
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
        self.update_events: list[dict] = []
        self.rate_milliframes_per_s = 0
        self.rate_burst_frames = 0
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
                if kind == K_RESET_DONE and cell == self.cell_id:
                    self.reset_done.set()
                    continue
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
                    self.dispatcher.apply_upsert(instance, name, coverage, t_send, now)
                    self.delays["upsert"].append(now - t_send)
                else:
                    self.dispatcher.apply_tombstone(instance, name)
                    self.delays["tombstone"].append(now - t_send)
                self.update_events.append({
                    "event": "dispatcher_arrival", "cell": cell, "update_id": seq,
                    "owner": instance, "prefix_digest64": digest,
                    "op": "upsert" if kind == K_UP else "tombstone",
                    "generation_time": t_send, "dispatcher_arrival_time": now,
                    "coverage_tokens": coverage, "payload_bytes": FRAME,
                })
                self.received += 1
                # feedback ack so the relay's adaptive gate sees REAL delay
                writer.write(HDR.pack(K_ACK, 0, cell, seq, 0, 0, now) + b"\x00" * 32)
        except (asyncio.IncompleteReadError, ConnectionResetError):
            pass
        finally:
            self.down_writer = None
            writer.close()

    async def configure_cell(self, dispatcher: Dispatcher, cell_id: int, digest_map: dict[int, str], policy: str,
                             global_topk: int, relay_max_inflight: int, sig_bit_per_s: int,
                             rate_burst_frames: int) -> None:
        self.dispatcher = dispatcher
        self.cell_id = cell_id
        self.digest_map = digest_map
        self.delays = {"upsert": [], "tombstone": []}
        self.sent = 0
        self.received = 0
        self.update_events = []
        self.rate_milliframes_per_s = int((sig_bit_per_s / (WIRE_BYTES_PER_MSG * 8.0)) * 1000.0)
        self.rate_burst_frames = rate_burst_frames
        self.reset_done.clear()
        self.stats_future = None
        if not self.agent_writers:
            await self.open_agents()
        flags = POLICY_DEFS[policy]
        cfg_payload = CFG.pack(
            flags["mode"], flags["merge"], flags["priority"], flags["adaptive"],
            flags["dedup"], global_topk if flags["global_topk"] else 0,
            MAX_QUEUE, relay_max_inflight, self.rate_milliframes_per_s, self.rate_burst_frames,
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
            forwarded, rate, sup, cap, util, queue_drop, expired, maxq = await asyncio.wait_for(self.stats_future, timeout=15)
        except asyncio.TimeoutError:
            return {}
        return {
            "relay_forwarded": forwarded, "relay_drop_rate_limit": rate,
            "relay_drop_superseded": sup, "relay_drop_duplicate_holder": cap,
            "relay_drop_low_utility": util, "relay_drop_queue_drop": queue_drop,
            "relay_drop_expired": expired, "relay_max_queue": maxq,
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
                   cache_salt: str, link: LinkRuntime, cell_uid: int, rate_state: dict, args: argparse.Namespace) -> tuple[dict, list[dict], list[dict]]:
    dispatcher = Dispatcher(args.j, args.prefill_tokens_per_ms, args.queue_penalty_ms, args.guard_ms)
    shadows = [ShadowCache(args.kv_cache_tokens) for _ in URLS]
    # Source shadow state and generation times define the AoI reference.
    source_truth: list[dict[str, tuple[int, float]]] = [dict() for _ in URLS]
    # Source-side state remains complete for every formal policy.  Selection
    # occurs at the shared gateway, allowing all baselines to see the same
    # generated update stream and physical link capacity.
    advertised: list[dict[str, int]] = [dict() for _ in URLS]
    upserts_generated = 0
    source_upserts_sent = 0
    source_tombstones_sent = 0
    is_ideal = policy == IDEAL_POLICY
    forced_owner_resets = 0
    state_epoch_unix = time.time()
    overlap_seed_count = int(round(args.pool_size * args.overlap))
    phase0_coverage = {request.lineage_id: request.coverage_tokens for request in trace if request.phase == 0}
    overlap_seeds = [TraceRequest(
        request_id=-(index + 1), phase=0, lineage_id=index, step=0,
        tenant=f"tenant-{index % 8}", digest=f"L{index:04d}",
        coverage_tokens=phase0_coverage.get(index, BASE_WORDS), workload=args.workload, discard=True,
    ) for index in range(overlap_seed_count)]
    digest_map = {digest64(request.digest): request.digest for request in [*trace, *overlap_seeds]}
    sig_bit = 0
    tc_before: dict = {}
    tc_mid: dict = {}
    tc_mid_done = is_ideal
    if not is_ideal:
        sig_bit = max(64, int(rate_state["offered_bit_per_s"] / rho))
        print(sh(["bash", str(NET_DIR / "cell_rate_4t4.sh"), "--sig-bit", str(sig_bit)]).strip(), flush=True)
        if bg:
            background_cmd = ["docker", "exec", "-d", "b02-gateway4t4", "iperf3", "-c", "b02-bgserver4t4", "-p", "5211", "-t", "7200"]
            if args.background_rate:
                background_cmd += ["-b", args.background_rate]
            subprocess.call(background_cmd)
            await asyncio.sleep(1.0)
        tc_before = tc_snapshot(f"{cell_tag}_before", Path(args.out_dir))
        await link.configure_cell(dispatcher, cell_uid % 60000, digest_map, policy, args.global_topk,
                                  args.relay_max_inflight, sig_bit, args.rate_burst_frames)
    def publish_state(target: int, digest: str, coverage_tokens: int) -> None:
        """Publish one actual cache-state update via the active policy."""
        nonlocal source_tombstones_sent, source_upserts_sent, upserts_generated
        generated_at = time.time()
        evicted = shadows[target].insert(digest, coverage_tokens)
        source_truth[target][digest] = (coverage_tokens, generated_at)
        for victim in evicted:
            source_truth[target].pop(victim, None)
        upserts_generated += 1
        if is_ideal:
            dispatcher.apply_upsert(target, digest, coverage_tokens, generated_at, generated_at)
            for victim in evicted:
                dispatcher.apply_tombstone(target, victim)
            return
        link.send(K_UP, target, digest, coverage_tokens)
        source_upserts_sent += 1
        advertised[target][digest] = coverage_tokens
        for victim in evicted:
            if victim in advertised[target]:
                link.send(K_TOMB, target, victim, 0)
                source_tombstones_sent += 1
                advertised[target].pop(victim, None)

    records: list[dict] = []
    started = time.perf_counter()
    mid_at = int(len(trace) * 0.75)
    async with aiohttp.ClientSession() as session:
        # E2 uses a real warm overlap: the designated base prefixes are
        # requested from every vLLM instance before the measured lineage
        # trace. Their state updates still traverse the selected signaling
        # policy, so local redundancy and gateway-level dedup are observable.
        for seed_request in overlap_seeds:
            seed_responses = await asyncio.gather(*[
                one_request(session, url, prompt_for(seed_request), cache_salt, args.output_tokens, args.max_request_attempts)
                for url in URLS
            ])
            if not all(response["ok"] for response in seed_responses):
                raise RuntimeError("overlap preseed vLLM request failed")
            for target in range(len(URLS)):
                publish_state(target, seed_request.digest, seed_request.coverage_tokens)

        async def force_owner_churn(wave_index: int) -> None:
            """Inject a physical cache reset and state invalidations in churn only.

            The reset is issued to vLLM's real developer endpoint.  Its
            tombstones subsequently use the same LinkRuntime TCP/gateway/tc
            path as ordinary state events, so a dispatcher can temporarily
            retain a stale positive while the owner has already dropped KV.
            ``advance_epoch`` represents a restart epoch without taking an
            endpoint down mid-request; the separate native matrix verifies
            the corresponding owner-side validation path on all endpoints.
            """
            nonlocal source_tombstones_sent, forced_owner_resets
            interval = args.churn_reset_every_waves
            if interval <= 0 or wave_index == 0 or wave_index % interval:
                return
            target = (wave_index // interval) % len(URLS)
            async with session.post(URLS[target] + "/reset_prefix_cache") as response:
                if response.status != 200:
                    raise RuntimeError(f"owner cache reset failed on instance {target}: HTTP {response.status}")
                await response.read()
            if args.churn_advance_epoch:
                async with session.post(URLS[target] + "/b02/native_pin/advance_epoch") as response:
                    if response.status != 200:
                        raise RuntimeError(f"owner epoch advance failed on instance {target}: HTTP {response.status}")
                    await response.read()
            stale_digests = shadows[target].reset()
            for digest in stale_digests:
                source_truth[target].pop(digest, None)
                if is_ideal:
                    dispatcher.apply_tombstone(target, digest)
                elif digest in advertised[target]:
                    link.send(K_TOMB, target, digest, 0)
                    source_tombstones_sent += 1
                    advertised[target].pop(digest, None)
            forced_owner_resets += 1

        for offset in range(0, len(trace), args.concurrency):
            await force_owner_churn(offset // args.concurrency)
            if not tc_mid_done and offset >= mid_at:
                tc_mid = tc_snapshot(f"{cell_tag}_mid", Path(args.out_dir))
                tc_mid_done = True
            wave = trace[offset:offset + args.concurrency]
            decisions = []
            freshness: dict[int, tuple] = {}
            for request in wave:
                target, raw_fanout, evaluated, affinity, coverage, expected_net = dispatcher.choose(request)
                dispatch_time_unix = time.time()
                advertised_owners = [
                    index for index, view in enumerate(dispatcher.index)
                    if view.get(request.digest, 0) > 0
                ]
                truth_coverage = max((entry[0] for source in source_truth for entry in [source.get(request.digest)] if entry), default=0)
                view_coverage = max((view.get(request.digest, 0) for view in dispatcher.index), default=0)
                known_generated_at = dispatcher.generated_at[target].get(request.digest)
                dispatch_state_age_s = (dispatch_time_unix - (known_generated_at if known_generated_at is not None else state_epoch_unix)) if truth_coverage > 0 else None
                view_missing = bool(truth_coverage > 0 and view_coverage == 0)
                source_false_negative = bool(truth_coverage >= STALE_COVERAGE_THRESHOLD and view_coverage < STALE_COVERAGE_THRESHOLD)
                target_truth_coverage = source_truth[target].get(request.digest, (0, state_epoch_unix))[0]
                source_false_positive = bool(affinity and coverage > target_truth_coverage)
                freshness[request.request_id] = (dispatch_time_unix, truth_coverage, view_coverage,
                                                  dispatch_state_age_s, view_missing, source_false_negative,
                                                  source_false_positive, advertised_owners)
                dispatcher.loads[target] += 1
                decisions.append((request, target, raw_fanout, evaluated, affinity, coverage, expected_net))
            responses = await asyncio.gather(*[
                one_request(session, URLS[target], prompt_for(request), cache_salt, args.output_tokens, args.max_request_attempts)
                for request, target, _, _, _, _, _ in decisions
            ])
            for (request, target, raw_fanout, evaluated, affinity, coverage, expected_net), response in zip(decisions, responses):
                dispatcher.loads[target] -= 1
                publish_state(target, request.digest, request.coverage_tokens)
                if request.discard:
                    continue
                physical_cached = float(response["vllm_cached_tokens"] or 0)
                (dispatch_time_unix, truth_coverage, view_coverage, dispatch_state_age_s,
                 view_missing, source_false_negative, source_false_positive,
                 advertised_owners) = freshness[request.request_id]
                stale_fallback = bool(coverage >= STALE_COVERAGE_THRESHOLD and response["ok"] and physical_cached < STALE_COVERAGE_THRESHOLD)
                if stale_fallback:
                    validation_result = "fallback_prefill"
                elif physical_cached >= STALE_COVERAGE_THRESHOLD:
                    validation_result = "cache_reused"
                else:
                    validation_result = "cache_miss"
                records.append({
                    "request_id": request.request_id,
                    "trace_id": f"{request.workload}:rep{args.current_rep}:req{request.request_id}",
                    "workload": request.workload,
                    "phase": request.phase,
                    "lineage_id": request.lineage_id,
                    "step": request.step,
                    "digest": request.digest,
                    "reusable_prefix_tokens": request.coverage_tokens,
                    "selected_instance": target,
                    "advertised_owners": ";".join(map(str, advertised_owners)),
                    "candidate_hit": affinity,
                    "dispatch_time_unix": dispatch_time_unix,
                    "source_truth_coverage_tokens": truth_coverage,
                    "dispatcher_view_coverage_tokens": view_coverage,
                    "advertised_coverage_tokens": coverage,
                    "dispatch_state_age_s": dispatch_state_age_s,
                    "dispatcher_view_missing": view_missing,
                    "source_view_false_negative": source_false_negative,
                    "source_view_false_positive": source_false_positive,
                    "physical_false_positive_affinity": bool(affinity and coverage >= STALE_COVERAGE_THRESHOLD and response["ok"] and physical_cached < STALE_COVERAGE_THRESHOLD),
                    "expected_coverage_tokens": coverage,
                    "expected_net_prefill_ms": expected_net,
                    "raw_candidate_fanout": raw_fanout,
                    "evaluated_candidate_fanout": evaluated,
                    "actual_reusable_coverage_tokens": physical_cached,
                    "validation_attempt": bool(affinity),
                    "validation_result": validation_result,
                    "fallback": stale_fallback,
                    "stale_fallback": stale_fallback,
                    "forced_owner_resets_before_request": forced_owner_resets,
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
            subprocess.call(["docker", "exec", "b02-gateway4t4", "pkill", "iperf3"])
        tc_after = tc_snapshot(f"{cell_tag}_after", Path(args.out_dir))
        up = link.delays["upsert"]
        tomb = link.delays["tombstone"]
        net_metrics = {
            "rho": rho, "sig_bit_per_s": sig_bit, "background_traffic": bg,
            "background_rate": args.background_rate if bg else "none",
            "relay_max_inflight": args.relay_max_inflight,
            "source_local_topk": False,
            "gateway_global_topk": args.global_topk if POLICY_DEFS[policy]["global_topk"] else 0,
            "source_upserts_sent": source_upserts_sent,
            "source_tombstones_sent": source_tombstones_sent,
            "forced_owner_resets": forced_owner_resets,
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
    state_ages = [float(row["dispatch_state_age_s"]) for row in records if row["dispatch_state_age_s"] is not None]
    source_unique_prefixes = len({digest for source in source_truth for digest in source})
    dispatcher_unique_prefixes = len({digest for view in dispatcher.index for digest in view})
    wire_bytes = int(net_metrics.get("net_wire_bytes_sent", 0))
    n = len(records)
    metrics = {
        "request_count": n,
        "overlap_fraction": args.overlap,
        "overlap_seed_prefixes": overlap_seed_count,
        "overlap_seed_requests": overlap_seed_count * len(URLS),
        "cell_active_s": active_s,
        "upserts_generated": upserts_generated,
        "upserts_per_s": upserts_generated / active_s if active_s > 0 else 0.0,
        "offered_wire_bit_per_s": (upserts_generated * WIRE_BYTES_PER_MSG * 8) / active_s if active_s > 0 else 0.0,
        "affinity_selection_rate": sum(bool(row["candidate_hit"]) for row in records) / n if n else 0.0,
        "dispatch_state_age_defined_count": len(state_ages),
        "dispatch_state_age_mean_s": statistics.mean(state_ages) if state_ages else 0.0,
        "dispatch_state_age_p50_s": percentile(state_ages, 50),
        "dispatch_state_age_p95_s": percentile(state_ages, 95),
        "dispatcher_view_missing_at_dispatch_rate": sum(bool(row["dispatcher_view_missing"]) for row in records) / n if n else 0.0,
        "source_view_false_negative_rate": sum(bool(row["source_view_false_negative"]) for row in records) / n if n else 0.0,
        "source_view_false_positive_rate": sum(bool(row["source_view_false_positive"]) for row in records) / n if n else 0.0,
        "physical_false_positive_affinity_rate": sum(bool(row["physical_false_positive_affinity"]) for row in records) / n if n else 0.0,
        "source_unique_prefixes_at_end": source_unique_prefixes,
        "dispatcher_unique_prefixes_at_end": dispatcher_unique_prefixes,
        "dispatcher_unique_prefixes_per_wire_byte": dispatcher_unique_prefixes / wire_bytes if wire_bytes else 0.0,
        "vllm_cached_token_rate": sum(float(row["vllm_cached_tokens"] or 0) > 0 for row in records) / n if n else 0.0,
        "vllm_cached_tokens_total": sum(float(row["vllm_cached_tokens"] or 0) for row in records),
        "vllm_cached_tokens_per_request": sum(float(row["vllm_cached_tokens"] or 0) for row in records) / n if n else 0.0,
        "stale_fallback_count": sum(bool(row["stale_fallback"]) for row in records),
        "stale_fallback_rate": sum(bool(row["stale_fallback"]) for row in records) / n if n else 0.0,
        "mean_coverage_shortfall_tokens": statistics.mean(shortfalls) if shortfalls else 0.0,
        "ttft_mean_ms": statistics.mean(ttfts) if ttfts else 0.0,
        "ttft_p50_ms": percentile(ttfts, 50),
        "ttft_p95_ms": percentile(ttfts, 95),
        "ttft_p99_ms": percentile(ttfts, 99),
        "latency_p50_ms": percentile(latencies, 50),
        "latency_p95_ms": percentile(latencies, 95),
        "throughput_requests_per_s": n / active_s if active_s > 0 else 0.0,
        "validation_attempt_count": sum(bool(row["validation_attempt"]) for row in records),
        "validation_failure_count": sum(row["validation_result"] == "fallback_prefill" for row in records),
        "fallback_count": sum(bool(row["fallback"]) for row in records),
        "incorrect_kv_reuse_count": 0,
        "retried_request_count": sum(int(row["attempt_count"]) > 1 for row in records),
        "request_error_rate": 1.0 - len(success) / n if n else 1.0,
        **net_metrics,
    }
    return metrics, records, list(link.update_events)


def paired_rows(cells: list[dict]) -> list[dict]:
    by_cell = {(row["rep"], row["policy"]): row for row in cells if row["policy"] == IDEAL_POLICY}
    out: list[dict] = []
    for rep in sorted({row["rep"] for row in cells}):
        baseline = by_cell.get((rep, IDEAL_POLICY))
        if baseline is None:
            continue
        for row in cells:
            if row["rep"] != rep or row["policy"] == IDEAL_POLICY:
                continue
            ideal_total = float(baseline["vllm_cached_tokens_total"])
            out.append({
                "rep": rep,
                "cell_id": row["cell_id"],
                "policy": row["policy"],
                "workload": row["workload"],
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
    cached_exceeds_prompt = sum(float(item.get("vllm_cached_tokens") or 0) > float(item.get("input_tokens") or 0) for item in raw)
    negative_age = sum(item.get("dispatch_state_age_s") is not None and float(item["dispatch_state_age_s"]) < 0 for item in raw)
    invalid_owner = sum(int(item["selected_instance"]) not in range(4) for item in raw)
    selected_owners = {int(item["selected_instance"]) for item in raw}
    return [
        {"check_name": "same logical request has byte-identical prompt and input token count across cells", "status": "PASS" if mismatched_inputs == 0 else "FAIL", "offending_rows": mismatched_inputs, "suggested_fix": "remove all policy data from semantic prompt"},
        {"check_name": "same logical request has fixed output token count across cells", "status": "PASS" if mismatched_outputs == 0 else "FAIL", "offending_rows": mismatched_outputs, "suggested_fix": "keep min_tokens=max_tokens and ignore_eos"},
        {"check_name": "vLLM usage telemetry present for every request", "status": "PASS" if missing_usage == 0 else "FAIL", "offending_rows": missing_usage, "suggested_fix": "restart vLLM with --enable-prompt-tokens-details"},
        {"check_name": "cached tokens never exceed prompt tokens", "status": "PASS" if cached_exceeds_prompt == 0 else "FAIL", "offending_rows": cached_exceeds_prompt, "suggested_fix": "inspect response telemetry parser"},
        {"check_name": "dispatch state age is non-negative", "status": "PASS" if negative_age == 0 else "FAIL", "offending_rows": negative_age, "suggested_fix": "inspect cross-process timestamps"},
        {"check_name": "selected owner IDs are valid", "status": "PASS" if invalid_owner == 0 else "FAIL", "offending_rows": invalid_owner, "suggested_fix": "check four-endpoint dispatcher map"},
        {"check_name": "all four instances receive measured requests", "status": "PASS" if selected_owners == {0, 1, 2, 3} else "FAIL", "offending_rows": len(set(range(4)) - selected_owners), "suggested_fix": "check load balancing and endpoint health"},
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
    updates: list[dict] = []
    # A run-specific relay-cell namespace prevents docker-log telemetry from
    # colliding with a previous smoke/calibration run.
    cell_uid = stable_int("formal4t4-relay-cell", args.tag, args.seed, args.workload) % 50000
    relay_cell_ids: set[int] = set()
    order_by_rep: dict[int, list[str]] = {}

    async def do_cell(rep: int, policy: str, rho: float | None, bg: bool, trace: list[TraceRequest], trace_hash: str, order_index: int, order_seq: str, suffix: str = "") -> dict:
        nonlocal cell_uid
        cell_uid += 1
        relay_cell_id = cell_uid % 60000
        if policy != IDEAL_POLICY:
            relay_cell_ids.add(relay_cell_id)
        cell_id = IDEAL_POLICY if policy == IDEAL_POLICY else f"{policy}@rho{rho}" + ("+bg" if bg else "") + suffix
        cache_salt = f"formal4t4:{args.workload}:{args.tag}:{cell_id}:rep{rep}"
        args.current_rep = rep
        metrics, records, cell_updates = await run_cell(trace, policy, rho, bg, f"rep{rep}_{cell_id}_{args.tag}", cache_salt, link, relay_cell_id, rate_state, args)
        if policy == IDEAL_POLICY and metrics["offered_wire_bit_per_s"] > 0:
            rate_state["offered_bit_per_s"] = metrics["offered_wire_bit_per_s"]
        row = {
            "experiment_id": f"20260726_formal4t4_{args.workload}_{cell_id}_rep{rep}",
            "experiment": "formal4t4_live_vllm_real_kernel_link",
            "evidence_type": "hybrid_live_vllm_real_kernel_link",
            "code_commit": git_commit(),
            "model": MODEL_ID,
            "hardware": f"{len(URLS)}x Tesla T4; Qwen2.5-1.5B-Instruct; one vLLM instance/GPU on host; docker gateway+tc state channel",
            "cell_id": cell_id, "relay_cell_id": relay_cell_id, "policy": policy, "workload": args.workload,
            "policy_definition": "Immediate dispatcher visibility; no signaling cost; upper bound." if policy == IDEAL_POLICY else POLICY_DEFS[policy]["definition"],
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
            "status": "VALID", **metrics,
        }
        cells.append(row)
        raw.extend({"experiment_id": row["experiment_id"], "rep": rep, "cell_id": cell_id, "policy": policy, **record} for record in records)
        updates.extend({"experiment_id": row["experiment_id"], "rep": rep, "cell_id": cell_id,
                        "policy": policy, "rho": rho, "workload": args.workload, **event}
                       for event in cell_updates)
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
        # Required end-to-end smoke: four endpoints, two real policies, one
        # rho and one repetition.  Ideal calibrates offered state load only.
        rep = 0
        trace_path = root / "raw" / "traces" / f"{args.workload}_trace_rep{rep}.csv"
        trace = make_trace(trace_path, rep, args)
        trace_hash = sha256_file(trace_path)
        order_by_rep[rep] = [IDEAL_POLICY, "FullSync@rho1.0", "Adaptive@rho1.0"]
        await do_cell(rep, IDEAL_POLICY, None, False, trace, trace_hash, 0, ",".join(order_by_rep[rep]))
        for idx, (policy, rho, bg, suffix) in enumerate([
            ("FullSync", 1.0, False, ""), ("Adaptive", 1.0, False, ""),
        ], start=1):
            await do_cell(rep, policy, rho, bg, trace, trace_hash, idx, ",".join(order_by_rep[rep]), suffix)
    else:
        rhos = [float(value) for value in args.rhos.split(",")]
        policies = [value.strip() for value in args.policies.split(",") if value.strip()]
        unknown = sorted(set(policies) - set(POLICY_DEFS))
        if unknown:
            raise ValueError(f"unknown policies: {','.join(unknown)}")
        if args.paired_background and args.background:
            raise ValueError("--paired-background and --background are mutually exclusive")
        for rep in range(args.repetitions):
            trace_path = root / "raw" / "traces" / f"{args.workload}_trace_rep{rep}.csv"
            trace = make_trace(trace_path, rep, args)
            trace_hash = sha256_file(trace_path)
            plan: list[tuple[str, float | None, bool]] = [(IDEAL_POLICY, None, False)]
            if args.paired_background:
                # For every policy/rho, execute OFF and ON against the same
                # deterministic trace.  The per-cell cache salt remains
                # distinct, whereas prompt bytes and request ordering match.
                plan += [(policy, rho, background)
                         for policy in policies for rho in rhos
                         for background in (False, True)]
            else:
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
            order_by_rep[rep] = [
                IDEAL_POLICY if policy == IDEAL_POLICY else f"{policy}@rho{rho}" + ("+bg" if background else "")
                for policy, rho, background in order
            ]
            for order_index, (policy, rho, bg) in enumerate(order):
                await do_cell(rep, policy, rho, bg, trace, trace_hash, order_index, ",".join(order_by_rep[rep]))

    return {"cells": cells, "raw": raw, "updates": updates,
            "gateway_events": gateway_update_events(relay_cell_ids), "order_by_rep": order_by_rep}


def smoke_report(cells: list[dict], checks: list[dict]) -> list[dict]:
    by_id = {row["cell_id"]: row for row in cells}
    report: list[dict] = []

    def add(name: str, passed: bool, detail: str) -> None:
        report.append({"check": name, "status": "PASS" if passed else "FAIL", "detail": detail})

    full = by_id.get("FullSync@rho1.0", {})
    adaptive = by_id.get("Adaptive@rho1.0", {})
    ideal = by_id.get(IDEAL_POLICY, {})
    add("a: four vLLM instances have live request telemetry", all(int(row.get("request_count", 0)) > 0 for row in cells),
        "; ".join(f"{row['cell_id']}={row.get('request_count', 0)} requests" for row in cells))
    add("b: real tc signaling path carries frames", int(full.get("tc_class110_sent_bytes_after", 0)) > 0 and int(full.get("net_msgs_sent", 0)) > 0,
        f"class1:10={full.get('tc_class110_sent_bytes_after', 0)}B sent={full.get('net_msgs_sent', 0)}")
    add("c: ideal bypasses the network", not ideal.get("ad_delivery_delay_p95_s") and float(ideal.get("upserts_per_s", 0)) > 0,
        f"ideal network delay field is absent/empty, upserts_per_s={ideal.get('upserts_per_s')}")
    add("d: integrity checks", all(row["status"] == "PASS" for row in checks),
        "; ".join(f"{row['check_name']}={row['status']}" for row in checks))
    add("e: policy-specific state telemetry is complete", int(adaptive.get("net_msgs_sent", 0)) >= int(adaptive.get("net_msgs_delivered", 0)),
        f"Adaptive sent={adaptive.get('net_msgs_sent', 0)} delivered={adaptive.get('net_msgs_delivered', 0)}")
    return report


def main() -> None:
    global URLS
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", default="/home/byh/B02/analysis/formal4t4")
    parser.add_argument("--tag", default="formal")
    parser.add_argument("--seed", type=int, default=20260726)
    parser.add_argument("--repetitions", type=int, default=5)
    parser.add_argument("--instances", type=int, default=4, help="must be four local vLLM endpoints on ports 8000--8003")
    parser.add_argument("--n-requests", type=int, default=120)
    parser.add_argument("--warmup", type=int, default=24)
    parser.add_argument("--pool-size", type=int, default=64)
    parser.add_argument("--overlap", type=float, default=0.25,
                        help="fraction of phase-0 lineages physically seeded on all instances")
    parser.add_argument("--workload", choices=["original_compatible", "reuse_intensive"], default="reuse_intensive")
    parser.add_argument("--stage", choices=["smoke", "calibration", "baseline", "dynamic", "churn", "background"], default="baseline")
    parser.add_argument("--alpha", type=float, default=1.2)
    parser.add_argument("--steps", type=int, default=3)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--output-tokens", type=int, default=4)
    parser.add_argument("--kv-cache-tokens", type=int, required=True)
    parser.add_argument("--rhos", default="0.5,0.8,1.0,1.2")
    parser.add_argument("--policies", default=",".join(DEFAULT_LINK_POLICIES),
                        help="comma-separated link policies; includes gateway ablations")
    parser.add_argument("--global-topk", type=int, default=16,
                        help="Adaptive's maximum distinct useful prefixes retained before congestion tightening")
    parser.add_argument("--relay-max-inflight", type=int, default=4,
                        help="shared ACK-delimited gateway frame window for every real policy")
    parser.add_argument("--rate-burst-frames", type=int, default=4,
                        help="RateFIFO token bucket burst; calibration selects the frozen formal value")
    parser.add_argument("--background", action="store_true",
                        help="run every non-ideal cell with saturating iperf3 traffic")
    parser.add_argument("--background-rate", default="",
                        help="optional iperf3 TCP pacing rate such as 1000 or 10K; empty means saturating background")
    parser.add_argument("--paired-background", action="store_true",
                        help="for every policy/rho, run matched background OFF/ON cells on the same trace")
    parser.add_argument("--churn-reset-every-waves", type=int, default=0,
                        help="stage=churn only: reset one owner prefix cache every N dispatch waves")
    parser.add_argument("--churn-advance-epoch", action="store_true",
                        help="stage=churn only: pair reset injection with the native owner restart-epoch transition")
    parser.add_argument("--initial-offered-bit-per-s", type=float, default=1248.0,
                        help="used until the first ideal cell measures the real offered rate")
    parser.add_argument("--j", type=int, default=4)
    parser.add_argument("--topk", type=int, default=8)
    parser.add_argument("--prefill-tokens-per-ms", type=float, default=50.0)
    parser.add_argument("--queue-penalty-ms", type=float, default=2.0)
    parser.add_argument("--guard-ms", type=float, default=0.5)
    parser.add_argument("--max-request-attempts", type=int, default=3)
    parser.add_argument("--cooldown-s", type=float, default=1.0)
    parser.add_argument("--frozen-manifest", default="", help="path to a manifest whose SHA-256 is recorded with this run")
    parser.add_argument("--smoke", action="store_true")
    args = parser.parse_args()
    if args.instances != 4:
        raise ValueError("formal4t4 requires exactly four vLLM instances")
    URLS = [f"http://127.0.0.1:{8000 + index}" for index in range(args.instances)]
    if args.smoke:
        args.repetitions = 1
        args.n_requests = 32
        args.warmup = 12
        args.tag = args.tag if args.tag != "formal" else "smoke"
        args.stage = "smoke"
    if args.n_requests <= args.warmup or args.output_tokens < 1:
        raise ValueError("need measured requests and positive output tokens")
    if not 0.0 <= args.overlap <= 1.0:
        raise ValueError("overlap must be in [0, 1]")
    if args.relay_max_inflight < 0:
        raise ValueError("relay_max_inflight must be nonnegative")
    if args.rate_burst_frames < 1:
        raise ValueError("rate-burst-frames must be positive")
    if args.churn_reset_every_waves < 0:
        raise ValueError("churn-reset-every-waves must be nonnegative")
    if (args.churn_reset_every_waves or args.churn_advance_epoch) and args.stage != "churn":
        raise ValueError("churn reset/epoch injection is permitted only for stage=churn")
    if args.frozen_manifest and not Path(args.frozen_manifest).is_file():
        raise ValueError(f"frozen manifest does not exist: {args.frozen_manifest}")
    enforce_frozen_manifest(args)
    started = time.time()
    result = asyncio.run(run(args))
    cells, raw, updates, gateway_events = result["cells"], result["raw"], result["updates"], result["gateway_events"]
    if any(float(row["request_error_rate"]) > 0 for row in cells):
        raise RuntimeError("live request error observed; do not use this run")
    checks = validate_records(raw, args.output_tokens)
    nonideal = [row for row in cells if row["policy"] != IDEAL_POLICY]
    generated_lt_forwarded = sum(
        int(row.get("upserts_generated", 0)) + int(row.get("source_tombstones_sent", 0)) < int(row.get("net_msgs_delivered", 0))
        for row in nonideal
    )
    inactive_tc = sum(int(row.get("tc_class110_sent_bytes_after", 0)) <= 0 for row in nonideal)
    checks.extend([
        {"check_name": "generated state frames cover all delivered frames", "status": "PASS" if generated_lt_forwarded == 0 else "FAIL", "offending_rows": generated_lt_forwarded, "suggested_fix": "inspect relay cell reset or frame counters"},
        {"check_name": "tc shaping path carried signaling bytes", "status": "PASS" if inactive_tc == 0 else "FAIL", "offending_rows": inactive_tc, "suggested_fix": "inspect b02-gateway4t4 HTB setup"},
    ])
    if any(row["status"] != "PASS" for row in checks):
        raise RuntimeError(f"live comparability checks failed: {checks}")
    root = Path(args.out_dir)
    raw_dir, summary_dir = root / "raw" / args.stage, root / "summary"
    write_csv(raw_dir / f"requests_{args.tag}.csv", raw)
    write_csv(raw_dir / f"updates_{args.tag}.csv", updates)
    write_csv(raw_dir / f"gateway_events_{args.tag}.csv", gateway_events)
    write_csv(summary_dir / f"cells_{args.tag}.csv", cells)
    write_csv(summary_dir / f"pairs_{args.tag}.csv", paired_rows(cells))
    write_csv(summary_dir / f"sanity_checks_{args.tag}.csv", checks)
    metadata = {
        "started_at_unix": started, "finished_at_unix": time.time(),
        "duration_s": time.time() - started, "arguments": vars(args),
        "cell_order_by_rep": {str(rep): order for rep, order in result["order_by_rep"].items()},
        "cells": len(cells), "raw_requests": len(raw), "update_events": len(updates),
        "gateway_update_events": len(gateway_events),
        "frozen_manifest": args.frozen_manifest,
        "frozen_manifest_sha256": sha256_file(Path(args.frozen_manifest)) if args.frozen_manifest else "",
    }
    (raw_dir / f"run_metadata_{args.tag}.json").write_text(json.dumps(metadata, indent=2))
    if args.smoke:
        report = smoke_report(cells, checks)
        write_csv(summary_dir / f"smoke_report_{args.tag}.csv", report)
        print("SMOKE_4T4_REPORT " + json.dumps(report, indent=2))
        if any(row["status"] != "PASS" for row in report):
            raise SystemExit("smoke checks FAILED")
    print(json.dumps(metadata, indent=2))


if __name__ == "__main__":
    main()
