#!/usr/bin/env python3
"""Read the frozen ICC summaries. Does not start vLLM or the gateway.

Numbers come from analysis/icc_kv, which is gitignored. The CSVs written
next to this script are the audit extract. A missing field stays blank.
"""
from __future__ import annotations

import csv
import json
import statistics
from collections import defaultdict
from pathlib import Path

ROOT = Path("/home/byh/B02")
ICC = ROOT / "analysis/icc_kv"
OUT = ROOT / "analysis/p0_adaptive/d1_topk_boundary"
KSWEEP = ICC / "formal_A_5d2d300/ksweep_ov30"
METHODS = ["StaticTopK4", "StaticTopK8", "StaticTopK16", "StaticTopK32", "StaticTopK64", "Ideal"]


def load_summary(path: Path) -> tuple[dict, list[dict]]:
    meta = json.loads(path.read_text())
    return meta, meta["rows"]


def mean(values: list[float]) -> float | None:
    return None if not values else sum(values) / len(values)


def sd(values: list[float]) -> float | None:
    return None if len(values) < 2 else statistics.stdev(values)


def write_csv(path: Path, rows: list[dict]) -> None:
    columns = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def blank(value):
    return "" if value is None else value


def sweep_rows() -> list[dict]:
    meta, rows = load_summary(KSWEEP / "e2e_summary.json")
    out = []
    for row in rows:
        if not str(row["method"]).startswith("StaticTopK") and row["method"] != "Ideal":
            continue
        sent = row["foreground_up_sent"] or 0
        out.append({
            "source": "formal_A_5d2d300/ksweep_ov30",
            "git_commit": meta["commit"],
            "link_bit_s": meta["fixed_link_bit_s"],
            "scenario": row["scenario"],
            "workload": "ov30",
            "useful_pool": row["useful_pool"],
            "noise_overlap": row["noise_overlap"],
            "seed": row["seed"],
            "method": row["method"],
            "configured_k": "" if row["method"] == "Ideal" else row["method"].replace("StaticTopK", ""),
            "label": "OBSERVED",
            "elapsed_s": row["elapsed_s"],
            "measured_rho": row["measured_rho"],
            "loose_false_negative_rate": row["loose_false_negative_rate"],
            "loose_false_positive_rate": row["loose_false_positive_rate"],
            "ttft_mean_ms": row["ttft_mean_ms"],
            "ttft_p95_ms": row["ttft_p95_ms"],
            "reused_tokens_mean": row["reused_tokens_mean"],
            "foreground_lag_p95_s": row["foreground_lag_p95_s"],
            "missing_age_p95_s": row["missing_age_p95_s"],
            "relay_max_queue": row["relay_max_queue"],
            "relay_drop_global_topk": row["relay_drop_global_topk"],
            "relay_forwarded": row["relay_forwarded"],
            "relay_received": row["relay_received"],
            "forwarded_per_s": row["forwarded_per_s"],
            "gateway_cpu": row["gateway_cpu"],
            "foreground_undelivered": row["foreground_undelivered"],
            "foreground_up_sent": sent,
            "undelivered_fraction": row["foreground_undelivered"] / sent if sent else "",
            "scored_requests": row["scored_requests"],
            "time_at_cap": "NOT_MEASURED",
            "rank_17_24_coverage": "NOT_MEASURED",
            "rank_25_32_coverage": "NOT_MEASURED",
        })
    return out


def request_table(method: str, seed: int) -> dict[str, dict]:
    path = KSWEEP / f"requests_ultrahigh_{method}_seed{seed}.csv"
    rows = {}
    with path.open() as handle:
        for row in csv.DictReader(handle):
            if row["warmup"] == "1":
                continue
            rows[row["request_id"]] = row
    return rows


def harmful_rows() -> list[dict]:
    """Request-level FN flips. Not an update-level eviction class."""
    out = []
    for seed in (0, 1, 2):
        base = request_table("StaticTopK16", seed)
        for method in ("StaticTopK8", "StaticTopK32", "StaticTopK64"):
            other = request_table(method, seed)
            fixed = created = both = compared = 0
            deltas = []
            for request_id, left in base.items():
                right = other.get(request_id)
                if right is None:
                    continue
                if left["slot"] != right["slot"] or left["coverage_tokens"] != right["coverage_tokens"]:
                    continue
                compared += 1
                a = int(left["loose_false_negative"])
                b = int(right["loose_false_negative"])
                fixed += int(a == 1 and b == 0)
                created += int(a == 0 and b == 1)
                both += int(a == 1 and b == 1)
                if left["ttft_ms"] and right["ttft_ms"]:
                    deltas.append(float(right["ttft_ms"]) - float(left["ttft_ms"]))
            out.append({
                "source": "formal_A_5d2d300/ksweep_ov30",
                "seed": seed,
                "baseline": "StaticTopK16",
                "other": method,
                "compared_requests": compared,
                "fn_fixed": fixed,
                "fn_created": created,
                "fn_both": both,
                "mean_ttft_delta_ms": mean(deltas),
                "update_class": "COUNTERFACTUAL_NOT_VERIFIED",
                "note": "Separate end-to-end cells with the same request ids. Not a trace of one evicted update.",
            })
    return out


