# PREREG：ICC KV 覆盖优先有界准入，正式实验预注册

> 本文件用于预先固定分析与剔除规则。仓库中的 `docs/icc_formal/PREREG.md` 是**空白模板，不要修改仓库内的这份**。**正式 Run A 开始前**，先复制一份（`cp docs/icc_formal/PREREG.md ~/icc_formal/PREREG.md`），在副本里填好所有 `<…>`，再原样放进每个结果目录（`<A_DIR>/PREREG.md`、`<B_DIR>/PREREG.md`），并记录它的 sha256（见 RUN_GUIDE §1.8）。开跑后，填好的副本**只能追加**（追加到 §12「开跑后记录」），不允许修改 §1–§11。
> 字段名与 CLI 均已对照 commit `103d9d0` 的源码核实（`experiments/icc_kv/{e2e,full_queue,path_capacity,capacity_window,runtime,wire}.py`）。`103d9d0` = `bc59945`（= `3991559` + ksweep 的 StaticTopK32）+ `capacity_window` 平台上限。cell / e2e 代码路径与 `3991559` 完全相同，验证 run 仍然有代表性。无法核实的条目标为 **未核实**。

## 0. 身份

| 项 | 值 |
|---|---|
| 预注册日期（UTC+8） | `<YYYY-MM-DD HH:MM>`（必须早于 Run A 第一个 cell 的时间） |
| 实验代码 sha（CODE_SHA） | `103d9d0`：`experiments/` 最后一次改动所在的 commit。`103d9d0` = `bc59945`（= `3991559` + ksweep 的 StaticTopK32）+ capacity_window 平台集 rho_row ≤ 1.5。cell / e2e 与 `3991559` 相同 |
| 运行时 HEAD（RUN_SHA） | `<RUN_SHA>`：开跑时的 `git rev-parse --short HEAD`，等于 summary 的 `commit` 字段。docs-only commit（加入 `docs/icc_formal/`）不改变实验代码，所以 RUN_SHA 可以与 CODE_SHA 不同 |
| `git diff --stat <CODE_SHA> <RUN_SHA> -- experiments` 为空 | `<是/否>`（必须为是） |
| `git rev-parse HEAD` 与 `origin/main` 一致 | `<是/否>` |
| capacity 文件 | `<CAP_FILE>`（绝对路径），sha256 `<CAP_SHA>` |
| C（`c_drain_per_s`） | `<C>` frames/s |
| C_ingress_min（`c_ingress_min_per_s`） | `<CIN>` frames/s |
| `rho_max_in_window` | `<RHOMAX>`（必须 ≥ 2.0/0.9 ≈ 2.22） |
| 到达率 AR | `<AR>` req/s（来源：`<校准 json 路径或验证 run 路径>`） |
| Run A 目录 | `<A_DIR>` |
| Run B 目录 | `<B_DIR>` |
| 操作者 | `<name>` |

## 1. 论文主张（claim）

**Coverage-priority bounded admission**：控制面过载时，网关只保留覆盖度（prefix 覆盖 token 数）最高的 k 条待发更新（StaticTopK，k=16 为主方法），并且 tombstone 优先、同 key 合并去重。与同样有界、但按 FIFO / 优先级 / 语义合并来准入的队列相比，它能在相同链路预算下显著减少路由假阴性（FN）和前台更新未送达，同时低负载下不损害性能。

- **主方法**：`StaticTopK16`（`StaticTopK` 是它的别名，wire 中为 `(4,1,1,0,2,16)`）。
- **Adaptive 是消融，不是主方法**：`Adaptive`=`(5,1,1,1,2,16)`，`AdaptiveNoPriority`。
- **k 敏感性属于主张的一部分**：k 必须覆盖同时活跃的有用条目。论文要报告 k 不足时的退化，以及「足够」的 k。
- **不主张**：K（实例数）扩展性、真实 trace 回放、CPU-bound 容量、owner validation/fallback（见 §11 偏差表）。

