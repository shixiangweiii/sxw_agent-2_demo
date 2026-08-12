# ToolBroker 详解：效应感知的持久化工具调度协议

## 一、概述

**ToolBroker** 是 LLM 和外部世界之间的**可靠执行层**，负责工具调用的持久化、幂等性、超时控制和崩溃恢复。

**官方定义**（`agent/runtime/application/tool_broker.py:57-62`）：
```python
"""Effect-aware durable tool dispatch protocol.

The Store commits PREPARED + TOOL_CALL before this class invokes external
code.  Blob writes happen outside SQLite; metadata, result ref and
TOOL_RESULT are then committed together by ``settle_tool_execution``.
"""
```

**核心特性**：
- 持久化调度：工具执行状态写入数据库，支持崩溃恢复
- 效应感知：根据工具的副作用类型（effect_class）决定重试和对账策略
- 幂等保证：透传幂等键，支持安全重试
- 超时控制：deadline 向下传递，超时自动中止
- 结果管理：大结果自动存入 Artifact，返回引用

---

## 二、核心组件

### 2.1 ToolManifest：工具元数据

**定义**：`agent/runtime/domain/models.py:243-254`

```python
class ToolManifest(BaseModel):
    name: str                                    # 工具名称
    release_digest: str                          # 版本摘要（用于对账）
    effect_class: ToolEffectClass                # 效应分类
    timeout_seconds: float                       # 单次执行超时
    max_attempts: int = 1                        # 最大重试次数
    supports_idempotency: bool = False           # 是否支持幂等
    supports_reconcile: bool = False             # 是否支持对账钩子
    supports_cancel: bool = False                # 是否支持取消
    result_policy: str = "INLINE_OR_ARTIFACT"    # 结果存储策略
    concurrency_safe: bool = False               # 是否支持并发执行
    exclusive_resources: tuple[str, ...] = ()    # 独占资源列表
```

**关键字段**：
- `effect_class`：决定重试策略（见第三节）
- `supports_idempotency`：幂等工具必须透传 `idempotency_key`
- `supports_reconcile`：非幂等副作用工具若要自动恢复，必须提供对账钩子；缺失时进入 `MANUAL_REQUIRED`

---

### 2.2 ToolEffectClass：效应分类

**定义**：`agent/runtime/domain/models.py:95-99`

```python
class ToolEffectClass(StrEnum):
    READ_ONLY = "READ_ONLY"                      # 只读，可安全重试
    IDEMPOTENT_EFFECT = "IDEMPOTENT_EFFECT"      # 幂等副作用，透传幂等键
    NON_IDEMPOTENT_EFFECT = "NON_IDEMPOTENT_EFFECT"  # 非幂等副作用，需谨慎
    UNKNOWN_EFFECT = "UNKNOWN_EFFECT"            # 未知效应，保守处理
```

**处理策略对比**：

| Effect Class | 可重试 | 幂等键 | 对账钩子 | 失败处理 |
|---|---|---|---|---|
| `READ_ONLY` | ✅ 安全重试 | ❌ 不需要 | ❌ 不需要 | 直接失败 |
| `IDEMPOTENT_EFFECT` | ✅ 安全重试 | ✅ 必须透传 | ⚠️ 可选 | 对账确认 |
| `NON_IDEMPOTENT_EFFECT` | ❌ 不透明重试 | ⚠️ 可选 | 自动恢复需要 | 对账或人工 |
| `UNKNOWN_EFFECT` | ❌ 不透明重试 | ❌ 不需要 | ⚠️ 可选 | 对账或人工 |

**注册校验**（`tool_broker.py:85-91`）：
```python
def register(self, manifest: ToolManifest, executor: ToolExecutor, ...):
    if manifest.effect_class is ToolEffectClass.IDEMPOTENT_EFFECT and not manifest.supports_idempotency:
        raise ValueError(f"{manifest.name}: IDEMPOTENT_EFFECT requires supports_idempotency")
    if manifest.supports_reconcile and reconcile is None:
        raise ValueError(f"{manifest.name}: supports_reconcile requires a hook")
    if manifest.name in self._tools:
        raise ValueError(f"duplicate tool manifest: {manifest.name}")
    self._tools[manifest.name] = _RegisteredTool(manifest, executor, reconcile)
```

