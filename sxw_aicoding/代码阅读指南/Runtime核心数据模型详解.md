# Runtime 核心数据模型详解

本文档以 Claim 为引子，系统梳理可靠执行运行时的核心数据结构、领域模型和表结构设计，帮助理解整个 Runtime 的数据模型设计哲学。

---

## 目录

- [1. 数据模型全景图](#1-数据模型全景图)
- [2. 核心领域模型](#2-核心领域模型)
- [3. 状态机模型](#3-状态机模型)
- [4. 表结构设计](#4-表结构设计)
- [5. 数据流与事务边界](#5-数据流与事务边界)
- [6. 源码位置索引](#6-源码位置索引)

---

## 1. 数据模型全景图

### 1.1 模型层次

```text
┌─────────────────────────────────────────────────────────────────────────────┐
│                              应用层 (Application)                           │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐   │
│  │     Claim    │  │   Runtime    │  │   Working    │  │   Engine     │   │
│  │              │  │  Envelope    │  │    State     │  │  Outcome     │   │
│  └──────────────┘  └──────────────┘  └──────────────┘  └──────────────┘   │
└─────────────────────────────────────────────────────────────────────────────┘
                                      │
                                      ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                              领域层 (Domain)                                │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐   │
│  │  RunRecord   │  │   Activity   │  │  Canonical   │  │  Checkpoint  │   │
│  │              │  │   Record     │  │    Event     │  │   Record     │   │
│  └──────────────┘  └──────────────┘  └──────────────┘  └──────────────┘   │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐                     │
│  │    Tool      │  │   Artifact   │  │    Signal    │                     │
│  │  Execution   │  │   Metadata   │  │              │                     │
│  └──────────────┘  └──────────────┘  └──────────────┘                     │
└─────────────────────────────────────────────────────────────────────────────┘
                                      │
                                      ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                           持久化层 (SQLite)                                  │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐        │
│  │  runs    │ │activities│ │run_events│ │checkpoints│ │tool_     │        │
│  │          │ │          │ │          │ │          │ │executions│        │
│  └──────────┘ └──────────┘ └──────────┘ └──────────┘ └──────────┘        │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐        │
│  │conversa- │ │ run_     │ │ artifact │ │ signals  │ │ timers   │        │
│  │  tions   │ │requests  │ │_metadata │ │          │ │          │        │
│  └──────────┘ └──────────┘ └──────────┘ └──────────┘ └──────────┘        │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 1.2 核心实体关系

```text
                    ┌─────────────┐
                    │conversations│
                    └──────┬──────┘
                           │ 1:N
                           ▼
┌─────────────┐      ┌──────────┐      ┌─────────────┐
│run_requests │◄─────┤  runs    ├─────►│  activities │
│ (幂等记录)  │      │          │      │             │
└─────────────┘      └────┬─────┘      └──────┬──────┘
                          │ 1:N               │ 1:N
                          ▼                   ▼
                    ┌──────────┐        ┌─────────────┐
                    │run_events│        │ checkpoints │
                    │(append-only)      │             │
                    └──────────┘        └─────────────┘
                          │
                          │ N:1
                          ▼
                    ┌─────────────┐      ┌─────────────┐
                    │tool_execut- │◄─────│artifact_    │
                    │  ions       │      │  links      │
                    └─────────────┘      └─────────────┘
```

---

## 2. 核心领域模型

### 2.1 RuntimeEnvelope（运行信封）

**文件**: `agent/runtime/domain/models.py:128`

```python
class RuntimeEnvelope(BaseModel):
    schema_version: str              # "1"，冻结版本
    request_id: str                  # 服务端生成的请求 ID
    client_request_id: str           # 客户端请求 ID
    idempotency_key: str             # 幂等键
    conversation_id: str             # 会话 ID
    turn_id: str                     # 轮次 ID
    run_id: str                      # Run ID
    principal_id: str                # 用户 ID
    agent_id: str                    # Agent ID
    engine: EngineName               # 引擎类型
    deadline_at: int                 # 绝对截止时间 (epoch ms)
    cancel_token_id: str             # 取消令牌 ID
    release_fingerprint: str         # Release 指纹
    input_event_id: str              # 输入事件 ID
    attachment_refs: tuple[str, ...] # 附件引用列表
    created_at: int                  # 创建时间
```

**设计意图**：
- 封装 Run 的"身份与执行上下文"，是 Run 的不可变核心
- `release_fingerprint` 锁定执行时的代码版本，保证恢复兼容性
- `deadline_at` 是绝对时间，向下传递剩余预算

---

### 2.2 RunRecord（运行记录）

**文件**: `agent/runtime/domain/models.py:185`

```python
class RunRecord(BaseModel):
    envelope: RuntimeEnvelope        # 运行信封
    trace_id: str = ""               # 诊断轨迹 ID（故意在 Envelope 外）
    status: RunStatus                # 运行状态
    revision: int                    # CAS 版本号
    next_seq: int                    # 下一个事件 seq
    current_activity_id: str | None  # 当前 Activity
    terminal_status: RunStatus | None # 终态状态
    terminal_payload: dict | None    # 终态负载
    input_text: str                  # 输入文本
    pending_input: dict | None       # 等待的输入
    updated_at: int                  # 更新时间
```

**关键点**：
- `trace_id` 不在 Envelope 内：诊断信号，不进入幂等摘要
- `revision`：CAS 防并发
- `next_seq`：事件序列号生成器

---

### 2.3 ActivityRecord（活动记录）

**文件**: `agent/runtime/domain/models.py:203`

```python
class ActivityRecord(BaseModel):
    activity_id: str                 # Activity ID
    run_id: str                      # 所属 Run
    type: ActivityType               # 类型：ENGINE_RUN/TOOL_CALL/...
    logical_key: str                 # 逻辑键（用于 stable slot）
    status: ActivityStatus           # 状态
    attempt: int                     # 尝试次数
    available_at: int                # 可领取时间
    lease_owner: str | None          # 租约持有者
    lease_expires_at: int | None     # 租约过期时间
    fencing_token: int               # 围栏令牌
    revision: int                    # CAS 版本号
    result: dict | None              # 结果
    error: dict | None               # 错误
    resume_payload: dict | None      # 恢复负载
    created_at: int
    updated_at: int
```

**关键设计**：
- `logical_key`：`run_id + logical_key` 派生 UUIDv5，保证 stable slot
- `lease_owner + lease_expires_at`：租约机制，防 Worker 崩溃锁死
- `fencing_token`：防过期执行者迟到提交

---

### 2.4 CanonicalEvent（规范事件）

**文件**: `agent/runtime/domain/models.py:164`

```python
class CanonicalEvent(BaseModel):
    event_id: str                    # 事件 ID
    schema_version: str              # 版本
    run_id: str                      # Run ID
    turn_id: str                     # 轮次 ID
    activity_id: str | None          # Activity ID
    tool_execution_id: str | None    # 工具执行 ID
    seq: int                         # 序列号
    event_type: EventType            # 事件类型
    producer: str                    # 生产者
    payload: dict | None             # 负载
    payload_ref: str | None          # 负载引用（Artifact）
    visibility: Visibility           # 可见性：PUBLIC/INTERNAL
    sensitivity: Sensitivity         # 敏感性：PUBLIC/PRIVATE/SENSITIVE
    occurred_at: int                 # 发生时间
    terminal_status: RunStatus | None # 终态状态
    release_fingerprint: str         # Release 指纹
```

**核心规则**：
- **append-only**：只追加不修改，有触发器保证
- **seq 单调**：同一 Run 内 seq 严格递增
- **visibility**：PUBLIC 可被 SSE 推送，INTERNAL 只内部可见

---

### 2.5 CheckpointRecord（检查点记录）

**文件**: `agent/runtime/domain/models.py:222`

```python
class CheckpointRecord(BaseModel):
    checkpoint_id: str               # 检查点 ID
    run_id: str                      # Run ID
    activity_id: str                 # Activity ID
    revision: int                    # CAS 版本号
    working_state: WorkingState      # 工作状态
    engine_state: dict | None        # 引擎状态
    engine_state_ref: str | None     # 引擎状态引用
    release_fingerprint: str         # Release 指纹
    schema_version: str              # 版本
    created_at: int                  # 创建时间
```

**设计意图**：
- `working_state`：可恢复的认知状态
- `engine_state`：引擎私有状态（可能很大，用 ref 引用 Artifact）
- 崩溃恢复时从最后一个 committed checkpoint 继续

---

### 2.6 WorkingState（工作状态）

**文件**: `agent/runtime/domain/models.py:149`

```python
class WorkingState(BaseModel):
    goal: str                        # 目标
    constraints: list[str]           # 约束
    model_plan: list[dict]           # 规划
    confirmed_facts: list[dict]      # 已确认事实
    open_questions: list[str]        # 未解决问题
    pending_input: dict | None       # 等待的输入
    budget: dict                     # 预算
    artifact_refs: list[str]         # 工件引用
    evidence_refs: list[str]         # 证据引用
    release_fingerprint: str         # Release 指纹
```

---

### 2.7 Claim（执行权凭证）

**文件**: `agent/runtime/ports/store.py:66`

```python
@dataclass(frozen=True)
class Claim:
    run: RunRecord                   # Run 快照
    activity: ActivityRecord         # Activity 快照
```

**语义**：Worker 对 Activity 的排他性执行权，内存中，不持久化。

---

## 3. 状态机模型

### 3.1 Run 状态机

**文件**: `agent/runtime/domain/models.py:17`

```text
                              ┌─────────────────────────────────────────┐
                              │                                         │
                              ▼                                         │
┌──────────┐  admit   ┌──────────────────┐  claim   ┌─────────┐       │
│  客户端  │ ───────► │ DISPATCH_PENDING ├────────► │ RUNNING │       │
└──────────┘          └──────────────────┘          └────┬────┘       │
                                                         │            │
                                    ┌────────────────────┼────────────┤
                                    │                    │            │
                                    ▼                    ▼            │
                            ┌───────────────┐   ┌───────────────┐    │
                            │WAITING_RETRY  │   │WAITING_INPUT  │    │
                            └───────┬───────┘   └───────┬───────┘    │
                                    │                   │            │
                                    └───────────────────┼────────────┤
                                                        │            │
                                                        ▼            │
                                                ┌─────────────┐      │
                                                │ 终态 (6种)  │ ─────┘
                                                └─────────────┘
```

**状态集合**：

| 类别 | 状态 |
|---|---|
| 非终态 | `ACCEPTED`, `DISPATCH_PENDING`, `RUNNING`, `WAITING_RETRY`, `WAITING_INPUT`, `CANCEL_REQUESTED` |
| 终态 | `SUCCEEDED`, `FAILED`, `CANCELLED`, `TIMED_OUT`, `REJECTED`, `INCOMPATIBLE_RELEASE` |

**关键规则**：
- 终态最多一个，不可变
- cancel 与 terminal 竞争，先提交者赢
- 存在 unresolved ToolEffect 时不能直接 CANCELLED

---

### 3.2 Activity 状态机

**文件**: `agent/runtime/domain/models.py:42`

```text
         ┌─────────┐     claim     ┌─────────┐    mark_running   ┌─────────┐
         │ PENDING ├──────────────►│ CLAIMED ├────────────────► │ RUNNING │
         └────┬────┘               └────┬────┘                  └────┬────┘
              │                         │                            │
              │ recover                 │ lease expired              │
              │                         ▼                            │
              │                    ┌─────────┐                       │
              └────────────────────│ PENDING │◄──────────────────────┘
                                   └─────────┘
                                        │
              ┌─────────────────────────┼─────────────────────────┐
              │                         │                         │
              ▼                         ▼                         ▼
       ┌─────────────┐          ┌─────────────┐          ┌─────────────┐
       │WAITING_RETRY│          │WAITING_INPUT│          │  RECONCILE  │
       └─────────────┘          └─────────────┘          └─────────────┘
```

**状态集合**：

| 类别 | 状态 |
|---|---|
| 执行中 | `PENDING`, `CLAIMED`, `RUNNING` |
| 等待 | `WAITING_RETRY`, `WAITING_INPUT`, `RECONCILE`, `MANUAL` |
| 终态 | `SUCCEEDED`, `FAILED`, `CANCELLED` |

---

### 3.3 ToolEffect 状态机

**文件**: `agent/runtime/domain/models.py:102`

```text
┌──────────┐  dispatch  ┌────────────┐  success   ┌──────────┐
│ PREPARED ├──────────►│ DISPATCHED ├──────────► │COMMITTED │
└──────────┘            └─────┬──────┘            └──────────┘
                              │
                    ┌─────────┼─────────┐
                    │         │         │
                    ▼         ▼         ▼
             ┌────────┐ ┌────────┐ ┌─────────────┐
             │ FAILED │ │UNKNOWN │ │MANUAL_REQUIRED│
             └────────┘ └───┬────┘ └─────────────┘
                            │
                            ▼
                       ┌───────────┐
                       │RECONCILING│
                       └───────────┘
```

**状态集合**：

| 类别 | 状态 |
|---|---|
| 初始 | `PREPARED` |
| 执行中 | `DISPATCHED`, `RECONCILING` |
| 终态 | `COMMITTED`, `FAILED` |
| 不确定 | `UNKNOWN`, `MANUAL_REQUIRED` |

**Effect Class**：
- `READ_ONLY`：可安全重试
- `IDEMPOTENT_EFFECT`：必须透传稳定 key
- `NON_IDEMPOTENT_EFFECT`：不可透明重试
- `UNKNOWN_EFFECT`：未声明，默认保守

---

## 4. 表结构设计

### 4.1 核心表清单

| 表名 | 作用 | 关键特性 |
|---|---|---|
| `runs` | Run 记录 | 唯一活跃约束，revision CAS |
| `activities` | Activity 记录 | lease/fencing，claim 索引 |
| `run_events` | 事件流 | append-only，触发器保护 |
| `checkpoints` | 检查点 | revision CAS，latest 索引 |
| `tool_executions` | 工具执行账本 | stable slot，reconcile 支持 |
| `conversations` | 会话 | 轮次管理 |
| `run_requests` | 幂等记录 | 复合主键，digest 校验 |
| `artifact_metadata` | 工件元数据 | SHA-256 内容寻址 |
| `artifact_links` | 工件关联 | 来源追踪 |
| `signals` | 信号记录 | 幂等消费，late 拒绝 |
| `timers` | 定时器 | SCHEDULED → FIRED 一次 |
| `runtime_workers` | Worker 注册 | heartbeat，release map |

---

### 4.2 runs 表

```sql
CREATE TABLE runs (
    run_id TEXT PRIMARY KEY,
    schema_version TEXT NOT NULL,
    request_id TEXT NOT NULL UNIQUE,
    client_request_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
    turn_seq INTEGER NOT NULL CHECK (turn_seq >= 1),
    turn_id TEXT NOT NULL UNIQUE,
    principal_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    engine TEXT NOT NULL CHECK (engine IN ('plan_execute','agent_loop','native_loop')),
    deadline_at INTEGER NOT NULL,
    cancel_token_id TEXT NOT NULL UNIQUE,
    release_fingerprint TEXT NOT NULL,
    input_event_id TEXT NOT NULL UNIQUE,
    attachment_refs_json TEXT NOT NULL,
    input_text TEXT NOT NULL,
    state TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 0,
    next_seq INTEGER NOT NULL DEFAULT 1,
    current_activity_id TEXT,
    terminal_status TEXT,
    terminal_payload_json TEXT,
    pending_input_json TEXT,
    trace_id TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    
    -- 约束
    CHECK (terminal_status IS NULL OR state = terminal_status),
    UNIQUE (conversation_id, turn_seq)
);

-- 关键索引
CREATE UNIQUE INDEX uq_active_run_per_conversation ON runs(conversation_id)
WHERE state NOT IN ('SUCCEEDED','FAILED','CANCELLED','TIMED_OUT','REJECTED','INCOMPATIBLE_RELEASE');

CREATE INDEX ix_runs_status_created ON runs(state, created_at);
```

**设计要点**：
- `uq_active_run_per_conversation`：保证同一 conversation 最多一个非终态 Run
- `terminal_status IS NULL OR state = terminal_status`：终态时 state 必须等于 terminal_status
- `next_seq`：事件序列号生成器，与 event append 同事务递增

---

### 4.3 activities 表

```sql
CREATE TABLE activities (
    activity_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    type TEXT NOT NULL,
    logical_key TEXT NOT NULL,
    state TEXT NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 0,
    available_at INTEGER NOT NULL,
    lease_owner TEXT,
    lease_expires_at INTEGER,
    fencing_token INTEGER NOT NULL DEFAULT 0,
    revision INTEGER NOT NULL DEFAULT 0,
    result_json TEXT,
    error_json TEXT,
    pending_input_json TEXT,
    resume_payload_json TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    
    UNIQUE (run_id, logical_key),
    CHECK ((lease_owner IS NULL) = (lease_expires_at IS NULL))
);

-- 关键索引：claim_next 使用
CREATE INDEX ix_activities_claim ON activities(state, available_at, created_at, activity_id);
CREATE INDEX ix_activities_lease ON activities(state, lease_expires_at);
```

**设计要点**：
- `ix_activities_claim`：加速 `claim_next` 查询
- `logical_key`：stable slot，崩溃恢复时重新计算相同 ID
- `lease_owner/lease_expires_at`：成对出现或成对 NULL

---

### 4.4 run_events 表

```sql
CREATE TABLE run_events (
    event_id TEXT PRIMARY KEY,
    schema_version TEXT NOT NULL,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    turn_id TEXT NOT NULL,
    activity_id TEXT REFERENCES activities(activity_id),
    tool_execution_id TEXT,
    seq INTEGER NOT NULL CHECK (seq >= 1),
    event_type TEXT NOT NULL,
    producer TEXT NOT NULL,
    payload_json TEXT,
    payload_ref TEXT,
    visibility TEXT NOT NULL,
    sensitivity TEXT NOT NULL,
    occurred_at INTEGER NOT NULL,
    terminal_status TEXT,
    release_fingerprint TEXT NOT NULL,
    
    UNIQUE (run_id, seq),
    CHECK (payload_json IS NULL OR payload_ref IS NULL)
);

-- append-only 保护
CREATE TRIGGER run_events_no_update
BEFORE UPDATE ON run_events BEGIN
  SELECT RAISE(ABORT, 'RUN_EVENTS_APPEND_ONLY');
END;

CREATE TRIGGER run_events_no_delete
BEFORE DELETE ON run_events BEGIN
  SELECT RAISE(ABORT, 'RUN_EVENTS_APPEND_ONLY');
END;

-- 终态事件唯一
CREATE UNIQUE INDEX uq_run_terminal_event ON run_events(run_id)
WHERE event_type = 'RUN_TERMINATED';
```

**设计要点**：
- 触发器保证 append-only
- `UNIQUE(run_id, seq)`：seq 单调递增
- `uq_run_terminal_event`：最多一个终态事件

---

### 4.5 tool_executions 表

```sql
CREATE TABLE tool_executions (
    tool_execution_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    activity_id TEXT NOT NULL REFERENCES activities(activity_id),
    logical_key TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    release_digest TEXT NOT NULL,
    effect_class TEXT NOT NULL,
    effect_status TEXT NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 0,
    dispatch_fencing_token INTEGER,
    request_digest TEXT NOT NULL,
    request_json TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    result_json TEXT,
    result_ref TEXT,
    error_json TEXT,
    external_object_id TEXT,
    reconcile_state TEXT,
    supports_reconcile INTEGER NOT NULL CHECK (supports_reconcile IN (0,1)),
    revision INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    
    UNIQUE (run_id, logical_key)
);
```

**设计要点**：
- `logical_key`：stable slot，重放时校验 tool_name/digest 一致
- `effect_status`：ToolEffect 状态机
- `supports_reconcile`：是否支持人工查询

---

## 5. 数据流与事务边界

### 5.1 Admission 事务

```text
BEGIN IMMEDIATE
├─ 查 run_requests (幂等校验)
├─ 查 active_releases (release 校验)
├─ 查 artifact_metadata (附件校验)
├─ INSERT/UPDATE conversations
├─ INSERT runs
├─ INSERT activities
├─ INSERT run_requests
├─ INSERT artifact_links
├─ INSERT run_events (seq 1-4)
│   ├─ USER_MESSAGE_COMMITTED
│   ├─ RUN_STATUS_CHANGED (None → ACCEPTED)
│   ├─ RUN_STATUS_CHANGED (ACCEPTED → DISPATCH_PENDING)
│   └─ ACTIVITY_STATUS_CHANGED (None → PENDING)
└─ COMMIT
```

### 5.2 Claim 事务

```text
BEGIN IMMEDIATE
├─ SELECT ... FROM activities WHERE state='PENDING' ... LIMIT 1
├─ UPDATE activities SET state='CLAIMED', fencing_token+1
├─ UPDATE runs SET state='RUNNING'
├─ INSERT run_events
│   ├─ ACTIVITY_STATUS_CHANGED (PENDING → CLAIMED)
│   └─ RUN_STATUS_CHANGED (DISPATCH_PENDING → RUNNING)
└─ COMMIT
```

### 5.3 Finalize 事务

```text
BEGIN IMMEDIATE
├─ UPDATE runs SET state=terminal_status, terminal_status=..., terminal_payload=...
├─ INSERT run_events
│   ├─ ASSISTANT_MESSAGE_COMMITTED
│   ├─ CITATION_SET_COMMITTED
│   └─ RUN_TERMINATED
└─ COMMIT
```

---

## 6. 源码位置索引

| 类别 | 文件 | 说明 |
|---|---|---|
| 领域模型 | `agent/runtime/domain/models.py` | 所有 Record/Event/State 定义 |
| 状态枚举 | `agent/runtime/domain/models.py:17-126` | RunStatus/ActivityStatus/EventType 等 |
| 表结构 | `agent/runtime/adapters/sqlite/schema.sql` | 完整建表语句（单一当前 schema，无 migration） |
| 状态机规格 | `docs/reliability/state-machines.md` | 冻结的邻接表与错误码 |
| Store 接口 | `agent/runtime/ports/store.py` | Protocol 定义 |
| SQLite 实现 | `agent/runtime/adapters/sqlite/store.py` | 具体实现 |
| JSON Schema | `docs/reliability/schemas/` | 冻结的序列化契约 |

---

## 附录：设计原则总结

| 原则 | 体现 |
|---|---|
| **单一事实源** | 每类数据只在一个表中权威存储 |
| **append-only** | run_events 只追加，触发器保护 |
| **CAS 防并发** | revision/fencing_token 双重校验 |
| **stable slot** | UUIDv5 派生，崩溃恢复可重算 |
| **事务边界清晰** | admission/claim/finalize 各自原子 |
| **幂等设计** | idempotency_key + digest 校验 |
| **lease/fencing** | 防 Worker 崩溃锁死和过期提交 |

---

*文档生成时间: 2026-08-09*
*基于项目版本: sxw_agent-2_demo R0 冻结规格*
