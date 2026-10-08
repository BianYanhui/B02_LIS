# RUN_GUIDE：ICC KV 正式实验操作手册（实验代码 e2502fe）

> 适用代码：`BianYanhui/B02_LIS` main 上的实验代码 `e2502fe`（CODE_SHA）。`e2502fe` = `103d9d0` + spread gate 0.20。cell / e2e 代码路径与 `3991559` 完全相同，验证 run 仍然有代表性。本手册所在的 `docs/icc_formal/` 由 docs-only commit 维护，不改 `experiments/`，所以运行时的 HEAD（summary 的 `commit` 字段，记为 RUN_SHA）可以与 CODE_SHA 不同。网关 sha256 仍为 `efc08e2e…8af45`。
> 入口文件：`docs/icc_formal/START_HERE.md`。`docs/icc_formal/` 是临时目录，实验结束后会删除。
> 所有 CLI flag 都已逐一对照 `e2502fe` 源码（`full_queue.py`、`e2e.py`、`path_capacity.py`、`capacity_window.py`、`test_harness.py`、`4t4/test_policies.py`）核实，**没有编造的 flag**。cell / e2e 路径与 `3991559` 相同。未核实项见文末 §5。
> 占位符：`<RUN_SHA>` 运行时 HEAD 短 sha（等于脚本中的 `$SHA`）；`<CODE_SHA>` 实验代码 sha；`<C>` = `c_drain_per_s`；`<AR>` 到达率；`<CAP_FILE>` capacity_window.json 的绝对路径；`<A_DIR>` / `<B_DIR>` 结果目录。时间均为 UTC+8。

```bash
# 服务器上每个新 shell 先执行（路径来自 full_queue.py / runtime.py 的常量）
export ROOT=/home/byh/B02
export PY=$ROOT/poc/.venv/bin/python
export OUT=$ROOT/analysis/icc_kv
export SHA=$(git -C $ROOT rev-parse --short HEAD)   # RUN_SHA：运行时 HEAD = summary 的 commit 字段
export CODE_SHA=e2502fe             # 实验代码 sha（103d9d0 + spread gate 0.20）
export LINK=10000000
export TOOLS=$HOME/icc_formal       # 放在仓库外：运行脚本、填好的 PREREG 副本、日志
mkdir -p $TOOLS
cd $ROOT
```

**check_smoke.py**：随本手册放在仓库里，路径为 `docs/icc_formal/check_smoke.py`（`experiments/` 下没有 check_smoke，只有用途不同的 `smoke_replica_cap.py`）。**一律在仓库根目录 `$ROOT` 直接运行 `python3 $ROOT/docs/icc_formal/check_smoke.py …`**，不要复制，也不要修改。
- `--head $SHA`：检查 summary 的 `commit` 是否等于运行时 HEAD（前缀匹配）。
- `--code-sha $CODE_SHA --repo $ROOT`：额外检查 `git diff CODE_SHA <commit> -- experiments` 为空，即 docs-only HEAD 跑的确实是 CODE_SHA 的实验代码。

它只依赖 Python 标准库。本次的改动：
- 新增 G4（`stale_cache_hits`）；
- 新增 `--cpu-k64 0.85`；`--cpu` 默认同样是 0.85（与 V6 一致，k=64 不再更宽）；
- G10 的过载判断改为 `nominal_rho_effective > 1`，因此 m=1.5 的 burst（0.975）不会误报。

---

## 第一部分：开跑前（目标 Thu 10/08 11:30 → 13:00）

### 1.1 环境检查（约 10 分钟）

```bash
pgrep -af "experiments\.(icc_kv|4t4)" || echo "no experiment running"
# 4 个 vLLM 端点（e2e 的 formal.check_endpoints() 会请求 127.0.0.1:8000–8003/v1/models）
for p in 8000 8001 8002 8003; do curl -s -o /dev/null -w "$p %{http_code}\n" http://127.0.0.1:$p/v1/models; done
docker ps --format '{{.Names}} {{.Status}}' | grep b02-gateway4t4
nvidia-smi --query-gpu=index,memory.used,memory.total,utilization.gpu --format=csv
free -g; df -h $ROOT/analysis; nproc; uptime
```

通过标准：
- 4 个端点都返回 200；
- `b02-gateway4t4` 处于 Up；
- `$ROOT/analysis` 剩余空间 ≥ 20 GB（结果预计远小于 1 GB，未核实，留足余量）；
- 空闲内存 ≥ 8 GB（未核实）；
- load 远小于 `nproc`。

运行期间**不要**在本机跑其他重负载，否则会污染 `gateway_cpu` 和 `loop_lag_p95_ms`。cpuset 分配：网关 0、调度器 1、请求 2、噪声 3+。

### 1.2 git：干净且 HEAD 正确

```bash
git fetch origin
git rev-parse HEAD origin/main          # 两行必须相同（应为加入 docs/icc_formal/ 的 commit 或更晚），且以 $SHA 开头
git diff --stat $CODE_SHA HEAD -- experiments   # 必须无输出：实验代码与 CODE_SHA 完全相同
ls docs/icc_formal/                      # START_HERE.md PREREG.md RUN_GUIDE.md check_smoke.py
git log -1 --format='%H %ci %s'
git status --porcelain --untracked-files=no -- experiments   # 必须无输出（与 runtime.git_dirty_files 判据相同）
sha256sum experiments/4t4/net/gateway_relay_4t4.py          # e2502fe（网关文件与 3991559 相同）应为 efc08e2e…8af45
```

不得使用 `--allow-dirty` 或 `--allow-outside-window`，两者仅供调试。

