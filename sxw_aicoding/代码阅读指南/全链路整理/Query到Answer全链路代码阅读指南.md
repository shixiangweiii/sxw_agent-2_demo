# Query 到 Answer 全链路代码阅读指南

本文档整合 CreateRun、Worker 执行、SSE 推送、前端渲染的完整链路，包含端到端时序图、调用栈（含源码位置）、关键逻辑摘要，用于精读整体链路源码。

---

## 目录

- [1. 全链路概览](#1-全链路概览)
- [2. 端到端时序图](#2-端到端时序图)
- [3. 阶段一：HTTP 入口 → CreateRun](#3-阶段一http-入口--createrun)
- [4. 阶段二：Admission → SQLite 事务](#4-阶段二admission--sqlite-事务)
- [5. 阶段三：Worker 领取 Claim](#5-阶段三worker-领取-claim)
- [6. 阶段四：RunCoordinator 执行](#6-阶段四runcoordinator-执行)
- [7. 阶段五：Engine 执行 → Event 产出](#7-阶段五engine-执行--event-产出)
- [8. 阶段六：SSE 推送 → 前端消费](#8-阶段六sse-推送--前端消费)
- [9. 阶段七：前端渲染 → 终态](#9-阶段七前端渲染--终态)
- [10. 完整调用栈总览](#10-完整调用栈总览)
- [11. 源码位置索引](#11-源码位置索引)

---

## 1. 全链路概览

### 1.1 整体流程

```text
┌─────────────────────────────────────────────────────────────────────────────┐
│                              用户 (浏览器)                                  │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │  1. 输入 query + 附件                                                │   │
│  │  2. POST /runs → 获得 run_id                                         │   │
│  │  3. GET /runs/{id}/events → SSE 订阅                                 │   │
│  │  4. 消费 SSE 事件，渲染回答                                          │   │
│  │  5. 收到 terminal 事件，结束                                         │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────┬───────────────────────────────────────────┘
                                  │
                                  │ HTTP
                                  ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                         Runtime API 进程 (:8000)                            │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │  1. create_run() → AdmissionService.create()                         │   │
│  │  2. store.admit() → SQLite 事务                                      │   │
│  │  3. stream_events() → SSE 推送                                       │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────┬───────────────────────────────────────────┘
                                  │
                                  │ SQLite (runtime.db)
                                  ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                         Runtime Worker 进程                                 │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │  1. claim_next() → 领取 Activity                                     │   │
│  │  2. RunCoordinator.execute_claim() → 执行引擎                        │   │
│  │  3. EngineAdapter.execute() → 产出 Events                            │   │
│  │  4. CommittedEventSink → append_events()                             │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 1.2 关键里程碑

| 阶段 | 里程碑 | 关键产物 |
|---|---|---|
| HTTP 入口 | CreateRun 请求 | Pydantic 校验通过 |
| Admission | Run 创建成功 | run_id, conversation_id |
| Worker 领取 | Claim 获得执行权 | fencing_token, lease |
| 引擎执行 | 产出 Events | text/tool_call/tool_result |
| SSE 推送 | 前端收到事件 | 增量渲染 |
| 终态 | Run 完成 | terminal 事件 |

---

## 2. 端到端时序图

### 2.1 完整时序

```text
    浏览器                  API进程(:8000)           SQLite(runtime.db)         Worker进程
      │                         │                        │                        │
      │──POST /runs────────────>│                        │                        │
      │  {engine, input, ...}   │                        │                        │
      │  Idempotency-Key: xxx   │                        │                        │
      │                         │                        │                        │
      │                         │──[Admission 事务]──────>│                        │
      │                         │  1. 查 run_requests (幂等)                      │
      │                         │  2. 查 active_releases (release)                │
      │                         │  3. 查 artifact_metadata (附件)                 │
      │                         │  4. INSERT/UPDATE conversations                 │
      │                         │  5. INSERT runs (DISPATCH_PENDING)              │
      │                         │  6. INSERT activities (PENDING)                 │
      │                         │  7. INSERT run_requests (幂等记录)              │
      │                         │  8. INSERT artifact_links                       │
      │                         │  9. INSERT run_events (seq 1-4)                 │
      │                         │     ├─ USER_MESSAGE_COMMITTED                   │
      │                         │     ├─ RUN_STATUS_CHANGED (None→ACCEPTED)       │
      │                         │     ├─ RUN_STATUS_CHANGED (ACCEPTED→DISPATCH)   │
      │                         │     └─ ACTIVITY_STATUS_CHANGED (None→PENDING)   │
      │                         │  10. COMMIT                                     │
      │                         │                        │                        │
      │<─202 Accepted───────────│                        │                        │
      │  {run_id, events_url}   │                        │                        │
      │                         │                        │                        │
      │                         │                        │    (250ms 轮询)        │
      │                         │                        │<─────claim_next────────│
      │                         │                        │  [Claim 事务]          │
      │                         │                        │  1. SELECT activities  │
      │                         │                        │     WHERE state=PENDING│
      │                         │                        │  2. UPDATE activities  │
      │                         │                        │     SET state=CLAIMED  │
      │                         │                        │     fencing_token+1    │
      │                         │                        │  3. UPDATE runs        │
      │                         │                        │     SET state=RUNNING  │
      │                         │                        │  4. INSERT run_events  │
      │                         │                        │     ├─ ACTIVITY: PENDING→CLAIMED│
      │                         │                        │     └─ RUN: DISPATCH→RUNNING  │
      │                         │                        │  5. COMMIT             │
      │                         │                        │                        │
      │──GET /runs/{id}/events──>│                        │                        │
      │  ?after_seq=0           │                        │                        │
      │                         │──list_events───────────>│                        │
      │                         │<─返回 seq 1-4───────────│                        │
      │<─SSE id:1 event:user_message──│                   │                        │
      │<─SSE id:2 event:run_status────│                   │                        │
      │<─SSE id:3 event:run_status────│                   │                        │
      │<─SSE id:4 event:activity_status─│                 │                        │
      │                         │                        │                        │
      │                         │                        │    execute_claim()     │
      │                         │                        │    ┌───────────────────┤
      │                         │                        │    │ mark_activity_running
      │                         │                        │    │ 检查 release 兼容性
      │                         │                        │    │ compile_history()  │
      │                         │                        │    │ EngineAdapter.execute()
      │                         │                        │    │  ┌─────────────────┤
      │                         │                        │    │  │ 引擎循环执行     │
      │                         │                        │    │  │ LLM 调用         │
      │                         │                        │    │  │ 工具调用         │
      │                         │                        │    │  │ CommittedEventSink
      │                         │                        │    │  │  ┌───────────────┤
      │                         │                        │    │  │  │ emit() events │
      │                         │                        │    │  │  │ flush() 聚合  │
      │                         │                        │    │  │  └───────────────┤
      │                         │                        │    │  │ engine_outcome   │
      │                         │                        │    │  └─────────────────┤
      │                         │                        │    │ finalize_success() │
      │                         │                        │    │  ┌─────────────────┤
      │                         │                        │    │  │ [Finalize 事务] │
      │                         │                        │    │  │ INSERT events   │
      │                         │                        │    │  │ ├─ ASSISTANT_MSG│
      │                         │                        │    │  │ ├─ CITATION_SET │
      │                         │                        │    │  │ └─ RUN_TERMINATED
      │                         │                        │    │  │ UPDATE runs     │
      │                         │                        │    │  │ SET state=SUCCEEDED
      │                         │                        │    │  └─────────────────┤
      │                         │                        │    └───────────────────┤
      │                         │                        │                        │
      │                         │                        │                        │
      │<─SSE id:5 event:text────│                        │                        │
      │  delta: "混合召回是..." │                        │                        │
      │<─SSE id:6 event:tool_call─│                      │                        │
      │<─SSE id:7 event:tool_result─│                    │                        │
      │<─...────────────────────│                        │                        │
      │                         │                        │                        │
      │<─": heartbeat\n\n"────────│  (15秒无事件时)       │                        │
      │                         │                        │                        │
      │<─SSE id:N event:terminal─│                       │                        │
      │  terminal_status:SUCCEEDED                      │                        │
      │                         │                        │                        │
      │──连接关闭────────────────X                        │                        │
```

---

## 3. 阶段一：HTTP 入口 → CreateRun

### 3.1 调用栈

```text
[API 进程]
POST /api/v1/runs
├─ TraceMiddleware (设置 trace_id contextvar)
│
├─ create_run() runs.py:131
│   │
│   ├─ Pydantic 校验 CreateRunBody runs.py:40-48
│   │   ├─ engine: Literal["plan_execute", "agent_loop", "native_loop"]
│   │   ├─ input.text: min_length=1, max_length=200000
│   │   └─ input.attachment_refs: max_length=32
│   │
│   ├─ get_trace_id() ← 从 contextvar 获取
│   │
│   └─ AdmissionService.create() admission.py:50
│       │
│       ├─ 校验 idempotency_key 非空
│       │   └─ 空 → RuntimeFault("IDEMPOTENCY_KEY_REQUIRED", 400)
│       │
│       ├─ 计算 deadline
│       │   ├─ 未提供 → now + default_deadline_ms(600s)
│       │   └─ deadline <= now → RuntimeFault("DEADLINE_IN_PAST", 400)
│       │
│       ├─ 计算 request digest
│       │   └─ sha256_json(request.digest_payload())
│       │       └─ 包含: client_request_id, conversation_id, principal_id,
│       │              agent_id, engine, input.text, input.attachment_refs, deadline_at
│       │       └─ 不包含: trace_id (诊断信号，不影响幂等)
│       │
│       ├─ 构造 AdmissionCommand
│       │   ├─ 生成 ID (UUIDv4): run_id, turn_id, request_id, cancel_token_id, input_event_id
│       │   └─ activity_id = UUIDv5(run_id + "engine:0")  ← stable slot
│       │
│       └─ store.admit(command) ← 进入事务
```

### 3.2 关键代码

**文件**: `agent/runtime/api/runs.py:131-170`

```python
@router.post("", status_code=status.HTTP_202_ACCEPTED)
async def create_run(
    body: CreateRunBody,
    request: Request,
    response: Response,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict[str, Any]:
    settings: AgentSettings = request.app.state.settings
    service = AdmissionService(
        _store(request),
        default_deadline_ms=settings.runtime_default_deadline_seconds * 1000,
    )
    result = await service.create(
        CreateRunInput(
            client_request_id=str(body.client_request_id),
            conversation_id=body.conversation_id,
            principal_id=body.principal_id,
            agent_id=body.agent_id,
            engine=body.engine,
            text=body.input.text,
            attachment_refs=tuple(body.input.attachment_refs),
            deadline_at=rfc3339_to_ms(body.deadline_at),
            trace_id=get_trace_id(),  # ← 诊断信号，不进 digest
        ),
        idempotency_key=idempotency_key or "",
    )
    run = result.run
    base = f"/api/v1/runs/{run.envelope.run_id}"
    response.headers["Location"] = base
    return {
        "run_id": run.envelope.run_id,
        "turn_id": run.envelope.turn_id,
        "conversation_id": run.envelope.conversation_id,
        "status": run.status,
        "reused": result.reused,
        "status_url": base,
        "events_url": base + "/events",
    }
```

---

## 4. 阶段二：Admission → SQLite 事务

### 4.1 调用栈

```text
[API 进程]
store.admit(command) store.py:386
├─ BEGIN IMMEDIATE 事务
│
├─ 1. 查 run_requests (幂等校验)
│   └─ SELECT request_digest, run_id FROM run_requests
│      WHERE (principal_id, agent_id, idempotency_key)
│      ├─ 命中 + digest 不同 → 409 IDEMPOTENCY_KEY_REUSE
│      └─ 命中 + digest 相同 → 返回原 Run, reused=True
│
├─ 2. 查 active_releases (release 校验)
│   └─ SELECT release_fingerprint FROM active_releases WHERE engine=?
│      └─ 无结果 → 503 NO_ACTIVE_RELEASE
│
├─ 3. 查 artifact_metadata (附件校验)
│   └─ SELECT artifact_id FROM artifact_metadata WHERE artifact_id IN (...)
│      └─ 缺失 → 400 ARTIFACT_NOT_FOUND
│
├─ 4. 处理 conversation
│   ├─ conversation_id is null → INSERT 新 conversation, turn_seq=1
│   ├─ 指定 id 但不存在 → 404 NOT_FOUND
│   └─ 已存在 → 校验 ownership + UPDATE next_turn_seq+1
│
├─ 5. INSERT runs (state=DISPATCH_PENDING, next_seq=1)
│
├─ 6. INSERT activities (type=ENGINE_RUN, state=PENDING)
│
├─ 7. UPDATE runs SET current_activity_id
│
├─ 8. INSERT artifact_links (去重后的 INPUT_ATTACHMENT)
│
├─ 9. INSERT run_requests (幂等记录)
│
├─ 10. INSERT run_events (seq 1-4)
│    ├─ USER_MESSAGE_COMMITTED (seq=1)
│    ├─ RUN_STATUS_CHANGED: None → ACCEPTED (seq=2)
│    ├─ RUN_STATUS_CHANGED: ACCEPTED → DISPATCH_PENDING (seq=3)
│    └─ ACTIVITY_STATUS_CHANGED: None → PENDING (seq=4)
│
├─ 唯一约束检查
│   └─ uq_active_run_per_conversation 违反 → 409 CONVERSATION_BUSY
│
└─ COMMIT (原子提交)
```

### 4.2 关键代码

**文件**: `agent/runtime/adapters/sqlite/store.py:386-547`

```python
async def admit(self, command: AdmissionCommand) -> AdmissionResult:
    try:
        async with self.db.transaction() as conn:  # BEGIN IMMEDIATE
            # 1. 幂等查询
            prior = await (await conn.execute(
                """SELECT request_digest,run_id FROM run_requests
                   WHERE principal_id=? AND agent_id=? AND idempotency_key=?""",
                (command.principal_id, command.agent_id, command.idempotency_key),
            )).fetchone()
            if prior is not None:
                if prior["request_digest"] != command.request_digest:
                    raise conflict("IDEMPOTENCY_KEY_REUSE", ...)
                row = await self._require_run_row(conn, prior["run_id"])
                return AdmissionResult(run=_run_from_row(row), reused=True)
            
            # 2. 检查 active release
            release = await (await conn.execute(
                "SELECT release_fingerprint FROM active_releases WHERE engine=?",
                (command.engine,),
            )).fetchone()
            if release is None:
                raise unavailable("NO_ACTIVE_RELEASE", ...)
            
            # ... 后续步骤 ...
            
            # 3. 处理 conversation
            conversation_id = command.conversation_id or command.generated_conversation_id
            conversation = await (await conn.execute(
                "SELECT * FROM conversations WHERE conversation_id=?", (conversation_id,),
            )).fetchone()
            if conversation is None:
                if command.conversation_id is not None:
                    raise not_found("conversation", conversation_id)
                await conn.execute(
                    """INSERT INTO conversations ...""",
                    (conversation_id, command.principal_id, command.agent_id, 2, 1, ...),
                )
                turn_seq = 1
            else:
                # 校验 ownership + 推进 turn_seq
                turn_seq = int(conversation["next_turn_seq"])
                await conn.execute(
                    """UPDATE conversations SET next_turn_seq=next_turn_seq+1, ...""",
                    ...
                )
            
            # 4. INSERT runs
            await conn.execute(
                """INSERT INTO runs (...) VALUES (...)""",
                (command.run_id, SCHEMA_VERSION, ..., RunStatus.DISPATCH_PENDING, ...)
            )
            
            # 5. INSERT activities
            activity_id = stable_id("act", command.run_id, "engine:0")
            await conn.execute(
                """INSERT INTO activities (...) VALUES (...)""",
                (activity_id, command.run_id, ActivityType.ENGINE_RUN, ...)
            )
            
            # 6. INSERT run_requests (幂等记录)
            await conn.execute(
                """INSERT INTO run_requests (...) VALUES (...)""",
                ...
            )
            
            # 7. INSERT run_events
            await self._append_in_tx(conn, command.run_id, [
                EventDraft(EventType.USER_MESSAGE_COMMITTED, {...}, event_id=command.input_event_id),
                EventDraft(EventType.RUN_STATUS_CHANGED, {"from": None, "to": RunStatus.ACCEPTED}),
                EventDraft(EventType.RUN_STATUS_CHANGED, {"from": RunStatus.ACCEPTED, "to": RunStatus.DISPATCH_PENDING}),
                EventDraft(EventType.ACTIVITY_STATUS_CHANGED, {"from": None, "to": ActivityStatus.PENDING}, activity_id=activity_id),
            ])
            
            row = await self._require_run_row(conn, command.run_id)
            return AdmissionResult(run=_run_from_row(row), reused=False)
    except sqlite3.IntegrityError as exc:
        if "uq_active_run_per_conversation" in str(exc):
            raise conflict("CONVERSATION_BUSY", ...)
```

---

## 5. 阶段三：Worker 领取 Claim

### 5.1 调用栈

```text
[Worker 进程]
RuntimeWorker.run() dispatcher.py:57
├─ heartbeat_worker(ACTIVE) ← 启动时写 heartbeat
│
├─ while not self._stop.is_set():
│   ├─ _maintenance(now) ← 触发定时器、恢复过期、清理 Artifact
│   │
│   └─ while len(self._tasks) < self.concurrency:
│       ├─ claim_next() store.py:721
│       │   └─ BEGIN IMMEDIATE 事务
│       │       ├─ SELECT activities WHERE state=PENDING AND available_at<=now
│       │       │   AND (run.state=DISPATCH_PENDING OR cancel_reconcile)
│       │       │   AND run.deadline_at > now
│       │       │   AND run.engine IN (worker支持的引擎)
│       │       │   ORDER BY available_at, created_at LIMIT 1
│       │       │
│       │       ├─ UPDATE activities SET
│       │       │   state='CLAIMED', attempt+1, lease_owner=?, lease_expires_at=?,
│       │       │   fencing_token+1, revision+1
│       │       │
│       │       ├─ UPDATE runs SET state='RUNNING' (或保持 CANCEL_REQUESTED)
│       │       │
│       │       ├─ INSERT run_events
│       │       │   ├─ ACTIVITY_STATUS_CHANGED: PENDING → CLAIMED
│       │       │   └─ RUN_STATUS_CHANGED: DISPATCH_PENDING → RUNNING
│       │       │
│       │       └─ RETURN Claim(run, activity)
│       │
│       └─ create_task(_execute(claim)) ← 异步执行
```

### 5.2 关键代码

**文件**: `agent/runtime/adapters/sqlite/store.py:721-805`

```python
async def claim_next(self, *, worker_id: str, lease_ms: int, now_ms: int, engines: Sequence[str]):
    async with self.db.transaction() as conn:
        cursor = await conn.execute(
            f"""UPDATE activities SET
                   state='CLAIMED', attempt=attempt+1, lease_owner=?, lease_expires_at=?,
                   fencing_token=fencing_token+1, revision=revision+1, updated_at=?
                 WHERE activity_id=(
                   SELECT a.activity_id FROM activities a JOIN runs r ON r.run_id=a.run_id
                   WHERE a.type='ENGINE_RUN' AND a.state='PENDING' AND a.available_at<=?
                     AND (
                       r.state='DISPATCH_PENDING'
                       OR (r.state='CANCEL_REQUESTED' AND json_extract(...) = 'reconciliation')
                     )
                     AND r.deadline_at>?
                     AND r.engine IN ({placeholders})
                   ORDER BY a.available_at, a.created_at, a.activity_id LIMIT 1
                 ) AND state='PENDING'
                 RETURNING *""",
            (worker_id, now_ms + lease_ms, now_ms, now_ms, RECONCILIATION_MARKER_KIND, now_ms, *engines),
        )
        activity_row = await cursor.fetchone()
        if activity_row is None:
            return None
        
        # 更新 runs 状态
        if cancel_reconcile_claim:
            updated = await conn.execute(
                """UPDATE runs SET revision=revision+1, current_activity_id=?, updated_at=?
                   WHERE run_id=? AND state='CANCEL_REQUESTED'""",
                ...
            )
        else:
            updated = await conn.execute(
                """UPDATE runs SET state='RUNNING', revision=revision+1, current_activity_id=?, updated_at=?
                   WHERE run_id=? AND state='DISPATCH_PENDING'""",
                ...
            )
        
        # 追加事件
        await self._append_in_tx(conn, activity_row["run_id"], drafts)
        
        return Claim(run=_run_from_row(run_row), activity=_activity_from_row(activity_row))
```

---

## 6. 阶段四：RunCoordinator 执行

### 6.1 调用栈

```text
[Worker 进程]
RunCoordinator.execute_claim(claim) coordinator.py:74
├─ use_trace_id(claim.run.trace_id or run_id) ← 恢复诊断关联
│
└─ _execute_claim() coordinator.py:86
    │
    ├─ 1. mark_activity_running() ← CAS + fencing
    │   └─ UPDATE activities SET state='RUNNING' WHERE state='CLAIMED' AND fencing_token=?
    │
    ├─ 2. 检查 ToolReconciliationMarker
    │   └─ resume_payload 是 reconcile marker → 只走 query hook，不执行引擎
    │
    ├─ 3. 检查 cancel 抢占
    │   └─ marker=None + status=CANCEL_REQUESTED → finalize_failure(CANCELLED)
    │
    ├─ 4. Release 兼容性检查
    │   ├─ adapter.release_fingerprint != run.envelope.release_fingerprint
    │   │   ├─ checkpoint=None → 无法升级 → INCOMPATIBLE_RELEASE
    │   │   ├─ 有 upgrader → 升级 checkpoint
    │   │   └─ 无 upgrader → INCOMPATIBLE_RELEASE
    │   └─ 匹配 → 继续
    │
    ├─ 5. Deadline 检查
    │   └─ now >= deadline_at → TIMED_OUT
    │
    ├─ 6. compile_history(run_id) ← 从 committed events 重建对话历史
    │
    ├─ 7. 构造 EngineRunRequest + CommittedEventSink
    │   ├─ flush_ms=100, flush_bytes=2048
    │   └─ attach_trace_span(span)
    │
    ├─ 8. adapter.execute(request, io) ← 引擎执行
    │   ├─ TimeoutError/ConnectionError → RETRYABLE_FAILURE
    │   └─ Exception → TERMINAL_FAILURE
    │
    └─ 9. 根据 outcome.kind 终结
        ├─ COMPLETED + 无 unresolved → finalize_success
        ├─ COMPLETED + 有 unresolved → wait_for_input(人工 reconcile)
        ├─ WAITING_INPUT → wait_for_input
        ├─ RETRYABLE_FAILURE + attempt<max → schedule_retry
        └─ 其他 → finalize_failure
```

### 6.2 关键代码

**文件**: `agent/runtime/application/coordinator.py:74-400`

```python
async def execute_claim(self, claim: Claim, *, worker_id: str) -> RunStatus:
    with use_trace_id(claim.run.trace_id or claim.run.envelope.run_id):
        return await self._execute_claim(claim, worker_id=worker_id)

async def _execute_claim(self, claim: Claim, *, worker_id: str) -> RunStatus:
    # 1. 标记 Activity 为 RUNNING
    activity = await self.store.mark_activity_running(
        claim.activity.activity_id,
        worker_id=worker_id,
        fencing_token=claim.activity.fencing_token,
        now_ms=self.clock.now_ms(),
    )
    
    # 2. 检查 ToolReconciliationMarker
    marker = ToolReconciliationMarker.parse_exact(activity.resume_payload)
    if marker is not None:
        # 只走 reconcile 路径，不执行引擎
        await self.tool_reconciler.reconcile_only(...)
        return (await self.store.settle_reconciliation_query(...)).status
    
    # 3. 检查 cancel 抢占
    if marker is None and run.status is RunStatus.CANCEL_REQUESTED:
        return (await self.store.finalize_failure(..., terminal_status=RunStatus.CANCELLED)).status
    
    # 4. Release 兼容性检查
    if adapter.release_fingerprint != run.envelope.release_fingerprint:
        # 检查 upgrader...
        if incompatibility is not None:
            return (await self.store.finalize_failure(..., terminal_status=RunStatus.INCOMPATIBLE_RELEASE)).status
    
    # 5. Deadline 检查
    if self.clock.now_ms() >= run.envelope.deadline_at:
        return (await self.store.finalize_failure(..., terminal_status=RunStatus.TIMED_OUT)).status
    
    # 6. 编译历史
    history = await self.store.compile_history(run.envelope.run_id)
    
    # 7. 构造请求和 IO
    request = EngineRunRequest(envelope=run.envelope, ...)
    io = CommittedEventSink(self.store, run_id=..., flush_ms=100, flush_bytes=2048)
    
    # 8. 执行引擎
    with start_span("runtime.engine_attempt", ...):
        outcome = await adapter.execute(request, io)
        await io.close()
    
    # 9. 根据 outcome 终结
    if outcome.kind is EngineOutcomeKind.COMPLETED:
        unresolved = await self.store.unresolved_tool_execution_ids(...)
        if unresolved:
            return (await self.store.wait_for_input(...)).status
        else:
            return (await self.store.finalize_success(...)).status
    # ... 其他分支 ...
```

---

## 7. 阶段五：Engine 执行 → Event 产出

### 7.1 调用栈

```text
[Worker 进程]
LegacyEngineAdapter.execute(request, io) legacy_engines.py:65
├─ 创建 ADK SessionService (per-attempt，attempt结束销毁)
│
├─ 编译 canonical_history → ADK session events
│
├─ 构造 user_message
│   ├─ 图片附件 → 完整读取，物化为 Part.from_bytes
│   └─ 非图片 → 8KiB preview + "[preview truncated...]"
│
├─ 构造 RunContext
│   ├─ tool_broker, fencing_token, release_fingerprint
│   ├─ runtime_io (CommittedEventSink)
│   └─ engine_checkpoint, runtime_working_state
│
├─ engine = build_engine(context, "native_loop")
│
├─ async for event in engine.run_stream(rc):
│   ├─ Broker-owned tool event → io.force_flush() + continue
│   └─ Engine-owned event → io.emit(event_type, data)
│       └─ CommittedEventSink.emit() events.py
│           ├─ 聚合: 100ms 或 2048 bytes
│           └─ flush 前: 切换 message/tool/checkpoint/terminal 时先 flush
│
├─ 返回 rc.engine_outcome (引擎显式设置，不能EOF推断)
│
└─ finally: reset_request_context(token)
```

### 7.2 CommittedEventSink

**文件**: `agent/runtime/application/events.py`

```python
class CommittedEventSink(RuntimeIO):
    """三代引擎唯一共同的事件出口"""
    
    async def emit(self, event_type: str, data: dict) -> None:
        # 聚合: 100ms 或 2048 bytes
        self._buffer.append(EventDraft(...))
        if self._should_flush():
            await self.flush()
    
    async def flush(self) -> None:
        # 先 commit，后 SSE 可见
        await self.store.append_events(self.run_id, self._buffer, ...)
        self._buffer.clear()
    
    async def close(self) -> None:
        await self.flush()  # 确保所有事件提交
```

---

## 8. 阶段六：SSE 推送 → 前端消费

### 8.1 后端 SSE 端点

**文件**: `agent/runtime/api/runs.py:272-316`

```python
@router.get("/{run_id}/events")
async def stream_events(run_id: str, after_seq: int | None = None, last_event_id: str | None = None):
    await store.get_run(run_id)  # 404 检查
    initial_cursor = after_seq if after_seq is not None else parse_last_event_id(last_event_id)
    
    async def generate():
        cursor = initial_cursor
        last_write = time.monotonic()
        while True:
            # 1. 查询新事件
            events = await store.list_events(run_id, after_seq=cursor, limit=500)
            for event in events:
                cursor = event.seq
                yield _sse(event)
                last_write = time.monotonic()
                if event.event_type is EventType.RUN_TERMINATED:
                    return
            
            # 2. 检查终态
            run = await store.get_run(run_id)
            if run.status in TERMINAL_RUN_STATUSES and not events:
                return
            
            # 3. Heartbeat: 15秒无事件
            if time.monotonic() - last_write >= settings.runtime_sse_heartbeat_seconds:
                yield ": heartbeat\n\n"
                last_write = time.monotonic()
            
            # 4. 轮询间隔 250ms
            await asyncio.sleep(settings.runtime_sse_poll_ms / 1000)
    
    return StreamingResponse(generate(), media_type="text/event-stream")
```

### 8.2 前端 SSE 消费

**文件**: `web/app.js`

```javascript
// 1. CreateRun 后启动 watch
async function handleSubmit() {
  const created = await createRun(query, refs);
  const assistant = appendMessage("assistant", "");
  await watchRun(assistant);
}

// 2. watchRun 循环
async function watchRun(assistant) {
  state.watching = true;
  while (state.watching && !state.terminal) {
    state.watchController = new AbortController();
    try {
      const response = await fetch(
        `/api/v1/runs/${state.runId}/events?after_seq=${state.lastSeq}`,
        { signal: state.watchController.signal }
      );
      await consumeSse(response, assistant);
      if (!state.terminal && state.watching) await sleep(500);
    } catch (error) {
      if (error.name === "AbortError") break;
      await sleep(750);  // 断线重连
    }
  }
}

// 3. consumeSse 流式解析
async function consumeSse(response, assistant) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    
    let boundary = buffer.indexOf("\n\n");
    while (boundary !== -1) {
      handleSseEvent(parseSseBlock(buffer.slice(0, boundary)), assistant);
      buffer = buffer.slice(boundary + 2);
      boundary = buffer.indexOf("\n\n");
    }
  }
}

// 4. handleSseEvent 渲染
function handleSseEvent(event, assistant) {
  if (!event.data) return;
  let envelope = JSON.parse(event.data);
  const payload = envelope.payload || {};
  
  if (event.id) {
    state.lastSeq = Math.max(state.lastSeq, event.id);
    localStorage.setItem("sxw.last_seq", String(state.lastSeq));
  }
  
  if (event.type === "text") {
    assistant.body.textContent += payload.delta || "";  // 增量追加
  } else if (event.type === "assistant_message") {
    assistant.body.textContent = payload.text || "";    // 完整覆盖
  } else if (event.type === "tool_call" || event.type === "tool_result") {
    addProcessItem(assistant.node, event.type, payload);
  } else if (event.type === "terminal") {
    state.terminal = true;
    state.watching = false;
    setStatus(`运行结束：${envelope.terminal_status}`);
  }
}
```

---

## 9. 阶段七：前端渲染 → 终态

### 9.1 渲染流程

```text
前端渲染逻辑:
├─ text 事件 → 增量追加文本 (assistant.body.textContent += delta)
├─ assistant_message 事件 → 完整覆盖 (assistant.body.textContent = text)
├─ tool_call 事件 → 添加到过程面板 (addProcessItem)
├─ tool_result 事件 → 添加到过程面板 (addProcessItem)
├─ citation 事件 → 添加引用 (addCitations)
├─ terminal 事件 → 结束 (state.terminal = true)
│   └─ 添加轨迹链接 (addTraceLink)
└─ heartbeat → 忽略 (SSE comment)
```

### 9.2 页面刷新重建

```javascript
// app.js:450-476
async function resumeStoredRun() {
  if (!state.runId) return;
  
  const run = await fetch(`/api/v1/runs/${state.runId}`).then(r => r.json());
  
  // 关键：lastSeq 重置为 0，从 committed events 重建 UI
  state.lastSeq = 0;
  state.terminal = false;
  localStorage.setItem("sxw.last_seq", "0");
  
  const assistant = appendMessage("assistant", "");
  await watchRun(assistant);  // 从 seq=0 重放所有事件
}
```

---

## 10. 完整调用栈总览

```text
[浏览器]
handleSubmit() app.js:401
├─ createRun() app.js:363
│   └─ POST /api/v1/runs
├─ appendMessage("assistant", "")
└─ watchRun() app.js:340
    └─ consumeSse() app.js:323
        └─ handleSseEvent() app.js:291

          │
          │ HTTP POST/GET
          ▼

[API 进程]
create_run() runs.py:131
├─ AdmissionService.create() admission.py:50
│   └─ store.admit() store.py:386
│       └─ BEGIN IMMEDIATE 事务
│           ├─ INSERT runs/activities/events
│           └─ COMMIT
│
stream_events() runs.py:272
└─ generate() runs.py:290
    └─ while True:
        ├─ list_events() → yield SSE
        └─ heartbeat → yield ": heartbeat\n\n"

          │
          │ SQLite
          ▼

[Worker 进程]
RuntimeWorker.run() dispatcher.py:57
├─ claim_next() store.py:721
│   └─ BEGIN IMMEDIATE 事务
│       ├─ UPDATE activities SET state=CLAIMED
│       └─ COMMIT
│
└─ RunCoordinator.execute_claim() coordinator.py:74
    ├─ mark_activity_running()
    ├─ compile_history()
    └─ LegacyEngineAdapter.execute() legacy_engines.py:65
        └─ engine.run_stream()
            └─ CommittedEventSink.emit() → flush() → append_events()
```

---

## 11. 源码位置索引

### 11.1 API 层

| 功能 | 文件 | 行号 |
|---|---|---|
| HTTP 入口 | `agent/runtime/api/runs.py` | 131 |
| Admission 服务 | `agent/runtime/application/admission.py` | 50 |
| SQLite admit 事务 | `agent/runtime/adapters/sqlite/store.py` | 386 |
| SSE 端点 | `agent/runtime/api/runs.py` | 272 |
| SSE 生成器 | `agent/runtime/api/runs.py` | 290 |

### 11.2 Worker 层

| 功能 | 文件 | 行号 |
|---|---|---|
| Worker 主循环 | `agent/runtime/worker/dispatcher.py` | 57 |
| claim_next | `agent/runtime/adapters/sqlite/store.py` | 721 |
| RunCoordinator | `agent/runtime/application/coordinator.py` | 74 |
| LegacyEngineAdapter | `agent/runtime/adapters/legacy_engines.py` | 65 |
| CommittedEventSink | `agent/runtime/application/events.py` | 1 |

### 11.3 前端

| 功能 | 文件 | 行号 |
|---|---|---|
| CreateRun | `web/app.js` | 363 |
| watchRun | `web/app.js` | 340 |
| consumeSse | `web/app.js` | 323 |
| handleSseEvent | `web/app.js` | 291 |
| 页面刷新重建 | `web/app.js` | 450 |

---

## 附录：关键设计原则

| 原则 | 体现 |
|---|---|
| **先 commit，后 SSE 可见** | 事件必须在 SQLite 事务提交后才能被 SSE 推送 |
| **幂等 admission** | idempotency_key + digest 校验 |
| **lease/fencing** | 防 Worker 崩溃锁死和过期提交 |
| **append-only events** | 触发器保护，只追加不修改 |
| **stable slot** | UUIDv5 派生，崩溃恢复可重算 |
| **heartbeat 保活** | 15秒无事件发送 SSE comment |

---

*文档生成时间: 2026-08-09*
*基于项目版本: sxw_agent-2_demo R0 冻结规格*
