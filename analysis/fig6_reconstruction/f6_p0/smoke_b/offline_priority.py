#!/usr/bin/env python3
"""CONTROLLED offline smoke for Fig. 6(b) and Fig. 6(d).

Uses the frozen Relay admission/service order and the frozen Dispatcher.choose.
Does not start vLLM, does not send TCP, and does not modify experiments/.

Labels:
  CONTROLLED  — stipulated queue, residency, and load
  DERIVED     — cost from the formal T model, not a measured TTFT
  OBSERVED    — counts read from analysis/formal4t4 summary tables
"""
from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path("/home/byh/B02")
RELAY_PATH = ROOT / "experiments/4t4/net/gateway_relay_4t4.py"
HARNESS_PATH = ROOT / "experiments/4t4/run_formal4t4.py"
OUT = ROOT / "analysis/fig6_reconstruction/f6_p0"
SIGNALING = ROOT / "analysis/formal4t4/summary/signaling_accounting.csv"
CORRECTNESS = ROOT / "analysis/formal4t4/summary/correctness.csv"

PREFILL_TOKENS_PER_MS = 50.0
QUEUE_PENALTY_MS = 2.0
GUARD_MS = 0.5
STALE_THRESHOLD = 512
COVERAGE = 2048
PROMPT_LEN = 2048
NOISE_AHEAD = 8


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def new_relay(relay_mod, *, priority: bool, dedup: int):
    args = argparse.Namespace(max_queue=4096, quiet=True, tau=1.0, util_lambda=16, gate=1.0)
    relay = relay_mod.Relay(args)
    relay.mode = relay_mod.MODE_STATIC
    relay.merge = False
    relay.priority = priority
    relay.adaptive = False
    relay.dedup = dedup
    relay.global_topk = 0
    relay.current_cell = 1
    return relay


def enqueue(relay_mod, relay, kind: int, instance: int, seq: int, coverage: int, digest: int) -> None:
    data = relay_mod.frame(kind, instance, 1, seq, coverage, digest, 0.0)
    relay.enqueue(data, kind, instance, seq, coverage, digest, 0.0)


def drain(relay_mod, relay) -> list[tuple[int, int, int, int]]:
    order = []
    while True:
        data = relay._dequeue()
        if data is None:
            break
        kind, instance, _cell, seq, coverage, digest, _sent = relay_mod.HDR.unpack(data[:32])
        order.append((kind, instance, seq, coverage, int(digest)))
    return order


def make_dispatcher(harness):
    harness.URLS = [f"http://127.0.0.1:{8000 + index}" for index in range(4)]
    return harness.Dispatcher(4, PREFILL_TOKENS_PER_MS, QUEUE_PENALTY_MS, GUARD_MS)


def request(harness, digest: str):
    return harness.TraceRequest(
        request_id=1, phase=1, lineage_id=1, step=1, tenant="t",
        digest=digest, coverage_tokens=COVERAGE, workload="controlled", discard=False,
    )


def model_cost(load: int, truth_coverage: int) -> float:
    prefill_tokens = max(0, PROMPT_LEN - truth_coverage)
    return load * QUEUE_PENALTY_MS + prefill_tokens / PREFILL_TOKENS_PER_MS


def choose_row(dispatcher, req) -> dict:
    target, raw, evaluated, affinity, coverage, expected = dispatcher.choose(req)
    return {
        "selected": target,
        "raw_fanout": raw,
        "evaluated": evaluated,
        "affinity": bool(affinity),
        "view_coverage": coverage,
        "expected_net_ms": expected,
    }