### 1.3 ksweep 的 StaticTopK32 已在 bc59945 完成

`wire.py` 里已有 `StaticTopK32`（`TOPK_SWEEP=(4,8,16,32,64)`）。用户已确认加入 StaticTopK32，已在 `bc59945` 完成（只改了 `full_queue.py` 中 ksweep 那一行）。`e2502fe` = `103d9d0` + spread gate 0.20，不再改这一行。核对：

```bash
git diff 3991559 bc59945 -- experiments   # 只应是 full_queue.py 中 ksweep methods 这一行
git diff bc59945 103d9d0 -- experiments   # 只应是 capacity_window.py 与 test_harness.py
git diff 103d9d0 e2502fe -- experiments   # 只应是 capacity_window.py
grep -n '"methods": "StaticTopK4' experiments/icc_kv/full_queue.py
```

代价是多 3 个 cell（约 32 分钟），已计入 §2.1。

### 1.4 测试（约 2 分钟）

```bash
$PY experiments/4t4/test_policies.py --output $TOOLS/test_policies_$SHA.csv   # --output 必填；共 40 项
$PY -m experiments.icc_kv.test_harness                                        # 最后应打印 "all passed"
```

检查 csv 中 40 项全部通过（可用 `column -s, -t` 查看，列名未核实）。只要有一项失败就停止，不开跑。

### 1.5 path_capacity（10 Mbit，跑两次，每次约 25 分钟）

已核实的 flag：
- `--policy`：逗号分隔，默认 FullSync；
- `--rates`；
- `--seconds`：默认 12；
- `--senders`：默认 4；
- `--link-bit`：必填；
- `--noise-coverage-mix`；
- `--allow-dirty`；
- `--out-dir`：输出 `<out-dir>/path_capacity.json`。

重测前可先做 §1.6 开头的可选步骤（约 5 分钟，读旧的 r1/r2/k64diag）。

```bash
# 重复点有意保留，用于可重复性
RATES=12000,12000,13000,14000,14000,15000,24000,28000,32000
POL=FullSync,BoundedFIFO16,BoundedSemantic16,StaticTopK16
for r in r1 r2; do
  $PY -u -m experiments.icc_kv.path_capacity --link-bit $LINK --senders 8 --seconds 30 \
      --policy $POL --rates $RATES --out-dir $OUT/pc10m_${SHA}_$r || break
done
# 诊断用（不参与容量窗口计算）：k=64 的网关 CPU
$PY -u -m experiments.icc_kv.path_capacity --link-bit $LINK --senders 8 --seconds 30 \
    --policy StaticTopK64 --rates 12000,20000,28000 --out-dir $OUT/pc10m_${SHA}_k64diag
```

说明：
- `--senders 8` 对应正式运行的 `--noise-senders 8`。两者是否完全等价未核实，但都是 SplitControl 的发送进程数。
- `RATES` 里的重复（12000、14000 各两次）是有意的，用来看同一目标下 `forwarded_per_s` 的波动。相差 > 5% 是预期的路径波动，不再因此停下。
- 12000–15000 是刚饱和附近的点，供平台集使用。24000、28000、32000 **只用于 C_ingress**，不要求 FullSync 把它们当成平台点。
- FullSync 至少要有 2 个速率点在 rho_row ≤ 1.5 时饱和（`queued_end` ≥ 64，且 `sent_per_s` ≤ 1.5×`forwarded_per_s`）。
- 需要的 ingress 是 ≥ 2.22×C（2.0/0.9），C≈9.5–10k 时约 21–22k/s，且 gap ≤ 1%、CPU < 0.85。高速率点服务于这项，不是平台集。
- `sender_fraction` 系统性偏低（发送节拍按 ms 向上取整），不是加发送端的理由；以 `sent_per_s` 判断实际 ingress。
- 单帧段理论上限约 9615 帧/s（130 B，含 TCP timestamp 与以太网头），见 §1.6。`c_drain_over_theory` 只作参考。
- StaticTopK64 **不放进**窗口文件：`capacity_window` 取所有 policy 中最小的 `c_ingress`，k=64 的 CPU 偏高，可能把窗口拉低。k=64 的有效性按 cell 用 V3/V4/V6 判定。

### 1.6 capacity_window 推导与通过标准

重测（§1.5）之前可选，约 5 分钟：读上一次标定留下的 r1/r2/k64diag（目录名里的 sha 是当时的 `2f8a579`）。这些旧目录不在就不要跑。脚本原样如下：

```bash
for r in r1 r2 k64diag; do
python3 - $OUT/pc10m_2f8a579_$r/path_capacity.json <<'EOF'
import json, sys
d = json.load(open(sys.argv[1]))
print(sys.argv[1], d.get("commit"), d.get("tc_link_bit_s"), d.get("git_dirty_files"))
for x in d["rows"]:
    f = max(x["forwarded_per_s"], 1)
    print(f'{x["policy"]:18s} tgt {x["rate_target"]:>6.0f} sent {x["sent_per_s"]:7.0f} recv {x["received_per_s"]:7.0f} '
          f'fwd {x["forwarded_per_s"]:6.0f} q {x["queued_end"]:>7} rho_row {x["sent_per_s"]/f:4.2f} cpu {x["gateway_cpu"]:.2f} '
          f'gap {x["ledger_ingress_gap"]:.4f} sf {x["sender_fraction"]:.2f} el {x["offer_elapsed_s"]:.1f} '
          f'maxq {x.get("relay_max_queue")} qdrop {x.get("relay_drop_queue_drop")} topk {x.get("relay_drop_global_topk")} '
          f'stale {x.get("relay_drop_stale_cell")} s2m {x.get("stats2_missing")}')
EOF
done
```

