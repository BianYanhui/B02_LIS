#!/usr/bin/env python3
"""Gateway relay for the isolated B02 4xT4 baseline experiment.

Runs INSIDE the `gateway` docker container (alpine python3, stdlib only).
All state-channel traffic traverses REAL kernel networking:

  instance agents (host harness) --TCP--> :9700 (this relay)
      --> optional mechanisms --> internal FIFO --> --TCP--> dispatcher
      endpoint (host harness, bridge IP:9701)

The container's eth0 egress is shaped by tc HTB (see setup_net.sh), so the
kernel sets the service rate and holds real backlog (visible in `tc -s`).
The relay's own FIFO is the pre-kernel queue that a real pre-link aggregator
controls; this is where merge/dedup/adaptive drop.  TCP backpressure is
real: the downstream socket has SO_SNDBUF pinned and the asyncio transport
high-water mark set low, so the release loop blocks exactly when the kernel
refuses more bytes.

Wire format: fixed 64-byte binary frames.  The relay is deliberately separate
from ``shared_link_exp/net/gateway_relay.py`` so the frozen legacy experiments
are never changed by this baseline study.

  header (32B, big-endian ">BBHIqQd"):
    kind u8 | instance u8 | cell u16 | seq u32 | coverage i64 |
    digest64 u64 | t_send f64 (wall clock; same host => shared clock)
  payload (32B): kind-specific
    config (kind 5): ">BBBBHIIIII" = mode, merge, priority, adaptive, dedup,
                               global_topk, max_queue, max_inflight,
                               rate_milliframes_per_s, rate_burst_frames
    stats  (kind 7): ">IIIIIIII" = forwarded, rate_limit, superseded,
                     duplicate_holder, low_utility, queue_drop, expired,
                     max_queue_depth
    ack    (kind 6): header.seq = acked seq, header.t_send = receiver wall time

kinds: 1 upsert, 2 tombstone   (agent -> relay -> dispatcher)
       3 reset, 4 stats_request, 5 config   (agent -> relay)
       6 ack                                 (dispatcher -> relay)
       7 stats, 8 reset_done                (relay -> agent control channel)

Mechanisms (set per cell via a config frame; passthrough = all off):
  FullSync: all events traverse FIFO; physical tc is the only rate limit.
  RateFIFO: token-bucket admission at the physical signaling rate; it has no
            state semantics.
  LatestOnly: a newer upsert cancels a queued unsent older upsert for the
              same (instance,digest); a tombstone cancels a queued upsert.
  AgeCov-Greedy: selects the pending update with greatest
            ``age_s * max(coverage_tokens, 1) / FRAME``; tombstones receive
            no priority lane or hand-tuned urgency.
  StaticSemantic: LatestOnly plus a tombstone priority lane and replica cap.
  Adaptive: StaticSemantic plus congestion-aware utility admission and a
            dynamically tightened global coverage set.
  --priority: tombstones go to a priority lane released first (non-preemptive).
  --dedup N:  replica cap: at most N instances may hold queued-or-forwarded
              upserts per digest (drop excess). Instance id is not an
              exemption. The replica map forgets single-holder digests once
              it passes 65536 entries, so a stream of unique noise cannot
              grow it without a drop decision. A digest held by two or more
              instances stays, and the cap still binds.
  --global-topk K: retain only the K highest-coverage distinct prefixes in
              the unsent cross-instance queue. Congestion shrinks K by 4.
              Instance id is not a special case: a short prefix is trimmed
              whether or not the router can place on that instance.
  --adaptive: utility gate: drop an upsert when congestion is sustained.
              Delay congestion is EWMA delivery delay Dq above --gate.
              Queue congestion is the pre-link queue staying at or above
              --adaptive-queue-gate for --congestion-hold seconds, so one
              send burst cannot open the gate by itself. The drop test is
              U = exp(-(age+Dq)/tau)*coverage - lambda*FRAME <= 0.
              With lambda=16 and a 64-byte frame the bar is 1024 tokens.
  --max-inflight: optional shared application-layer frame window.  It is
              applied to every policy equally, preserving an unsent gateway
              queue where semantic selection can still replace stale updates
              before they enter the real TCP/tc path.
Every update transition is emitted as a JSON line on the container log.  The
harness combines these events with dispatcher-arrival events into the required
per-update telemetry table.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import socket
import struct
import time
from collections import Counter, defaultdict, deque

FRAME = 64
# e2e background replay sends worker_id + 16. Routing only considers 0-3,
# so those frames must not take a replica-cap slot away from a real worker.
BACKGROUND_INSTANCE = 16
HDR = struct.Struct(">BBHIqQd")
CFG = struct.Struct(">BBBBHIIIII")
STATS = struct.Struct(">IIIIIIII")
K_UP, K_TOMB, K_RESET, K_STATS_REQ, K_CONFIG, K_ACK, K_STATS, K_RESET_DONE = 1, 2, 3, 4, 5, 6, 7, 8
RECENT_KEEP = 4096
MODE_FULLSYNC, MODE_RATEFIFO, MODE_LATEST, MODE_AGECOV, MODE_STATIC, MODE_ADAPTIVE = range(6)
MODE_NAMES = {
    MODE_FULLSYNC: "FullSync", MODE_RATEFIFO: "RateFIFO", MODE_LATEST: "LatestOnly",
    MODE_AGECOV: "AgeCov-Greedy", MODE_STATIC: "StaticSemantic", MODE_ADAPTIVE: "Adaptive",
}


def frame(kind: int, instance: int, cell: int, seq: int, coverage: int, digest: int, t: float, payload: bytes = b"") -> bytes:
    return HDR.pack(kind, instance, cell, seq, coverage, digest, t) + payload.ljust(32, b"\x00")[:32]


class Relay:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.mode = MODE_FULLSYNC
        self.merge = False
        self.priority = False
        self.adaptive = False
        self.global_topk = 0
        self.dedup = 0
        self.max_queue = args.max_queue
        self.max_inflight = 0
        self.rate_frames_per_s = 0.0
        self.rate_burst_frames = 0
        self.rate_tokens = 0.0
        self.rate_last = time.monotonic()
        self.queue: deque[bytes] = deque()
        self.pqueue: deque[bytes] = deque()
        # Latest unsent K_UP for a (instance, digest). Lookup replaces a scan
        # of the whole FIFO, which stalled Static on unique noise.
        self.queued_up: dict[tuple[int, int], bytes] = {}
        self.replicas: dict[int, set[int]] = defaultdict(set)
        self.recent: dict[int, float] = {}
        self.ewma_dq = 0.0
        self.queue_high_since: float | None = None
        self.drops: Counter[str] = Counter()
        self.forwarded = 0
        self.received = 0
        self.maxq = 0
        self.current_cell = -1
        self.down_writer: asyncio.StreamWriter | None = None
        self.queue_event = asyncio.Event()
        self.inflight_event = asyncio.Event()
        self.inflight_event.set()
        self.stats_requested = asyncio.Event()

    @property
    def passthrough(self) -> bool:
        return self.mode == MODE_FULLSYNC

    @property
    def rate_fifo(self) -> bool:
        return self.mode == MODE_RATEFIFO

    @property
    def agecov(self) -> bool:
        return self.mode == MODE_AGECOV

    def score(self, coverage: int, t_send: float) -> float:
        """A deliberately simple non-semantic Age x Coverage comparator."""
        age_s = max(0.0, time.time() - t_send)
        return age_s * max(1, coverage) / FRAME

    def emit_update(self, stage: str, kind: int, instance: int, cell: int, seq: int,
                    coverage: int, digest: int, t_send: float, *, selected: bool,
                    reason: str = "", score: float | None = None) -> None:
        if getattr(self.args, "quiet", False):
            return
        now = time.time()
        print(json.dumps({
            "event": "update", "stage": stage, "cell": cell, "update_id": seq,
            "owner": instance, "prefix_digest64": digest,
            "op": "upsert" if kind == K_UP else "tombstone",
            "generation_time": t_send, "gateway_arrival_time": now,
            "gateway_send_time": now if stage == "forwarded" else None,
            "payload_bytes": FRAME, "coverage_tokens": coverage,
            "queue_age_s": max(0.0, now - t_send), "semantic_score": score,
            "selected": selected, "suppression_reason": reason,
            "policy": MODE_NAMES.get(self.mode, str(self.mode)),
        }, sort_keys=True), flush=True)

    def consume_rate_token(self) -> bool:
        if not self.rate_fifo:
            return True
        now = time.monotonic()
        elapsed = max(0.0, now - self.rate_last)
        self.rate_last = now
        self.rate_tokens = min(
            float(self.rate_burst_frames),
            self.rate_tokens + elapsed * self.rate_frames_per_s,
        )
        if self.rate_tokens < 1.0:
            return False
        self.rate_tokens -= 1.0
        return True

    # ---------------- upstream (agents) ----------------
    async def agent_reader(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                data = await reader.readexactly(FRAME)
                kind, instance, cell, seq, coverage, digest, t_send = HDR.unpack(data[:32])
                if kind == K_RESET:
                    self.do_reset(cell)
                    # Cell-boundary acknowledgement deliberately returns on
                    # the unshaped agent control direction. Under severe
                    # congestion the shaped downstream link may need tens of
                    # seconds to drain; waiting on it would make the next
                    # cell's reset depend on the previous cell's backlog.
                    writer.write(frame(K_RESET_DONE, 0, cell, 0, 0, 0, time.time()))
                    await writer.drain()
                    continue
                if kind == K_CONFIG:
                    (mode, merge, priority, adaptive, dedup, global_topk, maxq,
                     max_inflight, rate_mfps, rate_burst) = CFG.unpack(data[32:58])
                    self.mode = mode
                    self.merge, self.priority, self.adaptive = bool(merge), bool(priority), bool(adaptive)
                    self.global_topk = global_topk
                    self.dedup, self.max_queue = dedup, maxq
                    self.max_inflight = max_inflight
                    self.rate_frames_per_s = rate_mfps / 1000.0
                    self.rate_burst_frames = rate_burst
                    self.rate_tokens = float(rate_burst)
                    self.rate_last = time.monotonic()
                    continue
                if kind == K_STATS_REQ:
                    # Return control-plane stats on the reverse direction of
                    # the requesting agent TCP connection. That path is not
                    # shaped by the gateway egress qdisc, so metrics are not
                    # stranded behind the very signaling backlog being read.
                    await self.send_stats(writer)
                    continue
                if kind not in (K_UP, K_TOMB):
                    continue
                if cell != self.current_cell:
                    self.drops["stale_cell"] += 1
                    continue
                self.enqueue(data, kind, instance, seq, coverage, digest, t_send)
        except (asyncio.IncompleteReadError, ConnectionResetError):
            pass
        finally:
            writer.close()

    def congested(self) -> bool:
        """True only after sustained delay or a queue that stays high.

        A single enqueue burst can push len(queue) past the gate for a few
        milliseconds and then drain. That is not the overload the utility
        gate is meant to see.
        """
        if not self.adaptive:
            return False
        if self.ewma_dq > self.args.gate:
            return True
        now = time.monotonic()
        if len(self.queue) >= self.args.adaptive_queue_gate:
            if self.queue_high_since is None:
                self.queue_high_since = now
            hold = float(getattr(self.args, "congestion_hold", 0.2))
            return now - self.queue_high_since >= hold
        self.queue_high_since = None
        return False

    def low_utility(self, coverage: int, t_send: float) -> bool:
        if not self.congested():
            return False
        age = max(0.0, time.time() - t_send)
        decay = 2.718281828459045 ** (-(age + self.ewma_dq) / self.args.tau)
        return decay * coverage - self.args.util_lambda * FRAME <= 0

    def _admit_replica(self, instance: int, digest: int) -> bool:
        """True when this instance may hold the digest. Every instance id counts.

        Single-holder digests are forgotten past 65536 keys. Unique noise
        never reaches a cap of 2, and a digest that already has two holders
        is kept, so the cap still applies to prefixes the router can place.
        """
        if not self.dedup:
            return True
        replicas = self.replicas[digest]
        if instance not in replicas and len(replicas) >= self.dedup:
            return False
        replicas.add(instance)
        if len(self.replicas) <= 65536:
            return True
        for old in list(self.replicas):
            if old == digest or len(self.replicas[old]) > 1:
                continue
            del self.replicas[old]
            if len(self.replicas) <= 65536:
                break
        return True

    def enqueue(self, data: bytes, kind: int, instance: int, seq: int, coverage: int, digest: int, t_send: float) -> None:
        _kind, _instance, cell, _seq, _coverage, _digest, _t = HDR.unpack(data[:32])
        self.received += 1
        score = self.score(coverage, t_send) if self.agecov else None
        if not self.consume_rate_token():
            self.drops["rate_limit"] += 1
            self.emit_update("suppressed", kind, instance, cell, seq, coverage, digest, t_send,
                             selected=False, reason="rate_limit", score=score)
            return
        if kind == K_UP:
            if self.merge:
                self._supersede_queued_up(instance, digest)
            if self.low_utility(coverage, t_send):
                self.drops["low_utility"] += 1
                self.emit_update("suppressed", kind, instance, cell, seq, coverage, digest, t_send,
                                 selected=False, reason="low_utility")
                return
            if not self._admit_replica(instance, digest):
                self.drops["replica_cap"] += 1
                self.emit_update("suppressed", kind, instance, cell, seq, coverage, digest, t_send,
                                 selected=False, reason="duplicate_holder")
                return
        if kind == K_TOMB and self.merge:
            self._supersede_queued_up(instance, digest)
        if kind == K_TOMB and self.priority:
            self.pqueue.append(data)
        else:
            self.queue.append(data)
            if kind == K_UP and self.merge:
                self.queued_up[(instance, digest)] = data
        self.emit_update("enqueued", kind, instance, cell, seq, coverage, digest, t_send,
                         selected=True, score=score)
        if kind == K_UP and self.global_topk:
            self.trim_global_topk()
        self.maxq = max(self.maxq, len(self.queue) + len(self.pqueue))
        self.queue_event.set()

    def trim_global_topk(self) -> None:
        """Keep only the highest-coverage distinct prefixes in the unsent FIFO.

        The relay deliberately applies this at the shared bottleneck rather
        than at sources: it sees all queued owners and can replace a lower
        marginal prefix from one instance with a more valuable one from
        another. Frames that have already entered the kernel are never
        revoked, preserving TCP's causal ordering.
        """
        best: dict[int, int] = {}
        for queued in self.queue:
            qkind, _inst, _cell, _seq, qcoverage, qdigest, _sent = HDR.unpack(queued[:32])
            if qkind == K_UP:
                best[qdigest] = max(best.get(qdigest, 0), qcoverage)
        # Adaptive mode changes state admission, not the physical link rate.
        limit = max(1, self.global_topk // 4) if self.congested() else self.global_topk
        keep = {digest for digest, _coverage in sorted(best.items(), key=lambda item: (-item[1], item[0]))[:limit]}
        if len(keep) == len(best):
            return
        retained: deque[bytes] = deque()
        for queued in self.queue:
            qkind, qinst, qcell, qseq, qcoverage, qdigest, qsent = HDR.unpack(queued[:32])
            if qkind == K_UP and qdigest not in keep:
                self._forget_queued_up(queued)
                self.drops["global_topk"] += 1
                self.drops["low_utility"] += 1
                self.replicas[qdigest].discard(qinst)
                self.emit_update("suppressed", qkind, qinst, qcell, qseq, qcoverage, qdigest, qsent,
                                 selected=False, reason="low_utility")
            else:
                retained.append(queued)
        self.queue = retained

    def _supersede_queued_up(self, instance: int, digest: int) -> None:
        prev = self.queued_up.pop((instance, digest), None)
        if prev is None:
            return
        self.queue.remove(prev)
        qkind, qinst, qcell, qseq, qcov, qdig, qt = HDR.unpack(prev[:32])
        self.drops["superseded"] += 1
        self.emit_update("suppressed", qkind, qinst, qcell, qseq, qcov, qdig, qt,
                         selected=False, reason="superseded")

    def _forget_queued_up(self, data: bytes) -> None:
        kind, instance, _cell, _seq, _coverage, digest, _sent = HDR.unpack(data[:32])
        if kind == K_UP and self.queued_up.get((instance, digest)) is data:
            del self.queued_up[(instance, digest)]

    def _requeue_front(self, data: bytes) -> None:
        kind, instance, _cell, _seq, _coverage, digest, _sent = HDR.unpack(data[:32])
        if kind == K_TOMB and self.priority:
            self.pqueue.appendleft(data)
            return
        self.queue.appendleft(data)
        if kind == K_UP and self.merge and (instance, digest) not in self.queued_up:
            self.queued_up[(instance, digest)] = data

    def do_reset(self, cell: int) -> None:
        self.queue.clear()
        self.pqueue.clear()
        self.queued_up.clear()
        self.replicas.clear()
        self.recent.clear()
        self.inflight_event.set()
        self.ewma_dq = 0.0
        self.queue_high_since = None
        self.drops.clear()
        self.forwarded = 0
        self.received = 0
        self.maxq = 0
        self.rate_tokens = float(self.rate_burst_frames)
        self.rate_last = time.monotonic()
        self.current_cell = cell
        # Fresh kernel state per cell: closing the downstream connection
        # discards every in-flight byte (socket buffers, qdisc backlog) from
        # the previous cell, so cells are independent.
        if self.down_writer is not None:
            self.down_writer.close()
            self.down_writer = None

    # ---------------- downstream (dispatcher) ----------------
    async def downstream_manager(self) -> None:
        host, port = self.args.downstream.rsplit(":", 1)
        while True:
            try:
                reader, writer = await asyncio.open_connection(host, int(port))
                sock = writer.get_extra_info("socket")
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, self.args.sndbuf)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                writer.transport.set_write_buffer_limits(high=2048)
                self.down_writer = writer
                print(json.dumps({"event": "downstream_connected", "to": self.args.downstream}), flush=True)
                await self.ack_reader(reader)
                print(json.dumps({"event": "downstream_eof"}), flush=True)
            except (ConnectionRefusedError, OSError, asyncio.IncompleteReadError) as exc:
                print(json.dumps({"event": "downstream_error", "error": repr(exc)[:120]}), flush=True)
            finally:
                self.down_writer = None
            await asyncio.sleep(0.2)

    async def ack_reader(self, reader: asyncio.StreamReader) -> None:
        while True:
            data = await reader.readexactly(FRAME)
            kind, _i, cell, seq, _c, _d, t_recv = HDR.unpack(data[:32])
            if kind != K_ACK or cell != self.current_cell:
                continue
            t_send = self.recent.pop(seq, None)
            if t_send is not None:
                delay = max(0.0, t_recv - t_send)
                self.ewma_dq = 0.8 * self.ewma_dq + 0.2 * delay
                self.inflight_event.set()

    async def release_loop(self) -> None:
        while True:
            if not self.pqueue and not self.queue:
                self.queue_event.clear()
                if not self.pqueue and not self.queue:
                    await self.queue_event.wait()
                continue
            # Keep a small, real ACK-delimited application window when
            # configured.  Exact FIFO, static, and adaptive all share this
            # transport discipline; only their treatment of unsent updates
            # differs.  With max_inflight=0, preserve the legacy behavior.
            if self.max_inflight and len(self.recent) >= self.max_inflight:
                self.inflight_event.clear()
                if len(self.recent) >= self.max_inflight:
                    await self.inflight_event.wait()
                continue
            if self.pqueue:
                data = self.pqueue.popleft()
            elif self.agecov:
                # No semantic invalidation priority: select solely by the
                # specified Age x Coverage score, with sequence number as a
                # deterministic tie-breaker.
                data = max(
                    self.queue,
                    key=lambda queued: (
                        self.score(HDR.unpack(queued[:32])[4], HDR.unpack(queued[:32])[6]),
                        -HDR.unpack(queued[:32])[3],
                    ),
                )
                self.queue.remove(data)
                self._forget_queued_up(data)
            else:
                data = self.queue.popleft()
                self._forget_queued_up(data)
            if self.down_writer is None:
                # Dispatcher endpoint not connected yet: requeue and wait.
                self._requeue_front(data)
                await asyncio.sleep(0.2)
                continue
            kind, instance, cell, seq, coverage, digest, t_send = HDR.unpack(data[:32])
            if kind == K_UP and self.low_utility(coverage, t_send):
                self.drops["low_utility"] += 1
                self.replicas[digest].discard(instance)
                self.emit_update("suppressed", kind, instance, cell, seq, coverage, digest, t_send,
                                 selected=False, reason="low_utility")
                continue
            try:
                self.down_writer.write(data)
                await self.down_writer.drain()
            except (ConnectionResetError, BrokenPipeError, OSError):
                self._requeue_front(data)
                await asyncio.sleep(0.2)
                continue
            self.forwarded += 1
            self.emit_update("forwarded", kind, instance, cell, seq, coverage, digest, t_send,
                             selected=True,
                             score=self.score(coverage, t_send) if self.agecov else None)
            self.recent[seq] = t_send
            if len(self.recent) > RECENT_KEEP:
                for old in list(self.recent)[: len(self.recent) - RECENT_KEEP]:
                    self.recent.pop(old, None)
            if kind == K_TOMB:
                self.replicas[digest].discard(instance)

    async def send_stats(self, reply_writer: asyncio.StreamWriter | None = None) -> None:
        payload = STATS.pack(
            self.forwarded,
            self.drops["rate_limit"],
            self.drops["superseded"],
            self.drops["replica_cap"],
            self.drops["low_utility"],
            self.drops["queue_drop"],
            self.drops["expired"],
            self.maxq,
        )
        if reply_writer is not None:
            # The 32-byte stats payload is full. Residual queue and received
            # frames ride in the header: seq is the queue, coverage is received.
            queued_now = len(self.queue) + len(self.pqueue)
            reply_writer.write(frame(
                K_STATS, 0, self.current_cell, queued_now, self.received, 0, time.time(), payload,
            ))
            await reply_writer.drain()
        print(json.dumps({
            "event": "cell_stats", "cell": self.current_cell, "forwarded": self.forwarded,
            "drops": dict(self.drops), "maxq": self.maxq, "ewma_dq_s": self.ewma_dq,
            "queued": len(self.queue) + len(self.pqueue),
        }), flush=True)

    async def stats_printer(self) -> None:
        while True:
            await asyncio.sleep(2.0)
            print(json.dumps({
                "event": "tick", "cell": self.current_cell, "queued": len(self.queue),
                "pqueued": len(self.pqueue), "forwarded": self.forwarded,
                "ewma_dq_s": round(self.ewma_dq, 4), "drops": dict(self.drops),
                "downstream": self.down_writer is not None,
            }), flush=True)


async def amain(args: argparse.Namespace) -> None:
    relay = Relay(args)
    server = await asyncio.start_server(relay.agent_reader, "0.0.0.0", args.listen)
    await asyncio.gather(
        server.serve_forever(),
        relay.downstream_manager(),
        relay.release_loop(),
        relay.stats_printer(),
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--listen", type=int, default=9700)
    parser.add_argument("--downstream", required=True, help="dispatcher endpoint host:port (host bridge IP:9701)")
    parser.add_argument("--sndbuf", type=int, default=2304,
                        help="downstream SO_SNDBUF; kept near the kernel minimum so queueing happens at the tc qdisc, not in socket buffers")
    parser.add_argument("--max-queue", type=int, default=200)
    parser.add_argument("--tau", type=float, default=30.0)
    parser.add_argument("--util-lambda", type=float, default=16.0)
    parser.add_argument("--gate", type=float, default=2.0)
    parser.add_argument("--adaptive-queue-gate", type=int, default=8,
                        help="queued upserts that trigger proactive adaptive admission")
    parser.add_argument("--congestion-hold", type=float, default=0.2,
                        help="seconds the pre-link queue must stay above the gate before adaptive admission treats it as congestion")
    parser.add_argument("--quiet", action="store_true",
                        help="do not print a JSON line per frame; counters and the 2s tick remain")
    args = parser.parse_args()
    asyncio.run(amain(args))


if __name__ == "__main__":
    main()
