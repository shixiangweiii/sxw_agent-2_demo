# Conversation-Run-Activity 层次模型详解

本文解释 Conversation、Run、Activity 三个聚合层的职责，以及当前 exact release、Canonical Event 历史、lease/fencing 和 final assistant 语义如何贯穿三层。

## 1. 层次结构

```text
Conversation（对话聚合）
  ├─ 固定 principal_id + agent_id 归属
  ├─ 分配单调 turn_seq
  └─ Run 1
       ├─ 一次用户输入与唯一终态
       ├─ 入场时冻结 engine + release fingerprint + deadline
       ├─ Canonical Events / Checkpoints / ToolExecutions
       └─ Activity 1..N
            ├─ 稳定 logical identity
            ├─ claim / attempt / lease / fencing
            └─ 可重试、等待输入或 reconcile
  └─ Run 2 ...
```

简单类比：

- Conversation 是一条持续聊天线。
- Run 是这条线上的一轮用户请求。
- Activity 是这一轮中可调度、可恢复的工作单元。

但需注意：对话历史不是直接从 Conversation 表的某个 JSON 字段读取，而是从已提交 Canonical Events 编译得到。

## 2. Conversation：归属和轮次空间

`conversations` 表的核心字段：

```text
conversation_id
principal_id
agent_id
next_turn_seq
revision
created_at / updated_at
```

### 2.1 职责

1. **归属隔离**：已有 Conversation 只能被相同 principal/agent 继续使用。
2. **轮次编号**：admission 在写事务中分配 `turn_seq`，并增加 `next_turn_seq`。
3. **并发串行化**：同一 Conversation 只能有一个非终态 Run。

第 3 点由 current schema 的 partial unique index 保护：

```sql
CREATE UNIQUE INDEX uq_active_run_per_conversation ON runs(conversation_id)
WHERE state NOT IN (
  'SUCCEEDED','FAILED','CANCELLED','TIMED_OUT','REJECTED'
);
```

这不是进程内锁。API 多进程时仍由 SQLite 唯一约束裁决，冲突对外投影为 `CONVERSATION_BUSY`。

### 2.2 Conversation 不拥有什么

Conversation 不持久化 message list、Engine session 或“当前 release”。每个 Run 自己冻结 release，每个 attempt 从 committed events 重建 history。

## 3. Run：一轮请求的裁决聚合

### 3.1 不可变执行信封

Run 读模型中的 `RuntimeEnvelope` 包含：

```text
request/client/idempotency identity
conversation_id / turn_id / run_id
principal_id / agent_id
engine
deadline_at / cancel_token_id
release_fingerprint
input_event_id / attachment_refs
created_at
schema_version = "1"   # 对外传输契约
```

`runs` 表本身不保存 `schema_version`。Run 的 release fingerprint 在 admission 写事务中从 `active_releases` 读取并冻结，之后不随 active pointer 改变。

### 3.2 可变状态

RunRecord 还包括：

```text
status, revision, next_seq, current_activity_id,
terminal_status, terminal_payload,
input_text, pending_input, updated_at, trace_id
```

`trace_id` 是跨 API/Worker 进程的诊断关联键，不参与幂等 digest 或终态裁决。

### 3.3 Run 状态机

```text
ACCEPTED -> DISPATCH_PENDING -> RUNNING
RUNNING  -> WAITING_RETRY -> DISPATCH_PENDING
RUNNING  -> WAITING_INPUT -> DISPATCH_PENDING
RUNNING  -> CANCEL_REQUESTED

终态：SUCCEEDED | FAILED | CANCELLED | TIMED_OUT | REJECTED
```

Run terminal 最多一个，由 `RUN_TERMINATED` 的 unique index 和 Store 状态 CAS 双重防护。Engine 只返回 `EngineOutcome`，不能直接写 Run；正常 EngineOutcome 由 RunCoordinator 裁决，而 cancel、deadline、lease recovery/reconciliation 等命令路径可以由 Store 在权威事务中直接终态化。

