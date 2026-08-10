# RuntimeWorker._maintenance 维护任务详解

## 1. 方法定位

`RuntimeWorker._maintenance(now)` 是 Runtime Worker 主调度循环每次轮询前执行的**后台 housekeeping（家务）方法**。它负责在真正领取并执行 Run 之前，把系统中需要定期推进的状态一次性处理掉，保证：

- 到期的定时器被触发；
- 因 Worker 崩溃而 lease 过期的 Activity 能被重新领取；
- 超出绝对 deadline 的 Run/Activity 被推进到终态；
- 不再被引用的 Artifact 孤儿被回收；
- Worker 自身心跳持续刷新，让外部知道它还活着。

## 2. 源码位置

- 调用点：`agent/runtime/worker/dispatcher.py` 第 70 行
- 实现：`agent/runtime/worker/dispatcher.py` 第 105–147 行

```python
async def _maintenance(self, now: int) -> None:
    await self.store.fire_due_timers(now_ms=now)
    await self.store.recover_expired(now_ms=now)
    expire = getattr(self.store, "expire_deadlines", None)
    if expire is not None:
        await expire(now_ms=now)
    # ... Artifact 孤儿清理 ...
    if now - self._last_heartbeat >= 5_000:
        await self.store.heartbeat_worker(...)
        self._last_heartbeat = now
```

## 3. 职责总览

`_maintenance` 按顺序完成 5 类任务：

| 顺序 | 任务 | 对应 Store 操作 | 作用 |
|---|---|---|---|
| 1 | 触发到期定时器 | `fire_due_timers` | 执行 `runtime.db` 中到期的 Timer |
| 2 | 恢复过期 Activity | `recover_expired` | 把 lease 过期、原 Worker 可能崩溃的 Activity 重新变为可领取 |
| 3 | 处理 deadline 超时 | `expire_deadlines` | 将超出绝对 deadline 的 Run/Activity 推进到 `TIMED_OUT` 等终态 |
| 4 | Artifact 孤儿清理 | `cleanup_orphans` | 删除超过 24 小时且不再被 metadata 引用的 Artifact blob |
| 5 | Worker 心跳 | `heartbeat_worker` | 每 5 秒写入 `ACTIVE` 心跳与当前 release map |

## 4. 各步骤详细说明

### 4.1 触发到期定时器

```python
await self.store.fire_due_timers(now_ms=now)
```

Runtime 内部会注册一些基于绝对时间的 Timer（例如延迟重试、等待信号的超时）。`_maintenance` 每次循环都会检查当前时间 `now` 之前有哪些 Timer 到期，并将它们触发，推进对应 Activity 或 Run 的状态。

### 4.2 恢复过期 Activity

```python
await self.store.recover_expired(now_ms=now)
```

Worker 执行 Activity 时会持有 lease（租约）。如果 Worker 进程崩溃或网络分区，lease 会在 `lease_ms`（默认 30 秒）后过期。`_maintenance` 由新的 Worker 调用时，会把这些过期的 Activity 重新释放出来，让其他 Worker 能够 `claim_next` 并恢复执行。

这是 Runtime **进程崩溃恢复**的关键路径之一。

### 4.3 处理 deadline 超时

```python
expire = getattr(self.store, "expire_deadlines", None)
if expire is not None:
    await expire(now_ms=now)
```

CreateRun 时可以传入绝对 `deadline_at`，缺省使用 `RUNTIME_DEFAULT_DEADLINE_SECONDS`（默认 600 秒）。`_maintenance` 会扫描超出 deadline 的 Run/Activity，并将它们推进到 `TIMED_OUT` 等终态。

使用 `getattr` 做防御性判断，是为了兼容测试用的 fake store 可能没有实现该方法。

### 4.4 Artifact 孤儿清理

```python
if (
    self.artifact_store is not None
    and (
        self._last_artifact_cleanup is None
        or now - self._last_artifact_cleanup >= self.artifact_cleanup_interval_ms
    )
):
    self._last_artifact_cleanup = now
    try:
        referenced = await self.store.referenced_artifact_ids()
        result = await self.artifact_store.cleanup_orphans(
            referenced_artifact_ids=referenced,
            older_than=datetime.fromtimestamp(
                (now - self.artifact_orphan_age_ms) / 1000,
                tz=timezone.utc,
            ),
        )
        if result.deleted:
            log_kv(logger, logging.INFO, "ArtifactGC", "orphans reclaimed", ...)
    except Exception as exc:
        log_kv(logger, logging.WARNING, "ArtifactGC", "cleanup failed", ...)
```