## 2. 固定配置（所有 block 都相同）

| 参数 | 值 | 代码中的位置 / 字段 |
|---|---|---|
| 链路 | 10 Mbit HTB，`--link-bit 10000000` | summary 顶层 `fixed_link_bit_s`、`tc_link_bit_s` |
| 网关 | 1 CPU（cpuset 0） | meta `gateway_cpus`=1，`gateway_cpuset`="0" |
| C | `<C>`，从 `capacity_window.json` 的 `c_drain_per_s` 读取（full_queue 只给 `--capacity-file` 时自动读取） | run_config.json `capacity`、`capacity_file` |
| burst 倍数 | `--burst-mult 1.5` → burst 平均名义 ρ = 0.9×(25+5×1.5)/30 = 0.975，峰值 1.35 | row `burst_mult`、`nominal_rho_effective` |
| 场景 ρ（RHOS） | normal 0.5 / near 0.9 / high 1.2 / xhigh 1.5 / ultrahigh 2.0 / burst 0.9（×m 峰值） | e2e `RHOS` |
| 到达率 | 单一值 `--arrival-rate <AR>`，Run A 与 Run B **相同**；不使用 `--gpu-rho`（它会在每个 out-dir 重新校准） | row `arrival_rate`、run_config `arrival_rate` |
| cell 时长 | `--cell-seconds 600`（每 cell 请求数 = max(warmup+1, ceil(600×AR))） | row `requests` |
| warmup | `--warmup-requests 10` | row `warmup_requests` |
| 并发 | 4（full_queue 固定传入 `--concurrency 4`） | row `concurrency` |
| KV cache | 104544 tokens（full_queue 固定传入） | — |
| 噪声 | `--noise-senders 8`、`--noise-workers 4`（默认）、`--invalidate-every 8`（默认） | row `noise_senders`、`noise_workers`、`invalidate_every` |
| 开环 | 是（full_queue 不传 `--closed-loop`） | row `open_loop` |

### 2.1 各 block 设置（full_queue `BLOCKS`，与 `bc59945` 相同；`103d9d0` 未改 full_queue）

| block | 场景 | 方法 | seeds（本次） | workload | 输出子目录 |
|---|---|---|---|---|---|
| main | xhigh, ultrahigh, burst | FullSync, RateFIFO, BoundedFIFO16, BoundedPrio16, BoundedSemantic16, StaticSemantic, StaticTopK16, Ideal | 3（Run A `--seeds 3`） | base：pool 16，有用前缀全部 4096，噪声 256 | `main_base` |
| ksweep | ultrahigh | StaticTopK4/8/16/32/64, BoundedSemantic4/16/64, Ideal | 3 | ov30 | `ksweep_ov30` |
| overlap | ultrahigh | StaticTopK16, BoundedSemantic16, BoundedPrio16, StaticSemantic, Ideal | 3 | ov0 / ov10 / ov30 / ov60 | `overlap_ov0` … `overlap_ov60` |
| ablation | ultrahigh, burst | StaticTopK16, StaticTopK16NoMerge, StaticTopK16NoDedup, StaticTopK16NoPriority, BoundedSemantic16, Adaptive, AdaptiveNoPriority | 2（Run B `--seeds 2`） | base | `ablation_base` |
| rhoscan | normal, near, high, xhigh, ultrahigh | FullSync, BoundedFIFO16, BoundedSemantic16, StaticTopK16, Ideal | 2 | base | `rhoscan_base` |

非 base workload 的参数：`--useful-pool 32 --useful-coverage-mix 1024:1,2048:1,4096:1`，噪声覆盖度分布如下：
ov0=`256:1`；ov10=`256:90,1024:4,2048:3,4096:3`；ov30=`256:70,1024:10,2048:10,4096:10`；ov60=`256:40,1024:20,2048:20,4096:20`。

