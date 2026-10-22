#!/usr/bin/env python3
"""Phase 1–2 tables from existing traces. Does not start vLLM or a replay."""
from __future__ import annotations

import csv
import math
import struct
from pathlib import Path

ROOT = Path("/home/byh/B02")
OUT = ROOT / "analysis/p0_pressure_realism/m1_scale_fanin"
P0 = ROOT / "analysis/p0_pressure_realism"
FRAME = struct.Struct("<QIII")
DRAIN_104 = 9408.0  # MEASURED frames/s on a CONFIGURED 10 Mbit path
LINK_10 = 10_000_000
LINK_100 = 100_000_000
LINK_1G = 1_000_000_000
TESTED_EVENTS = 256.0  # highest replay that kept up; not a saturation point


def write_csv(path: Path, rows: list[dict]) -> None:
    columns = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def load_frames():
    data = (P0 / "c1_real_indexer/raw/frames.bin").read_bytes()
    offset = 0
    frames = []
    while offset + FRAME.size <= len(data):
        recv_ns, topic_len, seq_len, payload_len = FRAME.unpack_from(data, offset)
        offset += FRAME.size
        offset += topic_len + seq_len + payload_len
        frames.append((recv_ns, payload_len))
    return frames


def load_events():
    with (P0 / "c1_real_indexer/raw/events.csv").open() as handle:
        return list(csv.DictReader(handle))


def load_prefix():
    with (P0 / "a4_invalidation_audit/prefix_state_transitions.csv").open() as handle:
        rows = list(csv.DictReader(handle))
    return [row for row in rows if row["scenario"] == "C_flood" and row["kind"] in ("UPSERT", "INVALIDATE")]


def bin_max(times_s: list[float], weights: list[float], bin_s: float, offsets_s: list[float], span_s: float) -> float:
    if not times_s or span_s <= 0:
        return 0.0
    nbins = max(1, int(span_s / bin_s) + 1)
    bins = [0.0] * nbins
    for offset in offsets_s:
        for time_s, weight in zip(times_s, weights):
            # The last event sits exactly at span_s. A plain modulo would
            # alias it onto t=0 and inflate the first bin.
            position = (min(time_s, math.nextafter(span_s, 0.0)) + offset) % span_s
            index = min(nbins - 1, int(position / bin_s))
            bins[index] += weight
    return max(bins)


