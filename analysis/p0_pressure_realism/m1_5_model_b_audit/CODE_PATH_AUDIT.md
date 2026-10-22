# P0-M1.5-A Phase 0：代码路径

审计对象是工作区 `HEAD` `f3adf24`（2026-10-20，Coverage Dominance）。没有启动 vLLM，没有跑多 Gateway。历史 formal4t4 的测量不能直接当成这份代码的行为。

当前论文算法对应 ICC 策略名 `StaticTopK`：`experiments/icc_kv/wire.py` 第 23 行，配置元组 `(mode=4, merge=1, priority=1, adaptive=0, dedup=2, global_topk=16)`。`adaptive=0` 时，缩 K 和 utility 丢弃不会启动。文件里仍留着 Adaptive 的函数，它们不属于当前论文。

## 实现落点

| 机制 | 位置 | 状态 | 是否当前论文算法 |
|---|---|---|---|
| 一个 Gateway 进程，多条 Worker TCP | `gateway_relay_4t4.py` `agent_reader` 216–263，`amain` 807–809 | `CODE_CONFIRMED` | 是，但这是一个进程里的多连接，不是多个 Gateway |
| 一条到 Dispatcher 的 TCP | 同文件 `downstream_manager` 674–691 | `CODE_CONFIRMED` | 是 |
| Supersession | `enqueue` 472–473，`_supersede_queued_up` 575–588。键是 `(instance, digest)` | `CODE_CONFIRMED` | 是 |
| Invalidation 优先 | `enqueue` 481–482，`_dequeue` 617–618，`pqueue` | `CODE_CONFIRMED`。`priority=1` 时生效 | 是 |
| Coverage-dominant replica cap | `_admit_replica` 376–414，`dedup=2` | `CODE_CONFIRMED`。状态在本进程的 `replicas` | 是。只在本 Gateway 内 |
| Fixed Top-K | `_topk_drop_new` 528–541，`trim_global_topk` 543–557，`topk_best` | `CODE_CONFIRMED`。只统计尚未写出的 upsert | 是。K 来自配置，StaticTopK 为 16 |
| K 在拥塞时变成 K/4 | `_topk_limit` 514–519，条件是 `congested()` | `CODE_CONFIRMED` 代码还在 | 否。`congested()` 273–281 在 `adaptive` 为假时直接返回假 |
| Utility / EWMA 准入 | `low_utility` 346–351，`ack_reader` 700–702 更新 `ewma_dq` | `CODE_CONFIRMED` 代码还在 | 否。StaticTopK 不调用到有效分支 |
| Owner-side validation | 论文 Algorithm 之后的命题。formal Dispatcher 的 `choose` 只读索引，不在 Worker 上做版本校验 | 本轮看到的调度路径没有这条校验 | 论文有，这条控制路径里没有实现 |
| Dispatcher apply | `experiments/icc_kv/runtime.py` `on_frame` 191–210，`_on_down` 366–388 | `CODE_CONFIRMED`。同步改字典后写 ACK | 索引是 `(worker, digest)` |
| formal4t4 Dispatcher | `experiments/4t4/run_formal4t4.py` `apply_upsert` 276–284，索引是长度 4 的 list | `CODE_CONFIRMED` | 历史四卡 harness。`instance` 超出 0–3 会越界 |

ICC harness 最后一次改这些文件的提交是 `3991559`（2026-10-07）。Gateway 的副本替换是后来的 `f3adf24`。帧头 `instance u8` 来自 `8b2b6b21`（2026-07-25），之后没有加宽。

## 两套 harness 不能混用

- ICC：`MAX_INFLIGHT = 0`（`runtime.py` 35）。应用层 ACK 不限制发送。Gateway 启动参数里仍带 `--gate 2 --adaptive-queue-gate 8`（132–134 行），但 StaticTopK 的 `adaptive` 位是 0，这些参数不改变准入。
- formal4t4：`--relay-max-inflight` 默认 4（1175 行）。那里 ACK 会卡住发送窗口。那是旧实验的传输配置，不是当前 ICC 路径。

当前部署脚本只创建一只容器 `b02-gateway4t4`，监听 `9710`，下游 `9711`（`prepare_fixed_gateway`，125–131 行）。没有第二只 Gateway。
