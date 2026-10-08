#!/usr/bin/env python3
"""Pass/fail check for a top-k smoke or block (e2e_summary.json). Stdlib only.

usage:
  check_smoke.py SUMMARY.json --head <run HEAD sha> --link-bit 10000000 [--main StaticTopK16]
                 [--methods FullSync,BoundedFIFO16,...] [--cpu 0.85] [--cpu-k64 0.85] [--gap 0.005]
                 [--code-sha 103d9d0 --repo /home/byh/B02]

--head is the HEAD at run time (the summary "commit" field). With --code-sha,
G1 also requires `git -C REPO diff --quiet CODE_SHA HEAD -- experiments`, i.e. a
docs-only HEAD runs exactly the experiment code of CODE_SHA.

Exit 0 only if the hard gates pass:
  G1  provenance: commit == --head (prefix), experiments/ identical to --code-sha (if given),
      clean tree, link fixed and read back,
      no failed requests, STATS2 present, no cell errors (summary or cell_errors.log)
  G2  ledger: ingress gap and balance gap <= --gap for every row
  G3  gateway core <= --cpu (<= --cpu-k64 for k=64 methods); measured_rho within 10% of the nominal (burst included)
  G4  stale_cache_hits == 0 (unsafe reuse)
  G8  noise windows within 10% inside one (seed, scenario, workload)
  G9  every (seed, scenario) has every method in --methods (if given)
  G10 mechanism engaged in overload (nominal rho > 1): StaticTopK* has global_topk drops,
      Bounded* has queue_drop; no top-k drops outside top-k methods, no
      queue_drop outside mode 6; BoundedFIFO* max queue <= k
Findings (never gating): main method vs each bounded baseline, hit TTFT vs
Ideal, the k sweep, the Adaptive ablation, per-seed sign counts.
"""
import argparse
import json
import re
import statistics
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

RHOS = {"normal": 0.5, "near": 0.9, "high": 1.2, "xhigh": 1.5, "ultrahigh": 2.0, "burst": 0.9}
OVERLOAD = {"high", "xhigh", "ultrahigh", "burst"}
COLS = ("loose_false_negative_rate", "foreground_lag_p95_s", "foreground_undelivered",
        "invalidate_lag_p95_s", "invalidate_undelivered", "loose_false_positive_rate",
        "stale_cache_hits", "prefill_tokens_mean", "hit_service_ttft_ms", "relay_drop_global_topk",
        "relay_drop_queue_drop", "relay_max_queue", "gateway_cpu")
LOWER_IS_BETTER = ("loose_false_negative_rate", "foreground_lag_p95_s", "invalidate_lag_p95_s",
                   "prefill_tokens_mean", "foreground_undelivered", "invalidate_undelivered")
LABEL = {"loose_false_negative_rate": "FN", "foreground_lag_p95_s": "fg_lag95", "invalidate_lag_p95_s": "inv_lag95",
         "prefill_tokens_mean": "prefill"}
BASELINES = ("BoundedSemantic{k}", "BoundedPrio{k}", "BoundedFIFO{k}", "StaticSemantic", "FullSync", "RateFIFO")


def canon(method: str) -> str:
    return "StaticTopK16" if method == "StaticTopK" else method


def k_of(method: str) -> int:
    match = re.fullmatch(r"(?:StaticTopK|BoundedFIFO|BoundedPrio|BoundedSemantic)(\d+)", method)
    return int(match.group(1)) if match else 0


def nominal(row: dict) -> float:
    if "nominal_rho_effective" in row:
        return float(row["nominal_rho_effective"])
    rho = RHOS.get(row["scenario"], 0.0)
    if row["scenario"] == "burst":
        mult = float(row.get("burst_mult", 5.0))
        return rho * (25 + 5 * mult) / 30
    return rho


def workload(row: dict) -> str:
    return f"pool{row.get('useful_pool', 16)}|u{row.get('useful_coverage_mix', '4096')}|n{row.get('noise_coverage_mix', '256')}"


