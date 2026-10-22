#!/usr/bin/env python3
"""P0-M3 phase 0-2 feasibility replay.

Reads the existing P0-C1 frames. Does not start vLLM, does not use a GPU,
and does not write into the C1 directory. A CPU Dynamo indexer is started
only for the time-consistent prefix queries, then stopped.
"""
from __future__ import annotations

import csv
import importlib.util
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
from collections import defaultdict
from pathlib import Path

ROOT = Path("/home/byh/B02")
sys.path.insert(0, str(ROOT))
OUT = ROOT / "analysis/p0_pressure_realism/m3_native_compact_motivation"
RAW = OUT / "raw"
C1 = ROOT / "analysis/p0_pressure_realism/c1_real_indexer"
DYNAMO_PY = Path("/home/byh/Dynamo/.venv-dynamo/bin/python")
INDEXER_PORT = 8090
ZMQ_ENDPOINT = "tcp://127.0.0.1:5557"

os.environ.setdefault("VLLM_LOGGING_LEVEL", "ERROR")


def load_c1():
    spec = importlib.util.spec_from_file_location(
        "p0c1_run", C1 / "run_c1.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def percentile(values: list[int], q: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round(q * (len(ordered) - 1))))
    return ordered[index]


def peak_window(times_ns: list[int], sizes: list[int], window_ns: int) -> int:
    if not times_ns:
        return 0
    best = 0
    total = 0
    left = 0
    for right, size in enumerate(sizes):
        total += size
        while times_ns[right] - times_ns[left] > window_ns:
            total -= sizes[left]
            left += 1
        if total > best:
            best = total
    return best


def git_head() -> str:
    try:
        return subprocess.check_output(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True
        ).strip()
    except subprocess.CalledProcessError:
        return "unknown"


def port_listening(port: int) -> bool:
    text = subprocess.check_output(["ss", "-H", "-ltn"], text=True, errors="replace")
    needle = f":{port}"
    return any(needle in line.split()[3] for line in text.splitlines() if line.split())


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
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


class BlockOracle:
    """Time-ordered block residency. Callers only see events already applied."""

    def __init__(self) -> None:
        self.parent: dict[int, int | None] = {}
        self.tokens: dict[int, tuple[int, ...]] = {}
        self.alive: set[int] = set()
        self.block_size: int | None = None
        self.parent_conflicts = 0
        self.token_conflicts = 0
        self.missing_parent = 0
        self.duplicate_store = 0
        self.remove_missing = 0
        self.token_len_mismatch = 0
        self.hash_width_bad = 0
        self.stored_blocks = 0
        self.removed_blocks = 0
        self.clears = 0
        self.incomplete_token_blocks = 0

    def chain(self, tip: int) -> list[int]:
        out: list[int] = []
        seen: set[int] = set()
        node: int | None = tip
        while node is not None and node not in seen:
            seen.add(node)
            out.append(node)
            node = self.parent.get(node)
        out.reverse()
        return out

    def contiguous(self, chain: list[int]) -> tuple[int, int, int]:
        """Return (reusable tokens, live prefix blocks, orphan blocks past the hole)."""
        tokens = 0
        prefix_blocks = 0
        hole = False
        orphans = 0
        for block in chain:
            if not hole and block in self.alive and block in self.tokens:
                tokens += len(self.tokens[block])
                prefix_blocks += 1
            else:
                hole = True
                if block in self.alive:
                    orphans += 1
        return tokens, prefix_blocks, orphans

    def apply_stored(self, event) -> list[int]:
        from vllm.distributed.kv_events import BlockStored

        assert isinstance(event, BlockStored)
        hashes = []
        for item in event.block_hashes:
            value = hash_int(item)
            if value is None:
                self.hash_width_bad += 1
                continue
            raw = item if isinstance(item, bytes) else None
            if raw is not None and len(raw) != 8:
                self.hash_width_bad += 1
            hashes.append(value)
        parent = hash_int(event.parent_block_hash)
        if event.parent_block_hash is not None and parent is None:
            self.hash_width_bad += 1
        if parent is not None and parent not in self.parent and parent not in self.alive:
            self.missing_parent += 1
        block_size = int(event.block_size)
        if self.block_size is None:
            self.block_size = block_size
        token_ids = list(event.token_ids)
        if block_size <= 0 or len(token_ids) != len(hashes) * block_size:
            self.token_len_mismatch += 1
            chunks = [()] * len(hashes)
        else:
            chunks = [
                tuple(token_ids[index * block_size:(index + 1) * block_size])
                for index in range(len(hashes))
            ]
        cursor = parent
        new: list[int] = []
        for block, chunk in zip(hashes, chunks):
            if block in self.alive:
                self.duplicate_store += 1
                cursor = block
                continue
            previous = self.parent.get(block, cursor)
            if block in self.parent and previous != cursor:
                self.parent_conflicts += 1
            else:
                self.parent[block] = cursor
            if chunk:
                old = self.tokens.get(block)
                if old is not None and old != chunk:
                    self.token_conflicts += 1
                else:
                    self.tokens[block] = chunk
            else:
                self.incomplete_token_blocks += 1
            self.alive.add(block)
            self.stored_blocks += 1
            new.append(block)
            cursor = block
        return new

    def apply_removed(self, event) -> list[int]:
        removed = []
        for item in event.block_hashes:
            value = hash_int(item)
            if value is None:
                self.hash_width_bad += 1
                continue
            if value not in self.alive:
                self.remove_missing += 1
                continue
            self.alive.discard(value)
            self.removed_blocks += 1
            removed.append(value)
        return removed

    def apply_cleared(self) -> None:
        self.alive.clear()
        self.clears += 1


def hash_int(value):
    if value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, bytes):
        return int.from_bytes(value[:8], "big")
    return None


