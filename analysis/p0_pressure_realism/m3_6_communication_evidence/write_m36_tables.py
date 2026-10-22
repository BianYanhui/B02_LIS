#!/usr/bin/env python3
"""Write P0-M3.6 CSV ledgers and the draft byte-cost figure from raw_recount.json."""
from __future__ import annotations

import csv
import json
from pathlib import Path

OUT = Path("/home/byh/B02/analysis/p0_pressure_realism/m3_6_communication_evidence")
data = json.loads((OUT / "raw_recount.json").read_text())
native = data["native"]
adapter = data["adapter"]
gw = data["gateway_cell_351"]["results_gateway"]
duration = native["duration_s"]
codec = data["codec"]["packed_len"]
accounting = data["codec"]["WIRE_BYTES"]

native_bytes = native["msgpack_payload_bytes"]
zmq_bytes = native["zmq_body_bytes"]
adapter_bytes = adapter["records"] * codec
kv_bytes = gw["forwarded"] * codec
adapter_104 = adapter["records"] * accounting
kv_104 = gw["forwarded"] * accounting

def mean_Bps(nbytes: int) -> float:
    return nbytes / duration

def kbps(nbytes: int) -> float:
    return mean_Bps(nbytes) * 8 / 1e3

cost_rows = [
    {
        "path": "A_native_msgpack",
        "evidence": "MEASURED_NATIVE",
        "count": native["messages"],
        "events": native["events"],
        "hash_appearances": native["hash_appearances"],
        "bytes": native_bytes,
        "byte_basis": "msgpack_payload",
        "duration_s": f"{duration:.9f}",
        "mean_Bps": f"{mean_Bps(native_bytes):.6f}",
        "mean_kbps": f"{kbps(native_bytes):.6f}",
        "saved_bytes_from_native": 0,
        "reduction_from_native_pct": "0",
        "saved_bytes_from_adapter": "",
        "reduction_from_adapter_pct": "",
        "notes": "212 ZMQ messages, seq 0-211, gaps 0. TCP wire bytes NOT_MEASURED.",
    },
    {
        "path": "A_native_zmq_body",
        "evidence": "MEASURED_NATIVE",
        "count": native["messages"],
        "events": native["events"],
        "hash_appearances": native["hash_appearances"],
        "bytes": zmq_bytes,
        "byte_basis": "topic_plus_seq_plus_msgpack",
        "duration_s": f"{duration:.9f}",
        "mean_Bps": f"{mean_Bps(zmq_bytes):.6f}",
        "mean_kbps": f"{kbps(zmq_bytes):.6f}",
        "saved_bytes_from_native": "",
        "reduction_from_native_pct": "",
        "saved_bytes_from_adapter": "",
        "reduction_from_adapter_pct": "",
        "notes": "Empty topic and 8-byte seq. Framing overhead 1696 B. Capture-file header is not included.",
    },
    {
        "path": "A_native_tcp",
        "evidence": "NOT_MEASURED",
        "count": "",
        "events": "",
        "hash_appearances": "",
        "bytes": "",
        "byte_basis": "tcp_wire",
        "duration_s": "",
        "mean_Bps": "",
        "mean_kbps": "",
        "saved_bytes_from_native": "",
        "reduction_from_native_pct": "",
        "saved_bytes_from_adapter": "",
        "reduction_from_adapter_pct": "",
        "notes": "No packet capture in the C1 raw directory.",
    },
    {
        "path": "B_adapter_codec64",
        "evidence": "TRACE_DERIVED",
        "count": adapter["records"],
        "events": "",
        "hash_appearances": "",
        "bytes": adapter_bytes,
        "byte_basis": "codec_64B",
        "duration_s": f"{duration:.9f}",
        "mean_Bps": f"{mean_Bps(adapter_bytes):.6f}",
        "mean_kbps": f"{kbps(adapter_bytes):.6f}",
        "saved_bytes_from_native": native_bytes - adapter_bytes,
        "reduction_from_native_pct": f"{100 * (native_bytes - adapter_bytes) / native_bytes:.6f}",
        "saved_bytes_from_adapter": 0,
        "reduction_from_adapter_pct": "0",
        "notes": "243 UPSERT. Bytes are count times the 64 B codec, not a captured compact pcap.",
    },
    {
        "path": "B_adapter_accounting104",
        "evidence": "TRACE_DERIVED",
        "count": adapter["records"],
        "events": "",
        "hash_appearances": "",
        "bytes": adapter_104,
        "byte_basis": "accounting_104B_not_codec",
        "duration_s": f"{duration:.9f}",
        "mean_Bps": f"{mean_Bps(adapter_104):.6f}",
        "mean_kbps": f"{kbps(adapter_104):.6f}",
        "saved_bytes_from_native": "",
        "reduction_from_native_pct": "",
        "saved_bytes_from_adapter": "",
        "reduction_from_adapter_pct": "",
        "notes": "Historical WIRE_BYTES boundary. Excluded from scale ratios and from the 10 Mbps drain.",
    },
    {
        "path": "C_kvselect_codec64",
        "evidence": "ACTUAL_GATEWAY_REPLAY",
        "count": gw["forwarded"],
        "events": "",
        "hash_appearances": "",
        "bytes": kv_bytes,
        "byte_basis": "codec_64B_times_forwarded_count",
        "duration_s": f"{duration:.9f}",
        "mean_Bps": f"{mean_Bps(kv_bytes):.6f}",
        "mean_kbps": f"{kbps(kv_bytes):.6f}",
        "saved_bytes_from_native": native_bytes - kv_bytes,
        "reduction_from_native_pct": f"{100 * (native_bytes - kv_bytes) / native_bytes:.6f}",
        "saved_bytes_from_adapter": adapter_bytes - kv_bytes,
        "reduction_from_adapter_pct": f"{100 * (adapter_bytes - kv_bytes) / adapter_bytes:.6f}",
        "notes": "Forwarded 205, superseded 38, replica_cap 0, global_topk 0. Mean is attributed to the native window. Egress timestamps NOT_MEASURED.",
    },
    {
        "path": "C_kvselect_accounting104",
        "evidence": "ACTUAL_GATEWAY_REPLAY",
        "count": gw["forwarded"],
        "events": "",
        "hash_appearances": "",
        "bytes": kv_104,
        "byte_basis": "accounting_104B_not_codec",
        "duration_s": f"{duration:.9f}",
        "mean_Bps": f"{mean_Bps(kv_104):.6f}",
        "mean_kbps": f"{kbps(kv_104):.6f}",
        "saved_bytes_from_native": "",
        "reduction_from_native_pct": "",
        "saved_bytes_from_adapter": "",
        "reduction_from_adapter_pct": "",
        "notes": "205 times 104. Excluded from scale ratios and from the 10 Mbps drain.",
    },
]
with (OUT / "communication_cost.csv").open("w", newline="") as handle:
    writer = csv.DictWriter(handle, fieldnames=list(cost_rows[0]))
    writer.writeheader()
    writer.writerows(cost_rows)

