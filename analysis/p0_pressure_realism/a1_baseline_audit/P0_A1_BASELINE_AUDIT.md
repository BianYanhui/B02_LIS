# P0-A1 Baseline Audit: Realistic KV Metadata Pressure

审计日期：2026-10-17。本轮只读历史数据。没有新 Cell，没有改 `experiments/`，没有改论文，没有改已有结果。

vLLM `127.0.0.1:8000–8003` 本轮没有在听。即便要做短时验证，也只能标 `LIVE_VALIDATION_DEFERRED`。历史 CSV 已经给出产生速率和链路设置，短时 Cell 也不会看到原生 KV 事件，所以没有启动 GPU。

本轮仓库 HEAD：`c44ab85`（`main`）。产生这些 Cell 的代码提交写在各 CSV 的 `code_commit` 里，见下。`experiments/` 相对冻结点 `48b64f4` 的 diff 在本轮开始时为空。

## 1. 数据从哪来

| 用途 | 文件 | 产生代码 |
|---|---|---|
| Original-compatible 基线，5 reps × 6 策略 + Ideal | `analysis/formal4t4/summary/cells_baseline_original_20260726.csv` | `experiments/4t4/run_formal4t4.py`，Ideal 的 rep 0 是 `57547bb975e2`，rep 1–4 是 `1dc24ecf1fda` |
| Reuse-intensive 基线 | `analysis/formal4t4/summary/cells_baseline_reuse_20260726.csv` | 同上，Ideal 五行都是 `1dc24ecf1fda` |
| 并发标定 1/2/4/8 | `analysis/formal4t4/summary/cells_calib_concurrency{1,2,4,8}_20260726.csv` | 各文件一个提交：`b4b6ff0dd512`、`066c38dc3076`、`25f353973a50`、`54659d0f52ac`。每种并发只有 1 次重复 |
| 请求落到哪张卡 | `analysis/formal4t4/raw/baseline/requests_baseline_{original,reuse}_20260726.csv` | 只有记入结果的请求，不含 warmup，不含 overlap seed |
| 逻辑 fan-in | `supplemental_20260920_controlplane/summary/anchor_L_of_N.csv`、`cells_calibrate.csv` | `analyze_controlplane.py`，校准 Cell 提交 `dffc55ccf89b` |
| 10 Mbit 下的排空 | `analysis/icc_kv/pc10m_a8a07b7_r1`、`pc10m_a8a07b7_r2` 的 `capacity_window.json` | `experiments/icc_kv/path_capacity.py`，发送方是合成流量 |

事件分类见 `event_provenance.csv`。速率见 `metadata_generation_baseline.csv`。容量见 `existing_capacity_evidence.csv`。

## 2. 事件从哪来

正式四卡基线里，能数到的 metadata 只有一类。

`publish_state()`（`experiments/4t4/run_formal4t4.py` 第 696–717 行）在一次真实 vLLM 请求返回之后发一条 `K_UP`。计数器 `upserts_generated` 加 1。摘要是 harness 的 lineage digest，长度是 harness 指定的 coverage。这是 Serving-Wrapper Advertisement。它依赖真实完成，但它不是 vLLM 内部的 KV block 分配或驱逐。函数注释写的是 “actual cache-state update”，这个说法超出了触发条件。

Reuse 基线还有 overlap seed：池子 64、overlap 0.25，即 16 个前缀，每个前缀打到全部 4 个 worker。`16 × 4 = 64`，加上 120 个 trace 请求（含 24 个 warmup），`upserts_generated = 184`。Original 基线 overlap 为 0，所以正好 120，等于 `n_requests`。

ShadowCache 的 LRU 驱逐在 token 总和超过 `kv_cache_tokens = 104544` 时才会产生 tombstone，而且只广告曾经发出去过的 digest。基线每一个非 Ideal Cell 的 `source_tombstones_sent` 都是 0。Ideal 路径不走网线，这个字段为空，按 0 计。104544 token 远大于单卡工作集，所以 shadow 没有发生驱逐。`churn_reset_every_waves = 0`，强制 reset 也没开。

原生 KV 生命周期事件在这个仓库里没有生成点。`poc/state_extraction/FINDINGS.md` 仍是空表。请求记录里的 `vllm_cached_tokens` 是响应上的真实复用遥测，不是一条生命周期消息。直接观察不到真实 EVICT。

不要算进四卡自然速率的还有两类合成流量：`experiments/icc_kv/e2e.py` 的 `offer_noise()`（worker id 从 16 起，按 `ρ × C` 发），以及 `supplemental_20260920_controlplane/run_controlplane.py` 的 `fanin_loop()`（`__fanin_*` 和 heartbeat）。`anchor_L_of_N.csv` 里 N=32、N=64 是公式外推，不是 32 张或 64 张 GPU。

## 3. 公式

时间窗 `T = cell_active_s`。它包住 overlap 预热和 warmup，不包请求循环结束之后的 drain。

```
R_upsert     = upserts_generated / cell_active_s          # 字段 upserts_per_s
R_tombstone  = source_tombstones_sent / cell_active_s
R_total      = (upserts_generated + source_tombstones_sent) / cell_active_s
```

