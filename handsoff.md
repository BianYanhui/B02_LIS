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

更新时间：2026-09-29 17:36（UTC+8）。消融正在跑，已完成 29/40。不要再开一份，也不要停掉 pid `1727905` 或现有 vLLM。

### 正在做的事

ICC 固定容量实验。容量、独立扩展、相关扩展、突发、端到端都已完成。消融从 2026-09-29 03:11 UTC 起单独在跑，17:36 时已运行约 6 小时 24 分。

Python pid `1727905`，父 shell pid `1727900`：

`poc/.venv/bin/python -u -m experiments.icc_kv.e2e --trace /home/byh/B02/analysis/icc_kv/trace/events.csv --capacity 8000 --scenarios near,burst --methods FullSync,StaticSemantic,AdaptiveNoPriority,Adaptive --seeds 5 --requests 500 --kv-cache-tokens 104544 --out-dir /home/byh/B02/analysis/icc_kv/ablation`

工作目录 `/home/byh/B02`。日志：`/home/byh/B02/analysis/icc_kv/ablation_run.log`。网关容器 `cd9de618e029`（`b02-gateway4t4`，Up 6 hours）听 `127.0.0.1:9710`。dispatcher 由 pid `1727905` 听 `172.31.0.1:9711`。背景容器 `a5b1a7bf2b69`（`b02-bgserver4t4`）。

`/home/byh/B02/analysis/icc_kv/ablation/e2e_summary.json` 在 09:27 UTC 有 29 行。最后一格是 seed 3、burst、FullSync，cell 38，500 条请求，错放率 0。下一格是 seed 3、burst、StaticSemantic。每格大约 13 分钟，剩下 11 格大约还要 2 到 3 小时。29 格的错放率和 coverage regret 最大值都是 0；同一 seed 同一场景里 prefill 和复用 token 不随方法变。

### 当前状态

不要再跑 `python -m experiments.icc_kv.overnight`。上一轮过夜进程 `1288945` 已在 2026-09-28 17:48:01 UTC 退出。它的消融在启动 4 秒后失败：`ConnectionResetError('Connection lost')`。这次是新进程。resume 键是 `(seed, scenario, method)`。

已完成：

- 容量：`/home/byh/B02/analysis/icc_kv/capacity/capacity_summary.json`，`capacity_events_per_s = 8000`。500–8000 稳定，缺口 0，P95 约 0.4–3 ms。16000 不稳定，缺口约 13%–15%，P95 约 34–38 秒。
- 独立扩展：`/home/byh/B02/analysis/icc_kv/scale_independent/scale_summary.json`，80/80。
- 相关扩展：`/home/byh/B02/analysis/icc_kv/scale_correlated/scale_summary.json`，80/80。
- 突发：`/home/byh/B02/analysis/icc_kv/burst/burst_summary.json`，40/40。
- 端到端：`/home/byh/B02/analysis/icc_kv/e2e/e2e_summary.json`，60/60。每格 500 条请求。2026-09-28 08:54:27–17:47:57 UTC。

vLLM 没重启。pid：8000=`1375061`，8001=`1375062`，8002=`1375063`，8003=`1375064`，从 2026-09-28 08:52 UTC 起一直在。四张 T4 显存大约 6.2–6.7 GiB / 15 GiB；17:36 时 GPU 0 利用率约 90%，其余约 0%，因为请求是串行的。

2026-09-29 下午跟用户对过结果，结论如下。不要把这次的 `applied/sent` 说成以前的送达率，也不要把突发的 16 秒、37 秒写成已经测出了错放：

