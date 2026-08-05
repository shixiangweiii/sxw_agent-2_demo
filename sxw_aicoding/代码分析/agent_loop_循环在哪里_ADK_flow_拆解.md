# Agent-Loop 的循环到底在哪里 —— ADK BaseLlmFlow 拆解

- 生成时间：2026-08-05
- 分析对象：`agent/engine/agent_loop/agent_loop_engine.py` 与 `google-adk==2.6.2` 的 flow 实现
- 起因问题：**「这里是用的 ADK 自带的 agent-loop 功能么？为什么看不到 while 调用工具的循环？」**
- 核实方式：全部结论对照本机 `.venv/lib/python3.12/site-packages/google/adk/` 源码逐行确认，
  flow 选型经运行时实际构造 `LlmAgent` 验证（见 §10）

---

## 1. 结论速览

**是 ADK 自带的循环。** `agent_loop_engine.py` 里看不到 `while`，是因为循环体位于
`BaseLlmFlow.run_async()`（`base_llm_flow.py:949`），一共只有 10 行。

本项目在 `agent_loop_engine.py` 里做的是**给这个循环装控制面和观测面**，而不是自己实现循环：

```
ADK 负责：怎么转（while + 终止判定 + 工具调度 + 事件落库）
本项目负责：转的时候带什么工具、遵循什么策略、什么时候必须停、事件怎么对外流
```

一句话概括这个取舍：**循环归框架，策略归自己。** 这也是 `README.md` 那条 talking point
「用 ADK 不止是调 Runner」的实际含义——加固全部落在 ADK 的官方扩展点上，没有 fork 框架。

---

## 2. 完整调用链

```text
agent_loop_engine.py:90   runner.run_async(...)
  └─ Runner                          google/adk/runners.py
       └─ LlmAgent._run_async_impl()      llm_agent.py:547
            └─ self._llm_flow.run_async(ctx)   llm_agent.py:841（flow 选型属性）
                 └─ BaseLlmFlow.run_async()    base_llm_flow.py:949   ← ★ while 在这里
                      └─ _run_one_step_async() base_llm_flow.py:964   ← 一步 = 一次 LLM 调用
                           ├─ _preprocess_async()     组装 llm_request（工具声明、instruction、历史）
                           ├─ llm.generate_content_async()   base_llm_flow.py:1447
                           └─ _postprocess_async()    base_llm_flow.py:1098
                                └─ _postprocess_handle_function_calls_async()  :1305
                                     └─ functions.handle_function_calls_async()  functions.py:418
                                          └─ asyncio.create_task × N + gather    functions.py:457-473
```

### 本项目实际走的是 `AutoFlow`（不是 `SingleFlow`）

`llm_agent.py:841` 的选型逻辑：

```python
@property
def _llm_flow(self) -> BaseLlmFlow:
    if (self.disallow_transfer_to_parent
        and self.disallow_transfer_to_peers
        and not self.sub_agents):
        return SingleFlow()
    else:
        return AutoFlow()
```

`disallow_transfer_to_parent` / `disallow_transfer_to_peers` 的默认值都是 `False`
（`llm_agent.py:357` / `:365`），因此**即使本项目的 `LlmAgent` 没有配 `sub_agents`，
拿到的仍然是 `AutoFlow`**（运行时实测确认，见 §10）。

`AutoFlow` 是 `SingleFlow` 的子类（`auto_flow.py:23`），只多挂一个 agent-transfer 请求处理器；
`while` 循环本体在共同的父类 `BaseLlmFlow` 中，两者完全一致。对本项目而言这个差异当前无实际影响
——没有 sub_agents 也没有 parent，transfer 能力不会被激活。

> 注意区分：本项目的「子代理」`researcher`（`sub_agent_tool.py`）和 Claude SKILL 子 Agent
> **都不是 ADK 的 `sub_agents`**，它们是被包成 `AgentTool` 的标准工具（Agent-as-Tool）。
> 所以主循环里不存在 agent transfer，只有普通的 tool call。

---

## 3. 循环本体（`base_llm_flow.py:949-962`）