windows = [("1ms", "0.001"), ("10ms", "0.01"), ("100ms", "0.1"), ("1s", "1")]
profiles = [
    ("A_native_msgpack", "MEASURED_NATIVE", "subscriber_recv_ns", native["payload_windows"], native_bytes),
    ("A_native_zmq_body", "MEASURED_NATIVE", "subscriber_recv_ns", native["zmq_windows"], zmq_bytes),
    ("B_adapter_codec64", "TRACE_DERIVED", "source_event_recv_ns_copied_onto_the_update", adapter["windows"], adapter_bytes),
]
burst_rows = []
for name, evidence, semantics, profile, total in profiles:
    for label, key in windows:
        item = profile[key]
        burst_rows.append({
            "path": name,
            "evidence": evidence,
            "window": label,
            "timestamp_semantics": semantics,
            "peak_bytes": item["peak_bytes"],
            "peak_equivalent_kbps": f"{item['peak_bytes'] * 8 / float(key) / 1e3:.6f}",
            "p95_event_aligned_bytes": f"{item['p95_event_aligned_bytes']:.4f}",
            "p95_uniform_grid_bytes": f"{item['p95_uniform_grid_bytes']:.4f}",
            "uniform_empty_fraction": f"{item['uniform_empty_fraction']:.6f}",
            "mean_Bps": f"{mean_Bps(total):.6f}",
            "notes": "peak_equivalent_kbps treats the peak window as if it were sustained. p95_event_aligned repeats a shared timestamp once per record.",
        })
