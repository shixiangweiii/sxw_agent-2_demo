"""SSE 事件旁路：把统一事件流记进 root span，并给 done 事件补上 trace_id。

**这一层是引擎无关的**——它包在 `with_citations` 外面，只看统一 StreamEvent，
不知道背后是 plan_execute / agent_loop / native_loop。所以一处改动，三代引擎全覆盖，
且产出的信号天然对等（评测的失败归因规则正是要建立在这种对等字段上，
否则"谁埋点更深"会变成引擎对比里的假优势）。

**text 增量不逐条记 event**：一次回答几百个 delta，逐条记会把 span 撑爆且没有诊断价值。
改为累积成一个 payload + 计数 + TTFT——这恰好是评测需要的形态。
其余事件（tool_call / tool_result / plan_step / citation / skill_event / error）
逐条记录：它们才是"这一轮到底做了什么"的证据。
"""
from __future__ import annotations

import time
from typing import Any, AsyncIterator

from agent.stream.event_converters import StreamEvent
from common.obs import get_trace_id

# 逐条记录的事件类型（text 走累积，done 走收尾属性）
_RECORDED = frozenset({
    "tool_call", "tool_result", "plan_step", "citation", "skill_event", "error",
})


async def with_trace_tap(
    events: AsyncIterator[StreamEvent], span: Any,
) -> AsyncIterator[StreamEvent]:
    """透明中间件：原样放行全部事件，另外把它们记进 ``span``。

    ``span`` 可以是真 Span 也可以是 tracing 关闭时的空实现，调用方无需判断。
    """
    started = time.monotonic()
    text_parts: list[str] = []
    counts: dict[str, int] = {}
    ttft_ms: float | None = None
    had_error = False
    finished = False
    finish_reason: str | None = None

    try:
        async for ev in events:
            counts[ev.event] = counts.get(ev.event, 0) + 1

            if ev.event == "text":
                if ttft_ms is None:
                    ttft_ms = round((time.monotonic() - started) * 1000, 1)
                text_parts.append(str(ev.data.get("delta", "")))
            elif ev.event == "done":
                finished = True
                # native_loop 在收口时把 transition 名填进 finish_reason
                # （hard_cap / model_error / completed …），agent_loop 与 plan_execute
                # 填 "stop"。这是三代引擎唯一共有的"为什么结束"字段，收进 span 才能
                # 让评测的失败归因规则对三代通用。
                finish_reason = ev.data.get("finish_reason")
                # 把 trace_id 交给客户端：响应头虽然也有，但 SSE 客户端拿事件字段
                # 比拿 header 稳，评测据此建立 (engine, trace_id) 联查键。
                ev.data.setdefault("trace_id", get_trace_id())
            else:
                if ev.event == "error":
                    had_error = True
                if ev.event in _RECORDED:
                    span.add_event(ev.event, **_event_fields(ev))

            yield ev
    finally:
        # 必须在 finally：客户端断开时这段收尾属性同样要落进轨迹，
        # 否则被取消的运行在报告里会缺字段、与正常运行不可比。
        answer = "".join(text_parts)
        span.set(
            ttft_ms=ttft_ms,
            total_ms=round((time.monotonic() - started) * 1000, 1),
            answer_chars=len(answer),
            event_counts=counts or None,
            had_error=had_error,
            finished=finished,
            finish_reason=finish_reason,
        )
        if answer:
            span.set_payload("answer", answer)


def _event_fields(ev: StreamEvent) -> dict[str, Any]:
    """按事件类型挑出有诊断价值的字段（避免整包 data 灌进去）。"""
    data = ev.data or {}
    if ev.event == "tool_call":
        return {"tool": data.get("name"), "call_id": data.get("id"), "args": data.get("args")}
    if ev.event == "tool_result":
        return {"tool": data.get("name"), "call_id": data.get("id"),
                "response": data.get("response")}
    if ev.event == "citation":
        refs = data.get("refs") or []
        return {"doc_ids": [r.get("doc_id") for r in refs if isinstance(r, dict)]}
    if ev.event == "plan_step":
        return {"step": data.get("step"), "total": data.get("total"),
                "title": data.get("title"), "status": data.get("status")}
    if ev.event == "skill_event":
        return {"data_type": data.get("dataType"), "is_thinking": data.get("isThinking")}
    if ev.event == "error":
        return {"message": data.get("message")}
    return {"data": data}
