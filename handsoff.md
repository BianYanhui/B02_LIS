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

更新时间：2026-10-17 17:40（UTC+8）。四条 motivation 补强路径在 4×T4 上探过。vLLM 已停，GPU 空闲。没有改 `experiments/`，没有改论文。

### 正在做的事

P0-A2 决策仍是 C。随后在空闲的四张 T4 上探过 motivation 还能不能从别的口径立住。记录在 `analysis/p0_pressure_realism/a3_motivation_explore/MOTIVATION_POSSIBILITIES.md`。vLLM 用现有 `restart_4t4.sh` 拉起（KV cache 104544 token，block 16），探针结束已停。四卡 unique prefill 的 prefix-cache 查询是 353 block/s（5,647 token/s）；单卡 220 block/s。按 104 字节记账约 0.3 Mbit/s，四倍单卡外推约 0.7 Mbit/s，仍低于 10 Mbit 排空（9408 frame/s），但高于写死的 200 frame/s。热缓存 1028 token 是 0.058 s，冷的是 0.360 s。缓存打满后早期前缀会变成 miss，正式负载打不满这块缓存。`kv_cache_usage_perc` 全程是 0，不能当驻留证据。

### 当前状态

停着。探针用的四台 vLLM 已按 pid 文件停掉。2026-10-17 17:40 `nvidia-smi` 四张 T4 显存 0。`127.0.0.1:9710` 上的网关容器还在，不要动 edgevisor、lhjj、gitlab。

### 下一步

停在 P0-A2 的决策 C。用户明确要求改论文表述时，再按报告第 7 节收窄 motivation，并附上 L_A 的 ρ 表。不要开原生事件测量，除非用户改口要保留“自然负载会拥塞”并接受事先写明的 K 判定线（C1、N=64、Original 为 284）。不要重跑四卡，不要加噪声，不要开 16/32/64 正式矩阵。P0-B 的 2×2 也不要开；那条审计在分支 `p0-b1-activation-audit`。

### 已经定下来的约束

- 四卡自然负载不能写成顶满 10 Mbit。formal4t4 的 kbit/s 链路是 ρ 缩放，不是测到的物理容量。
- `offered_wire_bit_per_s` 只乘 UPSERT，104 字节是记账约定。
- fan-in 的 N=32/64 不是 GPU 数。ρ_ctrl≥1 还依赖 `MU=200` 和每秒全表刷新假设（`r_sync=21.8821`）。
- 原生 block 查询测到过：四卡 unique prefill 约 353 block/s。正式基线的 shadow tombstone 仍是 0，因为工作集打不满 104544 token。缓存打满后的真实驱逐只在 2026-10-17 的探针里看到一次 miss，没有 tombstone 速率。

### Git

P0-A1 在 `p0-a1-pressure-realism`。P0-A2 在 `p0-a2-realism-gate`。这次探索在 `p0-a3-motivation-explore`，从 A2 拉出。不要合并到 `main`，除非用户明确说。`p0-b1-activation-audit` 保持独立。

不要提交、不要推送：`ICC_KV_实验结果_20261005.zip`、`analysis/icc_kv_export/`、`analysis/admission_overhead_4t4/`、`supplemental_20260922_cp_queue_delay/`、论文 PDF、`/tmp/icc_link_smoke/`、`/tmp/icc_link_smoke2/`。`analysis/icc_kv/` 在 `.gitignore`。
