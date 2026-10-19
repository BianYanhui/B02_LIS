#!/usr/bin/env python3
"""F6-A2 real prefix growth and frozen-gateway extension supersession.

The wrapper in this file is EXPERIMENTAL_WRAPPER. It is not the formal
publish_state path. The gateway process is a second copy of the frozen relay
on ports 9720/9721. It does not talk to b02-gateway4t4 or port 9710.

Visibility delays are measured on the replay clock. Inter-arrival gaps are the
captured gaps. They are not an inline delay from the capture itself.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import importlib.util
import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import aiohttp
import msgspec
import zmq
from transformers import AutoTokenizer
from vllm.distributed.kv_events import BlockRemoved, BlockStored, KVEventBatch

ROOT = Path("/home/byh/B02")
OUT = ROOT / "analysis/fig6_reconstruction/f6_a2"
RAW = OUT / "raw"
VLLM = ROOT / "poc/.venv/bin/vllm"
MODEL = "/home/byh/.cache/modelscope/qwen/Qwen2.5-1.5B-Instruct"
RELAY_PY = ROOT / "experiments/4t4/net/gateway_relay_4t4.py"
HARNESS_PY = ROOT / "experiments/4t4/run_formal4t4.py"
BLOCK = 16
TARGETS = (1024, 2048, 4096)
FILLER_TEXT = " a"
FEAS_SEED = 2026101905
FORMAL_SEEDS = (2026101906, 2026101907, 2026101908)
RELAY_PORT = 9720
DISPATCH_PORT = 9721
# Frozen before any replay result: service one frame at half the feasibility
# offered rate, using the relay's real max_inflight window. Not HTB, not the
# rejected F6-P3 model, and not retuned per seed.
ACK_INTENSITY = 2.0


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def sha_ids(token_ids: list[int]) -> str:
    raw = ",".join(map(str, token_ids)).encode()
    return hashlib.sha256(raw).hexdigest()


def norm_hash(value) -> int | None:
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray)):
        return int.from_bytes(value, "big")
    return int(value)


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((p / 100.0) * (len(ordered) - 1))))
    return float(ordered[index])


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("")
        return
    columns = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def git_commit() -> str:
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()


class EventTap:
    def __init__(self, worker: int, port: int) -> None:
        self.worker = worker
        self.port = port
        self.rows: list[dict] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self.zmq_messages = 0
        self.block_stored = 0
        self.block_removed = 0
        self.cleared = 0
        self.hash_occurrences = 0

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)

    def _run(self) -> None:
        ctx = zmq.Context.instance()
        sock = ctx.socket(zmq.SUB)
        sock.setsockopt(zmq.SUBSCRIBE, b"")
        sock.setsockopt(zmq.RCVTIMEO, 200)
        sock.connect(f"tcp://127.0.0.1:{self.port}")
        decoder = msgspec.msgpack.Decoder(KVEventBatch)
        while not self._stop.is_set():
            try:
                parts = sock.recv_multipart()
            except zmq.Again:
                continue
            except zmq.ZMQError:
                break
            if len(parts) < 3:
                continue
            self.zmq_messages += 1
            seq = int.from_bytes(parts[1], "big") if len(parts[1]) == 8 else -1
            try:
                batch = decoder.decode(parts[2])
            except msgspec.DecodeError:
                continue
            recv = time.time()
            for index, event in enumerate(batch.events):
                kind = type(event).__name__
                hashes: list[int] = []
                parent = None
                token_ids: list[int] = []
                block_size = 0
                if isinstance(event, BlockStored):
                    self.block_stored += 1
                    hashes = [norm_hash(item) for item in event.block_hashes]
                    parent = norm_hash(event.parent_block_hash)
                    token_ids = [int(item) for item in event.token_ids]
                    block_size = int(event.block_size)
                    self.hash_occurrences += len(hashes)
                elif isinstance(event, BlockRemoved):
                    self.block_removed += 1
                    hashes = [norm_hash(item) for item in event.block_hashes]
                    self.hash_occurrences += len(hashes)
                else:
                    self.cleared += 1
                self.rows.append({
                    "worker": self.worker,
                    "recv_time": recv,
                    "batch_ts": float(batch.ts),
                    "batch_seq": seq,
                    "event_index": index,
                    "event_type": kind,
                    "parent": parent,
                    "hashes": hashes,
                    "token_ids": token_ids,
                    "block_size": block_size,
                })
        sock.close(linger=0)


def start_vllm(worker: int) -> subprocess.Popen:
    port = 8000 + worker
    zmq_port = 5560 + worker
    log_path = RAW / f"vllm_gpu{worker}.log"
    log = open(log_path, "w")
    env = os.environ.copy()
    env["CUDA_VISIBLE_DEVICES"] = str(worker)
    env["VLLM_SERVER_DEV_MODE"] = "1"
    config = json.dumps({
        "enable_kv_cache_events": True,
        "publisher": "zmq",
        "endpoint": f"tcp://*:{zmq_port}",
    })
    proc = subprocess.Popen(
        [
            str(VLLM), "serve", MODEL,
            "--host", "127.0.0.1", "--port", str(port),
            "--gpu-memory-utilization", "0.40",
            "--max-model-len", "6144", "--max-num-seqs", "4",
            "--enable-prefix-caching", "--enable-prompt-tokens-details",
            "--swap-space", "4", "--block-size", "16", "--enforce-eager",
            "--kv-events-config", config,
        ],
        env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
    )
    proc._log = log  # type: ignore[attr-defined]
    return proc


def stop_vllm(procs: list[subprocess.Popen]) -> None:
    for proc in procs:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGTERM)
    for proc in procs:
        try:
            proc.wait(timeout=25)
        except subprocess.TimeoutExpired:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL)
        log = getattr(proc, "_log", None)
        if log is not None:
            log.close()


def wait_ready(procs: list[subprocess.Popen], timeout_s: float = 240) -> str:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if any(proc.poll() is not None for proc in procs):
            raise RuntimeError("a vLLM process exited during startup")
        ready = 0
        model_name = ""
        for worker in range(4):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{8000+worker}/v1/models", timeout=2) as handle:
                    body = json.loads(handle.read().decode())
                ready += 1
                model_name = body["data"][0]["id"]
            except Exception:
                pass
        if ready == 4:
            return model_name
        time.sleep(2)
    raise TimeoutError("vLLM did not become ready")


def lineage_tokens(tokenizer, seed: int, worker: int, index: int) -> list[int]:
    nonce = tokenizer.encode(f"S{seed}W{worker}N{index:02d} ", add_special_tokens=False)
    filler = tokenizer.encode(FILLER_TEXT, add_special_tokens=False)
    if len(filler) != 1:
        raise RuntimeError(f"filler token is not a single id: {filler}")
    if not nonce or len(nonce) >= 64:
        raise RuntimeError(f"nonce length {len(nonce)} is outside the planned range")
    full = list(nonce) + [filler[0]] * (TARGETS[-1] - len(nonce))
    if len(full) != TARGETS[-1]:
        raise RuntimeError("token plan did not land on 4096 ids")
    return full


def chain_tokens(blocks: dict[int, tuple[int | None, list[int]]], tip: int | None) -> tuple[list[int], bool]:
    if tip is None or tip not in blocks:
        return [], False
    reversed_blocks: list[list[int]] = []
    seen = set()
    cursor: int | None = tip
    missing = False
    while cursor is not None:
        if cursor in seen or cursor not in blocks:
            missing = True
            break
        seen.add(cursor)
        parent, toks = blocks[cursor]
        reversed_blocks.append(toks)
        cursor = parent
    tokens: list[int] = []
    for part in reversed(reversed_blocks):
        tokens.extend(part)
    return tokens, not missing


def absorb(blocks: dict[int, tuple[int | None, list[int]]], event: dict) -> int | None:
    if event["event_type"] != "BlockStored":
        return None
    hashes = event["hashes"]
    token_ids = event["token_ids"]
    block_size = event["block_size"] or BLOCK
    if not hashes or len(token_ids) != len(hashes) * block_size:
        return None
    parent = event["parent"]
    tip = None
    for index, block_hash in enumerate(hashes):
        toks = token_ids[index * block_size:(index + 1) * block_size]
        blocks[block_hash] = (parent, toks)
        parent = block_hash
        tip = block_hash
    return tip


async def one_completion(session, port: int, model: str, token_ids: list[int]) -> dict:
    payload = {"model": model, "prompt": token_ids, "max_tokens": 1, "temperature": 0}
    started = time.time()
    async with session.post(
        f"http://127.0.0.1:{port}/v1/completions",
        json=payload,
        timeout=aiohttp.ClientTimeout(total=180),
    ) as response:
        body = await response.json()
        status = response.status
    finished = time.time()
    usage = body.get("usage") or {}
    details = usage.get("prompt_tokens_details") or {}
    return {
        "http_status": status,
        "started": started,
        "finished": finished,
        "prompt_tokens": usage.get("prompt_tokens"),
        "cached_tokens": details.get("cached_tokens"),
        "error": body.get("error"),
    }


async def capture_seed(tokenizer, model: str, taps: list[EventTap], seed: int, lineages: int, phase: str) -> list[dict]:
    blocks = [dict() for _ in range(4)]
    rows: list[dict] = []
    async with aiohttp.ClientSession() as session:
        async def run_worker(worker: int) -> list[dict]:
            local = []
            for index in range(lineages):
                full = lineage_tokens(tokenizer, seed, worker, index)
                versions = [full[:target] for target in TARGETS]
                for left, right in zip(versions, versions[1:]):
                    if versions[versions.index(right)][:len(left)] != left:
                        raise RuntimeError("constructed token prefix failed")
                if any(len(item) % BLOCK for item in versions):
                    raise RuntimeError("target is not block aligned")
                lineage = f"S{seed}W{worker}N{index:02d}"
                removed_before = taps[worker].block_removed
                cleared_before = taps[worker].cleared
                for version, token_ids in enumerate(versions, start=1):
                    window_start = len(taps[worker].rows)
                    result = await one_completion(session, 8000 + worker, model, token_ids)
                    await asyncio.sleep(0.2)
                    window = taps[worker].rows[window_start:]
                    tip = None
                    new_hashes = []
                    removed = 0
                    cleared = 0
                    stored = 0
                    for event in window:
                        if event["event_type"] == "BlockRemoved":
                            removed += 1
                        elif event["event_type"] == "AllBlocksCleared":
                            cleared += 1
                        elif event["event_type"] == "BlockStored":
                            stored += 1
                            new_hashes.extend(event["hashes"])
                            tip = absorb(blocks[worker], event) or tip
                    chain, complete = chain_tokens(blocks[worker], tip)
                    prompt_tokens = int(result["prompt_tokens"] or 0)
                    cached = int(result["cached_tokens"] or 0)
                    new_block_tokens = 0
                    for event in window:
                        if event["event_type"] == "BlockStored" and event["block_size"] in (0, BLOCK):
                            new_block_tokens += len(event["token_ids"])
                    chain_ok = complete and chain[:prompt_tokens] == token_ids[:prompt_tokens] and len(chain) >= prompt_tokens
                    aligned = prompt_tokens % BLOCK == 0 and prompt_tokens == len(token_ids)
                    accounted = (cached // BLOCK) * BLOCK + new_block_tokens
                    if aligned and chain_ok and len(chain) >= prompt_tokens and removed == 0 and cleared == 0:
                        coverage = prompt_tokens
                        evidence = "BLOCK_CHAIN_MATCH"
                    elif aligned and accounted == prompt_tokens and removed == 0 and cleared == 0 and cached % BLOCK == 0:
                        coverage = prompt_tokens
                        evidence = "CACHED_PLUS_NEW_BLOCKS"
                    else:
                        coverage = prompt_tokens if aligned else (prompt_tokens // BLOCK) * BLOCK
                        evidence = "PARTIAL"
                    local.append({
                        "label": "OBSERVED",
                        "wrapper": "EXPERIMENTAL_WRAPPER",
                        "phase": phase,
                        "seed": seed,
                        "worker": worker,
                        "lineage": lineage,
                        "version": version,
                        "owner": worker,
                        "target_tokens": TARGETS[version - 1],
                        "prompt_tokens": prompt_tokens,
                        "cached_tokens": cached,
                        "coverage_tokens": coverage,
                        "evidence": evidence,
                        "token_sha256": sha_ids(token_ids),
                        "token_prefix_of_next_plan": True,
                        "nonce_len": len(tokenizer.encode(f"S{seed}W{worker}N{index:02d} ", add_special_tokens=False)),
                        "first_ids": " ".join(map(str, token_ids[:8])),
                        "chain_tokens": len(chain),
                        "chain_complete": int(complete),
                        "chain_matches_prompt": int(chain_ok),
                        "new_block_stored_events": stored,
                        "new_block_hashes": len(new_hashes),
                        "new_block_tokens": new_block_tokens,
                        "block_removed_in_window": removed,
                        "cleared_in_window": cleared,
                        "http_status": result["http_status"],
                        "generation_time": result["finished"],
                        "request_started": result["started"],
                        "error": json.dumps(result["error"]) if result["error"] else "",
                    })
                if taps[worker].block_removed != removed_before or taps[worker].cleared != cleared_before:
                    for row in local[-3:]:
                        row["worker_saw_removal_during_lineage"] = 1
                else:
                    for row in local[-3:]:
                        row["worker_saw_removal_during_lineage"] = 0
            return local

        groups = await asyncio.gather(*[run_worker(worker) for worker in range(4)])
    for group in groups:
        rows.extend(group)
    rows.sort(key=lambda row: (row["generation_time"], row["worker"], row["version"]))
    return rows


def gate12(rows: list[dict]) -> dict:
    by = {}
    for row in rows:
        by.setdefault(row["lineage"], []).append(row)
    good = 0
    for lineage, items in by.items():
        items = sorted(items, key=lambda item: item["version"])
        if len(items) != 3:
            continue
        coverages = [item["coverage_tokens"] for item in items]
        evidences = [item["evidence"] for item in items]
        cached_ok = items[1]["cached_tokens"] >= items[0]["coverage_tokens"] and items[2]["cached_tokens"] >= items[1]["coverage_tokens"]
        if coverages[0] < coverages[1] < coverages[2] and all(item != "PARTIAL" for item in evidences) and cached_ok and all(item["chain_matches_prompt"] for item in items):
            good += 1
    upserts = len(rows)
    partial = sum(row["evidence"] == "PARTIAL" for row in rows)
    return {
        "lineages": len(by),
        "growing_lineages": good,
        "upserts": upserts,
        "partial_rows": partial,
        "gate1": good >= 1,
        "gate2": upserts == len(by) * 3 and partial == 0 and good == len(by),
    }


def offered_rate(rows: list[dict]) -> float:
    times = [row["generation_time"] for row in rows]
    span = max(times) - min(times)
    if span <= 0:
        return 0.0
    return (len(rows) - 1) / span


class RelaySession:
    def __init__(self, relay_mod, harness) -> None:
        self.relay_mod = relay_mod
        self.h = harness
        self.proc: subprocess.Popen | None = None
        self.lines: list[dict] = []
        self._stop = threading.Event()
        self.server = None
        self.ack_delay = 0.0
        self.cell_id = -1
        self.applied: list[dict] = []
        self.dispatcher = None
        self.digest_map: dict[int, str] = {}
        self._reader_task = None

    def start_process(self) -> None:
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        self.proc = subprocess.Popen(
            [sys.executable, str(RELAY_PY), "--listen", str(RELAY_PORT), "--downstream", f"127.0.0.1:{DISPATCH_PORT}", "--max-queue", "4096"],
            cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True, env=env, text=True,
        )
        thread = threading.Thread(target=self._read_stdout, daemon=True)
        thread.start()

    def _read_stdout(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        handle = RAW / "relay_stdout.jsonl"
        with handle.open("w") as sink:
            for line in self.proc.stdout:
                sink.write(line)
                sink.flush()
                try:
                    self.lines.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

    async def start_dispatcher(self) -> None:
        self.server = await asyncio.start_server(self._on_down, "127.0.0.1", DISPATCH_PORT)

    async def _on_down(self, reader, writer) -> None:
        try:
            while True:
                data = await reader.readexactly(64)
                recv = time.time()
                kind, instance, cell, seq, coverage, digest, t_send = self.h.HDR.unpack(data[:32])
                if kind == self.h.K_UP and cell == self.cell_id and self.dispatcher is not None:
                    name = self.digest_map.get(int(digest))
                    if name is not None:
                        self.dispatcher.apply_upsert(instance, name, int(coverage), float(t_send), recv)
                    self.applied.append({
                        "cell": cell,
                        "owner": instance,
                        "digest": int(digest),
                        "lineage": name,
                        "coverage_tokens": int(coverage),
                        "update_id": seq,
                        "generation_time_shifted": float(t_send),
                        "dispatcher_recv_time": recv,
                        "dispatcher_apply_time": recv,
                    })
                if self.ack_delay > 0:
                    await asyncio.sleep(self.ack_delay)
                writer.write(self.h.HDR.pack(self.h.K_ACK, 0, cell, seq, 0, 0, time.time()) + b"\x00" * 32)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionResetError):
            pass
        finally:
            writer.close()

    async def connect_agent(self):
        deadline = time.time() + 10
        last = None
        while time.time() < deadline:
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", RELAY_PORT)
                return reader, writer
            except OSError as exc:
                last = exc
                await asyncio.sleep(0.1)
        raise RuntimeError(f"relay agent port not ready: {last}")

    async def run_arm(self, rows: list[dict], *, cell_id: int, merge: int, ack_delay: float, condition: str) -> dict:
        self.cell_id = cell_id
        self.ack_delay = ack_delay
        self.applied = []
        self.h.URLS = [f"http://127.0.0.1:{8000+i}" for i in range(4)]
        self.dispatcher = self.h.Dispatcher(4, 50.0, 2.0, 0.5)
        self.digest_map = {self.h.digest64(row["lineage"]): row["lineage"] for row in rows}
        reader, writer = await self.connect_agent()
        relay = self.relay_mod
        mode = relay.MODE_FULLSYNC
        payload = relay.CFG.pack(mode, merge, 0, 0, 0, 0, 4096, 1 if ack_delay else 0, 0, 1)
        writer.write(self.h.HDR.pack(self.h.K_CONFIG, 0, cell_id, 0, 0, 0, time.time()) + payload.ljust(32, b"\x00"))
        await writer.drain()
        writer.write(self.h.HDR.pack(self.h.K_RESET, 0, cell_id, 0, 0, 0, time.time()) + b"\x00" * 32)
        await writer.drain()
        reset = await asyncio.wait_for(reader.readexactly(64), timeout=10)
        kind, *_rest = self.h.HDR.unpack(reset[:32])
        if kind != self.h.K_RESET_DONE:
            raise RuntimeError(f"reset ack kind {kind}")
        await asyncio.sleep(0.3)
        mark = len(self.lines)
        first = rows[0]["generation_time"]
        origin = time.time()
        shifted = []
        for seq, row in enumerate(rows):
            when = origin + (row["generation_time"] - first)
            shifted.append(when)
            delay = when - time.time()
            if delay > 0:
                await asyncio.sleep(delay)
            frame = self.h.HDR.pack(
                self.h.K_UP, row["worker"], cell_id, seq, int(row["coverage_tokens"]),
                self.h.digest64(row["lineage"]), when,
            ) + b"\x00" * 32
            writer.write(frame)
            await writer.drain()
        timeout = max(30.0, (ack_delay + 0.05) * (len(rows) + 2))
        deadline = time.perf_counter() + timeout
        while time.perf_counter() < deadline:
            forwarded = [
                line for line in self.lines[mark:]
                if line.get("event") == "update" and line.get("cell") == cell_id and line.get("stage") == "forwarded"
            ]
            if len(forwarded) + sum(
                1 for line in self.lines[mark:]
                if line.get("event") == "update" and line.get("cell") == cell_id and line.get("stage") == "suppressed"
            ) >= len(rows) and len(self.applied) == len(forwarded):
                await asyncio.sleep(0.3)
                break
            await asyncio.sleep(0.05)
        writer.write(self.h.HDR.pack(self.h.K_STATS_REQ, 0, cell_id, 0, 0, 0, time.time()) + b"\x00" * 32)
        await writer.drain()
        stats = {}
        try:
            for _ in range(2):
                blob = await asyncio.wait_for(reader.readexactly(64), timeout=10)
                skind, _i, _c, seq, coverage, _d, _t = self.h.HDR.unpack(blob[:32])
                if skind == self.h.K_STATS:
                    forwarded, rate, superseded, cap, util, qdrop, expired, maxq = self.h.STATS.unpack(blob[32:64])
                    stats.update({
                        "forwarded": forwarded, "superseded": superseded, "replica_cap": cap,
                        "low_utility": util, "queue_drop": qdrop, "expired": expired, "maxq": maxq,
                        "received": coverage, "queued_at_stats": seq, "rate_limit": rate,
                    })
                elif skind == 9:
                    topk, stale, marked, depth, entries, congested_ms, pqueue, _z = self.h.STATS.unpack(blob[32:64])
                    stats.update({"live_depth": depth, "stale_cell": stale, "congested_ms": congested_ms, "pqueue": pqueue})
        except asyncio.TimeoutError:
            stats["error"] = "stats_timeout"
        await asyncio.sleep(self.ack_delay + 0.5)
        writer.close()
        updates = [line for line in self.lines[mark:] if line.get("event") == "update" and line.get("cell") == cell_id]
        ticks = [line for line in self.lines[mark:] if line.get("event") == "tick"]
        return {
            "condition": condition,
            "merge": merge,
            "ack_delay_s": ack_delay,
            "cell_id": cell_id,
            "shifted_generation": shifted,
            "updates": updates,
            "applied": list(self.applied),
            "stats": stats,
            "max_tick_queued": max((int(line.get("queued") or 0) for line in ticks), default=0),
            "dispatcher": self.dispatcher,
        }

    def close(self) -> None:
        self._stop.set()
        if self.proc is not None and self.proc.poll() is None:
            os.killpg(self.proc.pid, signal.SIGTERM)
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(self.proc.pid, signal.SIGKILL)
        if self.proc is not None and self.proc.stdout is not None:
            self.proc.stdout.close()


def classify_and_visibility(rows: list[dict], arm: dict) -> tuple[list[dict], list[dict], dict]:
    updates = arm["updates"]
    by_id = {}
    for line in updates:
        by_id.setdefault(int(line["update_id"]), []).append(line)
    events = []
    for line in updates:
        events.append({
            "condition": arm["condition"],
            "merge": arm["merge"],
            "seed": rows[0]["seed"],
            "stage": line.get("stage"),
            "reason": line.get("suppression_reason") or "",
            "update_id": line.get("update_id"),
            "owner": line.get("owner"),
            "coverage_tokens": line.get("coverage_tokens"),
            "digest": line.get("prefix_digest64"),
            "generation_time_shifted": line.get("generation_time"),
            "gateway_time": line.get("gateway_arrival_time"),
            "gateway_send_time": line.get("gateway_send_time"),
            "cell": line.get("cell"),
        })
    # Pending set tracked from the log order.
    pending = {}
    true_ext = same = tomb = anomalous = other = 0
    eligible = 0
    increasing_not_pending = 0
    classified = []
    order = sorted(updates, key=lambda line: (line.get("gateway_arrival_time") or 0, 0 if line.get("stage") == "suppressed" else 1))
    for line in order:
        stage = line.get("stage")
        owner = int(line.get("owner"))
        digest = int(line.get("prefix_digest64"))
        key = (owner, digest)
        coverage = int(line.get("coverage_tokens"))
        uid = int(line.get("update_id"))
        if stage == "enqueued":
            previous = pending.get(key)
            if previous is not None:
                increasing_not_pending += 0
            else:
                source = rows[uid]
                if source["version"] > 1:
                    increasing_not_pending += 1
            pending[key] = {"update_id": uid, "coverage": coverage}
        elif stage == "suppressed" and line.get("suppression_reason") == "superseded":
            previous = pending.get(key)
            kind = "OTHER"
            if previous is not None:
                if coverage > previous["coverage"]:
                    kind = "TRUE_EXTENSION"
                    true_ext += 1
                    eligible += 1
                elif coverage == previous["coverage"]:
                    kind = "SAME_COVERAGE_DUPLICATE"
                    same += 1
                else:
                    kind = "ANOMALOUS"
                    anomalous += 1
                pending.pop(key, None)
            else:
                kind = "OTHER"
                other += 1
            classified.append({"old_update_id": None if previous is None else previous["update_id"], "new_coverage": coverage, "old_coverage": None if previous is None else previous["coverage"], "class": kind, "owner": owner, "digest": digest})
        elif stage == "forwarded":
            pending.pop(key, None)
    # The suppressed line carries the OLD frame's coverage, and the pending
    # entry is that old frame. Recompute with a correct read of the log:
    # _supersede emits the old frame, then enqueue emits the new frame.
    true_ext = same = anomalous = other = eligible = 0
    increasing_not_pending = 0
    classified = []
    pending = {}
    for line in order:
        stage = line.get("stage")
        owner = int(line.get("owner"))
        digest = int(line.get("prefix_digest64"))
        key = (owner, digest)
        coverage = int(line.get("coverage_tokens"))
        uid = int(line.get("update_id"))
        if stage == "suppressed" and line.get("suppression_reason") == "superseded":
            previous = pending.get(key)
            if previous is None or previous["update_id"] != uid:
                classified.append({"class": "OTHER", "update_id": uid, "owner": owner, "coverage": coverage})
                other += 1
                continue
            pending.pop(key, None)
            # The replacement is the next enqueued line for this key. Stash it.
            previous["superseded"] = True
            pending[("pending-super",) + key] = previous
        elif stage == "enqueued":
            marker = pending.pop(("pending-super",) + key, None)
            if marker is not None:
                eligible += 1
                if coverage > marker["coverage"]:
                    klass = "TRUE_EXTENSION"
                    true_ext += 1
                elif coverage == marker["coverage"]:
                    klass = "SAME_COVERAGE_DUPLICATE"
                    same += 1
                else:
                    klass = "ANOMALOUS"
                    anomalous += 1
                classified.append({
                    "class": klass,
                    "old_update_id": marker["update_id"],
                    "new_update_id": uid,
                    "owner": owner,
                    "old_coverage": marker["coverage"],
                    "new_coverage": coverage,
                    "lineage": rows[uid]["lineage"],
                })
            elif rows[uid]["version"] > 1:
                increasing_not_pending += 1
            pending[key] = {"update_id": uid, "coverage": coverage, "lineage": rows[uid]["lineage"]}
        elif stage == "forwarded":
            current = pending.get(key)
            if current is not None and current["update_id"] == uid:
                pending.pop(key, None)
    forwarded_ids = {int(line["update_id"]) for line in updates if line.get("stage") == "forwarded"}
    applied = {int(item["update_id"]): item for item in arm["applied"]}
    # Visibility: a version is visible when an apply for the same lineage/owner
    # has coverage >= its coverage. Later covering versions count.
    applies_by_key = {}
    for item in arm["applied"]:
        applies_by_key.setdefault((item["owner"], item["lineage"]), []).append(item)
    visibility = []
    for row, shifted in zip(rows, arm["shifted_generation"]):
        key = (row["worker"], row["lineage"])
        cover = None
        for item in applies_by_key.get(key, []):
            if int(item["coverage_tokens"]) >= int(row["coverage_tokens"]):
                cover = item
                break
        if cover is None:
            delay = None
            visible = 0
            covered_by = ""
        else:
            delay = cover["dispatcher_apply_time"] - shifted
            visible = 1
            covered_by = str(cover["update_id"])
        visibility.append({
            "seed": row["seed"],
            "condition": arm["condition"],
            "merge": arm["merge"],
            "lineage": row["lineage"],
            "owner": row["worker"],
            "version": row["version"],
            "coverage_tokens": row["coverage_tokens"],
            "generation_time_shifted": shifted,
            "visible": visible,
            "visibility_delay_s": delay,
            "covered_by_update_id": covered_by,
            "forwarded_itself": int(row and (rows.index(row) in forwarded_ids)),
        })
    # index() is ambiguous if duplicate objects; use enumerate instead.
    visibility = []
    for uid, (row, shifted) in enumerate(zip(rows, arm["shifted_generation"])):
        key = (row["worker"], row["lineage"])
        cover = None
        for item in applies_by_key.get(key, []):
            if int(item["coverage_tokens"]) >= int(row["coverage_tokens"]):
                cover = item
                break
        delay = None if cover is None else cover["dispatcher_apply_time"] - shifted
        visibility.append({
            "seed": row["seed"],
            "condition": arm["condition"],
            "merge": "on" if arm["merge"] else "off",
            "lineage": row["lineage"],
            "owner": row["worker"],
            "version": row["version"],
            "coverage_tokens": row["coverage_tokens"],
            "generation_time_shifted": shifted,
            "visible": int(cover is not None),
            "visibility_delay_s": "" if delay is None else delay,
            "covered_by_update_id": "" if cover is None else cover["update_id"],
            "forwarded_itself": int(uid in forwarded_ids),
            "evidence_label": "TIMED_REPLAY",
        })
    delays = [float(item["visibility_delay_s"]) for item in visibility if item["visible"]]
    never = sum(item["visible"] == 0 for item in visibility)
    final_match = 0
    final_total = 0
    regressions = 0
    seen = {}
    for item in arm["applied"]:
        key = (item["owner"], item["lineage"])
        previous = seen.get(key)
        if previous is not None and int(item["coverage_tokens"]) < previous:
            regressions += 1
        seen[key] = int(item["coverage_tokens"])
    latest = {}
    for row in rows:
        latest[(row["worker"], row["lineage"])] = row["coverage_tokens"]
    for key, coverage in latest.items():
        final_total += 1
        got = arm["dispatcher"].index[key[0]].get(key[1], 0)
        if got == coverage:
            final_match += 1
    summary = {
        "seed": rows[0]["seed"],
        "phase": rows[0]["phase"],
        "condition": arm["condition"],
        "merge": "on" if arm["merge"] else "off",
        "offered": len(rows),
        "forwarded": sum(line.get("stage") == "forwarded" for line in updates),
        "superseded": sum(line.get("stage") == "suppressed" and line.get("suppression_reason") == "superseded" for line in updates),
        "eligible_extension_opportunities": eligible,
        "true_extension_supersessions": true_ext,
        "same_coverage_duplicate": same,
        "anomalous": anomalous,
        "other_supersession": other,
        "increasing_already_forwarded": increasing_not_pending,
        "true_extension_ratio": (true_ext / eligible) if eligible else "",
        "visibility_p50_s": percentile(delays, 50),
        "visibility_p95_s": percentile(delays, 95),
        "never_visible": never,
        "never_visible_fraction": never / len(rows) if rows else "",
        "final_coverage_match": final_match,
        "final_coverage_total": final_total,
        "coverage_regressions": regressions,
        "max_tick_queued": arm["max_tick_queued"],
        "relay_maxq": arm["stats"].get("maxq"),
        "relay_forwarded_stats": arm["stats"].get("forwarded"),
        "relay_superseded_stats": arm["stats"].get("superseded"),
        "ack_delay_s": arm["ack_delay_s"],
        "stats_error": arm["stats"].get("error", ""),
    }
    for row in classified:
        row["seed"] = rows[0]["seed"]
        row["condition"] = arm["condition"]
        row["merge"] = "on" if arm["merge"] else "off"
    for row in events:
        row["seed"] = rows[0]["seed"]
    return events, visibility, summary | {"classes": classified}


def staleness(rows: list[dict], arm: dict) -> float | None:
    """Integral of max(0, worker_coverage - dispatcher_coverage) over the replay."""
    if not arm["shifted_generation"]:
        return None
    start = arm["shifted_generation"][0]
    ends = [item["dispatcher_apply_time"] for item in arm["applied"]] or [arm["shifted_generation"][-1]]
    end = max(max(ends), arm["shifted_generation"][-1])
    if end <= start:
        return None
    total = 0.0
    lineages = sorted({row["lineage"] for row in rows})
    for lineage in lineages:
        points = [(start, 0, 0)]
        worker = 0
        dispatcher = 0
        events = []
        for row, shifted in zip(rows, arm["shifted_generation"]):
            if row["lineage"] != lineage:
                continue
            worker_id = row["worker"]
            events.append((shifted, "gen", row["coverage_tokens"]))
        for item in arm["applied"]:
            if item["lineage"] != lineage:
                continue
            events.append((item["dispatcher_apply_time"], "apply", item["coverage_tokens"]))
        events.sort()
        cursor = start
        w_cov = 0
        d_cov = 0
        area = 0.0
        for stamp, kind, coverage in events:
            area += max(0, w_cov - d_cov) * max(0.0, stamp - cursor)
            cursor = stamp
            if kind == "gen":
                w_cov = max(w_cov, int(coverage))
            else:
                d_cov = int(coverage)
        area += max(0, w_cov - d_cov) * max(0.0, end - cursor)
        total += area
        _ = worker_id
    weight = len(lineages) * (end - start)
    return total / weight if weight else None


def ports_busy() -> list[int]:
    text = subprocess.check_output(["ss", "-ltn"], text=True)
    busy = []
    for port in (8000, 8001, 8002, 8003, RELAY_PORT, DISPATCH_PORT, 5560, 5561, 5562, 5563):
        if f":{port} " in text or text.rstrip().endswith(f":{port}"):
            busy.append(port)
    return busy


async def replay_trace(session: RelaySession, rows: list[dict], ack_delay: float, cell_base: int) -> list[dict]:
    arms = []
    plan = (
        ("natural", 0, 0.0),
        ("natural", 1, 0.0),
        ("controlled", 0, ack_delay),
        ("controlled", 1, ack_delay),
    )
    for offset, (condition, merge, delay) in enumerate(plan):
        arm = await session.run_arm(rows, cell_id=cell_base + offset, merge=merge, ack_delay=delay, condition=condition)
        arm["time_weighted_coverage_staleness"] = staleness(rows, arm)
        arms.append((rows, arm))
    return arms


def flatten(arms: list[tuple[list[dict], dict]]) -> tuple[list[dict], list[dict], list[dict], list[dict]]:
    gateway_rows = []
    visibility_rows = []
    summaries = []
    classes = []
    for rows, arm in arms:
        events, visibility, summary = classify_and_visibility(rows, arm)
        summary["time_weighted_coverage_staleness_tokens"] = arm.get("time_weighted_coverage_staleness")
        gateway_rows.extend(events)
        visibility_rows.extend(visibility)
        classes.extend(summary.pop("classes"))
        summaries.append(summary)
    return gateway_rows, visibility_rows, summaries, classes


async def async_main() -> int:
    RAW.mkdir(parents=True, exist_ok=True)
    busy = [port for port in ports_busy() if port != 9710]
    if busy:
        raise RuntimeError(f"required ports already in use: {busy}")
    tokenizer = AutoTokenizer.from_pretrained(MODEL, trust_remote_code=True)
    relay_mod = load_module("f6a2_relay", RELAY_PY)
    harness = load_module("f6a2_harness", HARNESS_PY)
    procs = [start_vllm(worker) for worker in range(4)]
    taps = [EventTap(worker, 5560 + worker) for worker in range(4)]
    session = RelaySession(relay_mod, harness)
    try:
        model = wait_ready(procs)
        for tap in taps:
            tap.start()
        await asyncio.sleep(0.5)
        feas_rows = await capture_seed(tokenizer, model, taps, FEAS_SEED, 2, "feasibility")
        write_csv(OUT / "f6_a2_source_growth.csv", feas_rows)
        gates = gate12(feas_rows)
        rate = offered_rate(feas_rows)
        ack_delay = (ACK_INTENSITY / rate) if rate > 0 else None
        (OUT / "capacity_rule.json").write_text(json.dumps({
            "rule": "ack_delay_s = 2 / feasibility_offered_upserts_per_s; max_inflight = 1 on the controlled arm only",
            "feasibility_offered_rate_per_s": rate,
            "ack_delay_s": ack_delay,
            "gates": gates,
            "fixed_before_replay": True,
        }, indent=2) + "\n")
        if not gates["gate1"] or not gates["gate2"] or not ack_delay:
            (OUT / "summary").mkdir(parents=True, exist_ok=True)
            (OUT / "summary" / "stop.json").write_text(json.dumps({"stopped": "gate12", "gates": gates, "rate": rate}, indent=2) + "\n")
            print(json.dumps({"stopped": "gate12", "gates": gates, "rate": rate}))
            return 2
        session.start_process()
        await session.start_dispatcher()
        feas_arms = await replay_trace(session, feas_rows, ack_delay, 100)
        all_rows = list(feas_rows)
        all_arms = list(feas_arms)
        native = {
            "phase": "feasibility",
            "zmq_messages": sum(tap.zmq_messages for tap in taps),
            "block_stored": sum(tap.block_stored for tap in taps),
            "block_removed": sum(tap.block_removed for tap in taps),
            "cleared": sum(tap.cleared for tap in taps),
            "hash_occurrences": sum(tap.hash_occurrences for tap in taps),
            "wrapper_upserts": len(feas_rows),
        }
        feas_summaries = flatten(feas_arms)[2]
        controlled_true = sum(row["true_extension_supersessions"] for row in feas_summaries if row["condition"] == "controlled" and row["merge"] == "on")
        natural_true = sum(row["true_extension_supersessions"] for row in feas_summaries if row["condition"] == "natural" and row["merge"] == "on")
        gate3 = controlled_true > 0 or natural_true > 0
        (OUT / "feasibility_gates.json").write_text(json.dumps({
            "gate1": gates["gate1"], "gate2": gates["gate2"], "gate3": gate3,
            "natural_true_extension": natural_true, "controlled_true_extension": controlled_true,
            "native_feasibility": native, "ack_delay_s": ack_delay,
        }, indent=2) + "\n")
        if not gate3:
            gateway_rows, visibility_rows, summaries, classes = flatten(all_arms)
            write_outputs(all_rows, gateway_rows, visibility_rows, summaries, classes, taps, model, ack_delay, "NO_GATE3")
            print(json.dumps({"stopped": "gate3", "natural_true": natural_true, "controlled_true": controlled_true}))
            return 3
        for seed in FORMAL_SEEDS:
            captured = await capture_seed(tokenizer, model, taps, seed, 4, "formal")
            all_rows.extend(captured)
            arms = await replay_trace(session, captured, ack_delay, 200 + (seed % 100) * 10)
            all_arms.extend(arms)
        gateway_rows, visibility_rows, summaries, classes = flatten(all_arms)
        write_outputs(all_rows, gateway_rows, visibility_rows, summaries, classes, taps, model, ack_delay, "RAN")
        print(json.dumps({"status": "ran", "cells": len(summaries), "ack_delay_s": ack_delay}))
        return 0
    finally:
        for tap in taps:
            tap.stop()
        session.close()
        if session.server is not None:
            session.server.close()
        stop_vllm(procs)


def write_outputs(source, gateway_rows, visibility_rows, summaries, classes, taps, model, ack_delay, status) -> None:
    write_csv(OUT / "f6_a2_source_growth.csv", source)
    write_csv(OUT / "f6_a2_gateway_events.csv", gateway_rows)
    write_csv(OUT / "f6_a2_visibility.csv", visibility_rows)
    write_csv(OUT / "f6_a2_per_cell.csv", summaries)
    write_csv(OUT / "f6_a2_supersession_classes.csv", classes)
    native_rows = []
    for phase in ("feasibility", "formal"):
        subset = [row for row in source if row["phase"] == phase]
        if not subset:
            continue
    native_rows.append({
        "model": model,
        "wrapper": "EXPERIMENTAL_WRAPPER",
        "zmq_messages": sum(tap.zmq_messages for tap in taps),
        "block_stored_events": sum(tap.block_stored for tap in taps),
        "block_removed_events": sum(tap.block_removed for tap in taps),
        "all_blocks_cleared": sum(tap.cleared for tap in taps),
        "block_hash_occurrences": sum(tap.hash_occurrences for tap in taps),
        "wrapper_upserts": len(source),
        "ack_delay_s": ack_delay,
        "status": status,
        "git_commit": git_commit(),
    })
    write_csv(OUT / "f6_a2_native_counts.csv", native_rows)
    # Pair merge on/off within seed and condition.
    pairs = []
    keyed = {(row["seed"], row["phase"], row["condition"], row["merge"]): row for row in summaries}
    for (seed, phase, condition, _merge), row in list(keyed.items()):
        if row["merge"] != "on":
            continue
        off = keyed.get((seed, phase, condition, "off"))
        if off is None:
            continue
        reduction = ""
        if off["forwarded"]:
            reduction = 1 - (row["forwarded"] / off["forwarded"])
        pairs.append({
            "seed": seed,
            "phase": phase,
            "condition": condition,
            "offered": row["offered"],
            "forwarded_off": off["forwarded"],
            "forwarded_on": row["forwarded"],
            "signaling_reduction": reduction,
            "true_extension_on": row["true_extension_supersessions"],
            "eligible_on": row["eligible_extension_opportunities"],
            "true_extension_ratio_on": row["true_extension_ratio"],
            "visibility_p95_off": off["visibility_p95_s"],
            "visibility_p95_on": row["visibility_p95_s"],
            "staleness_off": off["time_weighted_coverage_staleness_tokens"],
            "staleness_on": row["time_weighted_coverage_staleness_tokens"],
            "never_visible_on": row["never_visible"],
            "never_visible_off": off["never_visible"],
            "final_match_on": row["final_coverage_match"],
            "final_match_off": off["final_coverage_match"],
            "regressions_on": row["coverage_regressions"],
            "max_tick_queued_on": row["max_tick_queued"],
        })
    write_csv(OUT / "f6_a2_summary.csv", pairs)
    (OUT / "run_status.json").write_text(json.dumps({"status": status, "git_commit": git_commit(), "model": model}, indent=2) + "\n")


def main() -> None:
    code = asyncio.run(async_main())
    sys.exit(code)


if __name__ == "__main__":
    main()
