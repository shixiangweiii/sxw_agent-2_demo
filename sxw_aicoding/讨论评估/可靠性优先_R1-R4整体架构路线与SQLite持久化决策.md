# 可靠性优先：R1–R4 整体架构路线与 SQLite 持久化决策

> 决策日期：2026-08-09  
> 当前项目：sxw_agent-2_demo@d594962  
> 决策状态：已确认，作为后续可靠性改造的路线基线  
> 文档性质：架构路线设计与决策记录，不代表相关能力已经实现  
> 核心结论：先完成可靠性地基，再建设记忆系统，最后深化行为评测。可靠性阶段按 R0 → R1 → R2 → R3 → R4 推进；第一阶段以本地 SQLite 为唯一 Runtime 持久化事实源，目标是单机、可恢复、可验证的可靠执行，不宣称分布式高可用。

---

## 1. 文档目标

本文用于固化本轮关于“持久化可靠执行、长任务和单一事实来源”的讨论过程、决策依据、最终选择与实施路线，避免后续开发过程中重复讨论或因局部实现重新产生双事实源。

本文重点回答：

1. 为什么可靠性必须先于记忆和行为评测建设？
2. 为什么单一事实来源是可靠执行的一部分，而不是独立的数据治理工作？
3. 当前项目中哪些状态正在充当重复事实源？
4. R0、R1、R2、R3、R4 分别负责解决什么问题？
5. 为什么第一阶段选择 SQLite，而不是立即引入 PostgreSQL 或 Temporal？
6. SQLite 能承诺什么、不能承诺什么？
7. 三代引擎如何共享 Canonical Runtime，又如何诚实保留不同恢复粒度？
8. Tool、Artifact、Evidence 为什么必须在长任务之前完成可靠性建模？
9. 哪些旧状态和写路径应在 R4 被删除或降级？
10. 不做 Memory 和行为评测的阶段，如何证明可靠性改造真正成立？

## 2. 项目背景与长期前提

本项目由公司生产项目的核心链路抽取、简化而来，主要用于个人学习、技术方案验证和面试准备，不承担真实线上流量。

后续架构改造遵循以下长期有效前提：

- 不考虑历史接口兼容；
- 不考虑旧数据结构兼容；
- 不为既有技术债保留兼容层；
- 不考虑线上灰度和存量数据迁移；
- 旧设计不合理时可以直接替换或删除；
- 优先采用先进、清晰、可验证且符合生产级工程原则的方案；
- 生产级不等于无条件增加基础设施，而是边界、状态、事务、恢复和验收语义必须完整。

因此，本路线不会复制生产项目常见的双写、shadow、灰度、旧表回读和长期兼容 DTO。开发期间可以分阶段提交，但最终状态必须只有一套权威写路径。

## 3. 当前问题与代码事实

### 3.1 HTTP、执行和 SSE 仍是同一生命周期

当前 agent/api/chat.py 在一次 StreamingResponse generator 内直接驱动：

~~~text
HTTP request
  → engine.run_stream
  → citation
  → trace
  → SSE
~~~

客户端断开会沿生成器向下传播取消，执行是否继续取决于请求生命周期。它适合“发起一次请求并观看一次回答”，但不具备：

- durable accepted；
- 请求与执行解耦；
- Run 独立状态查询；
- 跨请求 cancel/signal；
- 服务重启恢复；
- SSE after_seq 重放。

代码证据：

- agent/api/chat.py:33-122
- agent/skills/stream_merge.py:30-95

### 3.2 缺少独立 Run 实体

当前 ReasoningEngine 只接收 RunContext，字段主要是 agent、user、session、message、settings，没有：

- run_id；
- turn_id；
- request_id；
- idempotency_key；
- deadline；
- release fingerprint；
- revision；
- checkpoint；
- terminal。

代码证据：

- agent/engine/base.py:24-40

### 3.3 StreamEvent 同时承担引擎事件和 SSE DTO

当前 StreamEvent 只有 event 和 data，没有稳定 event_id、run_id、seq、schema_version、visibility、sensitivity 和 terminal_status。

它既是引擎向外输出的内部事件，又被直接格式化为 SSE，因此：

- 无法在发布前持久提交；
- 无法按 seq 重放；
- 无法证明 terminal 唯一；
- 无法区分 Runtime Event 与 Delivery Event；
- EOF、done、error 和业务终态容易混在一起。

代码证据：

- agent/stream/event_converters.py:12-20

### 3.4 会话和历史存在多套状态

当前：

- agent_loop 和 plan_execute 使用 ADK InMemorySessionService；
- native_loop 使用独立 HistoryStore；
- native HistoryStore 采用 get-copy → replace；
- native compact 可能用摘要替换历史；
- 两套历史重启后都会丢失；
- 两套结构回答的都是“下一轮模型应该看到什么”，但没有 Canonical Conversation Event 作为统一来源。

代码证据：

- agent/session/session_service.py:14-30
- agent/engine/native_loop/history.py:21-56
- agent/engine/native_loop/engine.py:91-134

### 3.5 计划和进度没有统一裁判

当前计划状态分别存在于：

- plan_execute 的局部 plan 列表；
- agent_loop 的 ADK tool_context.state；
- native_loop 的 request-local tool_state。

代码证据：

- agent/engine/plan_execute/plan_execute_engine.py:20-40
- agent/engine/loop_tools/task_plan_tool.py:14-29
- agent/engine/native_loop/loop.py:87-100

必须避免后续同时让 Run Step.status 和 WorkingState.completed_steps/current_step 裁决执行进度。

目标区分：

- Runtime Activity：由 Runtime 调度并裁决是否完成；
- Model Plan Item：模型对任务的认知计划，只是 WorkingState 内容；
- Plan UI 状态：从 Model Plan 和 Runtime Activity 派生的展示投影。

### 3.6 Tool 没有持久执行账本

当前 ToolCall、ToolResult 主要存在于：

- ADK/native history；
- SSE；
- trace；
- 技能调用日志；
- 进程内并发任务。

系统无法在重启后稳定回答：

- Tool 是否已经发出？
- 外部副作用是否已经提交？
- Tool 成功但 ACK 是否丢失？
- 是否允许自动重试？
- 应复用哪个完整结果？

### 3.7 Artifact 仍是内存实现

当前图片 Artifact 使用 ADK InMemoryArtifactService，重启即丢。大型工具结果在 native_loop 中主要通过字符截断控制，没有统一、持久、可校验的 ArtifactRef。

代码证据：

