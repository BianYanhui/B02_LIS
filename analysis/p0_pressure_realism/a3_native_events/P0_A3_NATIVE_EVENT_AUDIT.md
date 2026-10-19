# P0-A3 Native KV Events and Dispatch-Relevant Metadata

测量日期：2026-10-17。仓库起点 `4daceb0`。vLLM 0.10.2，包内 commit `26b999c71`，`VLLM_USE_V1=1`。

只开了 GPU 0 上一张 T4。启动参数与正式 `restart_4t4.sh` 相同：显存 0.40，max model len 6144，block 16，prefix caching，eager。日志里的 GPU KV cache 是 104,544 token，6,534 个 block。观测用官方接口 `--kv-events-config '{"enable_kv_cache_events": true, "publisher": "zmq", "endpoint": "tcp://*:5557"}'`。没有改 cache 算法，没有改 Gateway，没有加噪声，没有设 ρ，没有开四卡。测完进程已停，四张卡显存回到 0。

主动测量：Scenario A 10.5 s（20 条），B 3.3 s（1 冷 + 8 热），C 的灌入 70.6 s（115 条）加 3 条植入和 3 条回放。另有一次关掉事件发布的热路径对照，大约 17 s。合计远低于 10 分钟。

判定是 **Case A**：原生 block 事件很多，真正改变前缀覆盖的更新很少，和现在的 wrapper 同量级。下一问的推荐是 **D**，收窄普遍拥塞的 motivation。

## 1. 这版 vLLM 能直接看到什么

| 信号 | 类别 | 结果 |
|---|---|---|
| `BlockStored` | DIRECT_NATIVE_EVENT | 有。一个满 block 首次进入 prefix cache 时发出，一条事件里可以有多个 hash |
| `BlockRemoved` | DIRECT_NATIVE_EVENT | 有。空闲块被重新分配、原来的缓存块被挤掉时发出。请求结束只是把块放回空闲链表，那时没有事件 |
| `AllBlocksCleared` | DIRECT_NATIVE_EVENT | 有。本轮只出现在我们调用 `/reset_prefix_cache` 之后 |
| 分配一个尚未写满的块 | 没有事件 | `get_new_blocks` 不发 |
| prefix hit / block 复用 | 没有事件 | `touch()` 只加引用计数 |
| `prefix_cache_queries/hits` | ENGINE_INTERNAL_COUNTER | 按 token 计数，不是消息 |
| `cached_tokens` | ENGINE_INTERNAL_COUNTER | 响应里的命中 token 数 |
| `kv_cache_usage_perc` | ENGINE_INTERNAL_COUNTER | 全程 0.0，包括缓存打满和出现 `BlockRemoved` 之后。不能当驻留证据 |
| prompt token / 16 | DERIVED_FROM_TOKENS | 之前的 353 block/s 属于这一类 |
| `publish_state()` 的 UPSERT | WRAPPER_GENERATED | 本轮没有跑 harness，Model C 是离线按“一次完成一条”计数 |

事件里的 `token_ids` 没有落盘，只留了 blake2s。`block_id`（池内槽位）事件里没有，trace 里这个字段为空。前缀身份用 block hash，这是缓存键本身。

`BlockStored` 的开关只决定要不要把已经发生的插入放进队列。Qwen2.5-1.5B 不是 hybrid cache，开启事件没有改 block 大小或替换规则。热路径延迟见第 6 节，没有看到吞吐下降。

## 2. 三个场景

提示是聊天请求，目标正文 1024 token。API 报的 prompt 长度是 1053，其中 1040 个 token 对齐到 65 个满 block，剩下 13 个加上 4 个生成 token 会再凑出一个满 block。所以一次冷的唯一前缀是 65 个 prompt block 再加 1 个生成尾巴。第一条请求会把大约 16 token 的共享聊天前缀也算进去；之后的请求 `cached_tokens=16`，新的正文 block 是 64 个，再加那个尾巴。

**A，冷的互异前缀，20 条，10.47 s。** `BlockStored` 40 条事件、1301 个 hash。20 × 65 个满 block 加 20 个生成尾巴等于 1301，和 hash 数一致。`prompt_tokens/16 = 1316`，多出来的是没有写满、不会被缓存的尾巴，再加上已经被命中的那 16 token。查询计数器在这段的增量是 21217，等于 warmup 的 157 加上这 20 条的 21060。命中计数器 +304，等于后 19 条各命中 16 token。

这条链上的速率：1301 hash / 10.47 s = **124 block/s**。前缀覆盖变化是 20 次，**1.91 /s**。wrapper 规则同样是 20 条，**1.91 /s**。

**B，同一前缀，1 冷 + 8 热，3.2 s。** 冷请求 `cached_tokens` 为空，延迟 0.429 s，写入 65+1 个 hash。8 次热请求 `cached_tokens` 都是 1040，稳态延迟 0.133–0.141 s。每次热请求仍然收到一条 `BlockStored`，但 hash 都是 `130cc5ffbba98b16`，和冷请求那个尾巴相同。Owner 表里这个 hash 已经在，覆盖没有增加。前缀级广告只需要冷的那 1 条。wrapper 规则会发 9 条。热命中不需要新的 UPSERT。

