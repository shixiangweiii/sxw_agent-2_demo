"""ADK 2.3.0 工具参数适配。

ADK 在把 LiteLLM 消息转换成 ``types.FunctionCall`` 时要求 args 为对象；部分
模型会返回顶层数组或标量，导致 Pydantic 在插件回调前抛错。这里仅包装该
转换切面：正常对象保持原路径，异常形态转换成可被插件安全短路的 sentinel。
"""
from __future__ import annotations

import copy
import json
from functools import wraps
from importlib.metadata import version
from typing import Any

from agent.tool_args_contract import (
    TaskPlanArgumentsError,
    build_tool_args_parse_error,
    normalize_task_plan_args,
)

_INSTALL_MARKER = "__sxw_tool_args_normalizer_v1__"
_UNCHANGED = object()
_PLAN_TOOL_NAME = "update_task_plan"
_SUPPORTED_ADK_VERSION = "2.3.0"


def install_adk_tool_args_normalizer() -> None:
    """幂等安装 ADK 2.3.0 消息转换 shim。"""
    import google.adk.models.lite_llm as adk_lite_llm

    installed_version = version("google-adk")
    if installed_version != _SUPPORTED_ADK_VERSION:
        raise RuntimeError(
            "工具参数适配仅支持 google-adk=="
            f"{_SUPPORTED_ADK_VERSION}，当前为 {installed_version}"
        )

    required_names = (
        "_message_to_generate_content_response",
        "_split_message_content_and_tool_calls",
        "_parse_tool_call_arguments",
    )
    missing = [name for name in required_names if not callable(getattr(adk_lite_llm, name, None))]
    if missing:
        raise RuntimeError(
            "ADK LiteLlm 私有接口与当前适配不兼容，请检查 google-adk 版本："
            + ", ".join(missing)
        )

    current = adk_lite_llm._message_to_generate_content_response
    if getattr(current, _INSTALL_MARKER, False):
        return

    original = current
    split_message = adk_lite_llm._split_message_content_and_tool_calls
    parse_arguments = adk_lite_llm._parse_tool_call_arguments

    @wraps(original)
    def normalized_message_to_response(message: Any, *args: Any, **kwargs: Any) -> Any:
        content, tool_calls = split_message(message)
        replacements: dict[int, str] = {}
        for index, tool_call in enumerate(tool_calls):
            if getattr(tool_call, "type", None) != "function":
                continue
            function = getattr(tool_call, "function", None)
            replacement = _coerce_tool_arguments(
                getattr(function, "arguments", None),
                getattr(function, "name", ""),
                parse_arguments,
            )
            if replacement is not _UNCHANGED:
                replacements[index] = replacement

        if not replacements:
            return original(message, *args, **kwargs)

        # 只有异常消息才复制；同时实体化 inline-text fallback 的拆分结果，避免
        # original 再次从原文本生成一批 tool_calls 而丢失下面的参数替换。
        normalized_message = copy.deepcopy(message)
        normalized_tool_calls = copy.deepcopy(tool_calls)
        _set_value(normalized_message, "content", content)
        _set_value(normalized_message, "tool_calls", normalized_tool_calls)
        for index, replacement in replacements.items():
            function = getattr(normalized_tool_calls[index], "function", None)
            _set_value(function, "arguments", replacement)
        return original(normalized_message, *args, **kwargs)

    setattr(normalized_message_to_response, _INSTALL_MARKER, True)
    adk_lite_llm._message_to_generate_content_response = normalized_message_to_response


def _coerce_tool_arguments(
    raw_arguments: Any,
    tool_name: str,
    parse_arguments: Any,
) -> object | str:
    try:
        parsed = parse_arguments(raw_arguments)
    except (TypeError, ValueError) as exc:
        return _json_payload(build_tool_args_parse_error(type(exc).__name__))

    if isinstance(parsed, dict):
        return _UNCHANGED
    if tool_name == _PLAN_TOOL_NAME and isinstance(parsed, list):
        try:
            return _json_payload(normalize_task_plan_args(parsed))
        except TaskPlanArgumentsError as exc:
            return _json_payload(build_tool_args_parse_error(type(exc).__name__))
    return _json_payload(build_tool_args_parse_error("NonObjectToolArguments"))


def _json_payload(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _set_value(target: Any, key: str, value: Any) -> None:
    if target is None:
        raise RuntimeError(f"ADK tool call 缺少 {key} 容器")
    try:
        target[key] = value
    except (TypeError, AttributeError, KeyError):
        setattr(target, key, value)
