# P0-M3.6 物理部署边界

字节都来自同一条 Trace 的 59.885 s 窗口。Native 是 msgpack payload 284529 B。Compact 是 64 B codec：Adapter 15552 B，KVSelect 出口 13120 B。TCP、以太网和网卡计数是 `NOT_MEASURED`。下表的“跨网络”指：若这一跳的两端在不同机器上，应用层要交给传输的字节。C1 的采集和 M3.5 的回放都在本机完成，所以这些跨网络数字是边界计算，不是一次远程抓包。

M3.5 的真实路径是本机 Driver 做 Adapter，再经 `127.0.0.1` 进入 Gateway 和 Dispatcher。那一次运行本身没有跨机器的网卡节省。下面三行回答的是：同一批字节若放在不同的进程边界上，哪一跳会变小。

KVSelect 的 38 条合并发生在 Gateway 队列里。Worker 本机只做 Adapter 时，离开 Worker 的仍是 243 条、15552 B。那 2432 B 要等 Gateway 执行合并之后才从后续一跳消失。

## 三跳对照

| 部署 | 跨网络的 Native | 跨网络的 Compact | 实际变小的那一段 | 当前证据 |
|---|---:|---:|---|---|
| A Worker 本地 Adapter，KVSelect 仍在 Gateway | Worker→Gateway 为 0 | Worker→Gateway 15552 B；若 Gateway 与 Dispatcher 分机，后一跳 13120 B | Worker 离开本机的元数据，从 284529 B 降到 15552 B。降幅属于 Adapter | 字节是 `TRACE_DERIVED`。这个放置方式没有在远程 Worker 上跑过 |
| B Gateway 本地 Adapter | Worker→Gateway 284529 B | Gateway→Dispatcher 13120 B | 只有 Gateway 出口及其后的网络。入口的 Native 字节已经穿过 Worker 到 Gateway | 出口条数是 `ACTUAL_GATEWAY_REPLAY`，入口字节是 `MEASURED_NATIVE` |
| C Gateway 与 Dispatcher 同机，Adapter 在控制节点 | Worker→控制节点 284529 B | 控制节点内部不再有一条跨机器的 Compact 跳 | 内部状态更新从 243 条变成 205 条。Worker 到控制节点的带宽不变 | M3.5 的回放就是同机回环，语义计数可用，网卡节省没有测到 |

单 Reporter、这一窗口内，A 在 Worker 出口少发送 268977 B。B 在 Gateway 出口相对“把 Native 再转发一遍”少的是另一段链路：入口仍是 284529 B，出口是 13120 B。C 的 Worker 出口仍是 284529 B。

若 C 的上游再加一个 Worker 本地 Adapter，那就是 A 的入口再接一个同机的 Gateway 与 Dispatcher。此时 Worker→控制节点变成 15552 B，节点内部的合并再把后续状态更新收到 205 条。这个组合要单独写出来。它不是 M3.5 已经部署的结构，因为 M3.5 的 Adapter 和 Gateway 在同一台机器上。

## 部署 A

```text
vLLM Worker
  Native block events（本机）
  Local Block-to-Prefix Adapter
  Compact updates，15552 B
  ---- 网络 ----
  Gateway 上的 KVSelect
  Compact updates，13120 B
  ---- 若 Dispatcher 在另一台机器，这是第二跳网络 ----
  Dispatcher
```

Worker 本地转换降低的是 Worker 离开本机时的元数据。这一跳的节省是表示转换，不是 KVSelect 已经单独验证过的贡献。跨 Worker 的 Replica Suppression 和全池 Top-K 仍然要等更新到达 Pool Gateway 之后才能做。这条 Trace 上那两项的丢弃数都是 0，所以即使 KVSelect 在 Gateway，本窗口内第二跳相对第一跳少掉的 2432 B 也只来自扩展合并。

## 部署 B

```text
Workers
  Native block metadata，每 Reporter 284529 B
  ---- 网络或本机 fabric ----
  Gateway
  Block-to-Prefix Adapter
  KVSelect
  Compact，13120 B
  ---- 网络 ----
  Dispatcher
```

Worker 到 Gateway 继续承担 Native 字节。能记成通信收益的是 Gateway 出口以及出口之后的网络。入口两边的差额不能写成“入口带宽已经被节省”。

按 4096 个同构 Reporter 的条件外推（`EXTRAPOLATED`，见规模分析）：Worker→Gateway 的 Native 平均是 155.689 Mbit/s，Gateway→Dispatcher 的 KVSelect 出口平均是 7.179 Mbit/s。前者是入口，后者是出口。只有出口是这条部署里变小的一段。

## 部署 C

```text
Workers
  Native block metadata，284529 B / Reporter
  ---- 网络 ----
  Control node
    Gateway + Adapter + KVSelect
    Dispatcher
```

Worker 在别的机器、Adapter 在控制节点时，完整的 Native 元数据已经穿过网络。节点内部的转换减少的是后续状态更新的条数和索引要消化的表示：243 条变成 205 条，15552 B 的 codec 记账变成 13120 B。这一段位于控制节点内部。它不减少 Worker 到控制节点的网络带宽。

M3.5 实际跑的就是这一类同机回环，外加本机 Adapter。那次运行证明了条数和最终 coverage。它没有证明某一块网卡少收了 268977 B。

## 规模下每个 Gateway 看到什么

Model B 里每个 Gateway 收 64 个 Reporter。部署 B 下，一个 Gateway 的 Native 入口平均是 2.433 Mbit/s（`EXTRAPOLATED`）。64 个 Gateway 的入口之和是 155.689 Mbit/s，由共享 Dispatcher 前的那一组出口汇合。把 Adapter 放在 Worker 上，会改变每一条 Worker→Gateway 的入口；把 Adapter 放在 Gateway 上，入口不变，变化出现在 Gateway→Dispatcher。
