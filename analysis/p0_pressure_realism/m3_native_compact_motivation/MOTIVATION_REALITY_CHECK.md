# P0-M3 Motivation Reality Check

本轮只完成 Phase 0–2。Phase 3 和 Phase 4 是 `NOT_RUN`。因此不能把字节差记成 KVSelect 的收益，也不能把单条 trace 说成已经拥塞的网络。

## 判定

同时成立的是：

- `COMPACTION_FIDELITY_RISK`：对 C1 那种末端 tombstone 成立。1040 token 的广告在打断点仍有 448 token 可复用，tombstone 的可见覆盖是 0。
- `COMMUNICATION_COST_SUPPORTED_BUT_CONGESTION_UNPROVEN`：一条 worker 的原生 msgpack 平均 38.01 kbit/s。字节体积可以量化，共享链路拥塞没有被测量。

不成立的是：

- `NATIVE_TO_COMPACT_MOTIVATION_SUPPORTED` 不能作为总判。缩短覆盖的转换在这条 trace 上保住了 Oracle 覆盖；末端 tombstone 没有保住。两种 compact 不是一回事。
- `COMPACTION_DOMINATES_KVSELECT` 与 `KVSELECT_INCREMENTAL_VALUE_SUPPORTED`：四路径没有跑，Top-K 增量是 `NOT_MEASURED`。
- `INSUFFICIENT_NATIVE_SEMANTIC_EVIDENCE`：块覆盖和 Dynamo 查询是够的。正式 lineage 字符串仍然缺失，所以可行性是 `PARTIALLY_SUFFICIENT`，不是证据全无。

## 五个问题

**Q1.** 现有 P0-C1 trace 够做可信的块到前缀转换，范围限于一个 engine、有序事件、以及从块 token 重建的查询。212 条原始消息、父指针、token id 都在。存活块 6525 与 Dynamo dump 一致。101 个请求哈希和 101 个分支根一一对应。正式 KVSelect digest 不在 trace 里。判定 `PARTIALLY_SUFFICIENT`。

**Q2.** 缩短后的覆盖可以送进现有 `Dispatcher.on_frame`，不必改 Top-K、K=16 或准入规则。进程内写入先 1056 再 448，索引留下 448。同一接口再写入旧的 1040，索引回到 1040，因为没有版本号。最小改动是给 `(worker, digest)` 加单调版本，本轮没有实现。`K_RESET` 清的是整个 Gateway cell，不能代表一个 owner 的 `AllBlocksCleared`；那个清空要用每条广告自己的 `K_TOMB`。

**Q3.** 在 C1 的打断时间，精确块索引和 Dynamo 都返回 448，洞后存活块为 0。末端 tombstone 丢失这 448 个仍可复用的 token，占原 1040 广告的 43.1%。缩短覆盖的转换结果是 448，整段 trace 上与 Oracle 的覆盖差是 0。trace 结束时同一序列还剩 416，转换结果也是 416。另一条链只掉了 16 个尾 token，1040 的前缀还在；若对那条末端发 tombstone，会再丢 1040。

**Q4.** 表示转换有希望成为动机里的一个事实：原生载荷 284529 字节里，约 75.6% 是 token id 数组（重编码差值，`DERIVED`）；转换器 243 条更新若用 64 字节帧发送，是 15552 字节，约为 msgpack 的 5.5%。这 15552 字节是逐次缩短都发送的上界，不是 Semantic Reduction，也不是 Top-K。平均速率 38.01 kbit/s，最忙的 1 秒是 8769 字节。这个体积支持“单 engine 的块事件比一条覆盖广告大”，不支持“当前网络已经拥塞”。

**Q5.** 方向可以留着，四路径规模实验先不要跑。下一次若批准，只应先决定版本号是否要加，然后才在这条同一 trace 上做有序的四路径对照。不要外推 4096 个 reporter，不要为了让 Top-K 出现收益而注入突发，不要把 15552 对 284529 写成 KVSelect 的压缩比。

## 本轮没有做的归因

Native 到 compact 的字节差来自表示：一条链一个覆盖，帧里不带 token id，也不为每个块各发一帧。KVSelect 的时间合并、作废优先、副本抑制和固定 Top-K16 都没有打开。`native_to_compact_comparison.csv` 四行都是 `NOT_RUN`。
