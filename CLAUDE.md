# CLAUDE.md

本仓库的完整 AI 编码约定以 `AGENTS.md` 为准；运行步骤见 `RUNBOOK.md`，R0 冻结语义见 `docs/reliability/README.md`。本文件保留 Claude/Codex 在每次改动前必须掌握的最小、同步架构上下文。

## 定位与决策

项目由公司生产链路抽取、简化，用于个人学习、架构验证和面试准备，不承担真实线上流量。当前仓库是可本机运行的持久化 Agent Runtime 参考实现，但方案设计必须从真实集群化、分布式部署出发，不能把当前单机形态当作目标架构上限。

不受旧接口、旧本地数据、旧状态、旧行为、既有技术债或线上兼容要求约束，不建设 compatibility/shadow/双写层；不合理的旧设计可直接替换或删除。优先采用当前先进、优秀且可验证的生产级方案；边界 case、状态机、并发、幂等、超时、重试、部分失败和恢复必须严谨。改动后仍必须保持四服务五进程、主链路和与风险相称的可靠性测试完整。

代码保持简洁直白，变量、类型、函数、注释和文档语义明确。不要炫技或滥用语法糖、元编程和设计模式；先进性体现在设计方案而不是语法。组合优于继承，继承链最多两级：`Base -> Sub`。

共享状态和协调机制默认按多实例、跨节点设计。取消信号、租约、锁、幂等记录或执行进度不能只保存在进程内存或单机私有文件；例如“停止回复”应由 Redis 等中心化协调组件或等价的持久化权威承载。缺少必要的 Redis、消息队列或分布式存储时，明确提出依赖、边界与取舍并和用户讨论，不能用单机 workaround 充当最终方案。

生产实现参考（只提炼设计）：

| 方向 | 路径 |
|---|---|
| 接入/会话/上传 | `/Users/shixiangweii/IdeaProjects/sxw_work/codes/fy26_albert_chat2/albert-chat-2` |
| Agent Runtime | `/Users/shixiangweii/PycharmProjects/fy26_deap_agent/albert-agent-2` |
| Skill Center/A2A | `/Users/shixiangweii/IdeaProjects/sxw_work/codes/2026_albert-skill-center_proj/albert-skill-center` |
| ARAG | `/Users/shixiangweii/PycharmProjects/arag_learn_proj/lippi-arag` |

## 主链路

```text
Artifact upload → POST /api/v1/runs → runtime.db
→ independent Runtime Worker → RunCoordinator
→ per-Run EngineAdapter → Tool/Artifact/ARAG/Skill/A2A
→ committed Canonical Events → SSE replay/tail
```

进程：Runtime API(:8000)、Runtime Worker、ARAG(:8100)、skill-center(:8200)、A2A(:8300)。API 只做 admission/status/cancel/signal/Event/Artifact/Web，不能加载 LLM 或远程 Tool 目录；Worker 负责执行并注册三个 immutable engine release。Web 面有两个：`/chat-ui` 会话，`/trace-ui` 只读 Trace Console（列表 + 瀑布图，读 `GET /api/v1/traces[/{id}]`）。

API 与 Worker 当前共享本机 `runtime.db` 和 Artifact CAS。这是实现现状和测试基线，不是长期设计准则；新增能力仍须检查多实例一致性、跨节点协调、节点失联和恢复行为。

## 代码分层

`agent/runtime/` 是六边形分层，**改行为前先认准该改哪一层**：

| 层 | 职责 |
|---|---|
| `domain/` | `models.py` 权威对象与枚举、`artifact.py`、`errors.py`；无 I/O |
| `ports/` | `store.py`/`engine.py`/`tool.py`/`artifact.py`/`clock.py` 接口定义 |
| `application/` | 编排：`coordinator.py`（terminal 唯一提交者）、`admission.py`、`events.py`（三代引擎唯一事件出口）、`tool_broker.py` |
| `adapters/` | `sqlite/store.py` 是全部 SQLite authority（约 4.4k 行，改状态机基本都落在这里）、`filesystem_artifact.py`、`legacy_engines.py` |
| `api/` `worker/` | 两个进程各自的入口 |

