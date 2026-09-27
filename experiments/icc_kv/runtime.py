"""Fixed-capacity Gateway/Dispatcher path for the ICC KV coordination plan.

The HTB ceiling is set once, far above the CPU path, and is not retuned per
utilization point. Gateway CPU quota stays at one core for the whole matrix.
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from experiments.icc_kv.wire import (
    FRAME, HDR, K_ACK, K_RESET, K_RESET_DONE, K_STATS, K_TOMB, K_UP, config_frame, frame,
)

ROOT = Path("/home/byh/B02")
NET = ROOT / "experiments/4t4/net"
OUT = ROOT / "analysis/icc_kv"
RELAY_PORT = 9710
DISPATCH_PORT = 9711
# One fixed ceiling. Utilization is later L/C, never a new tc rate.
FIXED_LINK_BIT_S = 1_000_000_000
MAX_QUEUE = 4096
MAX_INFLIGHT = 0


def sh(args: list[str]) -> str:
    return subprocess.check_output(args, text=True)


def bridge_ip() -> str:
    net_id = sh(["docker", "network", "inspect", "b02-4t4-net", "-f", "{{.Id}}"]).strip()
    out = sh(["bash", "-c", f"ip -4 -o addr show dev br-{net_id[:12]} | awk '{{print $4}}' | cut -d/ -f1"]).strip()
    if not out:
        raise RuntimeError("cannot determine b02-4t4-net bridge IP")
    return out


def prepare_fixed_gateway() -> str:
    """Recreate the 4T4 gateway once: 1 CPU, fixed HTB ceiling, bounded logs."""
    sh(["bash", str(NET / "setup_net_4t4.sh")])
    ip = bridge_ip()
    subprocess.check_call(["docker", "rm", "-f", "b02-gateway4t4"], stdout=subprocess.DEVNULL)
    subprocess.check_call([
        "docker", "run", "-d", "--name", "b02-gateway4t4", "--network", "b02-4t4-net",
        "--cap-add", "NET_ADMIN", "--cpus", "1", "--cpuset-cpus", "0",
        "--log-opt", "max-size=20m", "--log-opt", "max-file=2",
        "-p", "127.0.0.1:9710:9710",
        "b02-gw4t4", "--listen", "9710", "--downstream", f"{ip}:9711",
        "--max-queue", str(MAX_QUEUE), "--tau", "30", "--util-lambda", "16",
        "--gate", "2", "--adaptive-queue-gate", "8",
    ])
    half = FIXED_LINK_BIT_S // 2
    subprocess.check_call(["docker", "exec", "b02-gateway4t4", "sh", "-c", f"""
      set -e
      ip link set dev eth0 mtu 296
      tc qdisc replace dev eth0 root handle 1: htb default 20
      tc class add dev eth0 parent 1: classid 1:1 htb rate {FIXED_LINK_BIT_S}bit
      tc class add dev eth0 parent 1:1 classid 1:10 htb rate {half}bit ceil {FIXED_LINK_BIT_S}bit
      tc class add dev eth0 parent 1:1 classid 1:20 htb rate {half}bit ceil {FIXED_LINK_BIT_S}bit
      tc class add dev eth0 parent 1:1 classid 1:30 htb rate {FIXED_LINK_BIT_S}bit ceil {FIXED_LINK_BIT_S}bit
      tc qdisc add dev eth0 parent 1:10 handle 10: bfifo limit 65536
      tc qdisc add dev eth0 parent 1:20 handle 20: bfifo limit 65536
      tc qdisc add dev eth0 parent 1:30 handle 30: bfifo limit 8192
      tc filter add dev eth0 protocol ip parent 1:0 prio 1 u32 match ip dport 9711 0xffff flowid 1:10
      tc filter add dev eth0 protocol ip parent 1:0 prio 2 u32 match ip dport 5211 0xffff flowid 1:20
      tc filter add dev eth0 protocol ip parent 1:0 prio 3 u32 match ip sport 9710 0xffff flowid 1:30
    """])
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
    ground: dict[tuple[int, int], tuple[int, bool]] = field(default_factory=dict)
    resets: int = 0

    def on_frame(self, kind: int, worker: int, seq: int, coverage: int, digest: int, generated_at: float, now: float) -> None:
        if kind == K_UP:
            self.tested[(worker, digest)] = coverage
            self.ground[(worker, digest)] = (coverage, True)
            name = "update"
        else:
            self.tested.pop((worker, digest), None)
            self.ground[(worker, digest)] = (0, False)
            name = "invalidate"
        self.applied.append(Applied(seq, worker, name, digest, coverage, generated_at, now))


class PathRuntime:
    def __init__(self) -> None:
        self.ip = ""
        self.dispatcher = Dispatcher(cell=-1)
        self._server: asyncio.AbstractServer | None = None
        self.agent = None
        self._agent_task: asyncio.Task | None = None

    async def start(self) -> None:
        os.sched_setaffinity(0, {1})
        self.ip = bridge_ip()
        self._server = await asyncio.start_server(self._on_down, self.ip, DISPATCH_PORT)
        reader, writer = await asyncio.open_connection("127.0.0.1", RELAY_PORT)
        sock = writer.get_extra_info("socket")
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.agent = writer
        self._agent_task = asyncio.create_task(self._on_agent(reader))

    async def _on_agent(self, reader: asyncio.StreamReader) -> None:
        try:
            while True:
                data = await reader.readexactly(FRAME)
                kind, _i, cell, _seq, _c, _d, _t = HDR.unpack(data[:32])
                if kind == K_RESET_DONE and cell == self.dispatcher.cell:
                    self.dispatcher.resets += 1
        except (asyncio.IncompleteReadError, ConnectionResetError):
            return

    async def _on_down(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                data = await reader.readexactly(FRAME)
                kind, worker, cell, seq, coverage, digest, generated_at = HDR.unpack(data[:32])
                now = time.time()
                if kind in (K_UP, K_TOMB) and cell == self.dispatcher.cell:
                    self.dispatcher.on_frame(kind, worker, seq, coverage, digest, generated_at, now)
                    writer.write(frame(K_ACK, 0, cell, seq, 0, 0, now))
                    await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionResetError):
            return

    async def configure(self, cell: int, policy: str, rate_frames_per_s: float) -> None:
        assert self.agent is not None
        self.dispatcher = Dispatcher(cell=cell)
        burst = 4 if policy == "RateFIFO" else 1
        self.agent.write(config_frame(
            cell, "FullSync" if policy == "Ideal" else policy,
            max_queue=MAX_QUEUE, max_inflight=MAX_INFLIGHT,
            rate_frames_per_s=rate_frames_per_s, rate_burst=burst,
        ))
        self.agent.write(frame(K_RESET, 0, cell, 0, 0, 0, time.time()))
        await self.agent.drain()
        deadline = time.time() + 5
        while self.dispatcher.resets < 1 and time.time() < deadline:
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.4)

    async def send_event(self, kind: int, worker: int, cell: int, seq: int, coverage: int, digest: int, generated_at: float) -> None:
        assert self.agent is not None
        self.agent.write(frame(kind, worker, cell, seq, coverage, digest, generated_at))

    async def drain_agent(self) -> None:
        if self.agent is not None:
            await self.agent.drain()

    def meta(self) -> dict:
        return {
            "commit": git_commit(),
            "fixed_link_bit_s": FIXED_LINK_BIT_S,
            "gateway_cpus": 1,
            "gateway_cpuset": "0",
            "dispatcher_cpuset": "1",
            "max_queue": MAX_QUEUE,
            "max_inflight": MAX_INFLIGHT,
            "frame_bytes": FRAME,
            "wire_bytes": 104,
        }


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n")
