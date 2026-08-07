# sxw_agent-2_demo — 生产级 Agent 运行时（规划/推理引擎）+ 混合召回 RAG

基于 **Google ADK 2.6.2** 精简复刻的一套**可独立运行的生产级 AI Agent 系统**，核心展示「**Agent 运行时的规划/推理引擎**」与混合召回 RAG。系统由 agent、arag、skill-center、a2a_service **4 个 FastAPI 服务**经 HTTP 协作，覆盖提问、规划与执行、工具/技能调用、知识检索、多模态输入、SSE 流式返回和全链路可观测。

## 项目定位与背景

本项目从公司生产项目的核心链路中抽取、简化而来，用于个人学习、技术方案验证和面试展示。它保留生产系统的关键架构形状与工程取舍，但不是生产源码的完整镜像，也不承担真实线上流量。

生产参考主要来自四个方向：接入层的会话管理与文件上传、Agent 核心运行时与推理引擎、技能中心与 A2A、ARAG 检索。仓库把这些能力裁剪并重新组织成可在本机独立运行的最小完整系统。

## 项目目标

1. 展示两代推理引擎从 Plan-Execute 到统一 Tool-Use Agent Loop 的演进、统一抽象与行为差异。
2. 复刻工具调用、技能执行、子代理委派、异常反馈、熔断和降级等生产级 Agent 主链路。
3. 展示向量检索、BM25、RRF 融合、查询改写、多模态入库和引用生成组成的工程化 RAG。
4. 通过统一 SSE、trace_id、结构化日志和真实 LLM 评测，让系统可运行、可观察、可比较。
5. 形成一套结构清晰、边界诚实、适合讲解和持续实验的 AI Agent 样板工程。

## 设计与改造原则

- 这是学习与面试项目，后续改造**不要求历史兼容、存量技术债兼容或线上灰度兼容**；可以调整接口、数据结构和模块边界，优先采用当前更先进、更清晰的技术方案。
- “不考虑兼容性”不等于忽略工程质量。任何改动仍应保持主链路可运行、关键失败可降级、配置/文档/评测同步更新。
- 优先保留能体现 Agent 核心能力和工程取舍的内容，非核心企业治理能力可以裁剪，过时实现可以直接替换或删除。
- 对未完整实现的能力保持诚实：Anthropic PromptCache 在默认 DashScope/Qwen 下是 no-op；AgentBay、GraphStore 等仍是演示桩或预留端口；LocalSandbox 不等同于生产级隔离。
- 密钥只通过环境变量或本地 `.env` 注入，禁止写入代码、文档、评测数据或 Git 历史。

---

## 架构

```
 用户(带图提问)
     │  HTTP + SSE(text/event-stream)
     ▼
┌──────────────────── agent 运行时 (ADK, :8000) ─────────────────────┐
│  POST /api/v1/chat/{uuid}/stream                                   │
│  ReasoningEngine（ENGINE 可切换）                                  │
│    ├─ Gen1 Plan-Execute：decision planner → execution planner      │
│    └─ Gen2 Tool-Use Agent Loop：ReAct 单循环 + 插件加固            │
│         · ToolArgsGuard（非对象参数→反馈，不分发真实工具）          │
│         · ToolErrorFeedback（异常→function_response，不中断）       │
│         · ContextOverflow（超长截断重试）· PromptCache(provider-aware)│
│         · TaskPlan 续推 · force-summary · message budget            │
│         · Agent-as-Tool（SKILL 子 Agent）· tool-search 动态发现      │
│  knowledge_search ──httpx(超时降级)──┐  citation([n]→引用块) · 多模态 │
└──────────────────────────────────────┼────────────────────────────┘
                                        ▼ POST /v1/retrieve
┌──────────────────── arag 检索 (RAG, :8100) ───────────────────────┐
│  query → rewrite → 向量(numpy余弦)+全文(BM25) → RRF 融合 → 过滤    │
│  index：parse → image caption(视觉) → chunk → embed → store        │
│  存储端口：VectorStore / FullTextIndex(接流) + GraphStore(端口占位) │
└────────────────────────────────────────────────────────────────────┘
```

---

## 核心特性

