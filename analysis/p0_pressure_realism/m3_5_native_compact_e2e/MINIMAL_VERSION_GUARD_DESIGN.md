# P0-M3.5 Minimal Version Guard

状态：`PROPOSED_ONLY`。没有改正式帧，没有建实验分支，没有打补丁。

版本保护是为了 `STALE_UPDATE_RESURRECTION`。它不能解决 Scenario C：一条更新的低覆盖修正被已经满员的 Top-K 丢掉。那是准入问题，不在这个设计里顺便改掉。

## 规则

每个 `(owner, digest)` 在 Dispatcher 保存最后接受的 `(epoch, version)`。Gateway 可以保存同一份记录，用来少发注定会被拒绝的帧；安全边界在 Dispatcher，因为帧也可能从另一条连接进来。

比较的是 `(epoch, version)`，不是覆盖大小。

- UPSERT：只有新的 `(epoch, version)` 才写入覆盖。相等的重放丢掉。更旧的丢掉。
- Rollback：仍是 UPSERT，版本必须更新。1056 的 version 1 之后，448 的 version 2 留下；迟到的 1040 若仍带着 version 1，则丢掉。
- Tombstone：带着自己的版本。接受之后键从可路由索引删除，但 `(epoch, version)` 保留。更旧的 UPSERT 不能把键再建出来。
- 重复回放：同一版本再次到达，索引不变。
- 断线重连：版本记在进程状态里，不在 TCP 连接里。连接断开不清版本。
- Restart：新进程不能从 version 0 和旧进程的版本放在同一个 epoch 里比。旧的高版本会把新进程的更新全部丢掉，索引停在重启前；若重启把表清掉，旧帧又会复活。Owner 的 epoch 要持久化，重启后 epoch 加一，新的 version 从 1 开始。只重启 Gateway、Dispatcher 仍记住旧 epoch 时，Gateway 丢失的内存不影响正确性，Dispatcher 会拒绝过期帧。

## 字段和字节

正式帧长 64。头 32 字节没有空位。后 32 字节目前是填充。建议占用其中 8 字节：`uint16 epoch` 和 `uint32 version`，其余填充不动。

| 项 | 值 |
|---|---|
| 每条帧新增的线上字节 | 0。占用的是现有填充 |
| 旧实现 | 不读这 8 字节，帧仍能解析。旧 Dispatcher 不会因此获得版本保护 |
| 新实现 | Gateway 和 Dispatcher 都要执行比较，只改其中一端不够 |
| Gateway 状态 | 可选的 `(owner, digest) → (epoch, version)`，用于少转发 |
| Dispatcher 状态 | 必须保留，包括已经 Tombstone 的键 |
| 内存 | 每个曾经广告过的键一条记录，直到该 owner 的 epoch 前进后才能丢掉旧 epoch 的记录。这条 trace 是 101 个键 |
| Algorithm 1 | 覆盖排序和 K=16 的选择式不变 |
| 正式数据可比性 | 已有正式回放没有注入迟到旧帧。顺序单调的版本不会改变那些运行的准入结果。旧日志没有这 8 字节的意义，不能回放出版本保护 |

## 预计改动，未应用

```text
wire frame payload[0:6] = epoch:u16, version:u32
Dispatcher.on_frame:
    previous = versions.get((worker, digest))
    if previous is not None and (epoch, version) <= previous:
        drop
    versions[(worker, digest)] = (epoch, version)
    if kind == K_UP: index[(worker, digest)] = coverage
    if kind == K_TOMB: index.pop((worker, digest), None)
Gateway enqueue:
    the same comparison may drop before admission
    it does not change merge, replica cap, or top-k ranking
```

这个补丁不改变 K，不恢复 Adaptive，也不给缩短覆盖增加新的优先级。Scenario C 仍会在 Top-K 名额满时丢掉 448，除非另作准入修改。那一项本轮不设计成默认补丁。