StaticTopK32 是否纳入 ksweep：是（`bc59945` 相对 `3991559` 只改了 `full_queue.py` 中 ksweep methods 这一行；`git diff 3991559 bc59945 -- experiments` 应只显示这一行。`103d9d0` 不改这一行）。

seed 顺序：e2e 外层循环是 `for seed in range(N)`。cell 内方法顺序用 `Random(seed*1009+scenario_index*9176)` 打乱。Run A 用 seeds {0,1,2}，Run B 用 {0,1}。

## 3. 假设与证伪标准

记号：Δ_s(X) = metric(StaticTopK16) − metric(X)，在**同一 block / workload / scenario / seed** 上配对。FN 与未送达率都是越低越好，所以 Δ<0 表示 StaticTopK16 更好。「CI」指 §6 定义的 bootstrap 95% 区间。

**H1（主结果，过载）**：在 main block 的 xhigh 与 ultrahigh 上，StaticTopK16 的 `loose_false_negative_rate` 和前台未送达率 `foreground_undelivered/foreground_up_sent` 低于 BoundedSemantic16（H1a，主比较）、BoundedPrio16（H1b）、BoundedFIFO16（H1c）。
- 合并方式：xhigh 与 ultrahigh 合并为 n=6 对（3 seeds × 2 场景）。
- **支持**：FN 的 mean Δ ≤ −0.03，CI 上界 < 0，并且 ≥5/6 对 Δ<0。前台未送达率同样要求 CI 上界 < 0。
- **证伪**：CI 包含 0，或 mean Δ > −0.01，或 ≤3/6 对 Δ<0。H1a 被证伪时，论文不得把「覆盖优先」写成相对语义合并有界队列的优势。
- 结果介于支持与证伪之间（例如 −0.03 < mean Δ ≤ −0.01）时，写作「趋势」，不作为主张。

**H1-burst（次要）**：burst（m=1.5，平均名义 ρ 0.975，峰值 1.35）单独报告，n=3。只有 3/3 对 Δ<0 且 mean Δ ≤ −0.02 才写「突发下也成立」，否则写「突发下无显著差异」。不得为了 burst 去改 m。

**H2（机制归因）**：优势来自覆盖度排序，而不仅是 tombstone 优先或合并。
- 比较 StaticTopK16 与 BoundedSemantic16：两者合并/去重/优先级参数相同（`(4,1,1,0,2,16)` vs `(6,1,1,0,2,0)`），差别只在 top-k 按覆盖度准入，还是按 cap=16 FIFO 截断。
- **支持**：H1a 成立。
- **证伪**：|mean Δ| < 0.01，且 CI 包含 0。此时主张改写为「有界 + 合并/优先级」，不得声称覆盖排序是关键。
- ablation 中 StaticTopK16NoPriority / NoMerge / NoDedup 只作描述性报告（n=4：2 seeds × {ultrahigh, burst}），不作统计主张。

**H3（覆盖度重叠）**：overlap block（ultrahigh）中，StaticTopK16 相对 BoundedSemantic16 的优势在 ov0、ov10、ov30 都存在；ov60 时优势可以缩小，退化应先出现在低覆盖度（1024）类别。
- **支持**：ov0+ov10+ov30 合并 n=9 对，FN 的 CI 上界 < 0，且每个 ov 水平 ≥2/3 对 Δ<0。另外，在 `fn_rate_by_coverage` 中，StaticTopK16 从 ov30 到 ov60 的 FN 增量满足 "1024" 类 ≥ "4096" 类（描述性）。
- **证伪 / 限定**：ov10 上 StaticTopK16 的 FN 比 ov0 高 ≥0.05（均值），或 ov30 上相对 BoundedSemantic16 的 CI 包含 0。出现任一情况，论文都必须把主张限定为「噪声与有用更新覆盖度可分离时」。ov60 结果无论好坏都要报告。
- 一致性检查（非假设）：`overlap_ov30` 与 `ksweep_ov30` 中重复的 StaticTopK16 / BoundedSemantic16 / Ideal 应在 ±0.03 FN 内一致。不一致只报告，不合并。

