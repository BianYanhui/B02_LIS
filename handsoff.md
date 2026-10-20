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

更新时间：2026-10-20 14:50（UTC+8）。F6-D1b 已提交在分支 `f6-d1b-coverage-dominance`。vLLM 已停。GPU 空闲。`/handsoff-skill` 没有对应 Skill 文件，这次没有调用。

### 现在在做的事

加强方案已实现并重测 Fig. 6(d)。Krep=2 满员后，严格更大的 coverage 替换最弱 holder（coverage 更小，其次 instance id 更小）。相等 coverage 不替换。已转发的被替换 Owner 会先收到 tombstone，再转发新的 upsert。`Dispatcher.choose`、guard、代价模型没改。

### 结论

正式 D1 四对：平均 ΔTTFT（cap−full）+1.57 ms，中位数 +0.51 ms，样本 SD 2.83 ms，范围 −0.49 到 +5.76 ms。覆盖损失 0。路由变化 0/4。模型 regret 0.00 ms，标签 MODEL_DERIVED，不是 TTFT。两边请求的 cached token 都是 4096。

这是新策略的测量。旧的到达顺序结果仍是 `f6_d1/`：+1812.59 ms、覆盖损失 3072、路由变化 4/4。不要把两者相减，也不要拿去减论文里的 118–391 ms。

D0 相等 coverage，cap 仍留前两名。路由变化 2/4，都在 assignment B，覆盖损失 0。均值 −325.17 ms 只来自第一对正式试验（1435.5 vs 129.6 ms，同一 worker，两边都缓存 4096）。中位数 −0.05 ms。D2 后来的 1024 被丢掉，路由变化 0，覆盖损失 0，均值 −0.30 ms。D3 两对都选中空闲的 4096 owner，覆盖损失 0；均值被第一对的 1424.5 vs 115.8 ms 拉低，第二对 −0.10 ms。

30/30 有效。Pilot 2026102009 不进正式均值。种子是 2026102010–13，不是旧的 2026101910–13。

### 路径

`experiments/4t4/net/gateway_relay_4t4.py` 的 `_admit_replica`。
`analysis/fig6_reconstruction/f6_d1b_coverage_dominance/`。
报告 `F6_D1B_MEASURED_TTFT_REPORT.md`。表：`f6_d1_summary.csv`、`f6_d1_paired_results.csv`、`f6_d1_per_trial.csv`。

### 机器

四张 GPU 0 MiB。本轮 vLLM 已退出。`127.0.0.1:9710` 仍在听。9732/9733 没有残留。

### 下一步

不要 merge main。不要把这组数写成已经替换论文 Fig. 6(d)。

### 不要提交、不要覆盖

不要提交 `*.log` 和 `__pycache__`。不要覆盖 `analysis/fig6_reconstruction/f6_d1/`。不要改 `Dispatcher.choose`。`run_status.json` 里的 `8d4df0b` 是开跑时的 HEAD，保留规则在这次提交里。
