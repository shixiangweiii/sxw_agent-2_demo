"""NativeLoopEngine：把自研循环接到统一的 ReasoningEngine 端口上。

职责收得很窄——组装工具面、取/存会话历史、把 genai Content 转成内部消息、
把循环产出的 StreamEvent 透出去。真正的"循环"在 loop.py。

进程级共享的东西（模型客户端、工具注册表、会话历史）放在 ``NativeRuntime`` 单例里：
``build_engine()`` 每请求都会新建引擎实例，这些重对象不能跟着一起重建。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, AsyncIterator, Optional

from agent.engine.base import ReasoningEngine, RunContext
from agent.engine.loop_tools import LOOP_INSTRUCTION, resolve_sub_agent_engine
from agent.engine.loop_tools.task_plan_tool import update_task_plan
from agent.engine.loop_tools.tool_search_tool import build_deferred_tools, tool_search
from agent.engine.native_loop.history import HistoryStore
from agent.engine.native_loop.llm_client import NativeLlmClient, get_shared_client
from agent.engine.native_loop.loop import LoopConfig, NativeLoop
from agent.engine.native_loop.messages import content_to_msg
from agent.engine.native_loop.sub_agent import build_researcher_tool
from agent.engine.native_loop.tools import ToolRegistry, build_registry
from agent.skills.stream_merge import merge_runner_events
from agent.stream.event_converters import StreamEvent
from common.obs import get_logger, log_kv

if TYPE_CHECKING:
    from agent.context import AgentContext

logger = get_logger("agent.native")

# 与 agent_loop 对齐：硬熔断 = 业务软收尾轮次 + 该余量，给 force-summary 留生效窗口。
_HARD_CAP_MARGIN = 2


@dataclass
class NativeRuntime:
    client: NativeLlmClient
    history: HistoryStore
    registry: ToolRegistry


# 进程级单例。与 config.get_settings() 的 lru_cache 是同一种做法：
# ENGINE 是启动期配置，同进程只会有一代引擎在跑，不存在多份运行时并存。
_runtime: Optional[NativeRuntime] = None


def get_runtime(ctx: "AgentContext") -> NativeRuntime:
    global _runtime  # noqa: PLW0603 - 进程级单例的既定写法
    if _runtime is None:
        _runtime = NativeRuntime(
            client=get_shared_client(ctx.settings),
            history=HistoryStore(),
            registry=build_registry(_collect_tools(ctx)),
        )
    return _runtime


def _collect_tools(ctx: "AgentContext") -> list[Any]:
    """组装本引擎的工具面。

    必须与 `agent_loop`（见 agent/engine/agent_loop/agent_loop_engine.py 的
    build_loop_agent）**逐个一致**，否则两代引擎的对比就变成了工具面之争。
    ctx.tools 是两代共享的部分；下面 4 行是 loop 引擎专属的。
    """
    tools: list[Any] = list(ctx.tools)                     # 内置 + 检索 + 技能 + Claude SKILL + A2A
    tools.append(update_task_plan)                         # 计划即工具
    tools.append(tool_search)                              # 动态工具发现
    tools.extend(build_deferred_tools())                   # 延迟工具：translate / text_stats

    # 子代理委派：两种实现同名（researcher）、同描述、同参数（request），
    # 只是内核不同——ADK 版经 adk_bridge 自建子 Runner 执行。
    sub_engine = resolve_sub_agent_engine(ctx.settings.sub_agent_engine, ctx.settings.engine)
    if sub_engine == "adk":
        from agent.engine.loop_tools.sub_agent_tool import build_sub_agent_tool  # noqa: PLC0415
        tools.append(build_sub_agent_tool(ctx.llm))
    else:
        tools.append(build_researcher_tool(ctx.chat))
    log_kv(logger, logging.INFO, "NativeLoop", "sub agent engine resolved",
           configured=ctx.settings.sub_agent_engine, resolved=sub_engine)
    return tools


class NativeLoopEngine(ReasoningEngine):
    def __init__(self, ctx: "AgentContext") -> None:
        self._ctx = ctx
        self._runtime = get_runtime(ctx)

    async def run_stream(self, rc: RunContext) -> AsyncIterator[StreamEvent]:
        runtime = self._runtime
        settings = rc.settings
        hard_cap = settings.max_loop_iters + _HARD_CAP_MARGIN

        # 会话历史 = 一个扁平 Msg 数组（CC 的 mutableMessages）。取出的是副本，
        # 本轮可以自由改写；成功收口后整体写回。
        messages = runtime.history.get(rc.user_id, rc.session_id)
        messages.append(content_to_msg(rc.user_message))
        # 先落一次盘：即使本轮中途崩了，用户这句话也已经进历史，
        # 下一轮不至于丢上下文（对应 CC 在进入循环前先 recordTranscript）。
        runtime.history.replace(rc.user_id, rc.session_id, messages)

        loop = NativeLoop(
            client=runtime.client,
            registry=runtime.registry,
            system_instruction=LOOP_INSTRUCTION,
            # 摘要压缩复用已有的轻量单轮补全客户端（无需流式、无需工具）。
            chat=self._ctx.chat,
            config=LoopConfig(
                max_iters=settings.max_loop_iters,
                hard_cap=hard_cap,
                max_tool_concurrency=settings.native_max_tool_concurrency,
                streaming_tool_exec=settings.native_streaming_tool_exec,
                tool_result_max_chars=settings.native_tool_result_max_chars,
                context_window_tokens=settings.context_window_tokens,
                compact_buffer_tokens=settings.compact_buffer_tokens,
                compact_preserve_units=settings.compact_preserve_units,
            ),
        )
        log_kv(logger, logging.INFO, "NativeLoop", "request",
               user=rc.user_id, session=rc.session_id,
               history=len(messages), max_iters=settings.max_loop_iters, hard_cap=hard_cap)

        # merge_runner_events 与 ADK 无关（签名是 AsyncIterator + 转换器），
        # 这里复用它来并发 drain 技能 UI 队列：技能/沙箱在一次工具调用内部推进来的
        # 展示帧因此能实时穿插在 text / tool_call 之间，而不是等工具整体返回才一次性出现。
        async for event in merge_runner_events(loop.run(messages), lambda e: [e]):
            yield event

        # 循环正常收口才写回完整历史；异常路径下 final_messages 为空，
        # 保留上面已落盘的"仅含用户消息"版本，不写入半截状态。
        if loop.final_messages:
            runtime.history.replace(rc.user_id, rc.session_id, loop.final_messages)
