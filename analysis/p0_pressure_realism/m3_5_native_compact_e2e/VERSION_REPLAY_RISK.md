# P0-M3.5 Version Replay Risk

标记：`STALE_UPDATE_RESURRECTION`。

帧里没有版本。Gateway 和 Dispatcher 都不会用生成顺序拒绝一个后到的旧 UPSERT。下面的顺序是受控回归，旧广告的“旧”来自它的发送序号，不是因为它的覆盖数字更小或更大。标记 `SYNTHETIC_REGRESSION`。

## 实测顺序

Digest 501，owner 0，每一步都等上一帧 Apply 之后再发下一帧，所以不存在“还在排队所以被 merge 掉”。

| 顺序 | 帧 | Dispatcher 覆盖 |
|---:|---|---:|
| 1 | UPSERT 1056 | 1056 |
| 2 | UPSERT 448 | 448 |
| 3 | 迟到的旧 UPSERT 1040 | 1040 |
| 4 | TOMB | 键不存在 |
| 5 | 更早状态的 UPSERT 1056 | 1056 |

第 3 步把已经正确的 448 恢复成 1040。第 5 步在 Tombstone 之后重新创建了 affinity。Gateway 的 drop 计数没有拦住它们：两次测试的 `superseded` 都是 0，因为旧帧到达时队列里已经没有同键的待发更新。

Dispatcher 的历史是 1056、448、1040、tomb、1056。`on_frame` 按到达顺序覆盖。

## 现有机制里没有的保护

- Gateway merge 只替换还没发出的同键 UPSERT。已经发出的 448 不会挡住后到的 1040。
- Replica dominance 比较的是不同 owner 的覆盖，不比较同一 owner 的新旧。
- Top-K 不记录“448 比 1040 新”。
- Tombstone 删掉索引项，但不留下“这个版本已经作废”的记录。更旧的 UPSERT 会把键再建出来。
- 同一条连接上的有序回放不能外推到重试、第二条连接或 Gateway 重启之后的重放。

真实 trace 的有序回放没有插入这种迟到帧，所以 Phase 1 的 101 条匹配不推翻这里的结果。
