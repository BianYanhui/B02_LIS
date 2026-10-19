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

更新时间：2026-10-19 17:35（UTC+8）。P0-D1 已完成并停止。没有开 GPU。`/handsoff-skill` 没有对应 Skill 文件，这次没有调用。

### 现在在做的事

分支 `p0-d1-topk-boundary`，从 main `7426cf8` 拉出。问题是 Static Top-K16 有没有容量边界，值得不值得做 Adaptive Expansion。没有改网关，没有改 Algorithm 1，没有跑新格子。

### 结论

主判定 `CAPACITY_TRADEOFF_WITHOUT_NET_GAIN`。控制判定 `ADAPTIVE_CONTROL_NOT_YET_FEASIBLE`。不要开始 P0-D2。

证据是已有的 `analysis/icc_kv/formal_A_5d2d300/ksweep_ov30`，commit `5d2d300`，10 Mbit，ultrahigh，pool 32，ov30，3 个 seed，K = 4/8/16/32/64。K=24 没有配过，记 `NOT_MEASURED`。该提交的 Top-K 代码和当前 `HEAD` 的 `gateway_relay_4t4.py`、`wire.py`、`e2e.py` 一致。

K=16 在所有看过的正式格里都会把 pending 顶满（peak queue 16–19）。base 负载（pool 16，噪声 256）上 FN 率是 0，包括 ρ=0.5 到 2.0。ov30 上 K=16 的 FN 率均值 0.030，K=32 是 0.013，但配对 TTFT 是 +210、−12、+60 ms，复用 token 不升。转发速率在 K=16 已经约 10148/s，K=32 仍是 10140/s，链路已经满。同一张表里 FN 最低的是 K=8（0.0075），不是更大的 K。

更新级别的淘汰分类没有网关事件日志，记 `COUNTERFACTUAL_NOT_VERIFIED`。队列深度和 drop 计数在 FN=0 的 base 格里也很高，不能当作扩容信号。

### 路径

`analysis/p0_adaptive/d1_topk_boundary/`

- `P0_D1_EXISTING_DATA_AUDIT.md`
- `adaptive_feasibility_assessment.md`
- `NEXT_ITERATION_RECOMMENDATION.md`
- `audit_existing_ksweep.py`
- `static_k_sweep.csv`，`topk_candidate_pressure.csv`，`topk_harmful_drop_analysis.csv`，`capacity_visibility_tradeoff.csv`

`analysis/icc_kv/` 仍被 gitignore，原始 summary 不入库。

### 机器

没有启动 vLLM。`b02-gateway4t4` 仍在 `127.0.0.1:9710`。本轮没有新端口。

### 下一步

不要实现 Adaptive Expansion，不要改默认 K，不要为了补 K=24 开 GPU。下一轮如果还做 Adaptive，先换问题；现有问题已经有答案。

### 不要提交、不要覆盖

不要提交 `analysis/icc_kv/`、`ICC_KV_实验结果_20261005.zip`、`analysis/admission_overhead_4t4/`、`analysis/icc_kv_export/`、`supplemental_20260922_cp_queue_delay/`、`*.log`、`*.pid`。不要改 `experiments/4t4/` 的冻结网关和 `Dispatcher.choose`。不要重启 `b02-gateway4t4`。
