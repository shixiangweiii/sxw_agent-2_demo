# 状态机、邻接表与非法迁移错误码 v1

状态：**FROZEN**

所有迁移必须在短 `BEGIN IMMEDIATE` 事务中以 `revision` CAS 提交，同时追加对应状态事件。调用 LLM、Tool、RAG、Skill、文件系统或等待人工不得发生在状态事务内。

## 1. RunState

### 1.1 状态集合

非终态：

```text
ACCEPTED | DISPATCH_PENDING | RUNNING | WAITING_RETRY |
WAITING_INPUT | CANCEL_REQUESTED
```

终态：

```text
SUCCEEDED | FAILED | CANCELLED | TIMED_OUT |
REJECTED
```

`REJECTED` 只用于请求已持久受理、但在执行资格策略检查中被拒绝；校验失败或 admission 事务未提交不是 Run，也没有 `REJECTED` Run。

### 1.2 邻接表

| From | 允许的 To | 必要 guard / 唯一语义 |
|---|---|---|
| `ACCEPTED` | `DISPATCH_PENDING` | admission 后首个 Activity 已就绪，进入调度 |
| `ACCEPTED` | `REJECTED` | 已受理但执行资格策略拒绝；必须携带稳定策略码 |
| `ACCEPTED` | `CANCELLED` | cancel CAS 先于领取提交，且不存在 unresolved ToolEffect |
| `ACCEPTED` | `TIMED_OUT` | 领取前绝对 deadline 已到 |
| `DISPATCH_PENDING` | `RUNNING` | Worker 以有效 lease/fencing 领取当前 Activity |
| `DISPATCH_PENDING` | `CANCELLED` | cancel CAS 先提交，且无 unresolved effect |
| `DISPATCH_PENDING` | `CANCEL_REQUESTED` | cancel CAS 先提交，但已有 `DISPATCHED/UNKNOWN/RECONCILING/MANUAL_REQUIRED` effect；不得直接宣称无副作用取消 |
| `DISPATCH_PENDING` | `TIMED_OUT` | deadline 到且无 unresolved effect |
| `RUNNING` | `WAITING_RETRY` | Outcome 为 retryable，attempt 未耗尽；已创建唯一 retry timer |
| `RUNNING` | `WAITING_INPUT` | Outcome 为 `WAITING_INPUT`/`INTERRUPT`；pending input 已 checkpoint，Activity 不占 lease |
| `RUNNING` | `CANCEL_REQUESTED` | cancel CAS 先提交，且存在正在执行或 `DISPATCHED/UNKNOWN/RECONCILING/MANUAL_REQUIRED` effect，必须等待安全边界/reconcile |
| `RUNNING` | `SUCCEEDED` | 仅 Finalize Transaction；final assistant、citation、terminal 同事务 |
| `RUNNING` | `FAILED` | Coordinator 判定 terminal failure，且不存在应继续 reconcile 的 unknown effect；若存在则先走 `RUNNING → WAITING_INPUT` 并保存 sticky pending terminal |
| `RUNNING` | `CANCELLED` | cancel 已生效，且确认无 unresolved effect；late result 不得覆盖 |
| `RUNNING` | `TIMED_OUT` | deadline 到；terminal payload 列出所有 unresolved tool execution IDs |
| `WAITING_RETRY` | `DISPATCH_PENDING` | 唯一 retry timer CAS `SCHEDULED → FIRED` 后重新排队 |
| `WAITING_RETRY` | `CANCELLED` | cancel CAS 先于 timer/resume，且无 unresolved effect |
| `WAITING_RETRY` | `CANCEL_REQUESTED` | cancel CAS 先提交，但已有 unresolved effect；取消只停止普通 retry，随后进入 reconcile/deadline 收口 |
| `WAITING_RETRY` | `TIMED_OUT` | deadline 到；不得再触发普通 retry |
| `WAITING_INPUT` | `DISPATCH_PENDING` | 唯一 signal 首次消费并提交 resume Activity；重复 signal 不重复唤醒 |
| `WAITING_INPUT` | `CANCELLED` | cancel CAS 先于 signal 消费，且无 unresolved effect |
| `WAITING_INPUT` | `CANCEL_REQUESTED` | cancel CAS 先提交，但已有 unresolved effect；原人工输入不能绕过 Tool reconcile 语义 |
| `WAITING_INPUT` | `FAILED` | 仅严格 Tool reconciliation boundary 携带 `pending_terminal={status:FAILED,code,message}`，且最后一个 unresolved effect 已被确定；同一 signal/query settlement 事务提交原失败，不恢复 Engine |
| `WAITING_INPUT` | `TIMED_OUT` | deadline 或 wait timeout 到；迟到 signal 写审计后拒绝 |
| `CANCEL_REQUESTED` | `CANCELLED` | 执行到安全边界，或 reconcile 已证明 effect 状态，且不再有 unresolved execution |
| `CANCEL_REQUESTED` | `TIMED_OUT` | 到 deadline 仍存在 unresolved effect；terminal payload 必须列出 IDs |
| 任一终态 | 无 | 终态不可变；同一 Run 最多一个 terminal |

