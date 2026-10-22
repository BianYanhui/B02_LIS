# P0-M3.6 Motivation 证据摘要

目录：`analysis/p0_pressure_realism/m3_6_communication_evidence/`。没有启动 vLLM，没有重放，没有改 Adapter、KVSelect、K 或论文。核心计数从 `frames.bin`、`converter_updates.csv` 和 cell 351 的 Gateway 日志重新算出，与既有汇总一致。

草稿图 `motivation_byte_cost_draft.png` 只放在这个目录。它不是论文 Fig. 1 的替换件。

## Q1：三条路径的通信成本

同一条单引擎 Trace，59.885 s。

| 路径 | 字节 | 平均 | 证据 |
|---|---:|---:|---|
| Native msgpack | 284529 B，212 条消息，252 个事件 | 38.010 kbit/s | `MEASURED_NATIVE` |
| Adapter | 15552 B，243 条 × 64 B | 2.078 kbit/s | `TRACE_DERIVED` |
| KVSelect 出口 | 13120 B，205 条 × 64 B | 1.753 kbit/s | 条数是 `ACTUAL_GATEWAY_REPLAY`，字节是 codec 乘积 |

表示转换少掉 268977 B，94.534%。KVSelect 再少掉 38 条、2432 B，占 Compact 的 15.638%，占 Native 的 0.855%。管道总差额 271409 B（95.389%）里，99.104% 来自 Adapter。

这 38 条全部是 `superseded`。`global_topk = 0`，`replica_cap = 0`。TCP 线上字节是 `NOT_MEASURED`。104 B 是历史记账，不进入这些百分比。

短窗口：Native 在 1 ms、10 ms、100 ms 的峰值都是一条 3772 B 的消息；最忙 1 秒是 8769 B。这些窗口里 1 ms 有 99.6% 是空的。Adapter 最忙 1 ms 是同一条消息展开的 40 个更新、2560 B，仍低于那条 Native 消息。KVSelect 的 1 ms 到 1 s 出口峰值是 `NOT_MEASURED`，因为 quiet 日志没有逐帧时间。

## Q2：Model B 里完整 Native 同步的量级

外推标记 `EXTRAPOLATED`。每只 Reporter 重复这只 TP=1 引擎。每 Gateway 64 个 Reporter 是研究设定。Reporter 数不是 GPU 数。TP>1 的引擎速率是 `NOT_MEASURED`。重复 Trace 只放大字节，不产生 4096 套独立 prefix。

Native 平均 offered load：

| Reporter | Gateway 数 | Native 平均 |
|---:|---:|---:|
| 256 | 4 | 9.73 Mbit/s |
| 512 | 8 | 19.46 Mbit/s |
| 1024 | 16 | 38.92 Mbit/s |
| 4096 | 64 | 155.69 Mbit/s |

同一规模的 Adapter 平均在 4096 Reporter 时是 8.51 Mbit/s，KVSelect 出口平均是 7.18 Mbit/s。每个 Gateway 的 Native 入口平均停留在 2.43 Mbit/s。共享 Dispatcher 看到的是聚合后的 155.69 Mbit/s。

10、100、1000 Mbit/s 是 `HYPOTHETICAL_CAPACITY`。4096 Reporter 的 Native 平均超过 100 Mbit/s 假设（比值 1.557），低于 1 Gbit/s 假设（比值 0.156）。这只说明该假设容量被平均负载超过。它不是数据中心已经拥塞的测量。

对齐相位是 `SYNCHRONIZED_EXTRAPOLATION`。4096 Reporter 对齐时，最忙 1 秒相当于 287 Mbit/s。64 个 Reporter 把那条 3772 B 的消息对齐到同一毫秒时，相当于 1.93 Gbit/s，而且只覆盖这一毫秒。独立相位的短窗口峰值没有测。

## Q3：哪种部署真的减少哪一段网络

应用层字节，网卡未抓。

- Worker 本地 Adapter：Worker 出口从 284529 B 降到 15552 B。这段节省属于表示转换。KVSelect 若留在 Gateway，跨 Worker 的合并仍然发生在 Gateway；本 Trace 上那一跳再少 2432 B。
- Gateway 本地 Adapter：Worker→Gateway 仍是 284529 B。变小的是 Gateway→Dispatcher，13120 B。入口差额不能写成入口带宽已经节省。
- Gateway 与 Dispatcher 同机，Adapter 在控制节点：Native 284529 B 已经穿过 Worker 到控制节点的网络。节点内部把 243 条更新收成 205 条。M3.5 的回放就是同机回环，没有测到网卡节省。

## Q4：能否不大改算法就加强动机

可以，而且应该只改文字。主张 C 和主张 D 有本轮数据。主张 A、B 要带上单引擎和同构外推这两个限定。主张 E、F 不能写进论文。

算法保持 Extension Supersession、Invalidation Priority、Coverage-Dominant Replica Suppression、固定 Top-K 16 和 Owner-Side Validation。不把 Adapter 写进方法，不把 94.534% 写成 KVSelect 的收益，不把 Native Trace 和 Section IV 的四 Worker 实验合成一个负载，也不把 block 级可见性和 prefix coverage 写成同一件事。

## Q5：截稿前性价比最高的修改

三处正文，大约六到八行，都不需要新实验：

1. Introduction 在 Compact 元数据那一句后写上 284529 / 15552 / 2432 这三个数，并写明后一个数才是这条 Trace 上的 KVSelect 增量。
2. 把 “quickly saturate” 改成 “负载超过控制预算时，FIFO 会拖住决策相关的更新”。Fig. 1 保留。
3. Section IV-A 写明 64 B 和 10 Mbit/s 是这套 Compact 实验的配置，Native 单引擎字节不进入那个 offered load。

不改 Fig. 1，不加方法小节，不改 Section II 的单 Gateway 模型，不改 Conclusion 里的四实例结果。草稿图留在分析目录，供作者决定要不要在删掉别的句子之后再放一个小图。

## 主张一览

A `PARTIALLY_SUPPORTED`。B `PARTIALLY_SUPPORTED`。C `SUPPORTED`，归 Adapter。D `SUPPORTED`，只归 38 条扩展合并。E `NOT_SUPPORTED`。F `NOT_SUPPORTED`。
