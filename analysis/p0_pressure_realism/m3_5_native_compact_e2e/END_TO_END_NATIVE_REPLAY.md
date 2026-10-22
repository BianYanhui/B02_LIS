# P0-M3.5 Native Replay Through the Real Pipeline

路径是 `frames.bin → P0-M3 Adapter → 真实 Gateway（StaticTopK，K=16）→ 真实 Dispatcher.on_frame`。发送间隔用 Native 消息的原始接收时间，全程约 60 秒。没有 tc，没有 Apply Sleep，没有人为把队列堆高。

排空之后，Dispatcher 的 101 条覆盖与 Adapter 的最终广告一致，也与块 Oracle 一致。

## 计数

| 指标 | 值 | 标记 |
|---|---:|---|
| `native_events_processed` | 252 | `TRACE_REPLAY` |
| `adapter_updates_generated` | 243 | `TRACE_REPLAY` |
| Gateway `received` | 243 | `OBSERVED` |
| 其中被 merge 替换、没有发出 | 38 | `OBSERVED`，`superseded` |
| `gateway_updates_forwarded` | 205 | `OBSERVED` |
| `dispatcher_updates_applied` | 205 | `OBSERVED` |
| `global_topk` 丢弃 | 0 | `OBSERVED` |
| `replica_cap` / `low_utility` / `queue_drop` / `stale_cell` | 0 | `OBSERVED` |
| 排队峰值 `maxq` | 3 | `OBSERVED` |
| 排空后的 `coverage_mismatch_count` | 0 | `TRACE_REPLAY` |
| `false_positive_coverage_tokens` | 0 | `TRACE_REPLAY` |
| `false_negative_coverage_tokens` | 0 | `TRACE_REPLAY` |
| `invalidation_errors` | 0 | `TRACE_REPLAY`。这条 trace 没有把覆盖收到 0 |

243 = 205 + 38。少掉的 38 条是同一条 lineage 上还没发出就被更新的 UPSERT，不是 Top-K 丢掉的，也不是表示错误。

205 条 64 字节帧是 13120 字节（`CODE_DERIVED`）。Adapter 若每条更新都发出是 15552 字节。Native msgpack 是 284529 字节。13120 相对 284529 的差，绝大部分来自 Adapter 不再发送 token 和逐块哈希；Gateway 在此之上只少发了 38 条、2432 字节。这 2432 字节是 Semantic Reduction 的增量，不是 Top-K 的增量。

## 三个检查点

时间序列在 `coverage_e2e_timeseries.csv`。Dispatcher 只在检查点采样，不在每一条 Adapter 更新上采样。

| 检查点 | Oracle / Adapter | 第一次看见的 Dispatcher | 排空后的 Dispatcher | 标记 |
|---|---:|---:|---:|---|
| 覆盖从 1040 增到 1056 | 1056 | 1056，当时已应用 2/2 | 1056 | `TRACE_REPLAY` |
| 缩短到 448 | 448 | 1056，当时已发 240、已应用 200，在途 40 | 448，已应用 203 | `TRACE_REPLAY` |
| trace 结束 | 416 |  | 416，101/101 键一致 | `TRACE_REPLAY` |

缩短那一组更新来自同一条 ZMQ 消息，接收时间相同，所以它们会一起进入 Gateway。第一次采样时，旧的 1056 还在 Dispatcher 里，40 条更新还在路上。这是传播延迟。排空后覆盖是 448，448 个仍可复用的 token 没有被收成 0。

trace 尾部同一条 lineage 再缩短到 416。Dispatcher 最终也是 416。

## Test A–D

**A，覆盖增长。** 同一 lineage 从 1040 到 1056，Dispatcher 见到 1056。同一时间戳上的多次缩短被 merge 收成最后一条，没有把旧的高覆盖留在待发队列里。

**B，部分驱逐。** Oracle 在该消息结束时是 448，Adapter 写出 448，Gateway 没有把待发的高覆盖留到最后，Dispatcher 排空后是 448。结束时是 416。没有用 Tombstone 代替这次缩短。

**C，完整失效。** 这条 Native trace 里没有“覆盖收到 0”的广告。完整失效放在受控回归里，标记 `SYNTHETIC_REGRESSION`，见 `REPLICA_INVALIDATION_REGRESSION.md`。

**D，重复更新。** Adapter 重放时，8 次重复 `BlockStored` 没有产生更高的覆盖，Oracle 失配为 0。Gateway 这条路径上没有把已删除的键重新写活；trace 本身没有删除后的重复 UPSERT。删除之后的旧 UPSERT 复活见版本回归，那是 `SYNTHETIC_REGRESSION`。

## 在途和语义错误

第一次看到 1056、而 Oracle 已经是 448，发生在相关更新还没有 Apply 的时候。全部相关更新 Apply 之后，这个差消失。排空后的全表没有剩余的语义错误。
