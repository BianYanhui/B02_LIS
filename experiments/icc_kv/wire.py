"""64-byte state frames shared with the frozen 4×T4 gateway relay."""
from __future__ import annotations

import struct
import time

FRAME = 64
HDR = struct.Struct(">BBHIqQd")
CFG = struct.Struct(">BBBBHIIIII")
K_UP, K_TOMB, K_RESET, K_STATS_REQ, K_CONFIG, K_ACK, K_STATS, K_RESET_DONE = 1, 2, 3, 4, 5, 6, 7, 8
WIRE_BYTES = 104

# mode, merge, priority, adaptive, dedup, global_topk
POLICIES: dict[str, tuple[int, int, int, int, int, int]] = {
    "FullSync": (0, 0, 0, 0, 0, 0),
    "RateFIFO": (1, 0, 0, 0, 0, 0),
    "StaticSemantic": (4, 1, 1, 0, 2, 0),
    "Adaptive": (5, 1, 1, 1, 2, 16),
    "AdaptiveNoPriority": (5, 1, 0, 1, 2, 16),
    "Ideal": (0, 0, 0, 0, 0, 0),
}


def frame(kind: int, instance: int, cell: int, seq: int, coverage: int, digest: int, t: float, payload: bytes = b"") -> bytes:
    return HDR.pack(kind, instance & 0xFF, cell & 0xFFFF, seq & 0xFFFFFFFF, coverage, digest, t) + payload.ljust(32, b"\x00")[:32]


def config_frame(cell: int, policy: str, *, max_queue: int, max_inflight: int, rate_frames_per_s: float, rate_burst: int) -> bytes:
    mode, merge, priority, adaptive, dedup, topk = POLICIES[policy]
    payload = CFG.pack(
        mode, merge, priority, adaptive, dedup, topk,
        max_queue, max_inflight, int(rate_frames_per_s * 1000), rate_burst,
    )
    return frame(K_CONFIG, 0, cell, 0, 0, 0, time.time(), payload)