### 1.3 硬规则

- clean EOF、生成器正常退出、旧 `done`/`error` 事件都不是 Run 迁移依据。
- SSE 断开不触发任何 Run 迁移。
- Worker 丢失只触发 Activity lease recovery，不直接令 Run `FAILED`。
- Worker 只能精确 claim 其 `release_map` 中 `(engine, release_fingerprint)` 匹配的 Activity；release 不匹配不是 Run 状态迁移。
- terminal 事务先提交时，后续 cancel 返回 `409 RUN_ALREADY_TERMINAL`。
- cancel CAS 先提交时，后续执行结果仅记为 late result，不能提交 `SUCCEEDED`。
- 存在 `DISPATCHED/UNKNOWN/RECONCILING/MANUAL_REQUIRED` ToolEffect 的 cancel 不能直接宣称 `CANCELLED`。
- 除 `TIMED_OUT` 外，任一 terminal 事务都必须再次查询 ToolEffect authority；存在 unresolved execution 时 fail closed。普通 planned `FAILED` 把精确 `{status,code,message}` 持久化到 strict reconciliation `pending_input.pending_terminal`，不得丢失原错误、改成成功或重跑模型。

## 2. ActivityState

### 2.1 状态集合

```text
PENDING | CLAIMED | RUNNING | WAITING_RETRY | WAITING_INPUT |
RECONCILE | MANUAL | SUCCEEDED | FAILED | CANCELLED
```

终态为 `SUCCEEDED | FAILED | CANCELLED`。`RECONCILE` 与 `MANUAL` 是持久化、非终态的 Activity 状态，不是只存在于内存中的 classifier 标签：前者表示该 Activity 只能处理既有不确定 ToolExecution，后者表示自动确认已经停止、必须等待受审计处置。它们分别与 ToolEffect 的 `UNKNOWN/RECONCILING` 和 `MANUAL_REQUIRED` 协同，但两套状态各有自己的事实所有权，不能互相替代。

### 2.2 邻接表

