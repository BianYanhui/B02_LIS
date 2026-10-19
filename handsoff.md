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

更新时间：2026-10-19 16:40（UTC+8）。任务 F6-B1 已完成并停止。没有正在跑的实验。`/handsoff-skill` 在仓库和 `/home/byh/.cursor` 里都没有对应 Skill 文件，这次没有调用该 Skill，只按本文件分隔线以下的规则更新。

### 现在在做的事

Fig. 6(b) 的真实 KV 失效、Tombstone 优先级和过期亲和路由已做完。判定是 `CONDITIONAL_PRIORITY_BENEFIT`：受控 ACK 窗口下，优先级缩短 Tombstone 排队并消除决策层的无效路由；自然投递两边一致。Fig. 6(a)(c)(d) 没有新实验。论文和论文里的 Fig. 6 没有改。

分支：`f6-b1-real-invalidation`。跑实验时的代码是 `3a3ce7d`。结果在其后的提交里。本交接文件单独提交。没有合并 main。父分支是 `f6-d1-measured-routing-regret` 的 `3d5bf33`。

### 做了什么

两次四卡启动。第一次 pilot（seed `2026101919`）里 Worker 0 的清空是真的，但后六个 256-token 背景请求复用了 16 token 的公共块，新窗口看不到父块，被标成 PARTIAL，Relay 没有启动。原始表在 `analysis/fig6_reconstruction/f6_b1/raw/pilot_shared_prefix_window/`。计分改为沿用该 Worker 上已记录的父块。`choose`、Relay、ACK 公式没有改。

第二次启动跑完。脚本墙钟 193 秒。Pilot `2026101919`，正式 seed `2026101920`–`2026101922`。四个 capture 都是 `BLOCK_CHAIN_MATCH`，Worker 0 的 `AllBlocksCleared` 在触发请求的 store 之前，Worker 1 没有 clear 或 removal。适配器标记为 `EXPERIMENTAL_INVALIDATION_ADAPTER`。失效标记为 `CONTROLLED_REAL_CACHE_INVALIDATION`，不是自然 `BlockRemoved`。

Relay 是冻结程序，端口 9730/9731。`merge=dedup=adaptive=topk=0`。唯一变化是 `priority`。受控臂 `max_inflight=1`，ACK 保持 0.7922039031982422 秒，由 pilot 的捕获跨度除以 4 得到，正式 seed 之前冻结，调整次数 0。探测偏移 2.371638536453247 秒，取 pilot 两条 apply 延迟的中点，之后没有移动。自然臂没有 ACK 保持。没有动 `b02-gateway4t4` 和 9710。

### 结果和标签

物理失效：`OBSERVED` / `CONTROLLED_REAL_CACHE_INVALIDATION`。Reset HTTP 完成到 native clear 约 4.7–5.3 ms。命令到 clear 约 9–11 ms。Clear 不是物理失效时刻，因为 vLLM 要等下一次 scheduler step。触发请求已记录，不是前缀 X。

Tombstone：每个 seed 一条，owner 0，lineage `F6B1S{seed}X`，coverage 0。Dispatcher 只删掉这个 owner。18 个 Relay cell 结束后 Worker 1 仍是 4096。

Gateway：`TIMED_RELAY_REPLAY`。正式受控臂入队时还有 7 条 upsert。FIFO 出队位置 7，优先级位置 0。正式队列等待均值 FIFO 5.584 s，优先级 0.029 s。配对差（优先级减 FIFO）均值 −5.555 s，样本 SD 0.0009 s。自然臂 pending 为 0，队列等待小于 0.3 ms，配对差均值 0.0008 s。优先级不能抢占已经出队的那一帧。正式 seed 上这一帧的剩余等待是 17–52 ms；pilot 上是 0.786 s。