- agent/artifacts/artifact_service.py:1-32
- agent/engine/native_loop/messages.py:221-255

### 3.8 Evidence 与 RAG 文档生命周期不完整

当前 ARAG 入库顺序为：

~~~text
parse/chunk
  → embed
  → vector_store.add
  → fulltext_index.add
~~~

vector 与 fulltext 是顺序双写，没有原始 Document/version 权威记录。两类索引只按 chunk_id upsert；当文档缩短或切片策略改变时，旧 chunk 可能残留。

当前 citation 主要依赖一次请求内的 n、title、doc_id，缺少：

- content_version；
- index_version；
- page/span；
- ACL/scope；
- evidence_id；
- EvidenceSet Artifact；
- 检索状态 HIT/MISS/DEGRADED/DENIED/ERROR。

代码证据：

- arag/api/index.py:20-31
- arag/store/vector_store.py:161-187
- arag/store/fulltext_index.py:42-53
- arag/schemas.py:25-44
- agent/citation/citation_injector.py:19-45

### 3.9 Trace 只能诊断，不能恢复

当前 trace 采用 JSONL 追加并允许留下半截轨迹，这对问题分析很有价值。但 trace：

- 可能采样或关闭；
- payload 会裁剪、脱敏或摘要；
- 没有业务事务；
- 没有状态 CAS；
- 没有唯一 terminal 约束；
- 不能确认 Tool effect。

因此 trace 必须长期保持“诊断事实”，不得参与 Run 恢复和业务裁决。

代码证据：

- common/trace.py:1-24
- agent/stream/trace_tap.py:27-81

## 4. 总体战略决策

### 4.1 建设顺序

最终建设顺序为：

~~~text
R0 可靠性规格先行
  → R1 Canonical Runtime 外壳
  → R2 Tool / Artifact / Evidence 可靠性
  → R3 长任务纵向闭环
  → R4 删除旧事实源并完成可靠性门禁
  → Memory System
  → 行为质量与三引擎评测深化
~~~

该顺序的含义不是“评测不重要”，而是区分两类验证：

- 可靠性验证：状态机、不变量、事务、并发、故障恢复，必须伴随 R0–R4；
- 行为评测：回答质量、Prompt、RAG 排名、LLM judge、三引擎质量成本对比，后置。

### 4.2 可靠性范围

本轮可靠性包含：

- 持久化；
- durable accepted；
- 显式 Run 和终态；
- 幂等；
- Tool effect；
- Artifact；
- Evidence；
- checkpoint；
- 长任务；
- timer；
- signal/HITL；
- deadline；
- cancel；
- SSE replay；
- 单一事实来源；
- release/schema 固定；
- 自动化可靠性门禁。

### 4.3 暂不建设的能力

本轮明确后置：

- Semantic Memory；
- Episodic Memory；
- Procedural Memory；
- 自动 Memory extraction；
- Mem0/向量记忆/图记忆；
- Context Compiler 完整体；
- Prompt 优化；
- rerank 质量优化；
- LLM judge；
- 三代引擎真实 LLM 大规模对比；
- 企业 IAM、跨租户治理；
- 分布式多机高可用；
- WebSocket。

### 4.4 WorkingState 不属于后置 Memory

可靠执行仍必须建设最小 WorkingState。它是当前 Run 的 checkpoint，不是跨 Session 长期记忆。

第一阶段只保存：

- goal；
- constraints；
- model_plan；
- confirmed_facts；
- open_questions；
- pending_input；
- budget；
- artifact_refs；
- evidence_refs；
- release_fingerprint。

Runtime Activity 的完成状态不重复保存在 WorkingState，由 Runtime Store 投影得出。

## 5. 持久化技术选型决策

### 5.1 最终选择

R1–R4 第一阶段采用本地 SQLite。

定位：

> SQLite 是单机 Canonical Runtime 的唯一持久化事实源，用于验证生产级可靠性语义，而不是模拟分布式数据库。

### 5.2 选择 SQLite 的理由

1. 当前项目主要在本机运行，真实需求是进程重启恢复和断线续传，不是多机容灾。
2. Run、Event、Checkpoint、ToolExecution 可以在同一个本地事务中提交，SSOT 边界清晰。
3. 不需要先引入 PostgreSQL、MQ、Temporal、MinIO 等多套基础设施，便于聚焦 Agent Runtime 本身。
4. SQLite 支持事务、唯一约束、WAL、CAS、RETURNING，足以验证 R1–R4 的核心不变量。
5. 当前数据规模和并发量适合单写者模型。
6. 项目不要求历史数据兼容，未来切换技术时可以直接重置存储，不需要双写迁移。

### 5.3 未立即选择 PostgreSQL 的原因

PostgreSQL 更适合：

- 多实例；
- 高并发写；
- SKIP LOCKED worker queue；
- 更完整的在线迁移；
- 主从、备份和容灾；
- 生产运维。

但这些不是当前阶段的核心验证目标。现在引入 PostgreSQL 会增加部署和排障成本，却不会自动解决状态所有权、Tool effect、commit boundary 和恢复语义。

### 5.4 未立即选择 Temporal 的原因

Temporal 的优势是：

- durable workflow history；
- activity retry；
- timer；
- signal；
- cancellation；
- worker failover；
- deterministic replay。

但当前阶段若同时保留 SQLite RunStore 和 Temporal Workflow History，会立即引入“谁裁决 Run/Step”的新问题。

因此第一阶段不引入 Temporal，由 SQLite Runtime 明确拥有：

- Run；
- Activity；
- timer；
- signal；
- checkpoint；
- terminal。

未来若引入 Temporal，必须进行一次干净的所有权切换，而不是双写。

### 5.5 方案对比

| 方案 | 优点 | 主要问题 | 当前决策 |
|---|---|---|---|
| 继续纯内存 | 简单 | 重启丢失，无法验证 durable | 拒绝 |
| 文件 JSON/WAL | 容易理解 | 事务、并发、查询和约束能力弱 | 拒绝 |
| SQLite | 单机事务完整、低运维、可验证核心语义 | 单写者、无多机 HA | 采用 |
| PostgreSQL | 并发与生产运维能力强 | 当前基础设施成本偏高 | 后续可替换 |
| Temporal | 长任务语义最完整 | 引入后必须成为唯一执行权威 | 后续独立决策 |

## 6. 能力声明与边界

### 6.1 第一阶段可以承诺