**H4（k 敏感性）**：ksweep（ov30，ultrahigh）中：
- H4a：同一 k 下 StaticTopK_k 优于 BoundedSemantic_k，k∈{4,16,64}。**支持**：至少 2 个 k 的 mean Δ<0，且合并 n=9 的 CI 上界 < 0。**证伪**：≤1 个 k 满足。
- H4b：k 太小时 FN 升高。FN(StaticTopK4) − FN(StaticTopK64) 的 mean > 0，且 CI 下界 > 0。证伪则论文写「在测试范围内对 k 不敏感」，并删除「k 必须覆盖活跃条目」的表述。
- 报告 k*：使 StaticTopK_k 的 mean FN 与 StaticTopK64 相差 ≤0.02 的最小 k（描述性）。论文措辞：「k 需不小于同时活跃的有用更新数」，并给出 k*。不预先断言 FN 对 k 单调（验证 run 中 k4 与 k16 已不单调）。

**H5（低负载无损）**：rhoscan 的 normal（0.5）与 near（0.9）上，StaticTopK16 与 FullSync 比较：
- **支持**：每个 seed 都满足 |ΔFN| ≤ 0.02，`ttft_mean_ms` 比值在 [0.9, 1.1]，`prefill_tokens_mean` 比值在 [0.95, 1.05]。
- **证伪**：任一场景的两个 seed 中 StaticTopK16 的 FN 都比 FullSync 高 >0.02。n=2，只作描述性报告，不计算 CI。
- 健全性：normal 下 FullSync 的 FN ≤ 0.05，`relay_drop_global_topk` 对 StaticTopK16 应接近 0。若 FullSync FN > 0.05，先排查实现，再解释结果。

**H6（Adaptive 消融，诚实报告）**：ablation 中 Adaptive ≈ StaticTopK16，判据为 |mean ΔFN| ≤ 0.02（n=4，描述性），同时报告 `congested_fraction`。
- 若 Adaptive 明显更好（mean ΔFN ≤ −0.03，且 4/4 同号），必须在论文中如实写出，并讨论为何仍以 StaticTopK16 为主方法（简单、无需拥塞检测）。不得删去该结果。
- 若 Adaptive 的 `congested_fraction` 在过载下 ≈0（<0.05），要写明「拥塞检测未触发，所以 Adaptive 实际退化为无 top-k」。

**H7（安全性，硬约束）**：所有 cell 的 `stale_cache_hits` = 0；同时报告 `loose_false_positive_rate`。任一 cell >0 → 停止实验并排查（见 §5），不是简单剔除。

## 4. 指标

### 4.1 主要指标（主张只基于它们）

| 指标 | 字段（e2e summary row） |
|---|---|
| 路由假阴性率 | `loose_false_negative_rate` |
| 前台更新未送达率 | `foreground_undelivered / foreground_up_sent`（两个原始计数都报告） |
| 前台更新延迟 p95（删失版） | `foreground_censored_lag_p95_s`（同时给 `foreground_lag_p95_s`） |

### 4.2 次要指标

- 失效：`invalidate_censored_lag_p95_s`、`invalidate_lag_p95_s`、`invalidate_undelivered`、`tombs_sent`
- 安全：`loose_false_positive_rate`、`stale_cache_hits`
- 放置：`wrong_placement_rate`、`coverage_wrong_rate`、`coverage_regret_mean`、`coverage_shortfall_rate`
- 缓存与延迟：`prefill_tokens_mean`、`reused_tokens_mean`、`ttft_mean_ms`、`ttft_p95_ms`、`e2e_ttft_mean_ms`、`server_ttft_mean_ms`、`sched_delay_mean_ms`、`slo_violation_2s`、`hit_service_ttft_ms`
- 分覆盖度：`fn_rate_by_coverage`、`requests_by_coverage`
- 送达：`fn_delivery_lag_p95_s`、`hit_delivery_lag_p95_s`、`missing_age_p95_s`、`tomb_pending_p95_s`

