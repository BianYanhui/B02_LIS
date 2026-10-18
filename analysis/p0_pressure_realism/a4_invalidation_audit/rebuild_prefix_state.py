#!/usr/bin/env python3
"""Rebuild advertised-prefix validity from the P0-A3 ZMQ trace.

Reads the A3 trace. Does not contact vLLM and does not modify A3 files.
An advertised prefix is the tip of a BlockStored batch that newly caches
at least 16 blocks. A later BlockRemoved invalidates that advertisement
only when it breaks that tip's still-live chain. One broken advertisement
is one invalidation, not one message per removed block.
"""
from __future__ import annotations

import csv
import math
from collections import defaultdict
from pathlib import Path

A3 = Path("/home/byh/B02/analysis/p0_pressure_realism/a3_native_events")
OUT = Path("/home/byh/B02/analysis/p0_pressure_realism/a4_invalidation_audit")
DRAIN = 9408.0


def load_events() -> list[dict]:
    rows = list(csv.DictReader((A3 / "native_event_trace.csv").open()))
    for index, row in enumerate(rows):
        row["_i"] = index
        row["_ts"] = int(row["timestamp_ns"])
        row["_n"] = int(row["n_blocks"] or 0)
    return rows


def load_requests() -> list[dict]:
    return list(csv.DictReader((A3 / "raw" / "requests.csv").open()))


def scenario_window(reqs: list[dict], scenario: str) -> tuple[int, int, float, int]:
    group = [row for row in reqs if row["scenario"] == scenario and row["ok"] == "True"]
    t0 = min(int(row["started_ns"]) for row in group)
    t1 = max(int(row["finished_ns"]) for row in group)
    return t0, t1, (t1 - t0) / 1e9, len(group)


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