def get(row: dict, key: str) -> float:
    value = row.get(key)
    return float(value) if isinstance(value, (int, float)) else float("nan")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("summary")
    ap.add_argument("--head", required=True)
    ap.add_argument("--link-bit", type=int, required=True)
    ap.add_argument("--main", default="StaticTopK16")
    ap.add_argument("--methods", default="", help="expected methods per (seed, scenario); empty = no G9")
    ap.add_argument("--gap", type=float, default=0.005)
    ap.add_argument("--cpu", type=float, default=0.85)
    ap.add_argument("--cpu-k64", type=float, default=0.85, help="CPU gate for k=64 methods (ksweep)")
    ap.add_argument("--code-sha", default="", help="commit whose experiments/ tree must equal the run HEAD")
    ap.add_argument("--repo", default=".", help="git checkout used for --code-sha (default: cwd)")
    a = ap.parse_args()
    path = Path(a.summary)
    data = json.loads(path.read_text())
    rows = data["rows"]
    for row in rows:
        row["method"] = canon(row["method"])
    hard: list[str] = []

    # G1
    commit = str(data.get("commit") or "")
    if not commit or not (commit.startswith(a.head) or a.head.startswith(commit)):
        hard.append(f"G1 commit {commit or 'missing'} != HEAD {a.head}")
    if a.code_sha and commit:
        try:
            diff = subprocess.run(
                ["git", "-C", a.repo, "diff", "--name-only", a.code_sha, commit, "--", "experiments"],
                capture_output=True, text=True, timeout=30,
            )
            if diff.returncode != 0:
                hard.append(f"G1 git diff {a.code_sha}..{commit} failed: {diff.stderr.strip()[:120]}")
            elif diff.stdout.strip():
                hard.append(f"G1 experiments/ differs from code sha {a.code_sha}: {diff.stdout.split()[:5]}")
        except (OSError, subprocess.TimeoutExpired) as exc:
            hard.append(f"G1 git diff not runnable: {exc!r}")
    if data.get("git_dirty_files"):
        hard.append(f"G1 dirty tree {data['git_dirty_files'][:3]}")
    if data.get("fixed_link_bit_s") != a.link_bit or data.get("tc_link_bit_s") != a.link_bit:
        hard.append(f"G1 link fixed={data.get('fixed_link_bit_s')} tc={data.get('tc_link_bit_s')} != {a.link_bit}")
    if data.get("cell_errors"):
        hard.append(f"G1 cell_errors={data['cell_errors']}")
    errors_log = path.parent / "cell_errors.log"
    if errors_log.exists() and errors_log.read_text().strip():
        hard.append(f"G1 {errors_log} is not empty ({len(errors_log.read_text().splitlines())} lines)")
    for row in rows:
        tag = f"{row['scenario']}/{row['method']}/s{row['seed']}"
        if row.get("failed_requests"):
            hard.append(f"G1 {tag} failed_requests={row['failed_requests']}")
        if row.get("stats2_missing"):
            hard.append(f"G1 {tag} STATS2 missing (stale gateway image?)")
        # G2
        for key in ("ledger_ingress_gap", "ledger_balance_gap"):
            if get(row, key) != get(row, key) or get(row, key) > a.gap:
                hard.append(f"G2 {tag} {key}={row.get(key)}")
        # G3
        cpu_gate = a.cpu_k64 if k_of(row["method"]) == 64 else a.cpu
        if not get(row, "gateway_cpu") <= cpu_gate:
            hard.append(f"G3 {tag} gateway_cpu={row.get('gateway_cpu')} > {cpu_gate}")
        if int(row.get("stale_cache_hits") or 0) != 0:
            hard.append(f"G4 {tag} stale_cache_hits={row.get('stale_cache_hits')}")
        want = nominal(row)
        if want and abs(get(row, "measured_rho") / want - 1) > 0.10:
            hard.append(f"G3 {tag} measured_rho={get(row, 'measured_rho'):.2f} nominal={want:.2f}")
        # G10
        method = row["method"]
        topk_drops = int(row.get("relay_drop_global_topk") or 0)
        queue_drops = int(row.get("relay_drop_queue_drop") or 0)
        is_topk = method.startswith(("StaticTopK", "Adaptive"))
        is_bounded = method.startswith(("BoundedFIFO", "BoundedPrio", "BoundedSemantic"))
        if topk_drops and not is_topk:
            hard.append(f"G10 {tag} has global_topk drops {topk_drops} but is not a top-k method")
        if queue_drops and not is_bounded:
            hard.append(f"G10 {tag} has queue_drop {queue_drops} but is not mode 6")
        if nominal(row) > 1.0:
            if method.startswith("StaticTopK") and topk_drops == 0:
                hard.append(f"G10 {tag} top-k never bound in overload (check k and the config frame)")
            if is_bounded and k_of(method) and k_of(method) < 4096 and queue_drops == 0:
                hard.append(f"G10 {tag} cap never bound in overload")
        if method.startswith("BoundedFIFO") and k_of(method) and int(row.get("relay_max_queue") or 0) > k_of(method):
            hard.append(f"G10 {tag} relay_max_queue {row.get('relay_max_queue')} > cap {k_of(method)}")

    by: dict[tuple, dict] = defaultdict(dict)
    for row in rows:
        by[(workload(row), row["scenario"], row["seed"])][row["method"]] = row
    # G8
    for key, cell in by.items():
        windows = [r["noise_window_s"] for r in cell.values() if "noise_window_s" in r]
        if windows and (max(windows) - min(windows)) / statistics.median(windows) > 0.10:
            hard.append(f"G8 {key} noise_window spread {min(windows):.0f}-{max(windows):.0f}s")
    # G9
    expected = {canon(m) for m in a.methods.split(",") if m}
    if expected:
        for key, cell in by.items():
            missing = expected - set(cell)
            if missing:
                hard.append(f"G9 {key} missing {sorted(missing)}")

    # Findings
    main_k = k_of(a.main) or 16
    for key in sorted(by, key=lambda item: (item[0], item[1], item[2])):
        cell = by[key]
        print(f"\n== {key[0]}  {key[1]}  seed {key[2]}")
        print("method".ljust(22) + "".join(c[:14].rjust(15) for c in COLS))
        for method in sorted(cell):
            print(method.ljust(22) + "".join(f"{get(cell[method], c):15.3f}" for c in COLS))
        main_row, ideal = cell.get(a.main), cell.get("Ideal")
        if main_row and ideal and get(ideal, "hit_service_ttft_ms") > 0:
            ratio = get(main_row, "hit_service_ttft_ms") / get(ideal, "hit_service_ttft_ms") - 1
            print(f"G7 {a.main} hit TTFT vs Ideal {ratio:+.1%} ({'ok' if abs(ratio) <= 0.10 else 'CHECK'})")
        for template in BASELINES:
            base = template.format(k=main_k)
            if main_row and base in cell:
                deltas = ", ".join(f"{LABEL[c]} {get(main_row, c) - get(cell[base], c):+.3f}" for c in LOWER_IS_BETTER[:4])
                print(f"  {a.main} - {base}: {deltas}")
        for method in sorted(cell):
            by_cov = cell[method].get("fn_rate_by_coverage") or {}
            if len(by_cov) > 1:
                print(f"  FN by useful coverage {method}: " + ", ".join(f"{c}:{v:.3f}" for c, v in by_cov.items()))
        for ablation in ("Adaptive", "AdaptiveNoPriority", "StaticTopK16NoMerge", "StaticTopK16NoDedup", "StaticTopK16NoPriority"):
            if main_row and ablation in cell:
                print(f"  ablation {ablation} - {a.main}: FN {get(cell[ablation], 'loose_false_negative_rate') - get(main_row, 'loose_false_negative_rate'):+.3f}, "
                      f"prefill {get(cell[ablation], 'prefill_tokens_mean') - get(main_row, 'prefill_tokens_mean'):+.0f}, "
                      f"congested_fraction {get(cell[ablation], 'congested_fraction'):.2f}")

    # k sweep and seed-level sign counts
    groups: dict[tuple, dict] = defaultdict(lambda: defaultdict(list))
    for row in rows:
        groups[(workload(row), row["scenario"])][row["method"]].append(row)
    for (wl, scenario), methods in sorted(groups.items()):
        sweep = sorted((k_of(m), m) for m in methods if m.startswith("StaticTopK") and k_of(m))
        if len(sweep) >= 2:
            print(f"\n-- k sweep {wl} {scenario}")
            for k, m in sweep:
                fn = [get(r, "loose_false_negative_rate") for r in methods[m]]
                lag = [get(r, "foreground_lag_p95_s") for r in methods[m]]
                semantic = methods.get(f"BoundedSemantic{k}", [])
                extra = ""
                if semantic:
                    extra = f"   BoundedSemantic{k} FN {statistics.mean(get(r, 'loose_false_negative_rate') for r in semantic):.3f}"
                print(f"  k={k:<4} FN {statistics.mean(fn):.3f}  fg lag p95 {statistics.mean(lag):.2f}s  n={len(fn)}{extra}")
        if a.main in methods:
            for template in BASELINES[:3]:
                base = template.format(k=main_k)
                if base not in methods:
                    continue
                pairs = {r["seed"]: r for r in methods[base]}
                wins = ties = losses = 0
                for r in methods[a.main]:
                    other = pairs.get(r["seed"])
                    if other is None:
                        continue
                    d = get(r, "loose_false_negative_rate") - get(other, "loose_false_negative_rate")
                    wins += d < -1e-9
                    losses += d > 1e-9
                    ties += abs(d) <= 1e-9
                print(f"  seeds {wl} {scenario}: {a.main} vs {base} FN better/tie/worse = {wins}/{ties}/{losses}")

    print("\nHARD GATES:", "PASS" if not hard else "FAIL")
    for item in hard:
        print("  " + item)
    sys.exit(1 if hard else 0)


if __name__ == "__main__":
    main()
