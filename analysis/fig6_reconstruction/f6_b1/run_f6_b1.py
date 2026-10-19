#!/usr/bin/env python3
"""F6-B1 real cache invalidation and invalidation priority.

EXPERIMENTAL_INVALIDATION_ADAPTER. Not publish_state.
The relay is a second frozen gateway_relay_4t4.py on 9730/9731.
It does not talk to b02-gateway4t4 or port 9710.

Physical loss is reset_prefix_cache on worker 0 only.
Label: CONTROLLED_REAL_CACHE_INVALIDATION. Not a natural BlockRemoved.

Replay delays are TIMED_RELAY_REPLAY. A choose() without a request is
READ_ONLY_ROUTING_DECISION. A streamed request is EXECUTED_VLLM_REQUEST.
Those labels are not interchangeable.

ACK delay and the read-only probe offset are computed from the pilot
and frozen before any formal replay. They are not the F6-A2 0.588 s value.
"""
from __future__ import annotations

import asyncio
import csv
import hashlib
import importlib.util
import json
import os
import signal
import socket
import statistics
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
from pathlib import Path

import aiohttp
import msgspec
import zmq
from transformers import AutoTokenizer
from vllm.distributed.kv_events import BlockRemoved, BlockStored, KVEventBatch

ROOT = Path("/home/byh/B02")
OUT = ROOT / "analysis/fig6_reconstruction/f6_b1"
RAW = OUT / "raw"
VLLM = ROOT / "poc/.venv/bin/vllm"
PY = ROOT / "poc/.venv/bin/python"
MODEL = "/home/byh/.cache/modelscope/qwen/Qwen2.5-1.5B-Instruct"
RELAY_PY = ROOT / "experiments/4t4/net/gateway_relay_4t4.py"
HARNESS_PY = ROOT / "experiments/4t4/run_formal4t4.py"
BLOCK = 16
PREFIX_LEN = 4096
SUFFIX_LEN = 32
PROMPT_LEN = PREFIX_LEN + SUFFIX_LEN
BG_LEN = 256
N_BACKGROUND = 8
RELAY_PORT = 9730
DISPATCH_PORT = 9731
PREFILL = 50.0
PENALTY = 2.0
GUARD = 0.5
PILOT_SEED = 2026101919
FORMAL_SEEDS = (2026101920, 2026101921, 2026101922)
EXECUTED_FORMAL_SEED = 2026101920
WALL_BUDGET_S = 45 * 60


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def sha_ids(token_ids: list[int]) -> str:
    return hashlib.sha256(",".join(map(str, token_ids)).encode()).hexdigest()


def git_commit() -> str:
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()


def port_open(port: int) -> bool:
    sock = socket.socket()
    sock.settimeout(0.3)
    try:
        return sock.connect_ex(("127.0.0.1", port)) == 0
    finally:
        sock.close()


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = list(dict.fromkeys(key for row in rows for key in row)) if rows else []
    with path.open("w", newline="") as handle:
        if not columns:
            handle.write("")
            return
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def norm_hash(value) -> int | None:
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray)):
        return int.from_bytes(value, "big")
    return int(value)


class EventTap:
    def __init__(self, worker: int) -> None:
        self.worker = worker
        self.rows: list[dict] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self.block_removed = 0
        self.cleared = 0

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        if not self._thread.is_alive():
            return
        self._stop.set()
        self._thread.join(timeout=2)

    def _run(self) -> None:
        sock = zmq.Context.instance().socket(zmq.SUB)
        sock.setsockopt(zmq.SUBSCRIBE, b"")
        sock.setsockopt(zmq.RCVTIMEO, 200)
        sock.connect(f"tcp://127.0.0.1:{5560 + self.worker}")
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
            try:
                batch = decoder.decode(parts[2])
            except msgspec.DecodeError:
                continue
            recv = time.time()
            for event in batch.events:
                hashes: list[int] = []
                parent = None
                token_ids: list[int] = []
                block_size = 0
                if isinstance(event, BlockStored):
                    kind = "BlockStored"
                    hashes = [norm_hash(item) for item in event.block_hashes]
                    parent = norm_hash(event.parent_block_hash)
                    token_ids = [int(item) for item in event.token_ids]
                    block_size = int(event.block_size)
                elif isinstance(event, BlockRemoved):
                    kind = "BlockRemoved"
                    self.block_removed += 1
                    hashes = [norm_hash(item) for item in event.block_hashes]
                else:
                    kind = "AllBlocksCleared"
                    self.cleared += 1
                self.rows.append({
                    "worker": self.worker, "recv_time": recv, "event_type": kind,
                    "parent": parent, "hashes": hashes, "n_hashes": len(hashes),
                    "token_ids": token_ids, "block_size": block_size,
                })
        sock.close(linger=0)


def start_vllm(worker: int) -> subprocess.Popen:
    log = open(RAW / f"vllm_gpu{worker}.log", "w")
    env = os.environ.copy()
    env["CUDA_VISIBLE_DEVICES"] = str(worker)
    env["VLLM_SERVER_DEV_MODE"] = "1"
    config = json.dumps({"enable_kv_cache_events": True, "publisher": "zmq", "endpoint": f"tcp://*:{5560 + worker}"})
    proc = subprocess.Popen(
        [str(VLLM), "serve", MODEL, "--host", "127.0.0.1", "--port", str(8000 + worker),
         "--gpu-memory-utilization", "0.40", "--max-model-len", "6144", "--max-num-seqs", "4",
         "--enable-prefix-caching", "--enable-prompt-tokens-details", "--swap-space", "4",
         "--block-size", "16", "--enforce-eager", "--kv-events-config", config],
        env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
    )
    proc._log = log  # type: ignore[attr-defined]
    return proc


def stop_vllm(procs: list[subprocess.Popen]) -> None:
    for proc in procs:
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
    for proc in procs:
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL)
        log = getattr(proc, "_log", None)
        if log is not None:
            log.close()


def wait_ready(procs: list[subprocess.Popen]) -> str:
    deadline = time.time() + 240
    while time.time() < deadline:
        if any(proc.poll() is not None for proc in procs):
            raise RuntimeError("a vLLM process exited during startup")
        ready, name = 0, ""
        for worker in range(4):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{8000 + worker}/v1/models", timeout=2) as handle:
                    name = json.loads(handle.read().decode())["data"][0]["id"]
                ready += 1
            except Exception:
                pass
        if ready == 4:
            return name
        time.sleep(2)
    raise TimeoutError("vLLM did not become ready")


def build_prefix(tokenizer, seed: int) -> tuple[list[int], list[int]]:
    nonce = tokenizer.encode(f"F6B1S{seed}X ", add_special_tokens=False)
    filler = tokenizer.encode(" a", add_special_tokens=False)
    suffix = tokenizer.encode(" b", add_special_tokens=False)
    if filler != [264] or suffix != [293] or not (8 <= len(nonce) < 64):
        raise RuntimeError(f"token plan nonce={len(nonce)} filler={filler} suffix={suffix}")
    prefix = list(nonce) + filler * (PREFIX_LEN - len(nonce))
    prompt = prefix + suffix * SUFFIX_LEN
    if prompt[:PREFIX_LEN] != prefix or len(prompt) != PROMPT_LEN:
        raise RuntimeError("prefix plan broke")
    return prefix, prompt