def pressure_rows() -> list[dict]:
    sources = [
        ("formal_A_5d2d300/ksweep_ov30", "ov30", KSWEEP / "e2e_summary.json"),
        ("formal_A_5d2d300/main_base", "base", ICC / "formal_A_5d2d300/main_base/e2e_summary.json"),
        ("formal_B_5d2d300/rhoscan_base", "base", ICC / "formal_B_5d2d300/rhoscan_base/e2e_summary.json"),
    ]
    out = []
    for source, workload, path in sources:
        meta, rows = load_summary(path)
        for row in rows:
            method = row["method"]
            if method not in ("StaticTopK16", "StaticTopK4", "StaticTopK8", "StaticTopK32", "StaticTopK64"):
                continue
            if source.endswith("main_base") and not (method == "StaticTopK16" and row["scenario"] == "ultrahigh"):
                continue
            if source.endswith("rhoscan_base") and method != "StaticTopK16":
                continue
            k = int(method.replace("StaticTopK", ""))
            out.append({
                "source": source,
                "git_commit": meta["commit"],
                "link_bit_s": meta["fixed_link_bit_s"],
                "workload": workload,
                "useful_pool": row["useful_pool"],
                "noise_overlap": row["noise_overlap"],
                "scenario": row["scenario"],
                "seed": row["seed"],
                "method": method,
                "configured_k": k,
                "peak_pending_frames": row["relay_max_queue"],
                "peak_reaches_k": int(row["relay_max_queue"] >= k),
                "topk_drops": row["relay_drop_global_topk"],
                "forwarded_per_s": row["forwarded_per_s"],
                "loose_false_negative_rate": row["loose_false_negative_rate"],
                "foreground_undelivered": row["foreground_undelivered"],
                "window_10ms_lineages": "NOT_MEASURED",
                "window_100ms_lineages": "NOT_MEASURED",
                "window_1s_lineages": "NOT_MEASURED",
                "time_at_cap_s": "NOT_MEASURED",
            })
    return out


def tradeoff_rows(sweep: list[dict]) -> list[dict]:
    by = defaultdict(dict)
    for row in sweep:
        if row["method"] == "Ideal":
            continue
        by[int(row["seed"])][row["method"]] = row
    out = []
    for seed, group in sorted(by.items()):
        base = group["StaticTopK16"]
        for method in ("StaticTopK8", "StaticTopK32", "StaticTopK64"):
            other = group[method]
            out.append({
                "source": "formal_A_5d2d300/ksweep_ov30",
                "seed": seed,
                "from_method": "StaticTopK16",
                "to_method": method,
                "delta_fn_rate": other["loose_false_negative_rate"] - base["loose_false_negative_rate"],
                "delta_ttft_mean_ms": other["ttft_mean_ms"] - base["ttft_mean_ms"],
                "delta_reused_tokens": other["reused_tokens_mean"] - base["reused_tokens_mean"],
                "delta_foreground_undelivered": other["foreground_undelivered"] - base["foreground_undelivered"],
                "delta_forwarded": other["relay_forwarded"] - base["relay_forwarded"],
                "delta_forwarded_per_s": other["forwarded_per_s"] - base["forwarded_per_s"],
                "delta_lag_p95_s": other["foreground_lag_p95_s"] - base["foreground_lag_p95_s"],
                "delta_gateway_cpu": other["gateway_cpu"] - base["gateway_cpu"],
                "delta_max_queue": other["relay_max_queue"] - base["relay_max_queue"],
                "label": "OBSERVED_PAIRED_CELL",
            })
    return out


def main() -> None:
    sweep = sweep_rows()
    harmful = harmful_rows()
    pressure = pressure_rows()
    trade = tradeoff_rows(sweep)
    write_csv(OUT / "static_k_sweep.csv", sweep)
    write_csv(OUT / "topk_harmful_drop_analysis.csv", harmful)
    write_csv(OUT / "topk_candidate_pressure.csv", pressure)
    write_csv(OUT / "capacity_visibility_tradeoff.csv", trade)
    print(json.dumps({
        "sweep_rows": len(sweep),
        "harmful_rows": len(harmful),
        "pressure_rows": len(pressure),
        "trade_rows": len(trade),
    }))


if __name__ == "__main__":
    main()
