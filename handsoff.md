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

更新时间：2026-10-19 00:08（UTC+8）。任务 F6-P1 已停。判定 **P1-PASS**。目标是在真实 Worker Load 和真实 KV residency 下，看 `Krep=2` 会不会藏住更便宜的 owner。没有改 `experiments/`、KVSelect admission、Replica Suppression、Dispatcher、论文。没有动 formal4t4、Run A、Run B、F6-P0。没有画 Fig. 6。

### 正在做的事

一个 GPU cell、seed 1 已跑完并停掉。四个 vLLM worker 持有同一条前缀。离线对同一快照做了 full view 和 `Krep=2`。正 regret 和 0 regret 的样本都留着。

### 结论（带标签）

- OBSERVED：四张 T4 上的 vLLM 0.10.2，端口 8000–8003，Qwen2.5-1.5B-Instruct，Automatic Prefix Caching。预热 cached tokens 都是 0，prompt 1083。探测和负载窗口之后的再探测，四张卡 cached tokens 都是 1056。每张卡的 `prefix_cache_hits` 在探测时增加 1056，`prefix_cache_queries` 增加 1083。这等于 block size 16 对齐后的公共前缀。客户端公共 token 前缀 SHA-256 是 `a0c92e73685889a436edaafe47579af53b47baed287a82c208ee5f3f8a84ac7d`。引擎 block hash NOT_MEASURED。没有用 ShadowCache。
- OBSERVED 负载：Dispatcher 用的是 harness in-flight。引擎确认的不平衡是 `[1,1,0,0]`（B1）和 `[1,1,1,0]`（B2）。平衡样本是 `[0,0,0,0]` 和 `[1,1,1,1]`。B0、B4–B7 的 harness 计数和 `num_requests_running` 不一致，不能当引擎确认的不平衡。`num_requests_waiting` 全是 0。
- OBSERVED 广告：入队顺序 0、1、2、3。冻结的 `_admit_replica`（`dedup=2`）每次都留下 0 和 1，丢掉 2 和 3。转发 2，丢弃 2。没有改这条规则。merge、priority、adaptive、top-k 都关着，两边只差 dedup。
- MODEL-DERIVED：`T_i = load_i * 2 ms + max(0, 1083 - 1056) / 50`。预填充项四张卡都是 0.54 ms。B1、B2 的 `Regret = T(cap) - T(full) = 2.0 ms`。这是一个 in-flight 请求乘正式队列权重，不是测到的 TTFT。Full view 选出的 worker 每次都在模型 argmin 里。Cap 在 regret 为 2 ms 时不在。
- 平衡负载下 regret 全是 0。`rr=2` 和 `rr=3` 时选出的下标会变，因为 cap 看不见被 round-robin 指到的 worker，但 `T` 相同。
- B5、B6 的 harness regret 也是 2.0 ms，但当时引擎 running 是 `[1,1,1,1]`。这两条不算引擎确认。
- 负载计划在开跑前写死：8 个 96-token holder，worker 顺序 `[0,1,2,3,0,1,2,3]`，间隔 300 ms。跑完没有改，也没有丢掉 regret 为 0 的样本。这个顺序从 worker 0 开始，和 cap 保留的前两个 owner 对齐，所以早段会让 2 和 3 空闲。这是真实 in-flight，也是这次不能外推的原因。
- Fig. 6(b) 仍是 INCONCLUSIVE，保持关闭。Fig. 6(a)(c) 不重跑。F6-P0 的 16 ms 仍是 CONTROLLED，这次没有复现那个 `[8,8,0,8]`。

### 已知问题和证据限制

- 正 regret 只在到达斜坡上出现，负载差是 1 个请求，不是持续的服务倾斜。
- `choose` 不是 `T` 的 argmin。负载计数包括被 cap 藏住的 worker，因为 load 是本地计数，不是广告。
- `kv_cache_usage_perc` 在负载窗口里大约 0.001。这个 vLLM 的 free queue 含有未被引用的 prefix block，所以该表不能证明前缀还在或不在。驻留证据是 cached tokens 和 hit 计数。
- 没有为反事实 worker 再发请求。`actual_ttft_ms` 是空的。探测 TTFT 约 1.6 s，再探测约 95 ms，都是实际打到四张卡上的请求，不是另一条视图的 TTFT。
- 主动测量 14.7 s，预热波 6.7 s。KV cache 104,544 tokens。

### 路径

