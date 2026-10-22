# P0-M3.5 Top-K Rollback Compatibility

固定 K=16。策略是正式 `StaticTopK`。三种场景都走真实 Gateway 的 enqueue、Top-K、merge 和 release loop。

## 场景结果

| 场景 | 设置 | Dispatcher 上的回退覆盖 | Top-K 丢弃 | 标记 |
|---|---|---:|---:|---|
| A，待发 lineage 少于 K | 3 条已发出，再把其中一条从 1056 收到 448 | 448 | 0 | `SYNTHETIC_REGRESSION` |
| B，待发正好 K | 一次写入 16 条，其中一条先 1056 再在同一批里写成 448 | 448 | 0 | `SYNTHETIC_REGRESSION` |
| 真实 trace | 101 条 lineage，但排队峰值只有 3 | 448，随后 416 | 0 | `TRACE_REPLAY` |
| C，待发超过 K | 1056 已经发出；再突发 16 条更高覆盖，并附上 448 修正 | 仍是 1056 | 1 | `SYNTHETIC_TOPK_STRESS` |

Scenario B 的 Gateway 收到 17 条，替换掉 1 条尚未发送的 1056，发出 16 条。发出去的那条是 448，不是 1056。待发集合里的回退会被后写覆盖，不会被“覆盖数字更大”留住。

Scenario C 的 448 没有进入待发集合。Dispatcher 继续保留已经发出的 1056。这次突发不是 trace 的原始时间间隔，不能写成生产压力。它说明的是机制：Top-K 按覆盖高低给新的待发 digest 排名，不把“这是一次已经发布状态的向下修正”单独放行。

## 四种情况不要混在一起

| 现象 | 在哪看到 | 性质 |
|---|---|---|
| 容量选择造成的可见性损失 | Scenario C：16 条高覆盖占满名额，448 进不去 | Top-K 的正常筛选。修正若被当成一条新的低覆盖广告，就会掉在这个规则下面 |
| 旧版本回写 | 版本回归：448 之后迟到的 1040 把索引写回 1040 | 帧里没有版本。与 Top-K 无关 |
| 回退没有被编码 | 本条 trace 没有发生。Adapter 写出的是更短的 UPSERT，不是 Tombstone | 编码本身可以把 448 送进 Pipeline |
| 失效没有优先服务 | 未在长队列上测量延迟。`priority=1` 时 `K_TOMB` 进入优先队列，这是代码路径 | 缩短覆盖走的是普通 UPSERT，没有失效优先级 |

Owner-side validation 在正式请求路径里发生在选中 worker 之后，用真实缓存核对广告覆盖。本轮没有启动 vLLM，所以没有测量路由尝试次数。若索引仍广告 1056 而 worker 只有 448，`choose` 会按 1056 排序；那是索引错误会带进选择，尝试次数本身是 `NOT_MEASURED`。

## 机制缺口

Coverage 缩短目前是普通 UPSERT。待发名额未满时它能传播。名额已满、而且旧的高覆盖已经发出时，这次修正可以被 Top-K 丢掉，Dispatcher 不会再被纠正。

最小的语义区别是：对这个 Gateway 已经发布过的 `(owner, digest)`，新的状态是替换，不是一条来竞争 K 个名额的新 lineage。那会改变准入，不属于本轮允许的改动。本轮没有增加新的优先级规则，也没有改 K。
