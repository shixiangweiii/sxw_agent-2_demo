"""native_loop 的流式模型客户端（OpenAI 兼容 / DashScope）。

这是自研循环里唯一直面线协议的地方，替代原来 ADK LiteLlm 代劳的部分。三件事：

1. **tool_calls 增量累积**——各家分片规则不一致（标准 OpenAI 首片带 id+name、
   后续片只追加 arguments 字符串；部分厂商一次性吐完整 tool_call）。累积器按
   ``index`` 聚合、``id``/``name`` 取首次非空值、``arguments`` 字符串累加，两种形态收敛到同一结果。
   实测形状见 ``scripts/probe_dashscope_tool_stream.py``。
2. **就绪信号**——为支持流式工具执行（CC 的 streaming tool execution），
   在更高 index 出现且已累积参数能解析成合法 JSON 时提前判定该调用就绪；
   否则一律等流结束再判定。JSON 可解析是这里的安全闸：半截参数解析不过，不会被误判就绪。
3. **异常分类**——上下文超长单独成类型，交给循环层触发压缩后重来（恢复优先于失败）。
"""
from __future__ import annotations

import json
import logging
from asyncio import CancelledError
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Optional

import openai

from agent.config import AgentSettings
from agent.engine.native_loop.messages import CHARS_PER_TOKEN, Msg, ToolCall, Usage, to_wire
from agent.llm.exceptions import CONTEXT_OVERFLOW, classify_llm_error
from common.obs import get_logger, log_kv
from common.trace import (
    KIND_LLM,
    STATUS_CANCELLED,
    STATUS_ERROR,
    STATUS_OK,
    close_span,
    open_span,
)

logger = get_logger("agent.native")

# 体积兜底判据的触发比例：请求字符数 ≥ 窗口 × 该比例时，把不认识的 400 当作超长。
# 取 0.9 而不是 1.0，是因为字符→token 估算本身有误差，留一点余量。
_OVERFLOW_SIZE_RATIO = 0.9


class NativeLlmError(RuntimeError):
    """模型调用失败（已分类）。"""

    def __init__(self, message: str, kind: str) -> None:
        super().__init__(message)
        self.kind = kind


class ContextOverflowError(NativeLlmError):
    """上下文超长。循环层据此触发反应式压缩并重来一轮，而不是直接失败。"""

    def __init__(self, message: str) -> None:
        super().__init__(message, CONTEXT_OVERFLOW)


# ── 流式事件 ───────────────────────────────────────────────────────────────

@dataclass
class TextDelta:
    """正文增量。前端打字机效果的来源。"""

    text: str


@dataclass
class ToolCallReady:
    """一次工具调用的参数已完整，可以投递执行。"""

    call: ToolCall


@dataclass
class TurnEnd:
    """本轮模型输出结束。"""

    finish_reason: Optional[str] = None
    usage: Optional[Usage] = None


StreamItem = TextDelta | ToolCallReady | TurnEnd


# ── tool_calls 累积器 ──────────────────────────────────────────────────────

@dataclass
class _PartialCall:
    index: int
    id: str = ""
    name: str = ""
    arguments: str = ""
    emitted: bool = False

    def merge(self, call_id: Any, name: Any, arguments: Any) -> None:
        # id/name 取首次非空值：标准分片下它们只在首片出现，
        # 后续片的空值不能把已拿到的值覆盖掉。
        if call_id and not self.id:
            self.id = str(call_id)
        if name and not self.name:
            self.name = str(name)
        if arguments:
            self.arguments += str(arguments)

    def is_parseable(self) -> bool:
        """参数能解析成 JSON 对象 —— 提前判定就绪的安全闸。"""
        text = self.arguments.strip()
        if not text:
            return False
        try:
            return isinstance(json.loads(text), dict)
        except (TypeError, ValueError):
            return False

    def to_call(self) -> ToolCall:
        return ToolCall(id=self.id, name=self.name, arguments=self.arguments)


