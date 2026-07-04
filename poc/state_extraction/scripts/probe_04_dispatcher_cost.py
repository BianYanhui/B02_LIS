#!/usr/bin/env python3
"""
Probe 4: dispatcher-side cost simulation.

Measures serialization / deserialization cost for simulated State View
updates at different frequencies and using different formats. This lets us
size the dispatcher overhead BEFORE we have a real vLLM running.
"""
import argparse
import json
import os
import statistics
import sys
import time
from typing import Any, Dict, List

# --- optional format deps; degrade gracefully if missing ---
try:
    import msgpack
    HAVE_MSGPACK = True
except Exception:
    HAVE_MSGPACK = False

try:
    import orjson
    HAVE_ORJSON = True
except Exception:
    HAVE_ORJSON = False

try:
    import zstandard as zstd
    HAVE_ZSTD = True
except Exception:
    HAVE_ZSTD = False


# --- representative payloads at three State View levels ---
def coarse_payload(instance_id: str, running: int, waiting: int, gpu_mem: float) -> Dict[str, Any]:
    return {
        "instance_id": instance_id,
        "timestamp": time.time(),
        "queue_length": waiting,
        "number_of_running_requests": running,
        "available_gpu_memory_gb": gpu_mem,
        "prefill_queue_length": waiting,
        "decode_queue_length": running,
        "current_prefill_load": float(waiting) * 1.5,
        "current_decode_load": float(running) * 0.8,
    }


def rich_payload(instance_id: str, running: int, waiting: int, gpu_mem: float, n_active_wfs: int = 30, n_steps_per_wf: int = 4) -> Dict[str, Any]:
    base = coarse_payload(instance_id, running, waiting, gpu_mem)
    base.update({
        "active_request_ids": [f"req-{i:08x}" for i in range(running)],
        "request_phases": ["decode"] * running + ["prefill"] * waiting,
        "input_lengths": [256 + (i % 200) for i in range(running)],
        "output_lengths": [12 + (i % 100) for i in range(running)],
        "kv_cache_blocks_used": 1500 + running * 8,
        "kv_cache_blocks_total": 8192,
        "prefix_cache_hit_rate": 0.42,
        "memory_pressure": gpu_mem / 24.0,
        # wrapper-layer data inline
        "active_workflow_count": n_active_wfs,
        "active_workflow_ids": [f"wf-{i:08x}" for i in range(n_active_wfs)],
        "workflows": [
            {
                "id": f"wf-{i:08x}",
                "affinity": ["gpu0", "gpu1", "gpu2", "gpu3"][i % 4],
                "steps_done": min(i % 8, n_steps_per_wf),
                "steps_total": n_steps_per_wf,
                "current_step_id": i % n_steps_per_wf,
                "tools_in_flight": ["web_search"] if i % 3 == 0 else [],
            }
            for i in range(n_active_wfs)
        ],
    })
    return base


def sketch_payload(instance_id: str, running: int, waiting: int, gpu_mem: float) -> Dict[str, Any]:
    return {
        "instance_id": instance_id,
        "timestamp": time.time(),
        "queue_len_8bit": min(255, waiting),
        "running_8bit": min(255, running),
        "gpu_mem_pct_8bit": int(gpu_mem / 24.0 * 255),
        "kv_cache_pct_8bit": 100,
        "wf_count_8bit": min(255, 64),
        "wf_affinity_bitmap": 0b1010,
        "tool_context_bitset": 0b10110,
        "phase_load": {"prefill": min(255, waiting), "decode": min(255, running)},
        "latency_sensitive_flag": 1,
    }


PAYLOAD_BUILDERS = {"coarse": coarse_payload, "rich": rich_payload, "sketch": sketch_payload}


def time_serialize(payload: Any, fmt: str) -> bytes:
    if fmt == "json-utf8":
        return json.dumps(payload).encode("utf-8")
    if fmt == "json-ascii":
        return json.dumps(payload, separators=(",", ":")).encode("ascii")
    if fmt == "orjson":
        if not HAVE_ORJSON:
            raise RuntimeError("orjson not available")
        return orjson.dumps(payload)
    if fmt == "msgpack":
        if not HAVE_MSGPACK:
            raise RuntimeError("msgpack not available")
        return msgpack.packb(payload, use_bin_type=True)
    raise ValueError(f"unknown format {fmt}")


