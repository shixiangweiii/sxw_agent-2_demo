# ToolBroker 详解：效应感知的持久化工具调度协议

本文按当前代码解释 `ToolBroker`。它不是一个普通的工具函数路由器，而是 Runtime 对“外部动作是否发生过、能否重试、结果是什么”的持久化裁决层。

建议先记住一条总不变量：

```text
每个 ToolCall 必须先持久化 stable slot 和 ToolCall 事实
→ 才允许调用对应外部 executor
→ ToolExecution/Artifact/Event 结算完成
→ 才把 ToolResult 放回模型消息
```

默认 `native_early_tool_dispatch=off` 还要求完整 batch 原子 PREPARE 后才 dispatch。`experimental_heuristic` 是明确的例外：安全只读调用可以逐 slot PREPARE 后提前执行，不等待完整 batch，但仍不能绕过 durable slot。

生产入口主要位于：

- `agent/runtime/application/tool_catalog.py`
- `agent/runtime/application/tool_outputs.py`
- `agent/runtime/application/tool_broker.py`
- `agent/runtime/adapters/brokered_tools.py`
- `agent/runtime/adapters/sqlite/store.py`
- `agent/runtime/domain/models.py`

文中不使用易漂移的源码行号；请按类名和方法名定位。

## 1. ToolBroker 解决什么问题

LLM 只给出一个工具调用意图，例如：

```json
{
  "name": "knowledge_search",
  "arguments": {"query": "Tool Broker 是什么"}
}
```

但生产执行还必须回答：

- 崩溃发生在外部请求前还是请求后？
- 重启后同一模型位置是否仍是同一个工具和参数？
- 这个工具能否安全重试？
- 超时后下游究竟成功了还是没成功？
- 大结果和 Evidence 的权威字节在哪里？
- 旧 Worker 的迟到结果是否仍有写入资格？
- 并发工具完成顺序不同，如何保持模型历史和 durable event 顺序稳定？

这些问题不能由模型消息、HTTP 超时、Trace 或进程内 task 回答。当前实现的权威分别是：

| 事实 | Authority |
|---|---|
| 调用身份与效应状态 | `tool_executions` |
| ToolCall/ToolResult 公开投影 | committed `run_events` |
| 大结果字节 | Artifact SHA-256 CAS |
| Evidence | 严格 `EvidenceSet` Artifact |
| 当前执行资格 | Activity lease、fencing token、revision |
| 工具版本与策略 | immutable `ToolCatalog` + Run release |

因此，`ToolBroker` 的职责是把一次不可靠的外部调用变成一条可恢复、可审计、效应感知的持久化协议。

## 2. Worker 启动时先冻结唯一 ToolCatalog

生产 Worker 在 `agent/runtime/worker/main.py::build_worker` 中按以下顺序启动：

```text
加载 builtin / Skill / Claude Skill / A2A / read_artifact
→ 收集 native_loop 与 agent_loop 的公开工具面
→ 校验两者 name/description/schema/effect policy 完全一致
→ build_runtime_tool_catalog(...)
→ register_tool_catalog(broker, catalog)
→ 用 catalog digest 计算三份 ReleaseManifest
→ 创建两个 AdkEngineAdapter 与一个 NativeLoopAdapter
→ 原子激活三份 release
```

### 2.1 ToolBinding

`ToolBinding` 冻结一个工具的完整运行契约：

- `name`
- `description`
- Draft 2020-12 object 参数 schema
- `ToolManifest`
- executor
- 协议专属 result adapter
- implementation identity

`ToolManifest` 进一步声明：

- `release_digest`
- `effect_class`
- timeout、max attempts
- idempotency、reconcile、cancel 能力
- result policy
- `concurrency_safe`
- `exclusive_resources`

每个 binding 有独立 tool release digest；整个目录又有确定性的 catalog digest。catalog digest 被写进 release manifest，所以不同工具声明、实现、结果适配器或策略的 Worker，不会被视为同一 release。