class _ToolCallAccumulator:
    def __init__(self) -> None:
        self._calls: dict[int, _PartialCall] = {}
        self._max_index: int = -1

    def add(self, index: int, call_id: Any, name: Any, arguments: Any) -> None:
        partial = self._calls.get(index)
        if partial is None:
            partial = _PartialCall(index=index)
            self._calls[index] = partial
        if partial.emitted and arguments:
            # 已判定就绪后又收到该 index 的新分片：说明上游是交错分片，
            # 提前就绪的判断在这种形态下不成立。响亮记一条，便于排障。
            log_kv(logger, logging.WARNING, "NativeLlm",
                   "fragment arrived after tool call was emitted", index=index)
        partial.merge(call_id, name, arguments)
        self._max_index = max(self._max_index, index)

    def take_ready(self, *, allow_early: bool) -> list[ToolCall]:
        """取出可以投递的调用。

        allow_early=False 时永不提前，全部留到 ``take_remaining``。
        """
        if not allow_early:
            return []
        ready: list[ToolCall] = []
        for index in sorted(self._calls):
            partial = self._calls[index]
            if partial.emitted:
                continue
            # 仅当"已出现更高 index"且"参数已能解析"时才判定就绪。
            if index < self._max_index and partial.name and partial.is_parseable():
                partial.emitted = True
                ready.append(partial.to_call())
        return ready

    def take_remaining(self) -> list[ToolCall]:
        remaining: list[ToolCall] = []
        for index in sorted(self._calls):
            partial = self._calls[index]
            if partial.emitted:
                continue
            partial.emitted = True
            remaining.append(partial.to_call())
        return remaining


# ── 客户端 ─────────────────────────────────────────────────────────────────

