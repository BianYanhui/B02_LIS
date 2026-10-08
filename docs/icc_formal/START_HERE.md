# START_HERE：ICC KV 正式实验入口（给跑实验的操作者）

> 这是临时目录：`docs/icc_formal/` 只服务于本轮正式实验，实验结束、仓库清理时会整体删除。实验代码 CODE_SHA 为 `103d9d0`：`103d9d0` = `bc59945`（= `3991559` + ksweep 的 StaticTopK32）+ `capacity_window` 平台上限（rho_row ≤ 1.5）。cell / e2e 代码路径与 `3991559` 完全相同，验证 run 仍然有代表性。网关 sha256 仍为 `efc08e2e…8af45`。
> 所有时间均为 UTC+8。数据冻结时间：**Mon 10/12 12:00**。

## 目的

在 10 Mbit 固定链路上跑 ICC 论文的正式矩阵，主张是 **coverage-priority bounded admission**：
- 主方法：`StaticTopK16`；
- 基线：`BoundedSemantic16`、`BoundedPrio16`、`BoundedFIFO16`、`FullSync`、`Ideal`；
- 消融：`Adaptive`；
- 补充实验：k 敏感性、覆盖度重叠、ρ 扫描。

分析规则与剔除规则都已在 PREREG 中预先固定，**开跑前不得修改规则，开跑后只能追加记录**。

## 按顺序阅读

1. `docs/icc_formal/START_HERE.md`：本文件，总清单。
2. `docs/icc_formal/PREREG.md`：预注册，包括主张、固定配置、假设 H1–H7 及证伪标准、有效 cell 判据 V1–V10、统计方法、偏差表。这是**模板**，复制后再填写。
3. `docs/icc_formal/RUN_GUIDE.md`：逐条命令，包括开跑前检查、path_capacity 与 capacity_window、AR、Run A/B、监控、resume、削减顺序、冻结、图表字段。
4. `docs/icc_formal/check_smoke.py`：结果门禁检查，只用标准库。在仓库根目录直接运行，不要复制或修改。

关键代码（快速浏览即可，**不要修改**）：
- `experiments/icc_kv/full_queue.py`：block 运行器，包括 BLOCKS、resume 身份和 `overload_latest.txt`。
- `experiments/icc_kv/e2e.py`：单个 cell 的执行与 summary 字段；容量窗口检查；`--calibrate-only`。
- `experiments/icc_kv/path_capacity.py`：链路容量测量。
- `experiments/icc_kv/capacity_window.py`：从 path_capacity 推导 C 与过载窗口。
- `experiments/icc_kv/workload.py`：覆盖度混合；burst 名义 ρ。
- `experiments/icc_kv/wire.py`：policy 定义（StaticTopK{4,8,16,32,64}、Bounded*、Adaptive）。
- `experiments/4t4/net/gateway_relay_4t4.py`：网关（sha256 应为 `efc08e2e…8af45`）。
- `experiments/4t4/test_policies.py`：40 项 policy 单测。

## 步骤清单

每一步的具体命令见 RUN_GUIDE 对应章节。

- [ ] **1. 拉代码**：`cd /home/byh/B02 && git pull --ff-only origin main`
- [ ] **2. 核对版本**：
  - 必须已经 `git pull --ff-only origin main` 到包含 `103d9d0` 的新 main（不要停在 `2f8a579`）；
  - `git rev-parse HEAD origin/main` 两行一致；
  - `git status --porcelain --untracked-files=no -- experiments` 无输出；
  - `git diff --stat 103d9d0 HEAD -- experiments` 无输出；
  - 设好 `SHA=$(git rev-parse --short HEAD)` 和 `CODE_SHA=103d9d0`。（RUN_GUIDE §1.2）
- [ ] **3. 环境**：4 个 vLLM 端点（8000–8003）返回 200，网关容器 Up，GPU、磁盘、内存正常，没有其他实验在跑。（§1.1）
- [ ] **4. 测试**：
  - `test_policies.py --output …`，40 项全部通过；
  - `python -m experiments.icc_kv.test_harness` 输出 `all passed`。（§1.4）
- [x] **5. StaticTopK32（已完成）**：用户已确认加入 StaticTopK32，已在 `bc59945` 完成（只改了 full_queue.py 中 ksweep 那一行），已包含在 CODE_SHA `103d9d0` 中，无需再改代码。
- [ ] **6. 容量校准**：
  - 速率网格见 RUN_GUIDE §1.5（重复点有意保留，用于可重复性）；
  - 10 Mbit 下跑两遍 path_capacity，加一次 k64 诊断；
  - 用 capacity_window 推导容量窗口；
  - 通过条件：`rho_max_in_window` ≥ 2.0/0.9 ≈ 2.22、`c_ingress_min_per_s` ≥ 2.22×C（C≈9.5–10k 时约 21–22k）、加了 rho_row ≤ 1.5 上限的平台上 `c_drain_spread` ≤ 0.05，且两遍的 C 相差 ≤ 5%；
  - 固定 `CAP_FILE` 并记录它的 sha256。（§1.5–1.6）
