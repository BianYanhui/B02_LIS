# 容量和下界对照

正式格是 warmup 5 秒、测量 30 秒、3 次重复。Pilot 和 smoke 不进入下界。

S1 下界 4000 updates/s（Gateway 数 [2, 4]），S2 下界 4000 updates/s（Gateway 数 [2, 4]）。两者都达到的下界是 4000 updates/s。这是 `SUSTAINABLE_CAPACITY_LOWER_BOUND`，不是最大吞吐。

判定 `STABLE` 要求：生成速率至少达到目标的 95%，Apply 速率至少达到生成速率的 95%，测量窗内 `generated-applied` 的净增长不超过目标帧数的 2%（且至少超过 100 帧才算增长），排空后计数链相等，校验和一致，FullSync 丢弃为 0。测量短于 20 秒时不做前 10 秒/后 10 秒延迟恶化检验。

| 场景 | Gateway | 目标/s | 判定 | 生成/s | Apply/s | 最慢 1 秒 Apply/s | Dispatcher CPU | 生成到 Apply P95 |
|---|---:|---:|---|---:|---:|---:|---:|---|
| S1 | 2 | 250 | STABLE,STABLE,STABLE | 249.9 | 249.9 | 248.0 | 0.030 | 0.42 ms |
| S1 | 2 | 500 | STABLE,STABLE,STABLE | 499.9 | 499.9 | 498.0 | 0.052 | 0.34 ms |
| S1 | 2 | 1000 | STABLE,STABLE,STABLE | 999.8 | 999.8 | 998.0 | 0.104 | 0.36 ms |
| S1 | 2 | 2000 | STABLE,STABLE,STABLE | 1999.7 | 1999.7 | 1995.4 | 0.201 | 0.35 ms |
| S1 | 2 | 4000 | STABLE,STABLE,STABLE | 3999.3 | 3999.4 | 3989.9 | 0.387 | 0.36 ms |
| S1 | 4 | 250 | STABLE,STABLE,STABLE | 249.8 | 249.9 | 246.0 | 0.030 | 0.47 ms |
| S1 | 4 | 500 | STABLE,STABLE,STABLE | 499.8 | 499.8 | 496.0 | 0.053 | 0.55 ms |
| S1 | 4 | 1000 | STABLE,STABLE,STABLE | 999.6 | 999.6 | 995.9 | 0.102 | 0.57 ms |
| S1 | 4 | 2000 | STABLE,STABLE,STABLE | 1999.3 | 1999.4 | 1993.1 | 0.201 | 0.53 ms |
| S1 | 4 | 4000 | STABLE,STABLE,STABLE | 3999.0 | 3999.0 | 3987.7 | 0.377 | 0.51 ms |
| S2 | 2 | 250 | STABLE,STABLE,STABLE | 249.9 | 249.9 | 248.0 | 0.029 | 0.41 ms |
| S2 | 2 | 500 | STABLE,STABLE,STABLE | 499.8 | 499.9 | 498.0 | 0.056 | 0.40 ms |
| S2 | 2 | 1000 | STABLE,STABLE,STABLE | 999.8 | 999.8 | 998.0 | 0.105 | 0.39 ms |
| S2 | 2 | 2000 | STABLE,STABLE,STABLE | 1999.7 | 1999.7 | 1995.4 | 0.216 | 0.40 ms |
| S2 | 2 | 4000 | STABLE,STABLE,STABLE | 3999.3 | 3999.4 | 3979.3 | 0.418 | 0.38 ms |
| S2 | 4 | 250 | STABLE,STABLE,STABLE | 249.8 | 249.9 | 246.0 | 0.031 | 0.45 ms |
| S2 | 4 | 500 | STABLE,STABLE,STABLE | 499.8 | 499.8 | 496.0 | 0.054 | 0.62 ms |
| S2 | 4 | 1000 | STABLE,STABLE,STABLE | 999.7 | 999.7 | 996.0 | 0.107 | 0.56 ms |
| S2 | 4 | 2000 | STABLE,STABLE,STABLE | 1999.4 | 1999.5 | 1991.9 | 0.204 | 0.44 ms |
| S2 | 4 | 4000 | STABLE,STABLE,STABLE | 3999.0 | 3999.0 | 3984.3 | 0.405 | 0.61 ms |

生成/s、Apply/s、CPU、延迟是三次重复的平均。CPU 是测量窗内 Dispatcher 进程的 `utime+stime`，单位是核。1.0 表示一个核跑满。生成到 Apply 含 Gateway 和 TCP。索引 CPU 见延迟表。

## 与 P0-M1 外推对照

外推来自 `analysis/p0_pressure_realism/m1_scale_fanin/MOTIVATION_REALITY_CHECK.md`。A1 是五个种子的集群速率均分到 4 个引擎再乘汇报者数。A4 是单卡灌满微基准再线性乘汇报者数。两者都不相加。汇报者数等于 GPU 数只在一卡实例的假设下使用。本轮只有 2 或 4 个 Gateway 进程，没有跑这些 Reporter。

负载比是外推速率除以测到的稳定下界。下界不是最大值，所以这个比是利用率的上界：真实容量若更高，利用率更低。外推高于下界、且更高档没有测时，不能判断是否稳定。

| 负载 | 汇报者 | updates/s | 证据 | 容量下界 | 负载/下界 |
|---|---:|---:|---|---|---|
| A1 | 256 | 132.6 | EXTRAPOLATED | 4000 MEASURED 下界 | 0.033 |
| A1 | 1024 | 530.2 | EXTRAPOLATED | 4000 MEASURED 下界 | 0.133 |
| A1 | 4096 | 2121.0 | EXTRAPOLATED | 4000 MEASURED 下界 | 0.530 |
| A4 | 256 | 493.6 | EXTRAPOLATED | 4000 MEASURED 下界 | 0.123 |
| A4 | 1024 | 1974.0 | EXTRAPOLATED | 4000 MEASURED 下界 | 0.493 |
| A4 | 4096 | 7897.0 | EXTRAPOLATED | 4000 MEASURED 下界 | 高于下界，无法判断 |

4000 这一档若稳定，不能写成 Dispatcher 的最大 Apply 速率。
