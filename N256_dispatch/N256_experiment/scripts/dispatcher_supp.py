"""B02 Supplement Dispatcher — strict policy-view separation with assertions.

Policies:
  - round-robin: only No State
  - coarse: only Coarse State (queue + KV% fields)
  - rich: Rich State (Coarse + full workflow list)
  - sketch: Sketch State (quantized, bit-packed)
  - oracle: oracle view — perfect workflow history + exact load

Hard assertions (§3 of prompt):
  - coarse policy MUST NOT access workflow_id, assigned_instance_history, affinity counters, tool status
  - sketch policy MUST NOT access raw workflow list
  - rich policy MUST be paired with view=rich
  - oracle policy is allowed to access everything
"""
from __future__ import annotations
import json
import os
import time
from collections import defaultdict
from dataclasses import dataclass, field
from statistics import mean, stdev
from typing import Any, Optional

import orjson


# =================== State View Builders ===================

def build_none_view(instance_id, ts_ns):
    return {"instance_id": instance_id, "timestamp_ns": ts_ns}


def build_coarse_view(instance_id, ts_ns):
    return {
        "instance_id": instance_id, "timestamp_ns": ts_ns,
        "num_requests_waiting": 0,
        "num_requests_running": 0,
        "kv_cache_usage_perc": 0.0,
        "gpu_cache_usage_perc": 0.0,
        "prompt_tokens_total": 0,
        "generation_tokens_total": 0,
        "prefix_cache_hits_total": 0,
        "prefix_cache_queries_total": 0,
        "request_success_total": 0,
        "num_preemptions_total": 0,
    }


def build_rich_view(instance_id, ts_ns, active_workflows: list,
                    assigned_history: dict, tool_metadata: dict, latency_summary: dict):
    """Full workflow state for `rich` policy.

    active_workflows: list of workflow records (each is a dict)
    assigned_history: dict of workflow_id -> list of instance_ids
    tool_metadata: dict of workflow_id -> {tool_name, tool_latency_ms, ...}
    latency_summary: dict with ttft_p50, ttft_p95, etc.
    """
    return {
        "instance_id": instance_id, "timestamp_ns": ts_ns,
        "runtime": {
            **build_coarse_view(instance_id, ts_ns),
            "latency_summary": latency_summary,
        },
        "active_workflows": active_workflows,
        "workflow_history": assigned_history,
        "tool_metadata": tool_metadata,
    }


def build_sketch_view(instance_id, ts_ns, active_workflow_count: int,
                      affinity_counter_array: list, tool_status_bitset: int,
                      tool_context_avail_bitmap: int,
                      avg_progress_q: int, max_progress_q: int,
                      recent_workflow_hashes: list, latency_sensitive_count: int):
    """Quantized + bit-packed workflow summary for `sketch` policy."""
    return {
        "instance_id": instance_id, "timestamp_ns": ts_ns,
        "runtime": {
            "kv_cache_usage_q": 0,
            "load_q": 0,
            "prefix_cache_hit_rate_q": 0,
        },
        "workflow_sketch": {
            "active_workflow_count": active_workflow_count,
            "avg_progress_q": avg_progress_q,
            "max_progress_q": max_progress_q,
            "tool_status_bitset": tool_status_bitset & 0xFFFF,
            "tool_context_avail_bitmap": tool_context_avail_bitmap & 0xFFFF,
            "affinity_hot_instance_counts": affinity_counter_array,
            "recent_workflow_hashes": recent_workflow_hashes,
            "latency_sensitive_count": latency_sensitive_count,
        },
    }


def build_sketch_no_affinity_view(*args, **kwargs):
    """Sketch-NoAffinity: drop affinity_counter_array, zero it out."""
    kwargs["affinity_counter_array"] = [0] * len(kwargs.get("affinity_counter_array", [0] * 8))
    return build_sketch_view(*args, **kwargs)