def main() -> None:
    events = load_events()
    reqs = load_requests()
    parent: dict[str, str] = {}
    alive: set[str] = set()
    ads: dict[str, dict] = {}
    transitions: list[dict] = []
    cycles = 0
    missing_parent = 0
    removed_on_ad = 0
    removed_off_ad = 0
    removed_unknown = 0

    def emit(kind: str, category: str, tip: str, scenario: str, request_id: str, ts: int, coverage: int, survivor: int, evidence: str, protocol: str) -> None:
        ad = ads.get(tip)
        version = 0 if ad is None else ad["version"] + 1
        if ad is not None:
            ad["version"] = version
            ad["validity"] = "VALID" if kind in {"UPSERT", "RE_ADVERTISE"} else "INVALID"
            ad["current_reusable_coverage"] = coverage if kind in {"UPSERT", "RE_ADVERTISE"} else survivor
            ad["last_transition_timestamp"] = ts
        transitions.append({
            "timestamp_ns": ts,
            "worker_id": 0,
            "scenario": scenario,
            "request_id": request_id,
            "prefix_identifier": tip,
            "kind": kind,
            "category": category,
            "advertised_coverage": "" if ad is None else ad["advertised_coverage"],
            "reusable_coverage_after": coverage if kind in {"UPSERT", "RE_ADVERTISE"} else survivor,
            "survivor_tokens": survivor,
            "validity_after": "VALID" if kind in {"UPSERT", "RE_ADVERTISE"} else "INVALID",
            "version": version if ad is not None else 1,
            "evidence": evidence,
            "protocol_op": protocol,
        })

    pending_removals: list[dict] = []

    def flush_removals() -> None:
        nonlocal removed_on_ad, removed_off_ad, removed_unknown
        if not pending_removals:
            return
        touched: set[str] = set()
        scenario = pending_removals[-1]["scenario"]
        request_id = pending_removals[-1]["request_id"]
        ts = pending_removals[-1]["_ts"]
        for ev in pending_removals:
            for block_hash in ev["block_hashes"].split():
                if block_hash not in alive:
                    removed_unknown += 1
                    continue
                alive.remove(block_hash)
                touched.add(block_hash)
        hit_tips = []
        for tip, ad in ads.items():
            if ad["validity"] != "VALID":
                continue
            full = chain_of(tip, parent)
            if not any(block in touched for block in full):
                continue
            survivor_hashes = []
            for node in full:
                if node not in alive:
                    break
                survivor_hashes.append(node)
            survivor = len(survivor_hashes) * 16
            if survivor == len(full) * 16:
                continue
            hit_tips.append((tip, survivor, len(full) * 16))
        if hit_tips:
            removed_on_ad += len(touched)
        else:
            removed_off_ad += len(touched)
        for tip, survivor, _full_tokens in hit_tips:
            ads[tip]["validity"] = "INVALID"
            ads[tip]["current_reusable_coverage"] = survivor
            protocol = "K_TOMB" if survivor == 0 else "K_TOMB"
            evidence = (
                "one removal batch broke this advertised tip; "
                f"survivor_tokens={survivor} remain on the ancestor chain"
            )
            emit("INVALIDATE", "invalidation", tip, scenario, request_id, ts, 0, survivor, evidence, protocol)
        pending_removals.clear()

    for ev in events:
        kind = ev["event_type"]
        scenario = ev["scenario"]
        request_id = ev["request_id"]
        ts = ev["_ts"]
        if kind != "BlockRemoved" and pending_removals:
            flush_removals()
        if kind == "AllBlocksCleared":
            invalidated = [tip for tip, ad in ads.items() if ad["validity"] == "VALID"]
            alive.clear()
            for tip in invalidated:
                emit("RESTART", "reset_not_eviction", tip, scenario, request_id, ts, 0, 0,
                     "AllBlocksCleared from the collector's reset_prefix_cache", "K_RESET")
            continue
        hashes = ev["block_hashes"].split() if ev["block_hashes"] else []
        if kind == "BlockRemoved":
            if pending_removals and pending_removals[-1]["_ts"] != ts:
                flush_removals()
            pending_removals.append(ev)
            continue
        if kind == "BlockStored":
            parent_hash = ev["parent_block_hash"]
            if parent_hash and parent_hash not in alive and parent_hash not in hashes:
                missing_parent += 1
            new = []
            cursor = parent_hash
            for block_hash in hashes:
                if block_hash in alive:
                    cursor = block_hash
                    continue
                if block_hash in parent or block_hash in {item["tip"] for item in ads.values()}:
                    cycles += 1
                parent[block_hash] = cursor or ""
                alive.add(block_hash)
                new.append(block_hash)
                cursor = block_hash
            if len(new) < 16:
                continue
            tip = hashes[-1]
            full = chain_of(tip, parent)
            coverage = len(full) * 16
            if tip in ads and ads[tip]["validity"] == "INVALID":
                ads[tip]["advertised_coverage"] = coverage
                emit("RE_ADVERTISE", "upsert", tip, scenario, request_id, ts, coverage, coverage,
                     "tip stored again after its advertisement was invalidated", "K_UP")
            elif tip not in ads:
                ads[tip] = {
                    "advertised_coverage": coverage,
                    "current_reusable_coverage": coverage,
                    "validity": "VALID",
                    "version": 0,
                    "last_transition_timestamp": ts,
                    "tip": tip,
                }
                emit("UPSERT", "upsert", tip, scenario, request_id, ts, coverage, coverage,
                     "BlockStored batch with at least 16 new hashes", "K_UP")
            continue
    flush_removals()

    # Recompute versions from transition order. A fresh UPSERT is inserted
    # before emit() assigns one, so the running counter above is discarded.
    # by rewriting version from the transition list order per tip.
    seen_version: dict[str, int] = defaultdict(int)
    for row in transitions:
        seen_version[row["prefix_identifier"]] += 1
        row["version"] = seen_version[row["prefix_identifier"]]

    write(OUT / "prefix_state_transitions.csv", transitions)

    # Accounting per scenario, using the same request windows as P0-A3.
    summary = []
    rate_rows = []
    windows = {}
    for scenario in ("A_cold", "B_reuse", "C_plant", "C_flood", "C_replay", "warmup"):
        if not any(row["scenario"] == scenario and row["ok"] == "True" for row in reqs):
            continue
        t0, t1, dur, nreq = scenario_window(reqs, scenario)
        windows[scenario] = (t0, t1, dur, nreq)
        local_events = [ev for ev in events if ev["scenario"] == scenario and t0 <= ev["_ts"] <= t1 + 50_000_000]
        stored = sum(ev["_n"] for ev in local_events if ev["event_type"] == "BlockStored")
        removed = sum(1 for ev in local_events if ev["event_type"] == "BlockRemoved")
        local_tx = [row for row in transitions if row["scenario"] == scenario]
        upserts = [row for row in local_tx if row["kind"] == "UPSERT"]
        readv = [row for row in local_tx if row["kind"] == "RE_ADVERTISE"]
        inv = [row for row in local_tx if row["kind"] == "INVALIDATE"]
        restarts = [row for row in local_tx if row["kind"] == "RESTART"]
        upsert_frames = len(upserts) + len(readv)
        total = upsert_frames + len(inv)
        summary.append({
            "scenario": scenario,
            "duration_s": round(dur, 3),
            "requests": nreq,
            "block_stored_hashes": stored,
            "block_removed_events": removed,
            "prefix_upsert": len(upserts),
            "prefix_re_advertisement": len(readv),
            "prefix_invalidation": len(inv),
            "reset_restart": len(restarts),
            "invalidation_with_survivor": sum(1 for row in inv if int(row["survivor_tokens"]) > 0),
            "invalidation_full": sum(1 for row in inv if int(row["survivor_tokens"]) == 0),
            "includes_real_invalidation": "yes" if inv else "no",
            "p0a3_prefix_rate_was": "upsert_only",
        })
        for name, frames in (
            ("R_block_stored", stored),
            ("R_block_removed", removed),
            ("R_prefix_upsert_including_readvertisement", upsert_frames),
            ("R_prefix_invalidation", len(inv)),
            ("R_prefix_re_advertisement_included_above", len(readv)),
            ("R_total_dispatch_metadata", total),
            ("R_reset_restart_excluded", len(restarts)),
        ):
            rate = frames / dur
            rate_rows.append({
                "scenario": scenario,
                "series": name,
                "events": frames,
                "duration_s": round(dur, 3),
                "per_s": round(rate, 4),
                "payload_64B_bit_per_s": round(rate * 64 * 8, 1),
                "wire_104B_bit_per_s": round(rate * 104 * 8, 1),
                "rho_vs_9408": round(rate / DRAIN, 6),
                "source": "a3 native_event_trace.csv offline rebuild",
            })
    write(OUT / "invalidation_accounting.csv", summary)
    write(OUT / "metadata_rate_recalculated.csv", rate_rows)

    burst_rows = []
    for scenario, (t0, t1, dur, _nreq) in windows.items():
        if scenario not in {"A_cold", "B_reuse", "C_flood", "C_replay"}:
            continue
        stamps = [int(row["timestamp_ns"]) / 1e9 for row in transitions
                  if row["scenario"] == scenario and row["kind"] in {"UPSERT", "RE_ADVERTISE", "INVALIDATE"}]
        inv_stamps = [int(row["timestamp_ns"]) / 1e9 for row in transitions
                      if row["scenario"] == scenario and row["kind"] == "INVALIDATE"]
        start = t0 / 1e9
        end = t1 / 1e9
        for label, times in (("dispatch_total", stamps), ("invalidation_only", inv_stamps)):
            for width, name in ((0.01, "10ms"), (0.1, "100ms"), (1.0, "1s")):
                nbins = max(1, math.ceil((end - start) / width))
                bins = [0] * nbins
                for ts in times:
                    index = int((ts - start) / width)
                    if 0 <= index < nbins:
                        bins[index] += 1
                cap = DRAIN * width
                burst_rows.append({
                    "scenario": scenario,
                    "series": label,
                    "window": name,
                    "n_windows": nbins,
                    "max": max(bins) if bins else 0,
                    "p99": round(percentile(bins, 99), 3),
                    "mean": round(sum(bins) / len(bins), 4) if bins else 0,
                    "capacity_frames": round(cap, 2),
                    "max_exceeds_capacity": int(max(bins) > cap) if bins else 0,
                    "unstable": "yes" if nbins < 30 else "no",
                })
    write(OUT / "burst_recalculated.csv", burst_rows)

    print("transitions", len(transitions))
    print("cycles_restored_after_removal_marker", cycles)
    print("stored_with_missing_parent", missing_parent)
    print("removed_touching_live_ad", removed_on_ad, "removed_not_on_live_ad", removed_off_ad, "removed_already_dead", removed_unknown)
    for row in summary:
        print(row)
    print("--- rates ---")
    for row in rate_rows:
        if row["series"] in {"R_prefix_upsert_including_readvertisement", "R_prefix_invalidation", "R_total_dispatch_metadata", "R_block_removed"}:
            print(row["scenario"], row["series"], row["events"], row["per_s"], "rho", row["rho_vs_9408"])
    print("--- burst max ---")
    for row in burst_rows:
        if row["window"] in {"10ms", "1s"} and row["series"] == "dispatch_total":
            print(row["scenario"], row["window"], "max", row["max"], "exceeds", row["max_exceeds_capacity"], "unstable", row["unstable"])


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
    if not rows:
        path.write_text("")
        return
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


if __name__ == "__main__":
    main()
