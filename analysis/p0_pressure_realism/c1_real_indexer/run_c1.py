#!/usr/bin/env python3
"""P0-C1: capture one GPU's native KV events, then replay them into Dynamo.

The capture does not change vLLM cache policy. Token ids stay inside the raw
ZMQ frames used for replay; the parsed CSV stores only hashes and counts.
"""
from __future__ import annotations

import argparse
import array
import asyncio
import csv
import hashlib
import json
import os
import signal
import struct
import subprocess
import time
import urllib.request
from collections import Counter, defaultdict
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

ROOT = Path("/home/byh/B02")
OUT = ROOT / "analysis/p0_pressure_realism/c1_real_indexer"
RAW = OUT / "raw"
MODEL = "/home/byh/.cache/modelscope/qwen/Qwen2.5-1.5B-Instruct"
VLLM = ROOT / "poc/.venv/bin/vllm"
DYNAMO_PY = Path("/home/byh/Dynamo/.venv-dynamo/bin/python")
INDEXER_PORT = 8090
VLLM_PORT = 8000
ZMQ_ENDPOINT = "tcp://127.0.0.1:5557"
FRAME = struct.Struct("<QIII")


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


def hash_int(value) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, bytes):
        return int.from_bytes(value[:8], "big")
    return None


class Capture:
    def __init__(self) -> None:
        self.ctx = zmq.Context.instance()
        self.sock = self.ctx.socket(zmq.SUB)
        self.sock.connect(ZMQ_ENDPOINT)
        self.sock.setsockopt(zmq.SUBSCRIBE, b"")
        self.frames: list[tuple[int, bytes, bytes, bytes]] = []
        self._stop = False
        self._closed = False

    def loop(self) -> None:
        while not self._stop:
            try:
                parts = self.sock.recv_multipart(flags=zmq.NOBLOCK)
            except zmq.Again:
                time.sleep(0.002)
                continue
            recv_ns = time.time_ns()
            if len(parts) != 3:
                continue
            self.frames.append((recv_ns, parts[0], parts[1], parts[2]))

    def stop(self) -> None:
        if self._closed:
            return
        self._stop = True
        self._closed = True
        time.sleep(0.05)
        while True:
            try:
                parts = self.sock.recv_multipart(flags=zmq.NOBLOCK)
            except zmq.Again:
                break
            if len(parts) == 3:
                self.frames.append((time.time_ns(), parts[0], parts[1], parts[2]))
        self.sock.close(linger=0)


def save_frames(path: Path, frames) -> None:
    with path.open("wb") as handle:
        for recv_ns, topic, seq, payload in frames:
            handle.write(FRAME.pack(recv_ns, len(topic), len(seq), len(payload)))
            handle.write(topic)
            handle.write(seq)
            handle.write(payload)


def load_frames(path: Path):
    frames = []
    data = path.read_bytes()
    offset = 0
    while offset + FRAME.size <= len(data):
        recv_ns, topic_len, seq_len, payload_len = FRAME.unpack_from(data, offset)
        offset += FRAME.size
        end = offset + topic_len + seq_len + payload_len
        topic = data[offset:offset + topic_len]
        seq = data[offset + topic_len:offset + topic_len + seq_len]
        payload = data[offset + topic_len + seq_len:end]
        offset = end
        frames.append((recv_ns, topic, seq, payload))
    return frames


def decode_payload(payload: bytes):
    decoder = msgspec.msgpack.Decoder(KVEventBatch)
    return decoder.decode(payload)


def event_rows(frames):
    rows = []
    for recv_ns, _topic, seq, payload in frames:
        batch = decode_payload(payload)
        seq_no = int.from_bytes(seq, "big") if len(seq) == 8 else -1
        for index, event in enumerate(batch.events):
            kind = type(event).__name__
            hashes = []
            parent = ""
            token_count = 0
            block_size = ""
            if isinstance(event, BlockStored):
                hashes = [hex_hash(item) for item in event.block_hashes]
                parent = hex_hash(event.parent_block_hash)
                token_count = len(event.token_ids)
                block_size = event.block_size
            elif isinstance(event, BlockRemoved):
                hashes = [hex_hash(item) for item in event.block_hashes]
            rows.append({
                "recv_ns": recv_ns,
                "batch_ts_s": batch.ts,
                "batch_seq": seq_no,
                "event_index": index,
                "event_type": kind,
                "n_blocks": len(hashes),
                "block_hashes": " ".join(hashes),
                "parent_block_hash": parent,
                "token_count": token_count,
                "block_size": block_size,
            })
    return rows


