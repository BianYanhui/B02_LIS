"""B02 Trade-off v3: 8 instances, 3 reps, statistical tests.

8 vLLM instances (2 per GPU), 4 policies, 2 load conditions, 3 reps:
  - 4 (policies) * 2 (load) * 3 (reps) = 24 cell-runs
  - ~90s per cell = ~35 min total

Statistical tests: paired t-test / Wilcoxon on per-rep measurements.
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
log = logging.getLogger("v3")

sys.path.insert(0, os.path.expanduser("~/B02/experiments/scripts"))
from dispatcher import (
    Dispatcher, DispatcherConfig, WorkflowRecord, POLICIES,
)
from workloads import MODEL_ID, TOOL_NAMES, random_prompt

# 8 instances: 2 per GPU
N_INSTANCES = 8
INSTANCES = [f"instance_{i}" for i in range(N_INSTANCES)]
URLS = {f"instance_{i}": f"http://127.0.0.1:{8000+i}" for i in range(N_INSTANCES)}

LONG_SYSTEM_PROMPT = """You are a careful, helpful AI assistant. Below is a structured
dialogue between a user and the AI. The AI reasons step by step, considers edge cases,
and presents its answer in a clear, numbered format. It always double-checks arithmetic,
distinguishes correlation from causation, and refuses to invent facts it doesn't know.

When asked to analyze data, the AI:
1. Identifies the question being asked
2. Lists the relevant given information
3. Performs the analysis step by step
4. Sanity-checks the result
5. States the conclusion with appropriate confidence

When asked to write code, the AI:
1. Considers edge cases (empty input, null, unicode, very large values)
2. Picks a clear algorithm and names it
3. Writes the code with comments
4. Tests it mentally on a few examples
5. Reports time and space complexity

The AI is concise but never at the expense of correctness. It prefers precise language
over vague handwaving. It acknowledges uncertainty when present.
""" * 2

CHAT_HISTORY_PREFIX = """User: I am working through a multi-step problem. Please help
me reason carefully through each step.

Assistant: Understood. I will treat each step as part of one continuous reasoning process.

User: Here is the setup for the task. There are several subtasks that need to be solved
in order. Please answer each step in turn, carrying forward the reasoning.

Assistant: Got it. I am ready to receive the first step's question.

