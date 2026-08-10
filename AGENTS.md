# AGENTS.md

本文件记录 AI 编码时必须掌握的项目定位、可靠性所有权、跨模块架构和工程约定。用户入门见 `README.md`，运行与排障见 `RUNBOOK.md`，R0 冻结规格见 `docs/reliability/README.md`，行为评测见 `eval/README.md`。

## 项目定位

项目由公司生产链路抽取、简化，用于个人学习、技术方案验证和面试准备；不承担真实线上流量。当前仓库提供可在本机运行的持久化 Agent Runtime、三代推理引擎、Tool/Skill/A2A、混合召回 RAG、SSE 和评测实现，但设计目标不受单机演示形态限制：新增或重构方案必须从真实集群化、分布式部署出发审视状态、并发、故障和恢复边界。

长期决策：不受既有技术债和历史兼容包袱约束，不需要兼容旧 API、旧行为、旧业务数据或旧本地数据。目标状态不保留 schema migration、compatibility/shadow 层或为旧数据而设的兼容层；不合理的旧设计可直接替换或删除。临时双写或过渡适配只能有明确用途、删除条件和验证方式，不得成为最终状态。新代码不得主动制造缺乏明确收益的技术债。优先采用先进、成熟、可验证且与问题匹配的生产级方案；新增复杂度必须有明确收益。没有兼容包袱不等于降低质量：边界 case、状态机、并发竞争、幂等、超时与重试、部分失败、资源上限、背压、安全、可观测性和故障恢复都必须严谨；四服务五进程、主链路、可选下游降级和与风险相称的测试必须保持完整。

命名、代码结构、注释和文档应保持简洁直白、语义明确。不要为展示语法技巧而炫技，不滥用语法糖、元编程或设计模式；先进性应体现在方案和不变量上。优先选择可直接阅读的线性流程，不为了套用模式制造无价值的间接层；但不得以此放任失控的长函数、实质性重复或复杂分支，仍需控制函数复杂度、封装稳定职责，并只在确有收益时建立层次合适的抽象。组合优于继承，慎用继承；项目自有类最多两层 `Base -> Sub`，不允许 `Base -> Middle -> Sub`。

生产目标方案默认面向多实例和跨节点协作。涉及共享状态、任务调度、会话、取消、租约、幂等、副作用或进度管理时，必须明确跨节点的一致性、故障和恢复边界，不能只依赖进程内存或单机私有文件。若目标方案缺少所需中间件依赖，应明确指出约束、候选方案和取舍并与用户讨论。本地开发和测试可以使用单机适配器，但不得把只能在单进程或单节点上保证正确性的实现定义为生产目标方案。

生产项目源码默认只读，仅用于提炼设计；未经明确要求不得修改参考项目，也不得机械复制内部配置和治理代码：

| 方向 | 参考路径 |
|---|---|
| 接入、会话、文件上传 | `/Users/shixiangweii/IdeaProjects/sxw_work/codes/fy26_albert_chat2/albert-chat-2` |
| Agent Runtime/推理引擎 | `/Users/shixiangweii/PycharmProjects/fy26_deap_agent/albert-agent-2` |
| Skill Center/A2A | `/Users/shixiangweii/IdeaProjects/sxw_work/codes/2026_albert-skill-center_proj/albert-skill-center` |
| ARAG | `/Users/shixiangweii/PycharmProjects/arag_learn_proj/lippi-arag` |

## 当前实现架构

```text
Client
  → Artifact upload
  → POST /api/v1/runs（durable accepted）
  → runtime.db
  → 独立 Runtime Worker
  → RunCoordinator → per-Run EngineAdapter
  → Tool Broker / Artifact / ARAG / Skill / A2A
  → committed Canonical Events
  → SSE replay/tail
```

四个服务、至少五个进程：

