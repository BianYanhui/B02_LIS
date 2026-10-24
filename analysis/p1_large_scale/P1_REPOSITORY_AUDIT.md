# P1 Repository Audit

审计时间：2026-10-23。代码版本 `ea3b335`（已在 `origin/main` 的合并提交 `0036ee1` 里）。本文件只记录阅读结果和随后实验的隔离边界。正式 Gateway、Dispatcher、4×T4 脚本和历史结果都没有改。

## 可复用代码

| 路径 | 作用 | P1 怎么用 |
|---|---|---|
| `experiments/4t4/net/gateway_relay_4t4.py` | 正式 Gateway。`Relay.enqueue` / `_dequeue` 实现 FullSync、RateFIFO、LatestOnly、AgeCov-Greedy、StaticSemantic、StaticTopK、BoundedFIFO/Prio/Semantic | 进程内直接调用这些方法。不改源文件 |
| `experiments/icc_kv/wire.py` | 64 B 帧、`POLICIES`、`WIRE_BYTES=104` | 策略元组以这里和 `run_formal4t4.POLICY_DEFS` 为准。P1 不往这个表里加名字 |
| `experiments/icc_kv/runtime.py` | `Dispatcher.on_frame`：`(worker, digest) -> coverage`，后写覆盖先写；非 upsert 弹出该键 | 索引更新规则照抄。`ROUTABLE_BELOW=16` 不照搬，见下 |
| `experiments/icc_kv/e2e.py` `Placement` | 覆盖优先，平局看在途负载和轮转 | 请求打分用同一规则，推广到 N 个 worker |
| `experiments/4t4/run_formal4t4.py` | `make_trace`、`ShadowCache`、`digest64`、loose/strict 路由定义、104 B 记账 | 复用 trace 和 LRU shadow。不启动 vLLM，不碰 `9710` |
| `experiments/4t4/freeze_manifest.py` | reuse-intensive：120 请求、warmup 24、pool 64、α=1.2、35/40/25 的 1024/2048/4096、replica overlap 0.25 | 组内负载按这份冻结参数 |
| `experiments/icc_kv/capacity_window.py` | 10 Mbit/s 上 FullSync 排空 | 采用已测 `c_drain_per_s=9407.55`（`a8a07b7`，`analysis/icc_kv/pc10m_a8a07b7_r1`）。P1 不再改正式容器的 tc |
| `experiments/4t4/test_policies.py` | 账本恒等式 | 每轮结束用同一条恒等式检查 Relay |

论文里的 KVSelect，对应当前仓库的 **StaticTopK**：`mode=4, merge=1, priority=1, adaptive=0, dedup=2, global_topk=16`。Adaptive 是旧机制（效用门和拥塞时把 K 缩到 K/4），不是这一版论文算法。P1 的主对照不跑 Adaptive。

`wire.py` 没有 LatestOnly 和 AgeCov 这两个名字，但 Gateway 有模式 2 和 3。元组与 `run_formal4t4.POLICY_DEFS` 一致：LatestOnly `(2, merge=1)`，AgeCov-Greedy `(3, merge=0)`，按 `age_s * max(coverage,1) / 64` 从待发队列里选。

Bounded 系列在 `wire.py` 里按 K 生成：BoundedFIFO 只限队列长度；BoundedPrio 另加失效优先；BoundedSemantic 是合并 + 失效优先 + replica cap 2 + 队列长度 K。BoundedFIFO / BoundedPrio **没有** replica cap。这是仓库语义。P1 不给它们临时加上 replica cap。

## 和论文 Algorithm 1 的差异

Gateway 里 StaticTopK 实际做的事：

1. 同一 `(instance, digest)` 的未发送 UPSERT 只留最新一条（`_supersede_queued_up`）。
2. 失效帧进优先队列，非抢占（`pqueue`）。
3. 每个 digest 最多 `dedup` 个 holder。已满时，更大的 coverage 替换最弱 holder（coverage 小，其次 instance 小），并给被替换且已经发出去的 holder 补一条 tombstone。
4. 未发送集合里只留 coverage 最高的 K 个 digest。平局时更小的 digest 留下。拥塞锁存时 K 变成 `max(1, K/4)`，但 StaticTopK 的 `adaptive=0`，`congested()` 直接返回假，所以这条缩 K **不会**在 KVSelect 上触发。
5. 帧没有 version 字段。后到的 UPSERT 覆盖索引，包括更短的 coverage。这是已知缺口，P1 不修。

