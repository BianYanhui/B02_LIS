#!/usr/bin/env python3
"""
shared_link_sim.py — Discrete-event simulator for *network-aware semantic
state propagation over a capacity-limited shared control link*.

Research narrative
------------------
LLM serving instances continuously advertise KV-prefix affinity state
(create / extend / evict / restart events) to a central Dispatcher.  All
reports contend on ONE shared, capacity-limited FIFO byte-server (the control
link).  Queueing on that link makes state stale on arrival, and stale state
degrades KV-reuse dispatch decisions.  An aggregator placed *before* the
bottleneck performs cross-instance dedup, merging of superseded updates and
dynamic value/freshness/congestion-aware Top-K selection.  The thesis this
simulator is built to demonstrate:

    under a bounded shared signaling channel, fewer-but-fresher updates can
    beat a complete-but-stale state view.

System model
------------
* ``N`` instances, each holding up to ``R`` resident prefix resources.
  A resource is ``(digest, coverage_tokens)``; digests follow a global Zipf
  popularity distribution over ``V`` distinct prefixes (exponent ``alpha``).
* Ground-truth churn per instance: Poisson event stream at ``churn_rate``
  events/s.  Mix: create 45% / extend 35% / evict 19% / restart 1%.
  Popular digests are created more often (Zipf sampling); a create that hits
  an already-resident digest becomes an extend, so hot digests are also
  extended more often.  A restart invalidates all resources of the instance
  and bumps its epoch.
* Optional synchronized burst phase: every ``burst_period`` s the churn rate
  is multiplied by ``burst_mult`` for ``burst_len`` s, and (if
  ``burst_evict_frac`` > 0) a correlated mass eviction removes that fraction
  of every instance's resources at burst start.
* Every update message is ``msg_bytes`` (64 B, the paper's entry schema);
  tombstones (evict) and epoch-invalidate (restart) messages are also 64 B.
* The shared link is a single FIFO byte-server with capacity ``B`` bytes/s
  (serialization delay only).  Queueing delay, delivery time and queue length
  are measured.  ``ideal_exact`` uses infinite capacity (zero queueing).
* The Dispatcher index maps ``digest -> {instance: (coverage, seq, gen_time,
  epoch)}`` and applies messages on arrival (upsert / tombstone /
  epoch-invalidate).  An index entry's *state age* at time ``t`` is
  ``t - gen_time`` of the message that produced it.
* Dispatch requests arrive as a Poisson stream at ``req_rate``; each targets
  a Zipf-sampled digest.  The dispatcher picks the advertised replica with
  max coverage.  The hit is *valid* if that instance still truly holds the
  digest (ground truth check, epoch must match); then
  ``saved_prefill += min(advertised, true coverage)``.  Otherwise it is a
  *stale fallback* (saved 0).  No index entry at all => cold miss.

Policies (``--policy``)
-----------------------
1. ``ideal_exact``  — infinite link capacity; upper bound.
2. ``exact_fifo``   — every event enqueued, no filtering, no merging.
3. ``local_topk``   — each instance only emits events for its current top-``K``
   resources by coverage; tombstones for previously advertised resources and
   epoch-invalidate messages are always emitted.
4. ``agg_static``   — pre-link aggregator: (a) merge superseded (a queued,
   unsent older event for the same (instance, resource) key is cancelled when
   a newer one is generated); (b) cross-instance dedup capping advertised
   replicas per digest at ``max_replicas``, preferring largest coverage (an
   admitted larger-coverage replica displaces the smallest one via an
   aggregator-generated withdraw-tombstone); (c) two-lane priority:
   tombstones/epoch messages jump the high-priority lane, served
   non-preemptively before the normal lane.
5. ``agg_adaptive`` — ``agg_static`` plus a dynamic global budget: the
   aggregator keeps an EWMA ``Dq`` of recent queueing delays and scores each
   upsert ``U = P_valid(age + Dq) * coverage_value - lambda * size`` with
   ``P_valid = exp(-(age + Dq)/tau)`` (``tau`` = EWMA of observed resource
   lifetimes, init ``tau_init``), ``coverage_value = coverage/8192``,
   ``size = msg_bytes/64``, ``lambda = lambda0 * Dq/dq_ref``.  Upserts with
   ``U <= theta * Dq/dq_target`` are dropped (tombstones always pass).
   While ``Dq > dq_target`` the replica cap tightens to 1; it relaxes back to
   ``max_replicas`` when congestion clears.

Metrics
-------
Per repetition, after a ``warmup`` prefix excluded from all metrics: offered
load (B/s), transmitted signaling bytes, link utilization, queue length
(max/p95), queueing delay (mean/p50/p95), state-age-at-delivery (p50/p95),
tombstone delivery delay (p95), age-at-use (p95), valid-hit / cold-miss /
stale-fallback rates, saved prefill tokens (total and per-request mean), and
aggregator drop/merge/withdraw counts.  Across repetitions we report the mean
and a two-sided 95% t-confidence interval (Student t with n-1 dof).

Simplifications (documented for the paper)
------------------------------------------
* Extend/evict pick a uniformly random resident resource; the Zipf bias on
  creates (create-on-resident => extend) already makes hot digests dominate
  created/extended traffic.
* When an instance is at capacity, a create evicts a uniformly random victim
  (stands in for an LRU-style eviction).
* Aggregation is zero-cost and instantaneous; all queueing happens in the
  link queue(s).
* The link is non-preemptive; the high-priority lane is served first whenever
  both lanes are non-empty.
* The aggregator's replica view is its own admission state (it sees every
  event, so this is exact with respect to what it has admitted).

Usage
-----
    python3 shared_link_sim.py --experiment {e1,e2,e3,all} [overrides]

Deterministic given ``--seed``: repetition ``i`` of every cell uses seed
``seed + i`` (common random numbers across cells => paired comparison).
Only the Python standard library is used.
"""

from __future__ import annotations

import argparse
import bisect
import csv
import heapq
import json
import math
import random
import statistics
import time as _time
from collections import deque
from dataclasses import dataclass, replace
from pathlib import Path

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #
KIB = 1024.0
COV_MIN, COV_MAX_INIT = 512, 8192      # initial coverage_tokens ~ U[512, 8192]
COV_CAP = 65536                        # hard cap on coverage growth
EXT_MIN, EXT_MAX = 512, 2048           # coverage increment on extend

