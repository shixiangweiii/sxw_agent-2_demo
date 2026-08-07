"""Claude SKILL 子 Runner 的**可换内核**（driver）。

`skill_runner.run_skill` 是策略外壳，装着不该被复制的东西：沙箱装载、两次
attempt 重试、instruction-first 合规判定、错误码分流、双层取消安全清理。
这些逻辑只有一份。真正随 ``SUB_AGENT_ENGINE`` 切换的只是本文件里的"怎么驱动一轮子代理"。

两个内核对外契约完全一致：

    async def drive(...) -> str | None      # 返回子代理的终局文本

失败信号也保持一致：达到模型调用上限时**都抛 ADK 的 ``LlmCallsLimitExceededError``**，
这样策略外壳里"读了 SKILL.md 才算超限 / 没读算违规"的分流逻辑一行都不用改。
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from google.adk.agents import LlmAgent
from google.adk.agents.invocation_context import LlmCallsLimitExceededError
from google.adk.agents.run_config import RunConfig, StreamingMode
from google.adk.runners import InMemoryRunner
from google.genai import types

from agent.plugins.tool_args_guard_plugin import ToolArgsGuardPlugin
from agent.stream.event_converters import adk_event_to_stream_events

_APP = "claude-skill"

# 子代理产出的展示帧里，只有这两类会透传给前端；
# 子代理正文（含终局文本）不进 UI——主 Agent 才是唯一的最终答复者。
_UI_EVENTS = frozenset({"tool_call", "tool_result"})


def _adk_terminal_text(event: Any, agent_name: str) -> Optional[str]:
    """只接受子 Agent 最后一轮、非 thought、无工具调用的聚合 model 文本。"""
    content = getattr(event, "content", None)
    if (
        getattr(event, "author", None) != agent_name
        or bool(getattr(event, "partial", False))
        or content is None
        or getattr(content, "role", None) != "model"
        or not event.is_final_response()
        or event.get_function_calls()
        or event.get_function_responses()
    ):
        return None
    text = "".join(
        part.text
        for part in (getattr(content, "parts", None) or [])
        if getattr(part, "text", None) and not bool(getattr(part, "thought", False))
    ).strip()
    return text or None


async def drive_adk(
    *,
    agent_name: str,
    system_instruction: str,
    message_text: str,
    tools: list[Callable[..., Any]],
    llm: Any,
    max_llm_calls: int,
    emit: Callable[..., Any],
    close: Callable[..., Any],
) -> Optional[str]:
    """ADK 内核：InMemoryRunner 驱动一个 LlmAgent（原有实现，行为不变）。"""
    agent = LlmAgent(
        name=agent_name,
        model=llm,
        instruction=system_instruction,
        tools=tools,
    )
    message = types.Content(role="user", parts=[types.Part(text=message_text)])
    final_text: Optional[str] = None
    runner = InMemoryRunner(agent=agent, app_name=_APP, plugins=[ToolArgsGuardPlugin()])
    runner_failed = False
    try:
        session = await runner.session_service.create_session(app_name=_APP, user_id="skill")
        events = runner.run_async(
            user_id="skill",
            session_id=session.id,
            new_message=message,
            run_config=RunConfig(
                streaming_mode=StreamingMode.SSE,
                max_llm_calls=max_llm_calls,
            ),
        )
        events_failed = False
        try:
            async for event in events:
                candidate = _adk_terminal_text(event, agent_name)
                if candidate:
                    final_text = candidate
                for stream_event in adk_event_to_stream_events(event):
                    if stream_event.event in _UI_EVENTS:
                        await emit(stream_event.event, stream_event.data)
        except BaseException:
            events_failed = True
            raise
        finally:
            await close(events.aclose(), label="runner event stream close",
                        suppress_errors=events_failed)
    except BaseException:
        runner_failed = True
        raise
    finally:
        await close(runner.close(), label="runner close", suppress_errors=runner_failed)
    return final_text


async def drive_native(
    *,
    agent_name: str,
    system_instruction: str,
    message_text: str,
    tools: list[Callable[..., Any]],
    llm: Any,          # noqa: ARG001 - 原生内核不用 ADK 模型对象，签名与 ADK 内核对齐
    max_llm_calls: int,
    emit: Callable[..., Any],
    close: Callable[..., Any],  # noqa: ARG001 - 原生循环自管生命周期，无需外部清理钩子
) -> Optional[str]:
    """原生内核：用 native_loop 的循环驱动同一个技能包。

    工具面完全相同（沙箱工具本来就是普通 Python 函数）；
    区别只在"循环归谁驱动"，与主引擎的对比是同一条轴。
    """
    # 延迟 import：ENGINE=agent_loop 且 SUB_AGENT_ENGINE=adk 时不必加载原生循环。
    from agent.config import get_settings
    from agent.engine.native_loop.llm_client import get_shared_client
    from agent.engine.native_loop.loop import T_HARD_CAP, LoopConfig, NativeLoop
    from agent.engine.native_loop.messages import Msg
    from agent.engine.native_loop.tools import build_registry

    settings = get_settings()
    registry = build_registry(list(tools))
    loop = NativeLoop(
        # 复用进程级客户端：每次技能调用新建一个 AsyncOpenAI 会持续泄漏 httpx 连接池。
        client=get_shared_client(settings),
        registry=registry,
        system_instruction=system_instruction,
        # 技能子代理不做上下文压缩：它是有界的一次性任务，且额外的摘要调用
        # 会挤占 SKILL_MAX_LLM_CALLS 预算。窗口给足即可（chat=None 即关闭压缩）。
        chat=None,
        config=LoopConfig(
            max_iters=max(1, max_llm_calls - 1),      # 留一轮给收尾输出
            hard_cap=max_llm_calls,
            max_tool_concurrency=settings.native_max_tool_concurrency,
            streaming_tool_exec=settings.native_streaming_tool_exec,
            tool_result_max_chars=settings.native_tool_result_max_chars,
            context_window_tokens=settings.context_window_tokens,
            compact_buffer_tokens=settings.compact_buffer_tokens,
            compact_preserve_units=settings.compact_preserve_units,
        ),
    )

    async for event in loop.run([Msg(role="user", content=message_text)]):
        if event.event in _UI_EVENTS:
            await emit(event.event, event.data)

    # 与 ADK 内核共用同一个失败信号，策略外壳因此不必区分内核类型。
    if loop.stop_reason == T_HARD_CAP:
        raise LlmCallsLimitExceededError(
            f"Skill Agent 达到 {max_llm_calls} 次模型调用上限",
        )

    # 终局文本 = 最后一条不带工具调用的 assistant 消息（比 ADK 那套
    # is_final_response()/author/partial 三重启发式更直接，也更可靠）。
    for msg in reversed(loop.final_messages):
        if msg.role == "assistant" and not msg.tool_calls:
            text = msg.content if isinstance(msg.content, str) else ""
            if text.strip():
                return text.strip()
    return None


def resolve_driver(sub_agent_engine: str, main_engine: str) -> Callable[..., Any]:
    """按配置选内核。``auto`` 跟随主引擎（plan_execute 视同 adk）。"""
    choice = (sub_agent_engine or "auto").strip().lower()
    if choice == "auto":
        choice = "native" if main_engine == "native_loop" else "adk"
    return drive_native if choice == "native" else drive_adk


__all__ = ["drive_adk", "drive_native", "resolve_driver"]
