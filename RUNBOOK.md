# Runbook：Canonical Agent Runtime

本文覆盖本机安装、四服务五进程启动、新 Run/Artifact/SSE 协议、durable ARAG 入库、可靠性验证和故障排查。

## 1. 运行边界

- 支持本机磁盘上的进程崩溃恢复、幂等 admission、Activity lease/fencing、committed Event 重放、checkpoint、Tool effect、Artifact 和版本化 RAG。
- 不支持多机 HA、共享盘、主机/磁盘容灾、服务端 Delivery ACK、历史数据迁移、PostgreSQL/Temporal backend。
- `plan_execute` 与 `agent_loop` 以整个 ADK attempt/invocation 为恢复粒度，不承诺 mid-turn deterministic replay。
- 真实模型、embedding、A2A 和真实 LLM smoke 需要有效 API Key；`tests/reliability` 不需要。

## 2. 前置条件

- macOS/Linux，Python 3.12、Bash 3.2+、`curl`；
- 可访问配置的大模型 OpenAI-compatible endpoint；
- 端口 8000、8100、8200、8300 可用；
- SQLite 文件和 Artifact 必须位于本机磁盘，不要放在 NFS、共享盘或云同步目录。

所有 Python 命令必须使用 `.venv/bin/python`：

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pip check
```

## 3. 配置

```bash
cp .env.example .env
```

编辑 `.env`，至少填写 `DASHSCOPE_API_KEY`。也可以不创建 `.env`，仅在 shell 中 export 真实 Key，其余项使用代码默认值。`.env` 已被 Git 忽略；不要把真实 Key 写进源码、文档、测试报告或提交历史。真实环境变量优先于 `.env`，`run_all.sh` 的端口/数据库探针与各服务使用同一优先级；所有配置在对应进程启动时读取，修改后必须重启。

### 3.1 LLM 与服务

| 变量 | 默认值 | 说明 |
|---|---|---|
| `DASHSCOPE_API_KEY` | `sk-***` | 运行时必须替换；也可仅通过 shell export |
| `LLM_BASE_URL` | DashScope compatible-mode | OpenAI-compatible endpoint |
| `LLM_MODEL` | `qwen3.7-plus` | Agent、ARAG rewrite/caption、A2A 使用 |
| `EMBEDDING_MODEL` | `text-embedding-v3` | ARAG embedding |
| `AGENT_PORT` | `8000` | Runtime API / Web UI |
| `ARAG_PORT` | `8100` | ARAG |
| `SKILL_CENTER_PORT` | `8200` | skill-center |
| `A2A_SERVICE_PORT` | `8300` | A2A service |
| `ARAG_BASE_URL` | `http://127.0.0.1:8100` | Worker 与文档代理调用地址 |
| `SKILL_CENTER_BASE_URL` | `http://127.0.0.1:8200` | Worker 启动时加载 Skill/A2A 目录 |
| `A2A_SERVICE_BASE_URL` | `http://127.0.0.1:8300` | skill-center 注册表目标 |

### 3.2 Runtime 与 Worker

