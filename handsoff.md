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

更新时间：2026-10-18 17:05（UTC+8）。P0-C1 已完成并推送。没有正在跑的实验。没有改 `experiments/`，没有改论文。

### 正在做的事

P0-C1 停在 Case 1。单卡原始 ZMQ 事件的 msgpack 约 38 kbit/s。Dynamo debug 索引器在 64 倍回放（256 事件/s）排空后积压为 0。外部 hash 6525 对 6525。一条根前缀查询命中 1040/1040。前缀广告把 6615 次 hash 收成 102 条记录，但有 1 条部分驱逐留下 448 token，末端 tombstone 看不见这段祖先。报告：`/home/byh/B02/analysis/p0_pressure_realism/c1_real_indexer/P0_C1_ARCHITECTURE_AUDIT.md`。下一步说明：同目录 `NEXT_ITERATION_RECOMMENDATION.md`。

P0-A4 仍有效：1.91/s 和 1.67/s 只计覆盖增长。灌满阶段另有 18 条前缀 tombstone，合计 1.928/s。报告：`/home/byh/B02/analysis/p0_pressure_realism/a4_invalidation_audit/P0_A4_INVALIDATION_AUDIT.md`。

### 当前状态

已完成，停着。刚查过：四张 T4 显存 0 MiB，利用率 0%。没有 `vllm serve`，没有 `dynamo.indexer`，没有 `run_c1.py`。8000 和 5557 没有在听。`127.0.0.1:9710` 是容器 `b02-gateway4t4`，不要动。不要动 edgevisor、lhjj、gitlab。

### 下一步

用户说「继续」时，不要开实验。先读上面两份报告，等用户指定下一问。用户明确要求改论文时，只写已测到的两层：原生块事件存在，前缀广告少得多，索引器没有排队。不要把 64 倍回放写成 64 张 GPU。不要开 Dynamo 集成、64 卡、1024 replay 或 TTFT 矩阵。P0-B 的 2×2 也不要开。

### 已经定下来的约束

- 四卡自然负载不能写成顶满 10 Mbit。formal4t4 的 kbit/s 链路是 ρ 缩放，不是测到的物理容量。10 Mbit 路径实测排空约 9408 frame/s，不是索引器容量。
- `offered_wire_bit_per_s` 只乘 UPSERT，104 字节是记账约定。P0-C1 的 38 kbit/s 是实测 msgpack，TCP 载荷没抓到。
- fan-in 的 N=32/64 不是 GPU 数。ρ_ctrl≥1 还依赖 `MU=200` 和每秒全表刷新假设（`r_sync=21.8821`）。
- 353 block/s 是查询 token 除以 16，不是 `BlockStored`。P0-A3 单卡新 hash 约 124/s，前缀覆盖约 1.9/s。P0-C1 这次抓取的 hash 约 110/s，事件约 4.2/s。
- `kv_cache_usage_perc` 在驱逐发生时仍是 0。驱逐证据是 `BlockRemoved`。
- 热路径开/关 ZMQ 发布器的延迟都是 0.14 s，没有看到观测开销。
- 块级索引能留下部分祖先。前缀 tombstone 不能当成无损压缩。

### Git

当前分支 `p0-c1-real-indexer`，HEAD `1366d64`，与 `origin/p0-c1-real-indexer` 一致。这次交接只改 `handsoff.md`。P0-A1 在 `p0-a1-pressure-realism`。P0-A2 在 `p0-a2-realism-gate`。P0-A3 在 `p0-a3-motivation-explore`（`d978ad3`）。P0-A4 在 `p0-a4-invalidation-audit`（`9010ce0`）。不要合并到 `main`，除非用户明确说。`p0-b1-activation-audit` 保持独立。

Dynamo 检出在 `/home/byh/Dynamo/dynamo`，commit `7a2a40906`。`/home/byh/Dynamo/dynamo/lib/bindings/python/src/dynamo/_core.abi3.so` 是指向 debug `lib_core.so` 的符号链接，旧文件是同目录 `_core.abi3.so.pre-c1`。不要把 Dynamo 树提交进 B02。启动索引器需要 `LD_PRELOAD=/usr/lib/x86_64-linux-gnu/libstdc++.so.6`。

不要提交、不要推送：`ICC_KV_实验结果_20261005.zip`、`analysis/icc_kv_export/`、`analysis/admission_overhead_4t4/`、`supplemental_20260922_cp_queue_delay/`、论文 PDF、`/tmp/icc_link_smoke/`、`/tmp/icc_link_smoke2/`。`analysis/icc_kv/` 在 `.gitignore`。`*.log` 已被忽略，包括 `analysis/p0_pressure_realism/c1_real_indexer/raw/vllm_gpu0.log`。