### 4.3 健康 / 机制字段（只用于有效性与解释）

`failed_requests`、`stats2_missing`、`ledger_ingress_gap`、`ledger_balance_gap`、`measured_rho`、`nominal_rho_effective`、`gateway_cpu`、`congested_fraction`、`noise_window_s`、`offered_noise_per_s`、`forwarded_per_s`、`relay_drop_global_topk`、`relay_drop_queue_drop`、`relay_max_queue`、`relay_drop_superseded`、`relay_congested_ms`、`rpc_stale_replies`、`loop_lag_p95_ms`、`capacity_window`（含 `problems`）。

### 4.4 Ideal 的定位

`Ideal` 是**零延迟视图参考**（调度器直接看到真实状态），**不是性能上界**：
- 它的 TTFT / prefill 可能比 StaticTopK16 更差，例如验证 run 中 Ideal prefill 为 529，StaticTopK16 为 500（未核实，来自用户的验证 run）。
- 论文中写作 "zero-lag reference"，不写 "upper bound / oracle best"。

## 5. 有效 cell 判据（全部满足才有效）

| # | 条件 | 字段 |
|---|---|---|
| V1 | `failed_requests` == 0 | row |
| V2 | STATS2 存在：`stats2_missing` == 0，且没有 `stats_error` 键 | row |
| V3 | `ledger_ingress_gap` ≤ 0.005，且 `ledger_balance_gap` ≤ 0.005 | row |
| V4 | \|`measured_rho` / `nominal_rho_effective` − 1\| ≤ 0.10（burst 用 0.975） | row |
| V5 | `stale_cache_hits` == 0（违反时**停机**，见 H7） | row |
| V6 | `gateway_cpu` ≤ 0.85（所有方法，与 capacity_window 的接受条件一致；k=64 同样是 0.85，不再单独放宽） | row |
| V7 | 干净代码树：summary 顶层 `git_dirty_files` == [] | summary meta |
| V8 | summary 顶层 `commit` == `<RUN_SHA>`（运行时 HEAD，短 sha 前缀匹配），且 `git diff <CODE_SHA> <RUN_SHA> -- experiments` 为空（check_smoke `--code-sha` 自动检查） | summary meta + git |
| V9 | `fixed_link_bit_s` == `tc_link_bit_s` == 10000000 | summary meta |
| V10 | `noise_window_s` > 0，且 row `capacity_window.problems` == [] | row |

- 自动检查：在仓库根目录运行 `python3 docs/icc_formal/check_smoke.py SUMMARY --head <RUN_SHA> --code-sha <CODE_SHA> --repo . --link-bit 10000000 …`（覆盖 G1/G2/G3/G4/G8/G9/G10），详见 RUN_GUIDE §2.5。
- V6 的依据：容量测量中高 offer 下网关 CPU 为 0.69–0.81，因此在任何正式 cell 之前把门槛从 0.70 改为 0.85，与容量窗口一致。k=64 不再单独放宽。见 §11。

## 6. 失败处理

1. **只用 resume，不删除任何东西**：不删除、不改写结果目录中的任何文件，包括 `e2e_summary.json`、`cell_errors.log`、`requests_*.csv`、`runner.log`、`run_config.json`。
2. 失败的 cell（写入 `cell_errors.log`）会烧掉它的 cell id。用 `--resume` 以同一身份重跑时，e2e 只补跑不在 rows 中的 (seed, scenario, method)。
3. 已完成但无效（违反 V1–V10）的 cell **不重跑**，标记为无效并从配对中剔除（成对删除：剔除该 seed 下该比较的那一对），然后在报告中列出。
4. 某个 block 的无效 cell 超过 1/3 时，暂停对该 block 的解释并排查。修复后，用新 commit、新目录重跑**整个 block**，并作为偏差报告。不得挑选性保留。
5. 报告中必须给出计数：计划 cell 数、完成数、失败数（`cell_errors.log` 行数）、resume 补跑数、无效数（按 V# 分类）、最终用于分析的数量。

