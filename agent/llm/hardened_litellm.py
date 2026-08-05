"""HardenedLiteLlm：ADK LiteLlm 的生产加固子类。

对应原项目 `qwen_lite_llm.py` + `llm_exception_handler.py`：
- M0（当前）：基础接通 DashScope（OpenAI 兼容端点，litellm `openai/` provider）。
- M3 填充：ContextOverflow 反应式截断重试、PromptCache(provider-aware)、异常分类。
- ADK 2.6.2 适配：FunctionCall 构造前规范化非对象工具参数，交由 Plugin 安全反馈。

为何用 LiteLlm 子类：模型调用层是做「上下文超长 / 限流 / 缓存断点」等生产加固的唯一切面，
ADK 把每次模型调用收敛到 `generate_content_async`，子类覆写即可全局生效。
"""
from __future__ import annotations

import logging
from typing import AsyncGenerator

from google.adk.models.lite_llm import LiteLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse

from agent.config import AgentSettings
from agent.llm.exceptions import CONTEXT_OVERFLOW, classify_llm_error
from agent.llm.tool_args_normalizer import install_adk_tool_args_normalizer
from common.obs import get_logger, log_kv

logger = get_logger("agent.llm")

# ContextOverflow 反应式截断后保留的最近 contents 条数
_TRUNCATE_KEEP = 10


def _truncate_contents(llm_request: LlmRequest) -> int:
    """丢弃最旧的历史，仅保留最近若干条，返回丢弃数量。"""
    contents = list(llm_request.contents or [])
    if len(contents) <= _TRUNCATE_KEEP:
        return 0
    dropped = len(contents) - _TRUNCATE_KEEP
    llm_request.contents = contents[-_TRUNCATE_KEEP:]
    return dropped


def _apply_prompt_cache(llm_request: LlmRequest, model: str) -> None:
    """PromptCache：provider-aware 缓存断点。

    Anthropic 支持 `cache_control` 断点；DashScope/Qwen（OpenAI 兼容）不支持显式断点，
    其上下文缓存为服务端隐式 → 此处对 Qwen **优雅降级为 no-op**（诚实，不谎称启用）。
    """
    if "anthropic" in model or "claude" in model:
        # 生产中在此对 system / tools 段插入 cache_control 断点。
        return
    # 非支持 provider：no-op。


class HardenedLiteLlm(LiteLlm):
    """生产加固版 LiteLlm：ContextOverflow 反应式截断重试 + 异常分类 + PromptCache(provider-aware)。"""

    # ★ ADK 循环里"调模型"这一步最终落到本方法（BaseLlmFlow 每轮都会调它一次）。
    # 覆写它 = 一处修改、全局生效，是做限流/缓存/超长等模型层加固的唯一切面。
    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False,
    ) -> AsyncGenerator[LlmResponse, None]:
        _apply_prompt_cache(llm_request, self.model)
        try:
            # 正常路径：直接透传父类的流式输出，逐块 yield 给上层（前端的打字机效果来源）。
            async for resp in super().generate_content_async(llm_request, stream=stream):
                yield resp
            return
        except Exception as exc:  # noqa: BLE001 - 需按类型决定重试/上抛
            # 异常分类决定策略：只有"上下文超长"值得截断重试，其余原样上抛
            # （上抛后由 Plugin / 引擎层决定是喂回模型还是终止）。
            kind = classify_llm_error(exc)
            if kind != CONTEXT_OVERFLOW:
                log_kv(logger, logging.ERROR, "LLM", "call failed", kind=kind, error=type(exc).__name__)
                raise
            dropped = _truncate_contents(llm_request)
            log_kv(logger, logging.WARNING, "ContextOverflow", "truncate and retry",
                   dropped=dropped, keep=_TRUNCATE_KEEP)
        # 截断后重试（上下文超长异常发生在请求发起前，不会产生重复输出）
        # 之所以敢重试而不担心内容重复：超长是在请求发出前就被判定的，此时一个 chunk 都还没 yield 出去。
        # 注意这次调用在 try 之外——若二次失败会直接上抛，最终表现为一个 error SSE 事件。
        async for resp in super().generate_content_async(llm_request, stream=stream):
            yield resp


def build_llm(settings: AgentSettings) -> HardenedLiteLlm:
    """按配置构造加固版 LiteLlm。

    litellm 通过 `openai/<model>` + `api_base` + `api_key` 接任意 OpenAI 兼容端点；
    这里指向 DashScope compatible-mode。
    """
    # 必须在构造模型之前装 shim：它给 ADK 的"LiteLLM 消息 → FunctionCall"转换切面打补丁，
    # 把模型偶发生成的非对象工具参数换成 sentinel，否则会在 Plugin 回调之前就抛异常、直接断 turn。
    # 该 shim 依赖 ADK 私有符号，版本不匹配时会启动失败（故意的：宁可响亮失败也不静默降级）。
    install_adk_tool_args_normalizer()
    # litellm 用 "openai/<model>" 前缀走 OpenAI 兼容 provider，配合 api_base 即可接任意兼容端点。
    model = f"openai/{settings.llm_model}"
    log_kv(logger, logging.INFO, "LLM", "build litellm",
           model=settings.llm_model, base_url=settings.llm_base_url)
    log_kv(logger, logging.INFO, "PromptCache", "provider-aware breakpoints: no-op for openai-compatible provider",
           model=settings.llm_model)
    return HardenedLiteLlm(
        model=model,
        api_base=settings.llm_base_url,
        api_key=settings.dashscope_api_key,
        # 经 litellm extra_body 透传 DashScope 的 enable_thinking=false：
        # 关掉推理模型的思考过程，避免 thinking 文本污染流式输出（已实测生效）。
        extra_body={"enable_thinking": False},
    )