| 能力 | 实现 |
|---|---|
| **两代可切换推理引擎** | Plan-Execute（先规划后执行）/ Tool-Use Agent Loop（ReAct 单循环），统一 `ReasoningEngine` 端口，`ENGINE` 配置切换 |
| **生产级循环加固** | LiteLlm 工具参数规范化 + ADK Plugin 分发前短路/异常喂回，以及上下文超长截断重试、计划续推、force-summary、消息预算 |
| **混合召回 RAG** | 向量 + BM25 双路召回 → RRF 互惠排名融合 → 低价值过滤；查询改写 |
| **知识问答 + 引用** | agent→arag 微服务调用（超时降级）；正文 `[n]` → 末尾引用块；无命中不编造 |
| **技能调用（skill-center）** | agent→skill-center 第 3 个微服务；NDJSON `SkillResultDTO` 采用数据帧 + 独立 EOF，缺 EOF/坏帧按稳定错误码处理；任意失败帧使整体失败并保留首个根因，错误码透传到 function response 和结构化日志；展示帧合并为 `skill_event` |
| **SKILL Agent-as-Tool** | 每个 SKILL 作为主 Tool-Use Agent Loop 中的标准工具；每次调用把完整技能包复制到独立沙箱，子 Agent 首先读取 `SKILL.md` 后再执行；Runtime 负责超时、调用身份、并发与取消，主 Agent 根据 `tool_result` 继续决策 |
| **A2A 远程子代理** | ADK 原生 A2A（`a2a-sdk` + `RemoteA2aAgent` 客户端 + `to_a2a` 服务端）；远程调用是无父对话历史的新会话，因此工具声明要求 request 展开指代并携带完整上下文 |
| **多模态** | 图片输入（ADK artifact + 视觉模型）；文档图片 caption 入库可检索 |
| **流式 SSE** | `text / tool_call / tool_result / plan_step / citation / skill_event / done` 事件 |
| **依赖倒置** | 存储中间件抽象成端口（local 向量持久化起步，可换 pgvector/ES/Neo4j） |
| **可观测性** | trace_id 全链路 + 结构化 JSON 日志 + `[Tag]` 埋点 |
| **结构化轨迹** | 一次请求 = 一棵 Span 树（模型每轮真实输入 / token / 工具 / 检索质量），三代引擎同构；评测据此自动归因失败模式 |

---

## 技术栈

Python 3.12 · **Google ADK 2.6.2** · LiteLlm · FastAPI · SSE · numpy · rank_bm25 · jieba ·
LLM = 阿里云 DashScope `qwen3.7-plus`（文本+视觉+function-calling）· 嵌入 = `text-embedding-v3`。

---

## 快速开始

```bash
cd sxw_agent-2_demo

# 1) 配置（填入真实 DASHSCOPE_API_KEY，切勿提交）
cp .env.example .env

# 2) 依赖（统一使用 .venv/）
#    如需重建：python3.12 -m venv .venv
#    .venv/bin/pip install -r requirements.txt

# 3) 一键启动 a2a_service(:8300)+skill-center(:8200)+arag(:8100)+agent(:8000)，自动等待健康检查并入库样本
#    样本和 Web UI 上传文档的 embedding 默认持久化到 local_storage/embedding/
bash scripts/run_all.sh
```

打开浏览器访问 `http://127.0.0.1:8000/chat-ui/` 可使用内置 Web Chat 界面，支持文本对话、图片提问，以及 `txt/md/pdf/docx` 文档入库后问答。
arag 重启后会自动加载 `local_storage/embedding/` 中的向量与 chunks，并重建 BM25；清空该目录后再重新执行样本入库。

另开一个终端：

```bash
# 知识问答（带 [n] 引用）
curl -N -X POST http://127.0.0.1:8000/api/v1/chat/demo/stream \
  -F 'query=什么是混合召回？RRF 是什么？' -F user_id=u1 -F session_id=s1

# 多步任务（计划 + 工具）
curl -N -X POST http://127.0.0.1:8000/api/v1/chat/demo/stream \
  -F 'query=用工具计算 (3+4)*5，再把结果翻译成英文' -F user_id=u1 -F session_id=s1

# 多模态（带图提问）
curl -N -X POST http://127.0.0.1:8000/api/v1/chat/demo/stream \
  -F 'query=这张图里有什么？' -F user_id=u1 -F session_id=s1 -F 'image=@/path/to/pic.jpg'

# 技能调用（skill-center，流式 skill_event：思考帧/卡片）
curl -N -X POST http://127.0.0.1:8000/api/v1/chat/demo/stream \
  -F 'query=用天气卡片技能 query_weather 查询杭州天气' -F user_id=u1 -F session_id=s1

# SKILL 沙箱执行（子代理在沙箱跑 numpy）
curl -N -X POST http://127.0.0.1:8000/api/v1/chat/demo/stream \
  -F 'query=用数据分析技能算 12,7,9,20 的均值和方差' -F user_id=u1 -F session_id=s1

# A2A 远程子代理（agent-card 发现 + JSON-RPC 委派）
curl -N -X POST http://127.0.0.1:8000/api/v1/chat/demo/stream \
  -F 'query=用A2A数学专家精确计算 23*47' -F user_id=u1 -F session_id=s1
```