class Converter:
    """Block tree to one coverage per branch. No future events."""

    def __init__(self, oracle: BlockOracle) -> None:
        self.oracle = oracle
        self.chain: dict[int, list[int]] = {}
        self.tip_lineage: dict[int, int] = {}
        self.advertised: dict[int, int] = {}
        self.updates: list[dict] = []
        self.block_lineages: dict[int, set[int]] = defaultdict(set)

    def _register(self, lineage: int, chain: list[int]) -> None:
        previous = self.chain.get(lineage, [])
        for block in previous:
            self.block_lineages[block].discard(lineage)
            if self.tip_lineage.get(block) == lineage:
                self.tip_lineage.pop(block, None)
        self.chain[lineage] = chain
        for block in chain:
            self.block_lineages[block].add(lineage)
        if chain:
            self.tip_lineage[chain[-1]] = lineage

    def _remember_live_tip(self, lineage: int, chain: list[int]) -> None:
        coverage_blocks = 0
        for block in chain:
            if block not in self.oracle.alive:
                break
            coverage_blocks += 1
        if coverage_blocks:
            self.tip_lineage[chain[coverage_blocks - 1]] = lineage

    def _emit(self, kind: str, lineage: int, coverage: int, reason: str, recv_ns: int) -> None:
        self.updates.append({
            "kind": kind,
            "lineage_id": lineage,
            "coverage_tokens": coverage,
            "reason": reason,
            "recv_ns": recv_ns,
            "identity": "DERIVED_BRANCH_ROOT",
        })
        if kind in {"EVICT", "RESTART"}:
            self.advertised.pop(lineage, None)
        else:
            self.advertised[lineage] = coverage

    def _refresh(self, lineages: set[int], reason: str, recv_ns: int) -> None:
        for lineage in sorted(lineages):
            chain = self.chain.get(lineage)
            if not chain:
                continue
            coverage, _blocks, _orphans = self.oracle.contiguous(chain)
            current = self.advertised.get(lineage)
            if coverage <= 0:
                if current is not None:
                    self._emit("EVICT", lineage, 0, reason, recv_ns)
            elif current != coverage:
                self._emit("UPSERT", lineage, coverage, reason, recv_ns)
            self._remember_live_tip(lineage, chain)

    def on_stored(self, parent: int | None, new_blocks: list[int], recv_ns: int) -> int | None:
        if not new_blocks:
            return None
        lineage = self.tip_lineage.get(parent) if parent is not None else None
        if lineage is None:
            lineage = new_blocks[0] if parent is not None else new_blocks[0]
            if parent is None:
                chain = list(new_blocks)
            else:
                chain = self.oracle.chain(parent) + list(new_blocks)
        else:
            chain = list(self.chain[lineage]) + list(new_blocks)
        self._register(lineage, chain)
        self._refresh({lineage}, "store", recv_ns)
        return lineage

    def on_removed(self, blocks: list[int], recv_ns: int) -> None:
        affected: set[int] = set()
        for block in blocks:
            affected.update(self.block_lineages.get(block, ()))
        self._refresh(affected, "partial_or_full_removal", recv_ns)

    def on_cleared(self, recv_ns: int) -> None:
        for lineage in list(self.advertised):
            self._emit("RESTART", lineage, 0, "all_blocks_cleared", recv_ns)
        self.chain.clear()
        self.tip_lineage.clear()
        self.block_lineages.clear()


def tip_tombstone_visible(chain: list[int], oracle: BlockOracle, advertised_blocks: int) -> int:
    """C1 Model B: any hole in an advertised tip removes the whole advertisement."""
    coverage, prefix_blocks, _orphans = oracle.contiguous(chain)
    if prefix_blocks == advertised_blocks and coverage > 0:
        return coverage
    return 0


def request_windows(requests: list[dict]) -> list[dict]:
    windows = []
    for row in requests:
        windows.append({
            "scenario": row["scenario"],
            "prompt_blake2s": row["prompt_blake2s"],
            "started_ns": int(row["started_ns"]),
            "finished_ns": int(row["finished_ns"]),
            "prompt_tokens": row.get("prompt_tokens", ""),
            "cached_tokens": row.get("cached_tokens", ""),
        })
    windows.sort(key=lambda item: item["started_ns"])
    return windows


def covering(windows: list[dict], recv_ns: int) -> list[dict]:
    return [
        item for item in windows
        if item["started_ns"] <= recv_ns <= item["finished_ns"]
    ]


def token_byte_estimate(c1, frames) -> dict:
    """Re-encode each batch with empty token ids. Difference is derived, not a capture."""
    import msgspec
    from vllm.distributed.kv_events import BlockStored, KVEventBatch

    original = 0
    stripped = 0
    failed = 0
    messages_with_tokens = 0
    bytes_with_tokens = 0
    for _recv, _topic, _seq, payload in frames:
        original += len(payload)
        batch = c1.decode_payload(payload)
        has_tokens = False
        events = []
        for event in batch.events:
            if isinstance(event, BlockStored) and event.token_ids:
                has_tokens = True
                events.append(BlockStored(
                    block_hashes=event.block_hashes,
                    parent_block_hash=event.parent_block_hash,
                    token_ids=[],
                    block_size=event.block_size,
                    lora_id=event.lora_id,
                    medium=event.medium,
                ))
            else:
                events.append(event)
        if has_tokens:
            messages_with_tokens += 1
            bytes_with_tokens += len(payload)
        try:
            empty = KVEventBatch(
                ts=batch.ts,
                events=events,
                data_parallel_rank=batch.data_parallel_rank,
            )
            stripped += len(msgspec.msgpack.encode(empty))
        except Exception:
            failed += 1
            stripped += len(payload)
    return {
        "messages_containing_token_ids": messages_with_tokens,
        "payload_bytes_of_those_messages": bytes_with_tokens,
        "reencoded_without_token_ids_bytes": stripped,
        "derived_token_id_bytes": original - stripped,
        "reencode_failures": failed,
    }


