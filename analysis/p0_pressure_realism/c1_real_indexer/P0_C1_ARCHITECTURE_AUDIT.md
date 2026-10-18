# P0-C1 Real KV Event Dissemination

判定：**Case 1 的建议，外加一条修正。** 这条单卡 trace 的原生事件没有把网络或 Dynamo 索引打满。索引器在允许的最高回放档（64 倍）仍然跟得上，排空后积压是 0。Case 1 原文里的「网络负载较大」与测量不符：msgpack 负载大约 38 kbit/s。不以索引拥塞作为下一步动机。

没有改 `experiments/`，没有改论文，没有做 64 卡，也没有把 KVSelect 接到 Dynamo。

## 记录

| 项 | 值 |
|---|---|
| B02 分支 | `p0-c1-real-indexer` |
| vLLM | 0.10.2，安装在 `poc/.venv`，包内 commit `26b999c71` |
| Dynamo | `/home/byh/Dynamo/dynamo`，commit `7a2a40906`，crate 版本 1.5.0 |
| 索引器 | `python -m dynamo.indexer --threads 4 --block-size 16` |
| 构建 | 本机 debug 构建，打开 `kv-indexer` 和 `kv-indexer-metrics`。未做 release 优化 |
| GPU | 一张 Tesla T4，GPU 0。KV cache 104,544 token，block 16 |
| 工作负载 | 109 条顺序 chat，正文目标 1024 token，`max_tokens=4`。事件跨度 59.885 s |
| 脚本 | `analysis/p0_pressure_realism/c1_real_indexer/run_c1.py` |

B02 仓库里没有 Dynamo 集成。本机已有一份 Dynamo 检出。原 Python 扩展没有编进 indexer 入口，这次只对已有源码打开了 `kv-indexer` 特性，没有新装一套运行时。旧的 `_core.abi3.so` 留在同目录的 `_core.abi3.so.pre-c1`。

## 事件从哪来，到哪去

vLLM 0.10.2 在 `vllm/v1/core/block_pool.py` 里产生事件。`cache_full_blocks` 把一次新写入的多个 hash、父 hash 和这些块的 token id 放进一条 `BlockStored`。`_maybe_evict_cached_block` 每回收一个已缓存块发一条 `BlockRemoved`，里面是一个 hash。`reset_prefix_cache` 发 `AllBlocksCleared`。命中已有块的 `touch` 不发事件。

`vllm/v1/core/sched/scheduler.py` 每个调度步调用 `take_events()`。有事件才组一个 `KVEventBatch` 交给 `ZmqEventPublisher`。空步不发。发布器在 `vllm/distributed/kv_events.py`：msgspec msgpack，三条 ZMQ 帧，分别是 topic、8 字节大端序号、payload。序号从 0 递增。默认高水位 100000。代码里有 replay buffer，但这次 `replay_endpoint` 是空的，没有启用。没有压缩，没有确认，没有应用层去重。

Dynamo 的独立索引器在 `lib/kv-router/src/services/indexer/listener.rs` 订阅这个端点。`zmq_wire/deserialize.rs` 按 tagged array 解 vLLM 0.10.2 的字段，并忽略后面多出来的位置。`convert.rs` 保留线上的 64 位外部 hash，同时用块内 token 重新计算 `tokens_hash`。radix 树用 `tokens_hash` 做边，用外部 hash 做删除和父节点查找。`apply_event` 把事件放进无界 flume 队列，真正改树的是线程池里的 worker。默认 4 个线程，单个 worker 的事件落在其中一条 lane 上。

路由查询是 HTTP `POST /query`。服务端用同一套 `compute_block_hash_for_seq` 把 token 换成 `tokens_hash`，再走 radix。`/dump` 把当前树交回带外部 hash 的事件。

## 线上格式，这次实际看到的

P0-A3 的 CSV 只有 hash，token id 被丢掉了，不能还原 msgpack 尺寸。这次在 GPU 0 上重新抓了原始帧，存在 `raw/frames.bin`。

212 条 ZMQ 消息，252 个事件对象，6615 个 hash。一条消息是一个调度步，里面可以有多个事件。一条 `BlockStored` 最多带 65 个 hash，并且带上这些块的 token id。41 条 `BlockRemoved` 都是一个 hash。1 条 `AllBlocksCleared`，来自采集开始时的 `/reset_prefix_cache`。

序号 0 到 211，缺口 0。订阅在请求之前接上，没有看到丢批或乱序。

| 场景 | 窗长 | ZMQ 消息 | 事件 | hash | msgpack 字节 | 状态 |
|---|---:|---:|---:|---:|---:|---|
| A 冷 | 8.313 s | 16 | 17 | 521 | 23,442 | OBSERVED |
| B 重复 | 0.835 s | 6 | 6 | 6 | 594 | OBSERVED |
| C 植入 | 0.715 s | 4 | 4 | 130 | 5,262 | OBSERVED |
| C 灌入 | 47.901 s | 182 | 221 | 5,954 | 254,957 | OBSERVED |
| C 回放 | 0.287 s | 4 | 4 | 4 | 274 | OBSERVED，窗太短 |
| 全部 | 59.885 s | 212 | 252 | 6,615 | 284,529 | OBSERVED |

