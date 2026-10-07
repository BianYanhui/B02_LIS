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

更新时间：2026-10-07 12:11（UTC+8）。链路受限 smoke 已结束，代码已推送。没有实验进程。不要开正式矩阵，不要启动 `overnight.py` 或 `full_queue.py`。

### 正在做的事

上一份交接里的实验代码已经在 `7e41dfa` 提交并推送。其后按根因意见改了网关准入，并跑了一格 10 Mbit 的短 smoke，看 Adaptive 在策略队列成为瓶颈时是否好于 FullSync / StaticSemantic。

`f3a0058` 改了三处。`experiments/4t4/net/gateway_relay_4t4.py`：进队前按 coverage 做 O(limit) 的 top-k 拒绝，不再每帧拆整条 FIFO；拥塞要队列降到 `--adaptive-queue-exit`（默认 2）并保持 `--congestion-exit-hold`（默认 0.5 s）才清除；`recent` 用 `OrderedDict.popitem`；读循环每 256 帧 `sleep(0)`。`experiments/icc_kv/runtime.py` 的 `prepare_fixed_gateway(link_bit_s)` 接受一次固定天花板，默认仍是 1 Gbit。`experiments/icc_kv/e2e.py` 增加 `--link-bit`，0 表示 1 Gbit。单测 `experiments/4t4/test_policies.py` 已通过，输出在 `/tmp/icc_policy_checks.csv`。镜像 `b02-gw4t4` 为 `sha256:d95dcb1f13e9`，创建于 2026-10-06 09:20 UTC。

### 当前状态

已完成，停着。没有 `experiments.icc_kv`、`overnight` 或 `full_queue` 进程。

Smoke：`/tmp/icc_link_smoke/e2e_summary.json`，4 行，`failed_requests` 全是 0。命令是容量 8000、场景 ultrahigh、方法 Ideal,FullSync,StaticSemantic,Adaptive、种子 1、每格 20 条、到达率 0.237、噪声发送端 8、`--link-bit 10000000`。容器里 `tc class show dev eth0` 是 `rate 10Mbit`。摘要字段 `fixed_link_bit_s` 仍是 1000000000，因为 `ACTIVE_LINK_BIT_S` 写在父进程，子进程的 `meta()` 读到的是默认值。不要用这个字段判断天花板。`offered_noise_per_s` 约 14500，`forwarded_per_s` 约 8200–9400，`gateway_cpu` 约 0.49–0.64。顺序是 StaticSemantic、Ideal、FullSync、Adaptive。

| 方法 | prefill | 假阴性 | 错放 | 前景 P95 | 前景送达 | 失效 P95 | 队列峰值 | 命中 TTFT |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Ideal | 2702 | 0 | 0 | — | 本地 | — | 582k | 1061 ms（7） |
| FullSync | 2905 | 0.20 | 0.15 | 39.0 s | 12/20 | 25.7 s | 572k | 1378 ms（5） |
| StaticSemantic | 3315 | 0.30 | 0.30 | 37.1 s | 6/20 | 14 ms | 552k | 4579 ms（3） |
| Adaptive | 2497 | 0 | 0 | 14 ms | 20/20 | 12 ms | 17 | 1180 ms（8） |

假阴性是 `loose_false_negative_rate`，错放是 `wrong_placement_rate`。StaticSemantic 的失效走优先队列，所以失效仍是 14 ms，普通更新没有。Adaptive 的 `low_utility` 是 0，队列停在 17，说明丢掉的是 top-16 之外的噪声。收到 1304843、转发 844974、结束队列 0，`ledger_balance_gap` 约 0.35。STATS 帧没有 `global_topk` 栏，这 35% 就是那些丢弃，不是丢包。

结论：这条 10 Mbit 链路上，Adaptive 的前景和失效都在十几毫秒，假阴性和错放为 0，与 Ideal 同侧；FullSync / StaticSemantic 的前景积压到约 37–39 秒。prefill 和端到端 TTFT 方向相同，但只有 20 条，而且 Adaptive 的 prefill 低于 Ideal，不能写成服务时间优势。命中服务 TTFT 是 Adaptive 1180 ms、Ideal 1061 ms。上一次 `/tmp/icc_validate_smoke/e2e_b`（约 44000 帧/秒、网关 CPU 约 1、前景 Adaptive 1.15 s）仍然成立，那是单核打满，不要和这格混成一个结论。`/tmp/icc_validate_smoke/e2e` 仍是序号 bug 之前的一行，不要用。

机器：vLLM 仍是 127.0.0.1:8000–8003，pid 8000=`1375061`、8001=`1375062`、8002=`1375063`、8003=`1375064`，从 2026-09-28 08:52 UTC 起。四张 GPU 利用率 0，显存约 6.7 GiB / 15 GiB。网关容器 `71ce28c1209e`（`b02-gateway4t4`，Up 19 hours）只听 `127.0.0.1:9710`，HTB 仍是这次 smoke 留下的 10 Mbit。9711 没有监听。背景容器 `a81eb1b9461b`（`b02-bgserver4t4`）。没人要求就不要停这些进程。下一次不带 `--link-bit` 的 `prepare_fixed_gateway()` 会把天花板改回 1 Gbit。

### 下一步

用户说「继续」时，不要开正式矩阵，也不要重跑 smoke。只修两处记账：子进程 `meta()` 要报出真正的 HTB 天花板；STATS 帧要带上 `global_topk` 丢弃数。改完停下来等用户决定要不要加长这格。

### 已经定下来的约束

- 默认天花板仍是 1 Gbit，不要按 ρ 改 HTB、tau、效用系数。这次 10 Mbit 只是 `--link-bit 10000000` 的一格 smoke。Gateway `--cpus 1 --cpuset-cpus 0`。
- 背景噪声 coverage 256，请求前缀 `U0000`–`U0015` coverage 4096。
- 不要把 4 张 GPU 写成大规模系统。不要把 0.73–3.04 updates/s 和 84.5×10³ updates/s 写成同一个瓶颈。
- 正式结果不要写回 `e2e_full`、`scale_c13000`、`burst_c13000`、`ablation_c13000`，也不要写进 `/tmp/icc_validate_smoke/`。
- Python：`/home/byh/B02/poc/.venv/bin/python`。`analysis/icc_kv/` 在 `.gitignore`。

### Git

分支 `main`，与 `origin/main` 同步。HEAD 是 `f3a0058`（Drop frames that miss the coverage cap before they enter the queue.）。上一笔是 `7e41dfa`。不要 amend。

不要提交、不要推送：`ICC_KV_实验结果_20261005.zip`、`analysis/icc_kv_export/`、`analysis/admission_overhead_4t4/`、`supplemental_20260922_cp_queue_delay/`、论文 PDF、`/tmp/icc_link_smoke/`。
