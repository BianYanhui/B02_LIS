# 下一轮

本轮停在容量测量。标记 `NO_SATURATION_WITHIN_TESTED_RANGE`，稳定下界 4000 updates/s。

不要把 Dispatcher indexing bottleneck 当作 KVSelect 的既定动机继续做 FullSync / StaticTopK 拥塞对比。不要为这个动机启动 1024 或 4096 Reporter 回放。不要把 4000 updates/s 写成最大吞吐。不要恢复 Adaptive，不要改 K，不要改帧宽，不要改论文。

A4 4096 汇报者的 7897/s 仍高于已测范围。若要回答那一档，需要单独决定是否把入口加到该速率以上，并且先确认发送端不是限制。本轮不自动加档。S2 在 140000 键处有百毫秒级事件循环停顿，但没有压低 30 秒 Apply 速率。不要把这段尾部写成索引饱和。若要解释停顿，那是另一次短的分配器剖析，不是 Reporter 回放。

等待下一次 OODA。