- 同一台机器上的进程崩溃恢复；
- API 与执行生命周期解耦；
- 请求持久受理；
- 相同请求幂等返回同一 Run；
- 显式 Run 状态和唯一 terminal；
- Activity 重试与 checkpoint；
- WAITING_INPUT 和 signal；
- deadline、timer、cancel；
- Tool effect 持久记录；
- Artifact digest 校验；
- SSE after_seq 重放；
- 投影重建；
- 本地 worker 重启后继续执行。

### 6.2 第一阶段不能宣称

- 主机或磁盘损坏后恢复；
- 多节点自动故障转移；
- 跨机器 worker fencing；
- 跨地域容灾；
- 高吞吐多写者；
- Temporal 级 deterministic workflow replay；
- 分布式 exactly-once；
- 生产级对象存储持久性；
- 生产级多租户隔离。

因此对外准确表述应为：

> 单机持久化可靠 Agent Runtime 参考实现。

而不是：

> 分布式高可用生产 Runtime。

## 7. 目标架构

~~~mermaid
flowchart TB
    CLIENT["Client / Web UI"] --> API["Run API"]
    API --> ADMISSION["Request Admission + Idempotency"]
    ADMISSION --> DB["SQLite Canonical Runtime"]

    DB --> WORKER["Runtime Worker"]
    WORKER --> COORD["RunCoordinator"]
    COORD --> ADAPTER["Engine Adapter"]
    ADAPTER --> PE["plan_execute"]
    ADAPTER --> AL["agent_loop"]
    ADAPTER --> NL["native_loop"]

    COORD --> TOOL["Tool Broker"]
    TOOL --> LEDGER["ToolExecution"]
    TOOL --> ART["Content-addressed Artifact Store"]
    TOOL --> RAG["ARAG / Skill / A2A / Sandbox"]

    COORD --> EVENT["Canonical Event Append"]
    EVENT --> DB
    DB --> SSE["SSE Projection / after_seq"]
    DB --> HISTORY["Conversation Projection"]
    DB --> STATUS["Run Status Projection"]

    SIGNAL["Signal / Approval / Callback"] --> API
    CANCEL["Cancel"] --> API
    TIMER["Retry / Deadline Timer"] --> DB

    TRACE["Trace JSONL"] -. "diagnostic only" .-> COORD
~~~

## 8. 单一事实来源设计

### 8.1 State Ownership Registry

| 数据域/问题 | 唯一权威 | 允许的派生数据 | 禁止行为 |
|---|---|---|---|
| 请求是否已受理 | run_requests | HTTP response、日志 | 根据 SSE 是否打开反推受理 |
| Run 当前状态 | runs | status cache、UI 状态 | done/EOF/Session 独立改 Run |
| Activity 执行进度 | activities | plan UI、trace | WorkingState 重复保存 completed activities |
| 当前认知状态 | checkpoints | Prompt preview | ADK/native 各自做恢复 checkpoint |
| 用户输入和用户可见事件 | run_events | SSE、历史、报表 | SSE DTO 反向修改事件 |
| Tool effect | tool_executions | 日志、trace、指标 | 根据 HTTP 超时猜测成功/失败 |
| 外部业务对象 | 外部 Tool 系统 | external_object_ref | Runtime 声称能裁决外部对象真实状态 |
| Artifact 完整内容 | Artifact Store | preview、索引 | 完整结果复制进 Session/Event |
| Artifact 元数据 | artifact_metadata | UI/trace 投影 | 仅靠路径名证明内容 |
| 原始文档和有效版本 | Document Store | vector/BM25 index | 索引反向成为文档权威 |
| Evidence | Evidence DTO / EvidenceSet | citation 展示 | filename 或 [n] 作为稳定主键 |
| 客户端投递位置 | delivery_cursors | 浏览器本地 cursor | delivery ACK 改 Run 终态 |
| Release | immutable release manifest | active pointer | 运行中静默切换 Prompt/Tool 版本 |
| Trace | Trace Store | 评测摘要 | 用 trace 恢复 Run |
| 长期记忆 | 后续 Typed Memory Store | 向量索引 | 本轮提前建设空壳 MemoryStore |

### 8.2 SSOT 不等于一个数据库

第一阶段可以存在：

- runtime.db：Runtime 权威；
- rag.db：Document/version/index job 权威；
- local_storage/artifacts：Artifact 完整内容；
- local_storage/traces：诊断轨迹。

关键不是物理上只有一个文件，而是每个问题只有一个最终裁判。

## 9. SQLite 物理存储建议

### 9.1 Runtime 数据库

建议文件：

~~~text
local_storage/runtime/runtime.db
~~~

建议核心表：

~~~text
run_requests
runs
activities
run_events
checkpoints
tool_executions
artifact_metadata
signals
timers
delivery_cursors
projection_cursors
release_manifests
~~~

### 9.2 RAG 数据库

建议文件：

~~~text
local_storage/arag/rag.db
~~~

建议核心表：

~~~text
documents
document_versions
chunks
index_jobs
active_document_versions
~~~

vector、BM25 可以继续使用适合本地演示的实现，但必须是可重建投影，并记录 index version。

### 9.3 Artifact Store

大型内容不直接放 SQLite BLOB，使用内容寻址本地文件：

~~~text
local_storage/artifacts/sha256/<first-two>/<full-digest>
~~~

SQLite 只保存：

- artifact_id；
- sha256；
- mime_type；
- size；
- storage_uri；
- source_run_id；
- source_activity_id；
- source_tool_execution_id；
- sensitivity；
- retention_policy；
- preview；
- created_at。

### 9.4 SQLite 运行参数

至少启用：

~~~sql
PRAGMA journal_mode = WAL;
PRAGMA synchronous = FULL;
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;
~~~

约束：

- 数据库必须位于本机磁盘；
- 禁止放置在 NFS、共享盘、云同步目录；
- 事务内禁止调用 LLM、Tool、RAG、Skill 或等待人工；
- 长 I/O 前先提交领取状态，I/O 后用新事务提交结果；
- 写事务必须短；
- 不允许 SSE 长连接持有数据库事务。

## 10. 核心领域契约

### 10.1 RuntimeEnvelope

所有入口和引擎共享不可变 RuntimeEnvelope：

~~~text
schema_version
request_id
client_request_id
idempotency_key
conversation_id
turn_id
run_id
principal_id
agent_id
engine
deadline_at
cancel_token_id
release_fingerprint
input_event_id
attachment_refs
created_at
~~~

入口创建后，下游只能透传，不能重新生成业务 ID。

### 10.2 Run 状态机

非终态：

