"""Request decisions and the noise stream run in different processes.

The request process only places requests and talks to vLLM. A dispatcher
process keeps the routable view. Noise senders are separate processes, each
with its own gateway connection, so the request interpreter does not unpack
the background stream and one sender is not the rate limit.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import multiprocessing as mp
import os
import queue
import socket
import threading
import time
from multiprocessing.queues import Queue

from experiments.icc_kv.runtime import FIXED_LINK_BIT_S, RELAY_PORT, PathRuntime
from experiments.icc_kv.wire import K_UP, frame

NOISE_WORKER0 = 16


def noise_digest(seq: int) -> int:
    return int.from_bytes(hashlib.blake2b(f"noise-{seq}".encode(), digest_size=8).digest(), "big")


def _pin(cpu: int) -> None:
    try:
        os.sched_setaffinity(0, {cpu})
    except OSError:
        pass


async def _connect_agent():
    last: OSError | None = None
    for _attempt in range(40):
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", RELAY_PORT)
            sock = writer.get_extra_info("socket")
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 << 20)
            return reader, writer
        except OSError as exc:
            last = exc
            await asyncio.sleep(0.25)
    raise RuntimeError(f"noise sender cannot reach the gateway: {last!r}")


async def _send_until(writer: asyncio.StreamWriter, spec: dict, stop: mp.synchronize.Event) -> int:
    n = int(spec["n"])
    index = int(spec["index"])
    workers = max(1, int(spec["workers"]))
    coverage = int(spec["coverage"])
    cell = int(spec["cell"])
    seq0 = int(spec["seq0"])
    seconds = float(spec["seconds"])
    t0 = float(spec["t0"])
    scenario = str(spec["scenario"])
    sent = 0
    step = 0
    while not stop.is_set():
        elapsed = time.monotonic() - t0
        if seconds and elapsed >= seconds:
            break
        level = float(spec["level"])
        if scenario == "burst" and (elapsed % 30.0) >= 25.0:
            level *= float(spec.get("burst_mult", 5.0))
        rate = level / n
        if rate < 1.0:
            await asyncio.sleep(0.05)
            continue
        batch = max(1, int(rate / 200)) if rate >= 200 else 1
        started = time.perf_counter()
        now = time.time()
        for _offset in range(batch):
            if stop.is_set():
                break
            seq = seq0 + index + step * n
            step += 1
            writer.write(frame(
                K_UP, int(NOISE_WORKER0 + (seq % workers)), int(cell), int(seq),
                int(coverage), int(noise_digest(seq)), now,
            ))
            sent += 1
        if not await _drain(writer, stop):
            return sent
        # The sequence counter is `step`. A pause variable with the same name
        # rewound it, so a sender that got ahead retransmitted its first batch
        # for the rest of the cell.
        pause = batch / rate - (time.perf_counter() - started)
        while pause > 0 and not stop.is_set():
            slice_s = min(pause, 0.05)
            await asyncio.sleep(slice_s)
            pause -= slice_s
    await _drain(writer, stop, timeout=1.0)
    return sent


async def _drain(writer: asyncio.StreamWriter, stop: mp.synchronize.Event, timeout: float = 0.5) -> bool:
    """False when a stop request arrives before the socket accepts the bytes."""
    while True:
        try:
            await asyncio.wait_for(writer.drain(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            if stop.is_set():
                return False


async def _noise_async(spec_q: Queue, result_q: Queue, ready_q: Queue, stop: mp.synchronize.Event) -> None:
    _reader, writer = await _connect_agent()
    ready_q.put(os.getpid())
    try:
        while True:
            spec = await asyncio.to_thread(spec_q.get)
            if spec is None:
                return
            try:
                sent = await _send_until(writer, spec, stop)
                result_q.put(sent)
            except Exception as exc:
                import traceback
                result_q.put(RuntimeError(traceback.format_exc()))
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


def noise_entry(spec_q: Queue, result_q: Queue, ready_q: Queue, stop: mp.synchronize.Event, cpu: int) -> None:
    _pin(cpu)
    asyncio.run(_noise_async(spec_q, result_q, ready_q, stop))


async def _control_async(cmd_q: Queue, reply_q: Queue, emit_q: Queue, link_bit_s: int = 0) -> None:
    runtime = PathRuntime(link_bit_s=link_bit_s)
    try:
        await runtime.start()
    except Exception as exc:
        reply_q.put({"error": repr(exc)})
        return
    reply_q.put({"op": "ready"})
    stop = asyncio.Event()

    async def emit_pump() -> None:
        while not stop.is_set():
            item = await asyncio.to_thread(emit_q.get)
            if item is None:
                return
            kind, worker, cell, seq, coverage, digest, generated = item
            await runtime.send_foreground(kind, worker, cell, seq, coverage, digest, generated)

    async def handle(msg: tuple) -> dict:
        op = msg[0]
        if op == "configure":
            _op, cell, method, rate = msg
            await runtime.configure(cell, method, rate)
            return {"op": "configure"}
        if op == "state":
            tested, generated, delivered = runtime.dispatcher.placement_maps()
            return {"op": "state", "tested": tested, "generated": generated, "delivered": delivered}
        if op == "stats":
            return {"op": "stats", **(await runtime.fetch_stats())}
        if op == "counts":
            dispatcher = runtime.dispatcher
            with dispatcher.lock:
                applied = list(dispatcher.applied)
                frames, foreground, noise = dispatcher.frames, dispatcher.foreground_frames, dispatcher.noise_frames
            return {
                "op": "counts",
                "frames": frames,
                "foreground": foreground,
                "noise": noise,
                "applied": applied,
            }
        if op == "meta":
            return {"op": "meta", **runtime.meta()}
        return {"error": f"unknown control op {op}"}

    async def cmd_pump() -> None:
        while not stop.is_set():
            rid, msg = await asyncio.to_thread(cmd_q.get)
            if msg[0] == "stop":
                stop.set()
                reply_q.put({"op": "stopped", "rid": rid})
                return
            try:
                reply = await handle(msg)
            except Exception as exc:
                reply = {"error": repr(exc)}
            reply["rid"] = rid
            reply_q.put(reply)

    await asyncio.gather(cmd_pump(), emit_pump())
    await runtime.stop()


def control_entry(cmd_q: Queue, reply_q: Queue, emit_q: Queue, cpu: int, link_bit_s: int = 0) -> None:
    _pin(cpu)
    asyncio.run(_control_async(cmd_q, reply_q, emit_q, link_bit_s))


class NoisePool:
    def __init__(self, senders: int = 4) -> None:
        self.n = max(1, senders)
        self.ctx = mp.get_context("spawn")
        self.spec_q: Queue = self.ctx.Queue()
        self.result_q: Queue = self.ctx.Queue()
        self.ready_q: Queue = self.ctx.Queue()
        self.stop_noise = self.ctx.Event()
        self.procs: list[mp.Process] = []

    def start(self) -> None:
        for index in range(self.n):
            proc = self.ctx.Process(
                target=noise_entry,
                name=f"icc-noise-{index}",
                args=(self.spec_q, self.result_q, self.ready_q, self.stop_noise, 3 + index),
                daemon=True,
            )
            proc.start()
            self.procs.append(proc)
        for _index in range(self.n):
            self.ready_q.get(timeout=30)

    def _specs(self, spec: dict) -> None:
        self.stop_noise.clear()
        stamp = time.monotonic()
        for index in range(self.n):
            self.spec_q.put({**spec, "n": self.n, "index": index, "t0": stamp})

    def _collect(self, timeout: float) -> int:
        total = 0
        for _index in range(self.n):
            item = self.result_q.get(timeout=timeout)
            if isinstance(item, Exception):
                raise item
            total += int(item)
        return total

    def run_for(self, spec: dict) -> int:
        seconds = float(spec.get("seconds") or 0.0)
        self._specs(spec)
        return self._collect(max(30.0, seconds + 30.0))

    def begin(self, spec: dict) -> None:
        self._specs({**spec, "seconds": 0})

    def end(self) -> int:
        self.stop_noise.set()
        total = self._collect(60.0)
        self.stop_noise.clear()
        return total

    def close(self) -> None:
        self.stop_noise.set()
        for _index in range(self.n):
            self.spec_q.put(None)
        for proc in self.procs:
            proc.join(timeout=5)
            if proc.is_alive():
                proc.terminate()
        self.procs.clear()


class SplitControl:
    """Dispatcher in one process, noise in others, placement reads a small view."""

    def __init__(self, senders: int = 4, link_bit_s: int = 0) -> None:
        self.ctx = mp.get_context("spawn")
        self.cmd_q: Queue = self.ctx.Queue()
        self.reply_q: Queue = self.ctx.Queue()
        self.emit_q: Queue = self.ctx.Queue(maxsize=4096)
        self.pool = NoisePool(senders)
        self.proc: mp.Process | None = None
        self.local = None
        self.ideal = False
        self.sent_fg: list[tuple[int, str, int, float]] = []
        self.fg_up = 0
        self.fg_tomb = 0
        self._noise_live = False
        self.link_bit_s = int(link_bit_s) if link_bit_s else FIXED_LINK_BIT_S
        self._rpc_lock = threading.Lock()
        self._rid = 0
        self.stale_replies = 0

    def start(self) -> None:
        from experiments.icc_kv.runtime import Dispatcher

        self.local = Dispatcher(cell=-1)
        self.proc = self.ctx.Process(
            target=control_entry,
            name="icc-dispatcher",
            args=(self.cmd_q, self.reply_q, self.emit_q, 1, self.link_bit_s),
            daemon=True,
        )
        self.proc.start()
        ready = self.reply_q.get(timeout=30)
        if ready.get("error"):
            raise RuntimeError(ready["error"])
        self.pool.start()

    def _rpc(self, msg: tuple, timeout: float = 30.0) -> dict:
        with self._rpc_lock:
            self._rid += 1
            rid = self._rid
            self.cmd_q.put((rid, msg))
            deadline = time.monotonic() + timeout
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise TimeoutError(f"control rpc {msg[0]!r} rid={rid} timed out after {timeout}s")
                try:
                    reply = self.reply_q.get(timeout=left)
                except queue.Empty:
                    raise TimeoutError(f"control rpc {msg[0]!r} rid={rid} timed out after {timeout}s") from None
                if reply.get("rid") == rid:
                    break
                self.stale_replies += 1
        reply.pop("rid", None)
        if reply.get("error"):
            raise RuntimeError(reply["error"])
        return reply

    def begin_cell(self) -> None:
        self.sent_fg.clear()
        self.fg_up = 0
        self.fg_tomb = 0
        self.ideal = False

    def configure(self, cell: int, method: str, rate: float) -> None:
        self.ideal = method == "Ideal"
        self._rpc(("configure", cell, method, rate), timeout=30)

    def offer_for(self, spec: dict) -> int:
        self._noise_live = True
        try:
            return self.pool.run_for(spec)
        finally:
            self._noise_live = False

    def begin_noise(self, spec: dict) -> None:
        self._noise_live = True
        self.pool.begin(spec)

    def end_noise(self) -> int:
        if not self._noise_live:
            return 0
        self._noise_live = False
        return self.pool.end()

    def emit(self, kind: int, worker: int, cell: int, seq: int, coverage: int, digest: int) -> None:
        from experiments.icc_kv.wire import K_TOMB

        generated = time.time()
        kind_name = "invalidate" if kind == K_TOMB else "update"
        self.sent_fg.append((seq, kind_name, coverage, generated))
        if kind == K_TOMB:
            self.fg_tomb += 1
        else:
            self.fg_up += 1
        try:
            self.emit_q.put_nowait((kind, worker, cell, seq, coverage, digest, generated))
        except Exception:
            self.emit_q.put((kind, worker, cell, seq, coverage, digest, generated), timeout=1)

    def placement_state(self) -> tuple[dict, dict, dict]:
        if self.ideal:
            assert self.local is not None
            return self.local.placement_maps()
        reply = self._rpc(("state",), timeout=10)
        return reply["tested"], reply["generated"], reply["delivered"]

    def set_local(self, worker: int, digest: int, coverage: int) -> None:
        assert self.local is not None
        self.local.set_local(worker, digest, coverage)

    def clear_local(self, worker: int, digest: int) -> None:
        assert self.local is not None
        self.local.clear_local(worker, digest)

    def counts(self) -> dict:
        return self._rpc(("counts",), timeout=30)

    def stats(self) -> dict:
        try:
            reply = self._rpc(("stats",), timeout=20)
        except Exception as exc:
            return {"stats_error": repr(exc)[:160]}
        reply.pop("op", None)
        return reply

    def meta(self) -> dict:
        try:
            reply = self._rpc(("meta",), timeout=15)
        except Exception:
            return {}
        reply.pop("op", None)
        return reply

    def stop(self) -> None:
        try:
            self.pool.close()
        except Exception:
            pass
        if self.proc is not None and self.proc.is_alive():
            try:
                self.emit_q.put(None, timeout=2)
                self.cmd_q.put((0, ("stop",)), timeout=2)
                self.reply_q.get(timeout=20)
            except Exception:
                pass
            self.proc.join(timeout=10)
            if self.proc.is_alive():
                self.proc.terminate()
        self.proc = None