- [ ] **7. 确定 AR**：沿用验证 run 的 `arrival_rate`，或者用 `e2e --calibrate-only` 校准一次。A 和 B 都显式传同一个 `--arrival-rate`，**不要用 `--gpu-rho`**。（§1.7）
- [ ] **8. 填 PREREG**：
  - `cp docs/icc_formal/PREREG.md ~/icc_formal/PREREG.md`；
  - 填写所有 `<…>`，包括 RUN_SHA、C、AR、CAP_FILE 等（CODE_SHA 已填为 `103d9d0`）；
  - 计算 sha256；
  - Run A 启动后，把填好的副本和 sha256 放进结果目录。（§1.8）
- [ ] **9. 启动 Run A**（约 27.8 h）：
  - 参数：`--seeds 3 --blocks main,overlap,ksweep`；
  - 用 tmux 运行 `run_A.sh`；
  - 目标开跑时间 Thu 10/08 约 13:00。（§2.3）
- [ ] **10. 前 3 个 cell 后**（开跑约 35 分钟）：运行 `rows.py`，以及 `check_smoke.py --head $SHA --code-sha $CODE_SHA --repo $ROOT …`，逐项核对 §2.5.1。
- [ ] **11. 监控**：
  - 每天 09:00 和 21:00 各一次，每次 block 切换后再一次；
  - 检查进程、进度、`cell_errors.log`、check_smoke、环境。（§2.5.2）
- [ ] **12. Run A 完成**（约 Fri 16:50）：A 的每个子目录都跑一遍 check_smoke，并存档输出。
- [ ] **13. 启动 Run B**（约 13.7 h）：`--seeds 2 --blocks ablation,rhoscan`；AR 和 CAP_FILE 与 A 相同。（§2.4）
- [ ] **14. resume 注意**：
  - 每次非 resume 的启动都会**覆盖** `analysis/icc_kv/overload_latest.txt`；
  - 要 resume 某个 run，先把该文件改写为那个 run 的目录，再用**完全相同的 flags** 加 `--resume`；
  - 只修环境，不改代码；不删除、不清空任何结果文件，包括 `cell_errors.log`。（§2.6）
- [ ] **15. 时间不够时的削减**（必须在看结果之前决定，并记入 PREREG §12）：
  1. rhoscan 减到 1 seed；
  2. ksweep 减到 2 seeds；
  3. ablation 减到 1 seed。

  以下**不得削减**：
  - main block 的全部内容；
  - overlap 的 ov0、ov30、ov60；
  - ksweep 中的任何一个 k；
  - rhoscan 的 normal；
  - `--cell-seconds 600`。（§2.7）
- [ ] **16. 数据冻结 Mon 10/12 12:00**：最终 check_smoke、MANIFEST.sha256、环境记录、tar 包及其 sha256、设为只读，并在自己的第二个位置备份。（§2.8）

## 必须立即停下的情况（停下后报告用户，不要自行绕过）

- 任一 cell 的 `stale_cache_hits` > 0：存在不安全复用，在 tmux 里 Ctrl-C 停止 runner。
- 出现失败的 cell：`cell_errors.log` 非空，或 runner 以 `N cells failed` 退出，且两次自动 resume 后仍失败。
- 容量窗口检查失败：e2e 报 `capacity window: …`，或 capacity_window 不满足第 6 步的条件（含 spread）。停下并报告。同一目标速率的重复点 `forwarded_per_s` 相差 > 5% 是路径不稳定，由用户决定放宽或延期。**禁止**用 `--allow-outside-window` 绕过。
- dirty tree：`experiments/ has uncommitted changes`，或 `git diff 103d9d0 HEAD -- experiments` 非空。**禁止**用 `--allow-dirty` 绕过。
- check_smoke 的硬门禁失败，包括 G1 commit/链路、G2 gap > 0.5%、G3 CPU 或 ρ 超限、G10 机制没有生效。block 未跑完时 G9 报缺失是正常的，不算失败。
- 单个 cell 耗时超过 12 分钟：不用停，但要重算时间线并报告。

## 需要汇报给用户的内容

- **开跑前**：HEAD 与 CODE_SHA；测试结果；两份 `capacity_window.json` 的关键字段（`c_drain_per_s`、`c_ingress_per_s`、`rho_max_in_window`、`c_drain_over_theory`、`c_drain_rho_row_max`）；AR 及其来源；填好的 PREREG 的 sha256。
- **前 3 个 cell 后**：`rows.py` 的输出和 check_smoke 的结论。
- **每次监控**：进度（完成数/计划数）、预计完成时间、失败或无效 cell 的数量与原因、任何削减决定。
- **停机事件**：触发的条件、`runner.log` 末尾、`cell_errors.log` 中的相关记录、你已做的环境修复。
- **冻结后**：tar 包路径与 sha256、各 block 的 check_smoke 结论、cell 计数（计划/完成/失败/补跑/无效）。

## 注意

- 结果目录在 `analysis/icc_kv/` 下，已被 `.gitignore` 忽略，**不要提交**任何结果文件、填好的 PREREG 或运行脚本。
- 不要修改 `docs/icc_formal/` 中的文件，也不要修改 `experiments/` 中的任何文件。
- `docs/icc_formal/` 是临时目录，实验结束后会删除。
