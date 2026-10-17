# P0-B1 Activation Audit

Pressure-Triggered Filtering：历史数据有效性审计。

日期：2026-10-17。没有改网关算法，没有改阈值，没有改 Run A / Run B 的代码或结果，没有跑 Phase 2 smoke，没有做 2×2。

证据分成四类，后文用这四个词：

- **历史实测**：单元格或网关事件里读到的数。
- **代码推断**：当时提交里的控制流，用来解释计数器含义。
- **未记录**：字段不存在，不填 0。
- **未验证**：这次没有新实验。

逐格分类在 `analysis/p0_filtering_ablation/activation_cells.csv`（40 个 formal4t4 Adaptive 格 + 8 个 10 Mbit Run B Adaptive 格）。

## 结论

1. 论文主实验 formal4t4 的 Reuse-intensive 上，效用准入确实删过更新，但删的全是 1024-token。Original-compatible 上，效用准入一次都没有删过新更新；那里的 `relay_drop_low_utility` 是动态缩容把已入队帧丢掉时的重复计数。
2. 固定 10 Mbit 的 Run B 里，Adaptive 与 AdaptiveNoPriority 的 `congested_fraction`、`congested_entries`、`low_utility` 都是实测 0。Run A 没有 Adaptive。Top-K=16 把队列压在大约 18 帧，同一矩阵里 FullSync 的队列中位数到 5.7×10⁶ 帧。
3. 161 ± 85 ms 是 Reuse-intensive 上 Adaptive 相对 StaticSemantic 的 20 对 TTFT 差的均值。StaticSemantic 的 `gateway_global_topk` 是 0。这个差同时包含始终开启的 K=16、拥塞时缩到 4，以及效用准入。不能写成 Utility Filtering 的独立收益。
4. 不需要为了回答「有没有被激活」再开 smoke。不建议接着做 10 Mbit 上的完整 2×2。

## A. 历史实验索引

两套设置分开，不合并统计。

| 实验 | 目录 | Commit | 环境 | 与本文的关系 |
|---|---|---|---|---|
| formal4t4 baseline | `analysis/formal4t4/summary/cells_baseline_{original,reuse}_20260726.csv`，事件在 `analysis/formal4t4/raw/baseline/gateway_events_baseline_*.csv` | 单元格 `code_commit` 均为 `1dc24ecf1fda` | 4×T4，Qwen2.5-1.5B，可变 HTB `Cs = L/ρ`，ρ∈{0.5,0.8,1.0,1.2}，5 次配对，120 条测量请求 | 论文 IV-B / IV-C 主实验。250/250 格 `VALID`。Adaptive 40 格全部有效 |
| ICC Run A | `analysis/icc_kv/formal_A_5d2d300/` | 摘要顶层 `5d2d3008502e`，`git_dirty_files=[]`，`fixed_link_bit_s=10000000` | 固定 10 Mbit，600 s 口径的正式块 | main / ksweep / overlap。方法里没有 Adaptive。`formal_A_2b12640` 是作废的部分 Run A，本文不引用 |
| ICC Run B ablation | `analysis/icc_kv/formal_B_5d2d300/ablation_base/e2e_summary.json` | 同上 `5d2d300`，10 Mbit，干净树 | base 负载，ultrahigh 与 burst，seed 0 和 1 | 唯一一份带分离后的 STATS2、且含 Adaptive 的正式 10 Mbit 结果 |
| 2026-10-07 smoke | `/tmp/icc_link_smoke2/e2e_summary.json` | 摘要 `commit` 为空。当时交接记录 smoke 跑在提交前，容器源码哈希 `6ed88fbebeee` | 10 Mbit，ultrahigh，每格 20 条，容量 8000 | 短 smoke，不是正式格。只作机制旁证 |
| 更早的 1 Gbit 跑 | `analysis/icc_kv/e2e_full/`、`ablation_c13000/` | `61864db97691`，`fixed_link_bit_s=1000000000` | 1 Gbit 天花板 | 不是论文的 10 Mbit 设置。当时 `trim_global_topk` 仍把缩容删除累加进 `low_utility`。那份百万级 `low_utility` 不能当作效用过滤 |

formal4t4 网关启动命令在 `1dc24ec` 的 `setup_net_4t4.sh` 里不传 `--gate`、`--tau`、`--util-lambda`、`--adaptive-queue-gate`。当时源码默认值是 τ=30 s，λ=16，延迟门 2.0 s，队列门 8 帧，没有 0.2 s 进入保持。`congested()` 在该提交是即时判断：`adaptive and (ewma_dq > 2 or len(queue) >= 8)`。配置帧里 Adaptive 是 `global_topk=16`，StaticSemantic 是 `global_topk=0`。单元格里 Adaptive 的 `gateway_global_topk=16`，`max_queue_relay=4096`，`relay_max_inflight=4`。

