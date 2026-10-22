# P0-M3.5 Replica and Invalidation Regression

P0-C1 只有一个 owner。下面全部是 `SYNTHETIC_REGRESSION`。策略仍是 `StaticTopK`，replica cap 为 2，invalidation priority 打开。没有调用 `K_RESET` 来清空一个 owner。

## Replica suppression

同一 digest：

| Owner | 发送 | Dispatcher |
|---|---|---:|
| A | 1056，随后 448 | 448 |
| B | 1024，保持 | 1024 |

Gateway `replica_cap` 丢弃为 0，`superseded` 为 0，三条都转发。Coverage dominance 没有留下 A 的 1056，也没有把已经缩短的 A 从副本集合里删掉。cap 为 2 时这两个 owner 都在名额内。原始 trace 没有第三个 owner，所以“第三个更高覆盖把名额挤掉”没有测。

## Owner 清空

Owner 0 有三条 lineage，Owner 1 与其中一条共用 digest。对 Owner 0 的三条发 `K_TOMB`。

| 键 | 清空之后 | 再注入 Owner 0 的旧 UPSERT 之后 |
|---|---|---|
| Owner 0 的三条 | 全部不在索引里 | 被注入的那条回到 1056 |
| Owner 1 的同一 digest | 1056 | 1056 |

清空没有碰到另一个 owner。8 条帧全部转发，其中 4 条 UPSERT、3 条 TOMB、1 条迟到的 UPSERT。`global_topk` 丢弃为 0。排队峰值是 4，没有形成需要靠优先队列才能赶上的积压，所以 Tombstone 相对 UPSERT 的延迟优势这次是 `NOT_MEASURED`。代码里 `priority=1` 时 `K_TOMB` 进入 `pqueue`，`_dequeue` 先取优先队列。

迟到的 UPSERT 在 Tombstone 之后把 Owner 0 的键再建出来。这是版本缺口，不是“清空误伤了另一个 owner”。

## 缩短要不要走失效优先

当前缩短是普通 FIFO UPSERT。本条 trace 排队峰值为 3，它能排空。Scenario C 说明，名额满时这条普通 UPSERT 可以被 Top-K 丢掉，已经发出的高覆盖就留在 Dispatcher 里。

若要把这种修正当成必须及时送达的状态，就得改变准入：已发布 digest 的新状态不再和陌生 lineage 竞争 K 个名额，或者给它单独的优先级。本轮只记录这个缺口，没有加规则。
