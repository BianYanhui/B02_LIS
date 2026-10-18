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

更新时间：2026-10-19 00:36（UTC+8）。任务 F6-P2 已停。判定 **Case A**：在引擎确认的快照上，模型 regret 随被隐藏 owner 是否更空闲而出现或消失。不是测到的 TTFT。没有改 `experiments/`、KVSelect、Replica Suppression、Dispatcher、论文。没有动 formal4t4、Run A、Run B、F6-P0、F6-P1。没有画正式 Fig. 6。

### 正在做的事

三个短 cell 已在同一次 vLLM 启动里跑完并停掉。Cell A 反向、Cell B 交错、Cell C 用 seed 2 复现正向。广告顺序始终是 0、1、2、3。Full view 和 `Krep=2` 用同一快照。正 regret 和 0 regret 都留着。

### 结论（带标签）

- OBSERVED 驻留：每个 cell 自己的 cache salt。预热 cached tokens 都是 0。探测和再探测都是 1056，hit 计数也加 1056。prompt 1083。引擎 block hash NOT_MEASURED。没有用 ShadowCache。
- OBSERVED 负载：主指标仍是 harness in-flight。`ENGINE_CONFIRMED` 表示四张卡的 harness 计数等于 `num_requests_running` 且 waiting 为 0。采样在开跑前写死：holder 320 token，启动间隔 0.50 s，首次采样在启动后 0.25 s，之后每 0.50 s，共 8 点。
- Cell A，seed 1，顺序 `[3,2,1,0,3,2,1,0]`：8 个点里 4 个引擎确认，模型 regret 全是 0，mean DeltaLoad -0.5。后 4 个是 HARNESS_ONLY，regret 也是 0，不进均值。
- Cell B，seed 1，顺序 `[0,2,1,3,2,0,3,1]`：8/8 引擎确认。只有 `[1,1,1,0]` 的 regret 是 2.0 ms，其余是 0。mean regret 0.25 ms。
- Cell C，seed 2，顺序 `[0,1,2,3,0,1,2,3]`：8/8 引擎确认。`[1,1,0,0]`、`[1,1,1,0]`、`[2,2,1,1]`、`[2,2,2,1]` 的 regret 是 2.0 ms。mean regret 1.0 ms。F6-P1 的正向结果复现了，并且第二波也得到了引擎确认。
- MODEL-DERIVED：`T_i = load_i * 2 ms + max(0, 1083-1056)/50`。预填充项都是 0.54 ms。2.0 ms 是一个 in-flight 请求乘正式队列权重。`actual_ttft_ms` 为空。
- DeltaLoad = min(worker 0,1) - min(worker 2,3)。本轮和 F6-P1 的全部引擎确认点里，DeltaLoad 为 1 时 regret 是 2.0 ms，为 0 或 -1 时 regret 是 0。没有反例。这是 `choose` 算出来的，不是把 DeltaLoad 当成充分条件。
- 广告：每个快照都是转发 2、丢掉 2，藏住 2 和 3。Full view 选出的 worker 都在模型 argmin 里。
- F6-P1 正向：8 个点里 3 个引擎确认，mean regret 1.333 ms。这些点是一条轨迹，不是三个 seed。没有做显著性检验。
- Fig. 6(b) 仍关闭。F6-P0 的 16 ms 仍是 CONTROLLED。

### 已知问题和证据限制

- 差距只有 1 个请求。反向 cell 的第二波没有和引擎对齐。
- 同一 cell 里的快照是一条到达过程，不是独立重复。
- `choose` 不是 `T` 的 argmin。覆盖如果不再相同，DeltaLoad 和 regret 可以分开。
- 没有测反事实 TTFT，也没有走 gateway。
- 四个 owner 的 coverage 都是 1056，所以这轮没有回答 overlap。

### 路径

