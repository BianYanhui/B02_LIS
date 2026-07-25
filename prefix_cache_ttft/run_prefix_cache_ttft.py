#!/usr/bin/env python3
"""Isolated single-vLLM prefix-cache TTFT diagnostic.

No dispatcher, gateway, tc, signaling, or multi-instance scheduling is used.
The physical reuse ground truth is vLLM's cached_tokens usage telemetry.
"""
from __future__ import annotations

import argparse, asyncio, csv, datetime as dt, json, math, random, statistics, time, uuid
from collections import defaultdict
from pathlib import Path

import aiohttp
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from transformers import AutoTokenizer

MODEL_PATH = "/home/byh/.cache/modelscope/qwen/Qwen2.5-1.5B-Instruct"
# vLLM exposes a local-path model ID when launched from this cached checkpoint.
# Use that exact API ID; report text still names the Qwen model explicitly.
MODEL = MODEL_PATH
SUFFIX_A = "\nTask A: Reply only READY."
SUFFIX_B = "\nTask B: Summarize the context in one sentence."


def q(values, pct):
    values = sorted(values)
    p = (len(values) - 1) * pct / 100
    lo, hi = int(p), min(int(p) + 1, len(values) - 1)
    return values[lo] + (values[hi] - values[lo]) * (p - lo)


def make_prefix(tok, target, tag):
    """Unique tag is at content start: distinct misses share no long prefix."""
    text, fill = f"[prefix-id:{tag}] ", "evidence "
    count = lambda s: len(tok.encode(s, add_special_tokens=False))
    while count(text + fill) <= target:
        text += fill
    ids = tok.encode(text, add_special_tokens=False)
    if len(ids) > target:
        text = tok.decode(ids[:target], clean_up_tokenization_spaces=False)
    actual = count(text)
    if abs(actual - target) > 2:
        raise RuntimeError(f"unable to create ~{target} content tokens; got {actual}")
    return text, actual


async def ready(url):
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
        async with s.get(url + "/v1/models") as r:
            if r.status != 200:
                raise RuntimeError(f"vLLM unavailable: HTTP {r.status}")


async def _one_request(session, url, prompt, salt, label):
    request_id, usage, server_id = "pcache-" + uuid.uuid4().hex, {}, ""
    payload = {"model": MODEL, "messages": [{"role": "user", "content": prompt}],
               "max_tokens": 1, "min_tokens": 1, "ignore_eos": True, "temperature": 0.0,
               "cache_salt": salt, "stream": True, "stream_options": {"include_usage": True}}
    begin, first = time.perf_counter_ns(), 0
    async with session.post(url + "/v1/chat/completions", json=payload) as r:
        if r.status != 200:
            raise RuntimeError(f"{label}: HTTP {r.status}: {(await r.text())[:240]}")
        buffer = ""
        async for part in r.content.iter_any():
            buffer += part.decode(errors="ignore")
            while "\n\n" in buffer:
                event, buffer = buffer.split("\n\n", 1)
                data = next((x[6:] for x in event.splitlines() if x.startswith("data: ")), "")
                if not data or data == "[DONE]":
                    continue
                msg = json.loads(data)
                server_id = msg.get("id") or server_id
                if msg.get("choices") and not first:
                    first = time.perf_counter_ns()
                if msg.get("usage"):
                    usage = msg["usage"]
    end = time.perf_counter_ns()
    if "prompt_tokens" not in usage:
        raise RuntimeError(f"{label}: missing final vLLM usage telemetry")
    details = usage.get("prompt_tokens_details") or {}
    return {"request_id": request_id, "server_request_id": server_id,
            "timestamp_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
            "ttft_ms": ((first or end) - begin) / 1e6, "total_request_latency_ms": (end - begin) / 1e6,
            "prompt_tokens": usage["prompt_tokens"], "cached_tokens": details.get("cached_tokens", 0),
            "generated_tokens": usage.get("completion_tokens", 0)}


async def request(session, url, prompt, salt, label):
    """Retry only transient client-side disconnects, as in the live harness."""
    errors = []
    for attempt in range(1, 4):
        try:
            return await _one_request(session, url, prompt, salt, label)
        except (aiohttp.ClientError, asyncio.TimeoutError, ConnectionError) as exc:
            errors.append(repr(exc))
            if attempt < 3:
                await asyncio.sleep(0.1 * attempt)
    raise RuntimeError(f"{label}: transient request failures after 3 attempts: {errors}")


def write(path, rows, fields=None):
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields or list(rows[0]))
        w.writeheader(); w.writerows(rows)


