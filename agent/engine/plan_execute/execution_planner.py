"""执行器：按计划驱动 ADK Runner（带工具）执行，流式转出事件（Plan-Execute 的"执行"相）。"""
from __future__ import annotations

from typing import TYPE_CHECKING, AsyncIterator

from google.adk.agents import LlmAgent
from google.adk.agents.run_config import RunConfig, StreamingMode
from google.adk.runners import Runner

from agent.engine.base import RunContext
from agent.plugins.agent_invocation_plugin import AgentInvocationPlugin
from agent.session.session_service import APP_NAME
from agent.skills.stream_merge import merge_runner_events
from agent.stream.event_converters import StreamEvent, adk_event_to_stream_events

if TYPE_CHECKING:
    from agent.context import AgentContext

# 框架级硬熔断余量（与 Agent-Loop 对齐），避免执行相工具循环失控
_HARD_CAP_MARGIN = 2


def _build_instruction(plan: list[str]) -> str:
    steps = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(plan))
    return (
        "你是一个智能助理。请按以下执行计划完成用户请求；需要时调用可用工具获取信息；"
        "知识型/企业内部/事实型问题必须先调用 knowledge_search 检索，不得凭记忆直接作答；"
        "回答时严格基于检索资料，只复述资料中出现过的内容，"
        "不得补充或编造资料未提供的公式、参数、函数名或具体数字，在引用处用 [n] 标注（n 为资料序号）；"
        "若检索无资料或不可用（degraded），必须在开头显式声明『未能检索到知识库资料，以下基于常识』，"
        "再谨慎作答、绝不编造引用，也不得把常识冒充为检索结果；"
        "最终用简洁中文给出结果。\n\n[执行计划]\n" + steps
    )


class ExecutionPlanner:
    def __init__(self, ctx: "AgentContext") -> None:
        self._ctx = ctx

    async def execute(self, rc: RunContext, plan: list[str]) -> AsyncIterator[StreamEvent]:
        agent = LlmAgent(
            name="executor",
            model=self._ctx.llm,
            instruction=_build_instruction(plan),
            tools=list(self._ctx.tools),
        )
        runner = Runner(
            app_name=APP_NAME,
            agent=agent,
            session_service=self._ctx.session_manager.service,
            artifact_service=self._ctx.artifact_service,
            # 与 Agent-Loop 对齐：ToolErrorFeedback（工具异常喂回不中断）。
            # 不传 LoopController → before_model 为 no-op，不引入续推/force-summary 语义。
            plugins=[AgentInvocationPlugin()],
        )
        hard_cap = rc.settings.max_loop_iters + _HARD_CAP_MARGIN
        runner_events = runner.run_async(
            user_id=rc.user_id,
            session_id=rc.session_id,
            new_message=rc.user_message,
            run_config=RunConfig(streaming_mode=StreamingMode.SSE, max_llm_calls=hard_cap),
        )
        async for se in merge_runner_events(runner_events, adk_event_to_stream_events):
            yield se
