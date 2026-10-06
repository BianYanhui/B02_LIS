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

更新时间：2026-10-06 16:01（UTC+8）。验证 smoke 已完成。没有实验进程。不要开正式矩阵，不要启动 `overnight.py` 或 `full_queue.py`。

### 正在做的事

审查要求先修 N1、N2，给 `path_capacity.py` 加 `--policy`，把 offered 放在排空速率和读入速率之间，标定 GPU 到达率，再跑每格 200 条、四个方法。附件里的七条通过标准不在仓库里。代码改完并跑完 smoke，正式矩阵没有开。16:01 再核对过，没有新的实验。

未提交的代码在 `277200f` 之上，工作区相对 HEAD `086463d` 仍是这些文件。副本表用 `OrderedDict.popitem(last=False)`，不再每帧拷贝。墓碑先送出再读视图。放置 RPC 放到线程里。vLLM 按线程和子进程绑核的改动还在工作区，正在跑的四个 vLLM 没有重启，亲和性仍是 0–47。`path_capacity.py` 走分进程路径，`--policy` 可写多个。`--gpu-rho` 标定到达率。单格失败可续跑。`full_queue.py` 增加 `--out-dir` 和 `--resume`。发送端节拍变量曾盖掉序号计数器，重复发送第一批帧；Static 再用 `deque.remove` 扫整条队列。序号已分开，被替换的帧只做标记。网关镜像 `b02-gw4t4` 已按这份源码重建，创建时间 2026-10-06 05:29 UTC。摘要里的 `commit` 仍是 `277200f`，因为这些修改还没提交。

### 当前状态

已完成，停着。没有 `experiments.icc_kv`、`overnight` 或 `full_queue` 进程。

摘要：`/tmp/icc_validate_smoke/e2e_b/e2e_summary.json`，4 行，`failed_requests` 全是 0。容量名义 25000，场景 ultrahigh，`offered_noise_per_s` 约 43700–43900，`measured_rho` 约 1.75。到达率 0.237 req/s。`sched_delay_mean_ms` 约 0.01。`ledger_balance_gap` 和 `ledger_ingress_gap` 都是 0。`relay_queued` 都是 0。

| 方法 | prefill | 假阴性 | 假阳性 | 错放 | 前景 P95 | 失效 P95 | 队列峰值 | 命中 TTFT |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Ideal | 1202 | 0 | 0 | 0 | — | — | 587 | 997 ms |
| FullSync | 1140 | 0 | 0 | 0 | 3 ms | 4 ms | 1853 | 862 ms |
| StaticSemantic | 1140 | 0 | 0 | 0.02 | 4 ms | 4 ms | 3742 | 850 ms |
| Adaptive | 1140 | 0 | 0 | 0.025 | 1.15 s | 1.07 s | 19 | 875 ms |

前景 P95 来自 `foreground_lag_p95_s`，失效 P95 来自 `invalidate_lag_p95_s`，队列峰值是 `relay_max_queue`，命中 TTFT 是 `hit_service_ttft_ms`。端到端 `ttft_mean_ms` 约 3.5–3.8 秒，四种方法接近，不要写成 Adaptive 更快。

结论：这个速率上网关 CPU 约 0.97–1.00，FullSync 和 Static 仍能在数毫秒内排空，Adaptive 的 prefill 与它们相同，前景延迟反而约 1.15 秒。不能写 prefill 改善，也不能写视图更好。TTFT 链不能写。目标 60000/s 的 6 秒探针会在几秒内把队列打到约 16 万，长格子不能放在排空速率之上。`/tmp/icc_validate_smoke/e2e` 只有 1 行 StaticSemantic，是序号 bug 修掉之前的格子，不要和 `e2e_b` 混用。

机器：vLLM 仍是 127.0.0.1:8000–8003，pid 8000=`1375061`、8001=`1375062`、8002=`1375063`、8003=`1375064`，从 2026-09-28 08:52 UTC 起。四张 GPU 利用率 0，显存约 6.7 GiB / 15 GiB。网关容器 `f0a2b7cffe9e`（`b02-gateway4t4`，镜像 `b02-gw4t4`，Up 2 hours）只听 `127.0.0.1:9710`。9711 没有监听。背景容器 `3346b0e1cff4`（`b02-bgserver4t4`，Up 2 hours）。没人要求就不要停这些进程。

### 下一步

用户说「继续」时，先不要开正式矩阵。先把未提交的实验代码提交并推送，再等用户决定过载格怎么改。没说继续就不要跑长实验。

### 已经定下来的约束

- 不要按 ρ 改 HTB、tau、效用系数。HTB 天花板 1 Gbit。Gateway `--cpus 1 --cpuset-cpus 0`。
- 背景噪声 coverage 256，请求前缀 `U0000`–`U0015` coverage 4096。
- 不要把 4 张 GPU 写成大规模系统。不要把 0.73–3.04 updates/s 和 84.5×10³ updates/s 写成同一个瓶颈。
- 正式结果不要写回 `e2e_full`、`scale_c13000`、`burst_c13000`、`ablation_c13000`。
- Python：`/home/byh/B02/poc/.venv/bin/python`。`analysis/icc_kv/` 在 `.gitignore`。

### Git

分支 `main`，与 `origin/main` 同步（这次提交之前）。HEAD 是 `086463d`（2026-10-06 07:01 UTC，Record the validation smoke and the decision not to start the formal matrix）。再往前是 `277200f`。

未提交、这次不要加进去：`experiments/4t4/net/gateway_relay_4t4.py`、`experiments/4t4/test_policies.py`、`experiments/icc_kv/e2e.py`、`full_queue.py`、`path_capacity.py`、`runtime.py`、`split_path.py`。用户说「继续」时才提交这些。不要 amend。

不要提交、不要推送：`ICC_KV_实验结果_20261005.zip`、`analysis/icc_kv_export/`、`analysis/admission_overhead_4t4/`、`supplemental_20260922_cp_queue_delay/`、论文 PDF。