### 3.4 Release 不匹配不是 Run 终态

Worker 调用 `claim_next(release_map=...)`，SQL 同时要求：

```text
run.engine = worker 支持的 engine
run.release_fingerprint = 该 engine 的 Worker fingerprint
```

所以 wrong-release Worker 根本无法领取 Run。Run 继续保持 `DISPATCH_PENDING`/可调度状态，等待 exact-release Worker；若始终没有匹配 Worker，最终由绝对 deadline 推进到 `TIMED_OUT`，而不是伪造一个 release 错误终态。

## 4. Activity：可恢复的工作单元

### 4.1 类型与当前使用方式

`ActivityType` 领域词汇包括：

```text
ENGINE_RUN, MODEL_CALL, TOOL_CALL, RETRIEVAL,
CHECKPOINT, WAIT_INPUT, FINALIZE
```

CreateRun 直接创建的父工作单元是 `ENGINE_RUN`，`logical_key=engine:0`。Tool Broker 会另外建立稳定 ToolExecution slot 及对应 Activity 事实。Activity 类型是运行时词汇，不意味着每一次内部函数调用都会建一行。

### 4.2 Activity 身份和 attempt 身份分离

```text
稳定 Activity：activity_id + run_id + logical_key + type
可变 attempt：attempt + lease_owner + lease_expires_at + fencing_token
```

`activity_id` 可由 `run_id + logical_key` 的 UUIDv5 稳定派生。重试/恢复不换逻辑 Activity，而是增加 attempt 和 fencing token，让新旧执行权可明确区分。

### 4.3 Activity 状态

```text
PENDING -> CLAIMED -> RUNNING

RUNNING -> SUCCEEDED | FAILED | CANCELLED
RUNNING -> WAITING_RETRY | WAITING_INPUT
RUNNING -> RECONCILE | MANUAL

租约恢复边界：
CLAIMED/RUNNING -> PENDING       # 仅在可安全重放时
CLAIMED/RUNNING -> RECONCILE    # 存在不确定 effect
```

### 4.4 Claim、lease 和 fencing

`claim_next()` 在同一写事务内把 Activity 改为 `CLAIMED`，设置 lease，增加 attempt/fencing，并推进 Run。Coordinator 在执行 Engine 之前再把 Activity `CLAIMED -> RUNNING`。

attempt-owned 的 Event、Checkpoint、ToolExecution，以及 Coordinator 从该 attempt 发起的终态写入，都必须带 fencing token。cancel、deadline、lease recovery/reconciliation 等 Store 命令事务依靠 Run/Activity 状态 CAS 与 revision 裁决，不借用旧 attempt 的 fence。lease 使崩溃 Worker 的任务最终能重新领取，fencing 则阻止老 Worker 苏醒后晚到提交。

## 5. 三层与 Canonical history

Conversation history 的权威来自 `compile_history()` 读取的 committed events：

```text
USER_MESSAGE_COMMITTED
+ 只有成功 Run 的 ASSISTANT_MESSAGE_COMMITTED
```

不会进入下一 Run history 的东西：

- 失败 attempt 的 partial output delta。
- SSE 客户端曾经收到但未成功 terminalize 的正文。
- ADK 每 attempt 的 InMemorySession。
- Native kernel 进程内 message list。
- Trace 或日志。

`plan_execute` 和 `agent_loop` 的 `AdkEngineAdapter` 每个 attempt 创建临时 ADK session，把 canonical history 重放进去。`native_loop` 的 `NativeLoopAdapter` 直接编译 canonical history/current input/附件，不经过 ADK session/merge queue。

## 6. 三层与 Checkpoint

Checkpoint 归属 `run_id + activity_id`，按该 Run 的 checkpoint revision 序列 append-only，并用 `expected_revision` 做 CAS。它不存可由 Run 派生的 release fingerprint，也不存额外 schema version 或独立引擎状态外部指针。

```text
CheckpointRecord
  checkpoint_id
  run_id
  activity_id
  revision
  working_state
  engine_state
  created_at
```