~~~text
ACCEPTED
DISPATCH_PENDING
RUNNING
WAITING_RETRY
WAITING_INPUT
CANCEL_REQUESTED
~~~

终态：

~~~text
SUCCEEDED
FAILED
CANCELLED
TIMED_OUT
REJECTED
INCOMPATIBLE_RELEASE
~~~

规则：

- terminal 最多提交一次；
- terminal 后 Run 状态不可回退；
- clean EOF 不是 SUCCEEDED；
- SSE 断连不是 CANCELLED；
- worker 丢失不是 FAILED；
- WAITING_RETRY 和 WAITING_INPUT 不是终态；
- UNKNOWN 默认属于 ToolEffect，不作为普通 Run 终态；
- 每次迁移记录 from、to、reason、revision、occurred_at。

### 10.3 Activity 状态机

~~~text
PENDING
  → CLAIMED
  → RUNNING
  → SUCCEEDED
     | FAILED
     | WAITING_RETRY
     | WAITING_INPUT
     | CANCELLED
~~~

Activity 类型至少包括：

- MODEL_CALL；
- TOOL_CALL；
- RETRIEVAL；
- CHECKPOINT；
- WAIT；
- DELIVERY；
- FINALIZE。

Model Plan Item 不等于 Activity，二者不得共用 status 字段。

### 10.4 ToolEffect 状态机

~~~text
PREPARED
  → DISPATCHED
  → COMMITTED
     | FAILED
     | UNKNOWN
       → RECONCILING
       → COMMITTED / FAILED / MANUAL_REQUIRED
~~~

语义：

- COMMITTED：Runtime 有确定证据表明副作用已提交；
- FAILED：确定没有提交或明确失败；
- UNKNOWN：可能已经发生，但 ACK 或查询证据不足；
- 外部工单、消息或支付对象仍由外部系统裁决；
- 平台只能裁决“Runtime 对本次调用的已知状态”。

### 10.5 Delivery 状态

~~~text
AVAILABLE
  → DELIVERED
  → ACKED
~~~

Canonical Event 已提交只表示服务端可供投递，不表示用户实际看见。

### 10.6 Canonical Runtime Event

建议字段：

~~~text
event_id
schema_version
run_id
turn_id
activity_id
tool_execution_id
seq
event_type
producer
payload
payload_ref
visibility
sensitivity
occurred_at
terminal_status
release_fingerprint
~~~

约束：

- 同一 Run 内 seq 单调递增；
- UNIQUE(run_id, seq)；
- UNIQUE(event_id)；
- append-only；
- 修正通过新事件表达；
- 大内容使用 payload_ref；
- 发布到 SSE 之前必须已提交。

### 10.7 三类输出事实

流式文本必须区分：

1. output_delta committed/available：已持久化，可供 SSE 发布；
2. assistant_message committed：完整回答可进入后续语义历史；
3. delivery cursor/ack：客户端投递或确认位置。

失败 Run 的部分 delta 可以被用户看到，但不能自动进入后续 Conversation History。

最终 assistant_message 和成功 terminal 必须在同一 SQLite 事务中提交。

### 10.8 ToolExecution 与幂等

不再创建一张与 ToolCall 状态重复的 IdempotencyLedger。幂等信息直接属于 tool_executions：

~~~text
tool_execution_id
run_id
activity_id
model_tool_call_id
tool_name
tool_release_digest
effect_class
idempotency_key
attempt
effect_status
request_digest
result_ref
external_object_ref
error_code
revision
~~~

禁止只用 tool_name + args hash 去重。相同参数在同一任务中可能是两次合法调用。

Runtime 必须生成稳定 activity_id/tool_execution_id，并将其作为外部 idempotency key 透传。

### 10.9 ArtifactRef

~~~text
artifact_id
sha256
mime_type
size
storage_uri
preview
sensitivity
retention_policy
source_run_id
source_activity_id
source_event_id
created_at
~~~

模型默认只看到 preview；需要完整内容时通过有界 range-read 工具读取。

### 10.10 Evidence DTO

~~~text
evidence_id
dataset_id
doc_id
document_version
chunk_id
chunk_content_hash
index_version
page
span
score
retrieval_source
scope_hash
origin_uri
query_id
retrieved_at
~~~

检索响应必须有明确状态：

~~~text
HIT
MISS
DEGRADED
DENIED
ERROR
~~~

空 chunks 不能同时表达 MISS、下游失败和无权限。

## 11. API 与执行生命周期

### 11.1 目标 API

~~~text
POST /api/v1/runs
GET  /api/v1/runs/{run_id}
GET  /api/v1/runs/{run_id}/events?after_seq=N
POST /api/v1/runs/{run_id}/cancel
POST /api/v1/runs/{run_id}/signals
~~~

### 11.2 创建 Run

~~~text
Client
  → POST /runs + client_request_id
  → SQLite transaction:
       insert/reuse run_request
       create Run
       append USER_MESSAGE
       create first Activity
  → commit
  → return 202 + run_id
~~~

相同 idempotency key：

- payload digest 相同：返回原 Run；
- payload digest 不同：返回幂等冲突；
- 不创建第二个 Run。

### 11.3 执行

~~~text
Worker
  → atomic claim Activity
  → commit claim
  → execute model/tool outside transaction
  → commit Activity result + Run transition + Events
  → create next Activity / terminal
~~~

### 11.4 SSE

~~~text
Client
  → GET /runs/{id}/events?after_seq=N
  → replay committed events
  → tail new committed events
~~~

客户端断开只关闭订阅，不取消 Run。

### 11.5 Cancel

Cancel 是显式 command：

- CAS 写入 CANCEL_REQUESTED；
- 创建 cancel event；
- Worker 在安全边界检查；
- 可取消下游收到取消；
- 不可取消下游返回的迟到结果不得覆盖 terminal；
- Tool effect 已 UNKNOWN 时不能伪装成 CANCELLED 且无副作用。

### 11.6 Signal/HITL

WAITING_INPUT 时：

- checkpoint 保存 pending_input schema；
- Run 释放 worker；
- signal 使用 signal_id 幂等写入；
- signal 与等待 Activity 关联；
- Worker 重新领取后继续；
- signal 到达 terminal Run 时被明确拒绝或记录为迟到信号。

## 12. 三代引擎的恢复策略

### 12.1 公共承诺

三个引擎共同具备：

- Run ID；
- request idempotency；
- RuntimeEnvelope；
- Canonical Events；
- 唯一 terminal；
- explicit cancel；
- SSE replay；
- release fingerprint；
- ToolExecution/Artifact；
- 状态查询。

