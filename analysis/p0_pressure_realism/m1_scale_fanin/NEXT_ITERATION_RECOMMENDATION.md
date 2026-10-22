# 下一轮之前

Phase 3、4、5 没有开始。`fanin_validation.csv` 和 `visibility_lag.csv` 只有一行 `NOT_RUN`。不要把 P0-C1 排空后的队列 0 填进这两张表。

## 值不值得做真实 fan-in benchmark

Compact prefix，不值得为了寻找拥塞去跑。正式 serving 速率外推到 4096 个一卡实例，平均负载只有 10 Mbit 配置排空能力的 0.23。灌满微基准的平均值在同一点是 0.84，均匀相位的短窗仍低于 9408 frame/s。会越过这条线的只有标明的同步突发，而且要用灌满速率，不是论文里的 serving 混合。再跑一轮 compact 回放，预期结果是排空，不能加强动机。

Native indexer，只有一个问题还值得测：独立键的处理吞吐。256 个一卡实例的外推已经是 1077 events/s，等于已跑通速率的 4.2 倍，而那次 256 events/s 是同一套 hash、同一条连接。字节压力对 10 Mbit 配置在大约 256 个一卡实例处接近 1，但这条字节流不是 KVSelect 的输入。测 indexer 之前先要决定：问题是原生索引的容量，还是当前前缀协调的容量。后者的速率对照已经足够做判断。

按 Worker 切成 64 汇报者一片之后，C1 全段字节对 10 Mbit 的 ρ 降到约 0.24，事件速率约 269/s，和已经跑通的复制回放同一量级。这是算术，不是一次分片实验。

## 还缺的可信数据

1. A1 每个引擎的 upsert 计数，以及 warmup、overlap seed 的时间。现在的单引擎速率是集群除以 4。
2. TP>1 或更大模型的单引擎事件速率。现在这些格子是 `NOT_MEASURED`。
3. 第二个真实 Worker 的 hash 链。没有它，就不能生成独立键的大规模 Replay。
4. Release indexer 在互异键上的饱和吞吐、队列和 CPU。Debug 构建的 256 events/s 不能当容量。
5. 生产控制面带宽配额。10 Mbit 只是实验里的 HTB。
6. 真实部署的 gateway 分片宽度，以及一条事件会不会复制到多个 indexer。本轮扇出取 1，分片宽度取 64，都是分析选择。

## 停止条件

没有收到下一轮指令之前，不启动 vLLM，不跑 indexer 压测，不改 KVSelect，不改 formal4t4，不出论文图。
