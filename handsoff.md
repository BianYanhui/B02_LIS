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

更新时间：2026-10-17 01:00（UTC+8）。根目录 README 已改成当前 ICC KV 正式实验的入口说明。没有改 `experiments/`。没有实验进程。不要开正式矩阵，不要启动 `overnight.py` 或 `full_queue.py`。

### 正在做的事

替换过期的根目录 `README.md`。旧文还停在 2026-07 的语义 State Sketch 动机实验。新文指向 `docs/icc_formal/`，写明主方法 `StaticTopK16`、五个 block，以及不入库的本地产物。`handsoff.md` 分隔线以下按这次查看过的机器状态重写。

### 当前状态

停着。没有 `experiments.icc_kv`、`experiments.4t4`、`overnight` 或 `full_queue` 进程。`127.0.0.1:8000–8003` 没有在听，vLLM 没起来。网关容器 `b02-gateway4t4` 已 Up 6 天，`127.0.0.1:9710` 在听，9711 没有。同机还有 edgevisor、lhjj、gitlab 等无关容器，不要动。

正式实验入口是 `docs/icc_formal/START_HERE.md`。实验代码 CODE_SHA 仍是 `48b64f4`。改文档之前 `git diff --stat 48b64f4 HEAD -- experiments` 为空。

### 下一步

用户说「继续」时，不要开正式矩阵。先等用户决定要不要把 vLLM 拉起来。开跑仍按 `docs/icc_formal/RUN_GUIDE.md`，并且先核对 `experiments/` 相对 `48b64f4` 没有 diff。

### 已经定下来的约束

- 默认天花板仍是 1 Gbit。10 Mbit 只用于 `--link-bit 10000000`。Gateway `--cpus 1 --cpuset-cpus 0`。
- 主方法是 `StaticTopK16`。`Adaptive` 是消融。k 敏感性、覆盖度重叠、ρ 扫描的划分以 `experiments/icc_kv/full_queue.py` 的 `BLOCKS` 和 `docs/icc_formal/PREREG.md` 为准。
- 不要把 4 张 GPU 写成大规模系统。
- Python：`/home/byh/B02/poc/.venv/bin/python`。`analysis/icc_kv/` 在 `.gitignore`。

### Git

分支 `main`，跟踪 `origin/main`。这次提交只含 `README.md` 和 `handsoff.md`。不要 amend。

不要提交、不要推送：`ICC_KV_实验结果_20261005.zip`、`analysis/icc_kv_export/`、`analysis/admission_overhead_4t4/`、`supplemental_20260922_cp_queue_delay/`、论文 PDF、`/tmp/icc_link_smoke/`、`/tmp/icc_link_smoke2/`。
