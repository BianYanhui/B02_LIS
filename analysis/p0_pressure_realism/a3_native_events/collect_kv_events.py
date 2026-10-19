#!/usr/bin/env python3
"""Collect vLLM V1 KV-cache events for one short GPU session.

The script does not change cache policy. It subscribes to the official ZMQ
publisher and sends ordinary chat completions. Token ids from BlockStored are
hashed and dropped; prompts are not written to disk.
"""
from __future__ import annotations

import argparse
import array
import asyncio
import csv
import hashlib
import json
import time
import urllib.request
from pathlib import Path

import aiohttp
import msgspec
import zmq
from transformers import AutoTokenizer

from vllm.distributed.kv_events import (
    AllBlocksCleared,
    BlockRemoved,
    BlockStored,
    KVEventBatch,
)

MODEL = "/home/byh/.cache/modelscope/qwen/Qwen2.5-1.5B-Instruct"


def digest(data: bytes) -> str:
    return hashlib.blake2s(data, digest_size=8).hexdigest()


def hex_hash(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, int):
        return format(value, "x")
    return digest(repr(value).encode())


class Subscriber:
    def __init__(self, endpoint: str) -> None:
        self.ctx = zmq.Context.instance()
        self.sock = self.ctx.socket(zmq.SUB)
        self.sock.connect(endpoint)
        self.sock.setsockopt(zmq.SUBSCRIBE, b"")
        self.rows: list[dict] = []
        self._stop = False

    def drain(self, scenario: str) -> int:
        got = 0
        decoder = msgspec.msgpack.Decoder(KVEventBatch)
        while True:
            try:
                parts = self.sock.recv_multipart(flags=zmq.NOBLOCK)
            except zmq.Again:
                break
            recv_ns = time.time_ns()
            if len(parts) != 3:
                continue
            _topic, seq_bytes, payload = parts
            batch = decoder.decode(payload)
            seq = int.from_bytes(seq_bytes, "big")
            for index, event in enumerate(batch.events):
                kind = type(event).__name__
                hashes = []
                parent = ""
                token_digest = ""
                token_count = 0
                block_size = ""
                if isinstance(event, BlockStored):
                    hashes = [hex_hash(item) for item in event.block_hashes]
                    parent = hex_hash(event.parent_block_hash)
                    raw = array.array("I", event.token_ids).tobytes()
                    token_digest = digest(raw)
                    token_count = len(event.token_ids)
                    block_size = event.block_size
                elif isinstance(event, BlockRemoved):
                    hashes = [hex_hash(item) for item in event.block_hashes]
                self.rows.append({
                    "recv_ns": recv_ns,
                    "batch_ts_s": batch.ts,
                    "batch_seq": seq,
                    "event_index": index,
                    "scenario": scenario,
                    "event_type": kind,
                    "block_hashes": " ".join(hashes),
                    "n_blocks": len(hashes) if hashes else (0 if kind != "AllBlocksCleared" else 0),
                    "parent_block_hash": parent,
                    "token_ids_blake2s": token_digest,
                    "token_count": token_count,
                    "block_size": block_size,
                    "event_source": "vllm_v1_zmq_kv_event",
                    "event_semantics": {
                        "BlockStored": "full blocks newly inserted into the prefix cache",
                        "BlockRemoved": "cached block evicted while allocating a free block",
                        "AllBlocksCleared": "prefix cache reset",
                    }.get(kind, ""),
                })
                got += 1
        return got

    def close(self) -> None:
        self.sock.close(linger=0)


def metrics(port: int) -> dict[str, float]:
    text = urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=5).read().decode()
    wanted = (
        "vllm:prefix_cache_queries_total",
        "vllm:prefix_cache_hits_total",
        "vllm:kv_cache_usage_perc",
        "vllm:prompt_tokens_total",
    )
    out = {name: 0.0 for name in wanted}
    for line in text.splitlines():
        if line.startswith("#"):
            continue
        for name in wanted:
            if line.startswith(name + " ") or line.startswith(name + "{"):
                out[name] = float(line.split()[-1])
    return out


def make_prompt(tokenizer, nonce: str, target_tokens: int) -> tuple[str, int]:
    piece = f"item {nonce} "
    text = piece
    while len(tokenizer(text, add_special_tokens=False).input_ids) < target_tokens:
        text += piece
    ids = tokenizer(text, add_special_tokens=False).input_ids[:target_tokens]
    return tokenizer.decode(ids), len(ids)


async def one_request(session, url, prompt, max_tokens) -> dict:
    body = {
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0,
    }
    started = time.time_ns()
    async with session.post(url + "/v1/chat/completions", json=body) as response:
        payload = await response.json()
        status = response.status
    finished = time.time_ns()
    usage = (payload.get("usage") or {}) if status == 200 else {}
    details = usage.get("prompt_tokens_details") or {}
    return {
        "ok": status == 200,
        "status": status,
        "started_ns": started,
        "finished_ns": finished,
        "latency_s": (finished - started) / 1e9,
        "prompt_tokens": usage.get("prompt_tokens", ""),
        "cached_tokens": details.get("cached_tokens", ""),
        "completion_tokens": usage.get("completion_tokens", ""),
        "error": "" if status == 200 else json.dumps(payload)[:200],
    }


