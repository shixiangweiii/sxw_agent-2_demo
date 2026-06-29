"""LLM 调用异常分类（决定重试/降级策略）。"""
from __future__ import annotations

import litellm

CONTEXT_OVERFLOW = "context_overflow"
RATE_LIMIT = "rate_limit"
OTHER = "other"

_OVERFLOW_KEYS = (
    "context length", "maximum context", "context_length_exceeded",
    "too long", "reduce the length", "exceeds the maximum",
)


def classify_llm_error(exc: Exception) -> str:
    if isinstance(exc, litellm.ContextWindowExceededError):
        return CONTEXT_OVERFLOW
    rate_err = getattr(litellm, "RateLimitError", None)
    if rate_err is not None and isinstance(exc, rate_err):
        return RATE_LIMIT
    msg = str(exc).lower()
    if any(k in msg for k in _OVERFLOW_KEYS):
        return CONTEXT_OVERFLOW
    if "rate limit" in msg or "429" in msg:
        return RATE_LIMIT
    return OTHER
