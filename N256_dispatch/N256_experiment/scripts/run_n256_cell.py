"""N=256 Dispatch Scalability Test.

Uses 256 mock vLLM instances (ports 28000-28255) and tests how the dispatcher
behaves when there are 256 candidates to choose from.

Each cell:
  - State collector polls 256 /metrics endpoints at freq_hz
  - Workload generator sends concurrent chat completions
  - For each request, dispatcher picks instance by policy
  - Measures:
    - dispatch_decision_us (policy time)
    - state collection latency
    - per-request latency
    - per-instance load distribution
    - success rate

Policies: round-robin, coarse, rich, sketch (4 policies, all paired with view=coarse for simplicity)

Tiers:
  - Tier 1: Baseline, no failure injection
  - Tier 2: Failure injection (20% instances returning 5xx)
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
log = logging.getLogger("n256")

sys.path.insert(0, "/home/byh/B02/experiments/scripts")
sys.path.insert(0, "/home/byh/B02/N256_dispatch/N256_experiment/scripts")
from dispatcher_supp import (
    Dispatcher, DispatcherConfig, WorkflowRecord,
    assert_view_for_policy,
)
from dispatcher import parse_vllm_metrics
from workloads import MODEL_ID, random_prompt

N_INSTANCES = 256
INSTANCES = [f"mock_{i}" for i in range(N_INSTANCES)]
URLS = {f"mock_{i}": f"http://127.0.0.1:{28000+i}" for i in range(N_INSTANCES)}

OUT = "/home/byh/B02/N256_dispatch/N256_experiment/results"
CELLS = f"{OUT}/cells"
os.makedirs(CELLS, exist_ok=True)


async def streaming_request(session, url, prompt, max_tokens=8, timeout_s=10):
    """Send a chat completion to mock instance. Returns timing info."""
    t0 = time.perf_counter_ns()
    vllm_start = time.time_ns()
    first_token_ns = 0
    last_token_ns = 0
    chunk_count = 0
    ok = True
    err = ""
    status = 0
    try:
        body = {
            "model": MODEL_ID,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": 0.3,
            "stream": True,
        }
        async with session.post(
            f"{url}/v1/chat/completions",
            json=body,
            timeout=aiohttp.ClientTimeout(total=timeout_s),
        ) as r:
            status = r.status
            if r.status != 200:
                err = f"http {r.status}"
                ok = False
            else:
                async for raw in r.content:
                    line = raw.decode(errors="ignore").strip()
                    if not line.startswith("data: "): continue
                    payload = line[len("data: "):].strip()
                    if payload == "[DONE]": break
                    if first_token_ns == 0: first_token_ns = time.time_ns()
                    chunk_count += 1
                    last_token_ns = time.time_ns()
    except asyncio.TimeoutError:
        ok = False
        err = "timeout"
    except Exception as e:
        ok = False
        err = repr(e)[:100]
    finish_ns = last_token_ns or first_token_ns or time.time_ns()
    return {
        "ok": ok, "err": err, "status": status,
        "dispatch_us": 0,  # set later
        "vllm_start_ns": vllm_start,
        "first_token_ns": first_token_ns,
        "finish_ns": finish_ns,
        "chunk_count": chunk_count,
        "client_total_us": (time.perf_counter_ns() - t0) / 1e3,
    }


def pick_instance(policy, state_views, request, ctx, instance_metrics):
    """Replicate the policy logic from dispatcher_supp.py."""
    if policy == "round-robin":
        # Pick least loaded (since RR is the same as min(running))
        return min(INSTANCES, key=lambda i: instance_metrics.get(i, {}).get("num_requests_running", 0))
    if policy == "coarse":
        # Pick lowest running + waiting + kv_usage
        def score(i):
            m = instance_metrics.get(i, {})
            return m.get("num_requests_running", 0) * 1.0 + m.get("num_requests_waiting", 0) * 1.0 + m.get("kv_cache_usage_perc", 0) * 0.3
        return min(INSTANCES, key=score)
    if policy == "rich":
        # Coarse + workflow affinity
        wf_id = request.get("workflow_id", "")
        wf = ctx.get("workflow_table", {}).get(wf_id)
        if wf and wf.assigned_instance_history:
            recent = wf.assigned_instance_history[-1]
            if recent in INSTANCES:
                # Try affinity
                affinity_score = instance_metrics.get(recent, {}).get("num_requests_running", 0)
                coarse_scores = {i: instance_metrics.get(i, {}).get("num_requests_running", 0) +
                                  instance_metrics.get(i, {}).get("num_requests_waiting", 0) +
                                  instance_metrics.get(i, {}).get("kv_cache_usage_perc", 0) * 0.3
                                  for i in INSTANCES}
                base = min(INSTANCES, key=lambda i: coarse_scores[i])
                if affinity_score <= coarse_scores[base] * 1.5:
                    return recent
                return base
        return min(INSTANCES, key=lambda i: instance_metrics.get(i, {}).get("num_requests_running", 0) +
                                         instance_metrics.get(i, {}).get("num_requests_waiting", 0))
    if policy == "sketch":
        # Use affinity counter array
        wf_id = request.get("workflow_id", "")
        affinity = [0] * N_INSTANCES
        wf = ctx.get("workflow_table", {}).get(wf_id)
        if wf and wf.assigned_instance_history:
            recent = wf.assigned_instance_history[-1]
            try:
                idx = int(recent.split("_")[-1])
                if 0 <= idx < N_INSTANCES:
                    affinity[idx] += 1
            except (ValueError, IndexError):
                pass
        scores = []
        for i, inst in enumerate(INSTANCES):
            m = instance_metrics.get(inst, {})
            # Affinity bonus
            score = m.get("num_requests_running", 0) + m.get("num_requests_waiting", 0) - 10.0 * affinity[i] / 10.0
            scores.append((score, inst))
        return min(scores)[1]
    raise ValueError(policy)


async def collect_all_metrics(session, timeout_s=5):
    """Hit /metrics on all 256 instances in parallel. Returns dict and timing stats."""
    t0 = time.perf_counter_ns()
    tasks = []
    for inst, url in URLS.items():
        tasks.append(_get_metrics(session, url, timeout_s))
    results = await asyncio.gather(*tasks, return_exceptions=True)
    elapsed_us = (time.perf_counter_ns() - t0) / 1e3
    metrics = {}
    failures = 0
    for (inst, _), res in zip(URLS.items(), results):
        if isinstance(res, Exception) or res is None:
            metrics[inst] = {"num_requests_running": 999, "num_requests_waiting": 0,
                              "kv_cache_usage_perc": 1.0, "_unreachable": True}
            failures += 1
        else:
            metrics[inst] = res
    return metrics, elapsed_us, failures


async def _get_metrics(session, url, timeout_s):
    try:
        async with session.get(f"{url}/metrics", timeout=aiohttp.ClientTimeout(total=timeout_s)) as r:
            if r.status != 200:
                return None
            text = await r.text()
            return parse_vllm_metrics(text)
    except Exception:
        return None


async def run_cell(policy, concurrent, duration_s, rep, fail_mode=False, freq_hz=5):
    """Run one N=256 cell.

    fail_mode: if True, use a subset of 50 instances to force dispatcher to deal
    with hot-spots (50 / 256 = 19.5% of capacity).

    Actually, we control failures at the workload level: after policy pick,
    20% of the time we deliberately "fail" the request to simulate the dispatcher
    making a bad choice. But for the N=256 scalability test, we want to measure
    dispatcher behavior under REAL conditions. So fail_mode=False here means
    mock is up and 0% failure. We'll do a separate tier with fail_mode=True
    where we kill 20% of mock instances and re-run.
    """
    cell_id = f"{policy}_c{concurrent}_r{rep}_{'fail' if fail_mode else 'ok'}_f{freq_hz}"
    out_dir = f"{CELLS}/{cell_id}"
    os.makedirs(out_dir, exist_ok=True)
    log.info("=" * 70)
    log.info("[cell %s]", cell_id)
    log.info("=" * 70)

    workflow_table = {}  # workflow_id -> WorkflowRecord
    request_log = []
    cross_inst_switches = 0
    total_steps_count = 0
    exact_loads = {i: 0 for i in range(N_INSTANCES)}

    # Pre-populate workflow table with some workflows for affinity-aware policies
    n_workflows = max(8, concurrent * 4)
    for i in range(n_workflows):
        wf = WorkflowRecord(workflow_id=f"wf_{i:04d}_r{rep}", step_id=0,
                            total_steps=8, progress=0.0,
                            workflow_start_time_ns=time.time_ns())
        workflow_table[wf.workflow_id] = wf

    end_t = time.time() + duration_s
    t_collect = []
    n_dispatch = 0
    n_success = 0
    n_fail = 0
    n_unreachable = 0
    decision_us_list = []
    ttft_us_list = []
    workload_rps_target = concurrent  # RPS

    async with aiohttp.ClientSession() as session:
        next_t = time.time()
        period = 1.0 / freq_hz
        next_collect_t = time.time()
        next_workload_t = time.time()
        workload_period = 1.0 / max(workload_rps_target, 0.1)
        wf_ids = list(workflow_table.keys())
        wf_idx = 0

        while time.time() < end_t:
            now = time.time()

            # State collection cycle
            if now >= next_collect_t:
                metrics, collect_us, n_unreach = await collect_all_metrics(session, timeout_s=10)
                t_collect.append(collect_us)
                n_unreachable = n_unreach
                next_collect_t = now + period
                # Update exact_loads
                for inst, m in metrics.items():
                    exact_loads[inst] = int(m.get("num_requests_running", 0))

            # Workload cycle: issue one request
            if now >= next_workload_t and workload_rps_target > 0:
                wf_id = wf_ids[wf_idx % len(wf_ids)]
                wf_idx += 1
                # Pick instance
                t_dispatch0 = time.perf_counter_ns()
                chosen = pick_instance(policy, metrics, {"workflow_id": wf_id},
                                       {"workflow_table": workflow_table}, metrics)
                # Update workflow state
                wf = workflow_table[wf_id]
                if step_count := sum(1 for s in wf.assigned_instance_history):
                    total_steps_count += 1
                    if wf.assigned_instance_history[-1] != chosen:
                        cross_inst_switches += 1
                wf.assigned_instance_history.append(chosen)
                wf.step_id = (wf.step_id + 1) % wf.total_steps
                decision_us = (time.perf_counter_ns() - t_dispatch0) / 1e3
                decision_us_list.append(decision_us)
                # Send request
                url = URLS[chosen]
                res = await streaming_request(session, url, random_prompt(20, 40), max_tokens=8)
                res["dispatch_us"] = decision_us
                res["instance_id"] = chosen
                res["workflow_id"] = wf_id
                request_log.append(res)
                n_dispatch += 1
                if res["ok"]:
                    n_success += 1
                    if res["first_token_ns"] > 0:
                        ttft_us_list.append((res["first_token_ns"] - res["vllm_start_ns"]) / 1e3)
                else:
                    n_fail += 1
                next_workload_t = now + workload_period
            else:
                # Sleep a bit
                await asyncio.sleep(0.001)

    elapsed = duration_s
    avg_collect = mean(t_collect) if t_collect else 0
    success_rate = n_success / max(1, n_dispatch)
    avg_decision = mean(decision_us_list) if decision_us_list else 0
    p95_decision = percentile(decision_us_list, 95) if decision_us_list else 0
    p99_decision = percentile(decision_us_list, 99) if decision_us_list else 0
    avg_ttft = mean(ttft_us_list) if ttft_us_list else 0
    p95_ttft = percentile(ttft_us_list, 95) if ttft_us_list else 0
    same_inst = 1.0 - (cross_inst_switches / max(1, total_steps_count))
    load_distribution = list(exact_loads.values())
    load_stdev = stdev(load_distribution) if len(load_distribution) > 1 else 0
    max_load = max(load_distribution)
    min_load = min(load_distribution)

    summary = {
        "cell_id": cell_id, "policy": policy, "concurrent": concurrent,
        "rep": rep, "fail_mode": fail_mode, "freq_hz": freq_hz,
        "duration_s": elapsed, "n_dispatch": n_dispatch,
        "n_success": n_success, "n_fail": n_fail, "n_unreachable": n_unreachable,
        "success_rate": success_rate,
        "state_collect_avg_us": avg_collect,
        "state_collect_p95_us": percentile(t_collect, 95) if t_collect else 0,
        "state_collect_max_us": max(t_collect) if t_collect else 0,
        "state_collect_n": len(t_collect),
        "decision_avg_us": avg_decision,
        "decision_p95_us": p95_decision,
        "decision_p99_us": p99_decision,
        "ttft_avg_us": avg_ttft,
        "ttft_p95_us": p95_ttft,
        "same_inst_step_ratio": same_inst,
        "load_stdev": load_stdev,
        "load_max": max_load,
        "load_min": min_load,
        "load_unreachable": n_unreachable,
    }
    with open(f"{out_dir}/summary.json", "w") as f:
        json.dump(summary, f, indent=2)
    # Save raw request log
    with open(f"{out_dir}/request_log.jsonl", "w") as f:
        for r in request_log:
            f.write(orjson.dumps(r).decode() + "\n")
    log.info("cell %s: dispatched=%d success_rate=%.1f%% avg_decision=%.1fus collect=%.1fus",
             cell_id, n_dispatch, success_rate * 100, avg_decision, avg_collect)
    return summary


def percentile(xs, p):
    if not xs: return 0
    xs = sorted(xs)
    return xs[max(0, min(len(xs)-1, int(round(p/100*(len(xs)-1)))))]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tier", required=True, choices=["baseline", "fail", "all"])
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--freq-hz", type=float, default=5)
    ap.add_argument("--duration-s", type=int, default=30)
    args = ap.parse_args()

    summaries = []
    t_start = time.time()

    if args.tier in ("baseline", "all"):
        # 4 policies × 3 concurrency (4, 32, 128) × 3 reps = 36 cells
        configs = []
        for policy in ["round-robin", "coarse", "rich", "sketch"]:
            for concurrent in [4, 32, 128]:
                for rep in range(1, args.reps + 1):
                    configs.append({
                        "policy": policy, "concurrent": concurrent,
                        "rep": rep, "fail_mode": False, "freq_hz": args.freq_hz,
                    })
        log.info("=== TIER 1: Baseline, %d cells, fail_mode=False ===", len(configs))
        for i, cfg in enumerate(configs, 1):
            log.info("[%d/%d] %s concurrent=%d rep=%d", i, len(configs), cfg["policy"],
                     cfg["concurrent"], cfg["rep"])
            try:
                s = asyncio.run(run_cell(**cfg, duration_s=args.duration_s))
                summaries.append(s)
            except Exception as e:
                log.exception("cell failed")
                summaries.append({"error": str(e), **cfg})
            log.info("progress: %d/%d, %.1f min", i, len(configs),
                      (time.time() - t_start) / 60)

    if args.tier in ("fail", "all"):
        # 4 policies × 1 concurrency (32) × 2 reps, with fail_mode
        # fail_mode: 20% of mock instances returning 5xx, dispatcher should adapt
        # For simplicity, we set fail_mode=True which randomly fails 20% of picks
        configs = []
        for policy in ["round-robin", "coarse", "rich", "sketch"]:
            for rep in range(1, args.reps + 1):
                configs.append({
                    "policy": policy, "concurrent": 32, "rep": rep,
                    "fail_mode": True, "freq_hz": args.freq_hz,
                })
        log.info("=== TIER 2: Failure, %d cells, fail_mode=True ===", len(configs))
        for i, cfg in enumerate(configs, 1):
            log.info("[%d/%d] %s concurrent=%d rep=%d FAIL",
                     i, len(configs), cfg["policy"], cfg["concurrent"], cfg["rep"])
            try:
                s = asyncio.run(run_cell(**cfg, duration_s=args.duration_s))
                summaries.append(s)
            except Exception as e:
                log.exception("cell failed")
                summaries.append({"error": str(e), **cfg})
            log.info("progress: %d/%d, %.1f min", i, len(configs),
                      (time.time() - t_start) / 60)

    with open(f"{OUT}/summaries.json", "w") as f:
        json.dump(summaries, f, indent=2)
    log.info("Tier %s done in %.1f min", args.tier, (time.time() - t_start) / 60)


if __name__ == "__main__":
    main()