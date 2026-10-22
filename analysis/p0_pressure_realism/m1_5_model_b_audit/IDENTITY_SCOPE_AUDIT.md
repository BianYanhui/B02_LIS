# 身份作用域

`CODE_CONFIRMED`：帧头是 `kind u8 | instance u8 | cell u16 | seq u32 | coverage i64 | digest64 u64`（`gateway_relay_4t4.py` 23–25、92 行）。没有 Gateway 标识。

## 字段分别是什么

`instance` 被当成全局 Worker 编号使用，不是“某个 Gateway 内的局部编号”。Gateway 进程不改写这个字节。

ICC 的 `frame()` 用 `instance & 0xFF`（`wire.py` 43–44）。256 和 0 写成同一个字节，函数不报错。Gateway 自己的 `frame()` 用 `HDR.pack`，大于 255 会在发送端抛出 `struct.error`。两条路径对越界的处理不一样：一条静默别名，一条拒绝。

`cell` 是实验轮次，u16。`seq` 是发送计数。ICC Dispatcher 的索引键不用 seq。Gateway 的 `recent` 用 seq 匹配 ACK，作用域是这一只 Gateway 进程。两只进程各自计数，互不影响。

论文里的 lineage version `q` 不在帧里。Supersession 靠后到达的同一 `(instance, digest)` 覆盖先到达的未发送帧，不比较版本号。

`digest` 是 lineage 字符串的 64 位摘要。同一前缀在两个 Gateway 上摘要相同，这是同一 lineage，不是冲突。不同前缀撞摘要的概率本轮没有测量。

## Gateway 0 / Worker 1 / Prefix X 与 Gateway 1 / Worker 1 / Prefix X

若两只 Gateway 都写入 `instance=1` 和同一个 digest：

- Dispatcher 的键是 `(worker, digest)`（`on_frame` 200–206）。后到的 upsert 覆盖先到的 coverage。
- 任一 Gateway 的 tombstone 都会 `pop` 这一个键。它分不出原 Owner 属于哪一只 Gateway。
- 副本表在各自进程里。Gateway 局部的 `K_rep=2` 看不到另一只 Gateway 已经保留的 Owner。Dispatcher 侧也没有全局副本上限。局部抑制和全局可见性不是同一件事。

因此这两个缓存 Owner 在当前帧里表示成同一个 Owner。

不超过 256 个汇报者时，如果 harness 分配互异的全局 `instance`（0–255），现有字节够用，不需要改帧。Model B 图里每只 Gateway 从 Worker 0 重新编号，代码不支持。1024 和 4096 个汇报者超出一个字节。P0-M1 已经把这个边界记为 `DERIVED`。

formal4t4 的索引是四张表（`run_formal4t4.py` 242 行）。`instance` 同时是 list 下标，只覆盖 0–3。那份 harness 更不能表达 Model B。

## 最小修复，本轮不实施

不改 Top-K、不改 K=16、不恢复 Adaptive。

1. 256 个以内的 CPU 试验：harness 分配全局 `instance`，禁止每只 Gateway 从 0 编号。帧格式可以不动。
2. 要表达局部编号，或要超过 256 个 Owner：把 `instance` 加宽到 u16，或增加一个 `gateway_id` 字节。索引、tombstone、supersession 都改用 `(gateway, instance, digest)`。副本表仍留在各 Gateway 进程内。

这是身份字段的修改，不是准入算法的修改。
