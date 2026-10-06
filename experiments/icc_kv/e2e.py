"""Experiments 4 and 5: four real vLLM workers share the control path with replay.

Background events use worker ids 16 and above. Adaptive only sees ordinary
frames, so it cannot tell a real completion from a replayed one. Ideal applies
the real worker's update to the local view immediately and does not send it.

The dispatcher process keeps only the routable view. Noise senders are
separate processes. The request process places a request and waits on vLLM.
"""
from __future__ import annotations

import asyncio
import os
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

from experiments.icc_kv.runtime import (
    OUT, ROOT, PathRuntime, cpus_for_vllm, gateway_pid, pin_listeners,
    prepare_fixed_gateway, process_cpu_seconds, reapply_affinity, restore_affinity, write_json,
)
from experiments.icc_kv.split_path import SplitControl
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

    def start_background(self, cell: int, scenario: str, rho: float, capacity: float, workers: int = 4) -> None:
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
                    sent = await offer_noise(self.runtime, cell, level, window, bg_seq, workers)
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

    def clear_local(self, worker: int, digest: int) -> None:
        self.runtime.dispatcher.clear_local(worker, digest)

    def view_timing(self, worker: int, digest: int, now: float) -> tuple[float, float]:
        return self.runtime.dispatcher.view_timing(worker, digest, now)

    def applied(self) -> list:
        dispatcher = self.runtime.dispatcher
        with dispatcher.lock:
            return list(dispatcher.applied)

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


async def offer_noise(runtime: PathRuntime, cell: int, rate: float, seconds: float, seq0: int, workers: int = 4) -> int:
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
                K_UP, 16 + (seq % max(workers, 1)), cell, seq, NOISE_COVERAGE, noise_digest(seq), now,
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


def _pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(p / 100 * (len(ordered) - 1)))]


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


async def calibrate_gpu_arrival(concurrency: int, gpu_rho: float, requests: int = 16) -> float:
    """Arrival rate that keeps the GPUs near gpu_rho with no control-plane noise.

    Sequential requests measure service time. The open-loop rate is then
    gpu_rho times the concurrency, divided by that service time.
    """
    formal = load_formal()
    await formal.check_endpoints()
    samples: list[float] = []
    async with aiohttp.ClientSession() as session:
        await reset_prefix_caches(session, formal.URLS)
        for request_id in range(requests):
            slot = request_id % USEFUL_POOL
            started = time.perf_counter()
            response = await formal.one_request(
                session, formal.URLS[slot % 4],
                formal.prompt_for(formal.TraceRequest(
                    request_id=request_id, phase=0, lineage_id=slot, step=0,
                    tenant=f"tenant-{slot % 8}", digest=f"U{slot:04d}",
                    coverage_tokens=USEFUL_COVERAGE, workload="reuse_intensive", discard=False,
                )),
                f"icc-gpu-cal-{slot}", 4, 2,
            )
            if response["ok"]:
                samples.append(time.perf_counter() - started)
    scored = samples[min(4, len(samples)):] or samples
    service = sum(scored) / len(scored)
    arrival = gpu_rho * concurrency / max(service, 1e-3)
    print({
        "gpu_calibration": True,
        "service_s": round(service, 3),
        "gpu_rho": gpu_rho,
        "concurrency": concurrency,
        "arrival_rate": round(arrival, 4),
        "samples": len(scored),
    }, flush=True)
    return arrival


