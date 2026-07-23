#!/usr/bin/env python3
"""Convert ShareGPT-style chat logs to the AgentTrace JSONL schema.

The B02 structural replay harnesses (run_agenttrace_structural_replay_v3.py,
run_agenttrace_admission_oracle_v4.py) consume JSONL sessions with keys:
trace_id, metadata.chat_template, prompt, spans[], llm_steps[].
This converter maps multi-turn chat conversations onto that schema so the
*frozen* replay code runs unchanged on a real-chat workload family:

  trace_id                <- conversation id
  metadata.chat_template  <- "sharegpt-vicuna-v1"
  prompt                  <- first human turn text
  spans                   <- [] (chat has no tool executions)
  llm_steps[i].model_output <- gpt reply i PLUS the next human turn text
                               (conversation history grows with both sides;
                               the harness only uses text *lengths* to derive
                               prefix tokens, so folding the next human turn
                               into the previous step preserves the true
                               context-growth profile)

Only conversations with at least --min-turns gpt replies are kept.
No chat text is modified; the downstream harness hashes/lengths it and
discards raw text from its outputs, exactly as for AgentTrace.
"""
from __future__ import annotations

import argparse
import json
import random
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--min-turns", type=int, default=2)
    parser.add_argument("--max-sessions", type=int, default=8000)
    parser.add_argument("--seed", type=int, default=20260720)
    args = parser.parse_args()

    conversations = json.loads(Path(args.input).read_text())
    if not isinstance(conversations, list):
        raise ValueError("expected a JSON list of conversations")

    sessions: list[dict] = []
    for conv in conversations:
        turns = conv.get("conversations") or []
        roles = [turn.get("from") for turn in turns]
        texts = [str(turn.get("value") or "") for turn in turns]
        pairs: list[tuple[str, str]] = []  # (human, gpt) in order
        cursor = 0
        while cursor + 1 < len(roles):
            if roles[cursor] == "human" and roles[cursor + 1] == "gpt":
                pairs.append((texts[cursor], texts[cursor + 1]))
                cursor += 2
            else:
                cursor += 1
        if len(pairs) < args.min_turns:
            continue
        first_human = pairs[0][0]
        steps: list[dict] = []
        for index, (_, gpt_text) in enumerate(pairs):
            # Fold the next human turn into this step's output so history
            # length grows exactly as the real conversation context does.
            next_human = pairs[index + 1][0] if index + 1 < len(pairs) else ""
            steps.append({
                "reasoning_content": "",
                "model_output": gpt_text + ("\n" + next_human if next_human else ""),
            })
        sessions.append({
            "trace_id": str(conv.get("id", f"conv-{len(sessions)}")),
            "timestamp_utc": "",
            "prompt": first_human,
            "spans": [],
            "llm_steps": steps,
            "metadata": {"chat_template": "sharegpt-vicuna-v1", "source": "sharegpt_v3_unfiltered_cleaned_split"},
        })

    rng = random.Random(args.seed)
    rng.shuffle(sessions)
    sessions = sessions[: args.max_sessions]
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w") as handle:
        for session in sessions:
            handle.write(json.dumps(session, ensure_ascii=False) + "\n")
    steps_per = [len(s["llm_steps"]) for s in sessions]
    print(json.dumps({
        "kept_sessions": len(sessions),
        "mean_turns": sum(steps_per) / max(1, len(steps_per)),
        "max_turns": max(steps_per, default=0),
        "output": str(out),
    }))


if __name__ == "__main__":
    main()
