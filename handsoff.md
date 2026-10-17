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

更新时间：2026-10-17 16:30（UTC+8）。P0-A1 历史审计写完。没有新 Cell，没有改 `experiments/`，没有改论文。

### 正在做的事

P0-A1：四卡真实 KV metadata 压力是否足以支撑控制面问题。结论写在 `analysis/p0_pressure_realism/a1_baseline_audit/P0_A1_BASELINE_AUDIT.md`。Ideal 基线：Original 中位 2.071 upsert/s（1.53–2.97），Reuse 中位 1.071 upsert/s（0.85–1.40），tombstone 为 0。全部是 `publish_state()` 在请求完成后发的 wrapper UPSERT。`sig_bit_per_s = Ideal offered / ρ`，大约 1–5 kbit/s，是按 ρ 设的 HTB。相对 10 Mbit 下约 9400 frame/s 的排空，自然 ρ 约 0.00022。下一轮选 Option B，不要自动开。

### 当前状态

停着。`127.0.0.1:8000–8003` 没有在听。网关容器 `b02-gateway4t4` Up 7 天，`127.0.0.1:9710` 在听。同机 edgevisor、lhjj、gitlab 不要动。本轮标过：短时验证不需要；vLLM 没起来，若硬要跑也是 `LIVE_VALIDATION_DEFERRED`。

### 下一步

用户明确说做 P0-A2 时，按 `NEXT_ITERATION_RECOMMENDATION.md`：用已测速率对一个事先写明来源的容量做 N/4 放大，算出 ρ=1 的逻辑 worker 数。不要重跑四卡，不要加噪声，不要开 16/32/64 正式矩阵。P0-B 的 2×2 也不要开；那条审计在分支 `p0-b1-activation-audit`。

### 已经定下来的约束

- 四卡自然负载不能写成顶满 10 Mbit。formal4t4 的 kbit/s 链路是 ρ 缩放，不是测到的物理容量。
- `offered_wire_bit_per_s` 只乘 UPSERT，104 字节是记账约定。
- fan-in 的 N=32/64 不是 GPU 数。ρ_ctrl≥1 还依赖 `MU=200` 和每秒全表刷新假设（`r_sync=21.8821`）。
- 原生 EVICT 没有观测。shadow tombstone 在基线里是 0。

### Git

审计在分支 `p0-a1-pressure-realism`，从 `main` 的 `c44ab85` 拉出。不要合并到 `main`，除非用户明确说。`p0-b1-activation-audit` 保持独立。

不要提交、不要推送：`ICC_KV_实验结果_20261005.zip`、`analysis/icc_kv_export/`、`analysis/admission_overhead_4t4/`、`supplemental_20260922_cp_queue_delay/`、论文 PDF、`/tmp/icc_link_smoke/`、`/tmp/icc_link_smoke2/`。`analysis/icc_kv/` 在 `.gitignore`。
