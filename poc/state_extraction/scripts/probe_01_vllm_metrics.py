#!/usr/bin/env python3
"""
Probe 1: scrape vLLM HTTP /metrics endpoint and map Prometheus metrics
back to B02 Motivation Prompt Section 5.3 fields.

Usage (run on yhs1):
    source ~/B02/poc/.venv/bin/activate
    python probe_01_vllm_metrics.py [--port 8000] [--model MODEL]

Requires a running vLLM OpenAI-compatible server, e.g.:
    vllm serve Qwen/Qwen2.5-1.5B-Instruct --port 8000 --gpu-memory-utilization 0.85
"""
import argparse
import json
import os
import sys
import time
import urllib.request
import urllib.error
from typing import Any, Dict, List, Tuple

# Each entry: (B02 field, candidate Prometheus metric name substrings, note)
#  - candidate substrings matched if any vllm metric name contains one of them
#  - empty candidate list + non-empty note = field not exposed here (wrapper concern)
B02_FIELDS: List[Tuple[str, List[str], str]] = [
    # ---- trivial / config-driven ----
    ("instance_id",        [],           "config: --tensor-parallel-size / --pipeline-parallel-size / per-process"),
    ("timestamp",          [],           "client-side time.time()"),

    # ---- coarse state ----
    ("queue_length",                  ["num_requests_waiting", "num_waiting"],   ""),
    ("number_of_running_requests",    ["num_requests_running", "num_running"],   ""),
    ("available_gpu_memory",          ["gpu_memory_free", "gpu_memory_usage"],   "subtract from total"),
    ("prefill_queue_length",          ["num_preemptions", "num_requests_waiting"], "approximate"),
    ("decode_queue_length",           ["num_requests_running"],                  ""),
    ("current_prefill_load",          ["prompt_tokens", "num_prefill_tokens"],   ""),
    ("current_decode_load",           ["generation_tokens", "num_decode_tokens"], ""),

    # ---- rich state additions ----
    ("active_request_ids",            [],           "ENGINE INTERNALS — needs probe_02, not exposed via /metrics"),
    ("request_phase",                 [],           "ENGINE INTERNALS — see probe_02"),
    ("input_length",                  ["prompt_tokens_total", "prompt_tokens"], "aggregate only via /metrics"),
    ("output_length",                 ["generation_tokens_total", "generation_tokens"], ""),
    ("KV-cache block metadata",       ["kv_cache_usage", "kv_cache"],           ("per-block locality NOT exposed; see probe_02"),),
    ("cache hit / reuse",             ["prefix_cache_hits", "prefix_cache_queries", "cache_hit"], ""),
    ("memory pressure",               ["gpu_memory_usage", "kv_cache_usage"],   ""),

    # ---- wrapper-layer concern ----
    ("active_workflow_ids",           [],           "WRAPPER — not vLLM's concern; out-of-scope for vLLM probe"),
    ("workflow_step_id",              [],           "WRAPPER — see probe_03"),
    ("workflow_progress",             [],           "WRAPPER"),
    ("tool_execution_status",         [],           "WRAPPER"),
    ("tool_result_context_metadata",  [],           "WRAPPER"),
    ("workflow-to-instance affinity", [],           "WRAPPER"),

    # ---- latency ----
    ("e2e_request_latency",           ["e2e_request_latency", "request_latency"], ""),
    ("request_success_total",         ["request_success"],                       ""),
]

def fetch_metrics(base: str, timeout: float = 5.0) -> str:
    req = urllib.request.Request(f"{base}/metrics")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8")

def parse_prometheus(text: str) -> List[Dict[str, Any]]:
    """Crude Prometheus text-format parser. Skips HELP / TYPE lines."""
    out = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            if "{" in line:
                name_part, rest = line.split("{", 1)
                labels_part, value_part = rest.split("}", 1)
                value = value_part.strip().split()[0]
                labels = {}
                if labels_part.strip():
                    for kv in labels_part.split(","):
                        if "=" in kv:
                            k, v = kv.split("=", 1)
                            labels[k.strip()] = v.strip().strip('"')
            else:
                parts = line.split()
                if len(parts) < 2:
                    continue
                name_part, value = parts[0], parts[1]
                labels = {}
            out.append({"name": name_part.strip(), "labels": labels, "value": value})
        except Exception:
            continue
    return out

def server_alive(base: str) -> bool:
    try:
        with urllib.request.urlopen(f"{base}/v1/models", timeout=3) as r:
            return r.status == 200
    except Exception:
        return False

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(os.environ.get("VLLM_PORT", 8000)))
    ap.add_argument("--model", type=str, default=os.environ.get("VLLM_MODEL", "Qwen/Qwen2.5-1.5B-Instruct"))
    ap.add_argument("--dump-json", type=str, default=None, help="write full findings to this JSON file")
    args = ap.parse_args()

    base = f"http://localhost:{args.port}"
    print(f"== Probe 1: vLLM /metrics scrape ==")
    print(f"   target:   {base}/metrics")
    print(f"   model:    {args.model}")
    print()

    if not server_alive(base):
        print(f"[FAIL] no vLLM server at {base}")
        print(f"       start one with:")
        print(f"           vllm serve {args.model} --port {args.port} --gpu-memory-utilization 0.85")
        return 1

    raw = fetch_metrics(base)
    parsed = parse_prometheus(raw)
    names = sorted({s["name"] for s in parsed if s["name"].startswith("vllm:")})
    print(f"[OK] /metrics returned {len(parsed)} samples; {len(names)} unique vllm:* names")
    print()

    # Print all vllm: metrics for reference
    print("-- all vllm: metrics exposed --")
    for n in names:
        print(f"   {n}")
    print()

    # Map B02 fields
    findings = []
    print("-- B02 §5.3 field coverage from /metrics --")
    hdr = f"{'field':<35} {'verdict':<7} {'note'}"
    print(hdr)
    print("-" * 78)
    for field, candidates, note in B02_FIELDS:
        if not candidates:
            verdict = "N/A"
            matched = ""
        else:
            hit = [n for n in names if any(c in n for c in candidates)]
            if hit:
                verdict = "OK"
                matched = ", ".join(hit[:3])
            else:
                verdict = "MISS"
                matched = "(no match for " + ", ".join(candidates[:2]) + ")"
        print(f"{field:<35} {verdict:<7} {matched}")
        findings.append({"field": field, "verdict": verdict, "matched": matched, "note": note})

    if args.dump_json:
        out = {
            "port": args.port,
            "model": args.model,
            "exposed_vllm_metric_count": len(names),
            "all_vllm_metrics": names,
            "field_findings": findings,
        }
        with open(args.dump_json, "w") as f:
            json.dump(out, f, indent=2)
        print(f"\n[OK] findings written to {args.dump_json}")

    return 0

if __name__ == "__main__":
    sys.exit(main())
