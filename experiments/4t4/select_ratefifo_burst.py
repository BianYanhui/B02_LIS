#!/usr/bin/env python3
"""Select the frozen RateFIFO token-bucket burst from calibration only.

The rule is deliberately data-independent apart from the three pre-registered
candidate measurements: at every rho, normalize p95 dispatch state age and
view-missing rate by the across-candidate median, add the two normalized
quantities, and average across rho.  A numerical tie selects the medium
four-frame bucket.  The script refuses partial or non-VALID calibration input.
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
from pathlib import Path


CANDIDATES = (1, 4, 16)
RHOS = (0.8, 1.0, 1.2)


def load(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--summary-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit(f"refusing to overwrite calibration selection: {args.output}")

    values: dict[int, dict[float, dict[str, float]]] = {}
    for burst in CANDIDATES:
        path = args.summary_dir / f"cells_calib_ratefifo_burst{burst}_20260726.csv"
        if not path.is_file():
            raise SystemExit(f"missing pre-registered RateFIFO calibration: {path}")
        rows = [row for row in load(path) if row.get("policy") == "RateFIFO"]
        if len(rows) != len(RHOS):
            raise SystemExit(f"expected exactly {len(RHOS)} RateFIFO rows in {path}, found {len(rows)}")
        bucket: dict[float, dict[str, float]] = {}
        for row in rows:
            if row.get("status") != "VALID":
                raise SystemExit(f"invalid calibration row in {path}: {row.get('cell_id')}")
            rho = float(row["rho"])
            if rho not in RHOS or rho in bucket:
                raise SystemExit(f"unexpected/duplicate rho in {path}: {rho}")
            bucket[rho] = {
                "p95_state_age_s": float(row["dispatch_state_age_p95_s"]),
                "view_missing": float(row["dispatcher_view_missing_at_dispatch_rate"]),
                "request_count": float(row["request_count"]),
            }
        if set(bucket) != set(RHOS):
            raise SystemExit(f"incomplete rho grid in {path}: {sorted(bucket)}")
        values[burst] = bucket

    scales: dict[float, dict[str, float]] = {}
    scores = {burst: 0.0 for burst in CANDIDATES}
    components: dict[int, dict[str, float]] = {burst: {} for burst in CANDIDATES}
    for rho in RHOS:
        ages = [values[burst][rho]["p95_state_age_s"] for burst in CANDIDATES]
        missing = [values[burst][rho]["view_missing"] for burst in CANDIDATES]
        requests = [values[burst][rho]["request_count"] for burst in CANDIDATES]
        age_scale = max(statistics.median(ages), 1e-6)
        missing_scale = max(statistics.median(missing), 1.0 / max(statistics.median(requests), 1.0))
        scales[rho] = {"age_scale_s": age_scale, "view_missing_scale": missing_scale}
        for burst in CANDIDATES:
            term = (values[burst][rho]["p95_state_age_s"] / age_scale
                    + values[burst][rho]["view_missing"] / missing_scale)
            scores[burst] += term / len(RHOS)
            components[burst][str(rho)] = term

    best_score = min(scores.values())
    ties = [burst for burst in CANDIDATES if abs(scores[burst] - best_score) <= 1e-12]
    selected = 4 if 4 in ties else min(ties)
    output = {
        "status": "CALIBRATION_ONLY_NOT_FOR_PAPER",
        "selection_rule": "For rho in {0.8,1.0,1.2}, normalize p95 dispatch-state age and view-missing rate by the across-candidate median at that rho, add them, then average over rhos; numerical tie -> medium (4 frames).",
        "candidate_bursts_frames": list(CANDIDATES),
        "rhos": list(RHOS),
        "per_rho_scales": {str(rho): scales[rho] for rho in RHOS},
        "per_burst_inputs": {str(burst): values[burst] for burst in CANDIDATES},
        "per_burst_normalized_component": {str(burst): components[burst] for burst in CANDIDATES},
        "mean_normalized_score": {str(burst): scores[burst] for burst in CANDIDATES},
        "selected_ratefifo_burst_frames": selected,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, indent=2) + "\n")
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
