#!/usr/bin/env python3
"""F6-P3 offline readiness for Fig. 6(a) and Fig. 6(c).

Does not start vLLM, does not modify experiments/, and does not write into
analysis/formal4t4. Merge and replica-cap decisions are the frozen Relay.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path("/home/byh/B02")
OUT = ROOT / "analysis/fig6_reconstruction/f6_p3"
FORMAL = ROOT / "analysis/formal4t4"
RELAY_PATH = ROOT / "experiments/4t4/net/gateway_relay_4t4.py"
WIRE_BYTES = 104  # accounting convention, not a measured TCP length
TRACE_ORIGINAL = FORMAL / "raw/traces/original_compatible_trace_rep0.csv"

# Pre-registered before looking at extension labels.
PAIR_REP = 0
CONTROLLED_SIG_BIT = 1754  # same controlled HTB rate as the dynamic low phase
CONTROLLED_ARRIVALS_PER_SERVICE = 4


def load_relay():
    spec = importlib.util.spec_from_file_location("f6p3_relay", RELAY_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["f6p3_relay"] = module
    spec.loader.exec_module(module)
    return module


def digest64(digest: str) -> int:
    return int.from_bytes(hashlib.blake2b(digest.encode(), digest_size=8).digest(), "big")


def percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((pct / 100.0) * (len(ordered) - 1))))
    return float(ordered[index])


def fnum(value: float | None, digits: int = 6) -> str:
    if value is None:
        return ""
    return f"{value:.{digits}f}"


def read_csv(path: Path) -> list[dict]:
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


def make_relay(mod, merge: bool, dedup: int):
    args = argparse.Namespace(
        max_queue=4096, quiet=True, tau=1.0, util_lambda=16, gate=1.0,
        adaptive_queue_gate=8, adaptive_queue_exit=2, congestion_hold=0.2,
        congestion_exit_hold=0.5,
    )
    relay = mod.Relay(args)
    relay.mode = mod.MODE_LATEST if merge or dedup else mod.MODE_FULLSYNC
    relay.merge = bool(merge)
    relay.priority = False
    relay.adaptive = False
    relay.dedup = int(dedup)
    relay.global_topk = 0
    relay.max_inflight = 0
    relay.current_cell = -1
    return relay


def classify_log(path: Path, policy: str | None = None) -> dict:
    """Join each superseded row to the next log row. Ambiguous joins stay OTHER."""
    rows = read_csv(path)
    counts = {"EXTENSION": 0, "DUPLICATE": 0, "TOMBSTONE": 0, "OTHER": 0, "ANOMALOUS": 0}
    pending_durations = []
    examples = {key: [] for key in counts}
    offered = 0
    forwarded = 0
    cells = set()
    pending: dict[tuple, dict] = {}
    for index, row in enumerate(rows):
        if policy and row["policy"] != policy:
            continue
        if row["op"] != "upsert":
            continue
        cell = int(row["cell"])
        cells.add(cell)
        owner = int(row["owner"])
        digest = int(row["prefix_digest64"])
        update_id = int(row["update_id"])
        coverage = int(row["coverage_tokens"])
        arrival = float(row["gateway_arrival_time"])
        key = (cell, owner, digest)
        if row["stage"] == "enqueued":
            offered += 1
            pending[key] = {
                "update_id": update_id,
                "coverage": coverage,
                "arrival": arrival,
                "generation": float(row["generation_time"]),
            }
            continue
        if row["stage"] == "forwarded":
            forwarded += 1
            current = pending.get(key)
            if current and current["update_id"] == update_id:
                pending.pop(key, None)
            continue
        if row["stage"] != "suppressed" or row["suppression_reason"] != "superseded":
            continue
        previous = pending.get(key)
        still_pending = previous is not None and previous["update_id"] == update_id
        nxt = rows[index + 1] if index + 1 < len(rows) else None
        label = "OTHER"
        new_coverage = ""
        new_version = ""
        new_generation = ""
        if not still_pending:
            label = "ANOMALOUS"
        elif nxt is None:
            label = "OTHER"
        elif nxt["op"] == "tombstone" and int(nxt["cell"]) == cell and int(nxt["owner"]) == owner and int(nxt["prefix_digest64"]) == digest:
            label = "TOMBSTONE"
            new_version = int(nxt["update_id"])
            new_generation = float(nxt["generation_time"])
        elif nxt["op"] != "upsert":
            label = "OTHER"
        elif int(nxt["cell"]) != cell or int(nxt["owner"]) != owner or int(nxt["prefix_digest64"]) != digest:
            label = "OTHER"
        elif int(nxt["update_id"]) == update_id:
            label = "ANOMALOUS"
        else:
            new_coverage_i = int(nxt["coverage_tokens"])
            new_coverage = new_coverage_i
            new_version = int(nxt["update_id"])
            new_generation = float(nxt["generation_time"])
            if new_coverage_i > coverage:
                label = "EXTENSION"
            elif new_coverage_i == coverage:
                label = "DUPLICATE"
            else:
                label = "ANOMALOUS"
        counts[label] += 1
        if still_pending:
            pending_durations.append(arrival - previous["arrival"])
        if len(examples[label]) < 3:
            examples[label].append({
                "worker": owner,
                "digest": digest,
                "old_coverage": coverage,
                "new_coverage": new_coverage,
                "old_version": update_id,
                "new_version": new_version,
                "original_timestamp": previous["generation"] if previous else float(row["generation_time"]),
                "supersession_timestamp": arrival,
                "previous_still_pending": still_pending,
                "classification": label,
            })
        if still_pending and label == "TOMBSTONE":
            pending.pop(key, None)
        elif still_pending and label in ("EXTENSION", "DUPLICATE", "ANOMALOUS"):
            # The replacement upsert is recorded by the next row when it is enqueued.
            if nxt and nxt["stage"] != "enqueued":
                pending.pop(key, None)
    classified = sum(counts.values())
    return {
        "path": str(path.relative_to(ROOT)),
        "policy_filter": policy or "ALL",
        "offered_enqueued_upserts": offered,
        "forwarded_upserts": forwarded,
        "superseded": classified,
        "extension": counts["EXTENSION"],
        "duplicate": counts["DUPLICATE"],
        "other": counts["OTHER"],
        "tombstone": counts["TOMBSTONE"],
        "anomalous": counts["ANOMALOUS"],
        "true_extension_ratio": (counts["EXTENSION"] / offered) if offered else None,
        "total_supersession_ratio": (classified / offered) if offered else None,
        "duplicate_ratio": (counts["DUPLICATE"] / offered) if offered else None,
        "pending_duration_p50_s": percentile(pending_durations, 50),
        "pending_duration_p95_s": percentile(pending_durations, 95),
        "cells": sorted(cells),
        "examples": examples,
    }


def observed_visibility(rows: list[dict], policy: str) -> dict:
    upserts = []
    forwards = []
    for row in rows:
        if row["policy"] != policy or row["op"] != "upsert":
            continue
        item = {
            "owner": int(row["owner"]),
            "digest": int(row["prefix_digest64"]),
            "coverage": int(row["coverage_tokens"]),
            "arrival": float(row["gateway_arrival_time"]),
            "generation": float(row["generation_time"]),
            "send": float(row["gateway_send_time"]) if row["gateway_send_time"] else None,
            "stage": row["stage"],
        }
        if item["stage"] == "enqueued":
            upserts.append(item)
        elif item["stage"] == "forwarded" and item["send"] is not None:
            forwards.append(item)
    delays = []
    survivor = []
    missing = 0
    for update in upserts:
        candidates = [
            item["send"] for item in forwards
            if item["owner"] == update["owner"] and item["digest"] == update["digest"]
            and item["coverage"] >= update["coverage"] and item["send"] >= update["arrival"] - 1e-9
        ]
        if not candidates:
            missing += 1
            continue
        delays.append(min(candidates) - update["generation"])
    for item in forwards:
        survivor.append(item["send"] - item["generation"])
    span = 0.0
    if upserts:
        span = max(item["arrival"] for item in upserts) - min(item["arrival"] for item in upserts)
    return {
        "offered": len(upserts),
        "forwarded": len(forwards),
        "p95_visibility_s": percentile(delays, 95),
        "p50_visibility_s": percentile(delays, 50),
        "not_visible": missing,
        "not_visible_fraction": (missing / len(upserts)) if upserts else None,
        "p95_survivor_delay_s": percentile(survivor, 95),
        "p50_survivor_delay_s": percentile(survivor, 50),
        "offered_rate_per_s": (len(upserts) / span) if span > 0 else None,
        "span_s": span,
    }


def load_phases(path: Path) -> list[tuple[float, int]]:
    if not path.exists():
        return []
    phases = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        phases.append((float(item["timestamp_unix"]), int(item["sig_bit_per_s"])))
    return sorted(phases)


def rate_at(moment: float, phases: list[tuple[float, int]], default: int) -> int:
    current = default
    for stamp, rate in phases:
        if moment + 1e-9 >= stamp:
            current = rate
        else:
            break
    return current


def frame_seconds(sig_bit: int) -> float:
    return (WIRE_BYTES * 8.0) / float(sig_bit)


def offered_from_log(path: Path, policy: str) -> list[dict]:
    events = []
    for row in read_csv(path):
        if row["policy"] != policy or row["op"] != "upsert" or row["stage"] != "enqueued":
            continue
        events.append({
            "cell": int(row["cell"]),
            "owner": int(row["owner"]),
            "digest": int(row["prefix_digest64"]),
            "coverage": int(row["coverage_tokens"]),
            "arrival": float(row["gateway_arrival_time"]),
            "generation": float(row["generation_time"]),
            "seq": int(row["update_id"]),
        })
    events.sort(key=lambda item: (item["arrival"], item["seq"]))
    return events


def replay(mod, events: list[dict], phases: list[tuple[float, int]], merge: bool, dedup: int,
           default_sig_bit: int) -> dict:
    relay = make_relay(mod, merge=merge, dedup=dedup)
    if not events:
        return {"offered": 0}
    relay.current_cell = events[0]["cell"]
    index = 0
    next_service = None
    max_backlog = 0
    forwards = []
    classes = {"EXTENSION": 0, "DUPLICATE": 0, "OTHER": 0, "ANOMALOUS": 0}
    undrained = None
    tail = 0

    def enqueue(event: dict) -> None:
        if event["cell"] != relay.current_cell:
            raise RuntimeError("replay expected one cell; got a second cell id")
        key = (event["owner"], event["digest"])
        old_coverage = None
        old_seq = None
        if relay.merge and key in relay.queued_up:
            unpacked = mod.HDR.unpack(relay.queued_up[key][:32])
            old_seq = int(unpacked[3])
            old_coverage = int(unpacked[4])
        before = relay.drops["superseded"]
        data = mod.frame(
            mod.K_UP, event["owner"], event["cell"], event["seq"], event["coverage"],
            event["digest"], event["generation"],
        )
        relay.enqueue(
            data, mod.K_UP, event["owner"], event["seq"],
            event["coverage"], event["digest"], event["generation"],
        )
        if relay.drops["superseded"] > before and old_coverage is not None:
            if event["coverage"] > old_coverage:
                classes["EXTENSION"] += 1
            elif event["coverage"] == old_coverage:
                classes["DUPLICATE"] += 1
            else:
                classes["ANOMALOUS"] += 1
        elif relay.drops["superseded"] > before:
            classes["OTHER"] += 1

    while index < len(events) or relay.live_depth() > 0:
        if undrained is None and index >= len(events):
            undrained = relay.live_depth()
        arrival = events[index]["arrival"] if index < len(events) else None
        if next_service is None:
            if index >= len(events):
                break
            enqueue(events[index])
            next_service = events[index]["arrival"]
            index += 1
            max_backlog = max(max_backlog, relay.live_depth())
            continue
        if arrival is not None and arrival <= next_service:
            enqueue(events[index])
            index += 1
            max_backlog = max(max_backlog, relay.live_depth())
            continue
        if relay.live_depth() == 0:
            next_service = None
            continue
        data = relay._dequeue()
        if data is None:
            next_service = None
            continue
        kind, owner, _cell, seq, coverage, digest, generated = mod.HDR.unpack(data[:32])
        if kind != mod.K_UP:
            continue
        sig = rate_at(next_service, phases, default_sig_bit)
        forwards.append({
            "owner": owner, "digest": digest, "coverage": coverage, "seq": seq,
            "forward": next_service, "generation": generated,
        })
        if undrained is not None:
            tail += 1
        next_service += frame_seconds(sig)
        max_backlog = max(max_backlog, relay.live_depth())
    delays = []
    missing = 0
    for event in events:
        candidates = [
            item["forward"] for item in forwards
            if item["owner"] == event["owner"] and item["digest"] == event["digest"]
            and item["coverage"] >= event["coverage"] and item["forward"] >= event["arrival"] - 1e-6
        ]
        if not candidates:
            missing += 1
            continue
        delays.append(min(candidates) - event["generation"])
    survivor = [item["forward"] - item["generation"] for item in forwards]
    other_drops = sum(count for reason, count in relay.drops.items() if reason != "superseded" and reason != "replica_cap")
    span = events[-1]["arrival"] - events[0]["arrival"]
    return {
        "offered": len(events),
        "forwarded": len(forwards),
        "superseded": int(relay.drops["superseded"]),
        "replica_cap": int(relay.drops["replica_cap"]),
        "other_drops": int(other_drops),
        "extension": classes["EXTENSION"],
        "duplicate": classes["DUPLICATE"],
        "other": classes["OTHER"],
        "anomalous": classes["ANOMALOUS"],
        "undrained_at_trace_end": int(undrained or 0),
        "tail_drained": tail,
        "max_backlog": max_backlog,
        "p95_visibility_s": percentile(delays, 95),
        "p50_visibility_s": percentile(delays, 50),
        "not_visible": missing,
        "not_visible_fraction": missing / len(events),
        "p95_survivor_delay_s": percentile(survivor, 95),
        "p50_control_delay_s": percentile(survivor, 50),
        "offered_rate_per_s": (len(events) / span) if span > 0 else None,
        "span_s": span,
    }


def controlled_extension_events() -> list[dict]:
    """Same-owner assignment and compressed spacing. Not a vLLM arrival process."""
    rows = read_csv(TRACE_ORIGINAL)
    frame_s = frame_seconds(CONTROLLED_SIG_BIT)
    gap = frame_s / CONTROLLED_ARRIVALS_PER_SERVICE
    events = []
    for index, row in enumerate(rows):
        lineage = int(row["lineage_id"])
        arrival = index * gap
        events.append({
            "cell": 1,
            "owner": lineage % 4,
            "digest": digest64(row["digest"]),
            "coverage": int(row["coverage_tokens"]),
            "arrival": arrival,
            "generation": arrival,
            "seq": index + 1,
            "lineage": row["digest"],
            "step": int(row["step"]),
        })
    return events


def signaling_means() -> dict:
    grouped: dict[tuple[str, str], list[float]] = {}
    for row in read_csv(FORMAL / "summary/signaling_accounting.csv"):
        if row["policy"] not in ("LatestOnly", "StaticSemantic", "Adaptive"):
            continue
        grouped.setdefault((row["policy"], "superseded"), []).append(float(row["relay_drop_superseded_mean"]))
        grouped.setdefault((row["policy"], "duplicate"), []).append(float(row["relay_drop_duplicate_holder_mean"]))
    out = {}
    for (policy, kind), values in grouped.items():
        out[f"{policy}_{kind}_mean"] = sum(values) / len(values)
        out[f"{policy}_{kind}_n"] = len(values)
    return out


def overlap_catalog() -> list[dict]:
    catalog = []
    for fraction in ("0.0", "0.25", "0.5", "0.75"):
        path = FORMAL / f"summary/cells_calib_overlap{fraction}_20260726.csv"
        rows = read_csv(path)
        for row in rows:
            if row["policy"] not in ("FullSync", "Adaptive", "Ideal"):
                continue
            catalog.append({
                "overlap_fraction": row["overlap_fraction"],
                "policy": row["policy"],
                "pool_size": row["pool_size"],
                "seed": row["seed"],
                "n_requests": row["n_requests"],
                "instances": "4",
                "overlap_seed_prefixes": row["overlap_seed_prefixes"],
                "overlap_seed_requests": row["overlap_seed_requests"],
                "upserts_generated": row["upserts_generated"],
                "relay_forwarded": row["relay_forwarded"],
                "relay_drop_duplicate_holder": row["relay_drop_duplicate_holder"],
                "relay_drop_superseded": row["relay_drop_superseded"],
                "relay_drop_low_utility": row["relay_drop_low_utility"],
                "workload": row["workload"],
                "evidence_type": row["evidence_type"],
                "source_csv": str(path.relative_to(ROOT)),
            })
    return catalog


def multiplicity(events: list[dict]) -> dict:
    owners: dict[int, set[int]] = {}
    for event in events:
        owners.setdefault(event["digest"], set()).add(event["owner"])
    hist: dict[int, int] = {}
    for group in owners.values():
        hist[len(group)] = hist.get(len(group), 0) + 1
    over = sum(count for width, count in hist.items() if width > 2)
    return {
        "distinct_prefixes": len(owners),
        "prefixes_with_owners_gt_2": over,
        "multiplicity": ";".join(f"{width}:{hist[width]}" for width in sorted(hist)),
        "max_multiplicity": max(hist) if hist else 0,
        "owner_pairs": sum(len(group) for group in owners.values()),
    }


def replay_cap(mod, events: list[dict], dedup: int) -> dict:
    relay = make_relay(mod, merge=False, dedup=dedup)
    if events:
        relay.current_cell = events[0]["cell"]
    admitted_pairs = set()
    admitted_owners: dict[int, set[int]] = {}
    for event in events:
        if event["cell"] != relay.current_cell:
            raise RuntimeError("overlap replay crossed a cell boundary")
        before = relay.drops["replica_cap"]
        data = mod.frame(
            mod.K_UP, event["owner"], event["cell"], event["seq"], event["coverage"],
            event["digest"], event["generation"],
        )
        relay.enqueue(
            data, mod.K_UP, event["owner"], event["seq"],
            event["coverage"], event["digest"], event["generation"],
        )
        if relay.drops["replica_cap"] == before:
            admitted_pairs.add((event["digest"], event["owner"]))
            admitted_owners.setdefault(event["digest"], set()).add(event["owner"])
    undrained = relay.live_depth()
    forwarded = 0
    while True:
        data = relay._dequeue()
        if data is None:
            break
        kind, _owner, _cell, _seq, _coverage, _digest, _generated = mod.HDR.unpack(data[:32])
        if kind == mod.K_UP:
            forwarded += 1
    other = sum(count for reason, count in relay.drops.items() if reason != "replica_cap")
    return {
        "offered": len(events),
        "forwarded": forwarded,
        "replica_cap": int(relay.drops["replica_cap"]),
        "other_drops": int(other),
        "undrained_before_drain": undrained,
        "undrained_after_drain": relay.live_depth(),
        "admitted_pairs": len(admitted_pairs),
        "admitted_owners": admitted_owners,
        "window_end_unforwarded": 0,
    }


def mean_visible(visible: dict[int, int], digests: set[int]) -> float | None:
    if not digests:
        return None
    return sum(visible.get(digest, 0) for digest in digests) / len(digests)


def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("")
        return
    columns = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def pairing_row(work_point: str, arm: str, evidence: str, source: str, result: dict, extra: dict) -> dict:
    offered = result["offered"]
    return {
        "work_point": work_point,
        "arm": arm,
        "evidence": evidence,
        "source": source,
        "offered_updates": offered,
        "extension_supersessions": result.get("extension", ""),
        "duplicate_supersessions": result.get("duplicate", ""),
        "other_supersessions": result.get("other", ""),
        "anomalous_supersessions": result.get("anomalous", ""),
        "total_supersessions": result.get("superseded", ""),
        "true_extension_ratio": fnum((result.get("extension", 0) / offered) if offered else None),
        "total_supersession_ratio": fnum((result.get("superseded", 0) / offered) if offered else None),
        "duplicate_ratio": fnum((result.get("duplicate", 0) / offered) if offered else None),
        "forwarded_updates": result.get("forwarded", ""),
        "other_drops": result.get("other_drops", ""),
        "undrained_at_trace_end": result.get("undrained_at_trace_end", ""),
        "tail_drained": result.get("tail_drained", ""),
        "p95_latest_state_visibility_delay_s": fnum(result.get("p95_visibility_s")),
        "p50_latest_state_visibility_delay_s": fnum(result.get("p50_visibility_s")),
        "not_visible_fraction": fnum(result.get("not_visible_fraction")),
        "p95_survivor_delivery_delay_s": fnum(result.get("p95_survivor_delay_s")),
        "p50_control_path_delay_s": fnum(result.get("p50_control_delay_s")),
        "offered_update_rate_per_s": fnum(result.get("offered_rate_per_s")),
        "max_queue_backlog": result.get("max_backlog", ""),
        "span_s": fnum(result.get("span_s")),
        **extra,
    }


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    mod = load_relay()
    classifications = []
    for policy, folder in (("LatestOnly", "dynamic"), ("StaticSemantic", "dynamic")):
        for rep in range(5):
            path = FORMAL / f"raw/{folder}/gateway_events_{folder}_{policy}_rep{rep}_20260726.csv"
            classifications.append({"group": f"{folder}_{policy}", "rep": rep, **classify_log(path, policy)})
    churn = FORMAL / "raw/churn/gateway_events_churn_LatestOnly_20260726.csv"
    classifications.append({"group": "churn_LatestOnly", "rep": "file", **classify_log(churn, "LatestOnly")})

    pair_path = FORMAL / f"raw/dynamic/gateway_events_dynamic_LatestOnly_rep{PAIR_REP}_20260726.csv"
    phases = load_phases(FORMAL / f"raw/dynamic/phases_dynamic_LatestOnly_rep{PAIR_REP}_20260726.jsonl")
    events = offered_from_log(pair_path, "LatestOnly")
    cells = {event["cell"] for event in events}
    if len(cells) != 1:
        raise RuntimeError(f"expected one LatestOnly cell, found {cells}")
    default_rate = phases[0][1] if phases else CONTROLLED_SIG_BIT
    on_model = replay(mod, events, phases, True, 0, default_rate)
    off_model = replay(mod, events, phases, False, 0, default_rate)
    observed = observed_visibility(read_csv(pair_path), "LatestOnly")
    log_class = next(item for item in classifications if item["group"] == "dynamic_LatestOnly" and item["rep"] == PAIR_REP)

    controlled = controlled_extension_events()
    controlled_on = replay(mod, controlled, [], True, 0, CONTROLLED_SIG_BIT)
    controlled_off = replay(mod, controlled, [], False, 0, CONTROLLED_SIG_BIT)

    accounted = (
        log_class["offered_enqueued_upserts"] - observed["forwarded"] - log_class["superseded"]
    )
    observed_on = {
        "offered": log_class["offered_enqueued_upserts"],
        "extension": log_class["extension"],
        "duplicate": log_class["duplicate"],
        "other": log_class["other"],
        "anomalous": log_class["anomalous"],
        "superseded": log_class["superseded"],
        "forwarded": observed["forwarded"],
        "other_drops": accounted,
        "undrained_at_trace_end": accounted,
        "tail_drained": 0,
        "max_backlog": "",
        "p95_visibility_s": observed["p95_visibility_s"],
        "p50_visibility_s": observed["p50_visibility_s"],
        "not_visible_fraction": observed["not_visible_fraction"],
        "p95_survivor_delay_s": observed["p95_survivor_delay_s"],
        "p50_control_delay_s": observed["p50_survivor_delay_s"],
        "offered_rate_per_s": observed["offered_rate_per_s"],
        "span_s": observed["span_s"],
    }
    identity_off = {
        "offered": observed_on["offered"],
        "extension": 0, "duplicate": 0, "other": 0, "anomalous": 0, "superseded": 0,
        "forwarded": observed_on["offered"] if accounted == 0 else "",
        "other_drops": 0,
        "undrained_at_trace_end": 0,
        "tail_drained": "",
        "max_backlog": "",
        "p95_visibility_s": None,
        "p50_visibility_s": None,
        "not_visible_fraction": None,
        "p95_survivor_delay_s": None,
        "p50_control_delay_s": None,
        "offered_rate_per_s": observed["offered_rate_per_s"],
        "span_s": observed["span_s"],
    }
    mechanisms = "priority,replica_cap,adaptive,topk"
    pairing = [
        pairing_row(
            "A-low-observed", "merge_on", "OBSERVED_GATEWAY_LOG",
            str(pair_path.relative_to(ROOT)), observed_on,
            {
                "service_model": "none; counts and visibility are taken from the recorded LatestOnly log",
                "mechanisms_off": mechanisms,
                "note": "all classified supersessions are same-coverage duplicates",
            },
        ),
        pairing_row(
            "A-low-observed", "merge_off", "ACCOUNTING_IDENTITY",
            str(pair_path.relative_to(ROOT)), identity_off,
            {
                "service_model": "not used",
                "mechanisms_off": mechanisms,
                "note": "LatestOnly dropped only superseded frames and the log balances; merge-off forwards every offered update. Visibility delay was not assigned because the HTB replay does not match the log.",
            },
        ),
        pairing_row(
            "A-low-observed", "merge_on", "MODEL_REJECTED",
            str(pair_path.relative_to(ROOT)), on_model,
            {
                "service_model": "work-conserving HTB at the phase sig_bit values; frame_s=(104*8)/sig_bit",
                "mechanisms_off": mechanisms,
                "note": f"rejected: superseded {on_model['superseded']} vs log {log_class['superseded']}; p95 {on_model['p95_visibility_s']:.3f}s vs log {observed['p95_visibility_s']:.3f}s",
            },
        ),
        pairing_row(
            "A-low-observed", "merge_off", "MODEL_REJECTED",
            str(pair_path.relative_to(ROOT)), off_model,
            {
                "service_model": "work-conserving HTB at the phase sig_bit values; frame_s=(104*8)/sig_bit",
                "mechanisms_off": mechanisms,
                "note": "rejected with the merge-on replay; do not use these visibility numbers",
            },
        ),
        pairing_row(
            "A-high-controlled", "merge_on", "CONTROLLED",
            str(TRACE_ORIGINAL.relative_to(ROOT)), controlled_on,
            {
                "service_model": f"constant sig_bit={CONTROLLED_SIG_BIT}; arrival gap=frame/{CONTROLLED_ARRIVALS_PER_SERVICE}; owner=lineage%4",
                "mechanisms_off": mechanisms,
                "note": "compressed same-owner replay; not a measured vLLM extension frequency",
            },
        ),
        pairing_row(
            "A-high-controlled", "merge_off", "CONTROLLED",
            str(TRACE_ORIGINAL.relative_to(ROOT)), controlled_off,
            {
                "service_model": f"constant sig_bit={CONTROLLED_SIG_BIT}; arrival gap=frame/{CONTROLLED_ARRIVALS_PER_SERVICE}; owner=lineage%4",
                "mechanisms_off": mechanisms,
                "note": "compressed same-owner replay; not a measured vLLM extension frequency",
            },
        ),
    ]
    write_csv(OUT / "F6_A_MERGE_PAIRING.csv", pairing)

    class_rows = []
    for item in classifications:
        class_rows.append({
            "group": item["group"],
            "rep": item["rep"],
            "path": item["path"],
            "offered": item["offered_enqueued_upserts"],
            "forwarded": item["forwarded_upserts"],
            "extension": item["extension"],
            "duplicate": item["duplicate"],
            "other": item["other"],
            "tombstone": item["tombstone"],
            "anomalous": item["anomalous"],
            "true_extension_ratio": fnum(item["true_extension_ratio"]),
            "total_supersession_ratio": fnum(item["total_supersession_ratio"]),
            "duplicate_ratio": fnum(item["duplicate_ratio"]),
            "pending_duration_p50_s": fnum(item["pending_duration_p50_s"]),
            "pending_duration_p95_s": fnum(item["pending_duration_p95_s"]),
        })
    write_csv(OUT / "a_supersession_classification.csv", class_rows)

    cap_rows = []
    for fraction, tag in (("0.0", "0"), ("0.5", "50"), ("0.75", "75")):
        path = FORMAL / f"raw/calibration/gateway_events_calib_overlap{fraction}_20260726.csv"
        cells_path = FORMAL / f"summary/cells_calib_overlap{fraction}_20260726.csv"
        fullsync_meta = next(row for row in read_csv(cells_path) if row["policy"] == "FullSync")
        stream = offered_from_log(path, "FullSync")
        live_forwarded = sum(
            1 for row in read_csv(path)
            if row["policy"] == "FullSync" and row["op"] == "upsert" and row["stage"] == "forwarded"
        )
        if not stream:
            raise RuntimeError(f"no FullSync offered upserts in {path}")
        stats = multiplicity(stream)
        digests = set()
        owners: dict[int, set[int]] = {}
        for event in stream:
            digests.add(event["digest"])
            owners.setdefault(event["digest"], set()).add(event["owner"])
        arms = {}
        for name, dedup in (("no_suppression", 0), ("krep2", 2)):
            result = replay_cap(mod, stream, dedup)
            arms[name] = result
            base = arms.get("no_suppression")
            reduction = ""
            if name == "krep2" and base and base["forwarded"]:
                reduction = fnum(1.0 - result["forwarded"] / base["forwarded"])
            residual = ""
            if name == "krep2" and base:
                residual = (base["forwarded"] - result["forwarded"]) - result["replica_cap"]
            cap_rows.append({
                "overlap_percent": tag,
                "overlap_fraction": fraction,
                "strategy": name,
                "evidence": "CPU_REPLAY_OF_FULLSYNC_LOG",
                "source": str(path.relative_to(ROOT)),
                "seed": fullsync_meta["seed"],
                "pool_size": fullsync_meta["pool_size"],
                "workers": 4,
                "overlap_seed_prefixes": fullsync_meta["overlap_seed_prefixes"],
                "offered_advertisements": result["offered"],
                "forwarded_state_updates": result["forwarded"],
                "replica_cap_drops": result["replica_cap"],
                "other_drops": result["other_drops"],
                "window_end_unforwarded": result["window_end_unforwarded"],
                "live_fullsync_forwarded": live_forwarded if name == "no_suppression" else "",
                "live_fullsync_undrained": (result["offered"] - live_forwarded) if name == "no_suppression" else "",
                "drain": "untimed_full_drain",
                "reduction_vs_no_suppression": reduction,
                "forwarded_gap_minus_cap_drops": residual,
                "distinct_prefixes": stats["distinct_prefixes"],
                "prefixes_with_owners_gt_2": stats["prefixes_with_owners_gt_2"],
                "multiplicity_distribution": stats["multiplicity"],
                "max_multiplicity": stats["max_multiplicity"],
                "offered_owner_pairs": stats["owner_pairs"],
                "admitted_owner_pairs": result["admitted_pairs"],
                "suppressed_owner_advertisements": result["replica_cap"],
                "mean_remaining_visible_owners": fnum(
                    sum(len(result["admitted_owners"].get(digest, ())) for digest in digests) / len(digests)
                ),
                "missing_owner_pairs": stats["owner_pairs"] - result["admitted_pairs"],
                "metadata_payload_bytes": result["forwarded"] * 64,
                "metadata_wire_bytes_convention": result["forwarded"] * WIRE_BYTES,
                "mechanisms_off": "merge,priority,adaptive,topk",
            })
    write_csv(OUT / "F6_C_SUPPRESSION_PAIRING.csv", cap_rows)

    summary = {
        "signaling_cell_means": signaling_means(),
        "published_means_event_reconstruction": "NOT_RECONSTRUCTABLE",
        "published_means_reason": (
            "LatestOnly 37.78, StaticSemantic 30.95 and Adaptive 15.18 are means of eight "
            "workload x rho rows in signaling_accounting.csv. Raw gateway events are present "
            "only for the dynamic reuse_intensive nominal-rho-0.5 cells. Baseline "
            "original_compatible and the other reuse rho rows have metadata but no gateway_events CSV."
        ),
        "classifications": class_rows,
        "log_classification_rep0": {
            key: log_class[key] for key in (
                "extension", "duplicate", "other", "anomalous", "superseded",
                "true_extension_ratio", "total_supersession_ratio", "duplicate_ratio",
                "pending_duration_p50_s", "pending_duration_p95_s", "examples",
            )
        },
        "observed_visibility_rep0": observed,
        "model_on": on_model,
        "model_off": off_model,
        "controlled_on": controlled_on,
        "controlled_off": controlled_off,
        "phases_rep0": phases,
        "model_superseded_minus_log": on_model["superseded"] - log_class["superseded"],
        "model_forwarded_minus_log": on_model["forwarded"] - observed["forwarded"],
        "overlap_catalog": overlap_catalog(),
        "cap_rows": cap_rows,
        "controlled_trace_rows": len(controlled),
        "controlled_note": (
            "Owner is lineage_id mod 4 so a later step hits the same (instance, digest). "
            "Arrivals are equally spaced at one quarter of the 1754 bit/s frame time. "
            "This is not the formal baseline's lost gateway log and not a vLLM extension frequency."
        ),
    }
    (OUT / "ac_summary.json").write_text(json.dumps(summary, indent=2, default=str))
    print(json.dumps({
        "log_rep0": {
            "extension": log_class["extension"],
            "duplicate": log_class["duplicate"],
            "other": log_class["other"],
            "anomalous": log_class["anomalous"],
            "offered": log_class["offered_enqueued_upserts"],
        },
        "model_match": {
            "superseded_model": on_model["superseded"],
            "superseded_log": log_class["superseded"],
            "forwarded_model": on_model["forwarded"],
            "forwarded_log": observed["forwarded"],
            "p95_model": on_model["p95_visibility_s"],
            "p95_log": observed["p95_visibility_s"],
        },
        "off": {
            "forwarded": off_model["forwarded"],
            "p95": off_model["p95_visibility_s"],
            "superseded": off_model["superseded"],
        },
        "controlled": {
            "on_ext": controlled_on["extension"],
            "on_dup": controlled_on["duplicate"],
            "on_fwd": controlled_on["forwarded"],
            "on_p95": controlled_on["p95_visibility_s"],
            "off_fwd": controlled_off["forwarded"],
            "off_p95": controlled_off["p95_visibility_s"],
        },
        "cap": [
            {
                "overlap": row["overlap_percent"],
                "strategy": row["strategy"],
                "offered": row["offered_advertisements"],
                "forwarded": row["forwarded_state_updates"],
                "drops": row["replica_cap_drops"],
                "reduction": row["reduction_vs_no_suppression"],
                "gt2": row["prefixes_with_owners_gt_2"],
                "multiplicity": row["multiplicity_distribution"],
            }
            for row in cap_rows
        ],
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