三代引擎的实现在 `agent/engine/{plan_execute,agent_loop,native_loop}/`（内部 `ReasoningEngine`，见 `agent/engine/base.py`），由 `adapters/legacy_engines.py` 桥接成公开的 `EngineAdapter`。**别把 `agent/engine/` 当公开契约**：它是 adapter 内的兼容面。

## 不变量

- CreateRun 必填 `Idempotency-Key` 与 `engine=plan_execute|agent_loop|native_loop`；engine 是 Run 不可变字段。
- 同 scope key+digest 返回原 Run，不同 digest 409；幂等重放优先于 conversation busy。
- 同 conversation 同时最多一个非终态 Run。
- `runs` 是 status/terminal 唯一裁判，terminal 最多一个且只由 RunCoordinator 提交。
- Activity 由 `activities` 裁决；WorkingState 的 model plan 不是调度表。
- history 由 committed USER + 仅成功 ASSISTANT + checkpoint 编译；失败 partial delta 不进入后续语义历史。
- Canonical Event append-only；seq 分配和插入同事务。只有 committed event 可被 SSE 读取。
- output delta 按 100ms/2KiB 聚合并在边界前 flush；final assistant + citation + success terminal 原子提交。
- SSE 断开只停止观看，不取消 Run。cursor 在客户端；首版无服务端 ACK/观看位置表。
- lease/revision/fencing 抑制过期 Worker 迟到结果；Worker 丢失只触发恢复。
- deadline 为绝对 UTC；所有下游接收剩余预算。
- Trace 只诊断，关闭/缺失不影响恢复。
- SQLite 写事务必须短，事务内禁止 LLM、Tool、RAG、Skill、文件系统或等待人工。

完整状态机、错误码和 failure matrix 在 `docs/reliability/`；实现与其冲突时不得静默另起语义。

## 存储

| 路径 | Authority |
|---|---|
| `local_storage/runtime/runtime.db` | Run/Activity/Event/Checkpoint/ToolExecution/Signal/Timer/Release |
| `local_storage/artifacts/sha256/` | Runtime Artifact bytes |
| `local_storage/arag/rag.db` | Document/version/chunk/index job 权威及可重算 embedding rows |
| `local_storage/arag/documents/sha256/` | 原始文档 bytes |
| `local_storage/demo_effects/effects.db` | 模拟外部副作用，故意独立 |
| `local_storage/traces/` | 诊断，不是业务事实 |

Runtime/RAG 使用 `aiosqlite + 显式 SQL + checksum migration`，不引入 ORM/Alembic 或第二 backend。Runtime 连接启用 WAL、`synchronous=FULL`、foreign keys、busy timeout；未知 schema/checksum 改写 fail-fast。

这些 SQLite 规则属于当前冻结实现。分布式需求确实需要调整存储或协调架构时，先明确依赖，再通过 ADR、Schema、migration 和 reliability test 完整替换；不得临时增加第二事实源或退回进程内状态。

Artifact ID 等于 SHA-256；写入为 temp/fsync/atomic rename/fsync dir，再提交 metadata/ref。非图片附件只给模型已校验的 8KiB preview，`read_artifact` 有界读取默认 32KiB/最大 64KiB；图片从已校验 CAS 物化为 attempt-local 多模态 Part。HTTP Range 单次 1MiB。大结果/read 切片只存 Artifact ref/preview，native 恢复时重新物化。

## 三代引擎

公开端口：`EngineAdapter.execute(EngineRunRequest, RuntimeIO) -> EngineOutcome`。内部 `ReasoningEngine` 和 event draft 只是 adapter 内兼容面，不能裁决公开 terminal。