切换引擎：编辑 `.env` 的 `ENGINE=plan_execute|agent_loop|native_loop`（默认 `agent_loop`）。

三代引擎的对比轴是**循环归谁驱动**：`plan_execute` 前置规划、`agent_loop` 由 ADK 流程引擎驱动循环、
`native_loop` 自己拥有那个 `while`（以 Claude Code `query.ts` 为蓝本，不依赖任何 Agent 框架）。
三者共享同一套工具面、系统指令与 SSE 契约。

> ⚠️ `native_loop` **尚未评测**：`eval/reports/` 下的数字全部来自 `agent_loop` / `plan_execute`，不可套用到它身上。

### Claude SKILL Agent-as-Tool

Claude SKILL 不使用独立的多 Skill 编排器。主 Agent 将每次 Skill Agent 调用视为标准 `tool_use → tool_result`：有依赖的 Skill 在收到上游结果后跨轮串行调用；同轮调用只适用于彼此独立的任务。同一 invocation 内默认串行执行 Claude SKILL，只有 `parallel_safe: true` 且 `exclusive_resources: []` 的 Skill 才允许并行，同时仍受进程级并发上限约束。

每次调用都会把 `agent/claude_skill/skills_data/<id>/` 完整复制到独立沙箱的 `skills/<id>/`，包括 `scripts/`、`references/` 和资源文件；子 Agent 必须先读取该目录根部的 `SKILL.md`。技能 frontmatter 示例：

```yaml
---
name: 数据分析
description: 使用 Python 执行统计分析
parallel_safe: true
exclusive_resources: []
---
```

相关 agent 配置（修改后需重启）：

| 变量 | 默认 | 说明 |
|---|---:|---|
| `SKILL_CALL_TIMEOUT_SECONDS` | `120` | 单次 Claude SKILL 调用总超时，包含排队、装载、执行与结果整理 |
| `SKILL_MAX_LLM_CALLS` | `16` | 单个 Skill Agent 的最大模型调用次数 |
| `SKILL_MAX_PARALLEL_CALLS` | `2` | 进程内 Claude SKILL 最大并发调用数 |
| `SKILL_RESULT_MAX_CHARS` | `8000` | 回灌主 Agent 的 Skill 结果最大字符数 |

当前不支持 Artifact 跨 Skill 传递、Claude SKILL HITL/暂停恢复和真实 AgentBay；`agentbay` provider 仍是不可运行的云桩。LocalSandbox 会在调用结束后删除临时目录，因此不能依赖共享目录在多个 Skill 间传文件。

---

## 接口

| 服务 | 方法 路径 | 说明 |
|---|---|---|
| agent | `POST /api/v1/chat/{agent_uuid}/stream` | SSE 对话（multipart：query / user_id / session_id / image） |
| agent | `GET /chat-ui/` | 浏览器 Web Chat 界面 |
| agent | `POST /api/v1/documents/index` | Web Chat 文档入库代理（转发到 arag `/v1/index`） |
| agent | `GET /healthz` | 存活探针 |
| arag | `POST /v1/retrieve` | 混合召回（agent 调它） |
| arag | `POST /v1/rag` | arag 独立端到端问答 |
| arag | `POST /v1/index` · `POST /v1/index/sample` | 文档入库 / 入库内置样本 |
| arag | `GET /healthz` | 存活探针 + 后端选型 |
| skill-center | `POST /api/v1/skills/runtime/list` | 技能目录（tools[]+inputSchema，按 snapshotTag） |
| skill-center | `POST /api/v1/skills/runtime/execute` · `/execute-streaming` | 同步执行 / NDJSON 流式执行（SkillResultDTO） |
| skill-center | `POST /api/v1/a2a-agents/instance/list` | A2A 子代理注册表（含 agent-card 地址） |
| skill-center | `GET /healthz` | 存活探针 + 托管技能 |
| a2a_service | `GET /.well-known/agent-card.json` · JSON-RPC | A2A agent-card 发现 + message/send·stream（ADK `to_a2a`） |

---

## 项目结构