| 变量 | 默认值 | 说明 |
|---|---|---|
| `RUNTIME_DB_PATH` | `local_storage/runtime/runtime.db` | Runtime 唯一持久化事实源 |
| `ARTIFACT_ROOT` | `local_storage/artifacts` | SHA-256 CAS 根目录 |
| `DEMO_EFFECTS_DB_PATH` | `local_storage/demo_effects/effects.db` | 模拟外部副作用系统，故意独立于 Runtime |
| `RUNTIME_WORKER_ID` | `runtime-worker-local` | heartbeat/lease owner 标识 |
| `RUNTIME_WORKER_CONCURRENCY` | `4` | 单 Worker 进程内并发 Activity 上限 |
| `RUNTIME_WORKER_POLL_MS` | `250` | claim/maintenance 扫描间隔 |
| `RUNTIME_LEASE_SECONDS` | `30` | Activity lease |
| `RUNTIME_LEASE_RENEW_SECONDS` | `10` | 续租间隔 |
| `RUNTIME_SHUTDOWN_GRACE_SECONDS` | `20` | 停止领取后等待 in-flight 的上限 |
| `RUNTIME_EVENT_FLUSH_MS` | `100` | output delta 时间聚合阈值 |
| `RUNTIME_EVENT_FLUSH_BYTES` | `2048` | output delta 字节聚合阈值 |
| `RUNTIME_SSE_POLL_MS` | `250` | API 短查询 committed events 间隔 |
| `RUNTIME_SSE_HEARTBEAT_SECONDS` | `15` | 无 seq SSE heartbeat comment |
| `RUNTIME_BUSY_TIMEOUT_MS` | `5000` | SQLite busy timeout |
| `RUNTIME_DEFAULT_DEADLINE_SECONDS` | `600` | CreateRun 未提供 deadline 时的绝对期限 |
| `RUNTIME_ARTIFACT_CLEANUP_INTERVAL_SECONDS` | `3600` | Worker 扫描 Artifact orphan 的间隔 |
| `RUNTIME_ARTIFACT_ORPHAN_AGE_HOURS` | `24` | 无 metadata 引用 blob 的最短保留时间 |

每个 Run 在 JSON 中必填 `engine`，不通过启动配置选择。Worker 启动时同时注册三种 engine 的 immutable release；API 没找到目标 active release 时拒绝 admission。

### 3.3 引擎、Skill 与沙箱

| 变量 | 默认值 | 说明 |
|---|---|---|
| `MAX_LOOP_ITERS` | `8` | loop 软收尾轮次；硬上限为该值 + 2 |
| `SUB_AGENT_ENGINE` | `auto` | `auto` 跟随当前 Run；或 `adk` / `native`；远端 A2A 不受影响 |
| `NATIVE_STREAMING_TOOL_EXEC` | `true` | native 流式 Tool 提前执行安全阀 |
| `NATIVE_MAX_TOOL_CONCURRENCY` | `10` | native 只读并发批次上限 |
| `NATIVE_TOOL_RESULT_MAX_CHARS` | `8000` | 进模型的单条 ToolResult 上限 |
| `CONTEXT_WINDOW_TOKENS` | `128000` | native 上下文窗口估计 |
| `COMPACT_BUFFER_TOKENS` | `13000` | compact buffer |
| `COMPACT_PRESERVE_UNITS` | `6` | compact 后保留的尾部原子单元 |
| `SANDBOX_PROVIDER` | `local` | `local` 可运行；`agentbay` 是不可运行桩 |
| `SKILL_CALL_TIMEOUT_SECONDS` | `120` | Claude SKILL 排队、执行和整理总时限 |
| `SKILL_MAX_LLM_CALLS` | `16` | 单个 Skill Agent 模型调用上限 |
| `SKILL_MAX_PARALLEL_CALLS` | `2` | 进程级 Skill 并发上限 |
| `SKILL_RESULT_MAX_CHARS` | `8000` | 回灌主 Agent 的结果限制 |

### 3.4 ARAG

| 变量 | 默认值 | 说明 |
|---|---|---|
| `RAG_DB_PATH` | `local_storage/arag/rag.db` | Document/version/chunk/index-job 权威及可重算 embedding rows |
| `RAG_STORAGE_DIR` | `local_storage/arag` | 原始文档内容寻址根目录 |
| `INDEX_JOB_POLL_INTERVAL_SECONDS` | `0.25` | 进程内 index worker 扫描间隔 |
| `VECTOR_BACKEND` | `local` | 当前只实现 SQLite truth → numpy projection |
| `FULLTEXT_BACKEND` | `local` | 当前只实现 active chunks → BM25 projection |
| `GRAPH_BACKEND` | `local` | GraphStore 端口占位，未接检索流 |

### 3.5 Trace

