#!/usr/bin/env python3
"""CPU-only Model B correctness and backpressure check.

Uses the real gateway process and experiments.icc_kv.runtime.Dispatcher.on_frame.
An apply sleep, when set, is fault injection in this harness only.
"""
from __future__ import annotations

import asyncio
import contextlib
import csv
import hashlib
import json
import os
import signal
import socket
import subprocess
import time
from pathlib import Path

from experiments.icc_kv.runtime import Dispatcher
from experiments.icc_kv.wire import (
    FRAME, HDR, K_ACK, K_RESET, K_RESET_DONE, K_STATS, K_STATS2, K_STATS_REQ, K_TOMB, K_UP,
    STATS, STATS2, config_frame, frame,
)

ROOT = Path("/home/byh/B02")
OUT = ROOT / "analysis/p0_pressure_realism/m1_5b_model_b_benchmark"
RAW = OUT / "raw"
GATEWAY = ROOT / "experiments/4t4/net/gateway_relay_4t4.py"
PY = ROOT / "poc/.venv/bin/python"
DISP_PORT = 19811
GW_PORTS = (19820, 19821)
CELL0 = 41


def digest64(name: str) -> int:
    """Same blake2b-8 encoding as experiments/4t4/run_formal4t4.py digest64."""
    return int.from_bytes(hashlib.blake2b(name.encode(), digest_size=8).digest(), "big")


def percentile(values: list[float], p: float) -> float:
    if not values:
        return -1.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * p))))
    return ordered[index]