# heap event kinds
EV_CHURN, EV_PHASE_START, EV_PHASE_END, EV_REQUEST, EV_TX_DONE, EV_WARMUP = range(6)

# message kinds
M_UPSERT, M_TOMBSTONE, M_EPOCH = "upsert", "tombstone", "epoch"

# churn event-type mix: create / extend / evict / restart
MIX_CREATE, MIX_EXTEND, MIX_EVICT = 0.45, 0.35, 0.19  # restart = 0.01

POLICIES = ("ideal_exact", "exact_fifo", "local_topk", "agg_static", "agg_adaptive")

# default experiment grids
E1_NS = (16, 32, 64, 128, 256)
E2_BS_KIB = (8, 16, 32, 64, 128, 256)
E3_BS_KIB = (16, 32, 64)

# two-sided 95% Student-t critical values (df -> t)
_T95 = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447,
        7: 2.365, 8: 2.306, 9: 2.262, 10: 2.228, 11: 2.201, 12: 2.179,
        13: 2.160, 14: 2.145, 15: 2.131, 16: 2.120, 17: 2.110, 18: 2.101,
        19: 2.093, 20: 2.086, 25: 2.060, 30: 2.042}


def t_crit_95(df: int) -> float:
    """Conservative two-sided 95% t critical value."""
    if df <= 0:
        return 0.0
    if df in _T95:
        return _T95[df]
    if df > 30:
        return 1.96
    smaller = [k for k in _T95 if k < df]
    return _T95[max(smaller)] if smaller else 1.96


def percentile(data, q: float):
    """Linear-interpolation percentile (numpy 'linear' method). None if empty."""
    if not data:
        return None
    xs = sorted(data)
    if len(xs) == 1:
        return float(xs[0])
    pos = (len(xs) - 1) * q / 100.0
    lo = int(pos)
    hi = min(lo + 1, len(xs) - 1)
    frac = pos - lo
    return xs[lo] * (1.0 - frac) + xs[hi] * frac


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
@dataclass
class Config:
    # topology / workload
    N: int = 128                 # serving instances
    R: int = 64                  # resident resources per instance
    V: int = 4096                # distinct prefixes
    alpha: float = 0.55          # Zipf exponent
    churn_rate: float = 0.5      # ground-truth events/s per instance
    req_rate: float = 20.0       # dispatch requests/s
    # bursts
    bursts: bool = True
    burst_period: float = 60.0
    burst_len: float = 5.0
    burst_mult: float = 8.0
    burst_evict_frac: float = 0.25   # correlated mass eviction at burst start (0=off)
    # link
    B: float = 64 * KIB          # shared-link capacity, bytes/s
    msg_bytes: int = 64
    # policy knobs
    policy: str = "exact_fifo"
    K: int = 8                   # local_topk static K
    max_replicas: int = 2        # aggregator replica cap per digest
    lambda0: float = 0.25        # base congestion price
    dq_ref: float = 0.05         # reference queueing delay for lambda, s
    dq_target: float = 0.05      # congestion target, s
    theta: float = 0.05          # utility-threshold scale when congested
    tau_init: float = 30.0       # initial mean resource lifetime estimate, s
    # run control
    duration: float = 120.0      # measurement window, s
    warmup: float = 20.0         # warmup prefix excluded from metrics, s
    reps: int = 10
    seed: int = 1

    @property
    def total_time(self) -> float:
        return self.warmup + self.duration


# --------------------------------------------------------------------------- #
# Zipf popularity (CDF cached per (V, alpha))
# --------------------------------------------------------------------------- #
_ZIPF_CACHE: dict = {}


def zipf_cdf(V: int, alpha: float):
    key = (V, alpha)
    cdf = _ZIPF_CACHE.get(key)
    if cdf is None:
        cdf = []
        acc = 0.0
        for i in range(1, V + 1):
            acc += 1.0 / (i ** alpha)
            cdf.append(acc)
        _ZIPF_CACHE[key] = cdf
    return cdf


# --------------------------------------------------------------------------- #
# Messages and instances
# --------------------------------------------------------------------------- #
class Msg:
    """One signaling message on the shared link."""

    __slots__ = ("inst", "digest", "coverage", "seq", "epoch", "kind",
                 "gen", "enq", "size", "canceled")

    def __init__(self, kind, inst, digest, coverage, seq, epoch, gen, size):
        self.kind = kind            # M_UPSERT | M_TOMBSTONE | M_EPOCH
        self.inst = inst
        self.digest = digest        # -1 for epoch messages
        self.coverage = coverage
        self.seq = seq
        self.epoch = epoch
        self.gen = gen              # generation (ground-truth event) time
        self.enq = -1.0             # enqueue time into the link queue
        self.size = size
        self.canceled = False       # lazy deletion for merge-superseded


class Instance:
    """Ground truth of one serving instance."""

    __slots__ = ("iid", "res", "res_keys", "pos", "epoch")

    def __init__(self, iid: int):
        self.iid = iid
        self.res = {}        # digest -> [coverage, seq, born_time]
        self.res_keys = []   # parallel list for O(1) uniform random choice
        self.pos = {}        # digest -> index in res_keys
        self.epoch = 0

    def add(self, digest, coverage, now):
        self.res[digest] = [coverage, 1, now]
        self.pos[digest] = len(self.res_keys)
        self.res_keys.append(digest)

    def remove(self, digest):
        rec = self.res.pop(digest)
        idx = self.pos.pop(digest)
        last = self.res_keys.pop()
        if idx < len(self.res_keys):
            self.res_keys[idx] = last
            self.pos[last] = idx
        return rec


# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #
class Metrics:
    """Per-repetition metric accumulators.  ``reset`` is called at warmup end;
    every recorder is a no-op while ``active`` is False."""

    def __init__(self, active: bool):
        self.active = active
        self.reset()

    def reset(self):
        self.offered_bytes = 0
        self.offered_msgs = 0
        self.tx_bytes = 0
        self.tx_msgs = 0
        self.busy_time = 0.0
        self.q_samples = []      # queue length (waiting messages) at each change
        self.q_max = 0
        self.qdelays = []        # per-message queueing delay
        self.deliv_age = []      # state age at delivery (delivery - generation)
        self.tomb_delay = []     # delivery delay of tombstone/epoch messages
        self.reqs = 0
        self.hits = 0
        self.fb = 0
        self.cold = 0
        self.saved = 0.0
        self.age_use = []        # age of index entry used per non-cold decision
        self.dropped = 0         # aggregator/utility drops
        self.merged = 0          # superseded-queue merges
        self.withdrawn = 0       # aggregator-generated replica withdrawals
        self.filtered = 0        # local_topk non-emitted events

    # -- recorders -------------------------------------------------------- #
    def rec_offered(self, size):
        if self.active:
            self.offered_bytes += size
            self.offered_msgs += 1

    def rec_q(self, q):
        if self.active:
            self.q_samples.append(q)
            if q > self.q_max:
                self.q_max = q

    def rec_tx_start(self, msg, qdelay, tx_time):
        if self.active:
            self.qdelays.append(qdelay)
            self.tx_bytes += msg.size
            self.tx_msgs += 1
            self.busy_time += tx_time

    def rec_delivery(self, msg, now):
        if self.active:
            self.deliv_age.append(now - msg.gen)
            if msg.kind != M_UPSERT:
                self.tomb_delay.append(now - msg.gen)

    def rec_request_cold(self):
        if self.active:
            self.reqs += 1
            self.cold += 1

    def rec_request_hit(self, saved, age_use):
        if self.active:
            self.reqs += 1
            self.hits += 1
            self.saved += saved
            self.age_use.append(age_use)

    def rec_request_fallback(self, age_use):
        if self.active:
            self.reqs += 1
            self.fb += 1
            self.age_use.append(age_use)

    # -- final per-rep summary -------------------------------------------- #
    def finalize(self, span: float) -> dict:
        qdel = self.qdelays
        reqs = self.reqs
        return {
            "offered_Bps": self.offered_bytes / span,
            "offered_msgs": self.offered_msgs,
            "tx_bytes": self.tx_bytes,
            "tx_msgs": self.tx_msgs,
            "utilization": self.busy_time / span,
            "q_max": self.q_max,
            "q_p95": percentile(self.q_samples, 95) or 0.0,
            "qdelay_mean": statistics.fmean(qdel) if qdel else 0.0,
            "qdelay_p50": percentile(qdel, 50) or 0.0,
            "qdelay_p95": percentile(qdel, 95) or 0.0,
            "age_delivery_p50": percentile(self.deliv_age, 50) or 0.0,
            "age_delivery_p95": percentile(self.deliv_age, 95) or 0.0,
            "tombstone_delay_p95": percentile(self.tomb_delay, 95) or 0.0,
            "requests": reqs,
            "valid_hits": self.hits,
            "fallbacks": self.fb,
            "cold_misses": self.cold,
            "valid_hit_rate": (self.hits / reqs) if reqs else None,
            "cold_miss_rate": (self.cold / reqs) if reqs else None,
            "stale_fallback_rate": (self.fb / (self.hits + self.fb))
                                   if (self.hits + self.fb) else 0.0,
            "saved_prefill_total": self.saved,
            "saved_prefill_mean": (self.saved / reqs) if reqs else None,
            "age_use_p95": percentile(self.age_use, 95) or 0.0,
            "msgs_dropped": self.dropped,
            "msgs_merged": self.merged,
            "msgs_withdrawn": self.withdrawn,
            "msgs_filtered": self.filtered,
        }


# --------------------------------------------------------------------------- #
# Shared link (single FIFO byte-server, two-lane strict priority)
# --------------------------------------------------------------------------- #
class Link:
    """Capacity-``B`` byte-server.  High lane (tombstones/epochs) is served
    strictly before the normal lane, non-preemptively.  Cancelled messages
    are skipped lazily when they reach the head of their lane."""

    def __init__(self, sim, B: float):
        self.sim = sim
        self.B = B                     # math.inf for ideal_exact
        self.high = deque()
        self.normal = deque()
        self.in_service = False

    def enqueue(self, msg: Msg, lane: str, now: float):
        msg.enq = now
        # queue length = messages WAITING (not the one about to enter service):
        # sample the backlog the new arrival sees, before it is appended.
        self.sim.metrics.rec_q(len(self.high) + len(self.normal))
        (self.high if lane == "high" else self.normal).append(msg)
        if not self.in_service:
            self._serve_next(now)

    def _serve_next(self, now: float):
        msg = None
        while self.high:
            m = self.high.popleft()
            if not m.canceled:
                msg = m
                break
        while msg is None and self.normal:
            m = self.normal.popleft()
            if not m.canceled:
                msg = m
                break
        self.sim.metrics.rec_q(len(self.high) + len(self.normal))
        if msg is None:
            self.in_service = False
            return
        self.in_service = True
        qdelay = now - msg.enq
        tx = msg.size / self.B           # 0.0 when B is infinite
        self.sim.on_tx_start(msg, qdelay, tx)
        self.sim.push(now + tx, EV_TX_DONE, msg)

    def on_tx_done(self, msg: Msg, now: float):
        self.sim.deliver(msg, now)
        self._serve_next(now)


# --------------------------------------------------------------------------- #
# Policies
# --------------------------------------------------------------------------- #
class BasePolicy:
    """Interface: the simulator hands every ground-truth event to ``submit``;
    the policy decides what reaches the link."""

    def __init__(self, sim):
        self.sim = sim

    def submit(self, kind, inst, digest, cov, seq, epoch, now):
        raise NotImplementedError

    def tx_started(self, msg: Msg):
        pass

    def observe_qdelay(self, qd: float):
        pass

    def observe_lifetime(self, lt: float):
        pass


class ExactPolicy(BasePolicy):
    """ideal_exact / exact_fifo: forward everything, normal lane."""

    def submit(self, kind, inst, digest, cov, seq, epoch, now):
        m = Msg(kind, inst, digest, cov, seq, epoch, now, self.sim.cfg.msg_bytes)
        self.sim.link.enqueue(m, "normal", now)


