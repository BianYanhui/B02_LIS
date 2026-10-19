# Next iteration

Do not start this. The recommendation is one measurement, not a matrix.

## Verdicts

| Subplot | Verdict | Meaning |
| --- | --- | --- |
| Fig. 6(b) | INCONCLUSIVE | Priority changes a constructed relay-queue decision. No real stale-positive was observed. |
| Fig. 6(d) | PASS | CONTROLLED only. Ads 4→2. Similar load, regret 0. Imbalanced load, derived regret 16 ms. Not a TTFT. |
| Fig. 6(a)(c) | keep existing counters | Gateway superseded and duplicate-holder. No rerun. |

## Answers

**Q1. Is Fig. 6(b) worth a formal experiment now?**

No. The frozen policies bundle priority with merge and the replica cap. The baseline sent 0 tombstones, so the physical false-positive rate of 0 does not test the mechanism. A formal matrix would repeat that hole unless a new run first logs one real residency loss against one still-positive affinity entry.

**Q2. Is Fig. 6(d) worth a formal experiment now?**

No GPU matrix. The efficiency–flexibility trade-off is already computed on one snapshot, and the formal duplicate-holder counts already show the advertisement reduction. Another multi-seed TTFT run would measure a different quantity from routing regret.

**Q3. Which result is closer to a complete mechanism claim?**

Fig. 6(d). The owner set, the retention rule, the advertisement count, and the regret are on the same snapshot. Fig. 6(b) still lacks the real window the causal chain asks for.

**Q4. Did the audit find a definition problem?**

Yes.

- `tombstone_delay_p95_s` is 0 when no tombstone was delivered. Baseline tables look like zero delay.
- Published policies do not isolate priority. Churn compares bundles and injected resets. StaticSemantic is missing from the churn rows.
- `source_view_false_positive` is ShadowCache versus the dispatcher. `physical_false_positive_affinity` is the selected worker’s `vllm_cached_tokens`. Shadow tombstones are not `BlockRemoved`.
- Priority does not reorder frames already in the kernel.
- The replica cap keeps the first two enqueued instances, ignores load and coverage, and keeps them after forward until a tombstone.
- `Dispatcher.choose` is not argmin of `T`. Equal coverage on the least-loaded worker never takes the affinity branch.
- Gateway superseded counts are pending-queue replacements. They include same-coverage repeats. They are not vLLM block aggregation.

**Q5. If the next round can add only one formal experiment, which?**

Neither subplot needs a new GPU matrix. If one new measurement is allowed, make it an observed load snapshot for Fig. 6(d), so the 16 ms result can be confirmed or retired on real in-flight loads. Do not spend that slot on Fig. 6(b) until the stale window is logged.

**Q6. Minimal matrix, cell count, GPU time**

Recommended next action: none. GPU time 0.

If that one snapshot is approved later:

- 1 live cell. Four local vLLM workers. Preseed 4 shared prefixes (16 requests), then about 8 routed requests while the in-flight loads differ. Log every candidate’s view coverage, in-flight load, and `vllm_cached_tokens` at the decision. Then apply full / `Krep=2` / `Krep=1` offline on that snapshot.
- Cell count: 1 GPU cell, 3 offline views. Seeds: 1. No ρ sweep, no overlap sweep.
- Active measurement target: at most 120 seconds. Budget about 2 minutes of GPU time including startup. Stop if the preseed alone exceeds 120 seconds.
- Do not compare the resulting `T` with TTFT.

Fig. 6(b) stays closed until a separate design review. A later isolated pair would be FIFO versus priority only, one controlled eviction, one seed: 2 GPU cells, under 4 minutes, and only after the timeline in the feasibility audit is logged. That pair is not the next experiment.

## Stop

No paper figure, no caption, no formal matrix.
