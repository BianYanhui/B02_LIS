# Frozen 4×T4 baseline experiment

This directory is an isolated extension of `shared_link_exp/live_v3`. It does
not modify the legacy harness, Docker network, containers, vLLM logs, or
results. The state channel remains real host-agent TCP → Docker gateway →
Linux HTB/bfifo → dispatcher TCP; only the 4T4 containers and ports are new.

## Policies

| Policy | Exact behavior |
|---|---|
| `FullSync` | Every state event remains in FIFO. No replacement, deduplication, priority, or congestion admission. |
| `RateFIFO` | FIFO plus only a token bucket at the same physical `B_s(t)` as Adaptive. It has no KV semantics. Calibration sweeps bursts `1,4,16`; the frozen choice is recorded in the manifest. |
| `LatestOnly` | For the same `(owner,prefix)`, a new unsent upsert replaces the older one. Tombstones retain FIFO semantics. |
| `AgeCov-Greedy` | At each release, choose the pending update with `age_s * max(coverage_tokens,1) / 64`. No tombstone priority, deduplication, or adaptive gate. |
| `StaticSemantic` | Latest-update replacement, non-preemptive tombstone priority, and a two-owner cross-instance replica cap. |
| `Adaptive` | StaticSemantic plus EWMA delivery-delay/queue-aware utility admission and a dynamically tightened global useful-prefix set. |
| `Ideal` | Immediate dispatcher visibility without signaling cost; upper bound only. |

All non-Ideal policies run over the same HTB parent rate for a cell:
`B_s = measured_offered_state_rate / rho`. Fairness is therefore the physical
capacity rather than an equalized final frame count.

## Start and verify the platform

```bash
cd /home/byh/B02
experiments/4t4/restart_4t4.sh 0.40 6144
experiments/4t4/net/setup_net_4t4.sh --rebuild
poc/.venv/bin/python experiments/4t4/test_policies.py \
  --output analysis/formal4t4/summary/policy_unit_checks.csv
poc/.venv/bin/python experiments/4t4/run_formal4t4.py --smoke \
  --tag smoke_manual --seed 2026072602 --workload reuse_intensive \
  --kv-cache-tokens 104544 --out-dir analysis/formal4t4
```

`restart_4t4.sh` starts exactly one Qwen2.5-1.5B-Instruct vLLM per GPU on
ports 8000–8003. It only terminates PID files created by an earlier 4T4 run
and refuses occupied ports. The smoke includes Ideal solely for measured
offered-load calibration, then executes FullSync and Adaptive at the same rho.

## Calibration and formal execution

```bash
experiments/4t4/run_calibration_4t4.sh

# After inspecting calibration outputs and choosing the predeclared RateFIFO
# rule, create exactly one manifest. It refuses overwrite.
poc/.venv/bin/python experiments/4t4/freeze_manifest.py \
  --output analysis/formal4t4/experiment_manifest.json \
  --rate-burst-frames 4 --kv-cache-tokens 104544

experiments/4t4/run_formal_baselines_4t4.sh \
  analysis/formal4t4/experiment_manifest.json
```

`run_formal_baselines_4t4.sh` executes both frozen workloads, all six real
baselines, `rho={0.5,0.8,1.0,1.2}`, and five paired repetitions. It uses 120
requests plus 24 warm-up requests per cell and concurrency four. The
`original_compatible` workload retains the legacy 2048+extension lineage;
`reuse_intensive` uses a 64-prefix Zipf pool with 1024/2048/4096 prefix mix
35%/40%/25% and physical replica overlap 25%.

Dynamic low→high→low runs are invoked per policy:

```bash
experiments/4t4/run_dynamic_4t4.sh Adaptive dynamic_adaptive_r0 2026072700 \
  analysis/formal4t4/experiment_manifest.json
```

## Outputs and automatic checks

`analysis/formal4t4/` contains `raw/`, `summary/`, `figures/`, and `report/`.
The harness writes per-request telemetry, dispatcher arrival events, and
gateway enqueue/suppress/forward events. Gateway suppression reasons are
recorded as `rate_limit`, `superseded`, `duplicate_holder`, `low_utility`,
`expired`, or `queue_drop` (the latter two are explicit zero-count categories
unless activated by a future bounded-queue/expiry configuration).

Every run fails if any request errors, usage telemetry is missing, cached
tokens exceed prompt tokens, timestamp-derived age is negative, a selected
owner ID is invalid, any of the four instances receives no measured request,
generated state frames do not cover delivered frames, or HTB carries no
signaling bytes. Existing smoke/calibration data are marked **NOT FOR PAPER**;
only output referencing `experiment_manifest.json` is eligible for aggregation.
