# ACK 与反压

结论先说：当前 ICC 路径有应用层 ACK，它产生在索引更新之后。StaticTopK 不拿它做发送窗口。下游变慢时，能回到 Gateway 的只有 TCP。Top-K 只看还没交给内核的队列。

## 生命周期

Worker 或 harness 把 64 字节帧写到 Gateway 的 `agent_reader`。`enqueue` 做归并、副本上限和 Top-K，然后放进 `queue` 或 `pqueue`。

`release_loop`（705–752）取出一帧，立刻从 `topk_best` 里释放这个 lineage（`_dequeue` 636–637），再 `write` 并 `drain`。注释写明：已经写进内核的帧留在内核里（`trim_global_topk` 548–549）。

Dispatcher 的 `_on_down`（371–376）对每一帧先调用 `on_frame`，再写 `K_ACK`。`on_frame` 在同一调用里更新 `tested[(worker, digest)]`。没有单独的 apply 队列，也没有“先入队、稍后才可见”的阶段。

因此 ACK 的阶段是 `ACK_ON_APPLY`。索引在同一语句块里已经可见，不存在更晚的 `ACK_ON_INDEX_VISIBLE`。它不是 `ACK_ON_RECEIVE` 之前的空 ACK，也不是 `ACK_ON_ENQUEUE`。

formal4t4 的 `_on_downstream`（491–506）同样是先 `apply_upsert` / `apply_tombstone`，再写 ACK。注释写的用途是给 adaptive gate 提供延迟。

## max_inflight

`release_loop` 716–719：只有 `max_inflight > 0` 且 `recent` 里未确认的序号达到这个数时，发送才停。`ack_reader`（693–702）按序号弹出 `recent` 并更新 `ewma_dq`。

ICC 把这个值设为 0。发送循环不看 ACK。`ewma_dq` 仍会被更新，但 `congested()` 在 `adaptive` 为假时返回假，StaticTopK 不用它做准入。

formal4t4 默认是 4。那条路径上，ACK 窗口会让未发送队列变长。不能把这个默认值套到 ICC 的 StaticTopK 实验上。

## Dispatcher 变慢以后

`on_frame` 没有 `await`。它慢的时候会占住同一个 asyncio 循环，另一条 Gateway 连接的读也会停。连接本身没有应用层队列。读停之后，内核接收缓冲区变满，对端 `drain()` 才会堵住。

堵住之后，`release_loop` 停在 `drain`，新帧留在 Gateway 的 `queue` 里，Top-K 才会继续筛选。在缓冲区填满之前，已经 `write` 出去的帧不在 `topk_best` 里。

Gateway 侧发送缓冲被刻意设小：`SO_SNDBUF` 默认 2304（822 行），asyncio 高水位 2048（682 行）。接收侧没有设置 `SO_RCVBUF`。接收缓冲区的深度是内核默认值，本轮 `NOT_MEASURED`。所以“下游已经积压、Gateway 待发集合仍然很短”在接收缓冲被填满之前成立。

Dispatcher 的 `applied` 列表只追加记录（210 行），不参与调度。它会随帧数增长，但不是处理队列。索引字典按键覆盖，不会为同一 `(worker, digest)` 堆多份待处理更新。

## 对 Q2 的直接回答

1. ACK 在 apply 之后产生。
2. 存在应用层 `K_ACK`。ICC 的发送窗口不使用它，实际限流是 TCP。
3. `max_inflight=0` 时不限制在途帧数。
4. Apply 变慢要先填满接收缓冲，Gateway 的 `drain` 才会停。
5. 没有可无限增长的 apply 队列。遥测列表会增长。
6. 在内核缓冲填满前，Gateway 的待发集合可以仍然很短。
7. 静态 Top-K 只有在帧还留在用户态队列时才起作用。