**C，正常灌满后的驱逐。** 先植入 3 个前缀，再在 69.0 s 里发了 115 个互异前缀。第 97 个灌入请求时出现第一条 `BlockRemoved`，此前已经写入 6565 个 hash，容量是 6534。之后每来一条新前缀，就去掉大约 65 个旧 hash 并写入 64+1 个新 hash。灌入阶段共 7475 个新 hash（115 × 65）和 1138 个 `BlockRemoved`。

植入的 196 个 hash 里，195 个随后出现在 `BlockRemoved` 里。留下的那一个是仍被命中的共享 16 token 前缀。三条回放的 `cached_tokens` 都是 16，延迟 0.41–0.44 s，和冷请求一样，并且各自重新 `BlockStored` 了 64+1 个 hash。这是 **EVICTION_OBSERVED**：hash 被移除，并且回放重新计算了正文。不是只靠 Hit→Miss 推断。`kv_cache_usage_perc` 在这整段仍是 0。

## 3. 三种广告粒度

ρ 的分母是 10 Mbit tc 下测到的 9408 frame/s。104 字节是记账，不是抓包。200 frame/s 不拿来当饱和容量。

| 场景 | 口径 | frame/s | 104 B 记账 | ρ |
|---|---|---:|---:|---:|
| A 冷互异 | A block：新 hash | 124.2 | 104 kbit/s | 0.013 |
| A | B 前缀覆盖：每次新增 ≥16 个 hash | 1.91 | 1.6 kbit/s | 0.00020 |
| A | C wrapper：一次完成一条 | 1.91 | 1.6 kbit/s | 0.00020 |
| B 重复前缀 | A 按 hash 计，含重复发布 | 23.1 | 19 kbit/s | 0.0025 |
| B | B 新 hash | 0.62 | 0.5 kbit/s | 0.00007 |
| B | B 前缀覆盖 | 0.31 | 0.3 kbit/s | 0.00003 |
| B | C wrapper | 2.81 | 2.3 kbit/s | 0.00030 |
| C 灌满 | A 新 hash + `BlockRemoved` | 124.9 | 104 kbit/s | 0.013 |
| C | B 新 hash 事件，含逐块 EVICT | 19.8 | 17 kbit/s | 0.0021 |
| C | B 只计前缀覆盖 | 1.67 | 1.4 kbit/s | 0.00018 |
| C | B 前缀覆盖 + 逐块 EVICT | 18.2 | 15 kbit/s | 0.0019 |
| C | C wrapper | 1.67 | 1.4 kbit/s | 0.00018 |

回放只有 1.37 s、3 条请求，那里的 frame/s 不稳定，不拿来当速率。

冷的互异流量上，wrapper 和前缀覆盖是同一个数。重复前缀上，wrapper 更高，因为命中了也发。block 级大约是前缀级的 65 倍，这是一条前缀里面的块数，不是 Dispatcher 必须多知道 65 条前缀状态。缓存打满之后，逐块 EVICT 把链级消息抬到大约 20 /s，仍然是 9408 的 0.2%。

## 4. 突发

10 ms 窗口里大部分是空的，所以 p95 是 0。看 max 和 p99。

A 的 block hash：10 ms 最大 65，容量是 94，没有超过。1 s 窗口只有 11 个，统计不稳定，最大 194。

C 灌满后的 block hash：10 ms 最大 129，超过 94。这是同一步里大约 65 个删除加 64 个写入落在同一个时间戳上。129 / 9408 ≈ 14 ms 就能排空。1 s 最大 260，远低于 9408。前缀级和 wrapper 的 10 ms 最大分别是 66 和 1，都不超过 94。

超过一个 10 ms 切片的到达速率，不等于 Dispatcher 已经变陈旧。这里没有形成持续队列。

## 5. 353 block/s 是什么

它是四卡 unique prefill 的 `prefix_cache_queries` token 数 5647 除以 16，写在 `a3_motivation_explore/MOTIVATION_POSSIBILITIES.md`。那是查询 token 的换算，不是 `BlockStored` 的条数。本轮单卡冷互异的直接观测是 124 个新 hash/s，前缀级是 1.91 条/s。数量级和“全 miss 时每个满 block 一条”一致，和“每条查询 token 一条消息”不是一回事。四卡同时跑没有测，不能把 124 或之前的 220 乘 4 当成结果。

## 6. 观测开销

同一条热前缀，事件开着的稳态延迟中位 0.139 s，关掉发布器是 0.140 s（各取后几次，`cached_tokens=1040`）。没有看到发布器把热路径变慢。关掉发布器后的第一条冷请求是 9.0 s，那是进程起来后的第一次长 prefill，含引擎预热，不能和事件开着时已经预热过的 0.39 s 比。

## 7. 对四个问题的收束

原生事件多，是 block 粒度。Dispatcher 要维护的前缀覆盖，在冷流量上约等于每条新前缀一次，和 wrapper 相同；热流量上 wrapper 还多发。打满缓存后的真实驱逐存在，逐块上报大约 16 条 `BlockRemoved`/s，加上新前缀仍不到 20 条/s。对 9408 frame/s，ρ 最大的稳定口径是 block 级 0.013。四卡即使按单卡 block 速率线性外推，大约 500 frame/s，ρ 约 0.05，而且这是外推，不是测量。

所以本轮不支持“四卡自然 metadata 会压满 10 Mbit”。它支持的是：block 级上报是一种更细的协议，在这张卡上大约 100 kbit/s；论文现在的前缀级广告并没有漏掉同量级的覆盖变化。