def smoke_b(relay_mod, harness) -> list[dict]:
    """Same frames. Only the priority flag changes. Eviction is stipulated."""
    digest_text = "controlled-prefix"
    digest = harness.digest64(digest_text)
    rows = []
    for priority in (False, True):
        relay = new_relay(relay_mod, priority=priority, dedup=0)
        seq = 1
        for index in range(NOISE_AHEAD):
            enqueue(relay_mod, relay, relay_mod.K_UP, 1, seq, 64, digest + 1 + index)
            seq += 1
        enqueue(relay_mod, relay, relay_mod.K_TOMB, 0, seq, 0, digest)
        order = drain(relay_mod, relay)
        tomb_at = next(i for i, item in enumerate(order) if item[0] == relay_mod.K_TOMB)

        for served_before_request in (0, 1, tomb_at + 1):
            dispatcher = make_dispatcher(harness)
            dispatcher.loads = [5, 0, 0, 0]
            dispatcher.apply_upsert(0, digest_text, COVERAGE, 0.0, 0.0)
            for kind, instance, _seq, coverage, wire_digest in order[:served_before_request]:
                if kind == relay_mod.K_TOMB and wire_digest == digest:
                    dispatcher.apply_tombstone(0, digest_text)
                elif kind == relay_mod.K_UP:
                    dispatcher.apply_upsert(instance, f"noise-{wire_digest}", coverage, 0.0, 0.0)
            decision = choose_row(dispatcher, request(harness, digest_text))
            truth_coverage = 0
            stale = bool(
                decision["affinity"]
                and decision["view_coverage"] >= STALE_THRESHOLD
                and truth_coverage < STALE_THRESHOLD
            )
            rows.append({
                "label": "CONTROLLED",
                "strategy": "InvalidationPriority" if priority else "InvalidationFIFO",
                "priority_flag": int(priority),
                "merge": 0,
                "dedup": 0,
                "noise_upserts_ahead": NOISE_AHEAD,
                "tomb_queue_slot": tomb_at,
                "frames_served_before_request": served_before_request,
                "tomb_applied": int(served_before_request > tomb_at),
                "truth_coverage_worker0": truth_coverage,
                "stale_positive": int(stale),
                **decision,
            })
    return rows


def smoke_d(relay_mod, harness) -> list[dict]:
    digest_text = "shared-prefix"
    digest = harness.digest64(digest_text)
    scenarios = {
        "D-A-similar-load": [2, 2, 2, 2],
        "D-B-load-imbalance": [8, 8, 0, 8],
    }
    rows = []
    for name, loads in scenarios.items():
        truth = {index: COVERAGE for index in range(4)}
        costs = {index: model_cost(loads[index], truth[index]) for index in range(4)}
        best = min(costs, key=lambda index: (costs[index], index))
        best_set = [index for index, cost in costs.items() if cost == costs[best]]
        for krep in (0, 2, 1):
            relay = new_relay(relay_mod, priority=False, dedup=krep)
            admitted = []
            for instance in range(4):
                before = relay.drops["replica_cap"]
                enqueue(relay_mod, relay, relay_mod.K_UP, instance, instance + 1, COVERAGE, digest)
                if relay.drops["replica_cap"] == before:
                    admitted.append(instance)
            order = drain(relay_mod, relay)
            dispatcher = make_dispatcher(harness)
            dispatcher.loads = list(loads)
            for kind, instance, _seq, coverage, _wire in order:
                if kind == relay_mod.K_UP:
                    dispatcher.apply_upsert(instance, digest_text, coverage, 0.0, 0.0)
            decision = choose_row(dispatcher, request(harness, digest_text))
            selected = decision["selected"]
            regret = costs[selected] - costs[best]
            visible = [index for index in range(4) if dispatcher.index[index].get(digest_text, 0) > 0]
            hidden = [index for index in range(4) if index not in visible]
            rows.append({
                "label": "CONTROLLED",
                "scenario": name,
                "krep": "full" if krep == 0 else str(krep),
                "loads": ";".join(map(str, loads)),
                "truth_coverage": ";".join(str(truth[index]) for index in range(4)),
                "admitted_owners": ";".join(map(str, admitted)),
                "visible_owners": ";".join(map(str, visible)),
                "hidden_owners": ";".join(map(str, hidden)),
                "visible_owner_count": len(visible),
                "suppressed_owner_count": len(hidden),
                "replica_ads_forwarded": len(order),
                "replica_ads_dropped": relay.drops["replica_cap"],
                "best_owners": ";".join(map(str, best_set)),
                "best_owner_hidden": int(all(index in hidden for index in best_set)),
                "selected": selected,
                "selected_changed_vs_full": "",
                "model_cost_selected_ms": costs[selected],
                "model_cost_best_ms": costs[best],
                "routing_regret_ms": regret,
                "coverage_difference_tokens": truth[best] - dispatcher.index[selected].get(digest_text, 0),
                "prefill_cost_difference_ms": (
                    max(0, PROMPT_LEN - dispatcher.index[selected].get(digest_text, 0))
                    - max(0, PROMPT_LEN - truth[best])
                ) / PREFILL_TOKENS_PER_MS,
                "affinity": int(decision["affinity"]),
                "cost_note": "DERIVED T_i = load_i * 2ms + max(0, prompt-coverage_i) / 50 token/ms; not measured TTFT",
            })
        full_selected = next(row["selected"] for row in rows if row["scenario"] == name and row["krep"] == "full")
        for row in rows:
            if row["scenario"] == name:
                row["selected_changed_vs_full"] = int(row["selected"] != full_selected)
    return rows