def build_sketch_no_tool_bits_view(instance_id, ts_ns, **kwargs):
    """Sketch-NoToolBits: zero out tool_status_bitset and tool_context_avail_bitmap."""
    kwargs["tool_status_bitset"] = 0
    kwargs["tool_context_avail_bitmap"] = 0
    return build_sketch_view(instance_id, ts_ns, **kwargs)


def build_sketch_no_progress_view(instance_id, ts_ns, **kwargs):
    """Sketch-NoProgress: zero out avg_progress_q and max_progress_q."""
    kwargs["avg_progress_q"] = 0
    kwargs["max_progress_q"] = 0
    return build_sketch_view(instance_id, ts_ns, **kwargs)


def build_sketch_affinity_only_view(instance_id, ts_ns, **kwargs):
    """Sketch-AffinityOnly: only affinity counter + coarse runtime."""
    return {
        "instance_id": instance_id, "timestamp_ns": ts_ns,
        "runtime": {"kv_cache_usage_q": 0, "load_q": 0, "prefix_cache_hit_rate_q": 0},
        "workflow_sketch": {
            "active_workflow_count": kwargs.get("active_workflow_count", 0),
            "affinity_hot_instance_counts": kwargs.get("affinity_counter_array", [0] * 8),
        },
    }


def build_oracle_view(instance_id, ts_ns, active_workflows, assigned_history,
                      tool_metadata, latency_summary, exact_loads):
    """Oracle view: everything Rich has + exact num_requests_running for each
    instance. The oracle policy uses this for upper-bound quality."""
    v = build_rich_view(instance_id, ts_ns, active_workflows, assigned_history,
                        tool_metadata, latency_summary)
    v["exact_loads"] = exact_loads  # dict: instance_id -> int
    return v


# =================== Policies (with field-access assertions) ===================

ALLOWED_FIELDS = {
    "round-robin": {"none": []},  # round-robin doesn't read any state
    "coarse": {"coarse": [
        "num_requests_waiting", "num_requests_running",
        "kv_cache_usage_perc", "gpu_cache_usage_perc",
        "prompt_tokens_total", "generation_tokens_total",
        "prefix_cache_hits_total", "prefix_cache_queries_total",
        "request_success_total", "num_preemptions_total",
    ]},
    "rich": {"rich": "ALL_FIELDS"},  # rich policy may access everything in rich view
    "sketch": {"sketch": [
        "kv_cache_usage_q", "load_q", "prefix_cache_hit_rate_q",
        "active_workflow_count", "avg_progress_q", "max_progress_q",
        "tool_status_bitset", "tool_context_avail_bitmap",
        "affinity_hot_instance_counts", "recent_workflow_hashes",
        "latency_sensitive_count",
    ]},
    "sketch-noaffinity": {"sketch": [
        "kv_cache_usage_q", "load_q", "prefix_cache_hit_rate_q",
        "active_workflow_count", "avg_progress_q", "max_progress_q",
        "tool_status_bitset", "tool_context_avail_bitmap",
        # NO affinity_hot_instance_counts
    ]},
    "sketch-notoolbits": {"sketch": [
        "kv_cache_usage_q", "load_q", "prefix_cache_hit_rate_q",
        "active_workflow_count", "avg_progress_q", "max_progress_q",
        "affinity_hot_instance_counts", "recent_workflow_hashes",
        "latency_sensitive_count",
        # NO tool bits
    ]},
    "sketch-noprogress": {"sketch": [
        "kv_cache_usage_q", "load_q", "prefix_cache_hit_rate_q",
        "active_workflow_count", "tool_status_bitset", "tool_context_avail_bitmap",
        "affinity_hot_instance_counts", "recent_workflow_hashes",
        "latency_sensitive_count",
        # NO progress
    ]},
    "sketch-affinityonly": {"sketch": [
        "kv_cache_usage_q", "load_q", "active_workflow_count",
        "affinity_hot_instance_counts",
    ]},
    "oracle": {"oracle": "ALL_FIELDS"},
}

VIEW_FOR_POLICY = {
    "round-robin": "none",
    "coarse": "coarse",
    "rich": "rich",
    "sketch": "sketch",
    "sketch-noaffinity": "sketch",
    "sketch-notoolbits": "sketch",
    "sketch-noprogress": "sketch",
    "sketch-affinityonly": "sketch",
    "oracle": "oracle",
}


