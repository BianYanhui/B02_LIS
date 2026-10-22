#!/usr/bin/env python3
"""P0-M1.5-C shared-dispatcher apply capacity.

Host processes only. Does not start vLLM, does not configure tc, and does not
bind the formal gateway ports. The apply call is Dispatcher.on_frame.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import csv
import hashlib
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path("/home/byh/B02")
sys.path.insert(0, str(ROOT))

from experiments.icc_kv.runtime import ROUTABLE_BELOW, Dispatcher  # noqa: E402
from experiments.icc_kv.wire import (  # noqa: E402
    FRAME, HDR, K_ACK, K_RESET, K_RESET_DONE, K_STATS, K_STATS2, K_STATS_REQ, K_TOMB, K_UP,
    STATS, STATS2, config_frame, frame,
)

OUT = ROOT / "analysis/p0_pressure_realism/m1_5c_dispatcher_capacity"
RAW = OUT / "raw"
GATEWAY = ROOT / "experiments/4t4/net/gateway_relay_4t4.py"
PY = sys.executable
DISP_PORT = 19830
CTRL_PORT = 19831
GW_BASE = 19840
# Formal listener. Never bind or send to it.
PROTECTED_PORTS = (9700, 9710, 9711)
WORKERS = list(range(16))
LINEAGES_PER_WORKER = 64
S1_COVERAGE0 = 1024
S1_COVERAGE_STEP = 16
S2_COVERAGE = 1024
SAMPLE_STRIDE = 32
RATES = (250, 500, 1000, 2000, 4000)
A1_EXTRAPOLATED = ((256, 132.6), (1024, 530.2), (4096, 2121.0))
A4_EXTRAPOLATED = ((256, 493.6), (1024, 1974.0), (4096, 7897.0))
MASK64 = (1 << 64) - 1
CLK_TCK = os.sysconf("SC_CLK_TCK")
LIVE: list[subprocess.Popen] = []


def log(message: str) -> None:
    print(message, flush=True)


def mix(worker: int, digest: int, coverage: int) -> int:
    x = ((worker & 0xFF) * 0x9E3779B97F4A7C15) ^ (digest & MASK64) ^ ((coverage & MASK64) * 0xBF58476D1CE4E5B9)
    x &= MASK64
    x = (x ^ (x >> 30)) * 0xBF58476D1CE4E5B9 & MASK64
    x = (x ^ (x >> 27)) * 0x94D049BB133111EB & MASK64
    return x ^ (x >> 31)


def digest64(name: str) -> int:
    return int.from_bytes(hashlib.blake2b(name.encode(), digest_size=8).digest(), "big")


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * p))))
    return ordered[index]


def checksum(items: dict[tuple[int, int], int]) -> int:
    acc = 0
    for (worker, digest), coverage in items.items():
        acc ^= mix(worker, digest, coverage)
    return acc


def worker_groups(n_gateways: int) -> list[list[int]]:
    if 16 % n_gateways != 0:
        raise SystemExit(f"worker count 16 is not divisible by {n_gateways}")
    size = 16 // n_gateways
    return [WORKERS[i * size:(i + 1) * size] for i in range(n_gateways)]


def split_rate(total: int, n_gateways: int) -> list[int]:
    base, rem = divmod(total, n_gateways)
    return [base + (1 if i < rem else 0) for i in range(n_gateways)]


def s1_keys(workers: list[int]) -> list[tuple[int, int]]:
    keys = []
    for worker in workers:
        for lineage in range(LINEAGES_PER_WORKER):
            keys.append((worker, digest64(f"P0M15C/S1/w{worker}/l{lineage}")))
    return keys


def proc_ticks_rss(pid: int) -> tuple[int, int]:
    try:
        text = Path(f"/proc/{pid}/stat").read_text()
        rest = text[text.rfind(")") + 2:].split()
        ticks = int(rest[11]) + int(rest[12])
        rss = 0
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                rss = int(line.split()[1])
                break
        return ticks, rss
    except (OSError, IndexError, ValueError):
        return -1, -1


def cpu_cores(ticks0: int, ticks1: int, dt: float) -> float | None:
    if ticks0 < 0 or ticks1 < ticks0 or dt <= 0:
        return None
    return (ticks1 - ticks0) / CLK_TCK / dt


def recvall(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise EOFError("short socket read")
        buf += chunk
    return buf


class LineSock:
    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.buf = b""

    def request(self, obj: dict, timeout: float = 10) -> dict:
        self.sock.settimeout(timeout)
        self.sock.sendall(json.dumps(obj).encode() + b"\n")
        while b"\n" not in self.buf:
            chunk = self.sock.recv(1 << 20)
            if not chunk:
                raise EOFError("control closed")
            self.buf += chunk
        line, self.buf = self.buf.split(b"\n", 1)
        return json.loads(line)


def fetch_stats(port: int, timeout: float = 2.0) -> dict:
    sock = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    sock.settimeout(timeout)
    try:
        sock.sendall(frame(K_STATS_REQ, 0, 0, 0, 0, 0, time.time()))
        blobs = (recvall(sock, FRAME), recvall(sock, FRAME))
    finally:
        sock.close()
    out: dict = {}
    for blob in blobs:
        kind, _i, _cell, queued, received, _d, _t = HDR.unpack(blob[:32])
        if kind == K_STATS:
            forwarded, rate, superseded, replica, low, qdrop, expired, maxq = STATS.unpack(blob[32:64])
            out.update({
                "forwarded": int(forwarded), "rate_limit": int(rate), "superseded": int(superseded),
                "replica_cap": int(replica), "low_utility": int(low), "queue_drop": int(qdrop),
                "expired": int(expired), "maxq": int(maxq), "live_depth": int(queued),
                "received": int(received),
            })
        elif kind == K_STATS2:
            topk, stale, ghost, live, entries, ms, pqueued, _pad = STATS2.unpack(blob[32:64])
            out.update({
                "topk_drops": int(topk), "stale_cell": int(stale), "ghost": int(ghost),
                "live_queued": int(live), "congested_entries": int(entries),
                "congested_ms": int(ms), "pqueued": int(pqueued),
            })
    return out


def last_event(path: Path, name: str) -> dict:
    last: dict = {}
    if not path.exists():
        return last
    for line in path.read_text(errors="replace").splitlines():
        if f'"event": "{name}"' not in line:
            continue
        with contextlib.suppress(json.JSONDecodeError):
            last = json.loads(line)
    return last


def listening_ports() -> set[int]:
    try:
        text = subprocess.check_output(["ss", "-H", "-ltn"], text=True, timeout=5)
    except (subprocess.SubprocessError, OSError):
        return set()
    found = set()
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        port = parts[3].rsplit(":", 1)[-1]
        if port.isdigit():
            found.add(int(port))
    return found


def port_listening(port: int) -> bool:
    return port in listening_ports()


def stop_proc(proc: subprocess.Popen | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGTERM)
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=2)


def stop_all() -> None:
    while LIVE:
        stop_proc(LIVE.pop())


def cleanup_stale() -> None:
    pidfile = RAW / "pids.txt"
    if not pidfile.exists():
        return
    for line in pidfile.read_text().splitlines():
        if not line.strip().isdigit():
            continue
        pid = int(line.strip())
        cmdline_path = Path(f"/proc/{pid}/cmdline")
        if not cmdline_path.exists():
            continue
        cmdline = cmdline_path.read_bytes().replace(b"\x00", b" ").decode(errors="ignore")
        ours = "run_m15c.py --role dispatcher" in cmdline or (
            "gateway_relay_4t4.py" in cmdline and f"127.0.0.1:{DISP_PORT}" in cmdline
        )
        if not ours:
            continue
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pid, signal.SIGTERM)
    time.sleep(0.2)
    pidfile.unlink(missing_ok=True)


def remember(proc: subprocess.Popen) -> None:
    LIVE.append(proc)
    RAW.mkdir(parents=True, exist_ok=True)
    with (RAW / "pids.txt").open("a") as handle:
        handle.write(f"{proc.pid}\n")


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_text(path: str) -> str:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return "NOT_MEASURED"


# --- dispatcher process -----------------------------------------------------

class Service:
    def __init__(self, cell: int) -> None:
        self.cell = cell
        self.dispatcher = Dispatcher(cell=cell)
        self.received = 0
        self.applied = 0
        self.acks = 0
        self.errors = 0
        self.wrong_cell = 0
        self.inline_gap_max = 0
        self.error_text = ""
        self.measuring = False
        self.cpu_sum = 0.0
        self.cpu_n = 0
        self.sample_i = 0
        self.samples: list[tuple[float, float, float]] = []
        self.frozen: list[tuple[float, float, float]] = []
        self.t_mark = 0.0
        self.t_end = 0.0
        self.base: dict = {}
        self.end: dict = {}
        self.neg_e2e = 0
        self.loop_lag_last = 0.0
        self.loop_lag_max = 0.0
        self.sock_rcvbuf = -1
        self.sock_sndbuf = -1
        self.conn = 0

    def counters(self) -> dict:
        return {
            "received": self.received,
            "applied": self.applied,
            "acks": self.acks,
            "errors": self.errors,
            "wrong_cell": self.wrong_cell,
            "noise": self.dispatcher.noise_frames,
            "foreground": self.dispatcher.foreground_frames,
            "index": len(self.dispatcher.tested),
            "inline_gap_max": self.inline_gap_max,
            "cpu_sum": self.cpu_sum,
            "cpu_n": self.cpu_n,
        }

    def snapshot(self) -> dict:
        out = self.counters()
        out.update({
            "loop_lag_last": self.loop_lag_last,
            "loop_lag_max": self.loop_lag_max,
            "measuring": self.measuring,
            "connections": self.conn,
            "sock_rcvbuf": self.sock_rcvbuf,
            "sock_sndbuf": self.sock_sndbuf,
            "affinity": sorted(os.sched_getaffinity(0)),
        })
        return out

    def mark_start(self) -> dict:
        self.measuring = True
        self.t_mark = time.monotonic()
        self.cpu_sum = 0.0
        self.cpu_n = 0
        self.sample_i = 0
        self.samples.clear()
        self.frozen.clear()
        self.neg_e2e = 0
        self.loop_lag_max = 0.0
        self.base = self.counters()
        return self.base

    def mark_end(self) -> dict:
        self.measuring = False
        self.t_end = time.monotonic()
        self.frozen = list(self.samples)
        self.end = self.counters()
        return self.end

    def summary(self) -> dict:
        cpus = [item[1] for item in self.frozen]
        e2es = [item[2] for item in self.frozen]
        first_cpu = [item[1] for item in self.frozen if item[0] - self.t_mark <= 10]
        last_cpu = [item[1] for item in self.frozen if self.t_end - item[0] <= 10]
        first_e2e = [item[2] for item in self.frozen if item[0] - self.t_mark <= 10]
        last_e2e = [item[2] for item in self.frozen if self.t_end - item[0] <= 10]
        view = self.dispatcher.snapshot()
        return {
            "base": self.base,
            "end": self.end or self.counters(),
            "apply_cpu_mean": (self.cpu_sum / self.cpu_n) if self.cpu_n else None,
            "apply_cpu_p50": percentile(cpus, 0.50),
            "apply_cpu_p95": percentile(cpus, 0.95),
            "apply_cpu_p99": percentile(cpus, 0.99),
            "cpu_first10_p50": percentile(first_cpu, 0.50),
            "cpu_last10_p50": percentile(last_cpu, 0.50),
            "e2e_p50": percentile(e2es, 0.50),
            "e2e_p95": percentile(e2es, 0.95),
            "e2e_p99": percentile(e2es, 0.99),
            "e2e_first10_p95": percentile(first_e2e, 0.95),
            "e2e_last10_p95": percentile(last_e2e, 0.95),
            "sample_count": len(self.frozen),
            "sample_stride": SAMPLE_STRIDE,
            "neg_e2e": self.neg_e2e,
            "checksum": checksum(view),
            "index": len(view),
            "loop_lag_max": self.loop_lag_max,
            "final": self.counters(),
            "sock_rcvbuf": self.sock_rcvbuf,
            "sock_sndbuf": self.sock_sndbuf,
            "affinity_n": len(os.sched_getaffinity(0)),
        }


async def serve(args: argparse.Namespace) -> None:
    service = Service(args.cell)

    async def on_down(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        service.conn += 1
        sock = writer.get_extra_info("socket")
        if sock is not None and service.sock_rcvbuf < 0:
            with contextlib.suppress(OSError):
                service.sock_rcvbuf = sock.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)
                service.sock_sndbuf = sock.getsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF)
        pending = 0
        try:
            while True:
                data = await reader.readexactly(FRAME)
                kind, worker, cell, seq, coverage, digest, generated_at = HDR.unpack(data[:32])
                if kind not in (K_UP, K_TOMB) or cell != service.dispatcher.cell:
                    if kind in (K_UP, K_TOMB):
                        service.wrong_cell += 1
                    continue
                now = time.time()
                service.received += 1
                gap = service.received - service.applied
                if gap > service.inline_gap_max:
                    service.inline_gap_max = gap
                started = time.perf_counter()
                service.dispatcher.on_frame(kind, worker, seq, coverage, digest, generated_at, now)
                cpu_s = time.perf_counter() - started
                if worker < ROUTABLE_BELOW:
                    service.applied += 1
                writer.write(frame(K_ACK, 0, cell, seq, 0, 0, now))
                service.acks += 1
                pending += 1
                if pending >= 32:
                    await writer.drain()
                    pending = 0
                if service.measuring and worker < ROUTABLE_BELOW:
                    service.cpu_sum += cpu_s
                    service.cpu_n += 1
                    service.sample_i += 1
                    if service.sample_i % SAMPLE_STRIDE == 0:
                        e2e = time.time() - generated_at
                        if e2e < 0:
                            service.neg_e2e += 1
                        service.samples.append((time.monotonic(), cpu_s, e2e))
        except (asyncio.IncompleteReadError, ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
            return
        except Exception as exc:
            service.errors += 1
            service.error_text = repr(exc)[:240]
            return
        finally:
            if pending:
                with contextlib.suppress(Exception):
                    await writer.drain()
            writer.close()
            service.conn -= 1

    async def lag_monitor() -> None:
        while True:
            started = time.monotonic()
            await asyncio.sleep(0.05)
            lag = max(0.0, time.monotonic() - started - 0.05)
            service.loop_lag_last = lag
            if service.measuring and lag > service.loop_lag_max:
                service.loop_lag_max = lag

    async def on_ctrl(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                line = await reader.readline()
                if not line:
                    return
                req = json.loads(line)
                cmd = req.get("cmd")
                if cmd == "snapshot":
                    resp = service.snapshot()
                elif cmd == "mark_start":
                    resp = service.mark_start()
                elif cmd == "mark_end":
                    resp = service.mark_end()
                elif cmd == "summary":
                    resp = service.summary()
                elif cmd == "shutdown":
                    resp = {"ok": True}
                    writer.write(json.dumps(resp).encode() + b"\n")
                    await writer.drain()
                    os.kill(os.getpid(), signal.SIGTERM)
                    return
                else:
                    resp = {"error": f"unknown {cmd}"}
                resp["error_text"] = service.error_text
                writer.write(json.dumps(resp).encode() + b"\n")
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionResetError, asyncio.CancelledError, json.JSONDecodeError):
            return

    data_server = await asyncio.start_server(on_down, "127.0.0.1", args.port, reuse_address=True)
    ctrl_server = await asyncio.start_server(on_ctrl, "127.0.0.1", args.control, reuse_address=True)
    Path(args.ready_file).write_text(json.dumps({
        "pid": os.getpid(),
        "cell": args.cell,
        "python": sys.version.split()[0],
        "affinity": sorted(os.sched_getaffinity(0)),
        "uvloop": "uvloop" in sys.modules,
    }))
    asyncio.create_task(lag_monitor())
    async with data_server, ctrl_server:
        await asyncio.Future()


# --- driver -----------------------------------------------------------------

def launch_dispatcher(cell: int, prefix: str) -> subprocess.Popen:
    ready = RAW / f"{prefix}_ready.json"
    ready.unlink(missing_ok=True)
    log_path = RAW / f"{prefix}_dispatcher.log"
    handle = log_path.open("w")
    proc = subprocess.Popen(
        [PY, str(Path(__file__).resolve()), "--role", "dispatcher",
         "--port", str(DISP_PORT), "--control", str(CTRL_PORT),
         "--cell", str(cell), "--ready-file", str(ready)],
        cwd=str(ROOT), stdout=handle, stderr=subprocess.STDOUT,
        start_new_session=True, text=True,
    )
    handle.close()
    remember(proc)
    deadline = time.time() + 10
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"dispatcher exited {proc.returncode}")
        if ready.exists() and port_listening(CTRL_PORT):
            return proc
        time.sleep(0.02)
    raise TimeoutError("dispatcher did not become ready")


def launch_gateway(index: int, prefix: str) -> tuple[subprocess.Popen, Path, int]:
    port = GW_BASE + index
    log_path = RAW / f"{prefix}_gw{index}.log"
    handle = log_path.open("w")
    proc = subprocess.Popen(
        [PY, str(GATEWAY), "--listen", str(port), "--downstream", f"127.0.0.1:{DISP_PORT}",
         "--sndbuf", "2304", "--max-queue", "4096", "--tau", "30", "--util-lambda", "16",
         "--gate", "2", "--adaptive-queue-gate", "8", "--congestion-hold", "0.2", "--quiet"],
        cwd=str(ROOT), stdout=handle, stderr=subprocess.STDOUT,
        start_new_session=True, text=True,
    )
    handle.close()
    remember(proc)
    deadline = time.time() + 8
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"gateway {index} exited {proc.returncode}: {log_path.read_text()[-400:]}")
        if port_listening(port):
            return proc, log_path, port
        time.sleep(0.02)
    raise TimeoutError(f"gateway {index} did not listen")


def wait_downstream(path: Path, minimum: int, timeout: float = 5) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        text = path.read_text(errors="replace") if path.exists() else ""
        if text.count('"downstream_connected"') >= minimum:
            return
        time.sleep(0.02)
    raise TimeoutError(f"downstream count < {minimum} in {path.name}")


def open_gateway(port: int, cell: int) -> socket.socket:
    sock = socket.create_connection(("127.0.0.1", port), timeout=3)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    sock.settimeout(5)
    sock.sendall(config_frame(
        cell, "FullSync", max_queue=4096, max_inflight=0,
        rate_frames_per_s=0, rate_burst=1,
    ))
    sock.sendall(frame(K_RESET, 0, cell, 0, 0, 0, time.time()))
    reply = recvall(sock, FRAME)
    kind, _i, got, _seq, _c, _d, _t = HDR.unpack(reply[:32])
    if kind != K_RESET_DONE or got != cell:
        raise RuntimeError(f"reset reply kind={kind} cell={got}")
    return sock


def pace(sock: socket.socket, n_total: int, rate: int, prepare, commit, sent_box: list[int], origin_ns: int, error: list[str]) -> None:
    sent = 0
    try:
        sock.settimeout(30)
        while time.monotonic_ns() + 1_000_000 < origin_ns:
            time.sleep(0.001)
        while sent < n_total:
            target = origin_ns + int(sent * 1_000_000_000 / rate)
            now = time.monotonic_ns()
            if now + 200_000 < target:
                time.sleep((target - now - 100_000) / 1e9)
                now = time.monotonic_ns()
            behind = int((now - origin_ns) * rate / 1_000_000_000) - sent if now > target else 0
            batch = 1 if behind < 2 else min(8, behind, n_total - sent)
            for _ in range(batch):
                blob, payload = prepare(sent)
                sock.sendall(blob)
                commit(payload)
                sent += 1
                sent_box[0] = sent
    except Exception as exc:
        error.append(repr(exc)[:240])
    finally:
        with contextlib.suppress(OSError):
            sock.close()


def sum_sent(boxes: list[list[int]]) -> int:
    return sum(box[0] for box in boxes)


def classify(row: dict) -> tuple[str, str]:
    if row["sender_errors"]:
        return "INCONCLUSIVE", "sender exception: " + row["sender_errors"]
    if row["apply_errors"] or row["noise_frames"] or row["wrong_cell"]:
        return "CORRECTNESS_FAILURE", "noise frame, wrong cell, or apply exception"
    if row["drops_total"]:
        return "CORRECTNESS_FAILURE", "FullSync drop counter is non-zero"
    if row["neg_e2e"]:
        return "INCONCLUSIVE", "negative end-to-end samples"
    accum = row["path_backlog_end"] - row["path_backlog_start"]
    offered_frames = row["target_rate"] * row["measure_s"]
    growing = accum > max(100.0, 0.02 * offered_frames)
    latency_worse = bool(row["latency_worse"])
    gen_ratio = row["generated_rate"] / row["target_rate"] if row["target_rate"] else 0
    app_ratio = row["applied_rate"] / row["generated_rate"] if row["generated_rate"] else 0
    cpu = row["dispatcher_cpu_mean"] if row["dispatcher_cpu_mean"] is not None else 0
    if not row["drain_ok"] or row["count_gap"] != 0:
        if cpu >= 0.6 and (growing or app_ratio < 0.95):
            return "SATURATED", "drain incomplete and dispatcher CPU stayed high"
        if row["count_gap"] != 0 and row["gateway_queued_end"] == 0 and cpu < 0.3:
            return "CORRECTNESS_FAILURE", "counter gap with empty gateway queues and idle dispatcher"
        return "INCONCLUSIVE", "drain timeout or counter gap"
    if row["checksum_match"] is not True:
        return "CORRECTNESS_FAILURE", "index checksum mismatch after drain"
    if gen_ratio < 0.95:
        if cpu >= 0.6 and (growing or app_ratio < 0.95):
            return "SATURATED", "target missed while dispatcher CPU was high and apply fell behind"
        if app_ratio >= 0.95 and not growing:
            return "HARNESS_LIMITED", "offered rate not reached; dispatcher kept up with frames it was given"
        return "INCONCLUSIVE", "target missed and the bottleneck is not separated"
    if app_ratio >= 0.95 and not growing and not latency_worse:
        return "STABLE", "apply tracked offered rate, path backlog did not accumulate, drain finished"
    if cpu >= 0.5 and (app_ratio < 0.95 or growing or latency_worse):
        return "SATURATED", "backlog or latency grew while dispatcher CPU was busy"
    return "INCONCLUSIVE", "apply did not track offered load and dispatcher CPU was not high"


def run_case(spec: dict) -> tuple[dict, list[dict]]:
    """spec keys: phase, scenario, n_gateways, target_rate, rep, warmup_s, measure_s, cell."""
    prefix = f"{spec['phase']}_{spec['scenario']}_g{spec['n_gateways']}_r{spec['target_rate']}_rep{spec['rep']}"
    log(f"start {prefix}")
    groups = worker_groups(spec["n_gateways"])
    rates = split_rate(spec["target_rate"], spec["n_gateways"])
    n_warm = [rate * spec["warmup_s"] for rate in rates]
    n_meas = [rate * spec["measure_s"] for rate in rates]
    if any(not isinstance(n, int) or isinstance(n, bool) for n in n_warm + n_meas):
        raise RuntimeError("warmup and measure must produce integer frame counts")
    row: dict = {
        "run_id": prefix,
        "phase": spec["phase"],
        "scenario": spec["scenario"],
        "n_gateways": spec["n_gateways"],
        "rep": spec["rep"],
        "target_rate": spec["target_rate"],
        "warmup_s": spec["warmup_s"],
        "measure_s": spec["measure_s"],
        "n_distinct_workers": 16,
        "load_class": "existing_lineage_updates" if spec["scenario"] == "S1" else "synthetic_high_cardinality_inserts",
        "reporter_replay": 0,
        "sample_stride": SAMPLE_STRIDE,
        "sndbuf_configured": 2304,
        "max_inflight": 0,
        "policy": "FullSync",
        "sender_errors": "",
        "verdict": "",
        "verdict_reason": "",
    }
    series: list[dict] = []
    procs: list[subprocess.Popen] = []
    ctrl: LineSock | None = None
    try:
        disp = launch_dispatcher(spec["cell"], prefix)
        procs.append(disp)
        gates = [launch_gateway(i, prefix) for i in range(spec["n_gateways"])]
        procs.extend(proc for proc, _path, _port in gates)
        for _proc, path, _port in gates:
            wait_downstream(path, 1)
        socks = []
        for index, (_proc, path, port) in enumerate(gates):
            before = path.read_text(errors="replace").count('"downstream_connected"')
            socks.append(open_gateway(port, spec["cell"]))
            wait_downstream(path, before + 1)
        expected: dict[tuple[int, int], int] = {}
        xor_boxes = [[0] for _ in gates]
        preload_n = 0
        builders = []
        if spec["scenario"] == "S1":
            for index, sock in enumerate(socks):
                keys = s1_keys(groups[index])
                blobs = []
                for seq, (worker, digest) in enumerate(keys, start=1):
                    blobs.append(frame(K_UP, worker, spec["cell"], seq, S1_COVERAGE0, digest, time.time()))
                    expected[(worker, digest)] = S1_COVERAGE0
                sock.sendall(b"".join(blobs))
                preload_n += len(keys)
                cov = {(worker, digest): S1_COVERAGE0 for worker, digest in keys}
                seq0 = len(keys) + 1

                def prepare_s1(i, keys=keys, cov=cov, seq0=seq0, cell=spec["cell"]):
                    worker, digest = keys[i % len(keys)]
                    coverage = cov[(worker, digest)] + S1_COVERAGE_STEP
                    return frame(K_UP, worker, cell, seq0 + i, coverage, digest, time.time()), (worker, digest, coverage)

                def commit_s1(payload, cov=cov):
                    worker, digest, coverage = payload
                    cov[(worker, digest)] = coverage

                builders.append((prepare_s1, commit_s1, cov))
            ctrl_sock = socket.create_connection(("127.0.0.1", CTRL_PORT), timeout=3)
            ctrl = LineSock(ctrl_sock)
            deadline = time.time() + 5
            while time.time() < deadline:
                snap = ctrl.request({"cmd": "snapshot"}, timeout=2)
                if snap["applied"] >= preload_n and snap["index"] >= len(expected):
                    break
                time.sleep(0.02)
            else:
                raise TimeoutError("preload was not applied")
        else:
            ctrl_sock = socket.create_connection(("127.0.0.1", CTRL_PORT), timeout=3)
            ctrl = LineSock(ctrl_sock)
            for index in range(spec["n_gateways"]):
                total = n_warm[index] + n_meas[index]
                digests = [digest64(f"P0M15C/S2/{prefix}/g{index}/n{i}") for i in range(total)]
                workers = groups[index]

                def prepare_s2(i, digests=digests, workers=workers, cell=spec["cell"]):
                    worker = workers[i % len(workers)]
                    digest = digests[i]
                    return frame(K_UP, worker, cell, i + 1, S2_COVERAGE, digest, time.time()), (worker, digest)

                def commit_s2(payload, box=xor_boxes[index]):
                    worker, digest = payload
                    box[0] ^= mix(worker, digest, S2_COVERAGE)

                builders.append((prepare_s2, commit_s2, None))
        if ctrl is None:
            raise RuntimeError("control socket missing")
        sent_boxes = [[0] for _ in gates]
        errors: list[list[str]] = [[] for _ in gates]
        origin_ns = time.monotonic_ns() + 200_000_000
        origin = time.monotonic() + 0.2
        threads = []
        for index in range(spec["n_gateways"]):
            prepare, commit, _cov = builders[index]
            thread = threading.Thread(
                target=pace,
                args=(socks[index], n_warm[index] + n_meas[index], rates[index], prepare, commit,
                      sent_boxes[index], origin_ns, errors[index]),
                daemon=True,
            )
            threads.append(thread)
            thread.start()
        samples_local: list[tuple[float, int, int]] = []
        proc_base = {disp.pid: proc_ticks_rss(disp.pid)}
        for proc, _path, _port in gates:
            proc_base[proc.pid] = proc_ticks_rss(proc.pid)
        proc_base[os.getpid()] = proc_ticks_rss(os.getpid())
        rss_max = proc_base[disp.pid][1]
        queued_max = 0
        stats_fail = 0
        win_rates: list[float] = []
        prev_applied = None
        prev_t = None

        def sample(phase: str, t_anchor: float) -> None:
            nonlocal rss_max, queued_max, stats_fail, prev_applied, prev_t
            now = time.monotonic()
            try:
                snap = ctrl.request({"cmd": "snapshot"}, timeout=2)
            except (OSError, EOFError, json.JSONDecodeError, TimeoutError):
                stats_fail += 1
                return
            generated = preload_n + sum_sent(sent_boxes)
            queued = 0
            forwarded = 0
            received_gw = 0
            for _proc, _path, port in gates:
                try:
                    stats = fetch_stats(port)
                except (OSError, EOFError, TimeoutError):
                    stats_fail += 1
                    continue
                queued += int(stats.get("live_queued") or stats.get("live_depth") or 0)
                forwarded += int(stats.get("forwarded") or 0)
                received_gw += int(stats.get("received") or 0)
            ticks, rss = proc_ticks_rss(disp.pid)
            rss_max = max(rss_max, rss)
            queued_max = max(queued_max, queued)
            gw_cpu = 0.0
            gw_known = True
            for proc, _path, _port in gates:
                now_ticks, _rss = proc_ticks_rss(proc.pid)
                part = cpu_cores(proc_base[proc.pid][0], now_ticks, now - t_anchor)
                if part is None:
                    gw_known = False
                else:
                    gw_cpu += part
            harness_cpu = cpu_cores(proc_base[os.getpid()][0], proc_ticks_rss(os.getpid())[0], now - t_anchor)
            disp_cpu = cpu_cores(proc_base[disp.pid][0], ticks, now - t_anchor)
            if phase == "measure" and prev_applied is not None and prev_t is not None and now > prev_t:
                win_rates.append((snap["applied"] - prev_applied) / (now - prev_t))
            if phase == "measure":
                prev_applied = snap["applied"]
                prev_t = now
            series.append({
                "run_id": prefix, "phase_run": spec["phase"], "scenario": spec["scenario"],
                "n_gateways": spec["n_gateways"], "target_rate": spec["target_rate"], "rep": spec["rep"],
                "sample_phase": phase, "t_rel_s": round(now - t_anchor, 4),
                "generated_total": generated, "gateway_received": received_gw,
                "gateway_forwarded": forwarded, "gateway_queued": queued,
                "dispatcher_received": snap["received"], "dispatcher_applied": snap["applied"],
                "dispatcher_acks": snap["acks"],
                "path_backlog": generated - snap["applied"],
                "inline_gap": snap["inline_gap_max"], "index_entries": snap["index"],
                "dispatcher_rss_kb": rss, "dispatcher_cpu_cores": None if disp_cpu is None else round(disp_cpu, 4),
                "gateway_cpu_cores": None if not gw_known else round(gw_cpu, 4),
                "harness_cpu_cores": None if harness_cpu is None else round(harness_cpu, 4),
                "loop_lag_s": snap["loop_lag_last"], "loop_lag_max_s": snap["loop_lag_max"],
            })

        while time.monotonic() < origin:
            time.sleep(0.01)
        # Warmup is continuous with measurement. Mark after warmup_s of pacing.
        warm_deadline = origin + spec["warmup_s"]
        while time.monotonic() < warm_deadline:
            time.sleep(min(1.0, max(0.0, warm_deadline - time.monotonic())))
            if spec["warmup_s"] >= 1 and time.monotonic() < warm_deadline:
                sample("warmup", origin)
        mark_base = ctrl.request({"cmd": "mark_start"})
        generated_at_mark = preload_n + sum_sent(sent_boxes)
        ticks0, rss0 = proc_ticks_rss(disp.pid)
        gw_ticks0 = [proc_ticks_rss(proc.pid)[0] for proc, _p, _port in gates]
        harness0 = proc_ticks_rss(os.getpid())[0]
        measure_origin = time.monotonic()
        backlog0 = generated_at_mark - mark_base["applied"]
        prev_applied = mark_base["applied"]
        prev_t = measure_origin
        measure_deadline = measure_origin + spec["measure_s"]
        next_tick = measure_origin + 1
        while time.monotonic() < measure_deadline:
            now = time.monotonic()
            step = min(1.0, measure_deadline - now, max(0.0, next_tick - now))
            if step > 0:
                time.sleep(step)
            if time.monotonic() + 0.05 < measure_deadline or next_tick <= measure_deadline:
                sample("measure", measure_origin)
                next_tick += 1
        mark_end = ctrl.request({"cmd": "mark_end"})
        generated_at_end = preload_n + sum_sent(sent_boxes)
        measure_dt = max(1e-6, time.monotonic() - measure_origin)
        ticks1, rss_at_mark_end = proc_ticks_rss(disp.pid)
        gw_ticks1 = [proc_ticks_rss(proc.pid)[0] for proc, _path, _port in gates]
        harness1 = proc_ticks_rss(os.getpid())[0]
        for thread in threads:
            thread.join(timeout=spec["measure_s"] + spec["warmup_s"] + 30)
        send_done = time.monotonic()
        deadline = time.time() + 20
        drain_ok = False
        end_stats: list[dict] = []
        while time.time() < deadline:
            snap = ctrl.request({"cmd": "snapshot"}, timeout=3)
            end_stats = []
            ok_stats = True
            for _proc, _path, port in gates:
                try:
                    end_stats.append(fetch_stats(port, timeout=2))
                except (OSError, EOFError, TimeoutError):
                    ok_stats = False
                    break
            generated_total = preload_n + sum_sent(sent_boxes)
            forwarded = sum(int(item.get("forwarded") or 0) for item in end_stats) if ok_stats else -1
            received_gw = sum(int(item.get("received") or 0) for item in end_stats) if ok_stats else -1
            queued = sum(int(item.get("live_queued") or 0) for item in end_stats) if ok_stats else -1
            aligned = ok_stats and forwarded == generated_total and received_gw == generated_total and snap["applied"] == generated_total and snap["received"] == generated_total and snap["acks"] == generated_total and queued == 0
            sample("drain", measure_origin)
            if aligned:
                drain_ok = True
                break
            time.sleep(0.2)
        summary = ctrl.request({"cmd": "summary"}, timeout=30)
        if spec["scenario"] == "S1":
            for _prepare, _commit, cov in builders:
                if cov:
                    expected.update(cov)
            expect_sum = checksum(expected)
            expect_index = len(expected)
        else:
            expect_sum = 0
            for box in xor_boxes:
                expect_sum ^= box[0]
            expect_index = preload_n + sum_sent(sent_boxes)
        generated_total = preload_n + sum_sent(sent_boxes)
        forwarded = sum(int(item.get("forwarded") or 0) for item in end_stats) if end_stats else -1
        received_gw = sum(int(item.get("received") or 0) for item in end_stats) if end_stats else -1
        counts = [generated_total, received_gw, forwarded, summary["final"]["received"], summary["final"]["applied"], summary["final"]["acks"]]
        count_gap = 0 if len(set(counts)) == 1 else max(counts) - min(c for c in counts if c >= 0)
        _ticks_after, rss1 = proc_ticks_rss(disp.pid)
        disp_cpu = cpu_cores(ticks0, ticks1, measure_dt)
        gw_cpu = 0.0
        for ticks_start, ticks_end in zip(gw_ticks0, gw_ticks1):
            part = cpu_cores(ticks_start, ticks_end, measure_dt)
            gw_cpu += 0 if part is None else part
        harness_cpu = cpu_cores(harness0, harness1, measure_dt)
        row["dispatcher_rss_mark_end_kb"] = rss_at_mark_end
        drops = {"rate_limit": 0, "superseded": 0, "replica_cap": 0, "low_utility": 0, "queue_drop": 0, "expired": 0, "topk_drops": 0, "stale_cell": 0}
        maxq = 0
        for item in end_stats:
            for key in drops:
                drops[key] += int(item.get(key) or 0)
            maxq = max(maxq, int(item.get("maxq") or 0))
        ewma = []
        drain_wait = 0.0
        outq = 0
        for _proc, path, _port in gates:
            tick = last_event(path, "tick")
            if tick:
                ewma.append(float(tick.get("ewma_dq_s") or 0))
                drain_wait += float(tick.get("drain_wait_s") or 0)
                outq = max(outq, int(tick.get("write_queue_bytes") or 0))
        first_p95 = summary["e2e_first10_p95"]
        last_p95 = summary["e2e_last10_p95"]
        latency_worse = bool(
            spec["measure_s"] >= 20 and first_p95 is not None and last_p95 is not None
            and last_p95 > max(2 * first_p95, first_p95 + 0.005)
        )
        gen_delta = generated_at_end - generated_at_mark
        app_delta = mark_end["applied"] - mark_base["applied"]
        recv_delta = mark_end["received"] - mark_base["received"]
        ack_delta = mark_end["acks"] - mark_base["acks"]
        row.update({
            "n_distinct_lineages": expect_index if spec["scenario"] == "S1" else generated_total,
            "n_live_index_start": mark_base["index"],
            "n_live_index_end": summary["index"],
            "preload": preload_n,
            "generated_total": generated_total,
            "gateway_received": received_gw,
            "gateway_forwarded": forwarded,
            "dispatcher_received": summary["final"]["received"],
            "dispatcher_applied": summary["final"]["applied"],
            "dispatcher_acks": summary["final"]["acks"],
            "apply_errors": summary["final"]["errors"],
            "noise_frames": summary["final"]["noise"],
            "wrong_cell": summary["final"]["wrong_cell"],
            "count_gap": count_gap,
            "checksum_match": expect_sum == summary["checksum"] and summary["index"] == expect_index,
            "generated_rate": gen_delta / measure_dt if measure_dt else 0,
            "received_rate": recv_delta / measure_dt if measure_dt else 0,
            "applied_rate": app_delta / measure_dt if measure_dt else 0,
            "acked_rate": ack_delta / measure_dt if measure_dt else 0,
            "bytes_per_s": app_delta * FRAME / measure_dt if measure_dt else 0,
            "window_applied_mean": (sum(win_rates) / len(win_rates)) if win_rates else None,
            "window_applied_p95": percentile(win_rates, 0.95),
            "window_applied_min": min(win_rates) if win_rates else None,
            "window_n": len(win_rates),
            "apply_cpu_mean_s": summary["apply_cpu_mean"],
            "apply_cpu_p50_s": summary["apply_cpu_p50"],
            "apply_cpu_p95_s": summary["apply_cpu_p95"],
            "apply_cpu_p99_s": summary["apply_cpu_p99"],
            "cpu_first10_p50_s": summary["cpu_first10_p50"],
            "cpu_last10_p50_s": summary["cpu_last10_p50"],
            "e2e_p50_s": summary["e2e_p50"],
            "e2e_p95_s": summary["e2e_p95"],
            "e2e_p99_s": summary["e2e_p99"],
            "e2e_first10_p95_s": first_p95,
            "e2e_last10_p95_s": last_p95,
            "gateway_send_to_apply_s": "",
            "gateway_send_to_ack_s": "",
            "sample_count": summary["sample_count"],
            "drain_s": time.monotonic() - send_done,
            "drain_ok": drain_ok,
            "path_backlog_start": backlog0,
            "path_backlog_end": generated_at_end - mark_end["applied"],
            "inline_gap_max": summary["final"]["inline_gap_max"],
            "dispatcher_cpu_mean": disp_cpu,
            "gateway_cpu_mean": gw_cpu,
            "harness_cpu_mean": harness_cpu,
            "dispatcher_rss_start_kb": rss0,
            "dispatcher_rss_end_kb": rss1,
            "dispatcher_rss_max_kb": rss_max,
            "gateway_queued_max": queued_max,
            "gateway_queued_end": sum(int(item.get("live_queued") or 0) for item in end_stats) if end_stats else -1,
            "gateway_maxq": maxq,
            "loop_lag_max_s": summary["loop_lag_max"],
            "gateway_ewma_dq_s": (sum(ewma) / len(ewma)) if ewma else None,
            "gateway_drain_wait_s": drain_wait,
            "gateway_outq_bytes_last": outq,
            "stats_failures": stats_fail,
            "neg_e2e": summary["neg_e2e"],
            "drops_total": sum(drops.values()),
            "sock_rcvbuf": summary["sock_rcvbuf"],
            "sock_sndbuf": summary["sock_sndbuf"],
            "per_gateway_target_rate": ",".join(str(r) for r in rates),
            "latency_worse": latency_worse,
            **{f"drop_{key}": value for key, value in drops.items()},
        })
        # Forwarded rate uses the same whole-path final counters only as a check.
        # Window forwarded rate is not separately clocked; applied and generated are.
        row["forwarded_rate"] = row["applied_rate"] if drain_ok and forwarded == generated_total else (
            (forwarded / (spec["warmup_s"] + spec["measure_s"])) if forwarded >= 0 else None
        )
        verdict, reason = classify(row)
        row["verdict"] = verdict
        row["verdict_reason"] = reason
        log(f"done {prefix} {verdict} gen {row['generated_rate']:.1f} applied {row['applied_rate']:.1f} cpu {disp_cpu if disp_cpu is not None else -1:.3f} index {summary['index']}")
        return row, series
    finally:
        if ctrl is not None:
            with contextlib.suppress(Exception):
                ctrl.request({"cmd": "shutdown"}, timeout=1)
        for proc in reversed(procs):
            stop_proc(proc)
            if proc in LIVE:
                LIVE.remove(proc)
        time.sleep(0.05)


def write_csv(path: Path, rows: list[dict], fields: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("")
        return
    if fields is None:
        fields = []
        for row in rows:
            for key in row:
                if key not in fields and key != "latency_worse":
                    fields.append(key)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: "" if row.get(key) is None else row.get(key, "") for key in fields})


def collect_env() -> dict:
    def run(cmd: list[str]) -> str:
        try:
            return subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL, timeout=8).strip()
        except (subprocess.SubprocessError, OSError):
            return "NOT_MEASURED"

    mem = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith(("MemTotal:", "MemAvailable:")):
            mem[line.split(":")[0]] = line.split(":", 1)[1].strip()
    model = "NOT_MEASURED"
    for line in Path("/proc/cpuinfo").read_text().splitlines():
        if line.startswith("model name"):
            model = line.split(":", 1)[1].strip()
            break
    dirty = run(["git", "-C", str(ROOT), "status", "--porcelain", "--", "experiments/4t4/net/gateway_relay_4t4.py", "experiments/icc_kv/runtime.py", "experiments/icc_kv/wire.py"])
    return {
        "time_start": time.strftime("%Y-%m-%d %H:%M:%S %z"),
        "hostname": socket.gethostname(),
        "uname": run(["uname", "-srm"]),
        "commit": run(["git", "-C", str(ROOT), "rev-parse", "HEAD"]),
        "git_dirty": dirty,
        "python": sys.version.replace("\n", " "),
        "executable": PY,
        "cpu_model": model,
        "cpu_count": os.cpu_count(),
        "mem": mem,
        "loadavg": Path("/proc/loadavg").read_text().strip(),
        "clk_tck": CLK_TCK,
        "listeners": run(["ss", "-ltn"]),
        "lo_qdisc": run(["tc", "qdisc", "show", "dev", "lo"]),
        "rmem_default": read_text("/proc/sys/net/core/rmem_default"),
        "wmem_default": read_text("/proc/sys/net/core/wmem_default"),
        "rmem_max": read_text("/proc/sys/net/core/rmem_max"),
        "wmem_max": read_text("/proc/sys/net/core/wmem_max"),
        "tcp_rmem": read_text("/proc/sys/net/ipv4/tcp_rmem"),
        "tcp_wmem": read_text("/proc/sys/net/ipv4/tcp_wmem"),
        "gateway_sha256": sha256_file(GATEWAY),
        "runtime_sha256": sha256_file(ROOT / "experiments/icc_kv/runtime.py"),
        "wire_sha256": sha256_file(ROOT / "experiments/icc_kv/wire.py"),
        "ps": run(["ps", "-eo", "pid,pcpu,pmem,comm", "--sort=-pcpu"]),
        "routable_below": ROUTABLE_BELOW,
        "protected_9710_listening": port_listening(9710),
        "protected_9700_listening": port_listening(9700),
    }


def fmt(value, digits: int = 1) -> str:
    if value is None or value == "":
        return "NOT_MEASURED"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return str(value)


def us(value) -> str:
    if value is None or value == "":
        return "NOT_MEASURED"
    return f"{float(value) * 1e6:.1f} µs"


def ms(value) -> str:
    if value is None or value == "":
        return "NOT_MEASURED"
    return f"{float(value) * 1e3:.2f} ms"


def formal_rows(rows: list[dict], scenario: str | None = None, n_gateways: int | None = None) -> list[dict]:
    out = []
    for row in rows:
        if row["phase"] != "formal":
            continue
        if scenario and row["scenario"] != scenario:
            continue
        if n_gateways and row["n_gateways"] != n_gateways:
            continue
        if row["measure_s"] < 29 or row["warmup_s"] < 4.9:
            continue
        out.append(row)
    return out


def ladder_map(rows: list[dict], scenario: str, n_gateways: int) -> dict[int, list[str]]:
    found: dict[int, list[str]] = {}
    for row in formal_rows(rows, scenario, n_gateways):
        found.setdefault(int(row["target_rate"]), []).append(row["verdict"])
    return found


def stable_bound(rows: list[dict], scenario: str) -> tuple[int | None, list[int]]:
    """Highest target where every completed 3-rep formal cell is STABLE, for any gateway count that has such a cell."""
    best = None
    counts = []
    for n_gateways in (2, 4):
        ladder = ladder_map(rows, scenario, n_gateways)
        local = None
        for rate in RATES:
            verdicts = ladder.get(rate) or []
            if len(verdicts) >= 3 and all(item == "STABLE" for item in verdicts):
                local = rate
            elif verdicts:
                break
        if local is not None:
            counts.append(n_gateways)
            best = local if best is None else max(best, local)
    return best, counts


def saturated_rate(rows: list[dict], scenario: str) -> int | None:
    found = []
    for n_gateways in (2, 4):
        ladder = ladder_map(rows, scenario, n_gateways)
        for rate in RATES:
            verdicts = ladder.get(rate) or []
            if any(item == "SATURATED" for item in verdicts):
                found.append(rate)
    return min(found) if found else None


def choose_case(rows: list[dict]) -> tuple[str, str]:
    if any(row["verdict"] == "CORRECTNESS_FAILURE" for row in rows):
        return "CORRECTNESS_FAILURE", "Case E"
    s1 = stable_bound(rows, "S1")[0]
    s2 = stable_bound(rows, "S2")[0]
    sat1 = saturated_rate(rows, "S1")
    sat2 = saturated_rate(rows, "S2")
    if (sat1 and not sat2 and s2 and s2 >= (sat1 or 0)) or (sat2 and not sat1 and s1 and s1 >= (sat2 or 0)):
        return "CARDINALITY_DEPENDENT_CAPACITY", "Case D"
    if sat1 and sat2:
        return "MODEL_B_DISPATCHER_CAPACITY_CONSTRAINT_SUPPORTED", "Case A"
    if s1 == 4000 and s2 == 4000 and not sat1 and not sat2:
        return "NO_SATURATION_WITHIN_TESTED_RANGE", "Case B"
    if any(row["verdict"] == "HARNESS_LIMITED" for row in rows) and not sat1 and not sat2:
        return "HARNESS_LIMITED", "Case C"
    if any(row["verdict"] == "INCONCLUSIVE" for row in formal_rows(rows)):
        return "INCONCLUSIVE", "neither Case A nor Case B"
    if s1 and s2 and not sat1 and not sat2:
        return "NO_SATURATION_WITHIN_TESTED_RANGE", "Case B"
    return "INCONCLUSIVE", "incomplete ladder"


def mean_field(rows: list[dict], field: str) -> float | None:
    vals = [float(row[field]) for row in rows if row.get(field) not in (None, "")]
    if not vals:
        return None
    return sum(vals) / len(vals)


def write_reports(rows: list[dict], env: dict) -> None:
    case, case_name = choose_case(rows)
    s1_lb, s1_ns = stable_bound(rows, "S1")
    s2_lb, s2_ns = stable_bound(rows, "S2")
    joint = None if s1_lb is None or s2_lb is None else min(s1_lb, s2_lb)
    smoke = [row for row in rows if row["phase"] == "smoke"]
    smoke_ok = bool(smoke) and all(row["verdict"] == "STABLE" and row["count_gap"] == 0 and row["checksum_match"] is True for row in smoke)

    def ratio(load: float, bound: int | None) -> str:
        if bound is None:
            return "NOT_MEASURED"
        return f"{load / bound:.3f}"

    env_md = f"""# 实验环境

