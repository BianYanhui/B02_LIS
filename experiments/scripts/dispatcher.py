"""B02 Dispatcher: workflow state + state collector + dispatch policies.

Per experiments/design.md §1.1, §1.2, §1.4.

Usage:
    python dispatcher.py --config <yaml> --cell-id <id> --out-dir <dir>

The dispatcher:
  - polls each vLLM instance's /metrics at the configured frequency
  - builds Coarse / Rich / Sketch state views
  - exposes an HTTP endpoint to accept dispatch requests
  - logs everything to JSONL in out_dir
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import time
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import orjson
import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("dispatcher")


# ---------------------------------------------------------------------------
# State view schemas (frozen, design.md §1.4)
# ---------------------------------------------------------------------------

@dataclass
class WorkflowRecord:
    workflow_id: str
    step_id: int = 0
    total_steps: int = 0
    progress: float = 0.0  # 0..1
    last_assigned_instance: str = ""
    assigned_instance_history: list[str] = field(default_factory=list)
    tool_status: str = "idle"  # idle|running|done|failed
    last_tool_name: str = ""
    last_tool_latency_ms: float = 0.0
    tool_result_context_size: int = 0
    tool_result_context_type: str = "none"
    workflow_start_time_ns: int = 0
    last_step_finish_time_ns: int = 0

    def to_rich_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # shorten keys not used in §6.2 schema
        return d

    def to_sketch_bits(self) -> dict[str, Any]:
        progress_q = int(self.progress * 100) & 0xFF
        tool_state_bits = {"idle": 0, "running": 1, "done": 2, "failed": 3}[self.tool_status]
        return {
            "progress_q": progress_q,
            "tool_state_2b": tool_state_bits,
            "has_context": 1 if self.tool_result_context_size > 0 else 0,
        }


def build_coarse_view(instance_id: str, ts_ns: int, metrics: dict) -> dict:
    """Coarse = compact aggregate runtime metrics from vLLM."""
    return {
        "instance_id": instance_id,
        "timestamp_ns": ts_ns,
        "num_requests_waiting": int(metrics.get("num_requests_waiting", 0)),
        "num_requests_running": int(metrics.get("num_requests_running", 0)),
        "kv_cache_usage_perc": float(metrics.get("kv_cache_usage_perc", 0.0)),
        "gpu_cache_usage_perc": float(metrics.get("gpu_cache_usage_perc", 0.0)),
        "prompt_tokens_total": int(metrics.get("prompt_tokens_total", 0)),
        "generation_tokens_total": int(metrics.get("generation_tokens_total", 0)),
        "prefix_cache_hits_total": int(metrics.get("prefix_cache_hits_total", 0)),
        "prefix_cache_queries_total": int(metrics.get("prefix_cache_queries_total", 0)),
        "request_success_total": int(metrics.get("request_success_total", 0)),
        "num_preemptions_total": int(metrics.get("num_preemptions_total", 0)),
    }


def build_rich_view(instance_id: str, ts_ns: int, metrics: dict,
                    workflows: list[WorkflowRecord],
                    latency_summary: dict) -> dict:
    """Rich = Coarse + raw workflow records + latency summary."""
    return {
        "instance_id": instance_id,
        "timestamp_ns": ts_ns,
        "runtime": {
            **build_coarse_view(instance_id, ts_ns, metrics),
            "latency_summary": latency_summary,
        },
        "workflows": [wf.to_rich_dict() for wf in workflows],
    }


def build_sketch_view(instance_id: str, ts_ns: int, metrics: dict,
                      workflows: list[WorkflowRecord],
                      n_instances: int) -> dict:
    """Sketch = Coarse-style quantized + bit-packed workflow sketch.

    See design.md §1.4 for computation rules.
    """
    K = len(workflows)
    if K > 0:
        avg_progress_q = int(sum(wf.progress for wf in workflows) / K * 100) & 0xFF
        max_progress_q = int(max(wf.progress for wf in workflows) * 100) & 0xFF
        # pack tool_status as 2 bits per workflow (K<=16)
        ts_bits = 0
        ctx_bits = 0
        for i, wf in enumerate(workflows[:16]):
            b = wf.to_sketch_bits()
            ts_bits |= (b["tool_state_2b"] << (i * 2)) & 0xFFFF
            ctx_bits |= (b["has_context"] << i) & 0xFFFF
        # affinity hot counts (per instance)
        affinity = [0] * n_instances
        for wf in workflows:
            if wf.assigned_instance_history:
                last = wf.assigned_instance_history[-1]
                idx = int(last.split("_")[-1])
                if 0 <= idx < n_instances:
                    affinity[idx] += 1
    else:
        avg_progress_q = 0
        max_progress_q = 0
        ts_bits = 0
        ctx_bits = 0
        affinity = [0] * n_instances

    # quantized runtime
    kv_q = int(metrics.get("kv_cache_usage_perc", 0.0) * 100) & 0xFF
    p_hits = int(metrics.get("prefix_cache_hits_total", 0))
    p_q = int(metrics.get("prefix_cache_queries_total", 1))
    hit_rate = (p_hits / p_q * 100) if p_q > 0 else 0
    hit_q = int(hit_rate) & 0xFF
    load = int(metrics.get("num_requests_waiting", 0)) + int(metrics.get("num_requests_running", 0))
    load_q = min(load, 255)

    return {
        "instance_id": instance_id,
        "timestamp_ns": ts_ns,
        "runtime": {
            "kv_cache_usage_q": kv_q,
            "prefix_cache_hit_rate_q": hit_q,
            "load_q": load_q,
        },
        "workflow_sketch": {
            "active_workflow_count": K,
            "avg_progress_q": avg_progress_q,
            "max_progress_q": max_progress_q,
            "tool_status_bitset": ts_bits & 0xFFFF,
            "tool_context_avail_bitmap": ctx_bits & 0xFFFF,
            "affinity_hot_instance_counts": affinity,
            "recent_workflow_hashes": [0, 0, 0, 0],
        },
    }


# ---------------------------------------------------------------------------
# Dispatch policies (frozen, design.md §1.2)
# ---------------------------------------------------------------------------

def policy_round_robin(state_view, request, ctx) -> str:
    """Pick the instance with the oldest last-assigned timestamp."""
    instances = ctx["instances"]
    return min(instances, key=lambda i: ctx["last_assigned_at"].get(i, 0))


def policy_coarse(state_view, request, ctx) -> str:
    """α * (waiting + running) + β * kv_usage."""
    instances = ctx["instances"]
    alpha, beta = ctx["alpha"], ctx["beta"]
    def score(i: str) -> float:
        v = state_view.get(i, {})
        rt = v.get("runtime", v)  # Coarse has no 'runtime' key, rich does
        if "num_requests_waiting" in rt:
            w = rt["num_requests_waiting"]
            r = rt["num_requests_running"]
            kv = rt.get("kv_cache_usage_perc", rt.get("kv_cache_usage_q", 0) / 100.0)
        else:
            w = rt.get("load_q", 0)
            r = 0
            kv = rt.get("kv_cache_usage_q", 0) / 100.0
        return alpha * (w + r) + beta * kv
    return min(instances, key=score)


def policy_rich(state_view, request, ctx) -> str:
    """Coarse score + γ * affinity_score (negative for locality)."""
    base_instance = policy_coarse(state_view, request, ctx)
    # If request has a workflow_id, prefer its recent instance
    wf_id = request.get("workflow_id")
    if wf_id and wf_id in ctx["workflow_table"]:
        wf = ctx["workflow_table"][wf_id]
        if wf.assigned_instance_history:
            recent = wf.assigned_instance_history[-1]
            if recent in ctx["instances"]:
                # Apply locality reward only if coarse score is not too bad
                coarse_inst = base_instance
                coarse_score = next(
                    (s for inst, s in [
                        (i, _coarse_score(state_view.get(i, {}), i, ctx))
                        for i in ctx["instances"]
                    ] if inst == coarse_inst),
                    0.0,
                )
                recent_score = _coarse_score(state_view.get(recent, {}), recent, ctx)
                if recent_score < coarse_score * 1.5:
                    return recent
    return base_instance


def _coarse_score(v: dict, instance: str, ctx: dict) -> float:
    alpha, beta = ctx["alpha"], ctx["beta"]
    rt = v.get("runtime", v)
    if "num_requests_waiting" in rt:
        w = rt["num_requests_waiting"]
        r = rt["num_requests_running"]
        kv = rt.get("kv_cache_usage_perc", 0.0)
    else:
        w = rt.get("load_q", 0)
        r = 0
        kv = rt.get("kv_cache_usage_q", 0) / 100.0
    return alpha * (w + r) + beta * kv


def policy_sketch(state_view, request, ctx) -> str:
    """Like Rich but using quantized affinity from sketch."""
    base_instance = policy_coarse(state_view, request, ctx)
    wf_id = request.get("workflow_id")
    if not wf_id or wf_id not in ctx["workflow_table"]:
        return base_instance
    wf = ctx["workflow_table"][wf_id]
    # Use the sketch-style quantized affinity: from affinity_hot_instance_counts
    # of the candidate instances
    best = base_instance
    best_aff = -1
    for i in ctx["instances"]:
        v = state_view.get(i, {})
        ws = v.get("workflow_sketch", {})
        aff = ws.get("affinity_hot_instance_counts", [])
        if aff:
            try:
                idx = int(i.split("_")[-1])
                if 0 <= idx < len(aff):
                    if aff[idx] > best_aff:
                        best_aff = aff[idx]
                        best = i
            except (ValueError, IndexError):
                continue
    # Only override base if there's a clear hot affinity
    coarse_score_base = _coarse_score(state_view.get(base_instance, {}), base_instance, ctx)
    coarse_score_best = _coarse_score(state_view.get(best, {}), best, ctx)
    if best != base_instance and coarse_score_best < coarse_score_base * 1.5 and best_aff > 0:
        return best
    return base_instance


POLICIES = {
    "round-robin": policy_round_robin,
    "coarse": policy_coarse,
    "rich": policy_rich,
    "sketch": policy_sketch,
}


# ---------------------------------------------------------------------------
# State collector (poll /metrics)
# ---------------------------------------------------------------------------

VLLM_METRIC_KEYS = [
    "num_requests_waiting",
    "num_requests_running",
    "kv_cache_usage_perc",
    "gpu_cache_usage_perc",
    "prompt_tokens_total",
    "generation_tokens_total",
    "prefix_cache_hits_total",
    "prefix_cache_queries_total",
    "request_success_total",
    "num_preemptions_total",
]


def parse_vllm_metrics(text: str) -> dict[str, float]:
    """Minimal Prometheus parser — extract vllm:* values without labels."""
    out: dict[str, float] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # match `vllm:NAME{labels...} VALUE [timestamp]` or `vllm:NAME VALUE`
        if "vllm:" not in line:
            continue
        try:
            head, val = line.rsplit(" ", 1)
            val = float(val)
        except ValueError:
            continue
        # extract metric name
        name = head.split("{", 1)[0].strip()
        short = name.replace("vllm:", "")
        # skip histograms with _bucket/_sum/_count suffixes (we use totals instead)
        if short.endswith("_bucket") or short.endswith("_sum") or short.endswith("_count"):
            continue
        if short.endswith("_total"):
            short = short[:-6]  # we store without _total in our keys
        if short in VLLM_METRIC_KEYS:
            # sum across labels if multiple
            out[short] = out.get(short, 0.0) + val
    return out


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

@dataclass
class DispatcherConfig:
    instances: list[str]                 # ["instance_0", ...]
    instance_urls: dict[str, str]        # "instance_0" -> "http://127.0.0.1:8000"
    state_view: str                       # "none" | "coarse" | "rich" | "sketch"
    update_freq_hz: float                 # 1, 10, 50
    duration_s: float
    out_dir: str
    cell_id: str
    workload: str                         # "chatbot" | "agentic"
    rep: int
    policy: str = "round-robin"           # "round-robin" | "coarse" | "rich" | "sketch"


class Dispatcher:
    def __init__(self, cfg: DispatcherConfig):
        self.cfg = cfg
        self.n_instances = len(cfg.instances)
        # state
        self.state_views: dict[str, dict] = {}  # instance_id -> latest state view
        self.workflow_table: dict[str, WorkflowRecord] = {}
        self.last_assigned_at: dict[str, float] = {i: 0.0 for i in cfg.instances}
        self.latency_summary: dict[str, dict] = {i: {} for i in cfg.instances}
        # jsonl writers
        os.makedirs(cfg.out_dir, exist_ok=True)
        self.f_state = open(os.path.join(cfg.out_dir, "state_updates.jsonl"), "a")
        self.f_dispatch = open(os.path.join(cfg.out_dir, "dispatch_log.jsonl"), "a")
        # metrics
        self.update_count = 0
        # ctx for policies
        self.ctx = {
            "instances": cfg.instances,
            "last_assigned_at": self.last_assigned_at,
            "workflow_table": self.workflow_table,
            "alpha": 1.0,
            "beta": 0.1,
            "gamma": 10.0,
        }
        # request log for latency summary recompute
        self.request_log: list[dict] = []
        self._stopped = False

    # ---- public API ----
    def register_workflow(self, wf: WorkflowRecord):
        self.workflow_table[wf.workflow_id] = wf

    def update_workflow(self, wf_id: str, **kwargs):
        wf = self.workflow_table[wf_id]
        for k, v in kwargs.items():
            setattr(wf, k, v)

    def get_workflows_for_instance(self, instance_id: str) -> list[WorkflowRecord]:
        return [wf for wf in self.workflow_table.values()
                if wf.last_assigned_instance == instance_id]

    def pick_instance(self, request: dict) -> tuple[str, float]:
        """Run the configured policy. Returns (instance, decision_time_s)."""
        t0 = time.perf_counter_ns()
        policy_fn = POLICIES[self.cfg.policy]
        # The state_view param differs by view:
        # - none: empty dict
        # - coarse: build a coarse-only view per instance
        # - rich: full rich view per instance
        # - sketch: sketch view per instance
        sv = self._build_state_view_for_policy()
        chosen = policy_fn(sv, request, self.ctx)
        dt_ns = time.perf_counter_ns() - t0
        self.last_assigned_at[chosen] = time.time()
        return chosen, dt_ns / 1e9

    def _build_state_view_for_policy(self) -> dict[str, dict]:
        """Combine per-instance state views into the dict the policy sees."""
        out = {}
        for i in self.cfg.instances:
            sv = self.state_views.get(i)
            if sv is None:
                sv = build_coarse_view(i, time.time_ns(), {})  # empty placeholder
            if self.cfg.state_view == "none":
                out[i] = {}
            elif self.cfg.state_view == "coarse":
                # coarse view only has flat keys
                out[i] = {k: v for k, v in sv.items() if k != "instance_id" and k != "timestamp_ns"}
            elif self.cfg.state_view == "rich":
                out[i] = sv
            elif self.cfg.state_view == "sketch":
                out[i] = sv
        return out

    # ---- state collection loop ----
    def collect_once(self) -> dict:
        """One state collection cycle. Returns the metrics record."""
        ts_start = time.perf_counter_ns()
        per_instance_metrics: dict[str, dict] = {}
        per_instance_views: dict[str, dict] = {}
        sizes = {}
        ser_times = {}
        deser_times = {}
        merge_times = {}
        collect_times = {}

        for inst in self.cfg.instances:
            url = self.cfg.instance_urls[inst]
            t_c0 = time.perf_counter_ns()
            try:
                r = requests.get(f"{url}/metrics", timeout=5)
                text = r.text
            except Exception as e:
                log.warning("metrics scrape failed for %s: %s", inst, e)
                text = ""
            t_c1 = time.perf_counter_ns()
            metrics = parse_vllm_metrics(text)
            workflows = self.get_workflows_for_instance(inst)
            self._recompute_latency_summary(inst)

            # build view
            t_s0 = time.perf_counter_ns()
            if self.cfg.state_view == "none":
                view = {"instance_id": inst, "timestamp_ns": time.time_ns()}
            elif self.cfg.state_view == "coarse":
                view = build_coarse_view(inst, time.time_ns(), metrics)
            elif self.cfg.state_view == "rich":
                view = build_rich_view(inst, time.time_ns(), metrics,
                                       workflows, self.latency_summary[inst])
            elif self.cfg.state_view == "sketch":
                view = build_sketch_view(inst, time.time_ns(), metrics,
                                         workflows, self.n_instances)
            else:
                raise ValueError(f"unknown state_view: {self.cfg.state_view}")
            t_s1 = time.perf_counter_ns()

            # serialize
            t_se0 = time.perf_counter_ns()
            blob = orjson.dumps(view)
            t_se1 = time.perf_counter_ns()
            sizes[inst] = len(blob)

            # deserialize (to measure cost)
            t_de0 = time.perf_counter_ns()
            orjson.loads(blob)
            t_de1 = time.perf_counter_ns()

            # merge into dispatcher state view table
            t_m0 = time.perf_counter_ns()
            self.state_views[inst] = view
            t_m1 = time.perf_counter_ns()

            per_instance_metrics[inst] = metrics
            per_instance_views[inst] = view
            collect_times[inst] = (t_c1 - t_c0) / 1e3  # us
            ser_times[inst] = (t_se1 - t_se0) / 1e3
            deser_times[inst] = (t_de1 - t_de0) / 1e3
            merge_times[inst] = (t_m1 - t_m0) / 1e3

        ts_end = time.perf_counter_ns()
        self.update_count += 1

        rec = {
            "update_id": self.update_count,
            "ts_start_ns": ts_start,
            "ts_end_ns": ts_end,
            "state_view": self.cfg.state_view,
            "freq_hz": self.cfg.update_freq_hz,
            "per_instance": {
                inst: {
                    "metrics": per_instance_metrics[inst],
                    "size_bytes": sizes[inst],
                    "collect_us": collect_times[inst],
                    "ser_us": ser_times[inst],
                    "deser_us": deser_times[inst],
                    "merge_us": merge_times[inst],
                    "n_workflows": len(self.get_workflows_for_instance(inst)),
                } for inst in self.cfg.instances
            },
        }
        self.f_state.write(orjson.dumps(rec).decode() + "\n")
        self.f_state.flush()
        return rec

    def _recompute_latency_summary(self, inst: str):
        """Compute p50/p95 of TTFT/TPOT/etc from request_log entries for this instance.

        design.md §1.3: source is request_log, not Prometheus.
        """
        from statistics import median
        ttfts, tpots, queue_ts, prefills, decodes = [], [], [], [], []
        for r in self.request_log:
            if r.get("instance_id") != inst:
                continue
            if r.get("first_token_time_ns") and r.get("vllm_request_start_time_ns"):
                ttfts.append((r["first_token_time_ns"] - r["vllm_request_start_time_ns"]) / 1e6)
            if r.get("finish_time_ns") and r.get("first_token_time_ns") and r.get("output_tokens", 0) > 0:
                tpots.append((r["finish_time_ns"] - r["first_token_time_ns"]) / r["output_tokens"] / 1e6)
            if r.get("vllm_request_start_time_ns") and r.get("arrival_time_ns"):
                queue_ts.append((r["vllm_request_start_time_ns"] - r["arrival_time_ns"]) / 1e6)
            if r.get("first_token_time_ns") and r.get("vllm_request_start_time_ns"):
                prefills.append((r["first_token_time_ns"] - r["vllm_request_start_time_ns"]) / 1e6)
            if r.get("finish_time_ns") and r.get("first_token_time_ns"):
                decodes.append((r["finish_time_ns"] - r["first_token_time_ns"]) / 1e6)

        def q(xs, p):
            if not xs:
                return 0.0
            xs = sorted(xs)
            k = max(0, min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1)))))
            return xs[k]

        self.latency_summary[inst] = {
            "ttft_p50": q(ttfts, 50),
            "ttft_p95": q(ttfts, 95),
            "tpot_p50": q(tpots, 50),
            "tpot_p95": q(tpots, 95),
            "queue_time_p95": q(queue_ts, 95),
            "prefill_time_p95": q(prefills, 95),
            "decode_time_p95": q(decodes, 95),
        }

    # ---- forward a request ----
    def forward(self, request: dict) -> dict:
        chosen, decision_time_s = self.pick_instance(request)
        url = self.cfg.instance_urls[chosen]
        # The actual HTTP call is made by the workload, not here.
        # This just records the dispatch.
        rec = {
            "ts_ns": time.time_ns(),
            "instance_id": chosen,
            "policy": self.cfg.policy,
            "state_view": self.cfg.state_view,
            "decision_time_us": decision_time_s * 1e6,
        }
        self.f_dispatch.write(orjson.dumps(rec).decode() + "\n")
        self.f_dispatch.flush()
        return rec

    def close(self):
        self.f_state.close()
        self.f_dispatch.close()


# ---------------------------------------------------------------------------
# CLI for direct run
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--instances", nargs="+", default=["instance_0", "instance_1", "instance_2", "instance_3"])
    ap.add_argument("--urls", nargs="+", default=["http://127.0.0.1:8000", "http://127.0.0.1:8001", "http://127.0.0.1:8002", "http://127.0.0.1:8003"])
    ap.add_argument("--state-view", choices=["none", "coarse", "rich", "sketch"], default="coarse")
    ap.add_argument("--freq-hz", type=float, default=10)
    ap.add_argument("--duration-s", type=float, default=60)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--cell-id", required=True)
    ap.add_argument("--workload", choices=["chatbot", "agentic"], default="chatbot")
    ap.add_argument("--rep", type=int, default=1)
    ap.add_argument("--policy", choices=list(POLICIES.keys()), default="coarse")
    args = ap.parse_args()

    cfg = DispatcherConfig(
        instances=args.instances,
        instance_urls=dict(zip(args.instances, args.urls)),
        state_view=args.state_view,
        update_freq_hz=args.freq_hz,
        duration_s=args.duration_s,
        out_dir=args.out_dir,
        cell_id=args.cell_id,
        workload=args.workload,
        rep=args.rep,
        policy=args.policy,
    )
    d = Dispatcher(cfg)
    period = 1.0 / cfg.update_freq_hz
    t_end = time.time() + cfg.duration_s
    log.info("cell=%s view=%s freq=%g Hz duration=%gs policy=%s",
             cfg.cell_id, cfg.state_view, cfg.update_freq_hz, cfg.duration_s, cfg.policy)
    next_t = time.time()
    while time.time() < t_end:
        d.collect_once()
        next_t += period
        sleep_for = next_t - time.time()
        if sleep_for > 0:
            time.sleep(sleep_for)
        else:
            # fell behind — reset cadence
            next_t = time.time()
    d.close()
    log.info("cell=%s done, %d updates collected", cfg.cell_id, d.update_count)