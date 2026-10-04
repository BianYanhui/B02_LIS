"""Experiments 4 and 5: four real vLLM workers share the control path with replay.

Background events use worker ids 16 and above. Adaptive only sees ordinary
frames, so it cannot tell a real completion from a replayed one. Ideal applies
the real worker's update to the local view immediately and does not send it.

The dispatcher and the background sender run on their own thread. The request
thread only chooses a worker and waits on vLLM, so client TTFT is not the
cost of unpacking the background stream.
"""
from __future__ import annotations

import asyncio
import contextlib
import csv
import hashlib
import importlib.util
import json
import random
import sys
import threading
import time
from pathlib import Path

import aiohttp

from experiments.icc_kv.runtime import OUT, ROOT, PathRuntime, prepare_fixed_gateway, write_json
from experiments.icc_kv.wire import K_TOMB, K_UP

# Request prefixes are long enough to clear the 1024-token admission bar.
# Background noise is a new short prefix every event, so merge cannot collapse it.
USEFUL_POOL = 16
USEFUL_COVERAGE = 4096
NOISE_COVERAGE = 256


def load_formal():
    path = ROOT / "experiments/4t4/run_formal4t4.py"
    spec = importlib.util.spec_from_file_location("formal4t4_icc_e2e", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class Placement:
    """Same rule for the tested view and the local truth.

    Coverage wins. Ties, and the case with no coverage anywhere, use the
    smaller in-flight load and then a shared round-robin cursor. The cursor
    advances once per request, after both choices, so the two choices see
    the same tie.
    """

    def __init__(self) -> None:
        self.loads = [0, 0, 0, 0]
        self.rr = 0

    def select(self, view: dict[tuple[int, int], int], digest: int) -> tuple[int, int]:
        coverages = [view.get((worker, digest), 0) for worker in range(4)]
        best = max(coverages)
        if best > 0:
            candidates = [worker for worker, coverage in enumerate(coverages) if coverage == best]
        else:
            candidates = [0, 1, 2, 3]
        minimum = min(self.loads[worker] for worker in candidates)
        ties = [worker for worker in candidates if self.loads[worker] == minimum]
        return ties[self.rr % len(ties)], best


class ControlPlane:
    """Gateway agent, dispatcher, and background offer, off the request loop."""

    def __init__(self) -> None:
        self.runtime = PathRuntime()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.thread: threading.Thread | None = None
        self.ready = threading.Event()
        self.failed: BaseException | None = None
        self._bg: asyncio.Task | None = None
        self._bg_stop: asyncio.Event | None = None
        self._shutdown: asyncio.Event | None = None

    def start(self) -> None:
        self.thread = threading.Thread(target=self._run, name="icc-control", daemon=True)
        self.thread.start()
        if not self.ready.wait(30):
            raise RuntimeError(f"control plane did not start: {self.failed!r}")
        if self.failed is not None:
            raise RuntimeError("control plane failed") from self.failed

    def _run(self) -> None:
        try:
            asyncio.run(self._amain())
        except BaseException as exc:
            self.failed = exc
            self.ready.set()

    async def _amain(self) -> None:
        self.loop = asyncio.get_running_loop()
        self._shutdown = asyncio.Event()
        self._bg_stop = asyncio.Event()
        await self.runtime.start()
        self.ready.set()
        await self._shutdown.wait()
        if self._bg is not None:
            self._bg.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._bg
            self._bg = None
        await self.runtime.stop()

    def _submit(self, coro):
        if self.loop is None:
            raise RuntimeError("control plane is not running")
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout=180)

    def configure(self, cell: int, method: str, rate: float) -> None:
        self._submit(self.runtime.configure(cell, method, rate))

    def start_background(self, cell: int, scenario: str, rho: float, capacity: float) -> None:
        async def launch() -> None:
            assert self._bg_stop is not None
            self._bg_stop = asyncio.Event()

            async def background() -> None:
                bg_seq = 1_000_000_000
                started = time.perf_counter()
                assert self._bg_stop is not None
                while not self._bg_stop.is_set():
                    elapsed = time.perf_counter() - started
                    if scenario == "burst" and (elapsed % 30) >= 25:
                        level, window = rho * capacity * 5, 5.0
                    else:
                        level, window = rho * capacity, 5.0
                    sent = await offer_noise(self.runtime, cell, level, window, bg_seq)
                    bg_seq += sent

            self._bg = asyncio.create_task(background())

        self._submit(launch())

    def stop_background(self) -> None:
        async def halt() -> None:
            if self._bg_stop is not None:
                self._bg_stop.set()
            if self._bg is not None:
                self._bg.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._bg
                self._bg = None

        self._submit(halt())

    def emit(self, kind: int, worker: int, cell: int, seq: int, coverage: int, digest: int) -> None:
        self._submit(self.runtime.send_foreground(kind, worker, cell, seq, coverage, digest, time.time()))

    def view(self) -> dict[tuple[int, int], int]:
        return self.runtime.dispatcher.snapshot()

    def set_local(self, worker: int, digest: int, coverage: int) -> None:
        self.runtime.dispatcher.set_local(worker, digest, coverage)

    def stats(self) -> dict:
        try:
            return self._submit(self.runtime.fetch_stats())
        except Exception as exc:
            return {"stats_error": repr(exc)[:160]}

    def frame_counts(self) -> tuple[int, int]:
        dispatcher = self.runtime.dispatcher
        with dispatcher.lock:
            return dispatcher.frames, dispatcher.foreground_frames

    def stop(self) -> None:
        if self.loop is not None and self._shutdown is not None:
            self.loop.call_soon_threadsafe(self._shutdown.set)
        if self.thread is not None:
            self.thread.join(timeout=20)


