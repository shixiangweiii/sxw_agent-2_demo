# Conversation-Run-Activity 层次模型详解

本文档梳理 Runtime 中 Conversation、Run、Activity 三层实体的层次关系、设计意图和关键约束。

---

## 目录

- [1. 层次结构总览](#1-层次结构总览)
- [2. 实体详解](#2-实体详解)
- [3. 关系映射](#3-关系映射)
- [4. 关键约束](#4-关键约束)
- [5. 典型场景](#5-典型场景)
- [6. 源码位置索引](#6-源码位置索引)

---

## 1. 层次结构总览

### 1.1 三层模型

```text
┌─────────────────────────────────────────────────────────────────────────────┐
│                         Conversation (会话层)                               │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │  一次完整的对话上下文                                                 │   │
│  │  - 属于某个用户 (principal_id)                                       │   │
│  │  - 属于某个 Agent (agent_id)                                         │   │
│  │  - 包含多轮对话（多个 Run）                                           │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
│                                │                                            │
│                                │ 1:N                                        │
│                                ▼                                            │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │                         Run (执行层)                                 │   │
│  │  ┌─────────────────────────────────────────────────────────────┐   │   │
│  │  │  一次用户 query 的执行过程                                    │   │   │
│  │  │  - 包含用户输入 (input_text)                                  │   │   │
│  │  │  - 选择某个引擎 (engine)                                      │   │   │
│  │  │  - 产生多个 Activity                                          │   │   │
│  │  └─────────────────────────────────────────────────────────────┘   │   │
│  │                                │                                    │   │
│  │                                │ 1:N                                │   │
│  │                                ▼                                    │   │
│  │  ┌─────────────────────────────────────────────────────────────┐   │   │
│  │  │                    Activity (工作单元层)                      │   │   │
│  │  │  - ENGINE_RUN: 主引擎执行                                   │   │   │
│  │  │  - TOOL_CALL: 工具调用                                     │   │   │
│  │  │  - MODEL_CALL: 模型调用                                    │   │   │
│  │  │  - CHECKPOINT: 检查点                                      │   │   │
│  │  │  - WAIT_INPUT: 等待用户输入                                │   │   │
│  │  └─────────────────────────────────────────────────────────────┘   │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 1.2 类比理解

| 层次 | 类比 | 说明 |
|---|---|---|
| **Conversation** | 一次完整的聊天会话 | 用户和 Agent 之间的持续对话 |
| **Run** | 一轮问答 | 用户问一个问题，Agent 执行并回答 |
| **Activity** | 执行步骤 | 引擎执行、工具调用、检查点等具体工作 |

---

## 2. 实体详解

### 2.1 Conversation（会话）

**表**: `conversations`

```sql
CREATE TABLE conversations (
    conversation_id TEXT PRIMARY KEY,     -- 会话 ID
    principal_id TEXT NOT NULL,           -- 用户 ID
    agent_id TEXT NOT NULL,               -- Agent ID
    next_turn_seq INTEGER NOT NULL,       -- 下一个轮次序号
    revision INTEGER NOT NULL,            -- CAS 版本号
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
```

**核心职责**：
- 维护对话的归属关系（用户 + Agent）
- 管理轮次序号 `next_turn_seq`，每个 Run 分配一个递增的 `turn_seq`
- 支持多轮对话的历史关联

**创建时机**：
- CreateRun 时 `conversation_id=null` → 自动创建新 Conversation
- CreateRun 时 `conversation_id=conv_xxx` → 复用已有 Conversation

---

### 2.2 Run（运行）

**表**: `runs`

```sql
CREATE TABLE runs (
    run_id TEXT PRIMARY KEY,              -- Run ID
    conversation_id TEXT NOT NULL,        -- 所属会话
    turn_seq INTEGER NOT NULL,            -- 在会话中的轮次
    principal_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    engine TEXT NOT NULL,                 -- 引擎类型
    input_text TEXT NOT NULL,             -- 用户输入
    state TEXT NOT NULL,                  -- 运行状态
    deadline_at INTEGER NOT NULL,         -- 截止时间
    -- ... 其他字段
);
```

**核心职责**：
- 封装一次用户 query 的执行上下文
- 管理执行状态（DISPATCH_PENDING → RUNNING → 终态）
- 关联所有产生的 Activity 和 Event

**关键约束**：
- 同一 conversation 最多一个非终态 Run
- 终态最多一个，不可变

---

### 2.3 Activity（活动）

**表**: `activities`

```sql
CREATE TABLE activities (
    activity_id TEXT PRIMARY KEY,         -- Activity ID
    run_id TEXT NOT NULL,                 -- 所属 Run
    type TEXT NOT NULL,                   -- 类型
    logical_key TEXT NOT NULL,            -- 逻辑键（stable slot）
    state TEXT NOT NULL,                  -- 状态
    attempt INTEGER NOT NULL,             -- 尝试次数
    lease_owner TEXT,                     -- 租约持有者
    lease_expires_at INTEGER,             -- 租约过期时间
    fencing_token INTEGER NOT NULL,       -- 围栏令牌
    -- ... 其他字段
);
```

**核心职责**：
- 代表一个可执行的工作单元
- 管理执行权（lease/fencing）
- 支持崩溃恢复（通过 logical_key 重新计算 stable slot）

**Activity 类型**：

| 类型 | 说明 |
|---|---|
| `ENGINE_RUN` | 主引擎执行（每个 Run 必有一个） |
| `TOOL_CALL` | 工具调用 |
| `MODEL_CALL` | 模型调用 |
| `CHECKPOINT` | 检查点保存 |
| `WAIT_INPUT` | 等待用户输入 |
| `RETRIEVAL` | 检索 |
| `FINALIZE` | 终结 |

---

## 3. 关系映射

### 3.1 外键关系

```text
conversations
    │
    │ conversation_id (1:N)
    ▼
runs
    │
    │ run_id (1:N)
    ▼
activities
    │
    │ activity_id (1:N)
    ▼
tool_executions / checkpoints / signals / timers
```

### 3.2 数据流向

```text
用户输入 → CreateRun → 创建/复用 Conversation
                    → 创建 Run
                    → 创建 ENGINE_RUN Activity
                    → Worker 领取 Claim
                    → 执行引擎
                    → 产生子 Activity（工具调用等）
                    → 产生 Events
                    → 终态
```

---

## 4. 关键约束

### 4.1 同一 Conversation 最多一个非终态 Run

```sql
CREATE UNIQUE INDEX uq_active_run_per_conversation ON runs(conversation_id)
WHERE state NOT IN ('SUCCEEDED','FAILED','CANCELLED','TIMED_OUT','REJECTED','INCOMPATIBLE_RELEASE');
```

**设计意图**：
- 保证对话历史的一致性
- 防止并发执行导致历史冲突
- 必须等上一个 Run 终态后，才能创建下一个 Run

**违反时**：返回 `409 CONVERSATION_BUSY`

---

### 4.2 Run 的轮次管理

```sql
-- conversations 表
next_turn_seq INTEGER NOT NULL  -- 下一个可用的 turn_seq

-- runs 表
turn_seq INTEGER NOT NULL       -- 该 Run 在 conversation 中的轮次
UNIQUE (conversation_id, turn_seq)  -- 唯一约束
```

**流程**：
1. CreateRun 时，从 conversation 获取 `next_turn_seq`
2. 分配给新 Run 作为 `turn_seq`
3. conversation 的 `next_turn_seq` 递增

---

### 4.3 Activity 的 Stable Slot

```sql
UNIQUE (run_id, logical_key)  -- 同一 Run 内 logical_key 唯一
```

**设计意图**：
- `logical_key` 用于崩溃恢复时重新计算 stable slot
- native 引擎：`model_activity_id + tool ordinal` 派生 UUIDv5
- ADK 引擎：`invocation_activity + turn ordinal + call ordinal` 派生 UUIDv5
- 保证相同逻辑位置的 Activity 重放时得到相同 ID

---

## 5. 典型场景

### 5.1 首次对话

```text
用户: "什么是混合召回？"

CreateRun:
  conversation_id: null  ← 没指定，新建
  input_text: "什么是混合召回？"

结果:
  新建 conversation (conv_001)
  新建 run (run_001, turn_seq=1)
  新建 activity (act_001, type=ENGINE_RUN)
  
  conversation.next_turn_seq = 2
```

### 5.2 多轮对话

```text
用户: "能详细解释吗？"

CreateRun:
  conversation_id: "conv_001"  ← 复用上一轮的 conversation
  input_text: "能详细解释吗？"

结果:
  复用 conversation (conv_001)
  新建 run (run_002, turn_seq=2)
  新建 activity (act_002, type=ENGINE_RUN)
  
  conversation.next_turn_seq = 3
```

### 5.3 并发冲突

```text
Run 1 (run_001) 正在执行 (state=RUNNING)

用户: "新问题"

CreateRun:
  conversation_id: "conv_001"  ← 同一 conversation

结果:
  409 CONVERSATION_BUSY
  "another non-terminal run already owns this conversation"
```

### 5.4 换话题（新建会话）

```text
用户: "换个话题，帮我写代码"

CreateRun:
  conversation_id: null  ← 不指定，新建 conversation

结果:
  新建 conversation (conv_002)
  新建 run (run_003, turn_seq=1)
```

---

## 6. 源码位置索引

| 功能 | 文件 | 行号 |
|---|---|---|
| Conversation 表定义 | `agent/runtime/adapters/sqlite/migrations/001_runtime.sql` | 1 |
| Run 表定义 | `agent/runtime/adapters/sqlite/migrations/001_runtime.sql` | 25 |
| Activity 表定义 | `agent/runtime/adapters/sqlite/migrations/001_runtime.sql` | 77 |
| 活跃 Run 唯一约束 | `agent/runtime/adapters/sqlite/migrations/001_runtime.sql` | 63 |
| Conversation 创建逻辑 | `agent/runtime/adapters/sqlite/store.py` | 427 |
| Run 创建逻辑 | `agent/runtime/adapters/sqlite/store.py` | 458 |
| Activity 创建逻辑 | `agent/runtime/adapters/sqlite/store.py` | 476 |
| CONVERSATION_BUSY 错误 | `agent/runtime/adapters/sqlite/store.py` | 541 |

---

## 附录：层次关系速查

| 关系 | 类型 | 说明 |
|---|---|---|
| Conversation → Run | 1:N | 一个会话包含多个 Run |
| Run → Activity | 1:N | 一个 Run 包含多个 Activity |
| Run → Event | 1:N | 一个 Run 产生多个 Event |
| Activity → Checkpoint | 1:N | 一个 Activity 可有多个检查点 |
| Activity → ToolExecution | 1:N | 一个 Activity 可执行多个工具 |
| Run → Signal | 1:N | 一个 Run 可接收多个信号 |
| Run → Timer | 1:N | 一个 Run 可有多个定时器 |

---

*文档生成时间: 2026-08-09*
*基于项目版本: sxw_agent-2_demo R0 冻结规格*
