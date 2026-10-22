# P0-M3.5 End-to-End Interface Audit

接口判定：`COMPATIBLE_WITH_EXPERIMENTAL_MAPPING`。

现有 64 字节帧可以把这条 trace 的缩短覆盖送进真实 Gateway 和 `Dispatcher.on_frame`。正式实验的 lineage 字符串不在 Native trace 里，帧里也没有单调版本。这两件事都不需要先改 Algorithm 1 才能把本条 trace 跑通，但它们决定了结果不能写成“与正式 Lineage 完全等价”或“已经具备重放安全性”。

没有发现必须大改 KVSelect 才能接入，因此没有在 Phase 0 停止。

## 实验身份

| 项 | 值 |
|---|---|
| Git HEAD | `f3adf24fcf111c2bedcd64f3d28a0a9496d1abed` |
| 策略 | `wire.POLICIES["StaticTopK"]` = `(4, 1, 1, 0, 2, 16)` |
| K | 16，未改 |
| Adaptive | 关闭 |
| `max_inflight` | 0，`CONFIGURED`。不是限速 |
| Gateway | 工作区里的 `experiments/4t4/net/gateway_relay_4t4.py`。相对 HEAD 只有此前已有的诊断计时，准入路径没有改 |
| Dispatcher | `experiments/icc_kv/runtime.py` 的 `Dispatcher.on_frame` |
| 端口 | Dispatcher `127.0.0.1:19950`，控制 `19951`，Gateway `19960`。没有使用 `9710` |

命令：

```text
cd /home/byh/B02
PYTHONPATH=/home/byh/B02 poc/.venv/bin/python \
  analysis/p0_pressure_realism/m3_5_native_compact_e2e/run_m35.py
```

Trace 仍是 `analysis/p0_pressure_realism/c1_real_indexer/raw/frames.bin`。P0-M3 目录没有被改写。

## 0.1 Adapter 字段

| 字段 | 本轮的值 | 标记 |
|---|---|---|
| Owner | 采集只有 `data_parallel_rank=0`，帧里的 instance 用 0 | `TRACE_REPLAY` |
| Stable lineage | 分支根块哈希，与 101 个 `prompt_blake2s` 一一对应 | `DERIVED`，不是正式 lineage 字符串 |
| Prefix digest | 该哈希的低 64 位，放进现有 digest 字段 | `DERIVED` |
| Event type | 243 条都是 `UPSERT`。trace 上没有 `EVICT` | `TRACE_REPLAY` |
| Reusable coverage | 与块 Oracle 连续覆盖的差为 0 | `TRACE_REPLAY` |
| Compatibility scope | 单 owner、块大小 16、无 LoRA | `TRACE_REPLAY` |
| 生成时间 | 产生这次更新的 Native 消息接收时间 | `TRACE_REPLAY` |
| Native sequence | 按 `frames.bin` 的 seq 0–211 重放 | `TRACE_REPLAY` |
| State version | Adapter 内部没有版本计数，帧里也没有 | 不存在 |
| Trace source | P0-C1 `frames.bin` | `TRACE_REPLAY` |

Lineage 映射在这条 trace 上满足四条实验性质，标记 `EXPERIMENTAL_LINEAGE_MAPPING_ONLY`：

1. 同一分支的变长和缩短使用同一个 ID。回归链 `e108dae91d118dad` 从 1056 收到 448，再收到 416。
2. 101 个 prompt 对应 101 个 lineage，没有一对多，也没有多对一。210 次 `BlockStored` 都落在恰好一个请求窗内。
3. ID 在该分支第一次出现新块时确定，不用之后的事件回写身份。
4. ID 不是整段前缀的内容哈希。末端哈希会随扩展改变；分支根不变。

这不是正式 KVSelect 实验里 blake2b(lineage 字符串) 的等价物。

## 0.2 Gateway

`StaticTopK` 打开 merge、invalidation priority、replica cap 2 和 global top-k 16。普通 `K_UP` 在准入后走 FIFO。`K_TOMB` 进入优先队列。

代码和本轮实测一起说明缩短覆盖：

| 问题 | 结果 |
|---|---|
| 较短覆盖是否仍是合法 UPSERT | 是。Gateway 收下了 1056 之后的 448 |
| 排队中的高覆盖是否盖住新的低覆盖 | 同一 `(owner, digest)` 上，后到的更新替换尚未发送的旧 UPSERT。Scenario B 里 17 条收到、1 条被替换、发出去的是 448 |
| Replica dominance 是否挡住同一 owner 的缩短 | 两个 owner、cap 为 2 时没有。Owner A 变成 448，Owner B 保持 1024 |
| Top-K 是否淘汰缩短 | 本条 trace 的排队峰值是 3，`global_topk` 丢弃为 0。人为把待发集合塞满后，448 的修正被丢弃 1 次 |
| 是否多余地发 Tombstone | 本条 trace 的缩短没有产生 Tombstone。Dispatcher 上的键还在，覆盖是新值 |
| Dispatcher 是否看到新覆盖 | 有序排空后看到。见 Phase 1 |

没有预设 supersession 自动正确。上面两行“排队中的替换”和“塞满后的丢弃”是分开测的。

## 0.3 Dispatcher

`on_frame` 对 `K_UP` 用新覆盖替换 `(worker, digest)`，对其他数据帧按单键作废。帧头没有单调版本。同一 TCP 连接里的顺序只说明这次回放没有重排；它不保护另一条连接、重试或迟到的旧帧。

`K_RESET` 是 Gateway cell 的控制帧，不是一个 owner 的前缀清空。Owner 清空在本轮用该 owner 自己的 `K_TOMB`。

ACK 由这个 Dispatcher 进程按正式帧返回。`max_inflight=0`，所以 Gateway 不靠 ACK 窗口限速。

## 0.4 为什么不是另外三个判断

- `DIRECTLY_COMPATIBLE`：正式 lineage 字符串和版本字段都不在这条链路上。
- `SEMANTIC_CHANGE_REQUIRED`：不改算法也能跑完这条有序 trace。版本和满额 Top-K 的缺口记在后面的回归里，不把它提前说成“接口接不上”。
- `BLOCKED`：digest 放得进现有 `u64`，Owner 0 小于 `ROUTABLE_BELOW=16`。