def noise_digest(seq: int) -> int:
    return int.from_bytes(hashlib.blake2b(f"noise-{seq}".encode(), digest_size=8).digest(), "big")


async def offer_noise(runtime: PathRuntime, cell: int, rate: float, seconds: float, seq0: int) -> int:
    """Pace unique short prefixes. Each digest is new, so semantic merge cannot collapse the flood."""
    count = max(1, int(rate * seconds))
    batch = max(1, int(rate / 200)) if rate >= 200 else 1
    started = time.perf_counter()
    sent = 0
    while sent < count and not (time.perf_counter() - started >= seconds + 1):
        target = started + sent / max(rate, 1e-6)
        delay = target - time.perf_counter()
        if delay > 0:
            await asyncio.sleep(delay)
        now = time.time()
        step = min(batch, count - sent)
        for offset in range(step):
            seq = seq0 + sent + offset
            await runtime.send_event(
                K_UP, 16 + (seq % 4), cell, seq, NOISE_COVERAGE, noise_digest(seq), now,
            )
        sent += step
        if sent % 400 == 0:
            await runtime.drain_agent()
    await runtime.drain_agent()
    return sent


def prom_total(text: str, name: str) -> float:
    total = 0.0
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        key = line.split(" ", 1)[0].split("{", 1)[0]
        if key == name:
            total += float(line.split()[-1])
    return total


async def metric_totals(session: aiohttp.ClientSession, urls: list[str]) -> list[tuple[float, float]]:
    rows = []
    for url in urls:
        try:
            async with session.get(url + "/metrics", timeout=aiohttp.ClientTimeout(total=5)) as response:
                text = await response.text()
        except Exception:
            rows.append((0.0, 0.0))
            continue
        rows.append((
            prom_total(text, "vllm:time_to_first_token_seconds_sum"),
            prom_total(text, "vllm:time_to_first_token_seconds_count"),
        ))
    return rows


async def reset_prefix_caches(session: aiohttp.ClientSession, urls: list[str]) -> None:
    for url in urls:
        async with session.post(url + "/reset_prefix_cache", timeout=aiohttp.ClientTimeout(total=30)) as response:
            if response.status != 200:
                body = (await response.text())[:120]
                raise RuntimeError(f"reset_prefix_cache {url} -> {response.status} {body}")