- 实验报告：`/home/byh/B02/analysis/fig6_reconstruction/f6_p2/F6_P2_EXPERIMENT_REPORT.md`
- 条件分析：`/home/byh/B02/analysis/fig6_reconstruction/f6_p2/F6_P2_CONDITIONAL_TRADEOFF.md`
- 下一步全文：`/home/byh/B02/analysis/fig6_reconstruction/f6_p2/NEXT_ITERATION_RECOMMENDATION.md`
- 表：`observed_snapshots.csv`，`counterfactual_routing.csv`，`f6_p1_p2_comparison.csv`，`f6_p1_p2_points.csv`，都在 `/home/byh/B02/analysis/fig6_reconstruction/f6_p2/`
- 原始记录：`/home/byh/B02/analysis/fig6_reconstruction/f6_p2/raw_cells.json`
- 内部诊断图（不是 Fig. 6）：`/home/byh/B02/analysis/fig6_reconstruction/f6_p2/deltaload_regret_diagnostic.svg`
- 脚本：`run_p2_cells.py`，`analyze_p1_p2.py`
- 服务器日志：`/home/byh/B02/analysis/fig6_reconstruction/f6_p2/server_logs/`（忽略，不要提交）

### 下一步

用户说「继续」时，不要画 Fig. 6，不要改论文，不要开 overlap 矩阵，不要开 Fig. 6(b)，不要再跑同一条斜坡的新 seed。先读 `NEXT_ITERATION_RECOMMENDATION.md`。

若只加一轮：把负载稳住再采样，而不是只采到达斜坡。顺序用正向和反向两种。目标 DeltaLoad 为 +1 和 -1，并且要等到 running gauge 与 harness 一致。seed 1 和 2。广告顺序仍是 0、1、2、3。8 个短阶段，一次启动四张 T4。启动约 2 分钟，测量约 3 分钟。预热超过 120 秒就停那个阶段。模型 regret 和实际执行的那一条 TTFT 分开记。不要把后者写成另一条视图的 TTFT。这次不要自己开。

P0-C1 仍然有效，不要重跑。不要开 Dynamo 集成、64 卡、1024 replay、TTFT 套件、P0-B 的 2×2。

### 机器

可以安全停下。2026-10-19 00:36（UTC+8）刚查过：四张 T4 显存 0 MiB，利用率 0%。这次的四个 `vllm serve` 已停，8000–8003 没有在听。没有残留的 `run_p2_cells.py`。`127.0.0.1:9710` 仍是 `b02-gateway4t4`，没有重启。不要动 edgevisor、lhjj、gitlab。

### Git

当前分支 `f6-p2-replica-tradeoff`。研究报告 commit `fcf6fe4`，父提交是 `78b6c1c`（F6-P1 交接，分支 `f6-p1-observed-replica`）。这次交接只改 `handsoff.md`。不要合并到 `main`，除非用户明确说。

F6-P1 仍在 `f6-p1-observed-replica`（`78b6c1c`，研究 `78fa6d3`）。F6-P0 在 `f6-p0-fig6-feasibility`（`36f3bfa`）。P0-C1 在 `p0-c1-real-indexer`（`fff14ff`）。

Dynamo 检出在 `/home/byh/Dynamo/dynamo`，commit `7a2a40906`。不要把 Dynamo 树提交进 B02。

不要提交、不要推送：`ICC_KV_实验结果_20261005.zip`、`analysis/icc_kv_export/`、`analysis/admission_overhead_4t4/`、`supplemental_20260922_cp_queue_delay/`、论文 PDF、`/tmp/icc_link_smoke/`、`/tmp/icc_link_smoke2/`、`analysis/fig6_reconstruction/f6_p2/server_logs/`。`*.log` 已被忽略。

### 仍然有效的旧约束

- 四卡自然负载不能写成顶满 10 Mbit。10 Mbit 路径实测排空约 9408 frame/s。
- `offered_wire_bit_per_s` 只乘 UPSERT，104 字节是记账约定。P0-C1 的 38 kbit/s 是实测 msgpack，TCP 载荷 NOT_MEASURED。
- fan-in 的 N=32/64 不是 GPU 数。
- `kv_cache_usage_perc` 在驱逐时可以仍是 0。未引用的 prefix block 也会让这个表接近 0。驱逐证据是 `BlockRemoved`。
- 前缀 tombstone 会藏住部分祖先。P0-C1 有 1 条：广告 1040，留下 448。
