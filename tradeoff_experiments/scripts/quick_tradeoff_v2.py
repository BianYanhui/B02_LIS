"""B02 Trade-off v2: imbalanced load + quality vector.

Per policy, run with 2 load conditions:
  - balanced:    no background noise
  - imbalanced:  3 RPS noise to instance_0 and instance_1 (bypass dispatcher)

Quality metrics (vector):
  - TTFT p50 / p95 / p99  (true streaming-mode TTFT)
  - TPOT p50 / p95 / p99
  - Cache hit rate (vllm prefix cache)
  - Per-instance load stdev (lower = more balanced)
  - SLA success rate (% of steps with TTFT < 3s)
  - Workflow completion p95
  - Failure rate

Cost metrics:
  - State size p95 (B)
  - State traffic (B/s)
  - Dispatch decision p95 (us)
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
log = logging.getLogger("tradeoff2")

sys.path.insert(0, os.path.expanduser("~/B02/experiments/scripts"))
from dispatcher import (
    Dispatcher, DispatcherConfig, WorkflowRecord,
    POLICIES,
)
from workloads import MODEL_ID, TOOL_NAMES, random_prompt  # noqa

INSTANCES = ["instance_0", "instance_1", "instance_2", "instance_3"]
URLS = {f"instance_{i}": f"http://127.0.0.1:{8000+i}" for i in range(4)}


# Long shared system prompt + dialogue prefix for prefix-cache hit signal.
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
""" * 2  # ~500 tokens

CHAT_HISTORY_PREFIX = """User: I am working through a multi-step problem. Please help
me reason carefully through each step.

Assistant: Understood. I will treat each step as part of one continuous reasoning process.

User: Here is the setup for the task. There are several subtasks that need to be solved
in order. Please answer each step in turn, carrying forward the reasoning.

Assistant: Got it. I am ready to receive the first step's question.

""" * 3  # ~400 tokens


# ---------------------------------------------------------------------------
# Background noise (bypass dispatcher, hit specific instances)
# ---------------------------------------------------------------------------