def aggregate(rows):
    grouped = defaultdict(list)
    for r in rows: grouped[(int(r["prefix_length_tokens"]), r["condition"])].append(r)
    summary = []
    for (length, condition), data in sorted(grouped.items()):
        ttft, cached = [float(x["ttft_ms"]) for x in data], [float(x["cached_tokens"]) for x in data]
        summary.append({"prefix_length": length, "condition": condition, "n": len(data),
                        "ttft_mean_ms": statistics.mean(ttft), "ttft_std_ms": statistics.stdev(ttft) if len(ttft)>1 else 0,
                        "ttft_p50_ms": q(ttft,50), "ttft_p95_ms": q(ttft,95),
                        "cached_tokens_mean": statistics.mean(cached)})
    lookup = {(x["prefix_length"], x["condition"]): x for x in summary}
    paired=[]
    for length in sorted({x["prefix_length"] for x in summary}):
        miss, hit = lookup[(length,"miss")], lookup[(length,"hit")]
        saving = miss["ttft_mean_ms"]-hit["ttft_mean_ms"]
        paired.append({"prefix_length":length,"ttft_miss_mean_ms":miss["ttft_mean_ms"],"ttft_hit_mean_ms":hit["ttft_mean_ms"],
                       "absolute_saving_ms":saving,"relative_saving_percent":100*saving/miss["ttft_mean_ms"],
                       "miss_cached_tokens_mean":miss["cached_tokens_mean"],"hit_cached_tokens_mean":hit["cached_tokens_mean"]})
    return summary, paired


def figure(summary, paired, output):
    plt.rcParams.update({"font.family":"serif","font.serif":["Times New Roman","DejaVu Serif"],"font.size":8,
                         "pdf.fonttype":42,"ps.fonttype":42,"axes.linewidth":.8,"xtick.direction":"in","ytick.direction":"in"})
    fig, ax = plt.subplots(1,2,figsize=(7.0,2.35),constrained_layout=True)
    lengths=sorted({x["prefix_length"] for x in summary})
    for c, color, marker, label in [("miss","black","o","KV Miss"),("hit","0.48","s","KV Hit")]:
        d={x["prefix_length"]:x for x in summary if x["condition"]==c}
        ax[0].errorbar(lengths,[d[x]["ttft_mean_ms"] for x in lengths],
                       yerr=[d[x]["ttft_std_ms"]/math.sqrt(d[x]["n"]) for x in lengths],color=color,marker=marker,
                       linewidth=1.1,markersize=4,capsize=2,label=label)
    ax[0].set(xlabel="Reusable prefix length (tokens)",ylabel="Mean TTFT (ms)",title="(a) TTFT with and without KV reuse")
    ax[0].legend(frameon=False,fontsize=7); ax[0].grid(axis="y",color=".88",linewidth=.5)
    ax[1].plot([x["prefix_length"] for x in paired],[x["absolute_saving_ms"] for x in paired],color="black",marker="D",linewidth=1.1,markersize=4)
    ax[1].axhline(0,color=".55",linewidth=.7); ax[1].set(xlabel="Reusable prefix length (tokens)",ylabel="TTFT saving (ms)",title="(b) Benefit of KV reuse")
    ax[1].grid(axis="y",color=".88",linewidth=.5); fig.savefig(output,format="pdf",bbox_inches="tight"); plt.close(fig)


def markdown(paired, output, args):
    lines=["# Prefix-cache TTFT diagnostic","","## Setup","",f"- `{MODEL}`, one vLLM at `{args.url}`, concurrency = 1.",
           "- No dispatcher, gateway, tc, signaling, or multi-instance scheduling.","- Hit reuses a completed prefix with a changed suffix; Miss uses a new leading prefix and cache salt.","",
           "## Results","","| Prefix | Miss TTFT | Hit TTFT | Saving | Saving | Miss cached | Hit cached |","|---:|---:|---:|---:|---:|---:|---:|"]
    for x in paired: lines.append(f"| {x['prefix_length']} | {x['ttft_miss_mean_ms']:.1f} ms | {x['ttft_hit_mean_ms']:.1f} ms | {x['absolute_saving_ms']:.1f} ms | {x['relative_saving_percent']:.1f}% | {x['miss_cached_tokens_mean']:.1f} | {x['hit_cached_tokens_mean']:.1f} |")
    inc = paired[-1]["absolute_saving_ms"] > paired[0]["absolute_saving_ms"]
    lines += ["","## Interpretation","",f"1. 512-token Hit/Miss gap: {paired[0]['absolute_saving_ms']:.1f} ms.",
              *[f"{x['prefix_length']}-token gap: {x['absolute_saving_ms']:.1f} ms." for x in paired[1:]],
              f"2. Saving {'increases' if inc else 'does not increase'} between the shortest and longest tested prefix.",
              "3. Hit/Miss validity is confirmed by low miss cached tokens and materially higher hit cached tokens; the pre-run sanity gate aborts otherwise.",
              "4. This isolates Prefill/KV reuse.  It cannot attribute any main-experiment effect to dispatch or signaling."]
    if inc and paired[-1]["absolute_saving_ms"] > 2*max(1,paired[0]["absolute_saving_ms"]):
        lines.append("5. The isolated trend is consistent with short reusable prefixes contributing to a weak end-to-end TTFT effect on Qwen2.5-1.5B/T4.")
    else: lines.append("5. The isolated trend does not by itself establish that short prefixes explain weak end-to-end TTFT effects.")
    output.write_text("\n".join(lines)+"\n")