CLI（已核实）：`python -m experiments.icc_kv.capacity_window <path_capacity.json> [--out FILE]`，没有其他 flag。不加 `--out` 时，默认写到输入文件旁边的 `capacity_window.json`。

平台集是同时满足以下条件的 FullSync 行：`queued_end` ≥ 64、`gateway_cpu` < 0.85、`ledger_ingress_gap` ≤ 0.01，并且 `sent_per_s` ≤ 1.5×`forwarded_per_s`（rho_row ≤ 1.5）。C 是这些行 `forwarded_per_s` 的中位数。rho_row > 1.5 的高速率行不进平台集，但仍参与该 policy 的 C_ingress。

```bash
for r in r1 r2; do
  $PY -m experiments.icc_kv.capacity_window $OUT/pc10m_${SHA}_$r/path_capacity.json; echo "exit $?"
done
python3 - $OUT/pc10m_${SHA}_r1/capacity_window.json $OUT/pc10m_${SHA}_r2/capacity_window.json <<'PYEOF'
import json, sys
from pathlib import Path
missing = [p for p in sys.argv[1:] if not Path(p).is_file()]
if missing:
    print("capacity_window.json 未写出（spread > 0.20、饱和行少于 2 或 dirty 失败时不写文件）：")
    for p in missing:
        print(" ", p)
    raise SystemExit(1)
w = [json.load(open(p)) for p in sys.argv[1:]]
for x in w:
    shown = {k: x[k] for k in ("link_bit_s","c_drain_per_s","c_drain_rows","c_drain_spread","c_drain_rho_row_max","c_ingress_min_per_s",
                             "c_link_theory_per_s","c_drain_over_theory","rho_max_in_window")}
    if x.get("c_drain_spread_warning"):
        shown["c_drain_spread_warning"] = True
    print(shown, x["c_ingress_per_s"])
c1, c2 = w[0]["c_drain_per_s"], w[1]["c_drain_per_s"]
print("C r1/r2 diff", abs(c1 - c2) / c1)
x = w[0]
print("PASS ultrahigh (rho_max>=2.22, i.e. 2.0/0.9):", x["rho_max_in_window"] >= 2.0 / 0.9)
print("PASS C_ingress >= 2.22*C:", x["c_ingress_min_per_s"] >= (2.0 / 0.9) * x["c_drain_per_s"])
PYEOF
```

通过标准（必须全部满足）：

| 项 | 标准 | 原因 |
|---|---|---|
| 退出码 | 0。`rho_max` ≤ 1.2 时非零，但仍写出文件。spread > 0.20、饱和行少于 2、dirty 时非零且**不写** json。0.05 < spread ≤ 0.20 时打印 WARNING，仍写出 json，退出码按上面的规则 | capacity_window.main |
| `link_bit_s` | 10000000 | e2e 的 window_problems 会检查 |
| `c_drain_rows`、`c_drain_spread` | ≥ 2，≤ 0.20。0.05 < spread ≤ 0.20 时打印 WARNING、json 含 `c_drain_spread_warning: true`，并仍写出 json；把警告原文和两遍 spread 记入 PREREG | 平台集：FullSync，且 queued_end≥64、cpu<0.85、gap≤0.01、sent_per_s≤1.5×forwarded_per_s |
| `c_drain_over_theory` | 只作参考 | 单帧段上限约 9615/s（130 B，含 TCP timestamp 与以太网头）。代码按 104 B 计算该字段，不是干净的效率 |
| r1 与 r2 的 C 差 | ≤ 10%（中位数） | 可重复性（替代 Word 计划的 3×120 s） |
| `rho_max_in_window` | **≥ 2.0/0.9 ≈ 2.22** | ultrahigh 峰值 2.0C ≤ 0.9×C_ingress |
| `c_ingress_min_per_s` | **≥ 2.22×C**（C≈9.5–10k 时约 21–22k） | 与上一行同一条件；burst 的 1.5×C 在 rho_max ≥ 2.22 时自动满足 |

固定 `CAP_FILE=$OUT/pc10m_${SHA}_r1/capacity_window.json`（使用绝对路径），记录 `sha256sum $CAP_FILE`。**正式运行期间不得重新生成该文件**，原因有二：
- full_queue 把 `capacity_file` 路径和解析出的 `capacity` 写进 resume 身份；
- e2e 会做 ±10% 检查。

不通过时：
- spread > 0.20，或饱和行少于 2，或 dirty：程序不写 json。停下并报告。spread 在 0.05 与 0.20 之间会打印 WARNING 并仍写出 json：把警告原文和两遍的 `c_drain_spread` 记入 PREREG。同一目标速率的重复点 `forwarded_per_s` 相差 > 5% 是预期的路径波动，不再因此停下。只有某一遍 spread > 0.20，或两遍 C（中位数）相差 > 10%，才停下。**绝不**使用 `--allow-outside-window`。
- `sender_fraction` 系统性偏低，不是失败原因，也不要为此加发送端；看 `sent_per_s`。
- 顶端出现 `gateway_cpu` ≥ 0.85 或 `ledger_ingress_gap` > 0.01：这是真实的 ingress 上限。停下并报告，记为偏差。**不要**用 `--allow-outside-window`。

### 1.7 确定到达率 AR（二选一，写入 PREREG）

**方案 A：沿用验证 run 的 AR**（推荐，与验证保持一致）。

