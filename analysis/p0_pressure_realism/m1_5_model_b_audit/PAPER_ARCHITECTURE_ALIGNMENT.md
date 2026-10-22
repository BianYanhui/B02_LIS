# 论文架构对照

论文文件是用户给出的 `/home/byh/.cursor/projects/home-byh-B02/uploads/0d7cd075-1999-420e-8182-c5bf8e468e96/Bare_Demo_of_IEEEtran_cls_for_IEEE_Journals (5).pdf`，769899 字节，2026-10-21。正文里没有 Adaptive、没有 K 从 16 收到 4、没有 utility 门槛。Algorithm 1 的准入是语义归约之后按 coverage 取固定的 K 个 lineage，失效优先，其余 FIFO。这和 `StaticTopK` 在 `adaptive=0` 时的代码一致。代码里残留的 Adaptive 函数不是这篇论文的算法。

本轮不改论文。

## 论文写的是一只 Gateway

Section II-A：一个站点 `s`，实例集合 `I_s`，一只站点网关 `G_s`，一只 Dispatcher。控制面是站点内部的，不是多站点。

Section II-B：`C_s(t) = min{C_net_s, μ_gw_s, μ_disp_s}`，负载是站点的 `L_s(t)`。三个量是这一只站点路径上的传输、网关处理和 Dispatcher 处理。公式可以在数值上写成共享 Dispatcher，但符号只有一个网关处理率，没有 `sum_g F_g`。

Section III 和 Fig. 2：`G_s` 上有一份 `P_s`、一份 `Q_inv_s`、一份 `A_s`。K=16 是这一份待发集合的 lineage 上限。论文没有写“每只 Gateway 各有一个 K=16”。从单 Gateway 的写法里可以推出每只网关自己做准入，但这不是文中的句子。

Algorithm 1 的 `K` 是输入常数，没有压力分支。

Evaluation：四只 vLLM、一只网关路径、64 字节更新、HTB。固定容量试验是 10 Mbit。没有第二只 Gateway。

Conclusion：实验是四只 vLLM。Future work 写的是 multi-site coordination 和 cross-gateway scheduling。跨 Gateway 调度被放在未来工作，不在当前架构里。

## 若要把 Model B 写成当前系统

需要改的是架构句子，不是本轮的代码：

- `G_s` 从一只改成一组，每只有自己的 `P_g` 和 K=16。
- `C_s` 要拆开 `C_g`、可选的 `C_shared_net`、以及 `C_D`。现在的 min 把三者合成一个站点容量，看不出“每只出口都闲、Dispatcher 忙”。
- K=16 要写明是每只 Gateway 的待发 lineage 上限，不是 Dispatcher 的状态容量，也不是发送速率。
- Conclusion 里的 cross-gateway 若仍是未来工作，正文就不能把多 Gateway 汇聚说成已经设计和评估过的结构。
- 四卡、单 HTB 的实验不能作为 Model B 的测量。

公式 `L_D = sum F_g` 与 `C_s = min(...)` 不矛盾，前提是有人把 `μ_disp_s` 定义成共享 apply，并把每只 Gateway 的转发写成 `F_g`。论文现在没有这组符号。
