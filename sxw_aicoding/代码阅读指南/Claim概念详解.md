# Claim 概念详解

## 1. 一句话定义

**Claim 是 Worker 从 SQLite 原子领取 Activity 后获得的、带 exact release、lease 和 fencing 约束的当前 attempt 执行凭证。**

它不是长期所有权，也不是一个独立数据库实体。Worker 只在 lease 未过期、fencing token 仍是当前值、Run release 与 Worker 完全匹配时有权执行和提交。

## 2. 数据结构

`Claim` 定义在 `agent/runtime/ports/store.py`：

```python
@dataclass(frozen=True)
class Claim:
    run: RunRecord
    activity: ActivityRecord
```

| 部分 | 作用 |
|---|---|
| `run` | 执行语义快照：engine、release fingerprint、deadline、input、conversation 等 |
| `activity` | attempt 执行权快照：activity id、attempt、lease owner/expiry、fencing token、resume payload 等 |

Activity 是 `activities` 表中可多次领取的持久工作单元；Claim 只是领取成功时返回给 Worker 的不可变快照。同一 Activity 恢复后再次被 claim 时，`activity_id` 不变，但 `attempt` 和 `fencing_token` 会增加。

## 3. Worker 为什么携带 release_map

Worker 启动完成后持有：

```text
release_map = {
  "plan_execute": <fingerprint>,
  "agent_loop":   <fingerprint>,
  "native_loop":  <fingerprint>
}
```

它来自当前 Worker 已经成功构造的三个 Adapter，不是临时从 active pointer 猜测的值。`claim_next()` 签名直接接收完整 mapping：

```python
claim_next(
    *,
    worker_id: str,
    lease_ms: int,
    now_ms: int,
    release_map: Mapping[str, str],
) -> Claim | None
```

这个设计把“Worker 能不能解释 Run”前置到领取 SQL，而不是领到以后再判失败。

## 4. claim_next 的原子过程

实现在 `agent/runtime/adapters/sqlite/store.py`。整个领取是一个短 `BEGIN IMMEDIATE`，核心是单条 `UPDATE ... WHERE activity_id=(SELECT ...) RETURNING *`。

### 4.1 候选 Activity 条件

普通 Engine Activity 必须同时满足：

```text
activity.type = ENGINE_RUN
activity.state = PENDING
activity.available_at <= now
run.state = DISPATCH_PENDING
run.pending_input 为空
run.deadline_at > now
(run.engine, run.release_fingerprint) 精确命中 release_map
```

`DISPATCH_PENDING` 还有一个刻意收窄的例外：Run 保存着
`pending_terminal.status=FAILED` 时，只允许 `resume_payload` 为字段集合和类型都完全
正确的 reconcile-only marker，不能当成普通 Engine Activity 领取。`CANCEL_REQUESTED`
同样只允许这种 query-only claim。普通、恢复、deferred-failure reconcile 和
cancel-owned reconcile 都必须通过同一 exact release predicate。

### 4.2 领取时原子更新 Activity

```text
state: PENDING -> CLAIMED
attempt: attempt + 1
fencing_token: fencing_token + 1
lease_owner: worker_id
lease_expires_at: now + lease_ms
revision: revision + 1
```

候选选择和更新在同一 SQL/事务边界中完成。SQLite 写事务串行化竞争 Worker：第二个 Worker 获得写锁后重新基于已提交状态选择，不会再领到同一条 `PENDING` Activity。

### 4.3 同事务推进 Run 和 Event

普通路径还会：

```text
Run: DISPATCH_PENDING -> RUNNING
Run.current_activity_id = activity_id
append ACTIVITY_STATUS_CHANGED
append RUN_STATUS_CHANGED
```

如果 Run 状态 CAS 没有命中，整个 claim 回滚，不会留下一条孤立的 `CLAIMED` Activity。

## 5. Exact release 语义

Run 在 admission 写事务中从 `active_releases` 冻结 `release_fingerprint`。Worker 必须精确匹配 engine 和 fingerprint 才能 claim。