---

### 2.3 ToolCallContext：执行上下文

**定义**：`tool_broker.py:28-46`

```python
@dataclass(frozen=True)
class ToolCallContext:
    run_id: str                          # 所属 Run ID
    parent_activity_id: str              # 父 Activity ID
    tool_activity_id: str                # 工具 Activity ID
    tool_execution_id: str               # 工具执行 ID
    idempotency_key: str                 # 幂等键（透传给下游）
    deadline_at_ms: int                  # 截止时间（绝对 UTC）
    attempt: int                         # 当前尝试次数
    clock: Clock                         # 时钟（用于计算剩余时间）
    prior_result_ref: str | None         # 上次结果引用（恢复场景）
    prior_external_object_id: str | None # 上次外部对象 ID
    prior_preview: Any | None            # 上次预览
    prior_error_code: str | None         # 上次错误码
    prior_error_message: str | None      # 上次错误消息
    
    @property
    def remaining_ms(self) -> int:
        """剩余时间预算"""
        return max(0, self.deadline_at_ms - self.clock.now_ms())
```

**用途**：
- 传递给 `executor` 和 `reconcile` 钩子
- 提供执行上下文（run_id、activity_id 等）
- 提供剩余时间预算（`remaining_ms`）
- 提供幂等键（`idempotency_key`）

---

### 2.4 ToolResultEnvelope：执行结果

**定义**：`agent/runtime/domain/models.py:257-282`

```python
class ToolResultEnvelope(BaseModel):
    status: ToolResultStatus             # 执行状态
    preview: Any | None                  # 预览（小结果）
    result_ref: str | None               # 结果引用（大结果的 Artifact ID）
    error_code: str | None               # 错误码
    error_message: str | None            # 错误消息
    external_object_id: str | None       # 外部对象 ID（如工单号）
    pending_input: dict[str, Any] | None # 中断时的待处理输入

class ToolResultStatus(StrEnum):
    SUCCESS = "SUCCESS"                  # 成功
    FAILURE = "FAILURE"                  # 失败
    INTERRUPT = "INTERRUPT"              # 中断（等待人工输入）
    NO_OUTPUT = "NO_OUTPUT"              # 无输出
    UNKNOWN = "UNKNOWN"                  # 未知（需对账）
```

---

## 三、状态机

### 3.1 ToolEffectStatus 状态流转

**定义**：`agent/runtime/domain/models.py:102-109`

```python
class ToolEffectStatus(StrEnum):
    PREPARED = "PREPARED"                # 已准备（写入数据库）
    DISPATCHED = "DISPATCHED"            # 已派发（调用 executor）
    COMMITTED = "COMMITTED"              # 已提交（成功完成）
    FAILED = "FAILED"                    # 已失败（确认失败）
    UNKNOWN = "UNKNOWN"                  # 未知（需对账）
    RECONCILING = "RECONCILING"          # 对账中
    MANUAL_REQUIRED = "MANUAL_REQUIRED"  # 需人工介入
```

### 3.2 状态流转图

```
                    ┌──────────────┐
                    │   PREPARED   │
                    └──────┬───────┘
                           │ mark_tool_dispatched()
                           ↓
                    ┌──────────────┐
            ┌──────│  DISPATCHED  │──────┐
            │      └──────────────┘      │
            │                            │
            │ executor 成功              │ executor 失败
            ↓                            ↓
    ┌──────────────┐              ┌──────────────┐
    │  COMMITTED   │              │    FAILED    │
    └──────────────┘              └──────────────┘
            ▲                            │
            │                            │ safe_replay = false
            │                            ↓
            │                    ┌──────────────┐
            │                    │   UNKNOWN    │
            │                    └──────┬───────┘
            │                           │ reconcile()
            │                           ↓
            │                    ┌──────────────┐
            ├────────────────────│ RECONCILING  │
            │                    └──────┬───────┘
            │                           │
            │              ┌────────────┼────────────┐
            │              │            │            │
            │              ↓            ↓            ↓
            │       ┌──────────┐  ┌──────────┐  ┌──────────────┐
            │       │COMMITTED │  │  FAILED  │  │MANUAL_REQUIRED│
            │       └──────────┘  └──────────┘  └──────────────┘
            │
            └──────────────────────────────────────┘
```