带宽两套口径，都只乘 UPSERT 条数：

```
payload_bit/s = upserts_generated * 64 * 8 / cell_active_s
offered_wire_bit_per_s = upserts_generated * 104 * 8 / cell_active_s
```

104 是 `WIRE_BYTES_PER_MSG`（第 76 行）：64 字节 payload 加约 40 字节头部的记账约定，不是抓包字节。第 910 行的公式没有 tombstone。本基线 tombstone 为 0，所以两个口径在数值上重合；这不表示公式覆盖了全部生命周期消息。

阶段要分开：

| 阶段 | 字段 | 基线里是多少 |
|---|---|---|
| Generated | `upserts_generated` | Original 每 Cell 120；Reuse 每 Cell 184 |
| Emitted | `source_upserts_sent` | Ideal 为 0（本地 apply，不上线）；其余策略等于 generated |
| Forwarded | `relay_forwarded` | 随策略和 ρ 下降。FullSync 在 ρ≤0.8 时接近全部转发，ρ=1.2 时明显少于 generated |
| Delivered | 网关计数 | 本轮问题用的是产生速率，不把转发率当成产生率 |

`throughput_requests_per_s = request_count / cell_active_s`。分子不含 warmup。Original Ideal 的中位请求速率 1.657/s 与 upsert 速率 2.071/s 之比是 0.80，等于 96/120。Reuse 是 0.559/1.071 = 0.522，等于 96/184。差的是分子里的 warmup 和 overlap，不是另一套时钟。

链路容量：

```
sig_bit_per_s = max(64, int(Ideal 的 offered_wire_bit_per_s / rho))   # 第 685 行
```

Ideal Cell 先跑，第 1021–1022 行把该 rep 的 `offered_wire_bit_per_s` 存进 `rate_state`。后面的策略用这个数除以目标 ρ。所以 ρ 是设定值，不是测出来的负载比。

每 worker 的产生速率：Cell CSV 没有分 worker 计数。请求 CSV 只有记入结果的请求。Ideal 的 480 条（5 rep × 96）落在四张卡上大致均匀：Original 106/120/132/122，Reuse 120/125/109/126。warmup 和 64 条 overlap seed 不在这张表里。因此 `per_worker_upserts_per_s = NOT_AVAILABLE`。集群速率是可靠的；除以 4 只是近似。

## 4. 四卡自然产生速率

下面用 Ideal。Ideal 没有 tc，完成速率最接近 GPU 本身。同一 trace 上其他策略的 upsert 速率低一些，因为控制面排队拉长了完成时间；Original 非 Ideal 的中位大约 1.4–1.8/s，Reuse 大约 0.9–1.1/s。产生机制相同：一条完成，一条 UPSERT。

| 负载 | 集群 R_upsert | 范围（5 reps） | R_tombstone | payload 64 B | wire 约定 104 B | 中位 T |
|---|---:|---:|---:|---:|---:|---:|
| Original-compatible，并发 4，池 32，前缀 2048+512×step | 2.071/s | 1.533–2.967 | 0 | 1060 bit/s | 1723 bit/s | 57.9 s |
| Reuse-intensive，并发 4，池 64，1024/2048/4096 按 35/40/25，overlap 0.25 | 1.071/s | 0.847–1.403 | 0 | 549 bit/s | 891 bit/s | 171.7 s |

这 2.071/s 和 1.071/s 全部是 Wrapper Advertisement。其中 0 条是原生 KV 事件，0 条是 shadow tombstone，0 条是合成噪声。

并发标定是另一套更短的 Reuse（池 32，48 个请求，每种并发 1 次），不能和上面的池 64 基线拼成同一个工作点。Ideal 的集群速率：并发 1 为 1.894/s，并发 2 为 2.524/s，并发 4 为 3.005/s，并发 8 为 2.240/s。从 1 到 4 上升，到 8 回落。单次重复，只能看方向。

速率跟着请求完成走。前缀更长时完成更慢，所以 Reuse 的 metadata 更少，而不是更多。lineage 的三步扩展每次请求仍然只发一条，coverage 变大，条数不变。Zipf 和 overlap 改变命中哪一个前缀；overlap 额外贡献的是那 64 条 seed，不是持续的倍率。cache churn 在这套基线里没有变成消息。

## 5. 这些速率对着什么容量

formal4t4 的 `sig_bit_per_s` 是 Ideal 的 offered 除以 ρ。中位数（5 reps；各策略共用同一组 Ideal 速率，个别策略的中位差几十 bit/s）：

| 负载 | ρ=0.5 | ρ=0.8 | ρ=1.0 | ρ=1.2 |
|---|---:|---:|---:|---:|
| Original | 3446 bit/s（2550–4937） | 2153（1594–3085） | 1723（1275–2468） | 1435（1062–2057） |
| Reuse | 1888 bit/s（1535–2334） | 1180（959–1459） | 944（704–1167） | 787（639–972） |

这是 1–5 kbit/s，按目标 ρ 缩放出来的 HTB 速率。没有一次物理链路测量说边缘控制链路是这个量级。ρ≥1 的 Cell 会排队，是因为容量被设成了 offered 的 1/ρ。

