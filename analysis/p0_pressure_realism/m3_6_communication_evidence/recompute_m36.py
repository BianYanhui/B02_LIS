#!/usr/bin/env python3
"""Recompute P0-M3.6 communication costs from raw traces.

Reads C1 frames.bin, the M3 converter CSV, and the M3.5 gateway log.
Does not start vLLM, the gateway, or a fan-in harness, and does not
overwrite C1, M3, or M3.5 outputs.
"""
from __future__ import annotations

import csv
import json
import struct
import sys
from pathlib import Path

ROOT = Path("/home/byh/B02")
sys.path.insert(0, str(ROOT))

import msgspec
from vllm.distributed.kv_events import BlockStored, KVEventBatch

from experiments.icc_kv.wire import FRAME as CODEC_FRAME
from experiments.icc_kv.wire import HDR, WIRE_BYTES, frame

OUT = ROOT / "analysis/p0_pressure_realism/m3_6_communication_evidence"
C1_FRAMES = ROOT / "analysis/p0_pressure_realism/c1_real_indexer/raw/frames.bin"
CONVERTER = ROOT / "analysis/p0_pressure_realism/m3_native_compact_motivation/raw/converter_updates.csv"
M35 = ROOT / "analysis/p0_pressure_realism/m3_5_native_compact_e2e"
CAPTURE = struct.Struct("<QIII")
WINDOWS_S = (0.001, 0.010, 0.100, 1.0)


def load_frames(path: Path):
    data = path.read_bytes()
    frames = []
    offset = 0
    while offset + CAPTURE.size <= len(data):
        recv_ns, topic_len, seq_len, payload_len = CAPTURE.unpack_from(data, offset)
        offset += CAPTURE.size
        end = offset + topic_len + seq_len + payload_len
        if end > len(data):
            raise SystemExit(f"truncated capture at offset {offset}")
        topic = data[offset:offset + topic_len]
        seq = data[offset + topic_len:offset + topic_len + seq_len]
        payload = data[offset + topic_len + seq_len:end]
        offset = end
        frames.append((recv_ns, topic, seq, payload))
    if offset != len(data):
        raise SystemExit(f"capture tail {len(data) - offset} bytes")
    return frames, len(data)


def percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    pos = q * (len(ordered) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(ordered) - 1)
    frac = pos - lo
    return float(ordered[lo] * (1 - frac) + ordered[hi] * frac)


