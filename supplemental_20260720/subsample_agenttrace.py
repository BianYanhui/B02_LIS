#!/usr/bin/env python3
"""Subsample an AgentTrace-schema JSONL to a fixed number of sessions.

The NL2Bash source used by the paper has 200 sessions (~4.3 steps each), so a
512-request replay revisits every lineage ~2.5 times.  This subsampler puts
other traces into the same dense-reuse regime; otherwise sessions appear at
most once and no lineage reuse can materialize in the replay window.
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
    parser.add_argument("--min-steps", type=int, default=2)
    parser.add_argument("--sessions", type=int, default=200)
    parser.add_argument("--seed", type=int, default=20260720)
    args = parser.parse_args()

    rows = [json.loads(line) for line in Path(args.input).open()]
    eligible = [row for row in rows if len(row.get("llm_steps") or []) >= args.min_steps]
    rng = random.Random(args.seed)
    rng.shuffle(eligible)
    kept = eligible[: args.sessions]
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w") as handle:
        for row in kept:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    steps = [len(row["llm_steps"]) for row in kept]
    print(json.dumps({
        "source_sessions": len(rows), "eligible": len(eligible), "kept": len(kept),
        "mean_steps": sum(steps) / max(1, len(steps)), "total_steps": sum(steps),
    }))


if __name__ == "__main__":
    main()
