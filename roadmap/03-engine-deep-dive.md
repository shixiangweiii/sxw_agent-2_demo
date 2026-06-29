# 03 · 引擎深潜（demo 核心）

> **这是 demo 的灵魂**：生产级 Agent 运行时**规划与推理引擎**。
> 两代引擎都基于 ADK，统一 `ReasoningEngine` 端口、配置切换。

---

## 0. 统一端口

```python
class RunContext:
    agent_uuid: str
    user_message: types.Content        # 文本 + 可选图片(多模态)
    session: Session                   # ADK 会话(历史)
    tools: list[BaseTool]
    settings: Settings

class ReasoningEngine(ABC):
    @abstractmethod
    async def run_stream(self, ctx: RunContext) -> AsyncIterator[StreamEvent]:
        """产出统一流事件：text / tool_call / tool_result / plan_step / citation / done"""

def build_engine(settings) -> ReasoningEngine:   # 工厂：ENGINE 选型
    return {"plan_execute": PlanExecuteEngine,
            "agent_loop": AgentLoopEngine}[settings.engine](...)
```

---

## 1. Gen1 · Plan-Execute（最早 ADK 范式，源自 `app/lumi`）

**思想**：先让模型产出**整张计划**（意图识别 + 步骤拆解），再**逐步执行**每步（调工具），最后 summary。

```
user query
   ▼
DecisionPlanner（LlmAgent，结构化输出）
   │  → plan = [step1: 检索知识, step2: 生成答案, ...]   ← 意图识别在此发生
   ▼
ExecutionPlanner：for step in plan:
   │     run step（可能触发 tool_call）→ 收集结果 → emit plan_step 事件
   ▼
SummaryAgent：综合各步结果 → 流式最终答案
```

| 组件 | demo 文件 | 原项目 |
|---|---|---|
| 决策/规划 | `engine/plan_execute/decision_planner.py` | `lumi_text_decision_planner.py` |
| 执行 | `engine/plan_execute/execution_planner.py` | `lumi_text_execution_planner.py` |
| 汇总 | （复用 SummaryAgent） | `lumi_summary_agent.py` |

**取舍**：步骤清晰时高效、可控、可解释；但计划一旦定死，难应对执行中才暴露的新信息（→ 催生 Gen2）。

---

## 2. Gen2 · Agent-Loop（最新范式，源自 `single_loop`，"mirrors Claude Code"）

**思想**：**单循环 ReAct**——模型在一个循环里反复 `思考→调工具→看结果→再思考`，直到产出最终答案或计划全部完成。计划是**动态**的（`TaskPlanTool`），可中途调整。

```
              ┌─────────────────────────── loop (≤ MAX_LOOP_ITERS) ───────────────────────────┐
user query →  │  LlmAgent(qwen3.7-plus, tools)                                                 │
              │     ├─ 产出 tool_calls → 执行 → function_response 回灌 → 继续循环               │
              │     └─ 产出最终文本 + 计划全 done → 退出                                        │
              │  续推判定：TaskPlanTool 仍有未完成步骤 → loop_processor 注入「计划提醒」继续    │
              └───────────────────────────────────────────────────────────────────────────────┘
```

**循环本体复用 ADK Runner 原生工具循环**（不是手写 while），每轮 LLM 请求前注入续推上下文。
> 落点：**原生产**在 `BaseLlmRequestProcessor` 层注入；**demo** 因公版 ADK 2.3 改用 `BasePlugin.before_model_callback` 实现同等语义（详见下方扩展点 B）。

| 组件 | demo 文件 | 原项目 | 作用 |
|---|---|---|---|
| 循环续推 | `engine/agent_loop/loop_processor.py`（`LoopController`，经 Plugin 调用） | `single_loop_request_processor.py` | 计划提醒 / force-summary / 消息预算 |
| 计划工具 | `engine/agent_loop/task_plan_tool.py` | `task_plan_tool.py` | 计划即工具，未完成步骤驱动续推 |
| 计划事件 | `engine/agent_loop/plan_event_detector.py` | `plan_event_detector.py` | 计划变更 → SSE `plan_step` 事件 |
| 子代理 | `engine/agent_loop/sub_agent_tool.py` | `sub_agent_tool.py` | 复杂子任务委派给子 Agent（ADK `AgentTool`） |
| 动态工具 | `engine/agent_loop/tool_search_tool.py` | `tool_search_tool.py` | deferred tools：按 query 检索可用工具再挂载 |

---

## 3. 生产级原语（含金量所在 · 进阶档）

**关键洞察**：「用 ADK」≠ 只调 `Runner.run_async`。生产级体现在**三个 ADK 扩展点**上的加固：

### 扩展点 A — `BasePlugin`（生命周期钩子）

| 原语 | demo 文件 | 原项目 | 行为 |
|---|---|---|---|
| **ToolErrorFeedback** | `plugins/agent_invocation_plugin.py` | `plugin/agent_invocation_plugin.py`（`d232526c7`） | 工具抛异常 → 捕获 → 封装成 `function_response`（含错误说明）喂回模型，**不中断 turn**；模型据此重试/换路 |
| 调用可观测 | 同上 | 同上 | `before/after tool` 钩子打点：tool 名 / 耗时 / 成功率 → 结构化日志 |