```text
Run(engine=native_loop, release=A)

Worker 1 release_map[native_loop] = A  -> 可 claim
Worker 2 release_map[native_loop] = B  -> 候选 SQL 根本不命中
```

后者不会把 Run 标记成某种“不兼容终态”。Run 保持待正确 Worker 领取，最终还有绝对 deadline 收口。

Coordinator 仍有一道防御断言：

```text
adapter.release_fingerprint == run.envelope.release_fingerprint
```

若这道本应不可达的检查失败，产生 `CLAIM_RELEASE_MISMATCH`，并按所有权异常处理：中止 attempt 并报警，不产生 Run 终态。

## 6. Lease 与 fencing 各自解决什么

### 6.1 Lease：执行权有有效期

`lease_expires_at` 让其他 Worker 能在原 Worker 崩溃后恢复 Activity。`RuntimeWorker` 为每个 attempt 启动续租 task，周期性调用 `renew_lease()`。

续租失败时，Worker 立即 cancel 当地 attempt。这是“不再做事”的快速路径。

### 6.2 Fencing：防止旧执行者晚到提交

仅依靠 lease 不够：旧 Worker 可能暂停后恢复，还以为自己有权写入。每次 claim 增加的 `fencing_token` 会被带到：

- `mark_activity_running`
- Event append/flush
- Checkpoint CAS
- ToolExecution prepare/settle
- Run finalization

Store 对比当前 token、owner、lease 与 Activity state。旧 token 即使“晚到”，写入也会被拒绝。

## 7. AttemptOwnershipLost

`agent/runtime/domain/errors.py` 将下列 Store fault 识别为 attempt 所有权丢失：

- `ACTIVITY_FENCING_STALE`
- `STALE_FENCING_TOKEN`
- `ACTIVITY_LEASE_EXPIRED`
- `ACTIVITY_LEASE_REQUIRED`
- `CHECKPOINT_REVISION_CONFLICT`
- `CLAIM_RELEASE_MISMATCH`

它们被转成 `AttemptOwnershipLost`，必须穿过 Engine/Coordinator 边界到 Worker：

```text
Store ownership fault
-> raise_if_ownership_lost()
-> AttemptOwnershipLost
-> RuntimeIO.abort() / cancel local attempt
-> Worker 记录所有权丢失
-> durable lease recovery 决定后续所有者
```

不可以将它包装成模型可见 ToolResult，不可以记为普通 Engine failure，也不可以 terminalize Run。这不是“用户任务失败”，而是“当前 Worker 已无权裁决”。

这条边界同时覆盖三个 Engine：

- Native executor 对 `AttemptOwnershipLost` 和 `RuntimeFault` 直接上抛；
- ADK `AgentInvocationPlugin` 只把普通工具异常转成模型可见反馈，Runtime 控制故障原样上抛；
- ADK 2.6.2 会把 plugin 异常包成 `RuntimeError`，`AdkEngineAdapter` 只沿
  `__cause__/__context__` 精确找回 `RuntimeFault`/`AttemptOwnershipLost`，不会把任意
  `RuntimeError` 猜成控制故障。

非 ownership 的 `RuntimeFault` 也不能回灌模型。若它发生在 Tool 已 durable
`DISPATCHED` 之后，Broker 先按 effect class 把 ledger 结算为确定失败或不确定效应，
再保留原错误码向上抛；这与“把异常包装成普通 ToolResult 让模型继续”是两回事。

## 8. 租约过期恢复不等于无条件重放

`recover_expired()` 会扫描租约过期的 `CLAIMED/RUNNING` Activity，但恢复结果取决于持久化事实：

1. deadline 已到：按 `TIMED_OUT` 收口，同时保留未决 effect 信息。
2. cancel 已经获得裁决权且无未决 effect：可确定性收口 `CANCELLED`。
3. 已有 sticky `pending_terminal=FAILED` 且所有 effect 已解决：直接提交原失败，不再运行 Engine。
4. sticky failure 仍有未决 effect：保留 marker 并回到严格 `RECONCILE/MANUAL` 边界。
5. 普通恢复中，只有 READ_ONLY 或具有稳定幂等键的 IDEMPOTENT effect 才可回到
   `PENDING` 并复用稳定 slot。
