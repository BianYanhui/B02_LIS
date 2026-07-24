#!/usr/bin/env python3
"""Gateway relay + semantic aggregator for the B02 shared-control-link platform.

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

Wire format: fixed 64-byte binary frames (documented in net/README notes and
run_live_shared_link_v3.py docstring).

  header (32B, big-endian ">BBHIqQd"):
    kind u8 | instance u8 | cell u16 | seq u32 | coverage i64 |
    digest64 u64 | t_send f64 (wall clock; same host => shared clock)
  payload (32B): kind-specific
    config (kind 5): ">BBBBHI" = merge, priority, adaptive, pad, dedup, max_queue
    stats  (kind 7): ">IIIIIII" = forwarded, drop_superseded, drop_replica_cap,
                     drop_low_utility, drop_backlog_cap, max_queue_depth, ewma_dq_ms
    ack    (kind 6): header.seq = acked seq, header.t_send = receiver wall time

kinds: 1 upsert, 2 tombstone   (agent -> relay -> dispatcher)
       3 reset, 4 stats_request, 5 config   (agent -> relay)
       6 ack                                 (dispatcher -> relay)
       7 stats, 8 reset_done                (relay -> dispatcher)

Mechanisms (set per cell via a config frame; passthrough = all off):
  --merge:    a newer upsert cancels a queued unsent older upsert for the
              same (instance,digest); a tombstone cancels a queued upsert.
  --priority: tombstones go to a priority lane released first (non-preemptive).
  --dedup N:  replica cap: at most N instances may hold queued-or-forwarded
              upserts per digest (drop excess).
  --adaptive: utility gate: drop an upsert when the ack-measured EWMA
              delivery delay Dq exceeds --gate and
              U = exp(-(age+Dq)/tau)*coverage - lambda*FRAME <= 0.
  Backpressure cap: in passthrough mode only, drop-oldest once the internal
  queue exceeds --max-queue (counted as drop_backlog_cap).
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
HDR = struct.Struct(">BBHIqQd")
CFG = struct.Struct(">BBBBHI")
STATS = struct.Struct(">IIIIIII")
K_UP, K_TOMB, K_RESET, K_STATS_REQ, K_CONFIG, K_ACK, K_STATS, K_RESET_DONE = 1, 2, 3, 4, 5, 6, 7, 8
RECENT_KEEP = 4096


def frame(kind: int, instance: int, cell: int, seq: int, coverage: int, digest: int, t: float, payload: bytes = b"") -> bytes:
    return HDR.pack(kind, instance, cell, seq, coverage, digest, t) + payload.ljust(32, b"\x00")[:32]


class Relay:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.merge = False
        self.priority = False
        self.adaptive = False
        self.dedup = 0
        self.max_queue = args.max_queue
        self.queue: deque[bytes] = deque()
        self.pqueue: deque[bytes] = deque()
        self.replicas: dict[int, set[int]] = defaultdict(set)
        self.recent: dict[int, float] = {}
        self.ewma_dq = 0.0
        self.drops: Counter[str] = Counter()
        self.forwarded = 0
        self.maxq = 0
        self.current_cell = -1
        self.down_writer: asyncio.StreamWriter | None = None
        self.pending_reset_done: int | None = None
        self.queue_event = asyncio.Event()
        self.stats_requested = asyncio.Event()

    @property
    def passthrough(self) -> bool:
        return not (self.merge or self.priority or self.adaptive or self.dedup)

    # ---------------- upstream (agents) ----------------
    async def agent_reader(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                data = await reader.readexactly(FRAME)
                kind, instance, cell, seq, coverage, digest, t_send = HDR.unpack(data[:32])
                if kind == K_RESET:
                    self.do_reset(cell)
                    continue
                if kind == K_CONFIG:
                    merge, priority, adaptive, _pad, dedup, maxq = CFG.unpack(data[32:42])
                    self.merge, self.priority, self.adaptive = bool(merge), bool(priority), bool(adaptive)
                    self.dedup, self.max_queue = dedup, maxq
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

    def enqueue(self, data: bytes, kind: int, instance: int, seq: int, coverage: int, digest: int, t_send: float) -> None:
        if kind == K_UP:
            if self.merge:
                for queued in list(self.queue):
                    qkind, qinst, _c, _s, _cov, qdig, _t = HDR.unpack(queued[:32])
                    if qkind == K_UP and qinst == instance and qdig == digest:
                        self.queue.remove(queued)
                        self.drops["superseded"] += 1
            if self.adaptive and self.ewma_dq > self.args.gate:
                age = time.time() - t_send
                utility = (2.718281828459045 ** (-(age + self.ewma_dq) / self.args.tau)) * coverage - self.args.util_lambda * FRAME
                if utility <= 0:
                    self.drops["low_utility"] += 1
                    return
            if self.dedup:
                replicas = self.replicas[digest]
                if instance not in replicas and len(replicas) >= self.dedup:
                    self.drops["replica_cap"] += 1
                    return
                replicas.add(instance)
        if kind == K_TOMB and self.merge:
            for queued in list(self.queue):
                qkind, qinst, _c, _s, _cov, qdig, _t = HDR.unpack(queued[:32])
                if qkind == K_UP and qinst == instance and qdig == digest:
                    self.queue.remove(queued)
                    self.drops["superseded"] += 1
        (self.pqueue if (kind == K_TOMB and self.priority) else self.queue).append(data)
        if self.passthrough:
            while len(self.queue) > self.max_queue:
                self.queue.popleft()
                self.drops["backlog_cap"] += 1
        self.maxq = max(self.maxq, len(self.queue) + len(self.pqueue))
        self.queue_event.set()

    def do_reset(self, cell: int) -> None:
        self.queue.clear()
        self.pqueue.clear()
        self.replicas.clear()
        self.recent.clear()
        self.ewma_dq = 0.0
        self.drops.clear()
        self.forwarded = 0
        self.maxq = 0
        self.current_cell = cell
        # Fresh kernel state per cell: closing the downstream connection
        # discards every in-flight byte (socket buffers, qdisc backlog) from
        # the previous cell, so cells are independent.  reset_done is sent
        # over the NEW connection by downstream_manager.
        self.pending_reset_done = cell
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
                if self.pending_reset_done is not None:
                    writer.write(frame(K_RESET_DONE, 0, self.pending_reset_done, 0, 0, 0, time.time()))
                    self.pending_reset_done = None
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

    async def release_loop(self) -> None:
        while True:
            if self.pqueue:
                data = self.pqueue.popleft()
            elif self.queue:
                data = self.queue.popleft()
            else:
                self.queue_event.clear()
                if not self.pqueue and not self.queue:
                    await self.queue_event.wait()
                continue
            if self.down_writer is None:
                # Dispatcher endpoint not connected yet: requeue and wait.
                (self.pqueue if HDR.unpack(data[:32])[0] == K_TOMB and self.priority else self.queue).appendleft(data)
                await asyncio.sleep(0.2)
                continue
            kind, instance, cell, seq, coverage, digest, t_send = HDR.unpack(data[:32])
            try:
                self.down_writer.write(data)
                await self.down_writer.drain()
            except (ConnectionResetError, BrokenPipeError, OSError):
                (self.pqueue if kind == K_TOMB and self.priority else self.queue).appendleft(data)
                await asyncio.sleep(0.2)
                continue
            self.forwarded += 1
            self.recent[seq] = t_send
            if len(self.recent) > RECENT_KEEP:
                for old in list(self.recent)[: len(self.recent) - RECENT_KEEP]:
                    self.recent.pop(old, None)
            if kind == K_TOMB:
                self.replicas[digest].discard(instance)

    async def send_stats(self, reply_writer: asyncio.StreamWriter | None = None) -> None:
        payload = STATS.pack(
            self.forwarded,
            self.drops["superseded"],
            self.drops["replica_cap"],
            self.drops["low_utility"],
            self.drops["backlog_cap"],
            self.maxq,
            int(self.ewma_dq * 1000),
        )
        if reply_writer is not None:
            reply_writer.write(frame(K_STATS, 0, self.current_cell, 0, 0, 0, time.time(), payload))
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
    parser.add_argument("--util-lambda", type=float, default=28.0)
    parser.add_argument("--gate", type=float, default=0.050)
    args = parser.parse_args()
    asyncio.run(amain(args))


if __name__ == "__main__":
    main()
