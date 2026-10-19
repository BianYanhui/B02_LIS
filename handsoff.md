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

更新时间：2026-10-19 17:15（UTC+8）。Fig. 6 重建已整理，准备并入 main。没有正在跑的实验。`/handsoff-skill` 没有对应 Skill 文件，这次没有调用。

### 现在在做的事

Fig. 6(a)(b)(c)(d) 的证据已经收在 `analysis/fig6_reconstruction/`。索引是 `analysis/fig6_reconstruction/README.md`。下一模块还没开始。不要自己开新实验。

分支：`f6-b1-real-invalidation`。Fig. 6(b) 的测量代码是 `3a3ce7d`，结果是 `6e8ee8d`。本交接在整理提交之后。没有改 `experiments/4t4/` 的冻结网关和 `Dispatcher.choose`。

### 论文该引用哪一版

- Fig. 6(a)：`f6_a2/`，Case C。自然投递不合并。受控 inflight 窗口下转发更新减少均值 0.549，P95 可见性 16.84 s 到 4.09 s。
- Fig. 6(b)：`f6_b1/`，条件收益。自然队列为空。冻结 ACK 窗口 0.792 s 下，决策层无效路由 FIFO 3/3、优先级 0/3。
- Fig. 6(c)：`f6_f1_formal_ac/`。冻结 `Krep=2` 回放的重叠削减。不是在线副本上限，也不是 TTFT。
- Fig. 6(d)：`f6_d1/`，Case A，负载 0。配对客户端 TTFT 均值差 1813 ms。61.44 ms 的模型 regret 不是这个延迟。

`f6_p0/` 到 `f6_p3/` 和 `analysis/p0_pressure_realism/` 留作阴性记录。F6-F1 的同覆盖度约 21% 不是 Fig. 6(a) 的扩展合并结论。候选图没有替换论文图。

### 丢掉的部分

没有删已入库的轮次。下列本地目录只忽略、不提交，文件还在磁盘上：

- `ICC_KV_实验结果_20261005.zip`
- `analysis/admission_overhead_4t4/`
- `analysis/icc_kv_export/`
- `supplemental_20260922_cp_queue_delay/`

`analysis/icc_kv/` 本来就在 `.gitignore`。

### 机器

四张 T4 为 0 MiB。vLLM 已停。8000–8003、9730、9731 没有监听。`b02-gateway4t4` 仍在，`127.0.0.1:9710`。

### 下一步

用户指定下一个模块之前，不要开 GPU，不要重跑 Fig. 6，不要改 0.792 s 的 ACK 保持，不要改 `choose`，不要改论文图。正式矩阵的开跑方式仍是 `docs/icc_formal/`。

### 不要提交、不要覆盖

不要提交上面四个本地目录、`server_logs`、`*.log`、`*.pid`。不要改 `analysis/formal4t4/`、`analysis/icc_kv/`、KVSelect 核心算法。不要重启或重配 `b02-gateway4t4`，不要跑 `cell_rate_4t4.sh` 或 `restart_4t4.sh`。