class LocalTopKPolicy(BasePolicy):
    """Emit events only for the instance's current top-K resources by
    coverage; tombstones for previously advertised resources and
    epoch-invalidate messages always pass."""

    def __init__(self, sim):
        super().__init__(sim)
        self.advertised = [set() for _ in range(sim.cfg.N)]

    def submit(self, kind, inst, digest, cov, seq, epoch, now):
        sim = self.sim
        if kind == M_EPOCH:
            self.advertised[inst].clear()
            sim.link.enqueue(Msg(kind, inst, digest, cov, seq, epoch, now,
                                 sim.cfg.msg_bytes), "normal", now)
            return
        if kind == M_TOMBSTONE:
            if digest in self.advertised[inst]:
                self.advertised[inst].discard(digest)
                sim.link.enqueue(Msg(kind, inst, digest, cov, seq, epoch, now,
                                     sim.cfg.msg_bytes), "normal", now)
            else:
                sim.metrics.filtered += 1 if sim.metrics.active else 0
            return
        # upsert: emit only if the resource ranks in the instance's top-K
        I = sim.instances[inst]
        mycov = I.res[digest][0]
        rank = 0
        for k in I.res_keys:
            if I.res[k][0] > mycov:
                rank += 1
                if rank >= sim.cfg.K:
                    break
        if rank < sim.cfg.K:
            self.advertised[inst].add(digest)
            sim.link.enqueue(Msg(kind, inst, digest, cov, seq, epoch, now,
                                 sim.cfg.msg_bytes), "normal", now)
        else:
            sim.metrics.filtered += 1 if sim.metrics.active else 0


class AggStaticPolicy(BasePolicy):
    """Pre-link aggregator: merge-superseded + cross-instance replica dedup
    (cap ``max_replicas``, prefer largest coverage, displace-by-withdraw) +
    strict-priority lane for tombstones/epochs."""

    def __init__(self, sim):
        super().__init__(sim)
        self.pending = {}                          # (inst, digest) -> queued Msg (upserts)
        self.adm = {}                              # digest -> {inst: coverage} admitted view
        self.adm_by_inst = [set() for _ in range(sim.cfg.N)]

    # -- hooks ------------------------------------------------------------ #
    def tx_started(self, msg: Msg):
        if msg.kind == M_UPSERT:
            key = (msg.inst, msg.digest)
            if self.pending.get(key) is msg:
                del self.pending[key]

    def replicas_eff(self) -> int:
        return self.sim.cfg.max_replicas

    def utility_ok(self, cov, now, gen) -> bool:
        return True                       # static policy admits everything

    # -- main entry ------------------------------------------------------- #
    def submit(self, kind, inst, digest, cov, seq, epoch, now):
        sim = self.sim
        size = sim.cfg.msg_bytes
        if kind == M_UPSERT:
            # (adaptive) utility gate first: keep any older pending update
            if not self.utility_ok(cov, now, now):
                if sim.metrics.active:
                    sim.metrics.dropped += 1
                return
            # cross-instance dedup on the admitted replica view
            s = self.adm.get(digest)
            if s is None:
                s = self.adm[digest] = {}
            if inst in s:
                s[inst] = cov
            else:
                cap = self.replicas_eff()
                if len(s) < cap:
                    s[inst] = cov
                    self.adm_by_inst[inst].add(digest)
                else:
                    min_inst = min(s, key=lambda i: (s[i], i))
                    if cov > s[min_inst]:
                        # displace the smallest replica: withdraw it explicitly
                        w = Msg(M_TOMBSTONE, min_inst, digest, 0, 0,
                                sim.instances[min_inst].epoch, now, size)
                        sim.metrics.rec_offered(size)
                        sim.link.enqueue(w, "high", now)
                        if sim.metrics.active:
                            sim.metrics.withdrawn += 1
                        del s[min_inst]
                        self.adm_by_inst[min_inst].discard(digest)
                        s[inst] = cov
                        self.adm_by_inst[inst].add(digest)
                    else:
                        if sim.metrics.active:
                            sim.metrics.dropped += 1
                        return
            # merge superseded: cancel a queued, unsent older update
            key = (inst, digest)
            old = self.pending.pop(key, None)
            if old is not None:
                old.canceled = True
                if sim.metrics.active:
                    sim.metrics.merged += 1
            m = Msg(M_UPSERT, inst, digest, cov, seq, epoch, now, size)
            self.pending[key] = m
            sim.link.enqueue(m, "normal", now)
        elif kind == M_TOMBSTONE:
            old = self.pending.pop((inst, digest), None)
            if old is not None:
                old.canceled = True
                if sim.metrics.active:
                    sim.metrics.merged += 1
            s = self.adm.get(digest)
            if s is not None and inst in s:
                del s[inst]
                self.adm_by_inst[inst].discard(digest)
                if not s:
                    del self.adm[digest]
            sim.link.enqueue(Msg(M_TOMBSTONE, inst, digest, cov, seq, epoch,
                                 now, size), "high", now)
        else:  # epoch invalidate
            for key, m in list(self.pending.items()):
                if key[0] == inst:
                    m.canceled = True
                    del self.pending[key]
                    if sim.metrics.active:
                        sim.metrics.merged += 1
            for dg in self.adm_by_inst[inst]:
                s = self.adm.get(dg)
                if s is not None and inst in s:
                    del s[inst]
                    if not s:
                        del self.adm[dg]
            self.adm_by_inst[inst].clear()
            sim.link.enqueue(Msg(M_EPOCH, inst, -1, 0, 0, epoch, now, size),
                             "high", now)