10 Mbit 正式跑用的是后来的网关：`--gate 2 --adaptive-queue-gate 8 --congestion-hold 0.2 --tau 30 --util-lambda 16`，并且 `low_utility` 与 `global_topk` 已经分开计数。

## B. 触发路径

`adaptive` 为假时 `congested()` 直接返回假。`low_utility()` 和动态 K 都读 `congested()`。正常时 K=16；`congested()` 为真时 K=`global_topk // 4`，16 时就是 4。

队列深度是准入之后的活队列。Top-K 先丢掉进不了集合的更新，再看队列。所以 K=16 本身会拆掉「队列在 8 帧以上持续停留」这条触发路径。

延迟路径不一样。formal4t4 把链路速率设成到达率除以 ρ，已经入队的帧可以在网关里等数秒。EWMA 用的是 ACK 时间减生成时间，队列等待本身就能超过 2 s。bfifo 只有 64 KiB，纯链路排队大约 52 ms，过不了 2 s 门；能过门的是预队列等待。

`1dc24ec` 还有一个计数污染，属于代码事实：`trim_global_topk()` 在丢掉已入队帧时同时执行 `drops["global_topk"] += 1` 和 `drops["low_utility"] += 1`，事件原因写成 `low_utility`。Harness 只保存了 STATS 的 `relay_drop_low_utility`，没有 STATS2。因此 formal4t4 的这个计数器 **不是** 效用准入次数。本文用网关事件把两种删除拆开：

- 从未 `enqueued` 就 `suppressed/low_utility`：入队前的效用准入。
- 先 `enqueued` 再 `suppressed/low_utility`：缩容驱逐，被误记成 low_utility。

`congested_entries`、`congested_ms`、`congested_fraction`、ACK EWMA 在 formal4t4 里是 **NOT_RECORDED**。下面不把它们写成 0。压力是否打开，由「这些删除只有 `congested()` 为真才会发生」加上已转发帧的最大排队等待来推断。

## C. formal4t4：40 个 Adaptive 格

40/40 格事件都能对上单元格，`stats_low_utility = direct + trim`。`relay_max_queue >= 8` 的只有 2 格（Reuse ρ=1.0 与 1.2 的 rep 2，都是 8）。队列门在这套数据里几乎没有站住。

| 负载 | ρ | 效用准入（从未入队） | 缩容驱逐（已入队后删） | 最大转发排队 | 判定 |
|---|---:|---:|---:|---:|---|
| original | 0.5 | 0 / 5 格 | 0 | 四格约 0.002 s，rep 4 为 2.268 s | Case 1。动作没有发生。rep 4 的等待刚过 2 s，其后没有删除 |
| original | 0.8 | 0 | 72（2048: 62，2560: 10） | 3.2–7.2 s | Case 2。压力在，效用准入没删新更新；缩容删了已入队的长前缀 |
| original | 1.0 | 0 | 138（2048: 122，2560: 16） | 4.5–9.7 s | 同上 |
| original | 1.2 | 0 | 155（2048: 141，2560: 14） | 5.4–10.5 s | 同上 |
| reuse | 0.5 | 117，全部 1024 | 28（1024: 19，2048: 9） | 3.8–10.3 s | Case 4 |
| reuse | 0.8 | 231，全部 1024 | 41（1024: 15，2048: 26） | 8.0–16.4 s | Case 4 |
| reuse | 1.0 | 289，全部 1024 | 78（1024: 5，2048: 73） | 7.9–11.7 s | Case 4 |
| reuse | 1.2 | 305，全部 1024 | 111（1024: 6，2048: 105） | 7.3–14.6 s | Case 4 |

Reuse 上的效用准入是历史实测。零龄零延迟时 `U = coverage − 1024`，coverage=1024 时 U=0，代码 `<= 0` 就丢。所以门一旦打开，1024-token 更新与年龄无关，全部被丢掉。2048 和 4096 的入队前效用删除次数是 0。2048 要 `age+Dq` 约 20.8 s 才过不了门，这次入队前没发生。

这些 1024-token 更新也不是被 K=16 丢掉的。当时活队列中位数大约 6，小于 16，K=16 的集合没有满。它们是在入队前被效用式拒绝的。缩容（K=4）另删了一批已经入队的 1024 和 2048。两件事都发生了，不能算成同一次筛选。

Original 的前缀是 2048/2560，年轻时 U>0。那里没有入队前的效用删除。所谓 `low_utility` 全部是缩容误记。

