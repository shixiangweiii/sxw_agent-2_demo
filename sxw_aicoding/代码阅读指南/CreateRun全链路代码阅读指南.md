# CreateRun 全链路代码阅读指南

本文档基于源码梳理从 `POST /api/v1/runs` 到 Worker 执行完成的完整服务端处理流程，包含时序图、调用链路、关键判断点和源码位置索引，帮助快速上手项目核心逻辑。

---

## 目录

- [1. 架构概览](#1-架构概览)
- [2. 全局时序图](#2-全局时序图)
- [3. 调用链路详解](#3-调用链路详解)
- [4. 源码位置索引](#4-源码位置索引)
- [5. 关键设计问题解答](#5-关键设计问题解答)
- [6. 阅读建议](#6-阅读建议)

---

## 1. 架构概览

### 1.1 四服务五进程模型

```text
┌─────────────────────────────────────────────────────────────────────────────┐
│                              Client / Web UI                                │
└─────────────────────────────────┬───────────────────────────────────────────┘
                                  │
        ┌─────────────────────────┼─────────────────────────┐
        │                         │                         │
        ▼                         ▼                         ▼
┌───────────────┐         ┌───────────────┐         ┌───────────────┐
│ Runtime API   │         │     ARAG      │         │ skill-center  │
│    :8000      │         │    :8100      │         │    :8200      │
│ (admission)   │         │ (RAG检索)     │         │ (Skill/A2A)   │
└───────┬───────┘         └───────────────┘         └───────────────┘
        │                                                   │
        │                      ┌────────────────────────────┘
        │                      │
        ▼                      ▼
┌───────────────────────────────────────────────────────────────────────────┐
│                         Runtime Worker (无HTTP端口)                        │
│    ┌─────────────────────────────────────────────────────────────────┐   │
│    │  RunCoordinator → EngineAdapter → ToolBroker / CommittedEventSink│   │
│    └─────────────────────────────────────────────────────────────────┘   │
└───────────────────────────────────────────────────────────────────────────┘
        │                      │                      │
        ▼                      ▼                      ▼
┌───────────────┐      ┌───────────────┐      ┌───────────────┐
│   runtime.db  │      │    rag.db     │      │ Artifact CAS  │
│  (SQLite WAL) │      │  (版本化文档) │      │ (SHA-256寻址) │
└───────────────┘      └───────────────┘      └───────────────┘
```

### 1.2 核心设计原则

| 原则 | 体现 |
|---|---|
| **API 与 Worker 分离** | API 只负责 admission 和 SSE，不加载 LLM；Worker 加载 LLM/工具但不监听端口 |
| **SQLite 是唯一事实源** | Run/Activity/Event/Checkpoint 全部在 `runtime.db`，无 Redis/消息队列 |
| **幂等 admission** | `Idempotency-Key` + request digest 保证同一请求不重复创建 |
| **lease/fencing 防过期执行** | Worker 用 CAS + fencing token 保证过期执行者不能提交结果 |
| **Event append-only** | Canonical Event 只追加不修改，seq 单调递增 |

---

## 2. 全局时序图

### 2.1 CreateRun 完整时序

```text
    Client                API进程(:8000)           SQLite(runtime.db)         Worker进程
      │                        │                        │                        │
      │──POST /runs───────────>│                        │                        │
      │  Idempotency-Key: xxx  │                        │                        │
      │  {engine, input, ...}  │                        │                        │
      │                        │                        │                        │
      │                        │──BEGIN IMMEDIATE──────>│                        │
      │                        │                        │                        │
      │                        │──查幂等记录────────────>│                        │
      │                        │<─无历史/已存在──────────│                        │
      │                        │                        │                        │
      │                        │──查active_releases─────>│                        │
      │                        │<─release_fingerprint────│                        │
      │                        │                        │                        │
      │                        │──校验attachment_refs───>│                        │
      │                        │<─全部存在───────────────│                        │
      │                        │                        │                        │
      │                        │──处理conversation──────>│                        │
      │                        │<─新建/复用/turn_seq+1───│                        │
      │                        │                        │                        │
      │                        │──INSERT runs───────────>│                        │
      │                        │  state=DISPATCH_PENDING │                        │
      │                        │                        │                        │
      │                        │──INSERT activities─────>│                        │
      │                        │  state=PENDING          │                        │
      │                        │                        │                        │
      │                        │──INSERT run_requests───>│                        │
      │                        │  (幂等记录)             │                        │
      │                        │                        │                        │
      │                        │──APPEND events─────────>│                        │
      │                        │  USER_MESSAGE(seq=1)    │                        │
      │                        │  STATUS:None→ACCEPTED   │                        │
      │                        │  STATUS:ACCEPTED→DISPATCH_PENDING               │
      │                        │  ACTIVITY:None→PENDING  │                        │
      │                        │                        │                        │
      │                        │──COMMIT────────────────>│                        │
      │                        │                        │                        │
      │<─202 Accepted──────────│                        │                        │
      │  {run_id, events_url}  │                        │                        │
      │                        │                        │                        │
      │                        │                        │    (250ms轮询)         │
      │                        │                        │<─────claim_next────────│
      │                        │                        │      WHERE state=PENDING│
      │                        │                        │      AND available_at<=now
      │                        │                        │                        │
      │                        │                        │──UPDATE state=CLAIMED─>│
      │                        │                        │  fencing_token+1        │
      │                        │                        │                        │
      │                        │                        │──UPDATE runs──────────>│
      │                        │                        │  state=RUNNING          │
      │                        │                        │                        │
      │                        │                        │──COMMIT───────────────>│
      │                        │                        │                        │
      │                        │                        │<─RETURN Claim──────────│
      │                        │                        │                        │
      │                        │                        │    execute_claim()      │
      │                        │                        │    ┌────────────────────┤
      │                        │                        │    │ mark_activity_running
      │                        │                        │    │ 检查release兼容性  │
      │                        │                        │    │ 编译history        │
      │                        │                        │    │ adapter.execute()  │
      │                        │                        │    │  ┌─────────────────┤
      │                        │                        │    │  │ 引擎循环执行     │
      │                        │                        │    │  │ emit events      │
      │                        │                        │    │  │ commit checkpoint│
      │                        │                        │    │  └─────────────────┤
      │                        │                        │    │ finalize_success   │
      │                        │                        │    └────────────────────┤
      │                        │                        │                        │
      │──GET /runs/{id}────────>│                        │                        │
      │<─{status: SUCCEEDED}───│                        │                        │
      │                        │                        │                        │
```

### 2.2 Worker 轮询领取流程

```text
Worker主循环                          SQLite
    │                                    │
    │──heartbeat(ACTIVE)────────────────>│
    │                                    │
    │  ┌─────────────────────────────────┐
    │  │ while not stop:                 │
    │  │   _maintenance()                │
    │  │   ├─ fire_due_timers            │
    │  │   ├─ recover_expired            │
    │  │   └─ heartbeat (每5秒)          │
    │  │                                 │
    │  │   while tasks < concurrency:    │
    │  │     claim = claim_next()  ──────┼──> SELECT + UPDATE activities
    │  │     if claim is None: break     │    WHERE state=PENDING
    │  │     create_task(_execute)       │    AND available_at<=now
    │  │                                 │    AND deadline_at>now
    │  │   wait(poll_ms=250ms)           │
    │  └─────────────────────────────────┘
    │                                    │
```

---

## 3. 调用链路详解

### 3.1 阶段一：HTTP 入口层

**文件**: `agent/runtime/api/runs.py:131-170`

```python
@router.post("", status_code=status.HTTP_202_ACCEPTED)
async def create_run(
    body: CreateRunBody,
    request: Request,
    response: Response,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict[str, Any]:
```

**调用栈**:
```text
create_run() runs.py:131
├─ Pydantic校验 CreateRunBody
│   ├─ engine: Literal["plan_execute", "agent_loop", "native_loop"]
│   ├─ input.text: min_length=1, max_length=200000
│   └─ input.attachment_refs: max_length=32
├─ get_trace_id()  ← TraceMiddleware已设置contextvar
└─ AdmissionService.create(CreateRunInput, idempotency_key)
```

**关键判断**:
- `deadline_at` 必须有 UTC offset（`field_validator` 校验）
- `trace_id` 不进 digest（诊断信号，避免重放被误判为冲突）

---

### 3.2 阶段二：Admission 应用层

**文件**: `agent/runtime/application/admission.py:50-83`

```python
class AdmissionService:
    async def create(self, request: CreateRunInput, *, idempotency_key: str) -> AdmissionResult:
```

**调用栈**:
```text
AdmissionService.create() admission.py:50
├─ 校验 idempotency_key 非空
│   └─ 空 → RuntimeFault("IDEMPOTENCY_KEY_REQUIRED", 400)
├─ 计算 deadline
│   ├─ 未提供 → now + default_deadline_ms(600s)
│   └─ deadline <= now → RuntimeFault("DEADLINE_IN_PAST", 400)
├─ 计算 request digest
│   └─ sha256_json(request.digest_payload())
│       ├─ client_request_id
│       ├─ conversation_id, principal_id, agent_id
│       ├─ engine, input.text, input.attachment_refs
│       └─ deadline_at (不含trace_id)
├─ 构造 AdmissionCommand
│   ├─ 生成 ID (UUIDv4带前缀): run_id, turn_id, request_id, cancel_token_id, input_event_id
│   └─ activity_id = UUIDv5(run_id + "engine:0")  ← 可恢复的stable slot
└─ store.admit(command)  ← 进入事务
```

---

### 3.3 阶段三：SQLite 事务层（原子提交）

**文件**: `agent/runtime/adapters/sqlite/store.py:386-548`

```python
async def admit(self, command: AdmissionCommand) -> AdmissionResult:
    async with self.db.transaction() as conn:  # BEGIN IMMEDIATE
```

**事务内执行顺序**:

```text
┌─────────────────────────────────────────────────────────────────────────────┐
│  1. 幂等查询 run_requests                                                   │
│     WHERE (principal_id, agent_id, idempotency_key)                         │
│     ├─ 命中 + digest 不同 → 409 IDEMPOTENCY_KEY_REUSE                       │
│     └─ 命中 + digest 相同 → 返回原 Run, reused=True                         │
│                                                                             │
│  2. 检查 active_releases                                                    │
│     SELECT release_fingerprint FROM active_releases WHERE engine=?          │
│     └─ 无结果 → 503 NO_ACTIVE_RELEASE                                       │
│                                                                             │
│  3. 校验 attachment_refs 存在                                               │
│     SELECT artifact_id FROM artifact_metadata WHERE artifact_id IN (...)    │
│     └─ 缺失 → 400 ARTIFACT_NOT_FOUND                                        │
│                                                                             │
│  4. 处理 conversation                                                       │
│     ├─ conversation_id is null → INSERT新conversation, turn_seq=1           │
│     ├─ 指定id但不存在 → 404 NOT_FOUND                                       │
│     └─ 已存在 → 校验(principal_id, agent_id) + UPDATE next_turn_seq+1       │
│                                                                             │
│  5. INSERT runs (state=DISPATCH_PENDING, next_seq=1, revision=0)            │
│     包含: schema_version, release_fingerprint, deadline_at, trace_id, ...   │
│                                                                             │
│  6. INSERT activities (type=ENGINE_RUN, state=PENDING, attempt=0)           │
│     activity_id = stable_id("act", run_id, "engine:0")                      │
│                                                                             │
│  7. UPDATE runs SET current_activity_id = activity_id                       │
│                                                                             │
│  8. INSERT artifact_links (去重后的 INPUT_ATTACHMENT)                       │
│     每个唯一 artifact_id 一条 link，关联 run/activity/event                 │
│                                                                             │
│  9. INSERT run_requests (幂等记录)                                          │
│     (principal_id, agent_id, idempotency_key, request_digest, run_id)       │
│                                                                             │
│  10. APPEND events (同事务，seq 连续，原子可见)                             │
│      ┌────────────────────────────────────────────────────────────────┐    │
│      │ seq=1: USER_MESSAGE_COMMITTED                                  │    │
│      │        {text, attachment_refs}                                 │    │
│      │ seq=2: RUN_STATUS_CHANGED                                      │    │
│      │        {from: None, to: ACCEPTED}                              │    │
│      │ seq=3: RUN_STATUS_CHANGED                                      │    │
│      │        {from: ACCEPTED, to: DISPATCH_PENDING}                  │    │
│      │ seq=4: ACTIVITY_STATUS_CHANGED                                 │    │
│      │        {from: None, to: PENDING, type: ENGINE_RUN}             │    │
│      └────────────────────────────────────────────────────────────────┘    │
│                                                                             │
│  11. 唯一约束检查                                                           │
│      uq_active_run_per_conversation: 同 conversation 最多一个非终态 Run     │
│      └─ 违反 → 409 CONVERSATION_BUSY                                       │
│                                                                             │
│  COMMIT (原子提交，所有写入同时可见)                                        │
└─────────────────────────────────────────────────────────────────────────────┘
```

**返回**: `AdmissionResult(run=RunRecord, reused=False)`

---

### 3.4 阶段四：Worker 轮询领取

**文件**: `agent/runtime/worker/dispatcher.py:57-89`

```python
class RuntimeWorker:
    async def run(self) -> None:
        await self.store.heartbeat_worker(...)  # 启动时写 ACTIVE
        try:
            while not self._stop.is_set():
                now = self.clock.now_ms()
                await self._maintenance(now)
                while len(self._tasks) < self.concurrency and not self._stop.is_set():
                    claim = await self.store.claim_next(...)
                    if claim is None:
                        break
                    task = asyncio.create_task(self._execute(claim))
                    self._tasks.add(task)
                    task.add_done_callback(self._tasks.discard)
                await asyncio.wait_for(self._stop.wait(), timeout=self.poll_ms / 1000)
        finally:
            await self._drain()
```

**`claim_next` SQL** (`store.py:721-805`):

```sql
UPDATE activities SET
    state='CLAIMED', attempt=attempt+1, lease_owner=?, lease_expires_at=?,
    fencing_token=fencing_token+1, revision=revision+1, updated_at=?
WHERE activity_id=(
    SELECT a.activity_id FROM activities a JOIN runs r ON r.run_id=a.run_id
    WHERE a.type='ENGINE_RUN'
      AND a.state='PENDING'
      AND a.available_at <= ?           -- 到期可领取
      AND (
        r.state='DISPATCH_PENDING'      -- 普通待执行
        OR (r.state='CANCEL_REQUESTED' AND json_extract(...) = 'reconciliation')
      )
      AND r.deadline_at > ?             -- 未过期
      AND r.engine IN (?, ?, ?)         -- Worker 支持的引擎
    ORDER BY a.available_at, a.created_at, a.activity_id  -- FIFO
    LIMIT 1
) AND state='PENDING'
RETURNING *
```

**领取后事务内**:
1. `UPDATE runs SET state='RUNNING'`
2. `APPEND events: RUN_STATUS_CHANGED(DISPATCH_PENDING → RUNNING)`
3. `RETURN Claim(run, activity)`

---

### 3.5 阶段五：RunCoordinator 执行

**文件**: `agent/runtime/application/coordinator.py:65-443`

```python
async def execute_claim(self, claim: Claim, *, worker_id: str) -> RunStatus:
    """恢复本 Run 的诊断 trace_id，再执行。"""
    with use_trace_id(claim.run.trace_id or claim.run.envelope.run_id):
        return await self._execute_claim(claim, worker_id=worker_id)
```

**调用栈**:

```text
RunCoordinator.execute_claim() coordinator.py:65
├─ use_trace_id()  ← 恢复诊断关联键（Worker 进程没有 TraceMiddleware，需要显式重建 contextvar）
└─ _execute_claim() coordinator.py:76
   │
   ├─ 1. mark_activity_running()  ← CAS + fencing
   │     UPDATE activities SET state='RUNNING' WHERE state='CLAIMED' AND fencing_token=?
   │
   ├─ 2. 检查 ToolReconciliationMarker
   │     └─ resume_payload 是 reconcile marker → 只走 query hook，不执行引擎
   │
   ├─ 3. 检查 cancel 抢占
   │     └─ marker=None + status=CANCEL_REQUESTED → finalize_failure(CANCELLED)
   │
   ├─ 4. Release 兼容性检查（本项目不提供 checkpoint 跨 release 升级路径，一律 fail closed）
   │     └─ adapter.release_fingerprint != run.envelope.release_fingerprint
   │           → 直接 finalize_failure(INCOMPATIBLE_RELEASE)，此判定不读 checkpoint，
   │             因此发生在拉取 checkpoint 之前
   │
   ├─ 5. 拉取最新 checkpoint（可能为 None，代表首次执行）
   │     Deadline 检查：claim/恢复准备可能耗时，进引擎前再校一次
   │     └─ now >= deadline_at → TIMED_OUT
   │
   ├─ 6. compile_history(run_id)  ← 从 committed events 重建对话历史
   │
   ├─ 7. 构造 EngineRunRequest + CommittedEventSink
   │     ├─ flush_ms=100, flush_bytes=2048
   │     └─ attach_trace_span(span)
   │
   ├─ 8. adapter.execute(request, io)  ← 引擎执行
   │     ├─ TimeoutError/ConnectionError → RETRYABLE_FAILURE
   │     └─ Exception → TERMINAL_FAILURE
   │
   └─ 9. 根据 outcome.kind 终结
         ├─ COMPLETED + 无 unresolved → finalize_success
         ├─ COMPLETED + 有 unresolved → wait_for_input(人工 reconcile)
         ├─ WAITING_INPUT → wait_for_input
         ├─ RETRYABLE_FAILURE + attempt<max → schedule_retry
         └─ 其他 → finalize_failure
```

> release 不一致曾经支持"从旧 checkpoint 升级到新 release"的路径（`ReleaseCompatibilityRegistry` + `CheckpointUpgrade*`），2026-08-12 的迁移机制清理提交（见 `sxw_aicoding/changelog/2026-08-12_移除migration机制与checkpoint-upgrader.md`）已整体删除：release 不一致时不再尝试升级，直接判 `INCOMPATIBLE_RELEASE`。

---

### 3.6 阶段六：LegacyEngineAdapter 执行引擎

**文件**: `agent/runtime/adapters/legacy_engines.py:46-189`

```python
class LegacyEngineAdapter:
    async def execute(self, request: EngineRunRequest, io: RuntimeIO) -> EngineOutcome:
```

**调用栈** (以 `native_loop` 为例):

```text
LegacyEngineAdapter.execute() legacy_engines.py:65
├─ 创建 ADK SessionService (per-attempt，attempt结束销毁)
├─ 编译 canonical_history → ADK session events
├─ 构造 user_message
│   ├─ 图片附件 → 完整读取，物化为 Part.from_bytes
│   └─ 非图片 → 8KiB preview + "[preview truncated...]"
├─ 构造 RunContext
│   ├─ tool_broker, fencing_token, release_fingerprint
│   ├─ runtime_io (CommittedEventSink)
│   └─ engine_checkpoint, runtime_working_state
├─ set_request_context(SkillRequestContext)  ← contextvar
├─ engine = build_engine(context, "native_loop")
├─ async for event in engine.run_stream(rc):
│   ├─ Broker-owned tool event → io.force_flush() + continue
│   └─ Engine-owned event → io.emit(event_type, data)
├─ 返回 rc.engine_outcome (引擎显式设置，不能EOF推断)
└─ finally: reset_request_context(token)
```

---

### 3.7 阶段七：CommittedEventSink 事件出口

**文件**: `agent/runtime/application/events.py`

```python
class CommittedEventSink:
    """三代引擎共同使用的事件实现；RuntimeIO 是独立的 Protocol。"""
```

**关键设计**:

```text
┌─────────────────────────────────────────────────────────────────────────────┐
│  聚合规则: 100ms 或 2048 bytes                                              │
│                                                                             │
│  flush 前必须满足:                                                          │
│  ├─ 切换 message/tool/checkpoint/terminal 时先 flush                         │
│  ├─ final assistant + citation + terminal 同事务                             │
│  └─ 先 commit，后 SSE 可见                                                   │
│                                                                             │
│  旁路 trace 信号:                                                           │
│  ├─ TTFT (Time To First Token)                                              │
│  ├─ event_counts (各类事件计数)                                              │
│  └─ had_error, finish_reason                                                │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 4. 源码位置索引

### 4.1 API 层

| 功能 | 文件 | 行号 |
|---|---|---|
| HTTP 入口 | `agent/runtime/api/runs.py` | 131 (装饰器) / 132 (函数) |
| Pydantic 校验 | `agent/runtime/api/runs.py` | 34-78 |
| SSE 流 | `agent/runtime/api/runs.py` | 273（`stream_events`；254 是内部 `_sse()` 序列化辅助函数） |

### 4.2 应用层

| 功能 | 文件 | 行号 |
|---|---|---|
| Admission 服务 | `agent/runtime/application/admission.py` | 50 |
| RunCoordinator | `agent/runtime/application/coordinator.py` | 65（`execute_claim`）/ 76（`_execute_claim`） |
| CommittedEventSink | `agent/runtime/application/events.py` | 1 |
| ToolBroker | `agent/runtime/application/tool_broker.py` | 1 |

### 4.3 SQLite 持久化

| 功能 | 文件 | 行号 |
|---|---|---|
| admit 事务 | `agent/runtime/adapters/sqlite/store.py` | 386 |
| claim_next | `agent/runtime/adapters/sqlite/store.py` | 721 |
| mark_activity_running | `agent/runtime/adapters/sqlite/store.py` | 807 |
| append_events | `agent/runtime/adapters/sqlite/store.py` | 582 |
| finalize_success | `agent/runtime/adapters/sqlite/store.py` | 973 |
| finalize_failure | `agent/runtime/adapters/sqlite/store.py` | 1110 |

### 4.4 Worker 调度

| 功能 | 文件 | 行号 |
|---|---|---|
| Worker 主循环 | `agent/runtime/worker/dispatcher.py` | 57 |
| _maintenance | `agent/runtime/worker/dispatcher.py` | 105 |
| _execute | `agent/runtime/worker/dispatcher.py` | 149 |
| _renew_lease | `agent/runtime/worker/dispatcher.py` | 176 |

### 4.5 引擎适配

| 功能 | 文件 | 行号 |
|---|---|---|
| LegacyEngineAdapter | `agent/runtime/adapters/legacy_engines.py` | 46 |
| Native 引擎 | `agent/engine/native_loop/engine.py` | 1 |
| Agent Loop (ADK) | `agent/engine/agent_loop/` | - |
| Plan Execute | `agent/engine/plan_execute/` | - |

### 4.6 领域模型

| 功能 | 文件 | 行号 |
|---|---|---|
| RunRecord | `agent/runtime/domain/models.py` | (搜索) |
| ActivityRecord | `agent/runtime/domain/models.py` | (搜索) |
| CanonicalEvent | `agent/runtime/domain/models.py` | (搜索) |
| RuntimeFault | `agent/runtime/domain/errors.py` | 1 |

---

## 5. 关键设计问题解答

### 5.1 Worker 是定时捞取还是主动触发？

**答案：短轮询 + CAS 抢占，不是事件驱动。**

**原因**:
1. **SQLite 不支持跨进程通知** — 单机多进程无法用消息队列，轮询是最简方案
2. **lease/fencing 已保证安全** — 即使多 Worker 并发 claim，CAS + fencing_token 保证只有一个胜出
3. **250ms 延迟可接受** — 单机场景，亚秒级延迟足够；不需要引入 Redis/pub-sub 等外部依赖
4. **恢复友好** — Worker 崩溃后 lease 过期，其他 Worker 轮询时自然捡走

**配置**:
- `RUNTIME_WORKER_POLL_MS=250` — 每 250ms 扫描一次
- `RUNTIME_WORKER_CONCURRENCY=4` — 最多同时 4 个 Activity

**代码位置**: `agent/runtime/worker/dispatcher.py:68-86`

---

### 5.2 为什么 trace_id 不进 digest？

**原因**: `trace_id` 是诊断信号，不是业务语义。如果让它参与幂等摘要，"同一请求重放但换了 trace_id" 会被误判成 409 冲突。

**代码位置**: `agent/runtime/application/admission.py:20-23`

---

### 5.3 为什么 activity_id 用 UUIDv5 派生？

**原因**: UUIDv5 是确定性 ID，由 `(run_id, "engine:0")` 派生。崩溃恢复时，Worker 可以用相同输入重新计算出相同的 activity_id，保证 stable slot。

**代码位置**: `agent/runtime/adapters/sqlite/store.py:476`

---

### 5.4 为什么 Engine 不能 EOF 推断成功？

**原因**: 生成器退出（EOF）不能证明引擎成功。每个引擎必须在 `RunContext.engine_outcome` 显式设置结果。如果 EOF 时 `engine_outcome is None`，返回 `TERMINAL_FAILURE`。

**代码位置**: `agent/runtime/adapters/legacy_engines.py:171-181`

---

### 5.5 为什么 Event 要先 commit 后 SSE 可见？

**原因**: 保证 SSE 客户端看到的事件都是已持久化的事实。如果先推送后 commit，推送成功但 commit 失败会导致客户端看到幽灵事件。

**代码位置**: `agent/runtime/application/events.py` (flush 逻辑)

---

## 6. 阅读建议

### 6.1 推荐阅读顺序

```text
1. agent/runtime/api/runs.py          ← 入口，理解 HTTP 契约
2. agent/runtime/application/admission.py  ← 幂等逻辑
3. agent/runtime/adapters/sqlite/store.py:386  ← 事务核心
4. agent/runtime/worker/dispatcher.py  ← 调度循环
5. agent/runtime/application/coordinator.py  ← 执行编排
6. agent/runtime/adapters/legacy_engines.py  ← 引擎适配
7. agent/engine/native_loop/engine.py  ← 具体引擎实现
```

### 6.2 测试用例参考

- **可靠性测试**: `tests/reliability/` — 使用 fake/scripted 依赖，不需要真实 LLM
- **行为评测**: `eval/` — 需要真实 `DASHSCOPE_API_KEY`

### 6.3 调试技巧

```bash
# 查看 Run 状态
curl -sS http://127.0.0.1:8000/api/v1/runs/run_xxx

# 查看 committed events
curl -N "http://127.0.0.1:8000/api/v1/runs/run_xxx/events?after_seq=0"

# 直接查 SQLite
sqlite3 local_storage/runtime/runtime.db \
  "SELECT run_id, state, engine, deadline_at FROM runs WHERE run_id='run_xxx';"

sqlite3 local_storage/runtime/runtime.db \
  "SELECT activity_id, state, attempt, fencing_token FROM activities WHERE run_id='run_xxx';"
```

### 6.4 关键日志观察点

```text
[Boot]         runtime API starting / Worker dispatcher started
[Runtime]      attempt settled (run_id, status)
[Worker]       lease fence lost (fencing 竞争失败)
[ArtifactGC]   orphans reclaimed
```

---

## 附录：状态机

### Run 状态迁移

```text
                ┌─────────────────────────────────────────┐
                │                                         │
                ▼                                         │
DISPATCH_PENDING ──claim_next──► RUNNING ──┬──► SUCCEEDED │
       ▲                                    │              │
       │                                    ├──► FAILED    │
       │                                    │              │
       │                                    ├──► CANCELLED │
       │                                    │              │
       │                                    ├──► TIMED_OUT │
       │                                    │              │
       │                                    └──► WAITING_INPUT ──signal──► DISPATCH_PENDING
       │
       └─── 幂等重放返回原 Run
```

### Activity 状态迁移

```text
PENDING ──claim_next──► CLAIMED ──mark_running──► RUNNING ──完成──► SUCCEEDED/FAILED/CANCELLED
RUNNING ──retryable failure──► WAITING_RETRY ──timer──► PENDING
RUNNING ──interrupt──► WAITING_INPUT ──signal──► PENDING
CLAIMED/RUNNING ──lease 过期 + classifier=REQUEUE──► PENDING
CLAIMED/RUNNING ──lease 恢复发现 unresolved effect──► RECONCILE/MANUAL
```

重试和 lease 恢复都更新同一条 Activity，不会为同一 logical key 新建 Activity。

---

*文档生成时间: 2026-08-12*
*基于项目版本: sxw_agent-2_demo R0 冻结规格*