for label, _key in windows:
    burst_rows.append({
        "path": "C_kvselect_codec64",
        "evidence": "NOT_MEASURED",
        "window": label,
        "timestamp_semantics": "gateway_egress",
        "peak_bytes": "NOT_MEASURED",
        "peak_equivalent_kbps": "NOT_MEASURED",
        "p95_event_aligned_bytes": "NOT_MEASURED",
        "p95_uniform_grid_bytes": "NOT_MEASURED",
        "uniform_empty_fraction": "NOT_MEASURED",
        "mean_Bps": f"{mean_Bps(kv_bytes):.6f}",
        "notes": "gateway.log ticks have no timestamps. Adapter recv_ns is not used as egress time. Mean is the forwarded-byte total divided by the native window.",
    })
with (OUT / "communication_burst_profile.csv").open("w", newline="") as handle:
    writer = csv.DictWriter(handle, fieldnames=list(burst_rows[0]))
    writer.writeheader()
    writer.writerows(burst_rows)

peaks = {
    "A_native_msgpack": {"1s": native["payload_windows"]["1"]["peak_bytes"], "1ms": native["payload_windows"]["0.001"]["peak_bytes"]},
    "B_adapter_codec64": {"1s": adapter["windows"]["1"]["peak_bytes"], "1ms": adapter["windows"]["0.001"]["peak_bytes"]},
    "C_kvselect_codec64": {"1s": None, "1ms": None},
}
totals = {
    "A_native_msgpack": native_bytes,
    "B_adapter_codec64": adapter_bytes,
    "C_kvselect_codec64": kv_bytes,
}
evidence_of = {
    "A_native_msgpack": "EXTRAPOLATED",
    "B_adapter_codec64": "EXTRAPOLATED",
    "C_kvselect_codec64": "EXTRAPOLATED",
}
scale_rows = []
for reporters, gateways in ((256, 4), (512, 8), (1024, 16), (4096, 64)):
    for path, total in totals.items():
        single_mean_mbps = total * 8 / duration / 1e6
        specs = [("INDEPENDENT_PHASE", "mean", single_mean_mbps)]
        for metric, peak_bytes in peaks[path].items():
            if peak_bytes is None:
                specs.append(("SYNCHRONIZED_EXTRAPOLATION", f"peak_{metric}", None))
            else:
                window_s = 1.0 if metric == "1s" else 0.001
                specs.append(("SYNCHRONIZED_EXTRAPOLATION", f"peak_{metric}", peak_bytes * 8 / window_s / 1e6))
        specs.append(("SYNCHRONIZED_EXTRAPOLATION", "mean", single_mean_mbps))
        for phase, metric, single_mbps in specs:
            for capacity in (10, 100, 1000):
                if single_mbps is None:
                    offered = ""
                    ratio = "NOT_MEASURED"
                    per_gw = ""
                else:
                    offered_v = reporters * single_mbps
                    offered = f"{offered_v:.6f}"
                    ratio = f"{offered_v / capacity:.6f}"
                    per_gw = f"{(reporters / gateways) * single_mbps:.6f}"
                scale_rows.append({
                    "reporters": reporters,
                    "gateways": gateways,
                    "reporters_per_gateway": reporters // gateways,
                    "phase": phase,
                    "path": path,
                    "metric": metric,
                    "evidence": evidence_of[path] if single_mbps is not None else "NOT_MEASURED",
                    "capacity_tag": "HYPOTHETICAL_CAPACITY",
                    "single_reporter_Mbps": "" if single_mbps is None else f"{single_mbps:.6f}",
                    "aggregate_offered_Mbps": offered,
                    "per_gateway_offered_Mbps": per_gw,
                    "assumed_capacity_Mbps": capacity,
                    "offered_over_capacity": ratio,
                    "workload": "P0-C1 single TP=1 engine, repeated as identical reporters",
                    "notes": "64 reporters/gateway is a research setting. Repeating the trace scales bytes only. Prefix identities are not independent workers. TP=2/4/8 is NOT_MEASURED. P0-A1 is not mixed in. Ratio>1 means this capacity assumption is exceeded.",
                })
with (OUT / "model_b_scale_projection.csv").open("w", newline="") as handle:
    writer = csv.DictWriter(handle, fieldnames=list(scale_rows[0]))
    writer.writeheader()
    writer.writerows(scale_rows)

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

