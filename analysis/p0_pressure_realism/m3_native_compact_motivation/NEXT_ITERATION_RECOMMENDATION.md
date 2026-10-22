# P0-M3 Next Iteration

停在这里，等下一次 OODA。不要自动开始 Phase 3 或 Phase 4。

## 已经可以写进动机笔记的事实

- 一条 vLLM 0.10.2 engine、59.885 秒、212 条 msgpack，平均 38.01 kbit/s。哈希出现 6615 次，网络消息不是 6615 条。
- 精确块索引在部分驱逐后仍能回答 448 个 token。末端 tombstone 的答案是 0。Dynamo `/query` 在同一事件边界返回 448。
- 用分支根做稳定 ID、用更短的 `K_UP` 表示剩余覆盖，这条 trace 上的可复用覆盖与块 Oracle 一致。正式 KVSelect 的 lineage 字符串不在 trace 里，这个 ID 是 `DERIVED`。
- 现有索引按最后一次写入覆盖。没有版本号时，旧的 1040 会把 448 盖掉。

## 下一次若继续，先做的决定

1. 是否接受“缩短覆盖的 `K_UP` + 单调版本”作为最小协议。不接受，就不要把 compact 前缀说成保留了块级可见性。
2. 决定之后，才在这条同一 `frames.bin` 上跑 Phase 3。四条路径必须从同一事件顺序出来。Baseline B 用本轮转换器的输出，不用 P0-A1 的 wrapper 更新。
3. 删除批次可以先合成一条缩短更新，再与逐块 41 次更新并列报告。不要只报合成后的更小字节数。
4. Phase 4 仍然先不要跑。单 worker 38 kbit/s 乘 reporter 数不是测到的链路拥塞。

## 不要做

- 不要启动 vLLM 或 GPU 重采集。这条 trace 对块覆盖是够的。
- 不要改 KVSelect 主体、K=16、Adaptive、帧格式或论文。
- 不要重跑 formal4t4 或 Run A。
- 不要把 15552 字节对 284529 字节写成 KVSelect 的收益。
- 不要把 4000 updates/s 的 Dispatcher 容量实验再拿来证明索引拥塞。M1.5-C 的结论仍然是：测到的范围内没有饱和。

## 复现

```text
cd /home/byh/B02
PYTHONPATH=/home/byh/B02 poc/.venv/bin/python \
  analysis/p0_pressure_realism/m3_native_compact_motivation/replay_m3.py
```

只重算 Oracle 和转换器、沿用已经写好的 Indexer 查询时，加 `M3_SKIP_INDEXER=1`。Git HEAD 是 `f3adf24fcf111c2bedcd64f3d28a0a9496d1abed`。Trace 是 `analysis/p0_pressure_realism/c1_real_indexer/raw/frames.bin`。Dynamo commit 是 `7a2a40906`。
