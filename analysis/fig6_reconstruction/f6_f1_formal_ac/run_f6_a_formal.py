#!/usr/bin/env python3
"""Formal Fig. 6(a) summary of the five recorded dynamic LatestOnly logs.

Reuses the F6-P3 join. Does not replay the rejected HTB model and does not
read the baseline corpus.
"""
from __future__ import annotations

import csv
import importlib.util
import json
import statistics
import sys
from pathlib import Path

ROOT = Path("/home/byh/B02")
OUT = ROOT / "analysis/fig6_reconstruction/f6_f1_formal_ac"
P3 = ROOT / "analysis/fig6_reconstruction/f6_p3/run_ac_readiness.py"
DYNAMIC = ROOT / "analysis/formal4t4/raw/dynamic"
POLICY = "LatestOnly"


def load_p3():
    spec = importlib.util.spec_from_file_location("f6p3_ac", P3)
    module = importlib.util.module_from_spec(spec)
    sys.modules["f6p3_ac"] = module
    spec.loader.exec_module(module)
    return module


def fnum(value: float | None, digits: int = 6) -> str:
    if value is None:
        return ""
    return f"{value:.{digits}f}"


def read_rows(path: Path) -> list[dict]:
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


def audit_rep(p3, rep: int) -> dict:
    path = DYNAMIC / f"gateway_events_dynamic_{POLICY}_rep{rep}_20260726.csv"
    meta_path = DYNAMIC / f"run_metadata_dynamic_{POLICY}_rep{rep}_20260726.json"
    meta = json.loads(meta_path.read_text())
    classified = p3.classify_log(path, POLICY)
    rows = [row for row in read_rows(path) if row["policy"] == POLICY and row["op"] == "upsert"]
    enqueued = [row for row in rows if row["stage"] == "enqueued"]
    forwarded = [row for row in rows if row["stage"] == "forwarded"]
    suppressed = [row for row in rows if row["stage"] == "suppressed"]
    superseded_rows = [row for row in suppressed if row["suppression_reason"] == "superseded"]
    other_rows = [row for row in suppressed if row["suppression_reason"] != "superseded"]
    enqueued_ids = {int(row["update_id"]) for row in enqueued}
    other_not_enqueued = [row for row in other_rows if int(row["update_id"]) not in enqueued_ids]
    other_after_enqueue = [row for row in other_rows if int(row["update_id"]) in enqueued_ids]
    offered = len(enqueued)
    forwarded_n = len(forwarded)
    superseded_n = len(superseded_rows)
    # A superseded row names an update that was already counted when it was enqueued.
    undrained = offered - forwarded_n - superseded_n - len(other_after_enqueue)
    balance = forwarded_n + superseded_n + len(other_after_enqueue) + undrained
    balanced = balance == offered and not other_not_enqueued and undrained == 0
    identity_ok = balanced and classified["extension"] == classified["extension"] and superseded_n == classified["superseded"]
    # Merge-off count is an identity only when the log balances, LatestOnly
    # removed nothing except superseded frames, and the queue drained.
    merge_off_ok = (
        balanced
        and undrained == 0
        and not other_rows
        and classified["other"] == 0
        and classified["anomalous"] == 0
        and classified["tombstone"] == 0
        and classified["superseded"] == superseded_n
    )
    arrivals = [float(row["gateway_arrival_time"]) for row in enqueued]
    span = (max(arrivals) - min(arrivals)) if len(arrivals) > 1 else 0.0
    reduction = (1.0 - forwarded_n / offered) if offered and merge_off_ok else None
    return {
        "rep": rep,
        "seed": meta["arguments"]["seed"],
        "workload": meta["arguments"]["workload"],
        "n_requests": meta["arguments"]["n_requests"],
        "pool_size": meta["arguments"]["pool_size"],
        "overlap": meta["arguments"]["overlap"],
        "rhos": meta["arguments"]["rhos"],
        "source": str(path.relative_to(ROOT)),
        "offered_updates": offered,
        "forwarded_updates": forwarded_n,
        "superseded_updates": superseded_n,
        "true_extension_supersessions": classified["extension"],
        "same_coverage_duplicate_supersessions": classified["duplicate"],
        "tombstone_supersessions": classified["tombstone"],
        "other_supersessions": classified["other"],
        "anomalous_supersessions": classified["anomalous"],
        "other_drops": len(other_rows),
        "other_drops_not_enqueued": len(other_not_enqueued),
        "undrained": undrained,
        "balance_sum": balance,
        "balance_ok": int(balance == offered and not other_not_enqueued),
        "accounting_identity_ok": int(merge_off_ok),
        "merge_off_evidence": "ACCOUNTING_IDENTITY" if merge_off_ok else "NOT_BALANCED",
        "forwarded_merge_off": offered if merge_off_ok else "",
        "paired_count_difference": (offered - forwarded_n) if merge_off_ok else "",
        "reduction": reduction,
        "true_extension_ratio": (classified["extension"] / offered) if offered else None,
        "supersession_ratio": (superseded_n / offered) if offered else None,
        "offered_update_rate_per_s": (offered / span) if span > 0 else None,
        "span_s": span,
        "pending_duration_p50_s": classified["pending_duration_p50_s"],
        "pending_duration_p95_s": classified["pending_duration_p95_s"],
        "classifier_superseded": classified["superseded"],
        "identity_classifier_match": int(identity_ok and classified["superseded"] == superseded_n),
    }