### 3.3 关键状态转换

| 当前状态 | 触发条件 | 目标状态 | 方法 |
|---|---|---|---|
| `PREPARED` | 调用 `executor` 前 | `DISPATCHED` | `store.mark_tool_dispatched()` |
| `DISPATCHED` | executor 成功 | `COMMITTED` | `store.settle_tool_execution()` |
| `DISPATCHED` | executor 失败（READ_ONLY） | `FAILED` | `store.settle_tool_execution()` |
| `DISPATCHED` | executor 失败（非 READ_ONLY） | `UNKNOWN` | `store.settle_tool_execution()` |
| `UNKNOWN` | 调用 `reconcile` 前 | `RECONCILING` | `store.mark_tool_reconciling()` |
| `RECONCILING` | reconcile 成功 | `COMMITTED` | `store.settle_tool_execution()` |
| `RECONCILING` | reconcile 失败 | `FAILED` | `store.settle_tool_execution()` |
| `RECONCILING` | reconcile 不确定 | `MANUAL_REQUIRED` | `store.settle_tool_execution()` |

---

## 四、核心流程详解

### 4.1 execute：工具执行主流程

**入口**：`tool_broker.py:217-423`

```python
async def execute(
    self,
    *,
    run_id: str,
    parent_activity_id: str,
    fencing_token: int,
    logical_key: str,
    tool_name: str,
    arguments: dict[str, Any],
    deadline_at_ms: int,
    manifest_override: ToolManifest | None = None,
    executor_override: ToolExecutor | None = None,
    reconcile_override: ReconcileHook | None = None,
) -> ToolResultEnvelope:
```

**执行流程**：

```
① 获取工具注册信息
   ↓
② 计算请求摘要（sha256_json(arguments)）
   ↓
③ store.prepare_tool_execution() → 状态：PREPARED
   ↓
④ 判断是否可安全重试（safe_replay）
   ↓
⑤ 检查当前状态
   ├─ COMMITTED → 返回已提交结果
   ├─ MANUAL_REQUIRED → 返回需人工介入
   ├─ FAILED + 不可重试 → 返回失败结果
   └─ DISPATCHED/UNKNOWN/RECONCILING → 进入对账流程
   ↓
⑥ 检查尝试次数（attempt < max_attempts）
   ↓
⑦ store.mark_tool_dispatched() → 状态：DISPATCHED
   ↓
⑧ 构建 ToolCallContext
   ↓
⑨ 调用 executor(arguments, ctx)
   ├─ 成功 → _commit_result() → 状态：COMMITTED
   ├─ 超时 → _settle_dispatch_failure() → 状态：FAILED/UNKNOWN
   └─ 异常 → _settle_dispatch_failure() → 状态：FAILED/UNKNOWN
   ↓
⑩ 返回 ToolResultEnvelope
```

**关键代码片段**：

