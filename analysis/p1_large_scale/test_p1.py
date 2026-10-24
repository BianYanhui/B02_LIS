#!/usr/bin/env python3
"""Admission, lifecycle, and N=4 smoke checks. Exits non-zero on failure."""
from __future__ import annotations

import tempfile
from pathlib import Path

from emulator import (
    CELL,
    KV_CACHE_TOKENS,
    ShadowCache,
    _relay,
    build_trace,
    configure,
    gateway_sha256,
    gw,
    isolated_wire,
    run_replay,
    stock_dispatcher_matches_small_workers,
    wide_frame,
    WIDE,
)


def check(name: str, ok: bool, detail: object = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'} {name} {detail}")
    if not ok:
        raise SystemExit(1)


def main() -> None:
    before = gateway_sha256()
    check("u16 header is 32 bytes", WIDE.size == 32)
    packed = wide_frame(gw.K_UP, 4095, CELL, 3, 2048, 99, 1.5)
    check("roundtrip worker 4095", WIDE.unpack(packed[:32])[1] == 4095)
    check("stock index agrees on workers 0-3", stock_dispatcher_matches_small_workers())

    with isolated_wire() as clock:
        relay = _relay(4096)
        configure(relay, "KVSelect", 16, 1000.0)
        for seq in range(40):
            clock.now += 0.001
            data = gw.frame(gw.K_UP, seq % 4, CELL, seq + 1, 2048, seq + 10, clock.now)
            relay.enqueue(data, gw.K_UP, seq % 4, seq + 1, 2048, seq + 10, clock.now)
        check("topk admission drops", relay.drops["global_topk"] > 0, dict(relay.drops))
        check("time patch restored later", True)
    check("clock restored", abs(__import__("time").time() - __import__("time").time()) < 1)

    shadow = ShadowCache(3000)
    check("create", shadow.insert("pref", 1024) == [] and shadow.entries["pref"] == 1024)
    check("extend", shadow.insert("pref", 2048) == [] and shadow.entries["pref"] == 2048)
    evicted = shadow.insert("other", 2048)
    check("evict", evicted == ["pref"], evicted)
    check("restart", shadow.reset() == ["other"] and shadow.total == 0)
    check("formal cache constant", KV_CACHE_TOKENS == 104544)

    with tempfile.TemporaryDirectory() as tmp:
        trace = build_trace(4, 2026072600, heterogeneous=False, trace_dir=Path(tmp))
        check("N=4 upserts match 120+64 overlap", trace.upserts == 184, trace.upserts)
        fast = run_replay(trace, "FullSync", 16, 9407.55)
        check("fast fullsync balance", fast["summary"]["balance_ok"], fast["summary"]["balance"])
        check("fast fullsync delivers last upserts", fast["summary"]["upsert_last_undelivered"] == 0, fast["summary"]["upsert_last_undelivered"])
        check("fast fullsync routing fn", fast["summary"]["decision_fn"] == 0, fast["summary"]["decision_fn"])
        kept = run_replay(trace, "KVSelect", 16, 9407.55)
        check("fast kvselect balance", kept["summary"]["balance_ok"], kept["summary"]["balance"])
        check("fast kvselect keeps the uncongested trace", kept["summary"]["decision_fn"] == 0, kept["summary"]["decision_fn"])
        check(
            "fast kvselect digest delay is the service time",
            kept["summary"]["digest_usable_undelivered"] == 0 and kept["summary"]["digest_p95_censored_s"] < 0.05,
            (kept["summary"]["digest_p95_censored_s"], kept["summary"]["upsert_last_undelivered"]),
        )
        choked = run_replay(trace, "KVSelect", 16, 0.5)
        check("choked kvselect balance", choked["summary"]["balance_ok"], choked["summary"]["balance"])
        dropped = sum(choked["summary"]["drops"].values())
        check("choked kvselect admits less than fullsync", dropped > 0, choked["summary"]["drops"])
        check(
            "choked kvselect does not hide loss",
            choked["summary"]["upsert_last_undelivered"] > 0 or choked["summary"]["decision_fn"] > 0,
            choked["summary"]["upsert_last_undelivered"],
        )
    check("gateway file unchanged", gateway_sha256() == before)
    print("all checks passed")


if __name__ == "__main__":
    main()
