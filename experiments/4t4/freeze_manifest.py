#!/usr/bin/env python3
"""Create the immutable configuration record for the 4xT4 formal study."""
from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path


MODEL = "/home/byh/.cache/modelscope/qwen/Qwen2.5-1.5B-Instruct"
POLICIES = {
    "FullSync": "FIFO full event synchronization; no semantic suppression.",
    "RateFIFO": "FIFO token bucket at the same physical B_s(t); no KV semantics.",
    "LatestOnly": "Latest unsent (owner,prefix) replacement only.",
    "AgeCov-Greedy": "pending priority = age_s * max(coverage_tokens,1) / 64 bytes.",
    "StaticSemantic": "replacement + invalidation priority + replica cap two; no adaptive admission.",
    "Adaptive": "StaticSemantic + EWMA delay/queue utility admission and dynamic global useful-prefix set.",
    "Ideal": "Immediate state visibility upper bound; not deployable.",
}


def commit() -> str:
    return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--rate-burst-frames", type=int, required=True)
    parser.add_argument("--kv-cache-tokens", type=int, required=True)
    parser.add_argument("--seed", type=int, default=2026072600)
    args = parser.parse_args()
    output = Path(args.output)
    if output.exists():
        raise SystemExit(f"refusing to overwrite frozen manifest: {output}")
    if args.rate_burst_frames not in {1, 4, 16}:
        raise SystemExit("rate-burst-frames must be a calibrated candidate: 1, 4, or 16")
    manifest = {
        "schema": "formal4t4-v1",
        "frozen_at_unix": time.time(),
        "code_commit": commit(),
        "model": MODEL,
        "hardware": {"gpus": ["Tesla T4"] * 4, "instances": 4, "mapping": {str(i): {"gpu": i, "port": 8000 + i} for i in range(4)}},
        "vllm": {"gpu_memory_utilization": 0.40, "max_model_len": 6144, "max_num_seqs": 8,
                 "prefix_caching": True, "prompt_tokens_details": True, "block_size": 16, "enforce_eager": True,
                 "kv_cache_tokens_per_instance": args.kv_cache_tokens,
                 "server_development_validation_endpoints": True},
        "network": {"path": "instance agent -> TCP b02-gateway4t4 -> Docker bridge -> tc HTB/bfifo -> dispatcher",
                    "frame_bytes": 64, "wire_bytes_per_msg": 104, "rho_definition": "offered state load / available signaling service budget",
                    "rhos": [0.5, 0.8, 1.0, 1.2], "budget_rule": "all non-Ideal policies use the same physical HTB parent rate B_s=offered/rho",
                    "ratefifo_burst_frames": args.rate_burst_frames, "relay_max_inflight": 4,
                    "tc": "HTB parent with signaling class 1:10 and background class 1:20; bfifo queues; MTU 296"},
        "policies": POLICIES,
        "workloads": {
            "original_compatible": {"requests": 120, "warmup": 24, "concurrency": 4, "pool_size": 32, "alpha": 1.2,
                                    "lineage_steps": 3, "prefix_coverage": "2048 + 512-token extensions", "replica_overlap": 0.0},
            "reuse_intensive": {"requests": 120, "warmup": 24, "concurrency": 4, "pool_size": 64, "alpha": 1.2,
                                "prefix_distribution_tokens": {"1024": 0.35, "2048": 0.40, "4096": 0.25},
                                "suffix": "distinct task turn per request", "replica_overlap": 0.25},
        },
        "formal": {"repetitions": 5, "paired_seeds": [args.seed + i for i in range(5)],
                   "request_generation": "fixed-seed Zipf, byte-identical logical prompts across policies within rep",
                   "excluded_components": ["Python delay simulation", "offline reuse accounting"]},
        "calibration": {"not_for_paper": True, "ratefifo_candidates": [1, 4, 16],
                        "selection_rule": "choose the candidate with the lowest mean normalized (p95 state age + view-missing) over rho 0.8,1.0,1.2; tie -> medium burst 4"},
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(manifest, indent=2) + "\n")
    print(output)


if __name__ == "__main__":
    main()
