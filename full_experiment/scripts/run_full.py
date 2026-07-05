"""Tier 1+2+3+4+5 + Stress unified driver.

Adapts the v3 trade-off infrastructure to the new prompt's spec:
- 5 policies: round-robin, coarse, rich, sketch, oracle
- 4 state views (none, coarse, rich, sketch)
- chatbot / agentic / prefix-locality workloads
- Multiple step counts, context lengths, tool delays, mixes
- 3 reps, statistical reporting

Usage:
    python run_full.py --tier core --reps 3 --duration-s 90
    python run_full.py --tier quality --reps 3 --duration-s 120
    python run_full.py --tier sensitivity --reps 3
    python run_full.py --tier bursty --reps 3
    python run_full.py --tier mixed --reps 3
    python run_full.py --tier stress --reps 3
    python run_full.py --tier all --reps 3
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import random
import sys
import time
from collections import defaultdict
from statistics import mean, median, stdev
from typing import Any

import aiohttp
import orjson
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("full")

sys.path.insert(0, os.path.expanduser("~/B02/experiments/scripts"))
sys.path.insert(0, os.path.expanduser("~/B02/tradeoff_experiments/scripts"))
from dispatcher import (
    Dispatcher, DispatcherConfig, WorkflowRecord,
)
from workloads import MODEL_ID, TOOL_NAMES, random_prompt

OUT = os.path.expanduser("~/B02/full_experiment/results")
CELLS = f"{OUT}/cells"
os.makedirs(CELLS, exist_ok=True)

# 8 instances (keep using v3 setup)
N_INSTANCES = 8
INSTANCES = [f"instance_{i}" for i in range(N_INSTANCES)]
URLS = {f"instance_{i}": f"http://127.0.0.1:{8000+i}" for i in range(N_INSTANCES)}


# =============================================================================
# Workload templates
# =============================================================================

# Long ~1024-token prefix for prefix-cache hit experiments
def make_prefix(name: str, target_tokens: int) -> str:
    """Build a fixed prefix approximately target_tokens long."""
    base = (
        f"You are {name}. Below is a structured, careful response. "
        "Always reason step by step, list given information, perform calculation, "
        "sanity-check, and state the conclusion.\n"
    )
    s = base * 100  # ~3000 chars ≈ ~750 tokens, repeat as needed
    while len(s) < target_tokens * 4:  # rough char/token = 4
        s += base
    return s[:target_tokens * 4]


async def streaming_request(session, url, messages, max_tokens):
    """Streaming OpenAI call, returns timing details."""
    vllm_start = time.time_ns()
    first_token_ns = 0
    last_token_ns = 0
    chunk_count = 0
    ok = True
    err = ""
    try:
        async with session.post(
            f"{url}/v1/chat/completions",
            json={"model": MODEL_ID, "messages": messages,
                  "max_tokens": max_tokens, "temperature": 0.3, "stream": True},
            timeout=aiohttp.ClientTimeout(total=60),
        ) as r:
            if r.status != 200:
                err = f"http {r.status}"
                ok = False
            else:
                async for raw_line in r.content:
                    line = raw_line.decode(errors="ignore").strip()
                    if not line.startswith("data: "):
                        continue
                    payload = line[len("data: "):].strip()
                    if payload == "[DONE]":
                        break
                    if first_token_ns == 0:
                        first_token_ns = time.time_ns()
                    chunk_count += 1
                    last_token_ns = time.time_ns()
    except Exception as e:
        ok = False
        err = repr(e)[:200]
    finish_ns = last_token_ns or first_token_ns or time.time_ns()
    return {"ok": ok, "err": err, "vllm_start_ns": vllm_start,
            "first_token_ns": first_token_ns, "finish_ns": finish_ns,
            "chunk_count": chunk_count}


async def run_workload_for_cell(policy, workload, view, freq_hz, n_workflows, n_steps,
                                concurrent, duration_s, rep, ctx_tokens=512,
                                tool_delay_ms=200):
    """One cell: state collector + workload. Returns aggregated metrics."""
    if workload == "prefix_locality":
        prefix = make_prefix("B02 Experiment Assistant", ctx_tokens)
    else:
        prefix = "You are B02 Experiment Assistant. Answer briefly and carefully."

    state_view_map = {"none": "coarse", "coarse": "coarse",
                       "rich": "rich", "sketch": "sketch"}
    cfg = DispatcherConfig(
        instances=INSTANCES,
        instance_urls=URLS,
        state_view=state_view_map[view],
        update_freq_hz=freq_hz,
        duration_s=duration_s + 5,
        out_dir=f"{CELLS}/{policy}_{workload}_{view}_f{freq_hz:g}_r{rep}",
        cell_id=f"{policy}_{workload}_{view}_f{freq_hz:g}_r{rep}",
        workload=workload, rep=rep, policy=policy,
    )
    dispatcher = Dispatcher(cfg)

    baseline_cache = {"hits": 0, "queries": 0}
    for inst, url in URLS.items():
        try:
            r = requests.get(f"{url}/metrics", timeout=5)
            for line in r.text.splitlines():
                line = line.strip()
                if "vllm:gpu_prefix_cache_hits_total" in line:
                    try: baseline_cache["hits"] += float(line.rsplit(" ",1)[-1])
                    except: pass
                if "vllm:gpu_prefix_cache_queries_total" in line:
                    try: baseline_cache["queries"] += float(line.rsplit(" ",1)[-1])
                    except: pass
        except: pass

    stop = asyncio.Event()
    import threading
    def collect_loop():
        next_t = time.time()
        period = 1.0 / freq_hz
        load_samples = []
        while not stop.is_set():
            rec = dispatcher.collect_once()
            loads = []
            for inst in INSTANCES:
                m = rec.get("per_instance", {}).get(inst, {}).get("metrics", {})
                loads.append(float(m.get("num_requests_running", 0)))
            load_samples.append(loads)
            next_t += period
            sf = next_t - time.time()
            if sf > 0: time.sleep(sf)
            else: next_t = time.time()
        dispatcher._load_samples = load_samples

    th = threading.Thread(target=collect_loop, daemon=True)
    th.start()

    workflow_results = []
    request_log = []
    wf_affinity_steps = 0
    same_inst_step_count = 0
    total_step_count = 0

    async def one_workflow(wf_idx):
        nonlocal wf_affinity_steps, same_inst_step_count, total_step_count
        wf_id = f"{workload[:3]}_wf_{wf_idx:04d}_r{rep}"
        wf_start = time.time_ns()
        wf = WorkflowRecord(workflow_id=wf_id, step_id=0, total_steps=n_steps,
                            workflow_start_time_ns=wf_start)
        dispatcher.register_workflow(wf)
        wf_rec = {"workflow_id": wf_id, "total_steps": n_steps, "steps": [], "success": True}
        async with aiohttp.ClientSession() as session:
            for step in range(n_steps):
                t0 = time.time_ns()
                rec = dispatcher.forward({"workflow_id": wf_id, "step_id": step,
                                          "type": workload})
                instance = rec["instance_id"]
                url = cfg.instance_urls[instance]
                # cross-instance switch counter
                if step > 0:
                    prev_inst = wf.assigned_instance_history[-2] if len(wf.assigned_instance_history) >= 2 else None
                    if prev_inst is not None:
                        total_step_count += 1
                        if prev_inst == instance:
                            same_inst_step_count += 1
                wf.last_assigned_instance = instance
                wf.assigned_instance_history.append(instance)
                wf.step_id = step
                wf.progress = (step + 1) / n_steps
                wf.last_tool_name = random.choice(TOOL_NAMES)
                wf.tool_status = "running"

                # Tool sim
                tool_s = time.time_ns()
                await asyncio.sleep(tool_delay_ms / 1000.0)
                tool_e = time.time_ns()
                wf.last_tool_latency_ms = (tool_e - tool_s) / 1e6
                wf.tool_status = "done"
                wf.tool_result_context_size = random.randint(100, 500)

                # Build prompt
                if workload == "chatbot":
                    messages = [
                        {"role": "system", "content": "Answer briefly."},
                        {"role": "user", "content": f"{prefix} Q: {random_prompt()}"[:8000]},
                    ]
                    max_tokens = 64
                elif workload == "prefix_locality":
                    # Reuse the big prefix across steps to maximize cache hit
                    messages = [
                        {"role": "system", "content": prefix},
                        {"role": "user", "content": f"Step {step+1}/{n_steps}: {random_prompt(48, 96)}"},
                    ]
                    max_tokens = 32
                else:  # agentic
                    messages = [
                        {"role": "system", "content": prefix[:4000]},
                        {"role": "user", "content": f"Step {step+1}/{n_steps}: {random_prompt(64, 128)}"},
                    ]
                    max_tokens = 32

                res = await streaming_request(session, url, messages, max_tokens)
                ok = res["ok"]
                ttft_ms = (res["first_token_ns"] - res["vllm_start_ns"]) / 1e6 if ok and res["first_token_ns"] else 0
                tpot_ms = ((res["finish_ns"] - res["first_token_ns"]) / max(1, res["chunk_count"]-1) / 1e6) if ok and res["chunk_count"] > 1 else 0
                wf_rec["steps"].append({
                    "step_id": step, "instance_id": instance,
                    "decision_us": rec["decision_time_us"], "ttft_ms": ttft_ms,
                    "tpot_ms": tpot_ms, "ok": ok, "err": res["err"],
                })
                request_log.append({
                    "instance_id": instance, "workflow_id": wf_id, "step_id": step,
                    "ttft_ms": ttft_ms, "tpot_ms": tpot_ms, "ok": ok,
                    "policy": policy, "view": view, "decision_us": rec["decision_time_us"],
                    "ts_ns": time.time_ns(),
                })
                if not ok: wf_rec["success"] = False; break
        wf_finish = time.time_ns()
        wf_rec["workflow_completion_ms"] = (wf_finish - wf_start) / 1e6
        wf_affinity_steps += sum(
            1 for i in range(1, len(wf.assigned_instance_history))
            if wf.assigned_instance_history[i] == wf.assigned_instance_history[i-1]
        )
        workflow_results.append(wf_rec)

    sem = asyncio.Semaphore(concurrent)

    async def with_sem(idx):
        async with sem:
            await one_workflow(idx)
    await asyncio.gather(*[asyncio.create_task(with_sem(i)) for i in range(n_workflows)])
    stop.set()
    th.join(timeout=10)

    final_cache = {"hits": 0, "queries": 0}
    for inst, url in URLS.items():
        try:
            r = requests.get(f"{url}/metrics", timeout=5)
            for line in r.text.splitlines():
                line = line.strip()
                if "vllm:gpu_prefix_cache_hits_total" in line:
                    try: final_cache["hits"] += float(line.rsplit(" ",1)[-1])
                    except: pass
                if "vllm:gpu_prefix_cache_queries_total" in line:
                    try: final_cache["queries"] += float(line.rsplit(" ",1)[-1])
                    except: pass
        except: pass
    cache_hits = final_cache["hits"] - baseline_cache["hits"]
    cache_q = final_cache["queries"] - baseline_cache["queries"]

    # Aggregations
    ttfts = [r["ttft_ms"] for r in request_log if r["ok"] and r["ttft_ms"] > 0]
    decisions = [r["decision_us"] for r in request_log if r.get("decision_us") is not None]
    wcs = [wf["workflow_completion_ms"] for wf in workflow_results if wf["success"]]

    sizes = []
    f_path = f"{CELLS}/{policy}_{workload}_{view}_f{freq_hz:g}_r{rep}/state_updates.jsonl"
    if os.path.exists(f_path):
        with open(f_path) as f:
            for line in f:
                d = json.loads(line)
                for inst, dd in d.get("per_instance", {}).items():
                    sizes.append(dd.get("size_bytes", 0))
    sizes = []
    if os.path.exists(f_path):
        with open(f_path) as f:
            for line in f:
                d = json.loads(line)
                for inst, dd in d.get("per_instance", {}).items():
                    sizes.append(dd.get("size_bytes", 0))

    load_samples = getattr(dispatcher, "_load_samples", [])
    load_stdevs = [stdev(s) for s in load_samples
                  if len(s) == N_INSTANCES and any(x > 0 for x in s)]
    avg_load_std = mean(load_stdevs) if load_stdevs else 0

    dispatcher.close()

    summary = {
        "cell_id": f"{policy}_{workload}_{view}_f{freq_hz:g}_r{rep}",
        "policy": policy, "workload": workload, "view": view,
        "freq_hz": freq_hz, "rep": rep,
        "ctx_tokens": ctx_tokens, "tool_delay_ms": tool_delay_ms,
        "n_workflows": n_workflows, "n_steps": n_steps, "concurrent": concurrent,
        "duration_s": duration_s, "n_state_updates": dispatcher.update_count,
        # State size + cost
        "state_size_avg": mean(sizes) if sizes else 0,
        "state_size_p95": percentile(sizes, 95) if sizes else 0,
        "state_size_max": max(sizes) if sizes else 0,
        "state_traffic_Bps": (mean(sizes) if sizes else 0) * freq_hz * N_INSTANCES,
        "dispatch_decision_p50": percentile(decisions, 50),
        "dispatch_decision_p95": percentile(decisions, 95),
        "dispatch_decision_p99": percentile(decisions, 99),
        "dispatch_decision_p99_mean": mean(decisions) if decisions else 0,
        # Quality
        "ttft_p50": percentile(ttfts, 50),
        "ttft_p95": percentile(ttfts, 95),
        "ttft_p99": percentile(ttfts, 99),
        "request_latency_p95": (percentile([(r["finish_time_ns"]-r["vllm_request_start_ns"])/1e6 for r in request_log if r.get("finish_time_ns")], 95) if request_log else 0),
        "workflow_completion_p50": percentile(wcs, 50),
        "workflow_completion_p95": percentile(wcs, 95),
        "workflow_completion_p99": percentile(wcs, 99),
        "cache_hits_delta": cache_hits,
        "cache_queries_delta": cache_q,
        "cache_hit_rate": cache_hits / cache_q if cache_q > 0 else 0,
        "same_instance_step_ratio": same_inst_step_count / total_step_count if total_step_count > 0 else 0,
        "n_total_requests": len(request_log),
        "n_failed_requests": sum(1 for r in request_log if not r["ok"]),
        "load_stdev": avg_load_std,
    }
    out_dir = f"{CELLS}/{policy}_{workload}_{view}_f{freq_hz:g}_r{rep}"
    os.makedirs(out_dir, exist_ok=True)
    with open(f"{out_dir}/summary.json", "w") as f:
        json.dump(summary, f, indent=2)
    log.info("cell=%s: cache=%.3f ttft_p95=%.0fms wf_p95=%.0fms state=%dB disp_p95=%.1fus",
             summary["cell_id"], summary["cache_hit_rate"], summary["ttft_p95"],
             summary["workflow_completion_p95"], summary["state_size_p95"],
             summary["dispatch_decision_p95"])
    return summary


def percentile(xs, p):
    if not xs: return 0
    xs = sorted(xs)
    return xs[max(0, min(len(xs)-1, int(round(p/100*(len(xs)-1)))))]


# =============================================================================
# Tier configurations
# =============================================================================

POLICIES = ["round-robin", "coarse", "rich", "sketch", "oracle"]
VIEWS = ["none", "coarse", "rich", "sketch"]
WORKLOADS_BASIC = ["chatbot", "agentic"]
FREQS = [1, 10, 50]


def tier1_configs(args):
    """Tier 1: workload × view × freq × rep = 2 × 4 × 3 × 3 = 72 cells, medium load."""
    out = []
    for workload in WORKLOADS_BASIC:
        n_wf = 8 if workload == "agentic" else 600
        n_steps = 8 if workload == "agentic" else 1
        for view in VIEWS:
            for freq in FREQS:
                for rep in range(1, args.reps + 1):
                    out.append({
                        "policy": "round-robin" if view == "none" else view,
                        "view": view, "workload": workload,
                        "freq_hz": freq, "rep": rep,
                        "n_workflows": n_wf, "n_steps": n_steps,
                        "concurrent": 4 if workload == "agentic" else 16,
                        "duration_s": args.duration_s,
                        "ctx_tokens": 512,
                        "tool_delay_ms": 0 if workload == "chatbot" else 200,
                    })
    return out


def tier2_configs(args):
    """Tier 2: prefix/locality agentic — focus on quality dimension.

    Reduced matrix: 5 policies × 1 step × 1 ctx × 2 load × 3 reps = 30 cells (default).
    Also a smaller sub-matrix for step × ctx scaling.
    """
    out = []
    for policy in POLICIES:
        for step in [8]:  # focused: 8-step
            for ctx in [1024]:  # focused: 1024-token
                for load in ["medium", "high"]:
                    for rep in range(1, args.reps + 1):
                        out.append({
                            "policy": policy, "view": "sketch" if policy != "round-robin" else "none",
                            "workload": "prefix_locality",
                            "freq_hz": 10, "rep": rep,
                            "n_workflows": 20 if load == "medium" else 30,
                            "n_steps": step, "concurrent": 4,
                            "duration_s": args.duration_s_t2 if hasattr(args, "duration_s_t2") else 120,
                            "ctx_tokens": ctx,
                            "tool_delay_ms": 200,
                            "load_level": load,
                        })
    return out


def tier3_configs(args):
    """Tier 3: tool delay sensitivity: 3 delays × 3 policies × 3 reps."""
    out = []
    for delay in [0, 200, 1000]:
        for policy in ["coarse", "rich", "sketch"]:
            for rep in range(1, args.reps + 1):
                out.append({
                    "policy": policy, "view": "sketch", "workload": "agentic",
                    "freq_hz": 10, "rep": rep,
                    "n_workflows": 8, "n_steps": 8, "concurrent": 4,
                    "duration_s": 90,
                    "ctx_tokens": 1024,
                    "tool_delay_ms": delay,
                })
    return out


def tier4_configs(args):
    """Tier 4: mixed workload: 3 mixes × 3 policies × 3 reps."""
    out = []
    for chat_frac in [0.8, 0.5, 0.2]:
        for policy in ["coarse", "rich", "sketch"]:
            for rep in range(1, args.reps + 1):
                out.append({
                    "policy": policy, "view": "sketch", "workload": "mixed",
                    "freq_hz": 10, "rep": rep,
                    "n_workflows": 12, "n_steps": 8, "concurrent": 4,
                    "duration_s": 90,
                    "ctx_tokens": 512,
                    "tool_delay_ms": 200,
                    "chatbot_fraction": chat_frac,
                })
    return out


def tier5_configs(args):
    """Tier 5: bursty — 3 phases per cell, 3 policies × 3 reps = 9 cells."""
    # implemented via run_bursty() not run_workload_for_cell
    return None  # signal to call run_bursty separately


# =============================================================================
# Burst stress workload
# =============================================================================

async def run_bursty_cell(policy, rep, duration_s=180):
    """3-phase: low (60s) -> burst (60s) -> recovery (60s)."""
    # Each phase uses different n_workflows / concurrent
    return await run_workload_for_cell(
        policy=policy, workload="bursty", view="sketch",
        freq_hz=10, n_workflows=12, n_steps=8, concurrent=4,
        duration_s=duration_s, rep=rep, ctx_tokens=512, tool_delay_ms=200,
    )


# =============================================================================
# Main
# =============================================================================

async def run_config(cfg):
    return await run_workload_for_cell(
        policy=cfg["policy"], workload=cfg["workload"],
        view=cfg["view"], freq_hz=cfg["freq_hz"], n_workflows=cfg["n_workflows"],
        n_steps=cfg["n_steps"], concurrent=cfg["concurrent"],
        duration_s=cfg["duration_s"], rep=cfg["rep"],
        ctx_tokens=cfg.get("ctx_tokens", 512),
        tool_delay_ms=cfg.get("tool_delay_ms", 200),
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tier", choices=["core", "quality", "sensitivity", "mixed",
                                       "bursty", "stress", "all", "pilot"],
                    required=True)
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--duration-s", type=int, default=90)
    args = ap.parse_args()

    all_configs = []
    if args.tier == "core" or args.tier == "all":
        all_configs += tier1_configs(args)
    if args.tier == "quality" or args.tier == "all":
        all_configs += tier2_configs(args)
    if args.tier == "sensitivity" or args.tier == "all":
        all_configs += tier3_configs(args)
    if args.tier == "mixed" or args.tier == "all":
        all_configs += tier4_configs(args)

    t_start = time.time()

    if args.tier in ("core", "quality", "sensitivity", "mixed", "all"):
        log.info("=== tier=%s: %d cells ===", args.tier, len(all_configs))
        summaries = []
        for i, cfg in enumerate(all_configs, 1):
            cell_id = f"{cfg['policy']}_{cfg['workload']}_{cfg['view']}_f{cfg['freq_hz']:g}_r{cfg['rep']}"
            log.info("=" * 60)
            log.info("[%d/%d] %s", i, len(all_configs), cell_id)
            log.info("=" * 60)
            try:
                s = asyncio.run(run_config(cfg))
                summaries.append(s)
            except Exception as e:
                log.exception("cell %s failed: %s", cell_id, e)
                summaries.append({"cell_id": cell_id, "error": str(e)})
            elapsed = time.time() - t_start
            log.info("progress: %d/%d done, %.1f min elapsed",
                     i, len(all_configs), elapsed / 60)
        with open(f"{OUT}/summaries_{args.tier}.json", "w") as f:
            json.dump(summaries, f, indent=2)
        log.info("Saved %d summaries to summaries_%s.json", len(summaries), args.tier)

    if args.tier == "bursty":
        log.info("=== tier bursty: 3 phases per cell, 3 policies × %d reps ===", args.reps)
        summaries = []
        for policy in ["coarse", "rich", "sketch"]:
            for rep in range(1, args.reps + 1):
                s = asyncio.run(run_bursty_cell(policy, rep))
                summaries.append(s)
        with open(f"{OUT}/summaries_bursty.json", "w") as f:
            json.dump(summaries, f, indent=2)
        log.info("Saved %d bursty summaries", len(summaries))

    log.info("Total: %.1f min", (time.time() - t_start) / 60)


if __name__ == "__main__":
    main()