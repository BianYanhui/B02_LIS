"""B02 Quick Trade-off Test.

For each of 4 dispatch policies, run a long-prefix agentic workload for ~3 min
and report:
  - cache hit rate (vllm gpu_prefix_cache_hits / queries)  <- DISPATCH QUALITY
  - state view size p95 (bytes)                            <- STATE COST
  - dispatch decision p95 (us)                             <- DECISION COST
  - workflow completion p95 (ms)                           <- E2E QUALITY

All other knobs held constant (10 Hz, 1 rep, 12 concurrent workflows, 16 steps each).

Output: prints a summary table at the end. Also writes JSON to results/.
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
from dataclasses import asdict
from statistics import mean, median
from typing import Any

import aiohttp
import orjson
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("tradeoff")

# Reuse the original dispatcher + workloads
sys.path.insert(0, os.path.expanduser("~/B02/experiments/scripts"))
from dispatcher import (
    Dispatcher, DispatcherConfig, WorkflowRecord,
    build_coarse_view, build_rich_view, build_sketch_view,
    parse_vllm_metrics, POLICIES,
)
from workloads import MODEL_ID, TOOL_NAMES, random_prompt  # noqa

INSTANCES = ["instance_0", "instance_1", "instance_2", "instance_3"]
URLS = {f"instance_{i}": f"http://127.0.0.1:{8000+i}" for i in range(4)}


# ---------------------------------------------------------------------------
# Long-prefix system prompt (designed for prefix caching)
# ---------------------------------------------------------------------------

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

The AI is currently helping with a multi-step reasoning task. Each step builds on the
previous steps' context. The user is methodical and patient, so the AI can take time to
reason carefully without rushing.

Remember: the goal is to be correct, not to be fast. Take the time you need.
""" * 2  # ~500 tokens

CHAT_HISTORY_PREFIX = """User: I am working through a multi-step problem. Please help
me reason carefully through each step. The context is the same across all steps, so you
don't need to ask for clarification — just continue from where the previous step left off.

Assistant: Understood. I will treat each step as part of one continuous reasoning
process, building on the prior context.

User: Here is the setup for the task I am working on. There are several subtasks that
need to be solved in order. The first subtask introduces variables and constraints, the
second requires computing intermediate values, the third asks for an analysis, and the
final subtask asks for a summary. I will provide the actual question for each step
separately. Please answer in a clear, structured way.

Assistant: Got it. I am ready to receive the first step's question. I will answer each
step in turn, carrying forward the reasoning.

""" * 3  # ~400 tokens of "dialogue" so the prefix has multiple distinct sections


# ---------------------------------------------------------------------------
# Per-cell run
# ---------------------------------------------------------------------------