### 2.2 fail-fast 规则

以下情况直接阻止 Worker 启动，不会跳过单个坏工具：

- 重复名称；
- 空 description；
- 非 object 根 schema 或没有 `properties`；
- 非法 Draft 2020-12 schema；
- schema 含不可序列化值；
- executor/result adapter 不可调用；
- tool release digest 非 SHA-256；
- 非 READ_ONLY 工具却声明 `concurrency_safe=true`；
- 重复 exclusive resource；
- agent_loop/native_loop 公开声明或结果协议不一致。

可选远程 Skill/A2A 连接失败仍可按既有 best-effort 返回空目录；但一旦目录请求成功，任何畸形条目都必须使启动失败。这不是旧 schema 兼容层，当前代码只接受唯一 current catalog。

## 3. Broker 内部唯一输出契约

executor 的原始返回值不能直接进入 Broker 账本。所有协议入口都必须先得到：

```text
ToolExecutionOutput
├─ result: ToolResultEnvelope
└─ evidence: EvidenceSet | null
```

定义位于 `agent/runtime/domain/models.py`，适配位于 `agent/runtime/application/tool_outputs.py`。

### 3.1 协议适配只发生一次

| 工具来源 | result adapter | 责任 |
|---|---|---|
| builtin/普通 Python | `plain_json_output` | JSON → SUCCESS；`None` → NO_OUTPUT |
| Skill Center | `skill_center_output` | 只在这里解释 `isError/errorCode/content` |
| Claude Skill | `claude_skill_output` | 只在这里解释 `SkillCallResult` |
| A2A | `a2a_output` | 只在这里解释 A2A error 对象 |

Broker 核心不再猜测任意 dict 的别名。即使 executor 返回已经构造好的 Pydantic 对象，边界仍会重新验证，防止 `model_construct` 等绕过校验。

严格 JSON 约束会拒绝：

- bytes、tuple；
- 非字符串 object key；
- NaN、Infinity；
- 非法 UTF-8；
- 循环引用；
- 与当前 DTO 不匹配的额外字段。

违反结果契约统一为 `TOOL_RESULT_CONTRACT_INVALID`，不能被包装成普通的、可继续推理的工具错误。

### 3.2 ToolResultEnvelope

状态词汇只有：

| 状态 | 含义 |
|---|---|
| `SUCCESS` | 已确认成功 |
| `NO_OUTPUT` | 已确认执行完成且无正文 |
| `FAILURE` | 已确认失败，必须有 code/message |
| `INTERRUPT` | 等待输入，必须有 `pending_input` |
| `UNKNOWN` | 外部效应不确定，必须有 code/message |

`result_ref` 是小写 SHA-256 Artifact ID。`NO_OUTPUT` 不允许携带 preview/ref；`FAILURE` 和 `UNKNOWN` 必须说明稳定错误码与消息。

### 3.3 Evidence 不允许补造

`EvidenceSet`/`EvidenceItem` 使用 `strict=True + extra="forbid"`。producer 必须给出完整：

- query/query id；
- run/activity/tool execution identity；
- principal、dataset scope、scope；
- document/version/index/content hash；
- page/span/source/score；
- retrieval status、rewrites、degraded reasons、retrieved time。

Broker 只核对身份和内部一致性，不从普通 hits 猜 provenance，不识别任何隐藏 Evidence 载体，也不会填合成的默认版本或 scope。缺失或矛盾统一报 `EVIDENCE_CONTRACT_INVALID`。

## 4. stable slot：恢复身份不是 provider call id

模型供应商给出的 function-call id 只用于本 attempt 的关联，不能成为跨 attempt 的业务身份。当前稳定 slot 由引擎位置确定：

```text
Native: native:turn:{turn_ordinal}:call:{call_ordinal}
ADK:    adk:turn:{turn_ordinal}:call:{call_ordinal}
```