```python
async def run_async(self, invocation_context) -> AsyncGenerator[Event, None]:
    """Runs the flow."""
    while True:
        last_event = None
        async with Aclosing(self._run_one_step_async(invocation_context)) as agen:
            async for event in agen:
                last_event = event
                yield event                        # 事件实时冒泡，不等整轮结束
        if not last_event or last_event.is_final_response() or last_event.partial:
            if last_event and last_event.partial:
                logger.warning('The last event is partial, which is not expected.')
            break
```

三个要点：

1. **终止条件就是 `last_event.is_final_response()`** —— 模型这一步产出的最后一个事件如果不再包含
   function call，循环结束。没有独立的"决定是否继续"的判定器，是否继续完全由模型的输出形态决定。
   这正是 Tool-Use Agent Loop 与 Plan-Execute 的本质差别：**下一步做什么，是模型每轮现场决定的。**
2. **`yield` 是逐事件穿透的**，不是攒完一轮再吐。所以 SSE 的 `text` 增量、`tool_call`、`tool_result`
   能实时流出去——本项目的 `merge_runner_events` 才有东西可以并发穿插技能 UI 事件。
3. **循环没有次数上限**。软/硬熔断都不在这里，见 §5。

## 4. 一步（one step）= 一次 LLM 调用

`_run_one_step_async` 的 docstring 写得很直白：**"One step means one LLM call."**（`base_llm_flow.py:968`）

一步的内部时序：

```text
preprocess  →  组装 llm_request（工具声明、system instruction、历史 contents）
               ★ before_model_callback 在此附近触发（base_llm_flow.py:1391）
调模型      →  increment_llm_call_count()    ← ★ 硬熔断检查点（base_llm_flow.py:1446）
               llm.generate_content_async()  ← 本项目的 HardenedLiteLlm 覆写点
postprocess →  _postprocess_handle_function_calls_async()（:1305）
               └─ functions.handle_function_calls_async()（functions.py:418）
                    ★ before_tool_callback / on_tool_error_callback 在此触发
                    产出 function_response_event
```

**工具执行发生在 postprocess 里**，产出 `function_response_event` 后本步结束；此时
`is_final_response()` 为假 → 回到 `while` 顶开始下一步。