fig, axes = plt.subplots(1, 2, figsize=(10.2, 4.2))
labels = ["Native\nmsgpack", "Adapter\n64 B codec", "KVSelect egress\n64 B codec"]
values = [native_bytes, adapter_bytes, kv_bytes]
colors = ["#1f4e79", "#2a9d8f", "#e09f3e"]
bars = axes[0].bar(labels, [v / 1024 for v in values], color=colors, width=0.72)
axes[0].set_ylabel("Payload over 59.885 s (KiB)")
axes[0].set_title("One TP=1 engine")
for bar, value in zip(bars, values):
    axes[0].text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 4, f"{value:,} B", ha="center", va="bottom", fontsize=8)
axes[0].set_ylim(0, native_bytes / 1024 * 1.18)
axes[0].spines["top"].set_visible(False)
axes[0].spines["right"].set_visible(False)

reporters = [256, 512, 1024, 4096]
for path, color, label in (
    ("A_native_msgpack", colors[0], "Native msgpack"),
    ("B_adapter_codec64", colors[1], "Adapter 64 B"),
    ("C_kvselect_codec64", colors[2], "KVSelect egress 64 B"),
):
    series = [n * totals[path] * 8 / duration / 1e6 for n in reporters]
    axes[1].plot(reporters, series, marker="o", color=color, label=label, linewidth=1.8)
for cap, style in ((10, ":"), (100, "--"), (1000, "-.")):
    axes[1].axhline(cap, color="#666666", linestyle=style, linewidth=0.9)
    axes[1].text(3900, cap * 1.12, f"{cap} Mbit/s hypothetical", fontsize=7, color="#444444", ha="right")
axes[1].set_xscale("log", base=2)
axes[1].set_yscale("log")
axes[1].set_xticks(reporters)
axes[1].set_xticklabels([str(n) for n in reporters])
axes[1].set_xlabel("Reporters (identical copies of one engine)")
axes[1].set_ylabel("Mean offered load (Mbit/s)")
axes[1].set_title("Model B extrapolation")
axes[1].legend(frameon=False, fontsize=8)
axes[1].spines["top"].set_visible(False)
axes[1].spines["right"].set_visible(False)
fig.suptitle("Draft motivation ledger. This is not a replacement for paper Fig. 1.", fontsize=11)
fig.tight_layout()
fig.savefig(OUT / "motivation_byte_cost_draft.png", dpi=160)
fig.savefig(OUT / "motivation_byte_cost_draft.pdf")
print(json.dumps({
    "native_bytes": native_bytes,
    "adapter_bytes": adapter_bytes,
    "kv_bytes": kv_bytes,
    "saved_adapter": native_bytes - adapter_bytes,
    "adapter_pct": 100 * (native_bytes - adapter_bytes) / native_bytes,
    "saved_kv": adapter_bytes - kv_bytes,
    "kv_pct_of_adapter": 100 * (adapter_bytes - kv_bytes) / adapter_bytes,
    "kv_pct_of_native": 100 * (adapter_bytes - kv_bytes) / native_bytes,
    "pipeline_saved": native_bytes - kv_bytes,
    "pipeline_pct": 100 * (native_bytes - kv_bytes) / native_bytes,
    "share_of_pipeline_from_adapter": 100 * (native_bytes - adapter_bytes) / (native_bytes - kv_bytes),
    "native_mean_kbps": kbps(native_bytes),
    "adapter_mean_kbps": kbps(adapter_bytes),
    "kv_mean_kbps": kbps(kv_bytes),
    "native_4096_mbps": 4096 * native_bytes * 8 / duration / 1e6,
    "adapter_4096_mbps": 4096 * adapter_bytes * 8 / duration / 1e6,
    "kv_4096_mbps": 4096 * kv_bytes * 8 / duration / 1e6,
    "adapter_4096_104_mbps": 4096 * adapter_104 * 8 / duration / 1e6,
    "native_256_mbps": 256 * native_bytes * 8 / duration / 1e6,
    "native_512_mbps": 512 * native_bytes * 8 / duration / 1e6,
    "native_1024_mbps": 1024 * native_bytes * 8 / duration / 1e6,
    "per_gw_native_mbps": 64 * native_bytes * 8 / duration / 1e6,
    "sync_1s_256": 256 * 8769 * 8 / 1e6,
    "sync_1s_4096": 4096 * 8769 * 8 / 1e6,
    "sync_1ms_64": 64 * 3772 * 8 / 0.001 / 1e6,
    "sync_1ms_256": 256 * 3772 * 8 / 0.001 / 1e6,
    "rows_scale": len(scale_rows),
}, indent=2))
