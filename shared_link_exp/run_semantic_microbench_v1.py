#!/usr/bin/env python3
"""Targeted semantic-dissemination checks on B02's real TCP/tc state path.

This intentionally does *not* call vLLM or allocate a GPU.  It uses the
three configured gateway agents (instances 0--2) and the same 64-byte TCP
frames, relay and HTB egress qdisc as the live serving study.  Each case has
a deliberately matched workload for one semantic operation:

* merge: repeated extensions of the same owner/prefix;
* priority: a critical invalidation behind ordinary queued extensions;
* dedup: replica-overlap sweep with the same distinct-prefix coverage.

The output is a compact raw JSON plus a flat CSV for the final evidence pack.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import statistics
import subprocess
import time
from pathlib import Path

import run_live_shared_link_v3 as live


ROOT = Path(__file__).resolve().parent
RESULTS = ROOT / "live_v3" / "results"
CELL_RATE = ROOT / "net" / "cell_rate.sh"
WIRE_BYTES = live.WIRE_BYTES_PER_MSG


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = (len(ordered) - 1) * p
    lo, hi = int(rank), min(int(rank) + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (rank - lo)


def set_rate(bit_per_s: int) -> None:
    subprocess.run(["bash", str(CELL_RATE), "--sig-bit", str(bit_per_s)], check=True, text=True)


async def flush_agents(link: live.LinkRuntime) -> None:
    for writer in link.agent_writers:
        await writer.drain()


async def require_deliveries(link: live.LinkRuntime, expected: int, label: str, timeout_s: float = 120.0) -> None:
    if not await wait_for(link, lambda: link.received >= expected, timeout_s=timeout_s):
        raise RuntimeError(f"{label}: received {link.received}, expected {expected} frames")


async def wait_for(link: live.LinkRuntime, predicate, timeout_s: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return bool(predicate())


async def close_link(link: live.LinkRuntime) -> None:
    for task in link.agent_readers:
        task.cancel()
    for writer in link.agent_writers:
        writer.close()
    if link.down_writer is not None:
        # `server.close()` stops accepts but leaves this accepted downstream
        # connection alive.  Explicitly close it so the relay reconnects to
        # the fresh endpoint of the next isolated semantic case.
        link.down_writer.close()
    link._server.close()
    await asyncio.sleep(0)


async def configure(link: live.LinkRuntime, policy: str, cell_id: int, names: list[str], max_inflight: int) -> live.Dispatcher:
    dispatcher = live.Dispatcher(j=4, prefill_tokens_per_ms=50.0, queue_penalty_ms=2.0, guard_ms=0.5)
    digest_map = {live.digest64(name): name for name in names}
    prior_downstream = link.down_writer
    await link.configure_cell(dispatcher, cell_id, digest_map, policy, global_topk=16, relay_max_inflight=max_inflight)
    # `K_RESET` deliberately closes the old downstream TCP socket to make
    # cells independent.  Wait for the relay to establish the replacement
    # socket before injecting this cell's first frame; otherwise a short
    # microbench can finish its drain timeout before any frame has a route.
    # A reset deliberately retires the current socket.  Require a different
    # writer object, not merely a non-null stale reference, before injecting
    # frames for the new cell.
    if not await wait_for(link, lambda: link.down_writer is not None and link.down_writer is not prior_downstream, timeout_s=15.0):
        raise RuntimeError(f"downstream relay connection did not recover for cell {cell_id}")
    return dispatcher


def stats_summary(stats: dict, dispatcher: live.Dispatcher, name: str) -> dict:
    holders = sum(int(name in owner) for owner in dispatcher.index)
    return {
        **stats,
        "visible_holders": holders,
        "visible_coverage": max((owner.get(name, 0) for owner in dispatcher.index), default=0),
    }


def delivered_stats(link: live.LinkRuntime) -> dict:
    """Stats derived from frames actually received at the dispatcher.

    The experiment's evidence is the downstream reception/coverage, not an
    additional control RPC.  Under a deliberately tiny link, that RPC can
    itself block while the semantic data path has already completed.  Keep
    this benchmark independent of that auxiliary query.
    """
    return {"relay_forwarded": link.received}


async def merge_case(link: live.LinkRuntime, policy: str, cell_id: int, rate: int) -> dict:
    name = f"merge-prefix-{policy}-{cell_id}"
    dispatcher = await configure(link, policy, cell_id, [name], max_inflight=1)
    started = time.time()
    for step in range(10):
        link.send(live.K_UP, 0, name, 256 * (step + 1))
    await flush_agents(link)
    print(json.dumps({"event": "semantic_drain_begin", "mechanism": "merge", "cell": cell_id}), flush=True)
    # Exact FIFO must deliver every extension.  Under merge, the first frame
    # may have crossed the non-preemptive TCP release boundary before the
    # rest of the burst arrives.  The semantic invariant is therefore that
    # the *latest* coverage becomes visible, not exactly one received frame.
    if policy == "exact_fifo":
        await require_deliveries(link, 10, f"merge/{policy}")
    elif not await wait_for(link, lambda: dispatcher.index[0].get(name) == 2560, timeout_s=120.0):
        raise RuntimeError(f"merge/{policy}: final coverage did not arrive")
    print(json.dumps({"event": "semantic_drain_end", "mechanism": "merge", "cell": cell_id, "received": link.received}), flush=True)
    stats = delivered_stats(link)
    final = stats_summary(stats, dispatcher, name)
    final.update({
        "mechanism": "merge",
        "policy": policy,
        "rate_bit_per_s": rate,
        "updates_generated": 10,
        "duration_s": time.time() - started,
        "expected_final_coverage": 2560,
        "final_coverage_correct": final["visible_coverage"] == 2560,
        "wire_bytes_forwarded": stats.get("relay_forwarded", 0) * WIRE_BYTES,
        "upsert_delay_p95_s": percentile(link.delays["upsert"], 0.95),
        "delivered_updates": len(link.delays["upsert"]),
    })
    return final


async def priority_case(link: live.LinkRuntime, policy: str, cell_id: int, rate: int) -> dict:
    critical = f"critical-prefix-{policy}-{cell_id}"
    routine = [f"routine-{policy}-{cell_id}-{i}" for i in range(18)]
    dispatcher = await configure(link, policy, cell_id, [critical, *routine], max_inflight=1)
    # Establish an actually visible holder, then create a real FIFO backlog.
    link.send(live.K_UP, 0, critical, 2048)
    await flush_agents(link)
    await wait_for(link, lambda: critical in dispatcher.index[0], timeout_s=10.0)
    link.delays["tombstone"].clear()
    for i, name in enumerate(routine):
        link.send(live.K_UP, i % 3, name, 256 + i)
    link.send(live.K_TOMB, 0, critical, 0)
    await flush_agents(link)
    print(json.dumps({"event": "semantic_priority_wait_begin", "cell": cell_id}), flush=True)
    invalidated = await wait_for(link, lambda: critical not in dispatcher.index[0], timeout_s=120.0)
    print(json.dumps({"event": "semantic_priority_wait_end", "cell": cell_id, "received": link.received, "invalidated": invalidated}), flush=True)
    stats = delivered_stats(link)
    return {
        **stats_summary(stats, dispatcher, critical),
        "mechanism": "priority",
        "policy": policy,
        "rate_bit_per_s": rate,
        "routine_updates_before_invalidation": len(routine),
        "invalidation_delivered": invalidated,
        "invalidation_delay_s": link.delays["tombstone"][-1] if link.delays["tombstone"] else None,
        "ordinary_upserts_delivered_before_invalidation": len(link.delays["upsert"]) - 1,
        "wire_bytes_forwarded": stats.get("relay_forwarded", 0) * WIRE_BYTES,
    }


async def dedup_case(link: live.LinkRuntime, policy: str, overlap_percent: int, cell_id: int, rate: int) -> dict:
    prefix_count = 8
    replicated = prefix_count * overlap_percent // 100
    names = [f"dedup-{policy}-{overlap_percent}-{cell_id}-{i}" for i in range(prefix_count)]
    dispatcher = await configure(link, policy, cell_id, names, max_inflight=1)
    for index, name in enumerate(names):
        owners = range(3) if index < replicated else [index % 3]
        for owner in owners:
            link.send(live.K_UP, owner, name, 1024 + index)
    await flush_agents(link)
    print(json.dumps({"event": "semantic_drain_begin", "mechanism": "dedup", "cell": cell_id}), flush=True)
    expected = prefix_count + (replicated if policy == "dedup_only" else 2 * replicated)
    await require_deliveries(link, expected, f"dedup/{policy}/overlap={overlap_percent}")
    print(json.dumps({"event": "semantic_drain_end", "mechanism": "dedup", "cell": cell_id, "received": link.received}), flush=True)
    stats = delivered_stats(link)
    unique_visible = sum(int(any(name in owner for owner in dispatcher.index)) for name in names)
    forwarded = stats.get("relay_forwarded", 0)
    return {
        **stats,
        "mechanism": "dedup",
        "policy": policy,
        "rate_bit_per_s": rate,
        "overlap_percent": overlap_percent,
        "distinct_prefixes_generated": prefix_count,
        "replicated_prefixes": replicated,
        "updates_generated": prefix_count + 2 * replicated,
        "unique_prefixes_visible": unique_visible,
        "unique_prefixes_per_wire_byte": unique_visible / (forwarded * WIRE_BYTES) if forwarded else 0.0,
        "wire_bytes_forwarded": forwarded * WIRE_BYTES,
        "upsert_delay_p95_s": percentile(link.delays["upsert"], 0.95),
    }


async def run(args: argparse.Namespace) -> list[dict]:
    # This is an explicit safety guard: the microbench only opens agents 0--2
    # and never creates a connection to a GPU3/vLLM endpoint.
    live.URLS = [f"http://127.0.0.1:{8000 + index}" for index in range(3)]
    set_rate(args.rate_bit_per_s)
    rows: list[dict] = []
    cell = 60_000

    async def isolated(case):
        """Run one semantic workload with a fresh dispatcher TCP endpoint.

        Resetting a live-serving cell intentionally closes the downstream
        socket.  Reusing a single endpoint for many tiny microbench cells
        introduces a control-plane race that is unrelated to the semantic
        operation under test.  A fresh endpoint per case retains the same
        relay/HTB data path while making the cases genuinely independent.
        """
        link = live.LinkRuntime()
        await link.start()
        try:
            print(json.dumps({"event": "semantic_wait_initial_downstream"}), flush=True)
            if not await wait_for(link, lambda: link.down_writer is not None, timeout_s=15.0):
                raise RuntimeError("relay did not establish initial downstream connection")
            print(json.dumps({"event": "semantic_case_begin"}), flush=True)
            row = await case(link)
            print(json.dumps({"event": "semantic_case_end"}), flush=True)
            return row
        finally:
            print(json.dumps({"event": "semantic_close_begin"}), flush=True)
            await close_link(link)
            print(json.dumps({"event": "semantic_close_end"}), flush=True)
            await asyncio.sleep(0.25)

    for rep in range(args.repetitions):
        for policy in ("exact_fifo", "merge_only"):
            row = await isolated(lambda link: merge_case(link, policy, cell, args.rate_bit_per_s))
            row.update({"rep": rep, "cell_id": cell})
            rows.append(row)
            cell += 1
        for policy in ("exact_fifo", "priority_only"):
            row = await isolated(lambda link: priority_case(link, policy, cell, args.rate_bit_per_s))
            row.update({"rep": rep, "cell_id": cell})
            rows.append(row)
            cell += 1
        for overlap in (0, 25, 50, 75):
            for policy in ("exact_fifo", "dedup_only"):
                row = await isolated(lambda link: dedup_case(link, policy, overlap, cell, args.rate_bit_per_s))
                row.update({"rep": rep, "cell_id": cell})
                rows.append(row)
                cell += 1
    return rows


def write_outputs(tag: str, rows: list[dict], args: argparse.Namespace) -> None:
    invalid = []
    for row in rows:
        if row["mechanism"] == "merge" and not row["final_coverage_correct"]:
            invalid.append(f"merge rep={row['rep']} policy={row['policy']}")
        if row["mechanism"] == "priority" and not row["invalidation_delivered"]:
            invalid.append(f"priority rep={row['rep']} policy={row['policy']}")
        if row["mechanism"] == "dedup" and row["unique_prefixes_visible"] != row["distinct_prefixes_generated"]:
            invalid.append(f"dedup rep={row['rep']} policy={row['policy']} overlap={row['overlap_percent']}")
    if invalid:
        raise RuntimeError("semantic microbench validity failure: " + "; ".join(invalid))
    RESULTS.mkdir(parents=True, exist_ok=True)
    raw = {
        "tag": tag,
        "timestamp_unix": time.time(),
        "transport": "real TCP gateway plus Linux HTB/tc egress shaping",
        "gpu_usage": "none; opens only relay agents 0, 1, 2",
        "instances": [0, 1, 2],
        "rate_bit_per_s": args.rate_bit_per_s,
        "repetitions": args.repetitions,
        "rows": rows,
    }
    (RESULTS / f"semantic_microbench_{tag}.json").write_text(json.dumps(raw, indent=2, sort_keys=True) + "\n")
    columns = list(dict.fromkeys(key for row in rows for key in row))
    with (RESULTS / f"semantic_microbench_{tag}.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", required=True)
    parser.add_argument("--rate-bit-per-s", type=int, default=562)
    parser.add_argument("--repetitions", type=int, default=3)
    args = parser.parse_args()
    if args.repetitions < 1:
        raise ValueError("--repetitions must be positive")
    rows = asyncio.run(run(args))
    write_outputs(args.tag, rows, args)
    print(json.dumps({"tag": args.tag, "rows": len(rows), "rate_bit_per_s": args.rate_bit_per_s}, sort_keys=True))


if __name__ == "__main__":
    main()
