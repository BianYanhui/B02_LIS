# P0-M3.6 Model B 规模化通信需求

本文件只做算术外推。没有启动 Fan-in，没有重放 256 个 Reporter。表在 `model_b_scale_projection.csv`，144 行。

## 外推单位

已测工作点是 P0-C1 的一只 vLLM engine：一张 GPU，TP=1，一个发布端。窗口与 `COMMUNICATION_COST_REPORT.md` 相同，59.885151563 s。

P0-M1 的映射是：Reporter 数等于 serving instance 数，等于 GPU 数除以每实例 GPU 数。TP=2、4、8 的单引擎事件速率没有测过，那些格子是 `NOT_MEASURED`。本轮不把这只 TP=1 引擎的速率乘到 TP>1 的引擎上。表里的 256、512、1024、4096 是 Reporter 数，不是 GPU 数。

每个 Gateway 64 个 Reporter 是研究设定，不是测到的生产拓扑。

| Reporter | Gateway | 每 Gateway |
|---:|---:|---:|
| 256 | 4 | 64 |
| 512 | 8 | 64 |
| 1024 | 16 | 64 |
| 4096 | 64 | 64 |

重复同一条 Trace 只把这只引擎的字节按 Reporter 数相乘。它代表同构工作负载的时间缩放。它不是 4096 个独立 Worker 的索引：prefix 身份仍是这一条 Trace 里的 101 条实验 lineage。不能用重复的 hash 声称已经得到多 Worker 索引状态。

P0-A1 的正式负载没有加进这张表。

单 Reporter 平均速率：

| 路径 | B/s | kbit/s | Mbit/s |
|---|---:|---:|---:|
| Native msgpack | 4751.24 | 38.010 | 0.038010 |
| Adapter 64 B | 259.70 | 2.078 | 0.002078 |
| KVSelect 出口 64 B | 219.09 | 1.753 | 0.001753 |

KVSelect 的单 Reporter 速率是窗口归因值。它的短窗口峰值是 `NOT_MEASURED`，所以同步相位的峰值行对 C 路径留空。

现有帧的 `instance` 是一个字节。256 个身份还能放下。512 及以上在当前帧里不能保持互异身份。这是实现限制。字节外推不依赖这些身份已经能编码。

## 平均负载

独立相位和同步相位的平均负载相同。平均是线性相加，相位只改变短窗口能不能叠在一起。下表是聚合平均 offered load，单位 Mbit/s。证据标记 `EXTRAPOLATED`。

| Reporter | Native | Adapter 64 B | KVSelect 出口 64 B | 每个 Gateway 的 Native |
|---:|---:|---:|---:|---:|
| 256 | 9.731 | 0.532 | 0.449 | 2.433 |
| 512 | 19.461 | 1.064 | 0.897 | 2.433 |
| 1024 | 38.922 | 2.127 | 1.795 | 2.433 |
| 4096 | 155.689 | 8.510 | 7.179 | 2.433 |

每个 Gateway 固定收 64 个 Reporter，所以每 Gateway 的平均 Native 负载停留在 2.433 Mbit/s。共享 Dispatcher 看到的是全部 Gateway 之和，也就是上表的聚合列。Gateway 个数增加会把入口拆开，不会把共享 Dispatcher 的入口字节拆小。这里没有计入跨 Reporter 的 Replica 合并。这条 Trace 只有一个 Owner，跨 Reporter 合并是 `NOT_MEASURED`。若真实流量里存在可合并的副本，出口会低于表中的 KVSelect 列。表中的数字是“每只 Reporter 各自重复这条 Trace”的上沿，不是多副本节省。

## 同步相位的短窗口

`SYNCHRONIZED_EXTRAPOLATION`：每只 Reporter 在同一时刻处在这条 Trace 的同一位置。这是压力敏感性，不是生产集群的默认到达。

Native 最忙 1 秒是 8769 B，单 Reporter 相当于 70.152 kbit/s。对齐之后：

| Reporter | 对齐的 1 秒峰值 |
|---:|---:|
| 256 | 17.959 Mbit/s |
| 512 | 35.918 Mbit/s |
| 1024 | 71.836 Mbit/s |
| 4096 | 287.343 Mbit/s |

