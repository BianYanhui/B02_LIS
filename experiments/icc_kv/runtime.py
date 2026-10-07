"""Fixed-capacity Gateway/Dispatcher path for the ICC KV coordination plan.

The HTB ceiling is set once, far above the CPU path, and is not retuned per
utilization point. Gateway CPU quota stays at one core for the whole matrix.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import re
import socket
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from experiments.icc_kv.wire import (
    FRAME, HDR, K_ACK, K_RESET, K_RESET_DONE, K_STATS, K_STATS2, K_STATS_REQ, K_TOMB, K_UP,
    POLICY_MAX_QUEUE, STATS, STATS2, config_frame, frame,
)

ROOT = Path("/home/byh/B02")
NET = ROOT / "experiments/4t4/net"
OUT = ROOT / "analysis/icc_kv"
RELAY_PORT = 9710
DISPATCH_PORT = 9711
# One fixed ceiling. Utilization is later L/C, never a new tc rate.
FIXED_LINK_BIT_S = 1_000_000_000
ACTIVE_LINK_BIT_S = FIXED_LINK_BIT_S
MAX_QUEUE = 4096
MAX_INFLIGHT = 0
# Harness noise uses worker ids at or above this value. Those frames are
# counted and are not part of the placement view.
ROUTABLE_BELOW = 16


_TC_UNITS = {"bit": 1, "kbit": 1_000, "mbit": 1_000_000, "gbit": 1_000_000_000}


def parse_tc_rate(text: str, classid: str = "1:1") -> int:
    """Parse `tc class show` rate for one class. 0 when the class is absent."""
    for line in text.splitlines():
        match = re.search(
            rf"class htb {re.escape(classid)} .*?\brate (\d+(?:\.\d+)?)([KMG]?bit)\b",
            line,
        )
        if match:
            return int(round(float(match.group(1)) * _TC_UNITS[match.group(2).lower()]))
    return 0


def tc_link_bit_s() -> int:
    """0 when docker or tc cannot be read. Callers that configure the link still reject 0."""
    try:
        text = subprocess.check_output(
            ["docker", "exec", "b02-gateway4t4", "tc", "class", "show", "dev", "eth0"],
            text=True, timeout=10,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return 0
    return parse_tc_rate(text)


def git_dirty_files() -> list[str]:
    """Tracked edits under experiments/. Untracked result files are ignored."""
    try:
        out = subprocess.check_output(
            ["git", "-C", str(ROOT), "status", "--porcelain", "--untracked-files=no", "--", "experiments"],
            text=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return ["git-status-failed"]
    return [line[3:] for line in out.splitlines() if len(line) > 3]


def require_clean_tree(allow_dirty: bool = False) -> list[str]:
    dirty = git_dirty_files()
    if dirty and not allow_dirty:
        raise SystemExit("experiments/ has uncommitted changes: " + ", ".join(dirty))
    return dirty


def gateway_source_sha256() -> str:
    return hashlib.sha256((NET / "gateway_relay_4t4.py").read_bytes()).hexdigest()


def check_gateway_source() -> str:
    remote = subprocess.check_output(
        ["docker", "exec", "b02-gateway4t4", "sha256sum", "/opt/gateway_relay_4t4.py"],
        text=True,
    ).split()[0]
    local = gateway_source_sha256()
    if remote != local:
        raise RuntimeError(f"gateway source mismatch local={local} container={remote}")
    return remote


def sh(args: list[str]) -> str:
    return subprocess.check_output(args, text=True)


def bridge_ip() -> str:
    net_id = sh(["docker", "network", "inspect", "b02-4t4-net", "-f", "{{.Id}}"]).strip()
    out = sh(["bash", "-c", f"ip -4 -o addr show dev br-{net_id[:12]} | awk '{{print $4}}' | cut -d/ -f1"]).strip()
    if not out:
        raise RuntimeError("cannot determine b02-4t4-net bridge IP")
    return out


def prepare_fixed_gateway(link_bit_s: int | None = None) -> str:
    """Recreate the 4T4 gateway once: 1 CPU, one HTB ceiling, bounded logs.

    link_bit_s stays at the 1 Gbit ceiling unless a run passes one lower
    value for the whole matrix. It is not retuned per rho.
    """
    sh(["bash", str(NET / "setup_net_4t4.sh"), "--rebuild"])
    ip = bridge_ip()
    global ACTIVE_LINK_BIT_S
    link = int(link_bit_s) if link_bit_s else FIXED_LINK_BIT_S
    ACTIVE_LINK_BIT_S = link
    subprocess.check_call(["docker", "rm", "-f", "b02-gateway4t4"], stdout=subprocess.DEVNULL)
    subprocess.check_call([
        "docker", "run", "-d", "--name", "b02-gateway4t4", "--network", "b02-4t4-net",
        "--cap-add", "NET_ADMIN", "--cpus", "1", "--cpuset-cpus", "0",
        "--log-opt", "max-size=20m", "--log-opt", "max-file=2",
        "-p", "127.0.0.1:9710:9710",
        "b02-gw4t4", "--listen", "9710", "--downstream", f"{ip}:9711",
        "--max-queue", str(MAX_QUEUE), "--tau", "30", "--util-lambda", "16",
        "--gate", "2", "--adaptive-queue-gate", "8",
        "--congestion-hold", "0.2", "--quiet",
    ])
    half = link // 2
    subprocess.check_call(["docker", "exec", "b02-gateway4t4", "sh", "-c", f"""
      set -e
      ip link set dev eth0 mtu 296
      tc qdisc replace dev eth0 root handle 1: htb default 20
      tc class add dev eth0 parent 1: classid 1:1 htb rate {link}bit
      tc class add dev eth0 parent 1:1 classid 1:10 htb rate {half}bit ceil {link}bit
      tc class add dev eth0 parent 1:1 classid 1:20 htb rate {half}bit ceil {link}bit
      tc class add dev eth0 parent 1:1 classid 1:30 htb rate {link}bit ceil {link}bit
      tc qdisc add dev eth0 parent 1:10 handle 10: bfifo limit 65536
      tc qdisc add dev eth0 parent 1:20 handle 20: bfifo limit 65536
      tc qdisc add dev eth0 parent 1:30 handle 30: bfifo limit 8192
      tc filter add dev eth0 protocol ip parent 1:0 prio 1 u32 match ip dport 9711 0xffff flowid 1:10
      tc filter add dev eth0 protocol ip parent 1:0 prio 2 u32 match ip dport 5211 0xffff flowid 1:20
      tc filter add dev eth0 protocol ip parent 1:0 prio 3 u32 match ip sport 9710 0xffff flowid 1:30
    """])
    observed = tc_link_bit_s()
    if observed != link:
        raise RuntimeError(f"HTB class 1:1 rate is {observed}, expected {link}")
    check_gateway_source()
    return ip


def git_commit() -> str:
    try:
        return sh(["git", "-C", str(ROOT), "rev-parse", "HEAD"]).strip()
    except subprocess.CalledProcessError:
        return "unknown"


@dataclass
class Applied:
    seq: int
    worker: int
    kind: str
    digest: int
    coverage: int
    generated_at: float
    applied_at: float


@dataclass
class Dispatcher:
    cell: int
    applied: list[Applied] = field(default_factory=list)
    tested: dict[tuple[int, int], int] = field(default_factory=dict)
    generated_at_map: dict[tuple[int, int], float] = field(default_factory=dict)
    delivered_at_map: dict[tuple[int, int], float] = field(default_factory=dict)
    ground: dict[tuple[int, int], tuple[int, bool]] = field(default_factory=dict)
    resets: int = 0
    frames: int = 0
    foreground_frames: int = 0
    noise_frames: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def on_frame(self, kind: int, worker: int, seq: int, coverage: int, digest: int, generated_at: float, now: float) -> None:
        with self.lock:
            self.frames += 1
            if worker >= ROUTABLE_BELOW:
                self.noise_frames += 1
                return
            self.foreground_frames += 1
            if kind == K_UP:
                name = "update"
                self.tested[(worker, digest)] = coverage
                self.generated_at_map[(worker, digest)] = generated_at
                self.delivered_at_map[(worker, digest)] = now
                self.ground[(worker, digest)] = (coverage, True)
            else:
                name = "invalidate"
                self.tested.pop((worker, digest), None)
                self.generated_at_map.pop((worker, digest), None)
                self.delivered_at_map.pop((worker, digest), None)
                self.ground[(worker, digest)] = (0, False)
            self.applied.append(Applied(seq, worker, name, digest, coverage, generated_at, now))

    def snapshot(self) -> dict[tuple[int, int], int]:
        with self.lock:
            return dict(self.tested)

    def placement_maps(self) -> tuple[dict[tuple[int, int], int], dict[tuple[int, int], float], dict[tuple[int, int], float]]:
        with self.lock:
            return dict(self.tested), dict(self.generated_at_map), dict(self.delivered_at_map)

    def set_local(self, worker: int, digest: int, coverage: int) -> None:
        now = time.time()
        with self.lock:
            self.tested[(worker, digest)] = coverage
            self.generated_at_map[(worker, digest)] = now
            self.delivered_at_map[(worker, digest)] = now

    def clear_local(self, worker: int, digest: int) -> None:
        with self.lock:
            self.tested.pop((worker, digest), None)
            self.generated_at_map.pop((worker, digest), None)
            self.delivered_at_map.pop((worker, digest), None)

    def view_timing(self, worker: int, digest: int, now: float) -> tuple[float, float]:
        """Delivery lag of the view entry, and how long ago it was applied. -1 if absent."""
        with self.lock:
            generated = self.generated_at_map.get((worker, digest))
            delivered = self.delivered_at_map.get((worker, digest))
        if generated is None or delivered is None:
            return -1.0, -1.0
        return max(0.0, delivered - generated), max(0.0, now - delivered)


class PathRuntime:
    def __init__(self, link_bit_s: int = 0) -> None:
        self.link_bit_s = int(link_bit_s) if link_bit_s else FIXED_LINK_BIT_S
        self.ip = ""
        self.dispatcher = Dispatcher(cell=-1)
        self._server: asyncio.AbstractServer | None = None
        self.agent = None
        self.fg_agent = None
        self._agent_task: asyncio.Task | None = None
        self._fg_task: asyncio.Task | None = None
        self._down_writers: set[asyncio.StreamWriter] = set()
        self._write_lock = asyncio.Lock()
        self._fg_lock = asyncio.Lock()
        self._stats_wait = asyncio.Event()
        self.last_stats: dict | None = None
        self._stats_partial: dict | None = None

    async def start(self) -> None:
        try:
            os.sched_setaffinity(0, {1})
            self.ip = bridge_ip()
            last_error: OSError | None = None
            for _attempt in range(5):
                try:
                    self._server = await asyncio.start_server(self._on_down, self.ip, DISPATCH_PORT)
                    last_error = None
                    break
                except OSError as exc:
                    if exc.errno != 98:
                        raise
                    last_error = exc
                    await asyncio.sleep(1.0)
            if last_error is not None:
                raise last_error
            reader, writer = await asyncio.open_connection("127.0.0.1", RELAY_PORT)
            sock = writer.get_extra_info("socket")
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.agent = writer
            self._agent_task = asyncio.create_task(self._on_agent(reader))
            fg_reader, fg_writer = await asyncio.open_connection("127.0.0.1", RELAY_PORT)
            fg_sock = fg_writer.get_extra_info("socket")
            fg_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.fg_agent = fg_writer
            self._fg_task = asyncio.create_task(self._on_agent(fg_reader))
        except BaseException:
            await self.stop()
            raise

    async def stop(self) -> None:
        """Release the dispatcher listen socket and the gateway agent."""
        task = self._agent_task
        self._agent_task = None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        agent = self.agent
        self.agent = None
        if agent is not None:
            agent.close()
            with contextlib.suppress(Exception):
                await agent.wait_closed()
        fg_task = self._fg_task
        self._fg_task = None
        if fg_task is not None:
            fg_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await fg_task
        fg_agent = self.fg_agent
        self.fg_agent = None
        if fg_agent is not None:
            fg_agent.close()
            with contextlib.suppress(Exception):
                await fg_agent.wait_closed()
        for writer in list(self._down_writers):
            writer.close()
        self._down_writers.clear()
        server = self._server
        self._server = None
        if server is not None:
            server.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(server.wait_closed(), timeout=5)

    async def _on_agent(self, reader: asyncio.StreamReader) -> None:
        try:
            while True:
                data = await reader.readexactly(FRAME)
                kind, _i, cell, seq, coverage, _d, _t = HDR.unpack(data[:32])
                if kind == K_RESET_DONE and cell == self.dispatcher.cell:
                    self.dispatcher.resets += 1
                elif kind == K_STATS:
                    forwarded, rate, superseded, replica_cap, low_utility, queue_drop, expired, maxq = STATS.unpack(data[32:64])
                    self._stats_partial = {
                        "relay_forwarded": forwarded,
                        "relay_drop_rate_limit": rate,
                        "relay_drop_superseded": superseded,
                        "relay_drop_replica_cap": replica_cap,
                        "relay_drop_low_utility": low_utility,
                        "relay_drop_queue_drop": queue_drop,
                        "relay_drop_expired": expired,
                        "relay_max_queue": maxq,
                        "relay_queued": int(seq),
                        "relay_received": int(coverage),
                    }
                elif kind == K_STATS2:
                    (global_topk, stale_cell, ghost_queued, live_queued, congested_entries,
                     congested_ms, pqueued, _reserved) = STATS2.unpack(data[32:64])
                    self.last_stats = {
                        **(self._stats_partial or {}),
                        "relay_drop_global_topk": global_topk,
                        "relay_drop_stale_cell": stale_cell,
                        "relay_ghost_queued": ghost_queued,
                        "relay_live_queued": live_queued,
                        "relay_pqueued": pqueued,
                        "relay_congested_entries": congested_entries,
                        "relay_congested_ms": congested_ms,
                        "stats2_missing": 0,
                    }
                    self._stats_wait.set()
        except (asyncio.IncompleteReadError, ConnectionResetError):
            return

    async def _on_down(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._down_writers.add(writer)
        pending = 0
        try:
            while True:
                data = await reader.readexactly(FRAME)
                kind, worker, cell, seq, coverage, digest, generated_at = HDR.unpack(data[:32])
                now = time.time()
                if kind in (K_UP, K_TOMB) and cell == self.dispatcher.cell:
                    self.dispatcher.on_frame(kind, worker, seq, coverage, digest, generated_at, now)
                    writer.write(frame(K_ACK, 0, cell, seq, 0, 0, now))
                    pending += 1
                    if pending >= 32:
                        await writer.drain()
                        pending = 0
        except (asyncio.IncompleteReadError, ConnectionResetError, asyncio.CancelledError):
            return
        finally:
            if pending:
                with contextlib.suppress(Exception):
                    await writer.drain()
            self._down_writers.discard(writer)
            writer.close()

    async def configure(self, cell: int, policy: str, rate_frames_per_s: float) -> None:
        # Config and reset go out the foreground connection. The background
        # socket can be sitting on a multi-megabyte noise backlog; a reset
        # written behind that backlog does not reach the gateway for minutes.
        assert self.fg_agent is not None
        self.dispatcher = Dispatcher(cell=cell)
        burst = 4 if policy == "RateFIFO" else 1
        async with self._fg_lock:
            self.fg_agent.write(config_frame(
                cell, "FullSync" if policy == "Ideal" else policy,
                max_queue=MAX_QUEUE, max_inflight=MAX_INFLIGHT,
                rate_frames_per_s=rate_frames_per_s, rate_burst=burst,
            ))
            self.fg_agent.write(frame(K_RESET, 0, cell, 0, 0, 0, time.time()))
            await self.fg_agent.drain()
        deadline = time.time() + 5
        while self.dispatcher.resets < 1 and time.time() < deadline:
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.4)

    async def fetch_stats(self) -> dict:
        assert self.fg_agent is not None
        self._stats_wait.clear()
        self.last_stats = None
        self._stats_partial = None
        async with self._fg_lock:
            self.fg_agent.write(frame(K_STATS_REQ, 0, self.dispatcher.cell, 0, 0, 0, time.time()))
            await self.fg_agent.drain()
        try:
            await asyncio.wait_for(self._stats_wait.wait(), timeout=15)
        except asyncio.TimeoutError:
            partial = dict(self.last_stats or self._stats_partial or {})
            partial["stats2_missing"] = 1
            return partial
        return dict(self.last_stats or {})

    async def send_event(self, kind: int, worker: int, cell: int, seq: int, coverage: int, digest: int, generated_at: float) -> None:
        assert self.agent is not None
        async with self._write_lock:
            self.agent.write(frame(kind, worker, cell, seq, coverage, digest, generated_at))

    async def send_foreground(self, kind: int, worker: int, cell: int, seq: int, coverage: int, digest: int, generated_at: float) -> None:
        """Request-path updates. A separate TCP connection so a full background window cannot block them."""
        assert self.fg_agent is not None
        async with self._fg_lock:
            self.fg_agent.write(frame(kind, worker, cell, seq, coverage, digest, generated_at))
            await self.fg_agent.drain()

    async def drain_agent(self) -> None:
        if self.agent is not None:
            async with self._write_lock:
                await self.agent.drain()

    def meta(self) -> dict:
        return {
            "commit": git_commit(),
            "fixed_link_bit_s": self.link_bit_s,
            "tc_link_bit_s": tc_link_bit_s(),
            "git_dirty_files": git_dirty_files(),
            "gateway_cpus": 1,
            "gateway_cpuset": "0",
            "dispatcher_cpuset": "1",
            "max_queue": MAX_QUEUE,
            "max_queue_enforced": "mode6_only",
            "policy_max_queue": dict(POLICY_MAX_QUEUE),
            "max_inflight": MAX_INFLIGHT,
            "request_cpuset": "2",
            "noise_cpuset": "3+",
            "frame_bytes": FRAME,
            "wire_bytes": 104,
        }


def gateway_pid() -> int:
    return int(sh(["docker", "inspect", "-f", "{{.State.Pid}}", "b02-gateway4t4"]).strip())


def process_cpu_seconds(pid: int) -> float:
    raw = Path(f"/proc/{pid}/stat").read_text()
    rest = raw.rsplit(")", 1)[1].split()
    ticks = os.sysconf(os.sysconf_names["SC_CLK_TCK"])
    return (int(rest[11]) + int(rest[12])) / ticks


def cpus_for_vllm() -> set[int]:
    """Leave cores 0–15 for the gateway, dispatcher, client, and noise senders."""
    rest = set(range(os.cpu_count() or 1)) - set(range(16))
    return rest or set(range(os.cpu_count() or 1))


def _child_pids(pid: int) -> list[int]:
    kids: list[int] = []
    task = Path(f"/proc/{pid}/task")
    if not task.is_dir():
        return kids
    for tid in task.iterdir():
        child_file = tid / "children"
        try:
            text = child_file.read_text()
        except OSError:
            continue
        kids.extend(int(token) for token in text.split())
    return kids


def _thread_ids(pid: int) -> list[int]:
    task = Path(f"/proc/{pid}/task")
    if not task.is_dir():
        return [pid]
    return [int(entry.name) for entry in task.iterdir() if entry.name.isdigit()]


def process_tree(pid: int) -> list[int]:
    """The process plus descendants. vLLM's engine is a child, not the listener thread."""
    seen: list[int] = []
    guard: set[int] = set()
    stack = [pid]
    while stack:
        current = stack.pop()
        if current in guard or not Path(f"/proc/{current}").exists():
            continue
        guard.add(current)
        seen.append(current)
        stack.extend(_child_pids(current))
    return seen