```bash
python3 -c 'import json,sys; r=json.load(open(sys.argv[1]))["rows"]; print(sorted({x["arrival_rate"] for x in r}))' \
  $OUT/topk_val_3991559/e2e_summary.json        # 验证 run 的路径未核实，summary 可能在子目录里
```

**方案 B：用 `--calibrate-only` 校准一次**（e2e flag 已核实；calibrate-only 时可以不给 `--capacity`）。

```bash
$PY -u -m experiments.icc_kv.e2e --calibrate-only --gpu-rho <GPU_RHO> --concurrency 4 \
    --link-bit $LINK --calibration-out $OUT/calibration_${SHA}.json
python3 -c 'import json,sys;d=json.load(open(sys.argv[1]));print(d["arrival_rate"],d["service_s"],d["gpu_rho"])' $OUT/calibration_${SHA}.json
```

`<GPU_RHO>` 必须与验证 run 相同（值未核实）。

拿到 AR 后，A、B 两轮**都显式传 `--arrival-rate <AR>`**，不要用 `--gpu-rho`：full_queue 会在每个 out-dir 重新校准，导致 A 和 B 的 AR 不一致。

每个 cell 的请求数 = max(11, ceil(600×AR))。例如 AR=0.24 时为 144 个请求，其中 134 个计分。

### 1.8 填写 PREREG

1. `cp $ROOT/docs/icc_formal/PREREG.md $TOOLS/PREREG.md`，只编辑副本，不修改仓库里的模板。
2. 填入 `<RUN_SHA>`（= `$SHA`）、`<CAP_FILE>`、`<CAP_SHA>`、`<C>`、`<CIN>`、`<RHOMAX>`、`<AR>`、两遍 `c_drain_spread`、WARNING 原文、预注册时间，以及目录：`A_DIR=$OUT/formal_A_${SHA}`、`B_DIR=$OUT/formal_B_${SHA}`。CODE_SHA 已填为 `e2502fe`，StaticTopK32 已纳入 ksweep，这两项不要改。
3. 生成校验和：
   ```bash
   sha256sum $TOOLS/PREREG.md | tee $TOOLS/PREREG.sha256
   ```
4. Run A 启动、目录建好后，立即执行 `cp $TOOLS/PREREG.md $TOOLS/PREREG.sha256 <A_DIR>/`。

### 1.9 tmux / nohup

推荐 tmux：ssh 断开后进程继续运行，也能回看输出。

```bash
tmux new -s icc_A        # 在里面启动运行脚本；Ctrl-b d 分离；tmux attach -t icc_A 重新接入
```

nohup 备选：`nohup bash $TOOLS/run_A.sh > $TOOLS/run_A.out 2>&1 &`。脚本、日志、PREREG 草稿都放在仓库外的 `$TOOLS`。

---

## 第二部分：正式运行

### 2.1 cell 数与时长

按每个 cell 约 10.5 分钟估算（600 s 加启动开销）。每个 stage 另有一次网关重建，耗时未核实，估计几分钟。

| Run | block（运行顺序） | cells | 时长 |
|---|---|---|---|
| A（`--seeds 3`） | main | 3 场景 × 8 方法 × 3 = 72 | 12.6 h |
| | overlap | 4 ov × 5 方法 × 3 = 60 | 10.5 h |
| | ksweep | 9 × 3 = 27 | 4.7 h |
| | **A 合计** | 159 | **≈ 27.8 h** |
| B（`--seeds 2`） | ablation | 2 × 7 × 2 = 28 | 4.9 h |
| | rhoscan | 5 × 5 × 2 = 50 | 8.75 h |
| | **B 合计** | 78 | **≈ 13.7 h** |

**block 顺序**设为 `main,overlap,ksweep` 和 `ablation,rhoscan`，目的是让最后一个 block 可以按 seed 截断（§2.7）：
- e2e 的外层循环是 seed，截断后剩下的都是完整的 seed；
- `--blocks` 不属于 resume 身份，调整顺序不影响 resume。

### 2.2 时间线（UTC+8）

| 时间 | 事件 |
|---|---|
| Thu 10/08 11:30–13:00 | 第一部分 |
| **Thu ~13:00** | 启动 Run A |
| Thu ~13:35 | 检查前 3 个 cell（§2.5.1） |
| Fri 10/09 ~01:40 | main 完成，进入 overlap |
| Fri ~09:00 | 检查点 1：A 的进度与投影，**决定是否削减** |
| Fri ~12:10 | overlap 完成，进入 ksweep |
| Fri ~16:50 | A 完成，对 A 的全部子目录跑 check_smoke |
| **Fri ~17:00** | 启动 Run B（启动前决定 B 是否削减） |
| Fri ~22:00 | ablation 完成，进入 rhoscan |
| Sat 10/10 ~02:20 | rhoscan seed 0 完成（25 cells） |
| Sat ~06:40 | B 完成 |
| Sat–Sun | 缓冲：resume 补跑、按 PREREG §6 整 block 重跑、出初版图表 |
| **Mon 10/12 12:00** | 数据冻结（§2.8） |
| Thu 10/15 晚 | 投稿（截止时区未核实，以会议页面为准） |

### 2.3 Run A