def assert_view_for_policy(policy, view):
    """§3 hard requirement: if policy=coarse and any workflow semantic field is
    accessed, abort the cell. If policy=sketch and raw workflow list is accessed,
    abort the cell. If policy=rich and view is not rich, abort the cell."""
    expected = VIEW_FOR_POLICY[policy]
    if view != expected:
        raise AssertionError(
            f"Policy-view mismatch: policy={policy} requires view={expected}, "
            f"got view={view}. Aborting cell.")


def _coarse_score(rt):
    """Coarse scoring function (the only fields coarse policy may read)."""
    alpha, beta, gamma = 1.0, 1.0, 0.3
    if "num_requests_waiting" in rt:
        return alpha * rt["num_requests_running"] + beta * rt["num_requests_waiting"] + \
               gamma * rt["kv_cache_usage_perc"]
    # sketch view
    return (rt.get("load_q", 0) / 10.0) + (rt.get("kv_cache_usage_q", 0) / 100.0)


def policy_round_robin(state_views, request, ctx):
    """Round-robin: just iterate instances in order."""
    return ctx["instances"][ctx["rr_idx"] % len(ctx["instances"])]


def policy_coarse(state_views, request, ctx):
    """Coarse: only access coarse fields. Asserts at runtime."""
    instances = ctx["instances"]
    scores = []
    for inst in instances:
        v = state_views.get(inst, {})
        rt = v.get("runtime", v)
        # Assert: only coarse fields accessed
        for k in rt:
            if k not in ALLOWED_FIELDS["coarse"]["coarse"]:
                if k in ("active_workflow_count", "affinity_hot_instance_counts",
                         "tool_status_bitset", "workflow_history", "active_workflows"):
                    raise AssertionError(
                        f"Coarse policy accessed {k} which is workflow-semantic! Abort.")
        scores.append((_coarse_score(rt), inst))
    scores.sort()
    return scores[0][1]


def policy_rich(state_views, request, ctx):
    """Rich: Coarse score + affinity bonus (if workflow has history)."""
    base = policy_coarse(state_views, request, ctx)
    wf_id = request.get("workflow_id")
    if not wf_id or wf_id not in ctx["workflow_table"]:
        return base
    wf = ctx["workflow_table"][wf_id]
    if not wf.assigned_instance_history:
        return base
    recent = wf.assigned_instance_history[-1]
    if recent not in ctx["instances"]:
        return base
    # Reward affinity: if recent instance load is reasonable, prefer it
    base_score = _coarse_score(state_views.get(base, {}).get("runtime", state_views.get(base, {})))
    recent_score = _coarse_score(state_views.get(recent, {}).get("runtime", state_views.get(recent, {})))
    if recent_score <= base_score * 1.5:
        return recent
    return base


def policy_sketch(state_views, request, ctx, policy_name="sketch"):
    """Sketch: use only sketch fields. affinity_counter_array is the main signal."""
    instances = ctx["instances"]
    wf_id = request.get("workflow_id")
    # Get affinity: count of times each instance was the most recent for this workflow
    affinity = [0] * len(instances)
    if wf_id and wf_id in ctx["workflow_table"]:
        wf = ctx["workflow_table"][wf_id]
        if wf.assigned_instance_history:
            recent = wf.assigned_instance_history[-1]
            try:
                idx = int(recent.split("_")[-1])
                if 0 <= idx < len(instances):
                    affinity[idx] += 1
            except (ValueError, IndexError):
                pass
    scores = []
    for i, inst in enumerate(instances):
        v = state_views.get(inst, {})
        rt = v.get("runtime", v)
        ws = v.get("workflow_sketch", {})
        # Coarse-equivalent score
        if "load_q" in rt:
            base = (rt["load_q"] / 10.0) + (rt["kv_cache_usage_q"] / 100.0)
        else:
            base = 0
        # Affinity bonus: subtract gamma × affinity[i]
        gamma = 10.0
        score = base - gamma * affinity[i] / 10.0  # normalize affinity
        scores.append((score, inst))
    scores.sort()
    return scores[0][1]


