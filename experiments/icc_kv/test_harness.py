"""Offline checks for the semantic top-k harness. No Docker and no vLLM."""
from __future__ import annotations

import asyncio

from experiments.icc_kv.capacity_window import derive_capacity, window_problems
from experiments.icc_kv.e2e import gather_or_cancel
from experiments.icc_kv.runtime import parse_tc_rate, require_clean_tree
from experiments.icc_kv.wire import CFG, FRAME, POLICIES, POLICY_MAX_QUEUE, TOPK_SWEEP, config_frame
from experiments.icc_kv.workload import burst_nominal_rho, interleave, noise_overlap, useful_slots


def check(name: str, ok: bool, detail: object = "") -> None:
    if not ok:
        raise SystemExit(f"FAIL {name}: {detail}")
    print(f"PASS {name}")


def test_policies() -> None:
    check("policy count", len(POLICIES) == 31, sorted(POLICIES))
    check("static alias", POLICIES["StaticTopK"] == POLICIES["StaticTopK16"] == (4, 1, 1, 0, 2, 16))
    check("sweep present", all(f"BoundedSemantic{k}" in POLICIES for k in TOPK_SWEEP))
    raw = config_frame(1, "BoundedFIFO16", max_queue=4096, max_inflight=0, rate_frames_per_s=0, rate_burst=1)
    decoded = CFG.unpack(raw[32:58])
    check("bounded cap on the wire", decoded[6] == 16 and decoded[0] == 6, decoded)
    check("semantic is mode 6 with merge", POLICIES["BoundedSemantic16"] == (6, 1, 1, 0, 2, 0))
    check("prio cap", POLICY_MAX_QUEUE["BoundedPrio32"] == 32)


def test_mix() -> None:
    slots = useful_slots(6, "1024:1,2048:1,4096:1")
    check("useful round robin", slots == [1024, 2048, 4096, 1024, 2048, 4096], slots)
    ov60 = "256:40,1024:20,2048:20,4096:20"
    table = interleave(ov60)
    check("ov60 reduced length", len(table) == 5 and table.count(256) == 2, table)
    check("ov60 overlap", abs(noise_overlap(ov60, slots) - 0.6) < 1e-9, noise_overlap(ov60, slots))
    check("base overlap is zero", noise_overlap("", [4096] * 16) == 0.0)
    check("burst nominal", abs(burst_nominal_rho(0.9, 5.0) - 1.5) < 1e-9)


def test_tc() -> None:
    sample = "class htb 1:1 root rate 10Mbit ceil 10Mbit burst 1600b cburst 1600b"
    check("tc 10Mbit", parse_tc_rate(sample) == 10_000_000, parse_tc_rate(sample))
    check("tc missing", parse_tc_rate("no class") == 0)


def test_window() -> None:
    rows = []
    for rate, forwarded, queued, cpu, gap in (
        (6000, 6000, 0, 0.4, 0.0),
        (12000, 9000, 100, 0.5, 0.0),
        (13000, 9100, 200, 0.6, 0.0),
        (24000, 6500, 400000, 0.6, 0.0),
    ):
        rows.append({
            "policy": "FullSync", "sent_per_s": rate, "forwarded_per_s": forwarded,
            "queued_end": queued, "gateway_cpu": cpu, "ledger_ingress_gap": gap,
        })
    rows.append({
        "policy": "StaticTopK16", "sent_per_s": 20000, "forwarded_per_s": 9000,
        "queued_end": 16, "gateway_cpu": 0.7, "ledger_ingress_gap": 0.0,
    })
    derived = derive_capacity({"link_bit_s": 10_000_000, "rows": rows})
    check("drain plateau", abs(derived["c_drain_per_s"] - 9050) < 1, derived)
    check("ingress min", derived["c_ingress_min_per_s"] == 20000, derived)
    check(
        "excluded row does not affect c_drain",
        derived["c_drain_rows"] == 2 and abs(derived["c_drain_per_s"] - 9050) < 1,
        derived,
    )
    problems = window_problems(derived, link=10_000_000, capacity=9050, peaks={"ultrahigh": 20000})
    check("window rejects a peak above ingress", bool(problems), problems)


def test_cancel() -> None:
    cancelled = 0

    async def boom() -> None:
        raise RuntimeError("serve failed")

    async def linger() -> None:
        nonlocal cancelled
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled += 1
            raise

    async def run() -> None:
        try:
            await gather_or_cancel([boom(), linger(), linger()])
        except RuntimeError:
            return
        raise SystemExit("gather did not surface the failure")

    asyncio.run(run())
    check("cancelled siblings", cancelled == 2, cancelled)


def test_dirty_allowed() -> None:
    dirty = require_clean_tree(allow_dirty=True)
    check("dirty list", isinstance(dirty, list), dirty)


def main() -> None:
    test_policies()
    test_mix()
    test_tc()
    test_window()
    test_cancel()
    test_dirty_allowed()
    check("frame size", FRAME == 64)
    print("all passed")


if __name__ == "__main__":
    main()