6. 存在不确定或不可透明重放的 ToolEffect：进入 `RECONCILE/MANUAL`，不再派原工具。

所以 Claim 不仅是“抢任务”，还是 ToolEffect 恢复安全的第一道所有权边界。

## 9. 状态流程

```text
首次执行：
Run      DISPATCH_PENDING -> RUNNING
Activity PENDING -> CLAIMED -> RUNNING -> SUCCEEDED/FAILED/...

可重试失败：
Run      RUNNING -> WAITING_RETRY -> DISPATCH_PENDING
Activity RUNNING -> WAITING_RETRY -> PENDING -> CLAIMED

lease 丢失且可安全恢复：
Activity CLAIMED/RUNNING -> PENDING -> CLAIMED
         fencing_token 在新 claim 时再增 1

effect 不确定：
Activity RUNNING -> RECONCILE/MANUAL

Engine 已失败但 effect 尚不确定：
Run      RUNNING -> WAITING_INPUT(pending_terminal=FAILED) -> FAILED
Activity RUNNING -> RECONCILE -> FAILED                    # mark_* signal
         RUNNING -> RECONCILE -> PENDING -> CLAIMED/RUNNING -> FAILED
                                                            # query-only claim
         两条都不重新执行 Engine
```

## 10. 常见误解

### Claim 成功是否等于 Run 成功？

不等于。Claim 只表示 Worker 当前有权执行某 Activity。正常 Engine 执行结束时，Coordinator 会结合 `EngineOutcome`、cancel、deadline 和 ToolEffect 做终态裁决；cancel API、deadline maintenance、lease recovery/reconciliation 等命令路径也可以由 Store 在权威写事务中直接提交终态。无论入口是谁，都必须经过 Run 状态 CAS 和唯一 terminal event 约束。

若计划终态是普通 `FAILED` 但仍有 unresolved ToolEffect，`finalize_failure()` 不会
返回终态，而是把原 code/message 保存为 sticky `pending_terminal` 并进入人工协调。
最后一个 effect 经 signal 或 query 解决后，同一 Store 事务提交原 `FAILED`，不会再
claim Engine。底层 `_finalize_terminal_in_tx()` 还禁止除 `TIMED_OUT` 外的任何终态
跨过 unresolved effect，作为所有上层入口的最终防线。

### 可否只按 engine 过滤 claim？

不可以。同一 engine 的源码、工具目录、provider 或语义配置不同时，release 已经不同。必须同时匹配 `(engine, fingerprint)`。

### 为什么还需要 Activity revision？

fencing 解决 attempt 所有者，revision 则用于对 Activity 内部状态变化做 CAS。Checkpoint 另有自己的 revision CAS。不同 revision 服务于不同聚合根，不能相互代替。

## 11. 源码阅读索引

- `agent/runtime/ports/store.py`：`Claim`、`RuntimeStore.claim_next()` 端口。
- `agent/runtime/adapters/sqlite/store.py`：`claim_next()`、`renew_lease()`、`recover_expired()`。
- `agent/runtime/worker/dispatcher.py`：调度循环、attempt task 与 lease renewal task。
- `agent/runtime/application/coordinator.py`：`mark_activity_running()`、release 防御断言、Engine 执行。
- `agent/runtime/domain/errors.py`：`AttemptOwnershipLost`。
- `agent/plugins/agent_invocation_plugin.py` 与 `agent/runtime/adapters/adk_engines.py`：ADK
  Runtime 控制故障透传和精确解包。
- `agent/runtime/application/tool_broker.py`：DISPATCHED 后 RuntimeFault 的 effect-aware
  结算及 ownership-loss 不改账本边界。
- `agent/runtime/adapters/sqlite/schema.sql`：Activity/Run 状态、claim 索引和 release 外键。
