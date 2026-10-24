# P1 Experiment Report

代码 `ea3b335`。Gateway 文件未改，哈希 `7ef3c660…f1fd6`。正式矩阵 594 组全部 `balance_ok`，另有 5 组校准。种子 `2026102301`、`2026102302`、`2026102303`，没有因结果不好而删种子。墙钟约 1.87 小时，其中三组 N=4096、ρ=1.2 的 AgeCov-Greedy 各用了约 1.5–1.8 小时 CPU，因为正式 `_dequeue` 每次都扫描整条待发队列。汇总在 `aggregate.csv`。图是 `fig8a_scaling_delay.pdf`、`fig8b_scaling_routing_fn.pdf`，K 扫描是 `fig_k_sensitivity.pdf`。ρ=1.2 的对照图是 `figS_rho12_*.pdf`，不是 Fig. 8。

逻辑 worker 不是 GPU。链路是虚拟 bfifo，服务率锚定 10 Mbit/s 上测过的 9407.55 frame/s。2/5 Mbit/s 是线性缩放。

## Experiment A：固定带宽

Offered 帧率（upsert+tomb，本组 tomb 为 0）随 N 上升：1.094、15.18、60.23、240.2、957.9 frame/s。相对 9407.55 frame/s 的 ρ 是 0.00012、0.0016、0.0064、0.0255、0.102。2 Mbit/s 上 N=4096 的 ρ 是 0.509。5 Mbit/s 介于两者之间。没有为了做出拥塞去改主链路。

10 Mbit/s 上，FullSync、LatestOnly、AgeCov-Greedy、KVSelect 的 routing FN 在五个规模都是 0。Digest 级 P95 延迟在 N≤1024 都是 106 µs；N=4096 时前三条是 171 µs，KVSelect 是 151 µs。这就是一帧到两帧的服务时间。Gateway 队列最大值在这条带宽上是 1。

RateFIFO 是例外。令牌桶 burst=1，和 reuse 正式运行的 `rate_burst_frames=1` 相同。N=4096 时 10 Mbit/s 上 FN 均值 1.55%（1.543–1.550%），P95 延迟 39.8 s（38.6–41.2 s）。5 Mbit/s 上 FN 2.92%、延迟 80.2 s。2 Mbit/s 上其他四条策略的 FN 仍是 0。RateFIFO 的丢弃来自波内 10 ms 的四条突发超过 burst=1，不是 Top-K。

KVSelect 在 10 Mbit/s、N=4096、种子 2026102301 上转发 131072 / 188416 帧，`replica_cap` 57344，`global_topk` 0。少掉的是每个 group 里超过 2 个的 owner，不是覆盖排名。Digest 未送达数是 0。

N=4096、10 Mbit/s 的 Gateway CPU 时间：FullSync 3.44 s，KVSelect 3.85 s，AgeCov 4.03 s。188416 次事件摊下来大约每事件 20 µs。RSS 约 290 MB，里面主要是轨迹和 routing log。这个负载下瓶颈不是 Gateway CPU，也不是 10 Mbit/s 链路。

Fig. 8 画的就是这个结果。除了 RateFIFO 在 4096 抬起来，其余四条线贴在服务时间地板上。

## Experiment B：同一 ρ

服务帧率 = 该种子轨迹的 offered frame/s / ρ。ρ 不是事后挑选的。

ρ=0.8：除 RateFIFO 外，FN 都是 0。RateFIFO 在 N=4096 仍有 9.6% FN。令牌桶在 ρ<1 时就已经丢掉突发。

ρ=1.2 的 FN 均值（N=4, 64, 256, 1024, 4096）：

| 策略 | 4 | 64 | 256 | 1024 | 4096 |
|---|---:|---:|---:|---:|---:|
| FullSync | 13.5% | 32.9% | 35.6% | 36.5% | 36.8% |
| LatestOnly | 13.5% | 32.9% | 20.9% | 16.7% | 17.4% |
| AgeCov-Greedy | 13.5% | 32.9% | 30.0% | 28.2% | 27.8% |
| RateFIFO | 12.8% | 20.2% | 13.8% | 13.1% | 12.9% |
| KVSelect | 1.4% | 1.8% | 1.9% | 2.2% | 3.3% |

