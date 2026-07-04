#!/usr/bin/env python3
"""
Probe 2: introspect vLLM in-process via vllm.LLM/LLMEngine.

This is the in-process test — it brings up the engine without HTTP serving,
which lets us see exactly what fields the scheduler and engine expose.

Usage:
    source ~/B02/poc/.venv/bin/activate
    python probe_02_vllm_engine.py [--model MODEL]

If --no-load is passed we skip model loading and only probe module imports,
useful when CUDA is busy or no GPU is free.
"""
import argparse
import json
import os
import sys
import time
from typing import Any, Dict, List

def safe_repr(obj) -> str:
    try:
        r = repr(obj)
        return r if len(r) < 200 else r[:197] + "..."
    except Exception as e:
        return f"<unreprable: {type(e).__name__}: {e}>"

def dump_attrs(obj, prefix="", max_depth=2, depth=0, visited=None):
    """Walk an object's attributes up to max_depth, returning a tree of types."""
    if visited is None:
        visited = set()
    if depth > max_depth:
        return
    oid = id(obj)
    if oid in visited:
        return
    visited.add(oid)
    try:
        for attr in sorted(dir(obj)):
            if attr.startswith("_"):
                continue
            try:
                value = getattr(obj, attr)
            except Exception:
                continue
            tname = type(value).__name__
            line = f"{prefix}.{attr} :: {tname}"
            print(line)
            if depth < max_depth and tname in ("Scheduler", "Worker", "ModelRunner", "CacheEngine", "KVCache"):
                dump_attrs(value, prefix + "." + attr, max_depth, depth + 1, visited)
    except Exception as e:
        print(f"{prefix}<cannot iterate: {e}>")
    visited.discard(oid)

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", type=str, default=os.environ.get("MODEL", "Qwen/Qwen2.5-1.5B-Instruct"))
    ap.add_argument("--max-tokens", type=int, default=20)
    ap.add_argument("--max-model-len", type=int, default=4096)
    ap.add_argument("--gpu-mem-util", type=float, default=0.85)
    ap.add_argument("--no-load", action="store_true", help="skip model load and only probe imports")
    ap.add_argument("--dump-json", type=str, default=None)
    args = ap.parse_args()

    print("== Probe 2: vLLM in-process engine internals ==")
    print(f"   model: {args.model}")
    print()

    # Module structure check
    try:
        import vllm
        print(f"[OK] vllm module: {vllm.__version__}")
    except Exception as e:
        print(f"[FAIL] cannot import vllm: {e}")
        return 1

    submodules = [
        ("vllm", "LLM"),
        ("vllm", "SamplingParams"),
        ("vllm.engine.llm_engine", "LLMEngine"),
        ("vllm.engine.async_llm_engine", "AsyncLLMEngine"),
        ("vllm.core.scheduler", "Scheduler"),
        ("vllm.worker.worker", "Worker"),
        ("vllm.spec_decode", None),
    ]
    print("-- importable entry points --")
    for mod, name in submodules:
        try:
            m = __import__(mod, fromlist=name and [name] or [])
            if name:
                obj = getattr(m, name, None)
                if obj is None:
                    print(f"   {mod}.{name}  ::  MISSING")
                else:
                    print(f"   {mod}.{name}  ::  OK ({type(obj).__name__})")
            else:
                print(f"   {mod}  ::  OK")
        except Exception as e:
            print(f"   {mod}{(('.' + name) if name else '')}  ::  FAIL ({type(e).__name__})")
    print()

    if args.no_load:
        return 0

    # Load model
    print(f"[step] loading model={args.model} gpu_mem={args.gpu_mem_util} ...")
    try:
        from vllm import LLM, SamplingParams
    except Exception as e:
        print(f"[FAIL] import vllm.LLM: {e}")
        return 1
    t0 = time.time()
    llm = LLM(model=args.model, gpu_memory_utilization=args.gpu_mem_util,
              max_model_len=args.max_model_len, enforce_eager=False)
    print(f"[OK] model loaded in {time.time() - t0:.1f}s")
    print()

    # === Engine tree ===
    print("-- engine object tree (top-level public attrs) --")
    engine = getattr(llm, "llm_engine", llm)
    dump_attrs(engine, prefix="engine", max_depth=2)
    print()

    # === Try to reach scheduler ===
    print("-- scheduler introspection --")
    sched = None
    for path in ("scheduler", "scheduler_list", "_scheduler", "engine.scheduler"):
        try:
            obj = engine
            for p in path.split("."):
                obj = getattr(obj, p)
            sched = obj
            print(f"   found scheduler via engine.{path}: {type(sched).__name__}")
            break
        except AttributeError:
            continue
    if sched is None:
        # newer versions: scheduler might live under engine_core
        print("   cannot find scheduler on engine; trying engine_core ...")
        ec = getattr(engine, "engine_core", None)
        if ec:
            print(f"   engine_core: {type(ec).__name__}")
            dump_attrs(ec, prefix="engine_core", max_depth=1)
    else:
        dump_attrs(sched, prefix="scheduler", max_depth=1)
    print()

    # === Generate a few requests and inspect output shape ===
    print("-- generating sample requests --")
    prompts = ["Hello, my name is", "The capital of France is", "Once upon a time,"]
    sp = SamplingParams(max_tokens=args.max_tokens)
    t0 = time.time()
    outputs = llm.generate(prompts, sp)
    dt = time.time() - t0
    print(f"   generated {len(outputs)} outputs in {dt:.2f}s")
    print()
    print("-- output shape --")
    for i, out in enumerate(outputs[:3]):
        print(f"   output[{i}]: type={type(out).__name__}")
        print(f"      attrs: {sorted(a for a in dir(out) if not a.startswith('_'))}")
        print(f"      request_id: {getattr(out, 'request_id', 'n/a')!r}")
        try:
            print(f"      prompt_token_ids[:8]: {out.prompt_token_ids[:8]}")
            print(f"      outputs[0].text[:60]: {out.outputs[0].text[:60]!r}")
            print(f"      outputs[0].token_ids[:8]: {out.outputs[0].token_ids[:8]}")
            print(f"      outputs[0].finish_reason: {out.outputs[0].finish_reason}")
        except Exception as e:
            print(f"      partial dump failed: {e}")
    print()

    # === Inspect after generation: scheduler waiting/running lists ===
    print("-- post-generate scheduler queue lengths --")
    def try_count(path):
        obj = sched
        for p in path.split("."):
            if not hasattr(obj, p):
                return None
            obj = getattr(obj, p)
        try:
            return len(obj)
        except TypeError:
            try:
                return len(list(obj))
            except Exception:
                return None
    if sched is not None:
        for p in ("waiting", "running", "swapped", "finished_requests"):
            n = try_count(p)
            print(f"   scheduler.{p}: {n}")
    else:
        print("   no scheduler handle; skipped")
    print()

    # === Final notes for FINDINGS.md ===
    if args.dump_json:
        findings = {
            "model": args.model,
            "vllm_version": getattr(vllm, "__version__", "?"),
            "imported": [m for m, n in submodules],
            "scheduler_handle_obtained": sched is not None,
            "output_request_id_seen": any(getattr(o, 'request_id', None) is not None for o in outputs),
        }
        with open(args.dump_json, "w") as f:
            json.dump(findings, f, indent=2)
        print(f"[OK] wrote {args.dump_json}")

    return 0

if __name__ == "__main__":
    sys.exit(main())