async def background_noise(instances_to_load: list[str], urls: dict,
                            rate_rps_per_instance: float, duration_s: float,
                            n_tokens: int = 1):
    """Continuously send small requests to specific instances to create load."""
    end = time.time() + duration_s
    interval = 1.0 / rate_rps_per_instance if rate_rps_per_instance > 0 else 0
    sem = asyncio.Semaphore(8)
    async with aiohttp.ClientSession() as session:
        async def one_noise(inst: str):
            async with sem:
                url = urls[inst]
                try:
                    async with session.post(
                        f"{url}/v1/chat/completions",
                        json={"model": MODEL_ID,
                              "messages": [{"role": "user",
                                            "content": f"echo {random.randint(0, 99999)}"}],
                              "max_tokens": n_tokens, "temperature": 0.0},
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


# ---------------------------------------------------------------------------
# Streaming-mode request
# ---------------------------------------------------------------------------

async def streaming_request(session: aiohttp.ClientSession, url: str,
                             messages: list, max_tokens: int) -> dict:
    """Send a streaming request, return timing info + success."""
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
    return {
        "ok": ok, "err": err,
        "vllm_start_ns": vllm_start,
        "first_token_ns": first_token_ns,
        "finish_ns": finish_ns,
        "chunk_count": chunk_count,
    }


# ---------------------------------------------------------------------------
# Per-cell run
# ---------------------------------------------------------------------------

async def run_cell(policy: str, freq_hz: float, n_workflows: int, n_steps: int,
                    concurrent: int, duration_s: float, out_dir: str,
                    imbalanced: bool) -> dict:
    os.makedirs(out_dir, exist_ok=True)
    cell_id = f"{policy}_f{freq_hz:g}_{'imbal' if imbalanced else 'bal'}_w{n_workflows}_s{n_steps}_c{concurrent}"

    cfg = DispatcherConfig(
        instances=INSTANCES,
        instance_urls=URLS,
        state_view={"round-robin": "coarse", "coarse": "coarse",
                    "rich": "rich", "sketch": "sketch"}[policy],
        update_freq_hz=freq_hz,
        duration_s=duration_s,
        out_dir=out_dir,
        cell_id=cell_id,
        workload="agentic",
        rep=1,
        policy=policy,
    )
    dispatcher = Dispatcher(cfg)

    baseline_cache = get_cache_metrics(URLS)

    stop = asyncio.Event()
    import threading
    def collect_loop():
        next_t = time.time()
        period = 1.0 / freq_hz
        per_inst_load_samples = []  # list of [4 instance loads] samples
        while not stop.is_set():
            rec = dispatcher.collect_once()
            # extract per-instance load
            loads = []
            for inst in INSTANCES:
                pi = rec.get("per_instance", {}).get(inst, {})
                m = pi.get("metrics", {})
                loads.append(float(m.get("num_requests_running", 0)))
            per_inst_load_samples.append(loads)
            next_t += period
            sleep_for = next_t - time.time()
            if sleep_for > 0:
                time.sleep(sleep_for)
            else:
                next_t = time.time()
        dispatcher._tradeoff_load_samples = per_inst_load_samples

    th = threading.Thread(target=collect_loop, daemon=True)
    th.start()

    workflow_results = []
    request_log = []

    async def one_workflow(wf_idx: int):
        wf_id = f"wf_{wf_idx:04d}"
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
                decision_us = rec["decision_time_us"]
                wf.last_assigned_instance = instance
                wf.assigned_instance_history.append(instance)
                wf.step_id = step
                wf.progress = (step + 1) / n_steps

                # tool sim
                wf.tool_status = "running"
                wf.last_tool_name = random.choice(TOOL_NAMES)
                await asyncio.sleep(0.05)
                wf.tool_status = "done"

                messages = [
                    {"role": "system", "content": LONG_SYSTEM_PROMPT},
                    {"role": "user",
                     "content": CHAT_HISTORY_PREFIX + f"Step {step+1}/{n_steps}: {random_prompt(64, 96)}"},
                ]
                res = await streaming_request(session, url, messages, max_tokens=32)
                ok = res["ok"]
                ttft_ms = 0
                decode_ms = 0
                tpot_ms = 0
                if ok and res["first_token_ns"]:
                    ttft_ms = (res["first_token_ns"] - res["vllm_start_ns"]) / 1e6
                    if res["finish_ns"] and res["first_token_ns"]:
                        decode_ms = (res["finish_ns"] - res["first_token_ns"]) / 1e6
                        if res["chunk_count"] > 1:
                            tpot_ms = decode_ms / (res["chunk_count"] - 1)
                step_rec = {
                    "step_id": step, "instance_id": instance,
                    "decision_us": decision_us,
                    "ttft_ms": ttft_ms, "decode_ms": decode_ms, "tpot_ms": tpot_ms,
                    "chunk_count": res["chunk_count"],
                    "ok": ok, "err": res["err"],
                }
                wf_rec["steps"].append(step_rec)
                request_log.append({
                    "instance_id": instance, "workflow_id": wf_id, "step_id": step,
                    "arrival_ns": t0, "vllm_start_ns": res["vllm_start_ns"],
                    "first_token_ns": res["first_token_ns"],
                    "finish_ns": res["finish_ns"],
                    "ttft_ms": ttft_ms, "tpot_ms": tpot_ms,
                    "ok": ok, "err": res["err"],
                    "policy": policy, "decision_us": decision_us,
                })
                if not ok:
                    wf_rec["success"] = False
                    break
        wf_finish = time.time_ns()
        wf_rec["workflow_finish_ns"] = wf_finish
        wf_rec["workflow_completion_ms"] = (wf_finish - wf_start) / 1e6
        workflow_results.append(wf_rec)

    sem = asyncio.Semaphore(concurrent)
    async def run_with_sem(idx):
        async with sem:
            await one_workflow(idx)

    # Start background noise if imbalanced
    noise_task = None
    if imbalanced:
        # Make instance_0 and instance_1 loaded via 2 RPS noise each
        noise_task = asyncio.create_task(background_noise(
            ["instance_0", "instance_1"], URLS, rate_rps_per_instance=2.0,
            duration_s=duration_s,
        ))

    # Start workflows
    wf_task = asyncio.gather(*[asyncio.create_task(run_with_sem(i))
                                for i in range(n_workflows)])

    # Wait for workflows to finish
    await wf_task
    if noise_task:
        noise_task.cancel()
        try:
            await noise_task
        except asyncio.CancelledError:
            pass

    stop.set()
    th.join(timeout=10)

    final_cache = get_cache_metrics(URLS)

    with open(os.path.join(out_dir, f"{cell_id}_workflow.jsonl"), "w") as f:
        for r in workflow_results:
            f.write(orjson.dumps(r).decode() + "\n")
    with open(os.path.join(out_dir, f"{cell_id}_request_log.jsonl"), "w") as f:
        for r in request_log:
            f.write(orjson.dumps(r).decode() + "\n")

    # Aggregate
    ttfts = [r["ttft_ms"] for r in request_log if r["ok"] and r["ttft_ms"] > 0]
    tpots = [r["tpot_ms"] for r in request_log if r["ok"] and r["tpot_ms"] > 0]
    decision_us = [r["decision_us"] for r in request_log if r.get("decision_us") is not None]
    wcs = [wf["workflow_completion_ms"] for wf in workflow_results if wf["success"]]

    # Per-instance load stdev (average across cells)
    load_samples = getattr(dispatcher, "_tradeoff_load_samples", [])
    load_stdevs = []
    for sample in load_samples:
        if len(sample) == 4 and any(x > 0 for x in sample):
            load_stdevs.append(stdev(sample))
    avg_load_stdev = mean(load_stdevs) if load_stdevs else 0

    # SLA-success: % of steps with TTFT < 3000 ms
    n_total_steps = len(request_log)
    n_sla_ok = sum(1 for r in request_log if r["ok"] and r["ttft_ms"] > 0 and r["ttft_ms"] < 3000)
    sla_success = n_sla_ok / max(1, n_total_steps)

    # Failure rate
    n_fail = sum(1 for r in request_log if not r["ok"])
    failure_rate = n_fail / max(1, n_total_steps)

    # State sizes
    sizes = []
    f_path = os.path.join(out_dir, "state_updates.jsonl")
    if os.path.exists(f_path):
        with open(f_path) as f:
            for line in f:
                d = json.loads(line)
                for inst, dd in d.get("per_instance", {}).items():
                    sizes.append(dd.get("size_bytes", 0))

    cache_hits = final_cache["hits"] - baseline_cache["hits"]
    cache_queries = final_cache["queries"] - baseline_cache["queries"]
    cache_hit_rate = cache_hits / cache_queries if cache_queries > 0 else 0

    dispatcher.close()

    summary = {
        "cell_id": cell_id,
        "policy": policy,
        "imbalanced": imbalanced,
        "freq_hz": freq_hz,
        "n_workflows": n_workflows,
        "n_steps": n_steps,
        "concurrent": concurrent,
        "duration_s": duration_s,
        "n_state_updates": dispatcher.update_count,
        # Quality vector
        "ttft_p50": percentile(ttfts, 50),
        "ttft_p95": percentile(ttfts, 95),
        "ttft_p99": percentile(ttfts, 99),
        "tpot_p50": percentile(tpots, 50),
        "tpot_p95": percentile(tpots, 95),
        "tpot_p99": percentile(tpots, 99),
        "cache_hit_rate": cache_hit_rate,
        "load_stdev": avg_load_stdev,
        "sla_success_rate": sla_success,
        "workflow_completion_p50": percentile(wcs, 50),
        "workflow_completion_p95": percentile(wcs, 95),
        "workflow_completion_p99": percentile(wcs, 99),
        "n_workflows_succeeded": sum(1 for w in workflow_results if w["success"]),
        "n_total_steps": n_total_steps,
        "n_failed_steps": n_fail,
        "failure_rate": failure_rate,
        # Cost
        "state_size_avg": mean(sizes) if sizes else 0,
        "state_size_p95": percentile(sizes, 95),
        "state_traffic_Bps": (mean(sizes) if sizes else 0) * freq_hz * 4,
        "dispatch_decision_p50": percentile(decision_us, 50),
        "dispatch_decision_p95": percentile(decision_us, 95),
        "dispatch_decision_p99": percentile(decision_us, 99),
    }
    with open(os.path.join(out_dir, "summary.json"), "w") as f:
        json.dump(summary, f, indent=2)
    log.info("policy=%s imbal=%s: cache_hit=%.3f ttft_p95=%.0fms load_stdev=%.2f sla=%.2f%% wf_p95=%.0fms state=%dB disp_p95=%.1fus",
             policy, imbalanced, cache_hit_rate, summary["ttft_p95"],
             avg_load_stdev, sla_success*100, summary["workflow_completion_p95"],
             summary["state_size_p95"], summary["dispatch_decision_p95"])
    return summary


def percentile(xs, p):
    if not xs:
        return 0
    xs = sorted(xs)
    k = max(0, min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1)))))
    return xs[k]


