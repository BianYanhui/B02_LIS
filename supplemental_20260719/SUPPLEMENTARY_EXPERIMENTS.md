# B02 论文补充实验说明（2026-07-19）

> 论文：*Cost-Aware State Interfaces for LLM Request Dispatch*（Minimal State Sketch）
> 实验机：yhs1（4× Tesla T4 15GB，每卡一个 vLLM 0.10.2 实例，Qwen2.5-1.5B-Instruct）
> 代码仓库：`yhs1:/home/byh/B02`（commit `19fe277`）；补充实验目录 `supplemental_20260719/`

## 0. 为什么需要补充实验（缺口分析）

论文的四组核心证据（§4.2 状态成本与 admission 消融、§4.3 live K 扫描、§4.4 跨策略与
SLO、§4.5 控制面与 owner 验证）中，**live serving 证据（§4.3，Figure 4）全部来自单一
工作负载点**：96 个活跃前缀、Zipf α=0.55、2048-token 前缀、并发 4、J=4。由此产生三个
未被证据覆盖的缺口：

1. **缺口 G1：live 结论对工作负载参数的稳健性未知。** 论文 Limitations（§5）第一条即
   "Admission should adapt K and its ranking signal to demand concentration"，但目前
   没有任何 live 数据表明需求集中度（demand concentration）如何改变 K 的权衡曲线：
   "K=16 保留 89.1% 增量价值、K=4 有害（+44.4 ms）"这两个核心 live 结论都可能在
   不同的需求分布下不成立。审稿人最直接的质疑就是"single workload point"。
2. **缺口 G2：abstention（弃权）机制只有动机、没有实验。** §4.3 末尾由 K=4 有害得出
   "a bounded interface needs an abstention mode"，§3.6/Eq.10 的 net-benefit 守卫、
   §4.4 的 queue–affinity conflict 证据均为 **modeled dispatcher replay**（论文已注明
   不是 live 证据）。弃权机制在真实 serving 中是否有益、代价多大，完全没有量化。
3. **缺口 G3：live 队列敏感性未知。** live 扫描并发固定为 4；排队成本上升时
   affinity 收益是否存活（net-benefit 原则的 live 检验）未知。

本次补充实验即针对 G1–G3，全部复用论文冻结的 V5 live harness
（`supplemental_20260715/run_live_k_tradeoff_v5.py` + `run_fixed_prompt_t4_replay_v4.py`），
不改任何已有实验代码；S2 新增一个子类化 Dispatcher 的独立脚本。

**统一实验协议**（与论文 V5 primary 完全一致，保证可比性）：12 次配对重复；每次重复内
所有策略重放同一份字节级相同 trace；每个 (rep, policy) cell 用独立 `cache_salt` 隔离
缓存命名空间；192 请求/cell（64 预热 + 128 测量）；2048-token 前缀；固定 4 输出 token、
贪心解码（temperature=0, min_tokens=max_tokens, ignore_eos）；每次运行前重启全部
vLLM 实例以清空残留 KV。所有 cell 通过 harness 内置 sanity checks（跨策略 prompt
字节一致、输出长度一致、usage 遥测完整、fanout 不越界 J）后方纳入分析。

**指标口径**：`Rinc` 为论文 Eq. 11 的增量价值比 (S_policy − S_LoadOnly) /
(S_Exact − S_LoadOnly)，按 rep 计算后取 95% bootstrap CI；`ΔTTFT` 为与 Load-Only
的配对差（ms，负值为更快）；index bytes 为 dispatcher 侧索引状态字节数。物理缓存
token（vLLM `prompt_tokens_details.cached_tokens`）与 dispatcher 估计的 saved-prefill
分别报告，与论文口径一致。

## 1. 实验 S1：需求集中度（Zipf α）对 live K 权衡的影响

**(a) 补充论文哪部分数据**：§4.3 / Figure 4 的 live K 扫描（原文仅 α=0.55 一个点）；
同时直接回应 §5 Limitations 第一条（K 应随 demand concentration 调整——原文无任何
对应数据）。

