# P1 Calibration

校准对象是 N=4、`heterogeneous=False`、种子 `2026072600`（与 `analysis/formal4t4/raw/baseline/run_metadata_baseline_reuse_20260726.json` 的正式种子相同）、10 Mbit/s 虚拟链路。五条策略各一轮。原始记录在 `runs/p1-v1_CAL_*`。这不是四张 T4 上的重跑，不能写成正式单元格的数值等价。

## 对齐了什么

| 项 | 正式 reuse baseline | 本次 N=4 仿真 | 判定 |
|---|---|---|---|
| 请求数 / warmup / pool / overlap | 120 / 24 / 64 / 0.25 | 同一份 `make_trace` | 一致 |
| UPSERT 条数 | 结构上 120 + 16×4 = 184 | 184 | 一致 |
| Tombstone | 正式中位接近 0；baseline 的 churn 间隔是 0 | 0 | 一致。104544 token 的 shadow 在这条开环分配下没有腾出空间 |
| 打分请求 | 96 | 96 | 一致 |
| 每组 upsert 速率 | 正式 Ideal 中位 1.071/s | 184 / 168.15 s = 1.094/s | 差约 2%。波间隔按 184/1.071 排成 46 波，波内 10 ms 和发布滞后 50 ms 把跨度拉短了一点 |
| Routing FN，10 Mbit/s | 正式实验的 ρ 是按 offered 把 HTB 调到约 1 kbit/s，不是 10 Mbit/s | 五条策略都是 0/96 | 只能说明仿真在远低于链路的负载下没有制造假阴性。不能和正式 ρ=1.2 的 FN 对表 |
| Gateway CPU | 正式路径含 vLLM | FullSync 2.7 ms，KVSelect 2.9 ms | 只含 Relay 准入、出队和索引。不含推理 |
| 转发字节 | 正式用 104 B 记账 | FullSync 184 帧；KVSelect 130 帧，`replica_cap` 54 | 64 B 载荷与 104 B 记账都可从帧数乘出。不把 54 条副本抑制写成 Top-K |

10 Mbit/s 的服务率是 9407.55 frame/s，取自 `a8a07b7` 的 `c_drain_per_s`，不是这台机器上新做的 tc。2 Mbit/s 和 5 Mbit/s 按这个数线性缩放，缩放本身没有重新测。bfifo 深度 65536 B，按 104 B/帧占用。Gateway 源文件哈希 `7ef3c660e81d4fb6a4ea60a17f430d1429df048a3174adad5e3cd3472b2f1fd6`，与跑前相同。

## 没有对齐的部分

- 没有逐条正式控制面时间戳，所以波间隔是由 1.071/s 反推的，不是经验分布。
- 虚拟时钟替代了 TCP `drain()` 和 HTB。AgeCov 的年龄和 RateFIFO 的令牌桶走的是这个时钟，不是墙上的 socket 时间。
- 开环 residency：owner 在进 Gateway 之前就由 group 内的 oracle 定死。正式实验是闭环，策略会改变请求落在哪张卡上，从而改变下一条 KV 事件。
- `max_inflight=0`。2026-07 的 formal4t4 清单是 4。
- `instance` 在实验进程内是 u16。N=4 用不到这个宽度，但帧布局已经不是容器里的 u8 头。
- Tombstone 为 0，失效优先在这条校准轨迹上没有被走到。
- 没有 GPU TTFT，也没有把仿真 FN 换成缓存 token。

## 仿真器引入的差

在 10 Mbit/s 上，digest 级 P95 协调延迟是 106 µs，等于 1/9407.55 s，也就是一帧服务时间。Gateway 队列最大值是 1。算法之间没有延迟差，除了 KVSelect 少转发 54 帧。这 54 帧是 replica cap 2 对「4 个 owner 都播种」的抑制，digest 仍然可见，所以 FN 仍是 0。

因此：轨迹条数和正式结构对齐；到达过程是构造的；链路是按旧 tc 排空率标定的虚拟队列。N>4 的结果是这个构造的放大，不是 4096 张 GPU 的测量。