| 变量 | 默认值 | 说明 |
|---|---|---|
| `TRACE_ENABLED` | `true` | 诊断轨迹开关；关闭不影响恢复 |
| `TRACE_PAYLOAD_LEVEL` | `full` | `none / summary / full`；full 是本 demo 调试取向 |
| `TRACE_DIR` | `local_storage/traces` | 本机诊断目录 |
| `TRACE_MAX_FIELD_CHARS` | `20000` | 单字段限制 |
| `TRACE_RETENTION_DAYS` | `7` | `0` 表示不自动清理 |

`full` 可能包含用户原始提问和模型完整输入；这些文件不得随评测报告分发。

两个只读出口：

- `GET /api/v1/traces?day=&engine=&status=&q=&limit=` —— 轨迹摘要列表（新→旧），供控制台浏览；
- `GET /api/v1/traces/{trace_id}?level=none|summary|full` —— 单条轨迹（`level` 只能在落盘级别之上再降级）。

关闭 tracing 时两者都返回 503 而不是空结果——空列表会被误读成"没有轨迹"。

**Trace Console**（<http://127.0.0.1:8000/trace-ui/>）：左侧按日期/引擎/状态/子串浏览，中间是瀑布图与 Span 树，右侧展开单个 Span 的属性、payload 与事件时间线。默认按 `summary` 取 payload，避免把原始提问与完整模型输入整页铺开；需要原文再切 `full`。可开自动刷新观察进行中的轨迹（根 span 收口前显示为「进行中」）。

关联键的来源固定为 CreateRun 那一刻：请求带 `x-trace-id` 就用它，没带则由 `TraceMiddleware` 生成，并在响应头 `x-trace-id` 里回显。该值随 Run 持久化（`runs.trace_id`），Worker 执行时再绑定回来——API 与 Worker 是两个进程，不落库就接不上（见 ADR-0007）。所以：

- 一个 Run 一条 trace，文件名以 trace_id 结尾；Worker 重启后重试会写出第二个文件，读取时按 trace_id 合并成一条（`trace_files` 列出全部）；
- 查询的是 Run 的执行轨迹，用 CreateRun 的 trace_id，不是后续 SSE 订阅请求的；
- API 进程查不到内存命中时读盘，因此 Worker 重启后仍可取回（受 `TRACE_RETENTION_DAYS` 约束）。

轨迹的根是 `runtime.engine_attempt`（每次 attempt 一个）。TTFT、`event_counts`、`had_error`、`finish_reason` 由 `CommittedEventSink` 的事件旁路写在它上面——那是三代引擎唯一共同的事件出口，因此信号天然对等。

## 4. 一键启动

```bash
bash scripts/run_all.sh
```

启动顺序：

```text
a2a_service → skill-center → arag
→ Runtime Worker（加载 LLM/工具并注册 3 个 release）
→ Runtime API
→ sample index jobs 受理并轮询到 ACTIVATED（FAILED/超时则整体退出）
```

启动脚本会在拉起任何进程前拒绝空/占位 Key。Worker ready 必须同时满足：进程仍存活、本次启动后的 heartbeat 为 `ACTIVE`、三种 release map 与 active pointers 完全一致；旧数据库中的 stale active pointers 不能误判 ready。启动完成后脚本持续监督五个进程，任一进程异常退出都会统一停机。Ctrl-C 会统一通知五个进程停止。Web UI：<http://127.0.0.1:8000/chat-ui/>；Trace Console：<http://127.0.0.1:8000/trace-ui/>。

## 5. 手动启动

在五个终端执行：

```bash
PY=.venv/bin/python

$PY -m uvicorn a2a_service.main:app --port 8300
$PY -m uvicorn skillcenter.main:app --port 8200
$PY -m uvicorn arag.main:app        --port 8100
$PY -m agent.runtime.worker.main
$PY -m uvicorn agent.main:app       --port 8000
```

Worker 应先于 API admission 可用；若 API 先起，`GET /healthz` 的 `active_releases` 为空，CreateRun 会返回 `503 NO_ACTIVE_RELEASE`，直到 Worker 完成注册。