| From | 允许的 To | 必要 guard / 唯一语义 |
|---|---|---|
| `PENDING` | `CLAIMED` | `available_at <= now`；按 `available_at, created_at, activity_id`；原子 `UPDATE ... RETURNING` 分配 lease/fencing |
| `PENDING` | `CANCELLED` | 所属 Run 已取消/终态，且 Activity 从未 dispatch |
| `PENDING` | `RECONCILE` | cancel 在 replacement claim 前接管已有 unresolved effect；只建立严格 reconcile boundary，不允许普通 Engine/Tool claim |
| `PENDING` | `MANUAL` | recovery-only：exact query 在 hook 前中断，或父 lease 丢失时不可安全 replay 的 Tool child 被原子降为人工；同步把 effect 置为 `MANUAL_REQUIRED`，禁止执行原副作用 |
| `CLAIMED` | `RUNNING` | 同一 Worker、有效 lease、revision 和 fencing token |
| `CLAIMED` | `PENDING` | lease 过期且 classifier=`REQUEUE`；确认未发生外部 dispatch，attempt/fencing 增长 |
| `CLAIMED` | `CANCELLED` | cancel 在 dispatch 前生效 |
| `CLAIMED` | `RECONCILE` | 仅 exact reconcile-only marker 的父 Activity 在 hook 前丢失且仍有 unresolved effect；清 lease/marker，恢复人工边界 |
| `RUNNING` | `SUCCEEDED` | 结果/事件/checkpoint 已按相应事务边界提交 |
| `RUNNING` | `FAILED` | 非重试失败或 attempt 耗尽；无 unresolved effect |
| `RUNNING` | `CANCELLED` | cancel 在安全边界生效；late result 被 fencing/CAS 抑制 |
| `RUNNING` | `PENDING` | 仅 lease recovery classifier=`REQUEUE`；未发现不可安全普通重放的 unresolved effect，旧 fencing 已失效 |
| `RUNNING` | `WAITING_RETRY` | 普通 retryable failure；已保存 retry timer/`available_at` |
| `RUNNING` | `WAITING_INPUT` | Engine `INTERRUPT`；已持久化 pending input |
| `RUNNING` | `RECONCILE` | 已存在 `DISPATCHED/UNKNOWN/RECONCILING` effect，普通 completion/retry 不再安全 |
| `RUNNING` | `MANUAL` | ToolEffect 已确定为 `MANUAL_REQUIRED`，停止自动执行 |
| `WAITING_RETRY` | `PENDING` | 唯一 timer 首次 fired，更新 `available_at` 和 attempt |
| `WAITING_RETRY` | `CANCELLED` | Run cancel/terminal 先提交 |
| `WAITING_RETRY` | `RECONCILE` | cancel/安全边界发现 unresolved effect；普通 retry 被严格 Tool reconcile 取代 |
| `WAITING_INPUT` | `PENDING` | 匹配 `wait_activity_id` 的 signal 首次消费 |
| `WAITING_INPUT` | `CANCELLED` | Run cancel/terminal 先提交 |
| `WAITING_INPUT` | `RECONCILE` | Engine generic wait 不得覆盖既有 Tool uncertainty；pending input 改为严格 Tool reconcile boundary |
| `RECONCILE` | `PENDING` | 稳定 ToolExecution 获得再次领取资格；后续只能执行允许的 reconcile/受保护重试路径 |
| `RECONCILE` | `SUCCEEDED` | reconcile 得到可验证的已提交结果 |
| `RECONCILE` | `FAILED` | reconcile 得到可验证的未提交/失败结果，或 deadline 收口 |
| `RECONCILE` | `MANUAL` | 无 hook、预算耗尽或结论仍不确定，ToolEffect 同步为 `MANUAL_REQUIRED` |
| `RECONCILE` | `CANCELLED` | cancel 已获所有权且最后一个 unresolved effect 已被人工或 hook 确定；仅 Store terminal 事务可走此边 |
| `MANUAL` | `PENDING` | 受审计 signal/运维处置允许重新查询或恢复，并保留稳定身份 |
| `MANUAL` | `SUCCEEDED` | 受审计处置提供可验证的外部成功证据 |
| `MANUAL` | `FAILED` | 受审计处置提供可验证的未提交/失败证据，或 deadline 收口 |
| `MANUAL` | `CANCELLED` | 已确认无 unresolved effect，cancel 可以安全收口 |
| 任一 Activity 终态 | 无 | 不可回退；重试通过原 Activity 的受保护 attempt 或新逻辑 Activity 表达，不覆写终态 |

### 2.3 Lease recovery classifier

| 观察 | disposition | Activity 迁移 | 后续动作 |
|---|---|---|---|
| 仅 `CLAIMED`，无 dispatch 证据 | `REQUEUE` | `CLAIMED → PENDING` | fencing 增长后可重新领取 |
| `RUNNING`，无 unresolved effect，或全部 effect 可按 READ_ONLY/IDEMPOTENT guard 安全恢复 | `REQUEUE` | `RUNNING → PENDING` | fencing 增长；Tool Broker 仍须按稳定账本决定复用、reconcile 或允许的重试 |
| 存在不可安全普通重放的 `DISPATCHED/UNKNOWN` effect | `RECONCILE` | parent `RUNNING → RECONCILE`；child `RUNNING/PENDING/RECONCILE → MANUAL` | 同一 recovery 事务记录有界 UNKNOWN 审计并把 effect 置为 `MANUAL_REQUIRED`；Run 进入严格 `WAITING_INPUT`，若 cancel 已先提交则保持 `CANCEL_REQUESTED` |
| reconcile 无 hook、预算耗尽或仍不明 | `MANUAL` | `RECONCILE → MANUAL` | ToolEffect=`MANUAL_REQUIRED`；等待受审计 signal/运维处置，不得透明 redispatch |
| 人工授权 query 后，父 Worker 在 hook 前/执行中丢失 | `MANUAL` | child `PENDING/RECONCILE → MANUAL`；parent `CLAIMED/RUNNING → RECONCILE` | effect `RECONCILING → MANUAL_REQUIRED`，清 exact marker；旧 signal 只能幂等重放，必须提交新 signal 才可再查 |
| query 已提交确定 ToolResult，但父结算前丢失 | `REQUEUE` / cancel 收口 | parent `CLAIMED/RUNNING → PENDING`，或 cancel terminal | 清 exact marker；普通 Run 下一 claim 从 committed ToolResult 恢复，cancel Run 收口；不得再次调用 hook |
| stale Worker 迟到提交 | `REJECT_STALE` | 无 | 返回 `ACTIVITY_FENCING_STALE`，可记录诊断 late result |

