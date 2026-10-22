#!/usr/bin/env python3
"""P0-M3.5: native adapter updates through the real KVSelect gateway.

CPU only. Does not start vLLM, does not change K, and does not listen on 9710.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import csv
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path("/home/byh/B02")
sys.path.insert(0, str(ROOT))
OUT = ROOT / "analysis/p0_pressure_realism/m3_5_native_compact_e2e"
RAW = OUT / "raw"
M3 = ROOT / "analysis/p0_pressure_realism/m3_native_compact_motivation"
C1 = ROOT / "analysis/p0_pressure_realism/c1_real_indexer"
GATEWAY = ROOT / "experiments/4t4/net/gateway_relay_4t4.py"
PY = ROOT / "poc/.venv/bin/python"
DISP_PORT = 19950
CTRL_PORT = 19951
GW_PORT = 19960
REGRESSION = 16215451153097395629

from experiments.icc_kv.runtime import ROUTABLE_BELOW, Dispatcher
from experiments.icc_kv.wire import (
    FRAME, HDR, K_ACK, K_CONFIG, K_RESET, K_RESET_DONE, K_STATS, K_STATS2,
    K_STATS_REQ, K_TOMB, K_UP, STATS, STATS2, config_frame, frame,
)


def port_listening(port: int) -> bool:
    text = subprocess.check_output(["ss", "-H", "-ltn"], text=True, errors="replace")
    needle = f":{port}"
    return any(needle in line.split()[3] for line in text.splitlines() if line.split())


def recvall(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("short read")
        buf += chunk
    return buf


def git_head() -> str:
    try:
        return subprocess.check_output(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True
        ).strip()
    except subprocess.CalledProcessError:
        return "unknown"


# --- dispatcher process -----------------------------------------------------

async def serve_dispatcher(port: int, control: int, cell: int, ready: Path) -> None:
    service = Dispatcher(cell=cell)
    history: list[dict] = []
    applied = 0

    async def on_down(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        nonlocal applied
        pending = 0
        try:
            while True:
                data = await reader.readexactly(FRAME)
                kind, worker, got, seq, coverage, digest, generated_at = HDR.unpack(data[:32])
                if kind not in (K_UP, K_TOMB) or got != service.cell:
                    continue
                now = time.time()
                service.on_frame(kind, worker, seq, coverage, digest, generated_at, now)
                if worker < ROUTABLE_BELOW:
                    applied += 1
                    history.append({
                        "seq": seq, "worker": worker, "kind": kind,
                        "coverage": coverage, "digest": str(digest),
                    })
                writer.write(frame(K_ACK, 0, got, seq, 0, 0, now))
                pending += 1
                if pending >= 32:
                    await writer.drain()
                    pending = 0
        except (asyncio.IncompleteReadError, ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
            return
        finally:
            if pending:
                with contextlib.suppress(Exception):
                    await writer.drain()
            writer.close()

    async def on_ctrl(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        nonlocal service, history, applied
        try:
            while True:
                line = await reader.readline()
                if not line:
                    return
                req = json.loads(line)
                cmd = req.get("cmd")
                if cmd == "set_cell":
                    service = Dispatcher(cell=int(req["cell"]))
                    history = []
                    applied = 0
                    resp = {"ok": True, "cell": service.cell}
                elif cmd == "snapshot":
                    tested = [
                        [worker, str(digest), coverage]
                        for (worker, digest), coverage in sorted(service.tested.items())
                    ]
                    resp = {"applied": applied, "tested": tested, "cell": service.cell}
                elif cmd == "history":
                    resp = {"history": history}
                elif cmd == "shutdown":
                    writer.write(json.dumps({"ok": True}).encode() + b"\n")
                    await writer.drain()
                    os.kill(os.getpid(), signal.SIGTERM)
                    return
                else:
                    resp = {"error": f"unknown {cmd}"}
                writer.write(json.dumps(resp).encode() + b"\n")
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionResetError, asyncio.CancelledError, json.JSONDecodeError):
            return

    data_server = await asyncio.start_server(on_down, "127.0.0.1", port, reuse_address=True)
    ctrl_server = await asyncio.start_server(on_ctrl, "127.0.0.1", control, reuse_address=True)
    ready.write_text(json.dumps({"pid": os.getpid(), "cell": cell}))
    async with data_server, ctrl_server:
        await asyncio.Future()


# --- adapter ----------------------------------------------------------------

def build_adapter() -> dict:
    sys.path.insert(0, str(M3))
    import replay_m3

    c1 = replay_m3.load_c1()
    frames = c1.load_frames(C1 / "raw/frames.bin")
    with (C1 / "raw/requests.csv").open() as handle:
        requests = list(csv.DictReader(handle))
    windows = replay_m3.request_windows(requests)
    oracle = replay_m3.BlockOracle()
    converter = replay_m3.Converter(oracle)
    joins = {"one": 0, "zero": 0, "many": 0}
    native_events = 0
    prompt_lineage: list[tuple[str, int]] = []
    rows = []
    seen_ids: dict[int, int] = {}
    for recv_ns, _topic, _seq, payload in frames:
        batch = c1.decode_payload(payload)
        for event in batch.events:
            native_events += 1
            name = type(event).__name__
            before = len(converter.updates)
            if name == "BlockStored":
                owners = replay_m3.covering(windows, recv_ns)
                if len(owners) == 1:
                    joins["one"] += 1
                elif len(owners) == 0:
                    joins["zero"] += 1
                else:
                    joins["many"] += 1
                new = oracle.apply_stored(event)
                parent = replay_m3.hash_int(event.parent_block_hash)
                lineage = converter.on_stored(parent, new, recv_ns)
                if lineage is not None and len(owners) == 1:
                    prompt_lineage.append((owners[0]["prompt_blake2s"], lineage))
                    seen_ids[lineage] = seen_ids.get(lineage, 0) + 1
            elif name == "BlockRemoved":
                removed = oracle.apply_removed(event)
                converter.on_removed(removed, recv_ns)
            elif name == "AllBlocksCleared":
                oracle.apply_cleared()
                converter.on_cleared(recv_ns)
            for update in converter.updates[before:]:
                chain = converter.chain.get(update["lineage_id"], [])
                oracle_cov = oracle.contiguous(chain)[0] if chain else 0
                rows.append({
                    "kind": update["kind"],
                    "lineage_id": update["lineage_id"],
                    "coverage_tokens": update["coverage_tokens"],
                    "oracle_coverage": oracle_cov,
                    "reason": update["reason"],
                    "recv_ns": update["recv_ns"],
                    "native_events_so_far": native_events,
                })
    prompts: dict[str, set[int]] = {}
    lineages: dict[int, set[str]] = {}
    for prompt, lineage in prompt_lineage:
        prompts.setdefault(prompt, set()).add(lineage)
        lineages.setdefault(lineage, set()).add(prompt)
    stable = all(
        len({row["lineage_id"] for row in rows if row["lineage_id"] == lineage}) == 1
        for lineage in {row["lineage_id"] for row in rows}
    )
    shrink_same_id = True
    last: dict[int, int] = {}
    for row in rows:
        previous = last.get(row["lineage_id"])
        if previous is not None and row["coverage_tokens"] != previous and row["lineage_id"] != row["lineage_id"]:
            shrink_same_id = False
        last[row["lineage_id"]] = row["coverage_tokens"]
    return {
        "native_events": native_events,
        "frames": len(frames),
        "updates": rows,
        "joins": joins,
        "live": {str(key): value for key, value in converter.advertised.items()},
        "prompts_one": sum(1 for value in prompts.values() if len(value) == 1),
        "prompts_many": sum(1 for value in prompts.values() if len(value) > 1),
        "lineages_one": sum(1 for value in lineages.values() if len(value) == 1),
        "lineages_many": sum(1 for value in lineages.values() if len(value) > 1),
        "lineages": len(converter.chain),
        "identity_stable_within_row": stable and shrink_same_id,
        "alive": len(oracle.alive),
        "oracle_mismatches": sum(
            1 for row in rows
            if row["kind"] == "UPSERT" and row["coverage_tokens"] != row["oracle_coverage"]
        ),
    }


# --- gateway driver ---------------------------------------------------------

class Proc:
    def __init__(self) -> None:
        self.procs: list[subprocess.Popen] = []

    def add(self, proc: subprocess.Popen) -> None:
        self.procs.append(proc)

    def stop(self) -> None:
        for proc in self.procs:
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        for proc in self.procs:
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait(timeout=5)


def launch(procs: Proc) -> tuple[subprocess.Popen, Path]:
    RAW.mkdir(parents=True, exist_ok=True)
    ready = RAW / "dispatcher_ready.json"
    ready.unlink(missing_ok=True)
    disp_log = open(RAW / "dispatcher.log", "w")
    disp = subprocess.Popen(
        [str(PY), str(Path(__file__).resolve()), "--role", "dispatcher",
         "--port", str(DISP_PORT), "--control", str(CTRL_PORT),
         "--cell", "1", "--ready-file", str(ready)],
        cwd=str(ROOT), stdout=disp_log, stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    procs.add(disp)
    deadline = time.time() + 10
    while time.time() < deadline:
        if disp.poll() is not None:
            raise RuntimeError(disp_log_tail())
        if ready.exists() and port_listening(CTRL_PORT):
            break
        time.sleep(0.02)
    else:
        raise TimeoutError("dispatcher")
    gw_log = RAW / "gateway.log"
    handle = gw_log.open("w")
    gw = subprocess.Popen(
        [str(PY), str(GATEWAY), "--listen", str(GW_PORT),
         "--downstream", f"127.0.0.1:{DISP_PORT}",
         "--sndbuf", "2304", "--max-queue", "4096", "--tau", "30",
         "--util-lambda", "16", "--gate", "2", "--adaptive-queue-gate", "8",
         "--congestion-hold", "0.2", "--quiet"],
        cwd=str(ROOT), stdout=handle, stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    procs.add(gw)
    deadline = time.time() + 8
    while time.time() < deadline:
        if gw.poll() is not None:
            raise RuntimeError(gw_log.read_text(errors="replace")[-500:])
        if port_listening(GW_PORT):
            return gw, gw_log
        time.sleep(0.02)
    raise TimeoutError("gateway")


def disp_log_tail() -> str:
    path = RAW / "dispatcher.log"
    return path.read_text(errors="replace")[-500:] if path.exists() else ""


def ctrl(cmd: str, **fields) -> dict:
    sock = socket.create_connection(("127.0.0.1", CTRL_PORT), timeout=5)
    sock.sendall((json.dumps({"cmd": cmd, **fields}) + "\n").encode())
    buf = b""
    while b"\n" not in buf:
        chunk = sock.recv(1 << 20)
        if not chunk:
            break
        buf += chunk
    sock.close()
    return json.loads(buf.decode().split("\n", 1)[0])


def wait_connected(path: Path, minimum: int, timeout: float = 5) -> int:
    deadline = time.time() + timeout
    while time.time() < deadline:
        count = path.read_text(errors="replace").count('"downstream_connected"') if path.exists() else 0
        if count >= minimum:
            return count
        time.sleep(0.02)
    raise TimeoutError("downstream")


def open_agent(cell: int, gw_log: Path, connected: int) -> tuple[socket.socket, int]:
    ctrl("set_cell", cell=cell)
    sock = socket.create_connection(("127.0.0.1", GW_PORT), timeout=3)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    sock.settimeout(5)
    sock.sendall(config_frame(
        cell, "StaticTopK", max_queue=4096, max_inflight=0,
        rate_frames_per_s=0, rate_burst=1,
    ))
    sock.sendall(frame(K_RESET, 0, cell, 0, 0, 0, time.time()))
    reply = recvall(sock, FRAME)
    kind, _i, got, _seq, _c, _d, _t = HDR.unpack(reply[:32])
    if kind != K_RESET_DONE or got != cell:
        raise RuntimeError(f"reset kind={kind} cell={got}")
    connected = wait_connected(gw_log, connected + 1)
    return sock, connected


def stats(sock: socket.socket, cell: int) -> dict:
    sock.sendall(frame(K_STATS_REQ, 0, cell, 0, 0, 0, time.time()))
    first = recvall(sock, FRAME)
    second = recvall(sock, FRAME)
    kind, _i, _cell, queued, received, _d, _t = HDR.unpack(first[:32])
    if kind != K_STATS:
        raise RuntimeError(f"stats kind {kind}")
    forwarded, rate, superseded, replica, low, queue_drop, expired, maxq = STATS.unpack(first[32:64])
    kind2, *_rest = HDR.unpack(second[:32])
    if kind2 != K_STATS2:
        raise RuntimeError(f"stats2 kind {kind2}")
    topk, stale, ghost, live, entries, congested_ms, pqueued, _reserved = STATS2.unpack(second[32:64])
    return {
        "received": received, "queued": queued, "forwarded": forwarded,
        "superseded": superseded, "replica_cap": replica, "low_utility": low,
        "queue_drop": queue_drop, "expired": expired, "maxq": maxq,
        "global_topk": topk, "stale_cell": stale, "live_queued": live,
        "congested_ms": congested_ms, "pqueued": pqueued,
    }


def lookup(snap: dict, worker: int, digest: int) -> int | None:
    for item_worker, item_digest, coverage in snap["tested"]:
        if item_worker == worker and int(item_digest) == digest:
            return coverage
    return None


def send_frames(sock: socket.socket, cell: int, rows: list[tuple[int, int, int, int]], seq0: int) -> int:
    """rows: (kind, worker, coverage, digest). Returns next seq."""
    blob = b""
    seq = seq0
    for kind, worker, coverage, digest in rows:
        blob += frame(kind, worker, cell, seq, coverage, digest & ((1 << 64) - 1), time.time())
        seq += 1
    if blob:
        sock.sendall(blob)
    return seq


def wait_coverage(worker: int, digest: int, coverage: int | None, min_applied: int, timeout: float = 3.0) -> dict:
    deadline = time.time() + timeout
    last = ctrl("snapshot")
    while time.time() < deadline:
        last = ctrl("snapshot")
        got = lookup(last, worker, digest)
        if last["applied"] >= min_applied and got == coverage:
            return last
        time.sleep(0.01)
    return last


def wait_quiet(min_applied: int, timeout: float = 3.0) -> dict:
    deadline = time.time() + timeout
    last = ctrl("snapshot")
    while time.time() < deadline:
        last = ctrl("snapshot")
        if last["applied"] >= min_applied:
            return last
        time.sleep(0.01)
    return last


def compare_worker(snap: dict, worker: int, expected: dict[int, int]) -> dict:
    present = {
        int(digest): coverage
        for item_worker, digest, coverage in snap["tested"]
        if item_worker == worker
    }
    false_positive = 0
    false_negative = 0
    mismatches = 0
    for digest, coverage in expected.items():
        got = present.get(digest)
        if got is None:
            mismatches += 1
            false_negative += coverage
        elif got != coverage:
            mismatches += 1
            false_positive += max(0, got - coverage)
            false_negative += max(0, coverage - got)
    for digest, coverage in present.items():
        if digest not in expected:
            mismatches += 1
            false_positive += coverage
    return {
        "keys": len(present),
        "expected_keys": len(expected),
        "mismatches": mismatches,
        "false_positive_tokens": false_positive,
        "false_negative_tokens": false_negative,
    }


def phase1(sock: socket.socket, cell: int, adapter: dict) -> dict:
    updates = adapter["updates"]
    origin = time.perf_counter()
    t0 = updates[0]["recv_ns"]
    seq = 1
    sent = 0
    index = 0
    checkpoints = []
    growth_done = False
    rollback_done = False
    while index < len(updates):
        stamp = updates[index]["recv_ns"]
        end = index
        while end < len(updates) and updates[end]["recv_ns"] == stamp:
            end += 1
        delay = origin + (stamp - t0) / 1e9 - time.perf_counter()
        if delay > 0:
            time.sleep(delay)
        group = updates[index:end]
        rows = [
            (K_UP if row["kind"] == "UPSERT" else K_TOMB, 0, row["coverage_tokens"], row["lineage_id"])
            for row in group
        ]
        seq = send_frames(sock, cell, rows, seq)
        sent += len(rows)
        lineages = {row["lineage_id"]: row for row in group}
        if not growth_done and 82603029728786142 in lineages and lineages[82603029728786142]["coverage_tokens"] == 1056:
            first = ctrl("snapshot")
            drained = wait_coverage(0, 82603029728786142, 1056, sent)
            checkpoints.append({
                "name": "growth_1040_to_1056",
                "evidence": "TRACE_REPLAY",
                "sent": sent,
                "first_applied": first["applied"],
                "drained_applied": drained["applied"],
                "dispatcher_coverage": lookup(drained, 0, 82603029728786142),
                "adapter_coverage": 1056,
                "oracle_coverage": lineages[82603029728786142]["oracle_coverage"],
            })
            growth_done = True
        if not rollback_done and REGRESSION in lineages and lineages[REGRESSION]["coverage_tokens"] == 448:
            first = ctrl("snapshot")
            drained = wait_coverage(0, REGRESSION, 448, sent)
            checkpoints.append({
                "name": "rollback_to_448",
                "evidence": "TRACE_REPLAY",
                "sent": sent,
                "first_applied": first["applied"],
                "first_coverage": lookup(first, 0, REGRESSION),
                "drained_applied": drained["applied"],
                "dispatcher_coverage": lookup(drained, 0, REGRESSION),
                "adapter_coverage": 448,
                "oracle_coverage": lineages[REGRESSION]["oracle_coverage"],
                "in_flight_at_first_look": max(0, sent - first["applied"]),
            })
            rollback_done = True
        index = end
    final_snap = wait_quiet(0, timeout=0.2)
    # The last sends may still be in the gateway. Wait until applied stops growing.
    last_applied = -1
    stable = 0
    deadline = time.time() + 5
    while time.time() < deadline and stable < 5:
        final_snap = ctrl("snapshot")
        if final_snap["applied"] == last_applied:
            stable += 1
        else:
            stable = 0
            last_applied = final_snap["applied"]
        time.sleep(0.05)
    expected = {int(key): value for key, value in adapter["live"].items()}
    compared = compare_worker(final_snap, 0, expected)
    gateway = stats(sock, cell)
    checkpoints.append({
        "name": "end_of_trace",
        "evidence": "TRACE_REPLAY",
        "sent": sent,
        "dispatcher_coverage": lookup(final_snap, 0, REGRESSION),
        "adapter_coverage": expected.get(REGRESSION),
        "oracle_coverage": expected.get(REGRESSION),
        **compared,
    })
    return {
        "native_events_processed": adapter["native_events"],
        "adapter_updates_generated": len(updates),
        "gateway": gateway,
        "dispatcher_applied": final_snap["applied"],
        "checkpoints": checkpoints,
        "final": compared,
        "regression_final": lookup(final_snap, 0, REGRESSION),
        "pacing": "original_recv_ns",
        "policy": "StaticTopK",
        "max_inflight": 0,
    }


def burst_test(sock: socket.socket, cell: int, name: str, steps: list[dict]) -> dict:
    """Each step is {rows, wait_digest, wait_coverage, wait_worker, min_applied_add}."""
    seq = 1
    sent = 0
    observations = []
    for step in steps:
        seq = send_frames(sock, cell, step["rows"], seq)
        sent += len(step["rows"])
        if step.get("drain"):
            snap = wait_coverage(
                step.get("worker", 0), step["digest"], step["coverage"],
                sent if step.get("expect_forwarded", True) else 0,
                timeout=step.get("timeout", 3),
            )
            observations.append({
                "label": step["label"],
                "applied": snap["applied"],
                "coverage": lookup(snap, step.get("worker", 0), step["digest"]),
                "worker_b": lookup(snap, 1, step["digest"]) if step.get("also_worker_b") else None,
                "sent": sent,
            })
        else:
            time.sleep(step.get("pause", 0.05))
    snap = ctrl("snapshot")
    deadline = time.time() + 3
    last = -1
    stable = 0
    while time.time() < deadline and stable < 4:
        snap = ctrl("snapshot")
        if snap["applied"] == last:
            stable += 1
        else:
            stable = 0
            last = snap["applied"]
        time.sleep(0.05)
    return {
        "name": name,
        "evidence": "SYNTHETIC_REGRESSION" if "trace" not in name else "TRACE_REPLAY",
        "sent": sent,
        "applied": snap["applied"],
        "observations": observations,
        "tested": snap["tested"],
        "gateway": stats(sock, cell),
        "history": ctrl("history")["history"],
    }


def run_synthetics(gw_log: Path, connected: int) -> dict:
    out = {}
    # Version: in-order rollback, then a delayed older coverage, then tomb + late upsert.
    sock, connected = open_agent(352, gw_log, connected)
    version = []
    seq = send_frames(sock, 352, [(K_UP, 0, 1056, 501)], 1)
    version.append(wait_coverage(0, 501, 1056, 1))
    seq = send_frames(sock, 352, [(K_UP, 0, 448, 501)], seq)
    version.append(wait_coverage(0, 501, 448, 2))
    send_frames(sock, 352, [(K_UP, 0, 1040, 501)], seq)
    stale = wait_coverage(0, 501, 1040, 3)
    send_frames(sock, 352, [(K_TOMB, 0, 0, 501)], 4)
    after_tomb = wait_coverage(0, 501, None, 4)
    send_frames(sock, 352, [(K_UP, 0, 1056, 501)], 5)
    revived = wait_coverage(0, 501, 1056, 5)
    out["version"] = {
        "evidence": "SYNTHETIC_REGRESSION",
        "after_1056": lookup(version[0], 0, 501),
        "after_448": lookup(version[1], 0, 501),
        "after_delayed_1040": lookup(stale, 0, 501),
        "after_tomb": lookup(after_tomb, 0, 501),
        "after_late_upsert": lookup(revived, 0, 501),
        "gateway": stats(sock, 352),
        "history": ctrl("history")["history"],
    }
    sock.close()

    # Scenario A: fewer than K pending. Drain the long coverage, then roll back.
    sock, connected = open_agent(353, gw_log, connected)
    send_frames(sock, 353, [
        (K_UP, 0, 1056, 601),
        (K_UP, 0, 2000, 602),
        (K_UP, 0, 1800, 603),
    ], 1)
    wait_quiet(3)
    send_frames(sock, 353, [(K_UP, 0, 448, 601)], 4)
    drained = wait_coverage(0, 601, 448, 4)
    out["topk_a"] = {
        "evidence": "SYNTHETIC_REGRESSION",
        "dispatcher": lookup(drained, 0, 601),
        "applied": drained["applied"],
        "gateway": stats(sock, 353),
    }
    sock.close()

    # Scenario B: exactly 16 lineages in one burst, then the rollback of one of them
    # is in the same burst so it is still pending.
    sock, connected = open_agent(354, gw_log, connected)
    rows = [(K_UP, 0, 3000 + index, 700 + index) for index in range(15)]
    rows.append((K_UP, 0, 1056, 790))
    rows.append((K_UP, 0, 448, 790))
    send_frames(sock, 354, rows, 1)
    drained = wait_quiet(16, timeout=4)
    out["topk_b"] = {
        "evidence": "SYNTHETIC_REGRESSION",
        "dispatcher_rollback": lookup(drained, 0, 790),
        "applied": drained["applied"],
        "keys": len(drained["tested"]),
        "gateway": stats(sock, 354),
    }
    sock.close()

    # Scenario C: a published 1056, then a full pending set of higher lineages,
    # then the 448 correction. The burst is not the trace's timing.
    sock, connected = open_agent(355, gw_log, connected)
    send_frames(sock, 355, [(K_UP, 0, 1056, 801)], 1)
    published = wait_coverage(0, 801, 1056, 1)
    burst = [(K_UP, 0, 5000 + index, 810 + index) for index in range(16)]
    burst.append((K_UP, 0, 448, 801))
    send_frames(sock, 355, burst, 2)
    drained = wait_quiet(1, timeout=4)
    time.sleep(1.0)
    drained = ctrl("snapshot")
    out["topk_c"] = {
        "evidence": "SYNTHETIC_TOPK_STRESS",
        "published_1056": lookup(published, 0, 801),
        "after_burst": lookup(drained, 0, 801),
        "applied": drained["applied"],
        "keys": len(drained["tested"]),
        "gateway": stats(sock, 355),
        "history_for_801": [
            item for item in ctrl("history")["history"] if item["digest"] == str(801)
        ],
    }
    sock.close()

    # Replica: two owners, same digest. Not in the native trace.
    sock, connected = open_agent(356, gw_log, connected)
    send_frames(sock, 356, [(K_UP, 0, 1056, 901)], 1)
    wait_coverage(0, 901, 1056, 1)
    send_frames(sock, 356, [(K_UP, 1, 1024, 901)], 2)
    wait_coverage(1, 901, 1024, 2)
    send_frames(sock, 356, [(K_UP, 0, 448, 901)], 3)
    both = wait_coverage(0, 901, 448, 3)
    out["replica"] = {
        "evidence": "SYNTHETIC_REGRESSION",
        "owner_a": lookup(both, 0, 901),
        "owner_b": lookup(both, 1, 901),
        "applied": both["applied"],
        "gateway": stats(sock, 356),
    }
    sock.close()

    # Owner clear is per-digest tombstones, not K_RESET. A late upsert can revive.
    sock, connected = open_agent(357, gw_log, connected)
    send_frames(sock, 357, [
        (K_UP, 0, 1056, 911),
        (K_UP, 0, 1056, 912),
        (K_UP, 0, 1056, 913),
        (K_UP, 1, 1056, 911),
    ], 1)
    wait_quiet(4)
    send_frames(sock, 357, [
        (K_TOMB, 0, 0, 911),
        (K_TOMB, 0, 0, 912),
        (K_TOMB, 0, 0, 913),
    ], 5)
    cleared = wait_quiet(7)
    send_frames(sock, 357, [(K_UP, 0, 1056, 911)], 8)
    revived = wait_coverage(0, 911, 1056, 8)
    out["invalidation"] = {
        "evidence": "SYNTHETIC_REGRESSION",
        "owner_a_after_tombs": {
            "911": lookup(cleared, 0, 911),
            "912": lookup(cleared, 0, 912),
            "913": lookup(cleared, 0, 913),
        },
        "owner_b_same_digest": lookup(cleared, 1, 911),
        "revived_911": lookup(revived, 0, 911),
        "owner_b_after_revive": lookup(revived, 1, 911),
        "gateway": stats(sock, 357),
        "used_global_k_reset_for_owner": False,
    }
    sock.close()
    out["connected_count"] = connected
    return out


def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("")
        return
    fields: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in row:
            if key not in seen:
                seen.add(key)
                fields.append(key)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def driver() -> None:
    if port_listening(DISP_PORT) or port_listening(CTRL_PORT) or port_listening(GW_PORT):
        raise SystemExit(f"port busy {[DISP_PORT, CTRL_PORT, GW_PORT]}")
    RAW.mkdir(parents=True, exist_ok=True)
    adapter = build_adapter()
    (RAW / "adapter_trace.json").write_text(json.dumps({
        "native_events": adapter["native_events"],
        "updates": len(adapter["updates"]),
        "joins": adapter["joins"],
        "prompts_one": adapter["prompts_one"],
        "prompts_many": adapter["prompts_many"],
        "lineages_one": adapter["lineages_one"],
        "lineages_many": adapter["lineages_many"],
        "lineages": adapter["lineages"],
        "identity_stable_within_row": adapter["identity_stable_within_row"],
        "alive": adapter["alive"],
        "oracle_mismatches": adapter["oracle_mismatches"],
        "live_regression": adapter["live"].get(str(REGRESSION)),
    }, indent=2))
    if adapter["joins"]["one"] != 210 or adapter["prompts_many"] or adapter["lineages_many"]:
        raise SystemExit("lineage mapping failed the trace checks")
    if adapter["oracle_mismatches"]:
        raise SystemExit("adapter coverage diverged from the block oracle")
    procs = Proc()
    try:
        _gw, gw_log = launch(procs)
        connected = wait_connected(gw_log, 1)
        sock, connected = open_agent(351, gw_log, connected)
        paced = phase1(sock, 351, adapter)
        gateway_stats = paced["gateway"]
        sock.close()
        synthetic = run_synthetics(gw_log, connected)
        result = {
            "git_head": git_head(),
            "started_note": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "policy": "StaticTopK",
            "ports": {"dispatcher": DISP_PORT, "control": CTRL_PORT, "gateway": GW_PORT},
            "adapter": json.loads((RAW / "adapter_trace.json").read_text()),
            "phase1": paced,
            "synthetic": synthetic,
        }
        (RAW / "results.json").write_text(json.dumps(result, indent=2))
        timeseries = []
        for row in adapter["updates"]:
            timeseries.append({
                "point": "adapter_update",
                "evidence": "TRACE_REPLAY",
                "recv_ns": row["recv_ns"],
                "native_events_so_far": row["native_events_so_far"],
                "lineage_id": row["lineage_id"],
                "kind": row["kind"],
                "adapter_coverage": row["coverage_tokens"],
                "oracle_coverage": row["oracle_coverage"],
                "dispatcher_coverage": "",
                "note": "dispatcher sampled at checkpoints, not at every update",
            })
        for item in paced["checkpoints"]:
            timeseries.append({
                "point": item["name"],
                "evidence": item["evidence"],
                "adapter_coverage": item.get("adapter_coverage", ""),
                "oracle_coverage": item.get("oracle_coverage", ""),
                "dispatcher_coverage": item.get("dispatcher_coverage", ""),
                "first_applied": item.get("first_applied", ""),
                "drained_applied": item.get("drained_applied", item.get("dispatcher_applied", "")),
                "in_flight_at_first_look": item.get("in_flight_at_first_look", ""),
                "mismatches": item.get("mismatches", ""),
                "false_positive_tokens": item.get("false_positive_tokens", ""),
                "false_negative_tokens": item.get("false_negative_tokens", ""),
            })
        write_csv(OUT / "coverage_e2e_timeseries.csv", timeseries)
        def checkpoint_coverage(name: str):
            for item in paced["checkpoints"]:
                if item["name"] == name:
                    return item.get("dispatcher_coverage", "")
            return ""

        regression_rows = [
            {
                "test": "A_growth",
                "evidence": "TRACE_REPLAY",
                "expected": 1056,
                "dispatcher": checkpoint_coverage("growth_1040_to_1056"),
            },
            {
                "test": "B_rollback_448",
                "evidence": "TRACE_REPLAY",
                "expected": 448,
                "dispatcher": next((item["dispatcher_coverage"] for item in paced["checkpoints"] if item["name"] == "rollback_to_448"), ""),
            },
            {
                "test": "B_tail_416",
                "evidence": "TRACE_REPLAY",
                "expected": adapter["live"].get(str(REGRESSION)),
                "dispatcher": paced["regression_final"],
            },
            {
                "test": "version_rollback_then_stale_1040",
                "evidence": "SYNTHETIC_REGRESSION",
                "expected": "448_should_remain_if_versioned",
                "dispatcher": synthetic["version"]["after_delayed_1040"],
            },
            {
                "test": "version_tomb_then_late_upsert",
                "evidence": "SYNTHETIC_REGRESSION",
                "expected": "absent",
                "dispatcher": synthetic["version"]["after_late_upsert"],
            },
            {
                "test": "topk_A_under_capacity",
                "evidence": "SYNTHETIC_REGRESSION",
                "expected": 448,
                "dispatcher": synthetic["topk_a"]["dispatcher"],
            },
            {
                "test": "topk_B_exactly_k_pending",
                "evidence": "SYNTHETIC_REGRESSION",
                "expected": 448,
                "dispatcher": synthetic["topk_b"]["dispatcher_rollback"],
            },
            {
                "test": "topk_C_over_capacity",
                "evidence": "SYNTHETIC_TOPK_STRESS",
                "expected": 448,
                "dispatcher": synthetic["topk_c"]["after_burst"],
            },
            {
                "test": "replica_owner_a",
                "evidence": "SYNTHETIC_REGRESSION",
                "expected": 448,
                "dispatcher": synthetic["replica"]["owner_a"],
            },
            {
                "test": "replica_owner_b",
                "evidence": "SYNTHETIC_REGRESSION",
                "expected": 1024,
                "dispatcher": synthetic["replica"]["owner_b"],
            },
            {
                "test": "owner_tomb_leaves_other_owner",
                "evidence": "SYNTHETIC_REGRESSION",
                "expected": 1056,
                "dispatcher": synthetic["invalidation"]["owner_b_same_digest"],
            },
        ]
        write_csv(OUT / "coverage_regression_results.csv", regression_rows)
        print(json.dumps({
            "phase1_final": paced["final"],
            "phase1_gateway": gateway_stats,
            "checkpoints": paced["checkpoints"],
            "version": {key: synthetic["version"][key] for key in (
                "after_1056", "after_448", "after_delayed_1040", "after_tomb", "after_late_upsert")},
            "topk_a": synthetic["topk_a"]["dispatcher"],
            "topk_b": synthetic["topk_b"]["dispatcher_rollback"],
            "topk_c": synthetic["topk_c"]["after_burst"],
            "topk_c_drops": synthetic["topk_c"]["gateway"],
            "replica": synthetic["replica"],
            "invalidation": synthetic["invalidation"],
        }, indent=2, default=str))
    finally:
        procs.stop()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--role", choices=("driver", "dispatcher"), default="driver")
    parser.add_argument("--port", type=int, default=DISP_PORT)
    parser.add_argument("--control", type=int, default=CTRL_PORT)
    parser.add_argument("--cell", type=int, default=1)
    parser.add_argument("--ready-file", type=Path, default=RAW / "dispatcher_ready.json")
    args = parser.parse_args()
    if args.role == "dispatcher":
        asyncio.run(serve_dispatcher(args.port, args.control, args.cell, args.ready_file))
    else:
        driver()


if __name__ == "__main__":
    main()
