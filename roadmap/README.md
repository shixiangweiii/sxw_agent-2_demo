# sxw_optimization_demo — 实施 Roadmap（总览与导航）

> 基于生产级 AI Agent 运行时 **albert-agent-2**（钉钉智能体运行时）+ 其依赖的生产级 RAG 服务 **lippi-arag**，
> **精简复刻**出的一个可独立运行、可面试讲透的 demo。
> **核心目标：凸显「生产级的 Agent 运行时规划与推理引擎」。**

---

## 1. 一句话架构

> **双服务**（`agent` 运行时 + `arag` 检索服务）+ **HTTP 微服务边界**；
> `agent` 内置**两代 ADK 规划/推理引擎**（Plan-Execute / Agent-Loop）可配置切换；
> `arag` 是**混合召回 RAG**，存储走**端口-适配器**（本地实现起步、可换 pgvector/ES/Neo4j）。

```
 用户(带图提问)
     │  HTTP + SSE(text/event-stream)
     ▼
┌─────────────────────────────── agent 运行时 (ADK, :8000) ───────────────────────────────┐
│  SSE 入口 → ReasoningEngine(可切换)                                                       │
│     ├─ Gen1 Plan-Execute：decision planner → execution planner → summary                 │
│     └─ Gen2 Agent-Loop：ADK Runner 原生循环 + Plugin 续推加固（ReAct 单循环）            │
│  生产加固：ToolErrorFeedback(Plugin) / ContextOverflow+PromptCache(LiteLlm 子类) /        │
│            TaskPlan 续推 / sub-agent / tool-search / message-budget                       │
│  下游：knowledge_search 工具 ──httpx──┐    citation 注入 / 多模态 artifact / 会话保持      │
└──────────────────────────────────────┼──────────────────────────────────────────────────┘
                                        ▼  POST /v1/retrieve
┌─────────────────────────── arag 检索服务 (RAG, :8100) ───────────────────────────────────┐
│  query → rewrite → 混合召回(VectorStore 向量 + FullTextIndex BM25) → filter → rerank →    │
│  generate(带 citation)     |  index：parse → chunk → embed → store  |  image 多模态处理    │
│  存储端口：VectorStore / FullTextIndex(接流)  +  GraphStore(仅端口、不接流、TODO Neo4j)    │
└───────────────────────────────────────────────────────────────────────────────────────────┘
```

---

## 2. 指导原则（不可动摇）

| # | 原则 | 含义 |
|---|---|---|
| P1 | **引擎是主角** | 一切以「生产级运行时规划与推理引擎」为中心，两代引擎都要复刻到位 |
| P2 | **生产级而非玩具** | 保留真实的健壮性原语（异常喂回 / 超长截断重试 / 计划续推 / 混合召回 / rerank），不是 hello-world |
| P3 | **端到端可跑** | 真实 LLM（DashScope qwen3.7-plus）+ 真实嵌入 + 真实检索；配 key 即可 run |
| P4 | **技术栈对齐原项目** | Python + Google ADK + FastAPI + SSE + LiteLlm（agent）；FastAPI + 混合检索组件（arag） |
| P5 | **砍内部基建** | Nacos 灰度 / MaaS 网关 / HSF / SLS / langfuse / rocketmq / A2A / AgentOSFlow 全砍 |
| P6 | **依赖倒置** | 中间件（向量库/全文索引/图库）抽象成端口，本地实现起步，零改业务换正式后端 |
| P7 | **阶段一不写单测** | 仅保证 `python -m py_compile` 全量通过；E2E 用真实 LLM 手动验证 |

---

## 3. Roadmap 文档导航

| 文档 | 内容 |
|---|---|
| [`01-architecture.md`](01-architecture.md) | 系统架构、双服务边界、两代引擎、存储端口、端到端时序、**与原项目代码映射** |
| [`02-tech-stack-and-config.md`](02-tech-stack-and-config.md) | 技术栈、ADK+LiteLlm+DashScope 配置（占位 key）、env 变量清单、`.env.example` 规范 |
| [`03-engine-deep-dive.md`](03-engine-deep-dive.md) | **核心**：两代引擎 + 生产级原语分层落点 + 原始代码引用 |
| [`04-implementation-milestones.md`](04-implementation-milestones.md) | 实施里程碑 M0–M6（目标 / 交付物 / 验收标准） |
| [`05-second-reflection-risks.md`](05-second-reflection-risks.md) | **二次反思**：能力验证结论 + 风险/取舍/开放项 |
| [`06-skill-center-link.md`](06-skill-center-link.md) | **agent→skill-center 技能调用链路**：第 3 个服务、NDJSON SkillResultDTO 流、技能→工具、里程碑 S0–S3 |
| [`07-skill-sandbox.md`](07-skill-sandbox.md) | **SKILL：claude-skill 沙箱执行**：沙箱 provider 抽象（local/AgentBay）+ 沙箱子代理 + 两契约 |
| [`08-a2a.md`](08-a2a.md) | **A2A：agent-card 远程子代理**：a2a-sdk + ADK RemoteA2aAgent/to_a2a + 第 4 个服务 |

