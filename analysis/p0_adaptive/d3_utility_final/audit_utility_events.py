#!/usr/bin/env python3
"""Classify historical Adaptive drops. Does not start vLLM or the gateway."""
from __future__ import annotations

import csv
from collections import defaultdict
from pathlib import Path

OUT = Path("/home/byh/B02/analysis/p0_adaptive/d3_utility_final")
FORMAL = Path("/home/byh/B02/analysis/formal4t4")
IMMEDIATE_S = 0.05


def load_cells(path: Path) -> dict[str, dict]:
    meta: dict[str, dict] = {}
    with path.open() as handle:
        for row in csv.DictReader(handle):
            meta[row["relay_cell_id"]] = row
            meta[row["cell_id"]] = row
    return meta


def classify_update(events: list[dict]) -> tuple[str, dict]:
    enqueued = [event for event in events if event["stage"] == "enqueued"]
    suppressed = [event for event in events if event["stage"] == "suppressed"]
    terminal = suppressed[-1] if suppressed else None
    sample = terminal or events[-1]
    coverage = int(float(sample["coverage_tokens"]))
    if terminal is None:
        return "ADMITTED_OR_FORWARDED", sample
    reason = terminal["suppression_reason"]
    age = float(terminal["queue_age_s"] or 0)
    if reason == "low_utility" and not enqueued:
        if coverage <= 1024:
            kind = "LOW_COVERAGE_REJECT"
        else:
            kind = "FRESHNESS_DEPENDENT_REJECT"
    elif reason == "low_utility" and enqueued and age < IMMEDIATE_S:
        kind = "TOPK_SHRINK_MISLABELED"
    elif reason == "low_utility" and enqueued:
        kind = "POST_ENQUEUE_NOT_SEPARABLE"
    elif reason == "duplicate_holder":
        kind = "REPLICA_SUPPRESSION"
    elif reason == "superseded":
        kind = "SEMANTIC_SUPERSESSION"
    elif reason == "global_topk":
        kind = "TOPK_CAPACITY_REJECT"
    else:
        kind = "OTHER"
    return kind, terminal


def formal_rows(workload: str, events: Path, cells: Path) -> tuple[list[dict], list[dict]]:
    meta = load_cells(cells)
    grouped: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    with events.open() as handle:
        for row in csv.DictReader(handle):
            if row["policy"] != "Adaptive" or row["op"] != "upsert":
                continue
            grouped[(row["cell"], row["update_id"])].append(row)
    per_cell: dict[str, dict] = {}
    breakdown: list[dict] = []
    for (cell, _update), events_for_update in grouped.items():
        kind, terminal = classify_update(events_for_update)
        info = meta.get(cell, {})
        rho = info.get("rho", "")
        coverage = int(float(terminal["coverage_tokens"]))
        age = float(terminal.get("queue_age_s") or 0) if terminal.get("stage") == "suppressed" else ""
        bucket = per_cell.setdefault(cell, {
            "workload": workload,
            "rho": rho,
            "rep": info.get("rep", ""),
            "cell": cell,
            "ttft_mean_ms": info.get("ttft_mean_ms", ""),
            "relay_drop_low_utility_stats": info.get("relay_drop_low_utility", ""),
            "gateway_global_topk": info.get("gateway_global_topk", ""),
            "relay_max_queue": info.get("relay_max_queue", ""),
            "code_commit": info.get("code_commit", ""),
        })
        bucket[kind] = bucket.get(kind, 0) + 1
        if kind == "ADMITTED_OR_FORWARDED":
            continue
        breakdown.append({
            "source": "formal4t4_gateway_events",
            "code_commit": info.get("code_commit", ""),
            "workload": workload,
            "rho": rho,
            "rep": info.get("rep", ""),
            "class": kind,
            "coverage_tokens": coverage,
            "queue_age_s": age,
            "label": "OBSERVED_EVENT" if kind != "TOPK_SHRINK_MISLABELED" else "DERIVED_FROM_CONTROL_FLOW",
            "note": {
                "LOW_COVERAGE_REJECT": "Pre-enqueue low_utility. U<=0 at age under 2 ms because coverage is 1024 and the bar is 1024.",
                "FRESHNESS_DEPENDENT_REJECT": "Pre-enqueue low_utility of a coverage above 1024.",
                "TOPK_SHRINK_MISLABELED": "Enqueued, so utility had already returned false, then suppressed within 50 ms. 1dc24ec trim increments low_utility and writes that reason.",
                "POST_ENQUEUE_NOT_SEPARABLE": "Enqueued, then low_utility after 50 ms. Trim of a waited frame and the dequeue recheck both emit this reason. EWMA is not in the event.",
                "REPLICA_SUPPRESSION": "duplicate_holder",
                "SEMANTIC_SUPERSESSION": "superseded",
            }.get(kind, reason if (reason := terminal.get("suppression_reason")) else ""),
        })
    evidence = []
    for cell, bucket in per_cell.items():
        evidence.append({
            "source": "formal4t4",
            "git_commit": bucket.get("code_commit", ""),
            "workload": workload,
            "rho": bucket.get("rho", ""),
            "rep": bucket.get("rep", ""),
            "policy": "Adaptive",
            "pressure_trigger": "INFERRED_NOT_A_COUNTER",
            "stats_low_utility": bucket.get("relay_drop_low_utility_stats", ""),
            "pre_enqueue_utility": bucket.get("LOW_COVERAGE_REJECT", 0) + bucket.get("FRESHNESS_DEPENDENT_REJECT", 0),
            "low_coverage_reject": bucket.get("LOW_COVERAGE_REJECT", 0),
            "freshness_dependent_reject": bucket.get("FRESHNESS_DEPENDENT_REJECT", 0),
            "shrink_mislabeled": bucket.get("TOPK_SHRINK_MISLABELED", 0),
            "post_enqueue_not_separable": bucket.get("POST_ENQUEUE_NOT_SEPARABLE", 0),
            "replica_suppression": bucket.get("REPLICA_SUPPRESSION", 0),
            "semantic_supersession": bucket.get("SEMANTIC_SUPERSESSION", 0),
            "ttft_mean_ms": bucket.get("ttft_mean_ms", ""),
            "relay_max_queue": bucket.get("relay_max_queue", ""),
            "configured_k": bucket.get("gateway_global_topk", ""),
            "label": "OBSERVED",
        })
    return evidence, breakdown


