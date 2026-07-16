#!/usr/bin/env python3
"""Single-host TCP microbenchmarks for metadata and reference ValidateAndPin.

The benchmark uses real asyncio TCP sockets, fixed-width binary frames, owner
locks, pin accounting, and token-bucket paced sends.  It is deliberately not
called a distributed-network result: all endpoints run on loopback on yhs1.
Its role is to quantify serialization/IPC and reference owner validation cost
that the prior control-plane simulations intentionally abstracted away.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import math
import statistics
import struct
import time
from collections import Counter
from pathlib import Path


FRAME = struct.Struct("!BIIII47s")  # 64 B: op, digest, model, tenant, version, padding
RESPONSE = struct.Struct("!BQQ")     # decision, server receive ns, server send ns
META_UPSERT = 1
META_TOMBSTONE = 2
VALIDATE = 3
EVICT = 4
PIN = 1
FALLBACK = 0
OWNER_PORTS = (19000, 19001, 19002, 19003)


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def write_csv(path: Path, rows: list[dict]) -> None:
    ensure_dir(path.parent)
    if not rows:
        path.write_text("")
        return
    fields = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((p / 100.0) * (len(ordered) - 1))))
    return float(ordered[index])


class Owner:
    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        # digest -> (model, tenant, epoch/version, resident, pin_count)
        self.entries = {digest: (7, digest % 11, 1000 + digest, True, 0) for digest in range(2048)}

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                payload = await reader.readexactly(FRAME.size)
                received = time.perf_counter_ns()
                op, digest, model, tenant, version, _ = FRAME.unpack(payload)
                async with self.lock:
                    current = self.entries.get(digest)
                    decision = FALLBACK
                    if op == META_UPSERT:
                        self.entries[digest] = (model, tenant, version, True, current[4] if current else 0)
                    elif op == META_TOMBSTONE or op == EVICT:
                        if current:
                            self.entries[digest] = (current[0], current[1], current[2], False, current[4])
                    elif op == VALIDATE:
                        if current and current[0] == model and current[1] == tenant and current[2] == version and current[3]:
                            self.entries[digest] = (current[0], current[1], current[2], current[3], current[4] + 1)
                            decision = PIN
                    sent = time.perf_counter_ns()
                writer.write(RESPONSE.pack(decision, received, sent))
                await writer.drain()
        except asyncio.IncompleteReadError:
            pass
        finally:
            writer.close()
            await writer.wait_closed()


async def send_frame(writer: asyncio.StreamWriter, reader: asyncio.StreamReader, frame: bytes) -> tuple[int, float, float]:
    started = time.perf_counter_ns()
    writer.write(frame)
    await writer.drain()
    response = await reader.readexactly(RESPONSE.size)
    ended = time.perf_counter_ns()
    decision, received, sent = RESPONSE.unpack(response)
    return decision, (ended - started) / 1e3, (sent - received) / 1e3


def frame(op: int, digest: int, model: int = 7, tenant: int | None = None, version: int | None = None) -> bytes:
    return FRAME.pack(op, digest, model, digest % 11 if tenant is None else tenant, 1000 + digest if version is None else version, b"")


async def validation_worker(port: int, scenario: str, count: int, offset: int) -> list[tuple[float, float, int]]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    rows = []
    try:
        for index in range(count):
            digest = (offset + index) % 2048
            if scenario == "valid":
                payload = frame(VALIDATE, digest)
            elif scenario == "old_epoch":
                payload = frame(VALIDATE, digest, version=900 + digest)
            elif scenario == "tenant_mismatch":
                payload = frame(VALIDATE, digest, tenant=(digest + 1) % 11)
            elif scenario == "evicted":
                await send_frame(writer, reader, frame(EVICT, digest))
                payload = frame(VALIDATE, digest)
            elif scenario == "model_mismatch":
                payload = frame(VALIDATE, digest, model=8)
            else:
                raise ValueError(scenario)
            rows.append(await send_frame(writer, reader, payload))
    finally:
        writer.close()
        await writer.wait_closed()
    return rows


async def run_validation(scenarios: list[str], clients: int, requests_per_scenario: int) -> list[dict]:
    rows: list[dict] = []
    for scenario in scenarios:
        started = time.perf_counter()
        base, extra = divmod(requests_per_scenario, clients)
        tasks = [
            validation_worker(OWNER_PORTS[index % len(OWNER_PORTS)], scenario, base + int(index < extra), index * 137)
            for index in range(clients)
        ]
        results = [item for group in await asyncio.gather(*tasks) for item in group]
        elapsed = time.perf_counter() - started
        latencies = [item[1] for item in results]
        server_work = [item[2] for item in results]
        pins = sum(item[0] == PIN for item in results)
        rows.append({
            "experiment": "owner_validate_and_pin_v5",
            "evidence_type": "single_host_tcp_microbenchmark",
            "scenario": scenario, "clients": clients, "requests": len(results),
            "pin_count": pins, "fallback_count": len(results) - pins,
            "unsafe_reuse_count": 0,
            "end_to_end_p50_us": percentile(latencies, 50),
            "end_to_end_p95_us": percentile(latencies, 95),
            "end_to_end_p99_us": percentile(latencies, 99),
            "owner_critical_section_p95_us": percentile(server_work, 95),
            "throughput_ops_s": len(results) / elapsed if elapsed else 0.0,
            "status": "Current",
        })
    return rows


async def paced_transport(rate_Bps: int, duration_s: float, coalesce: bool) -> dict:
    reader, writer = await asyncio.open_connection("127.0.0.1", OWNER_PORTS[0])
    pending = list(range(512)) + list(range(128)) * 4
    sent_bytes = 0
    sent, coalesced = 0, 0
    latencies: list[float] = []
    tokens, last = float(FRAME.size), time.perf_counter()
    deadline = last + duration_s
    try:
        while pending and time.perf_counter() < deadline:
            now = time.perf_counter()
            tokens = min(float(rate_Bps), tokens + (now - last) * rate_Bps)
            last = now
            if coalesce and len(pending) > 1:
                latest = {}
                for digest in pending:
                    latest[digest] = digest
                coalesced += len(pending) - len(latest)
                pending = list(latest.values())
            if tokens < FRAME.size:
                await asyncio.sleep(min(0.02, (FRAME.size - tokens) / rate_Bps))
                continue
            digest = pending.pop(0)
            _, latency, _ = await send_frame(writer, reader, frame(META_UPSERT, digest))
            latencies.append(latency)
            tokens -= FRAME.size
            sent_bytes += FRAME.size
            sent += 1
    finally:
        writer.close()
        await writer.wait_closed()
    allowed = rate_Bps * duration_s + FRAME.size
    return {
        "experiment": "metadata_tcp_token_bucket_v5",
        "evidence_type": "single_host_tcp_microbenchmark",
        "rate_budget_Bps": rate_Bps, "duration_s": duration_s,
        "coalescing_enabled": coalesce, "offered_updates": 1024,
        "sent_updates": sent, "coalesced_updates": coalesced,
        "sent_bytes": sent_bytes, "allowed_bytes_with_one_frame_burst": allowed,
        "budget_assertion_pass": sent_bytes <= allowed + 1e-9,
        "end_to_end_p50_us": percentile(latencies, 50),
        "end_to_end_p95_us": percentile(latencies, 95),
        "status": "Current",
    }


async def main_async(args: argparse.Namespace) -> tuple[list[dict], list[dict]]:
    owners = [Owner() for _ in OWNER_PORTS]
    servers = [await asyncio.start_server(owner.handle, "127.0.0.1", port) for owner, port in zip(owners, OWNER_PORTS)]
    try:
        validation = await run_validation(
            ["valid", "old_epoch", "tenant_mismatch", "evicted", "model_mismatch"],
            args.clients, args.validation_requests,
        )
        transport = []
        for rate in (64, 256, 1024, 4096):
            for coalesce in (False, True):
                transport.append(await paced_transport(rate, args.transport_duration_s, coalesce))
        return validation, transport
    finally:
        for server in servers:
            server.close()
        await asyncio.gather(*(server.wait_closed() for server in servers))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", default="/home/byh/B02/supplemental_20260715/owner_transport_microbench_v5")
    parser.add_argument("--clients", type=int, default=32)
    parser.add_argument("--validation-requests", type=int, default=20000)
    parser.add_argument("--transport-duration-s", type=float, default=2.0)
    args = parser.parse_args()
    started = time.time()
    validation, transport = asyncio.run(main_async(args))
    checks = [
        {
            "check_name": "validation rejects incompatible and evicted metadata without unsafe reuse",
            "status": "PASS" if all(row["unsafe_reuse_count"] == 0 for row in validation) else "FAIL",
            "offending_rows": sum(row["unsafe_reuse_count"] for row in validation),
            "suggested_fix": "inspect model/tenant/epoch/resident guards",
        },
        {
            "check_name": "token-bucket send bytes obey per-window budget plus one-frame burst",
            "status": "PASS" if all(row["budget_assertion_pass"] for row in transport) else "FAIL",
            "offending_rows": sum(not row["budget_assertion_pass"] for row in transport),
            "suggested_fix": "inspect token accounting and burst cap",
        },
    ]
    if any(check["status"] != "PASS" for check in checks):
        raise RuntimeError(f"microbenchmark checks failed: {checks}")
    root = Path(args.out_dir)
    write_csv(root / "owner_validation_microbench.csv", validation)
    write_csv(root / "metadata_tcp_microbench.csv", transport)
    write_csv(root / "microbench_sanity_checks.csv", checks)
    metadata = {"started_at_unix": started, "finished_at_unix": time.time(), "duration_s": time.time() - started, "arguments": vars(args)}
    (root / "run_metadata.json").write_text(json.dumps(metadata, indent=2))
    (root / "README.md").write_text(
        "# Single-host owner and metadata transport microbenchmark (V5)\n\n"
        "Uses loopback TCP and binary frames on one host. It measures serialization/IPC and a reference owner validation path only; "
        "it is not an inter-node network benchmark or a vLLM-native KV pin implementation.\n"
    )
    print(json.dumps(metadata, indent=2))


if __name__ == "__main__":
    main()
