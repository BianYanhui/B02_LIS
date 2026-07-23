# B02 论文补充实验说明（2026-07-20，第二轮）

> 论文：*Cost-Aware State Interfaces for LLM Request Dispatch*（Minimal State Sketch）
> 实验机：yhs1（4× Tesla T4 15GB，每卡一个 vLLM 0.10.2 实例，Qwen2.5-1.5B-Instruct）
> 代码：`yhs1:/home/byh/B02`（commit `19fe277`）；本轮新增目录 `supplemental_20260720/`
> 第一轮结果见 `20260719-Exp/SUPPLEMENTARY_EXPERIMENTS.md`（α 扫描、守卫消融、并发 8）。

## 0. 本轮补什么（审稿人意见 → 落地实验）

审稿人对实验部分提了三条意见：(1) 与 SGLang Router / Preble 的跨系统 latency 对比；
(2) learned admission estimator；(3) 多 workload trace 验证。经讨论（见对话记录），
本轮落地其中两条的可行核心，第三条（learned admission）属于下一篇论文的研究内容，
不在本轮范围：

| 审稿人意见 | 本轮落地 | 形式 |
|---|---|---|
| #3 多 trace 验证 | **完整实施**：三个 workload family 的结构化 replay | CPU replay（冻结 harness 原样复用） |
| #1 跨系统 latency bar | **减配实施**：锁死 backend（同一 vLLM 集群）+ 锁死 native policy（least-loaded），只换 state view——把 SGLang Router 的 router-side learned prefix state 和 Preble 的 global+local view 实现为同一 harness 内的 dispatcher 变体 | live 4×T4 vLLM |
| #2 learned admission | 不做（3–6 个月研究项目，依赖本轮 #3 的多 trace 数据作为训练集动机） | — |

减配版第 1 条的理由：whole-stack 对打会把 backend 差异混入结论（论文 §4.5 已声明），
且 SGLang Router / Preble 的 state 语义本就绑定各自 runtime；在**同一后端、同一
native policy** 下只换 state view，才是对"谁的 state 更好"这一问题可解释的回答——
这正是论文 §2.1 自己的比较原则。ground truth 统一用 vLLM 返回的物理 cached tokens，
而非任何 dispatcher 推断。

## 1. 条目 3：多 workload trace 的结构化 replay 面板

**(a) 补充论文哪部分数据**：§4.2 / Figure 3 的 AgentTrace K 扫描与 admission 消融
（原文只有 NL2Bash 单 trace）；直接回应"K=16 甜点、coverage-first 规则、Oracle
gap 是否 portable"的质疑。

**(b) 实验设计**：论文冻结的 replay harness（`run_agenttrace_structural_replay_v3.py`
+ `run_agenttrace_admission_oracle_v4.py`，**一字未改**）跑三个 workload family：

| trace | family | 来源 | sessions | 平均步数 |
|---|---|---|---|---|
| NL2Bash | agentic 工具调用（论文原 trace） | `pagarsky/agent-trace` nl2bash_1_7B（Apache-2.0） | 200 | 4.3 |
| MBPP-s200 | agentic 工具调用（第二任务族） | `pagarsky/agent-trace` mbpp_1_7B，稠密抽 200 sessions | 200 | 3.9 |
| ShareGPT-s200 | 真实多轮对话 | `anon8231489123/ShareGPT_Vicuna_unfiltered`（≥3 轮，稠密抽 200 conversations） | 200 | 5.2 |

关键方法学点：每 trace 固定 ~200 sessions 使 512 请求的 replay 窗口内每个 lineage
平均被复访 ~2.5 次（与 NL2Bash 原始密度一致）；8k sessions 的稀疏采样会使
replay 窗口内不出现 lineage 复用（初步运行已证实该退化），故采用稠密子样本。
ShareGPT 经 `convert_sharegpt_to_agenttrace.py` 转成 AgentTrace schema（只保留轮次
结构与文本长度；原始文本不进任何输出，与 NL2Bash 相同的脱敏纪律）。两个 harness
的协议均为 10 次重复、512 请求、128 预热、capacity 128、J 与原文一致。

**结果**（closed_loop；Rinc = (S − S_load)/(S_exact − S_load)）：

| trace | affinity 边际（Exact−Load, tokens） | coverage K=16 Rinc | Oracle K=16 Rinc | coverage K=32 Rinc |
|---|---|---|---|---|
| NL2Bash（原文） | 71,255 | 0.815 | 0.989 | 0.967 |
| MBPP-s200 | 121,130 | 0.697 | 0.982 | 0.942 |
| ShareGPT-s200 | 13,538 | 0.881 | 0.989 | 1.000 |

admission 排名（K=16 Rinc）：三个 trace 上 coverage_first 均显著优于
lfu（0.28/0.28/0.29）、lru（0.41/0.35/0.32）、reuse_distance（0.41/0.43/0.34）、
saved_prefill_first（0.41/0.43/0.35）；Oracle 上界在三个 trace 上均为 0.98–0.99。
完整 K 曲线（K=2…128）见 `analysis/replay_panel_summary.csv`。

