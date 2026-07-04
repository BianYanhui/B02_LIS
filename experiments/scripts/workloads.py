"""Workload generators for B02 Motivation Experiment.

Two modes:
- chatbot: independent chat requests
- agentic: multi-step workflows with tool simulation

Both use asyncio + aiohttp to send concurrent requests through the dispatcher
which forwards to the configured vLLM instance.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import random
import time
from dataclasses import dataclass

import aiohttp
import orjson

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("workload")


# ---------------------------------------------------------------------------
# Sample prompts (deterministic)
# ---------------------------------------------------------------------------

CHATBOT_PROMPTS = [
    "Explain the difference between TCP and UDP in 2 sentences.",
    "What is the capital of France?",
    "Write a haiku about the moon.",
    "Define polymorphism in OOP.",
    "What year did World War II end?",
    "Convert 100 Fahrenheit to Celsius.",
    "Name three primary colors.",
    "Who wrote Hamlet?",
    "What is photosynthesis?",
    "Calculate 15% of 240.",
    "List the planets in our solar system.",
    "What does CPU stand for?",
    "Define machine learning in one sentence.",
    "Who painted the Mona Lisa?",
    "What is the speed of light?",
    "Summarize the plot of Romeo and Juliet briefly.",
    "What is the largest ocean?",
    "Define quantum entanglement.",
    "Name a famous Greek philosopher.",
    "What is DNA?",
    "Translate 'hello' to Spanish.",
    "What is the boiling point of water in Celsius?",
    "Define supply and demand.",
    "Who invented the telephone?",
    "What is cloud computing?",
]

TOOL_NAMES = ["search", "calculator", "code_exec", "db_query", "file_read"]


def random_prompt(min_tokens: int = 128, max_tokens: int = 256) -> str:
    """Build a prompt roughly min_tokens..max_tokens tokens long."""
    n_words = random.randint(min_tokens, max_tokens)
    base = random.choice(CHATBOT_PROMPTS)
    # Repeat base to approximate word count (rough: 1 word ≈ 1.3 tokens)
    target_words = int(n_words / 1.3)
    repeats = max(1, target_words // max(1, len(base.split())))
    return (" " + base) * repeats


# ---------------------------------------------------------------------------
# Dispatcher client (talks to dispatcher via in-process call)
# ---------------------------------------------------------------------------

@dataclass
class DispatchResult:
    instance_id: str
    decision_time_us: float
    success: bool
    ttft_ms: float
    tpot_ms: float
    request_latency_ms: float
    output_tokens: int
    arrival_time_ns: int
    dispatch_start_time_ns: int
    vllm_request_start_time_ns: int
    first_token_time_ns: int
    finish_time_ns: int
    error: str = ""


# ---------------------------------------------------------------------------
# Chatbot workload
# ---------------------------------------------------------------------------

async def run_chatbot(
    dispatcher,
    n_requests: int,
    target_rps: float,
    duration_s: float,
    out_log: str,
    out_request_log: str,
) -> dict:
    """Send n_requests chat requests, paced to target_rps.

    Returns a small summary dict.
    """
    log.info("chatbot: n=%d rps=%.1f dur=%.1fs", n_requests, target_rps, duration_s)
    interval = 1.0 / target_rps if target_rps > 0 else 0
    results: list[DispatchResult] = []
    start = time.time()

    async def one_request(session: aiohttp.ClientSession, prompt: str, wid: str):
        arrival = time.time_ns()
        # Dispatcher picks instance
        rec = dispatcher.forward({"workflow_id": wid, "type": "chatbot"})
        instance = rec["instance_id"]
        url = dispatcher.cfg.instance_urls[instance]
        # Make the actual call to vLLM
        vllm_start = time.time_ns()
        body = {
            "model": url,  # vLLM uses model name as registered id; but OpenAI API ignores this
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
                first_token_ns = time.time_ns()  # OpenAI returns all at once, so TTFT ≈ e2e
                body_resp = await r.json()
                finish_ns = time.time_ns()
                if "usage" in body_resp:
                    out_tok = body_resp["usage"].get("completion_tokens", 0)
                else:
                    out_tok = 0
                ok = r.status == 200
                err = "" if ok else json.dumps(body_resp)[:200]
        except Exception as e:
            finish_ns = time.time_ns()
            ok = False
            out_tok = 0
            err = repr(e)[:200]
            body_resp = {}

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
        sem = asyncio.Semaphore(64)  # bound concurrent in-flight

        async def paced(idx: int):
            async with sem:
                prompt = random_prompt()
                wid = f"chat_{idx:06d}"
                await one_request(session, prompt, wid)
                if interval > 0:
                    await asyncio.sleep(interval)

        tasks = [paced(i) for i in range(n_requests)]
        await asyncio.gather(*tasks)

    elapsed = time.time() - start
    # write logs
    with open(out_log, "w") as f:
        for r in results:
            f.write(orjson.dumps(r).decode() + "\n")
    with open(out_request_log, "w") as f:
        for r in results:
            f.write(orjson.dumps(r).decode() + "\n")

    n_ok = sum(1 for r in results if r["success"])
    summary = {
        "n_total": len(results),
        "n_ok": n_ok,
        "success_rate": n_ok / max(1, len(results)),
        "elapsed_s": elapsed,
        "achieved_rps": len(results) / max(elapsed, 1e-6),
    }
    log.info("chatbot done: %s", summary)
    return summary


# ---------------------------------------------------------------------------
# Agentic workload
# ---------------------------------------------------------------------------

async def run_agentic(
    dispatcher,
    n_workflows: int,
    step_counts: list[int],
    tool_delay_ms: int,
    duration_s: float,
    out_workflow_log: str,
    out_request_log: str,
) -> dict:
    """Run n_workflows workflows. Step count randomly picked from step_counts per workflow."""
    log.info("agentic: n_wf=%d steps=%s tool_delay=%dms", n_workflows, step_counts, tool_delay_ms)
    workflow_results: list[dict] = []

    async def one_workflow(wf_idx: int):
        total_steps = random.choice(step_counts)
        wf_id = f"wf_{wf_idx:05d}"
        wf_start = time.time_ns()
        wf_rec = {
            "workflow_id": wf_id,
            "total_steps": total_steps,
            "workflow_start_time_ns": wf_start,
            "steps": [],
            "success": True,
        }
        # register workflow
        from dispatcher import WorkflowRecord
        wf = WorkflowRecord(
            workflow_id=wf_id,
            step_id=0,
            total_steps=total_steps,
            workflow_start_time_ns=wf_start,
        )
        dispatcher.register_workflow(wf)

        async with aiohttp.ClientSession() as session:
            for step in range(total_steps):
                # step dispatch
                disp_start = time.time_ns()
                rec = dispatcher.forward({"workflow_id": wf_id, "step_id": step, "type": "agentic_step"})
                instance = rec["instance_id"]
                url = dispatcher.cfg.instance_urls[instance]
                # update workflow last_assigned
                wf.last_assigned_instance = instance
                wf.assigned_instance_history.append(instance)
                wf.step_id = step
                wf.progress = (step + 1) / total_steps

                # tool sim (running)
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

                # call vLLM for the step generation
                vllm_start = time.time_ns()
                body = {
                    "messages": [
                        {"role": "user", "content": f"Step {step+1}/{total_steps}: {random_prompt(64, 128)}"}
                    ],
                    "max_tokens": random.randint(48, 96),
                    "temperature": 0.7,
                }
                first_token_ns = 0
                finish_ns = 0
                ok = True
                err = ""
                out_tok = 0
                try:
                    async with session.post(
                        f"{url}/v1/chat/completions",
                        json=body,
                        timeout=aiohttp.ClientTimeout(total=60),
                    ) as r:
                        first_token_ns = time.time_ns()
                        body_resp = await r.json()
                        finish_ns = time.time_ns()
                        if "usage" in body_resp:
                            out_tok = body_resp["usage"].get("completion_tokens", 0)
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

    # Run all workflows concurrently
    sem = asyncio.Semaphore(min(n_workflows, 16))  # cap to avoid OOM
    async def run_with_sem(idx):
        async with sem:
            await one_workflow(idx)

    start = time.time()
    await asyncio.gather(*[run_with_sem(i) for i in range(n_workflows)])
    elapsed = time.time() - start

    n_ok = sum(1 for r in workflow_results if r["success"])
    # write logs
    with open(out_workflow_log, "w") as f:
        for r in workflow_results:
            f.write(orjson.dumps(r).decode() + "\n")
    with open(out_request_log, "a") as f:
        # already appended in line
        pass

    summary = {
        "n_workflows": len(workflow_results),
        "n_ok_workflows": n_ok,
        "success_rate": n_ok / max(1, len(workflow_results)),
        "elapsed_s": elapsed,
        "achieved_workflows_per_s": len(workflow_results) / max(elapsed, 1e-6),
    }
    log.info("agentic done: %s", summary)
    return summary


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["chatbot", "agentic"], required=True)
    ap.add_argument("--n-requests", type=int, default=1500)
    ap.add_argument("--target-rps", type=float, default=25)
    ap.add_argument("--duration-s", type=float, default=60)
    ap.add_argument("--n-workflows", type=int, default=40)
    ap.add_argument("--step-counts", nargs="+", type=int, default=[4, 8, 16])
    ap.add_argument("--tool-delay-ms", type=int, default=200)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--out-prefix", required=True)
    ap.add_argument("--dispatcher-config", required=True, help="path to a python file defining dispatcher instance")
    args = ap.parse_args()

    # Load dispatcher instance from python file
    import importlib.util
    spec = importlib.util.spec_from_file_location("user_dispatcher", args.dispatcher_config)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    dispatcher = mod.dispatcher

    if args.mode == "chatbot":
        asyncio.run(run_chatbot(
            dispatcher, args.n_requests, args.target_rps, args.duration_s,
            os.path.join(args.out_dir, f"{args.out_prefix}_chatbot.jsonl"),
            os.path.join(args.out_dir, f"{args.out_prefix}_requests.jsonl"),
        ))
    else:
        asyncio.run(run_agentic(
            dispatcher, args.n_workflows, args.step_counts, args.tool_delay_ms,
            args.duration_s,
            os.path.join(args.out_dir, f"{args.out_prefix}_workflow.jsonl"),
            os.path.join(args.out_dir, f"{args.out_prefix}_requests.jsonl"),
        ))