- `plan_execute`：显式 decision plan → execution；plan checkpoint 后不得重新规划。
- `agent_loop`：ADK `BaseLlmFlow` 驱动 Tool-Use Loop；Plugin/LiteLlm 做 guard、异常反馈和循环控制。
- `native_loop`：自研 loop；只读工具分批并发、流式工具执行、compact；`native-kernel-v1` 在 model request、完整 ToolCall batch、每个 ToolResult、next-turn/completed 提交 checkpoint。

三引擎共享系统指令/Tool/Skill/RAG 契约。修改共享 prompt/工具必须分别验证两个 loop 引擎。`SUB_AGENT_ENGINE` 取 `auto` 时跟随当前 Run；远端 A2A 不受影响。

两个 ADK 引擎只承诺 invocation/step 边界恢复，不宣称 mid-turn deterministic replay。ADK attempt-local session/artifact 用后销毁；native 不得恢复进程级历史。`native_loop` 从最后 committed kernel checkpoint 恢复；半个 model stream 重放 model slot，已提交 ToolExecution 由稳定 slot/Broker 复用，但不承诺 provider token 级流重放。`/reliability-demo` 是在通用 checkpoint 之上的 HITL signal → 独立幂等副作用 → Artifact 确定性纵切。

## Tool、Skill、A2A

Tool effect 必须显式分类：

- READ_ONLY 可受控 retry；
- IDEMPOTENT_EFFECT 必须透传 Runtime stable idempotency key；
- NON_IDEMPOTENT_EFFECT/UNKNOWN_EFFECT 不得透明 retry；
- dispatch 后 timeout/ACK 不明进入 UNKNOWN → reconcile/manual；MANUAL_REQUIRED 只接受严格 `tool_reconciliation` signal，人工 committed/failed 或再次查询与 Tool Activity、canonical result、父 Run 原子推进，人工 failed 不得重发；
- COMMITTED 结果复用；同 stable slot 的 name/request digest 不一致 fail closed。

未评审的新 Skill/A2A/Claude SKILL 默认 UNKNOWN_EFFECT。Skill UI 必须先 commit Canonical Event 再发布。

skill-center NDJSON 以独立 EOF frame 结束并保留首个失败 sticky。Claude SKILL 是 Agent-as-Tool，LocalSandbox 非生产隔离，AgentBay 是桩；无跨 Skill Artifact 和子 Runner HITL。A2A 是无父历史远端新会话，请求必须自包含。

精确锁定 `google-adk[a2a]==2.6.2` 和 `a2a-sdk==1.1.2`；依赖 ADK 私有契约处升级时重新审计，缺符号应 fail-fast。

## ARAG

入库是 durable job：`PREPARED → BUILDING → VALIDATING → READY → ACTIVATED|FAILED`。parse/caption/embed 在事务外；验证后单事务切 active version。Retrieval 只看 active version，旧 chunks 不可见。

numpy vector/BM25 是从 `rag.db` active truth 重建的不可变投影，不是 Document SSOT。投影损坏应重建并返回 DEGRADED。检索稳定区分 HIT/MISS/DEGRADED/DENIED/ERROR；Evidence 带完整 document/index/version/hash/page/span/scope/query 字段。

GraphStore 仍未接检索流。

## 命令

所有 Python 使用 `.venv`：

```bash
PY=.venv/bin/python

bash scripts/run_all.sh
bash scripts/check.sh

# 手动五进程
$PY -m uvicorn a2a_service.main:app --port 8300
$PY -m uvicorn skillcenter.main:app --port 8200
$PY -m uvicorn arag.main:app        --port 8100
$PY -m agent.runtime.worker.main
$PY -m uvicorn agent.main:app       --port 8000

# 单个测试 / 一个文件 / 关键字（pyproject 已设 asyncio_mode=auto，无需额外参数）
$PY -m pytest tests/reliability/test_trace_independence.py -q
$PY -m pytest "tests/reliability/test_runtime_api.py::test_create_requires_idempotency_key" -q
$PY -m pytest tests/reliability -q -k trace

# 真实 LLM 行为评测（需环境变量 Key）
bash eval/run_eval.sh
```

