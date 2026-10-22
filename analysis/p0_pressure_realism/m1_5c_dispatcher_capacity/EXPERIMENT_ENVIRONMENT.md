# 实验环境

时间起点：2026-10-21 12:15:45 +0000。结束：2026-10-21 12:55:19 +0000。主机 `cloud25`，`Linux 6.8.0-136-generic x86_64`。结束时 9710 仍在听：True。19830 仍在听：False。

| 项 | 值 | 证据 |
|---|---|---|
| Git HEAD | `f3adf24fcf111c2bedcd64f3d28a0a9496d1abed` | MEASURED |
| 工作区 | `M experiments/4t4/net/gateway_relay_4t4.py` | MEASURED |
| CPU | Intel(R) Xeon(R) Silver 4214R CPU @ 2.40GHz，48 逻辑核 | MEASURED |
| 内存 | {'MemTotal': '196511892 kB', 'MemAvailable': '141790408 kB'} | MEASURED |
| 负载 | `0.60 0.56 0.58 1/5648 2034808` | MEASURED，实验开始时 |
| Python | 3.12.9 | packaged by Anaconda, Inc. | (main, Feb  6 2025, 18:56:27) [GCC 11.2.0] | MEASURED |
| 解释器 | `/home/byh/B02/poc/.venv/bin/python` | CONFIGURED |
| ROUTABLE_BELOW | 16 | CODE |
| 9710 在听 | True | MEASURED。本轮不连接、不重启 |
| 9700 在听 | False | MEASURED。本轮不启动它 |
| lo qdisc | `qdisc noqueue 0: root refcnt 2` | MEASURED。没有在 lo 上加 tc |
| tcp_rmem | 4096	131072	6291456 | CONFIGURED，系统默认，本轮不改 |
| tcp_wmem | 4096	16384	4194304 | CONFIGURED |
| rmem_default / max | 212992 / 212992 | CONFIGURED |
| wmem_default / max | 212992 / 212992 | CONFIGURED |

Gateway 文件 sha256 `7ef3c660e81d4fb6a4ea60a17f430d1429df048a3174adad5e3cd3472b2f1fd6`。`runtime.py` `c669f8ebb83208479b200ee660e18689205b9c1328b70a575e6e06db9760bf98`。`wire.py` `bf1467f584a1f1a88d732e564588e52d35f761405a56ee633a2a775507ceb461`。

Dispatcher 是本脚本的 `--role dispatcher` 进程：一个 asyncio 线程，`start_server` 绑在 `127.0.0.1:19830`，控制口 `127.0.0.1:19831`。不调用 `PathRuntime.start`，因此不会把正式路径绑到 CPU 1，也不会连接容器 Gateway。亲和性是进程默认的全部在线 CPU。事件循环仍然只有一个线程，Apply 不能占用超过一个核。没有 uvloop。没有 Apply sleep，没有 CPU 配额。

Gateway 是 `experiments/4t4/net/gateway_relay_4t4.py` 的独立进程，端口 `19840` 起，下游 `127.0.0.1:19830`。参数：`--sndbuf 2304 --max-queue 4096 --tau 30 --util-lambda 16 --gate 2 --adaptive-queue-gate 8 --congestion-hold 0.2 --quiet`。`2304` 是该程序的默认 `SO_SNDBUF`，不是为了制造拥塞而另设的值。`max-queue`、tau、utility、gate 在 FullSync 下不参与准入。`--quiet` 关掉逐帧 JSON，2 秒 tick 仍在。

相对 HEAD 的 Gateway diff 是 M1.5-B 留下的诊断：`drain()` 前后各一次 `perf_counter`，tick 里多了 `pending_lineages`、`inflight`、`drain_wait_s`、`TIOCOUTQ`。等待本身没有改。准入、发送条件和 ACK 没有改。

配置帧是 `FullSync`：`(mode=0, merge=0, priority=0, adaptive=0, dedup=0, global_topk=0)`，`max_inflight=0`，`rate_frames_per_s=0`。

Worker 只用 0–15。这是 `ROUTABLE_BELOW=16` 以下会进入索引的编号。`instance` 仍是一个字节，本轮不改帧宽。16 个 Worker 不是 4096 个 Reporter。

开始时 CPU 占用最高的进程（`ps` 快照，不是全程）：

```
PID %CPU %MEM COMMAND
2034771 20.3  0.0 python
3628457 18.7  0.6 ruby
1708285  5.7  0.3 MainThread
1894455  4.0  0.3 MainThread
1857968  3.5  0.3 MainThread
3630595  3.4  0.2 prometheus
2034752  3.3  0.0 bash
   1901  2.0  0.0 dockerd
3630702  1.9  0.0 postgres_export
1402336  1.5  0.0 python3
3630554  1.4  0.0 gitlab-exporter
```

复现：

```text
cd /home/byh/B02
PYTHONPATH=/home/byh/B02 /home/byh/B02/poc/.venv/bin/python \
  analysis/p0_pressure_realism/m1_5c_dispatcher_capacity/run_m15c.py
```
