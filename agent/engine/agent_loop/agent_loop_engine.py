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
from agent.engine.agent_loop.sub_agent_tool import build_sub_agent_tool
from agent.engine.agent_loop.task_plan_tool import update_task_plan
from agent.engine.agent_loop.tool_search_tool import build_deferred_tools, tool_search
from agent.engine.base import ReasoningEngine, RunContext
from agent.plugins.agent_invocation_plugin import AgentInvocationPlugin
from agent.session.session_service import APP_NAME
from agent.skills.stream_merge import merge_runner_events
from agent.stream.event_converters import StreamEvent, adk_event_to_stream_events
from common.obs import get_logger, log_kv

if TYPE_CHECKING:
    from agent.context import AgentContext

logger = get_logger("agent.loop")

# 框架级硬熔断余量：硬上限 = 业务 max_loop_iters + 该余量，略高于软 force-summary，
# 给 force-summary 留生效窗口（避免在收尾轮立即被硬熔断）。
_HARD_CAP_MARGIN = 2

_LOOP_INSTRUCTION = (
    "你是一个生产级智能体（Agent-Loop 模式）。在一个循环里思考、调用工具、观察结果，直到产出最终答案。\n"
    "规则：\n"
    "1) 复杂任务可调用 update_task_plan 记录计划和进度；它不是调度器，实际顺序仍由本循环逐轮决定。\n"
    "2) 需要专用能力（如翻译、文本统计）时，先调用 tool_search 发现工具，再调用对应工具。\n"
    "3) 复杂、开放的子问题可委派给 researcher 子代理。\n"
    # 注：A/B(repeat3) 显示对 agent_loop 加重的"必须检索/严格忠实"约束反而使 kq-rag/kq-multidoc
    # 偶发跳过检索、忠实度下降（KQ 15/15→9/15、faith 4.5→3.5），故保留原较轻指令；
    # 同款约束对 plan_execute 是净增益（见 eval/reports/AB-prompt-v1.md）。
    "4) 回答知识型/企业内部问题前先调用 knowledge_search 检索；若返回了资料，"
    "回答时在引用处用 [n] 标注（n 为资料序号）；若无资料则据常识回答，绝不编造引用。\n"
    "5) Claude Skill 是与普通工具相同的 tool-use：一次调用对应一个有界的 Agent-as-Tool 结果。"
    "有数据依赖的 Skill 必须等上游 tool_result 后在下一轮调用；同轮多个 Skill 调用只能用于彼此独立的任务。\n"
    "6) 工具失败时检查结构化 error.code 与 error.retryable，决定重试、换路或降级，不要中断。\n"
    "7) 信息足够后用简洁中文给出最终答案，并停止调用工具。"
)


def build_loop_agent(ctx: "AgentContext") -> LlmAgent:
    tools: list[Any] = list(ctx.tools)                # 内置：calculator / get_weather
    tools.append(update_task_plan)                    # 计划即工具
    tools.append(tool_search)                         # 动态工具发现
    tools.extend(build_deferred_tools())              # 延迟工具：translate / text_stats
    tools.append(build_sub_agent_tool(ctx.llm))       # 子代理委派
    return LlmAgent(name="agent_loop", model=ctx.llm, instruction=_LOOP_INSTRUCTION, tools=tools)


class AgentLoopEngine(ReasoningEngine):
    def __init__(self, ctx: "AgentContext") -> None:
        self._ctx = ctx

    async def run_stream(self, rc: RunContext) -> AsyncIterator[StreamEvent]:
        agent = build_loop_agent(self._ctx)
        controller = LoopController(max_iters=rc.settings.max_loop_iters, budget=MessageBudget())
        runner = Runner(
            app_name=APP_NAME,
            agent=agent,
            session_service=self._ctx.session_manager.service,
            artifact_service=self._ctx.artifact_service,
            plugins=[AgentInvocationPlugin(controller)],
        )
        # 两层循环控制：业务层 force-summary（软收尾，LoopController）+ 框架层硬熔断。
        hard_cap = rc.settings.max_loop_iters + _HARD_CAP_MARGIN
        log_kv(logger, logging.INFO, "AgentLoop", "start",
               max_iters=rc.settings.max_loop_iters, hard_cap=hard_cap)
        def _convert(event: Any) -> list[StreamEvent]:
            plan_evs = plan_events_from(event)
            if plan_evs is not None:
                return plan_evs
            if is_plan_tool_response(event):
                return []
            return adk_event_to_stream_events(event)

        runner_events = runner.run_async(
            user_id=rc.user_id,
            session_id=rc.session_id,
            new_message=rc.user_message,
            run_config=RunConfig(streaming_mode=StreamingMode.SSE, max_llm_calls=hard_cap),
        )
        # merge_runner_events 并发 drain 技能 UI 队列 → skill_event 实时穿插
        async for se in merge_runner_events(runner_events, _convert):
            yield se
        yield StreamEvent("done", {"finish_reason": "stop"})