def time_deserialize(blob: bytes, fmt: str) -> Any:
    if fmt.startswith("json"):
        return json.loads(blob.decode("utf-8"))
    if fmt == "orjson":
        if not HAVE_ORJSON:
            raise RuntimeError("orjson not available")
        return orjson.loads(blob)
    if fmt == "msgpack":
        if not HAVE_MSGPACK:
            raise RuntimeError("msgpack not available")
        return msgpack.unpackb(blob, raw=False)
    raise ValueError(f"unknown format {fmt}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--view", choices=list(PAYLOAD_BUILDERS.keys()), default="rich")
    ap.add_argument("--freq", type=float, default=10.0, help="updates per second")
    ap.add_argument("--duration", type=float, default=2.0, help="seconds to run")
    ap.add_argument("--formats", nargs="+", default=["json-utf8", "json-ascii", "orjson", "msgpack"])
    ap.add_argument("--dump-json", type=str, default=None)
    args = ap.parse_args()

    print("== Probe 4: dispatcher-side cost simulation ==")
    print(f"   view={args.view}  freq={args.freq}Hz  duration={args.duration}s")
    print(f"   formats={args.formats}")
    print(f"   msgpack={HAVE_MSGPACK}  orjson={HAVE_ORJSON}")
    print()

    builder = PAYLOAD_BUILDERS[args.view]
    n_attempts = max(1, int(args.duration * args.freq))
    period = 1.0 / args.freq

    print(f"{'-'*70}")
    print(f"{'format':<14} {'bytes':>8} {'ser_us':>9} {'deser_us':>10} {'round_us':>10} {'qps_possible':>12}")
    print(f"{'-'*70}")
    results = []
    for fmt in args.formats:
        try:
            sizes = []
            ser_us = []
            des_us = []
            for i in range(n_attempts):
                payload = builder("gpu0", running=12, waiting=5, gpu_mem=8.0)
                t0 = time.perf_counter_ns()
                blob = time_serialize(payload, fmt)
                t1 = time.perf_counter_ns()
                obj = time_deserialize(blob, fmt)
                t2 = time.perf_counter_ns()
                sizes.append(len(blob))
                ser_us.append((t1 - t0) / 1000.0)
                des_us.append((t2 - t1) / 1000.0)
                if period > 0:
                    time.sleep(max(0, period - (t2 - t0) / 1e9))
            mean_size = statistics.mean(sizes)
            mean_ser = statistics.mean(ser_us)
            mean_des = statistics.mean(des_us)
            mean_round = mean_ser + mean_des
            qps_cap = 1e6 / mean_round if mean_round > 0 else float("inf")
            print(f"{fmt:<14} {mean_size:>8.0f} {mean_ser:>9.1f} {mean_des:>10.1f} {mean_round:>10.1f} {qps_cap:>12.1f}")
            results.append({"format": fmt, "avg_size_bytes": mean_size,
                            "avg_serialize_us": mean_ser, "avg_deserialize_us": mean_des,
                            "avg_round_us": mean_round,
                            "qps_possible": qps_cap, "n_samples": n_attempts})
        except Exception as e:
            print(f"{fmt:<14}     --  skipped ({type(e).__name__}: {e})")
            results.append({"format": fmt, "error": f"{type(e).__name__}: {e}"})

    print()
    # compare across views at default format
    print(f"{'-'*70}")
    print("-- payload size at the default format (json-ascii), per view --")
    print(f"{'view':<10} {'bytes':>8}")
    print(f"{'-'*70}")
    fmt = "json-ascii"
    sizes_per_view = {}
    for v, b in PAYLOAD_BUILDERS.items():
        blob = time_serialize(b("gpu0", 12, 5, 8.0), fmt)
        print(f"{v:<10} {len(blob):>8}")
        sizes_per_view[v] = len(blob)

    if args.dump_json:
        with open(args.dump_json, "w") as f:
            json.dump({"results": results, "sizes_per_view": sizes_per_view}, f, indent=2)
        print(f"\n[OK] wrote {args.dump_json}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
