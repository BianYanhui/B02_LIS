# P0-M3.5 Motivation Integration Assessment

本阶段只解释前面的测量，没有做四路径或三路径性能矩阵，没有外推 reporter 数量。

总判定不是单一的 Case A。可以同时写下：

- 实验性 Lineage：`EXPERIMENTAL_LINEAGE_MAPPING_ONLY`。正式字符串仍不等价，对应 Case D 的论文表述限制。
- 有序、未塞满的 Pipeline：这条 trace 上闭环成立。
- 版本：`MINIMAL_PROTOCOL_EXTENSION_REQUIRED`（Case B）。设计见 `MINIMAL_VERSION_GUARD_DESIGN.md`，未改帧。
- 满额 Top-K：`CORE_ADMISSION_SEMANTICS_CONFLICT`（Case C），只在 `SYNTHETIC_TOPK_STRESS` 里出现。真实 trace 的 Top-K 丢弃是 0。
- `DIRECT_INTEGRATION_FEASIBLE` 不成立。
- `MAJOR_REDESIGN_REQUIRED` 不成立。Algorithm 1 的覆盖排序不用改写才能解释这条 trace。满额时的修正丢失是一条准入例外，不是整套机制换掉。

## A. Block-to-Prefix 转换

| 问题 | 判断 |
|---|---|
| 能否按真实事件维护覆盖 | `SUPPORTED`。243 条更新与 Oracle 失配 0 |
| 能否留下有效祖先 | `SUPPORTED`。448 和随后的 416 都在，Tombstone 路径才会变成 0 |
| 是否仍是实验性 Lineage | `SUPPORTED` 的是分支根映射。与正式 lineage 字符串等价：`NOT_SUPPORTED` |
| 是否需要新的正式协议字段才能表达缩短 | 现有 UPSERT 就能写下 448。拒绝迟到旧帧才需要版本字段 |

## B. 当前 KVSelect 的直接兼容

| 问题 | 判断 |
|---|---|
| Semantic Reduction 能否处理回退 | `SUPPORTED`。有序 trace 和 Scenario A/B 里，后写的更短覆盖替换了尚未发出的高覆盖 |
| Coverage dominance 是否把旧高覆盖留住 | 这次两个 owner 的测试里没有。A 最终是 448，B 是 1024。`replica_cap` 丢弃为 0 |
| Fixed Top-K16 是否会压住修正 | 本条 trace 上没有，丢弃为 0，排队峰值 3。名额被 16 条更高覆盖占满时会：448 被丢，1056 留下。后者是 `SYNTHETIC_TOPK_STRESS` |
| Owner 级失效 | `CONDITIONALLY_SUPPORTED`。逐条 Tombstone 清掉一个 owner，另一个 owner 的同一 digest 仍是 1056。旧 UPSERT 仍能把已清除的键复活。没有用 cell 级 `K_RESET` |

## C. 动机里的五句话

| 主张 | 判断 | 依据 |
|---|---|---|
| Native KV-Block 元数据的通信成本较高 | `CONDITIONALLY_SUPPORTED` | 284529 字节 / 59.885 秒，平均 38.01 kbit/s。体积是实测的。拥塞不是 |
| Prefix 表示的潜在成本更低 | `SUPPORTED` 作为表示成本 | Adapter 243×64 = 15552 字节。这是 Adapter 的压缩，不是 KVSelect 的 |
| Gateway 放在汇聚点上说得通 | `CONDITIONALLY_SUPPORTED` | 一个 Gateway 收下了 243 条并转出 205 条。部署位置的网络节省没有单独测量，Worker 到 Gateway 的 Native 流量在这条回放里是本机帧，不是受限链路 |
| 当前 KVSelect 可以直接处理 Adapter 更新 | `CONDITIONALLY_SUPPORTED` | 有序且待发远小于 K 时，最终覆盖正确。迟到旧帧和满额 Top-K 下不正确 |
| 可见性和安全性的代价可接受 | `CONDITIONALLY_SUPPORTED` | 有序排空后可见性损失为 0。安全性代价是没有版本；满额时修正可能丢失。论文若只引用这条 trace 的有序结果，必须把这两个缺口写在旁边 |

KVSelect 在这条 trace 上的增量是 38 条被 merge 掉的更新，2432 字节。Top-K 的增量是 0 条。不要把 284529 到 13120 字节写成 KVSelect 的收益。13120 是 205 条已转发帧的 64 字节，`CODE_DERIVED`。

## 截稿前能落地的范围

可以写进动机的是：真实块事件的字节、末端 Tombstone 会丢掉仍可复用的 448 token、以及现有 StaticTopK Pipeline 在原始时间顺序下把缩短覆盖送到了 Dispatcher。

不能写的是：正式 Lineage 已经接上，协议已经防重放，以及 Top-K 在名额满时仍会送出向下修正。后两句需要协议或准入改动。那些改动会碰到论文里的固定 Top-K 语义，不适合在不改核心机制的前提下当成已经完成的集成。
