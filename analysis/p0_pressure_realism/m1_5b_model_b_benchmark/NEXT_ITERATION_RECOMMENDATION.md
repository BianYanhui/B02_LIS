# 下一轮

Phase 1 的 Owner 和 tombstone 通过了。Phase 2 能把窗口效应和 Coverage Top-K 分开。可以测 Dispatcher 的 apply 容量，但本轮没有测。

## 建议的容量配置

用 FullSync、`max_inflight=0`、无 sleep、无 `tc`。StaticTopK 会在突发时丢掉 lineage，转发速率会低于入口速率，那个差不是 Dispatcher 饱和。

同一套进程上再跑一列 StaticTopK，只用来对照转发了多少，不把它的转发速率写成索引容量。

总入口用 250、500、1000、2000、4000 updates/s。这些是测试点，不是规模外推本身。每档预热 5 秒，测量 30 秒，然后看排空，重复 3 次。若 apply 跟不上且积压持续变长，就停止加档。

Lineage 必须互异。同时写出 `N_distinct_workers` 和 `N_distinct_lineages`。Worker 仍用 0–255 的全局编号，不扩展帧。

## 风险

单线程 Gateway 和 Python 发送端可能先到顶。若入口打不满，标 `HARNESS_LIMITED`，不要写成索引饱和。

`on_frame` 的几微秒不能外推成每秒二十万次。读帧、ACK 和事件循环都在路径上。

不要用 Apply sleep 的 4 秒延迟论证生产 Dispatcher 会积压。
