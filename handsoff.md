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

更新时间：2026-10-19 01:27（UTC+8）。任务 F6-P3 已停。没有开正式矩阵，没有合并 `main`。

### 正在做的事

Fig. 6(a)(c)(d) 的正式实验前置验证已完成。Fig. 6(b) 没有跑。判定：

- Fig. 6(a)：`READY_WITH_LIMITATIONS`
- Fig. 6(c)：`READY_WITH_LIMITATIONS`
- Fig. 6(d)：`READY-MODEL`

### 结论（带标签）

- OBSERVED，Fig. 6(a)：动态 LatestOnly 五个 rep 的 gateway 日志都能把 supersession 分成扩展和重复。扩展全是 0。rep 0 是 384 条 offered、310 条转发、74 条 supersession，全部是同一 coverage 的重复广告。最新状态可见性 P95 是 41.42 s。幸存消息的投递延迟 P95 是 31.97 s，两者不是同一个指标。五个 rep 的日志都配平：转发加 superseded 等于 offered。
- 已发表的 LatestOnly 37.775、StaticSemantic 30.950、Adaptive 15.175 是八个 workload×rho 格子的均值，不是这轮新结果。这些格子的原始 gateway 事件缺失，标记 `NOT_RECONSTRUCTABLE`。动态日志是另一套语料（320 个请求加 64 条预播种），不要拿去替换那 8 格均值。
- ACCOUNTING IDENTITY：上述 LatestOnly 日志里，merge off 会转发全部 offered。这是信令条数对照，不是时延对照。
- MODEL-REJECTED：按 phase 文件里的 1754/730/1754 bit/s 做 HTB 回放，得到 21 次 supersession 和 5.09 s 的 P95，对不上日志的 74 和 41.42 s。这组速率是实验里的受控容量，不是 10 Mbit/s，也不是生产瓶颈。不要用这组可见性数字。
- CONTROLLED：把 `original_compatible_trace_rep0` 压到每帧时间的四分之一，并让同一 lineage 落到同一个 owner。出现 56 次 coverage 上升和 20 次 coverage 回绕。这不是 vLLM 的扩展频率。
- OBSERVED，Fig. 6(c)：overlap 是 phase-0 lineage 被四台机器同时种下的比例，不是重复广告比例。pool 32，四台 worker。seed `2026072640` 只是校准，不是论文点。FullSync 事件全量排空后，Krep=2 的转发差等于 cap 丢弃：0% 为 48→45（6.25%），50% 为 112→66（41.07%），75% 为 144→82（43.06%）。其他机制丢弃为 0，窗口尾差为 0。
- OBSERVED 旁证：75% 的现场 FullSync 只转发了 123/144，剩下 21 条是窗口结束时的积压，不是 cap 收益。Adaptive 旧表把 low-utility 和 supersession 混在 holder 丢弃里。gateway 到达顺序不是 0、1、2、3。
- OBSERVED，Fig. 6(d)：一次四卡启动，测量 38.3 s。预热 cached tokens 为 0，探测和两段再探测都是 1056，hit 加 1056，prompt 1083。D-positive `[1,1,0,0]` 四点全部引擎确认，DeltaLoad +1，模型 regret 2.0 ms。D-negative `[0,0,1,1]` 四点全部引擎确认，DeltaLoad -1，regret 0。两段之间引擎回到空闲。
- MODEL-DERIVED：预填充项四台都是 0.54 ms，2.0 ms 是一个队列权重。不是 TTFT。
- NOT_MEASURED：成对执行的 TTFT。判定 `TTFT_PAIRING_NOT_READY`。没有做伪配对。

### 正式实验计划

路径：`/home/byh/B02/analysis/fig6_reconstruction/f6_p3/FIG6_ACD_FORMAL_EXPERIMENT_PLAN.md`

最小可投稿版本，还没跑：