Native 最忙 1 ms 是一条 3772 B 的消息，单 Reporter 相当于 30.176 Mbit/s，而且只持续这一毫秒。对齐之后：

| 范围 | 对齐的 1 ms |
|---|---:|
| 1 个 Gateway，64 Reporter | 1.931 Gbit/s |
| 256 Reporter | 7.725 Gbit/s |
| 4096 Reporter | 123.601 Gbit/s |

Adapter 的对齐峰值用的是源事件时间，不是出口时间：1 ms 为 2560 B/Reporter，1 s 为 2816 B/Reporter。KVSelect 的对齐峰值是 `NOT_MEASURED`。

独立相位的短窗口峰值也是 `NOT_MEASURED`。它的下沿是单 Reporter 自己的峰值，上沿是上面的对齐峰值。本轮不在这两个端点之间插一个到达模型。

## 容量敏感性

10、100、1000 Mbit/s 都是 `HYPOTHETICAL_CAPACITY`。此前实验里配置过的 10 Mbit/s HTB 同样是配置容量，不是这台机器的生产配额，也不用 104 B 的排空率来解释下面的比值。

比值是聚合 offered Mbit/s 除以假设容量。大于 1 只说明：在这个容量假设下，平均或该峰值的 offered load 超过该假设的服务能力。它不能推出真实数据中心已经发生拥塞。

### 平均负载

| Reporter | Native / 10 | Native / 100 | Native / 1000 | Adapter / 10 | KVSelect / 10 |
|---:|---:|---:|---:|---:|---:|
| 256 | 0.973 | 0.097 | 0.010 | 0.053 | 0.045 |
| 512 | 1.946 | 0.195 | 0.019 | 0.106 | 0.090 |
| 1024 | 3.892 | 0.389 | 0.039 | 0.213 | 0.179 |
| 4096 | 15.569 | 1.557 | 0.156 | 0.851 | 0.718 |

Native 平均负载在 256 Reporter 时仍低于 10 Mbit/s 的假设（9.731 / 10）。从 512 Reporter 起超过这个 10 Mbit/s 假设，到 4096 Reporter 超过 100 Mbit/s 假设（1.557），仍低于 1 Gbit/s 假设（0.156）。

Adapter 与 KVSelect 的平均负载在 4096 Reporter 时分别是 8.510 和 7.179 Mbit/s，都低于 10 Mbit/s 假设。若误用 104 B 记账，Adapter 在 4096 Reporter 的平均会变成 13.828 Mbit/s，比值变成 1.383。主结论不用这一行。

每个 Gateway 的 Native 平均是 2.433 Mbit/s，对 10 Mbit/s 假设的比值是 0.243。共享 Dispatcher 的聚合才是会随 Reporter 总数上升的那一列。

### 对齐峰值，仅作敏感性

Native 对齐 1 秒峰值对 10 Mbit/s 的比值，在 256 Reporter 已是 1.796。也就是说，256 Reporter 的平均还低于这条假设容量，对齐的最忙 1 秒已经高于它。平均未超过容量时，短时峰值仍可能需要缓冲。本轮不做队列实验，不给出排队时延。

Native 对齐 1 ms 对 1 Gbit/s 的比值，在单个 64-Reporter Gateway 已是 1.931。这是“64 条 3772 B 的消息落在同一毫秒”的假设。独立相位下这个对齐没有被测到。

## 读这张表时能说的话

一只 TP=1 引擎的 Native 平均只有 38.0 kbit/s。把它按 Reporter 数线性相加之后，4096 个同构 Reporter 的 Native 平均是 155.7 Mbit/s，Adapter 平均是 8.5 Mbit/s，KVSelect 出口平均是 7.2 Mbit/s。数量级的差别来自表示转换。KVSelect 在这条 Trace 上再减掉的约 1.3 Mbit/s（4096 Reporter 规模）来自那 38 条合并。

这些数是条件外推。它们描述的是：若有 N 个和这只引擎一样忙的 Reporter，控制面要准备消化多少应用层字节。它们不是一次集群测量。