### 健康检查

```bash
curl -sS http://127.0.0.1:8000/healthz
curl -sS http://127.0.0.1:8100/healthz
curl -sS http://127.0.0.1:8200/healthz
curl -sS http://127.0.0.1:8300/.well-known/agent-card.json
```

- Runtime API 应返回三个 `active_releases`；
- ARAG 应显示 `document_authority=sqlite` 及 projection generation/chunk 数；
- Worker 没有 HTTP 端口，通过日志和 `runtime_workers` 的新鲜 `ACTIVE` heartbeat/release map 观察，Run 恢复仍以 Activity lease 为准。

## 6. 首次 R4 初始化或重置

不支持把旧本地 Session、旧向量文件或旧对话迁入新 schema。需要干净初始化时，先停全部进程，再把整个运行目录移走留作本机备份：

```bash
[ ! -e local_storage ] || mv local_storage "local_storage.pre-r4.$(date +%Y%m%d-%H%M%S)"
bash scripts/run_all.sh
```

不要只删除 Runtime 主 DB 而保留其 `-wal/-shm`，也不要在进程运行时复制单个 SQLite 主文件并当作完整备份。

## 7. Run 生命周期操作

### 7.1 创建

```bash
curl -i -X POST http://127.0.0.1:8000/api/v1/runs \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: runbook-001' \
  -d '{
    "client_request_id":"22222222-2222-4222-8222-222222222222",
    "conversation_id":null,
    "principal_id":"demo-user",
    "agent_id":"demo-agent",
    "engine":"agent_loop",
    "input":{"text":"用工具计算 (3+4)*5","attachment_refs":[]}
  }'
```

可选 `deadline_at` 使用 RFC3339 UTC，例如 `2026-08-09T10:00:00Z`。缺省时使用 `RUNTIME_DEFAULT_DEADLINE_SECONDS`。`Idempotency-Key` 必填；附件顺序参与请求摘要。

典型错误：

| HTTP | code | 含义 |
|---:|---|---|
| 400 | `IDEMPOTENCY_KEY_REQUIRED` | 缺 header |
| 400 | `DEADLINE_IN_PAST` | deadline 已过 |
| 409 | `IDEMPOTENCY_KEY_REUSE` | 同 scope/key 的请求摘要不同 |
| 409 | `CONVERSATION_BUSY` | 同 conversation 已有非终态 Run |
| 503 | `NO_ACTIVE_RELEASE` | Worker 尚未注册请求 engine 的 release |

同 key、同规范化请求返回原 Run 且 `reused=true`。如果未传 conversation，服务端创建；下一轮把上一响应的 `conversation_id` 带回。

### 7.2 状态

```bash
curl -sS http://127.0.0.1:8000/api/v1/runs/run_xxx
```

关注：`status`、`revision`、`current_activity_id`、`pending_input`、`terminal`、`last_seq`、`release_fingerprint` 和绝对 `deadline_at`。

终态：`SUCCEEDED / FAILED / CANCELLED / TIMED_OUT / REJECTED / INCOMPATIBLE_RELEASE`。

### 7.3 committed SSE replay/tail

```bash
curl -N 'http://127.0.0.1:8000/api/v1/runs/run_xxx/events?after_seq=0'
curl -N -H 'Last-Event-ID: 17' \
  'http://127.0.0.1:8000/api/v1/runs/run_xxx/events'
```

`after_seq` 显式提供时优先于 `Last-Event-ID`。客户端只把 seq 当不透明单调 cursor；可见性过滤允许跳号。断开订阅后 Run 继续，重连读取 `seq > cursor`。heartbeat comment 无 seq，不更新 cursor。

Web UI 的 `last_seq` 是传输 cursor，不是已渲染 DOM 的持久化快照：同页断线按 cursor 续订；浏览器刷新后从 `after_seq=0` 重放 committed events 重建页面，再继续 tail。