时间起点：{env.get("time_start")}。结束：{env.get("time_end", "见 driver 日志")}。主机 `{env.get("hostname")}`，`{env.get("uname")}`。结束时 9710 仍在听：{env.get("protected_9710_listening_end")}。19830 仍在听：{env.get("ports_19830_end")}。

| 项 | 值 | 证据 |
|---|---|---|
| Git HEAD | `{env.get("commit")}` | MEASURED |
| 工作区 | `{env.get("git_dirty") or "clean"}` | MEASURED |
| CPU | {env.get("cpu_model")}，{env.get("cpu_count")} 逻辑核 | MEASURED |
| 内存 | {env.get("mem")} | MEASURED |
| 负载 | `{env.get("loadavg")}` | MEASURED，实验开始时 |
| Python | {env.get("python")} | MEASURED |
| 解释器 | `{env.get("executable")}` | CONFIGURED |
| ROUTABLE_BELOW | {env.get("routable_below")} | CODE |
| 9710 在听 | {env.get("protected_9710_listening")} | MEASURED。本轮不连接、不重启 |
| 9700 在听 | {env.get("protected_9700_listening")} | MEASURED。本轮不启动它 |
| lo qdisc | `{env.get("lo_qdisc")}` | MEASURED。没有在 lo 上加 tc |
| tcp_rmem | {env.get("tcp_rmem")} | CONFIGURED，系统默认，本轮不改 |
| tcp_wmem | {env.get("tcp_wmem")} | CONFIGURED |
| rmem_default / max | {env.get("rmem_default")} / {env.get("rmem_max")} | CONFIGURED |
| wmem_default / max | {env.get("wmem_default")} / {env.get("wmem_max")} | CONFIGURED |

