# P0-M1 Phase 1：部署模型

数值表在 `deployment_models.csv`。映射表在 `fan_in_mapping.md`。全部目标规模的行是 `DERIVED`。唯一 `OBSERVED` 的拓扑是现有四卡实验。

## 汇报者不是 GPU

KV 事件由 vLLM engine 的 scheduler 发出，一个 engine 一个发布端。张量并行和流水并行增加的是这个 engine 占用的 GPU，不是发布端的个数。

已测实验是四个独立的 `vllm serve`，每个进程一张 T4，TP=1，PP=1。所以：

`4 GPU = 4 serving instance = 4 reporter = 1 gateway = 1 dispatcher`

这个等式只在这台机器的这种启动方式下成立。不能写成 `1 GPU = 1 reporter`。

目标规模用下面的关系，而不是把 GPU 数直接当成连接数：

`N_reporters = N_gpu / gpus_per_instance`

`gpus_per_instance` 取 1、2、4、8。TP>1 的单引擎事件速率没有测过，所以那些格子的 offered load 是 `NOT_MEASURED`。汇报者个数仍然可以算。

## 三个模型

Model A，单域上界。全部汇报者进一个 gateway，再进一个 dispatcher。这是集中式 fan-in 的上界，不是生产默认拓扑。

Model B，多个 gateway，一个共享 dispatcher。分析里每个 gateway 最多收 64 个汇报者。64 是本轮选定的分析粒度，不是测到的部署参数。Dispatcher 仍然看见全部汇报者，所以索引瓶颈不随 gateway 个数下降。

Model C，按 Worker 分片。同样以 64 个汇报者为一片，每片一个 dispatcher。这里假设一条事件只进一个片，`event_fanout = 1`。按前缀责任域复制到多个 indexer 的扇出没有测过，标 `NOT_MEASURED`。当前 gateway 和 dispatcher 没有这个分片实现。本轮只分析，不实现。

## 当前帧格式的身份上限

`experiments/4t4/net/gateway_relay_4t4.py` 的帧头把 `instance` 编成一个字节，取值 0–255，一共 256 个身份。256 个汇报者还能放下。512 个及以上在现有帧里不能保持互异身份。1024 GPU、每实例 1 GPU 的 Model A 已经超过这个格式。这是实现限制，不是网络带宽。

## 和 KVSelect 路径的关系

当前 KVSelect 协调的是 104 字节的 `K_UP` / `K_TOMB`。原生 `BlockStored` 不直接进入这条队列。部署模型把两种流量都映射到同一个汇报者个数上，但服务对象不同。见 Phase 2。
