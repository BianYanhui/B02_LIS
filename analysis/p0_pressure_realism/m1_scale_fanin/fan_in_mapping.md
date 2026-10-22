# Fan-in 映射

`N_serving_instances = N_reporters = N_gpu / gpus_per_instance`。下表的 TP 列是这个除数。PP=1。这些行是 `DERIVED`。

| GPU | 每实例 GPU | 实例 = 汇报者 | 现有 instance 字节 |
|---:|---:|---:|---|
| 256 | 1 | 256 | 恰好放下 |
| 256 | 2 | 128 | 放下 |
| 256 | 4 | 64 | 放下 |
| 256 | 8 | 32 | 放下 |
| 1024 | 1 | 1024 | 超限 |
| 1024 | 2 | 512 | 超限 |
| 1024 | 4 | 256 | 恰好放下 |
| 1024 | 8 | 128 | 放下 |
| 4096 | 1 | 4096 | 超限 |
| 4096 | 2 | 2048 | 超限 |
| 4096 | 4 | 1024 | 超限 |
| 4096 | 8 | 512 | 超限 |

已测四卡是 4 GPU、每实例 1 GPU、4 个汇报者、1 个 gateway、1 个 dispatcher。`OBSERVED`。

## 每个控制域看到几个汇报者

分片宽度 64 是分析选择，不是部署测量。

| 模型 | Gateway 数 | 每个 Gateway | Dispatcher 数 | 每个 Dispatcher |
|---|---:|---:|---:|---:|
| A 单域 | 1 | 全部汇报者 | 1 | 全部汇报者 |
| B 多 Gateway、共享 Dispatcher | ceil(汇报者/64) | 最多 64 | 1 | 全部汇报者 |
| C 按 Worker 分片 | ceil(汇报者/64) | 最多 64 | ceil(汇报者/64) | 最多 64 |

举例：4096 GPU、每实例 1 GPU，有 4096 个汇报者。

- Model A：1 个 gateway，1 个 dispatcher，各看 4096。帧格式放不下这些身份。
- Model B：64 个 gateway，每个 64 个汇报者；共享 dispatcher 仍看 4096。
- Model C：64 个 dispatcher，每个看 64 个汇报者。事件扇出按 1 计算。若真实系统把同一条事件复制到多个 indexer，这个表会低估流量，而那个扇出现在是 `NOT_MEASURED`。

每实例 8 GPU 时，4096 GPU 只对应 512 个汇报者。Model C 下每个 dispatcher 仍最多 64 个，共 8 个 dispatcher。GPU 数和索引扇入差一个数量级。
