"""Live check: background worker ids must not consume Adaptive replica slots.

Sends two background upserts (workers 16 and 17) for one prefix, then three
routable upserts (workers 0, 1, and 2). Workers 0 and 1 must arrive. Worker 2
must still be dropped by the replica cap of 2. Rebuilds the gateway image and
recreates the fixed-capacity gateway before sending.
"""
from __future__ import annotations

import asyncio
import subprocess
import time

from experiments.icc_kv.runtime import NET, PathRuntime, prepare_fixed_gateway
from experiments.icc_kv.wire import K_UP


DIGEST = 0x5A17
CELL = 1


def rebuild_image() -> None:
    subprocess.check_call(
        ["docker", "build", "-t", "b02-gw4t4", "-f", str(NET / "Dockerfile"), str(NET)],
    )


async def main() -> None:
    rebuild_image()
    prepare_fixed_gateway()
    runtime = PathRuntime()
    await runtime.start()
    try:
        await runtime.configure(CELL, "Adaptive", 0.0)
        for seq, worker in enumerate((16, 17, 0, 1, 2), start=1):
            await runtime.send_event(K_UP, worker, CELL, seq, 2048, DIGEST, time.time())
        await runtime.drain_agent()
        deadline = time.time() + 3.0
        while time.time() < deadline:
            got = {worker for worker in (16, 17, 0, 1, 2) if runtime.dispatcher.tested.get((worker, DIGEST)) == 2048}
            if {16, 17, 0, 1} <= got:
                break
            await asyncio.sleep(0.05)
        got = {worker for worker in (16, 17, 0, 1, 2) if runtime.dispatcher.tested.get((worker, DIGEST)) == 2048}
        ok = got == {16, 17, 0, 1}
        print({"arrived": sorted(got), "pass": ok}, flush=True)
        if not ok:
            raise SystemExit(1)
    finally:
        await runtime.stop()


if __name__ == "__main__":
    asyncio.run(main())