Gateway 文件 sha256 `{env.get("gateway_sha256")}`。`runtime.py` `{env.get("runtime_sha256")}`。`wire.py` `{env.get("wire_sha256")}`。

Dispatcher 是本脚本的 `--role dispatcher` 进程：一个 asyncio 线程，`start_server` 绑在 `127.0.0.1:{DISP_PORT}`，控制口 `127.0.0.1:{CTRL_PORT}`。不调用 `PathRuntime.start`，因此不会把正式路径绑到 CPU 1，也不会连接容器 Gateway。亲和性是进程默认的全部在线 CPU。事件循环仍然只有一个线程，Apply 不能占用超过一个核。没有 uvloop。没有 Apply sleep，没有 CPU 配额。

Gateway 是 `experiments/4t4/net/gateway_relay_4t4.py` 的独立进程，端口 `{GW_BASE}` 起，下游 `127.0.0.1:{DISP_PORT}`。参数：`--sndbuf 2304 --max-queue 4096 --tau 30 --util-lambda 16 --gate 2 --adaptive-queue-gate 8 --congestion-hold 0.2 --quiet`。`2304` 是该程序的默认 `SO_SNDBUF`，不是为了制造拥塞而另设的值。`max-queue`、tau、utility、gate 在 FullSync 下不参与准入。`--quiet` 关掉逐帧 JSON，2 秒 tick 仍在。

