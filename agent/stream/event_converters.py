"""ADK Event → 统一 SSE 流事件。

SSE 事件类型：text | tool_call | tool_result | plan_step | citation | done | error
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass
class StreamEvent:
    event: str
    data: dict[str, Any] = field(default_factory=dict)


def sse_format(ev: StreamEvent) -> str:
    """序列化为 SSE 帧：`event: <type>\\ndata: <json>\\n\\n`。"""
    return f"event: {ev.event}\ndata: {json.dumps(ev.data, ensure_ascii=False)}\n\n"


def _json_safe(obj: Any) -> Any:
    try:
        json.dumps(obj, ensure_ascii=False)
        return obj
    except (TypeError, ValueError):
        return str(obj)


def adk_event_to_stream_events(event: Any) -> list[StreamEvent]:
    """把单个 ADK Event 翻译为零或多个 SSE 流事件。

    文本只取流式增量（``event.partial`` 为真）；最终聚合文本跳过以免重复。
    工具调用 / 工具结果出现在非流式事件里，原样转出。
    """
    out: list[StreamEvent] = []
    content = getattr(event, "content", None)
    parts = getattr(content, "parts", None) if content else None
    if not parts:
        return out           # 纯控制类事件（无内容）不产生 SSE
    # partial=True 表示这是流式增量块；ADK 在一轮结束时还会再发一个 partial=False 的聚合事件。
    is_partial = bool(getattr(event, "partial", False))
    for part in parts:
        text = getattr(part, "text", None)
        # 只取增量、丢弃聚合：否则用户会先看到逐字输出、末尾再被完整重复一遍。
        if text and is_partial:
            out.append(StreamEvent("text", {"delta": text}))
        fc = getattr(part, "function_call", None)
        if fc is not None:
            # 工具调用/结果只出现在非流式事件里，原样转出即可。
            # id 是 ADK 分配的 function call id，前端用它把同名工具的多次并行调用区分开。
            out.append(StreamEvent("tool_call", {
                "id": getattr(fc, "id", None),
                "name": getattr(fc, "name", ""),
                "args": dict(getattr(fc, "args", None) or {}),
            }))
        fr = getattr(part, "function_response", None)
        if fr is not None:
            out.append(StreamEvent("tool_result", {
                "id": getattr(fr, "id", None),
                "name": getattr(fr, "name", ""),
                "response": _json_safe(getattr(fr, "response", None)),
            }))
    return out