全部窗口的 msgpack 是 4,751 B/s，也就是 **38.0 kbit/s**。ZMQ 三个帧的体是 286,225 字节，和 payload 几乎一样。loopback 上的 TCP 抓包没有权限，TCP 载荷是 **NOT_MEASURED**。若按每条 ZMQ 消息一个 TCP 段、每段 40 字节 IP+TCP 头来估，头只有 8,480 字节，这是 **ESTIMATED**，不含 ACK。

热重复的 6 条消息一共 594 字节。每次仍有一条 `BlockStored`，但只有已经在缓存里的那个尾巴 hash。

## 索引器处理了什么

索引器没有和 vLLM 同时挂着跑。vLLM 停掉之后，把抓到的原始帧按原顺序重新发布给一个新的 indexer。这是 **OBSERVED** 的同一份字节，不是合成事件。回放时序号改成单调递增，否则 Dynamo 会把回绕的序号当成过期消息丢掉。事件内容没有改。

一次按原始时间播完：

- 发出 252，应用 252。`stored ok=210`，`removed ok=41`，`cleared ok=1`。没有 `parent_block_not_found`，没有 `block_not_found`。
- `duplicate_store` 警告 8。那是热路径上重复出现的尾巴 hash，树没有因此加出新块。
- 播完后仍驻留的外部 hash 是 6,525。`/dump` 里的外部 hash 也是 6,525。缺 0，多 0。
- 这一趟索引器 CPU 时间 0.33 s，RSS 约 92 MB。
- 用第一条从根开始的 `BlockStored` 的 1040 个 token 调 `POST /query`，返回 worker 0、GPU 命中 1040 token。查询耗时 3.96 ms。

逐事件 apply 延迟没有指标，记为 **NOT_MEASURED**。能看到的是：应用计数跟上了发送计数，2 秒排空后的差是 0。

## 处理能力

下面四档都是把这一份单卡 trace 循环加速，每档大约 42 秒。不是 4 台、16 台或 64 台 GPU。索引器是 debug 构建，所以这组吞吐偏慢，不是 release 的容量上限。

| 档 | 输入事件/s | 已应用/s | hash/s | 排空后积压 | CPU 秒 | 查询 p95 |
|---|---:|---:|---:|---:|---:|---:|
| 1x | 3.21 | 3.21 | 99 | 0 | 0.55 | 3 ms |
| 4x | 15.2 | 15.2 | 415 | 0 | 1.13 | 3 ms |
| 16x | 63.3 | 63.3 | 1,678 | 0 | 3.2 | 3 ms |
| 64x | 256 | 256 | 6,725 | 0 | 10.87 | 3 ms |

64 倍时 10.87 个 CPU 秒摊在 42 秒墙上时间里，大约四分之一个核。状态全部是 `ok`。没有持续增长的积压，所以没有再往上加。结论是 **NO_BOTTLENECK_OBSERVED**。9408 frame/s 仍只是以前那条 10 Mbit tc 路径的排空数，不拿来当索引器容量。

1 倍那一档的 40 秒窗口停在驱逐发生之前，所以它的 `removed ok` 是 0。4 倍及以上的循环包含了驱逐。完整的 41 次删除是在按原始时间播完的那一趟里应用成功的。

## 两种表示

Model A 是这次索引器实际保住的块级驻留。Model B 是 B02 的前缀广告：一次至少 16 个新 hash 的末端算一条 UPSERT，一个删除批次把仍有效的末端打断算一条 tombstone。

| | 记录 | 字节 |
|---|---:|---:|
| Model A | 6,615 个 hash 出现 | 284,529，OBSERVED msgpack |
| Model B | 101 条 UPSERT + 1 条失效 | 10,608，按 104 字节记账，ESTIMATED |

记录数大约少 65 倍。字节大约少 27 倍，因为原生事件里大头是 token id，不是 104 字节的控制帧。

这次灌入刚越过缓存容量，只打断了 1 条已经广告的前缀。打断结束时祖先链还剩 448 token，广告覆盖是 1040。Model A 仍能寻址这 448 token。Model B 对这条末端发 tombstone 之后，可见覆盖是 0。这是 **LOST_PARTIAL_PREFIX_VISIBILITY**。不是无损压缩。

两条植入前缀在回放时 `cached_tokens` 仍是 1040，延迟 0.14 s。它们没有被赶走，所以这次没有观察到「整段删除后再写入」。完全清空一条已广告前缀也没有发生。开头那次 `AllBlocksCleared` 发生在任何广告之前，索引器把它应用成功了，但当时没有需要撤销的前缀。

## 三件事不要混在一起

1. 物理网卡容量这次没有测。测到的是本机 ZMQ payload。38 kbit/s 对 10 Mbit 的比值是 0.0038，这只是和那条 tc 上限比较。
2. 索引器应用能力测到了。debug 构建在 256 事件/s 时积压为 0。
3. 有效控制服务如果只传播前缀广告，这条 trace 是 102 条消息对 252 个原生事件。前缀级更小，这和 P0-A4 一致。它不提高索引器的测得容量，也不创造拥塞。

64 个这样的 worker 若同时以这次的单卡速率发事件，事件率大约 4.2×64 = 269/s，payload 大约 2.4 Mbit/s。这是 **EXTRAPOLATED**。269/s 落在已经测过的 64 倍单连接回放附近，那一档没有积压。64 条连接、64 份 worker 状态没有测，不能写成「64 张 GPU 自然造成拥塞」。
