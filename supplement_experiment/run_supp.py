"""B02 Supplement Experiment Runner.

Drives all 6 sub-experiments (A-F) with clean policy-view mapping.
Each tier corresponds to a paper section.

Usage:
    python run_supp.py --tier A     # 25 cells, 5 reps, 120s each (~75 min)
    python run_supp.py --tier B     # 36 cells, 3 reps
    python run_supp.py --tier C     # 36 cells, 3 reps
    python run_supp.py --tier D     # 9 cells, 3 reps
    python run_supp.py --tier E     # 18 cells, 3 reps
    python run_supp.py --tier F     # 72 cells, 3 reps
    python run_supp.py --tier ABD   # priority 1 only
    python run_supp.py --tier all
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
from statistics import mean
from typing import Any

import aiohttp
import orjson
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("supp")

sys.path.insert(0, os.path.expanduser("~/B02/supplement_experiment"))
sys.path.insert(0, os.path.expanduser("~/B02/experiments/scripts"))
sys.path.insert(0, os.path.expanduser("~/B02/tradeoff_experiments/scripts"))

from dispatcher_supp import (
    Dispatcher, DispatcherConfig, WorkflowRecord,
    POLICIES,
    assert_view_for_policy,
)
from workloads import MODEL_ID, TOOL_NAMES, random_prompt

RESULTS = os.path.expanduser("~/B02/supplement_experiment/results_20260706_152943")
CELLS = f"{RESULTS}/cells"
os.makedirs(CELLS, exist_ok=True)

N_INSTANCES = 8
INSTANCES = [f"instance_{i}" for i in range(N_INSTANCES)]
URLS = {f"instance_{i}": f"http://127.0.0.1:{8000+i}" for i in range(N_INSTANCES)}


def make_prefix(target_tokens: int) -> str:
    base = ("You are a careful, helpful AI assistant. Below is a structured, careful response. "
            "Always reason step by step, list given information, perform calculation, "
            "sanity-check, and state the conclusion.")
    s = base * 200
    return s[:target_tokens * 4]


async def streaming_request(session, url, messages, max_tokens):
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
                err = f"http {r.status}"; ok = False
            else:
                async for raw in r.content:
                    line = raw.decode(errors="ignore").strip()
                    if not line.startswith("data: "): continue
                    payload = line[len("data: "):].strip()
                    if payload == "[DONE]": break
                    if first_token_ns == 0: first_token_ns = time.time_ns()
                    chunk_count += 1
                    last_token_ns = time.time_ns()
    except Exception as e:
        ok = False; err = repr(e)[:200]
    finish_ns = last_token_ns or first_token_ns or time.time_ns()
    return {"ok": ok, "err": err, "vllm_start_ns": vllm_start,
            "first_token_ns": first_token_ns, "finish_ns": finish_ns,
            "chunk_count": chunk_count}


async def run_cell(policy, view, workload, ctx_tokens, n_steps, n_workflows,
                   concurrent, duration_s, rep, freq_hz=10, tool_delay_ms=200,
                   keep_history=True, keep_tool=True, keep_latency=True,
                   n_workflows_chatbot_for_diag=None):
    """One cell: collector + workload in single process."""
    cell_id = (f"{policy}_{view}_{workload}_ctx{ctx_tokens}_s{n_steps}_w{n_workflows}"
               f"_c{concurrent}_td{tool_delay_ms}_f{freq_hz}_r{rep}"
               f"{'_nohist' if not keep_history else ''}"
               f"{'_notool' if not keep_tool else ''}"
               f"{'_nolat' if not keep_latency else ''}")
    out_dir = f"{CELLS}/{cell_id}"
    os.makedirs(out_dir, exist_ok=True)
    log.info("=" * 70)
    log.info("[cell %s]", cell_id)
    log.info("=" * 70)

    cfg = DispatcherConfig(
        instances=INSTANCES,
        instance_urls=URLS,
        state_view=view,
        update_freq_hz=freq_hz,
        duration_s=duration_s + 5,
        out_dir=out_dir,
        cell_id=cell_id,
        workload=workload,
        rep=rep,
        policy=policy,
        n_workflows=n_workflows, n_steps=n_steps, concurrent=concurrent,
        ctx_tokens=ctx_tokens, tool_delay_ms=tool_delay_ms,
        keep_workflow_history=keep_history,
        keep_tool_metadata=keep_tool,
        keep_latency_summary=keep_latency,
    )
    # Sanity: if rep=1 and policy=oracle, do extra "perfect" tracking
    try:
        dispatcher = Dispatcher(cfg)
    except AssertionError as e:
        log.error("policy-view assertion failed: %s", e)
        with open(f"{out_dir}/failure.json", "w") as f:
            json.dump({"cell_id": cell_id, "error_type": "AssertionError",
                       "error_message": str(e)}, f, indent=2)
        return {"cell_id": cell_id, "error": str(e)}

    # Baseline cache metrics
    baseline = {"hits": 0, "queries": 0}
    for inst, url in URLS.items():
        try:
            r = requests.get(f"{url}/metrics", timeout=5)
            for line in r.text.splitlines():
                line = line.strip()
                if "vllm:gpu_prefix_cache_hits_total" in line:
                    try: baseline["hits"] += float(line.rsplit(" ",1)[-1])
                    except: pass
                if "vllm:gpu_prefix_cache_queries_total" in line:
                    try: baseline["queries"] += float(line.rsplit(" ",1)[-1])
                    except: pass
        except: pass

    stop = asyncio.Event()
    import threading
    def collect_loop():
        next_t = time.time()
        period = 1.0 / freq_hz
        load_samples = []
        ser_times = []
        while not stop.is_set():
            t_c0 = time.perf_counter_ns()
            # collect /metrics
            inst_metrics = {}
            for inst, url in URLS.items():
                try:
                    r = requests.get(f"{url}/metrics", timeout=5)
                    text = r.text
                except Exception:
                    text = ""
                m = parse_vllm_metrics(text)
                inst_metrics[inst] = m
                # also exact loads for oracle
                dispatcher.exact_loads[inst] = int(m.get("num_requests_running", 0))
            t_c1 = time.perf_counter_ns()
            # build view for each instance
            new_views = {}
            for inst in INSTANCES:
                t_b0 = time.perf_counter_ns()
                if view == "sketch-noaffinity":
                    v = dispatcher._build_sketch_state(time.time_ns(), inst_metrics, variant="noaffinity")
                elif view == "sketch-notoolbits":
                    v = dispatcher._build_sketch_state(time.time_ns(), inst_metrics, variant="notoolbits")
                elif view == "sketch-noprogress":
                    v = dispatcher._build_sketch_state(time.time_ns(), inst_metrics, variant="noprogress")
                elif view == "sketch-affinityonly":
                    v = dispatcher._build_sketch_state(time.time_ns(), inst_metrics, variant="affinityonly")
                elif view == "rich":
                    # We need per-instance rich view; the current _build_rich_state only does the "first" instance.
                    # For per-instance, we filter active_workflows by last_assigned.
                    v = build_rich_for_instance(dispatcher, inst, time.time_ns(), inst_metrics[inst])
                elif view == "oracle":
                    v = build_rich_for_instance(dispatcher, inst, time.time_ns(), inst_metrics[inst])
                    v["exact_loads"] = dict(dispatcher.exact_loads)
                elif view == "coarse":
                    v = build_coarse_for_instance(inst, time.time_ns(), inst_metrics[inst])
                elif view == "none":
                    v = {"instance_id": inst, "timestamp_ns": time.time_ns()}
                else:
                    v = {}
                t_b1 = time.perf_counter_ns()
                t_s0 = time.perf_counter_ns()
                blob = orjson.dumps(v)
                t_s1 = time.perf_counter_ns()
                t_d0 = time.perf_counter_ns()
                orjson.loads(blob)
                t_d1 = time.perf_counter_ns()
                sz = len(blob)
                # size breakdown for rich
                if view == "rich" and policy == "rich":
                    breakdown = {
                        "instance_id": inst,
                        "total_bytes": sz,
                        "coarse_bytes": len(orjson.dumps(v.get("runtime", {}))),
                        "active_workflows_bytes": len(orjson.dumps(v.get("active_workflows", []))),
                        "assigned_history_bytes": len(orjson.dumps(v.get("workflow_history", {}))),
                        "tool_metadata_bytes": len(orjson.dumps(v.get("tool_metadata", {}))),
                        "latency_summary_bytes": len(orjson.dumps(v.get("runtime", {}).get("latency_summary", {}))),
                        "num_active_workflows": len(v.get("active_workflows", [])),
                        "num_history_items": sum(len(vv) for vv in v.get("workflow_history", {}).values()),
                    }
                    dispatcher.size_breakdown.append(breakdown)
                new_views[inst] = v
                dispatcher.f_state.write(orjson.dumps({
                    "ts_ns": time.time_ns(),
                    "update_id": dispatcher.request_count,
                    "instance_id": inst,
                    "size_bytes": sz,
                    "build_us": (t_b1 - t_b0) / 1e3,
                    "ser_us": (t_s1 - t_s0) / 1e3,
                    "deser_us": (t_d1 - t_d0) / 1e3,
                }).decode() + "\n")
                dispatcher.f_state.flush()
            # also collect per-instance load samples
            load_samples.append([inst_metrics[inst].get("num_requests_running", 0) for inst in INSTANCES])
            dispatcher.state_views = new_views
            dispatcher.request_count += 1
            next_t += period
            sf = next_t - time.time()
            if sf > 0: time.sleep(sf)
            else: next_t = time.time()
        dispatcher._load_samples = load_samples

    th = threading.Thread(target=collect_loop, daemon=True)
    th.start()

    workflow_results = []
    request_log = []
    cross_inst_switches = 0
    total_steps_count = 0

    async def one_workflow(wf_idx):
        nonlocal cross_inst_switches, total_steps_count
        wf_id = f"{workload[:3]}_wf_{wf_idx:04d}_r{rep}"
        wf_start = time.time_ns()
        wf = WorkflowRecord(workflow_id=wf_id, step_id=0, total_steps=n_steps,
                            workflow_start_time_ns=wf_start)
        dispatcher.register_workflow(wf)
        wf_rec = {"workflow_id": wf_id, "total_steps": n_steps, "steps": [], "success": True}
        async with aiohttp.ClientSession() as session:
            for step in range(n_steps):
                t0 = time.time_ns()
                chosen, dec_us, scores = dispatcher.pick(
                    {"workflow_id": wf_id, "step_id": step, "type": workload})
                url = cfg.instance_urls[chosen]
                wf.last_assigned_instance = chosen
                wf.assigned_instance_history.append(chosen)
                wf.step_id = step
                wf.progress = (step + 1) / n_steps
                if step > 0 and len(wf.assigned_instance_history) >= 2:
                    total_steps_count += 1
                    if wf.assigned_instance_history[-2] != chosen:
                        cross_inst_switches += 1
                wf.tool_status = "running"
                wf.last_tool_name = random.choice(TOOL_NAMES)
                await asyncio.sleep(tool_delay_ms / 1000.0)
                wf.tool_status = "done"
                wf.last_tool_latency_ms = tool_delay_ms
                wf.tool_result_context_size = random.randint(100, 500)
                wf.tool_result_context_type = random.choice(["text", "code", "json"])
                wf.last_step_finish_time_ns = time.time_ns()
                # Build prompt
                if workload == "chatbot":
                    prefix = "You are a helpful assistant."
                    messages = [
                        {"role": "system", "content": prefix},
                        {"role": "user", "content": f"Q: {random_prompt(50, 100)}"},
                    ]
                    max_tokens = 32
                elif workload == "agentic" or workload == "prefix_locality":
                    prefix = make_prefix(ctx_tokens)
                    messages = [
                        {"role": "system", "content": prefix},
                        {"role": "user", "content": f"Step {step+1}/{n_steps}: {random_prompt(50, 100)}"},
                    ]
                    max_tokens = 24
                elif workload == "mixed":
                    if random.random() < 0.5:
                        messages = [
                            {"role": "system", "content": "Answer briefly."},
                            {"role": "user", "content": f"Q: {random_prompt(50, 100)}"},
                        ]
                        max_tokens = 32
                    else:
                        prefix = make_prefix(512)
                        messages = [
                            {"role": "system", "content": prefix},
                            {"role": "user", "content": f"Step {step+1}/{n_steps}: {random_prompt(50, 100)}"},
                        ]
                        max_tokens = 24
                else:
                    messages = [{"role": "user", "content": "hi"}]
                    max_tokens = 16
                vllm_start = time.time_ns()
                first_token_ns = 0
                finish_ns = 0
                out_tok = 0
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
                            err = f"http {r.status}"; ok = False
                        else:
                            async for raw in r.content:
                                line = raw.decode(errors="ignore").strip()
                                if not line.startswith("data: "): continue
                                payload = line[len("data: "):].strip()
                                if payload == "[DONE]": break
                                if first_token_ns == 0: first_token_ns = time.time_ns()
                                out_tok += 1
                            finish_ns = time.time_ns()
                except Exception as e:
                    ok = False; err = repr(e)[:200]
                ttft_ms = (first_token_ns - vllm_start) / 1e6 if ok and first_token_ns else 0
                decode_ms = (finish_ns - first_token_ns) / 1e6 if ok and finish_ns and first_token_ns else 0
                tpot_ms = (decode_ms / max(1, out_tok - 1)) if out_tok > 1 else 0
                wf_rec["steps"].append({
                    "step_id": step, "instance_id": chosen,
                    "decision_us": dec_us, "ttft_ms": ttft_ms, "tpot_ms": tpot_ms,
                    "ok": ok, "err": err,
                    "scores": scores,
                })
                request_log.append({
                    "instance_id": chosen, "workflow_id": wf_id, "step_id": step,
                    "ttft_ms": ttft_ms, "tpot_ms": tpot_ms, "ok": ok,
                    "policy": policy, "decision_us": dec_us,
                    "scores": scores,
                })
                if not ok: wf_rec["success"] = False; break
        wf_finish = time.time_ns()
        wf_rec["workflow_completion_ms"] = (wf_finish - wf_start) / 1e6
        workflow_results.append(wf_rec)

    sem = asyncio.Semaphore(concurrent)
    async def with_sem(idx):
        async with sem:
            await one_workflow(idx)
    await asyncio.gather(*[asyncio.create_task(with_sem(i)) for i in range(n_workflows)])
    stop.set()
    th.join(timeout=10)

    final = {"hits": 0, "queries": 0}
    for inst, url in URLS.items():
        try:
            r = requests.get(f"{url}/metrics", timeout=5)
            for line in r.text.splitlines():
                line = line.strip()
                if "vllm:gpu_prefix_cache_hits_total" in line:
                    try: final["hits"] += float(line.rsplit(" ",1)[-1])
                    except: pass
                if "vllm:gpu_prefix_cache_queries_total" in line:
                    try: final["queries"] += float(line.rsplit(" ",1)[-1])
                    except: pass
        except: pass
    cache_hits = final["hits"] - baseline["hits"]
    cache_q = final["queries"] - baseline["queries"]
    cache_hit = cache_hits / cache_q if cache_q > 0 else 0

    # Save raw
    with open(f"{out_dir}/workflow_log.jsonl", "w") as f:
        for r in workflow_results: f.write(orjson.dumps(r).decode() + "\n")
    with open(f"{out_dir}/request_log.jsonl", "w") as f:
        for r in request_log: f.write(orjson.dumps(r).decode() + "\n")
    with open(f"{out_dir}/dispatch_log.jsonl", "w") as f:
        for r in dispatcher.dispatch_log: f.write(orjson.dumps(r).decode() + "\n")
    with open(f"{out_dir}/metrics_log.jsonl", "w") as f:
        for r in [{"ts": time.time_ns(), "workflow_count": len(workflow_results)}]:
            f.write(orjson.dumps(r).decode() + "\n")

    # Aggregations
    ttfts = [r["ttft_ms"] for r in request_log if r["ok"] and r["ttft_ms"] > 0]
    decisions = [r["decision_us"] for r in request_log if r.get("decision_us") is not None]
    wcs = [wf["workflow_completion_ms"] for wf in workflow_results if wf["success"]]
    load_samples = getattr(dispatcher, "_load_samples", [])
    load_stdevs = []
    for s in load_samples:
        if len(s) == N_INSTANCES and any(x > 0 for x in s):
            load_stdevs.append(__import__("statistics").stdev(s))
    avg_load_std = mean(load_stdevs) if load_stdevs else 0

    sizes = []
    f_state_path = f"{out_dir}/state_updates.jsonl"
    if os.path.exists(f_state_path):
        with open(f_state_path) as f:
            for line in f:
                d = json.loads(line)
                sizes.append(d.get("size_bytes", 0))
    same_inst_step_ratio = 1.0 - (cross_inst_switches / max(1, total_steps_count))

    summary = {
        "cell_id": cell_id, "policy": policy, "view": view, "workload": workload,
        "ctx_tokens": ctx_tokens, "n_steps": n_steps, "n_workflows": n_workflows,
        "concurrent": concurrent, "duration_s": duration_s, "rep": rep,
        "freq_hz": freq_hz, "tool_delay_ms": tool_delay_ms,
        "n_state_updates": dispatcher.request_count,
        "ttft_p50": percentile(ttfts, 50),
        "ttft_p95": percentile(ttfts, 95),
        "ttft_p99": percentile(ttfts, 99),
        "tpot_p50": percentile([r["tpot_ms"] for r in request_log if r["ok"] and r["tpot_ms"] > 0], 50),
        "tpot_p95": percentile([r["tpot_ms"] for r in request_log if r["ok"] and r["tpot_ms"] > 0], 95),
        "workflow_completion_p50": percentile(wcs, 50),
        "workflow_completion_p95": percentile(wcs, 95),
        "workflow_completion_p99": percentile(wcs, 99),
        "cache_hit_rate": cache_hit,
        "cache_hits_delta": cache_hits,
        "cache_queries_delta": cache_q,
        "n_total_steps": total_steps_count,
        "cross_instance_switches": cross_inst_switches,
        "same_instance_step_ratio": same_inst_step_ratio,
        "n_total_workflows": len(workflow_results),
        "n_successful_workflows": sum(1 for w in workflow_results if w["success"]),
        "n_failed_requests": sum(1 for r in request_log if not r["ok"]),
        "load_stdev": avg_load_std,
        "state_size_avg": mean(sizes) if sizes else 0,
        "state_size_p95": percentile(sizes, 95),
        "state_size_max": max(sizes) if sizes else 0,
        "state_traffic_Bps": (mean(sizes) if sizes else 0) * freq_hz * N_INSTANCES,
        "dispatch_decision_p50": percentile(decisions, 50),
        "dispatch_decision_p95": percentile(decisions, 95),
        "dispatch_decision_p99": percentile(decisions, 99),
    }
    with open(f"{out_dir}/summary.json", "w") as f:
        json.dump(summary, f, indent=2)
    # Save size breakdown if any
    if dispatcher.size_breakdown:
        with open(f"{out_dir}/size_breakdown.jsonl", "w") as f:
            for s in dispatcher.size_breakdown:
                f.write(orjson.dumps(s).decode() + "\n")
    dispatcher.close()
    log.info("cell %s: cache=%.3f ttft_p95=%.0fms wf_p95=%.0fms state=%dB disp_p95=%.1fus",
             cell_id, summary["cache_hit_rate"], summary["ttft_p95"],
             summary["workflow_completion_p95"], summary["state_size_p95"],
             summary["dispatch_decision_p95"])
    return summary


def percentile(xs, p):
    if not xs: return 0
    xs = sorted(xs)
    return xs[max(0, min(len(xs)-1, int(round(p/100*(len(xs)-1)))))]


def parse_vllm_metrics(text):
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"): continue
        if "vllm:" not in line: continue
        try:
            head, val = line.rsplit(" ", 1)
            val = float(val)
        except ValueError:
            continue
        name = head.split("{", 1)[0].strip().replace("vllm:", "")
        if name.endswith("_bucket") or name.endswith("_sum") or name.endswith("_count"): continue
        if name.endswith("_total"): name = name[:-6]
        if name in ("num_requests_waiting", "num_requests_running",
                    "kv_cache_usage_perc", "gpu_cache_usage_perc",
                    "prompt_tokens_total", "generation_tokens_total",
                    "prefix_cache_hits_total", "prefix_cache_queries_total",
                    "request_success_total", "num_preemptions_total"):
            out[name] = out.get(name, 0) + val
    return out


def build_coarse_for_instance(instance_id, ts_ns, m):
    return {
        "instance_id": instance_id, "timestamp_ns": ts_ns,
        "runtime": {
            "num_requests_waiting": int(m.get("num_requests_waiting", 0)),
            "num_requests_running": int(m.get("num_requests_running", 0)),
            "kv_cache_usage_perc": float(m.get("kv_cache_usage_perc", 0.0)),
            "gpu_cache_usage_perc": float(m.get("gpu_cache_usage_perc", 0.0)),
            "prompt_tokens_total": int(m.get("prompt_tokens_total", 0)),
            "generation_tokens_total": int(m.get("generation_tokens_total", 0)),
            "prefix_cache_hits_total": int(m.get("prefix_cache_hits_total", 0)),
            "prefix_cache_queries_total": int(m.get("prefix_cache_queries_total", 0)),
            "request_success_total": int(m.get("request_success_total", 0)),
            "num_preemptions_total": int(m.get("num_preemptions_total", 0)),
        },
    }


def build_rich_for_instance(dispatcher, instance_id, ts_ns, m):
    """Build rich view for ONE specific instance, filtering workflows by last_assigned."""
    active_workflows = []
    for wf_id, wf in dispatcher.workflow_table.items():
        if wf.last_assigned_instance == instance_id:
            active_workflows.append({
                "workflow_id": wf.workflow_id,
                "current_step_id": wf.step_id,
                "total_steps": wf.total_steps,
                "workflow_progress": wf.progress,
                "last_assigned_instance": wf.last_assigned_instance,
                "assigned_instance_history": wf.assigned_instance_history[-5:] if dispatcher.cfg.keep_workflow_history else [],
                "tool_execution_status": wf.tool_status if dispatcher.cfg.keep_tool_metadata else "n/a",
                "last_tool_name": wf.last_tool_name if dispatcher.cfg.keep_tool_metadata else "n/a",
                "last_tool_latency_ms": wf.last_tool_latency_ms if dispatcher.cfg.keep_tool_metadata else 0,
                "tool_result_context_size": wf.tool_result_context_size if dispatcher.cfg.keep_tool_metadata else 0,
                "tool_result_context_type": wf.tool_result_context_type if dispatcher.cfg.keep_tool_metadata else "n/a",
                "tool_result_hash": "0x" + str(hash(wf_id) & 0xFFFFFFFF),
                "workflow_to_instance_affinity": {},
                "workflow_start_time_ns": wf.workflow_start_time_ns,
                "last_step_finish_time_ns": wf.last_step_finish_time_ns,
                "latency_sensitive_flag": 0,
            })
    history = {wf_id: wf.assigned_instance_history[-10:] if dispatcher.cfg.keep_workflow_history else []
               for wf_id, wf in dispatcher.workflow_table.items()}
    tool_meta = {wf_id: {"name": wf.last_tool_name, "latency_ms": wf.last_tool_latency_ms}
                 for wf_id, wf in dispatcher.workflow_table.items()} if dispatcher.cfg.keep_tool_metadata else {}
    lat_summary = {
        "ttft_p50": 0, "ttft_p95": 0, "tpot_p50": 0, "tpot_p95": 0,
        "queue_time_p95": 0, "prefill_time_p95": 0, "decode_time_p95": 0,
    } if dispatcher.cfg.keep_latency_summary else {}
    coarse = build_coarse_for_instance(instance_id, ts_ns, m)
    return {
        "instance_id": instance_id, "timestamp_ns": ts_ns,
        "runtime": {**coarse["runtime"], "latency_summary": lat_summary},
        "active_workflows": active_workflows,
        "workflow_history": history,
        "tool_metadata": tool_meta,
    }


# =================== Tier Configurations ===================

def tierA_configs(args):
    """Experiment A: clean trade-off, 5 policies × 5 reps = 25 cells."""
    out = []
    for policy in ["round-robin", "coarse", "rich", "sketch", "oracle"]:
        view = {"round-robin": "none", "coarse": "coarse", "rich": "rich",
                 "sketch": "sketch", "oracle": "oracle"}[policy]
        for rep in range(1, args.reps_a + 1):
            out.append({
                "policy": policy, "view": view, "workload": "prefix_locality",
                "ctx_tokens": 1024, "n_steps": 8, "n_workflows": 12,
                "concurrent": 4, "duration_s": 120, "rep": rep,
                "freq_hz": 10, "tool_delay_ms": 200,
            })
    return out


def tierB_configs(args):
    """Experiment B: long-context, 4 policies × 3 ctx × 3 reps = 36 cells."""
    out = []
    for policy in ["coarse", "rich", "sketch", "oracle"]:
        view = {"coarse": "coarse", "rich": "rich",
                 "sketch": "sketch", "oracle": "oracle"}[policy]
        for ctx in [256, 1024, 1536]:
            for rep in range(1, args.reps_b + 1):
                out.append({
                    "policy": policy, "view": view, "workload": "prefix_locality",
                    "ctx_tokens": ctx, "n_steps": 8, "n_workflows": 12,
                    "concurrent": 4, "duration_s": 120, "rep": rep,
                    "freq_hz": 10, "tool_delay_ms": 0,  # tool delay 0 per spec
                })
    return out


def tierC_configs(args):
    """Experiment C: workflow length, 4 policies × 3 steps × 3 reps = 36 cells."""
    out = []
    for policy in ["coarse", "rich", "sketch", "oracle"]:
        view = {"coarse": "coarse", "rich": "rich",
                 "sketch": "sketch", "oracle": "oracle"}[policy]
        for steps in [4, 8, 16]:
            for rep in range(1, args.reps_c + 1):
                out.append({
                    "policy": policy, "view": view, "workload": "prefix_locality",
                    "ctx_tokens": 1024, "n_steps": steps, "n_workflows": 12,
                    "concurrent": 4, "duration_s": 120, "rep": rep,
                    "freq_hz": 10, "tool_delay_ms": 200,
                })
    return out


def tierD_configs(args):
    """Experiment D: Rich chatbot size diagnosis. 3 modes × 3 reps = 9 cells."""
    out = []
    for mode in ["no_workflow_state", "empty_workflow_container", "global_history_enabled"]:
        for rep in range(1, args.reps_d + 1):
            keep_hist = (mode != "no_workflow_state")
            keep_tool = (mode != "empty_workflow_container")
            out.append({
                "policy": "rich", "view": "rich", "workload": "chatbot",
                "ctx_tokens": 512, "n_steps": 1, "n_workflows": 100,
                "concurrent": 16, "duration_s": 60, "rep": rep,
                "freq_hz": 10, "tool_delay_ms": 0,
                "keep_history": keep_hist, "keep_tool": keep_tool, "keep_latency": False,
            })
    return out


def tierE_configs(args):
    """Experiment E: Sketch ablation, 6 policies × 3 reps = 18 cells."""
    out = []
    for policy in ["sketch", "sketch-noaffinity", "sketch-notoolbits",
                    "sketch-noprogress", "sketch-affinityonly", "rich", "coarse"]:
        view = {"coarse": "coarse", "rich": "rich", "sketch": "sketch",
                 "sketch-noaffinity": "sketch", "sketch-notoolbits": "sketch",
                 "sketch-noprogress": "sketch", "sketch-affinityonly": "sketch"}[policy]
        for rep in range(1, args.reps_e + 1):
            out.append({
                "policy": policy, "view": view, "workload": "prefix_locality",
                "ctx_tokens": 1024, "n_steps": 8, "n_workflows": 12,
                "concurrent": 4, "duration_s": 120, "rep": rep,
                "freq_hz": 10, "tool_delay_ms": 200,
            })
    return out


def tierF_configs(args):
    """Experiment F: stress test, 4 N × 2 f × 3 view × 3 reps = 72 cells.
    Uses logical emulator mode (no vLLM)."""
    out = []
    for n in [4, 64, 256, 512]:
        for f_hz in [10, 50]:
            for view in ["coarse", "rich", "sketch"]:
                for rep in range(1, args.reps_f + 1):
                    out.append({
                        "n": n, "f_hz": f_hz, "view": view, "rep": rep,
                    })
    return out


# =================== Main ===================

async def run_real_cell(cfg):
    return await run_cell(**cfg)


async def run_stress_cell(n, f_hz, view, rep, duration_s=60):
    """Logical emulator: synthesize state views and measure."""
    import numpy as np
    out_dir = f"{CELLS}/stress_n{n}_f{f_hz}_v{view}_r{rep}"
    os.makedirs(out_dir, exist_ok=True)
    log.info("[stress] N=%d f=%g view=%s rep=%d", n, f_hz, view, rep)
    instances = [f"instance_{i}" for i in range(n)]
    state_views = {}
    sizes = []
    n_updates = 0
    ser_us = []
    deser_us = []
    merge_us = []
    total_us = []
    start_ts = time.time()
    end_ts = start_ts + duration_s
    period = 1.0 / f_hz
    next_t = start_ts
    # n_workflows per instance: scales with N
    n_wf_per_inst = max(2, 20 // n)
    while time.time() < end_ts:
        ts = time.time_ns()
        # Build per-instance state view
        for i, inst in enumerate(instances):
            if view == "coarse":
                v = build_coarse_for_instance(inst, ts, {
                    "num_requests_waiting": np.random.randint(0, 50),
                    "num_requests_running": np.random.randint(0, 20),
                    "kv_cache_usage_perc": np.random.random() * 0.9,
                    "gpu_cache_usage_perc": np.random.random() * 0.9,
                    "prompt_tokens_total": 0, "generation_tokens_total": 0,
                    "prefix_cache_hits_total": 0, "prefix_cache_queries_total": 0,
                    "request_success_total": 0, "num_preemptions_total": 0,
                })
            elif view == "rich":
                workflows = [{"workflow_id": f"wf_{j}", "current_step_id": j % 8, "total_steps": 8,
                              "workflow_progress": j / 8, "last_assigned_instance": inst,
                              "assigned_instance_history": [inst] * (j % 5 + 1),
                              "tool_execution_status": "idle", "last_tool_name": "search",
                              "last_tool_latency_ms": 100, "tool_result_context_size": 200,
                              "tool_result_context_type": "text", "tool_result_hash": "0x0",
                              "workflow_to_instance_affinity": {}, "workflow_start_time_ns": 0,
                              "last_step_finish_time_ns": 0, "latency_sensitive_flag": 0}
                             for j in range(n_wf_per_inst)]
                v = {
                    "instance_id": inst, "timestamp_ns": ts,
                    "runtime": {**build_coarse_for_instance(inst, ts, {})["runtime"]},
                    "active_workflows": workflows,
                    "workflow_history": {f"wf_{j}": [inst] for j in range(n_wf_per_inst)},
                    "tool_metadata": {f"wf_{j}": {"name": "x", "latency_ms": 100}
                                     for j in range(n_wf_per_inst)},
                }
            else:  # sketch
                v = {
                    "instance_id": inst, "timestamp_ns": ts,
                    "runtime": {
                        "kv_cache_usage_q": int(np.random.random() * 100),
                        "load_q": np.random.randint(0, 100),
                        "prefix_cache_hit_rate_q": int(np.random.random() * 100),
                    },
                    "workflow_sketch": {
                        "active_workflow_count": n_wf_per_inst,
                        "avg_progress_q": 50, "max_progress_q": 100,
                        "tool_status_bitset": 0xAAAA,
                        "tool_context_avail_bitmap": 0xFFFF,
                        "affinity_hot_instance_counts": [0] * n,
                        "recent_workflow_hashes": [0, 0, 0, 0],
                        "latency_sensitive_count": 0,
                    },
                }
            t_b0 = time.perf_counter_ns()
            blob = orjson.dumps(v)
            t_s0 = time.perf_counter_ns()
            sz = len(blob)
            orjson.loads(blob)
            t_d0 = time.perf_counter_ns()
            state_views[inst] = v
            t_m0 = time.perf_counter_ns()
            t_m1 = time.perf_counter_ns()
            sizes.append(sz)
            ser_us.append((t_s0 - t_b0) / 1e3)
            deser_us.append((t_d0 - t_s0) / 1e3)
            merge_us.append((t_m1 - t_m0) / 1e3)
            total_us.append((t_m1 - t_b0) / 1e3)
        n_updates += 1
        next_t += period
        sf = next_t - time.time()
        if sf > 0: time.sleep(sf)
        else: next_t = time.time()
    elapsed = time.time() - start_ts
    target_traffic = (mean(sizes) if sizes else 0) * f_hz * n / 1e6
    achieved_traffic = (mean(sizes) if sizes else 0) * (n_updates / elapsed) * n / 1e6
    summary = {
        "cell_id": f"stress_n{n}_f{f_hz}_v{view}_r{rep}",
        "n": n, "f_hz": f_hz, "view": view, "rep": rep,
        "duration_s": duration_s,
        "n_updates": n_updates,
        "target_f": f_hz,
        "achieved_f": n_updates / elapsed,
        "size_p95": sorted(sizes)[int(len(sizes) * 0.95)] if sizes else 0,
        "size_avg": mean(sizes) if sizes else 0,
        "target_traffic_MBps": target_traffic,
        "achieved_traffic_MBps": achieved_traffic,
        "ser_us_p95": percentile(ser_us, 95),
        "deser_us_p95": percentile(deser_us, 95),
        "merge_us_p95": percentile(merge_us, 95),
        "total_us_p95": percentile(total_us, 95),
        "missed_deadline_rate": max(0, 1 - (n_updates / elapsed) / f_hz),
        "sustainable": (n_updates / elapsed) >= f_hz * 0.9,
    }
    with open(f"{out_dir}/summary.json", "w") as f:
        json.dump(summary, f, indent=2)
    log.info("stress %s: %d updates achieved=%.2fHz target=%.0fHz", summary["cell_id"], n_updates,
             summary["achieved_f"], f_hz)
    return summary


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tier", required=True,
                    choices=["A", "B", "C", "D", "E", "F", "ABD", "all", "stress"])
    ap.add_argument("--reps-a", type=int, default=5)
    ap.add_argument("--reps-b", type=int, default=3)
    ap.add_argument("--reps-c", type=int, default=3)
    ap.add_argument("--reps-d", type=int, default=3)
    ap.add_argument("--reps-e", type=int, default=3)
    ap.add_argument("--reps-f", type=int, default=3)
    args = ap.parse_args()

    t_start = time.time()
    if args.tier == "A" or args.tier == "ABD" or args.tier == "all":
        configs = tierA_configs(args)
        log.info("=== Tier A: %d cells, %d reps each ===", len(configs), args.reps_a)
        summaries = []
        for i, cfg in enumerate(configs, 1):
            try:
                s = asyncio.run(run_real_cell(cfg))
                summaries.append(s)
            except Exception as e:
                log.exception("cell failed")
                summaries.append({"error": str(e), **cfg})
            log.info("progress: %d/%d, %.1f min", i, len(configs), (time.time() - t_start) / 60)
        with open(f"{RESULTS}/summaries_A.json", "w") as f:
            json.dump(summaries, f, indent=2)
    if args.tier == "B" or args.tier == "all":
        configs = tierB_configs(args)
        log.info("=== Tier B: %d cells ===", len(configs))
        summaries = []
        for i, cfg in enumerate(configs, 1):
            try:
                s = asyncio.run(run_real_cell(cfg))
                summaries.append(s)
            except Exception as e:
                log.exception("cell failed")
                summaries.append({"error": str(e), **cfg})
            log.info("progress: %d/%d, %.1f min", i, len(configs), (time.time() - t_start) / 60)
        with open(f"{RESULTS}/summaries_B.json", "w") as f:
            json.dump(summaries, f, indent=2)
    if args.tier == "C" or args.tier == "all":
        configs = tierC_configs(args)
        log.info("=== Tier C: %d cells ===", len(configs))
        summaries = []
        for i, cfg in enumerate(configs, 1):
            try:
                s = asyncio.run(run_real_cell(cfg))
                summaries.append(s)
            except Exception as e:
                log.exception("cell failed")
                summaries.append({"error": str(e), **cfg})
            log.info("progress: %d/%d, %.1f min", i, len(configs), (time.time() - t_start) / 60)
        with open(f"{RESULTS}/summaries_C.json", "w") as f:
            json.dump(summaries, f, indent=2)
    if args.tier == "D" or args.tier == "ABD" or args.tier == "all":
        configs = tierD_configs(args)
        log.info("=== Tier D: %d cells ===", len(configs))
        summaries = []
        for i, cfg in enumerate(configs, 1):
            try:
                s = asyncio.run(run_real_cell(cfg))
                summaries.append(s)
            except Exception as e:
                log.exception("cell failed")
                summaries.append({"error": str(e), **cfg})
            log.info("progress: %d/%d, %.1f min", i, len(configs), (time.time() - t_start) / 60)
        with open(f"{RESULTS}/summaries_D.json", "w") as f:
            json.dump(summaries, f, indent=2)
    if args.tier == "E" or args.tier == "all":
        configs = tierE_configs(args)
        log.info("=== Tier E: %d cells ===", len(configs))
        summaries = []
        for i, cfg in enumerate(configs, 1):
            try:
                s = asyncio.run(run_real_cell(cfg))
                summaries.append(s)
            except Exception as e:
                log.exception("cell failed")
                summaries.append({"error": str(e), **cfg})
            log.info("progress: %d/%d, %.1f min", i, len(configs), (time.time() - t_start) / 60)
        with open(f"{RESULTS}/summaries_E.json", "w") as f:
            json.dump(summaries, f, indent=2)
    if args.tier == "F" or args.tier == "all" or args.tier == "stress":
        configs = tierF_configs(args)
        log.info("=== Tier F: %d stress cells ===", len(configs))
        summaries = []
        for i, cfg in enumerate(configs, 1):
            try:
                s = asyncio.run(run_stress_cell(**cfg))
                summaries.append(s)
            except Exception as e:
                log.exception("stress cell failed")
                summaries.append({"error": str(e), **cfg})
            log.info("progress: %d/%d, %.1f min", i, len(configs), (time.time() - t_start) / 60)
        with open(f"{RESULTS}/summaries_F.json", "w") as f:
            json.dump(summaries, f, indent=2)
    log.info("Tier %s done in %.1f min", args.tier, (time.time() - t_start) / 60)


if __name__ == "__main__":
    main()