def make_prompt(tokenizer, nonce: str, target_tokens: int) -> str:
    piece = f"item {nonce} "
    text = piece
    while len(tokenizer(text, add_special_tokens=False).input_ids) < target_tokens:
        text += piece
    ids = tokenizer(text, add_special_tokens=False).input_ids[:target_tokens]
    return tokenizer.decode(ids)


async def one_request(session, url, prompt) -> dict:
    body = {
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 4,
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
        "latency_s": round((finished - started) / 1e9, 4),
        "prompt_tokens": usage.get("prompt_tokens", ""),
        "cached_tokens": details.get("cached_tokens", ""),
        "completion_tokens": usage.get("completion_tokens", ""),
    }


def removed_count(frames, since: int) -> int:
    total = 0
    decoder = msgspec.msgpack.Decoder(KVEventBatch)
    for recv_ns, _topic, _seq, payload in frames[since:]:
        try:
            batch = decoder.decode(payload)
        except msgspec.DecodeError:
            continue
        total += sum(isinstance(event, BlockRemoved) for event in batch.events)
    return total


async def run_workload(capture: Capture) -> list[dict]:
    tokenizer = AutoTokenizer.from_pretrained(MODEL, trust_remote_code=True)
    url = f"http://127.0.0.1:{VLLM_PORT}"
    rows = []
    async with aiohttp.ClientSession() as session:
        async with session.post(url + "/reset_prefix_cache") as response:
            reset_body = await response.text()
        await asyncio.sleep(0.2)
        cold = [make_prompt(tokenizer, f"c1-cold-{i}", 1024) for i in range(8)]
        plant = [make_prompt(tokenizer, f"c1-plant-{i}", 1024) for i in range(2)]
        plan = [("A_cold", prompt) for prompt in cold]
        plan += [("B_reuse", cold[0]) for _ in range(6)]
        plan += [("C_plant", prompt) for prompt in plant]
        started = time.perf_counter()
        for scenario, prompt in plan:
            result = await one_request(session, url, prompt)
            rows.append({
                "scenario": scenario,
                "prompt_blake2s": digest(prompt.encode()),
                "reset_before_session": reset_body[:80],
                **result,
            })
            await asyncio.sleep(0.02)
        flood_started = time.perf_counter()
        index = 0
        baseline = len(capture.frames)
        while time.perf_counter() - flood_started < 90 and time.perf_counter() - started < 115:
            prompt = make_prompt(tokenizer, f"c1-flood-{index}", 1024)
            result = await one_request(session, url, prompt)
            rows.append({
                "scenario": "C_flood",
                "prompt_blake2s": digest(prompt.encode()),
                "reset_before_session": "",
                **result,
            })
            index += 1
            await asyncio.sleep(0.01)
            if removed_count(capture.frames, baseline) >= 30 and index >= 8:
                break
        for prompt in plant:
            result = await one_request(session, url, prompt)
            rows.append({
                "scenario": "C_replay",
                "prompt_blake2s": digest(prompt.encode()),
                "reset_before_session": "",
                **result,
            })
            await asyncio.sleep(0.05)
    return rows


def start_vllm() -> subprocess.Popen:
    RAW.mkdir(parents=True, exist_ok=True)
    log = open(RAW / "vllm_gpu0.log", "w")
    env = os.environ.copy()
    env["CUDA_VISIBLE_DEVICES"] = "0"
    env["VLLM_SERVER_DEV_MODE"] = "1"
    config = json.dumps({
        "enable_kv_cache_events": True,
        "publisher": "zmq",
        "endpoint": "tcp://*:5557",
    })
    proc = subprocess.Popen(
        [
            str(VLLM), "serve", MODEL,
            "--host", "127.0.0.1", "--port", str(VLLM_PORT),
            "--gpu-memory-utilization", "0.40",
            "--max-model-len", "6144", "--max-num-seqs", "8",
            "--enable-prefix-caching", "--enable-prompt-tokens-details",
            "--swap-space", "4", "--block-size", "16", "--enforce-eager",
            "--kv-events-config", config,
        ],
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    proc._log = log  # type: ignore[attr-defined]
    return proc


def wait_ready(proc: subprocess.Popen, timeout_s: float = 180) -> None:
    deadline = time.time() + timeout_s
    url = f"http://127.0.0.1:{VLLM_PORT}/v1/models"
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"vLLM exited early with code {proc.returncode}")
        try:
            urllib.request.urlopen(url, timeout=2).read()
            return
        except Exception:
            time.sleep(2)
    raise TimeoutError("vLLM did not become ready")


def stop_proc(proc: subprocess.Popen | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    os.killpg(proc.pid, signal.SIGTERM)
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=10)