10 Mbit 是另一次实验里用 `--link-bit 10000000` 加上的。`path_capacity.py` 在这个上限下，用合成发送方测到排空 9408 和 9349 frame/s，入口侧大约 27800 frame/s。10 Mbit 不是测出来的网卡容量，排空数也不是 vLLM 的产生速率。

对照：Original Ideal 中位 1723 bit/s，相对 10 Mbit 是 1/5800。按 frame 算，2.071/s 相对 9408 frame/s 的 ρ 是 0.00022。Reuse 更低。四张真实卡的自然负载处在 10 Mbit 可送走的流量的万分之二这个量级。

控制面补充实验的 HTB 父类是 100 Mbit，另加 netem 40±5 ms。`anchor_L_of_N.csv` 里的 ρ_ctrl 用的不是这条链路，而是脚本里的常数 `MU = 200` frame/s。公式是

```
L_n = 2.1479 * (N/4) * (1 + 21.8821)
```

2.1479/s 来自一个 Ideal 校准 Cell。21.8821 = 47 个前缀 / 2.1479 / 1 秒，是“每秒把整张前缀表再广告一遍”的假设，不是测到的 tombstone 或 heartbeat。乘上这个假设之后，N=32 的外推负载是 393 frame/s，ρ_ctrl=1.97；N=64 是 786 frame/s。去掉这个假设，N=32 只是 17.2 upsert/s，对 200 frame/s 的 ρ 是 0.086。N 不是 GPU 数。Dispatcher 在那个校准 Cell 上的 apply 时间是 p50 1.597 μs、p95 3.070 μs，这是单条处理延迟，不是饱和吞吐。

## 6. 三个层次

**Level 1.** 四张 T4、Qwen2.5-1.5B、当前正式负载：集群大约 1–3 条 wrapper UPSERT/s，0 条 tombstone，wire 约定大约 0.9–1.7 kbit/s。没有充分的自然控制面压力。

**Level 2.** 只把测到的 2.071 upsert/s 按集群数线性放大，要填满 10 Mbit 下测到的约 9408 frame/s 排空，需要约 4500 个同样的四卡集群，大约 1.8 万张同规格 GPU。按 104 字节把 10 Mbit 换成约 12000 frame/s，大约是 5800 个四卡集群、2.3 万张 GPU。补充实验的 N=32/64 外推在加上每秒全表刷新、并且容量取 200 frame/s 时才会 ρ_ctrl≥1。那是外推加假设，不是 32 或 64 张 GPU 的测量。即便按他们的 393 frame/s 计算，104 字节约定也只有约 327 kbit/s，仍低于 10 Mbit。

**Level 3.** ρ≥1 出现在三类设定里：formal4t4 把 HTB 设成 offered/ρ；icc_kv 用合成噪声把 offered 设成 ρ×C；fan-in 锚点在 μ=200 且带全表刷新假设时从 N=32 起超过 1。四张卡的自然负载对 10 Mbit 的 ρ 是 10^-4。

论文里看到的控制面瓶颈，主要来自 **D：人为压低控制网络容量**。B（多 worker 聚合）只以逻辑外推的形式存在，而且越过 1 还依赖一个未测量的全表刷新和一个写死的 200 frame/s。C（Gateway/Dispatcher 处理上限）没有被独立测成瓶颈：入口在合成洪水下高于 tc 排空，单条 apply 是微秒。A（自然 KV metadata 高产生速率）没有被这四张卡的数据支持。不能把 D 说成 A。

这对 motivation 的含义：现有实验可以说明“把控制链路压到几 kbit/s 之后，准入策略会改变转发和 TTFT”。它不能说明“真实四卡 LLM serving 自己会产生顶满 10 Mbit 控制面的 metadata”。如果论文的问题陈述依赖后一句，证据不够。

原生事件没有测到。按这个 harness 的粒度，一条请求一条消息。就算将来测到每个 KV block 一条，4096 token、block 16 大约是 256 条，乘上大约 2 次完成/s，也是几百 frame/s、几百 kbit/s，仍然到不了 10 Mbit。所以缺的不是再跑一次同样的四卡 Cell。缺的是一个不由 ρ 反推出来的控制容量，以及在这个容量下、用测到的每四卡速率做的 worker 放大。

## 7. 局限

- 速率是 2026-07-26 的 Cell，代码是 `1dc24ec` 一带，不是今天的 HEAD。Original Ideal 的最快一 rep（2.97/s）来自更早的 `57547bb`。中位 2.07/s 来自 `1dc24ec`。
- `cell_active_s` 含 warmup。用它做分母是对的，因为它也含对应的 upsert；不要拿它和“只统计稳态”的窗口直接比。
- 104 字节不是抓包。payload 口径是 64 字节。
- 并发标定每种只有 1 次，池子和请求数都和正式 Reuse 基线不同。
- 每 worker 速率没有完整记录。
- 没有原生 EVICT 或 block-alloc 计数。shadow 容量 104544 使 tombstone 路径在基线里没有被走到。
