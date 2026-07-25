#!/usr/bin/env python3
"""Deterministic unit checks for the six isolated 4T4 gateway policies.

These checks never contact vLLM or Docker.  They establish that the policy
switches exercise distinct update-selection behavior before any live run is
accepted as calibration/formal evidence.
"""
from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import time
from pathlib import Path
from types import SimpleNamespace


HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("gateway4t4", HERE / "net" / "gateway_relay_4t4.py")
assert SPEC and SPEC.loader
gw = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gw)


def relay() -> object:
    instance = gw.Relay(SimpleNamespace(max_queue=4096, gate=2.0, adaptive_queue_gate=8, tau=30.0, util_lambda=16.0))
    # The live relay emits JSON telemetry.  Unit tests verify queue behavior
    # directly and deliberately keep stdout limited to the check summary.
    instance.emit_update = lambda *args, **kwargs: None
    return instance


def configure(r: object, mode: int, *, merge: bool = False, priority: bool = False,
              adaptive: bool = False, dedup: int = 0, rate: float = 0.0, burst: int = 0) -> None:
    r.mode = mode
    r.merge = merge
    r.priority = priority
    r.adaptive = adaptive
    r.dedup = dedup
    r.rate_frames_per_s = rate
    r.rate_burst_frames = burst
    r.rate_tokens = float(burst)
    r.rate_last = time.monotonic()


def up(seq: int, owner: int = 0, digest: int = 7, coverage: int = 1024, age: float = 0.0) -> tuple[bytes, float]:
    sent = time.time() - age
    return gw.frame(gw.K_UP, owner, 17, seq, coverage, digest, sent), sent


def tomb(seq: int, owner: int = 0, digest: int = 7) -> tuple[bytes, float]:
    sent = time.time()
    return gw.frame(gw.K_TOMB, owner, 17, seq, 0, digest, sent), sent


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    rows: list[dict[str, object]] = []

    r = relay()
    configure(r, gw.MODE_LATEST, merge=True)
    for seq in range(10):
        frame, sent = up(seq)
        r.enqueue(frame, gw.K_UP, 0, seq, 1024, 7, sent)
    rows.append({"policy": "LatestOnly", "check": "ten extensions retain one latest unsent update",
                 "status": "PASS" if len(r.queue) == 1 and r.drops["superseded"] == 9 else "FAIL",
                 "detail": json.dumps({"queue": len(r.queue), "superseded": r.drops["superseded"]})})

    r = relay()
    configure(r, gw.MODE_RATEFIFO, rate=0.0, burst=1)
    for seq in range(2):
        frame, sent = up(seq)
        r.enqueue(frame, gw.K_UP, 0, seq, 1024, 7, sent)
    rows.append({"policy": "RateFIFO", "check": "token bucket is semantic-free and limits admission",
                 "status": "PASS" if len(r.queue) == 1 and r.drops["rate_limit"] == 1 else "FAIL",
                 "detail": json.dumps({"queue": len(r.queue), "rate_limit": r.drops["rate_limit"]})})

    r = relay()
    configure(r, gw.MODE_STATIC, merge=True, priority=True, dedup=2)
    for owner in range(3):
        frame, sent = up(owner, owner=owner)
        r.enqueue(frame, gw.K_UP, owner, owner, 1024, 7, sent)
    frame, sent = tomb(9, owner=0)
    r.enqueue(frame, gw.K_TOMB, 0, 9, 0, 7, sent)
    rows.append({"policy": "StaticSemantic", "check": "replica cap and non-preemptive tombstone priority",
                 "status": "PASS" if r.drops["replica_cap"] == 1 and len(r.pqueue) == 1 else "FAIL",
                 "detail": json.dumps({"duplicate_holder": r.drops["replica_cap"], "priority_queue": len(r.pqueue)})})

    r = relay()
    configure(r, gw.MODE_AGECOV)
    old, old_sent = up(1, coverage=512, age=4.0)
    fresh, fresh_sent = up(2, coverage=4096, age=0.0)
    r.enqueue(old, gw.K_UP, 0, 1, 512, 7, old_sent)
    r.enqueue(fresh, gw.K_UP, 0, 2, 4096, 8, fresh_sent)
    selected = max(r.queue, key=lambda data: (r.score(gw.HDR.unpack(data[:32])[4], gw.HDR.unpack(data[:32])[6]), -gw.HDR.unpack(data[:32])[3]))
    rows.append({"policy": "AgeCov-Greedy", "check": "selection is exactly age x coverage / bytes",
                 "status": "PASS" if gw.HDR.unpack(selected[:32])[3] == 1 else "FAIL",
                 "detail": json.dumps({"selected_seq": gw.HDR.unpack(selected[:32])[3]})})

    r = relay()
    configure(r, gw.MODE_ADAPTIVE, merge=True, priority=True, adaptive=True, dedup=2)
    r.ewma_dq = 10.0
    frame, sent = up(1, coverage=1)
    r.enqueue(frame, gw.K_UP, 0, 1, 1, 7, sent)
    rows.append({"policy": "Adaptive", "check": "congestion-aware low-utility admission activates",
                 "status": "PASS" if r.drops["low_utility"] == 1 else "FAIL",
                 "detail": json.dumps({"low_utility": r.drops["low_utility"]})})

    # FullSync's absence of a semantic transformation is checked separately
    # from the individual mechanisms above.
    r = relay()
    configure(r, gw.MODE_FULLSYNC)
    for seq in range(3):
        frame, sent = up(seq)
        r.enqueue(frame, gw.K_UP, 0, seq, 1024, 7, sent)
    rows.append({"policy": "FullSync", "check": "all events remain FIFO without suppression",
                 "status": "PASS" if len(r.queue) == 3 and not r.drops else "FAIL",
                 "detail": json.dumps({"queue": len(r.queue), "drops": dict(r.drops)})})

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["policy", "check", "status", "detail"])
        writer.writeheader()
        writer.writerows(rows)
    print(json.dumps(rows, indent=2))
    if any(row["status"] != "PASS" for row in rows):
        raise SystemExit("policy unit checks failed")


if __name__ == "__main__":
    main()