**(b) 实验设计**：除 α 外一切参数与 primary 相同，α ∈ {0.05（近均匀）, 1.35（重偏斜）}，
K ∈ {4, 16, 32} + Load-Only + Exact，12 次配对重复。
（运行目录：`live_alpha_uniform_a005/`、`live_alpha_skew_a135/`）

**结果**（Rinc 按 Eq. 11；ΔTTFT 单位 ms，95% CI）：

| 策略 | α=0.05（均匀） | α=0.55（primary，已有） | α=1.35（偏斜） |
|---|---|---|---|
| Exact ΔTTFT | −354.5 [−394.9, −317.1] | −375.4 [−433.2, −319.0] | −64.7 [−77.4, −51.8] |
| K=4 Rinc | 0.074 [0.035, 0.113] | 0.036 [−0.005, 0.079] | 0.379 [0.305, 0.459] |
| K=4 ΔTTFT | **−30.5 [−48.9, −9.8]** | **+44.4 [+22.4, +66.5]** | +17.8 [−1.7, +36.3] |
| K=16 Rinc | 0.770 [0.738, 0.801] | 0.891 [0.864, 0.917] | **1.000 [1.000, 1.000]** |
| K=16 ΔTTFT | −246.7 [−278.4, −215.3] | −328.9 [−392.7, −266.2] | −63.6 [−76.5, −49.4] |
| K=32 Rinc | 1.000 | 1.000 | 1.000 |

**这些结果能说明什么（原论文说明不了的问题）**：

1. **"K=4 有害"不是普适结论，而是需求集中度相关的现象。** 均匀需求下 K=4 反而有益
   （−30.5 ms，CI 不含 0）；偏斜需求下 K=4 倾向有害（+17.8 ms，p95 ΔTTFT +48.0
   [+24.8, +74.1]）。机理：偏斜需求下热门前缀集中在少数 owner 上，小 K 广告把请求
   持续引向最热的实例，排队恶化吃掉 prefill 节省；均匀需求下候选分散，无此效应。
   这为 §5 "adaptive K / admission" 的 future work 提供了首个 live 量化依据。
2. **"K=16 足够"在高集中度下更强、在低集中度下减弱但仍主要成立。** K=16 的 Rinc 从
   α=1.35 的 1.000（与 Exact 无差别）降到 α=0.05 的 0.770；原论文 89.1% 的单点结论
   恰好位于两者之间。论文若只报单点，无法知道该数字对需求分布的敏感方向与幅度。
3. **Exact 相对 Load-Only 的总收益本身随集中度大幅变化**（−354.5 → −375.4 → −64.7 ms）。
   高偏斜下 Load-Only 借稳定放置获得的"偶然复用"已覆盖大部分收益，affinity 接口的
   增量价值收窄——这量化了论文 "Load-Only may still obtain cache reuse through stable
   hashing or accidental co-location"（§2.1）的边界条件。

## 2. 实验 S2：net-benefit 守卫与 abstention 的 live 消融

**(a) 补充论文哪部分数据**：§4.3 末尾 "a bounded interface needs an abstention mode"
的设计主张（原无实验）；§4.4 / Figure 5 的 queue–affinity conflict（原为 modeled
dispatcher replay）；§3.6 / Eq. 10 的 net-benefit 选择规则（原无 live 消融）。

**(b) 实验设计**：与 primary 相同的工作负载（α=0.55，并发 4），对比三种路由规则：
- `affinity_first`：只要最佳广告覆盖超过 native 实例即选择 affinity（无守卫、最大化
  命中率的行为）；
- `abstain`：仅当增量覆盖 ≥1024 token **且** affinity owner 不比 native 选择更忙时才
  使用 affinity，否则退回 native 负载决策（§4.3 所述弃权机制的朴素实现）；
- 锚点：Load-Only 与 Exact（Exact 沿用原 net-benefit 守卫）。

脚本：`run_live_guard_ablation_v1.py`（新增，子类化 V4 Dispatcher，不改原有代码）。
（运行目录：`live_guard_ablation/`）

**结果**：

