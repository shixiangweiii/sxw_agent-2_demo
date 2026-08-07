"""SSE 对话入口：POST /api/v1/chat/{agent_uuid}/stream。

multipart 表单：query（必填）+ user_id / session_id（多轮会话）+ image（多模态，M5）。
"""
from __future__ import annotations

import logging
from typing import AsyncIterator, Optional

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import StreamingResponse
from google.genai import types

from agent.artifacts.artifact_service import save_image_artifact
from agent.citation.citation_injector import with_citations
from agent.context import AgentContext
from agent.engine.base import RunContext, build_engine
from agent.session.session_service import APP_NAME
from agent.skills.request_context import (
    SkillRequestContext,
    reset_request_context,
    set_request_context,
)
from agent.stream.event_converters import StreamEvent, sse_format
from agent.stream.trace_tap import with_trace_tap
from common.obs import get_logger, log_kv
from common.trace import KIND_REQUEST, start_span

router = APIRouter(prefix="/api/v1", tags=["chat"])
logger = get_logger("agent.chat")


@router.post("/chat/{agent_uuid}/stream")
async def chat_stream(
    agent_uuid: str,
    request: Request,
    query: str = Form(...),
    user_id: str = Form("demo-user"),
    session_id: str = Form("demo-session"),
    image: Optional[UploadFile] = File(None),  # noqa: B008 - FastAPI 依赖注入惯用法
) -> StreamingResponse:
    # ── 主链路第 1 步：接住用户提问 ──────────────────────────────────────
    # ctx 是启动期装配好的运行时单例（LLM / 工具集 / 会话 / 制品），见 agent/main.py 的 lifespan。
    ctx: AgentContext = request.app.state.ctx

    # 多模态：文本 +（可选）图片。ADK LiteLlm 会把图片 inline_data 转成 base64 image_url 喂给视觉模型。
    # 这里构造的是 ADK/GenAI 的标准 Content 结构：一条消息由多个 Part 组成，文本和图片是并列的 Part。
    parts = [types.Part(text=query)]
    if image is not None:
        raw = await image.read()
        if raw:
            mime = image.content_type or "image/jpeg"
            parts.append(types.Part.from_bytes(data=raw, mime_type=mime))
            await save_image_artifact(
                ctx.artifact_service, app_name=APP_NAME, user_id=user_id,
                session_id=session_id, filename=image.filename or "upload",
                data=raw, mime_type=mime,
            )
            log_kv(logger, logging.INFO, "Multimodal", "image attached",
                   filename=image.filename, bytes=len(raw))
    user_message = types.Content(role="user", parts=parts)
    # ── 主链路第 2 步：准备会话与引擎 ────────────────────────────────────
    # 会话必须先存在，ADK Runner 才能把本轮事件（模型输出、工具调用、工具结果）追加进去；
    # 多轮对话的"记忆"就来自这个 session 里累积的历史事件（当前是 InMemory 实现）。
    await ctx.session_manager.get_or_create(user_id, session_id)
    # 按 ENGINE 配置选 Gen1(plan_execute) 或 Gen2(agent_loop)；两者实现同一个 ReasoningEngine 端口。
    # 注意：build_engine 放在 generator 外面，所以引擎构造失败会直接变成 HTTP 500，
    # 而不是先返回 200 再吐一个 error 事件——这两种失败形态对客户端是不同的。
    engine = build_engine(ctx)
    run_ctx = RunContext(
        agent_uuid=agent_uuid,
        user_id=user_id,
        session_id=session_id,
        user_message=user_message,
        settings=ctx.settings,
    )
    log_kv(logger, logging.INFO, "Chat", "request",
           agent=agent_uuid, engine=ctx.settings.engine, query=query)

    # ── 主链路第 3 步：把推理过程包装成 SSE 流 ───────────────────────────
    # 注意这是个异步生成器：函数体在 StreamingResponse 开始拉取时才真正执行，
    # 每 yield 一个字符串就是一帧 SSE 推给浏览器/curl。
    async def generator() -> AsyncIterator[str]:
        # 设置每请求技能执行上下文（供 SelectedSkillTool 构造 skill-center 执行上下文）
        # 用 contextvar 而不是参数透传的原因：工具是被 ADK 在很深的调用栈里执行的，
        # 中间隔着 Runner/Flow，没法把请求级信息（谁问的、哪个会话）当参数一路传下去。
        token = set_request_context(SkillRequestContext(
            agent_uuid=agent_uuid, user_id=user_id, session_id=session_id,
            text=query, user={"userId": user_id},
        ))
        try:
            # ★ root span 必须在这里进入——即在 merge_runner_events 起 pump task 之前。
            #   span 父子关系走 contextvar，而 asyncio.create_task 是在**创建那一刻**复制
            #   context 的；晚一步进入，引擎内部所有 span 就会挂在 root 之外（甚至无父）。
            with start_span(
                "chat.request", KIND_REQUEST,
                agent=agent_uuid, engine=ctx.settings.engine,
                user_id=user_id, session_id=session_id,
                has_image=image is not None,
            ) as root_span:
                root_span.set_payload("query", query)
                # 这一行是整条主链路的中枢，从里往外读：
                #   engine.run_stream(run_ctx) → 驱动推理，吐出统一 StreamEvent
                #   with_citations(...)        → 途中记录检索命中、扫描正文 [n]，在 done 前补 citation 事件
                #   with_trace_tap(...)        → 记进轨迹并给 done 补 trace_id；包在最外层才看得见 citation
                #   sse_format(se)             → StreamEvent → "event: xxx\ndata: {...}\n\n" 文本帧
                async for se in with_trace_tap(
                    with_citations(engine.run_stream(run_ctx)), root_span,
                ):
                    yield sse_format(se)
        except Exception as exc:  # noqa: BLE001 - 兜底为 error 事件，保证流可收口
            # 走到这里说明推理链路抛了未被下层接住的异常（如硬熔断 LlmCallsLimitExceededError）。
            # HTTP 200 已经发出去了，改不了状态码，所以只能以 error 事件形式告知客户端并正常收流。
            log_kv(logger, logging.ERROR, "Chat", "stream error", error=type(exc).__name__)
            yield sse_format(StreamEvent("error", {"message": str(exc)}))
        finally:
            # 必须还原 contextvar，否则会污染同一 task 后续逻辑（客户端断开走的也是这里）。
            reset_request_context(token)

    # 返回后 FastAPI 才开始迭代 generator；客户端断开会向生成器抛入取消，
    # 沿 with_citations → engine → Runner 一路传播，最终由技能/沙箱层做清理。
    return StreamingResponse(generator(), media_type="text/event-stream")