## 3. ToolEffectState

### 3.1 状态集合与 effect class

执行账本状态：

```text
PREPARED | DISPATCHED | COMMITTED | FAILED | UNKNOWN |
RECONCILING | MANUAL_REQUIRED
```

工具 effect class：

```text
READ_ONLY | IDEMPOTENT_EFFECT | NON_IDEMPOTENT_EFFECT | UNKNOWN_EFFECT
```

未声明的 Skill、A2A、Claude SKILL 必须按 `UNKNOWN_EFFECT`，不得以 HTTP retry 自动重复。

### 3.2 邻接表

| From | 允许的 To | 必要 guard / 唯一语义 |
|---|---|---|
| `PREPARED` | `DISPATCHED` | ToolCall、Activity、ToolExecution 和 `TOOL_CALL_COMMITTED` 已提交；事务外即将/已经开始 I/O |
| `PREPARED` | `FAILED` | 明确未 dispatch 且本地校验失败、Run 取消或 deadline 到；必须记录 `not_dispatched=true` |
| `DISPATCHED` | `COMMITTED` | 收到可验证成功证据并持久化完整 result/ref |
| `DISPATCHED` | `FAILED` | 有确定证据表明 effect 未提交或调用明确失败 |
| `DISPATCHED` | `UNKNOWN` | timeout、进程丢失、ACK/commit 丢失，无法证明提交或未提交 |
| `DISPATCHED` | `MANUAL_REQUIRED`（recovery composite） | 仅父 lease/安全边界已丢失且 effect 不可 replay；同一短事务先保留 UNKNOWN result/error/correlation 审计，再停止自动执行；不是“确定失败” |
| `DISPATCHED` | `DISPATCHED`（新 attempt） | 仅 READ_ONLY，或 `IDEMPOTENT_EFFECT + supports_idempotency=true`，且 reconcile 未发现已提交结果、attempt 未耗尽；稳定 execution/key 不变 |
| `FAILED` | `DISPATCHED` | 仅 READ_ONLY，或 `IDEMPOTENT_EFFECT + supports_idempotency=true`；attempt 未耗尽，透传同一稳定 idempotency key |
| `UNKNOWN` | `RECONCILING` | `supports_reconcile=true`；只能查询/确认，不能普通 redispatch |
| `UNKNOWN` | `MANUAL_REQUIRED` | 无 reconcile hook，或策略禁止自动查询/补偿 |
| `UNKNOWN` | `DISPATCHED` | 仅上述 replay-safe effect；先按 reconcile 规则确认无已提交结果，再用同一 execution/idempotency key 受控重试 |
| `RECONCILING` | `COMMITTED` | 外部系统确认 effect 已提交；保存 `external_object_ref` |
| `RECONCILING` | `FAILED` | 外部系统确认 effect 未提交/明确失败 |
| `RECONCILING` | `UNKNOWN` | 查询仍无确定结论；按预算再次 reconcile 或转 manual |
| `RECONCILING` | `MANUAL_REQUIRED` | reconcile 次数耗尽、deadline 临近或仍不确定 |
| `RECONCILING` | `DISPATCHED` | 仅上述 replay-safe effect，query 无确定结果且 attempt 尚可用；NON_IDEMPOTENT/UNKNOWN_EFFECT 禁止此边 |
| `MANUAL_REQUIRED` | `RECONCILING` | 人工 signal 请求再次查询，并且 hook 可用 |
| `MANUAL_REQUIRED` | `COMMITTED` | 受审计 signal 提供可校验的外部成功证据 |
| `MANUAL_REQUIRED` | `FAILED` | 受审计 signal 提供可校验的未提交/失败证据 |
| `COMMITTED` | 无 | 完整结果可复用，不得重复执行副作用 |

