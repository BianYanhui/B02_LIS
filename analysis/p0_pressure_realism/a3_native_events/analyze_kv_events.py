#!/usr/bin/env python3
"""Offline mapping from the collected ZMQ trace. Does not contact vLLM."""
from __future__ import annotations

import csv
import math
from collections import defaultdict
from pathlib import Path

ROOT = Path("/home/byh/B02/analysis/p0_pressure_realism/a3_native_events")
RAW = ROOT / "raw"
CAPACITY = 9408.0


def load():
    reqs = list(csv.DictReader((RAW / "requests.csv").open()))
    evs = list(csv.DictReader((RAW / "events.csv").open()))
    ordinal = {}
    counters: dict[str, int] = defaultdict(int)
    for req in reqs:
        key = (req["scenario"], req["seq"], req["started_ns"])
        ordinal[key] = f"{req['scenario']}:{counters[req['scenario']]}"
        counters[req["scenario"]] += 1
        req["request_id"] = ordinal[key]
    return reqs, evs


def match_request(ev, reqs) -> str:
    ts = float(ev["batch_ts_s"])
    for req in reqs:
        if req["scenario"] != ev["scenario"]:
            continue
        t0 = int(req["started_ns"]) / 1e9 - 0.02
        t1 = int(req["finished_ns"]) / 1e9 + 0.05
        if t0 <= ts <= t1:
            return req["request_id"]
    return ""