Artifact 采用内容寻址（SHA-256）。写入流程是：temp 文件 → fsync → atomic rename → fsync 目录。如果 rename 成功后 metadata 事务失败，就可能留下没有被引用的 blob。

`_maintenance` 默认**每小时**扫描一次 Artifact 目录：

1. 先从 `runtime.db` 查出所有被引用的 `artifact_id`；
2. 再让 `artifact_store.cleanup_orphans(...)` 删除超过 `artifact_orphan_age_ms`（默认 24 小时）且不在引用集合中的 blob。

关键设计：先更新 `self._last_artifact_cleanup = now`，再执行清理。这样即使某次文件系统异常，也不会让 250 ms 的主循环变成清理热循环。

另外，Artifact GC 失败会被捕获并记录为 WARNING，**不会阻塞 Run 的调度执行**。

### 4.5 Worker 心跳

```python
if now - self._last_heartbeat >= 5_000:
    await self.store.heartbeat_worker(
        worker_id=self.worker_id, release_map=self.release_map,
        state="ACTIVE", now_ms=now,
    )
    self._last_heartbeat = now
```

每 5 秒向 `runtime_workers` 表写入一条 `ACTIVE` 心跳，携带当前 Worker 支持的 `release_map`（三引擎 release 的 digest）。

心跳有两个用途：

1. **`scripts/run_all.sh` readiness 判断**：脚本会等待本次启动后的新鲜 `ACTIVE` heartbeat，且三种 release 的 active pointer 与 Worker 注册的 release_map 完全一致，才认为 Worker ready。
2. **管理面/排障**：可以通过 `runtime_workers` 表查看哪个 Worker 还活着、它加载了哪些 release。

## 5. 设计要点

### 5.1 与 Run 执行分离

`_maintenance` 只负责状态推进和 housekeeping，真正执行 Run 的逻辑在 `_execute(claim)` 中。二者解耦：

- `_maintenance` 失败不会阻塞 `_execute`；
- `_execute` 失败也不会影响下一次 `_maintenance`。

### 5.2 错误隔离

除了 Artifact GC 用 `try/except` 捕获外，其他 Store 操作（fire_timers/recover_expired/expire_deadlines/heartbeat）通常由 Store 内部保证事务安全。如果某一步抛出异常，会向上传播到 `run()` 的 `try/finally`，最终触发 `_drain()` 优雅停机。

### 5.3 时间基准统一

所有操作都使用同一个 `now = self.clock.now_ms()`，避免在 250 ms 的轮询窗口内因时间不一致导致边界判断错误。

### 5.4 测试入口

`run_once()` 也调用 `_maintenance(now)`，保证单步测试时同样会推进定时器、恢复过期 Activity 和刷新心跳。

## 6. 相关配置参数

| 配置项 | 默认值 | 含义 |
|---|---|---|
| `RUNTIME_WORKER_POLL_MS` | 250 ms | 主循环轮询间隔 |
| `RUNTIME_LEASE_SECONDS` | 30 s | Activity lease 时长 |
| `RUNTIME_LEASE_RENEW_SECONDS` | 10 s | lease 续租间隔 |
| `RUNTIME_ARTIFACT_CLEANUP_INTERVAL_SECONDS` | 3600 s | Artifact 孤儿清理间隔 |
| `RUNTIME_ARTIFACT_ORPHAN_AGE_HOURS` | 24 h | Artifact blob 成为孤儿的最短保留时间 |

## 7. 相关文件

- `agent/runtime/worker/dispatcher.py` — Worker 调度器主循环与 `_maintenance` 实现
- `agent/runtime/adapters/sqlite/store.py` — `fire_due_timers`、`recover_expired`、`expire_deadlines`、`heartbeat_worker`、`referenced_artifact_ids` 等 Store 操作实现
- `agent/runtime/adapters/filesystem_artifact.py` — Artifact CAS 与 `cleanup_orphans` 实现
- `scripts/run_all.sh` — 通过心跳与 release map 判断 Worker readiness

## 8. 一句话总结

`_maintenance` 是 Runtime Worker 每次轮询前的"管家"：推进定时器、回收崩溃 Worker 的 Activity、处理 deadline 超时、清理 Artifact 孤儿、刷新自身心跳——确保系统状态持续收敛，同时不影响新 Activity 的领取与执行。