Dispatcher 索引是同步字典，不是论文里带版本号 `q` 的状态元组。P1 保持这个实现。

## 必须隔离、但不能改正式代码的两处

1. **`instance` 是 u8。** `gateway_relay_4t4.py` 的 `HDR = ">BBHIqQd"`。`instance > 255` 时 `pack` 会抛错。`wire.frame` 则是 `instance & 0xFF`，会静默撞车。4096 个 worker 不能走正式 64 B 头。P1 在实验进程里把模块全局 `HDR` / `frame` 换成同为 32 B 的 `">BHBIqQd"`（kind u8，instance u16，cell u8，其余不变）。磁盘上的 Gateway 文件不动。cell 固定为 1。这只加宽身份字段，不改 `enqueue` 的判断。
2. **`ROUTABLE_BELOW = 16`。** `runtime.Dispatcher.on_frame` 把 worker ≥ 16 计为噪声并丢弃，这是四卡回放把背景流量放在 16 号之后的测试台约定，不是 Algorithm 1。P1 的索引对 `0 .. N-1` 全部应用同一条赋值/弹出规则。worker 0–3 的单测要和正式 `on_frame` 一致。

虚拟链路代替了 Docker tc。服务速率锚定 10 Mbit/s 的实测排空 9407.55 frame/s，2 Mbit/s 和 5 Mbit/s 按这个比例线性缩放，并在校准报告里标明这是假设。bfifo 深度用正式脚本里的 65536 B，按 104 B 记账占用。Gateway 的准入和 `_dequeue` 选择是真实代码；TCP、`drain()` 和 HTB 分类不是本轮重新测量的。不连接 `127.0.0.1:9710`，不停 `b02-gateway4t4`。

`max_inflight` 用 `experiments/icc_kv/runtime.py` 的 0。2026-07 的 formal4t4 清单写的是 4。0 表示应用层窗口不额外限流，积压只来自虚拟 bfifo 的反压。RateFIFO 的令牌桶速率设成同一服务帧率，burst=1，与 reuse 那次正式运行的 `rate_burst_frames=1` 一致。

## 已有 Trace 里有什么

`analysis/formal4t4` 的 reuse-intensive 是 4 个真实 vLLM 上的请求轨迹，不是 4096 份拷贝。

- 有：Zipf 请求序、lineage id、phase、coverage、warmup 标记、25% overlap 预热（16 个 phase-0 前缀放到全部 4 个实例上）。
- 事件时刻：没有逐条控制面时间戳。单元格墙钟大约是 GPU 推理时间。reuse Ideal 的中位 upsert 速率是 1.071/s，184 条 upsert 对应约 172 s（`96/184 = 0.559/1.071`，见 `analysis/p0_pressure_realism/a1_baseline_audit`）。
- Eviction：`ShadowCache` 容量 104544 token（`run_metadata_baseline_reuse_20260726.json`）。正式 baseline 的 `churn_reset_every_waves=0`，没有强制 Restart。
- Extension：reuse 的长度是前缀档位，不是 original 那种 2048+512 步进。Extension 只在生命周期单测里出现，不写进主矩阵。
- 四卡自然负载远低于 10 Mbit/s。把 1.071 upsert/s 按 worker 数线性放大，4096 个逻辑 worker 大约 1.1×10³ frame/s，对 9407 frame/s 的 ρ 约 0.12。Experiment A 如果测到不拥塞，必须照实写。

P0-C1 的 native block trace 是另一个单引擎、另一种字节，不混进这条 reuse 负载。

## 规模模型（写死，跑完不改）

一个 **group** = 4 个逻辑 worker，复用一份 formal reuse 轨迹。

