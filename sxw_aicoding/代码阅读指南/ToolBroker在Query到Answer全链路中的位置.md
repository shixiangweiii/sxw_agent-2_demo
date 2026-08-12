# ToolBroker 在 Query 到 Answer 全链路中的位置

本文基于以下两份文档整理与补充：

- 《Query 到 Answer 全链路代码阅读指南》（`sxw_aicoding/代码阅读指南/全链路整理/Query到Answer全链路代码阅读指南.md`）
- 《ToolBroker 详解：效应感知的持久化工具调度协议》（`sxw_aicoding/代码阅读指南/ToolBroker 详解：效应感知的持久化工具调度协议.md`）

---

## 一、一句话定位

**ToolBroker 位于全链路阶段五「Engine 执行 → Event 产出」的内部，是 Engine 调用外部工具时的可靠执行层。**

它把 LLM 生成的 `tool_call` 意图，转换为一次**持久化、可重试、可对账、崩溃可恢复**的外部动作。ToolBroker/Store 在准备和结算时提交 `tool_executions` 账本及对应的公开 `TOOL_CALL_COMMITTED`/`TOOL_RESULT_COMMITTED` 投影；结果仍会以 `ToolResultEnvelope` 返回 Engine，但已由 Broker 拥有的事件不会再次经过 `CommittedEventSink`，最终由 SSE 从 `run_events` 推送给前端。

---

## 二、在 7 阶段全链路中的位置

| 阶段 | 名称 | ToolBroker 是否参与 | 说明 |
|---|---|---|---|
| 一 | HTTP 入口 → CreateRun | 否 | 仅做请求校验与幂等键检查 |
| 二 | Admission → SQLite 事务 | 否 | 创建 runs、activities、events |
| 三 | Worker 领取 Claim | 否 | claim_next 与 lease/fencing |
| 四 | RunCoordinator 执行 | 间接 | 注入 `ToolBroker` 到 `RunContext`，但不直接调用 |
| **五** | **Engine 执行 → Event 产出** | **核心** | **LLM 产生 tool_call → ToolBroker 执行 → 产出 tool_result/text** |
| 六 | SSE 推送 → 前端消费 | 间接 | ToolBroker 产生的事件经 SSE 推送 |
| 七 | 前端渲染 → 终态 | 间接 | 前端根据 tool_result 等事件渲染 |

ToolBroker 完全运行在 **Worker 进程**内部，API 进程不直接感知它的存在。

---

## 三、阶段五调用栈中的精确位置

阶段五的入口是 `LegacyEngineAdapter.execute()`（`agent/runtime/adapters/legacy_engines.py:65`）。其内部调用链如下：

```text
LegacyEngineAdapter.execute(request, io)
├─ 创建 ADK SessionService（per-attempt）
├─ 编译 canonical_history → ADK session events
├─ 构造 RunContext
│   ├─ tool_broker: ToolBroker              <-- 由 Worker 注册时注入
│   ├─ fencing_token                        <-- 来自 activity 的 fencing_token
│   ├─ release_fingerprint                  <-- 用于 release 兼容性校验
│   ├─ runtime_io: CommittedEventSink       <-- 事件出口
│   └─ engine_checkpoint / runtime_working_state
├─ engine = build_engine(context, "native_loop")
├─ async for event in engine.run_stream(rc):
│   ├─ Broker-owned tool event → io.force_flush()
│   └─ Engine-owned event → io.emit(event_type, data)
│       └─ 聚合 100ms / 2048 bytes → append_events()
└─ 返回 rc.engine_outcome
```

当 Engine 内部产生一次 tool_call 时，会走到：

```text
Engine 内部 tool_call 处理
    │
    ▼
ToolBroker.execute(
    run_id=...,
    parent_activity_id=...,
    fencing_token=...,          <-- 与 activity 的 fencing_token 一致
    logical_key=...,
    tool_name=...,
    arguments=...,
    deadline_at_ms=...,
)
    │
    ├─ 查注册表 _tools[tool_name]
    ├─ 计算 request_digest（sha256_json(arguments)）
    ├─ store.prepare_tool_execution()       → 状态 PREPARED
    ├─ 判断 safe_replay（依 effect_class）
    ├─ store.mark_tool_dispatched()         → 状态 DISPATCHED
    ├─ 构造 ToolCallContext
    ├─ 调用实际 executor(arguments, ctx)
    │   ├─ 成功 → _commit_result()          → 状态 COMMITTED
    │   ├─ 失败/超时 → _settle_dispatch_failure() → FAILED/UNKNOWN
    │
    └─ 返回 ToolResultEnvelope
```

