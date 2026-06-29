# sxw_optimization_demo — 生产级 Agent 运行时（规划/推理引擎）+ 混合召回 RAG

基于 **Google ADK** 精简复刻的一套**可独立运行的生产级 AI Agent 系统**，凸显「**Agent 运行时规划与推理引擎**」。
双服务（Agent 运行时 + RAG 检索）经 HTTP 协作，覆盖：提问 → 意图识别 → 规划/执行 → 技能调用 → 结果返回、
多模态图片/文件、流式 SSE/长连接、混合召回 RAG、可观测性。

> 设计依据与里程碑见 [`roadmap/`](roadmap/README.md)。

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
│    └─ Gen2 Agent-Loop：ReAct 单循环 + 插件加固                     │
│         · ToolErrorFeedback（异常→function_response，不中断）       │
│         · ContextOverflow（超长截断重试）· PromptCache(provider-aware)│
│         · TaskPlan 续推 · force-summary · message budget            │
│         · sub-agent 委派 · tool-search 动态发现                     │
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
| **两代可切换推理引擎** | Plan-Execute（先规划后执行）/ Agent-Loop（ReAct 单循环），统一 `ReasoningEngine` 端口，`ENGINE` 配置切换 |
| **生产级循环加固** | ADK Plugin：工具异常喂回、上下文超长截断重试、计划续推、force-summary、消息预算 |
| **混合召回 RAG** | 向量 + BM25 双路召回 → RRF 互惠排名融合 → 低价值过滤；查询改写 |
| **知识问答 + 引用** | agent→arag 微服务调用（超时降级）；正文 `[n]` → 末尾引用块；无命中不编造 |
| **技能调用（skill-center）** | agent→skill-center 第 3 个微服务；启动拉技能目录(快照)→技能包装成工具；NDJSON `SkillResultDTO` 流（思考/卡片/增量/算粒错误）经 UI 队列合并为 `skill_event` 实时流出 |
| **SKILL 沙箱执行（claude-skill）** | 技能包(SKILL.md)作为**子代理在沙箱中执行**；沙箱 provider 抽象（LocalSandbox 可跑 / AgentBay 云桩）+ file/shell/code 服务；两契约（output→LLM / 事件→UI） |
| **A2A 远程子代理** | ADK 原生 A2A（`a2a-sdk` + `RemoteA2aAgent` 客户端 + `to_a2a` 服务端）；`a2a_service`(:8300) 暴露 agent-card + JSON-RPC，skill-center 作注册表，agent 经 agent-card 发现 + 远程委派 |
| **多模态** | 图片输入（ADK artifact + 视觉模型）；文档图片 caption 入库可检索 |
| **流式 SSE** | `text / tool_call / tool_result / plan_step / citation / skill_event / done` 事件 |
| **依赖倒置** | 存储中间件抽象成端口（本地实现起步，可换 pgvector/ES/Neo4j） |
| **可观测性** | trace_id 全链路 + 结构化 JSON 日志 + `[Tag]` 埋点 |

---

## 技术栈

Python 3.12 · **Google ADK 2.3** · LiteLlm · FastAPI · SSE · numpy · rank_bm25 · jieba ·
LLM = 阿里云 DashScope `qwen3.7-plus`（文本+视觉+function-calling）· 嵌入 = `text-embedding-v3`。

---

## 快速开始

```bash
cd sxw_optimization_demo

# 1) 配置（填入真实 DASHSCOPE_API_KEY，切勿提交）
cp .env.example .env

# 2) 依赖（已安装在 env_sxw_demo/）；如需重建：
#    python3.12 -m venv env_sxw_demo
#    env_sxw_demo/bin/pip install -r requirements.txt

# 3) 一键启动 a2a_service(:8300)+skill-center(:8200)+arag(:8100)+agent(:8000)，自动等待健康检查并入库样本
bash scripts/run_all.sh
```

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

# SKILL 沙箱执行（claude-skill：子代理在沙箱跑 numpy）
curl -N -X POST http://127.0.0.1:8000/api/v1/chat/demo/stream \
  -F 'query=用数据分析技能算 12,7,9,20 的均值和方差' -F user_id=u1 -F session_id=s1

# A2A 远程子代理（agent-card 发现 + JSON-RPC 委派）
curl -N -X POST http://127.0.0.1:8000/api/v1/chat/demo/stream \
  -F 'query=用A2A数学专家精确计算 23*47' -F user_id=u1 -F session_id=s1
```

切换引擎：编辑 `.env` 的 `ENGINE=plan_execute|agent_loop`（默认 `agent_loop`）。

---

## 接口

| 服务 | 方法 路径 | 说明 |
|---|---|---|
| agent | `POST /api/v1/chat/{agent_uuid}/stream` | SSE 对话（multipart：query / user_id / session_id / image） |
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
  engine/{base, plan_execute/*, agent_loop/*}   两代引擎
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
common/{obs.py, skill_contract.py}    通用可观测性 + 技能线协议契约
roadmap/         设计依据与实施里程碑
```

---

## 面试脉络（talking points）

1. **两代推理引擎的演进与取舍**：Plan-Execute（前置规划、可控可解释）vs Agent-Loop（动态 ReAct、强适应），统一端口、配置切换。
2. **「用 ADK」不止调 Runner**：生产加固落在 ADK 的 **Plugin**（`before_model_callback` 续推/预算/force-summary、`on_tool_error_callback` 异常喂回）与 **LiteLlm 子类**（超长截断重试 / 异常分类 / 缓存断点 provider-aware）。
3. **RAG 是混合召回 + RRF 融合的工程化**，不是单路 `similarity_search`；查询改写、低价值过滤、图片多模态入库。
4. **依赖倒置**让存储中间件可演进（本地→pgvector/ES/Neo4j 零改业务）。
5. **全链路流式与可观测**：SSE 增量、计划步骤/工具调用事件、trace_id 贯穿、结构化日志埋点；微服务边界含超时与降级。

> ⚠️ 诚实声明：PromptCache 的显式缓存断点为 Anthropic 专属，本 demo 默认 provider（DashScope/Qwen）下为 no-op（见 `roadmap/05`）。

---

## 范围与边界

本 demo 是从生产系统**抽取并复刻的核心 Agent Runtime + RAG 主链路**，用于展示生产级架构原语与关键工程取舍；**有意识裁剪**了与"AI Agent 开发"非核心的线上治理逻辑：

- 内部基建：Nacos 灰度 / A2A 信封 / HSF 用户体系 / SLS·langfuse·rocketmq / MaaS 内部网关 / 计费·限流·租户权限
- 复杂治理：老路径 `ReferenceRewriter`、生产版 CitationInjector 完整 feed/flush 状态机、多语言标题白名单、历史引用清洗、图片 `__IMG__` placeholder 流式替换
- 入口形态：匿名游客问答、群聊 @ / 群感知、deviceType/dialogType 等 A2A 上下文字段

准确定性是「**保留主链路形状 + 关键工程取舍**」，而非「完整保留所有生产逻辑」。被裁剪项的对应关系见 `roadmap/01-architecture.md` 映射表与 `roadmap/05`。

> **两类工具失败模型**（面试可展开）：业务可预期失败由工具返回结构化错误（如 `calculator` / `knowledge_search` 降级）；框架级异常由 Plugin `on_tool_error_callback` 封装成 `function_response` 喂回模型、不中断 turn（可用 `simulate_unstable_operation(should_fail=true)` 现场演示）。
