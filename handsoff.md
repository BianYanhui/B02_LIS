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

更新时间：2026-10-18 17:55（UTC+8）。任务 F6-P0 已停。目标是 Fig. 6(b)(d) 的最小可行性，不是正式矩阵。没有改 `experiments/`，没有改 KVSelect，没有改论文，没有动 formal4t4、Run A、Run B 的结果。

### 正在做的事

Fig. 6(b) 判定 **INCONCLUSIVE**。Fig. 6(d) 判定 **PASS**，范围只限 CONTROLLED 反事实。Fig. 6(a)(c) 只审计，没有重跑，现有计数可以留。

没有启动 vLLM。离线脚本调用了冻结的 `Relay.enqueue` / `_dequeue` 和 `Dispatcher.choose`。

### 结论（带标签）

Fig. 6(b)，INCONCLUSIVE：

- OBSERVED：`analysis/formal4t4/summary/signaling_accounting.csv` 里 8 个正式 cell，所有策略 `source_tombstones_sent_mean` 都是 0。baseline 的 `physical_false_positive_affinity_rate_mean` 和 `tombstone_delay_p95_s_mean` 都是 0。后者在没有 tombstone 样本时由 `percentile()` 写成 0，不是测到的零延迟。
- CONTROLLED：FIFO 下 tombstone 排在 8 条无关 upsert 之后。服务 1 帧后，FIFO 仍按正向 affinity 选中 worker 0；priority 已经应用 tombstone，改选空闲 worker，affinity 为假。表：`/home/byh/B02/analysis/fig6_reconstruction/f6_p0/smoke_b/priority_window.csv`。
- 这条对照的“前缀已经失效”是设定的，不是 vLLM residency。NOT_MEASURED：真实 Stale-Positive、以秒计的可见性延迟、Owner Validation、TTFT。
- 已发布策略把 priority、merge、dedup 捆在一起。churn 是注入 reset，不是这条隔离对照。不能宣称调度收益。

Fig. 6(d)，PASS（CONTROLLED）：

- OBSERVED：同一张 signaling 表，StaticSemantic 平均丢掉 25.15 条 duplicate holder，Adaptive 17.68，dedup 关闭的策略是 0。重叠预热会向四张卡各发一次再广告 instance 0..3。
- CONTROLLED / DERIVED：同一快照上，Krep=2 转发 2 条、丢掉 2 条。负载都是 2 时，`T` regret 为 0。负载 `[8,8,0,8]` 时，cap 按入队顺序留下 0 和 1，藏起空闲的 worker 2，`Dispatcher.choose` 选 worker 1，regret 16 ms。`T_i = load_i * 2 ms + max(0, prompt-coverage_i) / 50`。这不是测到的 TTFT。覆盖差是 0，16 ms 全是负载项。
- NOT_MEASURED：正式 trace 里隐藏 owner 恰好最便宜的比例；被抑制 owner 当时的 `vllm_cached_tokens`。

Fig. 6(a)(c)，保留现有数据：

- (a) OBSERVED gateway `relay_drop_superseded`：LatestOnly 37.78，StaticSemantic 30.95，Adaptive 15.18，merge 关闭的策略是 0。这是未发送队列上同一 `(instance, digest)` 的替换。不是 vLLM 把多个 block hash 收成一条 `BlockStored`。计数不区分覆盖增长和重复广告。
- (c) OBSERVED 就是上面的 duplicate_holder。保留规则与 (d) 相同：先入队的两个 instance，不看负载和覆盖，转发后仍然占着名额，直到 tombstone。正式 baseline 没有 tombstone，名额不会释放。0/25/50/75% overlap 扫描按 `final_report.md` 仍是校准，不要写进论文。
- (c) 与 (d) 共用这条规则，共用不了 regret。(d) 的 regret 在 `smoke_d/replica_regret.csv`，是 CONTROLLED。

### 已知问题和未验证假设

- 优先级只重排 relay 里还没写进内核的帧。tc 里已有的积压这次没有测。
- `source_view_false_positive` 对的是 ShadowCache。`physical_false_positive_affinity` 对的是被选中 worker 的 `vllm_cached_tokens`。Shadow tombstone 不是 `BlockRemoved`。
- `choose` 不是 `T` 的 argmin。覆盖相同时，最轻负载的 worker 不会走 affinity 分支。
- P0-A4 的 18 条前缀 tombstone 没有和这次 dispatcher 决策对齐。`experiments/icc_kv/replay.py` 不能当这个对齐工具。
- 假设：D-B 这种负载不均会在正式预热之后出现。频率 NOT_MEASURED。