1. `agent.main:app`，:8000，仅 admission/status/cancel/signal/event/Artifact/Web；禁止加载 LLM 和远程工具目录。Web 挂载两个静态面：`/chat-ui`（会话）与 `/trace-ui`（只读 Trace Console），共用 `web/tokens.css`。API 进程本身不产生 span——它与 Worker 各有独立 `_Tracer`，同 trace_id 会写出两个文件。
2. `agent.runtime.worker.main`，无 HTTP；加载 LLM/工具/Skill/A2A、注册三个 release、领取 Activity。
3. `arag.main:app`，:8100；内部另有 durable index-job task。
4. `skillcenter.main:app`，:8200。
5. `a2a_service.main:app`，:8300。

API 和 Worker 当前共享本机 `runtime.db` 与 Artifact CAS。可选下游不可用时按既有 best-effort 语义降级；但 Worker 启动时只加载一次远程目录，恢复下游后通常需重启 Worker。这是仓库现状和当前测试基线，不是长期架构上限；新增能力不得因为现有部署是单机就忽略多实例一致性和跨节点故障场景。

## 唯一事实来源

| 问题 | Authority | 非权威 |
|---|---|---|
| admission/idempotency | `run_requests` | HTTP 连接、日志 |
| Run status/terminal | `runs` | Engine 内部控制事件、SSE EOF、Trace |
| Activity progress | `activities` | model plan、UI、WorkingState |
| Conversation history | committed USER events + 仅成功的 ASSISTANT event | 失败 partial delta、attempt-local session/messages |
| checkpoint | append-only `checkpoints` + revision CAS | Trace、request-local tool state |
| Tool effect | `tool_executions` | HTTP timeout 推测、参数 hash |
| Artifact bytes | SHA-256 CAS | preview、路径名 |
| Document/version/chunks | `rag.db` active version | numpy/BM25 snapshot |
| Evidence | committed EvidenceSet/版本字段 | filename、显示序号 |
| release | immutable manifest + active pointer | 当前进程环境的临时拼接 |
| client cursor | 客户端本地 | Runtime DB；首版无服务端投递确认 |
| Trace | 诊断 store | 任何业务恢复或裁决 |

完整表见 `docs/reliability/state-ownership-registry.md`。不要新增第二写路径。

## Runtime 领域约束

- CreateRun 必填 `Idempotency-Key` 和 `engine=plan_execute|agent_loop|native_loop`。
- 幂等范围 `(principal_id, agent_id, idempotency_key)`；同 key/digest 返回原 Run，不同 digest 409。
- 幂等重放先于 conversation busy 检查；同 conversation 只有一个非终态 Run。
- Run terminal 最多一个；只有 RunCoordinator 能提交 terminal。
- Engine outcome 是 Coordinator 输入，不得根据内部事件、生成器退出或 HTTP/SSE 生命周期推导 terminal。
- SQLite 时间为 UTC epoch ms；API 输出 RFC3339 UTC。业务 ID 带前缀 UUIDv4；可恢复 logical child 用 UUIDv5。
- Event append-only，`runs.next_seq` 更新与 event batch 同事务；回滚不留 seq 洞。
- output delta 按 100ms/2KiB 聚合，切换 message/Tool/checkpoint/terminal 前 flush；先 commit，后 SSE 可见。
- final assistant + citation + success terminal 同一事务。
- cancel/complete 由提交顺序决定；cancel-first 的 late success 不能覆盖。dispatched/unknown effect 存在时进入 `CANCEL_REQUESTED`。
- deadline 是绝对 UTC，向下只传剩余预算，不在每层重新开始 timeout。
- Worker 领取使用 lease/revision/fencing；旧 fencing 结果拒绝，Worker 丢失不直接失败 Run。
- 所有 SQLite 写使用短 `BEGIN IMMEDIATE`；事务内禁止 LLM、Tool、RAG、Skill、文件系统或等待人工。

