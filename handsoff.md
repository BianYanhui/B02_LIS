# handsoff

本段到下面的分隔线为止是固定说明。后来的 Agent 不要改写、不要删、不要把当次进度写进这一段。每次交接只整段替换分隔线以下的内容。

## 这份文档是干什么的

同一个人会在两个 Agent 之间交替做这个仓库的开发。一边做完一段工作，就把分隔线下面更新掉；另一边先读这份文件，然后接着做，不用再口头补一大段上下文。

## 接手时

1. 先读分隔线下面的全文，再看它点名的文件和命令，不要凭记忆补实验设计。
2. 用户说「继续」时，按「下一步」做。没写进下一步的长实验不要自己开。
3. 改代码、跑实验、只查了状态，都算一段工作。离开前更新分隔线下面。

## 交接时怎么写

替换分隔线以下的全部内容，保留分隔线本身。写清这些，有代码 diff 就写，没有也要写：

- 现在在做哪件事，做到哪一步。
- 机器上还留着什么：进程、端口、GPU、容器、没跑完的队列。
- 已经定下来的结论和约束。不要只贴 git diff。
- 卡住的原因，以及下一次打开终端应该先跑的命令或先改的函数。
- 相关 commit、关键路径、明确不要提交或不要推送的文件。

不要把对话原文、完整 diff 或论文 PDF 贴进来。事实写短，路径写全。

---

## 可变交接

更新时间：2026-10-24 12:17（UTC+8）。P1 大规模控制面仿真已写入本提交。论文没改。父提交是 `ea3b335`。分支 `f6-d1b-coverage-dominance`，还没进 main。Gateway 源文件没改。

### 现在在做的事

P1 结束。结果在 `analysis/p1_large_scale/`。KVSelect 仍是 StaticTopK（合并、失效优先、replica cap 2、K=16）。Adaptive 没跑。

### 结论

固定 10 Mbit/s、reuse 速率按组放大：N=4096 的 ρ 是 0.102。FullSync、LatestOnly、AgeCov、KVSelect 的 routing FN 是 0，digest P95 是 106–171 µs。RateFIFO（burst=1）在 4096 上 FN 1.55%、P95 39.8 s。

ρ=1.2 是预先规定的。FullSync FN 从 13.5% 到 36.8%。KVSelect 是 1.4–3.3%。N=4096 的延迟三颗种子是 0.81、36.4、50.6 s，不能写成平坦。K 从 16 到 128 没有降低 KVSelect 的 FN。BoundedSemantic 更低。这条轨迹 tombstone 为 0，BoundedPrio 等于 BoundedFIFO。

10 Mbit/s 上 KVSelect 少转发的是 replica cap，Top-K 丢弃是 0。不要写成算法在链路上的收益，不要写成 4096 张 GPU，不要换成 TTFT。

### 路径

先读 `analysis/p1_large_scale/P1_EXPERIMENT_REPORT.md`。审计在 `P1_REPOSITORY_AUDIT.md`，校准在 `P1_CALIBRATION.md`。Fig. 8 只含 Experiment A。ρ=1.2 在 `figS_rho12_*.pdf`。

### 机器

`127.0.0.1:9710` 没动。没有留下 P1 的 Gateway 或 vLLM。矩阵进程已退出。

### 下一步

停。不要改论文，不要把 Fig. 8 的地板写成 KVSelect 的规模优势，不要加大 K 再跑一轮来找更低的 FN。

### 不要覆盖

不要覆盖 C1、formal4t4、`ksweep_ov30`、`analysis/p1_large_scale/runs/`。不要停 9710。