class AggAdaptivePolicy(AggStaticPolicy):
    """agg_static + congestion/freshness-aware utility gate and a dynamic
    replica budget driven by an EWMA of observed queueing delay."""

    DQ_EWMA = 0.2        # weight of the newest queueing-delay sample
    TAU_EWMA = 0.1       # weight of the newest lifetime observation

    def __init__(self, sim):
        super().__init__(sim)
        self.dq = 0.0                      # EWMA of queueing delay
        self.tau = sim.cfg.tau_init        # EWMA of resource lifetime

    def observe_qdelay(self, qd: float):
        self.dq += self.DQ_EWMA * (qd - self.dq)

    def observe_lifetime(self, lt: float):
        self.tau += self.TAU_EWMA * (lt - self.tau)

    def replicas_eff(self) -> int:
        cfg = self.sim.cfg
        return 1 if self.dq > cfg.dq_target else cfg.max_replicas

    def utility_ok(self, cov, now, gen) -> bool:
        cfg = self.sim.cfg
        p_valid = math.exp(-((now - gen) + self.dq) / max(self.tau, 1e-9))
        lam = cfg.lambda0 * (self.dq / cfg.dq_ref)
        threshold = cfg.theta * (self.dq / cfg.dq_target)
        u = p_valid * (cov / 8192.0) - lam * (cfg.msg_bytes / 64.0) - threshold
        return u > 0.0


def make_policy(sim, name: str) -> BasePolicy:
    if name in ("ideal_exact", "exact_fifo"):
        return ExactPolicy(sim)
    if name == "local_topk":
        return LocalTopKPolicy(sim)
    if name == "agg_static":
        return AggStaticPolicy(sim)
    if name == "agg_adaptive":
        return AggAdaptivePolicy(sim)
    raise ValueError(f"unknown policy {name!r}")


# --------------------------------------------------------------------------- #
# Simulator
# --------------------------------------------------------------------------- #
class Sim:
    def __init__(self, cfg: Config, seed: int):
        self.cfg = cfg
        self.rng = random.Random(seed)
        self.cdf = zipf_cdf(cfg.V, cfg.alpha)
        self.cdf_total = self.cdf[-1]
        self.instances = [Instance(i) for i in range(cfg.N)]
        self.metrics = Metrics(active=(cfg.warmup <= 0.0))
        self.B = math.inf if cfg.policy == "ideal_exact" else float(cfg.B)
        self.link = Link(self, self.B)
        self.policy = make_policy(self, cfg.policy)
        # dispatcher index
        self.index = {}                                # digest -> {inst: (cov, seq, gen, epoch)}
        self.by_inst = [set() for _ in range(cfg.N)]   # inst -> {digest}
        self.disp_epoch = [0] * cfg.N                  # last epoch seen per instance
        # event queue
        self.heap = []
        self._counter = 0
        # piecewise-constant churn rate with rescaling on phase changes
        self.base_churn_rate = cfg.N * cfg.churn_rate
        self.cur_churn_rate = self.base_churn_rate
        self.next_churn = math.inf

    # -- event-queue helpers ---------------------------------------------- #
    def push(self, t, kind, payload=None):
        heapq.heappush(self.heap, (t, self._counter, kind, payload))
        self._counter += 1

    def zipf_sample(self) -> int:
        return bisect.bisect_left(self.cdf, self.rng.random() * self.cdf_total)

    def gen_msg(self, kind, inst, digest, cov, seq, epoch, now):
        """Every ground-truth event counts toward offered load, then the
        policy decides what actually reaches the link."""
        self.metrics.rec_offered(self.cfg.msg_bytes)
        self.policy.submit(kind, inst, digest, cov, seq, epoch, now)

    # -- ground-truth churn ------------------------------------------------ #
    def schedule_churn(self, now):
        self.next_churn = now + self.rng.expovariate(self.cur_churn_rate)
        self.push(self.next_churn, EV_CHURN)

    def on_churn(self, now):
        if now != self.next_churn:
            return                                     # stale, rescheduled
        self.do_churn(now)
        self.schedule_churn(now)

    def do_churn(self, now):
        cfg = self.cfg
        inst = self.rng.randrange(cfg.N)
        u = self.rng.random()
        if u < MIX_CREATE:
            self.create_or_extend(inst, now)
        elif u < MIX_CREATE + MIX_EXTEND:
            self.extend_random(inst, now)
        elif u < MIX_CREATE + MIX_EXTEND + MIX_EVICT:
            self.evict_random(inst, now)
        else:
            self.restart_instance(inst, now)

    def create_or_extend(self, inst, now):
        cfg = self.cfg
        I = self.instances[inst]
        d = self.zipf_sample()
        r = I.res.get(d)
        if r is not None:                              # create on resident => extend
            r[0] = min(r[0] + self.rng.randint(EXT_MIN, EXT_MAX), COV_CAP)
            r[1] += 1
            self.gen_msg(M_UPSERT, inst, d, r[0], r[1], I.epoch, now)
            return
        if len(I.res) >= cfg.R:                        # at capacity: evict a victim
            self.evict_resource(inst, self.rng.choice(I.res_keys), now)
        cov = self.rng.randint(COV_MIN, COV_MAX_INIT)
        I.add(d, cov, now)
        self.gen_msg(M_UPSERT, inst, d, cov, 1, I.epoch, now)

    def extend_random(self, inst, now):
        I = self.instances[inst]
        if not I.res:
            return
        d = self.rng.choice(I.res_keys)
        r = I.res[d]
        r[0] = min(r[0] + self.rng.randint(EXT_MIN, EXT_MAX), COV_CAP)
        r[1] += 1
        self.gen_msg(M_UPSERT, inst, d, r[0], r[1], I.epoch, now)

    def evict_random(self, inst, now):
        I = self.instances[inst]
        if I.res:
            self.evict_resource(inst, self.rng.choice(I.res_keys), now)

    def evict_resource(self, inst, digest, now):
        I = self.instances[inst]
        rec = I.remove(digest)
        self.policy.observe_lifetime(now - rec[2])
        self.gen_msg(M_TOMBSTONE, inst, digest, 0, rec[1], I.epoch, now)

    def restart_instance(self, inst, now):
        I = self.instances[inst]
        if I.res:
            mean_lt = sum(now - r[2] for r in I.res.values()) / len(I.res)
            self.policy.observe_lifetime(mean_lt)
        I.res.clear()
        I.res_keys.clear()
        I.pos.clear()
        I.epoch += 1
        self.gen_msg(M_EPOCH, inst, -1, 0, 0, I.epoch, now)

    # -- bursts ------------------------------------------------------------- #
    def on_phase(self, now, starting: bool):
        cfg = self.cfg
        old = self.cur_churn_rate
        self.cur_churn_rate = (self.base_churn_rate * cfg.burst_mult if starting
                               else self.base_churn_rate)
        # rescale the pending inter-arrival to keep a piecewise-homogeneous Poisson
        remaining = self.next_churn - now
        self.next_churn = now + remaining * old / self.cur_churn_rate
        self.push(self.next_churn, EV_CHURN)
        if starting and cfg.burst_evict_frac > 0.0:
            self.mass_evict(now)

    def mass_evict(self, now):
        frac = self.cfg.burst_evict_frac
        for inst in range(self.cfg.N):
            I = self.instances[inst]
            k = int(len(I.res_keys) * frac)
            for _ in range(k):
                self.evict_resource(inst, self.rng.choice(I.res_keys), now)

    # -- link callbacks ------------------------------------------------------ #
    def on_tx_start(self, msg: Msg, qdelay: float, tx_time: float):
        self.metrics.rec_tx_start(msg, qdelay, tx_time)
        self.policy.observe_qdelay(qdelay)
        self.policy.tx_started(msg)

    def deliver(self, msg: Msg, now: float):
        """Apply a message to the dispatcher index."""
        self.metrics.rec_delivery(msg, now)
        if msg.kind == M_UPSERT:
            if msg.epoch < self.disp_epoch[msg.inst]:
                return                                   # pre-restart straggler
            d = self.index.get(msg.digest)
            if d is None:
                d = self.index[msg.digest] = {}
            cur = d.get(msg.inst)
            if cur is not None and cur[1] > msg.seq:
                return                                   # stale reorder
            d[msg.inst] = (msg.coverage, msg.seq, msg.gen, msg.epoch)
            self.by_inst[msg.inst].add(msg.digest)
        elif msg.kind == M_TOMBSTONE:
            d = self.index.get(msg.digest)
            if d is not None and msg.inst in d:
                del d[msg.inst]
                self.by_inst[msg.inst].discard(msg.digest)
                if not d:
                    del self.index[msg.digest]
        else:                                            # epoch invalidate
            if msg.epoch > self.disp_epoch[msg.inst]:
                self.disp_epoch[msg.inst] = msg.epoch
            for dg in self.by_inst[msg.inst]:
                d = self.index.get(dg)
                if d is not None and msg.inst in d:
                    del d[msg.inst]
                    if not d:
                        del self.index[dg]
            self.by_inst[msg.inst].clear()

    # -- dispatch requests ---------------------------------------------------- #
    def on_request(self, now):
        cfg = self.cfg
        d = self.zipf_sample()
        cands = self.index.get(d)
        if not cands:
            self.metrics.rec_request_cold()
        else:
            inst, (cov, _seq, gen, epoch) = max(cands.items(),
                                                key=lambda kv: kv[1][0])
            I = self.instances[inst]
            r = I.res.get(d)
            if r is not None and I.epoch == epoch:       # ground-truth validity
                self.metrics.rec_request_hit(min(cov, r[0]), now - gen)
            else:
                self.metrics.rec_request_fallback(now - gen)
        self.push(now + self.rng.expovariate(cfg.req_rate), EV_REQUEST)

    # -- main loop ------------------------------------------------------------- #
    def run(self) -> dict:
        cfg = self.cfg
        T = cfg.total_time
        self.schedule_churn(0.0)
        self.push(self.rng.expovariate(cfg.req_rate), EV_REQUEST)
        if cfg.warmup > 0.0:
            self.push(cfg.warmup, EV_WARMUP)
        if cfg.bursts and cfg.burst_period > 0:
            t = cfg.burst_period
            while t < T:
                self.push(t, EV_PHASE_START)
                self.push(min(t + cfg.burst_len, T), EV_PHASE_END)
                t += cfg.burst_period
        heap = self.heap
        while heap:
            t, _c, kind, payload = heapq.heappop(heap)
            if t > T:
                break
            if kind == EV_CHURN:
                self.on_churn(t)
            elif kind == EV_PHASE_START:
                self.on_phase(t, True)
            elif kind == EV_PHASE_END:
                self.on_phase(t, False)
            elif kind == EV_REQUEST:
                self.on_request(t)
            elif kind == EV_TX_DONE:
                self.link.on_tx_done(payload, t)
            else:                                        # EV_WARMUP
                self.metrics.reset()
                self.metrics.active = True
        return self.metrics.finalize(cfg.duration)