Runtime 和 RAG 当前仍使用遗留的 checksum migration 实现；它是待独立移除的历史代码，不要求立刻大改，但从现在起不得新增增量 migration。目标是单一的当前 schema 初始化与版本/checksum 校验。数据库仅支持当前 schema：schema 或 checksum 不匹配时必须 fail-fast，并提示用户显式删除或重建对应本地数据库；程序不得静默删除、覆盖或自动迁移旧数据库。连接必须启用 WAL、`synchronous=FULL`、foreign keys、busy timeout。不要引入 ORM/Alembic 或预留 PostgreSQL backend。

数据库跨版本升级、滚动发布期间的 schema 兼容和旧数据保留不在本项目范围内。后续通过独立、可验证的重构移除遗留 migration，实现单一当前 schema 初始化和版本/checksum 校验。若新需求改变存储或协调架构，先明确分布式需求与中间件依赖，再按 ADR、current schema、版本/checksum 与 reliability test 完整替换；不能在现有 SQLite 旁边临时增加第二事实源，也不能退化为进程内状态。

## Engine Adapter 与三代引擎

对外端口是：

```text
EngineAdapter.execute(EngineRunRequest, RuntimeIO) -> EngineOutcome
```

`RuntimeIO` 提供 committed EventSink、checkpoint CAS、Tool Broker、Clock、deadline/cancel probe。当前三代引擎内部的窄 `ReasoningEngine`/event draft 只存在于 adapter 后，不得泄漏为公开传输或终态协议。

- `plan_execute`：decision planner → execution planner；plan checkpoint 后恢复不得重新规划。
- `agent_loop`：循环位于 ADK `BaseLlmFlow`；Plugin/LiteLlm 承担参数 guard、异常反馈、预算/收口。
- `native_loop`：自研 loop，支持只读工具分批并发、流式工具执行和 context compact；`native-kernel-v1` 在 model request、完整 ToolCall batch、每个 ToolResult、next-turn/completed 提交 checkpoint。

三引擎共享系统指令和 loop 工具面，修改 `agent/engine/loop_tools/` 必须同时验证 `agent_loop` 与 `native_loop`。`SUB_AGENT_ENGINE` 取 `auto` 时跟随当前 Run；`plan_execute` 的子 Runner 映射为 ADK。A2A 是远端自身 ADK，不受此配置影响。

恢复边界必须诚实：两个 ADK 引擎是粗粒度 invocation/step 恢复，不宣称 mid-turn deterministic replay。`native_loop` 从最后 committed kernel checkpoint 恢复；半个 model stream 会重放 model slot，已提交 ToolExecution 靠稳定 slot/Broker 复用，但不承诺 provider token 级重放。`/reliability-demo` 是在通用 checkpoint 之上的 WAITING_INPUT → signal → 幂等副作用 → Artifact 确定性纵切；不要把 Runtime 通用 signal 能力描述成所有子 Runner 都已支持 HITL。

ADK attempt 的 session/artifact 适配器必须每 attempt 创建并销毁；跨 attempt history 只能从 Canonical Events + Checkpoint 编译。native 不得重新引入进程级历史 store。

## Tool Broker 与 Artifact

ToolManifest 至少包含 release digest、effect class、timeout/attempt、idempotency/reconcile/cancel 支持、结果策略、并发与独占资源。

- `READ_ONLY` 可安全受控重试；
- `IDEMPOTENT_EFFECT` 必须向下游透传稳定 Runtime idempotency key；
- `NON_IDEMPOTENT_EFFECT/UNKNOWN_EFFECT` 不得透明 retry；
- `DISPATCHED` 后 timeout/ACK 不明进入 `UNKNOWN`，先 reconcile，无 hook/仍不明进入 manual；
- `COMMITTED` 的完整结果/ref 直接复用；
- stable slot 重放的 tool name/request digest 不一致必须 `TOOL_REPLAY_MISMATCH`；不能用 args hash 猜 identity。
- `MANUAL_REQUIRED` 只能用严格 `tool_reconciliation` signal 处置：`mark_committed/mark_failed/reconcile` 与 Tool Activity/Event/父 Run 在一个短事务中推进；人工 failed 是 sticky，普通 signal 不得只唤醒父 Activity。

