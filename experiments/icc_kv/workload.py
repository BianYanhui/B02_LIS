"""Coverage mixes for the semantic top-k workload.

Empty strings keep the historical workload: every useful prefix is 4096 tokens
and every noise frame is 256. A mix is ``coverage:weight`` pairs. Weights are
reduced by their gcd and then round-robin interleaved, so a slot or a noise
frame walks the classes instead of taking a long run of one class.
"""
from __future__ import annotations

from math import gcd


def parse_mix(text: str) -> list[tuple[int, int]]:
    items: list[tuple[int, int]] = []
    for raw in text.split(","):
        piece = raw.strip()
        if not piece:
            continue
        coverage_s, weight_s = piece.split(":")
        coverage, weight = int(coverage_s), int(weight_s)
        if coverage < 0 or weight <= 0:
            raise ValueError(f"bad mix term {piece!r}")
        items.append((coverage, weight))
    return items


def interleave(text: str) -> list[int]:
    items = parse_mix(text)
    if not items:
        return []
    divisor = 0
    for _coverage, weight in items:
        divisor = gcd(divisor, weight)
    counts = [weight // divisor for _coverage, weight in items]
    table: list[int] = []
    while any(counts):
        for index, left in enumerate(counts):
            if left:
                table.append(items[index][0])
                counts[index] -= 1
    return table


def useful_slots(pool: int, mix: str, default: int = 4096) -> list[int]:
    if pool < 1:
        raise ValueError("useful pool must be positive")
    if not mix.strip():
        return [default] * pool
    table = interleave(mix)
    if min(table) < 1024:
        raise ValueError("useful coverage must stay at or above the 1024-token admission bar")
    return [table[slot % len(table)] for slot in range(pool)]


def noise_overlap(noise_mix: str, useful: list[int]) -> float:
    table = interleave(noise_mix) if noise_mix.strip() else [256]
    if not table or not useful:
        return 0.0
    bar = min(useful)
    return sum(coverage >= bar for coverage in table) / len(table)


def burst_nominal_rho(base: float, burst_mult: float) -> float:
    """Mean offered rho of a 30 s cycle that is base for 25 s and base*mult for 5 s."""
    return base * (25.0 + 5.0 * burst_mult) / 30.0
