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

更新时间：2026-10-07 16:15（UTC+8）。Round 4b 里和现码对得上的准入、记账、链路回读已经提交并推送。短 smoke 已结束。没有实验进程。不要开正式矩阵，不要启动 `overnight.py` 或 `full_queue.py`。

### 正在做的事

对照 Round 4b 改了现码里确实存在、而且会让上一格 smoke 读错的部分。没有做容量标定、`full_queue` 改写、批量读写、P2 工作负载，也没有启用 B1b。

代码在 `c741937`。`experiments/4t4/net/gateway_relay_4t4.py`、`experiments/4t4/test_policies.py`、`experiments/icc_kv/wire.py`、`runtime.py`、`split_path.py`、`e2e.py`。中间区间不再清掉拥塞进入计时；深度不计幽灵帧；top-k 用引用计数；`K_STATS2` 带上 `global_topk` 和拥塞时间；子进程拿到真实链路速率并回读 tc；`prepare_fixed_gateway` 带 `--rebuild` 并核对 `/opt/gateway_relay_4t4.py` 的 sha256。新增 `StaticTopK`、`BoundedFIFO16`、`BoundedFIFO64`。单测 24/24 PASS，输出 `/tmp/icc_policy_checks.csv`。摘要里的 `commit` 仍是 `5010848`，因为 smoke 跑在提交之前。

### 当前状态

已完成，停着。没有 `experiments.icc_kv`、`overnight` 或 `full_queue` 进程。

Smoke：`/tmp/icc_link_smoke2/e2e_summary.json`，7 行，`failed_requests` 全是 0。容量 8000，场景 ultrahigh，每格 20 条，到达率 0.237，噪声发送端 8，`--link-bit 10000000`。摘要 `fixed_link_bit_s` 和 `tc_link_bit_s` 都是 10000000，`stats2_missing` 全是 0，`ledger_balance_gap` 和 `ledger_ingress_gap` 都约等于 0。`offered_noise_per_s` 约 14600，`forwarded_per_s` 约 7800–9500，`gateway_cpu` 约 0.51–0.65。摘要里的 `commit` 仍是 `5010848`，因为这次跑的是未提交工作区；容器内源码哈希和仓库文件一致，都是 `6ed88fbebeee`。

| 方法 | prefill | 假阴性 | 错放 | 前景 P95 | 前景送达 | 失效未送达 | 队列峰值 | global_topk 丢弃 | queue_drop | 命中 TTFT |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Ideal | 2702 | 0 | 0 | — | 本地 | 0 | 567k | 0 | 0 | 1330 ms（7） |
| FullSync | 3317 | 0.25 | 0.25 | 38.1 s | 9/20 | 1 | 615k | 0 | 0 | 2424 ms（3） |
| StaticSemantic | 3315 | 0.30 | 0.30 | 38.4 s | 7/20 | 0 | 564k | 0 | 0 | 4341 ms（3） |
| BoundedFIFO16 | 3110 | 0.25 | 0.15 | 16 ms | 12/20 | 3 | 16 | 0 | 581k | 2250 ms（3） |
| BoundedFIFO64 | 2907 | 0.15 | 0.15 | 22 ms | 11/20 | 1 | 64 | 0 | 569k | 1570 ms（5） |
| StaticTopK | 2497 | 0 | 0 | 12 ms | 20/20 | 0 | 18 | 490k | 0 | 1147 ms（8） |
| Adaptive | 2497 | 0 | 0 | 12 ms | 20/20 | 0 | 18 | 462k | 0 | 1234 ms（8） |

Adaptive 的 `congested_fraction`、`relay_congested_entries`、`relay_drop_low_utility` 都是 0。队列只有大约 16 帧，链路把它们排空的时间短于 0.2 s 的进入保持，所以拥塞没有锁住。这一格 Adaptive 和 StaticTopK 相同，都好于 FullSync / StaticSemantic。BoundedFIFO 把队列变短，但前景更新和墓碑会被尾丢，假阴性没有掉到 0。不要把 prefill 写成超过 Ideal 的服务时间优势，只有 20 条。上一次 `/tmp/icc_validate_smoke/e2e_b` 仍是单核打满的另一件事。`/tmp/icc_link_smoke/` 是改记账之前的 4 格，不要和这格混用。

机器：vLLM 仍是 127.0.0.1:8000–8003，pid `1375061`–`1375064`。四张 GPU 利用率 0，显存约 6.7 GiB / 15 GiB。网关容器 `6bbed5838299`（`b02-gateway4t4`，Up 12 minutes）听 `127.0.0.1:9710`，HTB 仍是 10 Mbit。9711 没有监听。没人要求就不要停 vLLM。下一次不带 `--link-bit` 的 `prepare_fixed_gateway()` 会把天花板改回 1 Gbit，并重建镜像。

### 下一步

用户说「继续」时，不要开正式矩阵，也不要重跑这格。拥塞没有锁住是测到的事实；B1b 会改方法本身，没有用户点头不要加。等用户看完 `c741937` 再决定论文主张怎么写。

### 已经定下来的约束

- 默认天花板仍是 1 Gbit，不要按 ρ 改 HTB、tau、效用系数。10 Mbit 只用于 `--link-bit 10000000`。Gateway `--cpus 1 --cpuset-cpus 0`。
- 背景噪声 coverage 256，请求前缀 `U0000`–`U0015` coverage 4096。`StaticTopK` 和 Adaptive 的 k 都是 16，等于 `USEFUL_POOL`。
- 不要把 4 张 GPU 写成大规模系统。不要把 0.73–3.04 updates/s 和 84.5×10³ updates/s 写成同一个瓶颈。
- 正式结果不要写回 `e2e_full`、`scale_c13000`、`burst_c13000`、`ablation_c13000`，也不要写进 `/tmp/icc_validate_smoke/` 或 `/tmp/icc_link_smoke/`。
- Python：`/home/byh/B02/poc/.venv/bin/python`。`analysis/icc_kv/` 在 `.gitignore`。

### Git

分支 `main`。代码 HEAD 是 `c741937`（Keep congestion armed across a shallow dip and count every top-k drop.）。上一笔是 `5010848`。不要 amend。

不要提交、不要推送：`ICC_KV_实验结果_20261005.zip`、`analysis/icc_kv_export/`、`analysis/admission_overhead_4t4/`、`supplemental_20260922_cp_queue_delay/`、论文 PDF、`/tmp/icc_link_smoke/`、`/tmp/icc_link_smoke2/`。
