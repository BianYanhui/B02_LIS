# State Extraction PoC

## Background

The B02 Motivation experiment requires a Dispatcher to maintain four State View
designs (No / Coarse / Rich / Sketch State). Rich State includes fields from
vLLM internals plus agentic-workflow wrappers.

This PoC tests which fields can actually be extracted from a real serving
engine with minimal instrumentation effort.

## Goals

For each field in B02 §5.3, classify:

- `easy`   - obtained via public API / metrics endpoint
- `medium` - requires engine internals hook or wrapper SDK
- `hard`   - requires deep instrumentation (monkey-patch, custom build)
- `nope`   - infeasible without major rework

## Method

1. Install vLLM (or llama.cpp as fallback) inside an isolated venv here.
2. Serve a small 1.5B-3B model on GPU 0 only (keep GPUs 1-3 free).
3. Send requests to drive non-trivial state.
4. Probe vLLM's metrics endpoint, engine internals, request wrappers.
5. Record findings in `FINDINGS.md`.

## Hardware

- 4x Tesla T4 16 GB (CUDA 12.8, driver 570.211)
- This PoC only uses GPU 0 to keep resources free for the full experiment.
