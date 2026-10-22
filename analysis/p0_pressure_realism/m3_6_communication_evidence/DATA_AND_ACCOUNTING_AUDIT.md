# P0-M3.6 数据冻结与口径审计

审计脚本：`recompute_m36.py`。它重新读取原始文件，不改 C1、M3、M3.5 的产物。计数链可以复现，后续分析继续。

来源：

- `analysis/p0_pressure_realism/c1_real_indexer/raw/frames.bin`，290465 B。
- `analysis/p0_pressure_realism/m3_native_compact_motivation/raw/converter_updates.csv`。
- `analysis/p0_pressure_realism/m3_5_native_compact_e2e/raw/results.json` 的 `phase1`。
- 同一目录 `raw/gateway.log` 里 cell 351 的 `cell_stats`。
- `experiments/icc_kv/wire.py` 的 `frame()`、`FRAME` 和 `WIRE_BYTES`。

## 复现结果

| 环节 | 重新计算 | 预期 | 标记 |
|---|---:|---:|---|
| Native ZMQ 消息 | 212 | 212 | `MEASURED_NATIVE` |
| Native 事件 | 252 | 252 | `MEASURED_NATIVE` |
| BlockStored / BlockRemoved / AllBlocksCleared | 210 / 41 / 1 | 同左 | `MEASURED_NATIVE` |
| Block hash 出现次数 | 6615 | 6615 | `MEASURED_NATIVE` |
| Msgpack payload | 284529 B | 284529 B | `MEASURED_NATIVE` |
| Adapter 记录 | 243，全部是 UPSERT | 243 | `TRACE_DERIVED` |
| Adapter 按 64 B | 15552 B | 15552 B | `TRACE_DERIVED` |
| Gateway 收到 | 243 | 243 | `ACTUAL_GATEWAY_REPLAY` |
| superseded | 38 | 38 | `ACTUAL_GATEWAY_REPLAY` |
| global_topk | 0 | 0 | `ACTUAL_GATEWAY_REPLAY` |
| replica_cap | 0 | 0 | `ACTUAL_GATEWAY_REPLAY` |
| 转发 | 205 | 205 | `ACTUAL_GATEWAY_REPLAY` |
| Dispatcher applied | 205 | 205 | `ACTUAL_GATEWAY_REPLAY` |
| 转发按 64 B | 13120 B | 13120 B | 计数是实测，字节是 codec 乘积 |

`243 = 205 + 38`。cell 351 的日志 `cell_stats` 与 `results.json` 一致：`forwarded = 205`，`drops.superseded = 38`。`drops` 里没有 `global_topk`，也没有 `replica_cap`。

序列号 0 到 211，缺口 0。Adapter 的 243 个 `recv_ns` 全部落在 Native 首尾时间戳之内。

## 三条字节边界

这三套数字回答的是不同的问题，后面的比例只用第一套和第三套里的 codec 行。

### 1. Native 应用载荷，`MEASURED_NATIVE`

`frames.bin` 的记录格式是本地采集头 `<QIII`（20 B）加上 topic、seq 和 msgpack。212 条采集头共 4240 B。这个头是采集文件自己的，不在链路上。

| 边界 | 字节 | 说明 |
|---|---:|---|
| Msgpack payload | 284529 | 本轮 Native 路径的主口径 |
| ZMQ body（topic + seq + payload） | 286225 | topic 为空，每条 seq 8 B，帧开销 1696 B |
| 采集文件 | 290465 | 含 4240 B 采集头 |
| TCP 线上字节 | `NOT_MEASURED` | C1 raw 目录没有 pcap |

订阅端 `recv_ns` 减去 batch 内的 `ts`：212 条都有差值，最小 0.52 ms，中位 1.51 ms，最大 3.04 ms。`recv_ns` 是本机订阅者的到达时间。`ts` 是事件批次里的发布侧时间。两者相差数毫秒。突发窗口用 `recv_ns`，因为它是测到的到达时刻。

Token id 占比沿用 M3 的重编码：把 `BlockStored.token_ids` 清空后再 msgpack 编码。重编码失败 0 次。派生 token id 字节 215157，占 payload 的 75.6187%。含 token id 的消息 210 / 212，这些消息的 payload 共 284449 B。这是 `TRACE_DERIVED`，不是抓包分段。