### 12.2 恢复粒度

| 引擎 | 第一阶段恢复粒度 | 原因 |
|---|---|---|
| native_loop | turn/model/tool boundary | while、消息和工具调度由项目控制 |
| plan_execute | plan 完成边界 + execution attempt | 执行相仍是整体 ADK Runner |
| agent_loop | invocation/attempt | while 在 ADK BaseLlmFlow 内 |

### 12.3 native_loop

R3 首选 native_loop，因为可以持久化：

- 模型请求前 checkpoint；
- 完整模型 ToolCall 批次；
- 每个 ToolExecution；
- 工具结果 ArtifactRef；
- 下一轮消息边界；
- compact boundary；
- stop transition。

### 12.4 plan_execute

第一阶段只承诺：

- Decision plan 已持久提交后不重新规划；
- execution attempt 有明确编号；
- Tool 副作用通过 ToolExecution 防重复；
- 不声称 plan 中每个自然语言步骤都是 Runtime Step。

### 12.5 agent_loop

ADK 重启后模型可能生成新的 function_call_id。若没有持久化并重放模型输出，不能保证恢复到同一个逻辑 ToolCall。

第一阶段策略：

- invocation 作为粗粒度 Activity；
- 已提交 Tool effect 通过 Tool Broker 拦截；
- 非幂等 Tool 必须具备平台稳定 activity identity；
- 无法获得稳定身份的高风险 Tool 不允许自动恢复重试；
- 不用参数 hash 冒充 ToolCall identity。

## 13. R0：可靠性规格先行

R0 是后续所有实现的约束，不是可选文档工作。

### 13.1 目标

- 冻结事实所有权；
- 冻结状态机；
- 冻结事务与提交边界；
- 冻结失败、取消、重试和恢复语义；
- 冻结三代引擎恢复等级；
- 定义可靠性验收。

### 13.2 交付物

1. State Ownership Registry；
2. RuntimeEnvelope v1；
3. Canonical Event v1；
4. Run/Activity/ToolEffect/Delivery 状态机；
5. ToolExecution/ArtifactRef/Evidence 契约；
6. transaction boundary ADR；
7. streaming commit boundary ADR；
8. SQLite authority ADR；
9. engine recovery level ADR；
10. release/schema compatibility ADR；
11. failure matrix；
12. reliability test specification。

### 13.3 必须回答的问题

- 哪些状态是权威、投影、缓存、索引、Artifact 或诊断？
- accepted 在哪个提交点成立？
- terminal 由谁提交？
- clean EOF 表示什么？
- 客户端断开表示什么？
- Tool ACK 丢失怎么办？
- cancel 与 tool complete 同时发生怎么办？
- worker 崩溃后由谁恢复？
- checkpoint 与历史如何区分？
- plan item 与 Runtime Activity 如何区分？
- output delta 与 final message 如何区分？
- SQLite schema 或 release 不兼容时怎么办？

### 13.4 R0 退出条件

- 所有权表无重叠裁判；
- 所有状态迁移可枚举；
- 所有 terminal 语义明确；
- 所有失败点都有恢复或终止策略；
- 所有 R1–R4 能力都有可自动验证的不变量；
- 不再以“生产可换某实现”代替当前真实设计。

## 14. R1：Canonical Runtime 外壳

### 14.1 目标

先在三个引擎外建立统一的可靠执行外壳，使 HTTP、Run、执行、事件和投递分离。

### 14.2 建设内容

- SQLite schema 与 repository/UoW；
- RunCoordinator；
- Runtime Worker；
- RuntimeEnvelope；
- Run/Activity 状态机；
- Request admission/idempotency；
- Canonical Event append；
- seq 分配；
- unique terminal；
- checkpoint revision/CAS；
- status/events/cancel API；
- SSE replay；
- release fingerprint；
- 三引擎适配接口；
- 最小 WorkingState。

### 14.3 推荐模块边界

~~~text
agent/runtime/
├── domain/
│   ├── models.py
│   ├── states.py
│   ├── events.py
│   ├── commands.py
│   └── invariants.py
├── application/
│   ├── coordinator.py
│   ├── admission.py
│   ├── event_service.py
│   └── recovery.py
├── ports/
│   ├── runtime_store.py
│   ├── artifact_store.py
│   ├── engine_adapter.py
│   └── clock.py
├── adapters/
│   ├── sqlite/
│   ├── filesystem_artifact/
│   └── engines/
├── worker/
│   ├── dispatcher.py
│   ├── timer_scanner.py
│   └── supervisor.py
└── api/
    ├── runs.py
    └── stream.py
~~~

该目录只是推荐边界，实际实现前可再按现有项目风格调整。

### 14.4 R1 最小纵切

第一条最小纵切不调用 Tool：

~~~text
POST /runs
  → durable accepted
  → Worker claim
  → one engine/model attempt
  → committed output events
  → final assistant message + terminal
  → SSE after_seq replay
~~~

### 14.5 R1 退出条件

- 同一 idempotency key 重放 10 次只创建一个 Run；
- 相同 key 不同 payload 明确冲突；
- 每个 Run 只有一个 terminal；
- final message 与 success terminal 同事务；
- SSE 断开不取消执行；
- after_seq 无重复、无缺失；
- process kill 后非终态 Run 可被重新扫描；
- trace 关闭不影响恢复；
- 三代引擎都能接入公共外壳，即使恢复粒度不同。

## 15. R2：Tool、Artifact 与 Evidence 可靠性

R2 建议内部拆成 R2A 和 R2B，但作为一个阶段完成。

### 15.1 R2A：Tool Broker 与 ToolExecution

建设：

- 统一 Tool Broker；
- Tool manifest 增加 effect class；
- runtime-generated tool_execution_id；
- 幂等键；
- attempt；
- ToolEffect 状态机；
- timeout/deadline；
- result envelope；
- reconcile hook；
- compensation 元数据；
- non-idempotent 工具策略；
- Tool result Artifact 化。

effect class 建议：

~~~text
READ_ONLY
IDEMPOTENT_EFFECT
NON_IDEMPOTENT_EFFECT
UNKNOWN_EFFECT
~~~

策略：

- READ_ONLY：可安全重试，但仍受 deadline/budget 控制；
- IDEMPOTENT_EFFECT：必须透传稳定幂等键；
- NON_IDEMPOTENT_EFFECT：无确认/补偿/查询能力时禁止自动重试；
- UNKNOWN_EFFECT：默认按高风险处理。

### 15.2 Tool 结果统一语义