| 策略 | Rinc | ΔTTFT mean (ms) | affinity 命中率 | abstain 率 |
|---|---|---|---|---|
| Exact | 1.000 | −276.6 [−315.6, −240.8] | 0.540 | — |
| K=4 affinity_first | 0.116 [0.075, 0.148] | −17.9 [−41.4, +7.4] | 0.103 | — |
| K=4 abstain | 0.026 [−0.037, 0.088] | −4.4 [−24.7, +17.2] | 0.044 | 2.1% |
| K=16 affinity_first | 0.855 [0.805, 0.900] | −231.3 [−267.0, −194.2] | 0.472 | — |
| K=16 abstain | **0.281 [0.220, 0.338]** | **−101.9 [−134.2, −72.3]** | 0.160 | 12.3% |

**这些结果能说明什么（原论文说明不了的问题）**：

1. **小 K 的不稳定性得到 live 复现与刻画。** primary 中 K=4 显著有害（+44.4 ms），
   本次同负载复跑中 K=4 affinity_first 仅为轻微负向且不显著（−17.9 [−41.4, +7.4]）。
   两次运行行为一致（命中率 0.109 vs 0.103）而结局相反——说明小 K 的损益本身对
   队列状态高度敏感，**正是"需要守卫/弃权"的最直接 live 证据**：没有守卫时小 K 的
   表现不可预测。
2. **朴素弃权机制的代价被首次量化：value-aware 守卫不可被 queue-blind 规则替代。**
   K=16 下启用"owner 更忙即弃权"的朴素规则后，Rinc 从 0.855 跌到 0.281（损失约
   2/3 增量价值），ΔTTFT 收益缩水过半。这说明论文 Eq. 10 的 net-benefit 形式
   （按节省量与排队代价的**净值**决策）是必要的；若按 §4.3 字面意思用"busy owner"
   触发弃权，会把大部分 affinity 收益一并弃掉。这为论文该句的修订提供了 live
   证据：弃权必须按净值而非按忙闲触发。
3. §4.4 中 modeled 的 "affinity hits 不能作为目标、覆盖必须摊销排队与验证开销"
   获得了 live 对应证据：K=16 abstain 组 12.3% 的请求触发了弃权，其中被弃的
   affinity 选择若执行将把请求推向更忙实例。

## 3. 实验 S3：并发（负载水平）对 live K=16 收益的敏感性

**(a) 补充论文哪部分数据**：§4.3 live 扫描（原仅并发 4 单点）与 §4.4 的
queue–affinity 权衡（原为 modeled replay）；另对 §4.3 "p95 不显著"的声明给出
一个更强负载下的新数据点。

**(b) 实验设计**：α=0.55，K ∈ {4, 16} + Load-Only + Exact，12 次配对重复。
并发档位的选择过程：原计划并发 12（每实例在途 ~3），但 4 次尝试均在前 2 分钟内
触发 EngineCore 崩溃（`CUBLAS_STATUS_EXECUTION_FAILED` / `illegal memory access`，
为该 vLLM 0.10.2 build 在 T4 上的已知不稳定问题，且崩溃后 `VLLM::EngineCore`
僵尸进程持有显存会污染后续重启——需显式清理）；改用并发 8（每实例在途 ~2，
`max-num-seqs=8` 内）后 48 cells 全部完成、零请求错误。
（运行目录：`live_concurrency8/`；失败尝试的日志见 `s3_driver.log`、
`s3_c8_driver.log` 与 `live_concurrency12.run.log`）

**结果**（并发 8，α=0.55；对照 primary 并发 4）：

| 策略 | 并发 4（primary） | 并发 8（S3） |
|---|---|---|
| Exact ΔTTFT mean | −375.4 [−433.2, −319.0] | −726.6 [−818.8, −643.5] |
| Exact ΔTTFT p95 | −6.8 [−58.9, +48.9]（不显著） | −341.4 [−550.4, −160.7]（显著） |
| K=16 Rinc | 0.891 [0.864, 0.917] | 0.874 [0.837, 0.909] |
| K=16 ΔTTFT mean | −328.9 [−392.7, −266.2] | −632.1 [−718.0, −554.3] |
| K=16 ΔTTFT p95 | +5.7 [−29.1, +42.5]（不显著） | −252.3 [−442.8, −103.6]（显著） |
| K=4 Rinc | 0.036 [−0.005, 0.079] | 0.061 [0.015, 0.113] |
| K=4 ΔTTFT mean | +44.4 [+22.4, +66.5]（有害） | −31.2 [−91.4, +17.6]（不显著） |

