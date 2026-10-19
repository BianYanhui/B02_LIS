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

更新时间：2026-10-19 11:36（UTC+8）。任务 F6-F1 已完成并停止。没有正在跑的实验。

### 现在在做的事

Fig. 6(a) 五次 Dynamic LatestOnly 日志的正式统计，以及 Fig. 6(c) 的 12 个 live cell 和 12 组配对 replay，都已完成。Fig. 6(b)(d) 没有开始。论文 LaTeX 和论文里的 Fig. 6 没有改。

分支：`f6-f1-formal-ac`。研究结果 commit `5d91cfd`（父提交 `7605cd5`）已推到 `origin/f6-f1-formal-ac`。本交接文件在该提交之后单独提交。没有合并 main。

### Fig. 6(a)

证据：`OBSERVED` 网关日志。Merge-OFF 转发数是 `ACCOUNTING_IDENTITY`，不是 `OBSERVED_MERGE_OFF`。五次 rep 都守恒：offered 384，extension / tombstone / other / anomalous / undrained 全是 0。superseded 全是 same-coverage duplicate：74、70、73、94、88。均值 79.8（样本 SD 10.5），reduction 均值 `399/1920 = 0.2078125`（样本 SD 0.0275，范围 0.182–0.245）。五次 offered 相同，macro 等于 pooled。

可以写的结论：pending 的同 coverage 重复广告平均少发约 21%。不能写 extension aggregation、时延收益、在线 A/B。旧八格均值 37.78 / 30.95 / 15.18 仍是 `NOT_RECONSTRUCTABLE`。F6-P3 的 HTB replay 仍是 `MODEL_REJECTED`，没有使用。这张子图偏浅，只适合窄 caption 的紧凑柱图。候选图没有替换论文图。

### Fig. 6(c)

正式矩阵 12/12 `VALID`，失败 0。另有 2 次采集失败（seed `2026101901`、overlap 0、attempt 1 和 2），原因是 quiet relay 不打印逐条 JSON；不进入均值。见 `failures_capture_r1.json`。

Seeds `2026101901` `2026101902` `2026101903`。未混入 calibration seed `2026072640`。一次四卡启动，cell 时间 399.7 s。HTB 没有改，仍是原来的 10 Mbit。

证据：offered stream 是 `LIVE_VLLM_OFFERED_TRACE`，采集点是 FullSync FIFO dispatcher，并用 relay `K_STATS` 核对 received = forwarded = sent、各类 drop 为 0、结束时队列为 0。`gateway_arrival_time` 是 `NOT_MEASURED`。两个策略臂是 `PAIRED_FROZEN_RELAY_REPLAY`，不是线上 Krep=2 服务。TTFT 和 routing regret 是 `NOT_MEASURED`。

每个 overlap 的 offered 都相同：48、80、112、144。Reduction 均值：

- 0%：0.0625（样本 SD 0.0751；三次是 0.145833、0.041667、0）
- 25%：0.316667（SD 0.0191；0.337500、0.300000、0.312500）
- 50%：0.392857（SD 0.0089；0.401786、0.383929、0.392857）
- 75%：0.428241（SD 0.0175；0.444444、0.430556、0.409722）

对应 mean cap drops：3、25.333、44、61.667。mean suppressed owner–prefix pairs：2、16.667、32.667、49。mean Krep=2 visible owners：1.223、1.445、1.626、1.718。

保留的异常：0% 有一个零收益 seed；被隐藏的 owner 不固定是 worker 2/3（跨 cell 的唯一 owner–prefix：worker 1 为 123，2 为 118，3 为 58，0 为 2）；到达顺序常见 `(0,3,2,1)`，不是 `(0,1,2,3)`。这三次 seed 的 reduction 逐步上升，最后一步只有约 3.5 个百分点。没有为单调改数据。cap drops 大于被隐藏的唯一 owner 数。

可以画 reduction 对 overlap 的候选图，图上或附表必须有 offered。不能把 replay 写成四卡端到端 Krep=2 性能。

### 路径

`analysis/fig6_reconstruction/f6_f1_formal_ac/`

- `F6_A_FORMAL_SUMMARY.md`，`f6_a_per_rep.csv`，`f6_a_summary.csv`
- `F6_C_FORMAL_EXPERIMENT_REPORT.md`，`f6_c_per_cell.csv`，`f6_c_per_arm.csv`，`f6_c_summary.csv`
- `F6_AC_PAPER_READINESS.md`，`NEXT_ITERATION_RECOMMENDATION.md`
- 候选图 `fig6a_candidate.pdf/png`，`fig6c_candidate_reduction.pdf/png`，`fig6c_candidate_forwarded.pdf/png`
- 原始帧和 relay stats 在 `raw/`。driver `run_f6_c_live.py` sha256 `0c04d4f47e8d1b069272394dc8a94a36720be18c59267f350cd7a7df7a063acf`。

### 机器

四张 T4 已回到 0 MiB。vLLM 已停。8000–8003 和 9711 没有监听。`b02-gateway4t4` 仍在，`127.0.0.1:9710`，Up 9 days。HTB class `1:1` 仍是 10 Mbit，`1:10` 仍是 5 Mbit ceil 10 Mbit，backlog 0。没有后台实验脚本。

### 下一步

用户说「继续」时，不要开 GPU，不要跑 Fig. 6(b)(d)，不要改论文。只把上面的候选图和两条 caption 限制交给用户做取舍：Fig. 6(a) 要么保留成窄的 pending-supersession 柱图，要么不放进正文；Fig. 6(c) 可以按 reduction 对 overlap 画，并同时给出 offered。

### 不要提交、不要覆盖

不要提交 `ICC_KV_实验结果_20261005.zip`、`analysis/icc_kv_export/`、`analysis/admission_overhead_4t4/`、`supplemental_20260922_cp_queue_delay/`、`server_logs`、`*.log`、`*.pid`。不要改 `analysis/formal4t4/`、`analysis/icc_kv/`、F6-P0/P1/P2/P3 的结果、Run A/B、KVSelect 核心算法、论文图。不要重启或重配 `b02-gateway4t4`，不要跑 `cell_rate_4t4.sh` 或 `restart_4t4.sh`。