slot 冻结：

- logical key；
- tool name；
- normalized arguments 的 request digest；
- tool release digest；
- effect class。

恢复或重放时，只要同一 slot 的名称、参数摘要、release 或 effect 漂移，就以 `TOOL_REPLAY_MISMATCH` fail-closed。Broker 不会用“参数看起来相似”或 provider id 猜测是同一次调用。

## 5. PREPARE 是外部执行的硬屏障

批入口是：

```python
await broker.prepare_batch(
    run_id=...,
    parent_activity_id=...,
    fencing_token=...,
    calls=(ToolBatchCall(...), ...),
)
```

Store 在一个短 `BEGIN IMMEDIATE` 中完成：

1. 校验当前 Activity lease/fencing；
2. 为每个 stable slot 创建或核对 `ToolExecution`；
3. 写 `PREPARED` 状态；
4. 追加对应 `TOOL_CALL_COMMITTED` 事件；
5. 整批提交，失败则整批回滚。

只有事务成功后，`PreparedToolExecution` 才会发布给执行层。`execute_prepared` 再次从 ledger 核对 slot 与 catalog，然后才可能进入外部 executor。

这条屏障保证：

```text
看不到 ToolCall 事实 → 外部动作一定尚未被本批派发
```

反过来并不成立：崩溃可能发生在 `DISPATCHED` 后、结果结算前，所以需要 effect/reconcile 状态机。

## 6. effect-aware 状态机

`ToolEffectStatus` 的核心状态为：

```text
PREPARED
  → DISPATCHED
      → COMMITTED
      → FAILED
      → UNKNOWN → RECONCILING → COMMITTED | FAILED | MANUAL_REQUIRED
```

实际恢复还可能直接读取已经 `COMMITTED`/`FAILED` 的 slot，而不重新派发。

### 6.1 不同效应的策略

| effect class | 透明重试 | 关键约束 |
|---|---|---|
| `READ_ONLY` | 可以 | 仍受 max attempts、deadline、cancel 限制 |
| `IDEMPOTENT_EFFECT` | 可以 | manifest 必须声明 idempotency，稳定 key 必须透传下游 |
| `NON_IDEMPOTENT_EFFECT` | 不可以 | DISPATCHED 后不确定必须 reconcile 或人工 |
| `UNKNOWN_EFFECT` | 不可以 | 采用最保守策略，不能赌“应该没成功” |

`ToolCallContext.idempotency_key` 来源于持久化 ToolExecution，不会随 Worker attempt 改变。下游 executor 对幂等副作用必须真正使用它；仅在本地算一个 hash 不构成下游幂等。

### 6.2 timeout 不是“失败证明”

在外部请求派发之后出现 timeout、断连或 ACK 丢失，只能证明本地不知道结果，不能证明下游没有执行。因此：

- 有 reconcile hook：进入查询式对账；
- 无 hook或仍不确定：进入 `MANUAL_REQUIRED`；
- 不能透明重试非幂等动作。

人工处置只接受严格 `tool_reconciliation` signal：`mark_committed`、`mark_failed`、`reconcile`。它与 Tool Activity、ToolExecution Event、父 Run 推进在短事务内完成；人工确认失败是 sticky 的。

## 7. 并发执行与有序结算是两件事

Native 对完整 batch 中满足以下条件的调用允许并发执行：

- `READ_ONLY`；
- `concurrency_safe=true`；
- 没有独占资源冲突。

副作用或 UNKNOWN 工具串行。并发上限由 `native_max_tool_concurrency` 控制。

即使外部 executor 并发，模型结果和 durable settlement 仍必须按 call ordinal 排序。`ToolSettlementOrder`/`ToolSettlementTurn` 实现这个 gate：

```text
call 0 executor ─────────────完成慢────┐
call 1 executor ──完成快──等待 ordinal 0│
                                      ▼
durable settle: call 0 → call 1
model messages: call 0 → call 1
```

