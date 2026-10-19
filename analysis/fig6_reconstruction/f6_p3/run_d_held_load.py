#!/usr/bin/env python3
"""F6-P3 stable held-load smoke. One prefix, two phases.

Holder placement and sample times were fixed before the run.
Do not add requests after a phase fails to reach its target vector.
Does not modify experiments/. Assumes vLLM is already listening on 8000-8003.
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
OUT = ROOT / "analysis/fig6_reconstruction/f6_p3"
HARNESS_PATH = ROOT / "experiments/4t4/run_formal4t4.py"
RELAY_PATH = ROOT / "experiments/4t4/net/gateway_relay_4t4.py"
MODEL = "/home/byh/.cache/modelscope/qwen/Qwen2.5-1.5B-Instruct"
URLS = [f"http://127.0.0.1:{8000 + index}" for index in range(4)]

# Locked before any F6-P3 routing result.
PREFIX_WORDS = 1024
TENANT = "tenant-0"
HOLD_TOKENS = 640
CONFIRM_WAIT_S = 5.0
POLL_S = 0.25
N_SAMPLES = 4
SAMPLE_INTERVAL_S = 0.50
FALLBACK_SAMPLE_START_S = 1.0
PREFILL_TOKENS_PER_MS = 50.0
QUEUE_PENALTY_MS = 2.0
GUARD_MS = 0.5
MIN_CACHED_TOKENS = 1024
WARMUP_TIMEOUT_S = 120.0
ACTIVE_BUDGET_S = 300.0
BLOCK = 16
SEED = 1
ADMISSION_ORDER = [0, 1, 2, 3]
VISIBLE_OWNERS = (0, 1)
HIDDEN_OWNERS = (2, 3)

# D-positive: visible owners hold one request, hidden owners hold none.
# D-negative: the opposite. Targets create the condition; they are not injected loads.
PHASES = [
    {"phase": "D-positive", "holders": [0, 1], "target_loads": [1, 1, 0, 0], "target_delta": 1},
    {"phase": "D-negative", "holders": [2, 3], "target_loads": [0, 0, 1, 1], "target_delta": -1},
]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def shared_prefix(digest: str) -> str:
    return f"[KV shared prefix id={digest} tenant={TENANT}]\n" + ("context " * PREFIX_WORDS)


def user_prompt(digest: str, turn: int) -> str:
    return shared_prefix(digest) + f"\nTask turn {turn}, variant 0: respond with OK."


def load_prompt(phase: str, job_id: int) -> str:
    return f"F6-P3 held load phase {phase} job {job_id} seed {SEED}. Reply with OK."


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


async def one_request(session, url: str, prompt: str, cache_salt: str, output_tokens: int) -> dict:
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
    async with session.post(url + "/v1/chat/completions", json=payload, timeout=aiohttp.ClientTimeout(total=180)) as response:
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
                prompt_tokens: int) -> list[dict]:
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
        dispatcher.rr = 0
        for instance in admitted:
            dispatcher.apply_upsert(instance, digest, int(coverages[instance]), 0.0, 0.0)
        selected, _raw, _evaluated, affinity, view_coverage, expected = dispatcher.choose(
            harness.TraceRequest(
                request_id=0, phase=0, lineage_id=0, step=1, tenant=TENANT,
                digest=digest, coverage_tokens=int(coverages[0]), workload="f6p3", discard=False,
            )
        )
        visible = [index for index in range(4) if dispatcher.index[index].get(digest, 0) > 0]
        hidden = [index for index in range(4) if index not in visible]
        decisions[name] = selected
        views.append({
            "view": name,
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
        row["full_is_model_argmin"] = int(full_selected in argmin)
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


async def wait_engine_idle(timeout_s: float = 40.0) -> tuple[bool, list[dict]]:
    deadline = time.perf_counter() + timeout_s
    metrics = await poll_metrics()
    while time.perf_counter() < deadline:
        if all(int(item["running"]) == 0 and int(item["waiting"]) == 0 for item in metrics):
            return True, metrics
        await asyncio.sleep(0.2)
        metrics = await poll_metrics()
    return False, metrics


async def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    harness = load_module("f6p3_harness", HARNESS_PATH)
    relay_mod = load_module("f6p3_relay_d", RELAY_PATH)
    digest = "F6P3D"
    cache_salt = "f6p3-prefix-seed1"
    identity = client_prefix_identity(digest)
    digest_int = harness.digest64(digest)
    started = time.perf_counter()
    report: dict = {
        "seed": SEED,
        "digest": digest,
        "cache_salt": cache_salt,
        "admission_order": ADMISSION_ORDER,
        "timing": {
            "hold_tokens": HOLD_TOKENS,
            "confirm_wait_s": CONFIRM_WAIT_S,
            "poll_s": POLL_S,
            "n_samples": N_SAMPLES,
            "sample_interval_s": SAMPLE_INTERVAL_S,
            "fallback_sample_start_s": FALLBACK_SAMPLE_START_S,
        },
        "identity": identity,
        "ttft_pairing": "TTFT_PAIRING_NOT_READY",
        "ttft_pairing_reason": [
            "no prefix-cache snapshot that can be restored without reset_prefix_cache",
            "a request on the full-view worker changes that worker's KV suffix and running gauge",
            "checking residency again is itself a request",
            "the held loads differ across the two selected workers by construction",
            "reset_prefix_cache would drop the shared prefix and the in-flight holders",
        ],
        "phases": [],
    }
    async with aiohttp.ClientSession() as session:
        for url in URLS:
            async with session.get(url + "/v1/models", timeout=aiohttp.ClientTimeout(total=10)) as response:
                if response.status != 200:
                    raise RuntimeError(f"{url} not ready: {response.status}")
        metrics_before = await poll_metrics()
        report["metrics_before_warmup"] = metrics_before
        warmups = list(await asyncio.wait_for(asyncio.gather(*[
            one_request(session, url, user_prompt(digest, 0), cache_salt, 4) for url in URLS
        ]), timeout=WARMUP_TIMEOUT_S))
        report["warmups"] = warmups
        metrics_mid = await poll_metrics()
        probes = list(await asyncio.gather(*[
            one_request(session, url, user_prompt(digest, 1), cache_salt, 4) for url in URLS
        ]))
        metrics_after = await poll_metrics()
        for worker in range(4):
            probes[worker]["prefix_cache_hits_delta"] = (
                metrics_after[worker]["prefix_cache_hits"] - metrics_mid[worker]["prefix_cache_hits"]
            )
        report["probes"] = probes
        problems = residency_problems(warmups, probes, identity)
        report["residency_problems"] = problems
        if problems:
            report["status"] = "residency_failed"
            (OUT / "raw_held.json").write_text(json.dumps(report, indent=2, default=str))
            print(json.dumps({"status": "residency_failed", "problems": problems}, indent=2))
            return 1
        idle_ok, idle_metrics = await wait_engine_idle(20)
        report["idle_before_load"] = idle_ok
        coverages = [int(item["vllm_cached_tokens"]) for item in probes]
        prompt_tokens = int(probes[0]["input_tokens"])
        report["coverages"] = coverages
        report["prompt_tokens"] = prompt_tokens
        report["idle_metrics"] = idle_metrics

        for spec in PHASES:
            if ACTIVE_BUDGET_S - (time.perf_counter() - started) < 40:
                report["phases"].append({"phase": spec["phase"], "status": "skipped_budget"})
                break
            idle_ok, idle_metrics = await wait_engine_idle(40)
            phase = {
                "phase": spec["phase"],
                "holders": spec["holders"],
                "target_loads": spec["target_loads"],
                "target_delta": spec["target_delta"],
                "idle_before": idle_ok,
                "samples": [],
                "notes": [],
            }
            if not idle_ok:
                phase["status"] = "not_idle"
                phase["notes"].append("engine was not idle; phase not started")
                phase["running"] = [int(item["running"]) for item in idle_metrics]
                report["phases"].append(phase)
                break
            inflight = [0, 0, 0, 0]
            lock = asyncio.Lock()
            hold_results: list[dict] = []

            async def hold(worker: int, job_id: int, phase_name: str = spec["phase"]) -> None:
                async with lock:
                    inflight[worker] += 1
                try:
                    held = await one_request(
                        session, URLS[worker], load_prompt(phase_name, job_id),
                        f"f6p3-load-{phase_name}-seed{SEED}", HOLD_TOKENS,
                    )
                except Exception as exc:
                    held = {"ok": False, "error": repr(exc)[:200], "ttft_ms": 0.0,
                            "input_tokens": None, "output_tokens": None, "vllm_cached_tokens": 0,
                            "latency_ms": 0.0}
                held["worker"] = worker
                held["job_id"] = job_id
                async with lock:
                    inflight[worker] -= 1
                    hold_results.append(held)

            tasks = [asyncio.create_task(hold(worker, job_id)) for job_id, worker in enumerate(spec["holders"])]
            t0 = time.perf_counter()
            confirmed_at = None
            deadline = t0 + CONFIRM_WAIT_S
            while time.perf_counter() < deadline:
                async with lock:
                    loads = list(inflight)
                metrics = await poll_metrics()
                if loads == spec["target_loads"] and confirmation(loads, metrics) == "ENGINE_CONFIRMED":
                    confirmed_at = time.perf_counter()
                    break
                await asyncio.sleep(POLL_S)
            phase["reached_target"] = confirmed_at is not None
            if confirmed_at is None:
                phase["notes"].append(
                    f"target {spec['target_loads']} was not engine-confirmed within {CONFIRM_WAIT_S}s; "
                    "no extra requests were added"
                )
                sample_origin = t0 + FALLBACK_SAMPLE_START_S
            else:
                sample_origin = confirmed_at
            for decision_index in range(N_SAMPLES):
                target = sample_origin + decision_index * SAMPLE_INTERVAL_S
                delay = target - time.perf_counter()
                if delay > 0:
                    await asyncio.sleep(delay)
                async with lock:
                    loads = list(inflight)
                metrics = await poll_metrics()
                label = confirmation(loads, metrics)
                views = route_views(harness, relay_mod, digest, digest_int, loads, coverages, prompt_tokens)
                cap = next(view for view in views if view["view"] == "krep2")
                full = next(view for view in views if view["view"] == "full")
                phase["samples"].append({
                    "decision_index": decision_index,
                    "timestamp_unix": time.time(),
                    "rel_s": time.perf_counter() - t0,
                    "loads": loads,
                    "running": [int(item["running"]) for item in metrics],
                    "waiting": [int(item["waiting"]) for item in metrics],
                    "kv_cache_usage_perc": [item["kv_cache_usage_perc"] for item in metrics],
                    "confirmation": label,
                    "matches_target": int(loads == spec["target_loads"] and label == "ENGINE_CONFIRMED"),
                    "delta_load": delta_load(loads),
                    "full_selected": full["selected_worker"],
                    "cap_selected": cap["selected_worker"],
                    "regret_vs_full_ms": cap["regret_vs_full_ms"],
                    "full_is_model_argmin": full["full_is_model_argmin"],
                    "selected_changed": cap["selected_changed_vs_full"],
                    "visible_owners": cap["visible_owners"],
                    "suppressed_owners": cap["suppressed_owners"],
                    "forwarded_ads": cap["forwarded_ads"],
                    "dropped_ads": cap["dropped_ads"],
                    "affinity": int(cap["affinity"]),
                    "costs": cap["costs"],
                })
            if tasks:
                await asyncio.wait(tasks, timeout=90)
            idle_after, after_metrics = await wait_engine_idle(45)
            phase["idle_after"] = idle_after
            phase["running_after"] = [int(item["running"]) for item in after_metrics]
            phase["hold_results"] = [
                {key: item.get(key) for key in ("ok", "error", "worker", "job_id", "ttft_ms", "latency_ms",
                                                "input_tokens", "output_tokens", "vllm_cached_tokens")}
                for item in hold_results
            ]
            if not all(item.get("ok") for item in hold_results):
                phase["notes"].append("one or more holders failed")
            reprobes = list(await asyncio.gather(*[
                one_request(session, url, user_prompt(digest, 2 + PHASES.index(spec)), cache_salt, 4)
                for url in URLS
            ]))
            phase["reprobes_cached"] = [int(item["vllm_cached_tokens"]) for item in reprobes]
            phase["reprobes_ok"] = all(
                item["ok"] and int(item["vllm_cached_tokens"]) >= MIN_CACHED_TOKENS for item in reprobes
            )
            phase["status"] = "ok" if phase["reached_target"] else "target_not_held"
            phase["active_wall_s"] = time.perf_counter() - t0
            report["phases"].append(phase)
            (OUT / "raw_held.json").write_text(json.dumps(report, indent=2, default=str))
            print(json.dumps({
                "phase": phase["phase"],
                "status": phase["status"],
                "reached_target": phase["reached_target"],
                "samples": [
                    {"i": sample["decision_index"], "loads": sample["loads"], "running": sample["running"],
                     "label": sample["confirmation"], "delta": sample["delta_load"],
                     "regret": sample["regret_vs_full_ms"], "full": sample["full_selected"],
                     "cap": sample["cap_selected"]}
                    for sample in phase["samples"]
                ],
            }, indent=2), flush=True)
            if not idle_after:
                report["stopped_because"] = "engine did not return to idle"
                break
    report["active_wall_s"] = time.perf_counter() - started
    report["status"] = "ok"
    (OUT / "raw_held.json").write_text(json.dumps(report, indent=2, default=str))
    rows = []
    for phase in report["phases"]:
        for sample in phase.get("samples", []):
            rows.append({
                "phase": phase["phase"],
                "target_loads": ";".join(map(str, phase.get("target_loads", []))),
                "target_delta": phase.get("target_delta", ""),
                "decision_index": sample["decision_index"],
                "rel_s": f"{sample['rel_s']:.3f}",
                "loads": ";".join(map(str, sample["loads"])),
                "running": ";".join(map(str, sample["running"])),
                "waiting": ";".join(map(str, sample["waiting"])),
                "confirmation": sample["confirmation"],
                "matches_target": sample["matches_target"],
                "delta_load": sample["delta_load"],
                "full_selected": sample["full_selected"],
                "cap_selected": sample["cap_selected"],
                "regret_vs_full_ms": sample["regret_vs_full_ms"],
                "full_is_model_argmin": sample["full_is_model_argmin"],
                "selected_changed": sample["selected_changed"],
                "visible_owners": ";".join(map(str, sample["visible_owners"])),
                "suppressed_owners": ";".join(map(str, sample["suppressed_owners"])),
                "forwarded_ads": sample["forwarded_ads"],
                "dropped_ads": sample["dropped_ads"],
                "kv_cache_usage_perc": ";".join(f"{value:.6f}" for value in sample["kv_cache_usage_perc"]),
                "coverages": ";".join(map(str, report.get("coverages", []))),
                "prompt_tokens": report.get("prompt_tokens", ""),
            })
    if rows:
        with (OUT / "held_samples.csv").open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
    print(json.dumps({"status": report["status"], "active_wall_s": report["active_wall_s"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