```python
# ① 获取工具
try:
    tool = self._tools[tool_name]
except KeyError as exc:
    raise RuntimeFault("TOOL_NOT_REGISTERED", f"tool is not registered: {tool_name}") from exc

# ② 计算请求摘要
request_digest = sha256_json(arguments)

# ③ 持久化 PREPARED 状态
execution = await self.store.prepare_tool_execution(
    run_id=run_id,
    parent_activity_id=parent_activity_id,
    fencing_token=fencing_token,
    logical_key=logical_key,
    tool_name=tool_name,
    release_digest=manifest.release_digest,
    effect_class=manifest.effect_class,
    request_digest=request_digest,
    request=arguments,
    supports_reconcile=manifest.supports_reconcile,
    now_ms=self.clock.now_ms(),
)

# ④ 判断是否可安全重试
frozen_effect_class = ToolEffectClass(execution["effect_class"])
safe_replay = (
    frozen_effect_class is ToolEffectClass.READ_ONLY
    or (
        frozen_effect_class is ToolEffectClass.IDEMPOTENT_EFFECT
        and manifest.supports_idempotency
        and bool(execution["idempotency_key"])
    )
)

# ⑦ 标记为 DISPATCHED
execution = await self.store.mark_tool_dispatched(
    tool_execution_id=execution["tool_execution_id"],
    parent_activity_id=parent_activity_id,
    fencing_token=fencing_token,
    now_ms=self.clock.now_ms(),
)

# ⑨ 调用 executor
ctx = ToolCallContext(
    run_id=run_id,
    parent_activity_id=parent_activity_id,
    tool_activity_id=execution["activity_id"],
    tool_execution_id=execution["tool_execution_id"],
    idempotency_key=execution["idempotency_key"],
    deadline_at_ms=deadline_at_ms,
    attempt=execution["attempt"],
    clock=self.clock,
    **_prior_context(execution),
)
timeout = min(manifest.timeout_seconds, remaining_ms / 1000)
try:
    async with asyncio.timeout(timeout):
        value = tool.executor(arguments, ctx)
        if inspect.isawaitable(value):
            value = await value
except TimeoutError as exc:
    result, execution = await self._settle_dispatch_failure(...)
except Exception as exc:
    result, execution = await self._settle_dispatch_failure(...)
else:
    result = _normalize_tool_result(value)
    if result.status not in {FAILURE, UNKNOWN}:
        return await self._commit_result(...)
```

---

### 4.2 _commit_result：提交结果

**入口**：`tool_broker.py:617-716`

**职责**：
- 处理大结果：超过 `inline_result_max_bytes`（默认 8KiB）存入 Artifact
- 处理 Evidence：knowledge_search 工具的 EvidenceSet 特殊处理
- 持久化结果：调用 `store.settle_tool_execution()` → 状态：`COMMITTED`
- 返回结果：根据 `return_full` 决定返回完整结果或预览

**关键逻辑**：

```python
async def _commit_result(self, execution, parent_activity_id, fencing_token, result, *, return_full=False):
    serialized = canonical_json(result.model_dump(mode="json")).encode("utf-8")
    
    # 大结果存入 Artifact
    if len(serialized) > self.inline_result_max_bytes:
        ref = await self.artifact_store.put_bytes(
            serialized,
            purpose=ArtifactPurpose.INTERNAL,
            media_type="application/json",
            filename=f"{execution['tool_execution_id']}.json",
        )
        result_ref = ref.artifact_id
        preview = serialized[:self.inline_result_max_bytes].decode("utf-8", errors="replace")
        stored = result.model_copy(update={"preview": preview, "result_ref": result_ref})
    else:
        stored = result
    
    # 持久化到数据库
    settled_execution = await self.store.settle_tool_execution(
        tool_execution_id=execution["tool_execution_id"],
        parent_activity_id=parent_activity_id,
        fencing_token=fencing_token,
        effect_status="COMMITTED",
        result=stored.model_dump(mode="json"),
        result_ref=result_ref,
        error=None,
        external_object_id=result.external_object_id,
        now_ms=self.clock.now_ms(),
    )
    
    return _tool_result_from_ledger(settled_execution)
```

**Artifact 存储策略**：
- `INLINE_OR_ARTIFACT`：小结果内联，大结果存 Artifact
- `ARTIFACT_BOUNDED_READ`：始终存 Artifact，返回时从 Artifact 读取

---

### 4.3 _resolve_uncertain：对账不确定状态

**入口**：`tool_broker.py:519-558`

**触发条件**：
- 状态为 `DISPATCHED`/`UNKNOWN`/`RECONCILING`
- 工具注册了 `reconcile` 钩子
- 未超过 deadline

**对账流程**：