首次 durable settle 前，每个调用等待自己的 turn；一次调用的 retry/manual 流程在其 turn 内完成。cancel、ownership loss 或控制故障会 abort 整个 gate，唤醒并终止所有 waiter，避免孤儿 task。

## 8. 结算、Artifact 与恢复

### 8.1 小结果

小结果以有界 preview 写入 ToolExecution，并追加 `TOOL_RESULT_COMMITTED`。事件是公开投影，ToolExecution ledger 才是 effect authority。

### 8.2 普通大结果

Broker 把完整 current `ToolResultEnvelope` 写入 Artifact CAS：

```text
temp bytes
→ SHA-256/size
→ fsync + atomic rename + fsync dir
→ Artifact metadata/link
→ ledger 保存有界 preview + result_ref + full_result_ref 标记
→ TOOL_RESULT_COMMITTED
```

恢复时 `materialize_committed_result` 从 CAS 分片读取、校验 digest/size/media type、按当前 DTO 解析完整 envelope，并与 ledger 的状态字段核对。它不会把被截断的 ledger preview 当作模型恢复输入，也不会再次调用原 executor。

### 8.3 read_artifact

`read_artifact` 是特殊的有界读取工具。恢复时根据已提交 ToolExecution 的原始请求，通过已注册 current catalog executor 重新物化该切片；它不会把大文件复制进 checkpoint。

### 8.4 Evidence

EvidenceSet 作为独立 Artifact/索引事实在 Broker 结算时持久化。citation 不在结算事务中生成；成功收口时，`store.finalize_success()` 才根据最终回答的引用标记和 committed Evidence index 派生 `CITATION_SET_COMMITTED`。Event、Checkpoint、Trace 只携带有界信息或引用，不能成为大结果/Evidence 的第二份事实源。

## 9. Native 与 ADK 如何接入 Broker

### 9.1 Native 生产默认：`off`

`NativeLoopAdapter` 直接持有 `RuntimeIO` 和 mandatory Broker。默认时序是：

```text
provider 显式结束完整 model stream
→ 严格校验 ToolCall batch
→ MODEL_RESPONSE_COMMITTED checkpoint
→ prepare_native_batch（整批原子 PREPARE）
→ TOOL_BATCH_COMMITTED checkpoint
→ begin_native_settlement_batch
→ 外部执行（安全只读可并发）
→ Broker 按 ordinal 结算
→ TOOL_RESULT_COMMITTED checkpoint
→ NEXT_TURN
```

Native 不经过 ADK `RunContext`、旧的 runner merge queue 或 authority 路由。Skill 进度仍通过 awaited RuntimeIO sink 实时提交，最终 ToolResult 仍一次性权威结算。

### 9.2 Native 实验模式

`experimental_heuristic` 保留基于流式 fragment 的提前派发，但不属于默认生产保证：

- 只允许已评审的安全 READ_ONLY；
- 每个 early call 仍必须先建立 stable slot 和 PREPARED/ToolCall 事实；
- 使用固定 worker + 有界队列并受全局并发/参数/调用数限制；
- 完整 batch 到达后再次核对；后续 fragment 漂移报 `TOOL_REPLAY_MISMATCH`；
- EOF、cancel、lease loss 后必须关闭 provider 并 await 所有 worker。

`provider_block_complete` 是未来接口；当前 OpenAI-compatible client 不声明该 capability，配置此值时 Worker 在 release 激活前以 `EARLY_DISPATCH_CAPABILITY_UNAVAILABLE` 启动失败。

### 9.3 两个 ADK 引擎

`AdkToolBatch` 在 ADK 的完整非 partial model response 回调中收集整批 function call，先 flush 前置文本，再 `prepare_batch`。`BrokeredAdkTool` 只允许执行已经关联到 stable slot 的调用。