### 7.4 取消与停止观看

关闭 SSE 只表示停止观看。要取消：

```bash
curl -X POST http://127.0.0.1:8000/api/v1/runs/run_xxx/cancel \
  -H 'Content-Type: application/json' \
  -d '{"command_id":"cancel-runbook-001","reason":"operator cancelled"}'
```

重复 `command_id` 返回当前幂等结果。terminal 先提交时 cancel 返回 `409 RUN_ALREADY_TERMINAL`；cancel 先提交时迟到 success 不能覆盖它。存在不确定 Tool effect 时 Run 会停在 `CANCEL_REQUESTED` 直到 reconcile 或 deadline。

### 7.5 Signal / HITL

```bash
curl -X POST http://127.0.0.1:8000/api/v1/runs/run_xxx/signals \
  -H 'Content-Type: application/json' \
  -d '{
    "signal_id":"approval-runbook-001",
    "wait_activity_id":"act_xxx",
    "type":"APPROVAL",
    "payload":{"approved":true}
  }'
```

相同 signal 和 digest 只消费一次；相同 ID、不同 digest 冲突。terminal 后的迟到 signal 会写 `REJECTED_LATE` 审计行并返回 409。

ToolEffect 进入人工处置时，GET Run 的 `pending_input.type` 为 `TOOL_RECONCILIATION_REQUIRED` 或 `TOOL_RECONCILIATION`，并列出 `unresolved_tool_execution_ids`。此边界只接受：

```bash
curl -X POST http://127.0.0.1:8000/api/v1/runs/run_xxx/signals \
  -H 'Content-Type: application/json' \
  -d '{
    "signal_id":"tool-resolution-001",
    "wait_activity_id":"act_xxx",
    "type":"tool_reconciliation",
    "payload":{
      "tool_execution_id":"tool_xxx",
      "action":"mark_failed",
      "evidence":{"source":"provider-ledger","effect_absent":true},
      "result":{
        "status":"FAILURE",
        "error_code":"EFFECT_NOT_COMMITTED",
        "error_message":"provider ledger confirms no effect"
      }
    }
  }'
```

三种 action：

- `mark_committed`：`result.status` 只能是 `SUCCESS/NO_OUTPUT`；成功必须有 preview、已上传的 `result_ref` 或 `external_object_id`。Artifact ref 会与 canonical ToolResult 同事务建立 provenance Link。
- `mark_failed`：必须提供 `FAILURE + error_code/error_message`；是 sticky 人工结论，即使原 manifest 尚有 attempt 预算也不会再次 dispatch。
- `reconcile`：只请求 Worker 再查询外部状态，payload 禁止 result/ref/external object，且该 ToolExecution 必须记录 `supports_reconcile=true`。授权提交后不会伪造 ToolResult；Worker 校验原 Tool 的冻结 release 后只调用 reconcile hook，绝不会调用原 executor 或 EngineAdapter。

所有 action 都要求非空 evidence。evidence 最大 4KiB，inline result 最大 8KiB；大证据/结果先走 Artifact。Tool ID 不在当前 pending list、ref 不存在、状态已变化或两个不同 signal 竞争时返回 409，事务不会留下 signal/event/link 或错误唤醒。

ToolResult 使用严格 v1 envelope：`FAILURE/UNKNOWN` 必须同时给有界 `error_code/error_message`，`INTERRUPT` 必须给 `pending_input`，`NO_OUTPUT` 不得携带 preview/ref，额外字段会被拒绝。账本 effect/result 必须匹配（例如 `COMMITTED` 不能配 `FAILURE`）。若同时给 `result_ref` 与 Artifact metadata，它们必须是同一个已注册 digest；envelope、账本列、canonical event 和 Artifact Link 任一不一致都会整笔回滚。

