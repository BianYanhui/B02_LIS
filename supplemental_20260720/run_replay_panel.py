#!/usr/bin/env python3
"""Multi-trace structural replay panel (supplementary item 3, 2026-07-20).

Runs the *frozen* V3 structural replay and V4 admission/Oracle harnesses,
unedited, over three workload families:

  nl2bash  - AgentTrace NL2Bash 1.7B (the paper's original trace)
  mbpp     - AgentTrace MBPP 1.7B (second agentic task family)
  sharegpt - ShareGPT Vicuna unfiltered, 8k multi-turn conversations
             converted to the AgentTrace schema (real-chat family)

The wrapper only patches the source-name metadata constants per run so cell
tables carry truthful provenance; harness logic is byte-identical to the
paper's runs.  Question answered per trace: does the K sweet spot, the
coverage-first admission ranking, and the Oracle gap survive across workload
families?
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, "/home/byh/B02/supplemental_20260715")

import run_agenttrace_structural_replay_v3 as v3  # noqa: E402
import run_agenttrace_admission_oracle_v4 as v4  # noqa: E402

TRACES = [
    {
        "name": "mbpp_s200",
        "source": "/home/byh/B02/supplemental_20260720/sources/agenttrace_mbpp_s200.jsonl",
        "source_name": "pagarsky/agent-trace: mbpp_1_7B_20260403T211347Z (200-session dense subsample, seed 20260720)",
        "family": "agentic_tooluse",
    },
    {
        "name": "sharegpt_s200",
        "source": "/home/byh/B02/supplemental_20260720/sources/sharegpt_chat_s200.jsonl",
        "source_name": "anon8231489123/ShareGPT_Vicuna_unfiltered: V3 cleaned split (200-session dense subsample, min 3 turns, seed 20260721)",
        "family": "real_chat_multiturn",
    },
]

OUT_ROOT = Path("/home/byh/B02/supplemental_20260720")


def run_one(trace: dict) -> dict:
    started = time.time()
    for module in (v3, v4):
        module.SOURCE_NAME = trace["source_name"]
    record = {"trace": trace["name"], "family": trace["family"]}
    argv = sys.argv
    try:
        sys.argv = [
            "run_agenttrace_structural_replay_v3.py",
            "--source-jsonl", trace["source"],
            "--out-dir", str(OUT_ROOT / f"replay_{trace['name']}_structural"),
        ]
        v3.main()
        record["structural"] = "ok"
        sys.argv = [
            "run_agenttrace_admission_oracle_v4.py",
            "--source-jsonl", trace["source"],
            "--out-dir", str(OUT_ROOT / f"replay_{trace['name']}_admission"),
        ]
        v4.main()
        record["admission"] = "ok"
    finally:
        sys.argv = argv
    record["duration_s"] = time.time() - started
    return record


def main() -> None:
    records = []
    for trace in TRACES:
        print(json.dumps({"starting": trace["name"]}), flush=True)
        records.append(run_one(trace))
        print(json.dumps(records[-1]), flush=True)
    (OUT_ROOT / "replay_panel_runs.json").write_text(json.dumps(records, indent=2))


if __name__ == "__main__":
    main()