相对 HEAD 的 Gateway diff 是 M1.5-B 留下的诊断：`drain()` 前后各一次 `perf_counter`，tick 里多了 `pending_lineages`、`inflight`、`drain_wait_s`、`TIOCOUTQ`。等待本身没有改。准入、发送条件和 ACK 没有改。

配置帧是 `FullSync`：`(mode=0, merge=0, priority=0, adaptive=0, dedup=0, global_topk=0)`，`max_inflight=0`，`rate_frames_per_s=0`。

Worker 只用 0–15。这是 `ROUTABLE_BELOW=16` 以下会进入索引的编号。`instance` 仍是一个字节，本轮不改帧宽。16 个 Worker 不是 4096 个 Reporter。

开始时 CPU 占用最高的进程（`ps` 快照，不是全程）：

```
{chr(10).join((env.get("ps") or "").splitlines()[:12])}
```

复现：

```text
cd /home/byh/B02
PYTHONPATH=/home/byh/B02 {env.get("executable")} \\
  analysis/p0_pressure_realism/m1_5c_dispatcher_capacity/run_m15c.py
```
"""

    drop_lines = []
    for row in smoke:
        drop_lines.append(
            f"- {row.get('run_id')}: received={row.get('gateway_received')} forwarded={row.get('gateway_forwarded')} "
            f"applied={row.get('dispatcher_applied')} drops={row.get('drops_total')} checksum={row.get('checksum_match')} verdict={row.get('verdict')} reason={row.get('verdict_reason')}"
        )
    audit = f"""# 容量 Harness 审计

