# 决策与下一步

## 四项判定

A. 架构：`MODEL_B_REQUIRES_PROTOCOL_FIX`

多条 TCP 可以被同一只 Dispatcher 接受。按图中的局部 Worker 编号，两个 Owner 会写成同一个 `(instance, digest)`。超过 255 个互异 Owner 时，一个字节不够。

B. 共享瓶颈：`NOT_MEASURED`

共享的是 Dispatcher 的事件循环和索引。不存在一条自动共享的 10 Mbit 路径。没有饱和数据。

C. Top-K 位置：`TOPK_EFFECT_DEPENDS_ON_FLOW_CONTROL`

Q3 的对应结论是 `CONDITIONAL_ON_TRANSPORT_BACKPRESSURE`。Top-K 只作用于未写出的队列。TCP 缓冲填满之后它才看得到下游变慢。

D. 下一步：Conditional Go

先有一份最小身份方案，再做 2–4 只 Gateway 的 CPU 试验。那次试验只检查身份和反压是否传到未发送队列。它不证明动机成立。

## 最小方案，本轮不写代码

- 2–4 只 Gateway、合计不超过 256 个 Owner：harness 发全局唯一的 `instance`。不改准入，不改 K。
- 要按局部编号复现图，或要测 1024 以上：加宽 `instance` 或增加 `gateway_id`，索引键改为 `(gateway, instance, digest)`。tombstone 用同一键。
- 反压试验若要让 Top-K 在内核缓冲填满前动作，把该次试验的 `max_inflight` 设成一个小的正数。这不开启 `adaptive`。试验报告里写明这是传输窗口，不是论文里的新准入。
- 不把 Dispatcher 人为限速的结果写成生产拥塞。

## 五项测试

| 测试 | 结果 |
|---|---|
| A 两只 Gateway 同时连接 | `SUPPORTED_BY_CODE`。`start_server` 接受多连接。没有现成的双进程 harness |
| B 局部 Worker ID | `REQUIRES_MINIMAL_CHANGE`。全局唯一且 ≤255 时可以不改帧 |
| C Apply 变慢传回 Gateway | `SUPPORTED_BY_CODE` 只到 TCP 缓冲满。缓冲深度 `NOT_MEASURED`。ACK 窗口在 ICC 里是关的 |
| D 语义归约互相独立 | `SUPPORTED_BY_CODE`。每只进程一份 `replicas` 和 `topk_best` |
| E 全局 affinity 正确 | `REQUIRES_MINIMAL_CHANGE`。键唯一时字典能合并；局部编号会互相覆盖，tombstone 会删错 Owner |

停在这里。不启动 GPU，不跑 fan-in，不改算法和论文。
