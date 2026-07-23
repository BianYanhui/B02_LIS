# Live net-benefit / abstention guard ablation (S2, 2026-07-19)

Same difficult workload as the V5 primary K sweep (alpha=0.55, 96 prefixes,
2048-token prefixes, concurrency 4). Compares affinity-first routing (no
guard) with an abstention mode that falls back to the native load decision
when incremental coverage is below 1024 tokens or the affinity owner is
strictly busier than the native choice. Tests the paper's claim that a
bounded interface needs an abstention mode (Section 4.3) and gives live
evidence for the net-benefit principle (Section 4.4, Eq. 10).
