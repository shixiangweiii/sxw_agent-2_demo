"""AgentInvocationPlugin：ADK BasePlugin。

聚合四件生产加固（对应原项目 agent_invocation_plugin）：
- before_tool_callback → ToolArgsGuard：解析 sentinel 在真实工具分发前短路；
- on_tool_error_callback → ToolErrorFeedback：工具异常封装为 function_response 喂回，不中断 turn；
- before_model_callback → 委托 LoopController 做续推 / 预算 / force-summary；
- before/after_tool_callback → 工具调用可观测。
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from google.adk.plugins.base_plugin import BasePlugin

from agent.engine.agent_loop.loop_processor import LoopController
from agent.plugins.tool_args_guard_plugin import build_tool_args_parse_error_response
from common.obs import get_logger, log_kv

logger = get_logger("agent.plugin")


class AgentInvocationPlugin(BasePlugin):
    def __init__(self, controller: Optional[LoopController] = None) -> None:
        super().__init__(name="agent_invocation")
        # controller=None：仅启用 ToolErrorFeedback / 可观测（供 Plan-Execute 复用，
        # 不引入 Agent-Loop 的续推 / force-summary 语义）。
        self._ctrl = controller

    async def before_model_callback(self, *, callback_context: Any, llm_request: Any) -> None:
        if self._ctrl is not None:
            self._ctrl.before_model(callback_context, llm_request)
        return None

    async def before_tool_callback(
        self, *, tool: Any, tool_args: dict[str, Any], tool_context: Any,
    ) -> Optional[dict[str, Any]]:
        parse_error = build_tool_args_parse_error_response(tool, tool_args)
        if parse_error is not None:
            return parse_error
        log_kv(logger, logging.INFO, "ToolCall", "invoke", tool=getattr(tool, "name", "?"))
        return None

    async def after_tool_callback(
        self, *, tool: Any, tool_args: dict[str, Any], tool_context: Any, result: Any,
    ) -> Optional[dict[str, Any]]:
        log_kv(logger, logging.INFO, "ToolCall", "done", tool=getattr(tool, "name", "?"))
        return None

    async def on_tool_error_callback(
        self, *, tool: Any, tool_args: dict[str, Any], tool_context: Any, error: Exception,
    ) -> Optional[dict[str, Any]]:
        log_kv(logger, logging.WARNING, "ToolErrorFeedback", "tool raised, feeding back",
               tool=getattr(tool, "name", "?"), error=type(error).__name__)
        return {
            "error": f"{type(error).__name__}: {error}",
            "hint": "工具执行失败，请根据错误调整参数后重试，或改用其它方式；不要中断对话。",
        }
