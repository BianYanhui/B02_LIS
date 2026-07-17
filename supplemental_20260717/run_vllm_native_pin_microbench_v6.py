#!/usr/bin/env python3
"""Benchmark B02 ValidateAndPin against vLLM's live BlockPool.

This script targets the experimental developer endpoints supplied by
vllm_0.10.2_b02_native_pin.patch.  A successful validation calls vLLM's
BlockPool.touch() while the EngineCore utility loop is handling the request;
the matching release calls BlockPool.free_blocks() on the same runtime.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import statistics
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def percentile(values: list[float], percentile_value: float) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    index = round((len(values) - 1) * percentile_value / 100.0)
    return float(values[max(0, min(len(values) - 1, index))])


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


class OwnerClient:
    def __init__(self, base_url: str, model: str, timeout_s: float) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_s = timeout_s

    def post(self, path: str, payload: dict[str, Any]) -> tuple[dict[str, Any], float]:
        body = json.dumps(payload).encode("utf-8")
        request = Request(self.base_url + path, data=body,
                          headers={"Content-Type": "application/json"},
                          method="POST")
        started = time.perf_counter_ns()
        try:
            with urlopen(request, timeout=self.timeout_s) as response:
                result = json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            result = {"ok": False, "reason": f"http_{error.code}",
                      "body": error.read().decode("utf-8", "replace")}
        return result, (time.perf_counter_ns() - started) / 1e3

    def get(self, path: str) -> tuple[dict[str, Any], float]:
        started = time.perf_counter_ns()
        with urlopen(self.base_url + path, timeout=self.timeout_s) as response:
            result = json.loads(response.read().decode("utf-8"))
        return result, (time.perf_counter_ns() - started) / 1e3

    def tokenize(self, prompt: str) -> list[int]:
        result, _ = self.post("/tokenize", {"model": self.model, "prompt": prompt})
        if "tokens" not in result:
            raise RuntimeError(f"tokenize failed: {result}")
        return [int(token) for token in result["tokens"]]

    def warm_prefix(self, prompt: str) -> None:
        result, _ = self.post("/v1/completions", {
            "model": self.model,
            "prompt": prompt,
            "max_tokens": 4,
            "min_tokens": 4,
            "temperature": 0,
            "seed": 17,
            "ignore_eos": True,
        })
        if "choices" not in result:
            raise RuntimeError(f"warm prefix failed: {result}")

    def advertise(self, token_ids: list[int], lease_ms: int = 60000) -> dict[str, Any]:
        result, _ = self.post("/b02/native_pin/advertise", {
            "token_ids": token_ids,
            "tenant": "tenant-b02",
            "model_revision": "qwen2.5-1.5b-instruct-r0",
            "lease_ms": lease_ms,
        })
        if not result.get("ok"):
            raise RuntimeError(f"advertise failed: {result}")
        return result

    def validate(self, ad: dict[str, Any], *, epoch: int | None = None,
                 sequence: int | None = None, tenant: str = "tenant-b02",
                 model_revision: str = "qwen2.5-1.5b-instruct-r0",
                 min_coverage_tokens: int = 1) -> tuple[dict[str, Any], float]:
        return self.post("/b02/native_pin/validate", {
            "digest": ad["digest"],
            "epoch": ad["epoch"] if epoch is None else epoch,
            "sequence": ad["sequence"] if sequence is None else sequence,
            "tenant": tenant,
            "model_revision": model_revision,
            "min_coverage_tokens": min_coverage_tokens,
        })

    def release(self, pin_handle: str, reason: str = "release") -> tuple[dict[str, Any], float]:
        return self.post("/b02/native_pin/release", {
            "pin_handle": pin_handle, "reason": reason})

    def evict(self, digest: str) -> tuple[dict[str, Any], float]:
        return self.post("/b02/native_pin/evict", {"digest": digest})


def make_prompt() -> str:
    return ("B02 native owner validation benchmark. The identical prefix "
            "must be tokenized once, cached by vLLM, and protected by a "
            "runtime BlockPool reference. " + "prefix ledger context " * 768)


def commit_hash() -> str:
    try:
        return subprocess.check_output(
            ["git", "-C", "/home/byh/B02", "rev-parse", "HEAD"], text=True).strip()
    except Exception:
        return "unknown"


def summarize(scenario: str, latencies: list[float], successes: int,
              fallbacks: int, unsafe: int, elapsed_s: float,
              **extra: Any) -> dict[str, Any]:
    return {
        "experiment": "vllm_native_validate_and_pin_v6",
        "evidence_type": "live_vllm_runtime_microbenchmark",
        "runtime": "vLLM 0.10.2 BlockPool.touch/free_blocks",
        "scenario": scenario,
        "operations": len(latencies),
        "validation_success_count": successes,
        "fallback_count": fallbacks,
        "unsafe_reuse_count": unsafe,
        "validate_p50_us": percentile(latencies, 50),
        "validate_p95_us": percentile(latencies, 95),
        "validate_p99_us": percentile(latencies, 99),
        "throughput_ops_s": len(latencies) / elapsed_s if elapsed_s else 0.0,
        **extra,
        "status": "Current",
    }


def run_valid_parallel(client: OwnerClient, ad: dict[str, Any], workers: int,
                       ops_per_worker: int, min_coverage_tokens: int) -> dict[str, Any]:
    def work(_: int) -> tuple[bool, float, bool, str]:
        result, latency = client.validate(ad, min_coverage_tokens=min_coverage_tokens)
        if result.get("ok"):
            release, _ = client.release(result["pin_handle"])
            return True, latency, not release.get("ok"), release.get("reason", "")
        return False, latency, False, str(result.get("reason"))

    started = time.perf_counter()
    futures = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        for index in range(workers * ops_per_worker):
            futures.append(executor.submit(work, index))
        results = [future.result() for future in as_completed(futures)]
    elapsed = time.perf_counter() - started
    latencies = [result[1] for result in results]
    successes = sum(result[0] for result in results)
    return summarize("valid_parallel", latencies, successes, len(results) - successes,
                     sum(result[2] for result in results), elapsed,
                     concurrent_clients=workers, release_failures=sum(result[2] for result in results),
                     min_coverage_tokens=min_coverage_tokens)


def run_invalid(client: OwnerClient, token_ids: list[int], scenario: str,
                operations: int, min_coverage_tokens: int) -> dict[str, Any]:
    latencies: list[float] = []
    unsafe = 0
    successes = 0
    started = time.perf_counter()
    for _ in range(operations):
        # The eviction scenario intentionally removes the matching physical
        # blocks. Rebuild the identical vLLM prefix before the next trial.
        if scenario == "evicted":
            client.warm_prefix(make_prompt())
        ad = client.advertise(token_ids)
        if scenario == "epoch_mismatch":
            result, latency = client.validate(ad, epoch=ad["epoch"] - 1,
                                              min_coverage_tokens=min_coverage_tokens)
        elif scenario == "sequence_mismatch":
            result, latency = client.validate(ad, sequence=ad["sequence"] - 1,
                                              min_coverage_tokens=min_coverage_tokens)
        elif scenario == "tenant_mismatch":
            result, latency = client.validate(ad, tenant="other-tenant",
                                              min_coverage_tokens=min_coverage_tokens)
        elif scenario == "model_revision_mismatch":
            result, latency = client.validate(ad, model_revision="other-model-r1",
                                              min_coverage_tokens=min_coverage_tokens)
        elif scenario == "lease_expired":
            ad = client.advertise(token_ids, lease_ms=1)
            time.sleep(0.01)
            result, latency = client.validate(ad, min_coverage_tokens=min_coverage_tokens)
        elif scenario == "evicted":
            evict, _ = client.evict(ad["digest"])
            if not evict.get("ok"):
                raise RuntimeError(f"eviction injection failed: {evict}")
            result, latency = client.validate(ad, min_coverage_tokens=min_coverage_tokens)
        else:
            raise ValueError(scenario)
        latencies.append(latency)
        if result.get("ok"):
            unsafe += 1
            client.release(result["pin_handle"])
            successes += 1
    return summarize(scenario, latencies, successes, operations - successes, unsafe,
                     time.perf_counter() - started,
                     concurrent_clients=1, min_coverage_tokens=min_coverage_tokens)


def run_epoch_transition(client: OwnerClient, token_ids: list[int],
                         operations: int, min_coverage_tokens: int) -> dict[str, Any]:
    latencies: list[float] = []
    unsafe = 0
    started = time.perf_counter()
    for _ in range(operations):
        ad = client.advertise(token_ids)
        advance, _ = client.post("/b02/native_pin/advance_epoch", {})
        if not advance.get("ok"):
            raise RuntimeError(f"epoch advance failed: {advance}")
        result, latency = client.validate(ad, min_coverage_tokens=min_coverage_tokens)
        latencies.append(latency)
        if result.get("ok"):
            unsafe += 1
            client.release(result["pin_handle"])
    return summarize("restart_epoch", latencies, 0, operations, unsafe,
                     time.perf_counter() - started, concurrent_clients=1,
                     min_coverage_tokens=min_coverage_tokens)


def run_pin_evict_race(client: OwnerClient, token_ids: list[int], attempts: int,
                       min_coverage_tokens: int) -> dict[str, Any]:
    ad = client.advertise(token_ids)
    validation, latency = client.validate(ad, min_coverage_tokens=min_coverage_tokens)
    if not validation.get("ok"):
        raise RuntimeError(f"cannot create pin for race: {validation}")
    pin_handle = validation["pin_handle"]
    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=min(16, attempts)) as executor:
        futures = [executor.submit(client.evict, ad["digest"]) for _ in range(attempts)]
        evictions = [future.result()[0] for future in as_completed(futures)]
    elapsed = time.perf_counter() - started
    blocked = sum(result.get("reason") == "pinned" for result in evictions)
    invalid_successes = sum(bool(result.get("ok")) for result in evictions)
    release, _ = client.release(pin_handle, reason="cancelled")
    if not release.get("ok"):
        raise RuntimeError(f"cancelled release failed: {release}")
    after_release, _ = client.evict(ad["digest"])
    return summarize("concurrent_evict_while_pinned", [latency], 1, 0,
                     invalid_successes, elapsed, concurrent_clients=min(16, attempts),
                     eviction_attempts=attempts, blocked_evictions=blocked,
                     post_cancel_release_evict_ok=bool(after_release.get("ok")),
                     cancellation_release_ok=True,
                     min_coverage_tokens=min_coverage_tokens)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--model", default="/home/byh/.cache/modelscope/qwen/Qwen2.5-1.5B-Instruct")
    parser.add_argument("--out-dir", default="/home/byh/B02/supplemental_20260717/vllm_native_pin_v6")
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument("--ops-per-worker", type=int, default=64)
    parser.add_argument("--invalid-operations", type=int, default=64)
    parser.add_argument("--eviction-attempts", type=int, default=128)
    parser.add_argument("--timeout-s", type=float, default=30.0)
    args = parser.parse_args()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    client = OwnerClient(args.base_url, args.model, args.timeout_s)
    status, _ = client.get("/b02/native_pin/status")
    if not status.get("ok"):
        raise RuntimeError(f"native owner endpoint unavailable: {status}")
    prompt = make_prompt()
    token_ids = client.tokenize(prompt)
    client.warm_prefix(prompt)
    initial_ad = client.advertise(token_ids)
    min_coverage_tokens = max(16, min(initial_ad["coverage_tokens"], len(token_ids) - 32))
    rows = [run_valid_parallel(client, initial_ad, args.workers,
                               args.ops_per_worker, min_coverage_tokens)]
    for scenario in ("epoch_mismatch", "sequence_mismatch", "tenant_mismatch",
                     "model_revision_mismatch", "lease_expired", "evicted"):
        rows.append(run_invalid(client, token_ids, scenario,
                                args.invalid_operations, min_coverage_tokens))
    # The final eviction trial deliberately removed all matching full blocks.
    # Reconstruct the identical prompt before testing restart and pin races.
    client.warm_prefix(prompt)
    rows.append(run_epoch_transition(client, token_ids, args.invalid_operations,
                                     min_coverage_tokens))
    rows.append(run_pin_evict_race(client, token_ids, args.eviction_attempts,
                                   min_coverage_tokens))
    for row in rows:
        row.update({
            "model": args.model,
            "prefix_tokens": len(token_ids),
            "advertised_coverage_tokens": initial_ad["coverage_tokens"],
            "code_commit": commit_hash(),
        })
    checks = [
        {
            "check_name": "all invalid advertisements fall back without unsafe reuse",
            "status": "PASS" if all(row["unsafe_reuse_count"] == 0 for row in rows) else "FAIL",
            "offending_rows": sum(row["unsafe_reuse_count"] for row in rows),
            "suggested_fix": "inspect owner scope, sequence, lease, epoch, and residency guards",
        },
        {
            "check_name": "pinned runtime blocks reject concurrent eviction and become evictable after cancelled release",
            "status": "PASS" if rows[-1]["blocked_evictions"] == args.eviction_attempts and rows[-1]["post_cancel_release_evict_ok"] else "FAIL",
            "offending_rows": args.eviction_attempts - rows[-1]["blocked_evictions"],
            "suggested_fix": "inspect BlockPool.touch/free_blocks lifecycle and release-on-cancellation path",
        },
        {
            "check_name": "valid validations pin then release successfully",
            "status": "PASS" if rows[0]["validation_success_count"] == rows[0]["operations"] and rows[0]["release_failures"] == 0 else "FAIL",
            "offending_rows": rows[0]["operations"] - rows[0]["validation_success_count"] + rows[0]["release_failures"],
            "suggested_fix": "inspect EngineCore utility dispatch and live prefix-cache residency",
        },
    ]
    if any(check["status"] != "PASS" for check in checks):
        raise RuntimeError(f"native runtime checks failed: {checks}")
    write_csv(out_dir / "vllm_native_validation_microbench.csv", rows)
    write_csv(out_dir / "vllm_native_validation_sanity_checks.csv", checks)
    (out_dir / "run_metadata.json").write_text(json.dumps({
        "arguments": vars(args), "initial_status": status,
        "initial_advertisement": initial_ad, "prefix_sha256": hashlib.sha256(
            json.dumps(token_ids).encode("utf-8")).hexdigest(),
    }, indent=2))
    (out_dir / "README.md").write_text(
        "# vLLM-native ValidateAndPin microbenchmark (V6)\n\n"
        "This is a live vLLM 0.10.2 runtime microbenchmark on one Tesla T4. "
        "The B02 developer endpoint validates scope/version/lease, rescans live prefix-cache blocks, and calls "
        "`BlockPool.touch()` in the serialized EngineCore utility loop; release calls `BlockPool.free_blocks()`. "
        "The test injects stale fields, synthetic owner epoch advance, actual BlockPool eviction, and concurrent eviction attempts while pinned. "
        "It measures owner-side runtime operations, not end-to-end dispatch or multi-node transport.\n"
    )
    print(json.dumps({"rows": len(rows), "checks": checks, "out_dir": str(out_dir)}, indent=2))


if __name__ == "__main__":
    main()