```
agent/   ADK Agent 运行时
  engine/{base, plan_execute/*, agent_loop/*, native_loop/*}
                                                三代引擎（native_loop 为自研循环）
  engine/loop_tools/                            两代 loop 引擎共享的工具面与系统指令
  plugins/agent_invocation_plugin.py            循环加固插件
  llm/{hardened_litellm, chat, exceptions}      模型适配 + 加固
  tools/ citation/ stream/ session/ artifacts/  工具/引用/流/会话/多模态
  skills/{client,stream_processor,selected_skill_tool,result_parser,catalog,ui_event_queue,stream_merge,request_context,args_coercion}
                                                技能调用链路（→ skill-center）
  claude_skill/{sandbox/{base,local_sandbox,agentbay_sandbox,factory},toolset,skill_runner,claude_skill_tool,catalog,skills_data/}
                                                SKILL 沙箱执行（provider 抽象 + 沙箱子代理）
  a2a/loader.py                                 A2A 远程子代理发现（RemoteA2aAgent）
arag/    混合召回 RAG 服务
  components/{embedding,llm,chunker,rewrite,retriever,reranker,filter,generator}
  processor/{document,image}                    解析 + 图片多模态
  store/{vector_store,fulltext_index,graph_store,factory}   存储端口
skillcenter/   技能中心（MCP 风格执行网关 + A2A 注册表）：{skills,api,a2a_api,main,config}
a2a_service/   A2A 运行时（ADK to_a2a 暴露 math_expert 子代理）：{agents,main,config}
common/{obs.py, trace.py, skill_contract.py}   日志/trace_id · 结构化轨迹 · 技能线协议契约
eval/            真实 LLM 黑盒评测、数据集、评分器与历史报告
web/             内置浏览器 Chat UI（静态 HTML/JS/CSS）
```

---

## 面试脉络（talking points）

1. **两代推理引擎的演进与取舍**：Plan-Execute（前置规划、可控可解释）vs Tool-Use Agent Loop（动态 ReAct、强适应），统一端口、配置切换。
2. **「用 ADK」不止调 Runner**：生产加固落在 ADK 的 **Plugin**（工具参数分发前短路、续推/预算/force-summary、异常喂回）与 **LiteLlm 子类/适配层**（非对象参数规范化、超长截断重试、异常分类、缓存断点 provider-aware）。
3. **RAG 是混合召回 + RRF 融合的工程化**，不是单路 `similarity_search`；查询改写、低价值过滤、图片多模态入库。
4. **依赖倒置**让存储中间件可演进（本地→pgvector/ES/Neo4j 零改业务）。
5. **全链路流式与可观测**：SSE 增量、计划步骤/工具调用事件、trace_id 贯穿、结构化日志埋点；微服务边界含超时与降级。
6. **可观测性要能驱动迭代**：日志说"发生了什么"，**结构化轨迹**（`common/trace.py`）说"为什么"——记录每轮模型真正看到的输入、token、工具与检索质量，让评测的 FAIL 直接接到证据上、按规则自动归因。三代引擎的 span 树刻意做成同构，避免"谁埋点更深"变成引擎对比里的假优势。
7. **Agent-as-Tool 而非额外编排器**：一个 Skill Agent 执行一个完整 SKILL 包；多 Skill 串并行仍由主 Tool-Use Agent Loop 动态决定，Runtime 只治理身份、并发、超时、取消和沙箱隔离。

> ⚠️ 诚实声明：PromptCache 的显式缓存断点为 Anthropic 专属，本 demo 默认 provider（DashScope/Qwen）下为 no-op。

---

## 范围与边界

本 demo 是从生产系统**抽取并复刻的核心 Agent Runtime + RAG 主链路**，用于展示生产级架构原语与关键工程取舍；**有意识裁剪**了与"AI Agent 开发"非核心的线上治理逻辑：

- 内部基建：Nacos 灰度 / A2A 信封 / HSF 用户体系 / SLS·langfuse·rocketmq / MaaS 内部网关 / 计费·限流·租户权限
- 复杂治理：老路径 `ReferenceRewriter`、生产版 CitationInjector 完整 feed/flush 状态机、多语言标题白名单、历史引用清洗、图片 `__IMG__` placeholder 流式替换
- 入口形态：匿名游客问答、群聊 @ / 群感知、deviceType/dialogType 等 A2A 上下文字段
- Claude SKILL 高阶能力：Artifact 跨 Skill 传递、HITL/暂停恢复和真实 AgentBay 执行尚未实现

准确定性是「**保留主链路形状 + 关键工程取舍**」，而非「完整保留所有生产逻辑」。判断是否新增或保留一项能力时，以它能否帮助理解 Agent Runtime、RAG、扩展机制、可靠性或评测为主要标准。

> **工具失败模型**（面试可展开）：业务可预期失败由工具返回结构化错误；模型生成非对象参数时由 LiteLlm 适配层转换为 sentinel、Plugin 在分发前短路；框架级异常由 `on_tool_error_callback` 封装成 `function_response`。三类失败都反馈给模型续推，不直接中断 turn。
