#!/usr/bin/env python3
"""Fig. 6(c) live FullSync recordings and paired frozen-relay replays.

Does not change KVSelect, does not retune b02-gateway4t4's HTB, and does not
write into analysis/formal4t4. The container and port 9710 stay up.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import importlib.util
import json
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

import aiohttp

ROOT = Path("/home/byh/B02")
OUT = ROOT / "analysis/fig6_reconstruction/f6_f1_formal_ac"
HARNESS_PATH = ROOT / "experiments/4t4/run_formal4t4.py"
P3_PATH = ROOT / "analysis/fig6_reconstruction/f6_p3/run_ac_readiness.py"
SEEDS = (2026101901, 2026101902, 2026101903)
OVERLAPS = (0.0, 0.25, 0.50, 0.75)
POOL = 32
N_REQUESTS = 48
WARMUP = 12
OUTPUT_TOKENS = 4
CONCURRENCY = 4
ALPHA = 1.2
STEPS = 3
KV_EXPECTED = 104544
TIME_BUDGET_S = 50 * 60
DRAIN_TIMEOUT_S = 60.0


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def code_commit() -> str:
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()


def ambient_link_rate() -> str:
    text = subprocess.check_output(["bash", str(ROOT / "experiments/4t4/net/tc_stats_4t4.sh")], text=True)
    for line in text.splitlines():
        if line.startswith("class htb 1:1 "):
            return line.strip()
    return "unparsed"


class Recorder:
    """Agent and dispatcher endpoints. Does not call cell_rate_4t4.sh."""

    def __init__(self, harness) -> None:
        self.h = harness
        self.cell_id = -1
        self.digest_map: dict[int, str] = {}
        self.dispatcher = None
        self.agent_writers = []
        self.agent_tasks = []
        self.reset_done = asyncio.Event()
        self.down_ready = asyncio.Event()
        self.stats_future = None
        self.stats_box: dict = {}
        self.captured: list[dict] = []
        self.down_writers = []
        self.sent = 0
        self.received = 0
        self.server = None

    async def start(self) -> None:
        ip = self.h.bridge_ip()
        self.server = await asyncio.start_server(self._on_downstream, ip, self.h.DISPATCH_PORT)
        self.agent_writers = []
        self.agent_tasks = []
        for _ in range(4):
            reader, writer = await asyncio.open_connection("127.0.0.1", self.h.RELAY_PORT)
            sock = writer.get_extra_info("socket")
            sock.setsockopt(self.h.socket.IPPROTO_TCP, self.h.socket.TCP_NODELAY, 1)
            self.agent_writers.append(writer)
            self.agent_tasks.append(asyncio.create_task(self._on_agent(reader)))
        deadline = time.perf_counter() + 10
        while time.perf_counter() < deadline and self.server.sockets:
            await asyncio.sleep(0.05)

    async def _on_agent(self, reader) -> None:
        try:
            while True:
                data = await reader.readexactly(self.h.FRAME)
                kind, _instance, cell, seq, coverage, _digest, _sent = self.h.HDR.unpack(data[:32])
                if kind == self.h.K_RESET_DONE and cell == self.cell_id:
                    self.reset_done.set()
                elif kind == self.h.K_STATS and cell == self.cell_id:
                    forwarded, rate, superseded, cap, util, queue_drop, expired, maxq = self.h.STATS.unpack(data[32:64])
                    self.stats_box.update({
                        "forwarded": forwarded, "rate_limit": rate, "superseded": superseded,
                        "replica_cap": cap, "low_utility": util, "queue_drop": queue_drop,
                        "expired": expired, "maxq": maxq, "queued": seq, "received": coverage,
                    })
                elif kind == 9 and cell == self.cell_id:
                    topk, stale, _marked, depth, _entries, _ms, pqueue, _zero = self.h.STATS.unpack(data[32:64])
                    self.stats_box.update({
                        "global_topk_drops": topk, "stale_cell": stale,
                        "live_depth": depth, "pqueue": pqueue,
                    })
                if (
                    self.stats_future is not None and not self.stats_future.done()
                    and "forwarded" in self.stats_box and "stale_cell" in self.stats_box
                ):
                    self.stats_future.set_result(dict(self.stats_box))
        except (asyncio.IncompleteReadError, ConnectionResetError):
            return

    async def _on_downstream(self, reader, writer) -> None:
        self.down_writers.append(writer)
        self.down_ready.set()
        try:
            while True:
                data = await reader.readexactly(self.h.FRAME)
                kind, instance, cell, seq, coverage, digest, t_send = self.h.HDR.unpack(data[:32])
                now = time.time()
                if kind not in (self.h.K_UP, self.h.K_TOMB):
                    continue
                if cell != self.cell_id or self.dispatcher is None:
                    continue
                op = "upsert" if kind == self.h.K_UP else "tombstone"
                self.captured.append({
                    "cell": cell,
                    "owner": instance,
                    "prefix_digest64": digest,
                    "op": op,
                    "stage": "forwarded",
                    "coverage_tokens": coverage,
                    "generation_time": t_send,
                    "dispatcher_recv_time": now,
                    "update_id": seq,
                    "recv_index": len(self.captured),
                    "capture": "FULLSYNC_FIFO_DISPATCHER",
                })
                name = self.digest_map.get(digest)
                if name is not None:
                    if kind == self.h.K_UP:
                        self.dispatcher.apply_upsert(instance, name, coverage, t_send, now)
                    else:
                        self.dispatcher.apply_tombstone(instance, name)
                self.received += 1
                writer.write(self.h.HDR.pack(self.h.K_ACK, 0, cell, seq, 0, 0, now) + b"\x00" * 32)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionResetError):
            pass
        finally:
            writer.close()

    async def configure(self, cell_id: int, digest_map: dict[int, str], dispatcher) -> None:
        relay = load_module("f6f1_relay_cfg", ROOT / "experiments/4t4/net/gateway_relay_4t4.py")
        self.cell_id = cell_id
        self.digest_map = digest_map
        self.dispatcher = dispatcher
        self.sent = 0
        self.received = 0
        self.captured = []
        self.stats_box = {}
        flags = self.h.POLICY_DEFS["FullSync"]
        payload = relay.CFG.pack(
            flags["mode"], flags["merge"], flags["priority"], flags["adaptive"],
            flags["dedup"], 0, self.h.MAX_QUEUE, 4, 0, 1,
        )
        self.agent_writers[0].write(self.h.HDR.pack(self.h.K_CONFIG, 0, cell_id, 0, 0, 0, time.time()) + payload.ljust(32, b"\x00"))
        await self.agent_writers[0].drain()
        self.down_ready.clear()
        for _ in range(3):
            self.reset_done.clear()
            self.agent_writers[0].write(self.h.HDR.pack(self.h.K_RESET, 0, cell_id, 0, 0, 0, time.time()) + b"\x00" * 32)
            await self.agent_writers[0].drain()
            try:
                await asyncio.wait_for(self.reset_done.wait(), timeout=15)
                break
            except asyncio.TimeoutError:
                continue
        else:
            raise RuntimeError("relay did not acknowledge cell reset")
        try:
            await asyncio.wait_for(self.down_ready.wait(), timeout=5)
        except asyncio.TimeoutError:
            raise RuntimeError("dispatcher downstream did not reconnect after reset")

    def send(self, kind: int, instance: int, digest: str, coverage: int) -> None:
        frame = self.h.HDR.pack(
            kind, instance, self.cell_id, self.sent, coverage, self.h.digest64(digest), time.time(),
        ) + b"\x00" * 32
        self.agent_writers[instance].write(frame)
        self.sent += 1

    async def drain(self) -> None:
        deadline = time.perf_counter() + DRAIN_TIMEOUT_S
        quiet_since = time.perf_counter()
        last = self.received
        while time.perf_counter() < deadline:
            await asyncio.sleep(0.1)
            if self.received != last:
                last = self.received
                quiet_since = time.perf_counter()
            elif self.received >= self.sent and time.perf_counter() - quiet_since > 0.5:
                return

    async def fetch_stats(self) -> dict:
        self.stats_box = {}
        self.stats_future = asyncio.get_running_loop().create_future()
        self.agent_writers[0].write(
            self.h.HDR.pack(self.h.K_STATS_REQ, 0, self.cell_id, 0, 0, 0, time.time()) + b"\x00" * 32
        )
        await self.agent_writers[0].drain()
        try:
            return await asyncio.wait_for(self.stats_future, timeout=10)
        except asyncio.TimeoutError:
            return {"error": "stats_timeout", "partial": dict(self.stats_box)}

    async def close(self) -> None:
        for task in self.agent_tasks:
            task.cancel()
        for writer in [*self.agent_writers, *self.down_writers]:
            writer.close()
        if self.server is not None:
            self.server.close()
            try:
                await asyncio.wait_for(self.server.wait_closed(), timeout=2)
            except asyncio.TimeoutError:
                pass


def gateway_events(since_iso: str, cell_id: int) -> list[dict]:
    output = subprocess.check_output(
        ["docker", "logs", "--since", since_iso, "b02-gateway4t4"],
        text=True, stderr=subprocess.STDOUT,
    )
    events = []
    for line in output.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("event") == "update" and int(event.get("cell", -1)) == cell_id:
            events.append(event)
    return events


def write_events(path: Path, events: list[dict]) -> None:
    if not events:
        path.write_text("")
        return
    columns = sorted({key for event in events for key in event})
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(events)


def offered_upserts(events: list[dict]) -> list[dict]:
    rows = []
    for event in events:
        if event.get("op") != "upsert":
            continue
        rows.append({
            "cell": int(event["cell"]),
            "owner": int(event["owner"]),
            "digest": int(event["prefix_digest64"]),
            "coverage": int(event["coverage_tokens"]),
            "arrival": float(event["dispatcher_recv_time"]),
            "generation": float(event["generation_time"]),
            "seq": int(event["update_id"]),
            "recv_index": int(event["recv_index"]),
        })
    rows.sort(key=lambda item: item["recv_index"])
    return rows


def arrival_order_stats(events: list[dict]) -> dict:
    order: dict[int, list[int]] = {}
    for event in events:
        if event.get("op") != "upsert":
            continue
        digest = int(event["prefix_digest64"])
        owner = int(event["owner"])
        bucket = order.setdefault(digest, [])
        if owner not in bucket:
            bucket.append(owner)
    multi = [tuple(owners) for owners in order.values() if len(owners) >= 3]
    first_two_visible = sum(1 for owners in multi if tuple(owners[:2]) == (0, 1))
    return {
        "prefixes": len(order),
        "prefixes_with_3plus_owners": len(multi),
        "first_two_are_0_1": first_two_visible,
        "owner_order_histogram": dict(Counter(tuple(owners[:4]) for owners in multi).most_common(8)),
    }


def live_counts(events: list[dict]) -> dict:
    upserts = [event for event in events if event.get("op") == "upsert"]
    stages = Counter((event.get("stage"), event.get("suppression_reason") or "") for event in upserts)
    enqueued = stages.get(("enqueued", ""), 0)
    forwarded = stages.get(("forwarded", ""), 0)
    other = sum(count for key, count in stages.items() if key[0] == "suppressed")
    return {
        "live_enqueued": enqueued,
        "live_forwarded": forwarded,
        "live_suppressed": other,
        "live_undrained": enqueued - forwarded,
        "live_stage_counts": {f"{stage}:{reason}": count for (stage, reason), count in stages.items()},
        "tombstones": sum(1 for event in events if event.get("op") == "tombstone"),
    }


async def metrics_idle() -> bool:
    import urllib.request
    def one(port: int) -> tuple[float, float]:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=5) as response:
            text = response.read().decode("utf-8", errors="replace")
        running = waiting = None
        for line in text.splitlines():
            if line.startswith("vllm:num_requests_running"):
                running = float(line.split()[-1])
            elif line.startswith("vllm:num_requests_waiting"):
                waiting = float(line.split()[-1])
        if running is None or waiting is None:
            raise RuntimeError(f"missing gauges on {port}")
        return running, waiting
    values = await asyncio.gather(*[asyncio.to_thread(one, 8000 + index) for index in range(4)])
    return all(running == 0 and waiting == 0 for running, waiting in values)


async def wait_idle(timeout_s: float = 40.0) -> bool:
    deadline = time.perf_counter() + timeout_s
    while time.perf_counter() < deadline:
        if await metrics_idle():
            return True
        await asyncio.sleep(0.3)
    return False


def namespace(seed: int, overlap: float) -> argparse.Namespace:
    return argparse.Namespace(
        seed=seed, workload="reuse_intensive", n_requests=N_REQUESTS, pool_size=POOL,
        alpha=ALPHA, steps=STEPS, warmup=WARMUP, overlap=overlap, output_tokens=OUTPUT_TOKENS,
        max_request_attempts=3, concurrency=CONCURRENCY, current_rep=0,
        prefill_tokens_per_ms=50.0, queue_penalty_ms=2.0, guard_ms=0.5,
    )


async def run_cell(harness, session, recorder: Recorder, seed: int, overlap: float, attempt: int, kv_tokens: int) -> dict:
    args = namespace(seed, overlap)
    cell_index = SEEDS.index(seed) * len(OVERLAPS) + OVERLAPS.index(overlap)
    cell_id = 41000 + cell_index
    tag = f"s{seed}_o{overlap:.2f}_a{attempt}"
    trace_path = OUT / "traces" / f"{tag}.csv"
    trace = harness.make_trace(trace_path, 0, args)
    overlap_count = int(round(POOL * overlap))
    phase0 = {request.lineage_id: request.coverage_tokens for request in trace if request.phase == 0}
    seeds = [
        harness.TraceRequest(
            request_id=-(index + 1), phase=0, lineage_id=index, step=0,
            tenant=f"tenant-{index % 8}", digest=f"L{index:04d}",
            coverage_tokens=phase0.get(index, harness.BASE_WORDS),
            workload=args.workload, discard=True,
        )
        for index in range(overlap_count)
    ]
    digest_map = {harness.digest64(request.digest): request.digest for request in [*trace, *seeds]}
    dispatcher = harness.Dispatcher(4, 50.0, 2.0, 0.5)
    shadows = [harness.ShadowCache(kv_tokens) for _ in range(4)]
    cache_salt = f"f6f1:{seed}:{overlap:.2f}"
    started_iso = time.strftime("%Y-%m-%dT%H:%M:%S")
    await recorder.configure(cell_id, digest_map, dispatcher)
    source_upserts = 0
    source_tombstones = 0
    request_failures = 0
    cell_started = time.perf_counter()

    def publish(target: int, digest: str, coverage: int) -> None:
        nonlocal source_upserts, source_tombstones
        evicted = shadows[target].insert(digest, coverage)
        source_upserts += 1
        recorder.send(harness.K_UP, target, digest, coverage)
        for victim in evicted:
            recorder.send(harness.K_TOMB, target, victim, 0)
            source_tombstones += 1

    async with aiohttp.ClientSession() as local:
        for seed_request in seeds:
            responses = await asyncio.gather(*[
                harness.one_request(local, url, harness.prompt_for(seed_request), cache_salt, OUTPUT_TOKENS, 3)
                for url in harness.URLS
            ])
            if not all(response["ok"] for response in responses):
                request_failures += 1
            for target in range(4):
                publish(target, seed_request.digest, seed_request.coverage_tokens)
        for offset in range(0, len(trace), CONCURRENCY):
            wave = trace[offset:offset + CONCURRENCY]
            decisions = []
            for request in wave:
                target, *_rest = dispatcher.choose(request)
                dispatcher.loads[target] += 1
                decisions.append((request, target))
            responses = await asyncio.gather(*[
                harness.one_request(local, harness.URLS[target], harness.prompt_for(request), cache_salt, OUTPUT_TOKENS, 3)
                for request, target in decisions
            ])
            for (request, target), response in zip(decisions, responses):
                dispatcher.loads[target] -= 1
                if not response["ok"]:
                    request_failures += 1
                publish(target, request.digest, request.coverage_tokens)
    await recorder.drain()
    stats = await recorder.fetch_stats()
    events = list(recorder.captured)
    raw_dir = OUT / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    write_events(raw_dir / f"gateway_events_{tag}.csv", events)
    (raw_dir / f"relay_stats_{tag}.json").write_text(json.dumps(stats, indent=2))
    counts = live_counts(events)
    drop_fields = ("rate_limit", "superseded", "replica_cap", "low_utility", "queue_drop", "expired", "global_topk_drops", "stale_cell")
    relay_drops = sum(int(stats.get(name, 0) or 0) for name in drop_fields) if "error" not in stats else -1
    counts["live_suppressed"] = relay_drops
    counts["live_undrained"] = int(stats.get("queued", -1) or 0) + int(stats.get("live_depth", 0) or 0)
    counts["relay_received"] = stats.get("received")
    counts["relay_forwarded"] = stats.get("forwarded")
    counts["live_forwarded"] = sum(1 for event in events if event.get("op") == "upsert")
    counts["live_enqueued"] = counts["live_forwarded"] if relay_drops == 0 else ""
    counts["tombstones"] = sum(1 for event in events if event.get("op") == "tombstone")
    stream = offered_upserts(events)
    expected_upserts = N_REQUESTS + 4 * overlap_count
    return {
        "tag": tag,
        "seed": seed,
        "overlap": overlap,
        "attempt": attempt,
        "cell_id": cell_id,
        "cache_salt": cache_salt,
        "code_commit": code_commit(),
        "kv_cache_tokens_shadow": kv_tokens,
        "overlap_seed_prefixes": overlap_count,
        "expected_upserts": expected_upserts,
        "source_upserts": source_upserts,
        "source_tombstones": source_tombstones,
        "request_failures": request_failures,
        "link_sent": recorder.sent,
        "link_received": recorder.received,
        "relay_stats": stats,
        "capture": "FULLSYNC_FIFO_DISPATCHER",
        "gateway_update_log": "NOT_EMITTED_RELAY_QUIET",
        "cell_active_s": time.perf_counter() - cell_started,
        "gateway_event_count": len(events),
        "offered_stream": len(stream),
        "arrival": arrival_order_stats(events),
        **counts,
        "events_path": str((raw_dir / f"gateway_events_{tag}.csv").relative_to(ROOT)),
        "trace_path": str(trace_path.relative_to(ROOT)),
        "stream": stream,
    }


def paired_replay(p3, relay_mod, stream: list[dict]) -> dict:
    arms = {}
    for name, dedup in (("no_suppression", 0), ("krep2", 2)):
        result = p3.replay_cap(relay_mod, stream, dedup)
        owners = result["admitted_owners"]
        digests = {event["digest"] for event in stream}
        kept = {digest: sorted(owners.get(digest, ())) for digest in digests}
        not_zero_one = sum(1 for group in kept.values() if len(group) >= 2 and tuple(group[:2]) != (0, 1) and set(group) != {0, 1})
        arms[name] = {
            "offered": result["offered"],
            "forwarded": result["forwarded"],
            "replica_cap_drops": result["replica_cap"],
            "other_drops": result["other_drops"],
            "replay_undrained": result["window_end_unforwarded"] + result.get("undrained_after_drain", 0),
            "admitted_owner_pairs": result["admitted_pairs"],
            "mean_visible_owners": (
                sum(len(owners.get(digest, ())) for digest in digests) / len(digests)
            ) if digests else None,
            "kept_not_exactly_0_and_1": not_zero_one,
        }
    base = arms["no_suppression"]
    cap = arms["krep2"]
    gap = base["forwarded"] - cap["forwarded"]
    valid = (
        gap == cap["replica_cap_drops"]
        and cap["other_drops"] == 0
        and base["other_drops"] == 0
        and cap["replay_undrained"] == 0
        and base["replay_undrained"] == 0
        and base["offered"] == cap["offered"]
        and base["offered"] == len(stream)
    )
    reduction = (1.0 - cap["forwarded"] / base["forwarded"]) if base["forwarded"] else None
    return {"arms": arms, "valid": valid, "reduction": reduction, "gap_minus_drops": gap - cap["replica_cap_drops"]}


def multiplicity(stream: list[dict]) -> dict:
    owners: dict[int, set[int]] = {}
    ads: Counter = Counter()
    for event in stream:
        owners.setdefault(event["digest"], set()).add(event["owner"])
        ads[event["digest"]] += 1
    hist = Counter(len(group) for group in owners.values())
    return {
        "distinct_prefixes": len(owners),
        "prefixes_with_owners_gt_2": sum(count for width, count in hist.items() if width > 2),
        "multiplicity": ";".join(f"{width}:{hist[width]}" for width in sorted(hist)),
        "owner_pairs": sum(len(group) for group in owners.values()),
        "advertisement_events": len(stream),
    }


def append_csv(path: Path, row: dict) -> None:
    exists = path.exists() and path.stat().st_size > 0
    with path.open("a", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(row.keys()))
        if not exists:
            writer.writeheader()
        writer.writerow(row)


async def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "traces").mkdir(exist_ok=True)
    harness = load_module("f6f1_harness", HARNESS_PATH)
    p3 = load_module("f6f1_p3", P3_PATH)
    relay_mod = p3.load_relay()
    commit = code_commit()
    link_rate = ambient_link_rate()
    config = {
        "code_commit": commit,
        "seeds": SEEDS,
        "overlaps": OVERLAPS,
        "pool_size": POOL,
        "n_requests": N_REQUESTS,
        "warmup": WARMUP,
        "output_tokens": OUTPUT_TOKENS,
        "workload": "reuse_intensive",
        "workers": 4,
        "mechanisms_off": "merge,priority,adaptive,topk,replica_cap",
                    "htb": "not retuned",
        "ambient_class_1_1": link_rate,
        "relay": "existing b02-gateway4t4 on 127.0.0.1:9710",
        "gateway_per_update_log": "NOT_EMITTED_RELAY_QUIET",
        "offered_capture": "FULLSYNC_FIFO_DISPATCHER plus relay K_STATS received/forwarded/drops",
        "evidence": "LIVE_VLLM_OFFERED_TRACE + PAIRED_FROZEN_RELAY_REPLAY",
    }
    (OUT / "run_config.json").write_text(json.dumps(config, indent=2))
    log_path = OUT / "server_logs" / "vllm_0.log"
    kv = KV_EXPECTED
    if log_path.exists():
        for line in log_path.read_text(errors="replace").splitlines():
            if "GPU KV cache size:" in line:
                kv = int(line.split("GPU KV cache size:", 1)[1].split("tokens", 1)[0].strip().replace(",", ""))
    if kv != KV_EXPECTED:
        (OUT / "kv_mismatch.json").write_text(json.dumps({"parsed": kv, "expected": KV_EXPECTED}))
        print(json.dumps({"stop": "kv cache size differs from the audited 104544", "parsed": kv}))
        return 2
    recorder = Recorder(harness)
    await recorder.start()
    started = time.perf_counter()
    failures = []
    completed = []
    try:
        for seed in SEEDS:
            for overlap in OVERLAPS:
                if time.perf_counter() - started > TIME_BUDGET_S:
                    failures.append({"seed": seed, "overlap": overlap, "status": "stopped_budget"})
                    raise RuntimeError("time budget")
                if not await wait_idle(30):
                    failures.append({"seed": seed, "overlap": overlap, "status": "not_idle"})
                    raise RuntimeError("engine not idle")
                cell = None
                for attempt in (1, 2):
                    print(json.dumps({"start_cell": True, "seed": seed, "overlap": overlap, "attempt": attempt}), flush=True)
                    try:
                        cell = await run_cell(harness, None, recorder, seed, overlap, attempt, kv)
                    except Exception as exc:
                        failures.append({"seed": seed, "overlap": overlap, "attempt": attempt, "error": repr(exc)[:400]})
                        (OUT / "failures.json").write_text(json.dumps(failures, indent=2))
                        cell = None
                        continue
                    accounting_ok = (
                        cell["request_failures"] == 0
                        and cell["offered_stream"] == cell["expected_upserts"]
                        and cell["offered_stream"] == cell["source_upserts"]
                        and cell["link_received"] == cell["link_sent"]
                        and cell["relay_stats"].get("received") == cell["link_sent"]
                        and cell["relay_stats"].get("forwarded") == cell["link_sent"]
                        and cell["live_suppressed"] == 0
                        and cell["live_undrained"] == 0
                    )
                    if not accounting_ok:
                        failures.append({
                            "seed": seed, "overlap": overlap, "attempt": attempt,
                            "status": "stream_mismatch_or_request_failure",
                            "request_failures": cell["request_failures"],
                            "offered": cell["offered_stream"],
                            "expected": cell["expected_upserts"],
                            "source_upserts": cell["source_upserts"],
                            "link_sent": cell["link_sent"],
                            "link_received": cell["link_received"],
                            "relay_stats": cell["relay_stats"],
                            "live_suppressed": cell["live_suppressed"],
                            "live_undrained": cell["live_undrained"],
                        })
                        (OUT / "failures.json").write_text(json.dumps(failures, indent=2))
                        if attempt == 2:
                            cell = None
                        continue
                    break
                if cell is None:
                    raise RuntimeError(f"cell failed seed={seed} overlap={overlap}")
                replay = paired_replay(p3, relay_mod, cell["stream"])
                multi = multiplicity(cell["stream"])
                status = "VALID" if replay["valid"] else "INVALID_ACCOUNTING"
                record = {
                    "seed": seed,
                    "overlap": overlap,
                    "attempt": cell["attempt"],
                    "status": status,
                    "cell_id": cell["cell_id"],
                    "cache_salt": cell["cache_salt"],
                    "code_commit": cell["code_commit"],
                    "offered_advertisements": cell["offered_stream"],
                    "expected_upserts": cell["expected_upserts"],
                    "source_upserts": cell["source_upserts"],
                    "source_tombstones": cell["source_tombstones"],
                    "live_enqueued": cell["live_enqueued"],
                    "live_forwarded": cell["live_forwarded"],
                    "live_undrained": cell["live_undrained"],
                    "live_suppressed": cell["live_suppressed"],
                    "link_sent": cell["link_sent"],
                    "link_received": cell["link_received"],
                    "relay_received": cell["relay_stats"].get("received"),
                    "relay_forwarded": cell["relay_stats"].get("forwarded"),
                    "capture": cell["capture"],
                    "cell_active_s": f"{cell['cell_active_s']:.3f}",
                    "distinct_prefixes": multi["distinct_prefixes"],
                    "prefixes_with_owners_gt_2": multi["prefixes_with_owners_gt_2"],
                    "multiplicity": multi["multiplicity"],
                    "owner_pairs": multi["owner_pairs"],
                    "forwarded_no_suppression": replay["arms"]["no_suppression"]["forwarded"],
                    "forwarded_krep2": replay["arms"]["krep2"]["forwarded"],
                    "replica_cap_drops": replay["arms"]["krep2"]["replica_cap_drops"],
                    "other_drops": replay["arms"]["krep2"]["other_drops"],
                    "replay_undrained": replay["arms"]["krep2"]["replay_undrained"],
                    "gap_minus_drops": replay["gap_minus_drops"],
                    "reduction": f"{replay['reduction']:.6f}" if replay["reduction"] is not None else "",
                    "suppressed_owner_pairs": multi["owner_pairs"] - replay["arms"]["krep2"]["admitted_owner_pairs"],
                    "mean_visible_owners_krep2": f"{replay['arms']['krep2']['mean_visible_owners']:.6f}",
                    "kept_sets_not_0_and_1": replay["arms"]["krep2"]["kept_not_exactly_0_and_1"],
                    "prefixes_3plus_first_two_are_0_1": cell["arrival"]["first_two_are_0_1"],
                    "prefixes_with_3plus_owners_arrival": cell["arrival"]["prefixes_with_3plus_owners"],
                    "events_path": cell["events_path"],
                    "evidence": "LIVE_VLLM_OFFERED_TRACE+PAIRED_FROZEN_RELAY_REPLAY",
                }
                append_csv(OUT / "f6_c_per_cell.csv", record)
                for arm_name in ("no_suppression", "krep2"):
                    arm = replay["arms"][arm_name]
                    append_csv(OUT / "f6_c_per_arm.csv", {
                        "seed": seed,
                        "overlap": overlap,
                        "strategy": arm_name,
                        "status": status,
                        "offered_advertisements": arm["offered"],
                        "forwarded": arm["forwarded"],
                        "replica_cap_drops": arm["replica_cap_drops"],
                        "other_drops": arm["other_drops"],
                        "replay_undrained": arm["replay_undrained"],
                        "admitted_owner_pairs": arm["admitted_owner_pairs"],
                        "mean_visible_owners": f"{arm['mean_visible_owners']:.6f}",
                        "payload_bytes": arm["forwarded"] * 64,
                        "wire_bytes_convention": arm["forwarded"] * 104,
                        "evidence": "PAIRED_FROZEN_RELAY_REPLAY",
                        "live_evidence": "LIVE_VLLM_OFFERED_TRACE",
                        "events_path": cell["events_path"],
                        "code_commit": commit,
                    })
                completed.append(record)
                (OUT / "cell_progress.json").write_text(json.dumps({
                    "completed": completed, "failures": failures,
                    "elapsed_s": time.perf_counter() - started,
                }, indent=2))
                print(json.dumps({
                    "seed": seed, "overlap": overlap, "status": status,
                    "offered": record["offered_advertisements"],
                    "fwd0": record["forwarded_no_suppression"],
                    "fwd2": record["forwarded_krep2"],
                    "drops": record["replica_cap_drops"],
                    "reduction": record["reduction"],
                    "live_undrained": record["live_undrained"],
                    "active_s": record["cell_active_s"],
                }), flush=True)
                if status != "VALID":
                    raise RuntimeError("invalid accounting")
    finally:
        await recorder.close()
    (OUT / "failures.json").write_text(json.dumps(failures, indent=2))
    print(json.dumps({"completed": len(completed), "failures": len(failures), "elapsed_s": time.perf_counter() - started}))
    return 0 if len(completed) == 12 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