`FAILED` 对不满足 retry guard 的调用是事实终点。`UNKNOWN` 绝不自动降级为普通 `FAILED`。只有 READ_ONLY，或能向下游透传同一稳定 key 的 `IDEMPOTENT_EFFECT`，才能在 reconcile 未确认结果后走受控 replay 边；Broker 和 Store 都必须从冻结账本验证 effect class、release digest、幂等能力和稳定 key，当前 manifest 漂移一律 `TOOL_REPLAY_MISMATCH`。`NON_IDEMPOTENT_EFFECT/UNKNOWN_EFFECT` 始终只能确定性收口或进入 manual。人工 `action=reconcile` 的 exact-marker 专路由对所有 effect class 都只允许 query，不授予 replay 权限。Run 到 deadline 时可保留任何仍未确定的 `DISPATCHED/UNKNOWN/RECONCILING/MANUAL_REQUIRED` ToolEffect，但 Run 必须 `TIMED_OUT` 并携带 unresolved IDs；terminal 后这些账本状态只供审计，不再可领取。

### 3.3 人工 ToolEffect 处置信号

`MANUAL_REQUIRED` 只能由公开、受审计的 `type=tool_reconciliation` signal 推进。payload 严格包含：

```text
tool_execution_id
action = mark_committed | mark_failed | reconcile
evidence（非空、有界 JSON）
result / result_ref / external_object_id（按 action 受约束）
```

- `mark_committed`：要求 `result.status=SUCCESS|NO_OUTPUT`；`SUCCESS` 必须携带有界 preview、已注册 Artifact `result_ref` 或 `external_object_id` 之一。
- `mark_failed`：要求 `result.status=FAILURE` 及稳定 `error_code/error_message`；该人工失败为 sticky，不因 manifest 尚有 attempt 预算而重新 dispatch。
- `reconcile`：禁止携带 result/ref/external object，且 ToolExecution 必须持久化 `supports_reconcile=true`；它只授权再次查询，不能普通 redispatch。Store 生成且只生成 `{kind, tool_execution_id, signal_id, expected_effect_revision}` exact marker。
- evidence 最多 4KiB、inline result 最多 8KiB；更大内容必须先 Artifact 化。`result_ref` 必须已存在，并在同一事务建立指向 `TOOL_RESULT_COMMITTED` 的来源 Link。

Store 在同一短事务中校验 deadline、Run 当前 wait boundary、`wait_activity_id`、pending unresolved IDs、ToolExecution/Run 所有权以及 `MANUAL_REQUIRED + MANUAL` 组合。`mark_committed/mark_failed` 同步 CAS ToolEffect/Tool Activity，写有界 `TOOL_RESULT_COMMITTED + ACTIVITY_STATUS_CHANGED + SIGNAL_RECORDED`；有 `result_ref` 时同事务建立 Artifact Link。若还有其他 unresolved effect，父 Run 保持原人工边界；普通 Run 仅在最后一个解决后恢复 `PENDING/DISPATCH_PENDING`，cancel-owned Run 仅在最后一个解决后以唯一 terminal 事务变为 `CANCELLED`。

若 strict boundary 携带 `pending_terminal={status:"FAILED",code,message}`，它表示 Coordinator 已产生不可重试的失败、只是 terminal commit 被 ToolEffect uncertainty 阻挡。该对象是 sticky authority：每次人工 signal、query-only 调度和 lease recovery 都必须原样保留。最后一个 effect 无论被 `mark_committed`、`mark_failed` 还是 query hook 确定，Store 都在同一事务提交原 `FAILED` 与原 code/message，绝不恢复普通 Engine。deadline 若先到仍以 `TIMED_OUT + unresolved_tool_execution_ids` 收口；cancel 若先获得所有权则改走既有 `CANCEL_REQUESTED` 语义。

这里的 pending list 只列 operator-actionable 的 `MANUAL_REQUIRED` IDs。普通 Run 的其余 uncertainty 若全部满足冻结 replay guard，则在最后一个人工 ID 解决后重排 Engine，由 Broker 复用或受控 replay；若还存在不可 replay 且未进入 manual 的 effect，事务必须 fail closed。cancel 一旦获得所有权，idle/safe boundary 上剩余的 `DISPATCHED/UNKNOWN/RECONCILING`（包括原本 replay-safe 的 effect）也会原子转为 `MANUAL_REQUIRED/MANUAL`，绝不能再恢复原 executor。generic `WAITING_INPUT`/普通 signal 同样不能覆盖这一边界。