`run_all.sh` 必须等待本次 Worker 的新鲜 `ACTIVE` heartbeat，且 release map 与三个 active pointers 完全一致后才能启动 API；不能用旧库中已有的三行 pointer 充当 readiness。脚本保持 macOS Bash 3.2 兼容，并监督全部五个进程。

`scripts/check.sh` 执行 py_compile、`pytest tests/reliability`、schema/checksum 和旧协议扫描。可靠性 PASS 不依赖真实 LLM；smoke/eval 依赖真实 Key，行为得分不能替代可靠性门禁。

门禁里有四条会**静默挂掉**的硬约束，动手前先确认：

- REL/FI 编号在 `check.sh` 里钉死为 `REL-001..030`、`FI-01..12`，且每行必须至少引用一个真实存在的 pytest 节点。新增可靠性测试要挂到**既有编号行**，自造 `REL-031` 会直接失败。
- 旧协议扫描覆盖 `agent/`、`arag/`、`web/`、`eval/harness/` 与六份根文档的 `.py/.js/.html/.md/.example`，命中任一条已废弃协议/权威的字面量即失败（正则表见 `scripts/check.sh` 末段的 `patterns`：已删除的 chat 端点、启动期引擎选择、旧流结束事件、旧历史/引用/会话所有者）。写前端、注释和文档时最容易误触——**连"说明不要用它"的那句话本身也会被扫到**，要描述就指向 `check.sh`，别把字面量抄进来。
- `docs/reliability/schemas/` 六份 v1 Schema 是冻结契约且 `additionalProperties: false`，并由 `test_schema_contracts.py` 校验**真实权威对象**的 `model_dump`。给 `RuntimeEnvelope`/`CanonicalEvent`/`WorkingState` 等加字段必须同步 Schema 与 ADR；纯诊断字段应挂在契约之外（例如 `RunRecord.trace_id`，见 ADR-0007）。
- SQLite migration 是 checksum 校验的**增量叠加**（`agent/runtime/adapters/sqlite/migrations/`），新增 `00N_*.sql` 会自动应用到既有库。不要为了"干净重建"去删 `local_storage/` 下的库——那既没必要，又会毁掉验证升级路径的证据；需要干净库时用 `tmp_path`。

## 改动要求

- 设计先按集群化部署检查共享状态归属、跨节点协调、一致性、幂等、故障恢复和扩缩容；缺少必要中间件时先提出并讨论。
- 代码和抽象保持直白：命名、注释、文档语义明确，避免炫技与过度设计，优先组合，继承链最多 `Base -> Sub` 两级。
- 改状态机/事务/契约：先读 `docs/reliability/`，同步 ADR、JSON Schema 和 reliability test。
- 改架构、API、配置、端口、能力边界或 eval：同步 `README.md`、`RUNBOOK.md`、`AGENTS.md`、本文件、`eval/README.md`、`.env.example`。
- 新 Tool 必须注册 effect manifest；新 native 只读 Tool 还要显式并发安全。
- 新 Claude SKILL 包含根 `SKILL.md`、frontmatter 和全部资源；默认不并行、effect unknown。
- API Key 只在真实环境变量/被忽略 `.env`，禁止出现在代码、文档、报告和提交。
- 工作树可能有用户改动；保留无关修改，不做破坏性 reset。

## 诚实边界

当前实现没有 Memory、完整 Context Compiler、PostgreSQL/Temporal、Redis 协调层、分布式 HA、磁盘容灾或服务端 Delivery ACK。这些是现状而不是新增单机设计的依据；需要相关能力时应显式提出依赖和演进方案。Prompt cache 显式断点仅 Anthropic 生效；默认 DashScope 是 no-op。Runtime 已有 Artifact/signal 不代表 Claude SKILL/A2A 自动具备跨调用 Artifact 或 HITL。
