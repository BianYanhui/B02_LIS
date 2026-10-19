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

更新时间：2026-10-19 23:50（UTC+8）。P0-D3 已完成并停止。没有开 GPU。`/handsoff-skill` 没有对应 Skill 文件，这次没有调用。

### 现在在做的事

分支 `p0-d3-utility-final`。问题是压力触发的 Utility Filtering 有没有独立于 Coverage Top-K 的收益，以及它到底是 freshness 还是 1024-token 门槛。没有改 tau、lambda、压力门、Top-K、Algorithm 1。

### 结论

主判定 `NO_INDEPENDENT_UTILITY_GAIN`。建议 `REMOVE_ADAPTIVE_FROM_MAIN_DESIGN`：从主要技术贡献里拿掉，不要在这轮删代码，也不要改论文。下一轮如果改论文，再改那句 161 ± 85 ms。

formal4t4 Reuse 的网关事件：入队前效用拒绝 942 条，全部是 1024 token，年龄 0.1–1.4 ms。2048 和 4096 的入队前拒绝是 0。这是覆盖门槛，不是 freshness。Original 上入队前拒绝是 0。10 Mbit Run B 上 congested 和 low_utility 都是 0。

`1dc24ec` 的 `relay_drop_low_utility` 混进了缩容。入队后 50 ms 内删掉的是缩容误记。更晚的同名事件分不清缩容和出队时的第二次效用检查，EWMA 没有记录，不记成 freshness 拒绝。

161.0 ms（20 对，样本 SD 193.3）是完整 Adaptive 相对 StaticSemantic。对方没有 Top-K。ρ=0.5 的分项均值是 −2.0 ms。Original 上同一对比均值 −25.2 ms。不能写成 Utility 的独立收益。Fig. 7(b) 也是整套 Adaptive，不是 freshness 消融。

A/B/C/D 没有跑。事件已经说明过滤器是 1024 门槛，不再为了找用户侧增益改准入。

### 路径

`analysis/p0_adaptive/d3_utility_final/`

### 机器

GPU 空闲。没有启动 vLLM。`127.0.0.1:9710` 仍在听。

### 下一步

等论文修改决定。不要再开 Adaptive 实验，不要调参，不要实现 coverage-threshold 模式。

### 不要提交、不要覆盖

不要提交 `analysis/icc_kv/`、`analysis/formal4t4/` 的原始格、`ICC_KV_实验结果_20261005.zip`、`analysis/admission_overhead_4t4/`、`analysis/icc_kv_export/`、`supplemental_20260922_cp_queue_delay/`、`*.log`、`*.pid`。不要改 `experiments/4t4/`。不要覆盖 P0-B1、P0-D1、P0-D1b。
