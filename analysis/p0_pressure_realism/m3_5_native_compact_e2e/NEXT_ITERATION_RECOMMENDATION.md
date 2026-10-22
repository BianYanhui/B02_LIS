# P0-M3.5 Next Iteration

停在这里，等下一次 OODA。不要自动改帧，不要自动给缩短覆盖加优先级，不要开性能矩阵。

## 五个问题

**Q1.** 能。在原始时间顺序下，252 个 Native 事件经过 Adapter 和真实 StaticTopK Gateway 之后，Dispatcher 排空时的 101 条覆盖与块 Oracle 一致，包括 448 和结束时的 416。缩短突发第一次被采样时还看得到旧的 1056，那是 40 条更新尚未 Apply；排空后变为 448。

**Q2.** 名额不满时安全：待发少于 K、以及正好 K 且旧 UPSERT 还在队列里，Dispatcher 都得到 448。两个 owner 的副本测试里，缩短后的 A 是 448，未缩短的 B 是 1024。名额被 16 条更高覆盖占满时不安全：448 的修正被 Top-K 丢掉，已经发出的 1056 留下来。后一种是 `SYNTHETIC_TOPK_STRESS`，不是这条 trace 的排队。

**Q3.** 不是唯一的缺口。版本字段能挡住迟到的 1040 和 Tombstone 之后的复活。它不能挡住 Scenario C。那是 Top-K 把向下修正当成一条新的低覆盖广告。修好它要改准入例外，不是再加一个版本整数。

**Q4.** 可以有条件地当作动机数据。能支持的是 Native 字节、Tombstone 与缩短覆盖的可见性差别、以及这条有序回放已经走过现有 Pipeline。不能支持的是“KVSelect 省下了这些字节”和“集成已经安全”。这条 trace 上 KVSelect 比 Adapter 少发的是 38 条 merge，Top-K 是 0 条。

**Q5.** 值得做的只有一张很小的三路径账：同一条 trace 上的 Native 284529 字节、Adapter 15552 字节、Gateway 实际转发 13120 字节，并标明 38 条 merge 和 0 条 Top-K。不值得为此改 Algorithm 1，也不值得做 256 到 4096 reporter 的回放。那张表仍要下一次明确批准后再跑；本轮没有做性能矩阵。

## 不要做

- 不要把 Scenario C 写成这条 worker 上的自然拥塞。
- 不要把实验性分支根写成正式 lineage 字符串。
- 不要恢复 Adaptive，不要改 K=16，不要改论文。
- 不要启动 vLLM 或 GPU。
- 不要把版本补丁合并进正式帧。设计在 `MINIMAL_VERSION_GUARD_DESIGN.md`，状态是 `PROPOSED_ONLY`。

## 复现

```text
cd /home/byh/B02
PYTHONPATH=/home/byh/B02 poc/.venv/bin/python \
  analysis/p0_pressure_realism/m3_5_native_compact_e2e/run_m35.py
```

Git HEAD `f3adf24fcf111c2bedcd64f3d28a0a9496d1abed`。结果在同目录的 `raw/results.json`。运行会占用 `19950`、`19951`、`19960`，结束时关闭。不要连到 `9710`。