Native 恢复有两个明确分支：

- `checkpoint is None`：允许从 canonical history + current input 初始化。
- checkpoint 存在：必须通过当前唯一 typed codec 严格校验，否则 `NATIVE_CHECKPOINT_INVALID`。

不会在解码失败后回到“当作首次执行”，因为那会破坏已提交 ToolExecution 和模型轮次的因果关系。

## 7. 三层与最终 Assistant

Native 模型循环可能是：

```text
中间 assistant text
-> ToolCall batch
-> ToolResult
-> 最终 assistant text
```

因此“当次 attempt 看到的所有 delta 串联”不等于 Conversation 的 Assistant message。

Native Adapter 为每次 model generation 分配 `message_id + generation_id`，并在只有最后一个完整、非空、无 ToolCall 的 Assistant turn 上调用：

```python
RuntimeIO.set_final_assistant(text, message_id, generation_id)
```

Coordinator 成功分支优先使用该 final override。Store 把 final assistant、由 committed Evidence 派生的 citation 和成功 terminal 原子提交。只有这条 Assistant 事件会进入后续 Conversation history。

ADK Adapter 未设置 final override 时，Coordinator 保留原有语义：使用 CommittedEventSink 累计文本作为 final assistant。

## 8. 典型场景

### 8.1 新 Conversation 的首个 Run

```text
Admission transaction
  -> INSERT conversations(next_turn_seq=2)
  -> INSERT runs(turn_seq=1, DISPATCH_PENDING, frozen release)
  -> INSERT activities(ENGINE_RUN, PENDING)
  -> append USER_MESSAGE + status events
```

### 8.2 同 Conversation 第二轮

只有第一个 Run 终态后，partial unique index 才释放该 Conversation。新 Run 获得下一 `turn_seq`，并从前面的 committed user/success assistant events 编译 history。

### 8.3 Worker 在 Engine 执行中崩溃

```text
Activity RUNNING + lease 过期
-> maintenance.recover_expired()
-> 读 Run deadline/cancel 和 ToolEffect truth
-> 安全可重放：Activity -> PENDING
   或不确定副作用：Activity -> RECONCILE/MANUAL
-> exact-release Worker 才能重新 claim
```

### 8.4 Worker 丢失 attempt 所有权

stale fence、lease loss 或 checkpoint CAS 冲突被转为 `AttemptOwnershipLost`。它只终止当地 attempt，不改写 Run terminal。这保持了聚合所有权：无权 Worker 不能对 Run 做业务裁决。

## 9. 关键约束总结

| 约束 | 持久化保障 |
|---|---|
| Conversation 归属不变 | admission 校验 principal/agent |
| 同 Conversation 最多一个非终态 Run | partial unique index |
| Run 最多一个 terminal | Run CAS + terminal event unique index |
| Run 执行语义不漂移 | admission 冻结 release fingerprint |
| wrong-release Worker 不执行 | exact `(engine, fingerprint)` claim SQL |
| 过期 Worker 不写入 | lease + monotonically increasing fence |
| 历史不吸收失败 partial | committed USER + successful ASSISTANT events |
| Native 最终回答不是 delta 拼接猜测 | explicit final assistant override |

## 10. 源码阅读索引

- `agent/runtime/domain/models.py`：三层读模型和状态机。
- `agent/runtime/adapters/sqlite/schema.sql`：表、CHECK、partial unique index。
- `agent/runtime/application/admission.py`：CreateRun 命令构造。
- `agent/runtime/adapters/sqlite/store.py`：Conversation/Run/Activity 的 admission、claim、recovery、history 和 terminal 事务。
- `agent/runtime/application/coordinator.py`：Activity 所有权、EngineOutcome 到 Run terminal 的裁决。
- `agent/runtime/application/events.py`：generation 事件和 final assistant。
- `agent/runtime/adapters/adk_engines.py`：两个 ADK Engine 的 per-attempt session。
- `agent/engine/native_loop/engine.py`：Native 直接 RuntimeIO 的恢复与 final 语义。