def other_tokens(tokenizer, text: str, length: int, banned: list[int]) -> list[int]:
    nonce = tokenizer.encode(text, add_special_tokens=False)
    filler = tokenizer.encode(" b", add_special_tokens=False)
    if len(nonce) >= length:
        raise RuntimeError(f"nonce too long: {text}")
    tokens = list(nonce) + filler * (length - len(nonce))
    if tokens[: min(64, len(banned))] == banned[: min(64, len(tokens))]:
        raise RuntimeError(f"{text} aliases the target prefix")
    return tokens


def chain_tokens(blocks, tip):
    if tip is None or tip not in blocks:
        return [], False
    rev, seen, cursor, missing = [], set(), tip, False
    while cursor is not None:
        if cursor in seen or cursor not in blocks:
            missing = True
            break
        seen.add(cursor)
        parent, toks = blocks[cursor]
        rev.append(toks)
        cursor = parent
    out: list[int] = []
    for part in reversed(rev):
        out.extend(part)
    return out, not missing


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
        blocks[block_hash] = (parent, token_ids[index * block_size:(index + 1) * block_size])
        parent = block_hash
        tip = block_hash
    return tip


def prom_value(text: str, name: str) -> float | None:
    exact = 0.0
    suffixed = 0.0
    saw_exact = False
    saw_suffix = False
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        key, _, raw = line.partition(" ")
        key = key.split("{", 1)[0]
        try:
            value = float(raw.split()[0])
        except (IndexError, ValueError):
            continue
        if key == name:
            exact += value
            saw_exact = True
        elif key == name + "_total":
            suffixed += value
            saw_suffix = True
    if saw_exact:
        return exact
    if saw_suffix:
        return suffixed
    return None


def fetch_metrics(worker: int) -> dict:
    with urllib.request.urlopen(f"http://127.0.0.1:{8000 + worker}/metrics", timeout=5) as response:
        text = response.read().decode("utf-8", errors="replace")
    keys = {
        "running": "vllm:num_requests_running",
        "waiting": "vllm:num_requests_waiting",
        "kv_cache_usage_perc": "vllm:kv_cache_usage_perc",
    }
    parsed = {name: prom_value(text, metric) for name, metric in keys.items()}
    if parsed["running"] is None or parsed["waiting"] is None:
        raise RuntimeError(f"missing gauges on worker {worker}")
    return parsed


async def gauges() -> list[dict]:
    return list(await asyncio.gather(*[asyncio.to_thread(fetch_metrics, worker) for worker in range(4)]))


async def wait_idle(timeout_s: float = 30) -> tuple[bool, list[dict] | None]:
    deadline = time.time() + timeout_s
    last = None
    while time.time() < deadline:
        last = await gauges()
        if all(int(item["running"]) == 0 and int(item["waiting"]) == 0 for item in last):
            return True, last
        await asyncio.sleep(0.25)
    return False, last


def cached_from_usage(usage: dict | None) -> tuple[int | None, int]:
    if not usage:
        return None, 0
    details = usage.get("prompt_tokens_details")
    if not details or "cached_tokens" not in details:
        return 0, 1
    return int(details["cached_tokens"]), 0


async def completion(session, port: int, model: str, token_ids: list[int]) -> dict:
    started = time.time()
    async with session.post(
        f"http://127.0.0.1:{port}/v1/completions",
        json={"model": model, "prompt": token_ids, "max_tokens": 1, "temperature": 0},
        timeout=aiohttp.ClientTimeout(total=180),
    ) as response:
        body = await response.json(content_type=None)
        status = response.status
    usage = body.get("usage") or {}
    cached, absent = cached_from_usage(usage)
    return {"http_status": status, "started": started, "finished": time.time(),
            "prompt_tokens": usage.get("prompt_tokens"), "cached_tokens": cached,
            "cached_field_absent": absent, "error": body.get("error")}


async def stream_ttft(session, port: int, model: str, token_ids: list[int]) -> dict:
    perf0, wall0 = time.perf_counter(), time.time()
    first = None
    usage = None
    status = None
    error = ""
    piece0 = ""
    try:
        async with session.post(
            f"http://127.0.0.1:{port}/v1/completions",
            json={"model": model, "prompt": token_ids, "max_tokens": 4, "temperature": 0,
                  "stream": True, "stream_options": {"include_usage": True}},
            timeout=aiohttp.ClientTimeout(total=180),
        ) as response:
            status = response.status
            buf = b""
            if status != 200:
                error = (await response.text())[:300]
            else:
                async for chunk in response.content.iter_any():
                    buf += chunk
                    while b"\n" in buf:
                        raw, buf = buf.split(b"\n", 1)
                        line = raw.decode("utf-8", "replace").strip()
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            continue
                        try:
                            obj = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        if obj.get("usage"):
                            usage = obj["usage"]
                        choices = obj.get("choices") or []
                        if choices and (choices[0].get("text") or "") and first is None:
                            first = time.perf_counter()
                            piece0 = choices[0]["text"][:40]
    except Exception as exc:
        error = repr(exc)[:300]
    cached, absent = cached_from_usage(usage)
    return {
        "http_status": status,
        "ttft_ms": None if first is None else (first - perf0) * 1000.0,
        "prompt_tokens": None if not usage else usage.get("prompt_tokens"),
        "cached_tokens": cached, "cached_field_absent": absent,
        "first_piece": piece0, "error": error, "start_wall": wall0,
    }


