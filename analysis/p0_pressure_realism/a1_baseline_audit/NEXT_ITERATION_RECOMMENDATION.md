# P0-A1 下一轮建议

选择 **Option B**。

真实四卡的 wrapper 更新速率已经测到，而且很低：Original Ideal 中位 2.071 upsert/s，Reuse Ideal 中位 1.071 upsert/s，tombstone 为 0。再做一轮原生插桩（Option A）可以补上“vLLM 内部到底发了多少条”这个缺口，但按 block 粒度粗算也到不了 10 Mbit，所以它不是下一轮最先要做的事。Gateway/Dispatcher 的饱和吞吐（Option C）还没单独测，不过现有 apply 延迟是微秒，合成洪水下的入口速率高于 10 Mbit tc 排空，瓶颈目前写在被压低的链路上。现有数据不够支撑“自然负载已经造成控制面问题”，所以不是 Option D。

## 下一轮最小实验

不要重跑四卡产生速率，不要加噪声，不要为了 ρ≥1 去改 HTB。

1. 先写死一个容量，并说明它从哪来。候选只有已经测过的数：10 Mbit tc 下的排空约 9400 frame/s，或补充实验里写死的 200 frame/s。二者都要标明是“tc 上限下的排空”或“配置的 token bucket”，不能写成硬件瓶颈。
2. 用本轮的集群速率做放大，分两列：只放大测到的 upsert（Original 2.071/s、Reuse 1.071/s，按 N/4）；以及加上 `analyze_controlplane.py` 那个每秒全表刷新假设。对每个容量给出 ρ=1 时的 N。N 必须写成逻辑 worker，不写成 GPU 数。
3. 这一步是算术，不必开 GPU。只有当用户要求用今天的 vLLM 确认完成速率还在 1–3/s 时，才跑一个不超过 180 秒的 Cell：现有正式 workload generator、零噪声、不设目标 ρ、不改 prefix cache。vLLM 没起来就继续标 `LIVE_VALIDATION_DEFERRED`。

不要在这一轮之后自动开始 16/32/64 的正式矩阵。