```bash
cat > $TOOLS/run_A.sh <<'EOS'
#!/usr/bin/env bash
# 首次运行；失败后最多自动 resume 2 次。不删除任何文件。
set -u
ROOT=/home/byh/B02; PY=$ROOT/poc/.venv/bin/python; OUT=$ROOT/analysis/icc_kv
SHA=<RUN_SHA>; A_DIR=$OUT/formal_A_$SHA
FLAGS=(--confirm --link-bit 10000000 --capacity-file <CAP_FILE> --arrival-rate <AR>
       --cell-seconds 600 --warmup-requests 10 --burst-mult 1.5 --noise-senders 8
       --seeds 3 --blocks main,overlap,ksweep)
cd $ROOT
$PY -u -m experiments.icc_kv.full_queue "${FLAGS[@]}" --out-dir "$A_DIR"; rc=$?
for i in 1 2; do
  [ $rc -eq 0 ] && break
  [ -f "$A_DIR/run_config.json" ] || break          # 身份没写出来 = 参数错误，不重试
  echo "$(date '+%F %T') auto-resume $i after rc=$rc" | tee -a "$A_DIR/auto_resume.log"
  sleep 120
  echo "$A_DIR" > "$OUT/overload_latest.txt"        # --resume 只读这个指针文件
  $PY -u -m experiments.icc_kv.full_queue "${FLAGS[@]}" --resume; rc=$?
done
echo "$(date '+%F %T') final rc=$rc" | tee -a "$A_DIR/auto_resume.log"
exit $rc
EOS
chmod +x $TOOLS/run_A.sh
tmux new -s icc_A "$TOOLS/run_A.sh 2>&1 | tee -a $TOOLS/run_A.out"
```

各 flag 的说明（均已核实）：
- `--capacity-file`：只给这个参数时，`capacity` 自动取文件里的 `c_drain_per_s`。
- `--cell-seconds 600`，`--requests` 保持默认 0：full_queue 会向 e2e 传 `--cell-seconds 600 --requests 1`。
- `--burst-mult 1.5` 必须显式传，默认值是 5.0。
- `--noise-senders 8`：默认值就是 8，显式写出是为了记录清楚。
- `--invalidate-every 8`、`--noise-workers 4` 使用默认值。
- `--concurrency 4` 和 `--kv-cache-tokens 104544` 由 full_queue 固定传给 e2e，**full_queue 本身没有这两个 flag**。

输出目录：
- `<A_DIR>/{main_base, overlap_ov0, overlap_ov10, overlap_ov30, overlap_ov60, ksweep_ov30}/`，每个子目录包含：
  - `e2e_summary.json`；
  - `requests_<scenario>_<method>_seed<s>.csv`；
  - 出错时还有 `cell_errors.log`。
- `<A_DIR>` 根目录下有 `run_config.json`（身份）和 `runner.log`。

### 2.4 Run B（A 完成并通过 check_smoke 后）

```bash
sed -e 's/formal_A_/formal_B_/; s/A_DIR/B_DIR/g' \
    -e 's/--seeds 3 --blocks main,overlap,ksweep/--seeds 2 --blocks ablation,rhoscan/' \
    $TOOLS/run_A.sh > $TOOLS/run_B.sh
chmod +x $TOOLS/run_B.sh; grep -n "FLAGS\|B_DIR=\|seeds" $TOOLS/run_B.sh     # 人工核对
tmux new -s icc_B "$TOOLS/run_B.sh 2>&1 | tee -a $TOOLS/run_B.out"
cp $TOOLS/PREREG.md $TOOLS/PREREG.sha256 $OUT/formal_B_$SHA/                  # 在另一个窗口执行
```

`<AR>` 和 `<CAP_FILE>` 必须与 A **完全相同**。B 的输出子目录为 `ablation_base` 和 `rhoscan_base`。

**关于 `overload_latest.txt`**：每次以非 resume 方式启动都会覆盖 `$OUT/overload_latest.txt`。B 启动之后如果需要 resume A，必须先执行：

```bash
echo $OUT/formal_A_$SHA > $OUT/overload_latest.txt
```

否则 `--resume` 会读到 B 的 `run_config.json`，因身份不同而拒绝运行。

可选的 A→B 串联（适合 A 预计在夜间结束的情况）：`$TOOLS/run_A.sh && $TOOLS/run_B.sh`。B 只会在 A 退出码为 0 时启动，但 A 的 check_smoke 需要事后补做。

### 2.5 监控清单

先准备一个查看 rows 的辅助脚本：

```bash
cat > $TOOLS/rows.py <<'PYEOF'
import json, sys
d = json.load(open(sys.argv[1]))
print("commit", d.get("commit"), "tc", d.get("tc_link_bit_s"), "fixed", d.get("fixed_link_bit_s"), "dirty", d.get("git_dirty_files"))
for r in d["rows"]:
    win = r.get("capacity_window")
    print(r["seed"], r["scenario"], r["method"], "req", r["requests"], "t", round(r["elapsed_s"]),
          "rho", round(r["measured_rho"], 2), "/", round(r["nominal_rho_effective"], 3),
          "cpu", round(r["gateway_cpu"], 2), "gap", round(r["ledger_ingress_gap"], 4), round(r["ledger_balance_gap"], 4),
          "FN", round(r["loose_false_negative_rate"], 3), "fgU", r["foreground_undelivered"], "/", r["foreground_up_sent"],
          "stale", r["stale_cache_hits"], "s2m", r.get("stats2_missing"), "topk", r.get("relay_drop_global_topk"),
          "qdrop", r.get("relay_drop_queue_drop"), "win", win.get("problems") if isinstance(win, dict) else None)
PYEOF
```

各 block 的 check_smoke 命令。`--methods` 用于 G9 完整性检查，block 未跑完时 G9 报缺失是正常的。

