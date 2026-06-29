# 04 · 实施里程碑（M0–M6）

> 原则：**每个里程碑结束都能 `python -m py_compile` 全量通过**；
> 关键里程碑用真实 LLM（qwen3.7-plus）做一次手动 E2E 冒烟；**阶段一不写单测**。

图例：交付物 = 要产出的文件/能力；验收 = 通过标准。

---

## M0 · 基座（双服务骨架 + 配置 + LLM + 可观测性 + 存储端口）  ✅ 已完成

> **实施记录**：venv `env_sxw_demo`(3.12.10) + google-adk 2.3.0；`py_compile` 全绿；导入冒烟 OK；
> E2E LLM 冒烟通过（ADK `InMemoryRunner`+`HardenedLiteLlm`→DashScope `qwen3.7-plus` 返回 "pong"）；
> `arag:8100` / `agent:8000` 均 boot 且 `/healthz`=200。
> 文件：`pyproject.toml` `.env.example` `.gitignore` `scripts/run_all.sh` `common/obs.py`
> `agent/{config,main}.py` `agent/llm/hardened_litellm.py` `arag/{config,main}.py` `arag/store/{base,vector_store,fulltext_index,graph_store,factory}.py`。
> 备注：观测性提取到顶层 `common/`（两服务共享，较 roadmap 原 `agent/observability` 更 DRY）。
> 发现：`qwen3.7-plus` 是推理模型会输出 thinking 文本 → M2 在 event_converters 分离 reasoning_content 与正文。

**目标**：把"地基"打好——两个 FastAPI 服务能起来、配置可读、LLM 可调、日志带 trace、存储端口可注入。

**交付物**
- `pyproject.toml` / `.env.example`（占位 key）/ `scripts/run_all.sh`
- `agent/config.py`、`arag/config.py`（pydantic-settings）
- `agent/llm/hardened_litellm.py`（先实现基础 LiteLlm 接通，加固原语留 M3 填充骨架）
- `agent/observability/{trace_middleware.py,logging.py}`（结构化 JSON + `[Tag]` + trace_id）
- `arag/store/{base.py,vector_store.py,fulltext_index.py,graph_store.py,factory.py}`
  - `VectorStore` ABC + `LocalVectorStore`（numpy 余弦 + 本地持久化）
  - `FullTextIndex` ABC + `LocalBM25Index`（rank_bm25 + jieba）
  - `GraphStore` ABC + `LocalGraphStore`（内存邻接表，**仅端口、方法占位**）
  - `factory.py`：按 `*_BACKEND` env 返回实现
- `agent/main.py` / `arag/main.py`（健康检查 `/healthz`）

**验收**
- `run_all.sh` 同时起 8000/8100，`/healthz` 均 200
- 一段脚本：`build_llm()` 调 qwen3.7-plus 返回文本（E2E LLM 冒烟）
- `py_compile` 全绿

---

## M1 · arag 检索服务（index 流水线 + 混合召回）  ✅ 已完成

> **实施记录**：`py_compile` 全绿；E2E 通过——`/v1/index/sample` 入库 3 chunks →
> `/v1/retrieve` 命中（向量+BM25 双路 RRF 融合，`source=fused`）→ `/v1/rag` 端到端答案带 `[1]` 引用。
> **延迟优化**：查询改写/生成默认 `enable_thinking=false`，检索 9718ms→1529ms（~6x）。
> 文件：`arag/schemas.py` `arag/context.py` `arag/sample_data.py`
> `arag/components/{embedding,llm,chunker,rewrite,filter,reranker,retriever,generator}.py`
> `arag/processor/document.py` `arag/api/{index,retrieve}.py`。
> 偏差说明：新增 `arag/components/llm.py`(ChatClient) 与 `arag/context.py`(DI 容器)，roadmap 未列但属合理装配；
> rerank 采用 **RRF 互惠排名融合**（无需训练模型，hybrid 融合的工业标准做法）。

**目标**：RAG 能"存进去、查出来"，混合召回 + rerank 真实可用。

**交付物**
- `arag/components/chunker.py`（token / markdown 切分）
- `arag/components/embedding.py`（DashScope `text-embedding-v3` 直连）
- `arag/processor/document.py`（markdown/text 解析 → chunk）
- `arag/components/rewrite.py`（查询改写：LLM 产 1~N 个改写 query）
- `arag/components/retriever.py`（**混合召回**：VectorStore 向量 + FullTextIndex BM25 → 合并）
- `arag/components/filter.py`（score 阈值 + low-value 过滤）
- `arag/components/reranker.py`（重排：先用规则/LLM 打分；接口预留 rerank 模型）
- `arag/components/generator.py`（带 citation 的答案生成，可选）
- `arag/api/index.py`：`POST /v1/index`（入库）
- `arag/api/retrieve.py`：`POST /v1/retrieve`（返回 chunks：doc_id/score/content/图片占位）
- 内置一份小样本知识库（含 1~2 张图）做演示

