"""动态工具发现（progressive disclosure）：基础工具集之外的"延迟工具"按需经 tool_search 暴露。

简化说明：演示版把延迟工具也注册到 agent（保证可调用），由 tool_search 负责"发现/披露"；
生产中可结合 deferred-tool 机制真正按需挂载，避免基础 prompt 过长。
"""
from __future__ import annotations

from typing import Any, Callable

_CATALOG: dict[str, str] = {
    "translate": "把文本翻译到目标语言（参数 text, target_lang）",
    "text_stats": "统计文本的字符数与词数（参数 text）",
}


def translate(text: str, target_lang: str) -> dict[str, Any]:
    """把文本翻译到目标语言（demo：返回占位翻译）。

    Args:
        text: 原文。
        target_lang: 目标语言代码，如 "en" / "ja"。
    """
    return {"source": text, "target_lang": target_lang, "translation": f"[{target_lang}] {text}"}


def text_stats(text: str) -> dict[str, Any]:
    """统计文本的字符数与词数。

    Args:
        text: 待统计文本。
    """
    return {"chars": len(text), "words": len(text.split())}


def tool_search(query: str) -> dict[str, Any]:
    """按能力描述检索可用的专用工具（基础工具集之外的能力在此发现后即可调用）。

    Args:
        query: 想要的能力关键词，如 "翻译" / "统计"。
    """
    q = query.strip().lower()
    matches = {n: d for n, d in _CATALOG.items() if q and (q in n.lower() or q in d.lower())}
    return {
        "query": query,
        "available_tools": matches or _CATALOG,
        "note": "可直接按工具名调用上述工具完成任务。",
    }


def build_deferred_tools() -> list[Callable[..., Any]]:
    return [translate, text_stats]
