# 反压与 Top-K

同一对 Gateway、同一个 Dispatcher、同一组输入。每只 Gateway 先写入一条 INV，再尽快写入 80 个互异 lineage，然后由 Gateway 0 对 INV 发 tombstone。K=16。无 `tc`。

`elapsed_s` 里包含 harness 在 Top-K 丢帧之后空等的时间（它在等一个永远不会达到的 160 次 apply）。比较时用 generation-to-apply 和 tombstone 延迟，不用这个空等。

Apply sleep 50 ms 是 `FAULT_INJECTED`。`on_frame` 本身的 CPU 时间中位数是 5–14 µs，这个数不是持续吞吐。

## 转发与丢弃

Gateway 0 收到 82 帧（INV、80 个 lineage、tombstone），Gateway 1 收到 80 帧。

| 配置 | 策略 | Gateway 0 转发 | Top-K 丢弃 | 用户态队列高水位 | INV tombstone 延迟 |
|---|---|---:|---:|---:|---:|
| A inflight 0，正常 | FullSync | 82 | 0 | 54 | 10 ms |
| A | StaticSemantic | 82 | 0 | 43 | 7 ms |
| A | StaticTopK | 33 | 49 | 17 | 13 ms |
| B inflight 4，正常 | FullSync | 82 | 0 | 77 | 17 ms |
| B | StaticSemantic | 82 | 0 | 77 | 3 ms |
| B | StaticTopK | 22 | 60 | 17 | 13 ms |
| C inflight 0，sleep | FullSync | 82 | 0 | 43 | 4.15 s |
| C | StaticSemantic | 82 | 0 | 43 | 2.01 s |
| C | StaticTopK | 44 | 38 | 17 | 1.39 s |
| D inflight 4，sleep | FullSync | 82 | 0 | 77 | 4.11 s |
| D | StaticSemantic | 82 | 0 | 77 | 0.26 s |
| D | StaticTopK | 22 | 60 | 17 | 0.25 s |

Gateway 1 的方向相同：FullSync 和 StaticSemantic 都转发 80；StaticTopK 在 A/C/B/D 分别转发 29、38、20、20。

`max_inflight` 是每只 Gateway 进程自己的下游窗口。两只进程的转发数不同，窗口没有合成一个全局计数。

## 窗口和准入不是同一件事

FullSync 在四种配置下都转发全部帧。Apply sleep 把它的 tombstone 推迟到约 4.1 s，inflight 0 和 4 几乎一样。窗口没有少做索引工作，tombstone 仍然排在 FIFO 尾部。

StaticSemantic 也不丢 Top-K。它和 FullSync 的差别是失效优先。inflight 0 加 sleep 时，tombstone 延迟 2.01 s：已经写进 TCP 的帧排在它前面。inflight 4 加 sleep 时，延迟降到 0.26 s。这是窗口把未确认帧限制在 4 个之后，优先队列才能马上发出 tombstone。正常速率下这个差别只有几毫秒。

StaticTopK 在正常突发里就已经丢帧：inflight 0 丢 49 和 51，inflight 4 丢 60 和 60。用户态高水位停在 16–17。80 个 lineage 一次写完时，读循环先于发送循环把它们放进待发集合，K=16 在 Dispatcher 仍然很快的时候就筛选了。这不是 Apply sleep 制造的。

inflight 0 让更多帧在后续更高 coverage 到达前离开待发集合，所以 sleep 时 Gateway 0 反而转发了 44，多于 inflight 4 的 22。窗口越小，还没发出去的 lineage 越多，Top-K 删得越多。

FullSync 在 sleep 下的用户态高水位是 inflight 0 的 43，对 inflight 4 的 77。多出来的帧留在用户态，而不是继续灌进内核。`drain()` 累计等待在所有配置里都不超过 3 ms；结束时 `TIOCOUTQ` 为 0。内核发送队列的峰值没有采到，标 `NOT_MEASURED`。能看见的吸收证据是这条高水位差。

轮询 3636 次，没有 stats 超时。Apply sleep 会让出事件循环，统计连接仍然能读。

## 筛选发生的条件

K=16 筛选未发送的 distinct digest。本轮测到两种：

1. 正常路径上的突发：一次送入的 lineage 多于 16，而且发送循环还没把它们写走。
2. `max_inflight=4`：未确认帧到 4 个就停发，后面的 lineage 留在待发集合里，丢弃更多。

已经 `write` 出去的帧不再回到 Top-K。Apply sleep 单独不会增加 Top-K 丢弃；inflight 0 加 sleep 时丢弃还少于正常突发，因为更早发出去的低 coverage 帧已经离开集合。