## 7. 统计

- 单位：seed 级配对差 Δ_s（同一 block / workload / scenario / seed）。每个 cell 本身就是一次 600 s 的测量，**不对请求做 bootstrap**。
- 区间：配对差的 percentile bootstrap 95% CI，**B = 10000 次重采样**，随机数种子 20261008，在 n 个 Δ 上有放回重采样，统计量为均值。
- 局限：n=3 时 bootstrap 的不同重采样只有 10 种，CI 很粗。因此除 CI 外**必须同时报告每个 seed 的值和同号计数**。合并方式只按 §3 预先声明的方式（例如 H1 合并 xhigh+ultrahigh），不得事后另选。
- 主比较只有 H1a 的 FN，其余比较是次要比较，不做多重比较校正，但一律报告。
- **不 p-hacking**：
  - 看过结果后**不加 seed**。`seeds_override` 在 `run_config.json` 身份中，resume 时代码会拒绝改变 seed 数。
  - 不换场景、不换 m、不换 k 集合、不换剔除阈值。
  - 唯一允许的追加运行是 §6 第 4 条（整 block 重跑，作为偏差报告）。
- 时间不足时，只能按 RUN_GUIDE §2.6 预先声明的削减顺序缩减。缩减必须在看结果之前决定，并记录在 §12。

## 8. 论文正文与附录

| 放正文 | 放附录 |
|---|---|
| 表：main block（xhigh/ultrahigh/burst × 8 方法），列出主要指标，以及 prefill、TTFT、FP | capacity window 表（`c_drain_per_s`、`c_ingress_per_s`、`rho_max_in_window`、`c_drain_over_theory`） |
| 图：k 敏感性（FN vs k，StaticTopK vs BoundedSemantic，ov30） | 健康表：每个 cell 的 gap、CPU、measured_rho、有效性 |
| 图：覆盖度重叠（FN vs ov 水平，并附 `fn_rate_by_coverage`） | 每个 seed 的原始值、全部次要指标 |
| 图：ρ 扫描（FN、TTFT vs ρ） | RateFIFO、StaticSemantic 的细节；NoMerge / NoDedup |
| 小表：消融（StaticTopK16 vs NoPriority / Adaptive / BoundedSemantic16） | Adaptive 的 `congested_fraction`；V6 为 0.85 的说明 |
| 图：burst 下逐请求 `delivery_lag_s` 随时间变化（取代 Word 中的 Fig 5 恢复时间曲线） | 偏差表（§11）、Word 计划映射、cell 计数（§6.5） |

## 9. 威胁与必须写入论文的说明

- 前台帧通过单独的 TCP 连接进入**同一个**网关队列。
- 调度器视图排除 worker ≥ 16（噪声实例），这是测量用的 oracle。
- 在 base workload 中，覆盖度把前台更新与噪声完全分离（4096 vs 256），这一威胁由 overlap block 处理。
- 单机、单网关、Qwen2.5-1.5B、4 个 vLLM 端点。

## 10. 预期（非假设，仅供对照）

来自用户的验证 run `topk_val_3991559`（ultrahigh，ov30，150 请求，1 seed，pool 24），**未核实**：
- FN：StaticTopK64 为 0，StaticTopK4 为 0.043，StaticTopK16 为 0.050，BoundedFIFO16 为 0.136，BoundedSemantic16 为 0.143，Ideal 为 0。

正式 run 的 pool 为 32，与验证 run（24）不同，因此不作为门槛。

## 11. 与 Word 计划的偏差