`external_object_id` 是稳定 ToolExecution 的单调外部 correlation：ACK 丢失后已知的 provider job/task ID 不会在 retry、reconcile、manual 或 Worker 重启中被清掉，也不能由新 attempt 改成另一个 ID。reconcile hook 会收到既有 ref/external identity；hook 新发现但仍不能判断结论的 identity 也会先持久化，供下一次人工查询使用。

同一 pending list 有多个 execution 时，每个 signal 只解决一个。list 只包含 operator-actionable 的 `MANUAL_REQUIRED` IDs：普通 Run 解决最后一个人工 ID 后，若其余 effect 都满足冻结 replay guard，则恢复执行并由 Broker 复用/受控 replay；已取消 Run 会把 idle remainder（包括原本 replay-safe 的 effect）也转成可审计人工项，保持 `CANCEL_REQUESTED`，到最后一个才提交 `CANCELLED`。因此不要并行处置同一 Run 的多个 ID，应在每次响应后重新 GET Run，并使用最新 `pending_input.unresolved_tool_execution_ids`。普通 approval/signal 不能覆盖 Tool reconciliation boundary。

cancel 已先提交也仍接受上述严格 signal。`mark_committed/mark_failed` 只收口 effect，不会把 Run 复活；`reconcile` 的 Worker claim 同样保持 `CANCEL_REQUESTED`。如果 query hook 缺失、Worker 所载 Tool release 与持久 execution 不一致、hook 异常或无确定结果，effect 回到 `MANUAL_REQUIRED`，可用新的 `signal_id` 再处置。Worker 在 query 前或 query 中退出时也采用同一恢复语义；旧 signal 重放只返回原幂等结果，不会再次唤醒，必须提交新 signal。

绝对 deadline 优先于人工处置和 lease recovery：deadline 后新 signal 返回 `RUN_DEADLINE_EXCEEDED`；dispatch/query 的 Store 标记事务与紧邻 executor/hook 的入口都会重新检查剩余时间，`<= 0` 不启动外部 I/O；执行中的 query 即使取得迟到结果，Run 仍 `TIMED_OUT`。`TIMED_OUT` terminal payload 会保留仍未解决的 ToolExecution IDs。遇到反复人工边界时检查：

```bash
curl -sS http://127.0.0.1:8000/api/v1/runs/run_xxx
sqlite3 local_storage/runtime/runtime.db \
  "select tool_execution_id,tool_name,release_digest,effect_class,effect_status,reconcile_state,result_ref,external_object_id,revision from tool_executions where run_id='run_xxx';"
```

不要直接改 `runtime.db`：exact marker 内含 effect revision，手工改行会被 fencing/revision guard 拒绝，且可能破坏 append-only 审计。

## 8. Artifact 操作

### 上传并绑定到 Run

```bash
curl -sS -X POST http://127.0.0.1:8000/api/v1/artifacts \
  -F 'file=@/absolute/path/to/image.png'
```

把响应的 64 位 `artifact_id` 放进 CreateRun 的 `attachment_refs`。Run 不接收 multipart。

### 完整性校验与 Range

```bash
curl -i -H 'Range: bytes=0-65535' \
  http://127.0.0.1:8000/api/v1/artifacts/ARTIFACT_SHA256
```

每次业务读取都会重新校验完整 blob 的 SHA-256；篡改返回 `409 ARTIFACT_INTEGRITY_ERROR`。只支持单个 `bytes=start-end` Range，单次最大 1MiB。

- 非图片附件只把已校验的 8KiB preview 放入初始模型输入；模型通过 `read_artifact(artifact_id, offset, max_bytes)` 分段读取，默认 32KiB、最大 64KiB。
- 图片从已校验 CAS 完整物化为当前 attempt 的多模态 Part；attempt 结束后不把二进制复制到 Session 或 checkpoint。
- 大 `read_artifact` 结果只保留 Artifact/ToolExecution 权威引用；`native_loop` 恢复时重新物化，不把大切片复制进 checkpoint JSON。
- Worker 默认每小时扫描一次 CAS；只有超过 24 小时且不在 Runtime metadata 引用集合中的 blob/temp 才会回收。Artifact rename 后 metadata 事务失败留下的 orphan 因此不会永久积累。

