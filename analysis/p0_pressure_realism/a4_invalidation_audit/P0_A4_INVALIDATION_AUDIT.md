# P0-A4 Invalidation Accounting

核查日期：2026-10-18。没有启动 vLLM，没有改 P0-A3 的原始文件。

数据是 `d978ad3` 里的 `analysis/p0_pressure_realism/a3_native_events/native_event_trace.csv`，请求窗口来自同目录 `raw/requests.csv`。统计脚本是本目录的 `rebuild_prefix_state.py`。vLLM 仍是 0.10.2，commit `26b999c71`，事件来自 GPU 0 的官方 ZMQ。

判定是 **Case A2**。P0-A3 报出的 1.91/s 和 1.67/s 是 UPSERT_ONLY。真实驱逐可以在这条 trace 上补回来，补完以后 flood 的前缀级速率从 1.667/s 变成 1.928/s。这不改变 Case A，也不改变 Option D。P0-A 可以结束。

## 1. P0-A3 的分子

`analyze_kv_events.py` 把 `BlockStored` 里至少 16 个新 hash 的批次记成 `dispatch_relevant=prompt_coverage`。1.91/s 是 A_cold 的 20 次除以 10.473 s。1.67/s 是 C_flood 的 115 次除以 68.984 s。两个分子都是这种覆盖增长。

`BlockRemoved` 在同一脚本里另记成 `kind=EVICT`、`dispatch_relevant=block_invalidation`，一条事件一个。C_flood 因此有一行 `B_prompt_plus_block_evict` = 115 + 1138 = 18.164/s。那一行把每个被删的块都当成了前缀失效，P0-A3 正文没有把它当成前缀级速率。`AllBlocksCleared` 被标成 `reset_between_scenarios`，没有进入 1.91 或 1.67。

同一 hash 再次出现时，P0-A3 只在它仍留在 `valid` 集合里才跳过。热请求重复的那个尾巴 hash 因此没有再记一条。被 `BlockRemoved` 摘掉之后再 `BlockStored`，会重新进入。这条规则没有把失效后的重新驻留永久去重。

所以原统计是 **UPSERT_ONLY**。它没有漏掉“块被删除”这件事的记录，但头条的前缀速率没有包含失效。

## 2. Trace 够不够重建

够用，边界写在下面。

每条 `BlockStored` 带有父 hash 和按顺序排列的新 hash。本轮 0 次出现“父 hash 当时不在缓存里”。广告对象取这条链的末端：一次至少 16 个新 hash 的写入，末端就是这次新前缀。覆盖按链长乘 16。A/B/C 里这个覆盖都是 1040 token，也就是 65 个块，和 API 的 `cached_tokens=1040` 一致。

`BlockRemoved` 不带父 hash，但 hash 能回到已经记下的链上。同一个时间戳上的删除是同一步，flood 里有 18 个这样的批次，大小是 65 或一次 33，合计 1138。先把整批从驻留集合拿掉，再看每条仍有效的广告链还剩多长。不能在批次中间就下结论：只删掉尖端时会短暂剩下 1024 token，同一步的其余删除会把这个数字再削掉。

由此能分开两件事。链从根上仍连续存活的部分，是剩余可复用覆盖。广告的那个末端 hash 只要不再整段存活，这条广告就不再为真。从未被广告的块，包括生成尾巴，删除时不产生调度消息。

`AllBlocksCleared` 四次，都紧跟采集脚本自己的 `/reset_prefix_cache`。这是实验复位，不是缓存自己的驱逐。

## 3. 转移规则

只给“至少 16 个新 hash”的末端建广告。生成尾巴不建。

| 观察到的变化 | 记成 | 协议里已有的帧 |
|---|---|---|
| 新的末端 | UPSERT | `K_UP` |
| 末端曾经失效，后来整段再次写入 | RE_ADVERTISE，算进 upsert，不另加一次 | `K_UP` |
| 一个删除批次把仍有效的末端链打断 | 一条 INVALIDATE | `K_TOMB` |
| 同一条广告在已经失效之后，剩下的块继续被删 | 不再发消息 | 已发过的 tombstone 仍然有效 |
| 热请求重复同一个仍存活的尾巴 hash | 不发 | 状态没变 |
| `AllBlocksCleared` | RESTART，不计入自然速率 | `K_RESET` |