| Word 计划 | 实际 | 论文措辞建议（英文） |
|---|---|---|
| CPU-bound 容量 | 10 Mbit HTB 固定链路瓶颈（link-bound） | "We fix a 10 Mbit/s HTB-shaped control link so that the bottleneck is bandwidth, not gateway CPU (gateway pinned to one core, utilization ≤ 0.85)." |
| trace 回放 / K 扩展 | 合成噪声，不做 K 扩展 | "Background control traffic is synthetic; we do not claim scaling in the number of instances." |
| 使用 Exp 0 trace | 未使用；有用前缀为 Zipf(1.2) | "Useful prefixes follow a Zipf(1.2) popularity over a pool of 16 (32 in overlap/k-sweep)." |
| 容量 120 s × 3 次 | path_capacity 每点 30 s，跑 2 次，每次 `<n>` 个速率点 | "Capacity is the FullSync link-limited service rate over rows with rho_row ≤ 1.5, measured in two independent runs (spread ≤ 5%)." |
| ρ 0.5/0.8/1.0/1.2，5 seeds | 0.5/0.9/1.2/1.5/2.0，rhoscan 2 seeds | "ρ ∈ {0.5, 0.9, 1.2, 1.5, 2.0}; two seeds per point in the scan." |
| burst：平均 <1，测恢复时间 | m=1.5（平均 0.975，峰值 1.35）；无恢复 / 队列时间序列，改为逐请求 lag 随时间 | "Bursts raise offered load to 1.35 C during the last 5 s of every 30 s window (mean 0.975 C)."（已核实：`split_path.py:71` 中 `elapsed % 30 >= 25` 时 ×`burst_mult`） |
| e2e Normal / Near | main block 用 xhigh/ultrahigh/burst；normal/near 只在 rhoscan 中 | "Low-load behaviour is reported in the ρ scan." |
| ≥500 请求，24 warmup | 每 cell 600 s，ceil(600×`<AR>`) 请求，10 warmup | "Each cell runs 600 s open-loop (≈`<N>` requests, first 10 discarded)." |
| owner validation / fallback | 无；安全性靠带版本 salt 的缓存键加 `stale_cache_hits` | "Correctness does not rely on the control plane: cache keys are version-salted, and we observe zero stale hits." |
| 方法集 | StaticTopK 为主，Bounded* 为基线（有意改变） | "We compare against bounded queues with the same capacity k and FIFO, priority or semantic-merge admission." |
| 消融 | NoMerge / NoDedup / NoPriority / Adaptive（有意改变） | — |
| events / queue / views / resources 文件 | 不产出 | 附录说明产出的是 `e2e_summary.json` 和逐请求 CSV |
| Ideal 作为上界 | 零延迟参考 | "Ideal: a zero-lag view reference, not an upper bound on TTFT." |
| 每 seed 统计 | seed 级配对 bootstrap（B=10000） | "We report seed-paired differences with 95% bootstrap CIs (10,000 resamples)." |
| C 的定义 | FullSync 在 rho_row ≤ 1.5 的行上的链路受限服务率（任何正式结果之前决定） | "C is the FullSync link-limited service rate on rows with rho_row ≤ 1.5, fixed before any formal cell." |
| 测试床：ACK 与父类共用 | 网关发往发送端的 TCP ACK（sport 9710，HTB class 1:30）与 10 Mbit 父类共用，重度 ingress（rho_row≈2–3）下有效服务率降至约 0.65C；所有方法同样受影响，作为 limitation 报告 | "Gateway ACKs to the senders share the 10 Mbit HTB parent, so under heavy ingress (rho_row ≈ 2–3) the effective service rate falls to about 0.65 C. This hits every method equally and is reported as a limitation." |
| V6 CPU | 任何正式 cell 之前由 0.70 放宽到 0.85。原因：容量测量显示高 offer 下网关 CPU 为 0.69–0.81 | "The CPU validity gate is 0.85 for every method, relaxed from 0.70 before any formal cell because capacity runs showed gateway CPU 0.69–0.81 at high offer." |

## 12. 开跑后记录（只追加）

| 时间（UTC+8） | 事件 | 说明 |
|---|---|---|
| `<…>` | Run A 开始 | `<A_DIR>` |
