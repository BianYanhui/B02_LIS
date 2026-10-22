# 实验环境

时间：2026-10-21。主机 `cloud25`，Linux 6.8.0-136-generic，Intel Xeon Silver 4214R 2.40 GHz，48 逻辑核，内存 187 GiB。没有启动 vLLM，没有 `tc`。

源码提交：`f3adf24fcf111c2bedcd64f3d28a0a9496d1abed`。Gateway 相对这个提交多了 27 行诊断计数，不改变准入、发送条件或 ACK。`drain()` 前后用 `perf_counter` 累加等待；tick 和 `cell_stats` 增加 `pending_lineages`、`inflight`、`drain_wait_s`、`write_queue_bytes`（`TIOCOUTQ`）。Diff 只在 `experiments/4t4/net/gateway_relay_4t4.py`。

## 进程

| 角色 | 实现 | 地址 |
|---|---|---|
| Dispatcher | `experiments.icc_kv.runtime.Dispatcher.on_frame` | `127.0.0.1:19811` |
| Gateway 0 | `experiments/4t4/net/gateway_relay_4t4.py` | `127.0.0.1:19820` |
| Gateway 1 | 同一程序的第二个进程 | `127.0.0.1:19821` |

读、更新索引、写 ACK 的顺序与 `PathRuntime._on_down` 相同：先 `on_frame`，再 `K_ACK`。Apply 阻塞只在这个测试进程里 `asyncio.sleep`，标 `FAULT_INJECTED`。正式监听端口 `9710` 没有被使用。

Worker 使用全局编号 0、1、2、3，副本测试另用 8。都小于 `ROUTABLE_BELOW=16`，因此会进入索引。没有复用同一个 Worker ID。

Digest 是 lineage 字符串的 blake2b-8，与 `run_formal4t4.py` 的 `digest64` 相同。帧里没有 version 字段。`max_inflight` 来自每只 Gateway 的 config 帧，作用在该进程唯一的下游连接上。

时钟是同一台机器的 `time.time()`。Dispatcher 侧 apply 前后的 CPU 时间用 `perf_counter`，只包住 `on_frame`，不含诊断 sleep。

命令：

```text
cd /home/byh/B02
PYTHONPATH=/home/byh/B02 poc/.venv/bin/python \
  analysis/p0_pressure_realism/m1_5b_model_b_benchmark/run_m15b.py
```

输入序列是确定的，没有随机种子。Phase 3 没有跑。
