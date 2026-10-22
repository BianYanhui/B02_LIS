# P0-M3 Native Trace Feasibility Audit

判定：`PARTIALLY_SUFFICIENT`。

块驻留、父子哈希、token 序列和按事件顺序的可复用覆盖，可以从 P0-C1 的原始帧恢复，并与 Dynamo Indexer 的查询结果一致。正式 KVSelect 使用的 lineage digest 不在这条 trace 里。多 owner、整段前缀删除、以及广告已经发出之后的 `AllBlocksCleared`，这条 trace 里没有。

本轮没有启动 vLLM，没有使用 GPU，没有重新采集。

## 证据来源

| 项 | 值 | 标记 |
|---|---|---|
| Git HEAD | `f3adf24fcf111c2bedcd64f3d28a0a9496d1abed` | 工作区另有未提交诊断改动，本轮没有改 KVSelect |
| 主机 | `cloud25` | |
| Trace | `analysis/p0_pressure_realism/c1_real_indexer/raw/frames.bin` | 290465 字节，未改写 |
| 请求表 | 同目录 `raw/requests.csv` | 109 行 |
| 事件表 | 同目录 `raw/events.csv` | 252 行，与重新解码的事件数相同 |
| vLLM | 0.10.2 V1 事件定义，用于解码已有 msgpack | 没有启动引擎 |
| Dynamo | commit `7a2a40906`，`dynamo.indexer` | 只做了一次有序回放，随后进程已退出 |
| 脚本 | `analysis/p0_pressure_realism/m3_native_compact_motivation/replay_m3.py` | |
| 数字汇总 | 同目录 `raw/summary.json` | |

命令：

```text
cd /home/byh/B02
PYTHONPATH=/home/byh/B02 poc/.venv/bin/python \
  analysis/p0_pressure_realism/m3_native_compact_motivation/replay_m3.py
```

P0-A3 的 CSV 去掉了 token id，单独不够做前缀查询。P0-A4 是另一条 trace，剩余覆盖记的是 528 token，本轮没有拿它代替 C1。

## 十条检查

1. 原始 ZMQ msgpack 还在。`frames.bin` 按 `<QIII` 头保存接收时间、topic、8 字节 seq 和 msgpack。212 条消息都能解码。
2. 可以按原始顺序重放。seq 为 0 到 211，缺口 0。首尾接收时间相距 59.885 秒。
3. Worker 身份只有一个。topic 是空字符串，`data_parallel_rank` 全部是 0。Indexer 命令里的 worker 0 是启动参数（`CONFIGURED`），不是每条事件里的另一套 owner 字段。
4. Block hash 和 parent hash 可以恢复。210 次 `BlockStored` 里 1 次 parent 为空，209 次带 parent。父哈希缺失 0，父子冲突 0。哈希都是 8 字节，`hash_width_bad = 0`。
5. Token id 足够构造受控前缀查询。每次存储的 token 数都等于块数乘 16，失配 0，token 冲突 0。`requests.csv` 只有 `prompt_blake2s`，没有 prompt 原文，也没有请求 token。查询序列来自已经存储的块，标记 `CONTROLLED_FROM_STORED_BLOCKS`。
6. 块驻留可以判断。按事件重放后的存活哈希是 6525，与 C1 当时对 Dynamo `/dump` 的参考集合相同。本轮再次 dump 也是 6525。
7. Dynamo 查询接口还在。`POST /query` 用 token id 重新计算块哈希。打断点返回 worker `0/0`、448 token；回放结束后同一条序列返回 416 token。
8. 原始 prompt 文本没有保存。109 个请求的时间窗互不重叠，101 个不同的 `prompt_blake2s`。210 次 `BlockStored` 每一次都落在恰好一个请求窗内。把窗尾放宽 80 ms 后有 9 次落入多个请求，所以本轮使用精确时间窗，不用这 80 ms。
9. 时间一致性可以做。Oracle 只吃当前事件及之前的状态。打断点查询发生在第 206 帧应用之后、后续帧发出之前。Indexer 当时已应用 247/247 个事件。
10. KVSelect 有 64 字节帧。`K_UP` / `K_TOMB` 由现有 `Dispatcher.on_frame` 接收。正式实验里的 digest 是 lineage 字符串的 blake2b-8，这条 trace 里没有这个字符串。

## 稳定 Lineage ID

不能把会随前缀变长而改变的末端哈希当成稳定 ID。C1 的广告键就是末端哈希 `38a2855819ec6b90`；链从 1040 token 缩短以后，这个键不再表示还活着的祖先。

本轮转换器使用从父指针推导出的分支根：一条新链的 ID 是它第一个不属于已有末端的块哈希。链继续变长或从末尾缩短时，这个 ID 不变。回归链的 ID 是 `e108dae91d118dad`（十进制 16215451153097395629），小于 2^64，放得进现有 digest 字段。

在这条 trace 上，该 ID 与请求哈希是一一对应的：101 个 lineage 各对应 1 个 `prompt_blake2s`，101 个 prompt 各对应 1 个 lineage，没有一对多。对应关系的标记是 `DERIVED`，不是帧里的原字段，也不是正式 KVSelect digest。

块树只有一个根。有一个父块下面挂了 101 个子块，也就是各请求共享开头的 16 token，然后分开。分支根取的是分开之后的第一个块，不是这个共享块。

## 这条 trace 里没有的东西

- 第二个 owner。跨 owner 共享前缀在 trace 中是 `NOT_OBSERVED`。
- 把一条已广告前缀的块全部删掉。41 次删除都落在两条链的末尾。
- 广告发出之后的 `AllBlocksCleared`。唯一一次清空是第一条事件，当时还没有广告。
- 正式路由器用的 lineage 字符串。

这些缺口用受控合成检查补了行为，结果标成 `SYNTHETIC`，不写入 trace 结论。

## Go / No-Go

`PARTIALLY_SUFFICIENT`：继续做了 Phase 1 和最小 Phase 2。Phase 3、Phase 4 按本轮范围保持 `NOT_RUN`。没有启动 GPU 重采集。