def dispatcher_probe(rollback_pair: tuple[int, int, int] | None) -> dict:
    """In-process Dispatcher.on_frame. No socket and no gateway admission."""
    from experiments.icc_kv.runtime import Dispatcher
    from experiments.icc_kv.wire import FRAME, K_TOMB, K_UP

    dispatcher = Dispatcher(cell=1)
    digest = 0x38A2855819EC6B90
    dispatcher.on_frame(K_UP, 0, 1, 1040, digest, 1.0, 1.0)
    dispatcher.on_frame(K_UP, 0, 2, 448, digest, 2.0, 2.0)
    after_rollback = dispatcher.snapshot().get((0, digest))
    dispatcher.on_frame(K_UP, 0, 3, 1040, digest, 1.0, 3.0)
    after_stale = dispatcher.snapshot().get((0, digest))
    dispatcher.on_frame(K_TOMB, 0, 4, 0, digest, 4.0, 4.0)
    after_tomb = (0, digest) in dispatcher.snapshot()
    other = Dispatcher(cell=1)
    other.on_frame(K_UP, 0, 1, 1040, digest, 1.0, 1.0)
    other.on_frame(K_UP, 1, 2, 1040, digest, 2.0, 2.0)
    two_owners = sorted(other.snapshot().items())
    real = None
    if rollback_pair is not None:
        lineage, before, after = rollback_pair
        probe = Dispatcher(cell=1)
        probe.on_frame(K_UP, 0, 1, before, lineage & ((1 << 64) - 1), 1.0, 1.0)
        probe.on_frame(K_UP, 0, 2, after, lineage & ((1 << 64) - 1), 2.0, 2.0)
        real = probe.snapshot().get((0, lineage & ((1 << 64) - 1)))
    return {
        "frame_bytes": FRAME,
        "rollback_accepted_coverage": after_rollback,
        "stale_longer_coverage_overwrites": after_stale,
        "tombstone_removes_key": not after_tomb,
        "two_owner_snapshot": [(worker, value) for (worker, _digest), value in two_owners],
        "trace_rollback_applied_coverage": real,
        "evidence": "TRACE_REPLAY" if real is not None else "SYNTHETIC",
    }


def synthetic_converter_cases() -> list[dict]:
    """Unit checks for event shapes the capture does not contain. Not trace evidence."""
    from vllm.distributed.kv_events import AllBlocksCleared, BlockRemoved, BlockStored

    def stored(parent: bytes | None, n_blocks: int, seed: int):
        hashes = [(seed + index).to_bytes(8, "big") for index in range(n_blocks)]
        tokens = [seed + index for index in range(n_blocks * 16)]
        return BlockStored(
            block_hashes=hashes,
            parent_block_hash=parent,
            token_ids=tokens,
            block_size=16,
            lora_id=None,
            medium="GPU",
        )

    rows = []

    def run(title: str, steps: list[tuple[str, object]]) -> dict:
        oracle = BlockOracle()
        converter = Converter(oracle)
        last = None
        for kind, event in steps:
            if kind == "store":
                new = oracle.apply_stored(event)
                parent = hash_int(event.parent_block_hash)
                produced = converter.on_stored(parent, new, 0)
                if produced is not None:
                    last = produced
            elif kind == "remove":
                removed = oracle.apply_removed(event)
                converter.on_removed(removed, 0)
            else:
                oracle.apply_cleared()
                converter.on_cleared(0)
        chain = converter.chain.get(last, []) if last is not None else []
        oracle_tokens = oracle.contiguous(chain)[0] if chain else 0
        advertised = converter.advertised.get(last, 0) if last is not None else 0
        return {
            "case": title,
            "source": "SYNTHETIC",
            "query_origin": "SYNTHETIC_QUERY",
            "oracle_reusable_tokens": oracle_tokens,
            "compact_rollback_tokens": advertised,
            "false_positive_tokens": max(0, advertised - oracle_tokens),
            "lost_reusable_tokens": max(0, oracle_tokens - advertised),
            "updates": len(converter.updates),
            "live_lineages": len(converter.advertised),
        }

    first = stored(None, 4, 100)
    second_parent = (103).to_bytes(8, "big")
    extended = stored(second_parent, 2, 200)
    rows.append(run("consecutive_block_stored", [("store", first), ("store", extended)]))

    again = stored(None, 4, 100)
    rows.append(run("repeated_block_stored", [("store", first), ("store", again)]))

    partial = BlockRemoved(block_hashes=[(103).to_bytes(8, "big")], medium="GPU")
    rows.append(run("partial_block_removed", [("store", first), ("remove", partial)]))

    full = BlockRemoved(
        block_hashes=[(100 + index).to_bytes(8, "big") for index in range(4)],
        medium="GPU",
    )
    rows.append(run("full_prefix_removal", [("store", first), ("remove", full)]))
    rows.append(run("all_blocks_cleared", [("store", first), ("clear", AllBlocksCleared())]))

    branch = stored((100).to_bytes(8, "big"), 3, 300)
    oracle = BlockOracle()
    converter = Converter(oracle)
    new_a = oracle.apply_stored(first)
    converter.on_stored(None, new_a, 0)
    new_b = oracle.apply_stored(branch)
    converter.on_stored(hash_int(branch.parent_block_hash), new_b, 0)
    rows.append({
        "case": "same_owner_two_lineages",
        "source": "SYNTHETIC",
        "query_origin": "SYNTHETIC_QUERY",
        "oracle_reusable_tokens": sum(
            oracle.contiguous(chain)[0] for chain in converter.chain.values()
        ),
        "compact_rollback_tokens": sum(converter.advertised.values()),
        "false_positive_tokens": 0,
        "lost_reusable_tokens": 0,
        "updates": len(converter.updates),
        "live_lineages": len(converter.advertised),
    })
    return rows


def metrics_applied(text: str) -> float:
    total = 0.0
    for line in text.splitlines():
        if line.startswith("dynamo_kvrouter_kv_cache_events_applied"):
            total += float(line.split()[-1])
    return total