保留的 `ToolBroker.execute(...)` 是 ADK 单调用包装：内部仍遵守 PREPARE/execute-prepared 协议，不代表 ADK 可以绕过 batch 屏障。

## 10. 取消、deadline 与 ownership fault

所有 deadline 都是 Run 的绝对 UTC 时间；`ToolCallContext.remaining_ms` 只是对同一绝对 deadline 的计算，不会在每层重开一个完整 timeout。

以下控制故障必须向 Worker 冒泡，不能变成可喂回模型的普通 ToolResult：

- stale fencing；
- lease loss；
- checkpoint CAS conflict；
- `AttemptOwnershipLost`；
- `TOOL_REPLAY_MISMATCH`；
- ToolResult/Evidence contract fault。

cancel、deadline、GeneratorExit、Adapter 异常或 ownership loss 时，Native 会关闭 provider stream，取消并 await 工具 task、HTTP 调用与 Skill 子进程。旧 Worker 的迟到结算会被 fencing 拒绝。

## 11. 一次 Native 工具轮次的完整时序

```text
NativeLoopAdapter       RuntimeIO/Store        ToolBroker          Executor/Artifact
       │                       │                    │                      │
       │ MODEL_RESPONSE ckpt   │                    │                      │
       ├──────────────────────>│ COMMIT             │                      │
       │                       │                    │                      │
       │ prepare_batch                              │                      │
       ├───────────────────────────────────────────>│                      │
       │                       │<─ PREPARED + TOOL_CALL batch txn ────────│
       │<───────────────────────────────────────────┤                      │
       │ TOOL_BATCH ckpt       │                    │                      │
       ├──────────────────────>│                    │                      │
       │                       │                    │                      │
       │ execute_prepared (N)                       │──并发安全只读────────>│
       │                       │                    │<────raw result────────│
       │                       │                    │ protocol adapter       │
       │                       │                    │ strict output/evidence │
       │                       │                    │ Artifact if large      │
       │                       │<─ ordinal settle + TOOL_RESULT txn ──────│
       │<───────────────────────────────────────────┤ ToolResultEnvelope   │
       │ TOOL_RESULT/NEXT ckpt│                    │                      │
       ├──────────────────────>│                    │                      │
       │ 下一次 model request  │                    │                      │
```

SQLite 写事务中不会等待模型、工具、网络或文件系统；外部工作与 blob 写入在事务外，只有权威 metadata/ledger/event 在短事务中结算。

## 12. 阅读与排障顺序

建议按以下顺序阅读：

1. `agent/runtime/domain/models.py`：effect/result/evidence DTO；
2. `agent/runtime/application/tool_catalog.py`：启动期目录不变量；
3. `agent/runtime/application/tool_outputs.py`：四种协议的唯一适配点；
4. `agent/runtime/application/tool_broker.py`：prepare、execute、reconcile、materialize；
5. `agent/runtime/adapters/brokered_tools.py`：Native/ADK bridge；
6. `agent/runtime/adapters/sqlite/store.py`：ToolExecution 事务；
7. `agent/engine/native_loop/engine.py`：生产 Native 调度顺序；
8. `tests/reliability/test_brokered_tool_adapters.py` 及 Native recovery/RuntimeIO 测试：冻结不变量。

排障时优先查看 ToolExecution 的 stable slot、effect status、effect revision、request/release digest、Activity fencing，再看 committed events。Trace 只用于诊断，不能反向裁决工具是否执行过。

## 13. 当前能力边界

当前 SQLite + 本机 Artifact CAS 能保证本机多进程恢复，不等于跨主机 HA。真正跨节点部署至少需要共享事务数据库、共享对象存储、跨节点 lease/fencing 与一致的 release/catalog 发布机制。

这个边界不影响本文的不变量：无论底层以后换成 PostgreSQL、对象存储还是工作流系统，都必须继续保持“先持久化稳定意图、效应感知执行、权威结算、有序恢复”。
