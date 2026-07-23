#!/usr/bin/env python3
"""
run_all.py — run the full E1/E2/E3 experiment grid in-process and write:

    results/e1_scaling.json   + results/e1_scaling.csv
    results/e2_freshness.json + results/e2_freshness.csv
    results/e3_dispatch.json  + results/e3_dispatch.csv
    results/summary.json

Default grid (paper configuration): reps=10, duration=120 s after a 20 s
warmup, bursts on, seed=1 (rep i uses seed+i, common across cells).

    python3 run_all.py                 # full grid
    python3 run_all.py --quick         # short smoke grid (~20 s runs, 2 reps)
"""

import argparse
import json
import time
from pathlib import Path

import shared_link_sim as S


def main():
    ap = argparse.ArgumentParser(description="Run the full E1/E2/E3 grid.")
    ap.add_argument("--quick", action="store_true",
                    help="short smoke grid: duration 15 s, warmup 5 s, 2 reps, "
                         "reduced sweeps")
    ap.add_argument("--reps", type=int, default=None)
    ap.add_argument("--duration", type=float, default=None,
                    help="measurement window per rep, s")
    ap.add_argument("--warmup", type=float, default=None)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--outdir", type=str,
                    default=str(Path(__file__).resolve().parent.parent / "results"))
    args = ap.parse_args()

    if args.quick:
        cfg = S.Config(reps=2, duration=15.0, warmup=5.0, seed=args.seed)
        e1_ns, e2_bs, e3_bs = (16, 128), (16.0, 64.0), (16.0,)
    else:
        cfg = S.Config(seed=args.seed)
        e1_ns, e2_bs, e3_bs = S.E1_NS, S.E2_BS_KIB, S.E3_BS_KIB
    if args.reps is not None:
        cfg.reps = args.reps
    if args.duration is not None:
        cfg.duration = args.duration
    if args.warmup is not None:
        cfg.warmup = args.warmup

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    print(f"config: reps={cfg.reps} duration={cfg.duration}s "
          f"warmup={cfg.warmup}s seed={cfg.seed} -> {outdir}")

    t_all = time.time()
    results = {}
    for name, runner, grid in (("e1", S.run_e1, e1_ns),
                               ("e2", S.run_e2, e2_bs),
                               ("e3", S.run_e3, e3_bs)):
        t0 = time.time()
        payload = runner(cfg, grid)
        payload["config"] = {k: (v if v != float("inf") else "inf")
                             for k, v in vars(cfg).items()}
        results[name] = payload
        S.write_results(outdir, S.OUT_NAMES[name], payload)
        print(f"[{S.OUT_NAMES[name]}] {len(payload['cells'])} cells x "
              f"{cfg.reps} reps in {time.time() - t0:.1f}s")
        for cell in payload["cells"]:
            S._print_cell(cell)

    summary = S.build_summary(results["e1"], results["e2"], results["e3"])
    summary["wall_s_total"] = round(time.time() - t_all, 2)
    with open(outdir / "summary.json", "w") as f:
        json.dump(summary, f, indent=2)
    print(f"[summary.json] written; total wall {time.time() - t_all:.1f}s")
    for bkey, line in summary["e3_dispatch"]["headline"].items():
        print("  " + line)


if __name__ == "__main__":
    main()