返回的 `ToolResultEnvelope` 被 Engine 消费，并生成 `tool_result` 草稿。对于已由 Broker 执行并结算的注册工具，`LegacyEngineAdapter` 识别其 `authority != "engine"`，只执行 `io.force_flush()` 后跳过 `io.emit()`；对应的公开 `TOOL_RESULT_COMMITTED` 已在 Broker 的结算事务中写入 `run_events`。只有未知工具或参数解析失败等 `authority="engine"` 投影才由 `CommittedEventSink.emit("tool_result", ...)` 负责写入。

---

## 四、与全链路时序图的对应关系

全链路时序图中这一段：

```text
浏览器                  API 进程(:8000)           SQLite(runtime.db)         Worker 进程
 │                         │                        │                        │
 │<─SSE id:5 event:text────│                        │                        │
 │  delta: "混合召回是..."  │                        │                        │
 │<─SSE id:6 event:tool_call│                       │                        │
 │<─SSE id:7 event:tool_result                       │                        │
```

其内部实际发生的是：

1. Engine 生成 `tool_call` 意图，任何 `TOOL_CALL_COMMITTED` 出现前都必须先 `force_flush()` 前置 text buffer；
2. ADK 路径由 `BrokeredToolBridge` 先 `force_flush()`，再批量 `prepare_tool_execution_batch()`；`native_loop` 则先 `yield authority="broker"` 草稿，`LegacyEngineAdapter` 完成 `force_flush()` 并跳过 `io.emit()`，生成器恢复后才进入 `tool_broker.execute()` 和 `prepare_tool_execution()`；
3. ToolBroker 在 prepare 事务中提交 ToolExecution + `TOOL_CALL_COMMITTED`，事务外执行 `PREPARED → DISPATCHED → executor`，再在结算事务中提交 `COMMITTED/FAILED/UNKNOWN` 与 `TOOL_RESULT_COMMITTED`；
4. 结果返回 Engine，适配器跳过 Broker-owned `tool_result` 草稿。未知工具/参数错误等没有 ToolExecution 的 engine-owned 事件，才经 `CommittedEventSink` 写入。

因此，`tool_call` 与对应 `tool_result` 标记了 ToolBroker 执行周期的两端；多工具并发或 Activity 状态事件可能插入其间，SSE 不保证二者在 seq 上相邻，应使用 `tool_execution_id`/stable logical key 做关联。

---

## 五、ToolBroker 子流程详解

### 5.1 核心数据结构

#### ToolManifest（`agent/runtime/domain/models.py:243-254`）

```python
class ToolManifest(BaseModel):
    name: str
    release_digest: str
    effect_class: ToolEffectClass
    timeout_seconds: float
    max_attempts: int = 1
    supports_idempotency: bool = False
    supports_reconcile: bool = False
    supports_cancel: bool = False
    result_policy: str = "INLINE_OR_ARTIFACT"
    concurrency_safe: bool = False
    exclusive_resources: tuple[str, ...] = ()
```

Engine 在构造 `RunContext` 时，会携带一组已注册的 `ToolManifest`。ToolBroker 根据 `effect_class` 决定工具的可重试性与对账策略。

#### ToolCallContext（`agent/runtime/application/tool_broker.py:28-46`）

```python
@dataclass(frozen=True)
class ToolCallContext:
    run_id: str
    parent_activity_id: str
    tool_activity_id: str
    tool_execution_id: str
    idempotency_key: str
    deadline_at_ms: int
    attempt: int
    clock: Clock
    prior_result_ref: str | None
    prior_external_object_id: str | None
    prior_preview: Any | None
    prior_error_code: str | None
    prior_error_message: str | None

    @property
    def remaining_ms(self) -> int:
        return max(0, self.deadline_at_ms - self.clock.now_ms())
```