async def run_tradeoff_cell(policy: str, freq_hz: float, n_workflows: int,
                             n_steps: int, concurrent: int,
                             duration_s: float, out_dir: str) -> dict:
    """Run one cell: state collector + long-prefix agentic workload, in one process."""
    os.makedirs(out_dir, exist_ok=True)
    cell_id = f"{policy}_f{freq_hz:g}_w{n_workflows}_s{n_steps}_c{concurrent}"

    cfg = DispatcherConfig(
        instances=INSTANCES,
        instance_urls=URLS,
        state_view="rich" if policy != "round-robin" else "coarse",
        # state_view only matters for serialization; for dispatch we use policy directly
        update_freq_hz=freq_hz,
        duration_s=duration_s,
        out_dir=out_dir,
        cell_id=cell_id,
        workload="agentic",
        rep=1,
        policy=policy,
    )
    dispatcher = Dispatcher(cfg)

    # Override policy if needed (we use the same 4 policies)
    # Pre-load the vllm cache metrics baseline
    baseline_cache = get_cache_metrics(URLS)

    # Background: state collector
    stop = asyncio.Event()

    def collect_loop():
        next_t = time.time()
        period = 1.0 / freq_hz
        while not stop.is_set():
            dispatcher.collect_once()
            next_t += period
            sleep_for = next_t - time.time()
            if sleep_for > 0:
                time.sleep(sleep_for)
            else:
                next_t = time.time()

    coll_thread = asyncio.get_event_loop()
    import threading
    th = threading.Thread(target=collect_loop, daemon=True)
    th.start()

    # Workload: long-prefix agentic with N concurrent workflows
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
                tool_s = time.time_ns()
                await asyncio.sleep(0.05)  # 50ms tool sim
                tool_e = time.time_ns()
                wf.last_tool_latency_ms = (tool_e - tool_s) / 1e6
                wf.tool_status = "done"

                # Construct long-prefix prompt
                messages = [
                    {"role": "system", "content": LONG_SYSTEM_PROMPT},
                    {"role": "user", "content": CHAT_HISTORY_PREFIX + f"Step {step+1}/{n_steps}: {random_prompt(64, 96)}"},
                ]
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
                              "max_tokens": 32, "temperature": 0.3, "stream": False},
                        timeout=aiohttp.ClientTimeout(total=60),
                    ) as r:
                        first_token_ns = time.time_ns()
                        body = await r.json()
                        finish_ns = time.time_ns()
                        out_tok = body.get("usage", {}).get("completion_tokens", 0)
                        ok = r.status == 200
                        if not ok:
                            err = json.dumps(body)[:200]
                except Exception as e:
                    finish_ns = time.time_ns()
                    ok = False
                    err = repr(e)[:200]

                step_rec = {
                    "step_id": step, "instance_id": instance,
                    "tool_name": wf.last_tool_name, "tool_latency_ms": wf.last_tool_latency_ms,
                    "decision_us": decision_us,
                    "vllm_start_ns": vllm_start, "first_token_ns": first_token_ns,
                    "finish_ns": finish_ns, "output_tokens": out_tok,
                    "success": ok, "error": err,
                }
                wf_rec["steps"].append(step_rec)
                request_log.append({
                    "instance_id": instance, "workflow_id": wf_id, "step_id": step,
                    "arrival_ns": t0, "vllm_start_ns": vllm_start,
                    "first_token_ns": first_token_ns, "finish_ns": finish_ns,
                    "output_tokens": out_tok, "success": ok,
                    "policy": policy, "decision_us": decision_us,
                })
                if not ok:
                    wf_rec["success"] = False
                    break
        wf_finish = time.time_ns()
        wf_rec["workflow_finish_ns"] = wf_finish
        wf_rec["workflow_completion_ms"] = (wf_finish - wf_start) / 1e6
        workflow_results.append(wf_rec)

    # Run workflows concurrently
    sem = asyncio.Semaphore(concurrent)
    async def run_with_sem(idx):
        async with sem:
            await one_workflow(idx)
    await asyncio.gather(*[asyncio.create_task(run_with_sem(i)) for i in range(n_workflows)])

    # Stop collector
    stop.set()
    th.join(timeout=5)

    # Final cache metrics
    final_cache = get_cache_metrics(URLS)

    # Save raw
    with open(os.path.join(out_dir, f"{cell_id}_workflow.jsonl"), "w") as f:
        for r in workflow_results:
            f.write(orjson.dumps(r).decode() + "\n")
    with open(os.path.join(out_dir, f"{cell_id}_request_log.jsonl"), "w") as f:
        for r in request_log:
            f.write(orjson.dumps(r).decode() + "\n")

    # Aggregate
    ttfts = []
    rls = []
    decision_us = []
    wcs = []
    sizes = []
    for u in dispatcher.state_updates.values() if dispatcher.state_updates else []:
        pass
    # recompute sizes from collected
    # use state_updates recorded in self.f_state
    f_path = os.path.join(out_dir, "state_updates.jsonl")
    if os.path.exists(f_path):
        with open(f_path) as f:
            for line in f:
                d = json.loads(line)
                for inst, dd in d.get("per_instance", {}).items():
                    sizes.append(dd.get("size_bytes", 0))
    for r in request_log:
        if not r["success"]:
            continue
        if r["first_token_ns"] and r["vllm_start_ns"]:
            ttfts.append((r["first_token_ns"] - r["vllm_start_ns"]) / 1e6)
        if r["finish_ns"] and r["vllm_start_ns"]:
            rls.append((r["finish_ns"] - r["vllm_start_ns"]) / 1e6)
        if r["decision_us"] is not None:
            decision_us.append(r["decision_us"])
    for wf in workflow_results:
        if wf["success"]:
            wcs.append(wf["workflow_completion_ms"])

    cache_hits = final_cache["hits"] - baseline_cache["hits"]
    cache_queries = final_cache["queries"] - baseline_cache["queries"]
    cache_hit_rate = cache_hits / cache_queries if cache_queries > 0 else 0

    dispatcher.close()

    summary = {
        "cell_id": cell_id,
        "policy": policy,
        "freq_hz": freq_hz,
        "n_workflows": n_workflows,
        "n_steps": n_steps,
        "concurrent": concurrent,
        "duration_s": duration_s,
        "n_state_updates": dispatcher.update_count,
        # Quality
        "cache_hits_delta": cache_hits,
        "cache_queries_delta": cache_queries,
        "cache_hit_rate": cache_hit_rate,
        "ttft_p50": percentile(ttfts, 50),
        "ttft_p95": percentile(ttfts, 95),
        "ttft_p99": percentile(ttfts, 99),
        "request_latency_p50": percentile(rls, 50),
        "request_latency_p95": percentile(rls, 95),
        "request_latency_p99": percentile(rls, 99),
        "workflow_completion_p50": percentile(wcs, 50),
        "workflow_completion_p95": percentile(wcs, 95),
        "workflow_completion_p99": percentile(wcs, 99),
        "n_workflows_succeeded": sum(1 for w in workflow_results if w["success"]),
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
    return summary


def percentile(xs, p):
    if not xs:
        return 0
    xs = sorted(xs)
    k = max(0, min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1)))))
    return xs[k]


