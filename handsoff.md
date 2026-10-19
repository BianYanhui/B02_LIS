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

更新时间：2026-10-19 22:50（UTC+8）。P0-D1b 已完成并停止。`/handsoff-skill` 没有对应 Skill 文件，这次没有调用。

### 现在在做的事

分支 `p0-d1b-k-crossover`，从 `p0-d1-topk-boundary` 的 `198a5b6` 拉出。问题是 K=8 和 K=16 有没有可重复的性能交叉，值不值得做 Adaptive。没有改网关准入，没有改 Algorithm 1。

### 结论

主判定 `STATIC_K8_SUFFICIENT`。不要开始 P0-D2。

ov30 已有三 seed：K=8 的 Routing FN 三个 seed 都更低（差值 −0.0075、−0.0451、−0.0150）。TTFT 只有两个 seed 更低，seed 0 是 +18 ms。K=4 没有优于 K=8。

Base 补了同一次进程里的配对：StaticTopK8 与 StaticTopK16，ultrahigh，pool 16，噪声 256，10 Mbit，seed 0–2，600 s，commit `198a5b6`。六个 cell 都成功。两边 FN 都是 0，143/143 前景更新都送达。复用 token 只在 seed 0 的一个请求上有差别。TTFT 差值（K=8−K=16）是 +0.4、+21.5、−9.1 ms。K=16 没有用户侧优势。K=16 每秒大约多转发 2100 帧，网关 CPU 高约 0.05。

这不是性能交叉。Base 上队列和 Top-K drop 都很高，同时 FN 是 0，这两个信号不能用来切换 K。10 月那次 Base K=16 的 TTFT 是 3300–3800 ms，这次同配置大约 600 ms，复用 token 接近，绝对 TTFT 不能跨天比较。

### 路径

`analysis/p0_adaptive/d1b_k_crossover/`

- `P0_D1B_SUMMARY.md`
- `DATA_AND_IMPLEMENTATION_AUDIT.md`
- `existing_k_sweep_paired.csv`
- `base_k8_k16_comparison.csv`
- `regime_crossover_analysis.csv`
- `mechanism_evidence_audit.md`
- `online_signal_feasibility.md`
- `NEXT_ITERATION_RECOMMENDATION.md`
- `smoke/`，`base_ultrahigh/`

### 机器

本轮用 `experiments/4t4/restart_4t4.sh` 拉起四个 vLLM，KV cache 104544。`prepare_fixed_gateway` 重建了 `b02-gateway4t4`，1 CPU，HTB 10 Mbit，这是正式矩阵的控制路径，不是准入改动。实验结束后关掉本轮的四个 vLLM。9710 上的网关容器保留。

### 下一步

不要实现 Adaptive，不要改默认 K，不要为了找交叉再调负载。

### 不要提交、不要覆盖

不要提交 `analysis/icc_kv/`、`ICC_KV_实验结果_20261005.zip`、`analysis/admission_overhead_4t4/`、`analysis/icc_kv_export/`、`supplemental_20260922_cp_queue_delay/`、`*.log`、`*.pid`。不要改 `experiments/4t4/` 的冻结网关和 `Dispatcher.choose`。