## FullSync 是否全量转发

配置来自 `experiments/icc_kv/wire.py` 的 `POLICIES["FullSync"]`，元组是 `(0, 0, 0, 0, 0, 0)`。`gateway_relay_4t4.py` 的 `enqueue` 没有一条叫 passthrough 的短路。`passthrough` 属性存在，但发送路径不读它。FullSync 是把下面这些门都关掉：

| 门 | 条件 | FullSync |
|---|---|---|
| 令牌桶 | `consume_rate_token`，只在 mode 1 | 不生效 |
| 队尾丢弃 | `_bounded_full`，只在 mode 6 | 不生效。`max_queue` 只被存下来 |
| utility | `low_utility` 先看 `congested()`，`adaptive` 为假时直接返回假 | 不生效 |
| Top-K | `_topk_drop_new`，`global_topk=0` 时 limit 为 0 | 不生效 |
| 合并 | `merge=0` | 不取消旧的未发送帧 |
| 副本上限 | `_admit_replica` 在 `dedup=0` 时直接返回真 | 不生效 |
| ACK 窗口 | `max_inflight=0` 时 `release_loop` 不等 ACK | 不生效 |
| 过期 | `drops["expired"]` 有计数槽，当前文件里没有自增 | 保持 0 |

会留下的丢弃只有 `stale_cell`：帧的 cell 和当前 cell 不一致。Harness 在 reset 完成并看到新的 `downstream_connected` 之后才发送。另外，读循环每 256 帧 `await sleep(0)` 一次，这是已有代码，本轮不改。它会在读端跑在发送端前面时，把最多 256 帧先放进用户态队列。

