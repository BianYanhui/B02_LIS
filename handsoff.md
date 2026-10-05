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

更新时间：2026-10-06 00:52（UTC+8）。补强代码已写完，2 倍 smoke 已通过。没有实验进程。不要开正式矩阵，不要动 vLLM，不要启动 `overnight.py`。

### 正在做的事

用户要了结果汇总，并要一份可下载的表。正式数字已收进 `/home/byh/B02/ICC_KV_实验结果_20261005.zip`（2.3 MB），同源文件在 `analysis/icc_kv_export/`。四个工作簿：`01_说明.xlsx`、`02_端到端.xlsx`、`03_控制面.xlsx`、`04_消融.xlsx`。说明页写了结论、指标定义和不宜写进论文的说法。均值是 5 个 seed 的算术平均。

磁盘上留下的原始结果只有 C=13000、唯一短噪声这一代：

1. `analysis/icc_kv/e2e_full/`：120 格摘要加 120 个逐请求 CSV。成功请求 41996，失败 4。
2. `analysis/icc_kv/scale_c13000/scale_summary.json`：100 行，`workload=unique_noise`。
3. `analysis/icc_kv/burst_c13000/burst_summary.json`：20 行，`workload=unique_noise_burst`。
4. `analysis/icc_kv/ablation_c13000/`：40 格摘要加 40 个逐请求 CSV。成功请求 13995，失败 5。
5. `capacity_cscan.log`、`e2e_full.log`、`full_queue.log`。

已删掉的是另一代结果，不要再找：C=8000 的 `e2e`、`ablation`、`burst`、`scale_independent`、`scale_correlated`、`capacity`；修正前的 `e2e_c13000` 和 `e2e_noise`；被正式矩阵盖过的冒烟 `e2e_noise2`；中途退出的 `e2e_r2`；`e2e_ultrahigh_smoke`；64 前缀 `trace/`。

### 当前状态

没有 icc_kv 进程。队列 2026-10-05 07:42 以 exit 0 结束。2 倍端到端均值：Ideal 假阴性 0、prefill 241、服务端 TTFT 987 ms；Adaptive 0.7%、262、956 ms，队列 17；FullSync 3.3%、323、1058 ms；Static 5.0%、351、1105 ms。0.5–1.2 倍挤在一起。控制面 2 倍 Adaptive 长前缀 P95 0.036 秒，FullSync 0.99 秒，Static 1.15 秒。消融里 Adaptive 与 AdaptiveNoPriority 的 prefill 都是 266。

### 下一步

2026-10-06 用户转来一份对照 Word 方案的总评，B−（6/10）。用现有表核对后，这条总评成立，不要把它当成过分苛刻。核对数字：假阳性全部为 0；错放里 91.3% 的 coverage regret 是 0；0.9× 上 Adaptive 的 prefill 比 Ideal 高 18.7 token，5 个 seed 都没有更好；2 倍命中请求的客户端 TTFT，Adaptive 比 Ideal 低约 120 ms，同时 prefill 更高。控制面 FullSync 在 1.2×（15600/s）仍全部送达、长前缀 P95 约 2 ms，2× 实际送达约 20337/s；端到端 0.9× 队列已经到数万。所以 13000 不是两条路径共用的有效容量。

论文只能写过载和突发下相对 FullSync、Static 的队列、长前缀 lag、prefill 和超过 2 秒。不能写 Word 那条完整因果链、错放改善、接近 Ideal、0.9× 收益、unsafe reuse 为 0、真实 trace 或按 K 扩展。用户问过这些缺口能不能补。结论：能补，不是整套设计报废。统一 C、补 delivery lag、错放改成 regret>0、开环请求和绑核，都是测量问题。假阳性为 0 是因为 16×4096 小于单卡 104544，`ShadowCache` 不会淘汰，墓碑发不出来；`enqueue` 里 K_TOMB 走优先级队列，不经过 `low_utility`。补失效要在现有长短分工上加淘汰，不能退回 64 前缀 trace。owner validation 的拒绝计数目前没有。0.9× 没有收益可能补完仍然成立，不能调参把它做成正结果。补强已经进代码，默认开环、每 8 条请求淘汰一条已安装的长前缀、摘要里有前景延迟、失效延迟、`coverage_wrong_rate` 和 `stale_cache_hits`。墓碑仍走优先级队列。没有改 tau、效用系数或 HTB。

2026-10-06 的 smoke 在 `/tmp/icc_strengthen_smoke`，只作方向，不是论文数字：1 个 seed、40 条请求、开环每秒 1 条、ultrahigh。Ideal / Adaptive / FullSync 都完成，失败 0。Adaptive 的 prefill 2494 与 Ideal 相同，FullSync 3007。假阴性 0、0.05、0.225。覆盖错放 0、0.025、0.225。失效 P95 延迟：Adaptive 0.16 秒，FullSync 28.9 秒。队列 17 对 42.9 万。Adaptive 的 `stale_cache_hits` 是 0，FullSync 是 1。服务端 TTFT 三者都在 7.5–8.0 秒，开环排队等待是几十秒，这格不要拿 TTFT 当收益。

用户要求看过 smoke 再点头。点头之前不要开正式实验，也不要把这 40 条写进 `e2e_full`。

### 已经定下来的约束

- C=13000。ultrahigh 是 2.0 倍 = 26000/s，xhigh 是 1.5 倍 = 19500/s。HTB 天花板 1 Gbit，不按 ρ 改。Gateway `--cpus 1 --cpuset-cpus 0 --quiet`。tau 30，util-lambda 16，gate 2，adaptive-queue-gate 8，congestion-hold 0.2。没有 `--util-relative`。
- 背景噪声 coverage 256，digest 为 `blake2b(noise-{seq})`。请求 digest 字符串 `U0000`–`U0015`，coverage 4096。缓存盐 `icc-noise-{run_id}-{cell}`。
- `cac8270` 的背景副本上限豁免还在。top-k 对所有 instance 生效。
- 不要把 4 张 GPU 写成大规模系统。不要把 0.73–3.04 updates/s 和 84.5×10³ updates/s 写成同一个瓶颈。错放率不是 Adaptive 的收益指标。失效优先在这组消融里没有拉开差距。
- Python：`/home/byh/B02/poc/.venv/bin/python`。`analysis/icc_kv/` 在 `.gitignore`。

### Git

HEAD 是 `61864db`，本地 `main` 比 `origin/main` 超前 1，还没推送。用户没说就不要再 commit，不要 amend，不要推送。

未提交：`handsoff.md`，`experiments/icc_kv/burst.py`，`experiments/icc_kv/replay.py`，`experiments/icc_kv/overnight.py`，`experiments/icc_kv/full_queue.py`。压缩包和 `analysis/icc_kv_export/` 是这次整理出来的，也不要提交。

不要提交、不要推送：`Bare_Demo_of_IEEEtran_cls_for_IEEE_Journals.pdf`、`analysis/icc_kv/`、`analysis/admission_overhead_4t4/`、`supplemental_20260922_cp_queue_delay/`。