def main() -> None:
    reqs, evs = load()
    trace = []
    for ev in evs:
        hashes = ev["block_hashes"].split() if ev["block_hashes"] else []
        trace.append({
            "timestamp_ns": int(float(ev["batch_ts_s"]) * 1e9),
            "recv_ns": ev["recv_ns"],
            "worker_id": 0,
            "request_id": match_request(ev, reqs),
            "event_type": ev["event_type"],
            "block_id": "",
            "prefix_identifier": hashes[-1] if hashes else "",
            "parent_block_hash": ev["parent_block_hash"],
            "n_blocks": ev["n_blocks"],
            "block_hashes": ev["block_hashes"],
            "token_count_hashed_not_stored": ev["token_count"],
            "block_size": ev["block_size"] or "16",
            "event_source": "DIRECT_NATIVE_EVENT",
            "event_semantics": ev["event_semantics"],
            "scenario": ev["scenario"],
        })
    write(ROOT / "native_event_trace.csv", trace)

    valid: dict[str, str | None] = {}
    children: dict[str, set[str]] = defaultdict(set)
    transitions = []
    for ev in trace:
        if ev["event_type"] == "AllBlocksCleared":
            transitions.append({
                "timestamp_ns": ev["timestamp_ns"], "scenario": ev["scenario"],
                "request_id": ev["request_id"], "transition": "ALL_INVALID",
                "kind": "RESTART", "prefix_identifier": "", "coverage_tokens": 0,
                "n_hashes": len(valid), "evidence": "AllBlocksCleared after reset_prefix_cache",
                "dispatch_relevant": "reset_between_scenarios",
            })
            valid.clear()
            children.clear()
            continue
        hashes = ev["block_hashes"].split() if ev["block_hashes"] else []
        if ev["event_type"] == "BlockStored":
            parent = ev["parent_block_hash"] or None
            new = []
            cursor = parent
            for block_hash in hashes:
                if block_hash not in valid:
                    valid[block_hash] = cursor
                    if cursor:
                        children[cursor].add(block_hash)
                    new.append(block_hash)
                cursor = block_hash
            if not new:
                continue
            tip = hashes[-1]
            depth = 0
            seen: set[str] = set()
            node = tip
            while node and node not in seen and node in valid:
                seen.add(node)
                depth += 1
                node = valid[node]
            kind = "EXTENSION" if parent and parent in valid and parent not in new else "UPSERT"
            transitions.append({
                "timestamp_ns": ev["timestamp_ns"], "scenario": ev["scenario"],
                "request_id": ev["request_id"], "transition": "COVERAGE_INCREASE",
                "kind": kind, "prefix_identifier": tip, "coverage_tokens": depth * 16,
                "n_hashes": len(new), "evidence": "BlockStored hashes absent from the owner table",
                "dispatch_relevant": "prompt_coverage" if len(new) >= 16 else "decode_tail_block",
            })
        elif ev["event_type"] == "BlockRemoved":
            for block_hash in hashes:
                if block_hash not in valid:
                    continue
                stack = [block_hash]
                gone = 0
                while stack:
                    cur = stack.pop()
                    if cur not in valid:
                        continue
                    gone += 1
                    stack.extend(children.pop(cur, ()))
                    parent = valid.pop(cur)
                    if parent and parent in children:
                        children[parent].discard(cur)
                transitions.append({
                    "timestamp_ns": ev["timestamp_ns"], "scenario": ev["scenario"],
                    "request_id": ev["request_id"], "transition": "VALID_TO_INVALID",
                    "kind": "EVICT", "prefix_identifier": block_hash, "coverage_tokens": 0,
                    "n_hashes": gone, "evidence": "BlockRemoved",
                    "dispatch_relevant": "block_invalidation",
                })
    write(ROOT / "dispatch_relevant_transitions.csv", transitions)

    rows = []
    for scenario in ("A_cold", "B_reuse", "C_flood", "C_replay"):
        group = [req for req in reqs if req["scenario"] == scenario and req["ok"] == "True"]
        t0 = min(int(req["started_ns"]) for req in group)
        t1 = max(int(req["finished_ns"]) for req in group)
        dur = (t1 - t0) / 1e9
        stored = sum(int(ev["n_blocks"]) for ev in trace if ev["scenario"] == scenario and ev["event_type"] == "BlockStored")
        removed = sum(1 for ev in trace if ev["scenario"] == scenario and ev["event_type"] == "BlockRemoved")
        raw_stored_events = sum(1 for ev in trace if ev["scenario"] == scenario and ev["event_type"] == "BlockStored")
        prompt = sum(1 for item in transitions if item["scenario"] == scenario and item["dispatch_relevant"] == "prompt_coverage")
        tail = sum(1 for item in transitions if item["scenario"] == scenario and item["dispatch_relevant"] == "decode_tail_block")
        evict = sum(1 for item in transitions if item["scenario"] == scenario and item["kind"] == "EVICT")
        for name, frames in (
            ("A_block_level_stored_plus_removed", stored + removed),
            ("B_new_hash_events", prompt + tail + evict),
            ("B_prompt_coverage_only", prompt),
            ("B_prompt_plus_block_evict", prompt + evict),
            ("C_wrapper_one_per_completion", len(group)),
        ):
            rate = frames / dur
            rows.append({
                "scenario": scenario,
                "model": name,
                "duration_s": round(dur, 3),
                "requests": len(group),
                "raw_blockstored_events": raw_stored_events,
                "frames": frames,
                "frames_per_s": round(rate, 3),
                "payload_64B_bit_per_s": round(rate * 64 * 8, 1),
                "wire_104B_bit_per_s": round(rate * 104 * 8, 1),
                "rho_vs_9408_frames_per_s": round(rate / CAPACITY, 6),
            })
    write(ROOT / "metadata_granularity_comparison.csv", rows)

    burst = []
    for scenario in ("A_cold", "B_reuse", "C_flood"):
        group = [req for req in reqs if req["scenario"] == scenario and req["ok"] == "True"]
        t0 = min(int(req["started_ns"]) for req in group) / 1e9
        t1 = max(int(req["finished_ns"]) for req in group) / 1e9
        block_times = []
        for ev in trace:
            if ev["scenario"] != scenario:
                continue
            ts = int(ev["timestamp_ns"]) / 1e9
            if ev["event_type"] == "BlockStored":
                block_times.extend([ts] * int(ev["n_blocks"]))
            elif ev["event_type"] == "BlockRemoved":
                block_times.append(ts)
        chain_times = [int(item["timestamp_ns"]) / 1e9 for item in transitions if item["scenario"] == scenario and item["kind"] in {"UPSERT", "EXTENSION", "EVICT"}]
        wrap_times = [int(req["finished_ns"]) / 1e9 for req in group]
        for name, times in (("native_block", block_times), ("dispatch_new_hash", chain_times), ("wrapper", wrap_times)):
            for width, label in ((0.01, "10ms"), (0.1, "100ms"), (1.0, "1s")):
                nbins = max(1, math.ceil((t1 - t0) / width))
                bins = [0] * nbins
                for ts in times:
                    index = int((ts - t0) / width)
                    if 0 <= index < nbins:
                        bins[index] += 1
                occupied = sum(1 for value in bins if value)
                burst.append({
                    "scenario": scenario, "series": name, "window": label,
                    "n_windows": nbins, "occupied_windows": occupied,
                    "mean": round(sum(bins) / len(bins), 4),
                    "p95": round(percentile(bins, 95), 3),
                    "p99": round(percentile(bins, 99), 3),
                    "max": max(bins),
                    "unstable": "yes" if nbins < 30 or occupied < 5 else "no",
                    "capacity_frames_per_window": round(CAPACITY * width, 2),
                    "max_exceeds_capacity": int(max(bins) > CAPACITY * width),
                })
    write(ROOT / "burst_analysis.csv", burst)
    for row in rows:
        print(row["scenario"], row["model"], row["frames_per_s"], "rho", row["rho_vs_9408_frames_per_s"])


def percentile(values: list[int], p: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    rank = (len(ordered) - 1) * p / 100
    lo = math.floor(rank)
    hi = math.ceil(rank)
    if lo == hi:
        return float(ordered[lo])
    return ordered[lo] * (hi - rank) + ordered[hi] * (rank - lo)


def write(path: Path, rows: list[dict]) -> None:
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


if __name__ == "__main__":
    main()
