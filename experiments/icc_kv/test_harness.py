"""Offline checks for the semantic top-k harness. No Docker and no vLLM."""
from __future__ import annotations

import asyncio

from experiments.icc_kv.capacity_window import derive_capacity, window_problems
from experiments.icc_kv.e2e import gather_or_cancel, is_stale_cache_hit, note_worker_dispatch
from experiments.icc_kv.runtime import parse_tc_rate, require_clean_tree
from experiments.icc_kv.split_path import _offered_deadline, _sender_rate
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


def test_stale_predicate() -> None:
    """Sibling reuse is not leftover KV. The first hit of a version still is."""
    seen: set[tuple[int, int, int]] = set()

    def hit(cached: int, truth: int, version: int, slot: int, worker: int) -> bool:
        earlier = note_worker_dispatch(seen, slot, version, worker)
        return is_stale_cache_hit(cached, truth, version, earlier)

    check("first hit of a version is stale", hit(4128, 0, 2, 3, 1) is True)
    check("later sibling on that worker is not", hit(4128, 0, 2, 3, 1) is False)
    check("same version on another worker still is", hit(512, 0, 2, 3, 0) is True)
    check("other slot on the same worker still is", hit(512, 0, 2, 4, 1) is True)
    check("version 0 is not stale", hit(4128, 0, 0, 3, 1) is False)
    check("routed truth blocks the count", hit(4128, 4096, 1, 5, 2) is False)
    check("cached below 512 is not stale", hit(511, 0, 1, 6, 2) is False)
    check("new version on the same worker counts again", hit(4128, 0, 3, 3, 1) is True)
    check("second request of the new version does not", hit(4128, 0, 3, 3, 1) is False)


def test_offered_deadline() -> None:
    """Absolute offer times track level, including the 25 s / 5 s burst window."""
    t0 = 1000.0
    level, senders = 4000.0, 4
    rate = level / senders
    for sent, offset in ((0, 0.0), (1, 1.0 / rate), (rate, 1.0), (25 * rate, 25.0)):
        got = _offered_deadline(t0, int(sent), level, senders, "xhigh", 5.0)
        check(f"steady deadline sent={sent}", abs(got - (t0 + offset)) < 1e-9, got)
    check(
        "steady rate ignores the burst window",
        _sender_rate(level, senders, "xhigh", 5.0, 27.0) == rate,
    )
    burst_mult = 1.5
    base = _sender_rate(level, senders, "burst", burst_mult, 24.999)
    burst = _sender_rate(level, senders, "burst", burst_mult, 25.0)
    check("burst base window", base == rate and _sender_rate(level, senders, "burst", burst_mult, 0.0) == rate)
    check("burst high window", burst == rate * burst_mult)
    check("burst window repeats", _sender_rate(level, senders, "burst", burst_mult, 30.0) == rate)
    per_cycle = base * 25.0 + burst * 5.0
    end = _offered_deadline(t0, int(per_cycle), level, senders, "burst", burst_mult)
    check("burst cycle ends at 30s", abs(end - (t0 + 30.0)) < 1e-6, end)
    at_25 = _offered_deadline(t0, int(base * 25.0), level, senders, "burst", burst_mult)
    check("burst base chunk ends at 25s", abs(at_25 - (t0 + 25.0)) < 1e-6, at_25)
    one_into_burst = _offered_deadline(t0, int(base * 25.0) + 1, level, senders, "burst", burst_mult)
    check(
        "first burst event is on the multiplied rate",
        abs(one_into_burst - (t0 + 25.0 + 1.0 / burst)) < 1e-6,
        one_into_burst,
    )
    # Oversleep does not move the target for a given count, so the next wait
    # shortens by the time actually lost. 1000 events at `rate` are due at 1s.
    now = t0
    sent = 0
    batch = max(1, int(rate / 200))
    while sent < int(rate):
        target = _offered_deadline(t0, sent, level, senders, "xhigh", 5.0)
        now = target + 0.001 if now < target else now
        sent += batch
        now += 0.0002
    check("oversleep stays on the schedule", now - t0 < 1.05, now - t0)


def test_dirty_allowed() -> None:
    dirty = require_clean_tree(allow_dirty=True)
    check("dirty list", isinstance(dirty, list), dirty)


def main() -> None:
    test_policies()
    test_mix()
    test_tc()
    test_window()
    test_cancel()
    test_stale_predicate()
    test_offered_deadline()
    test_dirty_allowed()
    check("frame size", FRAME == 64)
    print("all passed")


if __name__ == "__main__":
    main()
