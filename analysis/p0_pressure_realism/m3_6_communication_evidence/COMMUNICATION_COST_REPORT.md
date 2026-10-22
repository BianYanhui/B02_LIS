# P0-M3.6 三路径通信开销

同一条 P0-C1 Trace，窗口 59.885151563 s。三条路径的字节口径见 `DATA_AND_ACCOUNTING_AUDIT.md`。表在 `communication_cost.csv` 和 `communication_burst_profile.csv`。

Native 与 Adapter 的查询能力并不相同。Native 载荷里带有 block hash 和 token id。Compact 帧是一条 prefix lineage 的 coverage。下面的差额是表示和转发条数的差额，不是“同样的信息以更少字节送达”。

## 总账

| 路径 | 条数 | 字节 | 平均 | 证据 |
|---|---:|---:|---:|---|
| A Native msgpack | 212 条消息，252 个事件，6615 次 hash 出现 | 284529 B | 4751.24 B/s，38.010 kbit/s | `MEASURED_NATIVE` |
| A ZMQ body | 212 | 286225 B | 38.236 kbit/s | `MEASURED_NATIVE` |
| A TCP |  | `NOT_MEASURED` | `NOT_MEASURED` | 无 pcap |
| B Adapter | 243 UPSERT | 15552 B | 259.70 B/s，2.078 kbit/s | `TRACE_DERIVED`，条数 × 64 B |
| C KVSelect 出口 | 205 转发 | 13120 B | 219.09 B/s，1.753 kbit/s | `ACTUAL_GATEWAY_REPLAY` 的条数 × 64 B |

C 的平均速率是把 13120 B 摊到 Native 窗口上。出口时刻没有逐帧记录，所以这是窗口归因，不是采样到的发送曲线。

Hash 出现次数是 block hash 在事件里出现的次数。它不是消息数，也不能拿来和 212 或 243 比拥塞。

## 减少量

| 比较 | 少掉的字节 | 相对减少 | 归属 |
|---|---:|---:|---|
| Native → Adapter | 268977 B | 94.534% | 表示转换。Adapter 把带 token id 的 block 事件写成 64 B prefix 更新 |
| Adapter → KVSelect | 2432 B | 占 Adapter 的 15.638%，占 Native 的 0.855% | 38 条 `superseded`。Replica 0，Top-K 0 |
| Native → 最终出口 | 271409 B | 95.389% | 整条管道的账面差。其中 Adapter 占少掉字节的 99.104%，KVSelect 占 0.896% |

最后一行不能记成 KVSelect 的算法收益。KVSelect 在这条 Trace 上多拿掉的是 38 条仍在队列中的同 lineage 更新，2432 B。

条数方向和字节方向不一致，需要分开看。252 个 Native 事件变成 243 条 Compact 更新，消息数从 212 变成 243。表示转换减少的是字节，不是 ZMQ 消息个数。一条大的 BlockStored 变成一条 64 B 更新；一条包含逐步缩短的 BlockRemoved 会变成同一 lineage 的多条 64 B 更新。

## 突发窗口

窗口是滑动的。均匀网格从第一条的时刻铺到最后一条，步长为窗口的十分之一，空窗口计入。`peak_equivalent` 把该窗口里的字节除以窗口长度，只表示“若这个窗口一直持续，速率会是多少”。这条 Trace 里短窗口大部分是空的，所以那个等价值不是持续速率。

P95 有两列，含义不同：

- `p95_uniform_grid`：时间轴上 95% 的窗口不超过这个字节数。短窗口里它常常是 0，因为大部分时间没有消息。
- `p95_event_aligned`：只在每条记录的到达时刻看窗口。同一时间戳上的多条记录会重复计算同一个窗口。Adapter 最忙的 1 ms 有 40 条记录，所以事件对齐的 P95 会被这一瞬间抬高。读平均占用时用均匀网格那一列。

### Native msgpack，`MEASURED_NATIVE`

时间是订阅者到达时刻。

| 窗口 | 峰值字节 | 若持续则相当于 | 均匀网格 P95 | 空窗口比例 |
|---|---:|---:|---:|---:|
| 1 ms | 3772 B | 30.176 Mbit/s | 0 | 99.647% |
| 10 ms | 3772 B | 3.018 Mbit/s | 0 | 96.475% |
| 100 ms | 3772 B | 0.302 Mbit/s | 2678 B | 65.376% |
| 1 s | 8769 B | 70.152 kbit/s | 5846 B | 7.333% |

1 ms、10 ms、100 ms 的峰值都是单条最大消息 3772 B。这一秒以内没有第二条大消息叠进这些窗口。最忙的 1 秒是 8769 B，约是平均每秒 4751 B 的 1.85 倍。

ZMQ body 的对应峰值是 3780 / 3780 / 3780 / 8817 B，只比 payload 多出 seq 的 8 B。

### Adapter，`TRACE_DERIVED`

时间是源事件的 `recv_ns`。一条 Native 消息若产出多条更新，这些更新共用一个时间戳，字节按 64 B 相加。

| 窗口 | 峰值字节 | 若持续则相当于 | 均匀网格 P95 | 空窗口比例 |
|---|---:|---:|---:|---:|
| 1 ms | 2560 B | 20.480 Mbit/s | 0 | 99.660% |
| 10 ms | 2560 B | 2.048 Mbit/s | 0 | 96.603% |
| 100 ms | 2560 B | 0.205 Mbit/s | 64 B | 66.572% |
| 1 s | 2816 B | 22.528 kbit/s | 256 B | 8.848% |

2560 B 是 40 条更新。它们来自那条 3772 B 的 Native 消息：一条 lineage 从 1040 逐步写到 448，共 38 条，另有两条 lineage 各 1 条。即便在这个最挤的 1 ms，Compact 字节仍是 2560，低于同一时刻的 Native 3772。1 秒峰值 2816 B，低于 Native 的 8769 B。

全 Trace 的 94.534% 不能套到每个窗口上。缩短那一下，字节只从 3772 降到 2560，降幅 32.1%。大的 BlockStored 才是平均降幅的主要来源：99 条 Native 消息各自的 payload 已经大于 Adapter 最忙 1 ms 的 2560 B，而那些消息在 Adapter 侧通常只对应很少的 64 B 更新。

### KVSelect 出口，`NOT_MEASURED`

1 ms、10 ms、100 ms、1 s 的出口峰值和出口 P95 都是 `NOT_MEASURED`。Gateway 以 `--quiet` 运行，逐帧 JSON 被关掉。2 s tick 没有时间戳。Adapter 的 `recv_ns` 不拿来充当发送时刻。

能确定的是出口总量和队列深度。cell 351 转发 205 条，应用队列峰值 `maxq = 3`，结束时队列为空，`congested_ms = 0`。38 条减少全部来自 `superseded`。其中 37 条与上面那次逐步缩短的中间步骤对齐；第 38 条没有逐帧身份。Top-K 没有丢弃，Replica cap 没有丢弃。

因此这条 Trace 上，KVSelect 相对 Adapter 的额外节省是 38 条、2432 B。它没有给出一条测到的出口突发曲线。

## 和论文实验负载的边界

本表的平均速率来自 P0-C1 这一条单引擎 Trace。论文 Section IV 的 64 B 更新、HTB 和 10 Mbit/s 固定容量是另一套四 Worker 的 Compact 工作负载。两套字节不能加成一个 offered load。