def start_tcpdump() -> subprocess.Popen | None:
    pcap = RAW / "lo_5557.pcap"
    try:
        proc = subprocess.Popen(
            ["tcpdump", "-i", "lo", "-nn", "-s", "0", "-w", str(pcap), "tcp port 5557"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as exc:
        print("tcpdump_not_started", exc)
        return None
    time.sleep(0.3)
    if proc.poll() is not None:
        err = proc.stderr.read().decode()[:300] if proc.stderr else ""
        print("tcpdump_failed", err)
        return None
    return proc


def tcp_payload_bytes(pcap: Path) -> tuple[str, int, int]:
    if not pcap.exists() or pcap.stat().st_size == 0:
        return "NOT_MEASURED", 0, 0
    proc = subprocess.run(
        ["tcpdump", "-nn", "-q", "-r", str(pcap)],
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return "NOT_MEASURED", 0, 0
    payload = 0
    packets = 0
    for line in proc.stdout.splitlines():
        # 127.0.0.1.5557 > 127.0.0.1.12345: tcp 1448
        marker = " tcp "
        if marker not in line:
            continue
        tail = line.rsplit(marker, 1)[1].split()[0]
        if tail.isdigit():
            payload += int(tail)
            packets += 1
    return "OBSERVED", payload, packets


def assign_scenario(recv_ns: int, requests: list[dict]) -> str:
    chosen = "outside_requests"
    for row in requests:
        start = int(row["started_ns"])
        finish = int(row["finished_ns"]) + 80_000_000
        if start <= recv_ns <= finish:
            chosen = row["scenario"]
    return chosen


def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    fields: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in row:
            if key not in seen:
                seen.add(key)
                fields.append(key)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def percentile(values: list[int], q: float) -> int | str:
    if not values:
        return ""
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round(q * (len(ordered) - 1))))
    return ordered[index]


def wire_profile(frames, requests: list[dict]) -> list[dict]:
    tcp_status, tcp_bytes, tcp_packets = tcp_payload_bytes(RAW / "lo_5557.pcap")
    grouped = defaultdict(list)
    for frame in frames:
        grouped[assign_scenario(frame[0], requests)].append(frame)
    grouped["ALL"] = list(frames)
    rows = []
    for scenario, items in grouped.items():
        if not items:
            continue
        seqs = []
        payload_bytes = 0
        frame_bytes = 0
        events = hashes = stored = removed = cleared = 0
        per_message_hashes = []
        decode_ns = 0
        for recv_ns, topic, seq, payload in items:
            payload_bytes += len(payload)
            frame_bytes += len(topic) + len(seq) + len(payload)
            if len(seq) == 8:
                seqs.append(int.from_bytes(seq, "big"))
            t0 = time.perf_counter_ns()
            batch = decode_payload(payload)
            decode_ns += time.perf_counter_ns() - t0
            message_hashes = 0
            for event in batch.events:
                events += 1
                if isinstance(event, BlockStored):
                    stored += 1
                    message_hashes += len(event.block_hashes)
                elif isinstance(event, BlockRemoved):
                    removed += 1
                    message_hashes += len(event.block_hashes)
                elif isinstance(event, AllBlocksCleared):
                    cleared += 1
            hashes += message_hashes
            per_message_hashes.append(message_hashes)
        seqs_sorted = sorted(seqs)
        gaps = 0
        for prev, cur in zip(seqs_sorted, seqs_sorted[1:]):
            if cur > prev + 1:
                gaps += cur - prev - 1
        duration = max(1e-9, (items[-1][0] - items[0][0]) / 1e9)
        rows.append({
            "scenario": scenario,
            "zmq_messages": len(items),
            "event_objects": events,
            "block_stored_events": stored,
            "block_removed_events": removed,
            "all_blocks_cleared": cleared,
            "block_hashes": hashes,
            "msgpack_payload_bytes": payload_bytes,
            "zmq_frame_body_bytes": frame_bytes,
            "tcp_payload_bytes": tcp_bytes if scenario == "ALL" and tcp_status == "OBSERVED" else "",
            "tcp_payload_status": tcp_status if scenario == "ALL" else "NOT_MEASURED",
            "tcp_packets": tcp_packets if scenario == "ALL" and tcp_status == "OBSERVED" else "",
            "tcp_header_bytes": tcp_packets * 40 if scenario == "ALL" and tcp_status == "OBSERVED" else "",
            "tcp_header_status": "ESTIMATED" if scenario == "ALL" and tcp_status == "OBSERVED" else "NOT_MEASURED",
            "duration_s": round(duration, 3),
            "hashes_per_message_p50": percentile(per_message_hashes, 0.50),
            "hashes_per_message_max": max(per_message_hashes) if per_message_hashes else 0,
            "seq_first": seqs_sorted[0] if seqs_sorted else "",
            "seq_last": seqs_sorted[-1] if seqs_sorted else "",
            "seq_gaps": gaps,
            "subscriber_decode_s": round(decode_ns / 1e9, 6),
            "byte_status": "OBSERVED",
        })
    return rows


def chain_of(tip: str, parent: dict[str, str]) -> list[str]:
    out = []
    seen = set()
    node = tip
    while node and node not in seen:
        seen.add(node)
        out.append(node)
        node = parent.get(node, "")
    out.reverse()
    return out


def prefix_model(events: list[dict]) -> tuple[list[dict], list[dict]]:
    """Model A keeps block residency. Model B keeps advertised tips only."""
    alive: set[str] = set()
    parent: dict[str, str] = {}
    ads: dict[str, dict] = {}
    transitions = []
    probes = []

    def emit(kind, tip, scenario, ts, coverage, survivor, note):
        transitions.append({
            "kind": kind,
            "scenario": scenario,
            "prefix": tip,
            "coverage_tokens": coverage,
            "survivor_tokens": survivor,
            "note": note,
            "timestamp_ns": ts,
        })

    pending = []

    def flush():
        if not pending:
            return
        touched = set()
        scenario = pending[-1]["scenario"]
        ts = pending[-1]["recv_ns"]
        for event in pending:
            for block_hash in event["block_hashes"].split():
                alive.discard(block_hash)
                touched.add(block_hash)
        for tip, ad in list(ads.items()):
            if ad["validity"] != "VALID":
                continue
            full = chain_of(tip, parent)
            if not any(block in touched for block in full):
                continue
            survivor = []
            for node in full:
                if node not in alive:
                    break
                survivor.append(node)
            if len(survivor) == len(full):
                continue
            ad["validity"] = "INVALID"
            ad["survivor_tokens"] = len(survivor) * 16
            emit("INVALIDATE", tip, scenario, ts, ad["advertised_tokens"], ad["survivor_tokens"],
                 "removal batch broke an advertised tip")
        pending.clear()

    for event in events:
        if event["event_type"] != "BlockRemoved" and pending:
            flush()
        if event["event_type"] == "AllBlocksCleared":
            flush()
            for tip, ad in ads.items():
                if ad["validity"] == "VALID":
                    ad["validity"] = "INVALID"
                    ad["survivor_tokens"] = 0
                    emit("RESET", tip, event.get("scenario", ""), event["recv_ns"],
                         ad["advertised_tokens"], 0, "AllBlocksCleared")
            alive.clear()
            continue
        if event["event_type"] == "BlockRemoved":
            pending.append(event)
            continue
        if event["event_type"] != "BlockStored":
            continue
        hashes = event["block_hashes"].split()
        cursor = event["parent_block_hash"]
        new = []
        for block_hash in hashes:
            if block_hash in alive:
                cursor = block_hash
                continue
            parent[block_hash] = cursor or ""
            alive.add(block_hash)
            new.append(block_hash)
            cursor = block_hash
        if len(new) < 16 or not hashes:
            continue
        tip = hashes[-1]
        coverage = len(chain_of(tip, parent)) * 16
        if tip in ads and ads[tip]["validity"] == "INVALID":
            ads[tip]["validity"] = "VALID"
            ads[tip]["advertised_tokens"] = coverage
            emit("RE_ADVERTISE", tip, event.get("scenario", ""), event["recv_ns"], coverage, coverage,
                 "tip stored again after invalidation")
        elif tip not in ads:
            ads[tip] = {"validity": "VALID", "advertised_tokens": coverage, "survivor_tokens": coverage}
            emit("UPSERT", tip, event.get("scenario", ""), event["recv_ns"], coverage, coverage,
                 "new advertised tip")
    flush()

    # Probes against the final state, plus the historical partial-eviction fact.
    invalidated = [row for row in transitions if row["kind"] == "INVALIDATE"]
    partial = [row for row in invalidated if int(row["survivor_tokens"]) > 0]
    full = [row for row in invalidated if int(row["survivor_tokens"]) == 0]
    readv = [row for row in transitions if row["kind"] == "RE_ADVERTISE"]
    upserts = [row for row in transitions if row["kind"] == "UPSERT"]
    probes.append({
        "case": "cold_or_new_prefix",
        "observed": "yes" if upserts else "no",
        "model_a_keeps_block_chain": "yes" if upserts else "NOT_OBSERVED",
        "model_b_visible_tokens": upserts[-1]["coverage_tokens"] if upserts else "",
        "model_b_status": "advertised_tip" if upserts else "NOT_OBSERVED",
    })
    probes.append({
        "case": "partial_eviction",
        "observed": "yes" if partial else "no",
        "model_a_keeps_block_chain": "ancestor_blocks_remain" if partial else "NOT_OBSERVED",
        "model_b_visible_tokens": 0 if partial else "",
        "model_b_status": "LOST_PARTIAL_PREFIX_VISIBILITY" if partial else "NOT_OBSERVED",
        "survivor_tokens_at_break": partial[0]["survivor_tokens"] if partial else "",
        "advertised_tokens": partial[0]["coverage_tokens"] if partial else "",
        "count": len(partial),
    })
    probes.append({
        "case": "full_eviction",
        "observed": "yes" if full else "no",
        "model_a_keeps_block_chain": "tip_chain_empty" if full else "NOT_OBSERVED",
        "model_b_visible_tokens": 0 if full else "",
        "model_b_status": "tombstone_matches_full_loss" if full else "NOT_OBSERVED",
        "count": len(full),
    })
    probes.append({
        "case": "removed_then_stored",
        "observed": "yes" if readv else "no",
        "model_a_keeps_block_chain": "reinserted" if readv else "NOT_OBSERVED",
        "model_b_visible_tokens": readv[-1]["coverage_tokens"] if readv else "",
        "model_b_status": "re_advertised" if readv else "NOT_OBSERVED",
        "count": len(readv),
    })
    return transitions, probes


def representation_rows(events, frames, transitions, probes) -> list[dict]:
    payload = sum(len(frame[3]) for frame in frames)
    upserts = sum(1 for row in transitions if row["kind"] in {"UPSERT", "RE_ADVERTISE"})
    invalid = sum(1 for row in transitions if row["kind"] == "INVALIDATE")
    resets = sum(1 for row in transitions if row["kind"] == "RESET")
    hashes = sum(int(row["n_blocks"]) for row in events)
    partial = next(row for row in probes if row["case"] == "partial_eviction")
    rows = [
        {
            "model": "A_native_block_index",
            "metadata_records": hashes,
            "record_unit": "block_hash_occurrence",
            "metadata_bytes": payload,
            "byte_basis": "OBSERVED_MSGPACK_PAYLOAD",
            "full_prefix_match": "keeps_each_live_block",
            "partial_prefix_match": "ancestor_remains_addressable",
            "invalidation_accuracy": "per_removed_block",
            "partial_visibility": "kept",
            "evidence_count": hashes,
        },
        {
            "model": "B_compact_prefix_advertisement",
            "metadata_records": upserts + invalid,
            "record_unit": "upsert_or_tip_tombstone",
            "metadata_bytes": (upserts + invalid) * 104,
            "byte_basis": "ESTIMATED_104B_WIRE_CONVENTION",
            "full_prefix_match": "tip_coverage",
            "partial_prefix_match": partial["model_b_status"],
            "invalidation_accuracy": "one_tombstone_per_broken_tip",
            "partial_visibility": partial["model_b_status"],
            "evidence_count": f"upsert={upserts};invalidate={invalid};reset_excluded={resets}",
        },
    ]
    return rows


def capture_main() -> None:
    RAW.mkdir(parents=True, exist_ok=True)
    proc = start_vllm()
    dump = None
    capture = None
    try:
        wait_ready(proc)
        capture = Capture()
        thread_started = time.time()
        import threading
        thread = threading.Thread(target=capture.loop, daemon=True)
        thread.start()
        time.sleep(0.5)
        dump = start_tcpdump()
        requests = asyncio.run(run_workload(capture))
        time.sleep(0.3)
        capture.stop()
        thread.join(timeout=2)
        save_frames(RAW / "frames.bin", capture.frames)
        write_csv(RAW / "requests.csv", requests)
        events = event_rows(capture.frames)
        for event in events:
            event["scenario"] = assign_scenario(int(event["recv_ns"]), requests)
        write_csv(RAW / "events.csv", events)
        profile = wire_profile(capture.frames, requests)
        write_csv(OUT / "native_event_wire_profile.csv", profile)
        transitions, probes = prefix_model(events)
        write_csv(RAW / "prefix_transitions.csv", transitions)
        write_csv(RAW / "prefix_probes.csv", probes)
        write_csv(OUT / "prefix_representation_comparison.csv",
                  representation_rows(events, capture.frames, transitions, probes))
        log_text = (RAW / "vllm_gpu0.log").read_text(errors="replace")
        kv_line = ""
        for line in log_text.splitlines():
            if "GPU KV cache size" in line:
                kv_line = line.strip()
        meta = {
            "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "vllm": "0.10.2",
            "gpu": "Tesla T4 index 0",
            "requests": len(requests),
            "zmq_messages": len(capture.frames),
            "kv_cache_log": kv_line,
            "ready_wait_s": round(time.time() - thread_started, 1),
            "tcpdump": dump is not None,
        }
        (RAW / "run_metadata.json").write_text(json.dumps(meta, indent=2))
        print(json.dumps(meta))
        print("scenarios", Counter(row["scenario"] for row in requests))
    finally:
        if dump is not None:
            stop_proc(dump)
        if capture is not None:
            try:
                capture.stop()
            except Exception:
                pass
        stop_proc(proc)
        if proc._log:  # type: ignore[attr-defined]
            proc._log.close()


def analyze_main() -> None:
    frames = load_frames(RAW / "frames.bin")
    with (RAW / "requests.csv").open() as handle:
        requests = list(csv.DictReader(handle))
    events = event_rows(frames)
    for event in events:
        event["scenario"] = assign_scenario(int(event["recv_ns"]), requests)
    write_csv(RAW / "events.csv", events)
    write_csv(OUT / "native_event_wire_profile.csv", wire_profile(frames, requests))
    transitions, probes = prefix_model(events)
    write_csv(RAW / "prefix_transitions.csv", transitions)
    write_csv(RAW / "prefix_probes.csv", probes)
    write_csv(
        OUT / "prefix_representation_comparison.csv",
        representation_rows(events, frames, transitions, probes),
    )
    print("analyzed", len(frames), "frames", len(transitions), "transitions")


def metrics_text() -> str:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{INDEXER_PORT}/metrics", timeout=2) as handle:
            return handle.read().decode()
    except Exception:
        return ""


def applied_by_label(text: str) -> dict[str, float]:
    out = {}
    for line in text.splitlines():
        if not line.startswith("dynamo_kvrouter_kv_cache_events_applied"):
            continue
        out[line.rsplit("}", 1)[0] + "}"] = float(line.split()[-1])
    return out


def applied_total(text: str) -> float:
    return sum(applied_by_label(text).values())


def start_indexer() -> subprocess.Popen:
    env = os.environ.copy()
    env["LD_PRELOAD"] = "/usr/lib/x86_64-linux-gnu/libstdc++.so.6"
    log = open(RAW / "indexer.log", "w")
    proc = subprocess.Popen(
        [
            str(DYNAMO_PY), "-m", "dynamo.indexer",
            "--block-size", "16",
            "--port", str(INDEXER_PORT),
            "--threads", "4",
            "--workers", f"0={ZMQ_ENDPOINT}",
        ],
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    proc._log = log  # type: ignore[attr-defined]
    deadline = time.time() + 30
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"indexer exited {proc.returncode}")
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{INDEXER_PORT}/health", timeout=1).read()
            time.sleep(0.8)
            return proc
        except Exception:
            time.sleep(0.3)
    raise TimeoutError("indexer health check failed")


def proc_cpu_rss(pid: int) -> tuple[int, int]:
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    utime = int(fields[11])
    stime = int(fields[12])
    rss_pages = int(fields[21])
    return utime + stime, rss_pages * os.sysconf("SC_PAGE_SIZE")


def replay_once(frames, speed: float, seconds: float, query_tokens: list[int] | None) -> dict:
    ctx = zmq.Context.instance()
    pub = ctx.socket(zmq.PUB)
    pub.set_hwm(100_000)
    pub.bind("tcp://*:5557")
    time.sleep(0.8)
    base = frames[0][0]
    span_ns = max(1, frames[-1][0] - base)
    started = time.perf_counter()
    seq = 0
    sent_messages = 0
    sent_events = 0
    hashes = 0
    query_ms = []
    samples = []
    indexer_pid = replay_once.indexer_pid
    cpu0, rss0 = proc_cpu_rss(indexer_pid)
    wall0 = time.perf_counter()
    next_query = started + 0.5
    max_lag = 0.0
    lag_samples = []
    while time.perf_counter() - started < seconds:
        loop_origin = time.perf_counter()
        for recv_ns, topic, _seq, payload in frames:
            target = loop_origin + ((recv_ns - base) / 1e9) / speed
            delay = target - time.perf_counter()
            if delay > 0:
                time.sleep(delay)
            if time.perf_counter() - started >= seconds:
                break
            pub.send_multipart((topic, seq.to_bytes(8, "big"), payload))
            seq += 1
            sent_messages += 1
            batch = decode_payload(payload)
            sent_events += len(batch.events)
            for event in batch.events:
                if isinstance(event, (BlockStored, BlockRemoved)):
                    hashes += len(event.block_hashes)
            now = time.perf_counter()
            if now >= next_query:
                text = metrics_text()
                applied = applied_total(text)
                lag = sent_events - applied
                max_lag = max(max_lag, lag)
                lag_samples.append(lag)
                cpu1, rss1 = proc_cpu_rss(indexer_pid)
                samples.append((now - wall0, applied, cpu1 - cpu0, rss1))
                if query_tokens:
                    q0 = time.perf_counter()
                    body = json.dumps({
                        "token_ids": query_tokens,
                        "model_name": "default",
                        "routing_group": "default",
                    }).encode()
                    req = urllib.request.Request(
                        f"http://127.0.0.1:{INDEXER_PORT}/query",
                        data=body,
                        headers={"Content-Type": "application/json"},
                    )
                    try:
                        with urllib.request.urlopen(req, timeout=2) as handle:
                            body_text = handle.read()
                        query_ms.append((time.perf_counter() - q0) * 1000)
                        replay_once.last_query = body_text[:500]
                    except Exception as exc:
                        query_ms.append(-1)
                        replay_once.last_query = str(exc)[:300]
                next_query = now + 0.5
        if speed >= 16 and (time.perf_counter() - started) < seconds:
            # The schedule can finish a loop early when sends block. Continue.
            continue
    # Drain window. A growing lag that disappears here was queued, not lost.
    time.sleep(2.0)
    text = metrics_text()
    applied = applied_total(text)
    cpu1, rss1 = proc_cpu_rss(indexer_pid)
    elapsed = time.perf_counter() - started
    cpu_s = (cpu1 - cpu0) / os.sysconf("SC_CLK_TCK")
    labels = applied_by_label(text)
    pub.close(linger=0)
    return {
        "sent_messages": sent_messages,
        "sent_events": sent_events,
        "sent_hashes": hashes,
        "applied_events": applied,
        "lag_after_2s": sent_events - applied,
        "max_lag_events": max_lag,
        "elapsed_s": round(elapsed, 3),
        "input_events_per_s": round(sent_events / elapsed, 3),
        "applied_events_per_s": round(applied / elapsed, 3),
        "input_hashes_per_s": round(hashes / elapsed, 3),
        "cpu_seconds": round(cpu_s, 3),
        "rss_bytes": rss1,
        "query_ms_p50": percentile([int(v) for v in query_ms if v >= 0], 0.50),
        "query_ms_p95": percentile([int(v) for v in query_ms if v >= 0], 0.95),
        "query_ms_p99": percentile([int(v) for v in query_ms if v >= 0], 0.99),
        "query_samples": len([v for v in query_ms if v >= 0]),
        "metrics": labels,
        "metrics_text": text,
    }


def first_stored_tokens(frames) -> list[int]:
    """Tokens of the first root BlockStored. Hashing these walks from the index root."""
    for _recv, _topic, _seq, payload in frames:
        batch = decode_payload(payload)
        for event in batch.events:
            if (
                isinstance(event, BlockStored)
                and event.parent_block_hash is None
                and len(event.token_ids) >= 16
            ):
                return list(event.token_ids)
    return []


def collect_dump_hashes() -> set[int]:
    with urllib.request.urlopen(f"http://127.0.0.1:{INDEXER_PORT}/dump", timeout=30) as handle:
        payload = json.loads(handle.read().decode())
    found = set()

    def walk(node):
        if isinstance(node, dict):
            if "block_hash" in node and isinstance(node["block_hash"], int):
                found.add(node["block_hash"])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(payload)
    return found


def reference_alive(frames) -> set[int]:
    alive: set[int] = set()
    for _recv, _topic, _seq, payload in frames:
        batch = decode_payload(payload)
        for event in batch.events:
            if isinstance(event, AllBlocksCleared):
                alive.clear()
            elif isinstance(event, BlockStored):
                for item in event.block_hashes:
                    value = hash_int(item)
                    if value is not None:
                        alive.add(value)
            elif isinstance(event, BlockRemoved):
                for item in event.block_hashes:
                    value = hash_int(item)
                    if value is not None:
                        alive.discard(value)
    return alive


def accuracy_pass(frames) -> dict:
    """Play the capture once, in order, then compare the dump to the event stream."""
    replay_once.speed_note = "one_shot"
    result = replay_once(frames, speed=1.0, seconds=10**9, query_tokens=None)
    # replay_once stops on seconds. Override by a dedicated sender below if the
    # capture is longer than the capacity window. The caller uses play_all.
    return result


def play_all(frames) -> dict:
    ctx = zmq.Context.instance()
    pub = ctx.socket(zmq.PUB)
    pub.set_hwm(100_000)
    pub.bind("tcp://*:5557")
    time.sleep(0.8)
    base = frames[0][0]
    started = time.perf_counter()
    indexer_pid = play_all.indexer_pid
    cpu0, _rss = proc_cpu_rss(indexer_pid)
    seq = 0
    sent_events = 0
    for recv_ns, topic, _seq, payload in frames:
        target = started + (recv_ns - base) / 1e9
        delay = target - time.perf_counter()
        if delay > 0:
            time.sleep(delay)
        pub.send_multipart((topic, seq.to_bytes(8, "big"), payload))
        seq += 1
        sent_events += len(decode_payload(payload).events)
    time.sleep(2.0)
    text = metrics_text()
    cpu1, rss = proc_cpu_rss(indexer_pid)
    elapsed = max(1e-9, time.perf_counter() - started)
    pub.close(linger=0)
    reference = reference_alive(frames)
    dumped = collect_dump_hashes()
    missing = reference - dumped
    extra = dumped - reference
    return {
        "sent_events": sent_events,
        "applied_events": applied_total(text),
        "elapsed_s": round(elapsed, 3),
        "cpu_seconds": round((cpu1 - cpu0) / os.sysconf("SC_CLK_TCK"), 3),
        "rss_bytes": rss,
        "reference_alive_hashes": len(reference),
        "dumped_external_hashes": len(dumped),
        "missing_from_indexer": len(missing),
        "extra_in_indexer": len(extra),
        "metrics_text": text,
    }


def replay_main() -> None:
    frames = load_frames(RAW / "frames.bin")
    if not frames:
        raise RuntimeError("no captured frames")
    tokens = first_stored_tokens(frames)
    # One-shot accuracy at native timing.
    proc = start_indexer()
    try:
        play_all.indexer_pid = proc.pid
        accuracy = play_all(frames)
        (RAW / "accuracy_metrics.txt").write_text(accuracy.pop("metrics_text"))
        (RAW / "indexer_accuracy.json").write_text(json.dumps(accuracy, indent=2))
        print("accuracy", json.dumps(accuracy))
    finally:
        stop_proc(proc)
        if proc._log:  # type: ignore[attr-defined]
            proc._log.close()
        time.sleep(0.5)

    rows = []
    capacity = []
    native_events_per_s = None
    for speed in (1, 4, 16, 64):
        proc = start_indexer()
        try:
            replay_once.indexer_pid = proc.pid
            result = replay_once(frames, float(speed), 40.0, tokens or None)
            text = result.pop("metrics_text")
            (RAW / f"metrics_x{speed}.txt").write_text(text)
            labels = result.pop("metrics")
            if speed == 1:
                native_events_per_s = result["input_events_per_s"]
            lag = result["lag_after_2s"]
            stable = abs(lag) <= max(8, 0.02 * result["sent_events"])
            row = {
                "tier": f"{speed}x",
                "meaning": "offline_accelerated_replay_of_one_t4_capture",
                "window_s": result["elapsed_s"],
                "input_events_per_s": result["input_events_per_s"],
                "applied_events_per_s": result["applied_events_per_s"],
                "input_hashes_per_s": result["input_hashes_per_s"],
                "sent_events": result["sent_events"],
                "applied_events": result["applied_events"],
                "lag_after_2s_drain": lag,
                "max_sampled_lag_events": result["max_lag_events"],
                "stable": "yes" if stable else "no",
                "indexer_cpu_seconds": result["cpu_seconds"],
                "indexer_rss_bytes": result["rss_bytes"],
                "query_ms_p50": result["query_ms_p50"],
                "query_ms_p95": result["query_ms_p95"],
                "query_ms_p99": result["query_ms_p99"],
                "query_samples": result["query_samples"],
                "apply_latency_ms": "NOT_MEASURED",
                "status_counts": json.dumps(labels),
            }
            rows.append(row)
            capacity.append({
                "tier": row["tier"],
                "input_events_per_s": row["input_events_per_s"],
                "applied_events_per_s": row["applied_events_per_s"],
                "lag_after_drain": lag,
                "stable": row["stable"],
                "cpu_seconds": row["indexer_cpu_seconds"],
                "rss_bytes": row["indexer_rss_bytes"],
                "lookup_ms_p95": row["query_ms_p95"],
            })
            print("tier", speed, "stable", row["stable"], "lag", lag, "in", row["input_events_per_s"])
            if not stable:
                break
        finally:
            stop_proc(proc)
            if proc._log:  # type: ignore[attr-defined]
                proc._log.close()
            time.sleep(0.5)
    write_csv(OUT / "indexer_processing_profile.csv", rows)
    write_csv(OUT / "indexer_capacity.csv", capacity)
    print("native_replay_events_per_s", native_events_per_s)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=("capture", "analyze", "replay", "all"))
    args = parser.parse_args()
    if args.phase in {"capture", "all"}:
        capture_main()
    if args.phase in {"analyze", "all"}:
        analyze_main()
    if args.phase in {"replay", "all"}:
        replay_main()


if __name__ == "__main__":
    main()
