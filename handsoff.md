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

更新时间：2026-10-17 15:20（UTC+8）。P0-B1 历史审计已写完，放在分支 `p0-b1-activation-audit`。没有改 `experiments/`，没有开 smoke，没有做 2×2。不要把网关改动提交到 `main`。不要开正式矩阵，不要启动 `overnight.py` 或 `full_queue.py`。

### 正在做的事

审计 Pressure-Triggered Filtering 在已有实验里是否真的被激活。报告是 `analysis/p0_filtering_ablation/P0_B1_ACTIVATION_AUDIT.md`，逐格表是同目录的 `activation_cells.csv`。

已由历史数据证实：10 Mbit Run B 的 Adaptive `congested_fraction` 和 `low_utility` 都是 0，Top-K 把队列压在约 18 帧。formal4t4 Reuse-intensive 上效用准入只删了 1024-token 更新；Original-compatible 的 `relay_drop_low_utility` 是 `1dc24ec` 里缩容误记。161 ± 85 ms 是 Reuse 上 Adaptive 相对 StaticSemantic 的联合 TTFT 差，不能写成效用过滤的独立收益。

### 当前状态

停着。没有为这次审计重新拉起 vLLM。01:00 时 `127.0.0.1:8000–8003` 没有在听，网关容器 `b02-gateway4t4` 在听 `127.0.0.1:9710`。同机的 edgevisor、lhjj、gitlab 等容器不要动。

`main` 上 `experiments/` 相对 `48b64f4` 仍然为空。审计分支也不改实验代码。

### 下一步

用户说「继续」时，不要开 10 Mbit 的 2×2，也不要为了让过滤触发去改 tau、λ 或 queue gate。若用户要改论文句子，按审计报告 G 节替换，不要加长篇幅。只有用户明确要求拆 161 ms 时，才在 formal4t4 的 Reuse-intensive、ρ∈{0.8,1.0,1.2} 上另开分支设计小规模因子实验。

### 已经定下来的约束

- 默认天花板仍是 1 Gbit。10 Mbit 只用于 `--link-bit 10000000`。Gateway `--cpus 1 --cpuset-cpus 0`。
- 主方法是 `StaticTopK16`。`Adaptive` 是消融。旧的 `adaptive=1` 必须继续同时表示效用过滤和 K//4 缩容，已有单测不能改语义。
- 论文里相对 StaticSemantic 的 161 ± 85 ms 不能写成效用过滤的独立收益。
- 不要把 4 张 GPU 写成大规模系统。
- Python：`/home/byh/B02/poc/.venv/bin/python`。`analysis/icc_kv/` 在 `.gitignore`。

### Git

`main` 仍是 `c44ab85`，跟踪 `origin/main`。P0-B1 审计在分支 `p0-b1-activation-audit`，只含报告、逐格表和这份交接。不要 amend。不要把该分支合并进 `main`，除非用户明确要求。

不要提交、不要推送：`ICC_KV_实验结果_20261005.zip`、`analysis/icc_kv_export/`、`analysis/admission_overhead_4t4/`、`supplemental_20260922_cp_queue_delay/`、论文 PDF、`/tmp/icc_link_smoke/`、`/tmp/icc_link_smoke2/`。