def deployment_rows() -> list[dict]:
    rows = [{
        "model": "measured_testbed",
        "N_gpu": 4,
        "gpus_per_instance": 1,
        "tp": 1,
        "pp": 1,
        "N_serving_instances": 4,
        "N_reporters": 4,
        "N_gateways": 1,
        "reporters_per_gateway": 4,
        "N_dispatchers": 1,
        "reporters_per_dispatcher": 4,
        "event_fanout": 1,
        "sharding": "none",
        "current_wire_instance_id": "fits_u8",
        "label": "OBSERVED",
        "note": "Four independent vLLM processes, one GPU each. Not one TP=4 engine.",
    }]
    for n_gpu in (256, 1024, 4096):
        for gpi in (1, 2, 4, 8):
            reporters = n_gpu // gpi
            wire = "fits_u8" if reporters <= 256 else "EXCEEDS_U8"
            specs = [
                ("A_single_domain", 1, reporters, 1, reporters, "none"),
                ("B_gateway_64_shared_dispatcher", max(1, (reporters + 63) // 64), min(64, reporters), 1, reporters, "gateway_only"),
                ("C_worker_shard_64", max(1, (reporters + 63) // 64), min(64, reporters), max(1, (reporters + 63) // 64), min(64, reporters), "worker_shard"),
            ]
            for model, gateways, per_gw, dispatchers, per_disp, shard in specs:
                rows.append({
                    "model": model,
                    "N_gpu": n_gpu,
                    "gpus_per_instance": gpi,
                    "tp": gpi,
                    "pp": 1,
                    "N_serving_instances": reporters,
                    "N_reporters": reporters,
                    "N_gateways": gateways,
                    "reporters_per_gateway": per_gw,
                    "N_dispatchers": dispatchers,
                    "reporters_per_dispatcher": per_disp,
                    "event_fanout": 1,
                    "sharding": shard,
                    "current_wire_instance_id": wire,
                    "label": "DERIVED",
                    "note": "Reporters equal engines, not GPUs. TP>1 per-engine rate was not measured. Shard size 64 is an analysis choice, not a deployed topology.",
                })
    return rows


def scale_rows() -> list[dict]:
    frames = load_frames()
    events = load_events()
    prefix = load_prefix()
    t0 = frames[0][0]
    span = (frames[-1][0] - t0) / 1e9
    msg_t = [(recv - t0) / 1e9 for recv, _nbytes in frames]
    msg_b = [nbytes for _recv, nbytes in frames]
    # events share batch recv time; use event rows
    ev_t = [(int(row["recv_ns"]) - t0) / 1e9 for row in events]
    ev_h = [int(row["n_blocks"]) for row in events]
    flood_events = [row for row in events if row["scenario"] == "C_flood"]
    ft0 = int(flood_events[0]["recv_ns"])
    ft1 = int(flood_events[-1]["recv_ns"])
    fspan = (ft1 - ft0) / 1e9
    # one timestamp per ZMQ batch inside the flood
    flood_msgs = {}
    for row in flood_events:
        flood_msgs.setdefault(row["batch_seq"], int(row["recv_ns"]))
    fmsg_t = sorted((value - ft0) / 1e9 for value in flood_msgs.values())
    fev_t = [(int(row["recv_ns"]) - ft0) / 1e9 for row in flood_events]
    fev_h = [int(row["n_blocks"]) for row in flood_events]
    pt0 = int(prefix[0]["timestamp_ns"])
    pspan = (int(prefix[-1]["timestamp_ns"]) - pt0) / 1e9
    pt = [(int(row["timestamp_ns"]) - pt0) / 1e9 for row in prefix]

    native_msg_s = len(frames) / span
    native_ev_s = len(events) / span
    native_hash_s = sum(ev_h) / span
    native_B_s = sum(msg_b) / span
    flood_msg_s = len(fmsg_t) / fspan
    flood_ev_s = len(flood_events) / fspan
    flood_hash_s = sum(fev_h) / fspan
    flood_bytes = sum(nbytes for recv, nbytes in frames if ft0 <= recv <= ft1)
    flood_B_s = flood_bytes / fspan
    # Published C_flood window is 68.984 s (P0-A4), not the first-to-last gap.
    prefix_s = 133 / 68.984

    sources = [
        ("A_compact", "A1_original_ideal_per_engine", 2.0711 / 4, 1.5330 / 4, 2.9671 / 4, "EXTRAPOLATED", "uniform split of a 4-engine cluster; per-engine counts were not logged"),
        ("A_compact", "A1_reuse_ideal_per_engine", 1.0714 / 4, 0.8469 / 4, 1.4032 / 4, "EXTRAPOLATED", "uniform split of a 4-engine cluster; tombstones were 0"),
        ("A_compact", "A4_flood_one_gpu", prefix_s, prefix_s, prefix_s, "OBSERVED_ONE_GPU_THEN_EXTRAPOLATED", "cache-full single GPU; not the formal serving mix"),
    ]
    rows = []
    for n_gpu in (256, 1024, 4096):
        for gpi in (1, 2, 4, 8):
            reporters = n_gpu // gpi
            for track, source, mean_r, lo_r, hi_r, label, note in sources:
                for phase in ("EVEN_PHASE", "SYNC_BURST"):
                    if gpi != 1:
                        rows.append(_blank_scale(track, source, n_gpu, gpi, reporters, phase, "NOT_MEASURED", note + "; TP>1 completion rate was not measured"))
                        continue
                    records = mean_r * reporters
                    if source.startswith("A4"):
                        offsets = [0.0] * reporters if phase == "SYNC_BURST" else [i * pspan / reporters for i in range(reporters)]
                        peak_1s = bin_max(pt, [1.0] * len(pt), 1.0, offsets, pspan)
                        peak_100 = bin_max(pt, [1.0] * len(pt), 0.1, offsets, pspan)
                    else:
                        peak_1s = peak_100 = ""
                    rows.append({
                        "track": track,
                        "source": source,
                        "N_gpu": n_gpu,
                        "gpus_per_instance": gpi,
                        "N_reporters": reporters,
                        "phase": phase,
                        "records_per_s": records,
                        "records_per_s_low": lo_r * reporters,
                        "records_per_s_high": hi_r * reporters,
                        "events_per_s": records,
                        "hash_updates_per_s": "NOT_APPLICABLE",
                        "bytes_per_s_payload64": records * 64,
                        "bytes_per_s_wire104": records * 104,
                        "peak_1s_records": peak_1s,
                        "peak_100ms_records": peak_100,
                        "rho_vs_measured_104B_drain": records / DRAIN_104,
                        "rho_vs_nominal_10mbit": records * 104 * 8 / LINK_10,
                        "rho_vs_100mbit_nominal": records * 104 * 8 / LINK_100,
                        "rho_vs_1gbit_nominal": records * 104 * 8 / LINK_1G,
                        "events_over_tested_256": "NOT_APPLICABLE",
                        "hash_identity": "LINEAGE_DIGEST",
                        "label": label if phase == "EVEN_PHASE" else label + "_SYNC_BURST",
                        "note": note,
                    })
            # native whole capture and flood, gpi=1 only for rates
            native_specs = [
                ("B_native", "C1_whole_capture", native_msg_s, native_ev_s, native_hash_s, native_B_s, msg_t, msg_b, ev_t, ev_h, span, "OBSERVED_ONE_GPU_THEN_EXTRAPOLATED"),
                ("B_native", "C1_flood_window", flood_msg_s, flood_ev_s, flood_hash_s, flood_B_s, fmsg_t, [1.0] * len(fmsg_t), fev_t, fev_h, fspan, "OBSERVED_ONE_GPU_THEN_EXTRAPOLATED"),
            ]
            for track, source, msg_s, ev_s, hash_s, byte_s, mt, mw, et, ew, sp, label in native_specs:
                for phase in ("EVEN_PHASE", "SYNC_BURST"):
                    if gpi != 1:
                        rows.append(_blank_scale(track, source, n_gpu, gpi, reporters, phase, "NOT_MEASURED", "per-engine native rate for TP>1 was not measured"))
                        continue
                    offsets = [0.0] * reporters if phase == "SYNC_BURST" else [i * sp / reporters for i in range(reporters)]
                    peak_msg_1s = bin_max(mt, [1.0] * len(mt), 1.0, offsets, sp)
                    peak_hash_1s = bin_max(et, ew, 1.0, offsets, sp)
                    bytes_s = (byte_s * reporters) if byte_s != "" else ""
                    rows.append({
                        "track": track,
                        "source": source,
                        "N_gpu": n_gpu,
                        "gpus_per_instance": gpi,
                        "N_reporters": reporters,
                        "phase": phase,
                        "records_per_s": msg_s * reporters,
                        "records_per_s_low": "",
                        "records_per_s_high": "",
                        "events_per_s": ev_s * reporters,
                        "hash_updates_per_s": hash_s * reporters,
                        "bytes_per_s_payload64": "NOT_APPLICABLE",
                        "bytes_per_s_wire104": "NOT_APPLICABLE",
                        "bytes_per_s_msgpack": bytes_s,
                        "peak_1s_messages": peak_msg_1s,
                        "peak_1s_hash_updates": peak_hash_1s,
                        "peak_100ms_messages": bin_max(mt, [1.0] * len(mt), 0.1, offsets, sp),
                        "rho_vs_measured_104B_drain": "UNIT_MISMATCH",
                        "rho_vs_nominal_10mbit": (bytes_s * 8 / LINK_10) if bytes_s != "" else "NOT_SPLIT",
                        "rho_vs_100mbit_nominal": (bytes_s * 8 / LINK_100) if bytes_s != "" else "NOT_SPLIT",
                        "rho_vs_1gbit_nominal": (bytes_s * 8 / LINK_1G) if bytes_s != "" else "NOT_SPLIT",
                        "events_over_tested_256": (ev_s * reporters) / TESTED_EVENTS,
                        "hash_identity": "COPIED_SINGLE_TRACE",
                        "label": label if phase == "EVEN_PHASE" else label + "_SYNC_BURST",
                        "note": "Same hashes and one worker identity copied N times. Valid as a byte-rate envelope, not as independent index keys. 64x replay was this copy on one connection.",
                    })
    return rows


def _blank_scale(track, source, n_gpu, gpi, reporters, phase, label, note):
    return {
        "track": track,
        "source": source,
        "N_gpu": n_gpu,
        "gpus_per_instance": gpi,
        "N_reporters": reporters,
        "phase": phase,
        "records_per_s": "NOT_MEASURED",
        "events_per_s": "NOT_MEASURED",
        "hash_updates_per_s": "NOT_MEASURED",
        "bytes_per_s_msgpack": "NOT_MEASURED",
        "bytes_per_s_wire104": "NOT_MEASURED",
        "rho_vs_nominal_10mbit": "NOT_MEASURED",
        "events_over_tested_256": "NOT_MEASURED",
        "label": label,
        "note": note,
    }


def capacity_rows() -> list[dict]:
    return [
        {"quantity": "10mbit_htb_drain", "value": 9408, "unit": "frames_per_s", "label": "MEASURED", "scope": "CONFIGURED 10 Mbit path, 104-byte frames", "saturation": "this is the drain of that shaped path, not the NIC"},
        {"quantity": "10mbit_nominal", "value": 10_000_000, "unit": "bit_per_s", "label": "CONFIGURED", "scope": "tc HTB ceiling used by icc_kv", "saturation": "not a measured production quota"},
        {"quantity": "100mbit_nominal", "value": 100_000_000, "unit": "bit_per_s", "label": "SENSITIVITY_ONLY", "scope": "not measured as a control quota in this round", "saturation": "do not cite as production capacity"},
        {"quantity": "1gbit_nominal", "value": 1_000_000_000, "unit": "bit_per_s", "label": "SENSITIVITY_ONLY", "scope": "not measured", "saturation": "do not cite as production capacity"},
        {"quantity": "indexer_replay_kept_up", "value": 256, "unit": "events_per_s", "label": "MEASURED", "scope": "Dynamo debug indexer, one connection, copied single-GPU trace", "saturation": "NOT a saturation throughput; backlog was 0 and the rate was not increased"},
        {"quantity": "indexer_saturation", "value": "NOT_MEASURED", "unit": "events_per_s", "label": "NOT_MEASURED", "scope": "release build and independent keys were not run", "saturation": "unknown"},
        {"quantity": "dispatcher_apply_latency", "value": "p50 1.6 us", "unit": "per_record", "label": "MEASURED", "scope": "control-plane calibration cell, one record", "saturation": "not a throughput"},
        {"quantity": "unconstrained_local_transport", "value": "NOT_MEASURED", "unit": "bytes_per_s", "label": "NOT_MEASURED", "scope": "loopback TCP payload was not captured", "saturation": "unknown"},
    ]


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    write_csv(OUT / "deployment_models.csv", deployment_rows())
    write_csv(OUT / "offered_load_scaling.csv", scale_rows())
    write_csv(OUT / "control_capacity_evidence.csv", capacity_rows())
    # Phase 4–5 were not authorized.
    write_csv(OUT / "fanin_validation.csv", [{
        "status": "NOT_RUN",
        "label": "NOT_MEASURED",
        "reason": "Phase 4 waits for the next OODA decision. No queue time series was collected.",
    }])
    write_csv(OUT / "visibility_lag.csv", [{
        "status": "NOT_RUN",
        "label": "NOT_MEASURED",
        "reason": "Visibility lag was not measured. A drained queue of 0 from P0-C1 is not a scale result.",
    }])
    print("wrote", OUT)


if __name__ == "__main__":
    main()
