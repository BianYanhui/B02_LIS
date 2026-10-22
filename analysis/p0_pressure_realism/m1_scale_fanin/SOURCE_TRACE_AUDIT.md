# P0-M1 Phase 0：源 Trace 审计

标签：`OBSERVED` 表示文件里读到的事实。本轮没有启动 vLLM，没有重放，也没有生成多 Worker 事件日志。

结论先说：单源的时间和前缀身份可以恢复。独立的多 Worker 身份不能恢复。P0-C1 的 64 倍回放是同一条连接上的时间压缩，不是 64 个 Worker 的 fan-in。因此本轮只做速率和相位外推，不构造大规模 Replay 文件。

## 1. 还在的原始记录

| 来源 | 路径 | 内容 | 身份 |
|---|---|---|---|
| P0-A1 正式基线 | `analysis/formal4t4/raw/baseline/requests_baseline_*.csv` | 打分请求的 `lineage_id`、`digest`、`selected_instance`、`dispatch_time_unix`、coverage | 四个实例，但只覆盖打分请求 |
| P0-A1 汇总 | `analysis/p0_pressure_realism/a1_baseline_audit/metadata_generation_baseline.csv` | 每个 cell 的 upsert 计数和活跃时间 | 没有逐条 upsert |
| P0-A3 | `analysis/p0_pressure_realism/a3_native_events/native_event_trace.csv` | 单卡事件时间、hash、parent、`BlockStored` / `BlockRemoved` / `AllBlocksCleared` | `worker_id` 恒为 0 |
| P0-A4 | `analysis/p0_pressure_realism/a4_invalidation_audit/prefix_state_transitions.csv` | 由 A3 重建的前缀 UPSERT / INVALIDATE，含 coverage 和 version | `worker_id` 恒为 0 |
| P0-C1 | `analysis/p0_pressure_realism/c1_real_indexer/raw/frames.bin` | 212 条 ZMQ 消息的原始 msgpack，284529 字节载荷 | 一个发布端 |
| P0-C1 | `.../c1_real_indexer/raw/events.csv` | 252 个事件，6615 次 hash，`recv_ns`、`batch_seq`、parent、`n_blocks` | 没有 worker 列 |

这几份记录来自不同实验。A1 是四卡 serving wrapper。A3/A4 是另一张卡上的原生事件及其前缀重建。C1 是后来单独抓的一条 msgpack。它们不能拼成一条“真实大规模 trace”。

## 2. 逐项核对

1. 原始 trace 还在。上面的 CSV 和 `frames.bin` 都在磁盘上。
2. Native msgpack 只在 C1 的 `frames.bin`。A3 的 CSV 没有原始字节。
3. 时间可以恢复。C1 有 `recv_ns` 和 `batch_seq` 0–211。A4 前缀转移有 `timestamp_ns`。A1 只有打分请求的 `dispatch_time_unix`，warmup 和 overlap seed 不在请求 CSV 里，所以正式 cell 的完整 upsert 时序不能重建。
4. Worker、lineage、version 只有部分。A1 打分请求有 `selected_instance` 和 `lineage_id`。A4 有 version，但 worker 只有 0。C1 事件表没有 worker，也没有 lineage 字符串。
5. `BlockStored`、`BlockRemoved`、清空都在 A3 和 C1 里。C1 全段 252 个事件，其中 flood 窗口 221 个。
6. 单卡覆盖度可以沿 A4 的 `reusable_coverage_after` 重建。多卡覆盖度不能重建，因为没有第二张卡的状态。
7. 现有 background generator（`icc_kv` 的 noise、supplemental fan-in）按公式造逻辑源。本轮没有用它当测量源。
8. 现有 gateway 用帧头里的一个 `instance` 字节区分逻辑 Worker。四卡实验里这四字节是四个进程。C1 回放没有使用这个字段。

Token id 在 C1 的 msgpack 载荷里，没有导出到 `events.csv`。本轮没有解码它们。

## 3. 64 倍回放不是 64 个 Worker

`analysis/p0_pressure_realism/c1_real_indexer/run_c1.py` 的回放把同一批 `frames.bin` 按时间压缩重发。序号改成单调计数，载荷里的 hash 不改。Indexer 的启动参数是一条连接：`--workers 0=tcp://127.0.0.1:5557`。

因此 1× / 4× / 16× / 64× 共用：

- 同一个 Worker 身份
- 同一组 block hash 和 parent
- 同一条 ZMQ 连接

64× 达到约 256 events/s，排空后积压为 0。这只说明 debug indexer 跟上了这一条复制流。它不是 64 个独立汇报者，也不能当作索引饱和吞吐。重复更新同一批键会低估真正的索引维护成本。

## 4. 单源速率（后面外推的底数）

| 源 | 记录 | 事件 | Hash | 字节 | 标签 |
|---|---|---|---|---|---|
| A1 Original，4 个引擎合计 | 2.071 upsert/s（种子 1.533–2.967） | 同左 | 不适用 | 1723 bit/s 记账 | OBSERVED，集群 |
| A1 Reuse，4 个引擎合计 | 1.071 upsert/s（0.847–1.403） | 同左 | 不适用 | 891 bit/s | OBSERVED，集群；tombstone 为 0 |
| A4 C_flood，1 张卡 | 1.928/s（115 upsert + 18 invalidation，68.984 s） | 同左 | 不适用 | 1.928×104 B/s 记账 | OBSERVED，单卡灌满，不是正式 serving |
| C1 全段，59.885 s | 3.540 msg/s | 4.208 events/s | 110.5 hash/s | 4751 B/s msgpack | OBSERVED，单卡 |
| C1 flood 窗口 | 3.800 msg/s | 4.613 events/s | 124.3 hash/s | 5323 B/s | OBSERVED，单卡窗口 |

A1 的逐引擎计数没有落盘，`per_worker_upserts_per_s = NOT_AVAILABLE`。后文把集群速率除以 4，这是均匀拆分，不是测到的单引擎分布。

单源短窗，非重叠分箱：

- A4 flood：10 ms 最多 2 条，100 ms 最多 2 条，1 s 最多 4 条。与 `burst_recalculated.csv` 一致。
- C1 全段消息：10 ms 最多 1 条，100 ms 最多 2 条，1 s 最多 6 条。1 s 内 hash 最多 195。

## 5. 为什么不生成大规模 Replay

独立 Worker 需要各自的序号空间。同一 token 前缀可以合法共享 hash；不同前缀不能靠全局随机替换 hash，否则 parent 链会断。现有文件里没有第二套前缀的 hash 链。

把 C1 复制 N 份只能得到字节速率的包络，键仍然是同一套。这种文件一旦被当成 fan-in 输入，就会重复 P0-C1 已经犯过的错误。本轮的 `offered_load_scaling.csv` 把这种复制标成 `COPIED_SINGLE_TRACE`，并且没有写出事件日志。