- 快照报告：`/home/byh/B02/analysis/fig6_reconstruction/f6_p1/F6_P1_OBSERVED_SNAPSHOT_REPORT.md`
- 反事实：`/home/byh/B02/analysis/fig6_reconstruction/f6_p1/F6_P1_ROUTING_COUNTERFACTUAL.md`
- 下一步全文：`/home/byh/B02/analysis/fig6_reconstruction/f6_p1/NEXT_ITERATION_RECOMMENDATION.md`
- 表：`/home/byh/B02/analysis/fig6_reconstruction/f6_p1/observed_worker_snapshots.csv`，`/home/byh/B02/analysis/fig6_reconstruction/f6_p1/counterfactual_routing.csv`
- 原始记录：`/home/byh/B02/analysis/fig6_reconstruction/f6_p1/raw_observations.json`
- 脚本：`/home/byh/B02/analysis/fig6_reconstruction/f6_p1/run_observed_snapshot.py`
- 服务器日志：`/home/byh/B02/analysis/fig6_reconstruction/f6_p1/server_logs/`（`*.log` 与 `*.pid` 被忽略，不要提交）

### 下一步

用户说「继续」时，不要画 Fig. 6，不要改论文，不要开 overlap 矩阵，不要开 Fig. 6(b)。先读 `NEXT_ITERATION_RECOMMENDATION.md`。

若只加一次测量：holder 启动顺序改成 `[3,2,1,0,3,2,1,0]`，广告顺序仍是 0、1、2、3。只保留 harness in-flight 等于 `num_requests_running` 的样本。同一条前缀，seed 1，full 与 `Krep=2` 离线比较。不要为了正 regret 改 cap。1 个 GPU cell，2 个离线视图。启动大约 1–2 分钟，测量控制在 2 分钟内。预热波超过 120 秒就停。不要把 `T` 写成 TTFT。这次不要自己开。

P0-C1 仍然有效，不要重跑。不要开 Dynamo 集成、64 卡、1024 replay、TTFT 矩阵、P0-B 的 2×2。

### 机器

可以安全停下。2026-10-19 00:08（UTC+8）刚查过：四张 T4 显存 0 MiB，利用率 0%。这次的四个 `vllm serve` 已停，8000–8003 没有在听。没有 `dynamo.indexer`，没有 `run_c1.py`，没有 `run_observed_snapshot.py` 残留。`127.0.0.1:9710` 仍是容器 `b02-gateway4t4`，没有重启。不要动 edgevisor、lhjj、gitlab。

### Git

当前分支 `f6-p1-observed-replica`。研究报告 commit `78fa6d3`，已经推到 `origin/f6-p1-observed-replica`。父提交是 `36f3bfa`（F6-P0 交接，分支 `f6-p0-fig6-feasibility`）。这次交接只改 `handsoff.md`。不要合并到 `main`，除非用户明确说。

F6-P0 仍在 `f6-p0-fig6-feasibility`（`36f3bfa`）。P0-C1 仍在 `p0-c1-real-indexer`（`fff14ff`）。P0-A3 `d978ad3`。P0-A4 `9010ce0`。P0-A1、P0-A2、`p0-b1-activation-audit` 保持原分支。

Dynamo 检出在 `/home/byh/Dynamo/dynamo`，commit `7a2a40906`。`_core.abi3.so` 指向 debug `lib_core.so`，旧文件是 `_core.abi3.so.pre-c1`。不要把 Dynamo 树提交进 B02。

不要提交、不要推送：`ICC_KV_实验结果_20261005.zip`、`analysis/icc_kv_export/`、`analysis/admission_overhead_4t4/`、`supplemental_20260922_cp_queue_delay/`、论文 PDF、`/tmp/icc_link_smoke/`、`/tmp/icc_link_smoke2/`、`analysis/fig6_reconstruction/f6_p1/server_logs/`。`analysis/icc_kv/` 在 `.gitignore`。`*.log` 已被忽略。

### 仍然有效的旧约束

- 四卡自然负载不能写成顶满 10 Mbit。10 Mbit 路径实测排空约 9408 frame/s。
- `offered_wire_bit_per_s` 只乘 UPSERT，104 字节是记账约定。P0-C1 的 38 kbit/s 是实测 msgpack，TCP 载荷 NOT_MEASURED。
- fan-in 的 N=32/64 不是 GPU 数。
- `kv_cache_usage_perc` 在驱逐时可以仍是 0。驱逐证据是 `BlockRemoved`。未引用的 prefix block 也会让这个表接近 0。
- 前缀 tombstone 会藏住部分祖先。P0-C1 有 1 条：广告 1040，留下 448。
