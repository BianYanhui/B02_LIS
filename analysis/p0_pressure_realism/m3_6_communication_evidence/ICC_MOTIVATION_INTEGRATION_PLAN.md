# P0-M3.6 ICC Motivation 接入建议

论文文件是 2026-10-21 的六页 PDF：

`/home/byh/.cursor/projects/home-byh-B02/uploads/0d7cd075-1999-420e-8182-c5bf8e468e96/Bare_Demo_of_IEEEtran_cls_for_IEEE_Journals (5).pdf`

769899 B，6 页。仓库里没有这份正文的 TeX。本轮没有改 PDF，也没有改 Fig. 1。更早的 `KVSelect_20261016.pdf` 不作为这一版。

页码按这篇六页 PDF 的阅读顺序对应到节。引用以节号和句子为准。正文从 PDF 文本层还原，连字符断行已经接回。

草稿图在 `motivation_byte_cost_draft.png`。它对比的是这一条单引擎 Trace 的字节，不是论文 Fig. 1 的时序语义图。不要用它替换 Fig. 1。

## 主张判断

| 主张 | 判断 | 依据 |
|---|---|---|
| A Native KV-Block 生命周期元数据在真实采样里有显著通信成本 | `PARTIALLY_SUPPORTED` | 相对 Compact 帧，284529 B 对 15552 B，18.3 倍，75.6% 的 payload 来自 token id。单引擎平均只有 38.0 kbit/s，最忙 1 秒 70.2 kbit/s。显著的是表示差距，不是单引擎把一条 10 Mbit/s 链路占满 |
| B 大规模聚合会进一步提高控制面通信需求 | `PARTIALLY_SUPPORTED` | 条件外推。4096 个同构 Reporter、每 Gateway 64 个、TP=1 的这只引擎重复 N 次。Native 平均 155.7 Mbit/s。不是集群测量，Reporter 不是 GPU 数 |
| C Prefix 表示能显著降低状态同步字节 | `SUPPORTED` | 少 268977 B，94.534%。收益属于 Adapter |
| D KVSelect 能在 Compact 元数据上再减少要传播的更新 | `SUPPORTED`，范围只到这条 Trace 的扩展合并 | 38 条 `superseded`，2432 B，占 Compact 的 15.638%。Top-K 丢弃 0，Replica 丢弃 0。不能把论文里 46.1% 的四 Worker 结果算到这条 Native Trace 上 |
| E 当前结果证明某条真实生产控制链路已经拥塞 | `NOT_SUPPORTED` | 没有生产链路测量。10/100/1000 Mbit/s 是假设容量 |
| F 当前 KVSelect 已经完整、安全地支持 Native block 生命周期 | `NOT_SUPPORTED` | 实验 lineage 映射、旧 UPSERT 重放会复活状态、满额 Top-K 会丢掉 coverage 回退。M3.5 已经记录这些限制 |

## 建议 1：Introduction 补上表示成本和算法成本的分界

1. 位置：第 1 页，Section I，紧接 “propagates compact lifecycle metadata updates instead” 那一句之后。
2. 当前原文：

> To avoid migrating heavy KV tensor payloads across the edge network, the serving control plane propagates compact lifecycle metadata updates instead [9].

3. 建议替换的英文 LaTeX。原有 Dynamo 引用命令保持不动，写在第一句末尾：

```latex
To avoid migrating heavy KV tensor payloads across the edge network, the serving control plane propagates compact lifecycle metadata updates instead~\cite{<existing Dynamo key>}.
On one single-engine vLLM trace of 59.9\,s, native block metadata occupied 284{,}529\,B, the same activity as 64\,B prefix updates occupied 15{,}552\,B, and extension merging removed a further 2{,}432\,B.
The 15{,}552\,B term is a representation change.
The 2{,}432\,B term is the incremental KVSelect saving on that trace.
Section~IV uses a separate four-worker compact-update workload.
```

若第 1 页放不下这四句，把后四句收成脚注，正文只保留原句。脚注用同一段英文。

4. 为什么改：论文已经声明控制面传的是 Compact 元数据。缺的是这条真实 Trace 上的两个数字，以及它们分别属于 Adapter 和 KVSelect。不加这句，读者容易把 94.5% 的表示差额当成准入算法的收益。
5. 证据：`communication_cost.csv` 的 `A_native_msgpack`、`B_adapter_codec64`、`C_kvselect_codec64`。`MEASURED_NATIVE`、`TRACE_DERIVED`、`ACTUAL_GATEWAY_REPLAY`。
6. 需要新实验：不需要。

## 建议 2：把 “quickly saturate” 收回到容量模型

1. 位置：第 1 页，Section I，Fig. 1 之前的那一句。
2. 当前原文：

