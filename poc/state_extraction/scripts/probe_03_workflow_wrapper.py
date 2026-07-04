#!/usr/bin/env python3
"""
Probe 3: workflow / tool / affinity state - WRAPPER concern (NOT vLLM internals).

The B02 §5.3 Rich State fields about workflows (active_workflow_ids,
workflow_step_id, workflow_progress, tool_execution_status, tool_result_context,
workflow-to-instance affinity) are NOT tracked by vLLM. They must be supplied
by an outer wrapper that the Dispatcher controls.

This probe demonstrates how such a wrapper would be built, what the payload
looks like, and how big it is at various workload scales.
"""
import argparse
import json
import os
import random
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

import json as _json  # for serialised size measurement


@dataclass
class WorkflowStepRecord:
    step_id: int
    status: str                  # "running" / "done" / "failed"
    started_at: float
    finished_at: Optional[float]
    input_tokens: int
    output_tokens: int
    tool_used: Optional[str]
    tool_result_summary: Optional[str]   # NOT raw tool output - just summary
    assigned_instance: Optional[str]


@dataclass
class WorkflowRecord:
    workflow_id: str
    affinity_instance: str        # which GPU/instance owns this workflow
    created_at: float
    steps: List[WorkflowStepRecord] = field(default_factory=list)


class WorkflowTracker:
    """Mimics the workflow-state view the Dispatcher would maintain."""
    def __init__(self, instance_pool_size: int = 4):
        self.instances = [f"gpu{i}" for i in range(instance_pool_size)]
        self.workflows: Dict[str, WorkflowRecord] = {}
        self.completed_steps: List[Dict[str, Any]] = []

    def start_workflow(self, n_steps: int) -> str:
        wf_id = f"wf-{uuid.uuid4().hex[:8]}"
        wf = WorkflowRecord(
            workflow_id=wf_id,
            affinity_instance=random.choice(self.instances),
            created_at=time.time(),
        )
        for i in range(n_steps):
            wf.steps.append(WorkflowStepRecord(
                step_id=i, status="pending", started_at=0.0, finished_at=None,
                input_tokens=0, output_tokens=0, tool_used=None,
                tool_result_summary=None, assigned_instance=None,
            ))
        self.workflows[wf_id] = wf
        return wf_id

    def progress_workflow(self, wf_id: str, n_steps_done: int = 1):
        wf = self.workflows[wf_id]
        for i, s in enumerate(wf.steps):
            if s.status == "pending" and n_steps_done > 0:
                s.status = "running"
                s.started_at = time.time()
                s.assigned_instance = wf.affinity_instance
                s.input_tokens = random.randint(100, 800)
                s.output_tokens = random.randint(50, 400)
                if i % 2 == 0:
                    s.tool_used = random.choice([None, "web_search", "code_exec", "file_io"])
                    if s.tool_used:
                        s.tool_result_summary = "OK brief summary"
                n_steps_done -= 1
            elif s.status == "running" and n_steps_done > 0:
                s.status = "done"
                s.finished_at = time.time()
                n_steps_done -= 1

    def rich_state_view(self, max_workflows: int = 256) -> Dict[str, Any]:
        """Return the wrapper-layer Rich State sub-view payload."""
        wfs = list(self.workflows.values())[:max_workflows]
        return {
            "active_workflow_count": len(self.workflows),
            "active_workflow_ids": [w.workflow_id for w in wfs],
            "workflows": [
                {
                    "id": w.workflow_id,
                    "affinity_instance": w.affinity_instance,
                    "n_steps_total": len(w.steps),
                    "n_steps_done": sum(1 for s in w.steps if s.status == "done"),
                    "current_step_id": max((s.step_id for s in w.steps if s.status == "running"), default=None),
                    "steps": [
                        {
                            "sid": s.step_id,
                            "status": s.status,
                            "in_t": s.input_tokens,
                            "out_t": s.output_tokens,
                            "tool": s.tool_used,
                            "result_summary": s.tool_result_summary,
                            "instance": s.assigned_instance,
                        }
                        for s in w.steps if s.status in ("running", "done")
                    ],
                }
                for w in wfs
            ],
        }

    def sketch_state_view(self, max_workflows: int = 256) -> Dict[str, Any]:
        """Compressed workflow view - smaller payload, dispatch-relevant only."""
        wfs = list(self.workflows.values())[:max_workflows]
        # per-instance aggregates
        per_instance: Dict[str, Dict[str, int]] = {}
        for w in wfs:
            inst = w.affinity_instance
            per_instance.setdefault(inst, {"active": 0, "running_steps": 0, "latency_sensitive": 0})
            per_instance[inst]["active"] += 1
            per_instance[inst]["running_steps"] += sum(1 for s in w.steps if s.status == "running")
            # heuristic: workflows with many done steps are "deep" - latency sensitive
            done = sum(1 for s in w.steps if s.status == "done")
            if done >= 4:
                per_instance[inst]["latency_sensitive"] += 1
        return {
            "active_workflow_count": len(self.workflows),
            "per_instance": per_instance,
            "has_unbound_tool_context": sum(
                1 for w in wfs for s in w.steps if s.tool_used and s.tool_result_summary is None
            ),
        }


def measure_payload(label: str, payload: Any) -> int:
    s = json.dumps(payload, separators=(",", ":"))
    print(f"   {label:<26} payload bytes = {len(s.encode('utf-8'))}")
    return len(s.encode("utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n-workflows", type=int, default=64)
    ap.add_argument("--steps-per-workflow", type=int, default=8)
    ap.add_argument("--dump-json", type=str, default=None)
    args = ap.parse_args()

    print("== Probe 3: wrapper-layer workflow state ==")
    print(f"   n_workflows={args.n_workflows} steps/wf={args.steps_per_workflow}")
    print()

    random.seed(0)
    tracker = WorkflowTracker(instance_pool_size=4)
    for i in range(args.n_workflows):
        wf_id = tracker.start_workflow(args.steps_per_workflow)
        tracker.progress_workflow(wf_id, n_steps_done=min(2, args.steps_per_workflow))

    rich = tracker.rich_state_view()
    sketch = tracker.sketch_state_view()
    print("-- payload sizes --")
    measure_payload("rich_state (workflows)",   rich)
    measure_payload("sketch_state (workflows)", sketch)

    print()
    print("-- sketch snapshot --")
    print(json.dumps(sketch, indent=2)[:600])
    if len(json.dumps(sketch)) > 600:
        print("   ... (truncated)")
    print()
    print("-- rich snapshot (first workflow only, truncated) --")
    if rich["workflows"]:
        first = rich["workflows"][0]
        print(json.dumps(first, indent=2)[:800])
    print()

    if args.dump_json:
        with open(args.dump_json, "w") as f:
            json.dump({"rich": rich, "sketch": sketch}, f, indent=2)
        print(f"[OK] wrote {args.dump_json}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