def get_cache_metrics(urls: dict) -> dict:
    """Snapshot vllm prefix cache hits and queries across all instances."""
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
                if "vllm:gpu_prefix_cache_hits_total" in line and not line.endswith("0"):
                    try:
                        v = float(line.rsplit(" ", 1)[-1])
                        hits += v
                    except ValueError:
                        pass
                if "vllm:gpu_prefix_cache_queries_total" in line and not line.endswith("0"):
                    try:
                        v = float(line.rsplit(" ", 1)[-1])
                        queries += v
                    except ValueError:
                        pass
        except Exception:
            pass
    return {"hits": hits, "queries": queries}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

POLICIES_ORDERED = ["round-robin", "coarse", "rich", "sketch"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", default=os.path.expanduser("~/B02/tradeoff_experiments/results/cells"))
    ap.add_argument("--policies", nargs="+", default=POLICIES_ORDERED)
    ap.add_argument("--freq-hz", type=float, default=10.0)
    ap.add_argument("--n-workflows", type=int, default=12)
    ap.add_argument("--n-steps", type=int, default=16)
    ap.add_argument("--concurrent", type=int, default=4)
    ap.add_argument("--duration-s", type=float, default=180.0)
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args()

    if not args.all and len(args.policies) == 1:
        # single-cell mode
        s = asyncio.run(run_tradeoff_cell(
            args.policies[0], args.freq_hz, args.n_workflows,
            args.n_steps, args.concurrent, args.duration_s,
            os.path.join(args.out_dir, args.policies[0]),
        ))
        print(json.dumps(s, indent=2))
        return

    # Run all policies back-to-back
    summaries = []
    t_start = time.time()
    for policy in args.policies:
        log.info("=" * 60)
        log.info("Running policy: %s", policy)
        log.info("=" * 60)
        try:
            s = asyncio.run(run_tradeoff_cell(
                policy, args.freq_hz, args.n_workflows,
                args.n_steps, args.concurrent, args.duration_s,
                os.path.join(args.out_dir, policy),
            ))
            summaries.append(s)
            log.info("policy=%s: cache_hit_rate=%.3f ttft_p95=%.0fms state_size_p95=%dB dispatch_p95=%.1fus",
                     policy, s["cache_hit_rate"], s["ttft_p95"],
                     s["state_size_p95"], s["dispatch_decision_p95"])
        except Exception as e:
            log.exception("policy %s failed", policy)
            summaries.append({"policy": policy, "error": str(e)})
    elapsed = time.time() - t_start
    log.info("All done in %.1f min", elapsed / 60)

    # Print summary table
    print("\n" + "=" * 100)
    print(f"{'Policy':<15} {'Cache hit':>10} {'TTFT p95':>10} {'WfCompl p95':>12} {'State size p95':>15} {'Traffic B/s':>12} {'DispDec p95':>12}")
    print("-" * 100)
    for s in summaries:
        if "error" in s:
            print(f"{s['policy']:<15} ERROR: {s['error']}")
            continue
        print(f"{s['policy']:<15} {s['cache_hit_rate']*100:>9.1f}% {s['ttft_p95']:>9.0f}ms {s['workflow_completion_p95']:>11.0f}ms {s['state_size_p95']:>14d} {s['state_traffic_Bps']:>11.0f} {s['dispatch_decision_p95']:>11.1f}us")
    print("=" * 100)
    # Save to JSON
    with open(os.path.join(args.out_dir, "all_summaries.json"), "w") as f:
        json.dump(summaries, f, indent=2)


if __name__ == "__main__":
    main()