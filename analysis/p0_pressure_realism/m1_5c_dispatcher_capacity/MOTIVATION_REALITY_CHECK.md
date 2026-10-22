# Motivation Reality Check

标记：`NO_SATURATION_WITHIN_TESTED_RANGE`（Case B）。

可信的稳定 Apply 下界是 **4000 updates/s**。S1 4000，S2 4000。依据是正式 30 秒窗、每个计入下界的档 3 次都是 `STABLE`，并且生成端达到了目标。这个数不是最大吞吐。高于该下界的档位没有三次稳定的正式结果。默认最高目标是 4000 updates/s，没有自动再往上加。

A1 正式负载、4096 个一卡汇报者的外推是 2121 updates/s（`EXTRAPOLATED`）。4000 高于 2121，负载比上界 0.530。1024 个汇报者的 530.2/s 比上界是 0.133。在这个外推模型下，Dispatcher 索引处理没有显示为主要瓶颈。不应再把大规模 Dispatcher CPU 拥塞写成已经成立的事实。

A4 灌满、4096 个汇报者的外推是 7897 updates/s，高于已测下界。本轮不能判断那一档能不能稳住。A4 不是长期平均 serving，也不能和 A1 相加。

本轮没有人为放慢 Dispatcher。没有 tc，没有 Apply sleep，没有 CPU 配额。饱和判定要求 Dispatcher 进程 CPU 升高并且路径积压增长。这次没有出现这种格子。4000 updates/s 的 Dispatcher CPU 见容量表，三次平均大约 0.38–0.42 核。

S2 在这一档把索引插到约 140000 键时，生成到 Apply 的 P99 最高 43.60 ms，事件循环心跳最大滞后 171.61 ms。同一档 S1 的 P99 最高 0.64 ms。S2 的 P95 仍低于 1 ms，30 秒 Apply 速率没有掉下去。这不是持续容量损失。A1 的 2121/s 低于出现这段尾部的档位。A4 的 7897/s 高于已测范围，不能用这段尾部推它是否饱和。

下一轮没有必要为了证明“共享 Dispatcher 索引是瓶颈”去做多 Gateway 的大型 trace-driven 回放。A1 外推低于已测稳定下界，回放预期仍然排得空，不能把拥塞变成证据。路由可见性和误判不在本轮测量里。
