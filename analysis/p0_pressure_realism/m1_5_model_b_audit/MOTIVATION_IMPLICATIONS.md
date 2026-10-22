# Model B 对动机意味着什么

速率全部来自 P0-M1 的 compact 外推，每 Gateway 64 个汇报者。不是新测量。不用 native hash 速率。不把 10 Mbit 当成生产配额。

| 汇报者 | Gateway 数 | 合计 updates/s | 每只 Gateway |
|---:|---:|---:|---:|
| 256 | 4 | 133 | 约 33 |
| 1024 | 16 | 530 | 约 33 |
| 4096 | 64 | 2121 | 约 33 |

1. 以这个外推看，单只 Gateway 出口上没有已经识别的拥塞。共享点在 Dispatcher 的一个循环上。2121 次/s 的字典更新有没有超过 `C_D`，是 `NOT_MEASURED`。不能选“出口”或“Dispatcher”作为已经发生的瓶颈。

2. 出口不限速时，Top-K 的删除次数预期接近 0。帧一进 `release_loop` 就离开 `topk_best`。33 条/s、每条很快写走，待发 lineage 数到不了 16。这是从代码推出的，不是计数。

3. Apply 若真的慢过合计转发，反压要等内核接收缓冲满了才到 Gateway。当前 `max_inflight=0`，ACK 不提前收紧窗口。缓冲里的帧已经筛选不到。

4. 让现有 `max_inflight` 大于 0，会让 ACK 窗口在缓冲填满前停住发送，未发送队列更早达到 K。这是已有传输开关，StaticTopK 和 FullSync 共用，不改变准入规则，也不恢复 Adaptive。加宽 `instance` 同样不改变 Algorithm 1。两者都不是新的论文机制。

5. Coverage-bounded admission 比 FullSync 多做的事，只发生在某一只 Gateway 的未发送 lineage 超过 16 的时候。正式 serving 的外推速率本身不制造这个条件。单 Gateway 加 10 Mbit 整形时，历史实验确实看到大量 Top-K 丢弃；那是配置出来的出口限制。没有新的证据表明 Model B 的共享 Dispatcher 会制造同样的未发送积压。
