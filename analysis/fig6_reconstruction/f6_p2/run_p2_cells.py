#!/usr/bin/env python3
"""F6-P2 three short cells. Does not start vLLM and does not modify experiments/.

Timing and holder orders below were fixed before any F6-P2 routing result.
Do not edit them after the run to keep or drop a regret value.
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
OUT = ROOT / "analysis/fig6_reconstruction/f6_p2"
HARNESS_PATH = ROOT / "experiments/4t4/run_formal4t4.py"
RELAY_PATH = ROOT / "experiments/4t4/net/gateway_relay_4t4.py"
MODEL = "/home/byh/.cache/modelscope/qwen/Qwen2.5-1.5B-Instruct"
URLS = [f"http://127.0.0.1:{8000 + index}" for index in range(4)]

# Locked before the GPU run.
PREFIX_WORDS = 1024
TENANT = "tenant-0"
# 320 decode tokens keeps the first holder inside the engine across the
# 3.5 s launch window. F6-P1 used 96 and the second wave often disagreed
# with num_requests_running.
HOLD_TOKENS = 320
STAGGER_S = 0.50
SETTLE_S = 0.25
N_SAMPLES = 8
SAMPLE_INTERVAL_S = 0.50
PREFILL_TOKENS_PER_MS = 50.0
QUEUE_PENALTY_MS = 2.0
GUARD_MS = 0.5
MIN_CACHED_TOKENS = 1024
WARMUP_TIMEOUT_S = 120.0
ACTIVE_BUDGET_S = 360.0
BLOCK = 16
ADMISSION_ORDER = [0, 1, 2, 3]
VISIBLE_OWNERS = (0, 1)
HIDDEN_OWNERS = (2, 3)

# Cell C runs only after A and B, and only if at least one of them produced
# an engine-confirmed snapshot with a non-zero harness load.
CELLS = [
    {"cell": "A-reverse", "seed": 1, "order": [3, 2, 1, 0, 3, 2, 1, 0], "gate": False},
    {"cell": "B-interleaved", "seed": 1, "order": [0, 2, 1, 3, 2, 0, 3, 1], "gate": False},
    {"cell": "C-forward-seed2", "seed": 2, "order": [0, 1, 2, 3, 0, 1, 2, 3], "gate": True},
]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def shared_prefix(digest: str) -> str:
    return (
        f"[KV shared prefix id={digest} tenant={TENANT}]\n"
        + ("context " * PREFIX_WORDS)
    )


def user_prompt(digest: str, turn: int) -> str:
    return shared_prefix(digest) + f"\nTask turn {turn}, variant 0: respond with OK."


def load_prompt(cell: str, seed: int, job_id: int) -> str:
    return f"F6-P2 load holder cell {cell} job {job_id} seed {seed}. Reply with OK."


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


def client_prefix_identity(digest: str) -> dict:
    from transformers import AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(MODEL, trust_remote_code=False)

    def templated_ids(turn: int) -> list[int]:
        text = tokenizer.apply_chat_template(
            [{"role": "user", "content": user_prompt(digest, turn)}],
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
    forwarded = 0
    while True:
        data = relay._dequeue()
        if data is None:
            break
        kind, _instance, _cell, _seq, _coverage, _digest, _sent = relay_mod.HDR.unpack(data[:32])
        if kind == relay_mod.K_UP:
            forwarded += 1
    return admitted, forwarded, int(relay.drops["replica_cap"])


def model_cost(load: int, prompt_tokens: int, cached_tokens: int) -> float:
    return load * QUEUE_PENALTY_MS + max(0, prompt_tokens - cached_tokens) / PREFILL_TOKENS_PER_MS


def delta_load(loads: list[int]) -> int:
    return min(loads[index] for index in VISIBLE_OWNERS) - min(loads[index] for index in HIDDEN_OWNERS)


def confirmation(loads: list[int], metrics: list[dict]) -> str:
    running = [int(item["running"]) for item in metrics]
    waiting = [int(item["waiting"]) for item in metrics]
    if running == list(loads) and all(item == 0 for item in waiting):
        return "ENGINE_CONFIRMED"
    return "HARNESS_ONLY"


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
        selected, _raw, _evaluated, affinity, view_coverage, expected = dispatcher.choose(
            harness.TraceRequest(
                request_id=0, phase=0, lineage_id=0, step=1, tenant=TENANT,
                digest=digest, coverage_tokens=int(coverages[0]), workload="f6p2", discard=False,
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
            "affinity": bool(affinity),
            "view_coverage": view_coverage,
            "expected_net_ms": expected,
            "costs": costs,
            "argmin_workers": argmin,
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
        row["cap_is_model_argmin"] = int(decisions["krep2"] in argmin)
        selected_cached = coverages[row["selected_worker"]]
        row["prefill_term_selected_ms"] = max(0, prompt_tokens - selected_cached) / PREFILL_TOKENS_PER_MS
        row["load_term_selected_ms"] = loads[row["selected_worker"]] * QUEUE_PENALTY_MS
    return views


def residency_problems(warmups: list[dict], probes: list[dict], identity: dict) -> list[str]:
    problems = []
    cached = []
    floor = identity["block_aligned_common_tokens"] - BLOCK
    for worker, (warm, probe) in enumerate(zip(warmups, probes)):
        if not warm["ok"] or not probe["ok"]:
            problems.append(f"worker {worker} request failed warm={warm.get('error')} probe={probe.get('error')}")
            continue
        if int(warm["vllm_cached_tokens"]) > 128:
            problems.append(f"worker {worker} warmup cached {warm['vllm_cached_tokens']}; salt did not start cold")
        if int(probe["vllm_cached_tokens"]) < MIN_CACHED_TOKENS:
            problems.append(f"worker {worker} probe cached {probe['vllm_cached_tokens']} < {MIN_CACHED_TOKENS}")
        if int(probe["vllm_cached_tokens"]) < floor:
            problems.append(f"worker {worker} probe cached {probe['vllm_cached_tokens']} below {floor}")
        if int(probe["vllm_cached_tokens"]) < int(warm["vllm_cached_tokens"]) + MIN_CACHED_TOKENS:
            problems.append(f"worker {worker} probe did not reuse past the warmup")
        cached.append(int(probe["vllm_cached_tokens"]))
    if len(cached) == 4 and max(cached) - min(cached) > BLOCK:
        problems.append(f"probe cached tokens differ by more than one block: {cached}")
    return problems


async def poll_metrics() -> list[dict]:
    return list(await asyncio.gather(*[asyncio.to_thread(fetch_metrics, url) for url in URLS]))


async def wait_engine_idle(timeout_s: float = 30.0) -> tuple[bool, list[dict]]:
    deadline = time.perf_counter() + timeout_s
    metrics = await poll_metrics()
    while time.perf_counter() < deadline:
        if all(int(item["running"]) == 0 and int(item["waiting"]) == 0 for item in metrics):
            return True, metrics
        await asyncio.sleep(0.2)
        metrics = await poll_metrics()
    return False, metrics


def has_confirmed_load(cell: dict) -> bool:
    return any(
        sample["confirmation"] == "ENGINE_CONFIRMED" and sum(sample["loads"]) > 0
        for sample in cell.get("samples", [])
    )


async def run_cell(session, harness, relay_mod, spec: dict, budget_started: float) -> dict:
    cell = spec["cell"]
    seed = spec["seed"]
    order = list(spec["order"])
    digest = "F6P2" + cell[0]
    cache_salt = f"f6p2-{cell}-seed{seed}"
    load_salt = f"f6p2-load-{cell}-seed{seed}"
    identity = client_prefix_identity(digest)
    digest_int = harness.digest64(digest)
    result = {
        "cell": cell,
        "seed": seed,
        "holder_order": order,
        "admission_order": ADMISSION_ORDER,
        "digest": digest,
        "cache_salt": cache_salt,
        "load_salt": load_salt,
        "identity": identity,
        "timing": {
            "hold_tokens": HOLD_TOKENS,
            "stagger_s": STAGGER_S,
            "settle_s": SETTLE_S,
            "sample_interval_s": SAMPLE_INTERVAL_S,
            "n_samples": N_SAMPLES,
        },
        "samples": [],
        "notes": [],
    }
    if ACTIVE_BUDGET_S - (time.perf_counter() - budget_started) < 45:
        result["status"] = "skipped_budget"
        result["notes"].append("active budget had less than 45s left")
        return result

    cell_started = time.perf_counter()
    metrics_before = await poll_metrics()
    result["metrics_before_warmup"] = metrics_before
    try:
        warmups = list(await asyncio.wait_for(asyncio.gather(*[
            one_request(session, url, user_prompt(digest, 0), cache_salt, 4) for url in URLS
        ]), timeout=WARMUP_TIMEOUT_S))
    except asyncio.TimeoutError:
        result["status"] = "warmup_timeout"
        result["notes"].append("warmup wave exceeded 120s")
        return result
    result["warmup_wall_s"] = time.perf_counter() - cell_started
    result["warmups"] = warmups
    if not all(item["ok"] for item in warmups):
        result["status"] = "warmup_failed"
        return result

    metrics_mid = await poll_metrics()
    probes = list(await asyncio.gather(*[
        one_request(session, url, user_prompt(digest, 1), cache_salt, 4) for url in URLS
    ]))
    metrics_after = await poll_metrics()
    for worker in range(4):
        probes[worker]["prefix_cache_hits_delta"] = (
            metrics_after[worker]["prefix_cache_hits"] - metrics_mid[worker]["prefix_cache_hits"]
        )
        probes[worker]["prefix_cache_queries_delta"] = (
            metrics_after[worker]["prefix_cache_queries"] - metrics_mid[worker]["prefix_cache_queries"]
        )
    result["probes"] = probes
    problems = residency_problems(warmups, probes, identity)
    result["residency_problems"] = problems
    if problems:
        result["status"] = "residency_failed"
        result["notes"].extend(problems)
        return result

    idle_ok, idle_metrics = await wait_engine_idle(15)
    result["idle_before_load"] = idle_ok
    if not idle_ok:
        result["notes"].append("engine was not idle before the load window")
    coverages = [int(item["vllm_cached_tokens"]) for item in probes]
    prompt_tokens = int(probes[0]["input_tokens"])
    result["coverages"] = coverages
    result["prompt_tokens"] = prompt_tokens

    def record(scenario: str, decision_index: int, loads: list[int], metrics: list[dict], rel_s: float) -> None:
        stamp = time.time()
        label = confirmation(loads, metrics)
        views = route_views(harness, relay_mod, digest, digest_int, loads, coverages, prompt_tokens, 0)
        result["samples"].append({
            "scenario": scenario,
            "decision_index": decision_index,
            "timestamp_unix": stamp,
            "rel_s": rel_s,
            "loads": loads,
            "running": [int(item["running"]) for item in metrics],
            "waiting": [int(item["waiting"]) for item in metrics],
            "kv_cache_usage_perc": [item["kv_cache_usage_perc"] for item in metrics],
            "confirmation": label,
            "delta_load": delta_load(loads),
            "views": views,
            "metrics": metrics,
        })

    record("idle", -1, [0, 0, 0, 0], idle_metrics, 0.0)

    inflight = [0, 0, 0, 0]
    lock = asyncio.Lock()
    hold_results: list[dict] = []
    launches: list[dict] = []

    async def hold(worker: int, job_id: int, launched_unix: float) -> None:
        async with lock:
            inflight[worker] += 1
        try:
            held = await one_request(session, URLS[worker], load_prompt(cell, seed, job_id), load_salt, HOLD_TOKENS)
        except Exception as exc:
            held = {"ok": False, "error": repr(exc)[:200], "ttft_ms": 0.0,
                    "input_tokens": None, "output_tokens": None, "vllm_cached_tokens": 0,
                    "latency_ms": 0.0}
        held["worker"] = worker
        held["job_id"] = job_id
        held["launched_unix"] = launched_unix
        held["finished_unix"] = time.time()
        async with lock:
            inflight[worker] -= 1
            hold_results.append(held)

    hold_tasks: list[asyncio.Task] = []
    t0 = time.perf_counter()
    result["load_started_unix"] = time.time()

    async def launch() -> None:
        for job_id, worker in enumerate(order):
            target = t0 + job_id * STAGGER_S
            delay = target - time.perf_counter()
            if delay > 0:
                await asyncio.sleep(delay)
            launched = time.time()
            launches.append({"job_id": job_id, "worker": worker, "launched_unix": launched,
                             "rel_s": time.perf_counter() - t0})
            hold_tasks.append(asyncio.create_task(hold(worker, job_id, launched)))

    launcher = asyncio.create_task(launch())
    for decision_index in range(N_SAMPLES):
        target = t0 + SETTLE_S + decision_index * SAMPLE_INTERVAL_S
        delay = target - time.perf_counter()
        if delay > 0:
            await asyncio.sleep(delay)
        async with lock:
            loads = list(inflight)
        metrics = await poll_metrics()
        record("ramp", decision_index, loads, metrics, time.perf_counter() - t0)
    await launcher
    if hold_tasks:
        await asyncio.wait(hold_tasks, timeout=120)
    result["load_finished_unix"] = time.time()
    result["launches"] = launches
    result["hold_results"] = [
        {key: item.get(key) for key in ("ok", "error", "worker", "job_id", "ttft_ms", "latency_ms",
                                        "input_tokens", "output_tokens", "launched_unix", "finished_unix")}
        for item in hold_results
    ]
    idle_after, _metrics_after_load = await wait_engine_idle(30)
    result["idle_after_load"] = idle_after
    reprobes = list(await asyncio.gather(*[
        one_request(session, url, user_prompt(digest, 2), cache_salt, 4) for url in URLS
    ]))
    result["reprobes"] = reprobes
    reprobe_problems = []
    for worker, item in enumerate(reprobes):
        if not item["ok"] or int(item["vllm_cached_tokens"]) < MIN_CACHED_TOKENS:
            reprobe_problems.append(
                f"worker {worker} post-window cached {item.get('vllm_cached_tokens')} ok={item.get('ok')}"
            )
    result["reprobe_problems"] = reprobe_problems
    if reprobe_problems:
        result["notes"].extend(reprobe_problems)
    result["active_wall_s"] = time.perf_counter() - cell_started
    result["status"] = "ok"
    cap = next(view for view in result["samples"][0]["views"] if view["view"] == "krep2")
    if cap["suppressed_owners"] != list(HIDDEN_OWNERS) or cap["visible_owners"] != list(VISIBLE_OWNERS):
        result["notes"].append(
            f"cap owners differed from 0,1 visible / 2,3 hidden: {cap['visible_owners']} / {cap['suppressed_owners']}"
        )
    return result


def flatten(cells: list[dict]) -> tuple[list[dict], list[dict]]:
    snapshots = []
    routes = []
    for cell in cells:
        if not cell.get("samples"):
            continue
        probes = cell["probes"]
        warmups = cell["warmups"]
        identity = cell["identity"]
        for sample in cell["samples"]:
            snapshot_id = f"{cell['cell']}:{sample['scenario']}:{sample['decision_index']}"
            for worker in range(4):
                snapshots.append({
                    "cell": cell["cell"],
                    "seed": cell["seed"],
                    "holder_order": ";".join(map(str, cell["holder_order"])),
                    "snapshot_id": snapshot_id,
                    "scenario": sample["scenario"],
                    "decision_index": sample["decision_index"],
                    "timestamp_unix": f"{sample['timestamp_unix']:.6f}",
                    "rel_s": f"{sample['rel_s']:.3f}",
                    "worker": worker,
                    "harness_inflight": sample["loads"][worker],
                    "vllm_num_requests_running": sample["running"][worker],
                    "vllm_num_requests_waiting": sample["waiting"][worker],
                    "confirmation": sample["confirmation"],
                    "delta_load": sample["delta_load"],
                    "vllm_kv_cache_usage_perc": sample["kv_cache_usage_perc"][worker],
                    "probe_cached_tokens": probes[worker]["vllm_cached_tokens"],
                    "probe_prompt_tokens": probes[worker]["input_tokens"],
                    "probe_ttft_ms": probes[worker]["ttft_ms"],
                    "probe_prefix_cache_hits_delta": probes[worker]["prefix_cache_hits_delta"],
                    "warmup_cached_tokens": warmups[worker]["vllm_cached_tokens"],
                    "warmup_prompt_tokens": warmups[worker]["input_tokens"],
                    "reprobe_cached_tokens": cell["reprobes"][worker]["vllm_cached_tokens"],
                    "client_common_prefix_sha256": identity["client_common_prefix_sha256"],
                    "cache_salt": cell["cache_salt"],
                    "digest": cell["digest"],
                    "residency_source": "vllm_cached_tokens_probe",
                })
            for view in sample["views"]:
                routes.append({
                    "cell": cell["cell"],
                    "seed": cell["seed"],
                    "scenario": sample["scenario"],
                    "decision_index": sample["decision_index"],
                    "snapshot_id": snapshot_id,
                    "timestamp_unix": f"{sample['timestamp_unix']:.6f}",
                    "confirmation": sample["confirmation"],
                    "view": view["view"],
                    "krep": view["krep"],
                    "loads": ";".join(map(str, sample["loads"])),
                    "running": ";".join(map(str, sample["running"])),
                    "delta_load": sample["delta_load"],
                    "coverages": ";".join(map(str, cell["coverages"])),
                    "visible_owners": ";".join(map(str, view["visible_owners"])),
                    "suppressed_owners": ";".join(map(str, view["suppressed_owners"])),
                    "forwarded_ads": view["forwarded_ads"],
                    "dropped_ads": view["dropped_ads"],
                    "selected_worker": view["selected_worker"],
                    "affinity": int(view["affinity"]),
                    "full_selected_worker": view["full_selected_worker"],
                    "selected_changed_vs_full": view["selected_changed_vs_full"],
                    "argmin_workers": ";".join(map(str, view["argmin_workers"])),
                    "best_owner_hidden": view["best_owner_hidden"],
                    "any_best_owner_hidden": view["any_best_owner_hidden"],
                    "full_is_model_argmin": view["full_is_model_argmin"],
                    "cap_is_model_argmin": view["cap_is_model_argmin"],
                    "T_selected_ms": view["T_selected"],
                    "T_full_selected_ms": view["T_full_selected"],
                    "regret_vs_full_ms": view["regret_vs_full_ms"],
                    "prefill_term_selected_ms": view["prefill_term_selected_ms"],
                    "load_term_selected_ms": view["load_term_selected_ms"],
                    "expected_net_ms": view["expected_net_ms"],
                    "actual_executed_worker": "",
                    "actual_ttft_ms": "",
                    "regret_label": "MODEL-DERIVED",
                })
    return snapshots, routes


def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("")
        return
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def public_cell(cell: dict) -> dict:
    keep = (
        "cell", "seed", "holder_order", "admission_order", "digest", "cache_salt", "load_salt",
        "identity", "timing", "status", "notes", "residency_problems", "reprobe_problems",
        "warmup_wall_s", "active_wall_s", "coverages", "prompt_tokens", "idle_before_load",
        "idle_after_load", "load_started_unix", "load_finished_unix", "launches", "hold_results",
        "warmups", "probes", "reprobes",
    )
    out = {key: cell.get(key) for key in keep}
    out["samples"] = [
        {key: sample[key] for key in (
            "scenario", "decision_index", "timestamp_unix", "rel_s", "loads", "running", "waiting",
            "confirmation", "delta_load", "kv_cache_usage_perc",
        )}
        | {
            "regret_vs_full_ms": next(view["regret_vs_full_ms"] for view in sample["views"] if view["view"] == "krep2"),
            "full_selected": next(view["selected_worker"] for view in sample["views"] if view["view"] == "full"),
            "cap_selected": next(view["selected_worker"] for view in sample["views"] if view["view"] == "krep2"),
            "best_owner_hidden": next(view["best_owner_hidden"] for view in sample["views"] if view["view"] == "krep2"),
            "selected_changed": next(view["selected_changed_vs_full"] for view in sample["views"] if view["view"] == "krep2"),
            "forwarded_ads": next(view["forwarded_ads"] for view in sample["views"] if view["view"] == "krep2"),
            "dropped_ads": next(view["dropped_ads"] for view in sample["views"] if view["view"] == "krep2"),
            "full_is_model_argmin": next(view["full_is_model_argmin"] for view in sample["views"] if view["view"] == "full"),
        }
        for sample in cell.get("samples", [])
    ]
    return out


async def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    harness = load_module("f6p2_harness", HARNESS_PATH)
    relay_mod = load_module("f6p2_relay", RELAY_PATH)
    budget_started = time.perf_counter()
    finished: list[dict] = []
    skipped = []
    async with aiohttp.ClientSession() as session:
        for url in URLS:
            async with session.get(url + "/v1/models", timeout=aiohttp.ClientTimeout(total=10)) as response:
                if response.status != 200:
                    raise RuntimeError(f"{url} not ready: {response.status}")
        for index, spec in enumerate(CELLS):
            if spec["gate"]:
                reliable = any(has_confirmed_load(cell) and cell.get("status") == "ok" for cell in finished)
                if not reliable:
                    skipped.append({"cell": spec["cell"], "reason": "A and B produced no engine-confirmed nonzero load snapshot"})
                    break
            if index:
                idle_ok, metrics = await wait_engine_idle(30)
                if not idle_ok:
                    skipped.append({
                        "cell": spec["cell"],
                        "reason": "engine did not return to idle",
                        "running": [item["running"] for item in metrics],
                        "waiting": [item["waiting"] for item in metrics],
                    })
                    break
            cell = await run_cell(session, harness, relay_mod, spec, budget_started)
            finished.append(cell)
            (OUT / "raw_cells.json").write_text(json.dumps(
                {"cells": [public_cell(item) for item in finished], "skipped": skipped}, indent=2, default=str,
            ))
            print(json.dumps({
                "cell": cell["cell"],
                "status": cell.get("status"),
                "active_wall_s": cell.get("active_wall_s"),
                "confirmed_nonzero": has_confirmed_load(cell),
                "ramp": [
                    {"i": sample["decision_index"], "loads": sample["loads"], "running": sample["running"],
                     "label": sample["confirmation"], "delta": sample["delta_load"],
                     "regret": next(view["regret_vs_full_ms"] for view in sample["views"] if view["view"] == "krep2")}
                    for sample in cell.get("samples", []) if sample["scenario"] == "ramp"
                ],
            }, indent=2), flush=True)
    snapshots, routes = flatten(finished)
    write_csv(OUT / "observed_snapshots.csv", snapshots)
    write_csv(OUT / "counterfactual_routing.csv", routes)
    (OUT / "raw_cells.json").write_text(json.dumps(
        {"cells": [public_cell(item) for item in finished], "skipped": skipped,
         "active_budget_s": time.perf_counter() - budget_started},
        indent=2, default=str,
    ))
    print(json.dumps({"skipped": skipped, "active_budget_s": time.perf_counter() - budget_started}))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