# --------------------------------------------------------------------------- #
# Aggregation across repetitions
# --------------------------------------------------------------------------- #
def run_cell(cfg: Config):
    """Run cfg.reps repetitions (seeds cfg.seed + i) and aggregate."""
    reps = []
    for i in range(cfg.reps):
        reps.append(Sim(cfg, cfg.seed + i).run())
    return reps, aggregate(reps)


def aggregate(reps: list) -> dict:
    """Mean + two-sided 95% t-CI per metric across repetitions."""
    out = {}
    for k in reps[0]:
        vals = [r[k] for r in reps if r[k] is not None]
        if not vals:
            out[k] = {"mean": None, "ci95": None, "n": 0}
            continue
        mean = statistics.fmean(vals)
        if len(vals) > 1:
            sd = statistics.stdev(vals)
            ci = t_crit_95(len(vals) - 1) * sd / math.sqrt(len(vals))
        else:
            ci = 0.0
        out[k] = {"mean": mean, "ci95": ci, "n": len(vals)}
    return out


# --------------------------------------------------------------------------- #
# Experiments
# --------------------------------------------------------------------------- #
def _run_grid(cfg: Config, cells: list):
    """cells: list of (params_dict, cfg_override_dict)."""
    out = []
    for params, over in cells:
        c = replace(cfg, **over)
        t0 = _time.time()
        reps, agg = run_cell(c)
        out.append({"params": params, "aggregate": agg, "reps": reps,
                    "wall_s": round(_time.time() - t0, 3)})
    return out


def run_e1(cfg: Config, ns=E1_NS):
    """E1 scaling: exact_fifo, bursts on, B = 64 KiB/s, sweep N."""
    cells = [({"N": n, "B": int(64 * KIB), "policy": "exact_fifo"},
              {"N": n, "policy": "exact_fifo", "B": 64 * KIB, "bursts": True})
             for n in ns]
    return {"experiment": "e1_scaling",
            "description": "exact_fifo, bursts on, B=64KiB/s, sweep N",
            "cells": _run_grid(cfg, cells)}


