# P0-M3.6 下一步

本轮通信成本分析已经做完。停在这里，等下一次 OODA。不要自动开下一组实验。

## 已经能用的材料

`analysis/p0_pressure_realism/m3_6_communication_evidence/` 里有审计、两条成本 CSV、规模 CSV、三份部署和论文建议，以及一张不进入论文的草稿图。重算脚本是 `recompute_m36.py`，它会导入 vLLM 的事件类型来解码已经落盘的 msgpack，不会启动引擎。表格脚本是 `write_m36_tables.py`。

计数链闭合：284529 B → 15552 B → 13120 B。38 条是 `superseded`。Top-K 和 Replica 在这条 Trace 上是 0。

## 不要接着做的事

- 不改论文 PDF 或 Fig. 1。建议只写在 `ICC_MOTIVATION_INTEGRATION_PLAN.md`。
- 不把 94.534% 写进 KVSelect 的贡献。
- 不把 64 B codec 和 104 B 记账混进同一个 10 Mbit/s 排空率。
- 不把 155.7 Mbit/s 的外推说成生产网络已经拥塞。
- 不补 Gateway 逐帧时间。缺的出口峰值保持 `NOT_MEASURED`。
- 不扩展 Native Adapter，不修版本字段，不给 coverage 回退加优先级，不改 K，不恢复 Adaptive。
- 不跑 vLLM、GPU、大规模 Fan-in，不人为加带宽限制或 Apply Delay。
- 不覆盖 C1、M3、M3.5 的原始结果。

## 若下一次只做论文文字

按接入建议的三处改 Introduction 和 Section IV-A。改之前看六页还剩多少行。放不下时，把三个数字收成脚注。Section II 的单 Gateway 模型和 Conclusion 的四实例结果保持不动。

## 若下一次要提高某一条主张的证据等级

主张 E 要有真实控制链路的抓包或计数器。本轮的假设容量不能代替它。主张 F 要先有版本字段和满额 Top-K 下的回退语义，那是协议和准入改动，不属于这次动机包。主张 B 若要从外推变成测量，需要多只独立引擎的 Native 事件，而不是把这一条 Trace 复制 N 份。