ρ=0.5 的 Original 是唯一一组两种动作都是 0 的格子。最大队列 3，低于队列门。

## D. 固定 10 Mbit

Run A（`5d2d300`，干净树，10 Mbit）没有 Adaptive。同一矩阵里队列深度（中位数）：

| 场景 | FullSync max queue | StaticTopK16 max queue | StaticTopK16 的 global_topk 丢弃 |
|---|---:|---:|---:|
| xhigh (ρ=1.5) | 2.83×10⁶ | 18 | 2.47×10⁶ |
| ultrahigh (ρ=2.0) | 5.73×10⁶ | 18 | 4.99×10⁶ |
| burst | 2.19×10⁴ | 19 | 3.40×10⁵ |

这是历史实测：Coverage Top-K 把预队列从百万帧压到大约 18 帧。

Run B ablation 的 4 个 Adaptive 和 4 个 AdaptiveNoPriority（seed 0/1，ultrahigh 与 burst）全部是：

- `congested_fraction = 0`（OBSERVED_ZERO）
- `relay_congested_entries = 0`（OBSERVED_ZERO）
- `relay_congested_ms = 0`（OBSERVED_ZERO）
- `relay_drop_low_utility = 0`（OBSERVED_ZERO）
- `relay_drop_global_topk` 为 3.3×10⁵ 到 5.2×10⁶（OBSERVED_POSITIVE）
- `relay_max_queue` 为 16–19

ACK EWMA 仍是 NOT_RECORDED。判定是 Case 1：压力锁存没有发生，效用过滤没有删除。`low_utility=0` 在这里与 `congested_fraction=0` 一致，但两者是分开的字段，不是互相代替。

最大队列 16–19 说明深度曾经超过 8。锁存没发生，是因为 0.2 s 的进入保持没有被维持住：K=16 把队列排空的速度快于这个保持。这是 Top-K 抑制压力检测，不是检测器没装上。StaticTopK16 的 `congested_*=0` 不能单独当证据，因为 `adaptive=0` 时 `congested()` 根本不更新锁存。证据是 Adaptive 自己的计数，加上同矩阵 FullSync 的深队列。

`/tmp/icc_link_smoke2` 的 Adaptive 格与此相同：`congested_fraction=0`，`low_utility=0`，`global_topk=462016`，`max_queue=18`。同一次 smoke 的 FullSync `max_queue=614970`。这是 20 条请求的短跑，只支持「同一机制在 ultrahigh 上不锁存」，不替代 Run B。

`e2e_full` 和 `ablation_c13000` 是 1 Gbit、`61864db`。它们的 `low_utility` 达到 10⁵–10⁶，但该提交的缩容路径仍把删除累加进 `low_utility`，且没有 `congested_fraction`。标记为计数不可分，不进入结论。

## E. 161 ± 85 ms

来源：`analysis/formal4t4/summary/paired_results.csv`。比较是 Reuse-intensive 上 StaticSemantic 减 Adaptive 的 `delta_ttft_ms_baseline_minus_adaptive`，5 个 rep × 4 个 ρ，共 20 对。

- 20 对均值 **161.0 ms**。
- 样本标准差 193.3 ms，标准误 43.2 ms。`1.96 × 43.2 = 84.7`，与论文的 ±85 一致。
- 自助 95% 区间（10000 次，种子 0）是 [79.2, 244.3] ms，下界大于 0。这只说明联合策略的 TTFT 差在这 20 对上为正，不说明效用过滤单独为正。
- 按 ρ 的均值：0.5 为 −2.0 ms，0.8 为 196 ms，1.0 为 195 ms，1.2 为 255 ms。161 不是每个压力上都有的差。
- Original-compatible 的同样 20 对均值是 −25.2 ms，自助区间 [−75.0, 24.2] 包含 0。

参与比较的 Adaptive 打开了 K=16、动态缩到 4、以及效用准入。StaticSemantic 的 top-k 是 0。Reuse 上效用准入确实删过 1024-token 更新，所以不能说过滤完全没运行。现有配对没有「只开效用、不缩容、且对方也有 K=16」的格子，因此隔离不了效用的独立贡献。

**161 ± 85 ms 不能归因于 Pressure-Triggered Utility Filtering。**

可替换的英文句子见文末。

## F. 九个问题

**Q1.** 能。10 Mbit 正式矩阵里，StaticTopK16 / Adaptive 的最大队列大约 18 帧，FullSync / StaticSemantic 在 ultrahigh 上是数百万帧。formal4t4 上 Adaptive 的最大队列中位数也大约 6，40 格里只有 2 格达到 8。历史实测。