ToolResultEnvelope 至少区分：

~~~text
SUCCESS
FAILURE
INTERRUPT
NO_OUTPUT
UNKNOWN
~~~

失败应 sticky，不能被同一次聚合中的后到 success 静默覆盖。

UI-only frame、HITL interrupt 和真正 empty result 不能都映射为“空”。

### 15.3 Artifact

建设：

- content-addressed write；
- digest 校验；
- immutable metadata；
- preview；
- range-read；
- sensitivity/retention；
- orphan cleanup；
- tool_execution/event/source 关联；
- 大结果只在 Event/Checkpoint 保存 ref。

提交顺序：

~~~text
write immutable blob by digest
  → SQLite transaction:
       commit artifact metadata
       commit tool result
       append TOOL_RESULT event
       advance Activity
~~~

若 blob 已写而事务失败，留下的是可清理 orphan，不得发布结果引用。

### 15.4 R2B：Document 与 Evidence

建设：

- 原始 Document Store；
- document version/content hash；
- replace_document；
- staging version；
- active pointer；
- chunk version；
- index job；
- vector/BM25 作为投影；
- delete/rebuild/consistency check；
- RetrievalContext；
- RetrievalStatus；
- Evidence DTO；
- EvidenceSet Artifact；
- citation deterministic projection。

### 15.5 文档替换语义

~~~text
create document_version
  → parse/chunk/embed
  → build candidate indexes
  → validate counts/digests
  → atomically switch active_document_version
  → old version no longer visible
  → async cleanup old projections
~~~

相同 content hash 重复入库必须幂等。

### 15.6 R2 退出条件

- Tool 成功但 ACK 丢失时不会被普通 retry 重复执行；
- 同一 ToolExecution 的 COMMITTED 结果可复用；
- UNKNOWN 有 reconcile/manual 路径；
- 非幂等 Tool 不会被平台透明自动重试；
- 大型结果不进入 Session/Event/Checkpoint 全量副本；
- Artifact digest 与文件内容一致；
- 文档缩短后旧 chunk 不再可检索；
- vector/BM25 投影删除后可重建；
- HIT/MISS/DEGRADED/DENIED/ERROR 可判定；
- citation 可追溯到稳定 Evidence 和 document version。

## 16. R3：长任务纵向闭环

### 16.1 目标

用一条真实、可观察、包含等待和副作用的链路证明 R1/R2 不只是数据模型。

### 16.2 首选引擎

第一条纵切使用 native_loop。

原因：

- while 在项目代码中；
- ToolCall 解析和调度可控；
- 可在 model/tool boundary checkpoint；
- 可精确恢复消息和工具结果；
- 不依赖 ADK 内部 replay；
- 更适合解释 durable Agent Kernel 与 Runtime Orchestrator 的分工。

### 16.3 建议场景

~~~text
StartRun
  → slow/retryable read-only tool
  → persist WorkingState
  → WAITING_INPUT approval
  → user signal
  → idempotent local side effect
  → Artifact/Evidence output
  → final answer
  → success terminal
~~~

本地副作用应可观察，例如在专用表中创建一个有唯一业务键的“任务记录”，使测试能够证明没有重复提交。

### 16.4 长任务机制

- durable Activity queue；
- atomic claim；
- lease_until；
- fencing_token/revision；
- retry policy；
- persistent timer；
- signal；
- WAITING_INPUT；
- absolute deadline_at；
- cancel propagation；
- late result suppression；
- startup recovery scan；
- release/schema check；
- explicit incompatible release terminal。

### 16.5 SQLite Worker 约束

第一阶段建议：

- 一个 API 进程；
- 一个独立 Runtime Worker 进程；
- Worker 内有限并发；
- 使用短事务领取 Activity；
- 使用 UPDATE ... RETURNING 原子领取；
- 不依赖 PostgreSQL SKIP LOCKED；
- API 的 cancel/signal 写入设置 busy_timeout；
- 不在数据库锁内执行外部 I/O。

### 16.6 R3 故障点

必须注入：

1. Run 受理后、Worker 领取前 kill；
2. LLM 调用前 kill；
3. LLM 返回后、事件提交前 kill；
4. Tool 发出前 kill；
5. Tool 执行中 kill；
6. Tool 已成功但结果提交 ACK 丢失；
7. Artifact 写入后、SQLite 提交前 kill；
8. WAITING_INPUT 期间重启；
9. signal 提交后、继续执行前 kill；
10. final message 提交前 kill；
11. terminal 提交后、SSE 发布前 kill；
12. cancel 与 Tool complete 同时发生。

### 16.7 R3 退出条件

- HTTP 断连后 Run 继续；
- 重启后从已提交边界恢复；
- 等待 24 小时后可继续，测试使用 fake clock；
- 同一 signal 重复提交不重复推进；
- deadline 到达后确定收口；
- cancel 与完成竞态只有一个确定结果；
- 已提交副作用不重复；
- UNKNOWN 不被伪装成普通失败；
- SSE 可从 terminal 前任意 seq 重放；
- release/schema 不兼容明确拒绝。

## 17. R4：删除旧事实源并完成可靠性门禁

### 17.1 目标

完成所有权切换，删除或降级旧状态，确保系统中不再存在第二个裁判。

### 17.2 删除或降级项

1. ADK InMemorySessionService 不再作为对话历史权威；
2. native HistoryStore 不再作为会话历史权威；
3. request-local tool_state 不再作为 Runtime checkpoint；
4. update_task_plan 不再裁决 Runtime Activity；
5. StreamEvent.done/error 不再直接裁决 Run；
6. EOF/HTTP close 不再推导成功；
7. InMemoryArtifactService 不再保存权威 Artifact；
8. trace 不参与恢复；
9. vector/BM25 不再作为 Document truth；
10. 旧 chat stream 不再直接驱动引擎。

保留方式：

- ADK Session 可以作为某次 invocation 的临时投影；
- native messages 可以由 Canonical Event + Checkpoint 编译生成；
- plan_step 可以继续作为 UI 事件；
- trace 继续用于诊断；
- SSE 继续作为 Delivery adapter；
- vector/BM25 继续作为检索索引。

### 17.3 无兼容迁移策略

本项目不做：

- 老表双写；
- 新旧 Run shadow；
- fallback 读取旧 Session；
- 旧 SSE 契约长期兼容；
- 本地历史数据迁移。

可以直接：

- 停止服务；
- 清理旧 local_storage；
- 初始化新 schema；
- 切换新 API；
- 删除旧代码；
- 更新 README/RUNBOOK/AGENTS/CLAUDE/eval 文档中的能力边界。

