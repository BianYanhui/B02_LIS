# P0-M3 Block-to-Prefix Converter Audit

最小转换器把已经观察到的块事件收成每个分支一条覆盖。它没有改 KVSelect 的准入、Top-K 或 K。Phase 3 的四条路径没有跑。

身份是 `DERIVED_BRANCH_ROOT`：新分支的稳定 ID 是该分支第一个新块的 8 字节哈希。覆盖是从根开始、直到第一个洞为止的存活 token 数。洞后面的块不算进这条前缀。

## 事件语义

`BlockStored`：只把这次新出现的块接上。父块必须已经在树上，或者这次就是根。父块缺失次数是 0。重复存储不增加覆盖；trace 里有 8 次。新的可复用覆盖才发出 `UPSERT`。

`BlockRemoved`：重新计算受影响分支的连续覆盖。还剩祖先时发出更短的 `UPSERT`。覆盖变成 0 才发 `EVICT`。trace 里 41 次删除都是缩短，`EVICT` 次数是 0。

`AllBlocksCleared`：对每个还活着的广告发 `RESTART`，并从转换器状态里去掉该键。线上对应物是每个 digest 一条 `K_TOMB`。`K_RESET` 在 Gateway 里清的是整个 cell 的队列和回执，不是一个 owner 的前缀表（`experiments/4t4/net/gateway_relay_4t4.py` 里对 `K_RESET` 的处理）。trace 里的唯一一次清空发生在任何广告之前，所以 trace 上的 `RESTART` 是 0。清空活广告的检查是 `SYNTHETIC`。

输出：243 次 `UPSERT`，0 次 `EVICT`，0 次 `RESTART`。若每条都装进现有 64 字节帧，载荷是 15552 字节（`DERIVED`）。这是表示转换的字节数，不是 KVSelect 过滤后的字节数。

## 1040 到 448

C1 的广告末端是 `38a2855819ec6b90`，广告覆盖 1040，批次结束时祖先还剩 448，粗 tombstone 的可见覆盖是 0。

本轮在同一接收时间：

| 视图 | token | 标记 |
|---|---:|---|
| 块 Oracle 的连续覆盖 | 448 | `TRACE_REPLAY` |
| Dynamo `/query`，1040 token 与前 448 token | 448 | `TRACE_REPLAY` |
| 洞后仍存活的块 | 0 | `TRACE_REPLAY` |
| 缩短覆盖的转换结果 | 448 | `TRACE_REPLAY` |
| 末端 tombstone | 0 | 与 C1 Model B 相同 |

这一条 lineage 在该消息之前的覆盖是 1056。1056 含 C1 没有单独广告的 16 token 尾块。同一条 ZMQ 消息里的删除把覆盖从 1056 收成 448。现有 `Dispatcher.on_frame` 对这个 digest 先写入 1056、再写入 448，索引里留下 448（`TRACE_REPLAY`，进程内调用，没有走网络）。

被实际删掉的是 592 个 token（1040 减 448）。粗 tombstone 额外丢掉的是仍然可复用的 448 个 token，占原 1040 广告的 0.4308。转换器没有丢掉这 448。

同一条链在 trace 结束时还剩 416。Dynamo 对原来的 1040 token 序列也返回 416。转换器的最终广告是 416。若在打断点发过 tombstone 并且没有新的祖先广告，可见覆盖仍是 0。

另一条链只从 1056 收到 1040，损失的是 16 个尾 token，1040 的前缀还在。41 个被删块全在这两条链上：回归链 40 块，另一条 1 块。

整段 trace 上，转换器与 Oracle 的逐事件比较：false positive 0，丢失的可复用 token 0，错误作废 0。结束时 101 条广告的覆盖之和与 Oracle 相同，都是 106000。

## 正确性检查

| 检查 | 结果 | 标记 |
|---|---|---|
| 连续 `BlockStored` | 4 块再接 2 块，覆盖 96，与 Oracle 相同 | `SYNTHETIC` |
| 重复 `BlockStored` | 覆盖保持 64，广告仍是 1 条 | `SYNTHETIC` |
| 部分 `BlockRemoved` | 4 块删掉末端，覆盖 48 | `SYNTHETIC` |
| 整段删除 | 广告被 `EVICT`，剩余覆盖 0 | `SYNTHETIC`。trace 中 `NOT_OBSERVED` |
| `AllBlocksCleared` | 活广告被撤销，剩余 lineage 0 | `SYNTHETIC`。trace 里的清空发生在广告之前 |
| 两个 owner | `Dispatcher.on_frame` 对 worker 0 和 1 的同一 digest 都保留 1040 | `SYNTHETIC`。trace 只有 dp rank 0 |
| 同一 owner 的不同链 | trace 上 101 条 lineage，与 101 个 prompt 哈希一一对应 | `TRACE_REPLAY` |
| 祖先还在、后继已删 | 448 回归，洞后存活块 0 | `TRACE_REPLAY` |

`SYNTHETIC_QUERY` 只用于上表里标成 `SYNTHETIC` 的行。它们不代替 448 这个 trace 结果。

## 现有协议能表达什么

`Dispatcher.on_frame` 对 `K_UP` 的动作是用新覆盖替换 `(worker, digest)`。同一 digest 上 1040 之后的 448 会留下 448。随后再来一条覆盖为 1040 的 `K_UP`，索引回到 1040。帧里没有版本号。这与 M1.5-B 里“后到的旧覆盖可以覆盖新值”是同一条写入规则。

同一 owner 的更短覆盖，在 Gateway 的 replica 表里会更新已有 holder（`_admit_replica`：instance 已在 holder 中则写上新覆盖并返回真）。这是代码阅读，本轮没有再跑 Gateway 准入实验，标记 `CODE_INSPECTION`。

`K_TOMB` 会删掉整个键。用它表示“末端没了”，祖先的 448 token 也一起从索引消失。

`K_RESET` 不能当作单个 engine 的 `AllBlocksCleared`。它清的是 Gateway cell。

Top-K 的 `topk_best` 只在新覆盖更大时提高。本轮没有跑 Top-K，这个行为和固定 K=16 的丢弃量都是 `NOT_MEASURED`。

## 最小协议建议

不改现有算法。若要让缩短覆盖在乱序下仍然安全，给每个 `(worker, digest)` 增加单调版本：只应用版本更新的 `K_UP` 或 `K_TOMB`。版本更新的 448 可以替换 1040；版本更旧的 1040 不能把已删除的块写回去。

在这个字段出现之前，有序回放上的缩短覆盖可以被现有索引收下，乱序下的正确性没有被现有帧保证。

本轮没有改帧格式，没有改 K，没有恢复 Adaptive，没有改正式 Gateway 的准入。