**Q2.** 在 10 Mbit 上，能。队列门是 8 帧保持 0.2 s，Top-K 使这个保持达不到。Run B 的 Adaptive 锁存次数是实测 0。formal4t4 的可变速率不一样：队列门几乎没触发，但已转发帧等待达到 3–16 s，延迟门打开了，所以过滤和缩容在 Reuse 与 Original 的 ρ≥0.8 上仍然动作了。两套设置不能写成同一种抑制。历史实测加代码推断。

**Q3.** 有。formal4t4 Reuse：效用准入和缩容都发生了。formal4t4 Original 的 ρ≥0.8：只有缩容，没有入队前效用删除。10 Mbit Run B：两者都没有发生。历史实测。

**Q4.** 只在 formal4t4 Reuse 上改变了准入：1024-token 更新在入队前被拒绝，当时 K=16 的队列并没有满。Original 上效用准入没有改变新更新的去留。10 Mbit 上没有改变。历史实测。

**Q5.** 部分重合，不是完全重合。1024-token 在压力打开后会被效用式无条件拒绝，这和 Top-K 偏好长前缀是同一方向。但 K=16 当时没有满，这些帧不是被 K=16 拒绝的。缩容到 4 会再删掉已经入队的 2048。没有「关掉效用、保留同样压力」的对照格，重合比例未验证。

**Q6.** 不充分。10 Mbit 正式结果里的收益来自 Coverage Top-K，过滤没有被激活。可变带宽实验里过滤在 Reuse 上激活过，但论文没有把它和 Top-K、缩容分开。

**Q7.** 161 ± 85 ms 是 Reuse-intensive 上完整 Adaptive 相对没有 Top-K 的 StaticSemantic 的联合 TTFT 差。里面有 K=16、K→4、以及只针对 1024-token 的效用拒绝。ρ=0.5 的分项均值约 0。Original 上同一对比不是正的。

**Q8.** 不值得做 10 Mbit 上的完整 2×2。那里过滤没有被激活，再跑长矩阵只会重复 Case 1。若以后要拆 161 ms，唯一还有信息的设置是 formal4t4 的 Reuse-intensive、ρ∈{0.8, 1.0, 1.2}。那不是这一轮的任务，也不要改门限去逼出正结果。

**Q9.** 实测到的、可以写进主结论的增益是 Coverage-Aware Bounded Admission。Pressure-Triggered Filtering 可以留在机制描述里，但不能再作为已测到的额外 TTFT 来源。固定容量一节论文已经把 KVSelect-TopK 写成 non-triggered，这和 Run A/B 一致，应保持。

## G. 论文句子

只替换，不建议加段。下面是建议，没有改 PDF。

现在的句子（IV，相对 StaticSemantic）：

> Compared with StaticSemantic, freshness-aware admission yields an additional 161 ± 85 ms TTFT reduction.

建议换成：

> On the reuse-intensive workload, full KVSelect lowers mean TTFT by 161 ± 85 ms relative to StaticSemantic (20 paired runs; ±1.96 SE). The contrast enables coverage top-K (K=16), pressure-time shrinking of that set to 4, and utility admission together. At ρ=0.5 the paired mean is about zero. Utility admission rejected only 1024-token updates; it rejected no 2048- or 4096-token update before enqueue. The same contrast on the original-compatible workload averages −25 ms and includes zero.

固定容量一节若还把「freshness filtering」写成已测收益，改成：

> Fixed-capacity results use non-triggered coverage top-K (StaticTopK16). On that 10 Mbit path, Adaptive recorded no congestion latch and no utility drop, while top-K held the gateway queue near 18 frames against multi-million-frame FIFO backlogs. The routing and TTFT gains in this section are from coverage-bounded admission.

摘要里如果把 adaptive freshness filtering 和 118–391 ms 写在同一个因果句里，把因果收窄到 selective admission under a bounded control budget，不要点名 freshness filtering 为已分离的 TTFT 来源。

## H. Phase 2

没有跑。Phase 1 已经能区分 Case 1、Case 2 和 Case 4，也能回答 161 ms 的归因。再开 smoke 不会改变「10 Mbit 上未锁存」或「Reuse 上效用只删了 1024-token」这两个实测结论。

## I. 未验证

- formal4t4 没有 `congested_fraction` 时间序列。压力打开是由删除路径和排队等待推断的，不是直接读到的锁存时间。
- 没有 UtilityOnly 与 ShrinkOnly，所以交互项和独立 ΔU、ΔK 都没有。
- 1 Gbit 的 `e2e_full` / `ablation_c13000` 计数分不开。
- 没有为这篇审计改网关或重跑任何格。
