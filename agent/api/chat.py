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
from common.obs import get_logger, log_kv

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
    ctx: AgentContext = request.app.state.ctx

    # 多模态：文本 +（可选）图片。ADK LiteLlm 会把图片 inline_data 转成 base64 image_url 喂给视觉模型。
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
    await ctx.session_manager.get_or_create(user_id, session_id)
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

    async def generator() -> AsyncIterator[str]:
        # 设置每请求技能执行上下文（供 SelectedSkillTool 构造 skill-center 执行上下文）
        token = set_request_context(SkillRequestContext(
            agent_uuid=agent_uuid, user_id=user_id, session_id=session_id,
            text=query, user={"userId": user_id},
        ))
        try:
            async for se in with_citations(engine.run_stream(run_ctx)):
                yield sse_format(se)
        except Exception as exc:  # noqa: BLE001 - 兜底为 error 事件，保证流可收口
            log_kv(logger, logging.ERROR, "Chat", "stream error", error=type(exc).__name__)
            yield sse_format(StreamEvent("error", {"message": str(exc)}))
        finally:
            reset_request_context(token)

    return StreamingResponse(generator(), media_type="text/event-stream")
