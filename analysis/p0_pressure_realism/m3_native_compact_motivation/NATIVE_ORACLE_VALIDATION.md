# P0-M3 Native Oracle Validation

Oracle 是按事件顺序维护的块驻留。查询一个前缀时，只使用该事件已经应用之后的存活块。存活哈希的最终集合与 C1 记录的 Dynamo dump 一致，打断点的 token 查询又在本轮的 Dynamo Indexer 上复现。

## 回放范围

| 项 | 值 | 标记 |
|---|---|---|
| 输入 | 212 条原始 msgpack，252 个事件 | `TRACE_REPLAY` |
| 应用结果 | Indexer 最终 applied 252，dump 外部哈希 6525 | `TRACE_REPLAY` |
| Python 存活集 | 6525，等于 C1 `reference_alive` | `TRACE_REPLAY` |
| 存储块 | 6566，其中重复存储 8 | `TRACE_REPLAY` |
| 删除块 | 41，删除时找不到的块 0 | `TRACE_REPLAY` |
| 清空 | 1 | `TRACE_REPLAY` |
| 父块缺失 / 父子冲突 / token 冲突 | 0 / 0 / 0 | `TRACE_REPLAY` |
| 块大小 | 16 | `TRACE_REPLAY` |
| Indexer 时序 | 按帧顺序尽快发送，不是原始 59.885 秒墙钟 | 顺序与 trace 相同 |

C1 记录的 indexer 结果仍然有效：当时 applied 252，dump 6525，缺失 0，多出 0。本轮 dump 与那个参考集合的大小和 Python 重放集合都相同。8 次重复存储与 C1 的 `duplicate_store` 警告条数相同。

Indexer 进程使用 `/home/byh/Dynamo/.venv-dynamo/bin/python -m dynamo.indexer`，`--block-size 16`，`--workers 0=tcp://127.0.0.1:5557`。回放结束后进程已退出，5557 和 8090 不再监听。`127.0.0.1:9710` 没有被这个回放使用。

## 查询

真实 HTTP 请求的 token 序列没有保存在 `requests.csv`。下面两条查询使用该链上 `BlockStored` 事件里的 token，按父指针从根排到末端。标记：`CONTROLLED_FROM_STORED_BLOCKS`。

回归链是 C1 记下来的末端 `38a2855819ec6b90`。打断帧是第 206 帧，接收时间 `1792311129094531255`。当时这条链有 65 个块、1040 个 token。从根连续还活着的是 28 个块、448 token。洞后面的存活块是 0。

| 时刻 | 查询 | Dynamo `scores["0/0"]` | Python 连续覆盖 | 标记 |
|---|---|---:|---:|---|
| 第 206 帧之后，已应用 247/247 | 1040 个 token 的整段 | 448 | 448 | `TRACE_REPLAY` |
| 同一时刻 | 前 448 个 token | 448 | 448 | `TRACE_REPLAY` |
| 全部 252 个事件之后 | 同一段 1040 token | 416 | 该 lineage 最终广告 416 | `TRACE_REPLAY` |
| 全部事件之后 | 前 448 个 token | 416 | 同上 | `TRACE_REPLAY` |

打断点之后又删掉了 32 个 token，所以 trace 结束时连续覆盖是 416，不是 448。C1 报告里的 448 是打断批次结束时的值，本轮在同一时间点复现了它。

查询只命中 worker 0、dp rank 0。这与单 engine 采集一致。

## 通信剖面

完整表在 `native_traffic_profile.csv`。计费边界是原始 msgpack，不是“每个块一帧”。

| 指标 | 值 | 标记 |
|---|---|---|
| 消息 / 事件 / 哈希出现 | 212 / 252 / 6615 | `TRACE_REPLAY` |
| 时长 | 59.885 s | `TRACE_REPLAY` |
| msgpack | 284529 字节，3.540 消息/s，4.208 事件/s，110.461 哈希出现/s | 速率是 `DERIVED` |
| 平均载荷 | 4751.245 B/s，38.01 kbit/s | `DERIVED` |
| 载荷 P50 / P95 / P99 / max | 99 / 2824 / 2824 / 3772 字节 | `TRACE_REPLAY`，分位与 C1 相同的 nearest-rank |
| 含 topic 和 seq 的 ZMQ 体 | 286225 字节；P50 / P95 / P99 = 107 / 2832 / 2832 | `TRACE_REPLAY` |
| 1 ms、10 ms、100 ms 窗口峰值 | 都是 3772 字节载荷 | `DERIVED`。等于最大的单条消息，这三个窗口里没有第二条大消息叠上 |
| 1 s 窗口峰值 | 8769 字节载荷，约 70.15 kbit/s | `DERIVED` |
| `BlockStored` / `BlockRemoved` / `AllBlocksCleared` | 210 / 41 / 1，占事件的 83.3% / 16.3% / 0.4% | `TRACE_REPLAY` |
| 含 token id 的消息 | 210/212 条，占载荷字节 0.999719 | 字节比例 `DERIVED` |
| 把 token id 置空后重编码少掉的字节 | 215157 字节，占载荷 0.756187 | `DERIVED`，重编码失败 0 |

6615 是块哈希出现次数。网络消息是 212 条。

## 与路由可见性有关的最终块状态

101 条分支在 trace 结束时的连续覆盖之和是 106000 token：99 条 1056、1 条 1040、1 条 416。1056 是 1040 个已对齐的 prompt token 再加 16 个后续块 token。Oracle 与转换器在每个事件之后的差都是 0。

这 106000 是每条查询链各自的连续覆盖，共享的开头块会在多条链里各算一次。它不是存活块数乘 16。存活块是 6525。