`ToolCallContext` 是 executor 与 reconcile 钩子能看到的全部上下文，其中：

- `idempotency_key`：必须透传给下游幂等接口；
- `remaining_ms`：剩余 deadline 预算；
- `prior_*`：崩溃恢复时从 `tool_executions` 表中重放的上次执行状态。

### 5.2 状态机

```python
class ToolEffectStatus(StrEnum):
    PREPARED = "PREPARED"
    DISPATCHED = "DISPATCHED"
    COMMITTED = "COMMITTED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"
    RECONCILING = "RECONCILING"
    MANUAL_REQUIRED = "MANUAL_REQUIRED"
```

状态流转：

```text
                    ┌──────────────┐
                    │   PREPARED   │
                    └──────┬───────┘
                           │ mark_tool_dispatched()
                           ▼
                    ┌──────────────┐
            ┌──────│  DISPATCHED  │──────┐
            │      └──────────────┘      │
            │                            │
            │ executor 成功              │ executor 失败
            ▼                            ▼
    ┌──────────────┐              ┌──────────────┐
    │  COMMITTED   │              │    FAILED    │      (READ_ONLY 可直达)
    └──────────────┘              └──────┬───────┘
            ▲                            │ safe_replay = false
            │                            ▼
            │                    ┌──────────────┐
            │                    │   UNKNOWN    │
            │                    └──────┬───────┘
            │                           │ reconcile()
            │                           ▼
            │                    ┌──────────────┐
            ├────────────────────│ RECONCILING  │
            │                    └──────┬───────┘
            │                           │
            │              ┌────────────┼────────────┐
            │              ▼            ▼            ▼
            │       ┌──────────┐ ┌──────────┐ ┌──────────────┐
            │       │COMMITTED │ │  FAILED  │ │MANUAL_REQUIRED│
            │       └──────────┘ └──────────┘ └──────────────┘
            │
            └──────────────────────────────────────┘
```

### 5.3 effect_class 与处理策略

| Effect Class | 可安全重试 | 幂等键 | 对账钩子 | 失败处理 |
|---|---|---|---|---|
| `READ_ONLY` | 是 | 不需要 | 不需要 | 直接 FAILED |
| `IDEMPOTENT_EFFECT` | 是 | 必须透传 | 可选 | 可对账 |
| `NON_IDEMPOTENT_EFFECT` | 否 | 可选 | 建议提供；缺失时无法自动对账 | 进入 `UNKNOWN` 后对账或人工 |
| `UNKNOWN_EFFECT` | 否 | 不需要 | 可选 | 对账或人工 |

对应到当前系统的工具注册：

- `READ_ONLY`：如 `knowledge_search`（ARAG 检索）；
- `IDEMPOTENT_EFFECT`：如支持幂等键的工单创建类工具；
- `NON_IDEMPOTENT_EFFECT`：如发送邮件、支付等；
- `UNKNOWN_EFFECT`：未声明的 Skill / A2A / Claude SKILL 默认归为此类。

### 5.4 结果管理

- 小结果（≤ 8KiB）：直接内联在 `ToolResultEnvelope.preview` 中；
- 大结果（> 8KiB）：序列化后写入 `ArtifactStore`，`result_ref` 指向 Artifact ID，`preview` 截断；
- Evidence：knowledge_search 返回的 EvidenceSet 有特殊处理逻辑。

---

## 六、与周边组件的关系

```text
┌─────────────────────────────────────────────────────────┐
│                    Engine Adapter                        │
│  (plan_execute / agent_loop / native_loop)               │
└────────────────────┬────────────────────────────────────┘
                     │ execute(tool_name, arguments)
                     ▼
┌─────────────────────────────────────────────────────────┐
│                      ToolBroker                          │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  │
│  │   register   │  │   execute    │  │  reconcile   │  │
│  └──────────────┘  └──────────────┘  └──────────────┘  │
└────────┬─────────────────┬─────────────────┬───────────┘
         │                 │                 │
         ▼                 ▼                 ▼
┌────────────────┐  ┌──────────────┐  ┌──────────────────┐
│  RuntimeStore  │  │ ArtifactStore│  │  External Tools  │
│  (SQLite)      │  │ (CAS)        │  │  (HTTP/gRPC/...) │
└────────────────┘  └──────────────┘  └──────────────────┘
```

