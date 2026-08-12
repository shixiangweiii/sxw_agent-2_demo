# RuntimeWorker._maintenance 维护任务详解

## 1. 方法定位

`RuntimeWorker._maintenance(now)` 位于 `agent/runtime/worker/dispatcher.py`，是 Worker 主调度循环每次尝试 claim 之前执行的 durable housekeeping。

```python
while not self._stop.is_set():
    now = self.clock.now_ms()
    await self._maintenance(now)
    while len(self._tasks) < self.concurrency:
        claim = await self.store.claim_next(
            worker_id=self.worker_id,
            lease_ms=self.lease_ms,
            now_ms=self.clock.now_ms(),
            release_map=self.release_map,
        )
        # ...
```

它的职责是把“时间已经发生、但原执行者可能不在”的持久化状态向前推进。它不执行 LLM 或原工具，也不负责重新计算 release。

## 2. 当前执行顺序

```python
async def _maintenance(self, now: int) -> None:
    await self.store.fire_due_timers(now_ms=now)
    await self.store.recover_expired(now_ms=now)
    expire = getattr(self.store, "expire_deadlines", None)
    if expire is not None:
        await expire(now_ms=now)
    # 按周期做 Artifact 孤儿回收
    # 每 5 秒写 ACTIVE Worker 心跳
```

| 顺序 | 操作 | 权威对象 |
|---:|---|---|
| 1 | `fire_due_timers` | `schedule_retry()` 写入的到期 `RETRY` timer |
| 2 | `recover_expired` | lease 过期的 `CLAIMED/RUNNING` Activity 与关联 ToolEffect |
| 3 | `expire_deadlines` | 已超过绝对 deadline 但无活动 Worker 裁决的 Run |
| 4 | `cleanup_orphans` | Artifact CAS 中超过保护期且无持久引用的 blob |
| 5 | `heartbeat_worker` | `runtime_workers` 中本 Worker 的 state/release map/heartbeat |

这个顺序不是优先级队列，但能保证在 claim 新工作之前，已到期工作、失联 attempt 和 deadline 都有机会被推进。

## 3. fire_due_timers

Runtime 的 retry Timer 是持久化时间事实，不是 `asyncio.sleep()`。当 Coordinator 安排可重试失败时，`schedule_retry()` 写入 `kind='RETRY'` 的 scheduled timer，同时把 Run/Activity 置为 `WAITING_RETRY`。

`fire_due_timers(now_ms=now)` 当前只处理这条已实现路径：

```text
timer:    SCHEDULED -> FIRED
Activity: WAITING_RETRY -> PENDING
Run:      WAITING_RETRY -> DISPATCH_PENDING
append Activity/Run status events
```

`timers.kind` 的 current schema 词汇仍列出其他时间类型，但当前 Store 没有将它们装配成 `fire_due_timers()` 的独立行为。绝对 deadline 由后面的 `expire_deadlines()` 统一扫描；`WAITING_INPUT` 由 signal 唤醒，或由同一绝对 deadline 收口。不应把 schema 词汇误读为已实现的 Timer 能力。

因为 retry Timer 在 SQLite 中，Worker 重启后仍能继续扫描。进程内时钟只是发起这次扫描的观察值，不是 Timer authority。

## 4. recover_expired

### 4.1 为什么需要恢复

Worker 执行 Activity 时持有有限期 lease。如果进程崩溃、事件循环卡死或续租不再成功，Activity 不能永远留在 `CLAIMED/RUNNING`。

`recover_expired()` 使用库内的 `lease_expires_at` 判定失联 attempt，再由短写事务推进。它不依赖原 Worker 的内存状态。

### 4.2 不是一律回到 PENDING

恢复时必须优先看持久化 ToolEffect：

```text
lease 过期
  ├─ deadline 已到 -> TIMED_OUT
  ├─ sticky pending_terminal 且 effect 已全解决 -> 原 FAILED
  ├─ sticky pending_terminal 且仍 unresolved -> RECONCILE/MANUAL
  ├─ cancel-owned 且无未决 effect -> CANCELLED
  ├─ 无未决 effect -> Activity PENDING，可重新 claim
  ├─ 所有 effect 均可安全重放 -> Activity PENDING
  └─ 存在不确定/不可透明重放 effect -> RECONCILE/MANUAL
```