def write(path: Path, rows: list[dict]) -> None:
    columns = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    evidence: list[dict] = []
    breakdown: list[dict] = []
    for workload, tag in (("reuse_intensive", "reuse"), ("original_compatible", "original")):
        ev, br = formal_rows(
            workload,
            FORMAL / f"raw/baseline/gateway_events_baseline_{tag}_20260726.csv",
            FORMAL / f"summary/cells_baseline_{tag}_20260726.csv",
        )
        evidence.extend(ev)
        breakdown.extend(br)
    # aggregate the breakdown for the freshness table
    aggregate: dict[tuple, int] = defaultdict(int)
    for row in breakdown:
        aggregate[(row["workload"], row["rho"], row["class"], row["coverage_tokens"], row["label"])] += 1
    fresh = []
    for (workload, rho, kind, coverage, label), count in sorted(aggregate.items()):
        fresh.append({
            "workload": workload,
            "rho": rho,
            "class": kind,
            "coverage_tokens": coverage,
            "count": count,
            "label": label,
            "pressure_coverage_threshold_same_decision": (
                "YES_AT_ADMISSION" if kind == "LOW_COVERAGE_REJECT"
                else "NO_FRESHNESS_REJECTS_OBSERVED" if kind == "FRESHNESS_DEPENDENT_REJECT"
                else "NOT_THIS_CLASS"
            ),
        })
    write(OUT / "existing_utility_evidence.csv", evidence)
    write(OUT / "utility_rejection_breakdown.csv", [
        {
            "workload": row["workload"], "rho": row["rho"], "class": row["class"],
            "coverage_tokens": row["coverage_tokens"], "label": row["label"],
        }
        for row in breakdown
    ])
    # The per-event file above is large. Replace it with the aggregate plus a note file? 
    # Keep aggregate as the delivered breakdown; the task asked for a breakdown csv, not one row per event.
    write(OUT / "utility_rejection_breakdown.csv", fresh)
    write(OUT / "freshness_vs_coverage_analysis.csv", fresh)
    print("cells", len(evidence), "classes", len(fresh))
    for row in fresh:
        if row["class"] in ("LOW_COVERAGE_REJECT", "FRESHNESS_DEPENDENT_REJECT", "TOPK_SHRINK_MISLABELED", "POST_ENQUEUE_NOT_SEPARABLE"):
            print(row["workload"], row["rho"], row["class"], row["coverage_tokens"], row["count"])


if __name__ == "__main__":
    main()