async def main_async(args):
    out=Path(args.output).resolve(); out.mkdir(parents=True,exist_ok=True); await ready(args.url)
    tok=AutoTokenizer.from_pretrained(args.model_path,trust_remote_code=True)
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=args.timeout)) as s:
        for i in range(2):
            p,_=make_prefix(tok,128,f"warm-{i}"); await request(s,args.url,p+SUFFIX_A,f"warm-{i}","warmup")
        hp,ha=make_prefix(tok,512,"sanity-hit"); mp,ma=make_prefix(tok,512,"sanity-miss")
        miss=await request(s,args.url,mp+SUFFIX_B,"sanity-miss","sanity miss")
        await request(s,args.url,hp+SUFFIX_A,"sanity-hit","sanity populate")
        hit=await request(s,args.url,hp+SUFFIX_B,"sanity-hit","sanity hit")
        sanity={"prefix_tokens_requested":512,"hit_prefix_tokens_actual":ha,"miss_prefix_tokens_actual":ma,"miss":miss,"hit":hit}
        (out/"sanity_check.json").write_text(json.dumps(sanity,indent=2)+"\n")
        print(f"Miss cached tokens = {miss['cached_tokens']}"); print(f"Hit cached tokens = {hit['cached_tokens']}")
        if miss["cached_tokens"]>64 or hit["cached_tokens"]<128 or hit["cached_tokens"]<=miss["cached_tokens"]+128:
            raise RuntimeError("sanity failed: physical Hit/Miss cached-token separation is insufficient")
        rows=[]; rng=random.Random(args.seed)
        for length in args.prefix_lengths:
            for rep in range(args.repetitions):
                hp,ha=make_prefix(tok,length,f"hit-L{length}-R{rep}"); mp,ma=make_prefix(tok,length,f"miss-L{length}-R{rep}")
                order="hit_then_miss" if rng.randrange(2) else "miss_then_hit"
                async def hitrow():
                    await request(s,args.url,hp+SUFFIX_A,f"hit/L{length}/R{rep}","populate")
                    r=await request(s,args.url,hp+SUFFIX_B,f"hit/L{length}/R{rep}","hit")
                    r.update(prefix_length_tokens=length,prefix_length_actual_tokens=ha,repetition=rep,condition="hit",order=order); return r
                async def missrow():
                    r=await request(s,args.url,mp+SUFFIX_B,f"miss/L{length}/R{rep}","miss")
                    r.update(prefix_length_tokens=length,prefix_length_actual_tokens=ma,repetition=rep,condition="miss",order=order); return r
                rows.extend([await hitrow(),await missrow()] if order=="hit_then_miss" else [await missrow(),await hitrow()])
                print(f"completed L={length} rep={rep} {order}",flush=True)
    fields=["prefix_length_tokens","prefix_length_actual_tokens","repetition","condition","order","ttft_ms","prompt_tokens","cached_tokens","generated_tokens","total_request_latency_ms","timestamp_utc","request_id","server_request_id"]
    write(out/"raw_results.csv",rows,fields); summary,paired=aggregate(rows); write(out/"summary.csv",summary); write(out/"paired_summary.csv",paired)
    figure(summary,paired,out/"fig_prefix_cache_ttft.pdf"); markdown(paired,out/"report.md",args)
    (out/"run_manifest.json").write_text(json.dumps({"model":MODEL,"url":args.url,"prefix_lengths":args.prefix_lengths,"repetitions":args.repetitions,"seed":args.seed,"concurrency":1,"excluded_components":["dispatcher","gateway","tc","signaling","multi-instance scheduling"]},indent=2)+"\n")


if __name__=="__main__":
    p=argparse.ArgumentParser(); p.add_argument("--url",default="http://127.0.0.1:8010"); p.add_argument("--model-path",default=MODEL_PATH); p.add_argument("--output",default="analysis/prefix_cache_ttft"); p.add_argument("--prefix-lengths",type=int,nargs="+",default=[512,1024,2048,4096,8192]); p.add_argument("--repetitions",type=int,default=15); p.add_argument("--seed",type=int,default=20260725); p.add_argument("--timeout",type=int,default=240); args=p.parse_args()
    if args.repetitions<10 or any(x<=0 or x%16 for x in args.prefix_lengths): raise ValueError("need >=10 repetitions and positive 16-token-multiple lengths")
    asyncio.run(main_async(args))