KVSelect 的 4096 是三颗种子 0.83%、4.84%、4.84% 这一档的均值，不是一条更低的隐藏种子。ρ=1.0 时 KVSelect 的 FN 是 0、0、0.027%、0.20%、0.90%，延迟 P95 均值降到 N=4096 的 0.67 s。ρ=1.2 的延迟 P95 均值是 10.7、11.2、12.1、16.6、29.3 s。N=4096 的三颗种子是 0.81 s、36.4 s、50.6 s。均值 29 s 被两颗种子拉高，不能写成「延迟不随规模变化」。图上的区间用 df=2 的 t 区间，会宽到跨过 0；原始最小值仍是 0.81 s，不是负数。

FullSync 在 ρ=1.2、N=4096 把 188416 帧里的 161632 帧送完，队列最大 46219，P95 延迟 54.6 s，FN 36.8%。KVSelect 同一档的主要丢弃仍是 replica cap（种子 2026102301 为 57323），Top-K 是 5013，superseded 是 3913，队列最大 16。Stale-positive 在该种子上是 0。剩下的 FN 是视图里还没有可用 owner。

ρ=1.2 下 LatestOnly 的 FN 从 N=64 的 32.9% 降到 N=4096 的 17.4%，因为合并缩短了队列。它仍高于 KVSelect。AgeCov 的延迟升到 85.9 s，FN 27.8%，没有比 FullSync 更稳。

## Experiment C：K

预先固定 ρ=1.2。N=4096 的 FN 均值：

| K | KVSelect | BoundedSemantic | BoundedFIFO | BoundedPrio |
|---:|---:|---:|---:|---:|
| 16 | 3.27% | 1.91% | 4.27% | 4.27% |
| 32 | 3.09% | 1.67% | 4.27% | 4.27% |
| 64 | 3.22% | 1.60% | 4.27% | 4.27% |
| 128 | 3.34% | 1.57% | 4.27% | 4.27% |

K=16 在 4096 个逻辑 worker 上没有崩。把 K 加到 128 也没有把 KVSelect 的 FN 降下去。BoundedSemantic（合并、失效优先、replica cap 2、队列长度 K，没有 coverage Top-K）比 KVSelect 低大约 1.3–1.7 个百分点和大约 10 s 延迟。BoundedFIFO 与 BoundedPrio 数值相同，因为这条轨迹的 tombstone 是 0，优先队列没有帧。仓库里的 BoundedFIFO 不带 replica cap，这里没有给它补上。

N=256 时加大 K 还让 BoundedFIFO 的 FN 从 12.7% 升到 14.3%。多留在队列里的旧帧会占用服务时间。这个方向不利，图里保留了。

## 证据链

Worker 从 4 加到 4096，按每组 1.09 frame/s 放大后，10 Mbit/s 的 ρ 只到 0.10。协调延迟停在一帧服务时间，路由 FN 为 0。这不能用来证明拥塞下的算法优势。

把服务率压到 offered/1.2 之后，链路成为瓶颈。FullSync 的 FN 随规模升到约 37% 后趋平。KVSelect 停在约 1–3%，主要靠 replica cap，不是靠 K=16 的覆盖筛选。Gateway CPU 没有成为瓶颈。状态年龄的 P95 在 80–170 s，数量级等于轨迹长度：前缀在 overlap 波就广告了，后面的请求只是在读一个早就写过的视图。它不适合当作这次规模实验的主图。

## 能证明什么，不能证明什么

能证明：现有 Relay 的准入和出队，在这条按组复制、开环、reuse-intensive 的轨迹上，面对按 9407.55 frame/s 标定的虚拟链路时，上面这些计数。

不能证明：实测 TTFT、GPU 吞吐、4096 张 GPU、跨机或跨站点的网络、闭环调度下的同一条轨迹、失效优先在有 tombstone 时的收益。Adaptive 没有跑。论文里的 46.1% 和四卡 TTFT 不是这张图。

适合放进正文的是两句：固定 10 Mbit/s 下，按四卡 reuse 速率放大到 4096 个逻辑 worker，ρ 约为 0.10，FN 为 0；在预先规定的 ρ=1.2 下，KVSelect 的 FN 维持在约 3%，而 FullSync 约为 37%，且把 K 从 16 提到 128 没有再降低 FN。Fig. 8 如果只放 Experiment A，读者会看到地板和 RateFIFO 的突发丢弃。ρ=1.2 的分离在补充图里。K 扫描不支持「加大 K 就能修好 4096」这句。

## 失败与局限

没有失败的种子。AgeCov 在 N=4096、ρ=1.2 上很慢，是正式扫描实现的代价，结果已计入。虚拟链路没有和本次进程里的 tc 再对一次。组间相位是均匀随机，不是生产到达。开环把 KV 事件从路由决策里拆了出去。