---

## 4. 当前状态

- [x] 需求澄清 & 范围锁定（双服务 / 两代引擎 / lippi-arag RAG / 存储端口 / 砍内部基建）
- [x] LLM 能力验证：chat / vision / embedding / function-calling **全部通过**（见 `05`）
- [x] 环境就绪：独立 venv `env_sxw_demo`（Python 3.12.10）+ google-adk **2.3.0**
- [x] **M0** 基座 ✅（双服务骨架 + 配置 + hardened LiteLlm + 可观测性 + 存储端口）— 编译/导入/E2E LLM/双服务 healthz 均通过
- [x] **M1** arag：index 流水线 + 混合召回 retrieve ✅（向量+BM25 双路 RRF 融合；查询改写；/v1/{index,index/sample,retrieve,rag}；检索 ~1.5s）
- [x] **M2** agent：ADK Runner + SSE 入口 + event_converters + **Gen1 Plan-Execute 引擎** ✅（plan_step/tool_call/tool_result/text/done 流式；多轮会话记忆已验证）
- [x] **M3** **Gen2 Agent-Loop 引擎** + 进阶生产原语 ✅（ReAct循环/TaskPlan/plan_step流/ToolErrorFeedback/ContextOverflow/PromptCache(no-op)/force-summary/message-budget/sub-agent/tool-search 全验证）
- [x] **M4** 打通 knowledge_search + citation ✅（agent─httpx→arag；[n]→citation 事件；无命中不编造；arag 宕机降级 chat-mode 不报错）
- [x] **M5** 多模态（artifact 图片输入 + arag image 处理）✅（上传图→视觉作答；文档图→caption 入库可检索）
- [x] **M6** README + 一键启动 + 全量 `py_compile` 校验 ✅（项目 README；run_all 自动健康等待+入库；66 文件/2302 行编译全绿；turnkey E2E 通过）

> 🎉 **M0–M6 全部完成**，系统端到端可跑（两代引擎 / 混合召回 RAG / 多模态 / SSE / citation / 可观测）。
> ✅ **已纳入代码评审修复**（R10×5：硬熔断 / citation 跨 chunk / trace 透传 / 文档口径 / ToolErrorFeedback 演示工具，见 `05` §6）。

**扩展：agent → skill-center 技能调用链路（见 `06`）** 🎉 S0–S3 全部完成
- [x] **S0** skill-center 服务骨架 + DTO + /list 目录 + /execute 同步 + 2 演示技能 ✅
- [x] **S1** /execute-streaming（NDJSON SkillResultDTO 流）+ 流式技能（思考/卡片）+ 注入式算粒错误 ✅
- [x] **S2** agent 侧链路：SkillCenterClient + SSEStreamProcessor + 目录加载 → SelectedSkillTool 注入工具集 ✅
- [x] **S3** 流式集成：ui_event_queue + 引擎合并 → skill_event SSE；两代引擎贯通 + E2E ✅

**扩展：SKILL 沙箱执行（见 `07`）+ A2A 远程子代理（见 `08`）** 🎉 K0–K2 全部完成
- [x] **K0** a2a-sdk 依赖闸门：对齐 adk 2.3.0（`>=0.3.4,<0.4`，装 0.3.26）→ RemoteA2aAgent/to_a2a 可用 ✅
- [x] **K1** SKILL：沙箱 provider 抽象(local+AgentBay 桩) + 沙箱子代理 + ClaudeSkillTool + 数据分析技能；E2E（沙箱跑 numpy）✅
- [x] **K2** A2A：a2a_service(to_a2a math_expert) + skill-center 注册表 + RemoteA2aAgent 委派；E2E（远程算 23*47=1081）✅

---

## 5. 面试叙事（talking points）

1. **「我做了一个有两代可切换推理引擎的 Agent 运行时」**：Plan-Execute（先规划再执行）vs Agent-Loop（ReAct 单循环），统一 `ReasoningEngine` 端口、配置切换——能讲清两种范式的取舍与演进。
2. **「生产级不是调一下 Runner」**：在 ADK 扩展点做加固——`BasePlugin` 两类回调（`on_tool_error_callback` 工具异常喂回 `function_response` 不中断 turn；`before_model_callback` 计划续推 / force-summary / 消息预算，**原生产用 `BaseLlmRequestProcessor`，demo 因公版 ADK 2.3 改用 Plugin 同等语义**）、`LiteLlm` 子类（上下文超长截断重试 + 异常分类 + PromptCache provider-aware）、`RunConfig.max_llm_calls` 框架级硬熔断。
3. **「RAG 是混合召回 + rerank 的工程化」**：向量 + BM25 双路召回、低价值过滤、重排、查询改写、多模态图片入库——不是只会 `similarity_search`。
4. **「依赖倒置让中间件可演进」**：存储端口 + 工厂 + 配置选型，本地起步、生产换 pgvector/ES/Neo4j 零改业务。
5. **「全链路流式与可观测」**：SSE 长连接、计划步骤/工具调用流式事件、trace_id 贯穿、结构化日志埋点。
