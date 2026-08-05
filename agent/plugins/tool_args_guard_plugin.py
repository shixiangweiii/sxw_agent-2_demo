"""工具参数 sentinel 的分发前短路插件。"""
from __future__ import annotations

import logging
from typing import Any, Optional

from google.adk.plugins.base_plugin import BasePlugin

from agent.tool_args_contract import get_tool_args_parse_error
from common.obs import get_logger, log_kv

logger = get_logger("agent.plugin")


def build_tool_args_parse_error_response(
    tool: Any, tool_args: dict[str, Any],
) -> Optional[dict[str, Any]]:
    """命中 sentinel 时返回 function response；否则返回 None。"""
    marker = get_tool_args_parse_error(tool_args)
    if marker is None:
        return None
    tool_name = getattr(tool, "name", "?")
    log_kv(
        logger,
        logging.WARNING,
        "ToolArgumentsParseError",
        "invalid arguments blocked before dispatch",
        tool=tool_name,
        error=marker.get("errorType", "Unknown"),
    )
    return {
        "error": "ToolArgumentsParseError: 模型生成的工具参数不是可执行的对象。",
        "hint": "请按工具声明重新生成完整的对象参数后重试；不要中断对话。",
    }


class ToolArgsGuardPlugin(BasePlugin):
    """供没有 AgentInvocationPlugin 的独立 Runner 复用。"""

    def __init__(self) -> None:
        super().__init__(name="tool_args_guard")

    async def before_tool_callback(
        self, *, tool: Any, tool_args: dict[str, Any], tool_context: Any,
    ) -> Optional[dict[str, Any]]:
        return build_tool_args_parse_error_response(tool, tool_args)