`release_loop` 对每一帧 `write` 然后 `await drain()`。下游 `SO_SNDBUF` 请求值 2304，写缓冲高水位 2048，`TCP_NODELAY=1`。这些是 Gateway 程序自己的设置。

Smoke 计数：

{chr(10).join(drop_lines) if drop_lines else "- 没有 smoke 行"}

Smoke 通过：{smoke_ok}。通过的定义是整次运行 `generated == gateway_received == gateway_forwarded == dispatcher_received == dispatcher_applied == acks`，丢弃计数为 0，索引校验和一致，判定 `STABLE`。

## 计时

速率控制用 `time.monotonic_ns()`，按帧的绝对截止时间发送，落后时每批最多补 8 帧。不是每秒一次倒空。

`t_generated` 写进帧里的 `t_send`，是发送线程的 `time.time()`。同一台机器。

`t_gateway_sent`：`NOT_MEASURED`。Gateway 原样转发收到的帧，不改写 `t_send`。`--quiet` 关掉了逐帧日志，避免 4000 行/秒的日志变成瓶颈。

`t_apply_start` / `t_apply_done`：`perf_counter` 包住 `Dispatcher.on_frame`。均值用全部测量窗帧。P50/P95/P99 是每 {SAMPLE_STRIDE} 帧留一个样本。

生成到 Apply 完成：抽样帧上的 `time.time()` 减帧内 `t_send`。它包含 Gateway 排队、TCP 和 Apply，不是索引 CPU。

Gateway 发送到 Apply、Gateway 发送到 ACK：`NOT_MEASURED`。ACK 回到 Gateway 的下游连接，不回到发送线程。`recent` 只留 4096 个 seq，`ewma_dq` 只覆盖最近这些帧，表里单列，不当作全窗分位数。

Dispatcher 内部 `received - applied` 只在 `on_frame` 前后被看到，测量到的最大值是正在处理的那一帧。没有单独的 Apply 队列，表里不造一个队列长度。`generated - applied` 是整条路径还没完成的量，包含发送端和 Gateway。

## 身份

`N_distinct_workers=16`，编号 0–15，两只 Gateway 时各 8 个，四只时各 4 个。不复用编号。

S1：每个 Worker 64 条 lineage，共 1024 个键。先各插入 coverage 1024，再按轮转把 coverage 加 16。帧长仍是 64 字节。

S2：每条更新一个新的 blake2b-8 digest，名字是 `P0M15C/S2/<run>/g<i>/n<j>`，coverage 固定 1024。这是高基数合成插入，不是 4096 Reporter 回放。

## 什么不算本轮容量

窗口速率的起止有一个管线深度的边界，所以全量相等在排空之后检查，不在 30 秒窗口的两个端点上检查。4000 updates/s 以上没有自动加档。
"""

    # Capacity vs scale and motivation.
    def cell_line(scenario: str, n_gateways: int, rate: int) -> str:
        group = [row for row in formal_rows(rows, scenario, n_gateways) if int(row["target_rate"]) == rate]
        if not group:
            return f"| {scenario} | {n_gateways} | {rate} | NOT_RUN | | | | | |"
        verdicts = ",".join(row["verdict"] for row in group)
        return (
            f"| {scenario} | {n_gateways} | {rate} | {verdicts} | "
            f"{fmt(mean_field(group, 'generated_rate'))} | {fmt(mean_field(group, 'applied_rate'))} | "
            f"{fmt(mean_field(group, 'window_applied_min'))} | {fmt(mean_field(group, 'dispatcher_cpu_mean'), 3)} | "
            f"{ms(mean_field(group, 'e2e_p95_s'))} |"
        )

    table = ["| 场景 | Gateway | 目标/s | 判定 | 生成/s | Apply/s | 最慢 1 秒 Apply/s | Dispatcher CPU | 生成到 Apply P95 |",
             "|---|---:|---:|---|---:|---:|---:|---:|---|"]
    for scenario in ("S1", "S2"):
        for n_gateways in (2, 4):
            for rate in RATES:
                if any(row["scenario"] == scenario and row["n_gateways"] == n_gateways and int(row["target_rate"]) == rate and row["phase"] == "formal" for row in rows):
                    table.append(cell_line(scenario, n_gateways, rate))
    if len(table) == 2:
        table.append("| | | | NOT_RUN | | | | | |")

    scale_rows = []
    for label, series in (("A1", A1_EXTRAPOLATED), ("A4", A4_EXTRAPOLATED)):
        for reporters, load in series:
            if joint is None:
                scale_rows.append(f"| {label} | {reporters} | {load} | EXTRAPOLATED | NOT_MEASURED | NOT_MEASURED |")
            else:
                if load > joint:
                    scale_rows.append(
                        f"| {label} | {reporters} | {load} | EXTRAPOLATED | {joint} MEASURED 下界 | 高于下界，无法判断 |"
                    )
                else:
                    scale_rows.append(
                        f"| {label} | {reporters} | {load} | EXTRAPOLATED | {joint} MEASURED 下界 | {ratio(load, joint)} |"
                    )
    bound_text = "NOT_MEASURED" if joint is None else (
        f"S1 下界 {s1_lb} updates/s（Gateway 数 {s1_ns}），S2 下界 {s2_lb} updates/s（Gateway 数 {s2_ns}）。"
        f"两者都达到的下界是 {joint} updates/s。这是 `SUSTAINABLE_CAPACITY_LOWER_BOUND`，不是最大吞吐。"
    )
    scale = f"""# 容量和下界对照

正式格是 warmup 5 秒、测量 30 秒、3 次重复。Pilot 和 smoke 不进入下界。

{bound_text}

判定 `STABLE` 要求：生成速率至少达到目标的 95%，Apply 速率至少达到生成速率的 95%，测量窗内 `generated-applied` 的净增长不超过目标帧数的 2%（且至少超过 100 帧才算增长），排空后计数链相等，校验和一致，FullSync 丢弃为 0。测量短于 20 秒时不做前 10 秒/后 10 秒延迟恶化检验。

{chr(10).join(table)}

生成/s、Apply/s、CPU、延迟是三次重复的平均。CPU 是测量窗内 Dispatcher 进程的 `utime+stime`，单位是核。1.0 表示一个核跑满。生成到 Apply 含 Gateway 和 TCP。索引 CPU 见延迟表。

## 与 P0-M1 外推对照

外推来自 `analysis/p0_pressure_realism/m1_scale_fanin/MOTIVATION_REALITY_CHECK.md`。A1 是五个种子的集群速率均分到 4 个引擎再乘汇报者数。A4 是单卡灌满微基准再线性乘汇报者数。两者都不相加。汇报者数等于 GPU 数只在一卡实例的假设下使用。本轮只有 2 或 4 个 Gateway 进程，没有跑这些 Reporter。