默认评审为 READ_ONLY 的工具清单见 `agent/runtime/adapters/brokered_tools.py`；未声明的 Skill/A2A/Claude SKILL 是 UNKNOWN_EFFECT。

Artifact 身份等于小写 SHA-256。写入边界为 temp → digest/size → fsync → atomic rename → fsync dir，之后才可提交 metadata/link/event。非图片附件默认只给模型已校验的 8KiB preview，`read_artifact` 有界读取默认 32KiB/最大 64KiB；图片从已校验 CAS 物化为 attempt-local 多模态 Part。HTTP 单 Range 最大 1MiB。业务读取必须校验 digest，不可直接读诊断路径。大 ToolResult/read 切片只保存 ref/preview，不能复制到 Event/Checkpoint/Trace；native 恢复时从 Artifact + 已提交 ToolExecution 重新物化。

## ARAG

`rag.db` 保存 `documents/document_versions/active_document_versions/chunks/index_jobs/chunk_embeddings/projection_metadata`。原文位于 `local_storage/arag/documents/sha256/`；其 schema 生命周期遵循上文“仅当前 schema、fail-fast、显式重建”的规则。

入库端点返回 `202 + job_ids`，job 为：

```text
PREPARED → BUILDING → VALIDATING → READY → ACTIVATED | FAILED
```

parse/caption/embed 在事务外，校验后单事务切 active pointer。Retrieval 只读 active version；旧 version 即使保留也不可见。chunk ID 由 version + ordinal + content hash 决定。

numpy vector/BM25 是进程内不可变、可重建投影。投影缺失/损坏时从 SQLite truth 重建，并返回 `DEGRADED`；禁止把索引反向作为文档事实源。检索状态必须稳定区分 `HIT/MISS/DEGRADED/DENIED/ERROR`。Evidence 必须带 document/index version、content hash、page/span、scope 和 query identity。

## Skill、Claude SKILL 与 A2A

- skill-center NDJSON：若干 `eof=false` 数据帧 + 独立 `eof=true,data=null`；坏帧/缺 EOF 为稳定协议错误，首个失败 sticky。
- Skill UI frame 先成为 committed Canonical Event，再由 SSE 投影；易失队列不是事实源。
- Claude SKILL 是 Agent-as-Tool，每次复制完整包进独立 LocalSandbox；并发由 `parallel_safe/exclusive_resources` 与全局上限共同控制。
- LocalSandbox 是子进程工作目录限制，不是生产隔离；AgentBay 仍是不可运行桩。
- 当前没有 Claude SKILL 跨 Skill Artifact、子 Runner HITL/暂停恢复。
- A2A 每次远端调用是无父历史新会话，请求必须自包含；ADK A2A 仍 experimental。

精确锁定 `google-adk[a2a]==2.6.2`、`a2a-sdk==1.1.2`。工具参数 shim/telemetry hook 等私有契约升级时必须重新审计，符号不匹配应 fail-fast。

## SSE 与可观测

公开 SSE 从 committed `run_events` 短查询 replay/tail，支持 `after_seq` 和 `Last-Event-ID`；显式 query cursor 优先。visibility 可造成 seq 跳号。heartbeat 没有 seq。断开订阅不取消 Run；显式 cancel 是另一条命令。

主要投影：`text/tool_call/tool_result/plan_step/skill_event/citation/run_status/activity_status/terminal`。终态只看 `RUN_TERMINATED` 和 GET Run status。

Trace 分为 admission、Worker attempt、SSE subscription 等独立请求/执行轨迹，只用于诊断。`TRACE_PAYLOAD_LEVEL=full` 会落用户原文和模型输入，文件不得随报告分发；二进制必须被摘要占位。关闭 Trace 后全部恢复测试仍应通过。

## 常用命令

