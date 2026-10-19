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

更新时间：2026-10-19 15:22（UTC+8）。任务 F6-D1 已完成并停止。没有正在跑的实验。`/handsoff-skill` 在仓库和 `/home/byh/.cursor` 里都没有对应 Skill 文件，这次没有调用该 Skill，只按本文件分隔线以下的规则更新。

### 现在在做的事

Fig. 6(d) 的真实 coverage 异构和实测 TTFT 已做完。判定是 Case A，只针对负载为 0、隐藏 Owner 持有更长前缀的条件。Fig. 6(a)(b)(c) 没有新实验。论文和论文里的 Fig. 6 没有改。

分支：`f6-d1-measured-routing-regret`。跑实验时的代码是 `28a4ec3`。结果 commit `bc30302`，父提交含 `28a4ec3`。本交接文件在该提交之后单独提交。没有合并 main。

### 做了什么

两次四卡启动。第一次 pilot 把 `reset_prefix_cache` 推迟到下一步才发出的 `AllBlocksCleared` 当成了驱逐，四条 warmup 的 token chain 其实已经对齐，cached tokens 为 0，但被标成 PARTIAL，在任何 TTFT 之前停止。原始表在 `analysis/fig6_reconstruction/f6_d1/raw/pilot_reset_event_window/`。计分改为：store 之前的 clear 是 reset，store 之后的 clear 才是驱逐。种子、长度、`choose` 没有改。

第二次启动跑完。Pilot seed `2026101909`，正式 seed `2026101910`–`2026101913`，外加 D3 两个 seed。脚本墙钟 182 秒。30 个 trial 全部 valid。请求是 token id 列表，不是 chat template。发布器是 `EXPERIMENTAL_WRAPPER`。网关是冻结 Relay，端口 9720/9721，`dedup` 0 或 2。广告顺序是 visible 在前，标签 `CONTROLLED_ADMISSION_ORDER`。没有动 `b02-gateway4t4` 和 9710。没有改 `Dispatcher.choose`。

### 结果和标签

Coverage：`OBSERVED`。120 条 warmup 全是 `BLOCK_CHAIN_MATCH`，warmup cached tokens 全是 0，`BlockRemoved` 为 0。同一 trial 四个 Worker 的 prompt token hash 相同。短 Worker 1024，长 Worker 4096。正式请求返回的 cached tokens 与被选中 Worker 的 warmup coverage 一致。prompt 长度 4128。

路由：Full 与 Krep=2 在 D1 的四对里都选了不同 Worker，Full 拿到 4096，Cap 拿到 1024。Assignment B 的 cap 仍然把 native 选在被隐藏的 Worker 0 上，然后 guard 把它改到短 coverage 的 Worker 3。Native 负载均衡没有被人为关掉。

TTFT：`MEASURED_CLIENT_TTFT`。D1 四对，Δ = TTFT_Krep2 − TTFT_Full，均值 1812.59 ms，中位数 1802.58 ms，样本 SD 90.32 ms，范围 1716–1929 ms。coverage loss 每对都是 3072。A/B 两种角色、两种世界顺序都是正的。

D2：路由不变，Δ 均值 0.91 ms。D0：coverage loss 0，model regret 0。中位数 Δ −1.10 ms。均值 −316.97 ms，因为第一对正式 trial（seed 2026101910，同一 Worker，两边 cached 都是 4096）是 1390 ms 对 124 ms。这不是 suppression 的收益。这一对留在主表里。其余三对 D0 都在 1.4 ms 以内，包括换了 GPU 的 Assignment B。

模型：`MODEL_DERIVED`。D1 的 regret 每对都是 61.44 ms，大约是实测 Δ 的 1/30。符号一致，不能当成 TTFT。

D3：两个真实 holder，Δ 4072 ms 和 5485 ms。n=2，不是主结论。

Pilot 的第一对绝对时间更慢（1550 ms 和 3406 ms），不进正式均值。

### 论文

Case A，仅限上述条件。可以写成 measured serving penalty。caption 必须带上 D0 的第一对离群、D2 的近零、controlled admission，以及 61.44 ms 不是 TTFT。不能写成自然到达、已部署 wrapper、或生产拥塞。候选图 `fig6d_candidate.pdf` 没有替换论文图。

### 路径

`analysis/fig6_reconstruction/f6_d1/`

- `F6_D1_ARCHITECTURE_AUDIT.md`
- `F6_D1_COUNTERFACTUAL_DESIGN.md`
- `F6_D1_COVERAGE_VALIDATION.md`
- `F6_D1_ROUTING_ANALYSIS.md`
- `F6_D1_MEASURED_TTFT_REPORT.md`
- `F6_D1_PAPER_READINESS.md`
- `NEXT_ITERATION_RECOMMENDATION.md`
- `f6_d1_warmup.csv`，`f6_d1_per_trial.csv`，`f6_d1_paired_results.csv`，`f6_d1_summary.csv`，`f6_d1_model_vs_measured.csv`
- `feasibility_gates.json`，`run_status.json`，`raw/relay_stdout.jsonl`
- 候选图 `fig6d_candidate.pdf` / `.png`

vLLM 日志在 `raw/vllm_gpu*.log`，被 `*.log` 忽略，没有进 git。

### 机器

四张 T4 已回到 0 MiB。vLLM 已停。8000–8003、9720、9721 没有监听。`b02-gateway4t4` 仍在，`127.0.0.1:9710`。没有本轮后台脚本。

### 下一步

用户说「继续」时，不要开 GPU，不要跑 Fig. 6(a)(b)(c)，不要改 `choose` 或 50 tok/ms，不要改论文。下一步只是 caption 取舍：要不要把这个带 D0 离群点和 D2 近零的实测面板放进正文。除非审稿明确要求广告顺序必须是自然到达，否则不要重跑。那一次也必须走冻结 Relay。

### 不要提交、不要覆盖

不要提交 `ICC_KV_实验结果_20261005.zip`、`analysis/icc_kv_export/`、`analysis/admission_overhead_4t4/`、`supplemental_20260922_cp_queue_delay/`、`server_logs`、`*.log`、`*.pid`。不要改 `analysis/formal4t4/`、`analysis/icc_kv/`、F6-P0/P1/P2/P3/F1/A2 的结果、Run A/B、KVSelect 核心算法、论文图。不要重启或重配 `b02-gateway4t4`，不要跑 `cell_rate_4t4.sh` 或 `restart_4t4.sh`。