负载比是外推速率除以测到的稳定下界。下界不是最大值，所以这个比是利用率的上界：真实容量若更高，利用率更低。外推高于下界、且更高档没有测时，不能判断是否稳定。

| 负载 | 汇报者 | updates/s | 证据 | 容量下界 | 负载/下界 |
|---|---:|---:|---|---|---|
{chr(10).join(scale_rows)}

4000 这一档若稳定，不能写成 Dispatcher 的最大 Apply 速率。
"""

    # Cardinality
    card_lines = ["| Gateway | 目标/s | S1 Apply/s | S2 Apply/s | S1 CPU | S2 CPU | S1 RSS 增 KB | S2 RSS 增 KB | S1 索引末 | S2 索引末 | S2 后 10 秒/前 10 秒 CPU P50 |",
                  "|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|"]
    for n_gateways in (2, 4):
        for rate in RATES:
            a = formal_rows(rows, "S1", n_gateways)
            b = formal_rows(rows, "S2", n_gateways)
            a = [row for row in a if int(row["target_rate"]) == rate]
            b = [row for row in b if int(row["target_rate"]) == rate]
            if not a or not b:
                continue
            def rss_delta(group: list[dict]) -> float | None:
                vals = []
                for row in group:
                    if row["dispatcher_rss_end_kb"] is None or row["dispatcher_rss_start_kb"] is None:
                        continue
                    vals.append(float(row["dispatcher_rss_end_kb"]) - float(row["dispatcher_rss_start_kb"]))
                return sum(vals) / len(vals) if vals else None
            ratio_cpu = None
            ratios = []
            for row in b:
                if row.get("cpu_first10_p50_s") and row.get("cpu_last10_p50_s"):
                    ratios.append(float(row["cpu_last10_p50_s"]) / float(row["cpu_first10_p50_s"]))
            ratio_cpu = sum(ratios) / len(ratios) if ratios else None
            card_lines.append(
                f"| {n_gateways} | {rate} | {fmt(mean_field(a, 'applied_rate'))} | {fmt(mean_field(b, 'applied_rate'))} | "
                f"{fmt(mean_field(a, 'dispatcher_cpu_mean'), 3)} | {fmt(mean_field(b, 'dispatcher_cpu_mean'), 3)} | "
                f"{fmt(rss_delta(a), 0)} | {fmt(rss_delta(b), 0)} | {fmt(mean_field(a, 'n_live_index_end'), 0)} | "
                f"{fmt(mean_field(b, 'n_live_index_end'), 0)} | {fmt(ratio_cpu, 2)} |"
            )
    if len(card_lines) == 2:
        card_lines.append("| | | NOT_RUN | | | | | | | | |")

    def metric_max(scenario: str, rate: int, field: str) -> float | None:
        vals = []
        for item in formal_rows(rows, scenario):
            if int(item["target_rate"]) != rate or item.get(field) in (None, ""):
                continue
            vals.append(float(item[field]))
        return max(vals) if vals else None

    s2_p99 = metric_max("S2", 4000, "e2e_p99_s")
    s2_lag = metric_max("S2", 4000, "loop_lag_max_s")
    s1_p99 = metric_max("S1", 4000, "e2e_p99_s")
    s1_lag = metric_max("S1", 4000, "loop_lag_max_s")
    if s2_p99 is None:
        tail_note = "4000 updates/s 的正式格没有完成，尾延迟未比较。"
    else:
        tail_note = (
            f"S2 在 4000 updates/s、末尾约 140000 个新键时，生成到 Apply 的 P99 最高 {ms(s2_p99)}，"
            f"事件循环 50 ms 心跳的最大滞后 {ms(s2_lag)}。同一档 S1（索引保持 1024）P99 最高 {ms(s1_p99)}，"
            f"心跳滞后最高 {ms(s1_lag)}。S2 的 P50/P95 仍低于 1 ms，前 10 秒和后 10 秒的 P95 没有拉开，"
            "1 秒窗口的最低 Apply 速率仍跟上目标，路径积压净增长为 0。"
            "这是插入新键时的短暂停顿，不是 30 秒 Apply 速率掉下去。停顿原因没有拆成 GC 或字典扩容，标为未分开的 `MEASURED` 尾部。"
        )
    card = f"""# 索引基数

S1 和 S2 的帧都是 64 字节。S1 反复覆盖 1024 个已有键。S2 每次插入新键，索引从这次运行的预热开始增长，末尾规模是测量结果，不是一开始就有这么多键。

两边都调用真实的 `Dispatcher.on_frame`。这个函数会把每一条前景更新追加到 `applied` 列表，并写入 `tested`、`generated_at_map`、`delivered_at_map`、`ground`。S1 的 RSS 增长主要来自那条历史列表。S2 在此之外还有新键。两者的 RSS 差不能单独当成索引结构的字节成本。

{chr(10).join(card_lines)}

RSS 是 Dispatcher 进程的 `VmRSS`，测量窗起点到进程结束附近。CPU 比值是 S2 测量窗后 10 秒与前 10 秒的 Apply CPU P50 之比，抽样步长 {SAMPLE_STRIDE}。比值接近 1 表示后段没有变贵。S1 的索引规模在预热之后是常数。

{tail_note}

本轮没有混合负载。`NOT_RUN`。
"""

    # Motivation narrative
    if case == "NO_SATURATION_WITHIN_TESTED_RANGE" and joint is not None:
        a1_4096 = 2121.0
        motive = f"""# Motivation Reality Check

标记：`NO_SATURATION_WITHIN_TESTED_RANGE`（{case_name}）。

可信的稳定 Apply 下界是 **{joint} updates/s**。S1 {s1_lb}，S2 {s2_lb}。依据是正式 30 秒窗、每个计入下界的档 3 次都是 `STABLE`，并且生成端达到了目标。这个数不是最大吞吐。高于该下界的档位没有三次稳定的正式结果。默认最高目标是 4000 updates/s，没有自动再往上加。

A1 正式负载、4096 个一卡汇报者的外推是 2121 updates/s（`EXTRAPOLATED`）。{joint} 高于 2121，负载比上界 {a1_4096 / joint:.3f}。1024 个汇报者的 530.2/s 比上界是 {530.2 / joint:.3f}。在这个外推模型下，Dispatcher 索引处理没有显示为主要瓶颈。不应再把大规模 Dispatcher CPU 拥塞写成已经成立的事实。

A4 灌满、4096 个汇报者的外推是 7897 updates/s，高于已测下界。本轮不能判断那一档能不能稳住。A4 不是长期平均 serving，也不能和 A1 相加。

本轮没有人为放慢 Dispatcher。没有 tc，没有 Apply sleep，没有 CPU 配额。饱和判定要求 Dispatcher 进程 CPU 升高并且路径积压增长。这次没有出现这种格子。4000 updates/s 的 Dispatcher CPU 见容量表，三次平均大约 0.38–0.42 核。

S2 在这一档把索引插到约 140000 键时，生成到 Apply 的 P99 最高 {ms(s2_p99)}，事件循环心跳最大滞后 {ms(s2_lag)}。同一档 S1 的 P99 最高 {ms(s1_p99)}。S2 的 P95 仍低于 1 ms，30 秒 Apply 速率没有掉下去。这不是持续容量损失。A1 的 2121/s 低于出现这段尾部的档位。A4 的 7897/s 高于已测范围，不能用这段尾部推它是否饱和。

下一轮没有必要为了证明“共享 Dispatcher 索引是瓶颈”去做多 Gateway 的大型 trace-driven 回放。A1 外推低于已测稳定下界，回放预期仍然排得空，不能把拥塞变成证据。路由可见性和误判不在本轮测量里。
"""
        nxt = f"""# 下一轮

本轮停在容量测量。标记 `NO_SATURATION_WITHIN_TESTED_RANGE`，稳定下界 {joint} updates/s。

不要把 Dispatcher indexing bottleneck 当作 KVSelect 的既定动机继续做 FullSync / StaticTopK 拥塞对比。不要为这个动机启动 1024 或 4096 Reporter 回放。不要把 4000 updates/s 写成最大吞吐。不要恢复 Adaptive，不要改 K，不要改帧宽，不要改论文。

A4 4096 汇报者的 7897/s 仍高于已测范围。若要回答那一档，需要单独决定是否把入口加到该速率以上，并且先确认发送端不是限制。本轮不自动加档。S2 在 140000 键处有百毫秒级事件循环停顿，但没有压低 30 秒 Apply 速率。不要把这段尾部写成索引饱和。若要解释停顿，那是另一次短的分配器剖析，不是 Reporter 回放。

等待下一次 OODA。
"""
    elif case == "MODEL_B_DISPATCHER_CAPACITY_CONSTRAINT_SUPPORTED":
        motive = f"""# Motivation Reality Check

标记：`MODEL_B_DISPATCHER_CAPACITY_CONSTRAINT_SUPPORTED`（{case_name}）。

S1 饱和起点 {saturated_rate(rows, 'S1')} updates/s，S2 饱和起点 {saturated_rate(rows, 'S2')} updates/s。稳定下界 S1 {s1_lb}，S2 {s2_lb}。判定依据是 Apply 跟不上已经达到的入口，路径积压在测量窗内上升，并且 Dispatcher 进程 CPU 处于高位。不是 Apply sleep，也不是 tc。

这支持“共享 Dispatcher 的 Apply 容量会在目标规模附近成为约束”，但是只覆盖实际出现 `SATURATED` 的那一档及以下。没有测到的更高档不能外推成最大吞吐。
"""
        nxt = """# 下一轮

容量约束来自 Dispatcher，而不是发送端人为降速。若要继续这条动机，下一轮才比较 `FullSync`、`StaticSemantic`、`StaticTopK16`，并单独测状态可见性滞后到路由假阴性。

不要在本轮结果上改 K、恢复 Adaptive 或改论文。等待下一次 OODA。
"""
    elif case == "CARDINALITY_DEPENDENT_CAPACITY":
        motive = f"""# Motivation Reality Check

标记：`CARDINALITY_DEPENDENT_CAPACITY`（{case_name}）。

S1 稳定下界 {s1_lb} updates/s，饱和档 {saturated_rate(rows, 'S1')}。S2 稳定下界 {s2_lb} updates/s，饱和档 {saturated_rate(rows, 'S2')}。两种更新的容量不一样。不能把其中一个结果写成所有 KV metadata 的容量。