```python
async def _resolve_uncertain(self, tool, execution, parent_activity_id, fencing_token, deadline_at_ms):
    # 前置检查
    if (
        execution["effect_status"] == "MANUAL_REQUIRED"
        or tool.reconcile is None
        or self.clock.now_ms() >= deadline_at_ms
    ):
        return None
    
    # 标记为 RECONCILING
    execution = await self.store.mark_tool_reconciling(
        tool_execution_id=execution["tool_execution_id"],
        parent_activity_id=parent_activity_id,
        fencing_token=fencing_token,
        now_ms=self.clock.now_ms(),
    )
    
    # 构建上下文
    ctx = ToolCallContext(
        run_id=execution["run_id"],
        parent_activity_id=parent_activity_id,
        tool_activity_id=execution["activity_id"],
        tool_execution_id=execution["tool_execution_id"],
        idempotency_key=execution["idempotency_key"],
        deadline_at_ms=deadline_at_ms,
        attempt=execution["attempt"],
        clock=self.clock,
        **_prior_context(execution),
    )
    
    # 调用 reconcile 钩子
    timeout = (deadline_at_ms - self.clock.now_ms()) / 1000
    try:
        async with asyncio.timeout(timeout):
            value = tool.reconcile(ctx)
            if inspect.isawaitable(value):
                value = await value
            return value
    except Exception:
        return None
```

**对账结果处理**：
- `SUCCESS`/`NO_OUTPUT` → 提交结果，状态：`COMMITTED`
- `FAILURE` → 标记失败，状态：`FAILED`
- `UNKNOWN` → 标记未知，状态：`UNKNOWN`，可能进入 `MANUAL_REQUIRED`
- 异常或超时 → 返回 `None`，进入 `MANUAL_REQUIRED`

---

### 4.4 _require_manual：进入人工介入

**入口**：`tool_broker.py:455-517`

**触发条件**：
- 对账失败或不确定
- 超过最大重试次数
- 非幂等副作用工具失败

**处理逻辑**：

```python
async def _require_manual(self, execution, parent_activity_id, fencing_token, *, code, message):
    # 构建 MANUAL_REQUIRED 结果
    manual = ToolResultEnvelope(
        status=ToolResultStatus.UNKNOWN,
        error_code=code,
        error_message=message,
    )
    
    # 持久化到数据库
    settled_execution = await self.store.settle_tool_execution(
        tool_execution_id=execution["tool_execution_id"],
        parent_activity_id=parent_activity_id,
        fencing_token=fencing_token,
        effect_status="MANUAL_REQUIRED",
        result=manual.model_dump(mode="json"),
        result_ref=manual.result_ref,
        error={"code": manual.error_code, "message": manual.error_message},
        external_object_id=manual.external_object_id,
        now_ms=self.clock.now_ms(),
    )
    
    return _tool_result_from_ledger(settled_execution)
```

**后续处理**：
- 通过 `tool_reconciliation` signal 进行人工对账
- 支持三种操作：`mark_committed`、`mark_failed`、`reconcile`

---

## 五、注册工具示例

### 5.1 READ_ONLY 工具

```python
from agent.runtime.domain.models import ToolManifest, ToolEffectClass

manifest = ToolManifest(
    name="knowledge_search",
    release_digest="abc123...",
    effect_class=ToolEffectClass.READ_ONLY,
    timeout_seconds=10.0,
    max_attempts=3,
    supports_idempotency=False,
    supports_reconcile=False,
    result_policy="INLINE_OR_ARTIFACT",
    concurrency_safe=True,
)

async def knowledge_search_executor(arguments: dict, ctx: ToolCallContext):
    query = arguments["query"]
    # 调用 ARAG 检索
    results = await arag_client.search(query)
    return ToolResultEnvelope(
        status=ToolResultStatus.SUCCESS,
        preview={"hits": results},
    )

broker.register(manifest, knowledge_search_executor)
```

---

### 5.2 IDEMPOTENT_EFFECT 工具

```python
manifest = ToolManifest(
    name="create_ticket",
    release_digest="def456...",
    effect_class=ToolEffectClass.IDEMPOTENT_EFFECT,
    timeout_seconds=30.0,
    max_attempts=3,
    supports_idempotency=True,  # 必须为 True
    supports_reconcile=False,
    result_policy="INLINE_OR_ARTIFACT",
    concurrency_safe=False,
)

async def create_ticket_executor(arguments: dict, ctx: ToolCallContext):
    # 透传幂等键
    idempotency_key = ctx.idempotency_key
    ticket_id = await ticket_system.create(
        title=arguments["title"],
        idempotency_key=idempotency_key,  # 透传给下游
    )
    return ToolResultEnvelope(
        status=ToolResultStatus.SUCCESS,
        preview={"ticket_id": ticket_id},
        external_object_id=ticket_id,
    )

broker.register(manifest, create_ticket_executor)
```