def sliding_profile(stamps_ns: list[int], weights: list[int], window_s: float) -> dict:
    """Byte volume of a sliding window on a sorted arrival series."""
    window_ns = int(round(window_s * 1e9))
    n = len(stamps_ns)
    if n == 0 or window_ns <= 0:
        return {
            "peak_bytes": 0,
            "p95_event_aligned_bytes": 0.0,
            "p95_uniform_grid_bytes": 0.0,
            "uniform_empty_fraction": 1.0,
            "uniform_samples": 0,
        }
    order = sorted(range(n), key=lambda i: stamps_ns[i])
    times = [stamps_ns[i] for i in order]
    prefix = [0]
    for i in order:
        prefix.append(prefix[-1] + weights[i])

    def volume(left_ns: int, right_ns: int) -> int:
        # sum of weights with left_ns < t <= right_ns
        lo, hi = 0, n
        while lo < hi:
            mid = (lo + hi) // 2
            if times[mid] <= left_ns:
                lo = mid + 1
            else:
                hi = mid
        left = lo
        lo, hi = 0, n
        while lo < hi:
            mid = (lo + hi) // 2
            if times[mid] <= right_ns:
                lo = mid + 1
            else:
                hi = mid
        right = lo
        return prefix[right] - prefix[left]

    event_volumes = [volume(t - window_ns, t) for t in times]
    start, end = times[0], times[-1]
    step = max(window_ns // 10, 1)
    grid = []
    cursor = start
    while cursor <= end:
        grid.append(volume(cursor - window_ns, cursor))
        cursor += step
    if not grid or grid[-1] != volume(end - window_ns, end):
        grid.append(volume(end - window_ns, end))
    empty = sum(1 for item in grid if item == 0)
    return {
        "peak_bytes": max(event_volumes) if event_volumes else 0,
        "p95_event_aligned_bytes": percentile([float(v) for v in event_volumes], 0.95),
        "p95_uniform_grid_bytes": percentile([float(v) for v in grid], 0.95),
        "uniform_empty_fraction": empty / len(grid),
        "uniform_samples": len(grid),
    }


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    frames, file_bytes = load_frames(C1_FRAMES)
    decoder = msgspec.msgpack.Decoder(KVEventBatch)
    messages = []
    event_count = 0
    hash_appearances = 0
    stored = removed = cleared = 0
    token_messages = 0
    token_message_bytes = 0
    stripped_bytes = 0
    reencode_failures = 0
    seqs = []
    deltas_s = []
    for recv_ns, topic, seq, payload in frames:
        batch = decoder.decode(payload)
        seq_no = int.from_bytes(seq, "big") if len(seq) == 8 else -1
        seqs.append(seq_no)
        if batch.ts:
            deltas_s.append(recv_ns / 1e9 - float(batch.ts))
        n_hash = 0
        n_events = 0
        rebuilt = []
        has_tokens = False
        for event in batch.events:
            n_events += 1
            if isinstance(event, BlockStored):
                stored += 1
                n_hash += len(event.block_hashes)
                if event.token_ids:
                    has_tokens = True
                    rebuilt.append(BlockStored(
                        block_hashes=event.block_hashes,
                        parent_block_hash=event.parent_block_hash,
                        token_ids=[],
                        block_size=event.block_size,
                        lora_id=event.lora_id,
                        medium=event.medium,
                    ))
                else:
                    rebuilt.append(event)
            else:
                name = type(event).__name__
                if name == "BlockRemoved":
                    removed += 1
                    n_hash += len(event.block_hashes)
                elif name == "AllBlocksCleared":
                    cleared += 1
                rebuilt.append(event)
        event_count += n_events
        hash_appearances += n_hash
        if has_tokens:
            token_messages += 1
            token_message_bytes += len(payload)
        try:
            empty = KVEventBatch(ts=batch.ts, events=rebuilt, data_parallel_rank=batch.data_parallel_rank)
            stripped_bytes += len(msgspec.msgpack.encode(empty))
        except Exception:
            reencode_failures += 1
            stripped_bytes += len(payload)
        messages.append({
            "recv_ns": recv_ns,
            "batch_ts_s": float(batch.ts) if batch.ts else None,
            "seq": seq_no,
            "topic_len": len(topic),
            "seq_len": len(seq),
            "payload_len": len(payload),
            "zmq_body": len(topic) + len(seq) + len(payload),
            "events": n_events,
            "hashes": n_hash,
        })

    payload_bytes = sum(item["payload_len"] for item in messages)
    zmq_bytes = sum(item["zmq_body"] for item in messages)
    t0 = messages[0]["recv_ns"]
    t1 = messages[-1]["recv_ns"]
    duration_s = (t1 - t0) / 1e9
    seq_gaps = sum(1 for a, b in zip(seqs, seqs[1:]) if b != a + 1)

    updates = list(csv.DictReader(CONVERTER.open()))
    kinds = {}
    for row in updates:
        kinds[row["kind"]] = kinds.get(row["kind"], 0) + 1
    adapter_ns = [int(row["recv_ns"]) for row in updates]
    codec_len = len(frame(1, 0, 1, 1, 1, 1, 1.0))

    results = json.loads((M35 / "raw/results.json").read_text())
    phase1 = results["phase1"]
    gw = phase1["gateway"]
    cell351 = None
    tick351 = []
    for line in (M35 / "raw/gateway.log").read_text().splitlines():
        item = json.loads(line)
        if item.get("cell") != 351:
            continue
        if item.get("event") == "cell_stats":
            cell351 = item
        elif item.get("event") == "tick":
            tick351.append(item)

    native_payload_profile = {
        f"{window:g}": sliding_profile(
            [item["recv_ns"] for item in messages],
            [item["payload_len"] for item in messages],
            window,
        )
        for window in WINDOWS_S
    }
    native_zmq_profile = {
        f"{window:g}": sliding_profile(
            [item["recv_ns"] for item in messages],
            [item["zmq_body"] for item in messages],
            window,
        )
        for window in WINDOWS_S
    }
    adapter_profile = {
        f"{window:g}": sliding_profile(adapter_ns, [codec_len] * len(adapter_ns), window)
        for window in WINDOWS_S
    }
    same_ts = {}
    for stamp in adapter_ns:
        same_ts[stamp] = same_ts.get(stamp, 0) + 1
    busiest_stamp = max(same_ts, key=same_ts.get)

    numbers = {
        "capture_file_bytes": file_bytes,
        "native": {
            "messages": len(messages),
            "events": event_count,
            "stored": stored,
            "removed": removed,
            "cleared": cleared,
            "hash_appearances": hash_appearances,
            "msgpack_payload_bytes": payload_bytes,
            "zmq_body_bytes": zmq_bytes,
            "zmq_framing_bytes": zmq_bytes - payload_bytes,
            "tcp_wire_bytes": "NOT_MEASURED",
            "seq_min": min(seqs),
            "seq_max": max(seqs),
            "seq_gaps": seq_gaps,
            "recv_ns_first": t0,
            "recv_ns_last": t1,
            "duration_s": duration_s,
            "mean_payload_Bps": payload_bytes / duration_s,
            "token_messages": token_messages,
            "token_message_payload_bytes": token_message_bytes,
            "reencoded_without_token_ids_bytes": stripped_bytes,
            "derived_token_id_bytes": payload_bytes - stripped_bytes,
            "token_id_fraction_of_payload": (payload_bytes - stripped_bytes) / payload_bytes,
            "reencode_failures": reencode_failures,
            "recv_minus_batch_ts_s": {
                "n": len(deltas_s),
                "min": min(deltas_s) if deltas_s else None,
                "p50": percentile(deltas_s, 0.50) if deltas_s else None,
                "max": max(deltas_s) if deltas_s else None,
            },
            "payload_windows": native_payload_profile,
            "zmq_windows": native_zmq_profile,
        },
        "codec": {
            "FRAME": CODEC_FRAME,
            "packed_len": codec_len,
            "header_len": HDR.size,
            "WIRE_BYTES": WIRE_BYTES,
            "wire_minus_frame": WIRE_BYTES - CODEC_FRAME,
        },
        "adapter": {
            "records": len(updates),
            "kinds": kinds,
            "bytes_at_codec": len(updates) * codec_len,
            "recv_ns_first": min(adapter_ns),
            "recv_ns_last": max(adapter_ns),
            "inside_native_window": min(adapter_ns) >= t0 and max(adapter_ns) <= t1,
            "distinct_timestamps": len(same_ts),
            "busiest_timestamp_records": same_ts[busiest_stamp],
            "busiest_timestamp_bytes": same_ts[busiest_stamp] * codec_len,
            "windows": adapter_profile,
        },
        "gateway_cell_351": {
            "log_cell_stats": cell351,
            "results_gateway": gw,
            "dispatcher_applied": phase1.get("dispatcher_applied"),
            "tick_count": len(tick351),
            "tick_has_timestamp": False,
            "egress_window_profile": "NOT_MEASURED",
        },
    }
    (OUT / "raw_recount.json").write_text(json.dumps(numbers, indent=2) + "\n")

    expected = (
        len(messages) == 212
        and event_count == 252
        and payload_bytes == 284529
        and hash_appearances == 6615
        and len(updates) == 243
        and kinds.get("UPSERT") == 243
        and codec_len == 64
        and gw.get("received") == 243
        and gw.get("forwarded") == 205
        and gw.get("superseded") == 38
        and gw.get("global_topk") == 0
        and cell351 is not None
        and cell351.get("forwarded") == 205
        and cell351.get("drops", {}).get("superseded") == 38
    )
    print(json.dumps({
        "chain_matches_expected": expected,
        "messages": len(messages),
        "events": event_count,
        "payload": payload_bytes,
        "hashes": hash_appearances,
        "adapter": len(updates),
        "kinds": kinds,
        "codec": codec_len,
        "forwarded": gw.get("forwarded"),
        "superseded": gw.get("superseded"),
        "topk": gw.get("global_topk"),
        "duration_s": round(duration_s, 6),
        "token_fraction": round((payload_bytes - stripped_bytes) / payload_bytes, 6),
        "adapter_inside": numbers["adapter"]["inside_native_window"],
        "busiest_adapter_records": same_ts[busiest_stamp],
        "native_peak_1s": native_payload_profile["1"]["peak_bytes"],
        "adapter_peak_1ms": adapter_profile["0.001"]["peak_bytes"],
    }, indent=2))
    if not expected:
        raise SystemExit("core chain does not match; stop")


if __name__ == "__main__":
    main()