> Under chunked prefix caching, concurrent requests generate bursts of fine-grained lifecycle events that quickly saturate limited control-link capacity, gateway queues, and index synchronization.

3. 建议替换：

```latex
Under chunked prefix caching, concurrent requests generate bursts of lifecycle events.
When offered metadata load exceeds the control budget, FIFO queuing delays decision-relevant updates.
```

4. 为什么改：这一句目前写成已经发生的饱和。单引擎实测的平均是 38.0 kbit/s，最忙 1 秒是 70.2 kbit/s。论文 Section II-B 已经把积压定义成 \(\rho_s(t)>1\)，Section IV 用配置的 HTB 制造这个区间。动机句改成与这个模型一致。Fig. 1 后面关于过期广告和过期失效的叙述可以保留，那是机制要解决的调度后果。
5. 证据：Native 窗口在 `communication_burst_profile.csv`。容量比值在 `model_b_scale_projection.csv`，标记 `HYPOTHETICAL_CAPACITY`。单引擎对 10 Mbit/s 假设的平均比值是 0.0038。
6. 需要新实验：不需要。若要保留 “quickly saturate” 作为对生产网络的陈述，才需要新的链路测量。本轮建议不要保留这个陈述。

## 建议 3：Evaluation Setup 标明 64 B 实验不是 Native Trace

1. 位置：第 4 页附近，Section IV-A，Controlled Update-Timeliness Stress 这一段的末尾。以节号为准。
2. 当前原文：

> For the main KVSelect evaluation, 64-byte updates traverse a Linux tc HTB/bfifo path (64-KB link queue; 4096-frame gateway queue), with \(C_s=L_s/\rho\) for \(\rho\in\{0.5,0.8,1.0,1.2\}\). Separately, fixed-capacity tests use a 10-Mbit/s control link (calibrated FullSync drain rate: \(9.41\times10^{3}\) frames/s) with synthetic background updates and four vLLM workers.

3. 在这段之后追加：

```latex
These 64\,B records are the compact prefix frames of this testbed.
The 10\,Mbit/s rate is a configured stress capacity.
Bytes from the separate single-engine native block trace are not included in this offered load.
```

4. 为什么改：论文的 46.1%、状态年龄和 TTFT 来自四只 vLLM 的 Compact 更新实验。Native Trace 是另一条工作负载。并置一句，避免审稿人把 284529 B 和 HTB 上的 \(\rho\) 读成同一次实验。\(9.41\times10^{3}\) frame/s 是 104 B 记账下的标定排空，不要拿它去除本轮的 64 B 条数。
5. 证据：`freeze_manifest.py` 的 `frame_bytes: 64` 与 `wire_bytes_per_msg: 104`；本轮审计里的两条边界。Section IV 的工作负载描述与 C1 的 109 个请求、59.9 s 单引擎采集不是同一份日志。
6. 需要新实验：不需要。

## 建议不动的位置

Section II-A 仍然是一个站点、一只 Gateway \(G_s\)、一只 Dispatcher。Model B 的 4 到 64 只 Gateway 是外推用的研究拓扑。把 \(G_s\) 改成一组 Gateway 会变成架构改写，超出六页里的低改动，也会和 Conclusion 里 “cross-gateway scheduling 属于未来工作” 冲突。本轮不改 II-A。

Section II-B 已经写了平均负载低于 1 时，突发仍可能造成短暂过期。这与本轮的窗口观察一致：1 ms 窗口有 99.6% 为空，峰值是单条 3772 B。这一节不用为了本轮去改。

Section III 写明 gateway 状态建在 compact metadata 上。Algorithm 1 不用加入 Native block 事件。Adapter 不是新的方法小节。

Fig. 1 讲的是 FIFO 把广告和失效一起拖住。本轮的字节账本回答的是另一个问题。不改 Fig. 1，不把草稿柱状图放进论文，除非另有一段正文被删掉并且作者决定增加一个动机小图。那个小图的数据已经在 CSV 里，不需要新实验，但会花掉版面。截稿前的性价比低于建议 1 和建议 3。

Conclusion 的四实例 TTFT 和 fixed-capacity 路由结论来自 Section IV。不用把 Native 适配写成已经完成的能力。Claim F 是 `NOT_SUPPORTED`。

论文状态元组里的版本 \(q\) 没有出现在测到的 64 B 帧里。本轮是通信成本，不借动机修改去改版本字段，也不改 Algorithm 1。

## 截稿前的取舍

优先改 Introduction 的两处和 Evaluation Setup 的一处。三处合计大约六到八行。算法、K=16、Fig. 1 和 46.1% 的正式结果都保持。46.1% 继续只描述四 Worker 的 Compact 实验。
