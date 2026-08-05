"""消息预算：按字符预算主动裁剪历史，防上下文膨胀（与 LiteLlm 的 reactive 截断互补）。"""
from __future__ import annotations

import json
import logging
from typing import Any

from common.obs import get_logger, log_kv

logger = get_logger("agent.loop")


def _json_chars(value: Any) -> int:
    """返回对象进入 LLM JSON 上下文后的近似字符数。"""
    try:
        return len(json.dumps(value, ensure_ascii=False, default=str))
    except (TypeError, ValueError, RecursionError):
        return len(str(value))


def _content_chars(content: Any) -> int:
    parts = getattr(content, "parts", None) or []
    total = 0
    for part in parts:
        total += len(getattr(part, "text", "") or "")
        function_call = getattr(part, "function_call", None)
        if function_call is not None:
            total += len(getattr(function_call, "name", "") or "")
            total += _json_chars(getattr(function_call, "args", None) or {})
        function_response = getattr(part, "function_response", None)
        if function_response is not None:
            total += len(getattr(function_response, "name", "") or "")
            total += _json_chars(getattr(function_response, "response", None))
    return total


def _tool_ids(content: Any, field: str) -> set[str]:
    ids: set[str] = set()
    for part in getattr(content, "parts", None) or []:
        value = getattr(part, field, None)
        value_id = getattr(value, "id", None) if value is not None else None
        if value_id:
            ids.add(str(value_id))
    return ids


def _atomic_content_ranges(contents: list[Any]) -> list[tuple[int, int]]:
    """把有关联 function_call/response 的消息区间合并为不可拆分单元。"""
    positions: dict[str, list[int]] = {}
    for index, content in enumerate(contents):
        for call_id in _tool_ids(content, "function_call"):
            positions.setdefault(call_id, []).append(index)
        for call_id in _tool_ids(content, "function_response"):
            positions.setdefault(call_id, []).append(index)

    linked_ranges = sorted(
        (min(indices), max(indices))
        for indices in positions.values()
        if len(set(indices)) > 1
    )
    merged: list[tuple[int, int]] = []
    for start, end in linked_ranges:
        if merged and start <= merged[-1][1]:
            previous_start, previous_end = merged[-1]
            merged[-1] = (previous_start, max(previous_end, end))
        else:
            merged.append((start, end))

    atomic: list[tuple[int, int]] = []
    index = 0
    linked_index = 0
    while index < len(contents):
        if linked_index < len(merged) and index == merged[linked_index][0]:
            start, end = merged[linked_index]
            atomic.append((start, end))
            index = end + 1
            linked_index += 1
        else:
            atomic.append((index, index))
            index += 1
    return atomic


def _unit_for_index(units: list[tuple[int, int]], index: int) -> int:
    for unit_index, (start, end) in enumerate(units):
        if start <= index <= end:
            return unit_index
    return 0


class MessageBudget:
    def __init__(self, max_chars: int = 24000, keep_recent: int = 30) -> None:
        self._max_chars = max_chars
        self._keep_recent = keep_recent

    def trim(self, llm_request: Any) -> None:
        contents = list(getattr(llm_request, "contents", None) or [])
        original = len(contents)
        if not contents:
            return

        # ADK/LiteLLM 通过 call id 配对工具调用与响应。先构造原子区间，
        # 再从区间边界裁剪，避免留下孤立的 function_call/function_response。
        units = _atomic_content_ranges(contents)
        first_unit = 0
        if len(contents) > self._keep_recent:
            desired_start = max(0, len(contents) - self._keep_recent)
            first_unit = _unit_for_index(units, desired_start)

        retained_start = units[first_unit][0]
        total = sum(_content_chars(content) for content in contents[retained_start:])
        while total > self._max_chars and first_unit < len(units) - 1:
            drop_start, drop_end = units[first_unit]
            remaining_count = len(contents) - drop_end - 1
            if remaining_count < 2:
                break
            total -= sum(_content_chars(content) for content in contents[drop_start:drop_end + 1])
            first_unit += 1
            retained_start = units[first_unit][0]

        retained = contents[retained_start:]
        if len(retained) != original:
            llm_request.contents = retained
            log_kv(logger, logging.INFO, "MessageBudget", "trimmed history",
                   before=original, after=len(retained))