def run_e2(cfg: Config, bs_kib=E2_BS_KIB):
    """E2 freshness vs utilization: exact_fifo, N=128, bursts on, sweep B."""
    cells = [({"N": cfg.N, "B": int(b * KIB), "policy": "exact_fifo"},
              {"policy": "exact_fifo", "B": b * KIB, "bursts": True})
             for b in bs_kib]
    return {"experiment": "e2_freshness",
            "description": "exact_fifo, N=128, bursts on, sweep B",
            "cells": _run_grid(cfg, cells)}


def run_e3(cfg: Config, bs_kib=E3_BS_KIB, policies=POLICIES):
    """E3 dispatch quality: all policies x {16,32,64} KiB/s + ideal_exact."""
    cells = []
    for b in bs_kib:
        for pol in policies:
            if pol == "ideal_exact":
                continue                    # ideal is B-independent; added once
            cells.append(({"N": cfg.N, "B": int(b * KIB), "policy": pol},
                          {"policy": pol, "B": b * KIB, "bursts": True}))
    cells.append(({"N": cfg.N, "B": "inf", "policy": "ideal_exact"},
                  {"policy": "ideal_exact", "bursts": True}))
    return {"experiment": "e3_dispatch",
            "description": "all policies x B in {16,32,64}KiB/s + ideal_exact",
            "cells": _run_grid(cfg, cells)}


EXPERIMENTS = {"e1": run_e1, "e2": run_e2, "e3": run_e3}
OUT_NAMES = {"e1": "e1_scaling", "e2": "e2_freshness", "e3": "e3_dispatch"}


# --------------------------------------------------------------------------- #
# Output helpers
# --------------------------------------------------------------------------- #
def flatten_rows(payload: dict) -> list:
    rows = []
    for cell in payload["cells"]:
        row = {"experiment": payload["experiment"]}
        row.update(cell["params"])
        for k, v in cell["aggregate"].items():
            row[f"{k}_mean"] = v["mean"]
            row[f"{k}_ci95"] = v["ci95"]
        rows.append(row)
    return rows


def write_results(outdir: Path, name: str, payload: dict):
    """Write JSON + CSV.  Wall-clock cell timings are console-only (stripped
    here) so that result files are byte-identical for identical seeds."""
    outdir.mkdir(parents=True, exist_ok=True)
    clean = {k: v for k, v in payload.items()}
    clean["cells"] = [{k: v for k, v in cell.items() if k != "wall_s"}
                      for cell in payload["cells"]]
    with open(outdir / f"{name}.json", "w") as f:
        json.dump(clean, f, indent=2)
    rows = flatten_rows(payload)
    if rows:
        fields = list(rows[0].keys())
        with open(outdir / f"{name}.csv", "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerows(rows)


def _m(cell, key):
    v = cell["aggregate"][key]["mean"]
    return v


def build_summary(e1, e2, e3) -> dict:
    """Headline numbers per experiment."""
    s = {}
    # -- E1
    e1_rows = [{
        "N": c["params"]["N"],
        "offered_Bps": round(_m(c, "offered_Bps"), 1),
        "utilization": round(_m(c, "utilization"), 4),
        "q_max": round(_m(c, "q_max"), 1),
        "q_p95": round(_m(c, "q_p95"), 1),
        "qdelay_p95_s": round(_m(c, "qdelay_p95"), 4),
    } for c in e1["cells"]]
    s["e1_scaling"] = {
        "table": e1_rows,
        "note": ("offered load grows ~linearly in N; q_max/q_p95/qdelay_p95 "
                 "mark where burst-phase instantaneous offered load crosses "
                 "link capacity (64 KiB/s)."),
    }
    # -- E2
    e2_rows = [{
        "B_kib": c["params"]["B"] // 1024,
        "utilization": round(_m(c, "utilization"), 4),
        "age_delivery_p50_s": round(_m(c, "age_delivery_p50"), 4),
        "age_delivery_p95_s": round(_m(c, "age_delivery_p95"), 4),
        "age_use_p95_s": round(_m(c, "age_use_p95"), 4),
        "tombstone_delay_p95_s": round(_m(c, "tombstone_delay_p95"), 4),
        "stale_fallback_rate": round(_m(c, "stale_fallback_rate"), 4),
    } for c in e2["cells"]]
    s["e2_freshness"] = {
        "table": e2_rows,
        "note": ("state-age-at-delivery and stale-fallback rate blow up "
                 "non-linearly as link utilization approaches 1."),
    }
    # -- E3
    ideal = next(c for c in e3["cells"] if c["params"]["policy"] == "ideal_exact")
    ideal_saved = _m(ideal, "saved_prefill_total") or 1.0
    by_b = {}
    for c in e3["cells"]:
        b = c["params"]["B"]
        bkey = "ideal" if b == "inf" else f"{b // 1024}KiB"
        saved = _m(c, "saved_prefill_total")
        by_b.setdefault(bkey, {})[c["params"]["policy"]] = {
            "valid_hit_rate": round(_m(c, "valid_hit_rate"), 4),
            "stale_fallback_rate": round(_m(c, "stale_fallback_rate"), 4),
            "cold_miss_rate": round(_m(c, "cold_miss_rate"), 4),
            "saved_prefill_total": round(saved, 1),
            "saved_prefill_retention_vs_ideal": round(saved / ideal_saved, 4),
            "saved_prefill_mean_per_req": round(_m(c, "saved_prefill_mean"), 1),
            "qdelay_p95_s": round(_m(c, "qdelay_p95"), 4),
            "age_use_p95_s": round(_m(c, "age_use_p95"), 4),
            "tx_bytes": int(_m(c, "tx_bytes")),
        }
    headline = {}
    for bkey in ("16KiB", "32KiB", "64KiB"):
        if bkey in by_b and "exact_fifo" in by_b[bkey] and "agg_adaptive" in by_b[bkey]:
            ef = by_b[bkey]["exact_fifo"]
            aa = by_b[bkey]["agg_adaptive"]
            headline[bkey] = (
                f"at B={bkey}/s: p95 age-at-use exact_fifo "
                f"{ef['age_use_p95_s'] * 1e3:.0f} ms vs agg_adaptive "
                f"{aa['age_use_p95_s'] * 1e3:.0f} ms; saved-prefill retention "
                f"vs ideal_exact: exact_fifo "
                f"{ef['saved_prefill_retention_vs_ideal'] * 100:.1f}%, "
                f"agg_adaptive {aa['saved_prefill_retention_vs_ideal'] * 100:.1f}%; "
                f"stale-fallback rate exact_fifo "
                f"{ef['stale_fallback_rate'] * 100:.1f}% vs agg_adaptive "
                f"{aa['stale_fallback_rate'] * 100:.1f}%"
            )
    s["e3_dispatch"] = {
        "by_B": by_b,
        "ideal_exact_saved_prefill_total": round(ideal_saved, 1),
        "headline": headline,
    }
    return s


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Shared-link semantic state propagation simulator "
                    "(E1 scaling / E2 freshness / E3 dispatch quality).")
    p.add_argument("--experiment", choices=("e1", "e2", "e3", "all"),
                   default="all")
    p.add_argument("--N", type=int, default=None, help="instances (default 128)")
    p.add_argument("--B", type=float, default=None,
                   help="link capacity in KiB/s (default 64); ignored for "
                        "ideal_exact and for cells the experiment fixes")
    p.add_argument("--policy", choices=POLICIES, default=None,
                   help="restrict E3 to one policy")
    p.add_argument("--reps", type=int, default=None)
    p.add_argument("--duration", type=float, default=None,
                   help="measurement window in s (default 120)")
    p.add_argument("--warmup", type=float, default=None,
                   help="warmup excluded from metrics in s (default 20)")
    p.add_argument("--seed", type=int, default=None)
    p.add_argument("--outdir", type=str, default="results")
    p.add_argument("--K", type=int, default=None)
    p.add_argument("--max_replicas", type=int, default=None)
    p.add_argument("--churn_rate", type=float, default=None)
    p.add_argument("--req_rate", type=float, default=None)
    p.add_argument("--V", type=int, default=None)
    p.add_argument("--alpha", type=float, default=None)
    p.add_argument("--R", type=int, default=None)
    p.add_argument("--no_burst", action="store_true", help="disable bursts")
    p.add_argument("--burst_period", type=float, default=None)
    p.add_argument("--burst_len", type=float, default=None)
    p.add_argument("--burst_mult", type=float, default=None)
    p.add_argument("--burst_evict_frac", type=float, default=None,
                   help="0 disables the correlated mass eviction")
    p.add_argument("--lambda0", type=float, default=None)
    p.add_argument("--dq_ref", type=float, default=None)
    p.add_argument("--dq_target", type=float, default=None)
    p.add_argument("--tau_init", type=float, default=None)
    p.add_argument("--e1_ns", type=str, default=None,
                   help="comma list, e.g. 16,64,256")
    p.add_argument("--e2_bs", type=str, default=None,
                   help="comma list of KiB/s, e.g. 8,32,256")
    p.add_argument("--e3_bs", type=str, default=None,
                   help="comma list of KiB/s, e.g. 16,64")
    return p