```bash
CS="python3 $ROOT/docs/icc_formal/check_smoke.py --head $SHA --code-sha $CODE_SHA --repo $ROOT --link-bit $LINK"
A=$OUT/formal_A_$SHA; B=$OUT/formal_B_$SHA
$CS $A/main_base/e2e_summary.json --methods FullSync,RateFIFO,BoundedFIFO16,BoundedPrio16,BoundedSemantic16,StaticSemantic,StaticTopK16,Ideal
for ov in ov0 ov10 ov30 ov60; do
  $CS $A/overlap_$ov/e2e_summary.json --methods StaticTopK16,BoundedSemantic16,BoundedPrio16,StaticSemantic,Ideal
done
$CS $A/ksweep_ov30/e2e_summary.json --methods StaticTopK4,StaticTopK8,StaticTopK16,StaticTopK32,StaticTopK64,BoundedSemantic4,BoundedSemantic16,BoundedSemantic64,Ideal
$CS $B/ablation_base/e2e_summary.json --methods StaticTopK16,StaticTopK16NoMerge,StaticTopK16NoDedup,StaticTopK16NoPriority,BoundedSemantic16,Adaptive,AdaptiveNoPriority
$CS $B/rhoscan_base/e2e_summary.json --methods FullSync,BoundedFIFO16,BoundedSemantic16,StaticTopK16,Ideal
```

默认门槛与 PREREG V3/V6 一致：`--gap 0.005`、`--cpu 0.85`、`--cpu-k64 0.85`。

#### 2.5.1 前 3 个 cell（Thu ~13:35；B 启动约 35 分钟后再做一次）

```bash
tail -n 5 $A/runner.log
python3 $TOOLS/rows.py $A/main_base/e2e_summary.json
ls -l --time-style=+%H:%M:%S $A/main_base/requests_*.csv     # 相邻文件的时间间隔 = 单个 cell 耗时
$CS $A/main_base/e2e_summary.json
```

逐项核对：
- `commit` 以 `$SHA`（运行时 HEAD）开头；`tc` 和 `fixed` 都是 10000000；`dirty` 为 []。
- `req` = ceil(600×`<AR>`)；`t` ≈ 600–640 s。若单个 cell 超过 12 分钟，按实际耗时重算 §2.2 的时间线。
- `rho / nominal` 在 ±10% 以内。seed 0 第一个场景是 xhigh，nominal = 1.5。
- `cpu` ≤ 0.85；两个 `gap` 都 ≤ 0.005；`stale` = 0；`s2m` = 0；`win` 为 []。
- 机制确实生效：StaticTopK16 的 `topk` > 0；Bounded* 的 `qdrop` > 0；FullSync 和 Ideal 的两项都为 0。
- 出现任何 HARD GATE 失败，**立即停下排查**。

#### 2.5.2 每天两次（09:00、21:00）以及每次 block 切换后

1. **进程**：`pgrep -af full_queue`；`tail -n 3 <DIR>/runner.log`。最后一行应为 `start <block>-<workload>` 或 `done …: exit 0`。
2. **进度**：
   ```bash
   python3 -c 'import json,sys;print(len(json.load(open(sys.argv[1]))["rows"]))' <DIR>/<sub>/e2e_summary.json
   ```
   与计划 cell 数比较，重新推算完成时间。
3. **错误**：`wc -l <DIR>/*/cell_errors.log 2>/dev/null`。日志非空时用下面的命令查看：
   ```bash
   python3 -c 'import json,sys;[print(d["seed"],d["scenario"],d["method"],d["error"]) for d in map(json.loads,open(sys.argv[1]))]' <DIR>/<sub>/cell_errors.log
   ```
4. **check_smoke**：对正在跑的 block 和刚完成的 block 各运行一次，并存档：`… | tee <DIR>/<sub>/check_smoke_$(date +%m%d_%H%M).txt`。
5. **环境**：4 个端点返回 200；`docker ps` 中网关没有异常重启；`nvidia-smi` 正常；`df -h`；`uptime` 显示无外部负载。
6. **`stale_cache_hits` > 0**：**停机**（PREREG H7）。在 tmux 中按 Ctrl-C，然后排查。

### 2.6 失败处理与 `--resume`

以下行为均已核实：
- **单个 cell 失败**：写入 `cell_errors.log`，并烧掉该 cell id。该 stage 会跑完其余 cell，然后以 `N cells failed` 非零退出。**整个 runner 随之终止**，后续 block 不会运行。
- **`--resume` 读哪个目录**：读取 `$OUT/overload_latest.txt` 所指的目录，忽略 `--out-dir`。
- **身份必须一致**：身份必须与 `run_config.json` 完全一致，包括 `link_bit`、`capacity`、`capacity_file`、`arrival_rate`、`gpu_rho`、`cell_seconds`、`requests`、`warmup_requests`、`burst_mult`、`invalidate_every`、`noise_workers`、`noise_senders`、`seeds_override`。
- **只补缺失的 cell**：e2e 会跳过 rows 中已有的 (seed, scenario, method)。
- **不加 `--resume` 的情况**：如果目录里已有 `run_config.json`，runner 拒绝启动。

**手动 resume 步骤**（run_A.sh 的 2 次自动 resume 用完之后）：
1. 用 `tail <DIR>/runner.log` 和 `cell_errors.log` 找出原因。**只修环境**（vLLM、docker、磁盘），**不改代码**。改了代码会因 dirty tree 被拒；提交新 commit 则会让 commit 与已完成的 cell 不一致。
2. 运行 `cat $OUT/overload_latest.txt`。如果不是目标目录，执行 `echo <DIR> > $OUT/overload_latest.txt`。这只是改写指针文件，不删除任何东西。
3. 用**完全相同的 FLAGS** 加 `--resume` 重新运行：即 run_X.sh 里的 FLAGS 加上 `--resume`、去掉 `--out-dir`。可以用 `--blocks` 只列出剩余的 block（例如 `--blocks overlap,ksweep`）；已完成的 cell 无论如何都会被跳过。
4. 在 PREREG §12 追加一条记录：时间、原因、补跑的 cell 数。
5. resume 之后 `cell_errors.log` 里的旧记录仍然在，check_smoke 的 G1 会继续报告。此时应确认日志中的每个 (seed, scenario, method) 现在都已出现在 rows 中，并在 §12 注明。**不要删除或清空 `cell_errors.log`。**

