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

更新时间：2026-09-30 11:48（UTC+8）。没有实验在跑。ultrahigh 已结束。用户没说「继续」就不要开新的长实验。

### 正在做的事

ICC 固定容量实验。C=8000 那一轮的容量、两种扩展、突发、端到端、消融都已完成。随后按用户要求重选 C，并只把 2 倍负载跑完。进程 `2275150` 不在了。9711 没有在听。

### 当前状态

工作状态：已完成，停着。没有 icc_kv 的 python 进程。

C 的重扫日志是 `/home/byh/B02/analysis/icc_kv/capacity_cscan.log`，没有 summary json，因为扫到 14000 后进程被停掉。

- 13000：3/3 全部送达，P95 约 4–11 毫秒，稳定。
- 14000：3/3 缺口约 13%–17%，P95 约 13 秒、37 秒、37 秒，不稳定。

C 取 13000。1.2 倍是 15600，已经高于 14000。旧文件 `/home/byh/B02/analysis/icc_kv/capacity/capacity_summary.json` 里的 8000 不要再拿来乘 ρ。

`/home/byh/B02/analysis/icc_kv/e2e_c13000/e2e_summary.json` 共 29 行。其中 ultrahigh 20/20（5 个 seed × FullSync、StaticSemantic、Adaptive、Ideal，每格 500 条请求，容量 13000）。另外 9 行是更早停掉的 near、high，以及 seed 0 的 xhigh FullSync。日志：`/home/byh/B02/analysis/icc_kv/e2e_ultrahigh.log`。

seed 0、ultrahigh（cell 19–22）：FullSync 假阴性 0.008、prefill 232.8、TTFT 970 ms；Adaptive 假阴性 0.860、prefill 370.7、TTFT 1026 ms。错放率都是 0。FullSync 这一格的 prefill 可能吃到被杀掉的 xhigh 留下的缓存盐 `icc-e2e-19`。seed 1–4 四种方法的 prefill 完全相同，假阴性 FullSync 约 0.012–0.060，Adaptive 约 0.87–0.88。2 倍没有让 Adaptive 的视图好过 FullSync。

更早的 C=8000 结果仍在：

- 容量 8000：`/home/byh/B02/analysis/icc_kv/capacity/capacity_summary.json`。
- 独立扩展 80/80、相关扩展 80/80、突发 40/40。
- 端到端 60/60：`/home/byh/B02/analysis/icc_kv/e2e/e2e_summary.json`。
- 消融 40/40：`/home/byh/B02/analysis/icc_kv/ablation/e2e_summary.json`。错放率和 coverage regret 全是 0。

机器：vLLM 仍空转，pid 8000=`1375061`、8001=`1375062`、8002=`1375063`、8003=`1375064`，从 2026-09-28 08:52 UTC 起，显存约 6.2–6.7 GiB，利用率 0%。网关容器 `b02-gateway4t4` 听 `127.0.0.1:9710`，已启动约 9 小时。`b02-bgserver4t4` 也在。没人要求就不要停这些进程。

已经定下来的结论：

- 8000 不能当 C。C 要让 1.2 倍出现明显排队，且不低于 13000。测出来的点是 13000 稳、14000 不稳。
- 严格错放只在派到不同 worker 且理想覆盖大于 0 时计数。宽松假阴性是真值覆盖不低于 512、视图最高覆盖仍低于 512。先看严格；严格为 0 时用宽松。
- 到 2 倍为止，Adaptive 的宽松假阴性高于 FullSync。原因是网关丢掉了前景更新，不是 FullSync 的队列把视图拖过期。错放率仍是 0，因为空视图默认 worker 0，真值也写在刚选中的 worker 上。seed 1–4 的 prefill 不随方法变。
- 因此 near、high、xhigh、burst 不用为了找 Adaptive 的视图优势再跑。2 倍已经是这套负载里最有机会的一档，没有出现。

### 下一步

用户说「继续」时，不要重跑 C=8000 的队列，不要把五档矩阵自动续上，也不要再启动 `python -m experiments.icc_kv.overnight`。等用户指定下一件事。在那之前不要改放置逻辑，也不要开新的长实验。

### 已经定下来的约束

- Gateway：docker `b02-gateway4t4`，`--cpus 1 --cpuset-cpus 0`，`--max-queue 4096`，tau 30，util_lambda 16，gate 2，adaptive queue gate 8。Dispatcher 绑 CPU 1。HTB 天花板固定 1 Gbit，不按 ρ 改 C。
- 背景 reporter 的 worker id 用 `worker_id + 16`。`choose` 只扫 worker 0–3。Ideal 对前景更新本地生效，不送进链路。
- 不要把 4 张 GPU 写成大规模系统。不要把 0.73–3.04 updates/s 和 84.5×10³ updates/s 写成同一个瓶颈。
- Python：`/home/byh/B02/poc/.venv/bin/python`。原始结果目录 `analysis/icc_kv/` 已在 `.gitignore`。

### Git

分支 `main`，写这份交接之前与 `origin/main` 同步。最近一次已推送的交接是 `b343f0e`。

未提交、这次不要加进去：`experiments/icc_kv/runtime.py`（`PathRuntime.stop()`）、`replay.py`、`burst.py`、`capacity.py`（`--all-rates`，扫完不稳定档也继续）、`e2e.py`（`high`/`xhigh`/`ultrahigh` 负载，burst 基线 0.9，严格错放和宽松假阴性/假阳性同时记录）。用户没说就不要 commit 这些，不要 amend。

不要提交、不要推送：`Bare_Demo_of_IEEEtran_cls_for_IEEE_Journals.pdf`、`analysis/icc_kv/`、未跟踪的 `analysis/admission_overhead_4t4/`、`supplemental_20260922_cp_queue_delay/`。
