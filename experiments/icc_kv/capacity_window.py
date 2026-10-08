"""Derive the link-limited drain and the ingress each policy can still accept.

This is not experiments/icc_kv/capacity.py. That file is the earlier capacity
scan. The input is a path_capacity.json written with --link-bit.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    count = len(ordered)
    mid = count // 2
    if count % 2:
        return float(ordered[mid])
    return 0.5 * (ordered[mid - 1] + ordered[mid])


def _num(row: dict, key: str, default: float) -> float:
    value = row.get(key)
    if value is None or value == "":
        return default
    return float(value)


# ACKs to senders share the HTB parent, so drain falls with ingress.
# Plateau = link-limited service rate at row rho <= 1.5.
PLATEAU_RHO_ROW_MAX = 1.5


def derive_capacity(document: dict) -> dict:
    dirty = document.get("git_dirty_files") or []
    if dirty:
        raise SystemExit("capacity file was measured from a dirty experiments/ tree: " + ", ".join(dirty))
    link = int(document.get("link_bit_s") or document.get("fixed_link_bit_s") or 0)
    if link <= 0:
        raise SystemExit("capacity file has no link_bit_s")
    rows = list(document.get("rows") or [])
    fullsync = [
        row for row in rows
        if row.get("policy") == "FullSync"
        and _num(row, "queued_end", 0) >= 64
        and _num(row, "gateway_cpu", 1) < 0.85
        and _num(row, "ledger_ingress_gap", 1) <= 0.01
        and _num(row, "sent_per_s", 0) <= PLATEAU_RHO_ROW_MAX * _num(row, "forwarded_per_s", 0)
    ]
    if len(fullsync) < 2:
        raise SystemExit(
            f"need at least 2 saturated FullSync rows with rho_row <= {PLATEAU_RHO_ROW_MAX}, found {len(fullsync)}"
        )
    rates = [float(row["forwarded_per_s"]) for row in fullsync]
    drain = _median(rates)
    spread = (max(rates) - min(rates)) / drain if drain else 1.0
    # 10 Mbit FullSync plateau itself moves several percent at one offered load.
    # Above 0.20 is still not a plateau. Between 0.05 and 0.20, keep the median and warn.
    if spread > 0.20:
        raise SystemExit(
            f"FullSync drain is not a plateau: spread {spread:.3f} (gate 0.20)"
        )
    spread_warning = spread > 0.05
    by_policy: dict[str, float] = {}
    for row in rows:
        if _num(row, "ledger_ingress_gap", 1) > 0.01:
            continue
        if _num(row, "gateway_cpu", 1) >= 0.85:
            continue
        policy = str(row.get("policy"))
        sent = _num(row, "sent_per_s", 0)
        by_policy[policy] = max(by_policy.get(policy, 0.0), sent)
    if not by_policy:
        raise SystemExit("no policy stayed inside the ingress window")
    ingress_min = min(by_policy.values())
    theory = link / (104.0 * 8.0)
    rho_max = ingress_min / drain if drain else 0.0
    derived = {
        "link_bit_s": link,
        "c_drain_per_s": drain,
        "c_drain_rows": len(fullsync),
        "c_drain_spread": spread,
        "c_drain_rho_row_max": PLATEAU_RHO_ROW_MAX,
        "c_ingress_per_s": by_policy,
        "c_ingress_min_per_s": ingress_min,
        "c_link_theory_per_s": theory,
        "c_drain_over_theory": drain / theory if theory else 0.0,
        "rho_max_in_window": rho_max,
    }
    if spread_warning:
        derived["c_drain_spread_warning"] = True
    return derived


def window_problems(window: dict, *, link: int, capacity: float, peaks: dict[str, float]) -> list[str]:
    problems: list[str] = []
    if int(window.get("link_bit_s") or 0) != int(link):
        problems.append(f"link {link} does not match capacity file {window.get('link_bit_s')}")
    drain = float(window.get("c_drain_per_s") or 0)
    if drain <= 0 or abs(capacity - drain) / drain > 0.10:
        problems.append(f"capacity {capacity} is outside 10% of C_drain {drain}")
    ingress = float(window.get("c_ingress_min_per_s") or 0)
    for name, peak in peaks.items():
        if ingress <= 0 or peak > 0.9 * ingress:
            problems.append(f"{name} peak {peak:.1f} exceeds 0.9*C_ingress {ingress:.1f}")
    return problems


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("usage: python -m experiments.icc_kv.capacity_window PATH_CAPACITY.json --out FILE")
    source = Path(sys.argv[1])
    out = Path(sys.argv[sys.argv.index("--out") + 1]) if "--out" in sys.argv else source.with_name("capacity_window.json")
    derived = derive_capacity(json.loads(source.read_text()))
    if derived.get("c_drain_spread_warning"):
        spread = float(derived["c_drain_spread"])
        print(
            f"WARNING: FullSync plateau spread {spread:.3f} exceeds 0.05 (gate 0.20); "
            f"C is the median, uncertainty about +/-{spread / 2:.0%}",
            flush=True,
        )
    if derived["rho_max_in_window"] <= 1.2:
        out.write_text(json.dumps(derived, indent=2) + "\n")
        raise SystemExit(
            f"overload window is too small: rho_max_in_window={derived['rho_max_in_window']:.3f}"
        )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(derived, indent=2) + "\n")
    print(derived, flush=True)


if __name__ == "__main__":
    main()