`reconcile` 授权事务只写 `SIGNAL_RECORDED` 和 Activity/Run 调度事件，不伪造 `TOOL_RESULT_COMMITTED`。Worker 领取 exact marker 后，Coordinator 在 Engine registry、checkpoint 和 history 之前进入专用分支；Broker 校验冻结的 tool name/release digest/capability/revision，只调用 query hook，绝不调用原 executor。真正的 hook 结论才写 ToolResult；无 hook、release 漂移、异常或无结论均回 `MANUAL_REQUIRED/MANUAL`。query settlement 再以 parent fencing、marker、signal、effect revision 和绝对 deadline 做一次 CAS；旧 fence/旧 marker fail closed。任一校验或 CAS 失败整体回滚，普通 signal 不得唤醒 Tool reconciliation boundary。

### 3.4 稳定身份与 replay guard

- native：`model_activity_id + tool ordinal` 派生 Activity/ToolExecution UUIDv5。
- ADK：先提交完整 ToolCall batch，再由 `invocation_activity + turn ordinal + call ordinal` 派生 UUIDv5。
- ADK `function_call_id` 只用于关联，不是稳定幂等身份。
- 同一 slot 重放时，`tool_name`、规范化 request digest、release digest、effect class 或决定 replay/reconcile 语义的 capability 不一致，必须 fail closed：`TOOL_REPLAY_MISMATCH`。
- 禁止按参数 hash 猜测“两次相同参数调用其实是同一次”。

### 3.5 ToolResult 与外部 correlation

Store 入口只接受与冻结 `tool-result-envelope-v1` 同构的有界对象，额外字段禁止进入公开 envelope。effect/result 必须满足：

| ToolEffect settlement | 允许的 ToolResult status |
|---|---|
| `COMMITTED` | `SUCCESS | NO_OUTPUT | INTERRUPT` |
| `FAILED` | `FAILURE` |
| `UNKNOWN | RECONCILING | MANUAL_REQUIRED` | `UNKNOWN` |

`FAILURE/UNKNOWN` 必须有有界 `error_code/error_message`，`INTERRUPT` 必须有 `pending_input`，`NO_OUTPUT` 禁止伪带 preview/ref。`result_ref` 必须是已注册 Artifact；若调用同时提交 metadata，则 metadata ID、envelope、账本列、Event 与 Artifact Link 必须完全一致，否则整笔回滚。

`external_object_id` 是稳定 ToolExecution 的单调 correlation：一旦知道便不能被 retry、manual、reconcile 或 recovery 清除，也不能换成另一个外部对象；冲突 fail closed。未知结果已有的 preview/ref/external identity 会跨 `DISPATCHED → RECONCILING → MANUAL_REQUIRED` 保留，并通过 reconcile-only `ToolCallContext` 暴露给 query hook。hook 新发现的 correlation 先持久化再回 manual；conclusive result 即使省略 external ID，也继承账本中已知身份。大结果仍只保存有界 preview + Artifact ref。

## 4. Delivery v1

首版没有持久化 Delivery 状态机。Event 事务提交后即具备属性 `AVAILABLE`；客户端 cursor 不写数据库。

```text
Canonical Event committed → AVAILABLE
```

不存在服务端 `AVAILABLE → DELIVERED → ACKED` 迁移，不存在 `delivery_cursors`，也不存在 Delivery 状态错误码。重连只执行 `seq > cursor` 的可见事件查询。

## 5. 非状态枚举但必须 CAS 的对象

| 对象 | CAS / 唯一性 |
|---|---|
| Checkpoint | `(run_id, expected_revision)`；append-only revision |
| Event | `runs.next_seq` 与批量 insert 同事务；`UNIQUE(run_id, seq)`、`UNIQUE(event_id)` |
| Timer | `revision` CAS；`SCHEDULED → FIRED` 仅一次 |
| Signal | `(run_id, signal_id)` 唯一；相同 digest 重放，不同 digest 冲突 |
| Cancel command | `(run_id, command_id)` 唯一；重复返回原结果 |
| Conversation active Run | 仅非终态唯一索引；幂等重放必须先于此检查 |
| Run terminal | 数据库约束 + CAS 保证一次；terminal event 与 terminal 字段同事务 |

