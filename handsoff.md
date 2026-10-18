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

更新时间：2026-10-18 13:00（UTC+8）。P0-A4 离线核查写完。没有启动 vLLM。没有改 `experiments/`，没有改论文，没有改 P0-A3 的原始 trace。

### 正在做的事

P0-A4 用 A3 的 ZMQ trace 重算了前缀失效。报告在 `analysis/p0_pressure_realism/a4_invalidation_audit/P0_A4_INVALIDATION_AUDIT.md`。1.91/s 和 1.67/s 只计覆盖增长。灌满阶段 1138 条 `BlockRemoved` 收成 18 条前缀 tombstone，0.261/s。合计 1.928/s，ρ=0.000205。判定 Case A2。Case A 和 Option D 不变。P0-A 结束。不要自动改论文，不要开 GPU。

### 当前状态

停着。2026-10-18 01:30 这轮拉起的 GPU 0 vLLM 已停。`nvidia-smi` 四张 T4 显存 0，8000 没有在听。`127.0.0.1:9710` 上的网关容器还在，不要动 edgevisor、lhjj、gitlab。

### 下一步

P0-A 停在这里。用户明确要求改论文时，按 A2/A3/A4 的结论收窄 motivation：前缀级含失效仍约 2 条/s。不要把 `BlockRemoved`/s 写成 Invalidation/s。不要开 GPU、replay 或 64 卡。P0-B 的 2×2 也不要开。

### 已经定下来的约束

- 四卡自然负载不能写成顶满 10 Mbit。formal4t4 的 kbit/s 链路是 ρ 缩放，不是测到的物理容量。
- `offered_wire_bit_per_s` 只乘 UPSERT，104 字节是记账约定。
- fan-in 的 N=32/64 不是 GPU 数。ρ_ctrl≥1 还依赖 `MU=200` 和每秒全表刷新假设（`r_sync=21.8821`）。
- 353 block/s 是查询 token 除以 16，不是 `BlockStored`。单卡直接观测的新 hash 约 124/s，前缀覆盖约 1.9/s。
- `kv_cache_usage_perc` 在驱逐发生时仍是 0。驱逐证据是 `BlockRemoved`，再由回放重新计算确认。
- 热路径开/关 ZMQ 发布器的延迟都是 0.14 s，没有看到观测开销。

### Git

P0-A1 在 `p0-a1-pressure-realism`。P0-A2 在 `p0-a2-realism-gate`。P0-A3 在 `p0-a3-motivation-explore`（`d978ad3`）。P0-A4 在 `p0-a4-invalidation-audit`，从 A3 拉出。不要合并到 `main`，除非用户明确说。`p0-b1-activation-audit` 保持独立。

不要提交、不要推送：`ICC_KV_实验结果_20261005.zip`、`analysis/icc_kv_export/`、`analysis/admission_overhead_4t4/`、`supplemental_20260922_cp_queue_delay/`、论文 PDF、`/tmp/icc_link_smoke/`、`/tmp/icc_link_smoke2/`。`analysis/icc_kv/` 在 `.gitignore`。