**验收**
- `POST /v1/index` 入库样本 → `POST /v1/retrieve` 命中相关 chunk，vector 与 bm25 两路都有贡献
- `GraphStore` 不参与（仅注册）
- `py_compile` 全绿

---

## M2 · agent 运行时 + Gen1 Plan-Execute 引擎  ✅ 已完成

> **实施记录**：`py_compile` 全绿；E2E 通过——
> Turn1「计算 (3+4)*5」→ plan(1 步) → `calculator`→35 → 流式答案；
> Turn2（同 session）「结果再×2」→ plan(3 步) → `calculator(35*2)`→70 → 多轮记忆正确。
> SSE 事件 `plan_step / tool_call / tool_result / text / done` 全部就绪；ADK Runner `StreamingMode.SSE` 流式增量。
> 文件：`agent/engine/{base.py,plan_execute/*}` `agent/context.py` `agent/api/chat.py`
> `agent/stream/event_converters.py` `agent/session/session_service.py` `agent/llm/chat.py` `agent/tools/builtin_tools.py`。
> 决策：规划相用轻量 `AgentChatClient`（openai 直连，thinking 关）；执行相用 ADK Runner+LlmAgent+tools（HardenedLiteLlm 经 extra_body 关 thinking，解决 M0 发现的 reasoning 污染）。
> 偏差：`agent/context.py`(DI) 为合理新增；artifact_service 已就绪待 M5 接图片。

**目标**：跑通"提问→规划→执行→流式返回"，先用最早的 Plan-Execute 范式。

**交付物**
- `agent/api/chat.py`：`POST /api/v1/chat/{uuid}/stream`（SSE）
- `agent/session/session_service.py`（ADK `InMemorySessionService` 封装）
- `agent/stream/event_converters.py`（ADK Event → SSE 协议）
- `agent/engine/base.py`（`ReasoningEngine` 端口 + `build_engine` 工厂）
- `agent/engine/plan_execute/*`（decision_planner / execution_planner / engine）
- `agent/tools/builtin_tools.py`（计算器/天气，演示通用工具调用）

**验收**
- `ENGINE=plan_execute`：发一条会触发工具的提问 → SSE 增量返回 text/tool_call/plan_step/done
- 多轮会话历史生效（session 保持）
- `py_compile` 全绿

---

## M3 · Gen2 Agent-Loop 引擎 + 进阶生产原语 ★  ✅ 已完成

> **实施记录**：`py_compile` 全绿；E2E 通过——
> 多步任务「算 12*12 再翻译」→ `update_task_plan`(plan_step 流 running/done) → `calculator`→144 →
> `tool_search('翻译')` 发现 `translate` → `translate` → 计划完成 → 干净答案；
> MAX_LOOP_ITERS=2 → `[LoopControl] force summary` 注入 → 模型立即收尾（无失控）。
> ToolErrorFeedback 插件直接验证返回喂回结构；ContextOverflow 异常分类验证；PromptCache 对 Qwen no-op（日志诚实标注）。
> **架构决策（重要）**：循环加固落在 ADK **Plugin** 扩展点（`before_model_callback`=续推/预算/force-summary，
> `on_tool_error_callback`=ToolErrorFeedback），而非自研 `BaseLlmRequestProcessor`——因 2.x 公版 API 下 Plugin 更稳更地道；
> 循环本体复用 ADK Runner 原生工具循环。已在 `03-engine-deep-dive.md`/`05` 对此偏差留痕。
> 文件：`agent/engine/agent_loop/{agent_loop_engine,loop_processor,message_budget,task_plan_tool,plan_event_detector,sub_agent_tool,tool_search_tool}.py`
> `agent/plugins/agent_invocation_plugin.py` `agent/llm/{hardened_litellm(覆写),exceptions}.py`。

**目标**：核心里程碑——落地 ReAct 单循环引擎与全部进阶原语，凸显"生产级推理引擎"。

**交付物**
- `agent/engine/agent_loop/loop_processor.py`（`BaseLlmRequestProcessor`：续推/force-summary/max_iter/message-budget）
- `agent/engine/agent_loop/task_plan_tool.py` + `plan_event_detector.py`
- `agent/engine/agent_loop/agent_loop_engine.py`
- `agent/plugins/agent_invocation_plugin.py`（`BasePlugin`：on_tool_error 喂回 + before_model 续推）
- `agent/llm/hardened_litellm.py` 补全：ContextOverflow 截断重试 + 异常分类 + PromptCache（provider-aware，Qwen 上 no-op）
- 进阶：`agent/engine/agent_loop/sub_agent_tool.py`（ADK `AgentTool` 子代理）
- 进阶：`agent/engine/agent_loop/tool_search_tool.py`（deferred tools 动态发现）

