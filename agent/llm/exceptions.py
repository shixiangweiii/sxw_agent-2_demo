"""LLM 调用异常分类（决定重试/降级策略）。"""
from __future__ import annotations

import litellm

CONTEXT_OVERFLOW = "context_overflow"
RATE_LIMIT = "rate_limit"
OTHER = "other"

# 上下文超长的判定关键词。
#
# 为什么必须靠关键词：下面两个 isinstance 分支只对 litellm 抛出的异常成立
# （litellm 的异常类继承自 openai 的，反向不成立）。`agent_loop` / `plan_execute`
# 走 ADK LiteLlm，能命中；`native_loop` 直接用 openai SDK，抛的是 openai.*Error，
# 判定会**完全落到这组关键词上**。关键词覆盖不到，整条"超长 → 压缩 → 重来一轮"
# 的恢复链路就一次都不会被触发。
#
# 因此这里按 provider 措辞尽量放宽。误判的代价是可控的：多做一次摘要压缩后重试，
# 若仍失败则真实错误照常上抛（反应式压缩有单次守卫）；而漏判的代价是恢复能力直接失效。
_OVERFLOW_KEYS = (
    # OpenAI / Azure
    "context length", "maximum context", "context_length_exceeded",
    "too long", "reduce the length", "exceeds the maximum",
    # DashScope / Qwen 及其它 OpenAI 兼容实现的常见措辞
    # 注：DashScope 超长时的确切报文尚未实测（需真实密钥），
    # 这里覆盖常见形态，并由 native 侧的体积兜底判据补漏（见 llm_client._classify）。
    "range of input length", "input length", "input is too long",
    "exceeds model", "exceed the model", "prompt is too long",
    "maximum input", "input tokens exceed",
    # 部分实现返回中文报文
    "输入长度", "上下文长度", "超过最大长度", "超出最大长度", "超过模型",
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
