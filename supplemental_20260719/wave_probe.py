#!/usr/bin/env python3
"""Wave probe: distinct long prompts across 4 vLLM endpoints, S3-style waves."""
import asyncio
import sys

import aiohttp

MODEL = "/home/byh/.cache/modelscope/qwen/Qwen2.5-1.5B-Instruct"


async def one(session, port, tag):
    prompt = f"Probe distinct lineage {tag}. " + ("context " * 2048)
    payload = {
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 4, "min_tokens": 4, "ignore_eos": True, "temperature": 0.0,
        "cache_salt": f"probe:{tag}", "stream": True, "stream_options": {"include_usage": True},
    }
    try:
        async with session.post(
            f"http://127.0.0.1:{port}/v1/chat/completions", json=payload,
            timeout=aiohttp.ClientTimeout(total=180),
        ) as response:
            await response.read()
            return response.status
    except Exception as exc:  # noqa: BLE001
        return f"EXC:{exc!r:.80}"


async def wave(session, total, tag0):
    results = await asyncio.gather(*[one(session, 8000 + i % 4, f"{tag0}-{i}") for i in range(total)])
    ok = sum(1 for r in results if r == 200)
    return ok, [r for r in results if r != 200][:3]


async def main():
    wave_size = int(sys.argv[1]) if len(sys.argv) > 1 else 12
    n_waves = int(sys.argv[2]) if len(sys.argv) > 2 else 3
    async with aiohttp.ClientSession() as session:
        for w in range(n_waves):
            ok, bad = await wave(session, wave_size, f"w{w}")
            print(f"wave-of-{wave_size} #{w}: {ok}/{wave_size} ok bad={bad}", flush=True)
            await asyncio.sleep(0.3)


asyncio.run(main())