def get_cache_metrics(urls: dict) -> dict:
    hits = 0
    queries = 0
    for inst, url in urls.items():
        try:
            r = requests.get(f"{url}/metrics", timeout=5)
            text = r.text
            for line in text.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "vllm:gpu_prefix_cache_hits_total" in line:
                    try:
                        hits += float(line.rsplit(" ", 1)[-1])
                    except ValueError:
                        pass
                if "vllm:gpu_prefix_cache_queries_total" in line:
                    try:
                        queries += float(line.rsplit(" ", 1)[-1])
                    except ValueError:
                        pass
        except Exception:
            pass
    return {"hits": hits, "queries": queries}


POLICIES_ORDERED = ["round-robin", "coarse", "rich", "sketch"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", default=os.path.expanduser("~/B02/tradeoff_experiments/results_v2/cells"))
    ap.add_argument("--policies", nargs="+", default=POLICIES_ORDERED)
    ap.add_argument("--load-conditions", nargs="+", default=["balanced", "imbalanced"])
    ap.add_argument("--freq-hz", type=float, default=10.0)
    ap.add_argument("--n-workflows", type=int, default=10)
    ap.add_argument("--n-steps", type=int, default=10)
    ap.add_argument("--concurrent", type=int, default=4)
    ap.add_argument("--duration-s", type=float, default=90.0)
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args()

    summaries = []
    t_start = time.time()
    for load_cond in args.load_conditions:
        is_imbal = (load_cond == "imbalanced")
        for policy in args.policies:
            log.info("=" * 60)
            log.info("Running policy=%s load=%s", policy, load_cond)
            log.info("=" * 60)
            try:
                s = asyncio.run(run_cell(
                    policy, args.freq_hz, args.n_workflows,
                    args.n_steps, args.concurrent, args.duration_s,
                    os.path.join(args.out_dir, f"{policy}_{load_cond}"),
                    imbalanced=is_imbal,
                ))
                summaries.append(s)
            except Exception as e:
                log.exception("policy %s / %s failed", policy, load_cond)
                summaries.append({"policy": policy, "load": load_cond, "error": str(e)})
    elapsed = time.time() - t_start
    log.info("All done in %.1f min", elapsed / 60)

    # Summary table
    print("\n" + "=" * 130)
    print(f"{'Policy':<13} {'Load':<11} {'TTFT p50':>9} {'TTFT p95':>9} {'TPOT p95':>9} {'Cache%':>7} {'LoadStd':>8} {'SLA%':>7} {'WfP95':>9} {'State':>6} {'TrfB/s':>9} {'DispP95':>9}")
    print("-" * 130)
    for s in summaries:
        if "error" in s:
            print(f"{s['policy']:<13} {s['load']:<11} ERROR: {s['error']}")
            continue
        print(f"{s['policy']:<13} {'imbalanced' if s['imbalanced'] else 'balanced':<11} "
              f"{s['ttft_p50']:>8.0f}ms {s['ttft_p95']:>8.0f}ms {s['tpot_p95']:>8.2f}ms "
              f"{s['cache_hit_rate']*100:>6.1f}% {s['load_stdev']:>8.2f} "
              f"{s['sla_success_rate']*100:>6.1f}% {s['workflow_completion_p95']:>8.0f}ms "
              f"{s['state_size_p95']:>5d}B {s['state_traffic_Bps']:>8.0f} "
              f"{s['dispatch_decision_p95']:>8.1f}us")
    print("=" * 130)
    with open(os.path.join(args.out_dir, "all_summaries.json"), "w") as f:
        json.dump(summaries, f, indent=2)


if __name__ == "__main__":
    main()