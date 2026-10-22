# 多 Gateway 正确性

两只真实 Gateway 进程，一个真实 `Dispatcher.on_frame`。`StaticTopK`，`max_inflight=0`，无整形，无 Apply sleep。结果在 `correctness_results.csv`。

| 测试 | 判定 | 依据 |
|---|---|---|
| A Owner 隔离 | PASS | 4 次 apply，视图与期望一致。Prefix X 的 Owner 是 Worker 0 和 Worker 2 |
| B 失效隔离 | PASS | 只撤销 Worker 0 的 X。Worker 2 的 X、以及 Y、Z 仍在 |
| C 顺序版本 | PASS | 1024、2048、4096 按序到达后，索引为 4096 |
| C 旧覆盖后到 | 协议不保留版本 | 4096 已经 apply 之后再送 1024，索引变成 1024。帧里没有 version |
| D 两只 Gateway 各自归约 | PASS | 连接 Dispatcher 之前，两只 Gateway 的计数已经不同；最终 34 条索引与期望一致 |

D 在 Dispatcher 接受连接之前读到的计数：

| Gateway | Top-K 丢弃 | 副本丢弃 | Superseded |
|---|---:|---:|---:|
| 0 | 6 | 1 | 1 |
| 1 | 6 | 0 | 1 |

Gateway 0 对同一 digest 发了三个 coverage 同为 1024 的 Owner（0、1、8）。8 没有进入索引。Gateway 1 只有两个 Owner（2、3），副本丢弃是 0，两个都在，coverage 是 2048。局部 `K_rep=2` 没有变成全局上限。

同一 digest 的两次扩展只转发后者：Gateway 0 最终 2048，Gateway 1 最终 3072。每只 Gateway 的 superseded 是 1。

20 个按 coverage 排列的 lineage 与 merge、replica 一起竞争 K=16。最短的 6 个（coverage 100 到 600）在两只 Gateway 上都被丢掉，较长的留下来。两只 Gateway 的 lineage 集合互不包含。

A/B 失败才会停。旧更新覆盖新覆盖度是协议行为，不是 Owner 串号，所以继续做了反压对照。
