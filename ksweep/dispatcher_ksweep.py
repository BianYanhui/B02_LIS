"""K-sweep dispatcher for Experiment 1.

The K parameter: how many affinity entries per workflow the Instance advertises.
We cap `WorkflowRecord.assigned_instance_history` at K entries.

This is a more practical implementation than the full utility-ranked top-K:
we use the most recent K instances, not utility-ranked.

Policies tested:
  - coarse (baseline)
  - sketch_K=2, K=4, K=8, K=16, K=32, K=full (≈ Rich)
  - rich (full, for upper bound)
"""
from __future__ import annotations

import os, sys, time, random, json, asyncio, logging
from collections import defaultdict
from dataclasses import dataclass, field
from statistics import mean, stdev
from typing import Any

import aiohttp
import orjson
import requests

sys.path.insert(0, "/home/byh/B02/experiments/scripts")
sys.path.insert(0, "/home/byh/B02/tradeoff_experiments/scripts")
from workloads import MODEL_ID, TOOL_NAMES, random_prompt

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ksweep")

N_INSTANCES = 8
INSTANCES = [f"instance_{i}" for i in range(N_INSTANCES)]
URLS = {f"instance_{i}": f"http://127.0.0.1:{8000+i}" for i in range(N_INSTANCES)}


@dataclass
class WorkflowRecord:
    workflow_id: str
    step_id: int = 0
    total_steps: int = 0
    progress: float = 0.0
    last_assigned_instance: str = ""
    assigned_instance_history: list = field(default_factory=list)  # capped at K
    tool_status: str = "idle"
    last_tool_name: str = ""
    last_tool_latency_ms: float = 0.0


def cap_history(history: list, K: int) -> list:
    """Keep only the last K entries (most recent)."""
    if K is None or K <= 0 or len(history) <= K:
        return history
    return history[-K:]


async def streaming_request(session, url, messages, max_tokens=24, timeout_s=30):
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
            timeout=aiohttp.ClientTimeout(total=timeout_s),
        ) as r:
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
    except Exception as e:
        ok = False
        err = repr(e)[:200]
    finish_ns = last_token_ns or first_token_ns or time.time_ns()
    return {"ok": ok, "err": err,
            "vllm_start_ns": vllm_start, "first_token_ns": first_token_ns,
            "finish_ns": finish_ns, "chunk_count": chunk_count}


def pick_instance(policy: str, K, workflow_table, instance_metrics):
    """Policy-based instance selection.

    K controls how many history entries the sketch policy uses.
    """
    if policy == "coarse":
        return min(INSTANCES, key=lambda i: instance_metrics.get(i, {}).get("num_requests_running", 0))
    if policy == "rich":
        # Uses full history
        best = min(INSTANCES, key=lambda i: (
            instance_metrics.get(i, {}).get("num_requests_running", 0) * 1.0 +
            instance_metrics.get(i, {}).get("num_requests_waiting", 0) * 1.0 +
            instance_metrics.get(i, {}).get("kv_cache_usage_perc", 0) * 0.3))
        return best
    if policy.startswith("sketch_K"):
        # K = number of history entries
        score = {}
        for i, inst in enumerate(INSTANCES):
            m = instance_metrics.get(inst, {})
            base = m.get("num_requests_running", 0) + m.get("num_requests_waiting", 0)
            score[inst] = base
        # Apply affinity bonus from all workflows' history (capped at K)
        affinity = [0] * N_INSTANCES
        for wf_id, wf in workflow_table.items():
            history = cap_history(wf.assigned_instance_history, K)
            for h in history:
                try:
                    idx = int(h.split("_")[-1])
                    if 0 <= idx < N_INSTANCES:
                        affinity[idx] += 1
                except (ValueError, IndexError):
                    pass
        for i, inst in enumerate(INSTANCES):
            score[inst] = score[inst] - 10.0 * affinity[i] / 10.0
        return min(score.items(), key=lambda x: x[1])[0]
    raise ValueError(f"unknown policy: {policy}")


