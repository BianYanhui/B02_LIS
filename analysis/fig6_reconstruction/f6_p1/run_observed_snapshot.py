#!/usr/bin/env python3
"""F6-P1 one-cell observed snapshot for replica suppression.

Starts no vLLM process. Talks to workers already listening on 127.0.0.1:8000-8003.
Does not modify experiments/, the relay, or Dispatcher.choose.

Load schedule below is fixed before any routing result. Do not edit it after
the run to keep or drop a regret value. Every sample is written.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import importlib.util
import json
import sys
import time
import urllib.request
from pathlib import Path

import aiohttp

ROOT = Path("/home/byh/B02")
OUT = ROOT / "analysis/fig6_reconstruction/f6_p1"
HARNESS_PATH = ROOT / "experiments/4t4/run_formal4t4.py"
RELAY_PATH = ROOT / "experiments/4t4/net/gateway_relay_4t4.py"
MODEL = "/home/byh/.cache/modelscope/qwen/Qwen2.5-1.5B-Instruct"
URLS = [f"http://127.0.0.1:{8000 + index}" for index in range(4)]

# Locked before the GPU run. Seed 1. One cell.
SEED = 1
PREFIX_WORDS = 1024
DIGEST = "F6P1X"
TENANT = "tenant-0"
CACHE_SALT = "f6p1-seed1"
LOAD_SALT = "f6p1-load-seed1"
# Round-robin, two jobs on every worker. Not reordered after the cap result.
LOAD_WORKERS = [0, 1, 2, 3, 0, 1, 2, 3]
STAGGER_S = 0.30
N_SAMPLES = 8
SAMPLE_INTERVAL_S = 0.30
HOLD_TOKENS = 96
PREFILL_TOKENS_PER_MS = 50.0
QUEUE_PENALTY_MS = 2.0
GUARD_MS = 0.5
MIN_CACHED_TOKENS = 1024
WARMUP_TIMEOUT_S = 120.0
ACTIVE_BUDGET_S = 600.0
BLOCK = 16


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def shared_prefix() -> str:
    return (
        f"[KV shared prefix id={DIGEST} tenant={TENANT}]\n"
        + ("context " * PREFIX_WORDS)
    )


def user_prompt(turn: int) -> str:
    return shared_prefix() + f"\nTask turn {turn}, variant 0: respond with OK."


def load_prompt(job_id: int) -> str:
    return f"F6-P1 load holder job {job_id} seed {SEED}. Reply with OK."


def prom_value(text: str, name: str) -> float | None:
    exact = 0.0
    suffixed = 0.0
    saw_exact = False
    saw_suffix = False
    for line in text.splitlines():
        if not line or line[0] == "#":
            continue
        head, _, raw = line.partition(" ")
        key = head.split("{", 1)[0]
        try:
            value = float(raw.split()[0])
        except (IndexError, ValueError):
            continue
        if key == name:
            exact += value
            saw_exact = True
        elif key == name + "_total":
            suffixed += value
            saw_suffix = True
    if saw_exact:
        return exact
    if saw_suffix:
        return suffixed
    return None


def fetch_metrics(url: str) -> dict:
    with urllib.request.urlopen(url + "/metrics", timeout=5) as response:
        text = response.read().decode("utf-8", errors="replace")
    keys = {
        "running": "vllm:num_requests_running",
        "waiting": "vllm:num_requests_waiting",
        "kv_cache_usage_perc": "vllm:kv_cache_usage_perc",
        "prefix_cache_hits": "vllm:prefix_cache_hits",
        "prefix_cache_queries": "vllm:prefix_cache_queries",
    }
    parsed = {name: prom_value(text, metric) for name, metric in keys.items()}
    if parsed["running"] is None or parsed["waiting"] is None:
        raise RuntimeError(f"missing request gauges on {url}")
    return parsed


def client_prefix_identity() -> dict:
    from transformers import AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(MODEL, trust_remote_code=False)

    def templated_ids(turn: int) -> list[int]:
        text = tokenizer.apply_chat_template(
            [{"role": "user", "content": user_prompt(turn)}],
            tokenize=False,
            add_generation_prompt=True,
        )
        return tokenizer.encode(text, add_special_tokens=False)

    warm_ids = templated_ids(0)
    probe_ids = templated_ids(1)
    common = 0
    for left, right in zip(warm_ids, probe_ids):
        if left != right:
            break
        common += 1
    raw = ",".join(map(str, warm_ids[:common])).encode()
    return {
        "warmup_templated_tokens": len(warm_ids),
        "probe_templated_tokens": len(probe_ids),
        "common_prefix_tokens": common,
        "block_aligned_common_tokens": common // BLOCK * BLOCK,
        "client_common_prefix_sha256": hashlib.sha256(raw).hexdigest(),
        "engine_block_hash": "NOT_MEASURED",
    }


async def one_request(session: aiohttp.ClientSession, url: str, prompt: str, cache_salt: str, output_tokens: int) -> dict:
    payload = {
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": output_tokens,
        "min_tokens": output_tokens,
        "ignore_eos": True,
        "temperature": 0.0,
        "cache_salt": cache_salt,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    started = time.perf_counter_ns()
    first = 0
    usage: dict = {}
    async with session.post(url + "/v1/chat/completions", json=payload, timeout=aiohttp.ClientTimeout(total=90)) as response:
        if response.status != 200:
            body = (await response.text())[:240]
            return {"ok": False, "error": f"http_{response.status}: {body}", "ttft_ms": 0.0,
                    "input_tokens": None, "output_tokens": None, "vllm_cached_tokens": 0}
        buffer = ""
        async for part in response.content.iter_any():
            buffer += part.decode(errors="ignore")
            while "\n\n" in buffer:
                event, buffer = buffer.split("\n\n", 1)
                data = next((line[6:] for line in event.splitlines() if line.startswith("data: ")), "")
                if not data or data == "[DONE]":
                    continue
                parsed = json.loads(data)
                if not first and parsed.get("choices"):
                    first = time.perf_counter_ns()
                if parsed.get("usage"):
                    usage = parsed["usage"]
    ended = time.perf_counter_ns()
    if usage.get("prompt_tokens") is None:
        return {"ok": False, "error": "missing_final_usage", "ttft_ms": 0.0,
                "input_tokens": None, "output_tokens": None, "vllm_cached_tokens": 0}
    details = usage.get("prompt_tokens_details") or {}
    return {
        "ok": True,
        "error": "",
        "ttft_ms": ((first or ended) - started) / 1e6,
        "latency_ms": (ended - started) / 1e6,
        "input_tokens": usage.get("prompt_tokens"),
        "output_tokens": usage.get("completion_tokens"),
        "vllm_cached_tokens": int(details.get("cached_tokens") or 0),
    }


def new_relay(relay_mod, dedup: int):
    args = argparse.Namespace(
        max_queue=4096, quiet=True, tau=1.0, util_lambda=16, gate=1.0, adaptive_queue_gate=1,
    )
    relay = relay_mod.Relay(args)
    relay.mode = relay_mod.MODE_STATIC
    relay.merge = False
    relay.priority = False
    relay.adaptive = False
    relay.dedup = dedup
    relay.global_topk = 0
    relay.current_cell = 1
    return relay


def admit(relay_mod, relay, digest_int: int, coverages: list[int]) -> tuple[list[int], int, int]:
    admitted = []
    for instance, coverage in enumerate(coverages):
        before = relay.drops["replica_cap"]
        data = relay_mod.frame(relay_mod.K_UP, instance, 1, instance + 1, int(coverage), digest_int, 0.0)
        relay.enqueue(data, relay_mod.K_UP, instance, instance + 1, int(coverage), digest_int, 0.0)
        if relay.drops["replica_cap"] == before:
            admitted.append(instance)
    forwarded = []
    while True:
        data = relay._dequeue()
        if data is None:
            break
        kind, instance, _cell, _seq, coverage, digest, _sent = relay_mod.HDR.unpack(data[:32])
        if kind == relay_mod.K_UP:
            forwarded.append((instance, int(coverage), int(digest)))
    return admitted, len(forwarded), int(relay.drops["replica_cap"])


def model_cost(load: int, prompt_tokens: int, cached_tokens: int) -> float:
    prefill_tokens = max(0, prompt_tokens - cached_tokens)
    return load * QUEUE_PENALTY_MS + prefill_tokens / PREFILL_TOKENS_PER_MS


def route_views(harness, relay_mod, digest: str, digest_int: int, loads: list[int], coverages: list[int],
                prompt_tokens: int, rr: int) -> list[dict]:
    costs = [model_cost(loads[index], prompt_tokens, coverages[index]) for index in range(4)]
    best = min(costs)
    argmin = [index for index, cost in enumerate(costs) if cost == best]
    views = []
    decisions = {}
    for name, dedup in (("full", 0), ("krep2", 2)):
        relay = new_relay(relay_mod, dedup)
        admitted, forwarded, dropped = admit(relay_mod, relay, digest_int, coverages)
        dispatcher = harness.Dispatcher(4, PREFILL_TOKENS_PER_MS, QUEUE_PENALTY_MS, GUARD_MS)
        dispatcher.loads = list(loads)
        dispatcher.rr = rr
        for instance in admitted:
            dispatcher.apply_upsert(instance, digest, int(coverages[instance]), 0.0, 0.0)
        selected, raw, evaluated, affinity, view_coverage, expected = dispatcher.choose(
            harness.TraceRequest(
                request_id=0, phase=0, lineage_id=0, step=1, tenant=TENANT,
                digest=digest, coverage_tokens=int(coverages[0]), workload="f6p1", discard=False,
            )
        )
        visible = [index for index in range(4) if dispatcher.index[index].get(digest, 0) > 0]
        hidden = [index for index in range(4) if index not in visible]
        decisions[name] = selected
        views.append({
            "view": name,
            "krep": "full" if dedup == 0 else "2",
            "admitted_owners": admitted,
            "visible_owners": visible,
            "suppressed_owners": hidden,
            "forwarded_ads": forwarded,
            "dropped_ads": dropped,
            "selected_worker": selected,
            "raw_fanout": raw,
            "evaluated": evaluated,
            "affinity": bool(affinity),
            "view_coverage": view_coverage,
            "expected_net_ms": expected,
            "costs": costs,
            "argmin_workers": argmin,
            "rr_after": dispatcher.rr,
        })
    full_selected = decisions["full"]
    for row in views:
        hidden = set(row["suppressed_owners"])
        row["full_selected_worker"] = full_selected
        row["selected_changed_vs_full"] = int(row["selected_worker"] != full_selected)
        row["T_selected"] = costs[row["selected_worker"]]
        row["T_full_selected"] = costs[full_selected]
        row["regret_vs_full_ms"] = costs[row["selected_worker"]] - costs[full_selected]
        row["best_owner_hidden"] = int(bool(argmin) and all(index in hidden for index in argmin))
        row["any_best_owner_hidden"] = int(any(index in hidden for index in argmin))
        row["full_is_model_argmin"] = int(full_selected in argmin)
        selected_cached = coverages[row["selected_worker"]]
        row["prefill_term_selected_ms"] = max(0, prompt_tokens - selected_cached) / PREFILL_TOKENS_PER_MS
        row["load_term_selected_ms"] = loads[row["selected_worker"]] * QUEUE_PENALTY_MS
    return views


def join_metrics(snapshot_id: str, scenario: str, decision_index: int, stamp: float, loads: list[int],
                 metrics: list[dict], probes: list[dict], warmups: list[dict], identity: dict) -> list[dict]:
    rows = []
    for worker in range(4):
        rows.append({
            "snapshot_id": snapshot_id,
            "scenario": scenario,
            "decision_index": decision_index,
            "timestamp_unix": f"{stamp:.6f}",
            "worker": worker,
            "harness_inflight": loads[worker],
            "vllm_num_requests_running": metrics[worker]["running"],
            "vllm_num_requests_waiting": metrics[worker]["waiting"],
            "vllm_kv_cache_usage_perc": metrics[worker]["kv_cache_usage_perc"],
            "prefix_cache_hits_total": metrics[worker]["prefix_cache_hits"],
            "prefix_cache_queries_total": metrics[worker]["prefix_cache_queries"],
            "probe_cached_tokens": probes[worker]["vllm_cached_tokens"],
            "probe_prompt_tokens": probes[worker]["input_tokens"],
            "probe_ttft_ms": probes[worker]["ttft_ms"],
            "warmup_cached_tokens": warmups[worker]["vllm_cached_tokens"],
            "warmup_prompt_tokens": warmups[worker]["input_tokens"],
            "warmup_ttft_ms": warmups[worker]["ttft_ms"],
            "advertised_coverage_tokens": probes[worker]["vllm_cached_tokens"],
            "client_common_prefix_tokens": identity["common_prefix_tokens"],
            "client_common_prefix_sha256": identity["client_common_prefix_sha256"],
            "cache_salt": CACHE_SALT,
            "digest": DIGEST,
            "residency_source": "vllm_cached_tokens_probe",
        })
    return rows


def routing_rows(snapshot_id: str, scenario: str, decision_index: int, stamp: float, loads: list[int],
                 coverages: list[int], rr: int, views: list[dict]) -> list[dict]:
    rows = []
    for view in views:
        row = {
            "snapshot_id": snapshot_id,
            "scenario": scenario,
            "decision_index": decision_index,
            "timestamp_unix": f"{stamp:.6f}",
            "rr": rr,
            "view": view["view"],
            "krep": view["krep"],
            "loads": ";".join(map(str, loads)),
            "coverages": ";".join(map(str, coverages)),
            "visible_owners": ";".join(map(str, view["visible_owners"])),
            "suppressed_owners": ";".join(map(str, view["suppressed_owners"])),
            "forwarded_ads": view["forwarded_ads"],
            "dropped_ads": view["dropped_ads"],
            "selected_worker": view["selected_worker"],
            "affinity": int(view["affinity"]),
            "view_coverage": view["view_coverage"],
            "expected_net_ms": view["expected_net_ms"],
            "full_selected_worker": view["full_selected_worker"],
            "selected_changed_vs_full": view["selected_changed_vs_full"],
            "argmin_workers": ";".join(map(str, view["argmin_workers"])),
            "best_owner_hidden": view["best_owner_hidden"],
            "any_best_owner_hidden": view["any_best_owner_hidden"],
            "full_is_model_argmin": view["full_is_model_argmin"],
            "T_selected_ms": view["T_selected"],
            "T_full_selected_ms": view["T_full_selected"],
            "regret_vs_full_ms": view["regret_vs_full_ms"],
            "prefill_term_selected_ms": view["prefill_term_selected_ms"],
            "load_term_selected_ms": view["load_term_selected_ms"],
            "actual_executed_worker": "",
            "actual_ttft_ms": "",
            "regret_label": "MODEL-DERIVED",
        }
        for index, cost in enumerate(view["costs"]):
            row[f"T_worker{index}_ms"] = cost
        rows.append(row)
    return rows


def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("")
        return
    fieldnames = list(rows[0].keys())
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def residency_problems(warmups: list[dict], probes: list[dict], identity: dict) -> list[str]:
    problems = []
    cached = []
    floor = identity["block_aligned_common_tokens"] - BLOCK
    for worker, (warm, probe) in enumerate(zip(warmups, probes)):
        if not warm["ok"] or not probe["ok"]:
            problems.append(f"worker {worker} request failed warm={warm.get('error')} probe={probe.get('error')}")
            continue
        if int(probe["vllm_cached_tokens"]) < MIN_CACHED_TOKENS:
            problems.append(f"worker {worker} probe cached {probe['vllm_cached_tokens']} < {MIN_CACHED_TOKENS}")
        if int(probe["vllm_cached_tokens"]) < floor:
            problems.append(
                f"worker {worker} probe cached {probe['vllm_cached_tokens']} below block-aligned common prefix {floor}"
            )
        if int(probe["vllm_cached_tokens"]) < int(warm["vllm_cached_tokens"]) + MIN_CACHED_TOKENS:
            problems.append(
                f"worker {worker} probe did not reuse more cache than warmup "
                f"({probe['vllm_cached_tokens']} vs {warm['vllm_cached_tokens']})"
            )
        cached.append(int(probe["vllm_cached_tokens"]))
    if len(cached) == 4 and max(cached) - min(cached) > BLOCK:
        problems.append(f"probe cached tokens differ by more than one block: {cached}")
    return problems


async def poll_metrics() -> list[dict]:
    return await asyncio.gather(*[asyncio.to_thread(fetch_metrics, url) for url in URLS])


async def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    identity = client_prefix_identity()
    harness = load_module("f6p1_harness", HARNESS_PATH)
    relay_mod = load_module("f6p1_relay", RELAY_PATH)
    digest_int = harness.digest64(DIGEST)
    active_started = time.perf_counter()

    def budget_left() -> float:
        return ACTIVE_BUDGET_S - (time.perf_counter() - active_started)

    snapshot_rows: list[dict] = []
    route_rows: list[dict] = []
    notes: list[str] = []
    verdict_inputs: dict = {"identity": identity, "seed": SEED, "load_workers": LOAD_WORKERS}

    async with aiohttp.ClientSession() as session:
        for url in URLS:
            async with session.get(url + "/v1/models", timeout=aiohttp.ClientTimeout(total=10)) as response:
                if response.status != 200:
                    raise RuntimeError(f"{url} not ready: {response.status}")

        metrics_before = await poll_metrics()
        warm_started = time.perf_counter()
        try:
            warmups = list(await asyncio.wait_for(asyncio.gather(*[
                one_request(session, url, user_prompt(0), CACHE_SALT, 4) for url in URLS
            ]), timeout=WARMUP_TIMEOUT_S))
        except asyncio.TimeoutError:
            notes.append("warmup wave exceeded 120s; stopped before probes")
            (OUT / "raw_observations.json").write_text(json.dumps({"notes": notes, "identity": identity}, indent=2))
            print("WARMUP_TIMEOUT")
            return 2
        warm_s = time.perf_counter() - warm_started
        verdict_inputs["warmup_wall_s"] = warm_s
        if not all(item["ok"] for item in warmups):
            notes.append("warmup request failed")
            verdict_inputs["warmups"] = warmups
            verdict_inputs["notes"] = notes
            (OUT / "raw_observations.json").write_text(json.dumps(verdict_inputs, indent=2))
            print("WARMUP_FAILED")
            return 2

        metrics_mid = await poll_metrics()
        probes = list(await asyncio.gather(*[
            one_request(session, url, user_prompt(1), CACHE_SALT, 4) for url in URLS
        ]))
        metrics_after_probe = await poll_metrics()
        for worker in range(4):
            probes[worker]["prefix_cache_hits_delta"] = (
                metrics_after_probe[worker]["prefix_cache_hits"] - metrics_mid[worker]["prefix_cache_hits"]
            )
            probes[worker]["prefix_cache_queries_delta"] = (
                metrics_after_probe[worker]["prefix_cache_queries"] - metrics_mid[worker]["prefix_cache_queries"]
            )
        problems = residency_problems(warmups, probes, identity)
        verdict_inputs["warmups"] = warmups
        verdict_inputs["probes"] = probes
        verdict_inputs["metrics_before_warmup"] = metrics_before
        verdict_inputs["residency_problems"] = problems
        if problems:
            notes.extend(problems)
            verdict_inputs["notes"] = notes
            verdict_inputs["verdict"] = "P1-INVALID"
            (OUT / "raw_observations.json").write_text(json.dumps(verdict_inputs, indent=2, default=str))
            print("RESIDENCY_INVALID")
            for item in problems:
                print(item)
            return 3

        coverages = [int(item["vllm_cached_tokens"]) for item in probes]
        prompt_tokens = int(probes[0]["input_tokens"])
        if any(int(item["input_tokens"]) != prompt_tokens for item in probes):
            notes.append("probe prompt token counts differ across workers; T uses worker 0 prompt length")

        idle_metrics = await poll_metrics()
        idle_loads = [0, 0, 0, 0]
        for decision_index, rr in enumerate(range(4)):
            stamp = time.time()
            snapshot_id = f"A{decision_index}"
            snapshot_rows.extend(join_metrics(
                snapshot_id, "balanced", decision_index, stamp, idle_loads, idle_metrics, probes, warmups, identity,
            ))
            views = route_views(harness, relay_mod, DIGEST, digest_int, idle_loads, coverages, prompt_tokens, rr)
            route_rows.extend(routing_rows(
                snapshot_id, "balanced", decision_index, stamp, idle_loads, coverages, rr, views,
            ))

        if budget_left() < 30:
            notes.append("stopped before imbalance; active budget nearly exhausted")
            verdict_inputs["notes"] = notes
            verdict_inputs["verdict"] = "P1-INCONCLUSIVE"
            write_outputs(snapshot_rows, route_rows, verdict_inputs)
            return 4

        inflight = [0, 0, 0, 0]
        lock = asyncio.Lock()
        hold_results: list[dict] = []

        async def hold(worker: int, job_id: int) -> None:
            async with lock:
                inflight[worker] += 1
            try:
                result = await one_request(session, URLS[worker], load_prompt(job_id), LOAD_SALT, HOLD_TOKENS)
            except Exception as exc:
                result = {"ok": False, "error": repr(exc)[:200], "ttft_ms": 0.0,
                          "input_tokens": None, "output_tokens": None, "vllm_cached_tokens": 0}
            result["worker"] = worker
            result["job_id"] = job_id
            async with lock:
                inflight[worker] -= 1
                hold_results.append(result)

        hold_tasks: list[asyncio.Task] = []

        async def launch() -> None:
            for job_id, worker in enumerate(LOAD_WORKERS):
                hold_tasks.append(asyncio.create_task(hold(worker, job_id)))
                await asyncio.sleep(STAGGER_S)

        launcher = asyncio.create_task(launch())
        samples = []
        deadline = time.perf_counter() + 20
        while time.perf_counter() < deadline:
            async with lock:
                if sum(inflight) > 0:
                    break
            await asyncio.sleep(0.02)
        for decision_index in range(N_SAMPLES):
            async with lock:
                loads = list(inflight)
            metrics = await poll_metrics()
            stamp = time.time()
            samples.append({"decision_index": decision_index, "timestamp_unix": stamp, "loads": loads, "metrics": metrics})
            snapshot_id = f"B{decision_index}"
            snapshot_rows.extend(join_metrics(
                snapshot_id, "imbalanced", decision_index, stamp, loads, metrics, probes, warmups, identity,
            ))
            views = route_views(harness, relay_mod, DIGEST, digest_int, loads, coverages, prompt_tokens, 0)
            route_rows.extend(routing_rows(
                snapshot_id, "imbalanced", decision_index, stamp, loads, coverages, 0, views,
            ))
            await asyncio.sleep(SAMPLE_INTERVAL_S)
        await launcher
        if hold_tasks:
            await asyncio.wait(hold_tasks, timeout=90)

        reprobes = list(await asyncio.gather(*[
            one_request(session, url, user_prompt(2), CACHE_SALT, 4) for url in URLS
        ]))
        verdict_inputs["reprobes"] = reprobes
        verdict_inputs["hold_results"] = hold_results
        verdict_inputs["samples"] = [
            {"decision_index": item["decision_index"], "timestamp_unix": item["timestamp_unix"], "loads": item["loads"],
             "running": [metric["running"] for metric in item["metrics"]],
             "waiting": [metric["waiting"] for metric in item["metrics"]],
             "kv_cache_usage_perc": [metric["kv_cache_usage_perc"] for metric in item["metrics"]]}
            for item in samples
        ]
        reprobe_problems = []
        for worker, item in enumerate(reprobes):
            if not item["ok"] or int(item["vllm_cached_tokens"]) < MIN_CACHED_TOKENS:
                reprobe_problems.append(f"worker {worker} post-window cached {item.get('vllm_cached_tokens')} ok={item.get('ok')}")
        verdict_inputs["reprobe_problems"] = reprobe_problems
        if reprobe_problems:
            notes.append("prefix was not reconfirmed on every worker after the load window")
            notes.extend(reprobe_problems)

    verdict_inputs["notes"] = notes
    verdict_inputs["active_wall_s"] = time.perf_counter() - active_started
    verdict_inputs["prompt_tokens_used_for_T"] = prompt_tokens
    verdict_inputs["coverages"] = coverages
    write_outputs(snapshot_rows, route_rows, verdict_inputs)
    print(json.dumps({
        "active_wall_s": verdict_inputs["active_wall_s"],
        "warmup_wall_s": verdict_inputs["warmup_wall_s"],
        "coverages": coverages,
        "residency_problems": problems,
        "reprobe_problems": verdict_inputs["reprobe_problems"],
        "samples": verdict_inputs["samples"],
    }, indent=2))
    return 0


def write_outputs(snapshot_rows: list[dict], route_rows: list[dict], payload: dict) -> None:
    write_csv(OUT / "observed_worker_snapshots.csv", snapshot_rows)
    write_csv(OUT / "counterfactual_routing.csv", route_rows)
    (OUT / "raw_observations.json").write_text(json.dumps(payload, indent=2, default=str))


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        raise SystemExit(130)