> **两代引擎的加固边界（评审修复后）**：扩展点 A（ToolErrorFeedback / 调用可观测）与扩展点 C（`LiteLlm` 子类）对**两代引擎都生效**——Plan-Execute 的执行相 Runner 也挂了同一 `AgentInvocationPlugin`（`controller=None` → `before_model` no-op），并同样配 `RunConfig.max_llm_calls` 框架硬熔断；扩展点 B（计划续推 / force-summary / message-budget，由 `LoopController` 驱动）为 **Agent-Loop 专属**（Plan-Execute 按既定计划顺序执行，无需轮级续推）。

### 扩展点 B — 每轮请求前处理（原生产 `BaseLlmRequestProcessor` / demo 适配 `BasePlugin.before_model_callback`）

> 原项目在 `BaseLlmRequestProcessor` 注入续推上下文；demo 因公版 ADK 2.3 该类为私有路径、需侵入 flow，改用 `BasePlugin.before_model_callback` 委托 `LoopController` 实现同等语义（见 `05` R9）。

| 原语 | demo 文件 | 行为 |
|---|---|---|
| 计划续推 | `loop_processor.py` | 计划有未完成步骤 → 注入「继续推进计划」提醒，驱动下一轮 |
| force-summary | `loop_processor.py` | 达到 `max_iters` → 注入强制收尾消息（软收尾）；另配 `RunConfig(max_llm_calls)` 框架级硬熔断 |
| message budget | `loop_processor.py` / `message_budget.py` | 按字符预算裁剪历史，防上下文膨胀（与 LiteLlm reactive 截断互补） |

### 扩展点 C — `LiteLlm` 子类（模型调用层）

| 原语 | demo 文件 | 原项目 | 行为 |
|---|---|---|---|
| **ContextOverflow** | `llm/hardened_litellm.py` | `qwen_lite_llm.py` + `llm_exception_handler.py`（`061a5ede8`） | 上下文超长异常 → 反应式截断（保留首尾/系统）→ 重试；异常分类（超长/限流/其他） |
| **PromptCache** | `llm/hardened_litellm.py` | `qwen_lite_llm.py`（`39d26b2fd`） | 在 tools/system 处插「缓存断点」hint（详见下方 ⚠️ provider 取舍） |
| 异常分类 | `llm/hardened_litellm.py` | `llm_exception_handler.py` | 把上游错误归一为 `ContextOverflow / RateLimit / Other`，决定重试策略 |

> ⚠️ **PromptCache provider 取舍（二次反思）**：Anthropic 的 `cache_control` 缓存断点是**厂商专属**协议；
> DashScope/Qwen 的 OpenAI 兼容端点**不支持**显式 `cache_control`（其上下文缓存是服务端隐式的）。
> 故 demo 把 PromptCache 实现为**provider-aware 抽象**：识别到支持的 provider（如 Anthropic）才注入断点，
> 在 Qwen 上**优雅降级为 no-op** 并打 `[PromptCache] skipped: provider not supported` 日志。
> ——保留架构与面试谈资，同时对能力边界**诚实**，不谎称"在 Qwen 上启用了缓存"。

---

## 4. 两代对照（面试用一页纸）

| 维度 | Plan-Execute (Gen1) | Agent-Loop (Gen2) |
|---|---|---|
| 规划时机 | **前置**：先出整张计划 | **动态**：循环中按需调整 |
| 控制流 | 顺序执行固定步骤 | ReAct 单循环、工具驱动 |
| 适应性 | 弱（计划定死） | 强（边做边改） |
| 可解释性 | 强（计划显式） | 中（靠 plan 事件 + 工具轨迹） |
| 失败恢复 | 工具异常喂回（共享 ToolErrorFeedback 插件）；按计划顺序，无轮级续推 | 工具异常喂回 + 轮级续推 / force-summary |
| 对应原项目 | `app/lumi`（2026-01） | `single_loop`（2026-04~06，持续加固） |
| 切换 | `ENGINE=plan_execute` | `ENGINE=agent_loop` |

---

## 5. 流事件协议（SSE）

```
event: text         data: {"delta":"...部分文本..."}
event: tool_call    data: {"name":"knowledge_search","args":{...}}
event: tool_result  data: {"name":"knowledge_search","ok":true,"summary":"命中 5 篇"}
event: plan_step    data: {"step":2,"total":3,"title":"生成答案","status":"running"}
event: citation     data: {"refs":[{"n":1,"title":"...","doc_id":"..."}]}
event: done         data: {"finish_reason":"stop"}
```
> 统一封装：`stream/event_converters.py` 把 ADK `Event` 翻译成上述协议；
> `citation_injector` 在 text 流里识别 `[n]` 并在末尾补 `citation` 事件。