def summarize(rows: list[dict]) -> list[dict]:
    eligible = [row for row in rows if row["accounting_identity_ok"]]
    def series(name: str) -> list[float]:
        return [float(row[name]) for row in eligible]

    reductions = series("reduction")
    offered = series("offered_updates")
    same_input = len(set(offered)) == 1
    pooled_forwarded = sum(series("forwarded_updates"))
    pooled_offered = sum(offered)
    out = []
    for name in (
        "offered_updates", "forwarded_updates", "superseded_updates",
        "true_extension_supersessions", "same_coverage_duplicate_supersessions",
        "reduction", "supersession_ratio", "true_extension_ratio",
        "paired_count_difference", "offered_update_rate_per_s",
    ):
        values = series(name)
        sample = statistics.stdev(values) if len(values) > 1 else 0.0
        out.append({
            "metric": name,
            "n_reps": len(values),
            "mean": fnum(statistics.fmean(values)),
            "sample_stdev": fnum(sample),
            "min": fnum(min(values)),
            "max": fnum(max(values)),
        })
    out.append({
        "metric": "macro_mean_reduction",
        "n_reps": len(reductions),
        "mean": fnum(statistics.fmean(reductions)),
        "sample_stdev": fnum(statistics.stdev(reductions) if len(reductions) > 1 else 0.0),
        "min": fnum(min(reductions)),
        "max": fnum(max(reductions)),
    })
    out.append({
        "metric": "pooled_reduction" if same_input else "pooled_reduction_inputs_differ",
        "n_reps": len(eligible),
        "mean": fnum(1.0 - pooled_forwarded / pooled_offered),
        "sample_stdev": "",
        "min": "",
        "max": "",
    })
    return out


def write_csv(path: Path, rows: list[dict]) -> None:
    columns = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    p3 = load_p3()
    reps = [audit_rep(p3, rep) for rep in range(5)]
    per_rep = []
    for row in reps:
        per_rep.append({
            key: (fnum(value) if isinstance(value, float) else value)
            for key, value in row.items()
        })
    summary = summarize(reps)
    write_csv(OUT / "f6_a_per_rep.csv", per_rep)
    write_csv(OUT / "f6_a_summary.csv", summary)
    (OUT / "f6_a_audit.json").write_text(json.dumps({"reps": reps, "summary": summary}, indent=2))
    print(json.dumps({
        "reps": [
            {
                "rep": row["rep"],
                "seed": row["seed"],
                "offered": row["offered_updates"],
                "forwarded": row["forwarded_updates"],
                "superseded": row["superseded_updates"],
                "extension": row["true_extension_supersessions"],
                "duplicate": row["same_coverage_duplicate_supersessions"],
                "balance_ok": row["balance_ok"],
                "identity": row["merge_off_evidence"],
                "reduction": row["reduction"],
            }
            for row in reps
        ],
        "summary": summary,
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