**这些结果能说明什么（原论文说明不了的问题）**：

1. **K=16 的收益在更重负载下保持且绝对值放大**（−328.9 → −632.1 ms），Rinc 稳定
   （0.891 → 0.874）。负载升高时一次 prefill miss 的排队代价更高，affinity 的
   净收益反而增大——为 §4.4 modeled 的 queue–affinity 权衡给出 live 方向的
   证据：在本负载范围内不存在"队列变忙 affinity 收益消失"的拐点。
2. **p95 结论获得一个显著数据点。** 论文明确声明 "we claim mean/median TTFT
   improvement, not a live p95 result"（并发 4、128 请求/cell 下 p95 CI 含 0）。
   并发 8 下 K=16 的 p95 ΔTTFT 为 −252.3 [−442.8, −103.6]，Exact 为
   −341.4 [−550.4, −160.7]，均显著——尾延迟收益在更重负载下显现，可作为
   论文修订时放宽 p95 表述的依据（仍需注明工作负载特定）。
3. **K=4 在三个不同负载/日期条件下三次表现互异**（primary +44.4 有害、S2 同日
   复跑 −17.9 不显著、S3 并发 8 下 −31.2 不显著）——再次印证 G2 的结论：小 K
   的损益对队列状态高度敏感，论文"K=4 有害"应表述为"K=4 不稳定、可能有害"。

## 4. 汇总数据与复现

- `analysis/combined_metrics.csv`：全部运行 × 策略的长表（Rinc、ΔTTFT mean/p50/p95、
  index bytes、命中率、弃权率，均含 95% bootstrap CI）。
- `analysis/combined_summary.md`：上述表格的 Markdown 版。
- 各运行目录内含：`*_cells.csv`（cell 级指标）、`*_pairs.csv`（配对差）、
  `*_summary.csv`、`*_sanity_checks.csv`（全部 PASS）、`*_raw.json`（请求级原始
  记录）、`traces/`（字节级 trace）、`run_metadata.json`（参数与计时）。
- 分析脚本：`analyze_supplements_v1.py`；S2 脚本：`run_live_guard_ablation_v1.py`；
  S1/S3 使用仓库原有 `run_live_k_tradeoff_v5.py`（未改动）。
- 复现命令见各 `run_metadata.json` 的 `arguments` 字段与本目录 `README.md`。

## 5. 附注：硬件故障与处理

S3 在并发 12 下连续 4 次触发 EngineCore 崩溃：首次为一台 T4 的
`CUBLAS_STATUS_EXECUTION_FAILED`，后续为 `illegal memory access`（该 vLLM
0.10.2 build 在 T4 上的已知不稳定问题，harness 注释中亦有记录）。`nvidia-smi`
无 ECC 错误、无 retired pages。排查发现两个叠加因素：(i) 并发 12（每实例在途
~3 个 2K-token 请求）触发该 build 的批量 GEMM/attention 不稳定；(ii) 崩溃后
`VLLM::EngineCore` 僵尸进程持有约 13 GB 显存且进程名被改写、不被原清理脚本的
`pgrep -f 'vllm serve'` 匹配，导致后续重启显存不足而连环失败（已通过显式清理
僵尸进程解决）。完全清理 + 重启 + 逐端点压测通过后，并发 12 仍复现崩溃，故 S3
改用并发 8 完成（48 cells、零错误）。并发 12 的失败运行未纳入任何分析，相关
日志保留在 `s3_driver.log` / `live_concurrency12.run.log` 备查。

## 6. 未覆盖事项（诚实边界）

- 本批补充仍限于 Qwen2.5-1.5B / T4 / 单主机回环，与论文 §4.5 已声明的范围一致；
  更大模型与跨机验证需要新硬件（超出本次约束）。
- SLO/burst 结论仍为 modeled replay（论文已注明）；本次未将其变为 live 实验，
  因为需要把 harness 改为到达时间驱动调度，工作量与风险超出本轮。
- live 工作负载仍为 Zipf 合成前缀；端到端 agentic 工具调用负载（§5 future work）
  未在本轮实现。
