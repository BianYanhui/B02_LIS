# 压力与 Native 元数据实验索引

这一目录里，P0-A 到 P0-D 已经在各自提交里。下面是其后还没进 main 的一轮，按依赖顺序排列。每轮的结论以该目录里的 reality check 或 assessment 为准。运行日志匹配 `.gitignore` 的 `*.log`，不入库；CSV、JSON 和报告在库里。

Fig. 7(b) 不在这个目录。它用已有的 `ksweep_ov30` 画固定 K，见 `analysis/fig7b_fixed_k/FIG7B_EXPERIMENT_REPORT.md`。没有重跑 cell。K 从 4 到 64 的路由 FN 不是单调下降。

## 顺序

1. `m1_scale_fanin/`  
   单源速率外推到 256 / 1024 / 4096 个 Reporter。Reporter 不是 GPU 数。64 Reporter/Gateway 是研究设定。10 Mbit/s 上 104 B 帧的排空约 9408 frame/s，那是配置容量，不是生产配额。先读 `MOTIVATION_REALITY_CHECK.md`。

2. `m1_5_model_b_audit/`  
   多 Gateway、一个共享 Dispatcher 的代码路径。`instance` 是一个字节，512 个及以上身份放不下。论文仍是一只 Gateway。先读 `PAPER_ARCHITECTURE_ALIGNMENT.md` 和 `MOTIVATION_IMPLICATIONS.md`。

3. `m1_5b_model_b_benchmark/`  
   多 Gateway 的正确性和反压诊断。Apply sleep 是注入故障，不是部署拥塞。Dispatcher 稳定吞吐这一轮没有测。先读 `MODEL_B_MOTIVATION_ASSESSMENT.md`。

4. `m1_5c_dispatcher_capacity/`  
   FullSync、`max_inflight=0`，S1/S2，250 到 4000 updates/s。判定 `NO_SATURATION_WITHIN_TESTED_RANGE`。4000 updates/s 是稳定 Apply 的下界，不是最大值。A1 外推的 2121/s 低于这个下界。A4 外推的 7897/s 高于下界，不能判断。先读 `MOTIVATION_REALITY_CHECK.md`。

5. `m3_native_compact_motivation/`  
   P0-C1 的 212 条消息、252 个事件、284529 B msgpack。Block-to-Prefix 在这条 Trace 上与 Oracle 覆盖一致，包括 1040 收到 448。末端 tombstone 会把仍可复用的 448 收成 0。可行性 `PARTIALLY_SUFFICIENT`。先读 `MOTIVATION_REALITY_CHECK.md`。

6. `m3_5_native_compact_e2e/`  
   真实 StaticTopK Gateway，K=16，Adaptive 关。243 进、38 条扩展合并、205 转发、Top-K 丢弃 0。排空后 101 条覆盖与 Oracle 一致。旧 UPSERT 能复活已缩短或已清除的键。满额 Top-K 的回退丢失只出现在合成压力里。接口判定是 `COMPATIBLE_WITH_EXPERIMENTAL_MAPPING`，不是完整、安全的 Native 集成。先读 `MOTIVATION_INTEGRATION_ASSESSMENT.md`。

7. `m3_6_communication_evidence/`  
   同一条 Trace 的字节账：Native 284529 B，Adapter 15552 B，KVSelect 出口 13120 B。94.534% 属于表示转换。KVSelect 在这条 Trace 上再少 2432 B，而且只来自 38 条 `superseded`。64 B 是 codec，104 B 是另一套记账，两者不混用。4096 个同构 Reporter 的 Native 平均外推是 155.7 Mbit/s。这不是测到的生产拥塞。先读 `MOTIVATION_EVIDENCE_SUMMARY.md`。

## 不要混用的三件事

- P0-A1 的正式 Compact 负载，和 P0-C1 的单引擎 Native Trace，不是同一个 offered load。
- 64 B 帧长，和 104 B 的历史记账，以及那套 10 Mbit/s 排空率。
- 线性外推的 offered load，和一条真实控制链路已经拥塞。

Gateway 在这几轮多记了 drain 等待、在途帧数和内核发送队列字节。准入规则没有改。