def score_window(events: list[dict], token_ids: list[int], result: dict) -> dict:
    """Chain match on block hashes. A clear before the first store is the reset."""
    started = float(result["started"])
    window = [event for event in events if event["recv_time"] >= started - 0.05]
    blocks: dict[int, tuple[int | None, list[int]]] = {}
    tip = None
    removed = cleared_after = reset_clear = stored = 0
    seen = False
    order = []
    for event in window:
        if event["event_type"] == "BlockRemoved":
            removed += 1
            order.append("remove")
        elif event["event_type"] == "AllBlocksCleared":
            if seen:
                cleared_after += 1
                order.append("clear_after")
            else:
                reset_clear += 1
                order.append("reset_clear")
        elif event["event_type"] == "BlockStored":
            seen = True
            stored += 1
            tip = absorb(blocks, event) or tip
            order.append("store")
    chain, complete = chain_tokens(blocks, tip)
    prompt_tokens = int(result["prompt_tokens"] or 0)
    cached = 0 if result["cached_tokens"] is None else int(result["cached_tokens"])
    chain_ok = complete and len(chain) >= prompt_tokens and chain[:prompt_tokens] == token_ids[:prompt_tokens]
    aligned = prompt_tokens == len(token_ids) and prompt_tokens % BLOCK == 0
    if aligned and chain_ok and removed == 0 and cleared_after == 0:
        evidence, coverage = "BLOCK_CHAIN_MATCH", prompt_tokens
    else:
        evidence, coverage = "PARTIAL", prompt_tokens if aligned else (prompt_tokens // BLOCK) * BLOCK
    return {
        "prompt_tokens": prompt_tokens, "cached_tokens": cached, "coverage_tokens": coverage,
        "evidence": evidence, "chain_matches_prompt": int(chain_ok), "block_stored": stored,
        "block_removed": removed, "cleared_after_store": cleared_after,
        "reset_clear_before_store": reset_clear, "event_order": ",".join(order),
        "http_status": result["http_status"],
        "error": json.dumps(result["error"]) if result["error"] else "",
    }


def loss_window(events: list[dict], since: float, prefix: list[int]) -> dict:
    window = [event for event in events if event["recv_time"] >= since - 0.05]
    seen = False
    clear_time = None
    x_tokens = 0
    removed = 0
    order = []
    for event in window:
        if event["event_type"] == "AllBlocksCleared" and not seen:
            clear_time = event["recv_time"]
            order.append("reset_clear")
        elif event["event_type"] == "AllBlocksCleared":
            order.append("clear_after")
        elif event["event_type"] == "BlockRemoved":
            removed += 1
            order.append("remove")
        elif event["event_type"] == "BlockStored":
            seen = True
            order.append("store")
            toks = event["token_ids"]
            take = min(len(toks), len(prefix), 64)
            if take and toks[:take] == prefix[:take]:
                x_tokens += len(toks)
    return {
        "clear_time": clear_time, "x_tokens_after": x_tokens, "removed": removed,
        "event_order": ",".join(order), "lost": int(clear_time is not None and x_tokens == 0),
    }


def tripwire(harness) -> None:
    def selected(coverages: list[int]) -> int:
        dispatcher = harness.Dispatcher(4, PREFILL, PENALTY, GUARD)
        for worker, coverage in enumerate(coverages):
            if coverage:
                dispatcher.apply_upsert(worker, "X", coverage, 0.0, 0.0)
        request = harness.TraceRequest(0, 0, 0, 0, "t", "X", PROMPT_LEN, "x", False)
        return int(dispatcher.choose(request)[0])

    stale = selected([4096, 4096, 0, 0])
    dispatcher = harness.Dispatcher(4, PREFILL, PENALTY, GUARD)
    dispatcher.apply_upsert(0, "X", 4096, 0.0, 0.0)
    dispatcher.apply_upsert(1, "X", 4096, 0.0, 0.0)
    dispatcher.apply_tombstone(0, "X")
    cleared = int(dispatcher.choose(harness.TraceRequest(0, 0, 0, 0, "t", "X", PROMPT_LEN, "x", False))[0])
    if (stale, cleared) != (0, 1):
        raise RuntimeError(f"frozen choose tripwire failed: stale={stale} cleared={cleared}")


def index_vector(dispatcher, lineage: str) -> list[int]:
    return [int(dispatcher.index[worker].get(lineage, 0)) for worker in range(4)]


def counterfactual_selected(harness, dispatcher, lineage: str) -> int:
    fresh = harness.Dispatcher(4, PREFILL, PENALTY, GUARD)
    for worker in range(4):
        for digest, coverage in dispatcher.index[worker].items():
            if worker == 0 and digest == lineage:
                continue
            fresh.apply_upsert(worker, digest, coverage, 0.0, 0.0)
    request = harness.TraceRequest(0, 0, 0, 0, "f6b1", lineage, PROMPT_LEN, "f6b1", False)
    return int(fresh.choose(request)[0])


class RelaySession:
    def __init__(self, relay_mod, harness) -> None:
        self.relay_mod = relay_mod
        self.h = harness
        self.proc = None
        self.lines: list[dict] = []
        self.server = None
        self.cell_id = -1
        self.applied: list[dict] = []
        self.dispatcher = None
        self.names: dict[int, str] = {}
        self.ack_delay = 0.0
        self.loop = None
        self.tomb_enqueued = None
        self.pending_at_tomb = None
        self.tomb_enqueue_wall = None

    def start_process(self) -> None:
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        self.proc = subprocess.Popen(
            [str(PY), str(RELAY_PY), "--listen", str(RELAY_PORT), "--downstream",
             f"127.0.0.1:{DISPATCH_PORT}", "--max-queue", "4096"],
            cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            start_new_session=True, env=env, text=True,
        )
        threading.Thread(target=self._read_stdout, daemon=True).start()

    def _read_stdout(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        with (RAW / "relay_stdout.jsonl").open("a") as sink:
            for line in self.proc.stdout:
                sink.write(line)
                sink.flush()
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                self.lines.append(obj)
                if (obj.get("event") == "update" and obj.get("stage") == "enqueued"
                        and obj.get("op") == "tombstone" and int(obj.get("cell", -1)) == self.cell_id
                        and self.loop is not None and self.tomb_enqueued is not None
                        and not self.tomb_enqueued.is_set()):
                    pending = 0
                    for item in self.lines:
                        if item.get("event") != "update" or int(item.get("cell", -1)) != self.cell_id:
                            continue
                        if item.get("op") != "upsert":
                            continue
                        uid = item.get("update_id")
                        stages = [other.get("stage") for other in self.lines
                                  if other.get("event") == "update" and other.get("update_id") == uid
                                  and int(other.get("cell", -1)) == self.cell_id]
                        if "enqueued" in stages and "forwarded" not in stages and "suppressed" not in stages:
                            pending += 1
                    self.pending_at_tomb = pending
                    self.tomb_enqueue_wall = float(obj.get("gateway_arrival_time") or time.time())
                    self.loop.call_soon_threadsafe(self.tomb_enqueued.set)

    async def start_dispatcher(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.server = await asyncio.start_server(self._on_down, "127.0.0.1", DISPATCH_PORT)

    async def _on_down(self, reader, writer) -> None:
        try:
            while True:
                data = await reader.readexactly(64)
                recv = time.time()
                kind, instance, cell, seq, coverage, digest, t_send = self.h.HDR.unpack(data[:32])
                if cell == self.cell_id and self.dispatcher is not None:
                    name = self.names.get(int(digest))
                    if name is not None and kind == self.h.K_UP:
                        self.dispatcher.apply_upsert(int(instance), name, int(coverage), float(t_send), recv)
                    elif name is not None and kind == self.h.K_TOMB:
                        self.dispatcher.apply_tombstone(int(instance), name)
                    if name is not None and kind in (self.h.K_UP, self.h.K_TOMB):
                        self.applied.append({
                            "kind": int(kind), "owner": int(instance), "lineage": name,
                            "coverage": int(coverage), "seq": int(seq), "apply_time": recv,
                        })
                if self.ack_delay > 0:
                    await asyncio.sleep(self.ack_delay)
                writer.write(self.h.HDR.pack(self.h.K_ACK, 0, cell, seq, 0, 0, time.time()) + b"\x00" * 32)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionResetError):
            pass
        finally:
            writer.close()

    async def connect(self):
        deadline = time.time() + 10
        last = None
        while time.time() < deadline:
            try:
                return await asyncio.open_connection("127.0.0.1", RELAY_PORT)
            except OSError as exc:
                last = exc
                await asyncio.sleep(0.1)
        raise RuntimeError(f"relay not ready: {last}")

    async def configure(self, cell_id: int, priority: int, ack_delay: float, names: dict[int, str]):
        self.cell_id = cell_id
        self.ack_delay = ack_delay
        self.applied = []
        self.names = names
        self.pending_at_tomb = None
        self.tomb_enqueue_wall = None
        self.tomb_enqueued = asyncio.Event()
        self.h.URLS = [f"http://127.0.0.1:{8000 + i}" for i in range(4)]
        self.dispatcher = self.h.Dispatcher(4, PREFILL, PENALTY, GUARD)
        reader, writer = await self.connect()
        payload = self.relay_mod.CFG.pack(
            self.relay_mod.MODE_FULLSYNC, 0, priority, 0, 0, 0, 4096,
            1 if ack_delay else 0, 0, 1,
        )
        writer.write(self.h.HDR.pack(self.h.K_CONFIG, 0, cell_id, 0, 0, 0, time.time()) + payload.ljust(32, b"\x00"))
        await writer.drain()
        writer.write(self.h.HDR.pack(self.h.K_RESET, 0, cell_id, 0, 0, 0, time.time()) + b"\x00" * 32)
        await writer.drain()
        blob = await asyncio.wait_for(reader.readexactly(64), timeout=10)
        if self.h.HDR.unpack(blob[:32])[0] != self.h.K_RESET_DONE:
            raise RuntimeError("reset was not acknowledged")
        await asyncio.sleep(0.3)
        return reader, writer

    async def send_wait(self, writer, kind: int, owner: int, coverage: int, lineage: str, seq: int) -> None:
        mark = len(self.lines)
        frame = self.h.HDR.pack(kind, owner, self.cell_id, seq, coverage, self.h.digest64(lineage), time.time()) + b"\x00" * 32
        writer.write(frame)
        await writer.drain()
        deadline = time.time() + 30
        while time.time() < deadline:
            hits = [line for line in self.lines[mark:]
                    if line.get("event") == "update" and int(line.get("cell", -1)) == self.cell_id
                    and int(line.get("update_id", -1)) == seq]
            if any(line.get("stage") == "forwarded" for line in hits):
                while time.time() < deadline and not any(item["seq"] == seq for item in self.applied):
                    await asyncio.sleep(0.02)
                return
            if any(line.get("stage") == "suppressed" for line in hits):
                raise RuntimeError(f"frame {seq} suppressed: {hits}")
            await asyncio.sleep(0.02)
        raise TimeoutError(f"frame {seq} was not forwarded")

    def close(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(self.proc.pid, signal.SIGKILL)


def names_for(capture: dict) -> dict[int, str]:
    harness_digest = capture["digest_fn"]
    out = {harness_digest(capture["lineage"]): capture["lineage"]}
    for row in capture["backgrounds"]:
        out[harness_digest(row["lineage"])] = row["lineage"]
    return out


async def reset_one(session, worker: int) -> tuple[int, float, float]:
    command = time.time()
    async with session.post(
        f"http://127.0.0.1:{8000 + worker}/reset_prefix_cache",
        timeout=aiohttp.ClientTimeout(total=30),
    ) as response:
        await response.read()
        status = response.status
    return status, command, time.time()


async def reset_all(session) -> None:
    async def one(worker: int) -> int:
        async with session.post(f"http://127.0.0.1:{8000 + worker}/reset_prefix_cache",
                                 timeout=aiohttp.ClientTimeout(total=30)) as response:
            await response.read()
            return response.status
    codes = await asyncio.gather(*[one(worker) for worker in range(4)])
    if any(code != 200 for code in codes):
        raise RuntimeError(f"reset failed: {codes}")
    await asyncio.sleep(0.2)


async def capture_seed(session, taps, tokenizer, model: str, seed: int) -> dict:
    await reset_all(session)
    prefix, prompt = build_prefix(tokenizer, seed)
    lineage = f"F6B1S{seed}X"
    native_rows = []

    async def warm(worker: int) -> dict:
        window = len(taps[worker].rows)
        result = await completion(session, 8000 + worker, model, prefix)
        await asyncio.sleep(0.2)
        scored = score_window(taps[worker].rows[window:], prefix, result)
        return {"worker": worker, "lineage": lineage, "generation_time": result["finished"], **scored,
                "token_sha256": sha_ids(prefix)}

    targets = list(await asyncio.gather(warm(0), warm(1)))
    backgrounds = []

    async def one_bg(worker: int, index: int) -> dict:
        tokens = other_tokens(tokenizer, f"F6B1S{seed}W{worker}B{index:02d} ", BG_LEN, prefix)
        bg_lineage = f"F6B1S{seed}W{worker}B{index:02d}"
        window = len(taps[worker].rows)
        result = await completion(session, 8000 + worker, model, tokens)
        await asyncio.sleep(0.3)
        scored = score_window(taps[worker].rows[window:], tokens, result)
        return {"worker": worker, "lineage": bg_lineage, "generation_time": result["finished"],
                "token_sha256": sha_ids(tokens), **scored}

    async def bg_chain(worker: int) -> list[dict]:
        rows = []
        for index in range(worker - 2, N_BACKGROUND, 2):
            rows.append(await one_bg(worker, index))
        return rows

    groups = await asyncio.gather(bg_chain(2), bg_chain(3))
    for group in groups:
        backgrounds.extend(group)
    backgrounds.sort(key=lambda row: row["generation_time"])
    idle_marks = [(taps[1].cleared, taps[1].block_removed)]
    status, command, done = await reset_one(session, 0)
    trigger = other_tokens(tokenizer, f"F6B1TRIG{seed} ", 64, prefix)
    window0 = len(taps[0].rows)
    trig = await completion(session, 8000, model, trigger)
    await asyncio.sleep(0.4)
    loss = loss_window(taps[0].rows[window0:], command, prefix)
    worker1_changed = taps[1].cleared != idle_marks[0][0] or taps[1].block_removed != idle_marks[0][1]
    for event in taps[0].rows[window0:] + taps[1].rows:
        if event["recv_time"] >= command - 0.05:
            native_rows.append({
                "seed": seed, "worker": event["worker"], "recv_time": event["recv_time"],
                "event_type": event["event_type"], "n_hashes": event["n_hashes"],
                "n_tokens": len(event["token_ids"]),
                "matches_x_prefix": int(bool(event["token_ids"]) and event["token_ids"][:32] == prefix[:32]),
            })
    tombstone_time = loss["clear_time"]
    print(json.dumps({
        "capture": seed, "targets": [row["evidence"] for row in targets],
        "target_cov": [row["coverage_tokens"] for row in targets],
        "lost": loss["lost"], "clear": loss["clear_time"], "x_tokens": loss["x_tokens_after"],
        "order": loss["event_order"], "worker1_changed": int(worker1_changed),
        "bg": [row["evidence"] for row in backgrounds],
    }), flush=True)
    targets_ok = all(
        row["evidence"] == "BLOCK_CHAIN_MATCH" and row["coverage_tokens"] == PREFIX_LEN and row["cached_tokens"] == 0
        for row in targets
    )
    backgrounds_ok = len(backgrounds) == N_BACKGROUND and all(
        row["evidence"] == "BLOCK_CHAIN_MATCH" and row["coverage_tokens"] == BG_LEN and row["http_status"] == 200
        for row in backgrounds
    )
    return {
        "seed": seed, "lineage": lineage, "prefix_sha256": sha_ids(prefix),
        "prompt_sha256": sha_ids(prompt), "prefix": prefix, "prompt": prompt,
        "first_prefix_ids": prefix[:8], "targets": targets, "backgrounds": backgrounds,
        "reset_http": status, "reset_command_time": command, "reset_done_time": done,
        "trigger_start": trig["started"], "trigger_cached": trig["cached_tokens"],
        "loss": loss, "worker1_changed": int(worker1_changed),
        "tombstone_time": tombstone_time, "native_rows": native_rows,
        "gate1": targets_ok and backgrounds_ok and loss["lost"] == 1 and not worker1_changed and status == 200,
    }


async def replay_cell(relay: RelaySession, capture: dict, *, cell_id: int, priority: int,
                      ack_delay: float, probe_offset: float, condition: str) -> dict:
    names = {}
    # digest function is harness.digest64, passed by binding names here
    digest = relay.h.digest64
    names[digest(capture["lineage"])] = capture["lineage"]
    for row in capture["backgrounds"]:
        names[digest(row["lineage"])] = row["lineage"]
    if len(names) != 1 + len(capture["backgrounds"]):
        raise RuntimeError("lineage digest collision in this trial")
    reader, writer = await relay.configure(cell_id, priority, ack_delay, names)
    lineage = capture["lineage"]
    try:
        seq = 0
        for row in capture["targets"]:
            await relay.send_wait(writer, relay.h.K_UP, row["worker"], int(row["coverage_tokens"]), lineage, seq)
            seq += 1
        if index_vector(relay.dispatcher, lineage)[:2] != [PREFIX_LEN, PREFIX_LEN]:
            raise RuntimeError(f"index was not established: {index_vector(relay.dispatcher, lineage)}")
        backgrounds = list(capture["backgrounds"])
        if not backgrounds or capture["tombstone_time"] is None:
            raise RuntimeError("capture has no tombstone time")
        origin = time.time()
        first = backgrounds[0]["generation_time"]
        send_at = []
        for row in backgrounds:
            when = origin + (row["generation_time"] - first)
            delay = when - time.time()
            if delay > 0:
                await asyncio.sleep(delay)
            frame = relay.h.HDR.pack(
                relay.h.K_UP, row["worker"], cell_id, seq, int(row["coverage_tokens"]),
                digest(row["lineage"]), time.time(),
            ) + b"\x00" * 32
            writer.write(frame)
            await writer.drain()
            send_at.append(time.time())
            seq += 1
        tomb_seq = seq
        tomb_when = origin + (capture["tombstone_time"] - first)
        delay = tomb_when - time.time()
        if delay > 0:
            await asyncio.sleep(delay)
        tomb_send = time.time()
        writer.write(relay.h.HDR.pack(relay.h.K_TOMB, 0, cell_id, tomb_seq, 0, digest(lineage), tomb_send) + b"\x00" * 32)
        await writer.drain()
        try:
            await asyncio.wait_for(relay.tomb_enqueued.wait(), timeout=10)
        except asyncio.TimeoutError:
            pass
        decision = None
        if relay.tomb_enqueued.is_set() and probe_offset >= 0:
            remaining = (relay.tomb_enqueue_wall or tomb_send) + probe_offset - time.time()
            if remaining > 0:
                await asyncio.sleep(remaining)
            vector = index_vector(relay.dispatcher, lineage)
            contra = counterfactual_selected(relay.h, relay.dispatcher, lineage)
            request = relay.h.TraceRequest(0, 0, 0, 0, "f6b1", lineage, PROMPT_LEN, "f6b1", False)
            loads = relay.dispatcher.loads
            minimum = min(loads)
            ties = [index for index in range(4) if loads[index] == minimum]
            native_before = ties[relay.dispatcher.rr % len(ties)]
            selected, raw, evaluated, override, reported, net = relay.dispatcher.choose(request)
            decision = {
                "selected": int(selected), "override": int(bool(override)), "raw": int(raw),
                "evaluated": int(evaluated), "reported_coverage": int(reported),
                "returned_net_ms": float(net), "counterfactual_selected": contra,
                "index_cov": vector, "native_worker": native_before,
                "probe_time": time.time(),
            }
        deadline = time.time() + max(30.0, ack_delay * (N_BACKGROUND + 3))
        while time.time() < deadline:
            if any(item["seq"] == tomb_seq and item["kind"] == relay.h.K_TOMB for item in relay.applied):
                break
            await asyncio.sleep(0.05)
        writer.write(relay.h.HDR.pack(relay.h.K_STATS_REQ, 0, cell_id, 0, 0, 0, time.time()) + b"\x00" * 32)
        await writer.drain()
        await asyncio.sleep(0.2)
    finally:
        writer.close()
    updates = [line for line in relay.lines if line.get("event") == "update" and int(line.get("cell", -1)) == cell_id]
    tomb_lines = [line for line in updates if int(line.get("update_id", -1)) == tomb_seq]
    enq = next((line for line in tomb_lines if line.get("stage") == "enqueued"), None)
    fwd = next((line for line in tomb_lines if line.get("stage") == "forwarded"), None)
    applied = next((item for item in relay.applied if item["seq"] == tomb_seq), None)
    enqueue_t = None if enq is None else float(enq.get("gateway_arrival_time"))
    forward_t = None if fwd is None else fwd.get("gateway_send_time")
    forward_t = None if forward_t is None else float(forward_t)
    apply_t = None if applied is None else float(applied["apply_time"])
    later = [line for line in updates if line.get("stage") == "forwarded" and enqueue_t is not None
             and float(line.get("gateway_send_time") or 0) >= enqueue_t - 1e-6]
    later.sort(key=lambda line: float(line.get("gateway_send_time") or 0))
    position = next((index for index, line in enumerate(later) if int(line.get("update_id", -1)) == tomb_seq), None)
    detection = None
    if capture["tombstone_time"] is not None and capture["reset_done_time"] is not None:
        detection = float(capture["tombstone_time"]) - float(capture["reset_done_time"])
    queue_wait = None if enqueue_t is None or forward_t is None else forward_t - enqueue_t
    apply_delay = None if apply_t is None else apply_t - tomb_send
    stale = None
    if apply_t is not None and detection is not None:
        stale = (apply_t - tomb_send) + detection
    index_after = index_vector(relay.dispatcher, lineage)
    print(json.dumps({
        "replay": f"{capture['seed']}:{condition}:p{priority}",
        "pending": relay.pending_at_tomb, "position": position,
        "queue_wait_s": queue_wait, "stale_s": stale,
        "selected": None if decision is None else decision["selected"],
        "contra": None if decision is None else decision["counterfactual_selected"],
        "index": index_after,
    }), flush=True)
    return {
        "seed": capture["seed"], "condition": condition, "priority": priority, "cell_id": cell_id,
        "ack_delay_s": ack_delay, "probe_offset_s": probe_offset,
        "pending_at_tomb": relay.pending_at_tomb, "dequeue_position": position,
        "tomb_send": tomb_send, "tomb_enqueue": enqueue_t, "tomb_forward": forward_t,
        "tomb_apply": apply_t, "queue_wait_s": queue_wait, "apply_delay_s": apply_delay,
        "detection_delay_s": detection, "stale_duration_s": stale,
        "index_after": index_after, "decision": decision,
        "forward_order": ",".join(f"{line.get('op')}:{line.get('owner')}:{line.get('update_id')}" for line in later),
        "w1_kept": int(index_after[1] == PREFIX_LEN),
        "w0_cleared": int(index_after[0] == 0 and apply_t is not None),
    }


async def executed_world(session, taps, tokenizer, model: str, relay: RelaySession, *,
                         seed: int, mode: str, cell_id: int) -> dict:
    """mode stale: choose before the tombstone. mode cleared: apply it first."""
    await reset_all(session)
    prefix, prompt = build_prefix(tokenizer, seed)
    lineage = f"F6B1S{seed}X"

    async def warm(worker: int) -> dict:
        window = len(taps[worker].rows)
        result = await completion(session, 8000 + worker, model, prefix)
        await asyncio.sleep(0.2)
        return score_window(taps[worker].rows[window:], prefix, result)

    scored = await asyncio.gather(warm(0), warm(1))
    names = {relay.h.digest64(lineage): lineage}
    reader, writer = await relay.configure(cell_id, 0, 0.0, names)
    try:
        for seq, worker in enumerate((0, 1)):
            await relay.send_wait(writer, relay.h.K_UP, worker, PREFIX_LEN, lineage, seq)
        status, command, done = await reset_one(session, 0)
        window0 = len(taps[0].rows)
        trigger = other_tokens(tokenizer, f"F6B1TRIG{seed}E{mode} ", 64, prefix)
        await completion(session, 8000, model, trigger)
        await asyncio.sleep(0.4)
        loss = loss_window(taps[0].rows[window0:], command, prefix)
        idle_ok, live = await wait_idle(20)
        if live:
            for worker in range(4):
                relay.dispatcher.loads[worker] = int(live[worker]["running"]) + int(live[worker]["waiting"])
        if mode == "cleared":
            await relay.send_wait(writer, relay.h.K_TOMB, 0, 0, lineage, 2)
        vector = index_vector(relay.dispatcher, lineage)
        contra = counterfactual_selected(relay.h, relay.dispatcher, lineage)
        request = relay.h.TraceRequest(0, 0, 0, 0, "f6b1", lineage, PROMPT_LEN, "f6b1", False)
        loads = list(relay.dispatcher.loads)
        minimum = min(loads)
        ties = [index for index in range(4) if loads[index] == minimum]
        native_before = ties[relay.dispatcher.rr % len(ties)]
        selected, _raw, _ev, override, reported, net = relay.dispatcher.choose(request)
        measured = await stream_ttft(session, 8000 + int(selected), model, prompt)
    finally:
        writer.close()
        await asyncio.sleep(0.1)
    row = {
        "phase": "executed", "seed": seed, "mode": mode, "label": "EXECUTED_VLLM_REQUEST",
        "lineage": lineage, "reset_http": status, "lost": loss["lost"],
        "clear_time": loss["clear_time"], "reset_done_time": done,
        "warmup_evidence": ",".join(item["evidence"] for item in scored),
        "index_before_choose": ",".join(map(str, vector)),
        "selected_worker": int(selected), "affinity_override": int(bool(override)),
        "reported_coverage": int(reported), "returned_net_ms": float(net),
        "counterfactual_selected": contra,
        "cached_tokens": measured["cached_tokens"], "prompt_tokens": measured["prompt_tokens"],
        "ttft_ms": measured["ttft_ms"], "http_status": measured["http_status"],
        "first_piece": measured["first_piece"], "error": measured["error"],
        "physical_worker0": 0, "physical_worker1": PREFIX_LEN,
        "loads": loads, "native_worker": native_before, "idle_before_choose": int(idle_ok),
        "causal_invalid": int(int(selected) == 0 and contra != 0 and loss["lost"] == 1 and vector[0] >= PREFIX_LEN),
        "valid_loss": int(loss["lost"] == 1 and all(item["evidence"] == "BLOCK_CHAIN_MATCH" for item in scored)),
    }
    print(json.dumps({"executed": f"{seed}:{mode}", "selected": row["selected_worker"],
                      "cached": row["cached_tokens"], "causal": row["causal_invalid"],
                      "lost": row["lost"]}), flush=True)
    return row


def decision_row(cell: dict) -> dict:
    decision = cell["decision"] or {}
    vector = decision.get("index_cov") or [None, None, None, None]
    selected = decision.get("selected")
    contra = decision.get("counterfactual_selected")
    advertised = None if selected is None or not vector else vector[selected]
    physical = 0 if selected == 0 else PREFIX_LEN if selected == 1 else 0
    stale_positive = int(selected == 0 and vector and vector[0] and vector[0] > 0)
    causal = int(selected == 0 and contra not in (None, 0) and stale_positive == 1)
    delay = cell["stale_duration_s"]
    return {
        "phase": "replay", "seed": cell["seed"], "condition": cell["condition"],
        "priority": cell["priority"], "label": "READ_ONLY_ROUTING_DECISION",
        "selected_worker": selected, "counterfactual_selected": contra,
        "affinity_override": decision.get("override"),
        "advertised_coverage": advertised, "physical_coverage": physical,
        "stale_positive": stale_positive, "causal_invalid": causal,
        "executed": 0, "cached_tokens": "",
        "probe_offset_s": cell["probe_offset_s"],
        "tomb_applied_before_probe": int(cell["tomb_apply"] is not None and decision.get("probe_time") is not None and cell["tomb_apply"] <= decision["probe_time"]),
        "stale_duration_s": delay if delay is not None else "",
        "apply_delay_s": cell["apply_delay_s"] if cell["apply_delay_s"] is not None else "",
        "queue_wait_s": cell["queue_wait_s"] if cell["queue_wait_s"] is not None else "",
        "pending_at_tomb": cell["pending_at_tomb"] if cell["pending_at_tomb"] is not None else "",
        "dequeue_position": cell["dequeue_position"] if cell["dequeue_position"] is not None else "",
        "delay_status": "MEASURED" if delay is not None else "NO_SAMPLE",
        "probe_is_frozen": int(cell.get("probe_is_frozen", 0)),
        "physical_source": "OBSERVED_AT_CAPTURE",
    }


def summarize(cells: list[dict], decisions: list[dict]) -> list[dict]:
    rows = []
    groups: dict[tuple, list] = {}
    for cell in cells:
        if not cell.get("probe_is_frozen"):
            continue
        if cell["seed"] == PILOT_SEED:
            phase = "pilot"
        else:
            phase = "formal"
        groups.setdefault((phase, cell["condition"], cell["priority"]), []).append(cell)
    for (phase, condition, priority), items in sorted(groups.items()):
        def vals(key):
            return [float(item[key]) for item in items if item[key] is not None]
        stale, waits, applies = vals("stale_duration_s"), vals("queue_wait_s"), vals("apply_delay_s")
        matched = [row for row in decisions if row["phase"] == "replay" and row["condition"] == condition
                   and int(row["priority"]) == int(priority) and int(row.get("probe_is_frozen") or 0) == 1
                   and ((phase == "pilot" and int(row["seed"]) == PILOT_SEED) or (phase == "formal" and int(row["seed"]) != PILOT_SEED))]
        causal = [int(row["causal_invalid"]) for row in matched]
        rows.append({
            "phase": phase, "condition": condition, "priority": priority,
            "cells": len(items),
            "mean_stale_duration_s": None if not stale else sum(stale) / len(stale),
            "mean_queue_wait_s": None if not waits else sum(waits) / len(waits),
            "mean_apply_delay_s": None if not applies else sum(applies) / len(applies),
            "sd_stale_duration_s": None if len(stale) < 2 else statistics.stdev(stale),
            "min_stale_duration_s": None if not stale else min(stale),
            "max_stale_duration_s": None if not stale else max(stale),
            "mean_pending": None if not any(item["pending_at_tomb"] is not None for item in items)
            else sum(int(item["pending_at_tomb"]) for item in items if item["pending_at_tomb"] is not None)
            / sum(1 for item in items if item["pending_at_tomb"] is not None),
            "decision_cells": len(matched),
            "decision_causal_invalid_count": sum(causal),
            "decision_causal_invalid_rate": None if not causal else sum(causal) / len(causal),
            "no_sample": sum(1 for item in items if item["stale_duration_s"] is None),
        })
    return rows


def pilot_gates(capture: dict, cells: list[dict], executed: list[dict]) -> dict:
    controlled = [cell for cell in cells if cell["seed"] == PILOT_SEED and cell["condition"] == "controlled"]
    fifo = next((cell for cell in controlled if cell["priority"] == 0), None)
    pri = next((cell for cell in controlled if cell["priority"] == 1), None)
    gate1 = bool(capture and capture["gate1"])
    gate2 = bool(capture and capture["tombstone_time"] is not None and capture["loss"]["lost"] == 1)
    fifo_ahead = None if not fifo else fifo["dequeue_position"]
    pri_ahead = None if not pri else pri["dequeue_position"]
    gate3 = bool(fifo and fifo_ahead is not None and fifo_ahead >= 2)
    gate4 = bool(
        fifo and pri and fifo["apply_delay_s"] is not None and pri["apply_delay_s"] is not None
        and pri["apply_delay_s"] + 0.05 < fifo["apply_delay_s"]
        and pri_ahead is not None and fifo_ahead is not None and pri_ahead < fifo_ahead
    )
    gate5 = bool(fifo and pri and fifo["w0_cleared"] == 1 and pri["w0_cleared"] == 1 and fifo["w1_kept"] == 1 and pri["w1_kept"] == 1)
    fifo_dec = (fifo or {}).get("decision") or {}
    pri_dec = (pri or {}).get("decision") or {}
    gate6 = bool(fifo_dec and pri_dec and fifo_dec.get("selected") == 0 and pri_dec.get("selected") == 1
                 and fifo_dec.get("counterfactual_selected") == 1)
    stale_exec = next((row for row in executed if row["seed"] == PILOT_SEED and row["mode"] == "stale"), None)
    gate7 = bool(stale_exec and stale_exec["selected_worker"] == 0 and stale_exec["cached_tokens"] == 0
                 and stale_exec["prompt_tokens"] == PROMPT_LEN and stale_exec["valid_loss"] == 1)
    return {
        "gate1_residency": gate1, "gate2_tombstone": gate2, "gate3_backlog": gate3,
        "gate4_priority_order": gate4, "gate5_apply": gate5, "gate6_routing": gate6,
        "gate7_executed": gate7,
        "stop_formal": not (gate1 and gate2 and gate3 and gate4 and gate5 and gate6),
    }


async def async_main() -> int:
    RAW.mkdir(parents=True, exist_ok=True)
    started = time.time()
    commit = git_commit()
    busy = [port for port in (8000, 8001, 8002, 8003, 5560, 5561, 5562, 5563, RELAY_PORT, DISPATCH_PORT) if port_open(port)]
    if busy:
        raise RuntimeError(f"required ports already open: {busy}")
    harness = load_module("f6b1_harness", HARNESS_PY)
    relay_mod = load_module("f6b1_relay", RELAY_PY)
    tripwire(harness)
    tokenizer = AutoTokenizer.from_pretrained(MODEL, trust_remote_code=False)
    for seed in (PILOT_SEED, *FORMAL_SEEDS):
        prefix, _prompt = build_prefix(tokenizer, seed)
        other_tokens(tokenizer, f"F6B1TRIG{seed} ", 64, prefix)
        other_tokens(tokenizer, f"F6B1S{seed}W2B00 ", BG_LEN, prefix)
    procs: list[subprocess.Popen] = []
    taps = [EventTap(worker) for worker in range(4)]
    relay = RelaySession(relay_mod, harness)
    captures = {}
    cells: list[dict] = []
    executed: list[dict] = []
    native: list[dict] = []
    status = "ERROR"
    gates: dict = {}
    rule: dict = {}
    cell_id = 300
    try:
        for worker in range(4):
            procs.append(start_vllm(worker))
        model = await asyncio.to_thread(wait_ready, procs)
        for tap in taps:
            tap.start()
        await asyncio.sleep(0.4)
        relay.start_process()
        await relay.start_dispatcher()
        await asyncio.sleep(0.4)
        async with aiohttp.ClientSession() as session:
            pilot = await capture_seed(session, taps, tokenizer, model, PILOT_SEED)
            captures[PILOT_SEED] = pilot
            native.extend(pilot["native_rows"])
            if not pilot["gate1"] or pilot["tombstone_time"] is None:
                status = "GATE_STOP"
                gates = pilot_gates(pilot, [], [])
            else:
                span = float(pilot["tombstone_time"]) - float(pilot["backgrounds"][0]["generation_time"])
                ack_delay = span / max(1, N_BACKGROUND - 4)
                rule = {"ack_delay_s": ack_delay, "span_s": span, "n_background": N_BACKGROUND,
                        "formula": "span/(N-4)", "fixed_before_formal": False, "adjustments": 0}
                (OUT / "capacity_rule.json").write_text(json.dumps(rule, indent=2) + "\n")

                async def run_pair(seed: int, delay: float, offset: float, phase_condition_only=None):
                    nonlocal cell_id
                    capture = captures[seed]
                    out = []
                    plan = (("natural", 0.0), ("controlled", delay))
                    for condition, this_delay in plan:
                        if phase_condition_only and condition != phase_condition_only:
                            continue
                        for priority in (0, 1):
                            out.append(await replay_cell(
                                relay, capture, cell_id=cell_id, priority=priority,
                                ack_delay=this_delay, probe_offset=offset, condition=condition,
                            ))
                            cell_id += 1
                    return out

                # The timing pair measures queue position and apply delay only.
                # Its probe offset is temporary. The official pilot pair, and every
                # formal cell, use the offset frozen from this timing pair.
                timing = await run_pair(PILOT_SEED, ack_delay, ack_delay, "controlled")
                cells.extend(timing)
                gates = pilot_gates(pilot, timing, [])
                if not gates["gate3_backlog"] or not gates["gate4_priority_order"]:
                    ack_delay *= 2
                    rule["adjustments"] = 1
                    rule["ack_delay_s"] = ack_delay
                    (OUT / "capacity_rule.json").write_text(json.dumps(rule, indent=2) + "\n")
                    timing = await run_pair(PILOT_SEED, ack_delay, ack_delay, "controlled")
                    cells.extend(timing)
                    gates = pilot_gates(pilot, timing, [])
                fifo = next(item for item in timing if item["priority"] == 0)
                pri = next(item for item in timing if item["priority"] == 1)
                if fifo["apply_delay_s"] is None or pri["apply_delay_s"] is None:
                    probe_offset = ack_delay
                else:
                    probe_offset = 0.5 * (float(fifo["apply_delay_s"]) + float(pri["apply_delay_s"]))
                rule["probe_offset_s"] = probe_offset
                rule["pilot_fifo_apply_delay_s"] = fifo["apply_delay_s"]
                rule["pilot_priority_apply_delay_s"] = pri["apply_delay_s"]
                rule["pilot_fifo_dequeue_position"] = fifo["dequeue_position"]
                rule["pilot_priority_dequeue_position"] = pri["dequeue_position"]
                rule["fixed_before_formal"] = True
                (OUT / "capacity_rule.json").write_text(json.dumps(rule, indent=2) + "\n")
                official = await run_pair(PILOT_SEED, ack_delay, probe_offset)
                for item in official:
                    item["probe_is_frozen"] = 1
                cells.extend(official)
                for mode in ("stale", "cleared"):
                    executed.append(await executed_world(
                        session, taps, tokenizer, model, relay, seed=PILOT_SEED, mode=mode, cell_id=cell_id,
                    ))
                    cell_id += 1
                gates = pilot_gates(pilot, official, executed)
                (OUT / "feasibility_gates.json").write_text(json.dumps(gates, indent=2) + "\n")
                if gates["stop_formal"]:
                    status = "GATE_STOP"
                else:
                    status = "RAN"
                    for seed in FORMAL_SEEDS:
                        if time.time() - started > WALL_BUDGET_S:
                            status = "BUDGET_STOP"
                            break
                        captured = await capture_seed(session, taps, tokenizer, model, seed)
                        captures[seed] = captured
                        native.extend(captured["native_rows"])
                        if not captured["gate1"]:
                            status = "FORMAL_CAPTURE_FAIL"
                            break
                        formal_cells = await run_pair(seed, ack_delay, probe_offset)
                        for item in formal_cells:
                            item["probe_is_frozen"] = 1
                        cells.extend(formal_cells)
                        if seed == EXECUTED_FORMAL_SEED:
                            for mode in ("stale", "cleared"):
                                executed.append(await executed_world(
                                    session, taps, tokenizer, model, relay,
                                    seed=seed, mode=mode, cell_id=cell_id,
                                ))
                                cell_id += 1
        return 0
    except Exception:
        text = traceback.format_exc()
        for worker in range(4):
            path = RAW / f"vllm_gpu{worker}.log"
            if path.exists():
                text += f"\n\n--- vllm {worker} ---\n" + path.read_text(errors="replace")[-1200:]
        (RAW / "driver_error.txt").write_text(text)
        print(text, flush=True)
        status = "ERROR"
        return 1
    finally:
        ident = []
        for seed, cap in captures.items():
            ident.append({
                "seed": seed, "lineage": cap.get("lineage"),
                "prefix_sha256": cap.get("prefix_sha256"), "prompt_sha256": cap.get("prompt_sha256"),
                "first_prefix_ids": cap.get("first_prefix_ids"),
                "prefix_len": len(cap.get("prefix") or []),
                "prefix": cap.get("prefix"), "prompt": cap.get("prompt"),
                "targets": cap.get("targets"), "backgrounds": [
                    {key: row.get(key) for key in (
                        "worker", "lineage", "evidence", "coverage_tokens", "cached_tokens",
                        "generation_time", "token_sha256", "event_order", "http_status",
                    )} for row in cap.get("backgrounds") or []
                ],
                "reset_command_time": cap.get("reset_command_time"),
                "reset_done_time": cap.get("reset_done_time"),
                "trigger_start": cap.get("trigger_start"),
                "tombstone_time": cap.get("tombstone_time"),
                "loss": cap.get("loss"), "gate1": cap.get("gate1"),
                "worker1_changed": cap.get("worker1_changed"),
            })
        (RAW / "captures.json").write_text(json.dumps(ident, indent=2) + "\n")
        decisions = [decision_row(cell) for cell in cells if cell.get("decision")]
        for row in executed:
            decisions.append({
                "phase": "executed", "seed": row["seed"], "condition": row["mode"],
                "priority": "", "label": "EXECUTED_VLLM_REQUEST",
                "selected_worker": row["selected_worker"],
                "counterfactual_selected": row["counterfactual_selected"],
                "affinity_override": row["affinity_override"],
                "advertised_coverage": row["reported_coverage"],
                "physical_coverage": 0 if row["selected_worker"] == 0 else PREFIX_LEN,
                "stale_positive": int(row["mode"] == "stale" and row["selected_worker"] == 0),
                "causal_invalid": row["causal_invalid"], "executed": 1,
                "cached_tokens": row["cached_tokens"], "probe_offset_s": "",
                "tomb_applied_before_probe": int(row["mode"] == "cleared"),
                "stale_duration_s": "", "apply_delay_s": "", "queue_wait_s": "",
                "pending_at_tomb": "", "dequeue_position": "",
                "delay_status": "NOT_A_DELAY_CELL", "ttft_ms": row["ttft_ms"],
                "http_status": row["http_status"], "lost": row["lost"],
            })
        tomb_rows = []
        timelines = []
        for cell in cells:
            tomb_rows.append({
                "seed": cell["seed"], "condition": cell["condition"], "priority": cell["priority"],
                "ack_delay_s": cell["ack_delay_s"], "probe_offset_s": cell["probe_offset_s"],
                "probe_is_frozen": int(cell.get("probe_is_frozen", 0)),
                "pending_at_tomb": cell["pending_at_tomb"], "dequeue_position": cell["dequeue_position"],
                "tomb_send": cell["tomb_send"], "tomb_enqueue": cell["tomb_enqueue"],
                "tomb_forward": cell["tomb_forward"], "tomb_apply": cell["tomb_apply"],
                "queue_wait_s": cell["queue_wait_s"], "apply_delay_s": cell["apply_delay_s"],
                "detection_delay_s": cell["detection_delay_s"], "stale_duration_s": cell["stale_duration_s"],
                "w0_cleared": cell["w0_cleared"], "w1_kept": cell["w1_kept"],
                "forward_order": cell["forward_order"],
            })
            cap = captures.get(cell["seed"], {})
            timelines.append({
                "seed": cell["seed"], "condition": cell["condition"], "priority": cell["priority"],
                "reset_command_time": cap.get("reset_command_time"),
                "reset_done_time": cap.get("reset_done_time"),
                "native_clear_time": cap.get("tombstone_time"),
                "tombstone_generated": cap.get("tombstone_time"),
                "tomb_enqueue": cell["tomb_enqueue"], "tomb_forward": cell["tomb_forward"],
                "tomb_apply": cell["tomb_apply"],
                "selected": None if not cell.get("decision") else cell["decision"].get("selected"),
                "label_physical": "CONTROLLED_REAL_CACHE_INVALIDATION",
                "label_replay": "TIMED_RELAY_REPLAY",
                "label_decision": "READ_ONLY_ROUTING_DECISION",
            })
        per_cell = []
        for cell in cells:
            decision = cell.get("decision") or {}
            per_cell.append({
                "seed": cell["seed"], "condition": cell["condition"], "priority": cell["priority"],
                "ack_delay_s": cell["ack_delay_s"], "probe_offset_s": cell["probe_offset_s"],
                "probe_is_frozen": int(cell.get("probe_is_frozen", 0)),
                "pending_at_tomb": cell["pending_at_tomb"], "dequeue_position": cell["dequeue_position"],
                "queue_wait_s": cell["queue_wait_s"], "apply_delay_s": cell["apply_delay_s"],
                "detection_delay_s": cell["detection_delay_s"], "stale_duration_s": cell["stale_duration_s"],
                "delay_status": "MEASURED" if cell["stale_duration_s"] is not None else "NO_SAMPLE",
                "selected_worker": decision.get("selected"),
                "counterfactual_selected": decision.get("counterfactual_selected"),
                "w0_cleared": cell["w0_cleared"], "w1_kept": cell["w1_kept"],
                "index_after": ",".join(map(str, cell["index_after"])),
            })
        write_csv(OUT / "f6_b1_native_events.csv", native)
        write_csv(OUT / "f6_b1_tombstone_events.csv", tomb_rows)
        write_csv(OUT / "f6_b1_gateway_timeline.csv", timelines)
        write_csv(OUT / "f6_b1_routing_attempts.csv", decisions)
        write_csv(OUT / "f6_b1_per_cell.csv", per_cell)
        write_csv(OUT / "f6_b1_summary.csv", summarize(cells, decisions))
        (OUT / "feasibility_gates.json").write_text(json.dumps(gates, indent=2) + "\n")
        payload = {
            "status": status, "git_commit": commit, "gates": gates, "rule": rule,
            "elapsed_s": time.time() - started, "cells": len(cells), "executed": len(executed),
            "production_gateway": "not touched; 127.0.0.1:9710",
            "adapter": "EXPERIMENTAL_INVALIDATION_ADAPTER",
            "invalidation": "CONTROLLED_REAL_CACHE_INVALIDATION",
        }
        (OUT / "run_status.json").write_text(json.dumps(payload, indent=2) + "\n")
        (RAW / "executed.json").write_text(json.dumps(executed, indent=2, default=str) + "\n")
        for tap in taps:
            tap.stop()
        relay.close()
        stop_vllm(procs)
        print(json.dumps(payload, default=str), flush=True)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(async_main()))
