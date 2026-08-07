#!/usr/bin/env python
"""P0 探针：把 DashScope(OpenAI 兼容) 流式 tool_calls 的真实线形状打出来。

自研 agent-loop 的第一个坑就是"模型怎么把工具调用切成流式分片"——各家实现不一致：
标准 OpenAI 是首片带 index+id+name、后续片只追加 arguments 字符串；
部分厂商则一次性吐完整 tool_call。累积器必须两种都吃，所以先实测一次再动手写。

用法（密钥只从环境变量读，脚本不落盘、不打印密钥）：
    export DASHSCOPE_API_KEY=sk-***
    .venv/bin/python scripts/probe_dashscope_tool_stream.py

模型 / base_url 复用项目配置（AgentSettings），可用 LLM_MODEL / LLM_BASE_URL 覆盖。
本脚本是一次性诊断工具，不参与服务运行。
"""
from __future__ import annotations

import asyncio
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import openai  # noqa: E402

from agent.config import AgentSettings  # noqa: E402

# 探针用的工具声明。故意给两个"彼此独立"的工具，方便观察模型在一轮里
# 并行发起多个 tool_call 时 index / id 是怎么分配的。
_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "calculator",
            "description": "计算一个数学算术表达式并返回结果。",
            "parameters": {
                "type": "object",
                "properties": {
                    "expression": {
                        "type": "string",
                        "description": '形如 "2*(3+4)" 的纯算术表达式。',
                    },
                },
                "required": ["expression"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "查询指定城市的天气。",
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {"type": "string", "description": '城市名，如 "杭州"。'},
                },
                "required": ["city"],
            },
        },
    },
]

_CASES: list[tuple[str, str, bool]] = [
    # (用例名, 用户消息, 是否带工具)
    ("single_tool", "3*(4+5) 等于多少？请用工具计算。", True),
    ("parallel_tools", "同时做两件事：算一下 12*12，并查询杭州的天气。", True),
    ("text_only", "用一句话解释什么是向量检索。", False),
]


class _ToolCallTrace:
    """累积一个 index 位上的所有分片，用于事后判定线形状。"""

    def __init__(self) -> None:
        self.fragments: int = 0
        self.id_seen_at: list[int] = []        # id 出现在第几个分片
        self.name_seen_at: list[int] = []      # name 出现在第几个分片
        self.arg_chunks: list[str] = []        # 每个分片带来的 arguments 片段

    def add(self, call_id: Any, name: Any, arguments: Any) -> None:
        self.fragments += 1
        if call_id:
            self.id_seen_at.append(self.fragments)
        if name:
            self.name_seen_at.append(self.fragments)
        if arguments is not None:
            self.arg_chunks.append(str(arguments))

    @property
    def joined_arguments(self) -> str:
        return "".join(self.arg_chunks)


async def _probe_case(
    client: openai.AsyncOpenAI,
    model: str,
    name: str,
    prompt: str,
    with_tools: bool,
    include_usage: bool,
) -> None:
    print(f"\n{'=' * 78}\n[case] {name}  tools={with_tools}  include_usage={include_usage}")
    print(f"[prompt] {prompt}")

    kwargs: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": True,
        # 与 AgentChatClient / HardenedLiteLlm 一致：关掉 Qwen 思考过程，
        # 否则 thinking 文本会混进正文流。
        "extra_body": {"enable_thinking": False},
    }
    if with_tools:
        kwargs["tools"] = _TOOLS
    if include_usage:
        # 压缩阈值想用真实 token 数就依赖这个；不被支持时上层回落到字符估算。
        kwargs["stream_options"] = {"include_usage": True}

    traces: dict[int, _ToolCallTrace] = defaultdict(_ToolCallTrace)
    text_parts: list[str] = []
    finish_reasons: list[str] = []
    usage_payload: Any = None
    chunk_index = 0

    stream = await client.chat.completions.create(**kwargs)
    async for chunk in stream:
        chunk_index += 1
        # usage 通常只在最后一个（choices 为空的）chunk 上出现。
        if getattr(chunk, "usage", None):
            usage_payload = chunk.usage

        choices = getattr(chunk, "choices", None) or []
        if not choices:
            print(f"  #{chunk_index:03d} <no choices>  usage={_brief(usage_payload)}")
            continue

        choice = choices[0]
        delta = getattr(choice, "delta", None)
        finish_reason = getattr(choice, "finish_reason", None)
        if finish_reason:
            finish_reasons.append(finish_reason)

        content = getattr(delta, "content", None) if delta else None
        if content:
            text_parts.append(content)

        tool_calls = (getattr(delta, "tool_calls", None) if delta else None) or []
        for tc in tool_calls:
            idx = getattr(tc, "index", None)
            idx = 0 if idx is None else int(idx)
            fn = getattr(tc, "function", None)
            call_id = getattr(tc, "id", None)
            fn_name = getattr(fn, "name", None) if fn else None
            fn_args = getattr(fn, "arguments", None) if fn else None
            traces[idx].add(call_id, fn_name, fn_args)
            print(
                f"  #{chunk_index:03d} tool_call idx={idx} "
                f"type={getattr(tc, 'type', None)!r} id={_mask_id(call_id)!r} "
                f"name={fn_name!r} arguments={fn_args!r}"
            )

        if content:
            print(f"  #{chunk_index:03d} text {content!r}")
        if finish_reason:
            print(f"  #{chunk_index:03d} finish_reason={finish_reason!r}")

    _report(name, traces, text_parts, finish_reasons, usage_payload, chunk_index)