### 17.4 R4 退出条件

- 每类事实只有一个 owner；
- 删除任意投影后可以重建；
- 关闭 trace 后系统仍可恢复；
- 清空 ADK/native history 后仍可从 Canonical 数据构造下一轮输入；
- 所有旧写路径均不可达；
- 全部可靠性门禁通过；
- 文档不再把内存实现描述成可替换的生产能力；
- 能力声明明确为单机可靠执行。

## 18. 可靠性门禁

可靠性门禁不属于行为评测，不依赖真实 LLM。

### 18.1 测试基础

建议提供：

- fake clock；
- fake LLM；
- scripted engine；
- fake read-only tool；
- fake idempotent side-effect tool；
- fake non-idempotent tool；
- fault injector；
- deterministic worker；
- temporary SQLite database；
- temporary Artifact Store。

### 18.2 必测不变量

1. 相同 idempotency key 重放 10 次只有一个 Run；
2. 相同 key 不同 payload 返回冲突；
3. 一个 Run 最多一个 terminal；
4. terminal 后不能回退 RUNNING；
5. UNIQUE(run_id, seq)；
6. final message 与 success terminal 原子提交；
7. 已发布 SSE event 必须已持久化；
8. after_seq 不丢、不重；
9. 客户端断开不取消；
10. 同一 ToolExecution 不重复提交副作用；
11. ACK 丢失进入 UNKNOWN/reconcile；
12. cancel 与 Tool complete 竞态结果确定；
13. checkpoint CAS 冲突不会静默覆盖；
14. 过期 fencing_token 不能提交；
15. 重启 WAITING_INPUT Run 可继续；
16. timer 到期只唤醒一次；
17. signal 重放只消费一次；
18. Artifact digest 不匹配拒绝读取；
19. 投影可完整重建；
20. 文档替换后旧 chunks 不可见；
21. release/schema 不兼容明确失败；
22. trace 缺失不影响恢复。

### 18.3 当前 eval 不能替代可靠性门禁

现有 eval/harness/runner.py 的 PASS 主要根据 routing 和 rule scorer 计算，没有把 finished、had_error、transport_error 作为全局成功前置条件。

因此：

- 不修改现有行为评测作为本阶段主任务；
- 新建独立 reliability tests；
- 真实 LLM 仅可作为补充冒烟；
- R4 不以回答质量分数作为可靠性退出条件。

## 19. SQLite 特有风险与控制

### 19.1 单写者锁竞争

风险：

- Run/Event/Tool/Signal 同时写入；
- 流式 token 高频写；
- API 与 Worker 竞争锁。

控制：

- 短事务；
- busy_timeout；
- 有限 Worker 并发；
- output delta 聚合；
- 不在事务中做网络 I/O；
- 监控 SQLITE_BUSY。

### 19.2 output delta 写放大

不建议每个 token 单独写一次 SQLite。

建议：

- 按约 50–100ms 或 1–4KB 聚合 delta；
- 聚合后的 delta 先 commit 再 publish；
- terminal 前强制 flush；
- 记录 delta seq 和 assistant message boundary。

代价是极小的流式延迟，换取显著降低写放大。

### 19.3 数据库损坏和磁盘故障

SQLite 可以抵御正常进程崩溃，但不能抵御：

- 本机磁盘损坏；
- 文件被外部误删；
- 主机整体丢失。

第一阶段只声明进程级恢复。备份若需要，应使用 SQLite backup API 或在正确 checkpoint 后执行，不能在 WAL 活跃时只复制主 db 文件并宣称完整备份。

### 19.4 多 Worker

SQLite 不适合作为高并发分布式队列。

第一阶段只需：

- 一个 Worker 进程；
- Worker 内有限并发；
- 使用 revision/fencing 证明旧执行者不能迟到提交；
- 并发领取通过自动化测试验证。

### 19.5 时间语义

deadline 使用绝对 UTC 时间；测试使用 fake clock。

不要每层重新开始一个 timeout。Runtime 计算剩余预算并下传到 Model、Tool、RAG、Skill 和 Sandbox。

## 20. Release 与 Schema

即使不兼容历史数据，也必须记录 release/schema。

每个 Run 固定：

- runtime_schema_version；
- agent_release_id；
- engine；
- prompt digest；
- model policy digest；
- tool catalog digest；
- skill release digest；
- retrieval policy digest；
- Artifact schema；
- Evidence schema。

恢复时：

- 相同 release/schema：继续；
- 有显式升级函数：升级 checkpoint 后继续；
- 无升级能力：进入 INCOMPATIBLE_RELEASE；
- 禁止静默用新代码解释旧 checkpoint。

“不做历史兼容”表示可以重置本地数据，不表示运行中的长任务可以被新版本静默误解释。

## 21. 未来切换 PostgreSQL 或 Temporal

### 21.1 切换 PostgreSQL

未来需要多实例、高并发和生产运维时：

- 保留领域契约；
- 新实现 PostgreSQL adapter；
- 停止 SQLite 写入；
- 本地数据直接重置或一次性导入；
- 不长期维护双 backend；
- 不做双写裁决。

### 21.2 切换 Temporal

Temporal 一旦引入，必须明确接管：

- Run；
- Activity；
- retry；
- timer；
- signal；
- cancel；
- workflow history。

SQLite 中对应表必须：

- 删除；或
- 降级为明确的只读 projection。

ToolExecution、Canonical Event、Artifact、Evidence 可以继续由应用存储拥有，但不得让 Temporal history 和 SQLite RunStore同时裁决执行进度。

推荐切换方式：

~~~text
完成现有 SQLite Run
  → 停止创建新 SQLite Run
  → 清空本地开发数据
  → Temporal 成为新 Run authority
  → SQLite/PostgreSQL 仅保存应用事实和投影
~~~

不推荐：

~~~text
SQLite RunStore + Temporal History 双写
~~~

## 22. 明确禁止的实现方式

1. 只把 ADK Session 换成 SQLite 就宣称 durable execution；
2. 让 Run、Checkpoint、Session 和 History 都保存 completed_steps；
3. 用 SSE done、clean EOF 或 generator 正常退出推导 SUCCEEDED；
4. 在事件持久化前先发送给客户端；
5. 用 tool args hash 作为 ToolCall 唯一身份；
6. ACK 丢失后自动重试 non-idempotent Tool；
7. 把完整大型结果复制到 Event、Checkpoint、Session 和 trace；
8. 把 vector/BM25 命中当 Document truth；
9. 把 trace JSONL 当恢复日志；
10. 把 FAILED/CANCELLED/UNKNOWN Run 形成成功记忆；
11. 为未来可能切换数据库提前建设双 backend；
12. 在 SQLite 事务中调用网络或等待人工；
13. 让多个 Worker 长时间持有写锁；
14. 为旧接口保留兼容写路径；
15. 把可靠性测试全部推迟到行为评测阶段。

