# ADR-0008 记账落盘：spool + 回放 + 去重

- 状态：已采纳
- 日期：2026-11

## 背景

ADR-0007 把记账改成内存队列 + 批量提交，解决了吞吐和 500 问题，但引入了一个更严重的
属性：**进程被杀就丢账**。

`kill -9`、OOM、宿主机重启，队列里最多 1 万个事件会永久消失。对账务系统来说，
「丢了但没人知道」比「慢」严重得多——慢可以被监控发现，丢账只能等到月底对账才暴露。

## 决策

三层，每层解决一个具体故障：

1. **先落盘，再入队**。请求线程把事件以 NDJSON 追加到 `aigate_metering.spool`，
   然后才放进内存队列。落盘失败不阻断请求，但会退回同步写并记日志。
2. **提交后才确认**。数据库 COMMIT 成功，才从 spool 里删掉对应行（重写文件 + fsync）。
   崩在 COMMIT 之前 → 重启回放；崩在 COMMIT 之后、ack 之前 → 重启回放但会被去重。
3. **回放 + 去重**。启动时读 spool，分批写入；写入前用 `request_traces.request_id`
   过滤已存在的记录。语义是 **at-least-once 投递 + 有效 exactly-once 记账**。

## 两个非显然的细节

**去重必须 ack 全部，而不是只 ack"新写入的"。**
第一版只对 `fresh`（去重后剩下的）做 ack，结果重复事件永远留在 spool 里，
每次重启都回放一遍，文件永远清不空。测试抓到了这个：`test_replay_is_idempotent_*`。
正确做法是：只要这批 COMMIT 成功，无论它是新写入还是判定为重复，都算「已达成持久状态」。

**`fsync` 在 Windows 上对只读句柄会抛 `Bad file descriptor`。**
重写 spool 时用 `r+b` 打开才能 fsync。跨平台代码里这类差异只能在真机上撞出来。

## 持久性边界（写清楚，不含糊）

| 故障 | 结果 |
|---|---|
| 进程崩溃 / `kill -9` / OOM | **不丢**。append 后立即 flush 到 OS，重启回放 |
| 宿主机断电 / 内核 panic | 可能丢最后几毫秒（未 fsync 的 append） |
| 磁盘写满 | append 抛 `OSError`，退回同步写并记 error 日志 |

本地工具接受这个边界。支付系统要求每次 append 都 fsync，代价是每次请求多 1–3ms。

## 代价

- 每请求多一次 `open/write/flush/close`（约 50µs，相对于 10ms 的上游可忽略）
- 每次 ack 重写 spool：批大小 100、pending 通常 < 300 行，约 0.1ms
- spool 超过 16MB 时自动压缩（`_compact_if_huge`）

## 验证（真实进程，不只是单测）

```
写入 spool 3 条未落库事件 → 启动网关 → usage_records 3 → 6
spool 文件被 ack 后删除
再次重启 → 仍是 6 条（没有重复计费）
```

对应单测：`tests/test_metering_spool.py`（5 条，覆盖回放、幂等、部分 ack、损坏行容错）。