可安全自动重放的 effect 只有 READ_ONLY，或已经具备稳定 Runtime idempotency key 的 IDEMPOTENT_EFFECT。NON_IDEMPOTENT/UNKNOWN 已派发后不会因租约过期而盲目再调一次。

sticky `pending_terminal` 是一个更强的边界：它表示 Engine 已经不可逆地得出普通
`FAILED`，只是 terminal 被 ToolEffect 账本挡住。此时即便剩余 effect 原本属于
READ_ONLY，恢复也不能清掉 marker 再跑 Engine；未决项继续保持严格人工协调，全部
解决后直接提交已保存的 code/message。若 reconcile query 已提交结果、Worker 随即
丢 lease，`recover_expired()` 会识别“marker 仍在但 unresolved 已为空”，补交原
`FAILED`，不产生一次新的模型调用。

### 4.3 恢复后仍要 exact claim

Activity 即使被重置为 `PENDING`，新 Worker 仍必须使用完整 `release_map` 通过 `(run.engine, run.release_fingerprint)` 精确匹配才能领取。

wrong-release Worker 不会领取、不会读 checkpoint、也不会将 Run 写成某个不匹配终态。Run 保持 pending，直到 matching Worker 出现或 absolute deadline 收口。

## 5. expire_deadlines

CreateRun 将 deadline 以 UTC epoch ms 的绝对时间写入 Run。向下游传递的是剩余时间，不是每层新建一个完整 timeout。

`expire_deadlines()` 扫描已过期 Run，主要覆盖没有活跃 attempt 可在 Coordinator 内检查 deadline 的情况，例如：

- 长期没有 matching-release Worker 的 `DISPATCH_PENDING` Run。
- `WAITING_RETRY` / `WAITING_INPUT` 超时。
- 需要根据 ToolEffect 不确定性收口的 cancel/reconcile 状态。

deadline 是这些非终态边界之上的最高时间裁决：即使 Run 正处于 cancel-owned 或
sticky-failure reconciliation，过期后也提交 `TIMED_OUT`，并把仍未决的
`tool_execution_id` 写进 terminal payload。它不会错误恢复成原 `FAILED` 或
`CANCELLED`。这是唯一允许带 unresolved ToolEffect 的 terminal；Store 的通用
terminal helper 会拒绝 `SUCCEEDED/FAILED/CANCELLED/REJECTED` 跨过未决效应。

注意实现使用 `getattr(self.store, "expire_deadlines", None)`。这是为了让精简的测试 fake 可以不实现扩展方法；生产 SQLite Store 提供了该能力，不能把 deadline 解释为进程内最佳努力。

## 6. Artifact 孤儿回收

Artifact 的权威关系是：

```text
CAS blob bytes
  + artifact_metadata
  + artifact_links / ToolResult ref / Event ref
```

Runtime 使用 temp -> digest/size -> fsync -> atomic rename -> fsync dir 的写入边界，因此可能在“blob 已安全落盘，metadata/link 还未提交”时崩溃，留下无引用 blob。

`_maintenance` 按 `artifact_cleanup_interval_ms` 节流：

1. 先设置 `_last_artifact_cleanup=now`，避免文件系统异常把 250ms 调度循环变成 cleanup 热循环。
2. 从 Store 取 `referenced_artifact_ids()`。
3. 只删除无引用且早于 `now - artifact_orphan_age_ms` 的 CAS blob。
4. 记录删除数量和回收字节数。

cleanup 异常会被记录为 warning，不阻断 Run dispatch。这个容忍性只属于孤儿回收；Run、lease、checkpoint 和 ToolEffect 权威写入不会以相同方式吞错。

## 7. Worker 心跳与 release map

`RuntimeWorker.run()` 启动时会先立即写一次 `ACTIVE` heartbeat，但当前不会同步更新 `_last_heartbeat`；因此首轮 `_maintenance()` 可能紧接着再写一次。进入稳定主循环后，maintenance 才按 5 秒阈值节流：

