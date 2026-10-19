#!/usr/bin/env python3
"""F6-D1 measured routing regret.

EXPERIMENTAL_WRAPPER. Advertisements are built from observed vLLM warmup
coverage. This is not publish_state and it is not deployed.

The relay is a second process of the frozen gateway_relay_4t4.py on
127.0.0.1:9720. It does not connect to b02-gateway4t4 or port 9710.

Dispatcher.choose, apply_upsert, and Relay._admit_replica are not modified.
The request is sent to the worker choose() returns.

Client TTFT is the time from the streamed POST until the first non-empty
completion text. Empty SSE events and the usage chunk are not a token.
A missing first token is left empty. It is not coded as 0.

Model cost, fixed before the run:
    T = load * 2 ms + max(0, prompt_tokens - warmup_coverage) / 50
Regret = T(Krep=2 selected) - T(Full selected). Label MODEL_DERIVED.
It is not a TTFT.
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
OUT = ROOT / "analysis/fig6_reconstruction/f6_d1"
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
RELAY_PORT = 9720
DISPATCH_PORT = 9721
# Frozen with the protocol, before any TTFT. Not retuned after results.
PREFILL_TOKENS_PER_MS = 50.0
QUEUE_PENALTY_MS = 2.0
GUARD_MS = 0.5
J = 4
PILOT = {"seed": 2026101909, "assignment": "A", "order": ("full", "cap"), "scenario": "D1"}
FORMAL = (
    {"seed": 2026101910, "assignment": "A", "order": ("full", "cap")},
    {"seed": 2026101911, "assignment": "B", "order": ("cap", "full")},
    {"seed": 2026101912, "assignment": "A", "order": ("cap", "full")},
    {"seed": 2026101913, "assignment": "B", "order": ("full", "cap")},
)
SCENARIOS = ("D0", "D1", "D2")
WALL_BUDGET_S = 40 * 60
D3_BUDGET_S = 25 * 60


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


def write_csv(path: Path, rows: list[dict], fields: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows and not fields:
        path.write_text("")
        return
    columns = fields or list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in columns})


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
        "prefix_cache_hits": "vllm:prefix_cache_hits",
    }
    parsed = {name: prom_value(text, metric) for name, metric in keys.items()}
    if parsed["running"] is None or parsed["waiting"] is None:
        raise RuntimeError(f"missing gauges on worker {worker}")
    return parsed


def norm_hash(value) -> int | None:
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray)):
        return int.from_bytes(value, "big")
    return int(value)


class EventTap:
    def __init__(self, worker: int, port: int) -> None:
        self.worker = worker
        self.port = port
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
                    "recv_time": recv,
                    "event_type": kind,
                    "parent": parent,
                    "hashes": hashes,
                    "token_ids": token_ids,
                    "block_size": block_size,
                })
        sock.close(linger=0)


def start_vllm(worker: int) -> subprocess.Popen:
    log = open(RAW / f"vllm_gpu{worker}.log", "w")
    env = os.environ.copy()
    env["CUDA_VISIBLE_DEVICES"] = str(worker)
    env["VLLM_SERVER_DEV_MODE"] = "1"
    config = json.dumps({
        "enable_kv_cache_events": True,
        "publisher": "zmq",
        "endpoint": f"tcp://*:{5560 + worker}",
    })
    proc = subprocess.Popen(
        [
            str(VLLM), "serve", MODEL,
            "--host", "127.0.0.1", "--port", str(8000 + worker),
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
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
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
                with urllib.request.urlopen(f"http://127.0.0.1:{8000 + worker}/v1/models", timeout=2) as handle:
                    body = json.loads(handle.read().decode())
                ready += 1
                model_name = body["data"][0]["id"]
            except Exception:
                pass
        if ready == 4:
            return model_name
        time.sleep(2)
    raise TimeoutError("vLLM did not become ready")


def build_tokens(tokenizer, seed: int, scenario: str) -> tuple[list[int], list[int]]:
    nonce = tokenizer.encode(f"F6D1S{seed}{scenario} ", add_special_tokens=False)
    filler = tokenizer.encode(" a", add_special_tokens=False)
    suffix = tokenizer.encode(" b", add_special_tokens=False)
    if filler != [264] or suffix != [293]:
        raise RuntimeError(f"unexpected filler ids filler={filler} suffix={suffix}")
    if len(nonce) < 8 or len(nonce) >= 64:
        raise RuntimeError(f"nonce length {len(nonce)} is outside the planned range")
    prefix = list(nonce) + filler * (PREFIX_LEN - len(nonce))
    prompt = prefix + suffix * SUFFIX_LEN
    if len(prefix) != PREFIX_LEN or len(prompt) != PROMPT_LEN:
        raise RuntimeError("token plan length mismatch")
    if any(len(prompt[:n]) % BLOCK for n in (1024, 4096, PROMPT_LEN)):
        raise RuntimeError("planned lengths are not block aligned")
    if prompt[:1024] != prefix[:1024] or prompt[:4096] != prefix:
        raise RuntimeError("suffix broke the token prefix")
    return prefix, prompt


def holder_tokens(tokenizer, seed: int, worker: int, prompt: list[int]) -> list[int]:
    nonce = tokenizer.encode(f"F6D1HOLD{seed}W{worker} ", add_special_tokens=False)
    suffix = tokenizer.encode(" b", add_special_tokens=False)
    if len(nonce) >= 64:
        raise RuntimeError("holder nonce is too long")
    tokens = list(nonce) + suffix * (64 - len(nonce))
    if tokens[: min(len(tokens), len(prompt))] == prompt[: min(len(tokens), len(prompt))]:
        raise RuntimeError("holder tokens are a prefix of the measured prompt")
    return tokens


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
        blocks[block_hash] = (parent, token_ids[index * block_size:(index + 1) * block_size])
        parent = block_hash
        tip = block_hash
    return tip


def roles(assignment: str, scenario: str) -> tuple[list[int], list[int], dict[int, int], list[int]]:
    if assignment == "A":
        visible, hidden = [0, 1], [2, 3]
    elif assignment == "B":
        visible, hidden = [2, 3], [0, 1]
    else:
        raise RuntimeError(assignment)
    coverage = {worker: 0 for worker in range(4)}
    if scenario == "D0":
        for worker in range(4):
            coverage[worker] = 4096
        loads = [0, 0, 0, 0]
    elif scenario in ("D1", "D3"):
        for worker in visible:
            coverage[worker] = 1024
        for worker in hidden:
            coverage[worker] = 4096
        loads = [1 if worker in visible else 0 for worker in range(4)] if scenario == "D3" else [0, 0, 0, 0]
    elif scenario == "D2":
        for worker in visible:
            coverage[worker] = 4096
        for worker in hidden:
            coverage[worker] = 1024
        loads = [0, 0, 0, 0]
    else:
        raise RuntimeError(scenario)
    order = list(visible) + list(hidden)
    return visible, hidden, coverage, loads if scenario == "D3" else [0, 0, 0, 0]


def mirror_choose(index_cov: list[int], loads: list[int], rr: int) -> dict:
    """Read-only copy of Dispatcher.choose. The live call is authoritative."""
    def least(candidates: list[int], cursor: int) -> tuple[int, int]:
        minimum = min(loads[index] for index in candidates)
        ties = [index for index in candidates if loads[index] == minimum]
        return ties[cursor % len(ties)], cursor + 1

    native, cursor = least(list(range(4)), rr)
    native_cov = index_cov[native]
    candidates = [(index, index_cov[index]) for index in range(4) if index_cov[index] > 0]
    candidates.sort(key=lambda item: (-item[1], loads[item[0]], item[0]))
    evaluated = candidates[:J]
    pre_guard = None
    if evaluated:
        best_cov = evaluated[0][1]
        best = [index for index, coverage in evaluated if coverage == best_cov]
        target, _cursor = least(best, cursor)
        incremental = max(0, best_cov - native_cov)
        pre_guard = incremental / PREFILL_TOKENS_PER_MS
        pre_guard -= max(0, loads[target] - loads[native]) * QUEUE_PENALTY_MS
        if pre_guard > GUARD_MS:
            return {
                "native": native, "selected": target, "override": 1,
                "reported_coverage": best_cov, "returned_net_ms": pre_guard,
                "pre_guard_net_ms": pre_guard, "raw": len(candidates), "evaluated": len(evaluated),
            }
    return {
        "native": native, "selected": native, "override": 0,
        "reported_coverage": native_cov, "returned_net_ms": 0.0,
        "pre_guard_net_ms": pre_guard, "raw": len(candidates), "evaluated": len(evaluated),
    }


def tripwire(harness) -> None:
    def selected(coverages: list[int]) -> int:
        dispatcher = harness.Dispatcher(J, PREFILL_TOKENS_PER_MS, QUEUE_PENALTY_MS, GUARD_MS)
        for worker, coverage in enumerate(coverages):
            if coverage:
                dispatcher.apply_upsert(worker, "L", coverage, 0.0, 0.0)
        request = harness.TraceRequest(0, 0, 0, 0, "t", "L", PROMPT_LEN, "x", False)
        return int(dispatcher.choose(request)[0])

    full = selected([1024, 1024, 4096, 4096])
    cap = selected([1024, 1024, 0, 0])
    if (full, cap) != (3, 0):
        raise RuntimeError(f"frozen choose tripwire failed: full={full} cap={cap}")


def model_cost(load: int, warmup_coverage: int, prompt_tokens: int) -> float:
    return load * QUEUE_PENALTY_MS + max(0, prompt_tokens - warmup_coverage) / PREFILL_TOKENS_PER_MS


class RelaySession:
    def __init__(self, relay_mod, harness) -> None:
        self.relay_mod = relay_mod
        self.h = harness
        self.proc: subprocess.Popen | None = None
        self.lines: list[dict] = []
        self.server = None
        self.cell_id = -1
        self.applied: list[dict] = []
        self.dispatcher = None
        self.digest_name = ""

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
        with (RAW / "relay_stdout.jsonl").open("w") as sink:
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
                    if int(digest) == self.h.digest64(self.digest_name):
                        self.dispatcher.apply_upsert(instance, self.digest_name, int(coverage), float(t_send), recv)
                    self.applied.append({
                        "owner": int(instance),
                        "coverage": int(coverage),
                        "seq": int(seq),
                        "recv": recv,
                    })
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
        raise RuntimeError(f"relay agent port not ready: {last}")

    async def advertise(self, *, cell_id: int, dedup: int, lineage: str, order: list[int],
                        coverages: dict[int, int], loads: list[int]) -> dict:
        self.cell_id = cell_id
        self.applied = []
        self.digest_name = lineage
        self.h.URLS = [f"http://127.0.0.1:{8000 + i}" for i in range(4)]
        self.dispatcher = self.h.Dispatcher(J, PREFILL_TOKENS_PER_MS, QUEUE_PENALTY_MS, GUARD_MS)
        self.dispatcher.loads = list(loads)
        reader, writer = await self.connect()
        mark = len(self.lines)
        try:
            payload = self.relay_mod.CFG.pack(
                self.relay_mod.MODE_FULLSYNC, 0, 0, 0, dedup, 0, 4096, 0, 0, 1,
            )
            writer.write(self.h.HDR.pack(self.h.K_CONFIG, 0, cell_id, 0, 0, 0, time.time()) + payload.ljust(32, b"\x00"))
            await writer.drain()
            writer.write(self.h.HDR.pack(self.h.K_RESET, 0, cell_id, 0, 0, 0, time.time()) + b"\x00" * 32)
            await writer.drain()
            blob = await asyncio.wait_for(reader.readexactly(64), timeout=10)
            kind = self.h.HDR.unpack(blob[:32])[0]
            if kind != self.h.K_RESET_DONE:
                raise RuntimeError(f"reset ack kind {kind}")
            await asyncio.sleep(0.3)
            events = []
            for seq, owner in enumerate(order):
                frame = self.h.HDR.pack(
                    self.h.K_UP, owner, cell_id, seq, int(coverages[owner]),
                    self.h.digest64(lineage), time.time(),
                ) + b"\x00" * 32
                writer.write(frame)
                await writer.drain()
                hit = await self._wait_terminal(cell_id, seq, mark)
                events.extend(hit)
            expected_applied = sum(1 for line in events if line.get("stage") == "forwarded")
            deadline = time.time() + 5
            while time.time() < deadline and len(self.applied) < expected_applied:
                await asyncio.sleep(0.02)
            if len(self.applied) < expected_applied:
                raise TimeoutError(f"dispatcher applied {len(self.applied)} of {expected_applied}")
            writer.write(self.h.HDR.pack(self.h.K_STATS_REQ, 0, cell_id, 0, 0, 0, time.time()) + b"\x00" * 32)
            await writer.drain()
            stats = {}
            for _ in range(2):
                blob = await asyncio.wait_for(reader.readexactly(64), timeout=10)
                skind, _i, _c, seq, coverage, _d, _t = self.h.HDR.unpack(blob[:32])
                if skind == self.h.K_STATS:
                    forwarded, rate, superseded, cap, util, qdrop, expired, maxq = self.h.STATS.unpack(blob[32:64])
                    stats = {
                        "forwarded": int(forwarded), "replica_cap": int(cap),
                        "superseded": int(superseded), "received": int(coverage),
                        "rate_limit": int(rate), "low_utility": int(util),
                        "queue_drop": int(qdrop), "expired": int(expired), "maxq": int(maxq),
                    }
        finally:
            writer.close()
            await asyncio.sleep(0.05)
        forwarded_owners = sorted({int(item["owner"]) for item in self.applied})
        suppressed = sorted({
            int(line["owner"]) for line in events
            if line.get("stage") == "suppressed"
        })
        index_cov = [int(self.dispatcher.index[i].get(lineage, 0)) for i in range(4)]
        return {
            "events": events,
            "stats": stats,
            "forwarded_owners": forwarded_owners,
            "suppressed_owners": suppressed,
            "index_cov": index_cov,
            "dispatcher": self.dispatcher,
            "applied": list(self.applied),
        }

    async def _wait_terminal(self, cell: int, seq: int, mark: int) -> list[dict]:
        deadline = time.time() + 15
        while time.time() < deadline:
            hits = [
                line for line in self.lines[mark:]
                if line.get("event") == "update" and int(line.get("cell", -1)) == cell
                and int(line.get("update_id", -1)) == seq
            ]
            stages = {line.get("stage") for line in hits}
            if "forwarded" in stages or "suppressed" in stages:
                return hits
            await asyncio.sleep(0.02)
        raise TimeoutError(f"update {seq} on cell {cell} did not finish")

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


def cached_from_usage(usage: dict | None) -> tuple[int | None, int]:
    """vLLM omits prompt_tokens_details when cached tokens are 0."""
    if not usage:
        return None, 0
    if "prompt_tokens_details" not in usage or usage.get("prompt_tokens_details") is None:
        return 0, 1
    details = usage["prompt_tokens_details"] or {}
    if "cached_tokens" not in details:
        return 0, 1
    return int(details["cached_tokens"]), 0


async def completion(session, port: int, model: str, token_ids: list[int], max_tokens: int) -> dict:
    payload = {"model": model, "prompt": token_ids, "max_tokens": max_tokens, "temperature": 0}
    started = time.time()
    async with session.post(
        f"http://127.0.0.1:{port}/v1/completions",
        json=payload,
        timeout=aiohttp.ClientTimeout(total=180),
    ) as response:
        body = await response.json(content_type=None)
        status = response.status
    usage = body.get("usage") or {}
    cached, absent = cached_from_usage(usage)
    return {
        "http_status": status,
        "started": started,
        "finished": time.time(),
        "prompt_tokens": usage.get("prompt_tokens"),
        "cached_tokens": cached,
        "cached_field_absent": absent,
        "error": body.get("error"),
    }


async def stream_ttft(session, port: int, model: str, token_ids: list[int]) -> dict:
    payload = {
        "model": model,
        "prompt": token_ids,
        "max_tokens": 4,
        "temperature": 0,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    perf0 = time.perf_counter()
    wall0 = time.time()
    first_perf = None
    first_wall = None
    pieces: list[str] = []
    usage = None
    status = None
    error = ""
    first_piece = ""
    try:
        async with session.post(
            f"http://127.0.0.1:{port}/v1/completions",
            json=payload,
            timeout=aiohttp.ClientTimeout(total=180),
        ) as response:
            status = response.status
            if status != 200:
                error = (await response.text())[:400]
            else:
                buf = b""
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
                        if not choices:
                            continue
                        piece = choices[0].get("text") or ""
                        if piece:
                            if first_perf is None:
                                first_perf = time.perf_counter()
                                first_wall = time.time()
                                first_piece = piece[:80]
                            pieces.append(piece)
    except Exception as exc:
        error = repr(exc)[:400]
    done = time.perf_counter()
    cached, absent = cached_from_usage(usage)
    prompt_tokens = None if not usage else usage.get("prompt_tokens")
    completion_tokens = None if not usage else usage.get("completion_tokens")
    return {
        "http_status": status,
        "ttft_ms": None if first_perf is None else (first_perf - perf0) * 1000.0,
        "completion_ms": (done - perf0) * 1000.0,
        "request_start_wall": wall0,
        "first_token_wall": first_wall,
        "completion_wall": wall0 + (done - perf0),
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "cached_tokens": cached,
        "cached_field_absent": absent,
        "text": "".join(pieces)[:200],
        "first_piece": first_piece,
        "error": error,
    }


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


def gauge_vector(items: list[dict] | None, key: str) -> list[int]:
    if not items:
        return []
    return [int(item[key]) for item in items]


async def reset_caches(session) -> list[int]:
    async def one(worker: int) -> int:
        async with session.post(
            f"http://127.0.0.1:{8000 + worker}/reset_prefix_cache",
            timeout=aiohttp.ClientTimeout(total=30),
        ) as response:
            await response.read()
            return response.status
    return list(await asyncio.gather(*[one(worker) for worker in range(4)]))


def score_warmup(tap: EventTap, blocks: dict, window_start: int, token_ids: list[int], result: dict) -> dict:
    window = tap.rows[window_start:]
    tip = None
    removed = 0
    cleared = 0
    stored = 0
    new_tokens = 0
    for event in window:
        if event["event_type"] == "BlockRemoved":
            removed += 1
        elif event["event_type"] == "AllBlocksCleared":
            cleared += 1
        elif event["event_type"] == "BlockStored":
            stored += 1
            new_tokens += len(event["token_ids"])
            tip = absorb(blocks, event) or tip
    chain, complete = chain_tokens(blocks, tip)
    prompt_tokens = int(result["prompt_tokens"] or 0)
    cached = 0 if result["cached_tokens"] is None else int(result["cached_tokens"])
    chain_ok = complete and chain[:prompt_tokens] == token_ids[:prompt_tokens] and len(chain) >= prompt_tokens
    aligned = prompt_tokens % BLOCK == 0 and prompt_tokens == len(token_ids)
    if aligned and chain_ok and removed == 0 and cleared == 0:
        evidence = "BLOCK_CHAIN_MATCH"
        coverage = prompt_tokens
    elif aligned and removed == 0 and cleared == 0 and cached % BLOCK == 0 and (cached // BLOCK) * BLOCK + new_tokens == prompt_tokens:
        evidence = "CACHED_PLUS_NEW_BLOCKS"
        coverage = prompt_tokens
    else:
        evidence = "PARTIAL"
        coverage = prompt_tokens if aligned else (prompt_tokens // BLOCK) * BLOCK
    return {
        "prompt_tokens": prompt_tokens,
        "cached_tokens": cached,
        "cached_field_absent": result["cached_field_absent"],
        "coverage_tokens": coverage,
        "evidence": evidence,
        "chain_tokens": len(chain),
        "chain_matches_prompt": int(chain_ok),
        "block_stored": stored,
        "new_block_tokens": new_tokens,
        "block_removed": removed,
        "cleared": cleared,
        "http_status": result["http_status"],
        "error": json.dumps(result["error"]) if result["error"] else "",
    }


async def run_world(session, taps, tokenizer, model: str, relay: RelaySession, *,
                    phase: str, seed: int, scenario: str, assignment: str, view: str,
                    view_index: int, cell_id: int) -> tuple[dict, list[dict]]:
    visible, hidden, targets, planned_loads = roles(assignment, scenario)
    lineage = f"F6D1S{seed}{scenario}"
    prefix, prompt = build_tokens(tokenizer, seed, scenario)
    reasons: list[str] = []
    warmup_rows: list[dict] = []
    idle_ok, idle_gauges = await wait_idle(30)
    if not idle_ok:
        reasons.append("not_idle_before_reset")
    reset_status = []
    cleared_before = [tap.cleared for tap in taps]
    if idle_ok:
        reset_status = await reset_caches(session)
        if any(code != 200 for code in reset_status):
            reasons.append("reset_http")
        await asyncio.sleep(0.3)
    cleared_seen = [int(taps[w].cleared > cleared_before[w]) for w in range(4)]
    blocks = [dict() for _ in range(4)]
    windows = []

    async def warm(worker: int) -> dict:
        token_ids = prefix[:targets[worker]]
        window = len(taps[worker].rows)
        result = await completion(session, 8000 + worker, model, token_ids, 1)
        return {"worker": worker, "window": window, "token_ids": token_ids, "result": result}

    warmed = await asyncio.gather(*[warm(worker) for worker in range(4)])
    await asyncio.sleep(0.3)
    for item in warmed:
        worker = item["worker"]
        scored = score_warmup(taps[worker], blocks[worker], item["window"], item["token_ids"], item["result"])
        row = {
            "phase": phase, "seed": seed, "scenario": scenario, "assignment": assignment,
            "view": view, "worker": worker, "lineage": lineage,
            "target_tokens": targets[worker],
            "token_sha256": sha_ids(item["token_ids"]),
            "prompt_sha256": sha_ids(prompt),
            "first_ids": " ".join(map(str, item["token_ids"][:8])),
            "wrapper": "EXPERIMENTAL_WRAPPER",
            **scored,
        }
        warmup_rows.append(row)
        windows.append(row)
        if scored["evidence"] == "PARTIAL" or scored["coverage_tokens"] != targets[worker] or scored["cached_tokens"] != 0:
            reasons.append(f"residency_w{worker}")
        if scored["http_status"] != 200:
            reasons.append(f"warmup_http_w{worker}")
    removed_mark = [tap.block_removed for tap in taps]
    cleared_mark = [tap.cleared for tap in taps]
    idle2, gauges_after_warm = await wait_idle(40)
    if not idle2:
        reasons.append("not_idle_after_warmup")
    running = gauge_vector(gauges_after_warm, "running")
    waiting = gauge_vector(gauges_after_warm, "waiting")
    holder_error = ""
    holder_stop = asyncio.Event()
    holder_tasks = []
    if scenario == "D3" and not reasons:
        hold_ids = [holder_tokens(tokenizer, seed, worker, prompt) for worker in range(4)]

        async def hold(worker: int) -> None:
            try:
                async with session.post(
                    f"http://127.0.0.1:{8000 + worker}/v1/completions",
                    json={"model": model, "prompt": hold_ids[worker], "max_tokens": 256, "temperature": 0, "stream": True},
                    timeout=aiohttp.ClientTimeout(total=180),
                ) as response:
                    if response.status != 200:
                        return
                    async for _chunk in response.content.iter_any():
                        if holder_stop.is_set():
                            break
            except Exception:
                return

        for worker in visible:
            holder_tasks.append(asyncio.create_task(hold(worker)))
        deadline = time.time() + 5
        confirmed = False
        live = None
        while time.time() < deadline:
            live = await gauges()
            running = gauge_vector(live, "running")
            waiting = gauge_vector(live, "waiting")
            if running == planned_loads and all(item == 0 for item in waiting):
                confirmed = True
                break
            await asyncio.sleep(0.2)
        if not confirmed:
            reasons.append("load_unconfirmed")
            holder_error = f"running={running} waiting={waiting}"
            holder_stop.set()
    loads = list(planned_loads) if scenario != "D3" else list(running or planned_loads)
    if scenario != "D3" and (running != [0, 0, 0, 0] or waiting != [0, 0, 0, 0]):
        reasons.append("gauges_not_zero")
    ad = None
    decision = None
    measured = None
    hits_before = None
    if not reasons:
        try:
            ad = await relay.advertise(
                cell_id=cell_id, dedup=0 if view == "full" else 2, lineage=lineage,
                order=visible + hidden, coverages={row["worker"]: int(row["coverage_tokens"]) for row in warmup_rows},
                loads=loads,
            )
        except Exception as exc:
            reasons.append("advertise_error")
            holder_error = repr(exc)[:300]
    if ad is not None:
        expected_forward = [0, 1, 2, 3] if view == "full" else list(visible)
        expected_drop = [] if view == "full" else list(hidden)
        if ad["forwarded_owners"] != sorted(expected_forward) or ad["suppressed_owners"] != sorted(expected_drop):
            reasons.append("owner_set")
        if int(ad["stats"].get("replica_cap", -1)) != (0 if view == "full" else 2):
            reasons.append("replica_stat")
        if int(ad["stats"].get("forwarded", -1)) != len(expected_forward):
            reasons.append("forward_stat")
        index_cov = ad["index_cov"]
        mirror = mirror_choose(index_cov, loads, ad["dispatcher"].rr)
        request = relay.h.TraceRequest(0, 0, 0, 0, "f6d1", lineage, PROMPT_LEN, "f6d1", False)
        selected, raw, evaluated, override, reported, net = ad["dispatcher"].choose(request)
        decision = {
            "native": mirror["native"],
            "selected": int(selected),
            "override": int(bool(override)),
            "raw": int(raw),
            "evaluated": int(evaluated),
            "reported_coverage": int(reported),
            "returned_net_ms": float(net),
            "pre_guard_net_ms": mirror["pre_guard_net_ms"],
            "mirror_selected": mirror["selected"],
            "mirror_override": mirror["override"],
            "index_cov": index_cov,
        }
        if int(selected) != mirror["selected"] or int(bool(override)) != mirror["override"]:
            reasons.append("choose_mirror_mismatch")
        pre = await gauges()
        pre_running = gauge_vector(pre, "running")
        pre_waiting = gauge_vector(pre, "waiting")
        if scenario == "D3":
            if pre_running != planned_loads or any(item != 0 for item in pre_waiting):
                reasons.append("load_drift")
        elif pre_running != [0, 0, 0, 0] or pre_waiting != [0, 0, 0, 0]:
            reasons.append("gauge_drift")
        if any(taps[w].block_removed != removed_mark[w] or taps[w].cleared != cleared_mark[w] for w in range(4)):
            reasons.append("eviction_before_request")
        hits_before = [item.get("prefix_cache_hits") for item in pre]
        if "choose_mirror_mismatch" not in reasons and "eviction_before_request" not in reasons and "load_drift" not in reasons and "gauge_drift" not in reasons and "owner_set" not in reasons:
            measured = await stream_ttft(session, 8000 + int(selected), model, prompt)
            if measured["ttft_ms"] is None or measured["http_status"] != 200:
                reasons.append("ttft")
            selected_warmup = next(row["coverage_tokens"] for row in warmup_rows if row["worker"] == int(selected))
            if measured["cached_tokens"] != selected_warmup or measured["prompt_tokens"] != PROMPT_LEN:
                reasons.append("cached_mismatch")
        else:
            pre_running = gauge_vector(pre, "running")
            pre_waiting = gauge_vector(pre, "waiting")
    else:
        pre_running, pre_waiting = running, waiting
    holder_stop.set()
    if holder_tasks:
        await asyncio.gather(*holder_tasks, return_exceptions=True)
    await wait_idle(45)
    hits_after = None
    if measured is not None:
        try:
            after = await gauges()
            hits_after = [item.get("prefix_cache_hits") for item in after]
        except Exception:
            hits_after = None
    by_worker = {row["worker"]: row for row in warmup_rows}
    selected_worker = None if decision is None else decision["selected"]
    warmup_selected = None if selected_worker is None else by_worker[selected_worker]["coverage_tokens"]
    prompt_tokens = None if measured is None else measured["prompt_tokens"]
    load_selected = None if selected_worker is None else int(loads[selected_worker])
    cost = None
    if warmup_selected is not None and prompt_tokens is not None and load_selected is not None:
        cost = model_cost(load_selected, int(warmup_selected), int(prompt_tokens))
    trial = {
        "phase": phase,
        "seed": seed,
        "scenario": scenario,
        "assignment": assignment,
        "view": view,
        "view_index": view_index,
        "order_label": "",
        "lineage": lineage,
        "wrapper": "EXPERIMENTAL_WRAPPER",
        "admission": "CONTROLLED_ADMISSION_ORDER",
        "visible": ",".join(map(str, visible)),
        "hidden": ",".join(map(str, hidden)),
        "ad_order": ",".join(map(str, visible + hidden)),
        "planned_loads": ",".join(map(str, planned_loads)),
        "target_coverages": ",".join(str(targets[i]) for i in range(4)),
        "warmup_coverages": ",".join(str(by_worker[i]["coverage_tokens"]) for i in range(4)) if by_worker else "",
        "warmup_cached": ",".join(str(by_worker[i]["cached_tokens"]) for i in range(4)) if by_worker else "",
        "warmup_evidence": ",".join(by_worker[i]["evidence"] for i in range(4)) if by_worker else "",
        "prompt_sha256": sha_ids(prompt),
        "prefix_sha256": sha_ids(prefix),
        "reset_status": ",".join(map(str, reset_status)),
        "cleared_seen": ",".join(map(str, cleared_seen)),
        "idle_before_reset": int(idle_ok),
        "running_after_warmup": ",".join(map(str, running)),
        "waiting_after_warmup": ",".join(map(str, waiting)),
        "running_before_request": ",".join(map(str, pre_running)),
        "waiting_before_request": ",".join(map(str, pre_waiting)),
        "loads_used": ",".join(map(str, loads)),
        "forwarded_owners": "" if ad is None else ",".join(map(str, ad["forwarded_owners"])),
        "suppressed_owners": "" if ad is None else ",".join(map(str, ad["suppressed_owners"])),
        "relay_forwarded": None if ad is None else ad["stats"].get("forwarded"),
        "relay_replica_cap": None if ad is None else ad["stats"].get("replica_cap"),
        "index_cov": "" if decision is None else ",".join(map(str, decision["index_cov"])),
        "native_worker": None if decision is None else decision["native"],
        "selected_worker": selected_worker,
        "affinity_override": None if decision is None else decision["override"],
        "raw_candidates": None if decision is None else decision["raw"],
        "evaluated": None if decision is None else decision["evaluated"],
        "reported_coverage": None if decision is None else decision["reported_coverage"],
        "returned_net_ms": None if decision is None else decision["returned_net_ms"],
        "pre_guard_net_ms": None if decision is None else decision["pre_guard_net_ms"],
        "mirror_selected": None if decision is None else decision["mirror_selected"],
        "warmup_coverage_selected": warmup_selected,
        "request_cached_tokens": None if measured is None else measured["cached_tokens"],
        "request_prompt_tokens": prompt_tokens,
        "request_completion_tokens": None if measured is None else measured["completion_tokens"],
        "cached_field_absent": None if measured is None else measured["cached_field_absent"],
        "ttft_ms": None if measured is None else measured["ttft_ms"],
        "completion_ms": None if measured is None else measured["completion_ms"],
        "http_status": None if measured is None else measured["http_status"],
        "request_start_wall": None if measured is None else measured["request_start_wall"],
        "first_token_wall": None if measured is None else measured["first_token_wall"],
        "first_piece": "" if measured is None else measured["first_piece"],
        "output_text": "" if measured is None else measured["text"],
        "error": ("" if measured is None else measured["error"]) or holder_error,
        "hits_before": "" if not hits_before else ",".join("" if v is None else str(v) for v in hits_before),
        "hits_after": "" if not hits_after else ",".join("" if v is None else str(v) for v in hits_after),
        "model_cost_ms": cost,
        "model_label": "MODEL_DERIVED",
        "ttft_label": "MEASURED_CLIENT_TTFT" if measured and measured["ttft_ms"] is not None else "NOT_MEASURED",
        "invalid_reasons": ",".join(reasons),
        "valid": int(not reasons and measured is not None and measured["ttft_ms"] is not None),
        "cell_id": cell_id,
    }
    print(json.dumps({
        "trial": f"{phase}:{seed}:{scenario}:{view}",
        "selected": trial["selected_worker"],
        "native": trial["native_worker"],
        "ttft_ms": trial["ttft_ms"],
        "cached": trial["request_cached_tokens"],
        "valid": trial["valid"],
        "reasons": trial["invalid_reasons"],
    }), flush=True)
    return trial, warmup_rows


def pair_rows(trials: list[dict]) -> list[dict]:
    grouped: dict[tuple, dict] = {}
    for trial in trials:
        grouped.setdefault((trial["phase"], trial["seed"], trial["scenario"]), {})[trial["view"]] = trial
    rows = []
    for (phase, seed, scenario), views in sorted(grouped.items()):
        full = views.get("full")
        cap = views.get("cap")
        if full is None or cap is None:
            continue
        valid = int(full["valid"] == 1 and cap["valid"] == 1)
        delta = coverage_loss = regret = None
        if full["ttft_ms"] is not None and cap["ttft_ms"] is not None:
            delta = float(cap["ttft_ms"]) - float(full["ttft_ms"])
        if full["request_cached_tokens"] is not None and cap["request_cached_tokens"] is not None:
            coverage_loss = int(full["request_cached_tokens"]) - int(cap["request_cached_tokens"])
        if full["model_cost_ms"] is not None and cap["model_cost_ms"] is not None:
            regret = float(cap["model_cost_ms"]) - float(full["model_cost_ms"])
        same_prompt = full["prompt_sha256"] == cap["prompt_sha256"]
        if not same_prompt:
            valid = 0
        rows.append({
            "phase": phase,
            "seed": seed,
            "scenario": scenario,
            "assignment": full["assignment"],
            "order": f"{full['view'] if full['view_index']==0 else cap['view']}_then_"
                     f"{cap['view'] if full['view_index']==0 else full['view']}",
            "valid": valid,
            "same_prompt": int(same_prompt),
            "full_selected": full["selected_worker"],
            "cap_selected": cap["selected_worker"],
            "full_native": full["native_worker"],
            "cap_native": cap["native_worker"],
            "full_override": full["affinity_override"],
            "cap_override": cap["affinity_override"],
            "route_changed": int(full["selected_worker"] != cap["selected_worker"]) if full["selected_worker"] is not None and cap["selected_worker"] is not None else "",
            "full_warmup_selected": full["warmup_coverage_selected"],
            "cap_warmup_selected": cap["warmup_coverage_selected"],
            "full_cached": full["request_cached_tokens"],
            "cap_cached": cap["request_cached_tokens"],
            "coverage_loss": coverage_loss,
            "ttft_full_ms": full["ttft_ms"],
            "ttft_cap_ms": cap["ttft_ms"],
            "delta_ttft_ms": delta,
            "model_cost_full_ms": full["model_cost_ms"],
            "model_cost_cap_ms": cap["model_cost_ms"],
            "model_regret_ms": regret,
            "model_label": "MODEL_DERIVED",
            "ttft_label": "MEASURED_CLIENT_TTFT" if valid else "NOT_IN_PRIMARY",
            "full_reasons": full["invalid_reasons"],
            "cap_reasons": cap["invalid_reasons"],
            "full_running": full["running_before_request"],
            "cap_running": cap["running_before_request"],
            "visible": full["visible"],
            "hidden": full["hidden"],
        })
        # order string above is clumsy if view_index is not 0/1 as expected. Recompute.
        first = full if full["view_index"] < cap["view_index"] else cap
        second = cap if first is full else full
        rows[-1]["order"] = f"{first['view']}_then_{second['view']}"
    return rows


def _mean(values: list[float]) -> float | None:
    if not values:
        return None
    return sum(values) / len(values)


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    return float(statistics.median(values))


def _sd(values: list[float]) -> float | None:
    if len(values) < 2:
        return None
    return float(statistics.stdev(values))


def summarize(pairs: list[dict]) -> list[dict]:
    rows = []
    groups: dict[tuple, list[dict]] = {}
    for row in pairs:
        groups.setdefault((row["phase"], row["scenario"]), []).append(row)
    for (phase, scenario), items in sorted(groups.items()):
        valid = [item for item in items if item["valid"] == 1 and item["delta_ttft_ms"] is not None]
        deltas = [float(item["delta_ttft_ms"]) for item in valid]
        fulls = [float(item["ttft_full_ms"]) for item in valid]
        caps = [float(item["ttft_cap_ms"]) for item in valid]
        losses = [int(item["coverage_loss"]) for item in valid if item["coverage_loss"] is not None]
        regrets = [float(item["model_regret_ms"]) for item in valid if item["model_regret_ms"] is not None]
        changed = [item for item in valid if item["route_changed"] == 1]
        rows.append({
            "phase": phase,
            "scenario": scenario,
            "pairs": len(items),
            "valid_pairs": len(valid),
            "mean_ttft_full_ms": _mean(fulls),
            "mean_ttft_cap_ms": _mean(caps),
            "mean_delta_ttft_ms": _mean(deltas),
            "median_delta_ttft_ms": _median(deltas),
            "sd_delta_ttft_ms": _sd(deltas),
            "min_delta_ttft_ms": min(deltas) if deltas else None,
            "max_delta_ttft_ms": max(deltas) if deltas else None,
            "mean_coverage_loss": _mean(losses) if losses else None,
            "mean_model_regret_ms": _mean(regrets) if regrets else None,
            "route_change_rate": (len(changed) / len(valid)) if valid else None,
            "ttft_label": "MEASURED_CLIENT_TTFT",
            "model_label": "MODEL_DERIVED",
        })
    return rows


def pilot_gates(trials: list[dict], pairs: list[dict]) -> dict:
    pilot = [row for row in trials if row["phase"] == "pilot"]
    by_view = {row["view"]: row for row in pilot}
    full, cap = by_view.get("full"), by_view.get("cap")
    gate1 = bool(full and cap and "residency" not in full["invalid_reasons"] and "residency" not in cap["invalid_reasons"]
                 and full["warmup_evidence"].count("BLOCK_CHAIN_MATCH") == 4
                 and cap["warmup_evidence"].count("BLOCK_CHAIN_MATCH") == 4
                 and full["warmup_coverages"] == "1024,1024,4096,4096"
                 and cap["warmup_coverages"] == "1024,1024,4096,4096")
    gate2 = bool(full and cap and full["forwarded_owners"] == "0,1,2,3" and cap["forwarded_owners"] == "0,1"
                 and cap["suppressed_owners"] == "2,3" and full["relay_replica_cap"] == 0 and cap["relay_replica_cap"] == 2)
    gate3 = bool(full and cap and full["selected_worker"] is not None and cap["selected_worker"] is not None
                 and full["selected_worker"] != cap["selected_worker"]
                 and int(full["warmup_coverage_selected"]) > int(cap["warmup_coverage_selected"]))
    gate4 = bool(full and cap and full["ttft_ms"] is not None and cap["ttft_ms"] is not None
                 and full["http_status"] == 200 and cap["http_status"] == 200)
    gate5 = bool(cap and cap["warmup_cached"] == "0,0,0,0" and cap["idle_before_reset"] == 1
                 and "residency" not in cap["invalid_reasons"])
    return {
        "gate1_residency": gate1,
        "gate2_owners": gate2,
        "gate3_route": gate3,
        "gate4_ttft": gate4,
        "gate5_rebuild": gate5,
        "stop": not (gate1 and gate2 and gate4 and gate5),
        "pairs": len(pairs),
    }


async def async_main() -> int:
    RAW.mkdir(parents=True, exist_ok=True)
    started = time.time()
    commit = git_commit()
    busy = [port for port in (8000, 8001, 8002, 8003, 5560, 5561, 5562, 5563, RELAY_PORT, DISPATCH_PORT) if port_open(port)]
    if busy:
        raise RuntimeError(f"required ports already open: {busy}")
    harness = load_module("f6d1_harness", HARNESS_PY)
    relay_mod = load_module("f6d1_relaymod", RELAY_PY)
    tripwire(harness)
    tokenizer = AutoTokenizer.from_pretrained(MODEL, trust_remote_code=False)
    # Prefixes of different seeds/scenarios must differ inside the nonce.
    seen = {}
    for seed in [PILOT["seed"], *[item["seed"] for item in FORMAL]]:
        for scenario in ("D0", "D1", "D2", "D3"):
            prefix, prompt = build_tokens(tokenizer, seed, scenario)
            seen[(seed, scenario)] = prefix
            if prompt[:PREFIX_LEN] != prefix:
                raise RuntimeError("prompt prefix mismatch")
    keys = list(seen)
    for i, left in enumerate(keys):
        for right in keys[i + 1:]:
            a, b = seen[left], seen[right]
            if a[:64] == b[:64]:
                raise RuntimeError(f"prefixes alias inside 64 tokens: {left} {right}")
    procs: list[subprocess.Popen] = []
    taps = [EventTap(worker, 5560 + worker) for worker in range(4)]
    relay = RelaySession(relay_mod, harness)
    trials: list[dict] = []
    warmups: list[dict] = []
    status = "ERROR"
    gates = {}
    d3_note = "not_reached"
    cell = 900
    try:
        for worker in range(4):
            procs.append(start_vllm(worker))
        model = await asyncio.to_thread(wait_ready, procs)
        for tap in taps:
            tap.start()
        await asyncio.sleep(0.5)
        relay.start_process()
        await relay.start_dispatcher()
        await asyncio.sleep(0.5)
        async with aiohttp.ClientSession() as session:
            for view_index, view in enumerate(PILOT["order"]):
                trial, rows = await run_world(
                    session, taps, tokenizer, model, relay,
                    phase="pilot", seed=PILOT["seed"], scenario=PILOT["scenario"],
                    assignment=PILOT["assignment"], view=view, view_index=view_index, cell_id=cell,
                )
                cell += 1
                trials.append(trial)
                warmups.extend(rows)
            gates = pilot_gates(trials, pair_rows(trials))
            (OUT / "feasibility_gates.json").write_text(json.dumps(gates, indent=2) + "\n")
            if gates["stop"]:
                status = "GATE_STOP"
            else:
                status = "RAN"
                for spec in FORMAL:
                    if time.time() - started > WALL_BUDGET_S:
                        status = "BUDGET_STOP"
                        break
                    for scenario in SCENARIOS:
                        for view_index, view in enumerate(spec["order"]):
                            trial, rows = await run_world(
                                session, taps, tokenizer, model, relay,
                                phase="formal", seed=spec["seed"], scenario=scenario,
                                assignment=spec["assignment"], view=view, view_index=view_index,
                                cell_id=cell,
                            )
                            cell += 1
                            trials.append(trial)
                            warmups.extend(rows)
                formal_pairs = [row for row in pair_rows(trials) if row["phase"] == "formal"]
                formal_trials = [row for row in trials if row["phase"] == "formal"]
                elapsed = time.time() - started
                all_valid = len(formal_pairs) == 12 and all(row["valid"] == 1 for row in formal_pairs)
                idle_ok = all(row["idle_before_reset"] == 1 for row in formal_trials)
                if not all_valid or not idle_ok or elapsed >= D3_BUDGET_S or status == "BUDGET_STOP":
                    d3_note = f"skipped valid={all_valid} idle={idle_ok} elapsed_s={elapsed:.1f}"
                else:
                    d3_note = "ran"
                    for spec in FORMAL[:2]:
                        for view_index, view in enumerate(spec["order"]):
                            trial, rows = await run_world(
                                session, taps, tokenizer, model, relay,
                                phase="d3", seed=spec["seed"], scenario="D3",
                                assignment=spec["assignment"], view=view, view_index=view_index,
                                cell_id=cell,
                            )
                            cell += 1
                            trials.append(trial)
                            warmups.extend(rows)
                            if "load_unconfirmed" in trial["invalid_reasons"]:
                                d3_note = "skipped_after_unconfirmed_load"
                                break
                        if d3_note.startswith("skipped"):
                            break
        return 0
    except Exception:
        text = traceback.format_exc()
        for worker in range(4):
            log_path = RAW / f"vllm_gpu{worker}.log"
            if log_path.exists():
                tail = log_path.read_text(errors="replace")[-1500:]
                text += f"\n\n--- vllm_gpu{worker}.log ---\n{tail}"
        (RAW / "driver_error.txt").write_text(text)
        print(text, flush=True)
        status = "ERROR"
        return 1
    finally:
        pairs = pair_rows(trials)
        summary = summarize(pairs)
        write_csv(OUT / "f6_d1_warmup.csv", warmups)
        write_csv(OUT / "f6_d1_per_trial.csv", trials)
        write_csv(OUT / "f6_d1_paired_results.csv", pairs)
        write_csv(OUT / "f6_d1_summary.csv", summary)
        write_csv(OUT / "f6_d1_model_vs_measured.csv", pairs)
        payload = {
            "status": status,
            "git_commit": commit,
            "gates": gates,
            "d3": d3_note,
            "elapsed_s": time.time() - started,
            "trials": len(trials),
            "model_definition": "T=load*2+max(0,prompt-warmup_coverage)/50; regret=Tcap-Tfull; MODEL_DERIVED",
            "ttft_definition": "client stream POST to first non-empty completion text; MEASURED_CLIENT_TTFT",
            "production_gateway": "not touched; 127.0.0.1:9710",
        }
        (OUT / "run_status.json").write_text(json.dumps(payload, indent=2) + "\n")
        for tap in taps:
            tap.stop()
        relay.close()
        stop_vllm(procs)
        print(json.dumps(payload), flush=True)


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(async_main()))
    except Exception:
        traceback.print_exc()
        raise
