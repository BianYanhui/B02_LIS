# 瓶颈在哪一层

共享瓶颈是否发生：`NOT_MEASURED`。

代码能定位的共享点只有 Dispatcher 进程：一个事件循环、一张 `(worker, digest)` 表、所有 Gateway 的 TCP 都读进这里。多条 TCP 不会因此共享一条 10 Mbit 的逻辑链路。10 Mbit HTB 挂在单只 Gateway 容器的 eth0 上，过滤目的端口 9711（`runtime.py` 137–148）。第二只容器有自己的 eth0，不会自动用同一条 class。

## 和 P0-M1 速率的对照

P0-M1 的 compact 正式负载是 `EXTRAPOLATED`：256 / 1024 / 4096 个一卡汇报者约 133 / 530 / 2121 updates/s。按每 Gateway 64 个汇报者，Gateway 数为 4 / 16 / 64，每只入口约 33 updates/s。这是均匀拆分，不是测量。

33 条/s 进一只出口没有整形的 Gateway 时，`release_loop` 会马上把帧写走，`topk_best` 随写随减。待发集合到达 16 的条件是出口停住，不是这 33 条/s 本身。`DERIVED` 自代码，筛选次数 `NOT_MEASURED`。

2121 条/s 落在一个 Dispatcher 上，也还没有饱和测量。不能由“索引是共享的”推出它会积压。P0-C1 的 256 events/s 是原生事件的单连接回放，不能当作这里的 compact apply 容量。

## 两种速度

下游很快：用户态队列保持很短，Top-K 很少删除。历史 ICC 实验里大量 `global_topk` 丢弃，来自单 Gateway 上配置的 10 Mbit 加上合成噪声，入口远高于这条整形出口。那是 `HISTORICALLY_MEASURED` 的单 Gateway 整形，不是 Model B 的共享 Dispatcher。

Dispatcher 比 `sum F_g` 慢：读循环停住，接收缓冲先涨。缓冲是内核的，Top-K 看不见。缓冲满以后 TCP 把 `drain` 堵住，各 Gateway 的未发送队列才变长，Top-K 开始删未发送的 lineage。已经在缓冲里的帧会被 apply，Gateway 不能再筛选。

所以固定 Top-K 对共享压力的作用是 `CONDITIONAL_ON_TRANSPORT_BACKPRESSURE`。