**kill -9 或机器重启**：被打断的 cell 既不在 rows 中，也不在日志中，resume 时会自动重跑。重启后要先恢复 vLLM 和 docker；tc 由 `prepare_fixed_gateway` 在每个 stage 开始时重新设置。

已经完成但无效（违反 V1–V10）的 cell **不重跑**，按 PREREG §6 处理。

### 2.7 时间不足时的削减顺序

在检查点 Fri 09:00 或 Fri 17:00 决定，**必须在看结果之前**，并记录到 PREREG §12。目标是 B 的投影结束时间不晚于 Sun 10/11 12:00。

| 顺序 | 削减 | 节省 | 操作方法（无需改代码） |
|---|---|---|---|
| 1 | rhoscan 减为 1 seed | −4.4 h | B 启动前决定：把 B 拆成 B1 `--blocks ablation --seeds 2`（目录 `formal_B1_$SHA`）和 B2 `--blocks rhoscan`（不传 `--seeds`，使用默认值 1；目录 `formal_B2_$SHA`）。B 运行中决定：等 `rhoscan_base` 的 rows 达到 25（seed 0 完整）后按 Ctrl-C |
| 2 | ksweep 减为 2 seeds | −1.6 h | ksweep 是 A 的最后一个 block。rows 达到 18 后按 Ctrl-C，之后**不再** resume A |
| 3 | ablation 减为 1 seed | −2.5 h | B 启动前决定，B1 改用 `--seeds 1`。H6 的 n 从 4 变为 2，论文中要写明 |
| 4 | overlap 去掉 ov10，或 main 去掉 RateFIFO/StaticSemantic | −2.6 h / −3.2 h | **需要改 `full_queue.py` 的 BLOCKS**，并使用新 commit、新目录，属于偏差。只有在落后超过 8 h 且该 block 尚未开始时才考虑。main 是第一个 block，因此 main 的削减实际做不到 |

**不得削减**：
- main block 的全部内容：StaticTopK16 vs Bounded*16 / FullSync / Ideal，3 seeds；
- overlap 的 ov0、ov30、ov60；
- ksweep 至少保留 3 个 k 值（现有 5 个 k 值，都不要删）；
- rhoscan 的 normal（seed 0 必须完整）；
- `--cell-seconds 600`：不得中途缩短，它属于身份，缩短后 cell 之间不可比。

### 2.8 跑完之后：数据冻结（Mon 10/12 12:00 之前）

```bash
pgrep -af full_queue && echo "STILL RUNNING - do not freeze"
cd $OUT
FZ=freeze_${SHA}_$(date +%Y%m%d)
mkdir -p $FZ
# 1. 最终 check_smoke 存档：命令同 §2.5，每个都 tee 到 $FZ/check_smoke_<block>_<workload>.txt
# 2. 环境记录
git -C $ROOT rev-parse HEAD > $FZ/HEAD.txt
$PY -m pip freeze > $FZ/pip_freeze.txt
nvidia-smi -q > $FZ/nvidia_smi.txt
docker inspect b02-gateway4t4 --format '{{.Image}} {{.Created}}' > $FZ/gateway_image.txt
# 3. 校验和清单（结果、容量、校准、PREREG）
find formal_A_$SHA formal_B*_$SHA pc10m_${SHA}_* calibration_${SHA}.json -type f 2>/dev/null -print0 \
  | sort -z | xargs -0 sha256sum > $FZ/MANIFEST.sha256
cp $TOOLS/PREREG.md $TOOLS/PREREG.sha256 $TOOLS/run_*.sh $ROOT/docs/icc_formal/check_smoke.py $FZ/
git -C $ROOT diff --stat $CODE_SHA HEAD -- experiments > $FZ/experiments_diff_vs_code_sha.txt   # 应为空文件
# 4. 打包并设为只读（chmod 可逆，不删除任何文件）
tar -czf $FZ.tar.gz $FZ formal_A_$SHA formal_B*_$SHA pc10m_${SHA}_* $(ls calibration_${SHA}.json 2>/dev/null)
sha256sum $FZ.tar.gz | tee $FZ.tar.gz.sha256
chmod -R a-w formal_A_$SHA formal_B*_$SHA pc10m_${SHA}_*
```

把 tar 包复制一份到第二个**自己的**存储位置（外接盘或自己的另一台主机），不要公开分享。冻结后的分析只读取这些文件。

### 2.9 表与图：数据来源

所有数值都来自各子目录 `e2e_summary.json` 的 `rows`。配对和统计按 PREREG §7：seed 配对差，bootstrap B=10000，种子 20261008。