async def reset_cache(session, url) -> str:
    async with session.post(url + "/reset_prefix_cache") as response:
        body = await response.text()
        return f"{response.status} {body[:120]}"


async def run_scenario(session, sub, url, scenario, prompts, max_tokens, rows) -> None:
    for index, (prompt, prompt_tokens, kind) in enumerate(prompts):
        result = await one_request(session, url, prompt, max_tokens)
        # Events are published during the step; give the ZMQ thread a moment.
        await asyncio.sleep(0.05)
        sub.drain(scenario)
        rows.append({
            "scenario": scenario,
            "seq": index,
            "kind": kind,
            "prompt_blake2s": digest(prompt.encode()),
            "built_prompt_tokens": prompt_tokens,
            **result,
        })


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--events", default="tcp://127.0.0.1:5557")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--target-tokens", type=int, default=1024)
    parser.add_argument("--cold-count", type=int, default=24)
    parser.add_argument("--reuse-repeats", type=int, default=8)
    parser.add_argument("--churn-seconds", type=float, default=75.0)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    tokenizer = AutoTokenizer.from_pretrained(MODEL)
    sub = Subscriber(args.events)
    await asyncio.sleep(0.4)
    url = f"http://127.0.0.1:{args.port}"
    request_rows: list[dict] = []
    metric_rows = []
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as session:
        metric_rows.append({"mark": "start", **metrics(args.port)})
        print("reset", await reset_cache(session, url), flush=True)
        await asyncio.sleep(0.2)
        sub.drain("reset_before_a")

        warmup_prompt, warmup_n = make_prompt(tokenizer, "warmup", 128)
        await run_scenario(session, sub, url, "warmup", [(warmup_prompt, warmup_n, "warmup")], 4, request_rows)

        print("reset", await reset_cache(session, url), flush=True)
        await asyncio.sleep(0.2)
        sub.drain("reset_before_a")
        cold = []
        for i in range(args.cold_count):
            prompt, n = make_prompt(tokenizer, f"cold-{i:04d}", args.target_tokens)
            cold.append((prompt, n, "unique"))
        t0 = time.perf_counter()
        await run_scenario(session, sub, url, "A_cold", cold, 4, request_rows)
        print(f"scenario A {time.perf_counter() - t0:.1f}s", flush=True)
        metric_rows.append({"mark": "after_A", **metrics(args.port)})

        print("reset", await reset_cache(session, url), flush=True)
        await asyncio.sleep(0.2)
        sub.drain("reset_before_b")
        reuse_prompt, reuse_n = make_prompt(tokenizer, "reuse-same", args.target_tokens)
        reuse = [(reuse_prompt, reuse_n, "cold" if i == 0 else "warm") for i in range(1 + args.reuse_repeats)]
        t0 = time.perf_counter()
        await run_scenario(session, sub, url, "B_reuse", reuse, 4, request_rows)
        print(f"scenario B {time.perf_counter() - t0:.1f}s", flush=True)
        metric_rows.append({"mark": "after_B", **metrics(args.port)})

        print("reset", await reset_cache(session, url), flush=True)
        await asyncio.sleep(0.2)
        sub.drain("reset_before_c")
        # Plant three probes, then flood unique prefixes until the time budget.
        probes = []
        for name in ("early", "mid", "late"):
            prompt, n = make_prompt(tokenizer, f"probe-{name}", args.target_tokens)
            probes.append((name, prompt, n))
        t0 = time.perf_counter()
        await run_scenario(
            session, sub, url, "C_plant",
            [(prompt, n, f"plant-{name}") for name, prompt, n in probes],
            4, request_rows,
        )
        flood_i = 0
        while time.perf_counter() - t0 < args.churn_seconds:
            prompt, n = make_prompt(tokenizer, f"flood-{flood_i:04d}", args.target_tokens)
            await run_scenario(session, sub, url, "C_flood", [(prompt, n, "flood")], 4, request_rows)
            flood_i += 1
        print(f"scenario C flood {flood_i} prompts in {time.perf_counter() - t0:.1f}s", flush=True)
        # Replay probes. A miss after a hit is inferred eviction, not an observed one.
        await run_scenario(
            session, sub, url, "C_replay",
            [(prompt, n, f"replay-{name}") for name, prompt, n in probes],
            4, request_rows,
        )
        metric_rows.append({"mark": "after_C", **metrics(args.port)})
        await asyncio.sleep(0.3)
        sub.drain("tail")

    request_path = args.out / "requests.csv"
    event_path = args.out / "events.csv"
    metric_path = args.out / "metrics.csv"
    with request_path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(request_rows[0].keys()))
        writer.writeheader()
        writer.writerows(request_rows)
    if sub.rows:
        with event_path.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(sub.rows[0].keys()))
            writer.writeheader()
            writer.writerows(sub.rows)
    else:
        event_path.write_text("")
    with metric_path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(metric_rows[0].keys()))
        writer.writeheader()
        writer.writerows(metric_rows)
    print(f"requests {len(request_rows)} events {len(sub.rows)}", flush=True)
    sub.close()


if __name__ == "__main__":
    asyncio.run(main())
