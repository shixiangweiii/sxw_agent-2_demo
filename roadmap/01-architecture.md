# 01 · 系统架构

## 1. 目录结构（目标形态）

```
sxw_optimization_demo/
├── README.md                      # 项目说明 + 一键启动 + 面试脉络
├── .env.example                   # 环境变量样例（占位 key，不含真实凭据）
├── pyproject.toml                 # 依赖（google-adk / fastapi / litellm / ...）
├── scripts/
│   └── run_all.sh                 # 同时拉起 agent(:8000) + arag(:8100)
│
├── agent/                         # ★ ADK Agent 运行时（引擎中心）
│   ├── main.py                    # FastAPI app + 中间件装配 + uvicorn 启动
│   ├── api/
│   │   └── chat.py                # POST /api/v1/chat/{agent_uuid}/stream → SSE
│   ├── engine/                    # ★★ 生产级规划与推理引擎（demo 主角）
│   │   ├── base.py                #   ReasoningEngine 抽象端口 + 工厂(ENGINE 选型)
│   │   ├── plan_execute/          #   Gen1：Plan-Execute
│   │   │   ├── plan_execute_engine.py
│   │   │   ├── decision_planner.py    # 产计划（意图识别 + 步骤规划）
│   │   │   └── execution_planner.py   # 逐步执行 + summary
│   │   └── agent_loop/            #   Gen2：Agent-Loop (ReAct)
│   │       ├── agent_loop_engine.py
│   │       ├── loop_processor.py      # LoopController（经 Plugin.before_model）：续推/force-summary/预算
│   │       ├── task_plan_tool.py      # 计划即工具（循环终止条件）
│   │       ├── plan_event_detector.py # 计划步骤 → SSE 事件
│   │       ├── sub_agent_tool.py      # 子代理委派（ADK AgentTool）
│   │       └── tool_search_tool.py    # 动态工具发现（deferred tools）
│   ├── plugins/
│   │   └── agent_invocation_plugin.py # ADK BasePlugin：on_tool_error 喂回 + before_model 续推
│   ├── llm/
│   │   └── hardened_litellm.py    # LiteLlm 子类：ContextOverflow 截断重试 + PromptCache + 异常分类
│   ├── tools/
│   │   ├── knowledge_search.py    # 招牌工具：httpx 调 arag /v1/retrieve
│   │   └── builtin_tools.py       # 通用工具示例（计算器/天气）
│   ├── citation/
│   │   └── citation_injector.py   # 流式 [n] marker → 末尾引用块（ID 协议精简版）
│   ├── stream/
│   │   └── event_converters.py    # ADK Event → SSE 事件(文本/工具调用/工具结果/计划步骤)
│   ├── session/
│   │   └── session_service.py     # 复用 ADK BaseSessionService（InMemory 默认）
│   ├── artifacts/
│   │   └── artifact_service.py    # 复用 ADK BaseArtifactService（多模态图片/文件）
│   ├── observability/
│   │   ├── trace_middleware.py    # trace_id 注入
│   │   └── logging.py             # 结构化 JSON 日志 + [Tag] 前缀
│   └── config.py                  # pydantic-settings（env 驱动）
│
└── arag/                          # ★ 生产 RAG 服务（lippi-arag 精简）
    ├── main.py                    # FastAPI app + 启动
    ├── api/
    │   ├── retrieve.py            # POST /v1/retrieve（混合召回，agent 调它）
    │   └── index.py               # POST /v1/index（文档入库）
    ├── components/
    │   ├── chunker.py             # token / markdown 切分
    │   ├── embedding.py           # 嵌入客户端（DashScope text-embedding-v3）
    │   ├── rewrite.py             # 查询改写
    │   ├── retriever.py           # 混合召回编排（vector + fulltext）
    │   ├── reranker.py            # 重排
    │   ├── filter.py              # score 阈值 + low-value 过滤
    │   └── generator.py           # 带 citation 的答案生成
    ├── processor/
    │   ├── document.py            # markdown/text 解析 + 分块
    │   └── image.py               # 多模态：图片 caption（qwen3.7-plus 视觉）
    ├── store/                     # ★ 存储端口层（依赖倒置）
    │   ├── base.py                # SearchHit / 通用类型
    │   ├── vector_store.py        # VectorStore ABC + LocalVectorStore(numpy 余弦 + 本地持久化)
    │   ├── fulltext_index.py      # FullTextIndex ABC + LocalBM25Index(BM25 + jieba)
    │   ├── graph_store.py         # GraphStore ABC + LocalGraphStore(内存邻接表，仅端口)
    │   └── factory.py             # *_BACKEND=local|... env 选型
    ├── observability/             # 同 agent 侧通用日志/trace（可共享 lib）
    └── config.py
```

---

## 2. 双服务与边界

| 服务 | 端口 | 职责 | 蓝本 |
|---|---|---|---|
| `agent` | 8000 | Agent 运行时：SSE 入口、规划/推理引擎、工具调用、citation、多模态、会话 | `albert-agent-2`（`app/lumi` + `app/core/agent/planning_agent/single_loop`） |
| `arag` | 8100 | RAG 检索：文档入库、混合召回、rerank、生成 | `lippi-arag`（`app/components` + `app/processor`） |