---

### 5.3 NON_IDEMPOTENT_EFFECT 工具（需对账）

```python
manifest = ToolManifest(
    name="send_email",
    release_digest="ghi789...",
    effect_class=ToolEffectClass.NON_IDEMPOTENT_EFFECT,
    timeout_seconds=20.0,
    max_attempts=1,  # 不重试
    supports_idempotency=False,
    supports_reconcile=True,  # 需要同时注册 reconcile hook；否则会进入 MANUAL_REQUIRED
    result_policy="INLINE_OR_ARTIFACT",
    concurrency_safe=False,
)

async def send_email_executor(arguments: dict, ctx: ToolCallContext):
    message_id = await email_system.send(
        to=arguments["to"],
        subject=arguments["subject"],
        body=arguments["body"],
    )
    return ToolResultEnvelope(
        status=ToolResultStatus.SUCCESS,
        preview={"message_id": message_id},
        external_object_id=message_id,
    )

async def send_email_reconcile(ctx: ToolCallContext):
    # 查询邮件是否已发送
    message_id = ctx.prior_external_object_id
    if message_id:
        status = await email_system.get_status(message_id)
        if status == "SENT":
            return ToolResultEnvelope(
                status=ToolResultStatus.SUCCESS,
                preview={"message_id": message_id},
                external_object_id=message_id,
            )
        elif status == "FAILED":
            return ToolResultEnvelope(
                status=ToolResultStatus.FAILURE,
                error_code="EMAIL_SEND_FAILED",
                error_message="email delivery failed",
            )
    # 不确定状态
    return ToolResultEnvelope(
        status=ToolResultStatus.UNKNOWN,
        error_code="EMAIL_STATUS_UNKNOWN",
        error_message="cannot confirm email delivery status",
    )

broker.register(manifest, send_email_executor, reconcile=send_email_reconcile)
```

---

## 六、与其他组件的关系

### 6.1 架构位置

```
┌─────────────────────────────────────────────────────────┐
│                    Engine Adapter                        │
│  (plan_execute / agent_loop / native_loop)               │
└────────────────────┬────────────────────────────────────┘
                     │ execute(tool_name, arguments)
                     ↓
┌─────────────────────────────────────────────────────────┐
│                      ToolBroker                          │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  │
│  │   register   │  │   execute    │  │  reconcile   │  │
│  └──────────────┘  └──────────────┘  └──────────────┘  │
└────────┬─────────────────┬─────────────────┬───────────┘
         │                 │                 │
         ↓                 ↓                 ↓
┌────────────────┐  ┌──────────────┐  ┌──────────────────┐
│  RuntimeStore  │  │ ArtifactStore│  │  External Tools  │
│  (SQLite)      │  │ (CAS)        │  │  (HTTP/gRPC/...) │
└────────────────┘  └──────────────┘  └──────────────────┘
```

### 6.2 关键依赖

| 组件 | 用途 |
|---|---|
| `RuntimeStore` | 持久化工具执行状态（`tool_executions` 表） |
| `ArtifactStore` | 存储大结果（超过 8KiB） |
| `Clock` | 计算剩余时间预算 |
| `ToolExecutor` | 实际的工具执行函数 |
| `ReconcileHook` | 对账钩子（可选） |

---

## 七、最佳实践

### 7.1 Effect Class 选择

| 工具类型 | 推荐 Effect Class | 理由 |
|---|---|---|
| 知识检索、数据库查询 | `READ_ONLY` | 无副作用，可安全重试 |
| 创建工单、发送通知（支持幂等） | `IDEMPOTENT_EFFECT` | 有副作用，但幂等键保证安全 |
| 发送邮件、支付（不支持幂等） | `NON_IDEMPOTENT_EFFECT` | 有副作用，需对账确认 |
| 第三方 API（不确定） | `UNKNOWN_EFFECT` | 保守处理，需对账或人工 |

### 7.2 幂等键透传

```python
async def executor(arguments: dict, ctx: ToolCallContext):
    # 必须透传 ctx.idempotency_key
    result = await external_api.call(
        **arguments,
        idempotency_key=ctx.idempotency_key,  # 关键！
    )
    return ToolResultEnvelope(status=ToolResultStatus.SUCCESS, preview=result)
```