""" * 3


async def background_noise(instances_to_load, urls, rate_rps_per_instance, duration_s):
    end = time.time() + duration_s
    interval = 1.0 / rate_rps_per_instance if rate_rps_per_instance > 0 else 0
    sem = asyncio.Semaphore(8)
    async with aiohttp.ClientSession() as session:
        async def one_noise(inst):
            async with sem:
                url = urls[inst]
                try:
                    async with session.post(
                        f"{url}/v1/chat/completions",
                        json={"model": MODEL_ID,
                              "messages": [{"role": "user",
                                            "content": f"echo {random.randint(0, 99999)}"}],
                              "max_tokens": 1, "temperature": 0.0},
                        timeout=aiohttp.ClientTimeout(total=30),
                    ) as r:
                        await r.read()
                except Exception:
                    pass
        while time.time() < end:
            tasks = [asyncio.create_task(one_noise(i)) for i in instances_to_load]
            await asyncio.gather(*tasks, return_exceptions=True)
            if interval > 0:
                await asyncio.sleep(interval)


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
    return {"ok": ok, "err": err,
            "vllm_start_ns": vllm_start,
            "first_token_ns": first_token_ns, "finish_ns": finish_ns,
            "chunk_count": chunk_count}


def get_cache_metrics(urls):
    hits = queries = 0
    for inst, url in urls.items():
        try:
            r = requests.get(f"{url}/metrics", timeout=5)
            for line in r.text.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "vllm:gpu_prefix_cache_hits_total" in line:
                    try: hits += float(line.rsplit(" ", 1)[-1])
                    except ValueError: pass
                if "vllm:gpu_prefix_cache_queries_total" in line:
                    try: queries += float(line.rsplit(" ", 1)[-1])
                    except ValueError: pass
        except Exception:
            pass
    return {"hits": hits, "queries": queries}


def percentile(xs, p):
    if not xs: return 0
    xs = sorted(xs)
    return xs[max(0, min(len(xs)-1, int(round(p/100*(len(xs)-1)))))]


async def run_cell(policy, freq_hz, n_workflows, n_steps, concurrent, duration_s,
                   out_dir, rep, noise_targets=None, noise_rps=2.0):
    os.makedirs(out_dir, exist_ok=True)
    cell_id = f"{policy}_f{freq_hz:g}_{noise_targets or 'none'}_r{rep}_w{n_workflows}_s{n_steps}_c{concurrent}"
    cfg = DispatcherConfig(
        instances=INSTANCES,
        instance_urls=URLS,
        state_view={"round-robin": "coarse", "coarse": "coarse",
                    "rich": "rich", "sketch": "sketch"}[policy],
        update_freq_hz=freq_hz, duration_s=duration_s, out_dir=out_dir,
        cell_id=cell_id, workload="agentic", rep=rep, policy=policy,
    )
    dispatcher = Dispatcher(cfg)
    baseline = get_cache_metrics(URLS)

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

    async def one_workflow(wf_idx):
        wf_id = f"wf_{wf_idx:04d}_r{rep}"
        wf_start = time.time_ns()
        wf = WorkflowRecord(workflow_id=wf_id, step_id=0, total_steps=n_steps,
                            workflow_start_time_ns=wf_start)
        dispatcher.register_workflow(wf)
        wf_rec = {"workflow_id": wf_id, "total_steps": n_steps,
                  "workflow_start_time_ns": wf_start, "steps": [], "success": True}
        async with aiohttp.ClientSession() as session:
            for step in range(n_steps):
                t0 = time.time_ns()
                rec = dispatcher.forward({"workflow_id": wf_id, "step_id": step,
                                          "type": "agentic_step"})
                instance = rec["instance_id"]
                url = cfg.instance_urls[instance]
                wf.last_assigned_instance = instance
                wf.assigned_instance_history.append(instance)
                wf.step_id = step
                wf.progress = (step + 1) / n_steps
                wf.last_tool_name = random.choice(TOOL_NAMES)
                await asyncio.sleep(0.05)
                messages = [
                    {"role": "system", "content": LONG_SYSTEM_PROMPT},
                    {"role": "user",
                     "content": CHAT_HISTORY_PREFIX + f"Step {step+1}/{n_steps}: {random_prompt(64, 96)}"},
                ]
                res = await streaming_request(session, url, messages, max_tokens=32)
                ok = res["ok"]
                ttft_ms = (res["first_token_ns"] - res["vllm_start_ns"]) / 1e6 if ok and res["first_token_ns"] else 0
                tpot_ms = ((res["finish_ns"] - res["first_token_ns"]) / max(1, res["chunk_count"]-1) / 1e6) if ok and res["chunk_count"] > 1 else 0
                wf_rec["steps"].append({
                    "step_id": step, "instance_id": instance, "decision_us": rec["decision_time_us"],
                    "ttft_ms": ttft_ms, "tpot_ms": tpot_ms, "ok": ok, "err": res["err"],
                })
                request_log.append({
                    "instance_id": instance, "workflow_id": wf_id, "step_id": step,
                    "ttft_ms": ttft_ms, "tpot_ms": tpot_ms, "ok": ok,
                    "policy": policy, "decision_us": rec["decision_time_us"],
                })
                if not ok: wf_rec["success"] = False; break
        wf_finish = time.time_ns()
        wf_rec["workflow_completion_ms"] = (wf_finish - wf_start) / 1e6
        workflow_results.append(wf_rec)

    sem = asyncio.Semaphore(concurrent)
    async def with_sem(idx):
        async with sem: await one_workflow(idx)

    noise_task = None
    if noise_targets:
        noise_task = asyncio.create_task(background_noise(
            noise_targets, URLS, rate_rps_per_instance=noise_rps,
            duration_s=duration_s + 5,
        ))

    await asyncio.gather(*[asyncio.create_task(with_sem(i)) for i in range(n_workflows)])
    if noise_task:
        noise_task.cancel()
        try: await noise_task
        except asyncio.CancelledError: pass
    stop.set()
    th.join(timeout=10)

    final = get_cache_metrics(URLS)
    with open(os.path.join(out_dir, f"{cell_id}_workflow.jsonl"), "w") as f:
        for r in workflow_results: f.write(orjson.dumps(r).decode() + "\n")
    with open(os.path.join(out_dir, f"{cell_id}_request_log.jsonl"), "w") as f:
        for r in request_log: f.write(orjson.dumps(r).decode() + "\n")

    ttfts = [r["ttft_ms"] for r in request_log if r["ok"] and r["ttft_ms"] > 0]
    tpots = [r["tpot_ms"] for r in request_log if r["ok"] and r["tpot_ms"] > 0]
    decisions = [r["decision_us"] for r in request_log if r.get("decision_us") is not None]
    wcs = [wf["workflow_completion_ms"] for wf in workflow_results if wf["success"]]
    load_samples = getattr(dispatcher, "_load_samples", [])
    load_stdevs = []
    for s in load_samples:
        if len(s) == N_INSTANCES and any(x > 0 for x in s):
            load_stdevs.append(stdev(s))
    avg_load_stdev = mean(load_stdevs) if load_stdevs else 0
    n_total = len(request_log)
    n_sla = sum(1 for r in request_log if r["ok"] and r["ttft_ms"] > 0 and r["ttft_ms"] < 3000)
    sla = n_sla / max(1, n_total)
    n_fail = sum(1 for r in request_log if not r["ok"])
    sizes = []
    f_path = os.path.join(out_dir, "state_updates.jsonl")
    if os.path.exists(f_path):
        with open(f_path) as f:
            for line in f:
                d = json.loads(line)
                for inst, dd in d.get("per_instance", {}).items():
                    sizes.append(dd.get("size_bytes", 0))
    cache_hits = final["hits"] - baseline["hits"]
    cache_q = final["queries"] - baseline["queries"]
    cache_hit = cache_hits / cache_q if cache_q > 0 else 0
    dispatcher.close()

    summary = {
        "cell_id": cell_id, "policy": policy, "rep": rep,
        "noise_targets": noise_targets, "noise_rps": noise_rps,
        "freq_hz": freq_hz, "n_workflows": n_workflows, "n_steps": n_steps,
        "concurrent": concurrent, "duration_s": duration_s,
        "n_state_updates": dispatcher.update_count,
        "ttft_p50": percentile(ttfts, 50),
        "ttft_p95": percentile(ttfts, 95),
        "ttft_p99": percentile(ttfts, 99),
        "tpot_p50": percentile(tpots, 50),
        "tpot_p95": percentile(tpots, 95),
        "tpot_p99": percentile(tpots, 99),
        "cache_hit_rate": cache_hit,
        "cache_hits_delta": cache_hits,
        "cache_queries_delta": cache_q,
        "load_stdev": avg_load_stdev,
        "sla_success_rate": sla,
        "workflow_completion_p50": percentile(wcs, 50),
        "workflow_completion_p95": percentile(wcs, 95),
        "workflow_completion_p99": percentile(wcs, 99),
        "n_total_steps": n_total, "n_failed_steps": n_fail,
        "failure_rate": n_fail / max(1, n_total),
        "state_size_avg": mean(sizes) if sizes else 0,
        "state_size_p95": percentile(sizes, 95),
        "state_traffic_Bps": (mean(sizes) if sizes else 0) * freq_hz * N_INSTANCES,
        "dispatch_decision_p50": percentile(decisions, 50),
        "dispatch_decision_p95": percentile(decisions, 95),
        "dispatch_decision_p99": percentile(decisions, 99),
        # raw samples for stats tests
        "ttft_raw": ttfts,
        "decision_raw": decisions,
    }
    with open(os.path.join(out_dir, "summary.json"), "w") as f:
        json.dump(summary, f, indent=2)
    log.info("cell %s: cache=%.3f ttft_p95=%.0fms load_std=%.2f sla=%.2f%% wf_p95=%.0fms state=%dB disp_p95=%.1fus",
             cell_id, cache_hit, summary["ttft_p95"], avg_load_stdev,
             sla*100, summary["workflow_completion_p95"],
             summary["state_size_p95"], summary["dispatch_decision_p95"])
    return summary


POLICIES = ["round-robin", "coarse", "rich", "sketch"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", default=os.path.expanduser("~/B02/tradeoff_experiments/results_v3/cells"))
    ap.add_argument("--policies", nargs="+", default=POLICIES)
    ap.add_argument("--freq-hz", type=float, default=5.0)  # 5 Hz because 8 instances = 40 scrapes/s
    ap.add_argument("--n-workflows", type=int, default=12)
    ap.add_argument("--n-steps", type=int, default=10)
    ap.add_argument("--concurrent", type=int, default=4)
    ap.add_argument("--duration-s", type=float, default=75.0)
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args()

    # Load conditions: balanced (no noise), imbalanced_2 (2 of 8 loaded), imbalanced_6 (6 of 8 loaded)
    def load_targets(cond):
        if cond == "balanced":
            return None
        if cond == "imbalanced_2":
            return ["instance_0", "instance_1"]
        if cond == "imbalanced_6":
            return [f"instance_{i}" for i in range(6)]
        raise ValueError(cond)

    cells = []
    if args.all:
        configs = []
        for policy in args.policies:
            for cond in ["balanced", "imbalanced_2", "imbalanced_6"]:
                for rep in range(1, args.reps + 1):
                    configs.append((policy, cond, rep))
    else:
        configs = []
        for policy in args.policies:
            for cond in ["balanced", "imbalanced_2"]:
                configs.append((policy, cond, 1))

    summaries = []
    t_start = time.time()
    for i, (policy, cond, rep) in enumerate(configs, 1):
        nt = load_targets(cond)
        cell_id = f"{policy}_f{args.freq_hz:g}_{nt or 'none'}_r{rep}_w{args.n_workflows}_s{args.n_steps}_c{args.concurrent}"
        cell_dir = os.path.join(args.out_dir, cell_id)
        log.info("=" * 70)
        log.info("[%d/%d] policy=%s cond=%s rep=%d", i, len(configs), policy, cond, rep)
        log.info("=" * 70)
        try:
            s = asyncio.run(run_cell(
                policy, args.freq_hz, args.n_workflows, args.n_steps,
                args.concurrent, args.duration_s, cell_dir, rep,
                noise_targets=nt, noise_rps=2.0,
            ))
            summaries.append(s)
        except Exception as e:
            log.exception("cell %s failed", cell_id)
            summaries.append({"cell_id": cell_id, "error": str(e)})
        elapsed = time.time() - t_start
        log.info("progress: %d/%d done, elapsed %.1f min",
                 i, len(configs), elapsed / 60)

    elapsed = time.time() - t_start
    log.info("ALL DONE in %.1f min", elapsed / 60)

    with open(os.path.join(args.out_dir, "all_summaries.json"), "w") as f:
        json.dump(summaries, f, indent=2)
    log.info("Saved %d summaries", len(summaries))


if __name__ == "__main__":
    main()