- 这次 `applied/sent` 是调度器收下的帧除以塞进网关的帧。Adaptive 和 StaticSemantic 在每个稳态 ρ 都大约 15%，滞后 1–2 毫秒，FullSync 是 100%。差值来自网关里的合并、副本上限 2，以及突发时的效用门控，不是链路把帧弄丢。突发里 FullSync 的 90%（5 倍，P95 约 16.3 秒）和 85%（10 倍，P95 约 36.9 秒）才是没送完。Adaptive 在 5 倍时收下约 22%、P95 约 3 毫秒，10 倍时收下约 10%、P95 约 5.8 秒。
- 稳态 ρ=1.2 是 9600 events/s，高于 C=8000，FullSync 仍全部收下。独立回放 P95 约 15 毫秒、P99 约 61 毫秒；相关回放 P95 约 5.6 毫秒。请求串行，TTFT 大约 540–670 毫秒，这个延迟落在同一次请求里，到不了「下一次放置时视图过期」。拐点在 8000 和 16000 之间。1.2 倍没有把链路打成旧 tc 那种每秒大约 1 帧的管子。
- 突发回放的 16 秒和 37 秒只统计控制帧从发出到调度器收下的时间。那一组没有 vLLM，也没有调用 `choose`。端到端的 burst 只用了同一档 5 倍形状：基线 0.65×C 跑 25 秒，再以 26000 events/s 冲 5 秒。10 倍没有接到真实请求上。
- 旧的复用基线是 `/home/byh/B02/analysis/formal4t4/summary/cells_baseline_reuse_20260726.csv`，每格大约 184 条更新。ρ=0.5 时 FullSync 转发 184、Adaptive 转发约 116；ρ=1.2 时两边都大约 60，因为 tc 只容得下这么多。Adaptive 以前不是送达更多。当时拉开的是假阴性（ρ=1.2 时 FullSync 0.45、Adaptive 0.24）和 TTFT（大约 1881 ms 对 1668 ms）。那张表的假阳性率是 0。状态年龄 P95 是 100–200 秒。
- 端到端 60 格和消融已完成的 29 格，`wrong_placement_rate` 和 `coverage_regret_mean` 都是 0，prefill 和复用只随 seed 变。原因：`choose` 在视图没有覆盖时默认 worker 0；`truth` 在请求做完后立刻写在刚选中的 worker 上，理想放置也选 0；背景事件的 worker id 加了 16，`choose` 只扫 0–3。不要为了这个重跑前面的阶段。

对照图在聊天旁边的画布，不在这个仓库里：`/home/byh/.cursor/projects/home-byh-B02/canvases/icc-fixed-capacity-results.canvas.tsx`。

### 下一步

用户说「继续」时，先看 pid `1727905` 是否还在，以及 `/home/byh/B02/analysis/icc_kv/ablation/e2e_summary.json` 是否超过 29 行。超过 30 分钟没有新行，再看 `ablation_run.log`、9711 和四张卡。进程若已退出：先读日志里的 traceback。同一条命令会按 summary 的 key 跳过已完成格子。不要另起一套 vLLM，也不要再启动 `overnight`。消融自己跑完之前，不要改放置逻辑，也不要开新的长实验。

### 已经定下来的约束

- Gateway：docker `b02-gateway4t4`，`--cpus 1 --cpuset-cpus 0`，`--max-queue 4096`，tau 30，util_lambda 16，gate 2，adaptive queue gate 8。Dispatcher 绑 CPU 1。HTB 天花板固定 1 Gbit，不按 ρ 改 C。
- 背景 reporter 的 worker id 用 `worker_id + 16`，避免覆盖真实 worker 0–3。Ideal 对前景更新本地生效，不把它们送进链路。消融这一轮没有 Ideal，方法是 FullSync、StaticSemantic、AdaptiveNoPriority、Adaptive。
- 不要把 4 张 GPU 写成大规模系统。不要把 0.73–3.04 updates/s（旧文里按 ρ 配的 HTB 预算）和 84.5×10³ updates/s（单核 `AdaptiveRelay.enqueue` 微基准）写成同一个瓶颈。
- Python：`/home/byh/B02/poc/.venv/bin/python`。原始结果目录 `analysis/icc_kv/` 已在 `.gitignore`。

### Git

分支 `main`，与 `origin/main` 同步（这次提交之前）。已提交：

- `f253f02` Add a fixed-capacity ICC harness and keep the manuscript PDF untracked.
- `4e65c7c` Record live KV traces and replay them against one fixed capacity.
- `f2df559` Queue the ICC runs so each stage starts when the previous one finishes.
- `f8a15ff` Add a handoff note so the next agent can resume the ICC queue.

这次交接只提交 `handsoff.md`。仍未提交、用户没说就不要 commit：`experiments/icc_kv/runtime.py`、`replay.py`、`burst.py`、`capacity.py`、`e2e.py`（`PathRuntime.stop()` 及各阶段 `finally`，用来关掉 9711，避免下一阶段 `address already in use`）。不要 amend。

不要提交、不要推送：`Bare_Demo_of_IEEEtran_cls_for_IEEE_Journals.pdf`、`analysis/icc_kv/`、未跟踪的 `analysis/admission_overhead_4t4/`、`supplemental_20260922_cp_queue_delay/`。
