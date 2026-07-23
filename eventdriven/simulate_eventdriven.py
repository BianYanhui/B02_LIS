"""Event-driven vs Periodic event-driven cost comparison.

Simulates two dissemination strategies for affinity state:
  - Periodic: every tick Hz, all N instances send their full state
  - Event-driven: only on Upsert/Tombstone events, instance sends just the delta

Models:
  - N instances
  - Resource churn rate (events/sec/instance)
  - Affinity entries per instance (K)

Output: traffic bytes/sec, dispatcher CPU cost, dispatcher indexing cost.

This is pure Python, no GPU/vllm needed.
"""
from __future__ import annotations
import argparse
import json
import os
import random
import time
from collections import defaultdict
from statistics import mean, stdev


def simulate_periodic(N, K, freq_hz, duration_s, payload_per_entry=50):
    """Periodic: every freq_hz sec, scrape all N instances' state."""
    bytes_per_sec = 0
    bytes_per_cycle = 0
    n_cycles = 0
    dispatch_cpu_per_cycle_us = 200 + N * 0.5  # HTTP overhead + per-instance parse
    cpu_us_total = 0
    # At each cycle, all N instances send all K entries
    bytes_per_cycle = N * K * payload_per_entry
    cycles_per_sec = freq_hz
    for _ in range(int(duration_s * cycles_per_sec)):
        bytes_per_sec += bytes_per_cycle
        n_cycles += 1
        cpu_us_total += dispatch_cpu_per_cycle_us
    return {
        "mode": "periodic",
        "n_instances": N,
        "K_per_instance": K,
        "freq_hz": freq_hz,
        "duration_s": duration_s,
        "n_cycles": n_cycles,
        "total_bytes": bytes_per_sec,
        "avg_bytes_per_sec": bytes_per_sec / duration_s,
        "avg_cpu_per_cycle_us": dispatch_cpu_per_cycle_us,
        "total_cpu_us": cpu_us_total,
        "avg_cpu_per_sec_us": cpu_us_total / duration_s,
    }


def simulate_event_driven(N, K, churn_per_inst_per_sec, duration_s,
                            payload_per_entry=50, event_overhead=20):
    """Event-driven: only on Upsert/Tombstone events.

    Each instance generates `churn_per_inst_per_sec` events/sec.
    Each event is an Upsert (entry created) or Tombstone (entry deleted).
    Each event sends a single delta (one entry).
    """
    bytes_per_sec = 0
    cpu_us_total = 0
    # Per-event cost: serialize delta + index update
    cpu_per_event_us = 50 + 1.0  # log(K) indexing
    # Total events/sec
    total_events_per_sec = N * churn_per_inst_per_sec
    # For each event: send 1 entry (or 2 for tombstone)
    avg_bytes_per_event = payload_per_entry + event_overhead
    for _ in range(int(duration_s)):
        bytes_per_sec += total_events_per_sec * avg_bytes_per_event
        cpu_us_total += total_events_per_sec * cpu_per_event_us
    return {
        "mode": "event-driven",
        "n_instances": N,
        "K_per_instance": K,
        "churn_per_inst_per_sec": churn_per_inst_per_sec,
        "duration_s": duration_s,
        "total_bytes": bytes_per_sec,
        "avg_bytes_per_sec": bytes_per_sec / duration_s,
        "avg_events_per_sec": total_events_per_sec,
        "total_cpu_us": cpu_us_total,
        "avg_cpu_per_sec_us": cpu_us_total / duration_s,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", default="/home/byh/B02/eventdriven")
    ap.add_argument("--duration-s", type=int, default=60)
    ap.add_argument("--N-values", nargs="+", type=int,
                    default=[16, 64, 256, 1024])
    ap.add_argument("--K-values", nargs="+", type=int,
                    default=[4, 16, 64])
    ap.add_argument("--freq-values", nargs="+", type=float,
                    default=[1, 10, 50])
    ap.add_argument("--churn-values", nargs="+", type=float,
                    default=[0.1, 1, 10])
    args = ap.parse_args()

    os.makedirs(args.out_dir, exist_ok=True)
    os.makedirs(f"{args.out_dir}/cells", exist_ok=True)
    results = []

    # Periodic: vary N, freq, K
    print("=== Periodic mode ===")
    for N in args.N_values:
        for K in args.K_values:
            for freq in args.freq_values:
                r = simulate_periodic(N, K, freq, args.duration_s)
                r["cell_id"] = f"periodic_N{N}_K{K}_f{freq:g}"
                results.append(r)
                print(f"  {r['cell_id']}: {r['avg_bytes_per_sec']:.0f} B/s, "
                      f"{r['avg_cpu_per_sec_us']/1000:.1f} ms CPU/s")

    # Event-driven: vary N, K, churn
    print("\n=== Event-driven mode ===")
    for N in args.N_values:
        for K in args.K_values:
            for churn in args.churn_values:
                r = simulate_event_driven(N, K, churn, args.duration_s)
                r["cell_id"] = f"event_N{N}_K{K}_c{churn:g}"
                results.append(r)
                print(f"  {r['cell_id']}: {r['avg_bytes_per_sec']:.0f} B/s, "
                      f"{r['avg_cpu_per_sec_us']/1000:.1f} ms CPU/s, "
                      f"{r['avg_events_per_sec']:.0f} events/s")

    with open(f"{args.out_dir}/all_results.json", "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nSaved {len(results)} results to {args.out_dir}/all_results.json")


if __name__ == "__main__":
    main()