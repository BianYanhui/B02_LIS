# handsoff

本段到下面的分隔线为止是固定说明。后来的 Agent 不要改写、不要删、不要把当次进度写进这一段。每次交接只整段替换分隔线以下的内容。

## 这份文档是干什么的

同一个人会在两个 Agent 之间交替做这个仓库的开发。一边做完一段工作，就把分隔线下面更新掉；另一边先读这份文件，然后接着做，不用再口头补一大段上下文。

## 接手时

1. 先读分隔线下面的全文，再看它点名的文件和命令，不要凭记忆补实验设计。
2. 用户说「继续」时，按「下一步」做。没写进下一步的长实验不要自己开。
3. 改代码、跑实验、只查了状态，都算一段工作。离开前更新分隔线下面。

## 交接时怎么写

替换分隔线以下的全部内容，保留分隔线本身。写清这些，有代码 diff 就写，没有也要写：

- 现在在做哪件事，做到哪一步。
- 机器上还留着什么：进程、端口、GPU、容器、没跑完的队列。
- 已经定下来的结论和约束。不要只贴 git diff。
- 卡住的原因，以及下一次打开终端应该先跑的命令或先改的函数。
- 相关 commit、关键路径、明确不要提交或不要推送的文件。

不要把对话原文、完整 diff 或论文 PDF 贴进来。事实写短，路径写全。

---

## 可变交接

更新时间：2026-10-22 15:30（UTC+8）。P0-M1 到 P0-M3.6 已按 `analysis/p0_pressure_realism/EXPERIMENT_INDEX.md` 收成一包。Fig. 7(b) 在 `analysis/fig7b_fixed_k/`。论文没改。分支 `f6-d1b-coverage-dominance`。

### 现在在做的事

把这包证据送进 main。算法保持当前 StaticTopK：扩展合并、失效优先、覆盖占优的副本抑制、K=16、Owner 侧校验。Adaptive 仍不在论文算法里。

### 结论

Dispatcher 在测到的 4000 updates/s 以内没有饱和。这个数是下界。A4 外推的 7897/s 高于下界，不能判断。

Native msgpack 284529 B，Adapter 15552 B，KVSelect 出口 13120 B。94.534% 是表示转换。KVSelect 在这条 Trace 上再少的是 38 条 `superseded`、2432 B。Top-K 和 Replica 丢弃是 0。旧 UPSERT 仍能复活状态。满额 Top-K 丢掉 coverage 回退只在合成压力里出现。

64 B 是 codec。104 B 是记账。两者不和 10 Mbit/s 排空率混用。4096 Reporter 的 155.7 Mbit/s 是同构外推，不是生产拥塞。

Gateway 多记了 drain 等待和发送队列字节。准入没改。

### 路径

先读 `analysis/p0_pressure_realism/EXPERIMENT_INDEX.md`。通信成本的三个数在 `m3_6_communication_evidence/MOTIVATION_EVIDENCE_SUMMARY.md`。论文句子在同目录的 `ICC_MOTIVATION_INTEGRATION_PLAN.md`，只是建议，没有写进 PDF。

### 机器

`127.0.0.1:9710` 仍在听。没有留下这几轮的 Gateway、Dispatcher 或 vLLM。

### 下一步

停。等下一次 OODA。不要改论文，不要把 94.5% 写成算法收益，不要修版本或 Top-K 回退，不要为了找 Dispatcher 瓶颈再跑 Fan-in。

### 不要覆盖

不要覆盖 C1 原始帧、formal4t4、`ksweep_ov30`。不要停 9710。运行 `*.log` 被 gitignore 排除，证据在各目录的 CSV 和 JSON。