### 2. Compact codec，64 B

`frame()` 的返回长度是 64。头 `>BBHIqQd` 为 32 B，后面补 32 B。M3.5 的下游读端在断开时报告 `64 expected bytes`，与这个长度一致。

64 B 是帧 codec 的长度。M3.5 没有对 Compact 路径做 tcpdump，所以 15552 B 和 13120 B 都是「条数 × 64」，不是网卡计数。

### 3. 历史记账 104 B

`experiments/icc_kv/wire.py` 的 `WIRE_BYTES = 104`。`experiments/4t4/freeze_manifest.py` 同时写着 `frame_bytes: 64` 和 `wire_bytes_per_msg: 104`。两者相差 40 B，等于一个无选项 IPv4 头加一个无选项 TCP 头。P0-M1 用这个 104 B 把正式实验的帧数换成记账带宽，并给出配置成 10 Mbit/s 的链路上约 9408 frame/s 的排空。论文里对应的标定排空率是 \(9.41\times10^{3}\) frame/s。

那套排空率属于 104 B 记账边界，而且测的是另一套 Compact 工作负载。本轮不把 64 B 的条数除以 9408，也不把 104 B 代回 Native msgpack 去算 10 Mbit/s 占用。

单独列在表里、不进入比例的两行：

| 路径 | 104 B 记账 | 64 B codec |
|---|---:|---:|
| Adapter 243 条 | 25272 B | 15552 B |
| KVSelect 转发 205 条 | 21320 B | 13120 B |

104/64 = 1.625。若把 Adapter 的规模化平均速率误按 104 B 计算，4096 个 Reporter 的平均负载会从 8.510 Mbit/s 变成 13.828 Mbit/s。前者低于假设的 10 Mbit/s，后者高于它。这就是两条边界不能混用的直接后果。主表使用 64 B。

## 时间戳

| 路径 | 时间戳 | 窗口 |
|---|---|---|
| Native | 订阅者 `recv_ns` | 首 1792311069941330550 到末 1792311129826482113，跨度 59.885151563 s |
| Adapter | 源事件的同一个 `recv_ns` | 末条 1792311129730191102，仍在 Native 窗口内。这是离线转换时抄来的源事件时间，不是单独测到的 Compact 发送时刻 |
| KVSelect 出口 | `NOT_MEASURED` | `--quiet` 关闭了逐帧日志。2 s tick 的 JSON 没有时间戳。不用 Adapter 时间冒充出口时间 |

cell 351 结束时 `maxq = 3`，`drain_wait_s = 0.000998`，`write_queue_bytes = 0`，`congested_ms = 0`。队列深度是测到的。它说明这次回放没有堆积成长队列。它不是 1 ms 或 1 s 出口字节窗口。

## 38 条合并的归属

`drops["superseded"]` 只在 `_supersede_queued_up` 里增加。同一 `(instance, digest)` 的新 UPSERT 替换仍在队列里的旧 UPSERT 时走这里。Replica 替换走 `replica_cap`。Top-K 裁剪走 `global_topk`。后两个计数在 cell 351 都是 0。

因此这 38 条全部记在扩展合并这一类，不记在 Replica Suppression，也不记在 Top-K。

同一 `recv_ns` 的最忙一组有 40 条 Adapter 记录，来自一条 Native 消息，该消息的 msgpack 是 3772 B。其中一条实验 lineage 连续写了 38 个 coverage（1040 降到 448），中间步骤 37 个；另外两条 lineage 各 1 条。日志里有一次 tick 先出现 `superseded = 37`，cell 结束时是 38。37 与这一组中间步骤对齐。第 38 条合并仍是 `superseded` 计数，日志没有逐帧身份，本轮不指定它是哪一条记录。

## 结论

核心计数可以复现。64 B 是 codec 长度，104 B 是另一条记账边界。TCP 线上字节和 Gateway 逐帧出口时间是 `NOT_MEASURED`。分析继续，缺的两项保持空缺。
