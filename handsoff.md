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

更新时间：2026-10-19 14:21（UTC+8）。任务 F6-A2 已完成并停止。没有正在跑的实验。

### 现在在做的事

Fig. 6(a) 的真实前缀增长和网关 extension supersession 已做完。判定是 Case C。Fig. 6(b)(c)(d) 没有开始。论文和论文里的 Fig. 6 没有改。

分支：`f6-a2-real-extension`。研究结果 commit `6ec93f2`（父提交 `c999630`）已推到 `origin/f6-a2-real-extension`。本交接文件在该提交之后单独提交。没有合并 main。

### 做了什么

一次四卡启动。可行性 seed `2026101905`：每卡 2 条 lineage，共 24 个 upsert。正式 seed `2026101906` `2026101907` `2026101908`：每卡 4 条 lineage，每条 1024、2048、4096，共 48 个 upsert。每个正式 seed 有 Natural / Controlled × Merge off / on，共 12 个正式 gateway cell。可行性另有 4 个 cell，只用来冻结 ACK 延迟，不进正式均值。整段脚本墙钟 342 秒。

请求是 `/v1/completions` 的 token id 列表，不是 chat template。发布器是 `EXPERIMENTAL_WRAPPER`，不是 `publish_state`。网关是冻结 Relay 的独立进程，端口 9720/9721。没有动 `b02-gateway4t4` 和 9710。没有用 F6-P3 的 HTB 模型。

### 结果和标签

Prefix growth：`OBSERVED`。56 条 lineage 全部是 1024 < 2048 < 4096，证据 `BLOCK_CHAIN_MATCH`。version 2 的 cached tokens 是 1024，version 3 是 2048。`BlockRemoved` 为 0。不能写成完整生命周期无损。

Native：168 条 ZMQ 消息，168 条 `BlockStored`，14336 个 hash，168 条 wrapper upsert。消息数没有被 wrapper 再压缩。一个 `BlockStored` 平均 85.3 个 hash，这是 vLLM 批处理，不是网关合并。

Natural：`NATURAL_NO_MERGE_OPPORTUNITY`。三次正式 seed 的 reduction 都是 0，true extension 都是 0，P95 可见性延迟两边都约 0.003 秒。

Controlled：`CONTROLLED` ACK 窗口。延迟在可行性速率上冻结为 0.588170 秒，`max_inflight=1`，正式 seed 没有再调。实测服务速率约 1.697 frame/s。正式 offered 约 4.6–4.7 upsert/s，强度约 2.7–2.8。这不是 10 Mbit，也不是自然四卡瓶颈。

Controlled merge-on，三次正式 seed：转发 21、22、22；true extension 27、26、26；eligible 与 true extension 相同，比例 1。被替换的全是 1024→2048 或 2048→4096。reduction 均值 0.5486，样本 SD 0.0120。P95 可见性延迟从约 16.84 s 降到约 4.09 s。时间加权 coverage staleness 从约 1382 token 降到约 675 token。`TIMED_REPLAY`，不是在线 TTFT。never-visible 为 0。最终 coverage 全部对齐。coverage regression 为 0。version 1 只有 6/48 自己被转发，version 2 是 11/48，但它们被后续更高 coverage 覆盖的时间仍短于 merge-off 的 FIFO。

### 论文

Case C。可以写成条件性 extension supersession，caption 必须带上 Natural 的零。不能写成自然拥塞改善、TTFT、更快路由，也不能把实验适配器写成已部署 wrapper。候选图 `fig6a_candidate.pdf` 没有替换论文图。

### 路径

`analysis/fig6_reconstruction/f6_a2/`

- `F6_A2_ARCHITECTURE_AUDIT.md`
- `F6_A2_PREFIX_GROWTH_REPORT.md`
- `F6_A2_WRAPPER_REPRESENTATION.md`
- `F6_A2_GATEWAY_MERGE_REPORT.md`
- `F6_A2_VISIBILITY_REPORT.md`
- `F6_A2_PAPER_READINESS.md`
- `NEXT_ITERATION_RECOMMENDATION.md`
- `f6_a2_source_growth.csv`，`f6_a2_gateway_events.csv`，`f6_a2_visibility.csv`，`f6_a2_per_cell.csv`，`f6_a2_summary.csv`
- `capacity_rule.json`，`raw/relay_stdout.jsonl`
- 候选图 `fig6a_candidate.pdf` / `.png`

vLLM 日志在 `raw/vllm_gpu*.log`，被 `*.log` 忽略，没有进 git。

### 机器

四张 T4 已回到 0 MiB。vLLM 已停。8000–8003、9720、9721 没有监听。`b02-gateway4t4` 仍在，`127.0.0.1:9710`。没有本轮后台脚本。

### 下一步

用户说「继续」时，不要开 GPU，不要跑 Fig. 6(b)(c)(d)，不要改 ACK 延迟，不要改论文。下一步只是 caption 取舍：要不要把这个带 Natural 零点的条件性面板放进正文。除非审稿明确要求服务限制必须是 tc qdisc，否则不要重跑。那一次也不能复用 F6-P3 的 HTB 公式。

### 不要提交、不要覆盖

不要提交 `ICC_KV_实验结果_20261005.zip`、`analysis/icc_kv_export/`、`analysis/admission_overhead_4t4/`、`supplemental_20260922_cp_queue_delay/`、`server_logs`、`*.log`、`*.pid`。不要改 `analysis/formal4t4/`、`analysis/icc_kv/`、F6-P0/P1/P2/P3/F1 的结果、Run A/B、KVSelect 核心算法、论文图。不要重启或重配 `b02-gateway4t4`，不要跑 `cell_rate_4t4.sh` 或 `restart_4t4.sh`。