Stale-positive duration 是构造量：捕获上的检测延迟加上回放上的 apply 延迟。不是同一次在线秒表。正式受控均值 FIFO 5.589 s（SD 0.019），优先级 0.034 s（SD 0.020）。自然均值约 0.008 s 和 0.009 s。没有空样本，没有把 P95 写成 0。

路由：`READ_ONLY_ROUTING_DECISION`。正式受控臂 FIFO 3/3 因果无效（选中 Worker 0，反事实选中 Worker 1），优先级 0/3。自然臂两边都是 0/3。反事实只用于归因。

执行：`EXECUTED_VLLM_REQUEST`。四个独立世界，不是 12 个策略 cell。Stale 世界两次都选 Worker 0，`cached_tokens=0`，prompt 4128。Cleared 世界两次都选 Worker 1，`cached_tokens=4096`。Owner validation 没有调用，记 `NOT_MEASURED`。TTFT 不是结果：两次命中分别是 1548 ms 和 125 ms。

n=3，不做显著性声明。Pilot 不进正式均值。计时用的那一对 `probe_is_frozen=0`，也不进均值。

### 论文

可以写条件句：在单帧 ACK 窗口下，失效优先缩短 Tombstone 排队，并在预注册探测点消除过期亲和决策。不能写成自然四卡拥塞、10 Mbit/s、或已经执行的 FIFO/优先级请求比率。候选图 `fig6b_candidate.pdf` 的纵轴是决策层无效路由，没有替换论文图。

### 路径

`analysis/fig6_reconstruction/f6_b1/`

- `F6_B1_ARCHITECTURE_AUDIT.md`
- `F6_B1_REAL_INVALIDATION_REPORT.md`
- `F6_B1_TOMBSTONE_MAPPING.md`
- `F6_B1_CAUSAL_TIMELINE.md`
- `F6_B1_PRIORITY_COMPARISON.md`
- `F6_B1_ROUTING_EFFECT.md`
- `F6_B1_PAPER_READINESS.md`
- `NEXT_ITERATION_RECOMMENDATION.md`
- `f6_b1_native_events.csv`，`f6_b1_tombstone_events.csv`，`f6_b1_gateway_timeline.csv`，`f6_b1_routing_attempts.csv`，`f6_b1_per_cell.csv`，`f6_b1_summary.csv`
- `feasibility_gates.json`，`capacity_rule.json`，`run_status.json`
- `raw/captures.json`，`raw/executed.json`，`raw/relay_stdout.jsonl`
- 候选图 `fig6b_candidate.pdf` / `.png`

vLLM 日志在 `raw/vllm_gpu*.log`，被 `*.log` 忽略，没有进 git。

### 机器

四张 T4 已回到 0 MiB。vLLM 已停。8000–8003、9730、9731 没有监听。`b02-gateway4t4` 仍在，`127.0.0.1:9710`。没有本轮后台脚本。

### 下一步

用户说「继续」时，不要开 GPU，不要跑 Fig. 6(a)(c)(d)，不要改 `choose` 或 Relay，不要改 0.792 秒的 ACK 保持，不要改论文。下一步只是 caption：决策层无效路由作为纵轴，自然臂为 0，受控臂标明是 ACK 窗口，持续时间放正文。除非明确要求请求必须在 FIFO 积压期间真正发出，否则不要重跑。那一次也只是两个重建世界、各一个请求、使用已经冻结的偏移。

### 不要提交、不要覆盖

不要提交 `ICC_KV_实验结果_20261005.zip`、`analysis/icc_kv_export/`、`analysis/admission_overhead_4t4/`、`supplemental_20260922_cp_queue_delay/`、`server_logs`、`*.log`、`*.pid`。不要改 `analysis/formal4t4/`、`analysis/icc_kv/`、F6-P0/P1/P2/P3/F1/A2/D1 的结果、Run A/B、KVSelect 核心算法、论文图。不要重启或重配 `b02-gateway4t4`，不要跑 `cell_rate_4t4.sh` 或 `restart_4t4.sh`。