def historical() -> dict:
    signaling_rows = list(csv.DictReader(SIGNALING.open()))
    by_policy = {}
    for row in signaling_rows:
        policy = row["policy"]
        bucket = by_policy.setdefault(policy, Counter())
        bucket["n"] += 1
        for key in (
            "source_tombstones_sent_mean",
            "relay_drop_superseded_mean",
            "relay_drop_duplicate_holder_mean",
            "relay_forwarded_mean",
            "source_upserts_sent_mean",
        ):
            bucket[key] += float(row[key])
    signaling = {}
    for policy, bucket in by_policy.items():
        n = bucket["n"]
        signaling[policy] = {
            "cells": n,
            "tombstones_sent_mean": bucket["source_tombstones_sent_mean"] / n,
            "superseded_mean": bucket["relay_drop_superseded_mean"] / n,
            "duplicate_holder_mean": bucket["relay_drop_duplicate_holder_mean"] / n,
            "forwarded_mean": bucket["relay_forwarded_mean"] / n,
            "upserts_sent_mean": bucket["source_upserts_sent_mean"] / n,
        }
    correctness = []
    for row in csv.DictReader(CORRECTNESS.open()):
        if row["stage"] not in {"baseline", "churn"}:
            continue
        correctness.append({
            "stage": row["stage"],
            "policy": row["policy"],
            "physical_false_positive_affinity_rate_mean": row["physical_false_positive_affinity_rate_mean"],
            "tombstone_delay_p95_s_mean": row["tombstone_delay_p95_s_mean"],
            "forced_owner_resets_mean": row["forced_owner_resets_mean"],
            "stale_fallback_rate_mean": row["stale_fallback_rate_mean"],
        })
    return {"signaling_mean_across_rho": signaling, "correctness": correctness}


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    relay_mod = load_module("gateway_relay_4t4", RELAY_PATH)
    harness = load_module("run_formal4t4", HARNESS_PATH)
    b_rows = smoke_b(relay_mod, harness)
    d_rows = smoke_d(relay_mod, harness)
    write_csv(OUT / "smoke_b" / "priority_window.csv", b_rows)
    write_csv(OUT / "smoke_d" / "replica_regret.csv", d_rows)
    history = historical()
    (OUT / "audit").mkdir(parents=True, exist_ok=True)
    (OUT / "summary").mkdir(parents=True, exist_ok=True)
    (OUT / "audit" / "historical_formal4t4.json").write_text(json.dumps(history, indent=2) + "\n")
    summary = {
        "b_stale_when_one_frame_served": {
            row["strategy"]: row["stale_positive"]
            for row in b_rows if row["frames_served_before_request"] == 1
        },
        "b_tomb_slot": {
            row["strategy"]: row["tomb_queue_slot"]
            for row in b_rows if row["frames_served_before_request"] == 0
        },
        "d": [
            {
                "scenario": row["scenario"],
                "krep": row["krep"],
                "regret_ms": row["routing_regret_ms"],
                "ads_forwarded": row["replica_ads_forwarded"],
                "ads_dropped": row["replica_ads_dropped"],
                "best_owner_hidden": row["best_owner_hidden"],
                "selected": row["selected"],
            }
            for row in d_rows
        ],
    }
    (OUT / "summary" / "smoke_summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))
    print("signaling", json.dumps(history["signaling_mean_across_rho"], indent=2))


if __name__ == "__main__":
    main()