**边界契约**：`agent.tools.knowledge_search` 通过 `httpx` 调 `arag` 的 `POST /v1/retrieve`，
带**超时 + 降级**（检索超时/失败 → 引擎降级为纯对话 chat-mode，不中断会话）。
> 面试点：这就是真实生产里 agent-2 → arag 的微服务调用（含超时/降级/可观测）。

---

## 3. 两代引擎（核心抽象）

```python
class ReasoningEngine(ABC):
    """统一规划/推理引擎端口；两代实现可经 ENGINE 配置切换。"""
    @abstractmethod
    async def run_stream(self, ctx: RunContext) -> AsyncIterator[StreamEvent]: ...
```

| 引擎 | 范式 | 适用 | demo 实现 |
|---|---|---|---|
| `PlanExecuteEngine` | 先**规划**整张计划，再**逐步执行** | 步骤明确、可前置拆解的任务 | decision→execution→summary |
| `AgentLoopEngine` | **单循环 ReAct**，模型迭代调工具直至产出 | 探索式、需动态决策的任务 | ADK Runner 原生循环 + Plugin 续推（原生产 BaseLlmRequestProcessor）+ TaskPlan |

选型：`ENGINE=plan_execute|agent_loop`（替代原项目的 `gray_gate` 灰度门）。
> 详见 [`03-engine-deep-dive.md`](03-engine-deep-dive.md)。

---

## 4. 端到端时序（带图知识问答 + agent-loop）

```
用户带图提问
  │
  ▼ POST /api/v1/chat/{uuid}/stream   (multipart: text + image)
agent.api.chat
  │  trace_id 注入；artifact 存图片；建/取 session
  ▼
ReasoningEngine.run_stream  (ENGINE=agent_loop)
  │  loop_processor 注入计划提醒；LlmAgent(qwen3.7-plus, tools=[...])
  ▼  ── 第 1 轮：模型决定调 knowledge_search ──
tools.knowledge_search ──httpx──▶ arag POST /v1/retrieve
                                    │ rewrite → vector+BM25 混合召回 → filter → rerank
                                    ◀ chunks(含 doc_id/score/图片占位)
  │  ToolErrorFeedback：若 arag 异常 → function_response 喂回，循环继续(降级)
  ▼  ── 第 2 轮：模型基于 chunks 生成答案，输出 [n] marker ──
stream.event_converters：ADK Event → SSE
  │  citation_injector：流式 [n] → 末尾引用块拼接
  │  ContextOverflow：若超长 → 截断重试
  ▼ data: {...}\n\n   (text / tool_call / tool_result / plan_step / citation)
前端增量渲染
```

---

## 5. 与原项目代码映射（学习/面试用）

| demo 模块 | 原项目位置 | 说明 |
|---|---|---|
| `engine/plan_execute/*` | `app/lumi/runner/planner/text/{decision,execution}_planner.py` | 最早 ADK 规划/执行（2026-01） |
| `engine/agent_loop/loop_processor.py` | `app/core/agent/planning_agent/single_loop/single_loop_request_processor.py` | `_SingleLoopRequestProcessor(BaseLlmRequestProcessor)`，注释 "mirrors Claude Code" |
| `engine/agent_loop/task_plan_tool.py` | `.../single_loop/task_plan_tool.py` | 计划即工具，驱动循环终止 |
| `engine/agent_loop/tool_search_tool.py` | `.../single_loop/tool_search_tool.py` | deferred tools 动态发现 |
| `engine/agent_loop/sub_agent_tool.py` | `.../single_loop/sub_agent_tool.py` | 子代理委派 |
| `plugins/agent_invocation_plugin.py` | `.../planning_agent/plugin/agent_invocation_plugin.py` | `BasePlugin`：on_tool_error 喂回 + before_model 续推 |
| `llm/hardened_litellm.py` | `.../planning_agent/litellm/qwen_lite_llm.py` + `litellm/llm_exception_handler.py` | ContextOverflow / PromptCache / 异常分类 |
| `stream/event_converters.py` | `app/lumi/event_converters/*` | ADK Event → 前端事件 |
| `citation/citation_injector.py` | `app/core/agent/planning_agent/processor/stream/citation_injector.py` | ID 协议 citation |
| `arag/components/*` | `lippi-arag/app/components/*` | chunker/embedding/retriever/reranker/rewrite/filter/generator |
| `arag/processor/image.py` | `lippi-arag/app/processor/image/{llm_image_processor,ocr_image_processor}.py` | 图片多模态 |
| `arag/store/*` | `lippi-arag/app/components/store/{base,store_factory}.py` | `BaseStore(ABC)`+`StoreFactory` → 拆分式三端口 |

> **被砍模块**（原项目有、demo 不复刻）：Nacos 灰度、MaaS 模型网关、HSF 用户、SLS、langfuse、rocketmq、A2A 信封、AgentOSFlow mode-JSON 老 planner、匿名问答、群感知、计费。