def config_from_args(args) -> Config:
    cfg = Config()
    for k in ("N", "reps", "duration", "warmup", "seed", "K", "max_replicas",
              "churn_rate", "req_rate", "V", "alpha", "R", "burst_period",
              "burst_len", "burst_mult", "burst_evict_frac", "lambda0",
              "dq_ref", "dq_target", "tau_init"):
        v = getattr(args, k, None)
        if v is not None:
            setattr(cfg, k, v)
    if args.B is not None:
        cfg.B = args.B * KIB
    if args.no_burst:
        cfg.bursts = False
    return cfg


def _print_cell(cell):
    a = cell["aggregate"]
    p = cell["params"]
    print(f"  {p} -> util={_m(cell, 'utilization'):.3f} "
          f"q_p95={_m(cell, 'q_p95'):.0f} "
          f"qd_p95={_m(cell, 'qdelay_p95') * 1e3:.1f}ms "
          f"age_use_p95={_m(cell, 'age_use_p95') * 1e3:.1f}ms "
          f"sfr={_m(cell, 'stale_fallback_rate'):.3f} "
          f"saved={_m(cell, 'saved_prefill_total'):.0f} "
          f"({cell['wall_s']}s)")


def main():
    args = build_parser().parse_args()
    cfg = config_from_args(args)
    outdir = Path(args.outdir)
    names = ("e1", "e2", "e3") if args.experiment == "all" else (args.experiment,)
    results = {}
    for name in names:
        t0 = _time.time()
        if name == "e1":
            ns = (tuple(int(x) for x in args.e1_ns.split(","))
                  if args.e1_ns else E1_NS)
            payload = run_e1(cfg, ns)
        elif name == "e2":
            bs = (tuple(float(x) for x in args.e2_bs.split(","))
                  if args.e2_bs else E2_BS_KIB)
            payload = run_e2(cfg, bs)
        else:
            bs = (tuple(float(x) for x in args.e3_bs.split(","))
                  if args.e3_bs else E3_BS_KIB)
            pols = (args.policy,) if args.policy else POLICIES
            payload = run_e3(cfg, bs, pols)
        payload["config"] = {k: (v if v != math.inf else "inf")
                             for k, v in vars(cfg).items()}
        results[name] = payload
        write_results(outdir, OUT_NAMES[name], payload)
        print(f"[{OUT_NAMES[name]}] {len(payload['cells'])} cells, "
              f"{_time.time() - t0:.1f}s wall")
        for cell in payload["cells"]:
            _print_cell(cell)
    if len(results) == 3:
        summary = build_summary(results["e1"], results["e2"], results["e3"])
        outdir.mkdir(parents=True, exist_ok=True)
        with open(outdir / "summary.json", "w") as f:
            json.dump(summary, f, indent=2)
        print("[summary.json] written")
        for bkey, line in summary["e3_dispatch"]["headline"].items():
            print("  " + line)


if __name__ == "__main__":
    main()