## 6. 非法迁移与并发错误码

| 错误码 | HTTP/内部 | 触发条件 |
|---|---:|---|
| `INVALID_RUN_STATE_TRANSITION` | 409 | `from → to` 不在 Run 邻接表 |
| `RUN_ALREADY_TERMINAL` | 409 | terminal Run 收到 cancel 或普通迁移 |
| `RUN_TERMINAL_CONFLICT` | 内部冲突 | 两个不同 terminal 竞争；仅 CAS 获胜者提交 |
| `RUN_REVISION_CONFLICT` | 内部可重读 | expected revision 已变化 |
| `CONVERSATION_BUSY` | 409 | 真正的新 Run 违反 conversation 非终态唯一约束 |
| `IDEMPOTENCY_KEY_REUSE` | 409 | 同 scope key 的规范化请求 digest 不同 |
| `INVALID_ACTIVITY_STATE_TRANSITION` | 内部错误 | `from → to` 不在 Activity 邻接表 |
| `ACTIVITY_LEASE_REQUIRED` | 内部错误 | 无有效 claim/lease 尝试启动或提交 Activity |
| `ACTIVITY_LEASE_EXPIRED` | 内部可恢复 | lease 已过期，当前 Worker 失去执行资格 |
| `ACTIVITY_FENCING_STALE` | 内部拒绝 | 提交方 fencing token 小于当前值 |
| `ACTIVITY_REVISION_CONFLICT` | 内部可重读 | Activity CAS 失败 |
| `INVALID_TOOL_EFFECT_TRANSITION` | 内部错误 | ToolEffect 迁移不在邻接表 |
| `TOOL_REPLAY_MISMATCH` | terminal/内部 | 同稳定 slot 的 tool name/request digest 改变 |
| `TOOL_AUTORETRY_FORBIDDEN` | 内部裁决 | NON_IDEMPOTENT/UNKNOWN 或无幂等支持的 effect 尝试普通 retry |
| `TOOL_RECONCILE_REQUIRED` | 内部裁决 | UNKNOWN 被请求普通 redispatch |
| `TOOL_MANUAL_REQUIRED` | 409/等待输入 | 无法自动 reconcile，需要受审计 signal |
| `TOOL_RECONCILIATION_INVALID` | 409/HTTP 422 | reserved signal payload 不满足 action/result/evidence/ref 契约 |
| `TOOL_RECONCILIATION_MISMATCH` | 409 | ToolExecution 不属于当前 Run/pending unresolved boundary |
| `TOOL_RECONCILE_UNSUPPORTED` | 409 | `reconcile` action 的 ToolExecution 未持久化 reconcile 能力 |
| `TOOL_RECONCILIATION_SIGNAL_REQUIRED` | 409 | 普通 signal 尝试唤醒 ToolEffect reconciliation boundary |
| `CHECKPOINT_REVISION_CONFLICT` | 内部 ownership loss | checkpoint expected revision 不匹配；Adapter 转为 `AttemptOwnershipLost` 并冒泡给 Worker，不继续重读执行 |
| `EVENT_SEQUENCE_CONFLICT` | 内部错误 | seq 唯一约束或 `next_seq` CAS 失败 |
| `SIGNAL_REPLAY_MISMATCH` | 409 | 同 signal_id 的规范化 digest 不同 |
| `SIGNAL_REJECTED_LATE` | 409 | terminal Run 的 signal；仍写 `REJECTED_LATE` 审计行 |
| `TIMER_ALREADY_FIRED` | 内部幂等成功 | timer CAS 已由其他执行者完成 |
| `ACTIVE_RUNS_BLOCK_RELEASE_ACTIVATION` | 启动失败 | 激活新 fingerprint 时存在不同 fingerprint 的非终态 Run |
| `CLAIM_RELEASE_MISMATCH` | 不可达防御断言 | Coordinator 收到与 Adapter release 不一致的 claim；只中止 attempt 并报警，不终态化 Run |
| `CURRENT_SCHEMA_MISMATCH` | 启动失败 | 非空数据库的 `schema_digest` 与完整 current `schema.sql` SHA-256 不一致，或缺少 identity |

未知或未列出的迁移一律 fail closed，不通过日志警告后继续。
