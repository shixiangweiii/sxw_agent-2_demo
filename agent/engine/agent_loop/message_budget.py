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


# 为什么必须有本函数：OpenAI 兼容协议要求 role=tool 的消息必须紧跟在带 tool_calls 的
# assistant 消息之后。如果裁剪时把 function_call 丢掉、只留下 function_response，
# 请求会被上游直接判 400。所以裁剪的最小单位不能是"一条消息"，
# 而必须是"一次完整的调用—响应区间"。
def _atomic_content_ranges(contents: list[Any]) -> list[tuple[int, int]]:
    """把有关联 function_call/response 的消息区间合并为不可拆分单元。"""
    # 第 1 步：记录每个 call id 出现在哪些消息下标上。
    # 正常情况下一个 id 会出现两次：发起调用的那条 + 返回结果的那条。
    positions: dict[str, list[int]] = {}
    for index, content in enumerate(contents):
        for call_id in _tool_ids(content, "function_call"):
            positions.setdefault(call_id, []).append(index)
        for call_id in _tool_ids(content, "function_response"):
            positions.setdefault(call_id, []).append(index)

    # 第 2 步：只有跨多条消息的 id 才形成"不可拆分区间"（首次出现 → 最后出现）。
    # len(set(...)) > 1 过滤掉调用和响应恰好在同一条消息里的退化情况。
    linked_ranges = sorted(
        (min(indices), max(indices))
        for indices in positions.values()
        if len(set(indices)) > 1
    )
    # 第 3 步：合并相互重叠的区间。同一轮并行发起多个工具调用时，
    # 各自的区间会互相交错，必须先并成一个大区间，否则仍可能从中间切开。
    merged: list[tuple[int, int]] = []
    for start, end in linked_ranges:
        if merged and start <= merged[-1][1]:
            previous_start, previous_end = merged[-1]
            merged[-1] = (previous_start, max(previous_end, end))
        else:
            merged.append((start, end))

    # 第 4 步：把整个消息列表切成一串连续单元——落在合并区间里的整段算一个单元，
    # 其余每条消息各自成一个单元。后续裁剪只允许以"单元"为粒度丢弃。
    atomic: list[tuple[int, int]] = []
    index = 0
    linked_index = 0
    while index < len(contents):
        if linked_index < len(merged) and index == merged[linked_index][0]:
            start, end = merged[linked_index]
            atomic.append((start, end))
            index = end + 1          # 整段跳过，保证不会从区间中间切开
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
        # 第 1 道：条数上限。想从"倒数第 keep_recent 条"开始保留，
        # 但该位置可能落在某个调用—响应区间中间，所以要回退到它所属单元的起点。
        first_unit = 0
        if len(contents) > self._keep_recent:
            desired_start = max(0, len(contents) - self._keep_recent)
            first_unit = _unit_for_index(units, desired_start)

        # 第 2 道：字符预算。从最旧的单元开始整块丢弃，直到总量降到预算内。
        retained_start = units[first_unit][0]
        total = sum(_content_chars(content) for content in contents[retained_start:])
        while total > self._max_chars and first_unit < len(units) - 1:
            drop_start, drop_end = units[first_unit]
            # 兜底：至少给模型留 2 条消息，否则会把当前这一轮也裁没、模型无从作答。
            remaining_count = len(contents) - drop_end - 1
            if remaining_count < 2:
                break
            total -= sum(_content_chars(content) for content in contents[drop_start:drop_end + 1])
            first_unit += 1
            retained_start = units[first_unit][0]

        # 只从头部截断、保留尾部：越新的消息对当前决策越重要。
        retained = contents[retained_start:]
        if len(retained) != original:
            llm_request.contents = retained
            log_kv(logger, logging.INFO, "MessageBudget", "trimmed history",
                   before=original, after=len(retained))
