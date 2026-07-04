"""B02 single-cell runner: state collector + workload in ONE process.

This is the cleanest way: the dispatcher's state_views dict is updated in
the same Python process as the workload, so the policy reads fresh state.

Replaces the two-process (dispatcher.py + workloads.py + runner.py) pattern
to fix the cross-process state-visibility bug.

Usage:
    python run_cell.py --cell-id <id> --workload chatbot --state-view coarse \
        --freq-hz 1.0 --rep 1 --warmup-s 30 --duration-s 60
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
from dataclasses import asdict
from typing import Any

import aiohttp
import orjson

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("cell")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dispatcher import (
    Dispatcher, DispatcherConfig, WorkflowRecord,
    build_coarse_view, build_rich_view, build_sketch_view,
    POLICIES,
)
from workloads import MODEL_ID, CHATBOT_PROMPTS, TOOL_NAMES, random_prompt


INSTANCES = ["instance_0", "instance_1", "instance_2", "instance_3"]
URLS = {f"instance_{i}": f"http://127.0.0.1:{8000+i}" for i in range(4)}


# ---------------------------------------------------------------------------
# State collection loop (background task)
# ---------------------------------------------------------------------------

async def state_collector_loop(dispatcher: Dispatcher, stop_event: asyncio.Event):
    """Periodically call dispatcher.collect_once()."""
    period = 1.0 / dispatcher.cfg.update_freq_hz
    log.info("state collector: view=%s freq=%.1fHz", dispatcher.cfg.state_view, dispatcher.cfg.update_freq_hz)
    next_t = time.time()
    while not stop_event.is_set():
        # Off-load to threadpool since requests is sync
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, dispatcher.collect_once)
        next_t += period
        sleep_for = next_t - time.time()
        if sleep_for > 0:
            await asyncio.sleep(sleep_for)
        else:
            next_t = time.time()
    log.info("state collector stopped after %d updates", dispatcher.update_count)


# ---------------------------------------------------------------------------
# Workload loops (chatbot + agentic)
# ---------------------------------------------------------------------------

async def run_chatbot(dispatcher: Dispatcher, n_requests: int, target_rps: float,
                      out_log: str) -> dict:
    """Send n_requests chatbot requests, paced at target_rps."""
    interval = 1.0 / target_rps if target_rps > 0 else 0
    results = []
    sem = asyncio.Semaphore(64)

    async def one_request(session, prompt, wid):
        async with sem:
            arrival = time.time_ns()
            rec = dispatcher.forward({"workflow_id": wid, "type": "chatbot"})
            instance = rec["instance_id"]
            url = dispatcher.cfg.instance_urls[instance]
            vllm_start = time.time_ns()
            body = {
                "model": MODEL_ID,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": random.randint(64, 128),
                "temperature": 0.7,
                "stream": False,
            }
            first_token_ns = 0
            finish_ns = 0
            try:
                async with session.post(
                    f"{url}/v1/chat/completions",
                    json=body,
                    timeout=aiohttp.ClientTimeout(total=120),
                ) as r:
                    first_token_ns = time.time_ns()
                    body_resp = await r.json()
                    finish_ns = time.time_ns()
                    out_tok = body_resp.get("usage", {}).get("completion_tokens", 0) if "usage" in body_resp else 0
                    ok = r.status == 200
                    err = "" if ok else json.dumps(body_resp)[:200]
            except Exception as e:
                finish_ns = time.time_ns()
                ok = False
                out_tok = 0
                err = repr(e)[:200]
            rec2 = {
                "instance_id": instance,
                "success": ok,
                "arrival_time_ns": arrival,
                "vllm_request_start_time_ns": vllm_start,
                "first_token_time_ns": first_token_ns,
                "finish_time_ns": finish_ns,
                "output_tokens": out_tok,
                "workflow_id": wid,
                "policy": rec.get("policy"),
                "decision_time_us": rec.get("decision_time_us"),
                "error": err,
            }
            results.append(rec2)
            dispatcher.request_log.append(rec2)

    async with aiohttp.ClientSession() as session:
        # Pace by completion, not spawn: schedule each task with start_time = i*interval
        loop = asyncio.get_running_loop()
        start_wall = loop.time()
        async def scheduled(i):
            target = start_wall + i * interval
            now = loop.time()
            if target > now:
                await asyncio.sleep(target - now)
            await one_request(session, random_prompt(), f"chat_{i:06d}")
        await asyncio.gather(*[scheduled(i) for i in range(n_requests)], return_exceptions=True)

    with open(out_log, "w") as f:
        for r in results:
            f.write(orjson.dumps(r).decode() + "\n")
    n_ok = sum(1 for r in results if r["success"])
    return {
        "n_total": len(results),
        "n_ok": n_ok,
        "success_rate": n_ok / max(1, len(results)),
        "achieved_rps": len(results) / max(n_requests / max(target_rps, 0.001), 1e-6),
    }


async def run_agentic(dispatcher: Dispatcher, n_workflows: int, step_counts: list[int],
                      tool_delay_ms: int, out_log: str) -> dict:
    workflow_results = []
    sem = asyncio.Semaphore(min(n_workflows, 16))

    async def one_workflow(idx):
        async with sem:
            total_steps = random.choice(step_counts)
            wf_id = f"wf_{idx:05d}"
            wf_start = time.time_ns()
            wf = WorkflowRecord(workflow_id=wf_id, step_id=0, total_steps=total_steps,
                                workflow_start_time_ns=wf_start)
            dispatcher.register_workflow(wf)
            wf_rec = {"workflow_id": wf_id, "total_steps": total_steps,
                      "workflow_start_time_ns": wf_start, "steps": [], "success": True}

            async with aiohttp.ClientSession() as session:
                for step in range(total_steps):
                    disp_start = time.time_ns()
                    rec = dispatcher.forward({"workflow_id": wf_id, "step_id": step,
                                              "type": "agentic_step"})
                    instance = rec["instance_id"]
                    url = dispatcher.cfg.instance_urls[instance]
                    wf.last_assigned_instance = instance
                    wf.assigned_instance_history.append(instance)
                    wf.step_id = step
                    wf.progress = (step + 1) / total_steps

                    # tool sim
                    wf.tool_status = "running"
                    wf.last_tool_name = random.choice(TOOL_NAMES)
                    tool_start = time.time_ns()
                    await asyncio.sleep(tool_delay_ms / 1000.0)
                    tool_end = time.time_ns()
                    wf.last_tool_latency_ms = (tool_end - tool_start) / 1e6
                    wf.tool_status = "done"
                    wf.tool_result_context_size = random.randint(256, 2048)
                    wf.tool_result_context_type = random.choice(["text", "code", "json"])
                    wf.last_step_finish_time_ns = tool_end

                    vllm_start = time.time_ns()
                    body = {
                        "model": MODEL_ID,
                        "messages": [{"role": "user",
                                      "content": f"Step {step+1}/{total_steps}: {random_prompt(64, 128)}"}],
                        "max_tokens": random.randint(48, 96),
                        "temperature": 0.7,
                    }
                    first_token_ns = 0
                    finish_ns = 0
                    out_tok = 0
                    ok = True
                    err = ""
                    try:
                        async with session.post(
                            f"{url}/v1/chat/completions",
                            json=body,
                            timeout=aiohttp.ClientTimeout(total=60),
                        ) as r:
                            first_token_ns = time.time_ns()
                            body_resp = await r.json()
                            finish_ns = time.time_ns()
                            out_tok = body_resp.get("usage", {}).get("completion_tokens", 0) if "usage" in body_resp else 0
                            ok = r.status == 200
                            if not ok:
                                err = json.dumps(body_resp)[:200]
                    except Exception as e:
                        finish_ns = time.time_ns()
                        ok = False
                        err = repr(e)[:200]
                    step_rec = {
                        "step_id": step,
                        "instance_id": instance,
                        "tool_name": wf.last_tool_name,
                        "tool_latency_ms": wf.last_tool_latency_ms,
                        "dispatch_decision_us": rec["decision_time_us"],
                        "vllm_request_start_time_ns": vllm_start,
                        "first_token_time_ns": first_token_ns,
                        "finish_time_ns": finish_ns,
                        "output_tokens": out_tok,
                        "success": ok,
                        "error": err,
                    }
                    wf_rec["steps"].append(step_rec)
                    dispatcher.request_log.append({
                        "instance_id": instance,
                        "workflow_id": wf_id,
                        "step_id": step,
                        "arrival_time_ns": disp_start,
                        "vllm_request_start_time_ns": vllm_start,
                        "first_token_time_ns": first_token_ns,
                        "finish_time_ns": finish_ns,
                        "output_tokens": out_tok,
                        "success": ok,
                        "error": err,
                        "policy": rec["policy"],
                        "decision_time_us": rec["decision_time_us"],
                    })
                    if not ok:
                        wf_rec["success"] = False
                        break
            wf_finish = time.time_ns()
            wf_rec["workflow_finish_time_ns"] = wf_finish
            wf_rec["workflow_completion_ms"] = (wf_finish - wf_start) / 1e6
            workflow_results.append(wf_rec)

    await asyncio.gather(*[asyncio.create_task(one_workflow(i)) for i in range(n_workflows)])
    with open(out_log, "w") as f:
        for r in workflow_results:
            f.write(orjson.dumps(r).decode() + "\n")
    n_ok = sum(1 for r in workflow_results if r["success"])
    return {
        "n_workflows": len(workflow_results),
        "n_ok_workflows": n_ok,
        "success_rate": n_ok / max(1, len(workflow_results)),
    }


# ---------------------------------------------------------------------------
# Main cell driver
# ---------------------------------------------------------------------------

async def run_cell_async(args):
    cell_id = args.cell_id
    cd = args.out_dir
    os.makedirs(cd, exist_ok=True)
    policy = {"none": "round-robin", "coarse": "coarse", "rich": "rich", "sketch": "sketch"}[args.state_view]
    cfg = DispatcherConfig(
        instances=INSTANCES,
        instance_urls=URLS,
        state_view=args.state_view,
        update_freq_hz=args.freq_hz,
        duration_s=args.duration_s,
        out_dir=cd,
        cell_id=cell_id,
        workload=args.workload,
        rep=args.rep,
        policy=policy,
    )
    dispatcher = Dispatcher(cfg)
    log.info("cell=%s view=%s freq=%gHz workload=%s policy=%s",
             cell_id, args.state_view, args.freq_hz, args.workload, policy)

    # ---- Warmup phase (no measurement) ----
    log.info("WARMUP: %ds", args.warmup_s)
    stop_warmup = asyncio.Event()
    coll_task = asyncio.create_task(state_collector_loop(dispatcher, stop_warmup))
    if args.workload == "chatbot":
        warmup_summary = await run_chatbot(
            dispatcher,
            n_requests=int(10 * args.warmup_s),
            target_rps=10,
            out_log=os.path.join(cd, f"{cell_id}_warmup_chatbot.jsonl"),
        )
    else:
        warmup_summary = await run_agentic(
            dispatcher,
            n_workflows=max(2, int(0.3 * args.warmup_s)),
            step_counts=[4, 8],
            tool_delay_ms=200,
            out_log=os.path.join(cd, f"{cell_id}_warmup_workflow.jsonl"),
        )
    log.info("warmup summary: %s", warmup_summary)

    # ---- Measurement phase ----
    # Clear request log so latency_summary is computed only for measurement window
    dispatcher.request_log.clear()
    log.info("MEASUREMENT: %ds", args.duration_s)
    t_meas_start = time.time()
    if args.workload == "chatbot":
        measure_summary = await run_chatbot(
            dispatcher,
            n_requests=int(10 * args.duration_s),
            target_rps=10,
            out_log=os.path.join(cd, f"{cell_id}_chatbot.jsonl"),
        )
    else:
        measure_summary = await run_agentic(
            dispatcher,
            n_workflows=args.n_workflows,
            step_counts=[4, 8, 16],
            tool_delay_ms=200,
            out_log=os.path.join(cd, f"{cell_id}_workflow.jsonl"),
        )
    t_meas_elapsed = time.time() - t_meas_start
    log.info("measurement summary: %s elapsed=%.1fs", measure_summary, t_meas_elapsed)

    # Stop collector
    stop_warmup.set()
    await coll_task

    # Write cell summary
    cell_summary = {
        "cell_id": cell_id,
        "workload": args.workload,
        "state_view": args.state_view,
        "freq_hz": args.freq_hz,
        "rep": args.rep,
        "policy": policy,
        "warmup_s": args.warmup_s,
        "duration_s": args.duration_s,
        "warmup_summary": warmup_summary,
        "measurement_summary": measure_summary,
        "actual_measurement_elapsed_s": t_meas_elapsed,
        "state_updates_collected": dispatcher.update_count,
        "n_workflows_in_table": len(dispatcher.workflow_table),
    }
    with open(os.path.join(cd, "summary.json"), "w") as f:
        json.dump(cell_summary, f, indent=2)
    dispatcher.close()
    log.info("CELL %s DONE", cell_id)
    return cell_summary


def run_cell(args):
    return asyncio.run(run_cell_async(args))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--cell-id", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--workload", choices=["chatbot", "agentic"], required=True)
    ap.add_argument("--state-view", choices=["none", "coarse", "rich", "sketch"], required=True)
    ap.add_argument("--freq-hz", type=float, required=True)
    ap.add_argument("--rep", type=int, default=1)
    ap.add_argument("--warmup-s", type=float, default=30)
    ap.add_argument("--duration-s", type=float, default=60)
    ap.add_argument("--n-workflows", type=int, default=40)
    args = ap.parse_args()
    run_cell(args)