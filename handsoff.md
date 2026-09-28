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

更新时间：2026-09-28 10:10（UTC+8）。本次只写了这份交接，没有改实验代码，也没有重新启动队列。

### 正在做的事

ICC 固定容量实验。机制不变，约束从「每个 ρ 用 tc 重调 C」改成「测一次固定容量 C，再改变负载 L」。正式队列在 `experiments/icc_kv/overnight.py`。目标顺序是：容量标定 → 独立轨迹扩展 → 相关轨迹扩展 → 突发 → 端到端 → 消融。

### 当前状态

容量标定完成。`analysis/icc_kv/capacity/capacity_summary.json` 里 `capacity_events_per_s = 8000`。稳定判据是整段 gap ≤ 1%，且末段 P95 lag 不抬升。500 到 8000 events/s 稳定；16000 不稳定（约 13–15% 未送达，P95 lag 约 34–38 s）。队列用的 C 就是 8000。

轨迹在 `analysis/icc_kv/trace/`：2000 个请求、2032 个事件，seed 20260928。这是 harness 的 shadow trace，不是 vLLM radix 的真实 create/update/evict hook。

独立扩展 80/80 完成（4 个 ρ × 4 种方法 × 5 个 seed，180 s，copies=4，correlated=false）。文件：`analysis/icc_kv/scale_independent/scale_summary.json`。日志从 2026-09-27 17:16:29 到 21:19:05。

相关扩展、突发、端到端、消融都没有 summary。`analysis/icc_kv/overnight.log` 在 21:19:05 写完独立扩展，随后四段都是同一错误：

`OSError(98, "error while attempting to bind on address ('172.31.0.1', 9711): [errno 98] address already in use")`

21:21:29 队列写了 `overnight queue finished` 并退出。进程不在了。9711 现在没有监听。9710 仍由 docker gateway `b02-gateway4t4` 听着。

原因：`PathRuntime.start()` 在 `experiments/icc_kv/runtime.py` 里 `asyncio.start_server(..., 9711)`，没有 `stop()`。`run_scale` / `run_burst` / `run_e2e` 各自 `asyncio.run(...)`，上一段的 server 没关，下一段再 bind。`overnight.py` 的 `stage()` 捕获异常后继续，所以四段在几秒内连着失败。独立扩展的 resume 只跳过已完成的 `(seed, rho, method, correlated)`，不能躲开这个端口。

四张 T4 仍被空闲 vLLM 占着，利用率 0%，每张约 6219 MiB。端口与 pid：8000=1212292，8001=1212293，8002=1212294，8003=1212295。模型 Qwen2.5-1.5B-Instruct，`gpu_memory_utilization=0.40`，`max_model_len=6144`。这是 21:21 端到端阶段调用 `experiments/4t4/restart_4t4.sh 0.40 6144` 拉起来的，请求没跑。`restart_4t4.sh` 只杀自己 pid 文件里的进程，端口已被占用时会拒绝启动。再跑端到端之前先确认 pid 文件是不是这四个进程。

### 下一步

用户还没说「继续跑」。说了之后按这个顺序：

1. 给 `PathRuntime` 加 `stop()`：关掉 dispatcher server 和 gateway agent，并在 `run_scale`、`run_burst`、`run_e2e`、`run_capacity` 的 `finally` 里调用。不要让上一段的 9711 留在同一进程里。
2. 再跑 `poc/.venv/bin/python -m experiments.icc_kv.overnight`（工作目录 `/home/byh/B02`）。它会读已有 `capacity_summary.json`，独立扩展按 key 跳过已完成的 80 行，然后做相关扩展、突发、端到端、消融。
3. 端到端会再调 `restart_4t4.sh`。先处理已经占着 8000–8003 的四个 vLLM，避免脚本因端口占用直接失败。

### 已经定下来的约束

- Gateway：docker `b02-gateway4t4`，`--cpus 1 --cpuset-cpus 0`，`--max-queue 4096`，tau 30，util_lambda 16，gate 2，adaptive queue gate 8。Dispatcher 绑 CPU 1。HTB 天花板固定 1 Gbit，不按 ρ 改 C。
- 背景 reporter 的 worker id 用 `worker_id + 16`，避免覆盖真实 worker 0–3。Ideal 对前景更新本地生效，不把它们送进链路。
- 不要把 4 张 GPU 写成大规模系统。不要把 0.73–3.04 updates/s（旧文里按 ρ 配的 HTB 预算）和 84.5×10³ updates/s（单核 `AdaptiveRelay.enqueue` 微基准）写成同一个瓶颈。
- Python：`/home/byh/B02/poc/.venv/bin/python`。原始结果目录 `analysis/icc_kv/` 已在 `.gitignore`。

### Git

分支 `main`。下面这些已经在本地，并且会随这次推送一起到 `origin/main`：

- `f253f02` Add a fixed-capacity ICC harness and keep the manuscript PDF untracked.
- `4e65c7c` Record live KV traces and replay them against one fixed capacity.
- `f2df559` Queue the ICC runs so each stage starts when the previous one finishes.
- 本次新增的 `handsoff.md`。

不要提交、不要推送：`Bare_Demo_of_IEEEtran_cls_for_IEEE_Journals.pdf`、`analysis/icc_kv/`、未跟踪的 `analysis/admission_overhead_4t4/`、`supplemental_20260922_cp_queue_delay/`。用户没说推送就不要 push。没说提交就不要 commit。不要 amend 上面这些 commit。
