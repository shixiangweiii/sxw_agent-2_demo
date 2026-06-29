"""消息预算：按字符预算主动裁剪历史，防上下文膨胀（与 LiteLlm 的 reactive 截断互补）。"""
from __future__ import annotations

import logging
from typing import Any

from common.obs import get_logger, log_kv

logger = get_logger("agent.loop")


def _content_chars(content: Any) -> int:
    parts = getattr(content, "parts", None) or []
    return sum(len(getattr(p, "text", "") or "") for p in parts)


class MessageBudget:
    def __init__(self, max_chars: int = 24000, keep_recent: int = 30) -> None:
        self._max_chars = max_chars
        self._keep_recent = keep_recent

    def trim(self, llm_request: Any) -> None:
        contents = list(getattr(llm_request, "contents", None) or [])
        original = len(contents)
        if len(contents) > self._keep_recent:
            contents = contents[-self._keep_recent:]
        total = sum(_content_chars(c) for c in contents)
        while total > self._max_chars and len(contents) > 2:
            total -= _content_chars(contents.pop(0))
        if len(contents) != original:
            llm_request.contents = contents
            log_kv(logger, logging.INFO, "MessageBudget", "trimmed history",
                   before=original, after=len(contents))
