"""CC 式上下文压缩：阈值触发摘要 + compact boundary + 413 反应式恢复。

对应 CC 的 `autoCompact.ts` / `compact.ts` / `prompt.ts`（合计约 2400 行）的**最小完整版**：
保留形状与关键取舍，砍掉与本项目无关的部分。

做：
- 阈值 = 有效上下文窗口 − buffer（CC 用 13k）；
- 摘要 = 一次额外 LLM 调用，产出结构化要点，作为 boundary 消息接管早期历史；
- preserved tail = 保留最近若干个**原子单元**（不能从 call/response 中间切开）；
- 413 反应式恢复 = 模型报超长时压缩后重来一轮，而不是直接失败。

不做：microcompact（按 tool_use_id 的服务端缓存编辑）、snip、context-collapse、
post-compact 文件恢复——这些依赖 CC 特有的编码场景与服务端能力。

诚实边界：上游不返回 usage 时，token 数是**按字符估算**的，日志会标 ``estimated=true``。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

from agent.engine.native_loop.messages import (
    CHARS_PER_TOKEN,
    KIND_COMPACT_SUMMARY,
    Msg,
    Unit,
    Usage,
    atomic_units,
)
from agent.llm.chat import AgentChatClient
from common.obs import get_logger, log_kv

logger = get_logger("agent.native")

# 摘要本身要写得下 7 段结构化内容，512（AgentChatClient 默认值）必然被截断。
_SUMMARY_MAX_TOKENS = 4096
# 中文为主的语料，1 token ≈ 1.5 字符。刻意取偏小的除数 → 偏大的 token 估计 →
# 宁可早压一点，也不要因低估而撞上真正的 413。
_CHARS_PER_TOKEN = CHARS_PER_TOKEN
# 喂给摘要模型的历史文本上限，防止"为了压缩而先撑爆摘要请求"。
_HISTORY_RENDER_MAX_CHARS = 60_000
_PER_MESSAGE_RENDER_MAX_CHARS = 2_000

_SUMMARY_SYSTEM = (
    "你是对话历史压缩器。你的输出会**替换**掉这段历史，成为后续推理唯一能看到的上下文，"
    "因此必须完整保留继续完成任务所需的全部信息，宁可冗长也不要遗漏关键事实。\n"
    "先在 <analysis> 标签内梳理思路，再在 <summary> 标签内按下列七段输出：\n"
    "1. 用户的主要请求与意图：完整列出用户提出的所有请求，不要合并或概括掉细节。\n"
    "2. 关键概念与术语：本次对话涉及的领域概念、专有名词、约束条件。\n"
    "3. 已调用的工具与得到的结论：逐项列出调用过哪些工具、关键参数、返回的关键事实"
    "（尤其是检索到的资料内容与其序号，后续引用要用到）。\n"
    "4. 遇到的错误与修复：出现过的失败及其解决方式，特别是用户提出的纠正意见。\n"
    "5. 未完成的待办：用户明确要求但尚未完成的事项。\n"
    "6. 当前正在进行的工作：紧接这段历史之前正在做的事，尽可能具体。\n"
    "7. 下一步：仅当与用户最近一次明确请求直接相关时才写；否则写\"无\"。\n"
    "不要臆造历史中不存在的信息。"
)

_SUMMARY_PREFIX = "[早期对话已被压缩，以下是完整摘要，请把它当作已发生的事实继续推进任务]\n\n"


@dataclass
class CompactDecision:
    should: bool
    tokens: int
    threshold: int
    estimated: bool


def estimate_tokens(
    messages: list[Msg],
    last_usage: Optional[Usage],
    *,
    fixed_overhead_chars: int = 0,
) -> tuple[int, bool]:
    """估算当前上下文大小。优先用上游返回的真实 usage，否则按字符估算。

    ``fixed_overhead_chars`` 是每轮都会进请求、但不在 ``messages`` 里的部分
    （system 指令 + 全部工具的 JSON Schema）。不计它会系统性低估——当前工具面
    下这块就有约 1.8k token。

    返回 (tokens, estimated)。``estimated=True`` 表示纯字符估算，
    调用方在日志里如实标注——不要让人误以为是精确 token 计数。

    已知偏差（两个方向都存在，13k buffer 用于吸收）：
    - **偏高**：``apply_tool_result_budget`` 只作用于每轮请求的副本，
      历史里存的仍是全长 tool_result，这里按全长计；
    - **偏低**：字符→token 比按中文取值，英文占比高时实际 token 更少。
    """
    char_estimate = _char_tokens(messages, fixed_overhead_chars)
    if last_usage is not None and last_usage.prompt_tokens:
        # prompt_tokens 是"上一次请求"的输入规模；本轮又追加了 assistant + tool 消息，
        # 取二者较大者作为保守估计。注意压缩成功后调用方必须置空 last_usage
        # （见 NativeLoop._adopt_compacted），否则旧值会把估算顶回压缩前的水平。
        return max(int(last_usage.prompt_tokens), char_estimate), False
    return char_estimate, True


def _char_tokens(messages: list[Msg], fixed_overhead_chars: int = 0) -> int:
    total = fixed_overhead_chars
    for msg in messages:
        total += len(_render_content(msg.content))
        for call in msg.tool_calls or []:
            total += len(call.name) + len(call.arguments or "")
    return int(total / _CHARS_PER_TOKEN)


def decide(
    messages: list[Msg],
    last_usage: Optional[Usage],
    *,
    context_window_tokens: int,
    buffer_tokens: int,
    fixed_overhead_chars: int = 0,
) -> CompactDecision:
    tokens, estimated = estimate_tokens(
        messages, last_usage, fixed_overhead_chars=fixed_overhead_chars,
    )
    threshold = max(1, context_window_tokens - buffer_tokens)
    return CompactDecision(
        should=tokens >= threshold, tokens=tokens, threshold=threshold, estimated=estimated,
    )


def _render_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        # 多模态 block 数组：图片只留占位，摘要模型不需要（也不该）看到 base64。
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
            elif isinstance(block, dict) and block.get("type") == "image_url":
                parts.append("[图片]")
        return "\n".join(parts)
    if content is None:
        return ""
    return str(content)


def render_history(messages: list[Msg]) -> str:
    """把消息列表渲染成给摘要模型看的纯文本。"""
    lines: list[str] = []
    for msg in messages:
        if msg.role == "system":
            continue
        text = _render_content(msg.content)
        if len(text) > _PER_MESSAGE_RENDER_MAX_CHARS:
            text = text[:_PER_MESSAGE_RENDER_MAX_CHARS] + " …（本条已截断）"
        if msg.role == "assistant" and msg.tool_calls:
            calls = "；".join(
                f"{c.name}({(c.arguments or '')[:200]})" for c in msg.tool_calls
            )
            lines.append(f"[助手] {text}\n[发起工具调用] {calls}")
        elif msg.role == "tool":
            flag = "失败" if msg.is_error else "成功"
            lines.append(f"[工具结果 {msg.name} {flag}] {text}")
        else:
            lines.append(f"[{'用户' if msg.role == 'user' else '助手'}] {text}")
    rendered = "\n\n".join(lines)
    if len(rendered) > _HISTORY_RENDER_MAX_CHARS:
        # 头部最旧的内容优先丢：摘要的价值主要在近期上下文。
        rendered = "…（更早的历史因过长未纳入摘要）\n\n" + rendered[-_HISTORY_RENDER_MAX_CHARS:]
    return rendered


def _extract_summary(raw: str) -> str:
    """取 <summary> 段；模型没按格式输出时退回全文（宁可多留，不要丢信息）。"""
    lowered = raw.lower()
    start = lowered.find("<summary>")
    if start == -1:
        # 至少把 <analysis> 草稿段去掉，它是给模型自己整理思路用的。
        end_analysis = lowered.find("</analysis>")
        return raw[end_analysis + len("</analysis>"):].strip() if end_analysis != -1 else raw.strip()
    start += len("<summary>")
    end = lowered.find("</summary>", start)
    return (raw[start:end] if end != -1 else raw[start:]).strip()


def _preserved_start(messages: list[Msg], preserve_units: int) -> int:
    """算出保留尾部的起点下标。

    必须落在**原子单元边界**上：如果从 call/response 区间中间切开，
    剩下的孤立 tool 消息会让下一次请求被上游直接判 400。
    """
    units: list[Unit] = atomic_units(messages)
    if len(units) <= preserve_units:
        return units[0].start if units else 0
    return units[len(units) - preserve_units].start


async def compact(
    messages: list[Msg],
    chat: AgentChatClient,
    *,
    preserve_units: int,
    trigger: str,
) -> Optional[list[Msg]]:
    """执行一次压缩。成功返回新的消息列表，失败返回 None（调用方据此熔断）。"""
    start = _preserved_start(messages, preserve_units)
    to_summarize = messages[:start]
    preserved = messages[start:]
    if not to_summarize:
        # 保留段已经是全部历史 —— 说明单轮内容本身就超限，压缩帮不上忙。
        log_kv(logger, logging.WARNING, "Compact", "nothing to summarize, skip",
               trigger=trigger, total=len(messages), preserve_units=preserve_units)
        return None

    rendered = render_history(to_summarize)
    if not rendered.strip():
        return None

    try:
        raw = await chat.complete(
            system=_SUMMARY_SYSTEM,
            user=f"请压缩以下对话历史：\n\n{rendered}",
            # 摘要要求覆盖完整、不遗漏，所以给足输出预算（AgentChatClient 默认 512 会被截断）。
            max_tokens=_SUMMARY_MAX_TOKENS,
            temperature=0.1,
        )
    except Exception as exc:  # noqa: BLE001 - 压缩失败不应抛出，交由调用方熔断
        log_kv(logger, logging.ERROR, "Compact", "summarization failed",
               trigger=trigger, error=type(exc).__name__)
        return None

    summary = _extract_summary(raw)
    if not summary:
        log_kv(logger, logging.WARNING, "Compact", "empty summary, skip", trigger=trigger)
        return None

    boundary = Msg(
        role="user",
        content=_SUMMARY_PREFIX + summary,
        kind=KIND_COMPACT_SUMMARY,
    )
    log_kv(logger, logging.INFO, "Compact", "history compacted",
           trigger=trigger, summarized=len(to_summarize), preserved=len(preserved),
           summary_chars=len(summary))
    # 用 [摘要] + 保留尾部**替换**整段历史（CC 同样在压缩后释放前序消息）。
    # 代价是早期原文不再可回溯，收益是内存与上下文都被真正压下去。
    return [boundary, *preserved]
