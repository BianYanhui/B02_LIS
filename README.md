# B02

LLM 推理服务的实例–调度器控制面。当前主线是 ICC 论文实验：控制面过载时，网关按前缀覆盖度做有界准入（coverage-priority bounded admission）。主方法是 `StaticTopK16`：只保留覆盖度最高的 k 条待发更新，tombstone 优先，同 key 合并去重。

仓库里还留着 2026-07 的动机实验和其后的多轮补充。开跑和读当前代码以 `docs/icc_formal/`、`experiments/icc_kv/` 为准。两人交替改这个仓库时，先读 `handsoff.md` 分隔线以下。

## 当前实验

`Adaptive` 是消融。对照包括 `BoundedSemantic16`、`BoundedPrio16`、`BoundedFIFO16`、`FullSync`、`Ideal`。主张、假设和剔除规则在 `docs/icc_formal/PREREG.md`。仓库里的那份是空白模板，填好的副本放在仓库外。

正式矩阵在 `experiments/icc_kv/full_queue.py`：

| block | 内容 |
|---|---|
| main | xhigh / ultrahigh / burst 上的主比较 |
| ksweep | k = 4、8、16、32、64 |
| overlap | 覆盖度重叠 ov0、ov10、ov30、ov60 |
| ablation | 去掉 merge、dedup、priority，以及 Adaptive |
| rhoscan | normal 到 ultrahigh 的负载扫描 |

固定配置：4× NVIDIA T4，每卡一个 Qwen2.5-1.5B-Instruct（`127.0.0.1:8000–8003`），控制面 10 Mbit HTB，网关 1 个 CPU。状态帧 64 字节，路径是宿主机 TCP → Docker 网关 → Linux HTB → 调度器 TCP。策略编码在 `experiments/icc_kv/wire.py`，网关实现在 `experiments/4t4/net/gateway_relay_4t4.py`。

实验代码钉在 `48b64f4`。其后的 `docs/icc_formal/` 提交只改文档。开跑前 `git diff --stat 48b64f4 HEAD -- experiments` 应为空。结果目录 `analysis/icc_kv/` 已在 `.gitignore` 中。

按这个顺序读：

1. `docs/icc_formal/START_HERE.md`
2. `docs/icc_formal/PREREG.md`
3. `docs/icc_formal/RUN_GUIDE.md`

Python：`/home/byh/B02/poc/.venv/bin/python`。

## 目录

| 路径 | 内容 |
|---|---|
| `docs/icc_formal/` | 本轮正式实验的入口、预注册模板、操作手册、`check_smoke.py` |
| `experiments/icc_kv/` | 正式矩阵：单格执行、block 队列、容量窗口、负载、策略 |
| `experiments/4t4/` | 4×T4 网关、策略单测，以及更早的冻结基线 |
| `analysis/` | 已入库的汇总。`analysis/icc_kv/` 不入库 |
| `poc/` | vLLM 虚拟环境，以及早期可观测性探测 |
| `experiments/` | 2026-07 的动机实验（语义 State Sketch） |
| `shared_link_exp/`、`ksweep/`、`supplemental_*/`、`full_experiment/`、`tradeoff_experiments/`、`N256_dispatch/` | 各轮补充和中间实验 |

## 本地产物

这些留在工作区，不提交、不推送：

- `analysis/icc_kv/`
- `ICC_KV_实验结果_20261005.zip`
- `analysis/icc_kv_export/`
- `analysis/admission_overhead_4t4/`
- `supplemental_20260922_cp_queue_delay/`
- `docx/prompt/` 与论文 PDF

## 2026-07 动机实验

那一轮测量的是 State View 的体积：agentic workflow 状态下，完整语义状态比粗粒度视图大约一个数量级，紧凑 Sketch 可以压回接近粗粒度。设计和结论在 `experiments/design.md`、`experiments/results/analysis_report.md`。复现用 `experiments/scripts/` 里的 `launch_4vllm.sh`、`run_part_a.py`、`run_part_b.py`、`aggregate.py`。