**验收**
- `ENGINE=agent_loop`：多轮工具调用直至产出最终答案；plan_step 事件随计划推进
- 故意让某工具抛错 → 观察 `function_response` 喂回、turn 不中断、模型重试/换路（看日志 `[ToolErrorFeedback]`）
- 构造超长上下文 → 触发 `[ContextOverflow]` 截断重试
- `[PromptCache] skipped: provider not supported`（Qwen 下 no-op，诚实降级）
- sub-agent / tool-search 各一次成功演示
- `py_compile` 全绿

---

## M4 · 打通 knowledge_search + citation（招牌特性）  ✅ 已完成

> **实施记录**：`py_compile` 全绿；双服务 E2E 通过——
> 在线：`knowledge_search(混合召回 RRF)`→arag 命中 3 条→答案带 `[1]`→`citation` 事件 `refs=[(1,'kb-rag-pipeline')]`；
> 宕机：kill arag→`knowledge_search` count=0 降级→模型 chat-mode 据常识回答→**无 citation**（guard 不编造）→无报错，日志 `[QaRetrieve] degraded`。
> 文件：`agent/tools/knowledge_search.py`（httpx→arag，超时降级）、`agent/citation/citation_injector.py`（CitationInjector + with_citations 包裹两代引擎）；
> 接线：`context.py` 加 knowledge_search 工具、`api/chat.py` 包 with_citations、两引擎指令加 [n] 引用规则。

**目标**：agent 经 HTTP 调 arag 完成知识问答，并流式注入引用。

**交付物**
- `agent/tools/knowledge_search.py`（httpx 调 arag `/v1/retrieve`，**超时→降级 chat-mode**）
- `agent/citation/citation_injector.py`（流式 `[n]` → 末尾 `citation` 事件；ID 协议精简版）
- 引擎接入：检索结果作为上下文喂给生成；guard：无检索结果不编造引用

**验收**
- 问知识库内问题 → 命中 → 答案带 `[n]` + 末尾引用列表（`citation` 事件）
- arag 故意宕机 → 检索超时 → 引擎降级纯对话、不报错（看 `[QaRetrieve]`/降级日志）
- `py_compile` 全绿

---

## M5 · 多模态（artifact 图片输入 + arag image 处理）  ✅ 已完成

> **实施记录**：`py_compile` 全绿；E2E 通过——
> agent：上传 dog.jpeg(496KB)→`Part.from_bytes` 入 message + 存 artifact→模型答"金毛寻回犬和年轻女子"（ADK LiteLlm 自动转 base64 image_url）；
> arag：`/v1/index/sample` 触发 `ImageProcessor.enrich`→`[ImageCaption] captioned kb-agent-loop`→检索"狗和女孩"命中含"图片描述"的 chunk。
> 文件：`agent/artifacts/artifact_service.py`、`arag/processor/image.py`、`arag/components/llm.py`(vision)；接线 `agent/api/chat.py`、`arag/{context,api/index}.py`。

**目标**：端到端多模态——用户传图提问，arag 文档图片入库可被检索/描述。

**交付物**
- `agent/api/chat.py` 支持 multipart（文本 + 图片）；`agent/artifacts/artifact_service.py`（ADK `InMemoryArtifactService`）
- 图片以 OpenAI 兼容 content-array（`image_url`）喂给 qwen3.7-plus（已验证视觉可用）
- `arag/processor/image.py`：文档内图片 → qwen3.7-plus 生成 caption 入库（可检索）

**验收**
- 传一张图 + 文本提问 → 模型基于图作答（视觉链路通）
- 入库含图文档 → 检索能命中图片 caption
- `py_compile` 全绿

---

## M6 · 收口（文档 + 一键启动 + 全量校验）  ✅ 已完成

> **实施记录**：项目主 `README.md`（架构图/快速开始/接口/面试脉络）；
> `scripts/run_all.sh` 增强为「健康等待 + 自动入库样本」；
> 全量 `py_compile` 66 文件/2302 行全绿；
> turnkey E2E：`run_all.sh` → 自动 ready+seed → 单请求「知识+图片」(agent_loop)
> 返回 5 类 SSE 事件 + citation([1]→kb-rag-pipeline) + 视觉识别；真实 key 仅环境变量、未落盘。
> plan_execute 引擎已在 M2 验证（计算+多轮）；两代引擎经 `ENGINE` 配置切换。

**交付物**
- `sxw_optimization_demo/README.md`（架构图、快速开始、引擎切换、面试脉络）
- `scripts/run_all.sh` 完整（含等待健康检查）
- `.gitignore`（含 `.env`）

**验收**
- 全新环境：填 `.env` → `run_all.sh` → 浏览器/`curl` 跑通一次带图知识问答（两种 ENGINE 各一次）
- `find sxw_optimization_demo -name '*.py' | xargs python -m py_compile` 全绿

---

## 依赖关系（关键路径）

```
M0 ──► M1 ──────────────► M4 ──► M5 ──► M6
  └──► M2 ──► M3 ─────────┘
```
- M1（arag）与 M2（agent+Gen1）可并行；M3 依赖 M2；M4 依赖 M1+M3；M5 依赖 M4。