class DispatcherService:
    """Same read / apply / ACK order as PathRuntime._on_down, plus an optional sleep."""

    def __init__(self, cell: int) -> None:
        self.cell = cell
        self.dispatcher = Dispatcher(cell=cell)
        self.delay_s = 0.0
        self.server: asyncio.AbstractServer | None = None
        self.read_count = 0
        self.apply_count = 0
        self.ack_count = 0
        self.apply_cpu_s: list[float] = []
        self.tomb_delay: dict[tuple[int, int], float] = {}

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._on_down, "127.0.0.1", DISP_PORT)

    async def stop(self) -> None:
        server = self.server
        self.server = None
        if server is not None:
            server.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(server.wait_closed(), timeout=2)

    def replace(self, cell: int) -> None:
        self.cell = cell
        self.dispatcher = Dispatcher(cell=cell)
        self.read_count = 0
        self.apply_count = 0
        self.ack_count = 0
        self.apply_cpu_s.clear()
        self.tomb_delay.clear()

    async def _on_down(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        pending = 0
        try:
            while True:
                data = await reader.readexactly(FRAME)
                kind, worker, cell, seq, coverage, digest, generated_at = HDR.unpack(data[:32])
                if kind in (K_UP, K_TOMB) and cell == self.dispatcher.cell:
                    self.read_count += 1
                    delay = self.delay_s
                    if delay > 0:
                        await asyncio.sleep(delay)
                    started = time.perf_counter()
                    now = time.time()
                    self.dispatcher.on_frame(kind, worker, seq, coverage, digest, generated_at, now)
                    self.apply_cpu_s.append(time.perf_counter() - started)
                    self.apply_count += 1
                    if kind == K_TOMB:
                        self.tomb_delay[(worker, digest)] = max(0.0, now - generated_at)
                    writer.write(frame(K_ACK, 0, cell, seq, 0, 0, now))
                    self.ack_count += 1
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
            writer.close()

    def view(self) -> dict[tuple[int, int], int]:
        return self.dispatcher.snapshot()


class GatewayProc:
    def __init__(self, index: int, port: int) -> None:
        self.index = index
        self.port = port
        self.log_path = RAW / f"gw{index}.log"
        self.proc: subprocess.Popen[str] | None = None

    def start(self) -> None:
        RAW.mkdir(parents=True, exist_ok=True)
        log = self.log_path.open("w")
        self.proc = subprocess.Popen(
            [str(PY), str(GATEWAY), "--listen", str(self.port),
             "--downstream", f"127.0.0.1:{DISP_PORT}", "--sndbuf", "2304"],
            cwd=str(ROOT), stdout=log, stderr=subprocess.STDOUT, text=True,
            start_new_session=True,
        )
        log.close()

    def stop(self) -> None:
        proc = self.proc
        self.proc = None
        if proc is None or proc.poll() is not None:
            return
        os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=2)

    def wait_listening(self, timeout: float = 5.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc is not None and self.proc.poll() is not None:
                raise RuntimeError(f"gateway {self.index} exited {self.proc.returncode}")
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.2):
                    return
            except OSError:
                time.sleep(0.05)
        raise TimeoutError(f"gateway {self.index} did not listen")

    def wait_downstream(self, minimum: int, timeout: float = 5.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            text = self.log_path.read_text() if self.log_path.exists() else ""
            if text.count('"downstream_connected"') >= minimum:
                return
            time.sleep(0.05)
        raise TimeoutError(f"gateway {self.index} downstream count < {minimum}")

    def stats_from_log(self) -> dict:
        last = {}
        if not self.log_path.exists():
            return last
        for line in self.log_path.read_text().splitlines():
            if '"event": "cell_stats"' in line or '"event": "tick"' in line:
                with contextlib.suppress(json.JSONDecodeError):
                    last = json.loads(line)
        return last

    def max_pending_from_log(self) -> int:
        peak = 0
        if not self.log_path.exists():
            return 0
        for line in self.log_path.read_text().splitlines():
            if "pending_lineages" not in line:
                continue
            with contextlib.suppress(json.JSONDecodeError):
                peak = max(peak, int(json.loads(line).get("pending_lineages") or 0))
        return peak


async def open_agent(port: int, cell: int, policy: str, max_inflight: int):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(config_frame(
        cell, policy, max_queue=4096, max_inflight=max_inflight,
        rate_frames_per_s=0, rate_burst=1,
    ))
    writer.write(frame(K_RESET, 0, cell, 0, 0, 0, time.time()))
    await writer.drain()
    reply = await asyncio.wait_for(reader.readexactly(FRAME), timeout=3)
    kind, _i, got, _seq, _c, _d, _t = HDR.unpack(reply[:32])
    if kind != K_RESET_DONE or got != cell:
        raise RuntimeError(f"reset reply kind={kind} cell={got}")
    return reader, writer


async def send_updates(writer: asyncio.StreamWriter, cell: int, rows: list[tuple[int, int, int, int]]) -> None:
    """rows are (kind, worker, coverage, digest) with seq assigned here."""
    for seq, (kind, worker, coverage, digest) in enumerate(rows, start=1):
        writer.write(frame(kind, worker, cell, seq, coverage, digest, time.time()))
    await writer.drain()


async def fetch_stats(port: int, cell: int) -> dict:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(frame(K_STATS_REQ, 0, cell, 0, 0, 0, time.time()))
        await writer.drain()
        first = await asyncio.wait_for(reader.readexactly(FRAME), timeout=2)
        second = await asyncio.wait_for(reader.readexactly(FRAME), timeout=2)
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
    out = {}
    for blob in (first, second):
        kind, _i, _cell, queued, received, _d, _t = HDR.unpack(blob[:32])
        if kind == K_STATS:
            forwarded, rate, superseded, replica, low, qdrop, expired, maxq = STATS.unpack(blob[32:64])
            out.update({
                "forwarded": forwarded, "superseded": superseded, "replica_cap": replica,
                "low_utility": low, "queue_drop": qdrop, "maxq": maxq,
                "live_depth": int(queued), "received": int(received),
            })
        elif kind == K_STATS2:
            topk, stale, ghost, live, entries, ms, pqueued, _pad = STATS2.unpack(blob[32:64])
            out.update({
                "topk_drops": topk, "stale_cell": stale, "ghost": ghost,
                "live_queued": live, "pqueued": pqueued, "congested_ms": ms,
                "congested_entries": entries,
            })
    return out


async def wait_view(service: DispatcherService, expect: dict[tuple[int, int], int], timeout: float = 3.0) -> dict:
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        last = service.view()
        if last == expect:
            return last
        await asyncio.sleep(0.02)
    return last


def compare(actual: dict[tuple[int, int], int], expect: dict[tuple[int, int], int]) -> int:
    keys = set(actual) | set(expect)
    return sum(1 for key in keys if actual.get(key) != expect.get(key))


async def boot(cell: int) -> tuple[DispatcherService, list[GatewayProc]]:
    service = DispatcherService(cell)
    await service.start()
    gates = [GatewayProc(i, port) for i, port in enumerate(GW_PORTS)]
    for gate in gates:
        if gate.log_path.exists():
            gate.log_path.unlink()
        gate.start()
    for gate in gates:
        gate.wait_listening()
        gate.wait_downstream(1)
    return service, gates


def shutdown(service: DispatcherService, gates: list[GatewayProc]) -> None:
    for gate in gates:
        gate.stop()


async def run_owner_tests() -> list[dict]:
    service, gates = await boot(CELL0)
    rows: list[dict] = []
    try:
        agents = []
        for gate in gates:
            agents.append(await open_agent(gate.port, CELL0, "StaticTopK", 0))
            gate.wait_downstream(2)
        x, y, z = digest64("P0M15B/X"), digest64("P0M15B/Y"), digest64("P0M15B/Z")
        await send_updates(agents[0][1], CELL0, [
            (K_UP, 0, 1024, x), (K_UP, 1, 2048, y),
        ])
        await send_updates(agents[1][1], CELL0, [
            (K_UP, 2, 1024, x), (K_UP, 3, 4096, z),
        ])
        expect_a = {(0, x): 1024, (1, y): 2048, (2, x): 1024, (3, z): 4096}
        got = await wait_view(service, expect_a)
        owners_x = sorted(worker for (worker, digest), cov in got.items() if digest == x and cov > 0)
        rows.append(_row("A_owner_isolation", got, expect_a, service, gates, extra={
            "prefix_x_owners": ",".join(map(str, owners_x)),
        }))
        if compare(got, expect_a):
            return rows
        await send_updates(agents[0][1], CELL0, [(K_TOMB, 0, 0, x)])
        expect_b = {(1, y): 2048, (2, x): 1024, (3, z): 4096}
        got_b = await wait_view(service, expect_b)
        rows.append(_row("B_invalidation_isolation", got_b, expect_b, service, gates))
        for reader, writer in agents:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
    finally:
        shutdown(service, gates)
        await service.stop()
    return rows


async def run_version_test() -> list[dict]:
    cell = CELL0 + 1
    service, gates = await boot(cell)
    rows = []
    try:
        reader, writer = await open_agent(gates[0].port, cell, "StaticTopK", 0)
        gates[0].wait_downstream(2)
        version = digest64("P0M15B/V")
        for coverage in (1024, 2048, 4096):
            await send_updates(writer, cell, [(K_UP, 0, coverage, version)])
            await wait_view(service, {(0, version): coverage})
        got = service.view()
        rows.append(_row("C_version_in_order", got, {(0, version): 4096}, service, gates))
        await send_updates(writer, cell, [(K_UP, 0, 1024, version)])
        got_old = await wait_view(service, {(0, version): 1024}, timeout=2)
        # Last arrival wins. This row records that fact; it is not the ordered check.
        rows.append(_row("C_reorder_old_after_new", got_old, {(0, version): 4096}, service, gates, extra={
            "note": "expected column is version-aware retention; wire has no version field",
        }))
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
    finally:
        shutdown(service, gates)
        await service.stop()
    return rows


async def run_reduction_test() -> list[dict]:
    """Queue on both gateways before the dispatcher accepts, then release."""
    cell = CELL0 + 2
    gates = [GatewayProc(i, port) for i, port in enumerate(GW_PORTS)]
    for gate in gates:
        if gate.log_path.exists():
            gate.log_path.unlink()
        gate.start()
    service = DispatcherService(cell)
    rows = []
    try:
        for gate in gates:
            gate.wait_listening()
        agents = []
        for gate in gates:
            agents.append(await open_agent(gate.port, cell, "StaticTopK", 0))
        merge = digest64("P0M15B/M")
        replica = digest64("P0M15B/R")
        # Gateway 0: two extensions of M, three equal-coverage holders of R, 20 ranked lineages.
        gw0_rows = [
            (K_UP, 0, 1024, merge), (K_UP, 0, 2048, merge),
            (K_UP, 0, 1024, replica), (K_UP, 1, 1024, replica), (K_UP, 8, 1024, replica),
        ]
        gw0_rows += [(K_UP, 0, 100 * (i + 1), digest64(f"P0M15B/K0/{i:02d}")) for i in range(20)]
        gw1_rows = [
            (K_UP, 2, 1024, merge), (K_UP, 2, 3072, merge),
            (K_UP, 2, 2048, replica), (K_UP, 3, 2048, replica),
        ]
        gw1_rows += [(K_UP, 2, 100 * (i + 1), digest64(f"P0M15B/K1/{i:02d}")) for i in range(20)]
        await send_updates(agents[0][1], cell, gw0_rows)
        await send_updates(agents[1][1], cell, gw1_rows)
        await asyncio.sleep(0.3)
        stats_before = [await fetch_stats(gate.port, cell) for gate in gates]
        await service.start()
        for gate in gates:
            gate.wait_downstream(1)
        # Lowest four coverages on each gateway are 100..400. Twenty others plus the kept keys.
        # 22 digests compete for K=16: merge, replica, and 20 ranked lineages.
        # The six shortest ranked lineages (coverage 100..600) lose both gateways.
        kept0 = {digest64(f"P0M15B/K0/{i:02d}"): 100 * (i + 1) for i in range(6, 20)}
        kept1 = {digest64(f"P0M15B/K1/{i:02d}"): 100 * (i + 1) for i in range(6, 20)}
        expect = {
            (0, merge): 2048,
            (2, merge): 3072,
            (0, replica): 1024,
            (1, replica): 1024,
            (2, replica): 2048,
            (3, replica): 2048,
        }
        expect.update({(0, digest): cov for digest, cov in kept0.items()})
        expect.update({(2, digest): cov for digest, cov in kept1.items()})
        got = await wait_view(service, expect, timeout=4)
        dropped_third = (8, replica) not in got
        rows.append(_row("D_independent_reduction", got, expect, service, gates, extra={
            "gw0_topk_drops_before_connect": stats_before[0].get("topk_drops"),
            "gw1_topk_drops_before_connect": stats_before[1].get("topk_drops"),
            "gw0_replica_drops_before_connect": stats_before[0].get("replica_cap"),
            "gw1_replica_drops_before_connect": stats_before[1].get("replica_cap"),
            "gw0_superseded_before_connect": stats_before[0].get("superseded"),
            "gw1_superseded_before_connect": stats_before[1].get("superseded"),
            "third_holder_absent": int(dropped_third),
        }))
        for _reader, writer in agents:
            writer.close()
    finally:
        shutdown(service, gates)
        await service.stop()
    return rows


def _row(name: str, got: dict, expect: dict, service: DispatcherService, gates: list[GatewayProc], extra: dict | None = None) -> dict:
    mismatch = compare(got, expect)
    row = {
        "test": name,
        "verdict": "PASS" if mismatch == 0 else "FAIL",
        "generated_updates": "",
        "gateway0_forwarded": "",
        "gateway1_forwarded": "",
        "dispatcher_applied": service.apply_count,
        "dispatcher_received": service.read_count,
        "mismatch_count": mismatch,
        "actual": _fmt(got),
        "expected": _fmt(expect),
    }
    for gate in gates:
        stats = gate.stats_from_log()
        drops = stats.get("drops") or {}
        row[f"gateway{gate.index}_forwarded"] = stats.get("forwarded", "")
        row[f"gateway{gate.index}_topk"] = drops.get("global_topk", "")
        row[f"gateway{gate.index}_replica"] = drops.get("replica_cap", "")
        row[f"gateway{gate.index}_superseded"] = drops.get("superseded", "")
    if extra:
        row.update(extra)
    print(name, row["verdict"], "mismatch", mismatch, flush=True)
    return row


def _fmt(state: dict[tuple[int, int], int]) -> str:
    parts = [f"{worker}:{digest}:{cov}" for (worker, digest), cov in sorted(state.items())]
    return ";".join(parts)


async def run_backpressure() -> tuple[list[dict], list[dict]]:
    results = []
    series = []
    policies = ("FullSync", "StaticSemantic", "StaticTopK")
    configs = (
        ("A", 0, 0.0),
        ("B", 4, 0.0),
        ("C", 0, 0.05),
        ("D", 4, 0.05),
    )
    case = 0
    for policy in policies:
        for label, inflight, delay in configs:
            case += 1
            cell = 100 + case
            service, gates = await boot(cell)
            service.delay_s = 0.0
            try:
                agents = []
                for gate in gates:
                    agents.append(await open_agent(gate.port, cell, policy, inflight))
                    gate.wait_downstream(2)
                inv = digest64(f"P0M15B/INV/{policy}/{label}")
                await send_updates(agents[0][1], cell, [(K_UP, 0, 4096, inv)])
                await wait_view(service, {(0, inv): 4096})
                service.delay_s = delay
                burst = []
                for i in range(80):
                    burst.append((K_UP, i % 2, 1000 + i, digest64(f"P0M15B/B0/{label}/{i:03d}")))
                other = []
                for i in range(80):
                    other.append((K_UP, 2 + (i % 2), 1000 + i, digest64(f"P0M15B/B1/{label}/{i:03d}")))
                started = time.perf_counter()
                polls: list[asyncio.Task] = []
                stop = asyncio.Event()

                async def poll(port: int, gw: int) -> None:
                    while not stop.is_set():
                        try:
                            stats = await asyncio.wait_for(fetch_stats(port, cell), timeout=0.3)
                        except (asyncio.TimeoutError, OSError, asyncio.IncompleteReadError):
                            series.append({
                                "case": f"{policy}_{label}", "t_s": round(time.perf_counter() - started, 4),
                                "gateway": gw, "stats": "TIMEOUT",
                            })
                            await asyncio.sleep(0.05)
                            continue
                        series.append({
                            "case": f"{policy}_{label}", "t_s": round(time.perf_counter() - started, 4),
                            "gateway": gw, "live_depth": stats.get("live_depth"),
                            "topk_drops": stats.get("topk_drops"), "forwarded": stats.get("forwarded"),
                            "received_at_gateway": stats.get("received"),
                            "dispatcher_read": service.read_count, "dispatcher_applied": service.apply_count,
                        })
                        await asyncio.sleep(0.05)

                for gate in gates:
                    polls.append(asyncio.create_task(poll(gate.port, gate.index)))
                await send_updates(agents[0][1], cell, burst + [(K_TOMB, 0, 0, inv)])
                await send_updates(agents[1][1], cell, other)
                timeout = 8 if delay == 0 else 25
                deadline = time.time() + timeout
                while time.time() < deadline:
                    if service.apply_count >= 1 + 80 + 1 + 80 and service.delay_s == delay:
                        # All frames are not always applied: Top-K drops some.
                        await asyncio.sleep(0.3)
                        break
                    if delay == 0 and service.apply_count >= 160:
                        await asyncio.sleep(0.2)
                        break
                    await asyncio.sleep(0.05)
                else:
                    pass
                # Give a blocked Top-K run time to finish the frames it did admit.
                if delay:
                    quiet = time.time()
                    last = service.apply_count
                    while time.time() - quiet < 1.5 and time.time() < deadline + 20:
                        await asyncio.sleep(0.1)
                        if service.apply_count != last:
                            last = service.apply_count
                            quiet = time.time()
                elapsed = time.perf_counter() - started
                stop.set()
                for task in polls:
                    task.cancel()
                await asyncio.gather(*polls, return_exceptions=True)
                tomb_delay = service.tomb_delay.get((0, inv), -1)
                ages = []
                for item in service.dispatcher.applied:
                    ages.append(max(0.0, item.applied_at - item.generated_at))
                for gate in gates:
                    stats = {}
                    with contextlib.suppress(Exception):
                        stats = await fetch_stats(gate.port, cell)
                    log_stats = gate.stats_from_log()
                    results.append({
                        "case": label, "policy": policy, "max_inflight": inflight,
                        "apply_delay_s": delay,
                        "pressure": "FAULT_INJECTED" if delay else "NORMAL",
                        "gateway": gate.index,
                        "gateway_received": stats.get("received", ""),
                        "gateway_forwarded": stats.get("forwarded", log_stats.get("forwarded", "")),
                        "topk_drops": stats.get("topk_drops", ""),
                        "superseded": stats.get("superseded", ""),
                        "replica_cap": stats.get("replica_cap", ""),
                        "max_queue_seen": stats.get("maxq", ""),
                        "final_live_depth": stats.get("live_depth", ""),
                        "peak_pending_lineages_logged": gate.max_pending_from_log(),
                        "ewma_generation_to_apply_s": log_stats.get("ewma_dq_s", ""),
                        "drain_wait_s": log_stats.get("drain_wait_s", ""),
                        "write_queue_bytes_last": log_stats.get("write_queue_bytes", ""),
                        "dispatcher_read": service.read_count,
                        "dispatcher_applied": service.apply_count,
                        "index_entries": len(service.view()),
                        "tombstone_delay_s": round(tomb_delay, 6) if gate.index == 0 else "",
                        "generation_to_apply_p50_s": round(percentile(ages, 0.50), 6),
                        "generation_to_apply_p95_s": round(percentile(ages, 0.95), 6),
                        "apply_cpu_p50_s": round(percentile(service.apply_cpu_s, 0.50), 6),
                        "elapsed_s": round(elapsed, 3),
                        "inflight_scope": "per_gateway_process",
                    })
                print(policy, label, "applied", service.apply_count, "read", service.read_count,
                      "tomb_s", round(tomb_delay, 4), flush=True)
                for _r, writer in agents:
                    writer.close()
            finally:
                shutdown(service, gates)
                await service.stop()
    return results, series


def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("")
        return
    keys: list[str] = []
    for row in rows:
        for key in row:
            if key not in keys:
                keys.append(key)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=keys, lineterminator="\n", extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


async def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    RAW.mkdir(parents=True, exist_ok=True)
    # Refuse to touch the formal listener.
    try:
        with socket.create_connection(("127.0.0.1", DISP_PORT), timeout=0.2):
            raise SystemExit(f"{DISP_PORT} is already in use")
    except OSError:
        pass
    owner = await run_owner_tests()
    version = await run_version_test()
    reduction = await run_reduction_test()
    correctness = owner + version + reduction
    write_csv(OUT / "correctness_results.csv", correctness)
    failed = [row["test"] for row in owner if row["verdict"] != "PASS"]
    if failed:
        print("STOP phase 2, owner/tombstone failed:", failed, flush=True)
        return
    results, series = await run_backpressure()
    write_csv(OUT / "backpressure_results.csv", results)
    write_csv(OUT / "backpressure_timeseries.csv", series)
    print("done", len(results), "backpressure rows", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