**这些结果能说明什么（原论文说明不了的问题）**：

1. **K 响应曲线的形状是 portable 的，K=16 这个点不是。** 三个 family 上曲线都
   平滑单调、K=32 ≥ 0.94、K=64 = 1.00；但 K=16 的取值在 0.70–0.88 之间漂移。
   论文应把结论表述为"**预算 K 是 workload 相关的显式旋钮，K≈32 在三个 family
   上都稳健**"，而非钉死 K=16。这与 20260719 的 live α 扫描（K=16 Rinc 77%→100%
   随集中度漂移）相互印证：live 与 replay 两层证据指向同一结论。
2. **coverage-first 这一 admission 规则是 portable 的。** 在 agentic 第二任务族和
   真实对话上，它都以大比分击败 lru/lfu/reuse_distance/saved_prefill_first——
   §4.2 的消融结论不再依赖单 trace。
3. **Oracle gap（online admission headroom）是稳定现象而非 NL2Bash 特例**：
   三个 trace 上 coverage K=16 → Oracle 的差距为 11–28pp。这为 §5 的 learned
   admission future work 提供了跨 workload 的量化动机。
4. **affinity 接口的边际价值本身随 workload 类型变化近 10×**（chat 13.5K vs
   MBPP 121K tokens）：真实多轮对话的复用绝大部分被 Load-Only 的偶然共置覆盖，
   agentic 工具链才是 affinity state 的主战场——用真实对话数据定量支撑了
   §1/§2.3 的动机陈述。

## 2. 条目 1（减配版）：跨系统 state-view 的 live 对比

**(a) 补充论文哪部分数据**：§2.2 对 Preble / SGLang Router / vLLM APC cache-state
语义的批评（原文无同场对比）；§4.5 末尾"we do not plot SGLang Router or Preble
latency"的洞。本实验把"谁的 state view 更好"变成可回答的问题。

**(b) 实验设计**：同一 4×T4 vLLM 集群、同一 native least-loaded policy、同一份
字节级 trace，只换 state view，五个 arm：

| arm | state 语义 | 对应系统 |
|---|---|---|
| `load_only` | 无 affinity state | vLLM APC（runtime-local） |
| `sglang_approx` | router 侧自学习 prefix→worker 映射；按**假定容量**（4×真实）遗忘；无逐出通知、无版本/寿命验证 | SGLang Router cache-aware |
| `preble_global` | 逐出感知全局 prefix 视图 + locality-first 调度（负载仅破平局，无 net-benefit 守卫） | Preble global scheduler |
| `sketch_coverage_k16` | 有界版本化广告（K=16）+ net-benefit 守卫 | **Minimal State Sketch** |
| `exact` | 全量版本化目录 + 守卫 | Exact Affinity 上界 |

两个 workload 变体制造语义差异：

| 变体 | α | 活跃前缀池 | GPU util | 真实容量 | 假定容量（sglang） | 目的 |
|---|---|---|---|---|---|---|
| normal | 0.55 | 96 | 0.85 | 128/实例 | 512 | 无容量压力，近似状态永不陈旧 |
| eviction | 0.55 | 384 | 0.50（物理 KV ≈ 75 前缀/实例） | 64/实例 | 256 | 物理 KV 成为瓶颈，router 侧近似信念与物理事实可测地背离 |

每请求记录 believed coverage（`selected_coverage_tokens`）与物理 truth
（`vllm_cached_tokens`），陈旧率 = believed − physical > 1024 token 的请求占比。
协议与论文 V5 primary 完全一致：12 次配对重复、192 请求（64 预热）、并发 4、
2048-token 前缀、固定 4 输出 token、cache_salt 隔离、每变体前重启集群。

**结果**（12 次配对重复；ΔTTFT vs Load-Only，负值为更快；cached/believed 为
Exact 归一；数据文件：`crosssystem_normal/`、`crosssystem_eviction/`、
`analysis/crosssystem_summary.csv`）。

normal 变体（无容量压力，96 前缀池）：

| arm | ΔTTFT mean (ms) | ΔTTFT p95 (ms) | cached/Exact | believed/Exact | stale | index B |
|---|---|---|---|---|---|---|
| exact | −261.7 [−293.9, −228.3] | −23.9 [−55.7, +13.1] | 1.000 | 1.000 | 0.0% | 5,307 |
| preble_global | −259.7 [−296.3, −217.6] | −17.0 [−46.2, +8.4] | 0.998 | 1.000 | 0.0% | 5,307 |
| sglang_approx | −261.0 [−294.7, −226.9] | −19.5 [−54.7, +14.8] | 1.000 | 1.000 | 0.0% | 5,307 |
| sketch_k16 | −217.2 [−247.5, −183.1] | +0.9 [−26.2, +34.3] | 0.942 | 0.942 | 0.0% | 4,459 |

eviction 变体（物理 KV 逐出压力，384 前缀池、util 0.30）：

