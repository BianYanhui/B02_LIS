# State Extraction PoC - Findings (DRAFT)

> Working document. Will be filled in as probes run.

## TL;DR

| Status | Count | Items |
|---|---|---|
| easy (HTTP /metrics)   | TBD | |
| medium (engine internals via probe_02) | TBD | |
| hard (deep instrumentation) | TBD | |
| nope (infeasible) | TBD | |
| wrapper concern (OUT of vLLM scope) | 6 | active_workflow_ids, workflow_step_id, workflow_progress, tool_execution_status, tool_result_context_metadata, workflow-to-instance affinity |

## 1. Probe 1: vLLM HTTP /metrics endpoint

(Fill in after running `serve_vllm.sh` then `probe_01_vllm_metrics.py`.)

Expected covered fields from B02 §5.3:

- queue_length, running, kv_cache_usage, prefix_cache, prompt_tokens, generation_tokens, e2e_latency, etc.

## 2. Probe 2: in-process LLMEngine internals

(Fill in after running `probe_02_vllm_engine.py`.)

Captures: scheduler.waiting / running queues, per-request request_id / token_ids / phase.

## 3. Probe 3: wrapper-layer workflow state

Payload size at default (json-ascii) format:

| view          | 64 wf x 8 steps (early measurement) |
|---------------|-------:|
| coarse        | 249 B  |
| rich          | 4 808 B |
| sketch        | 270 B  |

> Run the script for fresh numbers at any scale:
>
>   python probe_03_workflow_wrapper.py --n-workflows 256 --steps-per-workflow 16

Conclusion: workflow-only Rich State is ~19x Coarse and ~18x Sketch.

## 4. Probe 4: dispatcher-side cost (serialization round-trip)

Measured on this host (single CPU thread, 50 Hz, 2 s, Rich view):

| format      | bytes | ser_us | deser_us | round_us | qps_possible |
|-------------|------:|-------:|---------:|---------:|-------------:|
| json-utf8   | 5 284 | 169.6  | 162.9    | 332.5    | 3 007        |
| json-ascii  | 4 808 | 186.8  | 170.7    | 357.5    | 2 797        |
| orjson      | 4 808 |  18.2  |  70.8    |  89.0    | 11 236       |
| msgpack     | -- (not installed) |||||

Conclusions to verify on real data:

- orjson dominates stdlib json (~3.7x faster round-trip on this host).
- Rich view ~4.8 KB at 50 Hz per instance ~ 240 KB/s per instance; 4 instances ~ 960 KB/s; linear in N.
- orjson round-trip 89 us at 50 Hz x 4 instances = 200 updates/s; dispatcher cpu ~ 18 ms/s ~ 2% of one core.

## 5. Threats to validity

- Probe_03 and Probe_04 use **simulated** workflow data; real agentic patterns may differ.
- Probe_02 introspection reflects vLLM `__version__` at run time; engine internals can shift across versions.
- Loopback measurements (localhost) undersell network cost; production will see NIC latency.
- T4 GPU is a research-server budget choice; larger GPUs may absorb state cost differently (esp. prefix caching).

## 6. Next steps if motivation holds

1. Build the 4-GPU vLLM serving experiment per B02 §4 Part A.
2. Wire the Dispatcher (probe_04-style hot loop) to 4 vLLM serve endpoints.
3. Run chatbot + agentic workloads per §6 with the 4 state view designs.
4. Aggregate into the report tables per §13.

## 7. Next steps if motivation is weak

If overhead is negligible, the experiment should still be completed (per §14 anti-bias),
and the prompt authors should consider a **stronger** motivation (e.g., cost of
workflow-aware state, not just metadata state).

## Reproducibility

All scripts live in `scripts/`. Order:

1. `bash scripts/install_vllm.sh`           # ~10 min
2. `bash scripts/serve_vllm.sh`            # background, blocks until /health ready
3. `source ~/B02/poc/.venv/bin/activate && python scripts/probe_01_vllm_metrics.py --dump-json findings_01.json`
4. `python scripts/probe_02_vllm_engine.py` (kills any serve before running)
5. `bash scripts/stop_vllm.sh`
6. `python scripts/probe_03_workflow_wrapper.py --dump-json findings_03.json`
7. `python scripts/probe_04_dispatcher_cost.py --dump-json findings_04.json`
8. Fill in `FINDINGS.md` from the JSON dumps.