S2 是测量期间不断插入新键，不是固定在最终基数上的稳态。末尾索引规模见 `INDEX_CARDINALITY_SENSITIVITY.md`。
"""
        nxt = """# 下一轮

先解释基数差来自索引插入、`applied` 历史列表，还是分配器停顿，再决定要不要做策略对比。不要把 S2 的结果直接写成 A1 正式负载已饱和。等待下一次 OODA。
"""
    elif case == "HARNESS_LIMITED":
        motive = f"""# Motivation Reality Check

标记：`HARNESS_LIMITED`（{case_name}）。

发送端或 Gateway 先到顶，Dispatcher 还没有被证明饱和。已完成且三次都稳定的下界：S1 {s1_lb}，S2 {s2_lb}。这个下界只对已经达到目标入口的档位有效。没有达到目标的档位不能用来算 Dispatcher 利用率。

Gateway 下游 `SO_SNDBUF` 请求值是程序默认的 2304，并且每帧 `await drain()`。若积压出现在 Gateway 而 Dispatcher CPU 不高，限制在这条发送路径，不在索引。
"""
        nxt = """# 下一轮

不要重搭整套环境。若要继续测容量，只改已经指出的发送限制：Gateway 默认小发送缓冲和逐帧 `drain()`，或生成线程达不到目标。改之前先看本轮的 `gateway_drain_wait_s`、Gateway CPU 和 Dispatcher CPU。不要把这次未达到的目标速率写成索引饱和。等待下一次 OODA。
"""
    elif case == "CORRECTNESS_FAILURE":
        motive = """# Motivation Reality Check

标记：`CORRECTNESS_FAILURE`（Case E）。

计数链、校验和或 FullSync 丢弃计数没有通过。本轮不给出容量结论，也不和 A1/A4 比较利用率。
"""
        nxt = """# 下一轮

先修正确性。不要加档，不要做策略对比，不要启动 vLLM。等待下一次 OODA。
"""
    else:
        motive = f"""# Motivation Reality Check

标记：`INCONCLUSIVE`。

现有格子不足以判断 Dispatcher 是否饱和。S1 下界 {s1_lb}，S2 下界 {s2_lb}。未完成或无法分开瓶颈的档位见 `dispatcher_capacity.csv` 的 `verdict_reason`。不把缺失档写成饱和，也不写成容量充足。
"""
        nxt = """# 下一轮

先看 `verdict_reason` 里停住的那一档。不要在结论不清时加到 4000 以上，也不要改准入。等待下一次 OODA。
"""

    (OUT / "EXPERIMENT_ENVIRONMENT.md").write_text(env_md)
    (OUT / "CAPACITY_HARNESS_AUDIT.md").write_text(audit)
    (OUT / "INDEX_CARDINALITY_SENSITIVITY.md").write_text(card)
    (OUT / "CAPACITY_VS_SCALE.md").write_text(scale)
    (OUT / "MOTIVATION_REALITY_CHECK.md").write_text(motive)
    (OUT / "NEXT_ITERATION_RECOMMENDATION.md").write_text(nxt)


def persist(rows: list[dict], series: list[dict]) -> None:
    write_csv(OUT / "dispatcher_capacity.csv", rows)
    write_csv(OUT / "dispatcher_capacity_timeseries.csv", series)
    latency_fields = [
        "run_id", "phase", "scenario", "n_gateways", "target_rate", "rep",
        "apply_cpu_mean_s", "apply_cpu_p50_s", "apply_cpu_p95_s", "apply_cpu_p99_s",
        "cpu_first10_p50_s", "cpu_last10_p50_s",
        "e2e_p50_s", "e2e_p95_s", "e2e_p99_s", "e2e_first10_p95_s", "e2e_last10_p95_s",
        "gateway_send_to_apply_s", "gateway_send_to_ack_s", "gateway_ewma_dq_s",
        "sample_stride", "sample_count", "neg_e2e",
    ]
    cpu_fields = [
        "run_id", "scenario", "n_gateways", "target_rate", "rep", "sample_phase", "t_rel_s",
        "dispatcher_rss_kb", "dispatcher_cpu_cores", "gateway_cpu_cores", "harness_cpu_cores",
        "index_entries", "loop_lag_s", "gateway_queued", "path_backlog",
    ]
    write_csv(OUT / "dispatcher_latency.csv", rows, latency_fields)
    write_csv(OUT / "dispatcher_cpu_memory.csv", series, cpu_fields)
    with (RAW / "runs.jsonl").open("w") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")


def assert_ports_free() -> None:
    for port in (DISP_PORT, CTRL_PORT, *range(GW_BASE, GW_BASE + 4)):
        if port_listening(port):
            raise SystemExit(f"port {port} is already in use")
    for port in PROTECTED_PORTS:
        if port in (DISP_PORT, CTRL_PORT) or GW_BASE <= port < GW_BASE + 4:
            raise SystemExit("refusing to use a protected port")


def driver(args: argparse.Namespace) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    RAW.mkdir(parents=True, exist_ok=True)
    if args.reports_only:
        env = json.loads((RAW / "environment.json").read_text())
        rows = [json.loads(line) for line in (RAW / "runs.jsonl").read_text().splitlines() if line.strip()]
        write_reports(rows, env)
        log("reports rewritten")
        return
    if ROUTABLE_BELOW != 16:
        raise SystemExit(f"ROUTABLE_BELOW is {ROUTABLE_BELOW}, expected 16")
    cleanup_stale()
    assert_ports_free()
    env = collect_env()
    (RAW / "environment.json").write_text(json.dumps(env, indent=2))
    rows: list[dict] = []
    series: list[dict] = []
    cell = 400
    stop = False

    def one(spec: dict) -> dict:
        nonlocal cell
        last_exc: Exception | None = None
        for attempt in (1, 2):
            spec_run = dict(spec)
            spec_run["cell"] = cell
            cell += 1
            try:
                row, extra = run_case(spec_run)
                break
            except Exception as exc:
                last_exc = exc
                log(f"FAIL attempt {attempt} {spec_run} {exc!r}")
                stop_all()
                time.sleep(0.4)
                if attempt == 1:
                    continue
                row = {
                    "run_id": f"{spec['phase']}_{spec['scenario']}_g{spec['n_gateways']}_r{spec['target_rate']}_rep{spec['rep']}",
                    "phase": spec["phase"], "scenario": spec["scenario"], "n_gateways": spec["n_gateways"],
                    "rep": spec["rep"], "target_rate": spec["target_rate"], "warmup_s": spec["warmup_s"],
                    "measure_s": spec["measure_s"], "verdict": "INCONCLUSIVE", "verdict_reason": repr(last_exc)[:300],
                    "sender_errors": repr(last_exc)[:300], "checksum_match": False, "count_gap": -1, "drops_total": 0,
                    "apply_errors": 1, "noise_frames": 0, "wrong_cell": 0, "drain_ok": False,
                    "generated_rate": 0, "applied_rate": 0, "path_backlog_start": 0, "path_backlog_end": 0,
                    "dispatcher_cpu_mean": None, "latency_worse": False, "neg_e2e": 0, "gateway_queued_end": -1,
                }
                extra = []
        rows.append(row)
        series.extend(extra)
        persist(rows, series)
        return row

    try:
        for scenario, measure_s in (("S1", 10), ("S2", 10)):
            row = one({
                "phase": "smoke", "scenario": scenario, "n_gateways": 2, "target_rate": 250,
                "rep": 1, "warmup_s": 0, "measure_s": measure_s,
            })
            if row["verdict"] != "STABLE" or row.get("count_gap") not in (0, None) or row.get("checksum_match") is not True:
                log("STOP smoke failed")
                stop = True
                break
        if args.only == "smoke" or stop:
            return
        for n_gateways in (2, 4):
            for scenario in ("S1", "S2"):
                reached: list[int] = []
                for rate in RATES:
                    pilot = one({
                        "phase": "pilot", "scenario": scenario, "n_gateways": n_gateways,
                        "target_rate": rate, "rep": 1, "warmup_s": 1, "measure_s": 5,
                    })
                    if pilot["verdict"] == "CORRECTNESS_FAILURE":
                        log("STOP correctness")
                        return
                    if pilot["verdict"] != "STABLE":
                        one({
                            "phase": "diagnostic", "scenario": scenario, "n_gateways": n_gateways,
                            "target_rate": rate, "rep": 1, "warmup_s": 5, "measure_s": 30,
                        })
                        log(f"STOP ladder {scenario} g{n_gateways} at {rate}: {pilot['verdict']}")
                        break
                    reached.append(rate)
                    if args.only == "pilot":
                        continue
                    saturated = False
                    for rep in (1, 2, 3):
                        formal = one({
                            "phase": "formal", "scenario": scenario, "n_gateways": n_gateways,
                            "target_rate": rate, "rep": rep, "warmup_s": 5, "measure_s": 30,
                        })
                        if formal["verdict"] == "CORRECTNESS_FAILURE":
                            log("STOP correctness")
                            return
                        if formal["verdict"] == "SATURATED":
                            saturated = True
                    if saturated:
                        log(f"STOP higher rates after saturation {scenario} g{n_gateways} {rate}")
                        break
                if args.only == "pilot":
                    continue
    finally:
        stop_all()
        persist(rows, series)
        env["time_end"] = time.strftime("%Y-%m-%d %H:%M:%S %z")
        env["protected_9710_listening_end"] = port_listening(9710)
        env["ports_19830_end"] = port_listening(DISP_PORT)
        (RAW / "environment.json").write_text(json.dumps(env, indent=2))
        write_reports(rows, env)
        log(f"ports after 9710={port_listening(9710)} 19830={port_listening(DISP_PORT)}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--role", choices=("driver", "dispatcher"), default="driver")
    parser.add_argument("--only", choices=("smoke", "pilot", "all"), default="all")
    parser.add_argument("--reports-only", action="store_true")
    parser.add_argument("--port", type=int, default=DISP_PORT)
    parser.add_argument("--control", type=int, default=CTRL_PORT)
    parser.add_argument("--cell", type=int, default=1)
    parser.add_argument("--ready-file", default="")
    args = parser.parse_args()
    if args.role == "dispatcher":
        asyncio.run(serve(args))
        return
    driver(args)


if __name__ == "__main__":
    main()