- `N ∈ {4,64,256,1024,4096}`，group 数 = N/4。
- 每个 group 有自己的 lineage 字符串（`G{g}-L{id}`），digest 用正式 `digest64`。不把同一个 lineage id 复制到所有 worker 上。
- group 内部的 25% overlap 与正式实验相同：16 个前缀播种到该 group 的 4 个 worker。跨 group 的副本率为 0。N=4 时这就是「播种到全部 4 个实例」。
- 请求的 residency 由 **开环** oracle 决定：在 group 的 4 个 worker 里按 Placement 规则选择，再走 `ShadowCache`。同一条事件流喂给每个策略。这和在线闭环不同；闭环会让 KV 事件本身随策略变化，没法满足「同一条 trace」。
- 打分时的路由器是全局的：看 Dispatcher 视图，覆盖优先。视图为空就在全部 N 个 worker 里做最小负载。因此视图缺失会选中一个并没有该前缀的 worker，会计入 routing FN。普通 cache miss（任何 worker 的 truth coverage 都 < 512）不计入 FN。
- 异构：group 数 > 1 时，每个 group 的波间隔乘以 Uniform(0.85, 1.15)，相位 Uniform(0, 一个波间隔)，种子固定。不做事后归一化。N=4 只有一个 group，尺度为 1，相位为 0。
- 波结构：16 个 overlap 波 + 30 个请求波，每波 4 条 upsert。波间隔 = (184/1.071)/46 s。波内 4 条间隔 10 ms。请求在波起点打分，状态在 +50 ms 发布。
- Restart 不在主 trace 里。生命周期单测单独覆盖 Create / Extension / Eviction / Restart。

## 新代码和数据流

目录是 `analysis/p1_large_scale/`，不放进 `analysis/icc_kv/`。后者被 `.gitignore` 排除。

```text
build_trace (formal make_trace × groups)
    → 开环 residency + 请求时刻
    → Relay.enqueue / Relay._dequeue     真实准入和出队选择
    → 虚拟 bfifo（9407.55 frame/s @ 10 Mbit/s 及其线性缩放）
    → 与 Dispatcher.on_frame 相同的索引赋值
    → 请求时刻读取 truth 与 view
    → runs/<id>/summary.json + routing_log.csv
```

入口：`analysis/p1_large_scale/run_p1.py`。绘图：`plot_fig8.py`。

Experiment A 的链路是 10 / 5 / 2 Mbit/s，不按 ρ 回调。Experiment B 的服务帧率 = 该 trace 的实测 offered frame/s / ρ，ρ ∈ {0.8, 1.0, 1.2}。Experiment C 预先规定用 ρ=1.2 的服务率，N ∈ {256,1024,4096}，K ∈ {16,32,64,128}。K=128 超出 `TOPK_SWEEP` 的生成表，但 Gateway 的 `global_topk` 就是一个整数，不需要改正式代码。

主对照：FullSync、RateFIFO、LatestOnly、AgeCov-Greedy、KVSelect。容量对照：BoundedFIFO、BoundedPrio、BoundedSemantic、KVSelect。种子 `2026102301..03`。校准另用正式种子 `2026072600`，不混进正式汇总。

## 延迟定义（开跑前写死）

Fig. 8(a) 用 **digest 级删失 P95**。一个 coverage ≥ 512 的前缀，从它第一次生成可用 UPSERT，到任意一个 owner 的可用 UPSERT 被索引应用。若到排空截止（最后事件后再等 5 s，已进入 bfifo 的帧会送完）仍然没有任何 owner 可见，延迟记为截止时刻减去生成时刻，不记成 0。这样丢掉唯一副本不会把 P95 变小。Replica cap 丢掉的多余 owner 不把这个前缀的延迟拉到截止时刻；那些 owner 级未送达记在 `upsert_last_undelivered`。只在已送达样本上算的 P95 另存为 `digest_p95_delivered_s`，不作为图的主值。

Routing FN 的分子是：truth 上存在 coverage ≥ 512 的 owner，但按 **Dispatcher 视图** 选出的 worker，其 truth coverage < 512。分母是去掉 warmup 的请求。truth 上谁都不到 512 的请求是 cache miss，不进分子。视图为空时在 `0 .. N-1` 上轮转，不看 ground truth。