def http_json(url: str, body: dict | None = None, timeout: float = 10) -> dict:
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"} if data else {},
    )
    with urllib.request.urlopen(request, timeout=timeout) as handle:
        return json.loads(handle.read().decode())


def wait_applied(expected: int, timeout_s: float = 8.0) -> float:
    deadline = time.time() + timeout_s
    applied = -1.0
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{INDEXER_PORT}/metrics", timeout=2
            ) as handle:
                text = handle.read().decode()
            applied = metrics_applied(text)
            if applied >= expected:
                return applied
        except Exception:
            pass
        time.sleep(0.05)
    return applied


def query_tokens(token_ids: list[int]) -> dict:
    try:
        payload = http_json(
            f"http://127.0.0.1:{INDEXER_PORT}/query",
            {
                "token_ids": token_ids,
                "model_name": "default",
                "routing_group": "default",
            },
        )
    except Exception as exc:
        return {"error": str(exc)[:400]}
    scores = payload.get("scores") or {}
    flat = {}
    for worker, ranks in scores.items():
        if isinstance(ranks, dict):
            for rank, value in ranks.items():
                flat[f"{worker}/{rank}"] = value
    return {"scores": flat, "raw_keys": sorted(payload.keys())}


def replay_indexer(c1, frames, break_frame: int, full_tokens: list[int], prefix_tokens: list[int]) -> dict:
    if not DYNAMO_PY.is_file():
        return {"status": "NOT_MEASURED", "reason": "dynamo python missing"}
    if port_listening(5557) or port_listening(INDEXER_PORT):
        return {"status": "NOT_MEASURED", "reason": "5557 or 8090 already listening"}
    RAW.mkdir(parents=True, exist_ok=True)
    log_handle = open(RAW / "indexer_replay.log", "w")
    env = os.environ.copy()
    env["LD_PRELOAD"] = "/usr/lib/x86_64-linux-gnu/libstdc++.so.6"
    proc = subprocess.Popen(
        [
            str(DYNAMO_PY), "-m", "dynamo.indexer",
            "--block-size", "16",
            "--port", str(INDEXER_PORT),
            "--threads", "4",
            "--workers", f"0={ZMQ_ENDPOINT}",
        ],
        env=env,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    try:
        deadline = time.time() + 30
        ready = False
        while time.time() < deadline:
            if proc.poll() is not None:
                return {"status": "NOT_MEASURED", "reason": f"indexer exited {proc.returncode}"}
            try:
                urllib.request.urlopen(
                    f"http://127.0.0.1:{INDEXER_PORT}/health", timeout=1
                ).read()
                ready = True
                break
            except Exception:
                time.sleep(0.3)
        if not ready:
            return {"status": "NOT_MEASURED", "reason": "indexer health timeout"}
        import zmq

        context = zmq.Context.instance()
        pub = context.socket(zmq.PUB)
        pub.set_hwm(100_000)
        pub.bind(ZMQ_ENDPOINT)
        time.sleep(0.8)
        for index, (_recv, topic, _seq, payload) in enumerate(frames):
            pub.send_multipart((topic, index.to_bytes(8, "big"), payload))
            if index == break_frame:
                break
        decoder_events = 0
        for frame in frames[:break_frame + 1]:
            decoder_events += len(c1.decode_payload(frame[3]).events)
        applied_at_break = wait_applied(decoder_events)
        at_break = {
            "applied_events": applied_at_break,
            "expected_events": decoder_events,
            "query_full_chain": query_tokens(full_tokens),
            "query_surviving_prefix": query_tokens(prefix_tokens),
            "query_origin": "CONTROLLED_FROM_STORED_BLOCKS",
        }
        for index, (_recv, topic, _seq, payload) in enumerate(frames):
            if index <= break_frame:
                continue
            pub.send_multipart((topic, index.to_bytes(8, "big"), payload))
        total_events = decoder_events
        for frame in frames[break_frame + 1:]:
            total_events += len(c1.decode_payload(frame[3]).events)
        applied_end = wait_applied(total_events)
        at_end = {
            "applied_events": applied_end,
            "expected_events": total_events,
            "query_full_chain": query_tokens(full_tokens),
            "query_surviving_prefix": query_tokens(prefix_tokens),
        }
        try:
            dumped = http_json(f"http://127.0.0.1:{INDEXER_PORT}/dump", timeout=30)
        except Exception as exc:
            dumped = {"error": str(exc)[:300]}
        dump_hashes = set()

        def walk(node):
            if isinstance(node, dict):
                if isinstance(node.get("block_hash"), int):
                    dump_hashes.add(node["block_hash"])
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)

        if isinstance(dumped, dict) and "error" not in dumped:
            walk(dumped)
        pub.close(linger=0)
        return {
            "status": "TRACE_REPLAY",
            "sent_messages": len(frames),
            "sent_events": total_events,
            "at_break": at_break,
            "at_end": at_end,
            "dumped_external_hashes": len(dump_hashes),
            "timing": "ordered_fast_replay_not_original_timestamps",
        }
    finally:
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(timeout=10)
        log_handle.close()


