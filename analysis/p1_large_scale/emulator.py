"""P1 trace-driven emulation.

The gateway admission and dequeue selection are the process-local Relay from
experiments/4t4/net/gateway_relay_4t4.py. This module swaps that module's
32-byte header for a 16-bit instance field inside this process only.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import random
import resource
import struct
import sys
import time
from collections import defaultdict, deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path("/home/byh/B02")
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


gw = _load("p1_gateway_relay_4t4", ROOT / "experiments/4t4/net/gateway_relay_4t4.py")
_formal = _load("p1_run_formal4t4", ROOT / "experiments/4t4/run_formal4t4.py")
ShadowCache = _formal.ShadowCache
digest64 = _formal.digest64
make_trace = _formal.make_trace

# Measured FullSync drain at a configured 10 Mbit/s HTB, commit a8a07b7.
DRAIN_FRAMES_PER_S_AT_10M = 9407.55
LINK_10M_BPS = 10_000_000
WIRE_BYTES = 104
BFIFO_BYTES = 65536
KV_CACHE_TOKENS = 104544
POOL = 64
N_REQUESTS = 120
WARMUP = 24
OVERLAP = 0.25
UPSERT_RATE_PER_GROUP = 1.071
N_WAVES = 46  # 16 overlap waves + 30 request waves
WAVE_GAP_S = (184 / UPSERT_RATE_PER_GROUP) / N_WAVES
BURST_GAP_S = 0.01
PUBLISH_LAG_S = 0.05
DRAIN_S = 5.0
STALE = 512
CELL = 1
ORIGIN = 10_000_000.0
WIDE = struct.Struct(">BHBIqQd")
assert WIDE.size == 32

GATEWAY_FILE = ROOT / "experiments/4t4/net/gateway_relay_4t4.py"


def os_pid() -> int:
    return __import__("os").getpid()


def gateway_sha256() -> str:
    digest = hashlib.sha256()
    digest.update(GATEWAY_FILE.read_bytes())
    return digest.hexdigest()


def wide_frame(kind: int, instance: int, cell: int, seq: int, coverage: int, digest: int, t: float, payload: bytes = b"") -> bytes:
    if not 0 <= int(instance) <= 65535:
        raise ValueError(f"instance {instance} does not fit in the isolated u16 field")
    if not 0 <= int(cell) <= 255:
        raise ValueError(f"cell {cell} does not fit in the isolated u8 field")
    return WIDE.pack(kind, int(instance), int(cell), int(seq) & 0xFFFFFFFF, int(coverage), int(digest), float(t)) + payload.ljust(32, b"\x00")[:32]


class Clock:
    def __init__(self) -> None:
        self.now = ORIGIN

    def time(self) -> float:
        return self.now

    def monotonic(self) -> float:
        return self.now


@contextmanager
def isolated_wire():
    """Point the imported Relay at a 16-bit instance header and a virtual clock."""
    clock = Clock()
    saved = (gw.time.time, gw.time.monotonic, gw.HDR, gw.frame)
    gw.time.time = clock.time
    gw.time.monotonic = clock.monotonic
    gw.HDR = WIDE
    gw.frame = wide_frame
    try:
        yield clock
    finally:
        gw.time.time, gw.time.monotonic, gw.HDR, gw.frame = saved


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((p / 100.0) * (len(ordered) - 1))))
    return float(ordered[index])


def service_fps_for_mbps(mbps: float) -> float:
    return DRAIN_FRAMES_PER_S_AT_10M * (mbps / 10.0)


@dataclass
class Source:
    t: float
    seq: int
    kind: int
    worker: int
    digest: int
    coverage: int
    last_upsert: bool = False


@dataclass
class Ask:
    t: float
    request_id: int
    group: int
    lineage_id: int
    digest: int
    coverage: int
    scored: bool


@dataclass
class Trace:
    sources: list[Source]
    asks: list[Ask]
    horizon_s: float
    upserts: int
    tombs: int
    groups: int

    @property
    def frames(self) -> int:
        return self.upserts + self.tombs

    @property
    def offered_fps(self) -> float:
        span = self.horizon_s if self.horizon_s > 0 else 1e-9
        return self.frames / span


def _formal_namespace(seed: int) -> argparse.Namespace:
    return argparse.Namespace(
        seed=seed,
        workload="reuse_intensive",
        n_requests=N_REQUESTS,
        pool_size=POOL,
        alpha=1.2,
        steps=3,
        warmup=WARMUP,
    )


class _Picker:
    def __init__(self, workers: list[int]) -> None:
        self.workers = workers
        self.load = {worker: 0 for worker in workers}
        self.rr = 0

    def pick(self, candidates: list[int]) -> int:
        minimum = min(self.load[worker] for worker in candidates)
        ties = [worker for worker in candidates if self.load[worker] == minimum]
        choice = ties[self.rr % len(ties)]
        self.rr += 1
        return choice


def build_trace(n_workers: int, seed: int, *, heterogeneous: bool, trace_dir: Path) -> Trace:
    if n_workers < 4 or n_workers % 4:
        raise ValueError("worker count must be a positive multiple of 4")
    groups = n_workers // 4
    trace_dir.mkdir(parents=True, exist_ok=True)
    base = make_trace(trace_dir / f"base_seed{seed}_{os_pid()}.csv", 0, _formal_namespace(seed))
    overlap_n = int(round(POOL * OVERLAP))
    phase0 = {request.lineage_id: request.coverage_tokens for request in base if request.phase == 0}
    rng = random.Random(seed)
    if groups == 1 or not heterogeneous:
        scales = [1.0] * groups
        offsets = [0.0] * groups
    else:
        scales = [rng.uniform(0.85, 1.15) for _ in range(groups)]
        offsets = [rng.uniform(0.0, WAVE_GAP_S) for _ in range(groups)]
    sources: list[Source] = []
    asks: list[Ask] = []
    seq = 1

    def add_source(t: float, kind: int, worker: int, digest: int, coverage: int) -> None:
        nonlocal seq
        sources.append(Source(t, seq, kind, worker, digest, coverage))
        seq += 1

    for group in range(groups):
        scale = scales[group]
        offset = offsets[group]
        workers = [group * 4 + slot for slot in range(4)]
        picker = _Picker(workers)
        shadows = {worker: ShadowCache(KV_CACHE_TOKENS) for worker in workers}
        held: dict[int, dict[int, int]] = {worker: {} for worker in workers}

        def choose(digest: int) -> int:
            owners = [(worker, held[worker].get(digest, 0)) for worker in workers if held[worker].get(digest, 0) > 0]
            if owners:
                best = max(coverage for _worker, coverage in owners)
                return picker.pick([worker for worker, coverage in owners if coverage == best])
            return picker.pick(workers)

        def publish(t: float, worker: int, text: str, coverage: int) -> None:
            digest = digest64(text)
            evicted = shadows[worker].insert(text, coverage)
            held[worker][digest] = coverage
            add_source(t, gw.K_UP, worker, digest, coverage)
            for victim in evicted:
                victim_digest = digest64(victim)
                held[worker].pop(victim_digest, None)
                add_source(t, gw.K_TOMB, worker, victim_digest, 0)

        for wave, lineage in enumerate(range(overlap_n)):
            text = f"G{group:04d}-L{lineage:04d}"
            coverage = phase0.get(lineage, 2048)
            t0 = offset + wave * WAVE_GAP_S * scale
            for slot, worker in enumerate(workers):
                publish(t0 + slot * BURST_GAP_S, worker, text, coverage)

        for wave_index, start in enumerate(range(0, len(base), 4)):
            wave = base[start:start + 4]
            t0 = offset + (overlap_n + wave_index) * WAVE_GAP_S * scale
            planned: list[tuple[object, int]] = []
            for request in wave:
                text = f"G{group:04d}-{request.digest}"
                digest = digest64(text)
                owner = choose(digest)
                picker.load[owner] += 1
                planned.append((request, owner))
                asks.append(Ask(
                    t0, group * N_REQUESTS + request.request_id, group, request.lineage_id,
                    digest, request.coverage_tokens, not request.discard,
                ))
            for slot, (request, owner) in enumerate(planned):
                text = f"G{group:04d}-{request.digest}"
                publish(t0 + PUBLISH_LAG_S + slot * BURST_GAP_S, owner, text, request.coverage_tokens)
                picker.load[owner] -= 1

    if sources:
        t0 = min(item.t for item in sources)
        for item in sources:
            item.t -= t0
        for item in asks:
            item.t -= t0
    last: dict[tuple[int, int], int] = {}
    for item in sources:
        if item.kind == gw.K_UP:
            last[(item.worker, item.digest)] = item.seq
    for item in sources:
        item.last_upsert = item.kind == gw.K_UP and last.get((item.worker, item.digest)) == item.seq
    sources.sort(key=lambda item: (item.t, item.seq))
    asks.sort(key=lambda item: (item.t, item.request_id))
    horizon = max((item.t for item in sources), default=0.0)
    upserts = sum(item.kind == gw.K_UP for item in sources)
    tombs = sum(item.kind == gw.K_TOMB for item in sources)
    return Trace(sources, asks, horizon, upserts, tombs, groups)


def _relay(max_queue: int) -> gw.Relay:
    args = argparse.Namespace(
        max_queue=max_queue,
        tau=30.0,
        util_lambda=16.0,
        gate=2.0,
        adaptive_queue_gate=8,
        congestion_hold=0.2,
        congestion_exit_hold=0.5,
        adaptive_queue_exit=2,
        quiet=True,
        sndbuf=2304,
    )
    return gw.Relay(args)


def configure(relay: gw.Relay, policy: str, k: int, service_fps: float) -> None:
    """Tuples match wire.py and run_formal4t4.POLICY_DEFS. KVSelect is StaticTopK."""
    table = {
        "FullSync": (gw.MODE_FULLSYNC, 0, 0, 0, 0, 0, 4096),
        "RateFIFO": (gw.MODE_RATEFIFO, 0, 0, 0, 0, 0, 4096),
        "LatestOnly": (gw.MODE_LATEST, 1, 0, 0, 0, 0, 4096),
        "AgeCov-Greedy": (gw.MODE_AGECOV, 0, 0, 0, 0, 0, 4096),
        "KVSelect": (gw.MODE_STATIC, 1, 1, 0, 2, k, 4096),
        "BoundedFIFO": (gw.MODE_BOUNDED, 0, 0, 0, 0, 0, k),
        "BoundedPrio": (gw.MODE_BOUNDED, 0, 1, 0, 0, 0, k),
        "BoundedSemantic": (gw.MODE_BOUNDED, 1, 1, 0, 2, 0, k),
    }
    mode, merge, priority, adaptive, dedup, topk, max_queue = table[policy]
    relay.mode = mode
    relay.merge = bool(merge)
    relay.priority = bool(priority)
    relay.adaptive = bool(adaptive)
    relay.dedup = dedup
    relay.global_topk = topk
    relay.max_queue = max_queue
    relay.max_inflight = 0
    relay.rate_frames_per_s = service_fps if policy == "RateFIFO" else 0.0
    relay.rate_burst_frames = 1
    relay.do_reset(CELL)


@dataclass
class Index:
    coverage: dict[tuple[int, int], int] = field(default_factory=dict)
    generated_at: dict[tuple[int, int], float] = field(default_factory=dict)
    by_digest: dict[int, dict[int, int]] = field(default_factory=lambda: defaultdict(dict))

    def apply(self, kind: int, worker: int, digest: int, coverage: int, generated_at: float) -> None:
        key = (worker, digest)
        if kind == gw.K_UP:
            self.coverage[key] = coverage
            self.generated_at[key] = generated_at
            self.by_digest[digest][worker] = coverage
        else:
            self.coverage.pop(key, None)
            self.generated_at.pop(key, None)
            holders = self.by_digest.get(digest)
            if holders is not None:
                holders.pop(worker, None)
                if not holders:
                    self.by_digest.pop(digest, None)


def _balance(relay: gw.Relay) -> int:
    return (
        relay.received + relay.dominance_tombstones
        - relay.forwarded - sum(relay.drops.values()) - relay.live_depth()
    )


def run_replay(trace: Trace, policy: str, k: int, service_fps: float) -> dict:
    if service_fps <= 0:
        raise ValueError("service rate must be positive")
    cpu0 = time.process_time()
    wall0 = time.perf_counter()
    with isolated_wire() as clock:
        relay = _relay(4096)
        configure(relay, policy, k, service_fps)
        truth = Index()
        view = Index()
        frame_service = 1.0 / service_fps
        inflight: deque[tuple[float, int, int, int, int, int, float]] = deque()
        inflight_bytes = 0
        next_tx = clock.now
        applied: dict[int, float] = {}
        queue_samples: list[int] = []
        routing: list[dict] = []
        n_workers = trace.groups * 4
        rr = 0
        state_epoch = clock.now
        if n_workers <= 0:
            raise RuntimeError("empty worker set")

        def deliver_due() -> None:
            nonlocal inflight_bytes
            while inflight and inflight[0][0] <= clock.now + 1e-12:
                done, kind, worker, seq, coverage, digest, generated_at = inflight.popleft()
                inflight_bytes -= WIRE_BYTES
                view.apply(kind, worker, digest, coverage, generated_at)
                applied[seq] = done

        def release() -> None:
            nonlocal inflight_bytes, next_tx
            while inflight_bytes + WIRE_BYTES <= BFIFO_BYTES:
                if not relay.pqueue and not relay.queue:
                    return
                data = relay._dequeue()
                if data is None:
                    return
                kind, worker, _cell, seq, coverage, digest, generated_at = gw.HDR.unpack(data[:32])
                if kind == gw.K_UP and relay.low_utility(coverage, generated_at):
                    relay.drops["low_utility"] += 1
                    relay._forget_replica(digest, worker)
                    continue
                start = max(clock.now, next_tx)
                done = start + frame_service
                next_tx = done
                inflight_bytes += WIRE_BYTES
                inflight.append((done, kind, worker, seq, coverage, digest, generated_at))
                relay.forwarded += 1
                if kind == gw.K_TOMB:
                    relay._forget_replica(digest, worker)

        def advance_to(target: float) -> None:
            guard = 0
            while clock.now < target - 1e-12:
                guard += 1
                if guard > 8_000_000:
                    raise RuntimeError("virtual clock did not advance")
                deliver_due()
                release()
                if inflight and inflight[0][0] > clock.now + 1e-15:
                    clock.now = min(target, inflight[0][0])
                else:
                    clock.now = target
            deliver_due()
            release()

        source_i = 0
        ask_i = 0
        while source_i < len(trace.sources) or ask_i < len(trace.asks):
            source_t = trace.sources[source_i].t if source_i < len(trace.sources) else float("inf")
            ask_t = trace.asks[ask_i].t if ask_i < len(trace.asks) else float("inf")
            if ask_t <= source_t:
                ask = trace.asks[ask_i]
                ask_i += 1
                advance_to(ORIGIN + ask.t)
                holders = truth.by_digest.get(ask.digest, {})
                truth_max = max(holders.values(), default=0)
                view_holders = view.by_digest.get(ask.digest, {})
                visible = {worker: coverage for worker, coverage in view_holders.items() if coverage > 0}
                if visible:
                    best = max(visible.values())
                    ties = [worker for worker, coverage in visible.items() if coverage == best]
                    routed = ties[rr % len(ties)]
                else:
                    routed = rr % n_workers
                rr += 1
                routed_truth = holders.get(routed, 0)
                view_max = max(view_holders.values(), default=0)
                view_routed = view_holders.get(routed, 0)
                usable = truth_max >= STALE
                generated = view.generated_at.get((routed, ask.digest))
                if truth_max > 0:
                    state_age = clock.now - (generated if generated is not None else state_epoch)
                else:
                    state_age = None
                if ask.scored:
                    routing.append({
                        "request_id": ask.request_id,
                        "group": ask.group,
                        "lineage_id": ask.lineage_id,
                        "routed": routed,
                        "truth_max": truth_max,
                        "routed_truth": routed_truth,
                        "view_max": view_max,
                        "decision_fn": int(usable and routed_truth < STALE),
                        "loose_fn": int(usable and view_max < STALE),
                        "view_missing": int(truth_max > 0 and view_max == 0),
                        "stale_positive": int(view_routed >= STALE and view_routed > routed_truth),
                        "state_age_s": "" if state_age is None else f"{state_age:.6f}",
                        "reusable_tokens": routed_truth,
                        "queue_depth": relay.live_depth(),
                    })
                    queue_samples.append(relay.live_depth())
            else:
                item = trace.sources[source_i]
                source_i += 1
                advance_to(ORIGIN + item.t)
                truth.apply(item.kind, item.worker, item.digest, item.coverage, clock.now)
                data = gw.frame(item.kind, item.worker, CELL, item.seq, item.coverage, item.digest, clock.now)
                relay.enqueue(data, item.kind, item.worker, item.seq, item.coverage, item.digest, clock.now)
                release()

        advance_to(clock.now + DRAIN_S)
        while inflight:
            clock.now = inflight[0][0]
            deliver_due()
        drain_end = clock.now

        last_delays: list[float] = []
        last_undelivered = 0
        last_total = 0
        for item in trace.sources:
            if not item.last_upsert:
                continue
            last_total += 1
            applied_at = applied.get(item.seq)
            if applied_at is None:
                last_undelivered += 1
                last_delays.append(drain_end - (ORIGIN + item.t))
            else:
                last_delays.append(applied_at - (ORIGIN + item.t))
        delivered_delays = [
            applied[item.seq] - (ORIGIN + item.t)
            for item in trace.sources
            if item.kind == gw.K_UP and item.seq in applied
        ]
        first_usable_gen: dict[int, float] = {}
        first_usable_apply: dict[int, float] = {}
        for item in trace.sources:
            if item.kind != gw.K_UP or item.coverage < STALE:
                continue
            first_usable_gen.setdefault(item.digest, item.t)
            applied_at = applied.get(item.seq)
            if applied_at is not None:
                previous = first_usable_apply.get(item.digest)
                if previous is None or applied_at < previous:
                    first_usable_apply[item.digest] = applied_at
        digest_delays: list[float] = []
        digest_delivered_delays: list[float] = []
        digest_undelivered = 0
        for digest, gen_t in first_usable_gen.items():
            applied_at = first_usable_apply.get(digest)
            if applied_at is None:
                digest_undelivered += 1
                digest_delays.append(drain_end - (ORIGIN + gen_t))
            else:
                delay = applied_at - (ORIGIN + gen_t)
                digest_delays.append(delay)
                digest_delivered_delays.append(delay)
        ages = [float(row["state_age_s"]) for row in routing if row["state_age_s"] != ""]
        reusable = [row["reusable_tokens"] for row in routing]
        den = len(routing)
        balance = _balance(relay)
        summary = {
            "policy": policy,
            "k": k,
            "implementation": "StaticTopK" if policy == "KVSelect" else policy,
            "service_fps": service_fps,
            "offered_fps": trace.offered_fps,
            "rho_observed": trace.offered_fps / service_fps,
            "horizon_s": trace.horizon_s,
            "upserts_generated": trace.upserts,
            "tombs_generated": trace.tombs,
            "frames_generated": trace.frames,
            "relay_received": relay.received,
            "relay_forwarded": relay.forwarded,
            "relay_max_queue": relay.maxq,
            "relay_live_depth_end": relay.live_depth(),
            "bfifo_bytes_end": inflight_bytes,
            "drops": {key: int(value) for key, value in relay.drops.items()},
            "dominance_tombstones": relay.dominance_tombstones,
            "balance": balance,
            "balance_ok": balance == 0,
            "applied_source_frames": sum(1 for item in trace.sources if item.seq in applied),
            "upsert_delivered": len(delivered_delays),
            "upsert_delivered_p95_s": percentile(delivered_delays, 95),
            "upsert_last_total": last_total,
            "upsert_last_undelivered": last_undelivered,
            "upsert_last_delivery_ratio": (last_total - last_undelivered) / last_total if last_total else None,
            "upsert_last_p95_censored_s": percentile(last_delays, 95),
            "upsert_last_p50_censored_s": percentile(last_delays, 50),
            "digest_usable_total": len(first_usable_gen),
            "digest_usable_undelivered": digest_undelivered,
            "digest_p95_censored_s": percentile(digest_delays, 95),
            "digest_p50_censored_s": percentile(digest_delays, 50),
            "digest_p95_delivered_s": percentile(digest_delivered_delays, 95),
            "requests_scored": den,
            "decision_fn": sum(row["decision_fn"] for row in routing),
            "loose_fn": sum(row["loose_fn"] for row in routing),
            "view_missing": sum(row["view_missing"] for row in routing),
            "stale_positive": sum(row["stale_positive"] for row in routing),
            "decision_fn_rate": sum(row["decision_fn"] for row in routing) / den if den else None,
            "loose_fn_rate": sum(row["loose_fn"] for row in routing) / den if den else None,
            "view_missing_rate": sum(row["view_missing"] for row in routing) / den if den else None,
            "stale_positive_rate": sum(row["stale_positive"] for row in routing) / den if den else None,
            "state_age_p95_s": percentile(ages, 95),
            "reusable_tokens_mean": (sum(reusable) / len(reusable)) if reusable else None,
            "queue_p95": percentile([float(value) for value in queue_samples], 95),
            "gateway_cpu_s": time.process_time() - cpu0,
            "wall_s": time.perf_counter() - wall0,
            "rss_kb": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            "drain_end_s": drain_end - ORIGIN,
        }
        return {"summary": summary, "routing": routing}


def stock_dispatcher_matches_small_workers() -> bool:
    """workers 0-3 follow runtime.Dispatcher.on_frame. Worker 20 is kept only by P1."""
    from experiments.icc_kv.runtime import Dispatcher, K_TOMB, K_UP

    stock = Dispatcher(cell=1)
    now = 10.0
    stock.on_frame(K_UP, 1, 1, 2048, 99, 9.0, now)
    stock.on_frame(K_UP, 1, 2, 1024, 99, 9.5, now)
    stock.on_frame(K_TOMB, 1, 3, 0, 99, 9.6, now)
    stock.on_frame(K_UP, 20, 4, 4096, 7, 9.7, now)
    local = Index()
    local.apply(K_UP, 1, 99, 2048, 9.0)
    local.apply(K_UP, 1, 99, 1024, 9.5)
    local.apply(K_TOMB, 1, 99, 0, 9.6)
    local.apply(K_UP, 20, 7, 4096, 9.7)
    return stock.snapshot() == {} and local.coverage == {(20, 7): 4096} and stock.noise_frames == 1
