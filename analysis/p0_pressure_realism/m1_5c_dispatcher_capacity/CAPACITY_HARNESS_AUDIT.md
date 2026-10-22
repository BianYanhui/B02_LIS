# 容量 Harness 审计

## FullSync 是否全量转发

配置来自 `experiments/icc_kv/wire.py` 的 `POLICIES["FullSync"]`，元组是 `(0, 0, 0, 0, 0, 0)`。`gateway_relay_4t4.py` 的 `enqueue` 没有一条叫 passthrough 的短路。`passthrough` 属性存在，但发送路径不读它。FullSync 是把下面这些门都关掉：

| 门 | 条件 | FullSync |
|---|---|---|
| 令牌桶 | `consume_rate_token`，只在 mode 1 | 不生效 |
| 队尾丢弃 | `_bounded_full`，只在 mode 6 | 不生效。`max_queue` 只被存下来 |
| utility | `low_utility` 先看 `congested()`，`adaptive` 为假时直接返回假 | 不生效 |
| Top-K | `_topk_drop_new`，`global_topk=0` 时 limit 为 0 | 不生效 |
| 合并 | `merge=0` | 不取消旧的未发送帧 |
| 副本上限 | `_admit_replica` 在 `dedup=0` 时直接返回真 | 不生效 |
| ACK 窗口 | `max_inflight=0` 时 `release_loop` 不等 ACK | 不生效 |
| 过期 | `drops["expired"]` 有计数槽，当前文件里没有自增 | 保持 0 |

会留下的丢弃只有 `stale_cell`：帧的 cell 和当前 cell 不一致。Harness 在 reset 完成并看到新的 `downstream_connected` 之后才发送。另外，读循环每 256 帧 `await sleep(0)` 一次，这是已有代码，本轮不改。它会在读端跑在发送端前面时，把最多 256 帧先放进用户态队列。

`release_loop` 对每一帧 `write` 然后 `await drain()`。下游 `SO_SNDBUF` 请求值 2304，写缓冲高水位 2048，`TCP_NODELAY=1`。这些是 Gateway 程序自己的设置。

Smoke 计数：

- smoke_S1_g2_r250_rep1: received=3524 forwarded=3524 applied=3524 drops=0 checksum=True verdict=STABLE reason=apply tracked offered rate, path backlog did not accumulate, drain finished
- smoke_S2_g2_r250_rep1: received=2500 forwarded=2500 applied=2500 drops=0 checksum=True verdict=STABLE reason=apply tracked offered rate, path backlog did not accumulate, drain finished

Smoke 通过：True。通过的定义是整次运行 `generated == gateway_received == gateway_forwarded == dispatcher_received == dispatcher_applied == acks`，丢弃计数为 0，索引校验和一致，判定 `STABLE`。

## 计时

速率控制用 `time.monotonic_ns()`，按帧的绝对截止时间发送，落后时每批最多补 8 帧。不是每秒一次倒空。

`t_generated` 写进帧里的 `t_send`，是发送线程的 `time.time()`。同一台机器。

`t_gateway_sent`：`NOT_MEASURED`。Gateway 原样转发收到的帧，不改写 `t_send`。`--quiet` 关掉了逐帧日志，避免 4000 行/秒的日志变成瓶颈。

`t_apply_start` / `t_apply_done`：`perf_counter` 包住 `Dispatcher.on_frame`。均值用全部测量窗帧。P50/P95/P99 是每 32 帧留一个样本。

生成到 Apply 完成：抽样帧上的 `time.time()` 减帧内 `t_send`。它包含 Gateway 排队、TCP 和 Apply，不是索引 CPU。

Gateway 发送到 Apply、Gateway 发送到 ACK：`NOT_MEASURED`。ACK 回到 Gateway 的下游连接，不回到发送线程。`recent` 只留 4096 个 seq，`ewma_dq` 只覆盖最近这些帧，表里单列，不当作全窗分位数。

Dispatcher 内部 `received - applied` 只在 `on_frame` 前后被看到，测量到的最大值是正在处理的那一帧。没有单独的 Apply 队列，表里不造一个队列长度。`generated - applied` 是整条路径还没完成的量，包含发送端和 Gateway。

## 身份

`N_distinct_workers=16`，编号 0–15，两只 Gateway 时各 8 个，四只时各 4 个。不复用编号。

S1：每个 Worker 64 条 lineage，共 1024 个键。先各插入 coverage 1024，再按轮转把 coverage 加 16。帧长仍是 64 字节。

S2：每条更新一个新的 blake2b-8 digest，名字是 `P0M15C/S2/<run>/g<i>/n<j>`，coverage 固定 1024。这是高基数合成插入，不是 4096 Reporter 回放。

## 什么不算本轮容量

窗口速率的起止有一个管线深度的边界，所以全量相等在排空之后检查，不在 30 秒窗口的两个端点上检查。4000 updates/s 以上没有自动加档。