```bash
PY=.venv/bin/python

# 四服务五进程 + sample job
bash scripts/run_all.sh

# 手动
$PY -m uvicorn a2a_service.main:app --port 8300
$PY -m uvicorn skillcenter.main:app --port 8200
$PY -m uvicorn arag.main:app        --port 8100
$PY -m agent.runtime.worker.main
$PY -m uvicorn agent.main:app       --port 8000

# 统一门禁
bash scripts/check.sh

# 行为评测（需要真实 Key）
bash eval/run_eval.sh
```

`run_all.sh` 的 Worker readiness 必须同时验证本次启动后的新鲜 `ACTIVE` heartbeat 与三种 active release pointer 完全一致；只看持久库里已有的三行 active release 会把旧指针误判为当前 Worker ready。脚本须保持 Bash 3.2 兼容，并持续监督五个子进程。

`scripts/check.sh` 应执行 py_compile、`pytest tests/reliability`、SQLite schema/checksum 验证和旧协议字符串扫描。遗留 migration 尚未移除前，其既有 checksum 检查仍是当前门禁的一部分；不得新增迁移。真实 LLM 行为分数不属于可靠性 PASS。

## 修改规则

- 搜索文件优先 `rg`/`rg --files`；Python 使用 `.venv`。
- 先按集群化部署检查设计：共享状态归属、跨节点协调、一致性、幂等、故障恢复和扩缩容必须有明确答案；缺少必要中间件时先提出并讨论。
- 实现保持直白、语义明确；优先线性流程而不是模式堆砌，同时控制函数复杂度、封装稳定职责、消除实质重复，只做合适层次的必要抽象；优先组合，项目自有类只允许 `Base -> Sub` 两层继承。
- 未触及的历史代码不强制改造；新增或实质修改的代码路径必须遵守本规则。不要为了顺手清理而扩大范围；每次小步重构必须独立正确、可测试。
- 编辑 Runtime 状态/事务/契约前先读 `docs/reliability/`；若改变冻结语义，先 ADR + current schema/版本/checksum + reliability test，禁止新增增量 migration。
- 改 API、配置、端口、架构、能力边界或评测协议，同步更新 `README.md`、`RUNBOOK.md`、本文件、`CLAUDE.md`、`eval/README.md` 和 `.env.example`。
- 改共享 prompt/工具面，分别验证两个 loop 引擎；不要用一代引擎结果代替另一代。
- 新通用工具：在 `agent/tools/builtin_tools.py` 实现并注册；同时给 ToolManifest effect 分类。native 只读并发还需声明安全性。
- 新 skill-center Skill：更新 `skillcenter/skills.py` 定义和执行分支。
- 新 Claude SKILL：完整包放 `agent/claude_skill/skills_data/<id>/`，根部 `SKILL.md` 声明并发/独占资源；新条目默认 UNKNOWN_EFFECT，需显式评审。
- 新 A2A：在 `a2a_service/agents.py` 暴露，并在 skill-center 注册；effect 同样显式评审。
- 任何大结果先 Artifact 化；敏感 Key 只来自真实环境变量或被忽略 `.env`。
- 输入需求与本项目背景约束冲突时，主动指出并与用户讨论后再继续。
- 工作树可能有用户改动，保留无关修改；禁止破坏性 reset。

## 诚实边界

- 当前实现只支持单机进程级恢复，不是分布式 HA，也不抵御磁盘/主机丢失；这是待演进的能力边界，不能作为新增单机方案的设计依据。
- 当前没有 Memory、完整 Context Compiler、PostgreSQL/Temporal、Redis 协调层或服务端 Delivery ACK；当前数据库也不支持跨版本升级、滚动发布期的 schema 兼容或旧数据保留。设计需要相关能力时应显式提出依赖和演进方案。
- 两个 ADK 引擎没有 mid-turn deterministic replay。
- Prompt cache 显式断点仅对 Anthropic 生效，默认 DashScope 下为 no-op。
- GraphStore 未接检索流；AgentBay 不可运行；LocalSandbox 非生产隔离。
- Runtime Artifact/signal 已存在，不代表所有 Skill/A2A/子 Runner 自动获得跨调用 Artifact 或 HITL。