所以「`tool_use → tool_result → 再推理」这个环，物理上就是 `while True` 加上
`is_final_response()` 这一个判断**——没有别的隐藏机制。

### 同轮多个 function call 是并行的

`functions.py:457-473`：

```python
tasks = [
    asyncio.create_task(_execute_single_function_call_async(...))
    for function_call in filtered_calls
]
function_response_events = await asyncio.gather(*tasks)
```

ADK 把**同一个模型轮次里的多个 function call 全部 create_task 后 gather**。这条事实是本项目两处
设计的直接前提：

- **Claude SKILL 的 instruction-first 守卫需要 `call_soon` 延迟激活**
  （`claude_skill/toolset.py:41-52`）：因为同轮的 sibling tool task 已经被创建，
  不能因为 `read_file(SKILL.md)` 恰好先完成就放行它们。
- **`SkillExecutionCoordinator` 的 invocation 读写门**（`execution_coordinator.py`）：
  同轮并行是 ADK 给的默认行为，所以并发治理必须由本项目自己补，否则 non-parallel-safe 的
  技能会真的并行跑起来。

反过来，**有数据依赖的调用必须跨轮串行**——因为下一轮的 function call 是模型看到上一轮
`tool_result` 之后才生成的。这就是 `_LOOP_INSTRUCTION` 第 5 条约束的由来
（`agent_loop_engine.py:48-49`）。

---

## 5. 本项目在这个循环上挂了什么

| 本项目代码 | 挂载到 ADK 的位置 | 作用 |
|---|---|---|
| `build_loop_agent()` 的 `tools=[...]` | `LlmAgent.tools` → preprocess 组装工具声明 | 决定模型能调什么：内置工具 + 计划工具 + `tool_search` 延迟工具 + Agent-as-Tool（`researcher` / Claude SKILL） |
| `_LOOP_INSTRUCTION` | system instruction | 循环策略：计划非调度器、依赖跨轮串行、同轮仅限独立任务、按 `error.code`/`retryable` 决策 |
| `AgentInvocationPlugin.before_model_callback` → `LoopController` | `base_llm_flow.py:1391`，每步开头 | 迭代计数、`MessageBudget` 裁剪、计划续推提醒、**force-summary 软收尾** |
| `AgentInvocationPlugin.before_tool_callback` | 工具分发前 | 工具参数 sentinel 短路，非对象参数不进真实工具 |
| `AgentInvocationPlugin.on_tool_error_callback` | 工具抛异常时 | 封成 `function_response` 喂回模型，**让 `while` 能继续转而不是中断 turn** |
| `HardenedLiteLlm.generate_content_async` | `base_llm_flow.py:1447` 调用的就是它 | 上下文超长截断重试、异常分类、PromptCache（provider-aware） |
| `RunConfig(max_llm_calls=hard_cap)` | `base_llm_flow.py:1446` 的 `increment_llm_call_count()` | **框架层硬熔断**，超限抛 `LlmCallsLimitExceededError` |
| `merge_runner_events(runner_events, _convert)` | 消费 `run_async` yield 出的 Event | ADK Event → 统一 SSE，并并发 drain 技能 UI 队列使 `skill_event` 实时穿插 |

一个观察：**这张表里没有一行是"修改 ADK 内部"**。全部通过 Plugin、模型子类、RunConfig、工具集
四个官方入口注入。唯一的私有依赖是工具参数 shim（`llm/tool_args_normalizer.py`），
且带版本 pin + 启动期符号校验 + 不匹配即 fail-fast。

---

## 6. 两层熔断：为什么 `_HARD_CAP_MARGIN = 2`

```python
# agent_loop_engine.py:35
_HARD_CAP_MARGIN = 2
hard_cap = rc.settings.max_loop_iters + _HARD_CAP_MARGIN   # 8 + 2 = 10
```

| 层 | 触发点 | 值 | 行为 |
|---|---|---:|---|
| 业务软收尾 | `LoopController.before_model`，`iter >= max_iters` | 8 | 注入「已达最大推理步数，请立即给出最终答案，不要再调用工具」 |
| 框架硬熔断 | ADK `increment_llm_call_count()` | 10 | 抛 `LlmCallsLimitExceededError` |

留 2 轮余量的原因：force-summary 是**软控制**——它只是往上下文里塞一条系统提醒，
模型还需要至少一次完整的 LLM 调用才能把最终答案生成出来。如果硬熔断贴着软收尾设，
模型会在"刚被提醒"的那一轮就被框架掐断，永远走不到最终答案。

两层熔断分别落在两个不同的扩展点上（Plugin 的 `before_model` vs `RunConfig`），
互不依赖——即使 Plugin 出问题，硬熔断仍然兜底。

---

## 7. 两代引擎对比：同一个 ADK 循环，差异全在外面

一个容易误解的点：**Gen1 `plan_execute` 用的是同一个 ADK 循环**
（`execution_planner.py:57` 也是 `runner.run_async` + 同样的 `max_llm_calls`）。

| | `plan_execute`（Gen1） | `agent_loop`（Gen2） |
|---|---|---|
| 循环实现 | ADK `BaseLlmFlow.run_async` | 同左 |
| 循环前 | 先用 `AgentChatClient` 单独调一次 LLM 产出计划，写进 instruction | 无前置规划 |
| 工具面 | 只有 `ctx.tools` | `ctx.tools` + `update_task_plan` + `tool_search` + 延迟工具 + `researcher` |
| `before_model` | **不挂 `LoopController`**（`AgentInvocationPlugin()` 不传 controller，回调为 no-op） | 挂：计数 / 预算 / 续推 / force-summary |
| 熔断形态 | 只有硬熔断 → `LlmCallsLimitExceededError` → 被 pump 转成 `error` SSE 事件，**用户拿不到最终答案** | 先 force-summary 软着陆，硬熔断只是兜底 |

所以两代引擎的差异**从来不在"有没有循环"，而在循环外面包了什么**：
前置规划 vs 无前置、工具面宽窄、有无软收尾。

> 这也是《`启发感悟/agent_loop_vs_plan_execute_复杂问题启发.md`》那条判断的代码级证据：
> 两者不是割裂范式，而是同一循环上「约束强度」的连续谱。plan_execute 一旦补上 observe/replan，
> 本质就会变成带计划约束的 agent-loop。
>
> 另注：上表最后一行的 plan_execute 熔断失败形态，已作为 **L1** 记入
> `sxw_aicoding/review/20260805_full_codebase_review.md`。

---

## 8. 为什么不自己写这个 while

如果自己实现循环，需要重新承担并持续维护：

- function call / function response 的 id 配对与补全（`populate_client_function_call_id`）
- 同一轮多个 function call 的并行 task 创建、gather、异常时的批量取消
- 事件落 session、branch 管理、partial 事件聚合
- `transfer_to_agent`、long-running tool、artifact、telemetry span
- 以上全部逻辑跟随 ADK 版本演进

而这些恰恰**不是本项目想展示的东西**。项目要展示的是「在生产里给 Agent Runtime 加什么控制」——
参数防护、异常喂回、上下文预算、熔断分层、并发治理、可观测性。这些全部可以在不碰循环实现的前提下完成。

代价是**接受一层版本耦合**：ADK 的 flow 是公开可依赖的 API，但它内部的行为细节（如同轮并行调度）
是本项目若干设计的隐含前提。因此仓库精确 pin `google-adk==2.6.2`，并在 `CLAUDE.md` / `AGENTS.md`
中记录了"ADK 私有契约须随版本审计"。

---

## 9. 升级 ADK 时需要重新确认的假设

本文分析的行为里，以下几条是本项目设计的**隐含前提**，升级 `google-adk` 时应逐条复核：

1. `BaseLlmFlow.run_async` 的终止条件仍是 `is_final_response()`（影响：熔断余量是否仍然够用）。
2. `increment_llm_call_count()` 仍在**每次调模型前**执行（影响：硬熔断实际生效点）。
3. `handle_function_calls_async` 仍**同轮并行**执行多个 function call
   （影响：Claude SKILL 的 `call_soon` 延迟激活屏障、Coordinator 并发门是否仍必要且正确）。
4. `before_model_callback` / `before_tool_callback` / `on_tool_error_callback` 的触发时机不变
   （影响：全部生产加固）。
5. `_llm_flow` 选型逻辑与 `disallow_transfer_to_*` 默认值不变（影响：是否会意外激活 agent transfer）。
6. LiteLlm 的三个私有符号仍存在且签名兼容（影响：工具参数 shim，已有启动期 fail-fast 保护）。

---

## 10. 核实记录

本文所有结论均对照本机 `google-adk==2.6.2` 源码确认，关键位置：

| 结论 | 位置 |
|---|---|
| 循环本体 `while True` | `flows/llm_flows/base_llm_flow.py:949-962` |
| "One step means one LLM call" | `flows/llm_flows/base_llm_flow.py:964-968` |
| flow 选型属性 | `agents/llm_agent.py:841-849` |
| `disallow_transfer_to_*` 默认 `False` | `agents/llm_agent.py:357` / `:365` |
| `_run_async_impl` 调 flow | `agents/llm_agent.py:547` |
| 硬熔断检查点 | `flows/llm_flows/base_llm_flow.py:1446` |
| `LlmCallsLimitExceededError` 定义与判定 | `agents/invocation_context.py:51` / `:94-100` / `:399` |
| `before_model_callback` 触发 | `flows/llm_flows/base_llm_flow.py:1391`（→ `:207` `_handle_before_model_callback`） |
| 工具执行入口 | `flows/llm_flows/base_llm_flow.py:1305` → `flows/llm_flows/functions.py:418` |
| 同轮并行 create_task + gather | `flows/llm_flows/functions.py:457-473` |

flow 选型另经运行时验证：

```bash
.venv/bin/python -c "
from google.adk.agents import LlmAgent
a = LlmAgent(name='t', model='x')
print(type(a._llm_flow).__name__, a.disallow_transfer_to_parent, a.disallow_transfer_to_peers, a.sub_agents)
"
# → AutoFlow False False []
```

即：**本项目的主 Agent 实际运行在 `AutoFlow` 上**，而非直觉上的 `SingleFlow`。