| arm | ΔTTFT mean (ms) | ΔTTFT p95 (ms) | cached/Exact | believed/Exact | stale | index B |
|---|---|---|---|---|---|---|
| exact | −235.1 [−291.5, −176.9] | −99.6 [−149.2, −47.7] | 1.000 | 1.000 | 0.3% | 6,528 |
| preble_global | −199.9 [−244.4, −152.7] | +11.3 [−58.5, +77.0] | 1.000 | 1.000 | 0.3% | 6,528 |
| sglang_approx | −175.9 [−235.5, −111.4] | +48.6 [−25.1, +130.5] | **0.996** | **1.093** | **3.5%** | 9,083 |
| sketch_k16 | −167.9 [−209.0, −119.4] | −53.8 [−97.8, −10.4] | 0.782 | 0.769 | 0.1% | 4,480 |

（注：cell 写出时 preble_global 的 index 列有记账 bug（advertised 未更新），
其全局视图按构造与 Exact 同量，汇总时按 Exact 每 rep 修正；脚本已修复。）

**这些结果能说明什么（原论文说明不了的问题）**：

1. **无容量压力时，三种全量视图（sglang 近似、preble 全局、exact）latency 统计
   不可分**（−259.7 ~ −261.7 ms）。此时 Sketch K=16 以 84% 的 index（4,459 B vs
   5,307 B）拿到 −217.2 ms——normal  regime 下有界状态的代价是 ~45 ms，收益是
   状态更小；这本身就是对 reviewer 的诚实回答：*state view 的差异不在平稳期的
   latency，而在压力期的语义正确性与状态成本*。
2. **逐出压力下，router 侧近似状态（SGLang-style）的信念与物理事实可测地背离**：
   believed/Exact = 1.093 而 cached/Exact = 0.996——**9.7pp 的过度承诺**，陈旧率
   3.5%（版本化视图 ≤0.3%），同时 index 反而更大（9,083 B vs Exact 6,528 B）且
   mean TTFT 差 59 ms、p95 差 148 ms（相对 Exact）。论文 §2.2 对 router-side
   learned prefix state 的语义批评第一次有了 live 定量证据。
3. **无守卫的 locality-first（Preble-style）在压力下 p95 显著恶化**：与 Exact 同样
   准确的视图，mean 差 35 ms、p95 差 111 ms——§4.4 modeled 的"覆盖率必须摊销
   排队代价"在 live 跨 view 对比中复现（与 20260719 S2 的守卫消融互证）。
4. **SGLang-style 的 affinity 使用率最高（27.6% vs Exact 25.3%）却收益最低**——
   "最大化命中率"再次被证否，这次是在真实系统语义的 view 上。
5. **Sketch K=16 在 384 前缀池下覆盖受限（believed 0.77 ≈ cached 0.78，陈旧
   仅 0.1%）**：有界版本化状态始终诚实（believed ≈ physical），但该 regime 需要
   K≈32——与 replay 面板（K=32 三族稳健）和第一轮 α 扫描结论三层互洽。

## 3. 与第一轮（20260719）结果的关系

本轮是在第一轮结论之上的**适用范围扩展**：

| 结论 | 第一轮（live，单/多 Zipf 点） | 第二轮（replay 多 trace + live 跨 view） |
|---|---|---|
| K 甜点随 workload 漂移 | live α∈{0.05,0.55,1.35}：K=16 Rinc 77%→100% | replay 三 family：K=16 Rinc 70%→88%；K=32 三层证据一致稳健 |
| 守卫须 value-aware | K=16 朴素弃权损失 2/3 价值 | preble_global（locality-first 无守卫）在逐出压力下 p95 比 Exact 差 111 ms |
| 版本化+验证的必要性 | 模拟 + owner 微基准 | eviction 下 sglang-style 近似信念过度承诺 9.7pp、陈旧率 3.5%（版本化 ≤0.3%） |
| 动机（agentic > chat） | 间接（Zipf 合成） | 真实 chat vs agentic 的 affinity 边际差 10×（直接） |
| Sketch 的定位 | 有界状态 + 近 Exact 价值 | normal 下以 84% index 拿 83% 的 Exact latency 收益；eviction 下始终 believed≈physical |

## 4. 文件清单与复现

- `analysis/replay_panel_summary.{csv,md}`：三 trace × 6 admission × 7 K 的完整面板
- `analysis/crosssystem_summary.csv`：跨系统 live 对比汇总（生成脚本
  `aggregate_crosssystem_v1.py`）
- `replay_{nl2bash,mbpp_s200,sharegpt_s200}_{structural,admission}/`：replay 原始
  cells/summary/sanity/derived traces（原始文本已脱敏）
- `crosssystem_{normal,eviction}/`：live cells/pairs/summary/sanity/raw/traces
- `sources/`：MBPP 原始 jsonl、ShareGPT 原始 json（672MB）、转换与抽样脚本
  （`convert_sharegpt_to_agenttrace.py`、`subsample_agenttrace.py`）
- `run_live_crosssystem_v1.py`、`run_replay_panel.py`、`restart_t4_vllm_util.sh`、
  `crosssystem_driver.sh`：本轮新增代码；冻结 harness 未做任何修改
- 复现命令见各 `run_metadata.json` 的 `arguments` 字段
