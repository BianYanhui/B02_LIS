# Model B 控制路径

下面是当前代码能够实现的路径，不是已部署的拓扑。

```text
Workers --TCP--> Gateway 进程 g
                    agent_reader          无独立 ingress 队列
                    enqueue
                      supersede (instance, digest)
                      replica cap，本进程
                      topk_best，最多 K 个未发送 lineage
                    queue / pqueue        用户态待发队列
                    release_loop
                      离开 topk_best
                      SO_SNDBUF 2304 + asyncio 高水位 2048
                    eth0                  只有这只容器配置了 HTB 时才有 C_g
                         |
                         |  每只 Gateway 自己的 TCP 连接
                         v
                    Dispatcher :9711
                      每连接一个 _on_down
                      同一个 asyncio 循环
                      on_frame 同步写入 tested[(worker, digest)]
                      然后 K_ACK
                      无 apply 队列
                         |
                    choose() 读这份字典
```

多只 Gateway 是多只进程，各有自己的 `Relay` 状态和一条下游连接。Dispatcher 的 `start_server` 会接受多条连接（`runtime.py` 267 行，formal 路径 441 行）。现有脚本只启动一只。

## 队列与容量

| 层 | 有没有真实队列 | 容量 |
|---|---|---|
| Gateway ingress | 没有单独的队列。读一帧就 `enqueue` | 受事件循环调度，`NOT_MEASURED` |
| Pending set `topk_best` | 有。未发送的 distinct digest | StaticTopK 为 16。这是待发 lineage 数，不是发送速率 |
| 用户态 `queue` | 有。`max_queue` 只在 mode 6 尾丢时生效 | StaticTopK 是 mode 4，不走这条尾丢 |
| TCP 发送缓冲 | 有，被设成约 2304 字节 | `CODE_CONFIRMED` 的配置，不是测到的瓶颈 |
| 本机 HTB | 一只 Gateway 容器的 eth0 上才有 | 10 Mbit 是某次实验的配置。第二只 Gateway 不会自动共享它 |
| Dispatcher 接收缓冲 | 内核套接字缓冲 | 代码未设置，`NOT_MEASURED` |
| Apply 队列 | 没有 | apply 在读循环里完成 |
| KV index | 字典，按键覆盖 | 不是速率上限 |
| Query | `choose` 直接读字典 | 没有单独的可见性队列 |

`C_g` 是这只 Gateway 实际能写进自己下游套接字的速率。代码里的上限只来自这只容器自己的 HTB，以及套接字缓冲。没有共享的 `C_shared_net`，除非两只 Gateway 的报文从同一块被整形的网卡出去。当前实现没有这块整形。

`F_g` 是 `release_loop` 成功 `write` 的速率，不是 Worker 的产生速率。`L_D = sum F_g` 进入同一个 `on_frame`。`C_D` 是这个循环完成字典更新的速率，本轮 `NOT_MEASURED`。

可能出现 `L_g < C_g` 同时 `sum F_g > C_D`：每只 Gateway 的出口都够快，合计写入超过 Dispatcher 的处理。当前结构里，超额帧先堆在 Dispatcher 的接收缓冲。缓冲满之后，各连接的 `drain` 停住，超额才回到各 Gateway 的用户态队列。