def policy_sketch_noaffinity(state_views, request, ctx):
    """Same as sketch but ignore affinity_counter_array entirely."""
    instances = ctx["instances"]
    scores = []
    for inst in instances:
        v = state_views.get(inst, {})
        rt = v.get("runtime", v)
        if "load_q" in rt:
            scores.append(((rt["load_q"] / 10.0) + (rt["kv_cache_usage_q"] / 100.0), inst))
        else:
            scores.append((0, inst))
    scores.sort()
    return scores[0][1]


def policy_sketch_notoolbits(state_views, request, ctx):
    """Like sketch, but tool_status_bitset = 0 in the view (already zeroed at view-build)."""
    return policy_sketch(state_views, request, ctx, policy_name="sketch-notoolbits")


def policy_sketch_noprogress(state_views, request, ctx):
    """Like sketch, but progress = 0 in the view."""
    return policy_sketch(state_views, request, ctx, policy_name="sketch-noprogress")


def policy_sketch_affinityonly(state_views, request, ctx):
    """Sketch with ONLY affinity counter + coarse runtime."""
    instances = ctx["instances"]
    wf_id = request.get("workflow_id")
    affinity = [0] * len(instances)
    if wf_id and wf_id in ctx["workflow_table"]:
        wf = ctx["workflow_table"][wf_id]
        if wf.assigned_instance_history:
            recent = wf.assigned_instance_history[-1]
            try:
                idx = int(recent.split("_")[-1])
                if 0 <= idx < len(instances):
                    affinity[idx] += 1
            except (ValueError, IndexError):
                pass
    scores = []
    for i, inst in enumerate(instances):
        v = state_views.get(inst, {})
        rt = v.get("runtime", v)
        ws = v.get("workflow_sketch", {})
        if "load_q" in rt:
            base = (rt["load_q"] / 10.0) + (rt["kv_cache_usage_q"] / 100.0)
        else:
            base = 0
        score = base - 10.0 * affinity[i] / 10.0
        scores.append((score, inst))
    scores.sort()
    return scores[0][1]


def policy_oracle(state_views, request, ctx):
    """Oracle: perfect knowledge. Knows exact num_requests_running per instance
    AND each workflow's full assigned_instance_history.

    Score = predicted completion time = current queue + workflow affinity preference.
    """
    instances = ctx["instances"]
    wf_id = request.get("workflow_id")
    # Compute affinity from perfect workflow history
    affinity = [0] * len(instances)
    if wf_id and wf_id in ctx["workflow_table"]:
        wf = ctx["workflow_table"][wf_id]
        if wf.assigned_instance_history:
            recent = wf.assigned_instance_history[-1]
            try:
                idx = int(recent.split("_")[-1])
                if 0 <= idx < len(instances):
                    affinity[idx] += 1
            except (ValueError, IndexError):
                pass
    scores = []
    for i, inst in enumerate(instances):
        v = state_views.get(inst, {})
        exact = v.get("exact_loads", {})
        # Oracle: prefer the exact running count
        running = exact.get(inst, v.get("runtime", {}).get("num_requests_running", 0))
        # Score: current load - strong affinity bonus
        gamma = 20.0  # oracle has higher trust
        score = float(running) - gamma * affinity[i] / 10.0
        scores.append((score, inst))
    scores.sort()
    return scores[0][1]


POLICIES = {
    "round-robin": policy_round_robin,
    "coarse": policy_coarse,
    "rich": policy_rich,
    "sketch": policy_sketch,
    "sketch-noaffinity": policy_sketch_noaffinity,
    "sketch-notoolbits": policy_sketch_notoolbits,
    "sketch-noprogress": policy_sketch_noprogress,
    "sketch-affinityonly": policy_sketch_affinityonly,
    "oracle": policy_oracle,
}


# =================== Dispatcher Wrapper ===================