身份是 block-hash 末端，不是 formal harness 的 lineage digest。trace 里没有 lineage digest，所以不能把“同一逻辑前缀、覆盖从 1040 改成更小”说成已经观察到的操作。末端不再整段存在时，对应的是这条末端的 `K_TOMB`。批次结束时祖先链上还留着 528 token（33 个块），那是另一个更短的 hash，本来就没有单独广告过，不再为它补一条 upsert。现有帧类型够用，没有另造操作码。

## 4. 重算的速率

时间窗与 P0-A3 相同，是该场景第一条请求开始到最后一条结束。速率不相加。

| 场景 | 窗长 | BlockStored hash/s | BlockRemoved/s | 前缀 upsert/s | 失效/s | 合计/s | 合计 ρ |
|---|---:|---:|---:|---:|---:|---:|---:|
| A 冷互异 | 10.473 s | 124.22 | 0 | 1.910（20） | 0 | 1.910 | 0.000203 |
| B 重复 | 3.203 s | 23.10 | 0 | 0.312（1） | 0 | 0.312 | 0.000033 |
| C 植入 | 1.267 s | 154.7 | 0 | 2.368（3） | 0 | 2.368 | 0.000252 |
| C 灌满 | 68.984 s | 108.36 | 16.50 | 1.667（115） | 0.261（18） | 1.928 | 0.000205 |
| C 回放 | 1.371 s | 142.2 | 142.3 | 2.189（3，全是重新广告） | 2.189（3） | 4.378 | 0.000465 |

回放只有 1.37 s，速率不稳定，只看条数：3 条失效、3 条重新广告。ρ 的分母是 9408 frame/s。64 字节和 104 字节两列在 `metadata_rate_recalculated.csv`。104 字节仍是记账。

灌满阶段的 18 条失效，全部来自这次 flood 里新广告的前缀，不是那 3 条植入。3 条植入在回放窗口里失效，并在随后的 `BlockStored` 里重新广告。198 个块有“删除之后再次写入”的痕迹，其中回放的 195 个 hash 就是这 3 条前缀的正文；调度消息只记 3 条，不按块再记。

## 5. BlockRemoved 里有多少真的拆掉了广告

1138 条 `BlockRemoved` 不是 1138 条失效。它们落在 18 个时间戳上，每个时间戳拆掉一条当时仍有效的 1040-token 广告。一条广告一条 `K_TOMB`。

批次结束时，这 18 条链都还剩 528 token 的连续祖先，所以不是“缓存清空”。对已经广告的 1040 token 来说，声明已经不成立。之后同链上剩余块的删除不再追加消息。回放时 API 的 `cached_tokens` 只剩 16，说明这些前缀最后连正文都没留下；那是后续删除的结果，不是第二条 tombstone。

## 6. 突发

把 upsert 和失效放在一起，灌满阶段 10 ms 窗口最大是 2，容量是 94。1 s 窗口最大是 4，容量是 9408。没有哪个窗口超过排空能力。离线积压是 0。这不是观察到的 Dispatcher staleness。失效没有带来新的突发。

## 7. Wrapper 漏了什么

正式 `publish_state()` 每次完成发一条 `K_UP`。`K_TOMB` 只在 ShadowCache 按 104544 token 的 LRU 挤掉一个已经广告过的 digest 时才发。正式基线的 `source_tombstones_sent` 是 0，因为那个工作集没有填满这块缓存。那次基线没有漏掉驱逐，是因为驱逐没有发生。

这条 flood trace 没有跑过 harness。按“一次完成一条、不看 `BlockRemoved`”来数，灌满阶段会少 18 条 tombstone，大约 0.26 条/s，占该窗口调度消息的 18/133。ShadowCache 如果跑在同一工作集上，有可能发出自己的 tombstone，但它的受害者是模型推的，不能当成这 18 条 `BlockRemoved` 批次。当前正式实验并没有发出这 18 条。

热命中仍然会让 wrapper 多发。B 里调度上只需要 1 条 upsert，wrapper 规则是 9 条。失效记账不改变这一点。

## 8. 判定

Case A2。1.91/s 和 1.67/s 不含失效。trace 能把失效补上。补上之后 flood 的合计是 1.928/s，ρ 从 0.000177 到 0.000205。Case A 和 Option D 不变：前缀级自然更新，加上真实失效，仍然远低于 9408 frame/s。

P0-A 的事件真实性核查到此结束。不需要再跑 GPU 来回答“失效有没有被算进去”。