## 23. 推荐实施顺序

建议后续按以下小步推进：

### Step 1：完成 R0

- 先只写规格、ADR、状态机和测试清单；
- 不修改现有引擎；
- 不新增 Memory。

### Step 2：R1 数据骨架

- SQLite schema；
- domain model；
- transaction/UoW；
- request admission；
- Run/Event/Activity；
- fake engine。

### Step 3：R1 API 与 SSE

- create/status/events/cancel；
- Worker；
- after_seq；
- final commit；
- crash recovery。

### Step 4：接入三个 Engine Adapter

- 先 native；
- 再 plan_execute；
- 最后 agent_loop；
- 只承诺对应恢复等级。

### Step 5：R2A Tool/Artifact

- Tool Broker；
- ToolExecution；
- effect class；
- Artifact CAS；
- unknown/reconcile。

### Step 6：R2B Evidence

- Document/version；
- index job；
- Evidence DTO；
- citation projection；
- atomic replace。

### Step 7：R3 长任务

- timer；
- WAITING_INPUT；
- signal；
- cancel；
- deadline；
- side effect；
- Artifact；
- 故障注入。

### Step 8：R4 清理

- 删除旧事实源；
- 删除旧写路径；
- 重置本地存储；
- 更新全部文档；
- 运行可靠性门禁。

## 24. Definition of Done

R0–R4 全部完成时，项目应能诚实演示：

1. 创建一个持久 Run，HTTP 立即返回 run_id；
2. 客户端断开，Run 继续；
3. 服务重启，Run 恢复；
4. SSE 使用 after_seq 继续；
5. 慢 Tool 执行结果可复用；
6. Tool ACK 丢失不会盲目重复副作用；
7. Run 可等待人工 signal；
8. deadline/cancel 有确定结果；
9. 大型结果通过 ArtifactRef 管理；
10. Evidence 可追溯到稳定 Document version；
11. final message 与 terminal 原子提交；
12. 一个事实只有一个裁判；
13. 删除投影后可以重建；
14. 三代引擎共用 Runtime 外壳，但恢复能力边界诚实；
15. 全部可靠性测试使用 fake 依赖稳定通过；
16. 项目明确声明这是单机持久可靠实现，不是分布式 HA。

## 25. 最终决策记录

| 编号 | 决策 | 结果 |
|---|---|---|
| D-01 | 先可靠性，后 Memory，再行为评测 | 接受 |
| D-02 | SSOT 属于可靠性第一约束 | 接受 |
| D-03 | R0 必须先于代码实现 | 接受 |
| D-04 | R1 建设统一 Canonical Runtime 外壳 | 接受 |
| D-05 | R2 在长任务前完成 Tool/Artifact/Evidence | 接受 |
| D-06 | R3 首条长任务使用 native_loop | 接受 |
| D-07 | R4 删除旧事实源，不保留兼容写路径 | 接受 |
| D-08 | 第一阶段使用本地 SQLite | 接受 |
| D-09 | 第一阶段不引入 Temporal | 接受 |
| D-10 | SQLite 只承诺单机可靠执行 | 接受 |
| D-11 | WorkingState 属于可靠执行，不属于后置 Memory | 接受 |
| D-12 | 行为评测后置，但可靠性自动化门禁同步建设 | 接受 |
| D-13 | 三代引擎恢复粒度不强求一致 | 接受 |
| D-14 | 未来切换 Temporal 必须替换 Run authority | 接受 |

## 26. 参考资料

### 26.1 生产项目评审

- /Users/shixiangweii/PycharmProjects/fy26_deap_agent/albert-agent-2/sxw_aicoding/架构评审/
- /Users/shixiangweii/PycharmProjects/fy26_deap_agent/albert-agent-2/sxw_aicoding/架构评审/08_渐进优化路线图.md
- /Users/shixiangweii/PycharmProjects/fy26_deap_agent/albert-agent-2/sxw_aicoding/架构评审/09_从零设计蓝图.md
- /Users/shixiangweii/PycharmProjects/fy26_deap_agent/albert-agent-2/sxw_aicoding/代码分析/DEAP持久执行与记忆系统缺口专题评审.md
- /Users/shixiangweii/PycharmProjects/fy26_deap_agent/albert-agent-2/sxw_aicoding/代码分析/DEAP单一事实来源问题专题分析.md

### 26.2 当前项目既有评估

- sxw_aicoding/讨论评估/会话管理_WebSocket_SSE生产级能力引入评估.md
- sxw_aicoding/讨论评估/RAG模块_lippi-arag参考覆盖与生产级能力引入评估.md
- sxw_aicoding/项目背景说明.txt
- AGENTS.md

### 26.3 当前项目关键代码

- agent/api/chat.py
- agent/engine/base.py
- agent/engine/agent_loop/
- agent/engine/plan_execute/
- agent/engine/native_loop/
- agent/session/session_service.py
- agent/artifacts/artifact_service.py
- agent/stream/event_converters.py
- agent/citation/citation_injector.py
- agent/tools/knowledge_search.py
- arag/api/index.py
- arag/store/
- arag/schemas.py
- common/trace.py
- eval/harness/

---

## 27. 最终结论

当前项目下一阶段不应继续扩展 Memory SDK、图记忆、复杂评测或更多智能体能力，而应先把“一个 Run 是否真实存在、执行到哪里、Tool 是否产生副作用、用户看到了什么、重启后从哪里继续”变成可持久、可裁决、可恢复、可自动验证的确定答案。

第一阶段选择 SQLite 并不降低架构标准。它只是把能力范围收敛为单机可靠执行，使项目可以在最小基础设施下完整展示：

- Canonical Runtime；
- 单一事实来源；
- durable accepted；
- 显式 terminal；
- Tool effect；
- Artifact/Evidence；
- checkpoint；
- signal/timer/cancel；
- SSE replay；
- 故障恢复。

完成 R0–R4 后，再建设 Memory，记忆才有可信的 source event、terminal、Artifact 和 release；再深化评测，评测结果才建立在确定、可复现、不会因断流或重复副作用而失真的 Runtime 之上。