## 9. ARAG 入库与检索

### 9.1 样本库

```bash
curl -sS -X POST http://127.0.0.1:8100/v1/index/sample
curl -sS http://127.0.0.1:8100/v1/index/jobs/job_xxx
```

提交返回 `202 + job_ids`。只有 `ACTIVATED` 才对 retrieval 可见；`FAILED` 查看 `error`。相同 dataset/doc/content hash 复用已有 version/job。

### 9.2 自定义文档

```bash
curl -sS -X POST http://127.0.0.1:8100/v1/index \
  -H 'Content-Type: application/json' \
  -d '{"documents":[{
    "dataset_id":"default",
    "doc_id":"runbook-doc",
    "title":"Runbook",
    "content":"可靠执行依赖明确的提交边界。",
    "metadata":{"scope":"public"}
  }]}'
```

Web UI 通过同源 `/api/v1/documents/index` 和 job 查询代理完成相同流程。

### 9.3 检索状态

```bash
curl -sS -X POST http://127.0.0.1:8100/v1/retrieve \
  -H 'Content-Type: application/json' \
  -d '{
    "query":"什么是可靠执行？",
    "datasets":["default"],
    "scope":"public",
    "top_k":6
  }'
```

- `HIT`：健康检索且有命中；
- `MISS`：所有启用路线健康且零命中；
- `DEGRADED`：至少一路投影/召回失败，仍可返回另一条路线结果；
- `DENIED`：dataset/scope 校验失败；
- `ERROR`：transport/unhandled failure 在 Agent 侧按 best-effort 继续。

重启 ARAG 会从 `rag.db` active chunks 重建 numpy/BM25 snapshot。投影是只读派生物；Document active pointer 才决定可见版本。

进程还会周期校验 active embedding 的 model、维度和 checksum。缺失/损坏时先保留 BM25 并标记 `DEGRADED`，随后在事务外重新 embedding；结果只有在 active source digest 仍一致时才原子发布，失败按指数退避继续修复。

## 10. native 恢复与长任务可靠性演示

### 10.1 通用 kernel checkpoint

普通 `engine=native_loop` Run 使用 `native-kernel-v1`，在以下边界提交 checkpoint：

```text
MODEL_REQUEST
TOOL_BATCH_COMMITTED（完整 ToolCall batch）
TOOL_RESULT_COMMITTED（每个结果）
NEXT_TURN
COMPLETED
```

Worker 在崩溃后从最后 committed 边界恢复。半个 model stream 未形成完整 checkpoint 时会重放该 model slot；已经提交的 ToolExecution 依靠稳定 slot 与 Tool Broker 复用。这里不承诺 provider token 级、逐字节相同的流重放。

### 10.2 确定性 HITL/副作用纵切

创建 `engine=native_loop` 且文本以 `/reliability-demo` 开头：

```text
slow_lookup（两次 retryable failure）
→ checkpoint
→ WAITING_INPUT
→ approval signal
→ 独立 effects.db 中幂等 create_demo_task
→ 大结果 Artifact
→ final assistant + SUCCEEDED
```

等待阶段不占 Worker slot。可以在 `WAITING_INPUT` 停掉 API/Worker再重启，然后提交 signal；稳定 signal/tool identity 防重复消费和重复副作用。

该确定性路由建立在通用 kernel checkpoint 之上，用于展示 WAITING_INPUT 和外部幂等副作用边界；不代表两个 ADK 引擎已经具备内部 ToolCall 边界的 HITL 恢复。

## 11. 可靠性门禁

```bash
bash scripts/check.sh
```

统一门禁应包括：

```text
py_compile
pytest tests/reliability
SQLite schema identity verification
旧协议/旧权威路径扫描
```

也可直接：

```bash
PY=.venv/bin/python
find agent arag common skillcenter a2a_service -name '*.py' -print0 \
  | xargs -0 "$PY" -m py_compile
$PY -m pytest tests/reliability
```