- Fig. 6(a)：五个已有动态 rep，merge on 用日志，merge off 用配平恒等式。10 行，0 次新实验，CPU 不到 10 分钟。
- Fig. 6(c)：overlap 0、0.25、0.5、0.75，种子 `2026101901` 到 `2026101903`。12 次 FullSync 实录加 12 次 CPU 回放，共 24 格。一次四卡启动约 30 分钟。
- Fig. 6(d)：DeltaLoad 为 -1、0、+1，种子 3、4、5。9 个保持阶段。加载约 1 分钟，测量约 5 分钟。不测 TTFT。

用户说「继续」时按 `NEXT_ITERATION_RECOMMENDATION.md`：先汇总 (a)，再跑 (c)，再跑 (d)。不要开 (a) 的可见性扫描，除非 rep 0 的时钟门通过（supersession 距 74 在 10% 以内，且 P95 距 41.42 s 在 20% 以内）。现有 HTB 模型已经失败。

### 路径

- 总判定：`/home/byh/B02/analysis/fig6_reconstruction/f6_p3/F6_P3_READINESS_REPORT.md`
- (a)：`F6_A_EXTENSION_AUDIT.md`，`F6_A_MERGE_PAIRING.csv`，`a_supersession_classification.csv`
- (c)：`F6_C_OVERLAP_AUDIT.md`，`F6_C_SUPPRESSION_PAIRING.csv`
- (d)：`F6_D_HELD_LOAD_REPORT.md`，`held_samples.csv`，`raw_held.json`
- 脚本：`run_ac_readiness.py`，`run_d_held_load.py`
- 下一步：`NEXT_ITERATION_RECOMMENDATION.md`
- 服务器日志：`/home/byh/B02/analysis/fig6_reconstruction/f6_p3/server_logs/`（忽略，不要提交）

### 机器

可以安全停下。2026-10-19 01:27（UTC+8）刚查过：四张 T4 显存 0 MiB，利用率 0%。这次的四个 `vllm serve` 已停，8000–8003 没有在听。没有残留的 held-load 脚本。`127.0.0.1:9710` 仍是 `b02-gateway4t4`，没有重启。不要动 edgevisor、lhjj、gitlab。

### Git

当前分支 `f6-p3-formal-readiness`。研究报告 commit `5f8bf0a`，父提交是 `f401014`（F6-P2 交接）。这次交接只改 `handsoff.md`。不要合并到 `main`。

F6-P2 在 `f6-p2-replica-tradeoff`（交接 `f401014`，研究 `fcf6fe4`）。F6-P1 在 `f6-p1-observed-replica`（`78b6c1c`，研究 `78fa6d3`）。F6-P0 在 `f6-p0-fig6-feasibility`（`36f3bfa`）。P0-C1 在 `p0-c1-real-indexer`（`fff14ff`）。

Dynamo 检出在 `/home/byh/Dynamo/dynamo`，commit `7a2a40906`。不要把 Dynamo 树提交进 B02。

不要提交、不要推送：`ICC_KV_实验结果_20261005.zip`、`analysis/icc_kv_export/`、`analysis/admission_overhead_4t4/`、`supplemental_20260922_cp_queue_delay/`、论文 PDF、`/tmp/icc_link_smoke/`、`/tmp/icc_link_smoke2/`、`analysis/fig6_reconstruction/f6_p3/server_logs/`、`analysis/fig6_reconstruction/f6_p2/server_logs/`。`*.log` 和 `analysis/**/*.pid` 已被忽略。

### 仍然有效的旧约束

- 四卡自然负载不能写成顶满 10 Mbit。10 Mbit 路径实测排空约 9408 frame/s。
- `offered_wire_bit_per_s` 只乘 UPSERT，104 字节是记账约定。
- fan-in 的 N=32/64 不是 GPU 数。
- `kv_cache_usage_perc` 接近 0 不能证明前缀还在。驻留证据是 cached tokens 和 prefix-cache hit。
- 前缀 tombstone 会藏住部分祖先。P0-C1 有 1 条：广告 1040，留下 448。
- 不要改 Gateway Admission、Extension Supersession、Replica Suppression、`Dispatcher.choose`、Pressure-Triggered Filtering、Owner Validation。
- 不要覆盖 `analysis/formal4t4/`、`analysis/icc_kv/`、Run A、Run B、F6-P0、F6-P1、F6-P2。