```text
runtime_workers
  worker_id
  release_map_json
  state = ACTIVE | DRAINING | STOPPED
  started_at
  heartbeat_at
```

`release_map_json` 必须是本 Worker 实际构造的三个 Adapter fingerprint，它同时用于：

- `claim_next()` 的 exact `(engine, fingerprint)` 筛选。
- `scripts/run_all.sh` 的 Worker readiness 判定。

readiness 不能只看库里已经存在的三个 active pointer，因为它们可能来自上次 Worker。正确判定还要求：

```text
本次启动后的新鲜 ACTIVE heartbeat
+ heartbeat release_map 与三个 active pointer 完全一致
```

## 8. maintenance 不负责的事

### 8.1 不负责 schema 升级

schema 身份在 API/Worker 启动时由 `store.initialize()` 校验。非空 DB 必须与 current `schema.sql` 字节 digest 完全一致，否则启动失败。`_maintenance` 不改表、不改 `schema_meta`。

### 8.2 不负责 release 激活

三份 release 在 Worker 完成 ToolCatalog 和 Adapter 构造后，由 `activate_current_releases()` 一次性原子激活。maintenance 只上报已冻结的 `self.release_map`，不重新计算或切换 pointer。

### 8.3 不负责活跃 attempt 续租

续租是 `_execute()` 为每个 Claim 单独创建的 `_renew_lease()` task，不在 `_maintenance` 内。续租失败会 cancel 对应 attempt；如果 Store 写入后续发现 stale fence/lease/checkpoint CAS 冲突，则以 `AttemptOwnershipLost` 冒泡到 Worker，不会把 Run 收成业务失败。

### 8.4 不负责重新运行 deferred-failure Engine

`pending_terminal=FAILED` 已经冻结原计划终态。maintenance 只能恢复 ToolEffect
协调边界，或在账本已无 unresolved 时原子补交该失败；它不能把 Activity 作为普通
Engine 工作重新领取。需要下游查询时，claim 也只接受 exact reconcile-only marker，
Coordinator 在加载 Adapter、checkpoint 和 history 之前就进入 query hook。

## 9. 错误处理分类

| 任务 | 异常时的原则 |
|---|---|
| Timer/lease/deadline authority 推进 | 不在 `_maintenance` 统一吞掉；写入失败应暴露给调度循环/进程监督 |
| Artifact GC | best-effort，记 warning，不停 dispatch |
| heartbeat | 是 readiness/运维事实，写入失败不应伪装 Worker 健康 |
| attempt ownership loss | 终止当地 attempt，交给 durable recovery，不 terminalize Run |
| sticky failure recovery | 保留原 code/message；只协调 ToolEffect，解决后提交原 FAILED |
| deadline 与 unresolved | 允许 TIMED_OUT 并审计 unresolved IDs；其他 terminal fail-closed |

## 10. 调度与扩展边界

`_maintenance` 在每个 Worker 循环中都可执行，所以 Store 操作必须是幂等/CAS/事务化的，不能依赖“只有一个 maintenance owner”。

当前 SQLite `BEGIN IMMEDIATE` 可在本机多进程中串行化这些写入。这不是跨主机分布式调度；若进化到跨节点 HA，Timer、lease、fencing、release activation 和 Artifact 引用都需迁移到共享权威存储，不能用各节点本地 SQLite 拼接出一致性。

## 11. 源码阅读索引

- `agent/runtime/worker/dispatcher.py`：主循环、`_maintenance()`、`_execute()`、`_renew_lease()` 和 drain。
- `agent/runtime/ports/store.py`：Timer/recovery/heartbeat 端口。
- `agent/runtime/adapters/sqlite/store.py`：`fire_due_timers()`、`recover_expired()`、`expire_deadlines()`、`heartbeat_worker()`。
- `agent/runtime/adapters/sqlite/schema.sql`：`timers`、`activities`、`runtime_workers` 表约束。
- `agent/runtime/worker/main.py`：三 release 原子激活和 Worker `release_map` 构造。
- `agent/runtime/domain/errors.py`：`AttemptOwnershipLost`。