测试使用临时 SQLite、Artifact 根目录、FakeClock/FakeRandom、ScriptedEngine/fake tools。不要把真实 LLM 结果当可靠性 PASS。

## 12. 行为评测

```bash
export DASHSCOPE_API_KEY=sk-***
bash eval/run_eval.sh
```

Harness 对每个 case 执行 CreateRun → committed SSE subscription → GET Run status，并由 terminal status 判断完成；同一个 :8000 Runtime API 可按请求选择 `agent_loop` 或 `plan_execute`，不再需要多端口 agent 实例。详见 [eval/README.md](eval/README.md)。

ARAG-down pass 需要手动停 :8100 后单独执行。真实 LLM smoke/行为分数只记录，不替代可靠性门禁。

## 13. 故障与恢复操作

| 故障 | 预期行为 | 操作 |
|---|---|---|
| API 进程停止 | Worker 继续执行；暂时不能 admission/status/SSE | 重启 `uvicorn agent.main:app`，按 cursor 重连 |
| Worker 进程停止 | accepted Run 保留；in-flight 等 lease 过期 | 重启 Worker；不要手改 Run 为 FAILED |
| SSE 客户端断开 | Run 不变 | 用最后 seq 重连 |
| ARAG 停止 | 知识检索走 best-effort 降级 | 恢复 :8100；查看 `[QaRetrieve]` |
| skill-center 启动时不可用 | Worker 跳过远程 Skill/A2A 目录 | 恢复下游后重启 Worker加载目录 |
| A2A 调用失败 | 错误作为 ToolResult 返回模型 | 检查 agent-card 与 :8300 |
| projection 损坏/缺 embedding | 检索标记 `DEGRADED`，BM25 继续可用，后台自动重嵌入 | 检查 ARAG health/repair 日志与 embedding provider；通常无需手工改库 |
| Artifact 被篡改 | 所有读取拒绝 | 从可信源重新上传，不要覆盖既有 digest 身份 |
| release 不匹配 | Run=`INCOMPATIBLE_RELEASE` | 使用匹配 release 或明确清理开发数据 |
| trace 写失败 | Runtime 恢复不受影响 | 单独修复权限/空间 |

## 14. 常见排障

| 现象 | 检查 |
|---|---|
| CreateRun 503 | Worker 是否存活；`GET /healthz` 是否有三个 active releases |
| Run 长期 `DISPATCH_PENDING` | Worker 日志、`runtime_workers`、SQLite lock、Activity `available_at` |
| `CONVERSATION_BUSY` | 查询该 conversation 的现有 Run；等 terminal 或显式 cancel，不要换幂等 key 绕过 |
| SSE 看起来跳号 | visibility 过滤的正常结果；按 opaque cursor 重连 |
| 图片不可用 | 先上传 Artifact，再把 digest 放入 `attachment_refs`；检查 20MiB 上限 |
| 索引提交后搜不到 | job 必须到 `ACTIVATED`；检查 dataset/scope 和 ARAG health |
| Tool effect unknown | 不要手动重复副作用调用；查 `tool_executions` reconcile/manual 状态 |
| `SQLITE_BUSY` | 确认本机磁盘、短事务、单 Worker 进程和 busy timeout；排查外部进程长事务 |
| A2A import warning | ADK A2A 为 experimental，精确依赖应为 `google-adk[a2a]==2.6.2`、`a2a-sdk==1.1.2` |
| `agentbay` 调用失败 | 当前是预留桩，使用 `SANDBOX_PROVIDER=local` |
| 修改 `.env` 无效 | 重启对应 API/Worker/下游进程 |

## 15. 停机

一键脚本用 Ctrl-C。手动模式先停止 Runtime API 接收新请求，再向 Worker 发 SIGTERM；Worker 停止领取并最多等待配置的 grace period，未完成 Activity 随后由 lease recovery 接管。最后停止下游服务。