| 产出 | 数据 | 字段 |
|---|---|---|
| **表 1 主结果** | `main_base` | 每个 (scenario, method) 的 `loose_false_negative_rate`、`foreground_undelivered`/`foreground_up_sent`、`foreground_censored_lag_p95_s`、`invalidate_censored_lag_p95_s`、`loose_false_positive_rate`、`prefill_tokens_mean`、`ttft_mean_ms`；另列相对 BoundedSemantic16、BoundedPrio16、BoundedFIFO16 的 Δ 与 CI |
| **图 k 敏感性** | `ksweep_ov30` | x 为 k（方法名中的数字），y 为 `loose_false_negative_rate`；两条线：StaticTopK_k 与 BoundedSemantic_k；Ideal 画水平线；副图为 `gateway_cpu` |
| **图 overlap** | `overlap_ov*` | x 为 `noise_overlap`（行字段：噪声中覆盖度 ≥ 最小有用覆盖度的比例）或 ov 标签；y 为 FN；另附 StaticTopK16 按 `fn_rate_by_coverage` 分组的柱状图 |
| **图 ρ 扫描** | `rhoscan_base` | x 为 `nominal_rho_effective`；y 为 FN 和 `ttft_mean_ms`；共 5 种方法 |
| **表 消融** | `ablation_base` | 相对 StaticTopK16 的 Δ：FN、`invalidate_censored_lag_p95_s`、`relay_drop_superseded`、`congested_fraction`、`prefill_tokens_mean` |
| **图 burst 逐请求延迟** | `main_base/requests_burst_<method>_seed<s>.csv` | x 为 `decision_s`，y 为 `delivery_lag_s`（≥0），按 `loose_false_negative` 着色，只用 `warmup`=0 的行。注意：`decision_s` 的起点（请求阶段开始）与噪声 burst 周期的起点（split_path 中的 t0）是否对齐**未核实**，不要直接画 25–30 s 的阴影 |
| 附表 容量 | `capacity_window.json`（r1、r2） | `c_drain_per_s`、`c_drain_rows`、`c_drain_spread`、`c_drain_rho_row_max`、`c_ingress_per_s`、`c_ingress_min_per_s`、`c_link_theory_per_s`、`c_drain_over_theory`、`rho_max_in_window` |
| 附表 健康/有效性 | 所有 rows | `gateway_cpu`、`measured_rho`、`nominal_rho_effective`、`ledger_ingress_gap`、`ledger_balance_gap`、`stats2_missing`、`failed_requests`、`stale_cache_hits`、`relay_max_queue`、`relay_drop_global_topk`、`relay_drop_queue_drop`；再加 V1–V10 的判定结果和 cell 计数 |
| 一致性 | `overlap_ov30` vs `ksweep_ov30`；`rhoscan_base` vs `main_base` 中 xhigh/ultrahigh 重叠的 seed | 同一方法的 FN 差应 ≤ 0.03。不一致时只报告，不合并 |

配对 bootstrap 参考实现（只用标准库）：

```python
import json, random, statistics as st
def rows(p): return json.load(open(p))["rows"]
def paired(rs, a, b, metric, scen):
    m = {(r["seed"], r["scenario"], r["method"]): r for r in rs}
    keys = sorted({(r["seed"], r["scenario"]) for r in rs if r["scenario"] in scen})
    return [m[(s, c, a)][metric] - m[(s, c, b)][metric] for s, c in keys if (s, c, a) in m and (s, c, b) in m]
def ci(d, B=10000, seed=20261008):
    rng = random.Random(seed); means = sorted(st.mean(rng.choices(d, k=len(d))) for _ in range(B))
    return st.mean(d), means[int(0.025 * B)], means[int(0.975 * B) - 1], sum(x < 0 for x in d), len(d)
d = paired(rows("main_base/e2e_summary.json"), "StaticTopK16", "BoundedSemantic16", "loose_false_negative_rate", {"xhigh", "ultrahigh"})
print(ci(d))   # (mean Δ, CI 下界, CI 上界, Δ<0 的个数, n)；先按 PREREG V1–V10 过滤掉无效 cell
```

---

## 5. 未核实或不存在的项

- **不存在的 flag，不要使用**：
  - e2e 没有 `--requests-per-run`；
  - path_capacity 没有重复次数 flag，要重复就跑两次，写到不同的 `--out-dir`；
  - capacity_window 只有 `--out` 一个 flag；
  - full_queue 没有 `--concurrency`、`--kv-cache-tokens`、`--scenarios`、`--methods`、`--useful-pool`，这些由 BLOCKS 或固定参数传入。
- **不存在的字段**：`e2502fe`（cell/e2e 与 `3991559` 相同）的 summary 中没有 `cell_errors` 和 `requests_per_run`。分别用 `cell_errors.log` 和 `requests` 字段代替，check_smoke 已兼容。
- **服务器上的取值（未核实）**：
  - `<C>`、`<AR>`、`<GPU_RHO>`；
  - 验证 run 的确切路径和数值；
  - k=64 的 CPU（0.72–0.74，来自你的描述；V6 现已对所有方法使用 0.85）；
  - 每个 cell 10.5 分钟的估计，以及每个 stage 重建网关的耗时；
  - 磁盘和内存需求；
  - `c_drain_over_theory` 只作参考（代码按 104 B/帧计算；单帧段实际上限约 9615/s）。
- **代码层面未核实**：
  - `path_capacity --senders` 与 e2e `--noise-senders` 在发送路径上是否完全等价；
  - `test_policies` 输出 csv 的列名（只确认了 `--output` 必填、共 40 项）；
  - 逐请求 CSV 中 `decision_s` 的起点是否与 burst 周期对齐。
- **其他**：
  - 投稿截止时间的时区：未核实。
  - 结果目录（`analysis/icc_kv/` 已在 `.gitignore` 中）**不要提交**到仓库。
- PREREG 只是文档，代码不会读取它。约束力来自 resume 身份（`seeds_override` 等），以及本手册和 PREREG 的 sha256 存档。