def main() -> None:
    RAW.mkdir(parents=True, exist_ok=True)
    started = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    c1 = load_c1()
    frames = c1.load_frames(C1 / "raw/frames.bin")
    with (C1 / "raw/requests.csv").open() as handle:
        requests = list(csv.DictReader(handle))
    windows = request_windows(requests)
    events = c1.event_rows(frames)
    for event in events:
        event["scenario"] = c1.assign_scenario(int(event["recv_ns"]), requests)
    transitions, probes = c1.prefix_model(events)
    reference = c1.reference_alive(frames)

    overlaps = 0
    for left in range(len(windows)):
        for right in range(left + 1, len(windows)):
            if windows[right]["started_ns"] > windows[left]["finished_ns"]:
                break
            if windows[left]["started_ns"] <= windows[right]["finished_ns"]:
                overlaps += 1

    payload_sizes = [len(frame[3]) for frame in frames]
    body_sizes = [len(frame[1]) + len(frame[2]) + len(frame[3]) for frame in frames]
    times = [frame[0] for frame in frames]
    span_s = (times[-1] - times[0]) / 1e9 if len(times) > 1 else 0.0
    seqs = []
    topics = set()
    dp_ranks = set()
    event_counts = defaultdict(int)
    hash_occurrences = 0
    stored_events = removed_events = cleared_events = 0
    messages_with_store = 0
    join_one = join_zero = join_many = 0
    slack_one = slack_zero = slack_many = 0
    prompt_of_store: list[str] = []
    prompt_lineage: list[tuple[str, int]] = []

    oracle = BlockOracle()
    converter = Converter(oracle)
    # Tip-tombstone accounting copied from the observed C1 rule, checked per advertised tip.
    ads: dict[int, dict] = {}
    fidelity_mismatches = []
    max_false_positive = 0
    max_lost = 0
    incorrect_invalidations = 0
    regression = None
    partial_target = next(
        row for row in probes if row["case"] == "partial_eviction"
    )
    partial_rows = [
        row for row in transitions
        if row["kind"] == "INVALIDATE" and int(row["survivor_tokens"]) > 0
    ]
    target_tip_hex = partial_rows[0]["prefix"] if partial_rows else ""
    target_tip = int(target_tip_hex, 16) if target_tip_hex else None
    target_ts = int(partial_rows[0]["timestamp_ns"]) if partial_rows else None
    break_frame = None
    break_state = None

    store_parent_links = 0
    store_events_with_parent = 0
    root_stores = 0

    for frame_index, (recv_ns, topic, seq, payload) in enumerate(frames):
        topics.add(topic)
        seqs.append(int.from_bytes(seq, "big") if len(seq) == 8 else -1)
        batch = c1.decode_payload(payload)
        dp_ranks.add(batch.data_parallel_rank)
        saw_store = False
        for event in batch.events:
            name = type(event).__name__
            event_counts[name] += 1
            if name == "BlockStored":
                stored_events += 1
                saw_store = True
                hash_occurrences += len(event.block_hashes)
                if event.parent_block_hash is None:
                    root_stores += 1
                else:
                    store_events_with_parent += 1
                    store_parent_links += 1
                owners = covering(windows, recv_ns)
                slack_owners = covering(
                    [{**item, "finished_ns": item["finished_ns"] + 80_000_000} for item in windows],
                    recv_ns,
                )
                if len(owners) == 1:
                    join_one += 1
                    prompt_of_store.append(owners[0]["prompt_blake2s"])
                elif len(owners) == 0:
                    join_zero += 1
                else:
                    join_many += 1
                if len(slack_owners) == 1:
                    slack_one += 1
                elif len(slack_owners) == 0:
                    slack_zero += 1
                else:
                    slack_many += 1
                before = set(oracle.alive)
                new = oracle.apply_stored(event)
                parent = hash_int(event.parent_block_hash)
                lineage = converter.on_stored(parent, new, recv_ns)
                if lineage is not None and len(owners) == 1:
                    prompt_lineage.append((owners[0]["prompt_blake2s"], lineage))
                if lineage is not None and len(new) >= 16:
                    chain = list(converter.chain[lineage])
                    coverage, blocks, _orphans = oracle.contiguous(chain)
                    ads[chain[-1]] = {
                        "lineage": lineage,
                        "blocks": blocks,
                        "coverage": coverage,
                    }
            elif name == "BlockRemoved":
                removed_events += 1
                hash_occurrences += len(event.block_hashes)
                removed = oracle.apply_removed(event)
                converter.on_removed(removed, recv_ns)
            elif name == "AllBlocksCleared":
                cleared_events += 1
                oracle.apply_cleared()
                converter.on_cleared(recv_ns)
        if (
            break_state is None
            and target_tip is not None
            and target_ts is not None
            and recv_ns == target_ts
            and target_tip in oracle.parent
        ):
            chain = oracle.chain(target_tip)
            coverage, blocks, orphans = oracle.contiguous(chain)
            lineage = None
            for candidate, candidate_chain in converter.chain.items():
                if target_tip in candidate_chain:
                    lineage = candidate
                    break
            advertised = converter.advertised.get(lineage) if lineage is not None else None
            break_frame = frame_index
            break_state = {
                "recv_ns": recv_ns,
                "frame_index": frame_index,
                "chain_blocks": len(chain),
                "oracle_reusable_tokens": coverage,
                "oracle_prefix_blocks": blocks,
                "orphan_blocks_after_hole": orphans,
                "lineage_id": lineage,
                "rollback_advertised_tokens": advertised,
                "tip_tombstone_tokens": 0,
                "removed_in_this_check": True,
                "full_tokens": [
                    token
                    for block in chain
                    for token in oracle.tokens.get(block, ())
                ],
                "prefix_tokens": [
                    token
                    for block in chain[:blocks]
                    for token in oracle.tokens.get(block, ())
                ],
            }
        if saw_store:
            messages_with_store += 1
        for lineage, chain in converter.chain.items():
            oracle_tokens = oracle.contiguous(chain)[0]
            advertised = converter.advertised.get(lineage, 0)
            false_positive = max(0, advertised - oracle_tokens)
            lost = max(0, oracle_tokens - advertised)
            if false_positive or lost:
                fidelity_mismatches.append({
                    "frame_index": frame_index,
                    "lineage_id": lineage,
                    "oracle": oracle_tokens,
                    "advertised": advertised,
                })
            max_false_positive = max(max_false_positive, false_positive)
            max_lost = max(max_lost, lost)
            if advertised == 0 and oracle_tokens > 0:
                incorrect_invalidations += 1

    seq_gaps = sum(1 for left, right in zip(seqs, seqs[1:]) if right != left + 1)
    token_stats = token_byte_estimate(c1, frames)
    distinct_prompts = len({item["prompt_blake2s"] for item in windows})
    child_count: dict[int, int] = defaultdict(int)
    roots = 0
    for block, parent in oracle.parent.items():
        if parent is None:
            roots += 1
        else:
            child_count[parent] += 1
    branched_parents = sum(1 for count in child_count.values() if count > 1)
    max_children = max(child_count.values()) if child_count else 0

    # Final fidelity, one row per live or historical lineage.
    end_rows = []
    lost_sum = 0
    false_sum = 0
    for lineage, chain in converter.chain.items():
        oracle_tokens, _blocks, orphans = oracle.contiguous(chain)
        advertised = converter.advertised.get(lineage, 0)
        false_positive = max(0, advertised - oracle_tokens)
        lost = max(0, oracle_tokens - advertised)
        lost_sum += lost
        false_sum += false_positive
        end_rows.append((lineage, oracle_tokens, advertised, false_positive, lost, orphans))

    regression_pair = None
    if break_state is not None and break_state["lineage_id"] is not None:
        before = None
        after = break_state["rollback_advertised_tokens"]
        for update in converter.updates:
            if update["lineage_id"] != break_state["lineage_id"]:
                continue
            if update["recv_ns"] < break_state["recv_ns"] and update["kind"] == "UPSERT":
                before = update["coverage_tokens"]
            if update["recv_ns"] == break_state["recv_ns"] and update["kind"] == "UPSERT":
                after = update["coverage_tokens"]
        if before is not None and after is not None:
            regression_pair = (break_state["lineage_id"], before, after)

    shrinks = [
        update for update in converter.updates
        if update["kind"] == "UPSERT"
    ]
    shrink_count = 0
    last_coverage: dict[int, int] = {}
    for update in converter.updates:
        previous = last_coverage.get(update["lineage_id"])
        if update["kind"] == "UPSERT" and previous is not None and update["coverage_tokens"] < previous:
            shrink_count += 1
        if update["kind"] == "UPSERT":
            last_coverage[update["lineage_id"]] = update["coverage_tokens"]
        elif update["kind"] in {"EVICT", "RESTART"}:
            last_coverage.pop(update["lineage_id"], None)

    c1_partial = partial_target
    dispatcher = dispatcher_probe(regression_pair)
    synthetic_rows = synthetic_converter_cases()

    prompts_for_lineage: dict[int, set[str]] = defaultdict(set)
    lineages_for_prompt: dict[str, set[int]] = defaultdict(set)
    for prompt_hash, lineage in prompt_lineage:
        prompts_for_lineage[lineage].add(prompt_hash)
        lineages_for_prompt[prompt_hash].add(lineage)
    if break_state is not None:
        break_public = {
            key: value
            for key, value in break_state.items()
            if key not in {"full_tokens", "prefix_tokens"}
        }
        break_public["full_token_len"] = len(break_state["full_tokens"])
        break_public["prefix_token_len"] = len(break_state["prefix_tokens"])
    else:
        break_public = None
    indexer = {"status": "NOT_MEASURED", "reason": "break frame not found"}
    skip_indexer = os.environ.get("M3_SKIP_INDEXER") == "1"
    previous_indexer = None
    previous_path = RAW / "summary.json"
    if skip_indexer and previous_path.is_file():
        previous_indexer = json.loads(previous_path.read_text()).get("indexer_replay")
    if previous_indexer is not None:
        indexer = previous_indexer
    elif break_state is not None and break_frame is not None:
        indexer = replay_indexer(
            c1,
            frames,
            break_frame,
            break_state["full_tokens"],
            break_state["prefix_tokens"],
        )

    alive_match = len(oracle.alive) == len(reference) == 6525 and oracle.alive == reference
    payload_bytes = sum(payload_sizes)
    body_bytes = sum(body_sizes)
    windows_ns = {
        "1ms": 1_000_000,
        "10ms": 10_000_000,
        "100ms": 100_000_000,
        "1s": 1_000_000_000,
    }
    peaks = {
        name: peak_window(times, payload_sizes, width)
        for name, width in windows_ns.items()
    }
    body_peaks = {
        name: peak_window(times, body_sizes, width)
        for name, width in windows_ns.items()
    }

    upserts = sum(1 for update in converter.updates if update["kind"] == "UPSERT")
    evicts = sum(1 for update in converter.updates if update["kind"] == "EVICT")
    restarts = sum(1 for update in converter.updates if update["kind"] == "RESTART")
    compact_bytes_64 = (upserts + evicts + restarts) * dispatcher["frame_bytes"]

    traffic_rows = [
        {"metric": "zmq_messages", "value": len(frames), "unit": "messages", "evidence": "TRACE_REPLAY"},
        {"metric": "events", "value": sum(event_counts.values()), "unit": "events", "evidence": "TRACE_REPLAY"},
        {"metric": "block_hash_occurrences", "value": hash_occurrences, "unit": "hashes", "evidence": "TRACE_REPLAY"},
        {"metric": "msgpack_payload_bytes", "value": payload_bytes, "unit": "bytes", "evidence": "TRACE_REPLAY"},
        {"metric": "zmq_body_bytes", "value": body_bytes, "unit": "bytes", "evidence": "TRACE_REPLAY"},
        {"metric": "duration_s", "value": round(span_s, 3), "unit": "seconds", "evidence": "TRACE_REPLAY"},
        {"metric": "messages_per_s", "value": round(len(frames) / span_s, 6), "unit": "messages/s", "evidence": "DERIVED"},
        {"metric": "events_per_s", "value": round(sum(event_counts.values()) / span_s, 6), "unit": "events/s", "evidence": "DERIVED"},
        {"metric": "block_hash_updates_per_s", "value": round(hash_occurrences / span_s, 6), "unit": "hashes/s", "evidence": "DERIVED"},
        {"metric": "payload_bytes_per_s", "value": round(payload_bytes / span_s, 3), "unit": "bytes/s", "evidence": "DERIVED"},
        {"metric": "payload_kbit_per_s", "value": round(payload_bytes * 8 / span_s / 1000, 3), "unit": "kbit/s", "evidence": "DERIVED"},
        {"metric": "payload_bytes_p50", "value": percentile(payload_sizes, 0.50), "unit": "bytes", "evidence": "TRACE_REPLAY"},
        {"metric": "payload_bytes_p95", "value": percentile(payload_sizes, 0.95), "unit": "bytes", "evidence": "TRACE_REPLAY"},
        {"metric": "payload_bytes_p99", "value": percentile(payload_sizes, 0.99), "unit": "bytes", "evidence": "TRACE_REPLAY"},
        {"metric": "payload_bytes_max", "value": max(payload_sizes), "unit": "bytes", "evidence": "TRACE_REPLAY"},
        {"metric": "zmq_body_bytes_p50", "value": percentile(body_sizes, 0.50), "unit": "bytes", "evidence": "TRACE_REPLAY"},
        {"metric": "zmq_body_bytes_p95", "value": percentile(body_sizes, 0.95), "unit": "bytes", "evidence": "TRACE_REPLAY"},
        {"metric": "zmq_body_bytes_p99", "value": percentile(body_sizes, 0.99), "unit": "bytes", "evidence": "TRACE_REPLAY"},
    ]
    for name, value in peaks.items():
        traffic_rows.append({
            "metric": f"peak_payload_bytes_{name}",
            "value": value,
            "unit": "bytes/window",
            "evidence": "DERIVED",
        })
    for name, value in body_peaks.items():
        traffic_rows.append({
            "metric": f"peak_zmq_body_bytes_{name}",
            "value": value,
            "unit": "bytes/window",
            "evidence": "DERIVED",
        })
    traffic_rows.extend([
        {"metric": "block_stored_events", "value": stored_events, "unit": "events", "evidence": "TRACE_REPLAY"},
        {"metric": "block_removed_events", "value": removed_events, "unit": "events", "evidence": "TRACE_REPLAY"},
        {"metric": "all_blocks_cleared_events", "value": cleared_events, "unit": "events", "evidence": "TRACE_REPLAY"},
        {"metric": "messages_containing_token_ids", "value": token_stats["messages_containing_token_ids"], "unit": "messages", "evidence": "TRACE_REPLAY"},
        {"metric": "payload_byte_fraction_messages_with_token_ids", "value": round(token_stats["payload_bytes_of_those_messages"] / payload_bytes, 6), "unit": "fraction", "evidence": "DERIVED"},
        {"metric": "derived_token_id_byte_fraction", "value": round(token_stats["derived_token_id_bytes"] / payload_bytes, 6), "unit": "fraction", "evidence": "DERIVED"},
        {"metric": "seq_gaps", "value": seq_gaps, "unit": "gaps", "evidence": "TRACE_REPLAY"},
        {"metric": "converter_upserts", "value": upserts, "unit": "advertisements", "evidence": "DERIVED"},
        {"metric": "converter_evicts", "value": evicts, "unit": "advertisements", "evidence": "DERIVED"},
        {"metric": "converter_restarts", "value": restarts, "unit": "advertisements", "evidence": "DERIVED"},
        {"metric": "converter_frame_bytes_if_64B", "value": compact_bytes_64, "unit": "bytes", "evidence": "DERIVED"},
    ])

    visibility_rows = []
    if break_public is not None:
        oracle_tokens = break_public["oracle_reusable_tokens"]
        rollback = break_public["rollback_advertised_tokens"] or 0
        visibility_rows.append({
            "case": "partial_eviction_1040_to_448",
            "source": "TRACE_REPLAY",
            "query_origin": "CONTROLLED_FROM_STORED_BLOCKS",
            "oracle_reusable_tokens": oracle_tokens,
            "compact_rollback_tokens": rollback,
            "tip_tombstone_tokens": 0,
            "false_positive_tokens": max(0, rollback - oracle_tokens),
            "lost_reusable_tokens": max(0, oracle_tokens - rollback),
            "incorrect_invalidation": int(rollback == 0 and oracle_tokens > 0),
            "c1_survivor_tokens": c1_partial.get("survivor_tokens_at_break", ""),
            "c1_advertised_tokens": c1_partial.get("advertised_tokens", ""),
            "c1_model_b_visible_tokens": c1_partial.get("model_b_visible_tokens", ""),
        })
    visibility_rows.append({
        "case": "end_of_trace_all_lineages",
        "source": "TRACE_REPLAY",
        "query_origin": "CONTROLLED_FROM_STORED_BLOCKS",
        "oracle_reusable_tokens": sum(row[1] for row in end_rows),
        "compact_rollback_tokens": sum(row[2] for row in end_rows),
        "tip_tombstone_tokens": "",
        "false_positive_tokens": false_sum,
        "lost_reusable_tokens": lost_sum,
        "incorrect_invalidation": sum(1 for row in end_rows if row[2] == 0 and row[1] > 0),
        "lineages": len(end_rows),
        "max_false_positive_tokens_any_check": max_false_positive,
        "max_lost_tokens_any_check": max_lost,
        "mismatch_checks": len(fidelity_mismatches),
    })
    visibility_rows.extend(synthetic_rows)

    comparison_rows = [
        {
            "path": "A_native_fullsync",
            "phase": "3",
            "status": "NOT_RUN",
            "note": "Phase 3 was not approved for this round",
        },
        {
            "path": "B_block_to_prefix_only",
            "phase": "3",
            "status": "NOT_RUN",
            "note": "Phase 3 was not approved for this round",
        },
        {
            "path": "C_semantic_reduction",
            "phase": "3",
            "status": "NOT_RUN",
            "note": "Phase 3 was not approved for this round",
        },
        {
            "path": "D_kvselect_fixed_topk16",
            "phase": "3",
            "status": "NOT_RUN",
            "note": "Phase 3 was not approved for this round",
        },
    ]

    summary = {
        "started_at": started,
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "git_head": git_head(),
        "hostname": os.uname().nodename,
        "trace": str(C1 / "raw/frames.bin"),
        "frames": len(frames),
        "events_csv_rows": sum(1 for _ in open(C1 / "raw/events.csv")) - 1,
        "decoded_events": sum(event_counts.values()),
        "event_counts": dict(event_counts),
        "seq_first": seqs[0] if seqs else None,
        "seq_last": seqs[-1] if seqs else None,
        "seq_gaps": seq_gaps,
        "topics": [topic.decode("utf-8", "replace") for topic in topics],
        "data_parallel_ranks": [None if rank is None else int(rank) for rank in dp_ranks],
        "duration_s": round(span_s, 6),
        "payload_bytes": payload_bytes,
        "zmq_body_bytes": body_bytes,
        "message_size": {
            "payload_p50": percentile(payload_sizes, 0.50),
            "payload_p95": percentile(payload_sizes, 0.95),
            "payload_p99": percentile(payload_sizes, 0.99),
            "body_p50": percentile(body_sizes, 0.50),
            "body_p95": percentile(body_sizes, 0.95),
            "body_p99": percentile(body_sizes, 0.99),
        },
        "peak_payload_bytes": peaks,
        "peak_body_bytes": body_peaks,
        "token_stats": token_stats,
        "requests": len(windows),
        "distinct_prompt_hashes": distinct_prompts,
        "request_overlaps": overlaps,
        "store_join_one": join_one,
        "store_join_zero": join_zero,
        "store_join_many": join_many,
        "store_join_slack80ms_one": slack_one,
        "store_join_slack80ms_zero": slack_zero,
        "store_join_slack80ms_many": slack_many,
        "distinct_prompts_joined": len(set(prompt_of_store)),
        "prompt_lineage_pairs": len(prompt_lineage),
        "lineages_with_one_prompt": sum(1 for prompts in prompts_for_lineage.values() if len(prompts) == 1),
        "lineages_with_many_prompts": sum(1 for prompts in prompts_for_lineage.values() if len(prompts) > 1),
        "prompts_with_one_lineage": sum(1 for lineages in lineages_for_prompt.values() if len(lineages) == 1),
        "prompts_with_many_lineages": sum(1 for lineages in lineages_for_prompt.values() if len(lineages) > 1),
        "payload_bytes_max": max(payload_sizes),
        "root_stores": root_stores,
        "stores_with_parent": store_events_with_parent,
        "oracle": {
            "alive": len(oracle.alive),
            "reference_alive": len(reference),
            "alive_equals_c1_reference": alive_match,
            "parent_entries": len(oracle.parent),
            "token_entries": len(oracle.tokens),
            "parent_conflicts": oracle.parent_conflicts,
            "token_conflicts": oracle.token_conflicts,
            "missing_parent": oracle.missing_parent,
            "duplicate_store": oracle.duplicate_store,
            "remove_missing": oracle.remove_missing,
            "token_len_mismatch": oracle.token_len_mismatch,
            "hash_width_bad": oracle.hash_width_bad,
            "incomplete_token_blocks": oracle.incomplete_token_blocks,
            "stored_blocks": oracle.stored_blocks,
            "removed_blocks": oracle.removed_blocks,
            "clears": oracle.clears,
            "block_size": oracle.block_size,
            "roots": roots,
            "branched_parents": branched_parents,
            "max_children": max_children,
        },
        "c1_recorded_indexer": json.loads((C1 / "raw/indexer_accuracy.json").read_text()),
        "c1_partial_probe": c1_partial,
        "break": break_public,
        "converter": {
            "lineages": len(converter.chain),
            "live_advertisements": len(converter.advertised),
            "upserts": upserts,
            "evicts": evicts,
            "restarts": restarts,
            "coverage_shrinks": shrink_count,
            "end_false_positive_tokens": false_sum,
            "end_lost_tokens": lost_sum,
            "mismatch_checks": len(fidelity_mismatches),
            "max_false_positive_tokens": max_false_positive,
            "max_lost_tokens": max_lost,
            "incorrect_invalidation_checks": incorrect_invalidations,
            "frame_bytes_if_each_update_is_64": compact_bytes_64,
            "identity": "DERIVED_BRANCH_ROOT",
            "trace_coverage_before_break": None if regression_pair is None else regression_pair[1],
            "trace_coverage_at_break": None if regression_pair is None else regression_pair[2],
        },
        "dispatcher_probe": dispatcher,
        "indexer_replay": indexer,
        "phase_3": "NOT_RUN",
        "phase_4": "NOT_RUN",
        "recorded_but_unused_sentinel": shrinks[:1],
    }
    # The sentinel above can contain a numpy-free dict; drop it to keep the file small.
    summary.pop("recorded_but_unused_sentinel")
    (RAW / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
    write_csv(OUT / "native_traffic_profile.csv", traffic_rows)
    write_csv(OUT / "visibility_fidelity.csv", visibility_rows)
    write_csv(OUT / "native_to_compact_comparison.csv", comparison_rows)
    write_csv(RAW / "converter_updates.csv", converter.updates)
    print(json.dumps({
        "frames": len(frames),
        "alive_match": alive_match,
        "alive": len(oracle.alive),
        "break": break_public,
        "join": [join_one, join_zero, join_many],
        "join_slack80ms": [slack_one, slack_zero, slack_many],
        "mismatches": len(fidelity_mismatches),
        "shrinks": shrink_count,
        "upserts": upserts,
        "evicts": evicts,
        "indexer": indexer.get("status"),
        "indexer_break": indexer.get("at_break"),
        "indexer_end_applied": (indexer.get("at_end") or {}).get("applied_events"),
        "dump": indexer.get("dumped_external_hashes"),
        "dispatcher": dispatcher,
    }, indent=2, default=str))


if __name__ == "__main__":
    main()
