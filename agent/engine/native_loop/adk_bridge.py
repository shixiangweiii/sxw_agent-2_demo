"""ADK Agent → native_loop 工具的桥接。

**为什么需要它**：`AgentTool.run_async` 会访问 ``tool_context._invocation_context``
取 user_id / credential_service / plugin_manager，再据此建子 Runner。我们的
``NativeToolContext`` 是个鸭子类型 shim，给不出这些东西——直接转调会 AttributeError，
被 executor 兜成 is_error，表现为"A2A 工具永远失败"。

所以对这类工具不能走 ``from_adk_tool``，得自己建一个 ADK Runner 来驱动它内部的 Agent。
这和 `agent/claude_skill/skill_runner.py` 给 Claude SKILL 建独立子 Runner 是同一套做法。

适用对象：A2A 远程子代理（``SelfContainedA2AAgentTool`` → ``RemoteA2aAgent``）。
按已确认的决策，A2A 客户端保持 ADK 实现、不随 ``SUB_AGENT_ENGINE`` 切换——
远端本来就跑在 a2a_service 里的 ADK 上，agent 侧的开关改不了它。
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from google.adk.agents.run_config import RunConfig, StreamingMode
from google.adk.runners import InMemoryRunner
from google.genai import types

from agent.asyncio_utils import await_with_deferred_cancellation
from agent.engine.native_loop.tools import (
    NativeToolContext,
    ToolSpec,
    _adk_declaration_to_schema,
)
from common.obs import get_logger, log_kv

logger = get_logger("agent.native")

_APP = "native-adk-bridge"
# 子代理的调用轮次上限。A2A 远程代理在对端自有循环，本地这层只是转发，
# 给个小值即可；真正的失控保护在对端。
_MAX_LLM_CALLS = 8


def is_agent_tool(tool: Any) -> bool:
    """识别需要走桥接的 ADK AgentTool（含其子类，如 A2A 的 SelfContainedA2AAgentTool）。"""
    try:
        from google.adk.tools.agent_tool import AgentTool
    except ImportError:  # pragma: no cover - ADK 缺失时不该走到这里
        return False
    return isinstance(tool, AgentTool)


def from_agent_tool(tool: Any) -> ToolSpec:
    """把 ADK ``AgentTool`` 包成 ToolSpec：保留其声明，执行时自建子 Runner。"""
    name = getattr(tool, "name", None) or type(tool).__name__
    description = getattr(tool, "description", "") or ""
    parameters = _adk_declaration_to_schema(tool, name)
    agent = getattr(tool, "agent", None)
    if agent is None:
        raise RuntimeError(f"AgentTool {name} 缺少内部 agent，无法桥接")

    async def run(args: dict[str, Any], ctx: NativeToolContext) -> Any:
        return await _run_agent(agent, args, name=name)

    return ToolSpec(
        name=name,
        description=description,
        parameters=parameters,
        run=run,
        # 远程子代理有副作用且不可控，一律串行（与 CC 对非只读工具的处理一致）。
        concurrency_safe=False,
    )


async def _run_agent(agent: Any, args: dict[str, Any], *, name: str) -> dict[str, Any]:
    """在独立 ADK Runner 中跑一次子代理，返回其终局文本。"""
    # AgentTool 的入参约定就是 request（见 a2a/loader.py 对该字段的描述增强）。
    request = args.get("request")
    if not isinstance(request, str) or not request.strip():
        return {
            "error": "request 必须是非空字符串",
            "hint": "远程子代理不继承父会话历史，request 必须是自包含的任务说明。",
        }

    message = types.Content(role="user", parts=[types.Part(text=request.strip())])
    runner = InMemoryRunner(agent=agent, app_name=_APP)
    runner_failed = False
    final_text: Optional[str] = None
    try:
        session = await runner.session_service.create_session(app_name=_APP, user_id="native")
        events = runner.run_async(
            user_id="native",
            session_id=session.id,
            new_message=message,
            run_config=RunConfig(streaming_mode=StreamingMode.NONE, max_llm_calls=_MAX_LLM_CALLS),
        )
        events_failed = False
        try:
            async for event in events:
                text = _terminal_text(event)
                if text:
                    final_text = text
        except BaseException:
            events_failed = True
            raise
        finally:
            await _close(events.aclose(), label="event stream close", suppress=events_failed)
    except Exception as exc:  # noqa: BLE001 - 转成结构化失败喂回模型，不中断主循环
        runner_failed = True
        log_kv(logger, logging.WARNING, "AdkBridge", "sub agent failed",
               tool=name, error=type(exc).__name__)
        return {"error": f"{type(exc).__name__}: {exc}",
                "hint": "远程子代理调用失败，可重试或改用其它方式；不要中断对话。"}
    finally:
        await _close(runner.close(), label="runner close", suppress=runner_failed)

    if not final_text:
        return {"error": "远程子代理未返回有效文本", "hint": "可换一种表述重试。"}
    return {"request": request.strip(), "result": final_text}


def _terminal_text(event: Any) -> Optional[str]:
    """只取子代理的终局聚合文本（非流式、无工具调用的那一条）。"""
    content = getattr(event, "content", None)
    if content is None or bool(getattr(event, "partial", False)):
        return None
    if getattr(content, "role", None) != "model":
        return None
    if event.get_function_calls() or event.get_function_responses():
        return None
    text = "".join(
        part.text for part in (getattr(content, "parts", None) or [])
        if getattr(part, "text", None) and not bool(getattr(part, "thought", False))
    ).strip()
    return text or None


async def _close(awaitable: Any, *, label: str, suppress: bool) -> None:
    """清理不得被调用方取消打断，也不得覆盖已有的主异常。"""

    def log_error(exc: BaseException) -> None:
        log_kv(logger, logging.WARNING, "AdkBridge", f"{label} failed after cancellation",
               error=type(exc).__name__)

    try:
        await await_with_deferred_cancellation(awaitable, on_error_after_cancel=log_error)
    except Exception as exc:  # noqa: BLE001
        if not suppress:
            raise
        log_kv(logger, logging.WARNING, "AdkBridge", f"{label} failed while preserving primary error",
               error=type(exc).__name__)