def _report(
    name: str,
    traces: dict[int, _ToolCallTrace],
    text_parts: list[str],
    finish_reasons: list[str],
    usage_payload: Any,
    chunk_count: int,
) -> None:
    print(f"\n  ---- 结论 [{name}] ----")
    print(f"  chunks={chunk_count}  finish_reasons={finish_reasons}")
    print(f"  usage_returned={usage_payload is not None}  {_brief(usage_payload)}")
    if text_parts:
        joined = "".join(text_parts)
        print(f"  text_deltas={len(text_parts)} total_chars={len(joined)}")
    if not traces:
        print("  tool_calls=0（本用例没有工具调用）")
        return

    print(f"  tool_calls={len(traces)}（按 index 聚合）")
    for idx in sorted(traces):
        t = traces[idx]
        args = t.joined_arguments
        try:
            parsed = json.loads(args) if args else None
            parse_note = f"json_ok type={type(parsed).__name__}"
        except (TypeError, ValueError) as exc:
            parse_note = f"json_FAIL {type(exc).__name__}"
        # 这三行是本次探针真正要的答案：
        #   fragments==1 → 一次性完整返回；>1 → 标准分片，累积器必须拼 arguments
        #   id/name 只在首片出现 → 累积器要"取首次非空值"而不是"取最后一个"
        print(
            f"    idx={idx} fragments={t.fragments} "
            f"id_at={t.id_seen_at} name_at={t.name_seen_at} "
            f"arg_fragments={len(t.arg_chunks)}"
        )
        print(f"      joined_arguments={args!r}  → {parse_note}")

    fragmented = any(t.fragments > 1 for t in traces.values())
    print(
        "  → 线形状："
        + ("标准分片（arguments 需跨 chunk 拼接）" if fragmented else "一次性完整返回")
    )


def _mask_id(value: Any) -> Any:
    """call id 不敏感，但保持短输出便于阅读。"""
    if isinstance(value, str) and len(value) > 12:
        return value[:8] + "…"
    return value


def _brief(usage: Any) -> str:
    if usage is None:
        return ""
    fields = ("prompt_tokens", "completion_tokens", "total_tokens")
    parts = [f"{f}={getattr(usage, f, None)}" for f in fields]
    return " ".join(parts)


async def main() -> int:
    settings = AgentSettings()
    if not settings.dashscope_api_key or settings.dashscope_api_key.startswith("sk-***"):
        print(
            "缺少 DASHSCOPE_API_KEY。请在 shell 中注入真实密钥后重试：\n"
            "    export DASHSCOPE_API_KEY=sk-***\n"
            "（密钥只允许来自环境变量或被 Git 忽略的本地 .env，切勿写入代码或文档）",
            file=sys.stderr,
        )
        return 1

    print(f"model={settings.llm_model}  base_url={settings.llm_base_url}")
    client = openai.AsyncOpenAI(
        api_key=settings.dashscope_api_key,
        base_url=settings.llm_base_url,
    )

    # 先试 include_usage；被拒就整轮回落，并明确告诉我们压缩阈值只能用字符估算。
    include_usage = True
    try:
        await _probe_case(client, settings.llm_model, *_CASES[0], include_usage=True)
    except openai.BadRequestError as exc:
        include_usage = False
        print(f"\n[warn] stream_options.include_usage 被拒绝（{type(exc).__name__}），"
              f"改为不带 usage 重跑；compact 阈值将只能用字符估算。")
        await _probe_case(client, settings.llm_model, *_CASES[0], include_usage=False)

    for case in _CASES[1:]:
        await _probe_case(client, settings.llm_model, *case, include_usage=include_usage)

    print(f"\n{'=' * 78}\n探针完成。累积器实现依据：")
    print("  1) arguments 是否需要跨 chunk 拼接（fragments > 1）")
    print("  2) id/name 是否只在首片出现（→ 取首次非空值）")
    print("  3) 并行调用是否靠 index 区分")
    print("  4) usage 是否随流返回（→ compact 阈值用真实 token 还是字符估算）")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
