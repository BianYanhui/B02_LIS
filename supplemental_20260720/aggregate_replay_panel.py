#!/usr/bin/env python3
"""Aggregate the 2026-07-20 multi-trace replay panel into one table.

For each trace family (nl2bash / mbpp_s200 / sharegpt_s200) and the closed_loop
mode, emits per-(admission, K) Exact-incremental saved-prefill ratios
(Rinc = (S - S_load) / (S_exact - S_load)) plus the Exact/Load totals and the
K-response curve for coverage_first and oracle_future_value.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

ROOT = Path("/home/byh/B02/supplemental_20260720")
OUT = ROOT / "analysis"
TRACES = ["nl2bash", "mbpp_s200", "sharegpt_s200"]
FAMILY = {
    "nl2bash": "agentic_tooluse (paper original)",
    "mbpp_s200": "agentic_tooluse (second task)",
    "sharegpt_s200": "real_chat_multiturn",
}


def load(path: Path) -> list[dict]:
    with path.open() as handle:
        return list(csv.DictReader(handle))


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    rows_out: list[dict] = []
    for name in TRACES:
        rows = load(ROOT / f"replay_{name}_admission" / "agenttrace_admission_summary.csv")
        cl = [r for r in rows if r["mode"] == "closed_loop"]
        exact = next(r for r in cl if r["policy"] == "exact")
        load_only = next(r for r in cl if r["policy"] == "load_only")
        es = float(exact["saved_prefill_tokens_total_mean"])
        ls = float(load_only["saved_prefill_tokens_total_mean"])
        margin = es - ls
        for r in cl:
            if r["policy"] != "sketch":
                continue
            v = float(r["saved_prefill_tokens_total_mean"])
            lo = float(r["saved_prefill_tokens_total_ci95_low"])
            hi = float(r["saved_prefill_tokens_total_ci95_high"])
            rows_out.append({
                "trace": name,
                "family": FAMILY[name],
                "admission": r["admission"],
                "K": r["K"],
                "rinc": round((v - ls) / margin, 4) if margin > 0 else "",
                "rinc_ci95_low": round((lo - ls) / margin, 4) if margin > 0 else "",
                "rinc_ci95_high": round((hi - ls) / margin, 4) if margin > 0 else "",
                "saved_prefill_mean": v,
                "exact_saved_mean": es,
                "load_saved_mean": ls,
                "exact_minus_load_margin": margin,
                "index_bytes_mean": float(r["dispatcher_index_bytes_mean"]),
            })
    columns = list(rows_out[0].keys())
    with (OUT / "replay_panel_summary.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows_out)

    lines = ["# Multi-trace replay panel (2026-07-20)\n"]
    lines.append("Rinc = (S_admission - S_load_only) / (S_exact - S_load_only), closed_loop mode.\n")
    for name in TRACES:
        group = [r for r in rows_out if r["trace"] == name]
        lines.append(f"\n## {name} — {FAMILY[name]}\n")
        g0 = group[0]
        lines.append(f"Exact saved {g0['exact_saved_mean']:.0f} tokens; Load-Only {g0['load_saved_mean']:.0f}; "
                     f"affinity margin {g0['exact_minus_load_margin']:.0f}.\n")
        lines.append("| admission | K=8 Rinc | K=16 Rinc | K=32 Rinc | K=64 Rinc |")
        lines.append("|---|---|---|---|---|")
        for admission in sorted({r["admission"] for r in group}):
            cells = {r["K"]: r["rinc"] for r in group if r["admission"] == admission}
            lines.append(f"| {admission} | {cells.get('8','')} | {cells.get('16','')} | {cells.get('32','')} | {cells.get('64','')} |")
    (OUT / "replay_panel_summary.md").write_text("\n".join(lines) + "\n")
    print(json.dumps({"rows": len(rows_out), "out": str(OUT)}))


if __name__ == "__main__":
    main()