async def run_e2e(
    trace: Path, capacity: float, scenarios: list[str], methods: list[str],
    seeds: int, requests_per_run: int, concurrency: int, kv_tokens: int, out_dir: Path,
    open_loop: bool = True, arrival_rate: float = 1.25, invalidate_every: int = 8,
    noise_workers: int = 4, noise_senders: int = 4, warmup_requests: int = 0,
    gpu_rho: float = 0.0,
) -> None:
    formal = load_formal()
    prepare_fixed_gateway()
    control = SplitControl(senders=noise_senders)
    pinned: list[tuple[int, set[int]]] = []
    vllm_roots: list[int] = []
    try:
        control.start()
        try:
            os.sched_setaffinity(0, {2})
        except OSError:
            pass
        vllm_allowed = cpus_for_vllm()
        pinned, vllm_roots = pin_listeners([8000, 8001, 8002, 8003], vllm_allowed)
        if gpu_rho > 0:
            arrival_rate = await calibrate_gpu_arrival(concurrency, gpu_rho)
        gateway = gateway_pid()
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
                    try:
                        rho = rhos[scenario]
                        rate = capacity if method == "RateFIFO" else 0.0
                        cell_id = cell
                        salt = f"icc-noise-{run_id}-{cell_id}"
                        started = time.perf_counter()
                        reapply_affinity(vllm_roots, vllm_allowed)
                        control.begin_cell()
                        control.configure(cell_id, method, rate)
                        control.begin_noise({
                            "cell": cell_id,
                            "level": rho * capacity,
                            "seconds": 0,
                            "scenario": scenario,
                            "coverage": NOISE_COVERAGE,
                            "seq0": 100_000_000,
                            "workers": noise_workers,
                        })
                        shadows = [formal.ShadowCache(kv_tokens) for _ in range(4)]
                        truth: dict[tuple[int, int], int] = {}
                        placement = Placement()
                        rng = random.Random(seed)
                        planned = []
                        for request_id in range(requests_per_run):
                            slot = bisect.bisect_left(cdf, rng.random())
                            planned.append((request_id, slot))
                        records = []
                        failed = 0
                        stale_cache_hits = 0
                        loop_lags: list[float] = []
                        view_sizes: list[int] = []
                        stop_probe = asyncio.Event()
                        versions = [0] * USEFUL_POOL
                        digest_text = [f"U{slot:04d}" for slot in range(USEFUL_POOL)]
                        digests = [formal.digest64(text) for text in digest_text]
                        seq_box = [1]
                        state_lock = asyncio.Lock()
                        inflight = asyncio.Semaphore(concurrency)
                        pending_emits: list[tuple] = []
                        fresh_tombs: set[tuple[int, int]] = set()
                        installed_at: dict[tuple[int, int], float] = {}
                        tomb_at: dict[tuple[int, int], float] = {}

                        def flush_emits() -> None:
                            batch = pending_emits[:]
                            pending_emits.clear()
                            for kind, worker, seq, coverage, digest in batch:
                                control.emit(kind, worker, cell_id, seq, coverage, digest)

                        def invalidate_slot(slot: int) -> None:
                            versions[slot] += 1
                            digest = digests[slot]
                            now = time.time()
                            for worker in range(4):
                                if truth.pop((worker, digest), None) is None:
                                    continue
                                installed_at.pop((worker, digest), None)
                                tomb_at[(worker, digest)] = now
                                shadows[worker].drop(digest_text[slot])
                                fresh_tombs.add((worker, digest))
                                if method == "Ideal":
                                    control.clear_local(worker, digest)
                                else:
                                    pending_emits.append((K_TOMB, worker, seq_box[0], 0, digest))
                                    seq_box[0] += 1

                        async def probe() -> None:
                            while not stop_probe.is_set():
                                mark = time.perf_counter()
                                try:
                                    await asyncio.wait_for(stop_probe.wait(), timeout=0.05)
                                except asyncio.TimeoutError:
                                    loop_lags.append((time.perf_counter() - mark - 0.05) * 1000)

                        async def serve(request_id: int, slot: int, due: float | None) -> None:
                            nonlocal failed, stale_cache_hits
                            if due is not None:
                                delay = due - time.perf_counter()
                                if delay > 0:
                                    await asyncio.sleep(delay)
                            wait_started = time.perf_counter()
                            async with inflight:
                                sched_delay_ms = (time.perf_counter() - wait_started) * 1000
                                async with state_lock:
                                    fresh_tombs.clear()
                                    if invalidate_every and request_id and request_id % invalidate_every == 0:
                                        installed = [
                                            candidate for candidate in range(USEFUL_POOL)
                                            if any(truth.get((worker, digests[candidate]), 0) >= USEFUL_COVERAGE for worker in range(4))
                                        ]
                                        if slot in installed:
                                            invalidate_slot(slot)
                                        elif installed:
                                            invalidate_slot(installed[0])
                                    # The tomb has to leave before the view is read. Otherwise
                                    # every non-Ideal method records a false positive on the
                                    # request that just invalidated its own prefix.
                                    flush_emits()
                                    if control.ideal:
                                        tested, generated_map, delivered_map = control.placement_state()
                                    else:
                                        tested, generated_map, delivered_map = await asyncio.to_thread(control.placement_state)
                                    view_sizes.append(len(tested))
                                    digest = digests[slot]
                                    version = versions[slot]
                                    ideal, ideal_cov = placement.select(truth, digest)
                                    routed, _view_best = placement.select(tested, digest)
                                    placement.rr += 1
                                    placement.loads[routed] += 1
                                    routed_truth = truth.get((routed, digest), 0)
                                    truth_cov = max((truth.get((worker, digest), 0) for worker in range(4)), default=0)
                                    view_cov_max = max((tested.get((worker, digest), 0) for worker in range(4)), default=0)
                                    view_cov = tested.get((routed, digest), 0)
                                    now_wall = time.time()
                                    generated_at = generated_map.get((routed, digest))
                                    delivered_at = delivered_map.get((routed, digest))
                                    if generated_at is None or delivered_at is None:
                                        delivery_lag_s, state_age_s = -1.0, -1.0
                                    else:
                                        delivery_lag_s = max(0.0, delivered_at - generated_at)
                                        state_age_s = max(0.0, now_wall - delivered_at)
                                    missing_ages = [
                                        now_wall - installed_at[(worker, digest)]
                                        for worker in range(4)
                                        if truth.get((worker, digest), 0) >= 512 and (worker, digest) in installed_at
                                    ]
                                    missing_age_s = max(missing_ages) if truth_cov >= 512 and view_cov_max < 512 and missing_ages else -1.0
                                    pending_ages = [
                                        now_wall - tomb_at[(worker, digest)]
                                        for worker in range(4)
                                        if (worker, digest) in tomb_at and tested.get((worker, digest), 0) >= 512
                                    ]
                                    tomb_pending_s = max(pending_ages) if pending_ages else -1.0
                                    decision = {
                                        "slot": slot,
                                        "digest": digest,
                                        "version": version,
                                        "ideal": ideal,
                                        "ideal_cov": ideal_cov,
                                        "routed": routed,
                                        "routed_truth": routed_truth,
                                        "loose_false_negative": int(truth_cov >= 512 and view_cov_max < 512),
                                        "loose_false_positive": int(
                                            view_cov >= 512 and view_cov > routed_truth and (routed, digest) not in fresh_tombs
                                        ),
                                        "coverage_shortfall": int(routed_truth < ideal_cov),
                                        "sched_delay_ms": sched_delay_ms,
                                        "delivery_lag_s": delivery_lag_s,
                                        "state_age_s": state_age_s,
                                        "missing_age_s": missing_age_s,
                                        "tomb_pending_s": tomb_pending_s,
                                        "decision_s": time.perf_counter() - origin,
                                    }
                                flush_emits()
                                response = await formal.one_request(
                                    session, formal.URLS[decision["routed"]],
                                    formal.prompt_for(formal.TraceRequest(
                                        request_id=request_id, phase=0, lineage_id=slot, step=0,
                                        tenant=f"tenant-{slot % 8}", digest=digest_text[slot],
                                        coverage_tokens=USEFUL_COVERAGE, workload="reuse_intensive", discard=False,
                                    )),
                                    f"{salt}-v{decision['version']}", 4, 2,
                                )
                                async with state_lock:
                                    routed = decision["routed"]
                                    placement.loads[routed] -= 1
                                    if not response["ok"]:
                                        failed += 1
                                        return
                                    cached = int(response["vllm_cached_tokens"] or 0)
                                    regret = max(0, decision["ideal_cov"] - decision["routed_truth"])
                                    if cached >= 512 and decision["routed_truth"] == 0 and decision["version"] > 0:
                                        stale_cache_hits += 1
                                    evicted = shadows[routed].insert(digest_text[slot], USEFUL_COVERAGE)
                                    truth[(routed, decision["digest"])] = USEFUL_COVERAGE
                                    installed_at[(routed, decision["digest"])] = time.time()
                                    tomb_at.pop((routed, decision["digest"]), None)
                                    if method == "Ideal":
                                        control.set_local(routed, decision["digest"], USEFUL_COVERAGE)
                                    else:
                                        pending_emits.append((K_UP, routed, seq_box[0], USEFUL_COVERAGE, decision["digest"]))
                                        seq_box[0] += 1
                                    for victim in evicted:
                                        victim_digest = formal.digest64(victim)
                                        truth.pop((routed, victim_digest), None)
                                        installed_at.pop((routed, victim_digest), None)
                                        shadows[routed].drop(victim)
                                        if method == "Ideal":
                                            control.clear_local(routed, victim_digest)
                                        else:
                                            pending_emits.append((K_TOMB, routed, seq_box[0], 0, victim_digest))
                                            seq_box[0] += 1
                                    prefill = max(0, int(response["input_tokens"] or 0) - cached)
                                    records.append({
                                        "request_id": request_id,
                                        "warmup": int(request_id < warmup_requests),
                                        "decision_s": decision["decision_s"],
                                        "ttft_ms": response["ttft_ms"],
                                        "sched_delay_ms": decision["sched_delay_ms"],
                                        "e2e_ttft_ms": response["ttft_ms"] + decision["sched_delay_ms"],
                                        "reused_tokens": cached,
                                        "prefill_tokens": prefill,
                                        "routed": routed,
                                        "ideal": decision["ideal"],
                                        "version": decision["version"],
                                        "wrong_placement": int(routed != decision["ideal"] and decision["ideal_cov"] > 0),
                                        "coverage_wrong": int(regret > 0),
                                        "coverage_regret": regret,
                                        "coverage_shortfall": decision["coverage_shortfall"],
                                        "loose_false_negative": decision["loose_false_negative"],
                                        "loose_false_positive": decision["loose_false_positive"],
                                        "stale_cache_hit": int(cached >= 512 and decision["routed_truth"] == 0 and decision["version"] > 0),
                                        "delivery_lag_s": decision["delivery_lag_s"],
                                        "state_age_s": decision["state_age_s"],
                                        "missing_age_s": decision["missing_age_s"],
                                        "tomb_pending_s": decision["tomb_pending_s"],
                                        "cache_hit": int(decision["coverage_shortfall"] == 0 and decision["loose_false_negative"] == 0 and prefill < 800),
                                    })
                                flush_emits()

                        probe_task = asyncio.create_task(probe())
                        try:
                            async with aiohttp.ClientSession() as session:
                                before = await metric_totals(session, formal.URLS)
                                await reset_prefix_caches(session, formal.URLS)
                                origin = time.perf_counter()
                                cpu_mark = process_cpu_seconds(gateway)
                                cpu_t0 = time.perf_counter()
                                if open_loop:
                                    await asyncio.gather(*[
                                        serve(request_id, slot, origin + request_id / max(arrival_rate, 1e-6))
                                        for request_id, slot in planned
                                    ])
                                else:
                                    for offset in range(0, len(planned), concurrency):
                                        await asyncio.gather(*[
                                            serve(request_id, slot, None)
                                            for request_id, slot in planned[offset:offset + concurrency]
                                        ])
                                after = await metric_totals(session, formal.URLS)
                                records.sort(key=lambda row: row["request_id"])
                                gateway_cpu = (process_cpu_seconds(gateway) - cpu_mark) / max(time.perf_counter() - cpu_t0, 1e-6)
                        finally:
                            stop_probe.set()
                            probe_task.cancel()
                            with contextlib.suppress(asyncio.CancelledError):
                                await probe_task
                            noise_sent = control.end_noise()
                        stats = {}
                        previous_received = -1
                        for _settle in range(12):
                            stats = control.stats()
                            got = int(stats.get("relay_received") or 0)
                            if got == previous_received:
                                break
                            previous_received = got
                            await asyncio.sleep(0.4)
                        counts = [0, 0, 0, 0]
                        scored = [row for row in records if not row["warmup"]]
                        for row in scored:
                            counts[row["routed"]] += 1
                        sum_delta = sum(after[index][0] - before[index][0] for index in range(4))
                        count_delta = sum(after[index][1] - before[index][1] for index in range(4))
                        counted = control.counts()
                        applied = counted["applied"]
                        frames = counted["frames"]
                        foreground_frames = counted["foreground"]
                        update_lags = [max(0.0, row.applied_at - row.generated_at) for row in applied if row.coverage >= USEFUL_COVERAGE]
                        invalidate_lags = [max(0.0, row.applied_at - row.generated_at) for row in applied if row.kind == "invalidate"]
                        applied_seqs = {row.seq for row in applied}
                        censored_up: list[float] = []
                        censored_inv: list[float] = []
                        closed = time.time()
                        for seq, kind, coverage, generated in control.sent_fg:
                            if seq in applied_seqs:
                                continue
                            lag = max(0.0, closed - generated)
                            if kind == "invalidate":
                                censored_inv.append(lag)
                            elif coverage >= USEFUL_COVERAGE:
                                censored_up.append(lag)
                        ttfts = [row["ttft_ms"] for row in scored]
                        e2e_ttfts = [row["e2e_ttft_ms"] for row in scored]
                        ordered = sorted(ttfts)
                        lags = sorted(loop_lags)

                        def pct(values: list[float], p: float) -> float:
                            if not values:
                                return 0.0
                            return values[min(len(values) - 1, round(p / 100 * (len(values) - 1)))]

                        def mean(values: list[float]) -> float:
                            return sum(values) / len(values) if values else 0.0

                        elapsed = max(time.perf_counter() - started, 1e-6)
                        drop_keys = (
                            "relay_drop_rate_limit", "relay_drop_superseded", "relay_drop_replica_cap",
                            "relay_drop_low_utility", "relay_drop_queue_drop", "relay_drop_expired",
                        )
                        forwarded = int(stats.get("relay_forwarded") or 0)
                        received = int(stats.get("relay_received") or 0)
                        queued = int(stats.get("relay_queued") or 0)
                        drops = sum(int(stats.get(key) or 0) for key in drop_keys)
                        sent_total = noise_sent + control.fg_up + control.fg_tomb
                        accounted = forwarded + drops + queued

                        def gap(left: int, right: int) -> float:
                            den = max(left, right)
                            return abs(left - right) / den if den else 0.0

                        summary = {
                            "seed": seed,
                            "scenario": scenario,
                            "method": method,
                            "method_order": order,
                            "cell": cell,
                            "cache_salt": salt,
                            "requests": len(records),
                            "scored_requests": len(scored),
                            "warmup_requests": warmup_requests,
                            "failed_requests": failed,
                            "elapsed_s": round(elapsed, 1),
                            "ttft_mean_ms": mean(ttfts),
                            "ttft_p95_ms": pct(ordered, 95),
                            "e2e_ttft_mean_ms": mean(e2e_ttfts),
                            "sched_delay_mean_ms": mean([row["sched_delay_ms"] for row in scored]),
                            "server_ttft_mean_ms": (sum_delta / count_delta * 1000) if count_delta > 0 else 0.0,
                            "foreground_lag_p50_s": _pct(update_lags, 50),
                            "foreground_lag_p95_s": _pct(update_lags, 95),
                            "foreground_censored_lag_p95_s": _pct(update_lags + censored_up, 95),
                            "foreground_undelivered": len(censored_up),
                            "foreground_applied": len(update_lags),
                            "foreground_up_sent": control.fg_up,
                            "invalidate_lag_p50_s": _pct(invalidate_lags, 50),
                            "invalidate_lag_p95_s": _pct(invalidate_lags, 95),
                            "invalidate_censored_lag_p95_s": _pct(invalidate_lags + censored_inv, 95),
                            "invalidate_undelivered": len(censored_inv),
                            "invalidate_applied": len(invalidate_lags),
                            "tombs_sent": control.fg_tomb,
                            "loop_lag_p95_ms": pct(lags, 95),
                            "slo_violation_2s": sum(value > 2000 for value in ttfts) / len(ttfts) if ttfts else 0.0,
                            "prefill_tokens_mean": mean([row["prefill_tokens"] for row in scored]),
                            "reused_tokens_mean": mean([row["reused_tokens"] for row in scored]),
                            "wrong_placement_rate": mean([row["wrong_placement"] for row in scored]),
                            "coverage_wrong_rate": mean([row["coverage_wrong"] for row in scored]),
                            "stale_cache_hits": stale_cache_hits,
                            "hit_service_ttft_ms": mean([row["ttft_ms"] for row in scored if row["cache_hit"]]),
                            "hit_requests": sum(row["cache_hit"] for row in scored),
                            "fn_delivery_lag_p95_s": _pct([row["delivery_lag_s"] for row in scored if row["loose_false_negative"] and row["delivery_lag_s"] >= 0], 95),
                            "hit_delivery_lag_p95_s": _pct([row["delivery_lag_s"] for row in scored if row["cache_hit"] and row["delivery_lag_s"] >= 0], 95),
                            "missing_age_p95_s": _pct([row["missing_age_s"] for row in scored if row["missing_age_s"] >= 0], 95),
                            "tomb_pending_p95_s": _pct([row["tomb_pending_s"] for row in scored if row["tomb_pending_s"] >= 0], 95),
                            "coverage_shortfall_rate": mean([row["coverage_shortfall"] for row in scored]),
                            "coverage_regret_mean": mean([row["coverage_regret"] for row in scored]),
                            "loose_false_negative_rate": mean([row["loose_false_negative"] for row in scored]),
                            "loose_false_positive_rate": mean([row["loose_false_positive"] for row in scored]),
                            "placement_counts": counts,
                            "dispatcher_frames": frames,
                            "foreground_frames": foreground_frames,
                            "noise_frames": counted["noise"],
                            "routable_view_max": max(view_sizes) if view_sizes else 0,
                            "noise_sent": noise_sent,
                            "ledger_sent": sent_total,
                            "ledger_received": received,
                            "ledger_accounted": accounted,
                            "ledger_ingress_gap": gap(sent_total, received),
                            "ledger_balance_gap": gap(received, accounted),
                            "offered_noise_per_s": noise_sent / elapsed,
                            "measured_rho": (noise_sent / elapsed / capacity) if capacity else 0.0,
                            "gateway_cpu": gateway_cpu,
                            "vllm_pinned": len(pinned),
                            "capacity_events_per_s": capacity,
                            "background_rho": rho,
                            "concurrency": concurrency,
                            "open_loop": open_loop,
                            "arrival_rate": arrival_rate if open_loop else 0.0,
                            "invalidate_every": invalidate_every,
                            "useful_pool": USEFUL_POOL,
                            "useful_coverage": USEFUL_COVERAGE,
                            "noise_coverage": NOISE_COVERAGE,
                            "noise_workers": noise_workers,
                            "noise_senders": noise_senders,
                            "forwarded_per_s": forwarded / elapsed,
                            **stats,
                        }
                        rows.append(summary)
                        write_json(path, {**control.meta(), "rows": rows})
                        if records:
                            csv_path = out_dir / f"requests_{scenario}_{method}_seed{seed}.csv"
                            with csv_path.open("w", newline="") as handle:
                                writer = csv.DictWriter(handle, fieldnames=list(records[0]), lineterminator="\n")
                                writer.writeheader()
                                writer.writerows(records)
                        print(summary, flush=True)
                        cell += 1
                    except Exception as exc:
                        error_path = out_dir / "cell_errors.log"
                        with error_path.open("a") as handle:
                            handle.write(f"{seed} {scenario} {method} {exc!r}\n")
                        print({"cell_error": repr(exc), "seed": seed, "scenario": scenario, "method": method}, flush=True)
                        try:
                            control.end_noise()
                        except Exception:
                            pass
                        continue
    finally:
        control.stop()
        restore_affinity(pinned)
        if vllm_roots:
            reapply_affinity(vllm_roots, set(range(os.cpu_count() or 1)))


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
    parser.add_argument("--closed-loop", action="store_true")
    parser.add_argument("--arrival-rate", type=float, default=1.25)
    parser.add_argument("--gpu-rho", type=float, default=0.0,
                        help="if positive, measure GPU service time and set the arrival rate to this load")
    parser.add_argument("--invalidate-every", type=int, default=8)
    parser.add_argument("--noise-workers", type=int, default=4)
    parser.add_argument("--noise-senders", type=int, default=4)
    parser.add_argument("--warmup-requests", type=int, default=0)
    parser.add_argument("--out-dir", type=Path, default=OUT / "e2e")
    args = parser.parse_args()
    asyncio.run(run_e2e(
        args.trace, args.capacity,
        [item for item in args.scenarios.split(",") if item],
        [item for item in args.methods.split(",") if item],
        args.seeds, args.requests, args.concurrency, args.kv_cache_tokens, args.out_dir,
        open_loop=not args.closed_loop,
        arrival_rate=args.arrival_rate,
        invalidate_every=args.invalidate_every,
        noise_workers=args.noise_workers,
        noise_senders=args.noise_senders,
        warmup_requests=args.warmup_requests,
        gpu_rho=args.gpu_rho,
    ))


if __name__ == "__main__":
    main()