def pin_listeners(ports: list[int], allowed: set[int]) -> tuple[list[tuple[int, set[int]]], list[int]]:
    """Pin every thread of each listener and of its child processes.

    sched_setaffinity on the process id only changes the main thread.
    """
    try:
        text = sh(["ss", "-ltnp"])
    except (subprocess.CalledProcessError, FileNotFoundError):
        return [], []
    roots: list[int] = []
    seen_roots: set[int] = set()
    for line in text.splitlines():
        if not any(re.search(rf":{port}\b", line) for port in ports):
            continue
        for match in re.finditer(r"pid=(\d+)", line):
            pid = int(match.group(1))
            if pid not in seen_roots:
                seen_roots.add(pid)
                roots.append(pid)
    saved: list[tuple[int, set[int]]] = []
    seen_threads: set[int] = set()
    for root in roots:
        for member in process_tree(root):
            for tid in _thread_ids(member):
                if tid in seen_threads:
                    continue
                seen_threads.add(tid)
                try:
                    previous = os.sched_getaffinity(tid)
                    os.sched_setaffinity(tid, allowed)
                except OSError:
                    continue
                saved.append((tid, previous))
    return saved, roots


def reapply_affinity(roots: list[int], allowed: set[int]) -> int:
    """Pin threads and children that appeared after the first pass."""
    pinned = 0
    for root in roots:
        for member in process_tree(root):
            for tid in _thread_ids(member):
                try:
                    os.sched_setaffinity(tid, allowed)
                except OSError:
                    continue
                pinned += 1
    return pinned


def restore_affinity(saved: list[tuple[int, set[int]]]) -> None:
    wide = set(range(os.cpu_count() or 1))
    for pid, cpus in saved:
        # A one-core mask is the control process, not a placement the servers had.
        target = wide if len(cpus) < 4 else cpus
        try:
            os.sched_setaffinity(pid, target)
        except OSError:
            pass


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n")
