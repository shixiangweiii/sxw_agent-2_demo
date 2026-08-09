"""Gen2 Agent-Loop 引擎：ADK Runner 原生工具循环 + 插件加固（ReAct 单循环）。

循环本身由 ADK Runner 驱动（模型调工具→ADK 执行→结果回灌→再调），
生产加固经 Plugin 注入：续推/预算/force-summary（before_model）、工具异常喂回（on_tool_error）。
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, AsyncIterator

from google.adk.agents import LlmAgent
from google.adk.agents.run_config import RunConfig, StreamingMode
from google.adk.runners import Runner

from agent.engine.agent_loop.loop_processor import LoopController
from agent.engine.agent_loop.message_budget import MessageBudget
from agent.engine.agent_loop.plan_event_detector import is_plan_tool_response, plan_events_from
from agent.engine.loop_tools import LOOP_INSTRUCTION, resolve_sub_agent_engine
from agent.engine.loop_tools.sub_agent_tool import build_sub_agent_tool
from agent.engine.loop_tools.task_plan_tool import update_task_plan
from agent.engine.loop_tools.tool_search_tool import build_deferred_tools, tool_search
from agent.engine.base import APP_NAME, ReasoningEngine, RunContext
from agent.runtime.domain.models import EngineOutcome, EngineOutcomeKind
from agent.runtime.adapters.brokered_tools import AdkToolBatch, broker_adk_tools
from agent.plugins.agent_invocation_plugin import AgentInvocationPlugin
from agent.skills.stream_merge import merge_runner_events
from agent.stream.event_converters import StreamEvent, adk_event_to_stream_events
from common.obs import get_logger, log_kv

if TYPE_CHECKING:
    from agent.context import AgentContext

logger = get_logger("agent.loop")

# 框架级硬熔断余量：硬上限 = 业务 max_loop_iters + 该余量，略高于软 force-summary，
# 给 force-summary 留生效窗口（避免在收尾轮立即被硬熔断）。
_HARD_CAP_MARGIN = 2

# 系统指令与 native_loop 共用同一份（见 agent/engine/loop_tools/__init__.py），
# 保证两代循环引擎面对完全相同的 prompt —— 否则对比就变成了 prompt 之争。
_LOOP_INSTRUCTION = LOOP_INSTRUCTION


def build_loop_agent(
    ctx: "AgentContext",
    *,
    rc: RunContext,
    tool_batch: AdkToolBatch | None = None,
) -> LlmAgent:
    # 组装本轮循环里模型"能看见"的全部工具。ADK 会把每个工具转成 FunctionDeclaration
    # 塞进每次模型请求——所以这张表直接决定了模型每一轮的可选动作空间。
    # ctx.tools 是两代引擎共享的部分（内置工具 + knowledge_search + skill-center 技能
    # + Claude SKILL + A2A 子代理）；下面 4 行是 agent_loop 独有的，plan_execute 没有。
    tools: list[Any] = list(ctx.tools)                # 内置：calculator / get_weather
    tools.append(update_task_plan)                    # 计划即工具
    tools.append(tool_search)                         # 动态工具发现
    tools.extend(build_deferred_tools())              # 延迟工具：translate / text_stats
    # 子代理委派：按 SUB_AGENT_ENGINE 选内核（两种实现同名 researcher、同描述、同参数）。
    sub_engine = resolve_sub_agent_engine(ctx.settings.sub_agent_engine, rc.engine)
    if sub_engine == "adk":
        tools.append(build_sub_agent_tool(ctx.llm))
    else:
        from agent.engine.native_loop.sub_agent import build_researcher_tool
        tools.append(build_researcher_tool(ctx.chat))   # 普通函数工具，ADK 会自动包成 FunctionTool
    # 注意这里没有配 sub_agents：researcher 和 Claude SKILL 都是被包成 AgentTool 的"普通工具"
    # （Agent-as-Tool），对主循环表现为一次标准 tool_use，不走 ADK 的 agent transfer 机制。
    return LlmAgent(
        name="agent_loop",
        model=ctx.llm,
        instruction=_LOOP_INSTRUCTION,
        tools=broker_adk_tools(tools, rc, batch=tool_batch),
    )


class AgentLoopEngine(ReasoningEngine):
    def __init__(self, ctx: "AgentContext") -> None:
        self._ctx = ctx

    async def run_stream(self, rc: RunContext) -> AsyncIterator[StreamEvent]:
        tool_batch = AdkToolBatch(rc) if rc.tool_broker is not None else None
        agent = build_loop_agent(self._ctx, rc=rc, tool_batch=tool_batch)
        # LoopController 每请求一份（它要记本轮的迭代次数），随插件注入到 ADK。
        controller = LoopController(max_iters=rc.settings.max_loop_iters, budget=MessageBudget())
        runner = Runner(
            app_name=APP_NAME,
            agent=agent,
            session_service=rc.session_service,
            artifact_service=rc.artifact_service,
            # ★ 生产加固的注入点：ADK Plugin 是官方扩展点，不需要 fork 框架。
            #   这一个插件同时承担：工具参数 sentinel 短路、工具异常喂回、
            #   以及把 before_model 委托给 LoopController（续推/预算/force-summary）。
            plugins=[AgentInvocationPlugin(controller, tool_batch=tool_batch)],
        )
        # 两层循环控制：业务层 force-summary（软收尾，LoopController）+ 框架层硬熔断。
        hard_cap = rc.settings.max_loop_iters + _HARD_CAP_MARGIN
        log_kv(logger, logging.INFO, "AgentLoop", "start",
               max_iters=rc.settings.max_loop_iters, hard_cap=hard_cap)
        # 事件翻译器：ADK Event → 本项目统一的 StreamEvent。对 update_task_plan 做特殊处理，
        # 让"计划"在 UI 上表现为进度条（plan_step）而不是一次普通的工具调用噪音。
        def _convert(event: Any) -> list[StreamEvent]:
            plan_evs = plan_events_from(event)
            if plan_evs is not None:
                # Tool callback persists WorkingState and MODEL_PLAN_UPDATED in
                # one Runtime transaction.  The request-local ADK projection is
                # deliberately suppressed so a crash cannot expose an
                # uncheckpointed plan or duplicate it on whole-invocation replay.
                return []
            if is_plan_tool_response(event):
                return []                # 计划工具的返回 → 直接丢弃，不给用户看
            return adk_event_to_stream_events(event)   # 其余照常翻译成 text/tool_call/tool_result

        # ★ 真正的"循环"从这一行开始，但它不在本文件里：
        #   runner.run_async → LlmAgent._run_async_impl → BaseLlmFlow.run_async()，
        #   后者内部是 `while True: 调模型 → 执行工具 → 判断 is_final_response()`。
        #   本文件的职责是"给这个循环装上工具面、策略、熔断和事件出口"，而不是自己实现循环。
        #   （详见 sxw_aicoding/代码分析/agent_loop_循环在哪里_ADK_flow_拆解.md）
        # 这里拿到的是异步生成器，尚未开始执行；下面 async for 拉取时才真正跑起来。
        runner_events = runner.run_async(
            user_id=rc.user_id,
            session_id=rc.session_id,
            new_message=rc.user_message,
            # StreamingMode.SSE：让模型输出按 token 增量返回，前端才有打字机效果；
            # max_llm_calls：框架层硬熔断，ADK 在每次调模型前检查，超限抛 LlmCallsLimitExceededError。
            run_config=RunConfig(streaming_mode=StreamingMode.SSE, max_llm_calls=hard_cap),
        )
        # merge_runner_events 并发 drain 技能 UI 队列 → skill_event 实时穿插
        # 为什么需要它：技能/沙箱工具在"一次工具调用内部"会产生大量中间展示帧，
        # 但 ADK 只在工具整体返回后才吐 tool_result。工具把中间帧推进 contextvar 里的队列，
        # 这里并发消费两个来源（Runner 事件 + UI 队列），技能过程才能真正边跑边显示。
        async for se in merge_runner_events(runner_events, _convert):
            yield se
        # Explicit control result, deliberately separate from the event
        # iterator.  Adapter EOF without this assignment fails closed.
        rc.engine_outcome = EngineOutcome(kind=EngineOutcomeKind.COMPLETED)
        # 不向传输层发 done；Run terminal 只由 Coordinator 裁决。