### 路径

- 审计：`/home/byh/B02/analysis/fig6_reconstruction/f6_p0/F6_P0_FEASIBILITY_AUDIT.md`
- (b)：`/home/byh/B02/analysis/fig6_reconstruction/f6_p0/F6_B_SMOKE_REPORT.md`
- (d)：`/home/byh/B02/analysis/fig6_reconstruction/f6_p0/F6_D_SMOKE_REPORT.md`
- (a)(c)：`/home/byh/B02/analysis/fig6_reconstruction/f6_p0/F6_AC_EXISTING_DATA_AUDIT.md`
- 下一步全文：`/home/byh/B02/analysis/fig6_reconstruction/f6_p0/NEXT_ITERATION_RECOMMENDATION.md`
- 脚本：`/home/byh/B02/analysis/fig6_reconstruction/f6_p0/smoke_b/offline_priority.py`
- 历史汇总：`/home/byh/B02/analysis/fig6_reconstruction/f6_p0/audit/historical_formal4t4.json`

### 下一步

用户说「继续」时，不要开 GPU，不要画 Fig. 6，不要改论文。先读 `NEXT_ITERATION_RECOMMENDATION.md`。

下一轮最值得做的不是新矩阵。Fig. 6(d) 的机制问题已经由这次离线快照回答。若只能新增一组测量，做一次 OBSERVED 负载快照：4 个共享前缀预热（16 次请求）加约 8 次路由，记下每个候选的 coverage、in-flight load、`vllm_cached_tokens`，再离线套 full / Krep=2 / Krep=1。1 个 GPU cell，1 个 seed，主动测量目标不超过 120 秒，含启动大约 2 分钟。预热单独超过 120 秒就停。不要把 `T` 写成 TTFT。这次不要自己开。

Fig. 6(b) 保持关闭，直到另一次设计把真实失效、仍为正的 affinity、以及窗口内请求记在同一条时间线上，并且只改 priority 标志。

P0-C1 的结论仍然有效，不要重跑。不要开 Dynamo 集成、64 卡、1024 replay、TTFT 矩阵、P0-B 的 2×2。

### 机器

可以安全停下。2026-10-18 17:55（UTC+8）刚查过：四张 T4 显存 0 MiB，利用率 0%。没有 `vllm serve`，没有 `dynamo.indexer`，没有 `run_c1.py`，没有这次的离线脚本残留。8000–8003、5557、8090 没有在听。`127.0.0.1:9710` 是容器 `b02-gateway4t4`，不要动。不要动 edgevisor、lhjj、gitlab。

### Git

当前分支 `f6-p0-fig6-feasibility`。研究报告 commit `7cecab4`，已经推到 `origin/f6-p0-fig6-feasibility`。父提交是 `fff14ff`（P0-C1 交接）。这次交接只改 `handsoff.md`。不要合并到 `main`，除非用户明确说。

P0-C1 仍在 `p0-c1-real-indexer`（`fff14ff`）。P0-A3 `d978ad3`。P0-A4 `9010ce0`。P0-A1、P0-A2、`p0-b1-activation-audit` 保持原分支。

Dynamo 检出在 `/home/byh/Dynamo/dynamo`，commit `7a2a40906`。`_core.abi3.so` 指向 debug `lib_core.so`，旧文件是 `_core.abi3.so.pre-c1`。不要把 Dynamo 树提交进 B02。

不要提交、不要推送：`ICC_KV_实验结果_20261005.zip`、`analysis/icc_kv_export/`、`analysis/admission_overhead_4t4/`、`supplemental_20260922_cp_queue_delay/`、论文 PDF、`/tmp/icc_link_smoke/`、`/tmp/icc_link_smoke2/`。`analysis/icc_kv/` 在 `.gitignore`。`*.log` 已被忽略。

### 仍然有效的旧约束

- 四卡自然负载不能写成顶满 10 Mbit。10 Mbit 路径实测排空约 9408 frame/s。
- `offered_wire_bit_per_s` 只乘 UPSERT，104 字节是记账约定。P0-C1 的 38 kbit/s 是实测 msgpack，TCP 载荷 NOT_MEASURED。
- fan-in 的 N=32/64 不是 GPU 数。
- `kv_cache_usage_perc` 在驱逐时可以仍是 0。驱逐证据是 `BlockRemoved`。
- 前缀 tombstone 会藏住部分祖先。P0-C1 有 1 条：广告 1040，留下 448。