### 7.3 对账钩子设计

```python
async def reconcile(ctx: ToolCallContext):
    # 1. 检查 prior_external_object_id
    if ctx.prior_external_object_id:
        status = await check_status(ctx.prior_external_object_id)
        if status == "SUCCESS":
            return ToolResultEnvelope(status=ToolResultStatus.SUCCESS, ...)
        elif status == "FAILED":
            return ToolResultEnvelope(status=ToolResultStatus.FAILURE, ...)
    
    # 2. 不确定时返回 UNKNOWN
    return ToolResultEnvelope(
        status=ToolResultStatus.UNKNOWN,
        error_code="STATUS_UNKNOWN",
        error_message="cannot confirm status",
    )
```

### 7.4 超时控制

```python
# executor 中必须检查剩余时间
async def executor(arguments: dict, ctx: ToolCallContext):
    if ctx.remaining_ms <= 0:
        return ToolResultEnvelope(
            status=ToolResultStatus.FAILURE,
            error_code="DEADLINE_EXPIRED",
            error_message="deadline expired before execution",
        )
    
    # 使用 async with asyncio.timeout 控制超时
    async with asyncio.timeout(ctx.remaining_ms / 1000):
        result = await long_running_task()
    
    return ToolResultEnvelope(status=ToolResultStatus.SUCCESS, preview=result)
```

### 7.5 大结果处理

```python
# 大结果自动存入 Artifact
async def executor(arguments: dict, ctx: ToolCallContext):
    large_result = await fetch_large_data()
    
    # ToolBroker 会自动判断大小
    # 超过 8KiB 存入 Artifact，返回 result_ref
    return ToolResultEnvelope(
        status=ToolResultStatus.SUCCESS,
        preview=large_result,  # 如果太大，会自动截断
    )
```

---

## 八、常见问题与陷阱

### 8.1 幂等键未透传

**问题**：`IDEMPOTENT_EFFECT` 工具未透传 `idempotency_key`，导致重试时产生重复副作用。

**解决**：
```python
# ❌ 错误
async def executor(arguments, ctx):
    result = await api.call(**arguments)  # 未透传幂等键

# ✅ 正确
async def executor(arguments, ctx):
    result = await api.call(**arguments, idempotency_key=ctx.idempotency_key)
```

---

### 8.2 对账钩子缺失

**问题**：`NON_IDEMPOTENT_EFFECT` 工具未提供 `reconcile` 钩子，失败后无法恢复。

**解决**：
```python
# ✅ 注册时提供 reconcile 钩子
broker.register(manifest, executor, reconcile=reconcile_hook)
```

---

### 8.3 超时未检查

**问题**：executor 未检查 `ctx.remaining_ms`，导致超期执行。

**解决**：
```python
async def executor(arguments, ctx):
    if ctx.remaining_ms <= 0:
        return ToolResultEnvelope(
            status=ToolResultStatus.FAILURE,
            error_code="DEADLINE_EXPIRED",
            error_message="deadline expired",
        )
    # 继续执行...
```

---

### 8.4 状态机混乱

**问题**：在 executor 中直接修改数据库状态，绕过 ToolBroker 的状态机。

**解决**：
- executor 只负责执行工具逻辑
- 状态变更由 ToolBroker 通过 `store.settle_tool_execution()` 管理
- 不要在 executor 中直接调用 `store` 方法

---

## 九、总结

ToolBroker 是 LLM 与外部世界之间的**可靠执行层**，核心价值在于：

1. **持久化保证**：工具执行状态写入数据库，支持崩溃恢复
2. **效应感知**：根据副作用类型决定重试和对账策略
3. **幂等控制**：透传幂等键，支持安全重试
4. **超时管理**：deadline 向下传递，超时自动中止
5. **结果管理**：大结果自动存入 Artifact，返回引用

**设计原则**：
- **显式声明**：工具必须声明 `effect_class` 和能力（幂等/对账/取消）
- **保守处理**：不确定的副作用进入 `MANUAL_REQUIRED`，等待人工介入
- **状态机驱动**：所有状态变更通过 ToolBroker 管理，保证一致性
