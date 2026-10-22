# P0-M3 Model B Scale Projection

状态：`NOT_RUN`。

Phase 4 需要 Phase 3 的四路径对照，而且本轮范围明确不跑规模外推。下面的部署没有被计算，也没有被说成 4096 GPU 的测量结果。

| Reporters | Gateways | Native ingress | Compact egress | KVSelect egress | Dispatcher input |
|---:|---:|---|---|---|---|
| 256 | 4 | `NOT_RUN` | `NOT_RUN` | `NOT_RUN` | `NOT_RUN` |
| 512 | 8 | `NOT_RUN` | `NOT_RUN` | `NOT_RUN` | `NOT_RUN` |
| 1024 | 16 | `NOT_RUN` | `NOT_RUN` | `NOT_RUN` | `NOT_RUN` |
| 4096 | 64 | `NOT_RUN` | `NOT_RUN` | `NOT_RUN` | `NOT_RUN` |

Deployment A（Gateway 与 Dispatcher 同机）和 Deployment B（Gateway 在受限链路上游）都没有估算。10 Mbps、100 Mbps、1 Gbps 都不是本轮测到的生产配额。

单 worker 的原生 msgpack 平均 38.01 kbit/s 记在 `native_traffic_profile.csv`，标记 `DERIVED`。那个数字不能线性乘成共享链路拥塞。