class NativeLlmClient:
    """流式 OpenAI 兼容客户端。每进程一份，无请求级状态。"""

    def __init__(self, settings: AgentSettings) -> None:
        self._client = openai.AsyncOpenAI(
            api_key=settings.dashscope_api_key,
            base_url=settings.llm_base_url,
        )
        self._model = settings.llm_model
        # include_usage 不被上游支持时置 False 并整进程记住，避免每轮都试错一次。
        # 影响：compact 阈值只能回落到字符估算（见 compact.py，日志会标 estimated=true）。
        self._include_usage = True
        # 超长兜底判据用（见 _classify）：请求体积超过窗口的这个比例时，
        # 即使报文措辞不认识也按超长处理。
        self._overflow_chars_threshold = int(
            settings.context_window_tokens * CHARS_PER_TOKEN * _OVERFLOW_SIZE_RATIO,
        )

    @property
    def model(self) -> str:
        return self._model

    async def stream(
        self,
        *,
        messages: list[Msg],
        tools: Optional[list[dict[str, Any]]] = None,
        allow_early_tool_dispatch: bool = True,
        temperature: float = 0.2,
    ) -> AsyncIterator[StreamItem]:
        """流式调用模型，产出 TextDelta / ToolCallReady / TurnEnd。"""
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": to_wire(messages),
            "stream": True,
            "temperature": temperature,
            # 与 AgentChatClient / HardenedLiteLlm 一致：关掉 Qwen 思考过程，
            # 否则 thinking 文本会混进正文流、污染前端输出。
            "extra_body": {"enable_thinking": False},
        }
        if tools:
            payload["tools"] = tools
        if self._include_usage:
            payload["stream_options"] = {"include_usage": True}

        # M4 兜底判据用：本次请求的字符规模。上游超长报文的措辞因 provider 而异，
        # 关键词匹配可能漏判，此时用"请求体积是否已经逼近窗口"作为 provider 无关的旁证。
        request_chars = _payload_chars(payload)

        # 轨迹埋点放在这一层而不是循环里：主循环、Claude SKILL 子 Runner、
        # researcher 共用同一个客户端，埋在这里所有调用方一次性覆盖。
        # 与 ADK 侧 `adk.llm` span 的字段刻意对齐（model / tokens / finish_reason /
        # tool_calls），否则两代引擎的轨迹没法放在一张表里比。
        #
        # ★ 用 open_span 而不是 `with start_span`：后者会把 llm span 压进 contextvar，
        #   而**流式工具执行**恰恰是在本流的迭代过程中 `asyncio.create_task` 投递的
        #   （loop.py 的 ToolCallReady 分支），那些工具就会挂到 llm span 底下。
        #   工具不是模型调用的一部分——它是本轮 turn 的一部分。不压 contextvar，
        #   当前 span 就仍是 turn，工具因此挂在正确的位置，也与 ADK 侧的树形一致。
        span = open_span(
            "native.llm", KIND_LLM,
            model=self._model, messages=len(messages),
            tool_count=len(tools or []), request_chars=request_chars,
        )
        span.set_payload("messages", [m.to_wire() for m in messages])
        status = STATUS_OK
        try:
            emitted = False
            try:
                async for item in self._consume(payload, allow_early_tool_dispatch):
                    emitted = True
                    yield _observe(span, item)
                return
            except openai.BadRequestError as exc:
                # 只有"上游不认 stream_options"这一种情况值得降级重试；
                # 其余 400（比如消息结构非法）降级也救不回来，直接分类上抛。
                # emitted 守卫：一旦已经吐过内容就绝不重试，否则会重复输出。
                # stream_options 是在 create() 阶段被拒的，此时必然还没 emit 过。
                if emitted or not (self._include_usage and _mentions_stream_options(exc)):
                    raise self._classify(exc, request_chars) from exc
                self._include_usage = False
                payload.pop("stream_options", None)
                span.set(stream_options_downgraded=True)
                log_kv(logger, logging.WARNING, "NativeLlm",
                       "stream_options unsupported, retrying without usage")
            except Exception as exc:  # noqa: BLE001 - 统一分类后上抛
                raise self._classify(exc, request_chars) from exc

            # 降级重试（不带 usage）。这次失败不再兜底。
            try:
                async for item in self._consume(payload, allow_early_tool_dispatch):
                    yield _observe(span, item)
            except Exception as exc:  # noqa: BLE001
                raise self._classify(exc, request_chars) from exc
        except GeneratorExit:
            status = STATUS_CANCELLED
            raise
        except BaseException as exc:
            status = STATUS_CANCELLED if isinstance(exc, CancelledError) else STATUS_ERROR
            if status == STATUS_ERROR:
                span.set(error_type=type(exc).__name__, error=str(exc)[:500])
            raise
        finally:
            close_span(span, status)

    async def _consume(
        self, payload: dict[str, Any], allow_early: bool,
    ) -> AsyncIterator[StreamItem]:
        accumulator = _ToolCallAccumulator()
        finish_reason: Optional[str] = None
        usage: Optional[Usage] = None

        # AsyncStream 实测**没有 __del__**：客户端断开导致 CancelledError 穿过这里时，
        # 不显式关闭底层 HTTP 响应就不会归还给连接池。async with 保证任何退出路径都关。
        async with await self._client.chat.completions.create(**payload) as stream:
            async for chunk in stream:
                chunk_usage = getattr(chunk, "usage", None)
                if chunk_usage is not None:
                    usage = _to_usage(chunk_usage)

                choices = getattr(chunk, "choices", None) or []
                if not choices:
                    continue
                choice = choices[0]
                reason = getattr(choice, "finish_reason", None)
                if reason:
                    finish_reason = reason

                delta = getattr(choice, "delta", None)
                if delta is None:
                    continue

                content = getattr(delta, "content", None)
                if content:
                    yield TextDelta(content)

                for raw in getattr(delta, "tool_calls", None) or []:
                    index = getattr(raw, "index", None)
                    function = getattr(raw, "function", None)
                    accumulator.add(
                        0 if index is None else int(index),
                        getattr(raw, "id", None),
                        getattr(function, "name", None) if function else None,
                        getattr(function, "arguments", None) if function else None,
                    )
                for call in accumulator.take_ready(allow_early=allow_early):
                    yield ToolCallReady(call)

        for call in accumulator.take_remaining():
            yield ToolCallReady(call)
        yield TurnEnd(finish_reason=finish_reason, usage=usage)

    def _classify(self, exc: Exception, request_chars: int = 0) -> NativeLlmError:
        """异常分类。先用共享判据，再用 provider 无关的体积兜底。

        为什么需要兜底：``agent/llm/exceptions.py`` 的前两个分支是
        ``isinstance(exc, litellm.*)``，而本客户端直接用 openai SDK
        （litellm 的异常类继承自 openai 的，反向不成立），这两个分支恒为假，
        判定完全落在关键词上。关键词漏掉某个 provider 的措辞，整条
        "超长 → 压缩 → 重来一轮"的恢复链路就一次都不会触发。

        兜底判据：请求是 400（BadRequestError）**且**本次请求体积已经逼近上下文窗口——
        这种组合几乎只可能是超长。误判的代价是多做一次压缩后重试（有单次守卫），
        远小于漏判导致恢复能力完全失效。
        """
        kind = classify_llm_error(exc)
        message = f"{type(exc).__name__}: {exc}"
        if kind == CONTEXT_OVERFLOW:
            return ContextOverflowError(message)

        if (
            isinstance(exc, openai.BadRequestError)
            and request_chars >= self._overflow_chars_threshold > 0
        ):
            log_kv(logger, logging.WARNING, "NativeLlm",
                   "unrecognized 400 with oversized request, treating as context overflow",
                   request_chars=request_chars, threshold=self._overflow_chars_threshold)
            return ContextOverflowError(message)

        log_kv(logger, logging.ERROR, "NativeLlm", "model call failed",
               kind=kind, error=type(exc).__name__)
        return NativeLlmError(message, kind)