async def run_e2e(
    trace: Path, capacity: float, scenarios: list[str], methods: list[str],
    seeds: int, requests_per_run: int, concurrency: int, kv_tokens: int, out_dir: Path,
) -> None:
    formal = load_formal()
    prepare_fixed_gateway()
    control = ControlPlane()
    control.start()
    try:
        await formal.check_endpoints()
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / "e2e_summary.json"
        rows: list[dict] = json.loads(path.read_text()).get("rows", []) if path.exists() else []
        done = {(row["seed"], row["scenario"], row["method"]) for row in rows}
        cell = 1 + len(rows)
        run_id = time.time_ns()
        import bisect
        cdf = formal.zipf_cdf(1.2, USEFUL_POOL)
        rhos = {"normal": 0.5, "near": 0.9, "high": 1.2, "xhigh": 1.5, "ultrahigh": 2.0, "burst": 0.9}

        for seed in range(seeds):
            for scenario_index, scenario in enumerate(scenarios):
                order = list(methods)
                random.Random(seed * 1009 + scenario_index * 9176).shuffle(order)
                for method in order:
                    if (seed, scenario, method) in done:
                        continue
                    rho = rhos[scenario]
                    rate = capacity if method == "RateFIFO" else 0.0
                    cell_id = cell
                    salt = f"icc-noise-{run_id}-{cell_id}"
                    started = time.perf_counter()
                    if method != "Ideal":
                        control.configure(cell_id, method, rate)
                    else:
                        control.configure(cell_id, "FullSync", 0.0)
                    control.start_background(cell_id, scenario, rho, capacity)
                    shadows = [formal.ShadowCache(kv_tokens) for _ in range(4)]
                    truth: dict[tuple[int, int], int] = {}
                    placement = Placement()
                    seq = 1
                    rng = random.Random(seed)
                    planned = []
                    for request_id in range(requests_per_run):
                        slot = bisect.bisect_left(cdf, rng.random())
                        planned.append((request_id, slot))
                    records = []
                    failed = 0
                    loop_lags: list[float] = []
                    stop_probe = asyncio.Event()

                    async def probe() -> None:
                        while not stop_probe.is_set():
                            mark = time.perf_counter()
                            try:
                                await asyncio.wait_for(stop_probe.wait(), timeout=0.05)
                            except asyncio.TimeoutError:
                                loop_lags.append((time.perf_counter() - mark - 0.05) * 1000)

                    probe_task = asyncio.create_task(probe())
                    try:
                        async with aiohttp.ClientSession() as session:
                            before = await metric_totals(session, formal.URLS)
                            await reset_prefix_caches(session, formal.URLS)
                            for offset in range(0, len(planned), concurrency):
                                tested = control.view()
                                decisions = []
                                for request_id, slot in planned[offset:offset + concurrency]:
                                    digest_s = f"U{slot:04d}"
                                    digest = formal.digest64(digest_s)
                                    coverage = USEFUL_COVERAGE
                                    ideal, ideal_cov = placement.select(truth, digest)
                                    routed, _view_best = placement.select(tested, digest)
                                    placement.rr += 1
                                    placement.loads[routed] += 1
                                    truth_cov = max((truth.get((worker, digest), 0) for worker in range(4)), default=0)
                                    view_cov_max = max((tested.get((worker, digest), 0) for worker in range(4)), default=0)
                                    view_cov = tested.get((routed, digest), 0)
                                    decisions.append({
                                        "request_id": request_id,
                                        "slot": slot,
                                        "digest_s": digest_s,
                                        "digest": digest,
                                        "coverage": coverage,
                                        "ideal": ideal,
                                        "ideal_cov": ideal_cov,
                                        "routed": routed,
                                        "loose_false_negative": int(truth_cov >= 512 and view_cov_max < 512),
                                        "loose_false_positive": int(view_cov >= 512 and view_cov > truth.get((routed, digest), 0)),
                                        "coverage_shortfall": int(truth.get((routed, digest), 0) < ideal_cov),
                                    })
                                responses = await asyncio.gather(*[
                                    formal.one_request(
                                        session, formal.URLS[item["routed"]],
                                        formal.prompt_for(formal.TraceRequest(
                                            request_id=item["request_id"], phase=0, lineage_id=item["slot"], step=0,
                                            tenant=f"tenant-{item['slot'] % 8}", digest=item["digest_s"],
                                            coverage_tokens=item["coverage"], workload="reuse_intensive", discard=False,
                                        )),
                                        salt, 4, 2,
                                    )
                                    for item in decisions
                                ])
                                for item, response in zip(decisions, responses):
                                    routed = item["routed"]
                                    digest = item["digest"]
                                    coverage = item["coverage"]
                                    placement.loads[routed] -= 1
                                    if not response["ok"]:
                                        failed += 1
                                        continue
                                    regret = max(0, item["ideal_cov"] - truth.get((routed, digest), 0))
                                    evicted = shadows[routed].insert(item["digest_s"], coverage)
                                    truth[(routed, digest)] = coverage
                                    if method == "Ideal":
                                        control.set_local(routed, digest, coverage)
                                    else:
                                        control.emit(K_UP, routed, cell_id, seq, coverage, digest)
                                        seq += 1
                                        for victim in evicted:
                                            victim_digest = formal.digest64(victim)
                                            truth.pop((routed, victim_digest), None)
                                            control.emit(K_TOMB, routed, cell_id, seq, 0, victim_digest)
                                            seq += 1
                                    records.append({
                                        "ttft_ms": response["ttft_ms"],
                                        "reused_tokens": int(response["vllm_cached_tokens"] or 0),
                                        "prefill_tokens": max(0, int(response["input_tokens"] or 0) - int(response["vllm_cached_tokens"] or 0)),
                                        "routed": routed,
                                        "ideal": item["ideal"],
                                        "wrong_placement": int(routed != item["ideal"] and item["ideal_cov"] > 0),
                                        "coverage_regret": regret,
                                        "coverage_shortfall": item["coverage_shortfall"],
                                        "loose_false_negative": item["loose_false_negative"],
                                        "loose_false_positive": item["loose_false_positive"],
                                    })
                            after = await metric_totals(session, formal.URLS)
                    finally:
                        stop_probe.set()
                        probe_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await probe_task
                        control.stop_background()
                    counts = [0, 0, 0, 0]
                    for row in records:
                        counts[row["routed"]] += 1
                    sum_delta = sum(after[index][0] - before[index][0] for index in range(4))
                    count_delta = sum(after[index][1] - before[index][1] for index in range(4))
                    frames, foreground_frames = control.frame_counts()
                    ttfts = [row["ttft_ms"] for row in records]
                    ordered = sorted(ttfts)
                    lags = sorted(loop_lags)

                    def pct(values: list[float], p: float) -> float:
                        if not values:
                            return 0.0
                        return values[min(len(values) - 1, round(p / 100 * (len(values) - 1)))]

                    def mean(values: list[float]) -> float:
                        return sum(values) / len(values) if values else 0.0

                    summary = {
                        "seed": seed,
                        "scenario": scenario,
                        "method": method,
                        "method_order": order,
                        "cell": cell,
                        "cache_salt": salt,
                        "requests": len(records),
                        "failed_requests": failed,
                        "elapsed_s": round(time.perf_counter() - started, 1),
                        "ttft_mean_ms": mean(ttfts),
                        "ttft_p95_ms": pct(ordered, 95),
                        "server_ttft_mean_ms": (sum_delta / count_delta * 1000) if count_delta > 0 else 0.0,
                        "loop_lag_p95_ms": pct(lags, 95),
                        "slo_violation_2s": sum(value > 2000 for value in ttfts) / len(ttfts) if ttfts else 0.0,
                        "prefill_tokens_mean": mean([row["prefill_tokens"] for row in records]),
                        "reused_tokens_mean": mean([row["reused_tokens"] for row in records]),
                        "wrong_placement_rate": mean([row["wrong_placement"] for row in records]),
                        "coverage_shortfall_rate": mean([row["coverage_shortfall"] for row in records]),
                        "coverage_regret_mean": mean([row["coverage_regret"] for row in records]),
                        "loose_false_negative_rate": mean([row["loose_false_negative"] for row in records]),
                        "loose_false_positive_rate": mean([row["loose_false_positive"] for row in records]),
                        "placement_counts": counts,
                        "dispatcher_frames": frames,
                        "foreground_frames": foreground_frames,
                        "capacity_events_per_s": capacity,
                        "background_rho": rho,
                        "concurrency": concurrency,
                        "useful_pool": USEFUL_POOL,
                        "useful_coverage": USEFUL_COVERAGE,
                        "noise_coverage": NOISE_COVERAGE,
                        **control.stats(),
                    }
                    rows.append(summary)
                    write_json(path, {**control.runtime.meta(), "rows": rows})
                    if records:
                        csv_path = out_dir / f"requests_{scenario}_{method}_seed{seed}.csv"
                        with csv_path.open("w", newline="") as handle:
                            writer = csv.DictWriter(handle, fieldnames=list(records[0]), lineterminator="\n")
                            writer.writeheader()
                            writer.writerows(records)
                    print(summary, flush=True)
                    cell += 1
    finally:
        control.stop()


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--trace", type=Path, default=OUT / "trace" / "events.csv")
    parser.add_argument("--capacity", type=float, required=True)
    parser.add_argument("--scenarios", default="normal,near,burst")
    parser.add_argument("--methods", default="FullSync,StaticSemantic,Adaptive,Ideal")
    parser.add_argument("--seeds", type=int, default=5)
    parser.add_argument("--requests", type=int, default=500)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--kv-cache-tokens", type=int, default=104544)
    parser.add_argument("--out-dir", type=Path, default=OUT / "e2e")
    args = parser.parse_args()
    asyncio.run(run_e2e(
        args.trace, args.capacity,
        [item for item in args.scenarios.split(",") if item],
        [item for item in args.methods.split(",") if item],
        args.seeds, args.requests, args.concurrency, args.kv_cache_tokens, args.out_dir,
    ))


if __name__ == "__main__":
    main()