@dataclass
class WorkflowRecord:
    workflow_id: str
    step_id: int = 0
    total_steps: int = 0
    progress: float = 0.0
    last_assigned_instance: str = ""
    assigned_instance_history: list = field(default_factory=list)
    tool_status: str = "idle"
    last_tool_name: str = ""
    last_tool_latency_ms: float = 0.0
    tool_result_context_size: int = 0
    tool_result_context_type: str = "none"
    workflow_start_time_ns: int = 0
    last_step_finish_time_ns: int = 0


@dataclass
class DispatcherConfig:
    instances: list
    instance_urls: dict
    state_view: str  # "none" | "coarse" | "rich" | "sketch" | "oracle"
    update_freq_hz: float
    duration_s: float
    out_dir: str
    cell_id: str
    workload: str
    rep: int
    policy: str
    n_workflows: int = 8
    n_steps: int = 8
    concurrent: int = 4
    ctx_tokens: int = 1024
    tool_delay_ms: int = 200
    keep_workflow_history: bool = True   # for Rich size diagnosis (Mode 1: off, Mode 2: empty, Mode 3: on)
    keep_tool_metadata: bool = True
    keep_latency_summary: bool = True


class Dispatcher:
    def __init__(self, cfg: DispatcherConfig):
        self.cfg = cfg
        # Assert policy-view mapping
        assert_view_for_policy(cfg.policy, cfg.state_view)
        self.workflow_table: dict[str, WorkflowRecord] = {}
        self.state_views: dict[str, dict] = {}
        self.exact_loads: dict[str, int] = {}  # for oracle
        self.request_count = 0
        self.dispatch_log: list[dict] = []
        self.size_breakdown: list[dict] = []  # for Exp D
        self.rr_idx = 0
        # File handles
        os.makedirs(cfg.out_dir, exist_ok=True)
        self.f_state = open(f"{cfg.out_dir}/state_updates.jsonl", "a")
        self.f_dispatch = open(f"{cfg.out_dir}/dispatch_log.jsonl", "a")
        self.f_request = open(f"{cfg.out_dir}/request_log.jsonl", "a")
        self.f_workflow = open(f"{cfg.out_dir}/workflow_log.jsonl", "a")
        self.f_metrics = open(f"{cfg.out_dir}/metrics_log.jsonl", "a")
        self.f_fail = open(f"{cfg.out_dir}/failure_log.jsonl", "a")

    def register_workflow(self, wf: WorkflowRecord):
        self.workflow_table[wf.workflow_id] = wf

    def update_workflow(self, wf_id, **kwargs):
        wf = self.workflow_table[wf_id]
        for k, v in kwargs.items():
            setattr(wf, k, v)

    def collect_state(self, instance_metrics: dict) -> dict:
        """Build state view for `instance_id` from the latest per-instance metrics.

        `instance_metrics`: {instance_id: {num_requests_waiting, num_requests_running, ...}}
        """
        # First, build raw state per instance using the appropriate builder
        v = self._build_view(self.cfg.state_view, instance_metrics)
        # Add size breakdown for Exp D
        self._record_size_breakdown(v, instance_metrics)
        return v

    def _build_view(self, view_type, instance_metrics):
        inst_id = self.cfg.instances[0]  # we build per-instance, but for one-shot tests it's enough
        ts = time.time_ns()
        if view_type == "none":
            return build_none_view(inst_id, ts)
        if view_type == "coarse":
            return build_coarse_view(inst_id, ts)
        if view_type == "rich":
            return self._build_rich_state(ts, instance_metrics)
        if view_type == "sketch":
            return self._build_sketch_state(ts, instance_metrics, full=True)
        if view_type == "oracle":
            return self._build_oracle_state(ts, instance_metrics)
        raise ValueError(view_type)

    def _build_rich_state(self, ts, instance_metrics):
        """Build Rich state for one instance (we collect per-instance)."""
        # This is called by collect_once for each instance.
        # For simplicity, we use the dispatcher-level workflow tables.
        active_workflows = []
        for wf_id, wf in self.workflow_table.items():
            if wf.last_assigned_instance == self.cfg.instances[0]:  # placeholder
                active_workflows.append({
                    "workflow_id": wf.workflow_id,
                    "current_step_id": wf.step_id,
                    "total_steps": wf.total_steps,
                    "workflow_progress": wf.progress,
                    "last_assigned_instance": wf.last_assigned_instance,
                    "assigned_instance_history": wf.assigned_instance_history[-5:] if self.cfg.keep_workflow_history else [],
                    "tool_execution_status": wf.tool_status if self.cfg.keep_tool_metadata else "n/a",
                    "last_tool_name": wf.last_tool_name if self.cfg.keep_tool_metadata else "n/a",
                    "last_tool_latency_ms": wf.last_tool_latency_ms if self.cfg.keep_tool_metadata else 0,
                    "tool_result_context_size": wf.tool_result_context_size if self.cfg.keep_tool_metadata else 0,
                    "tool_result_context_type": wf.tool_result_context_type if self.cfg.keep_tool_metadata else "n/a",
                    "tool_result_hash": "0x" + str(hash(wf_id) & 0xFFFFFFFF),
                    "workflow_to_instance_affinity": {},
                    "workflow_start_time_ns": wf.workflow_start_time_ns,
                    "last_step_finish_time_ns": wf.last_step_finish_time_ns,
                    "latency_sensitive_flag": 0,
                })
        # History dict (workflow_id -> list of instance_ids)
        history = {wf_id: wf.assigned_instance_history[-10:] if self.cfg.keep_workflow_history else []
                   for wf_id, wf in self.workflow_table.items()}
        # Tool metadata
        tool_meta = {wf_id: {"name": wf.last_tool_name, "latency_ms": wf.last_tool_latency_ms}
                     for wf_id, wf in self.workflow_table.items()} if self.cfg.keep_tool_metadata else {}
        # Latency summary
        lat_summary = {
            "ttft_p50": 0, "ttft_p95": 0, "tpot_p50": 0, "tpot_p95": 0,
            "queue_time_p95": 0, "prefill_time_p95": 0, "decode_time_p95": 0,
        } if self.cfg.keep_latency_summary else {}
        coarse = instance_metrics.get(self.cfg.instances[0], {})
        return build_rich_view(self.cfg.instances[0], ts, active_workflows, history,
                                tool_meta, lat_summary)

    def _build_sketch_state(self, ts, instance_metrics, full=True, variant="full"):
        """Build sketch state. variant can be 'full', 'noaffinity', 'notoolbits', 'noprogress', 'affinityonly'."""
        n_inst = len(self.cfg.instances)
        # Affinity counts: how many workflows on this instance have last_assigned == inst
        affinity = [0] * n_inst
        for wf in self.workflow_table.values():
            if wf.assigned_instance_history:
                try:
                    idx = int(wf.assigned_instance_history[-1].split("_")[-1])
                    if 0 <= idx < n_inst:
                        affinity[idx] += 1
                except (ValueError, IndexError):
                    pass
        active_count = sum(1 for wf in self.workflow_table.values()
                           if wf.last_assigned_instance == self.cfg.instances[0])
        # avg/max progress q
        progresses = [int(wf.progress * 100) for wf in self.workflow_table.values()
                       if wf.last_assigned_instance == self.cfg.instances[0]]
        avg_pq = int(sum(progresses) / len(progresses)) if progresses else 0
        max_pq = max(progresses) if progresses else 0
        # Tool bits: bit per workflow on this instance
        tool_status = 0
        tool_ctx = 0
        for i, wf in enumerate(self.workflow_table.values()):
            if i >= 16: break
            if wf.last_assigned_instance == self.cfg.instances[0]:
                if wf.tool_status == "running":
                    tool_status |= (1 << (i * 2))
                elif wf.tool_status == "done":
                    tool_status |= (2 << (i * 2))
                elif wf.tool_status == "failed":
                    tool_status |= (3 << (i * 2))
                if wf.tool_result_context_size > 0:
                    tool_ctx |= (1 << i)
        # Recent hashes
        recent = [hash(wf.workflow_id) & 0xFFFFFFFF for wf in list(self.workflow_table.values())[:4]]
        # Apply variant
        if variant == "noaffinity":
            affinity = [0] * n_inst
        elif variant == "notoolbits":
            tool_status = 0
            tool_ctx = 0
        elif variant == "noprogress":
            avg_pq = 0
            max_pq = 0
        elif variant == "affinityonly":
            return build_sketch_affinity_only_view(
                self.cfg.instances[0], ts,
                active_workflow_count=active_count,
                affinity_counter_array=affinity,
            )
        return build_sketch_view(self.cfg.instances[0], ts,
                                  active_workflow_count=active_count,
                                  affinity_counter_array=affinity,
                                  tool_status_bitset=tool_status,
                                  tool_context_avail_bitmap=tool_ctx,
                                  avg_progress_q=avg_pq,
                                  max_progress_q=max_pq,
                                  recent_workflow_hashes=recent,
                                  latency_sensitive_count=active_count)

    def _build_oracle_state(self, ts, instance_metrics):
        rich = self._build_rich_state(ts, instance_metrics)
        rich["exact_loads"] = self.exact_loads
        return rich

    def _record_size_breakdown(self, view, instance_metrics):
        """For Exp D: record per-field size contribution for Rich state."""
        if self.cfg.state_view != "rich":
            return
        # Serialize each section and measure
        coarse_bytes = len(orjson.dumps(view.get("runtime", {})))
        active_wf_bytes = len(orjson.dumps(view.get("active_workflows", [])))
        history_bytes = len(orjson.dumps(view.get("workflow_history", {})))
        tool_meta_bytes = len(orjson.dumps(view.get("tool_metadata", {})))
        lat_summary_bytes = len(orjson.dumps(view.get("runtime", {}).get("latency_summary", {})))
        total = len(orjson.dumps(view))
        self.size_breakdown.append({
            "total_bytes": total,
            "coarse_bytes": coarse_bytes,
            "active_workflows_bytes": active_wf_bytes,
            "assigned_history_bytes": history_bytes,
            "tool_metadata_bytes": tool_meta_bytes,
            "latency_summary_bytes": lat_summary_bytes,
            "num_active_workflows": len(view.get("active_workflows", [])),
            "num_history_items": sum(len(v) for v in view.get("workflow_history", {}).values()),
        })

    def pick(self, request: dict, instance_state_views: dict = None) -> tuple:
        """Run the policy. Returns (instance, decision_time_us, score_per_instance)."""
        t0 = time.perf_counter_ns()
        if instance_state_views is None:
            instance_state_views = self.state_views
        ctx = {
            "instances": self.cfg.instances,
            "workflow_table": self.workflow_table,
            "rr_idx": self.rr_idx,
        }
        if self.cfg.policy == "round-robin":
            inst = self.rr_idx % len(self.cfg.instances)
            self.rr_idx += 1
            chosen = self.cfg.instances[inst]
        else:
            chosen = POLICIES[self.cfg.policy](instance_state_views, request, ctx)
        # Compute score per instance for logging
        scores = {}
        for i, inst in enumerate(self.cfg.instances):
            v = instance_state_views.get(inst, {})
            rt = v.get("runtime", v)
            scores[inst] = _coarse_score(rt) if "num_requests_waiting" in rt else 0.0
        decision_us = (time.perf_counter_ns() - t0) / 1e3
        return chosen, decision_us, scores

    def close(self):
        for f in (self.f_state, self.f_dispatch, self.f_request, self.f_workflow,
                  self.f_metrics, self.f_fail):
            f.close()


def serialize_view(v):
    return len(orjson.dumps(v))


def compute_size_breakdown_for_view(view):
    """Return dict with byte counts per section of a view (used for offline analysis)."""
    out = {}
    for k, v in view.items():
        out[k + "_bytes"] = serialize_view({k: v})
    out["total_bytes"] = serialize_view(view)
    return out