| 组件 | ToolBroker 如何使用它 |
|---|---|
| `RuntimeStore` | 持久化 `tool_executions` 状态与结果 |
| `ArtifactStore` | 大结果（> 8KiB）写入 CAS |
| `Clock` | 计算 `remaining_ms`，控制超时 |
| `CommittedEventSink` | engine-owned 工具投影及其他引擎事件的出口；Broker-owned 工具事件不经此处重复写入 |
| `ToolExecutor` | 实际执行业务逻辑的函数 |
| `ReconcileHook` | 非幂等副作用工具失败后的对账钩子 |

---

## 七、可靠性保证在链路中的体现

全链路指南附录强调的可靠性原则，在 ToolBroker 中的具体落地：

| 原则 | ToolBroker 中的体现 |
|---|---|
| **先 commit，后 SSE 可见** | `prepare_tool_execution()`/`settle_tool_execution()` 在调用副作用前后提交 ToolExecution 账本及公开事件投影；结果提交后才返回 Engine |
| **lease/fencing** | 每次状态变更都校验 `fencing_token`，防止旧 Worker 过期提交 |
| **幂等** | `IDEMPOTENT_EFFECT` 工具必须透传 `idempotency_key` 给下游 |
| **稳定账本 + CAS** | `tool_executions` 保留同一 stable slot，通过 `revision`/CAS 更新效应状态；append-only 审计与 SSE 投影位于 `run_events` |
| **deadline 向下传递** | `deadline_at_ms` 在 `ToolCallContext` 中传递，`executor` 使用剩余预算而非本地重算 |
| **大结果不入 Event** | 大结果写入 Artifact，Event/Checkpoint 中只保留 `result_ref` |
| **崩溃恢复** | 从 `tool_executions` 表重放时，根据 `effect_class` 判断 safe_replay；`request_digest` 不一致时触发 `TOOL_REPLAY_MISMATCH` |

---

## 八、源码位置索引

| 功能 | 文件 | 行号 |
|---|---|---|
| ToolBroker 类定义与 execute 入口 | `agent/runtime/application/tool_broker.py` | 57 / 217 |
| ToolManifest / ToolEffectClass / ToolResultEnvelope | `agent/runtime/domain/models.py` | 95-109 / 243-282 |
| ToolCallContext | `agent/runtime/application/tool_broker.py` | 28-46 |
| 工具注册与校验 | `agent/runtime/application/tool_broker.py` | 85-91 |
| _commit_result（结果提交） | `agent/runtime/application/tool_broker.py` | 617-716 |
| _resolve_uncertain（对账） | `agent/runtime/application/tool_broker.py` | 519-558 |
| _require_manual（人工介入） | `agent/runtime/application/tool_broker.py` | 455-517 |
| RuntimeStore prepare/settle | `agent/runtime/adapters/sqlite/store.py` | 相关实现 |
| EngineAdapter 注入 ToolBroker | `agent/runtime/adapters/legacy_engines.py` | 65 附近 |
| CommittedEventSink | `agent/runtime/application/events.py` | 1 起 |

---

## 九、总结

ToolBroker 是 Query→Answer 全链路中**阶段五「Engine 执行 → Event 产出」**的核心子组件。

- **入口**：Engine 内部产生 `tool_call` 后，调用 `ToolBroker.execute()`；
- **职责**：持久化工具执行状态、按 effect_class 决定重试/对账策略、透传幂等键、控制 deadline、管理大结果 Artifact；
- **出口**：返回 `ToolResultEnvelope` 给 Engine；注册工具的 `tool_result` 公开投影已经由 Broker 结算事务写入 `run_events`，Engine-owned 异常投影才经 `CommittedEventSink` 落库，再由 SSE 推送给前端；
- **价值**：把一次普通的函数调用，提升为具备**持久化、幂等、对账、崩溃恢复、人工介入**能力的生产级工具调度协议。

因此，《ToolBroker 详解》可以视为《Query 到 Answer 全链路代码阅读指南》**阶段五的放大切片**：全链路指南回答“请求怎么从头到尾跑完”，ToolBroker 详解回答“其中一次工具调用是怎么被安全可靠地执行的”。