async def collect_metrics(session, urls):
    """Hit /metrics on all 8 instances in parallel."""
    async def _get(s, url):
        try:
            async with s.get(f"{url}/metrics", timeout=aiohttp.ClientTimeout(total=5)) as r:
                if r.status != 200: return None
                text = await r.text()
            return parse_metrics(text)
        except Exception:
            return None
    results = await asyncio.gather(*[_get(session, url) for inst, url in urls.items()])
    return dict(zip(urls.keys(), results))


def parse_metrics(text):
    """Minimal Prometheus parser for vllm metrics we care about."""
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
        if name.endswith("_total"): name = name[:-6]
        if name in ("num_requests_waiting", "num_requests_running",
                    "kv_cache_usage_perc", "gpu_cache_usage_perc",
                    "prefix_cache_hits_total", "prefix_cache_queries_total",
                    "prompt_tokens_total", "generation_tokens_total",
                    "request_success_total", "num_preemptions_total"):
            out[name] = out.get(name, 0) + val
    return out


async def run_ksweep_cell(policy: str, K, concurrent: int, duration_s: int,
                         n_workflows: int = 12, n_steps: int = 8,
                         ctx_tokens: int = 1024, tool_delay_ms: int = 200,
                         freq_hz: float = 10, rep: int = 1):
    """Run one K-sweep cell."""
    workflow_table = {}
    for i in range(n_workflows):
        wf = WorkflowRecord(
            workflow_id=f"wf_{i:04d}_r{rep}",
            step_id=0, total_steps=n_steps, progress=0.0,
        )
        workflow_table[wf.workflow_id] = wf

    end_t = time.time() + duration_s
    period = 1.0 / freq_hz
    next_t = time.time()
    next_workload_t = time.time()
    workload_period = 1.0 / max(concurrent, 0.1)
    wf_ids = list(workflow_table.keys())
    wf_idx = 0

    request_log = []
    cross_inst_switches = 0
    total_steps_count = 0
    state_collects = []
    decision_us_list = []

    # Prefix shared across workflows to maximize cache signal
    prefix = "You are a careful assistant. " * (ctx_tokens * 4 // 30)

    async with aiohttp.ClientSession() as session:
        while time.time() < end_t:
            now = time.time()
            if now >= next_t:
                # State collection
                t0 = time.perf_counter_ns()
                m = await collect_metrics(session, URLS)
                state_collects.append((time.perf_counter_ns() - t0) / 1e3)
                next_t = now + period
            if now >= next_workload_t:
                wf_id = wf_ids[wf_idx % len(wf_ids)]
                wf_idx += 1
                t_dispatch0 = time.perf_counter_ns()
                chosen = pick_instance(policy, K, workflow_table, m)
                decision_us = (time.perf_counter_ns() - t_dispatch0) / 1e3
                decision_us_list.append(decision_us)
                wf = workflow_table[wf_id]
                if step_count := sum(1 for s in wf.assigned_instance_history):
                    total_steps_count += 1
                    if wf.assigned_instance_history[-1] != chosen:
                        cross_inst_switches += 1
                wf.assigned_instance_history.append(chosen)
                wf.last_assigned_instance = chosen
                wf.step_id = (wf.step_id + 1) % wf.total_steps
                url = URLS[chosen]
                messages = [
                    {"role": "system", "content": prefix},
                    {"role": "user", "content": f"Step {wf.step_id+1}/{wf.total_steps}: {random_prompt(40, 80)}"},
                ]
                res = await streaming_request(session, url, messages, max_tokens=24)
                res["dispatch_us"] = decision_us
                res["instance_id"] = chosen
                res["policy"] = policy
                res["K"] = K
                res["workflow_id"] = wf_id
                request_log.append(res)
                next_workload_t = now + workload_period
            else:
                await asyncio.sleep(0.001)

    # Aggregate
    n_dispatch = len(request_log)
    n_success = sum(1 for r in request_log if r["ok"])
    ttfts = [r["first_token_ns"] - r["vllm_start_ns"] for r in request_log
             if r["ok"] and r["first_token_ns"] > 0]
    p50_ttft = percentile(ttfts, 50) / 1e6 if ttfts else 0
    p95_ttft = percentile(ttfts, 95) / 1e6 if ttfts else 0
    p99_ttft = percentile(ttfts, 99) / 1e6 if ttfts else 0
    same_inst = 1.0 - (cross_inst_switches / max(1, total_steps_count))
    success_rate = n_success / max(1, n_dispatch)
    return {
        "policy": policy, "K": K, "concurrent": concurrent, "rep": rep,
        "n_dispatch": n_dispatch, "n_success": n_success, "success_rate": success_rate,
        "ttft_p50_ms": p50_ttft, "ttft_p95_ms": p95_ttft, "ttft_p99_ms": p99_ttft,
        "same_inst_step_ratio": same_inst,
        "decision_p50_us": percentile(decision_us_list, 50),
        "decision_p95_us": percentile(decision_us_list, 95),
        "state_collect_p50_us": percentile(state_collects, 50),
        "state_collect_p95_us": percentile(state_collects, 95),
    }


def percentile(xs, p):
    if not xs: return 0
    xs = sorted(xs)
    return xs[max(0, min(len(xs)-1, int(round(p/100*(len(xs)-1)))))]


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--K-values", nargs="+", type=int,
                    default=[2, 4, 8, 16, 32, 0],
                    help="Sketch K values. 0 means full (≈ Rich)")
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--duration-s", type=int, default=60)
    ap.add_argument("--concurrent", type=int, default=4)
    ap.add_argument("--out-dir", type=str, default="/home/byh/B02/ksweep/results")
    args = ap.parse_args()

    os.makedirs(args.out_dir, exist_ok=True)
    os.makedirs(f"{args.out_dir}/cells", exist_ok=True)
    log.info("K-sweep: K=%s, reps=%d, duration=%ds, concurrent=%d",
             args.K_values, args.reps, args.duration_s, args.concurrent)

    K_values = args.K_values  # 0 = full
    # Build cells
    cells_to_run = []
    # Always include coarse and rich as baselines
    for policy in ["coarse", "rich"]:
        for rep in range(1, args.reps + 1):
            cells_to_run.append({"policy": policy, "K": None, "rep": rep})

    for K in K_values:
        if K == 0:
            policy = "sketch_K"
            K_for_run = None  # full
        else:
            policy = f"sketch_K"
            K_for_run = K
        for rep in range(1, args.reps + 1):
            cells_to_run.append({"policy": "sketch_K", "K": K_for_run, "rep": rep})

    log.info("Total cells to run: %d", len(cells_to_run))
    summaries = []
    t_start = time.time()
    for i, c in enumerate(cells_to_run, 1):
        cell_id = f"{c['policy']}_K{c['K']}_r{c['rep']}"
        log.info("[%d/%d] cell=%s", i, len(cells_to_run), cell_id)
        try:
            s = asyncio.run(run_ksweep_cell(
                policy=c["policy"], K=c["K"], concurrent=args.concurrent,
                duration_s=args.duration_s, rep=c["rep"]))
            s["cell_id"] = cell_id
            s["K_for_run"] = c["K"]
            # Save summary
            with open(f"{args.out_dir}/cells/{cell_id}.json", "w") as f:
                json.dump(s, f, indent=2)
            summaries.append(s)
            log.info("cell %s: success=%.1f%% ttft_p95=%.0fms same_inst=%.2f",
                     cell_id, s["success_rate"]*100, s["ttft_p95_ms"], s["same_inst_step_ratio"])
        except Exception as e:
            log.exception("cell %s failed", cell_id)
            summaries.append({"cell_id": cell_id, "error": str(e)})
        log.info("progress: %d/%d, %.1f min", i, len(cells_to_run),
                  (time.time() - t_start) / 60)

    with open(f"{args.out_dir}/all_summaries.json", "w") as f:
        json.dump(summaries, f, indent=2)
    log.info("All done in %.1f min", (time.time() - t_start) / 60)


if __name__ == "__main__":
    main()