def _observe(span: Any, item: StreamItem) -> StreamItem:
    """把流经的项目记进 llm span，原样放行（不改变流的语义）。

    正文只累计字符数：逐个 delta 记 event 会把 span 撑爆，而"这轮说了多少话"
    才是和 tool_call 一起看的路由信号。
    """
    if isinstance(item, TextDelta):
        span.incr("text_chars", len(item.text))
    elif isinstance(item, ToolCallReady):
        span.append("tool_calls", item.call.name)
    elif isinstance(item, TurnEnd):
        span.set(finish_reason=item.finish_reason)
        if item.usage is not None:
            span.set(prompt_tokens=item.usage.prompt_tokens,
                     completion_tokens=item.usage.completion_tokens,
                     total_tokens=item.usage.total_tokens)
    return item


def _to_usage(raw: Any) -> Usage:
    return Usage(
        prompt_tokens=getattr(raw, "prompt_tokens", None),
        completion_tokens=getattr(raw, "completion_tokens", None),
        total_tokens=getattr(raw, "total_tokens", None),
    )


def _mentions_stream_options(exc: Exception) -> bool:
    return "stream_options" in str(exc).lower()


# 进程级共享客户端。``openai.AsyncOpenAI`` 内含 httpx 连接池且**没有 __del__**，
# 每次调用新建一个就等于每次泄漏一个连接池。主引擎、Claude SKILL 子 Runner、
# researcher 子代理共用同一个实例（客户端本身无请求级状态）。
_shared_client: Optional["NativeLlmClient"] = None


def get_shared_client(settings: AgentSettings) -> "NativeLlmClient":
    global _shared_client  # noqa: PLW0603 - 进程级单例的既定写法
    if _shared_client is None:
        _shared_client = NativeLlmClient(settings)
    return _shared_client


def _payload_chars(payload: dict[str, Any]) -> int:
    """本次请求进入模型的字符规模（消息 + 工具声明）。"""
    try:
        return len(json.dumps(payload.get("messages") or [], ensure_ascii=False)) + len(
            json.dumps(payload.get("tools") or [], ensure_ascii=False),
        )
    except (TypeError, ValueError):
        return 0


__all__ = [
    "ContextOverflowError",
    "get_shared_client",
    "NativeLlmClient",
    "NativeLlmError",
    "StreamItem",
    "TextDelta",
    "ToolCallReady",
    "TurnEnd",
]
