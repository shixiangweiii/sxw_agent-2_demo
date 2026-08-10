"""Adapters from the three existing reasoning engines to EngineAdapter v1.

ADK Session and InMemoryArtifactService are created per attempt and discarded.
Canonical Runtime events are the only cross-attempt history source.
"""
from __future__ import annotations

import asyncio
from typing import Any

from google.adk.artifacts import InMemoryArtifactService
from google.adk.events import Event
from google.adk.sessions import InMemorySessionService
from google.genai import types

from agent.context import AgentContext
from agent.engine.base import RunContext, build_engine
from agent.runtime.domain.artifact import MAX_HTTP_RANGE_BYTES
from agent.runtime.domain.models import EngineOutcome, EngineOutcomeKind, EngineName, WorkingState
from agent.runtime.ports.artifact import ArtifactStore
from agent.runtime.ports.engine import EngineRunRequest, RuntimeIO
from agent.skills.request_context import (
    SkillRequestContext,
    reset_request_context,
    set_request_context,
)

APP_NAME = "sxw-agent"


def _broker_owns_tool_projection(event: Any, tool_broker: Any) -> bool:
    """Return whether a legacy Tool event is already a Broker-owned fact.

    ADK always prepares its aggregate ToolCall batch before callbacks run, so
    its unannotated projections belong to the Broker.  Native explicitly marks
    synthesized unknown/parse failures as engine-owned because no external
    dispatch or ToolExecution exists for them.
    """
    return (
        tool_broker is not None
        and event.event in {"tool_call", "tool_result"}
        and event.authority != "engine"
    )


class LegacyEngineAdapter:
    def __init__(
        self,
        *,
        engine: EngineName,
        context: AgentContext,
        release_fingerprint: str,
        artifact_store: ArtifactStore,
        artifact_metadata_loader: Any,
        tool_broker: Any,
    ) -> None:
        self.name = engine
        self.engine = engine
        self.context = context
        self.release_fingerprint = release_fingerprint
        self.artifact_store = artifact_store
        self.artifact_metadata_loader = artifact_metadata_loader
        self.tool_broker = tool_broker

    async def execute(self, request: EngineRunRequest, io: RuntimeIO) -> EngineOutcome:
        # InMemorySessionService 是 Google ADK（google.adk.sessions）提供的一个会话服务实现，
        # 顾名思义就是把会话（Session）状态保存在进程内存里，没有任何持久化落盘，进程重启或对象被丢弃后数据就丢失了
        # 这里用 InMemorySessionService 只是为了满足 ADK 的接口契约，把 canonical history 重放进去（第 74-81 行的 append_event 循环），让 ADK 引擎在这一次 attempt 内能看到完整上下文
        # 真正跨 attempt/可恢复的历史事实来源始终是数据库里的 committed events，
        # 这也是为什么 AGENTS.md 里强调"native 不得重新引入进程级历史 store"、ADK 的 session/artifact 适配器"必须每 attempt 创建并销毁"
        # "canonical history"（规范历史/权威历史）指的是从数据库里已提交事件重新编译出来的、作为事实来源的对话历史，区别于任何进程内、临时、可能不完整的历史副本。
        session_service = InMemorySessionService()
        session_id = request.envelope.run_id
        session = await session_service.create_session(
            app_name=APP_NAME,
            user_id=request.envelope.principal_id,
            session_id=session_id,
        )
        canonical_contents: list[types.Content] = []
        for item in request.history:
            role = str(item["role"])
            content = types.Content(role=role, parts=[types.Part(text=str(item.get("text", "")))])
            canonical_contents.append(content)
            await session_service.append_event(
                session,
                Event(author="user" if role == "user" else "assistant", content=content),
            )

        parts = [types.Part(text=request.input_text)]
        for artifact_id in request.envelope.attachment_refs:
            metadata = await self.artifact_metadata_loader(artifact_id)
            media_type = metadata.get("media_type") or "application/octet-stream"
            if media_type.startswith("image/"):
                raw = await self._read_all(
                    artifact_id,
                    total_size=int(metadata["size_bytes"]),
                )
                parts.append(types.Part.from_bytes(data=raw, mime_type=media_type))
            else:
                preview = await self.artifact_store.read_preview(artifact_id)
                suffix = "" if preview.end_exclusive >= preview.total_size else (
                    "\n[preview truncated; call read_artifact with an offset for more]"
                )
                parts.append(types.Part(text=(
                    f"[Attachment {artifact_id} media_type={media_type} "
                    f"bytes=0-{preview.end_exclusive}/{preview.total_size}]\n"
                    + preview.data.decode("utf-8", errors="replace")
                    + suffix
                )))
        user_message = types.Content(role="user", parts=parts)
        rc = RunContext(
            run_id=request.envelope.run_id,
            activity_id=request.activity_id,
            engine=self.engine,
            agent_uuid=request.envelope.agent_id,
            user_id=request.envelope.principal_id,
            session_id=session_id,
            user_message=user_message,
            settings=self.context.settings,
            canonical_history=tuple(canonical_contents), # "canonical history"（规范历史/权威历史）指的是从数据库里已提交事件重新编译出来的、作为事实来源的对话历史，区别于任何进程内、临时、可能不完整的历史副本。
            session_service=session_service,
            artifact_service=InMemoryArtifactService(),
            deadline_at_ms=request.envelope.deadline_at,
            tool_broker=self.tool_broker,
            fencing_token=request.fencing_token,
            release_fingerprint=request.envelope.release_fingerprint,
            runtime_io=io,
            engine_checkpoint=request.checkpoint,
            runtime_checkpoint_revision=request.checkpoint.revision if request.checkpoint else 0,
            runtime_working_state=(
                request.checkpoint.working_state
                if request.checkpoint
                else WorkingState(
                    goal=request.input_text,
                    release_fingerprint=request.envelope.release_fingerprint,
                )
            ),
        )
        engine = build_engine(self.context, self.engine)
        token = set_request_context(SkillRequestContext(
            agent_uuid=request.envelope.agent_id,
            user_id=request.envelope.principal_id,
            session_id=session_id,
            text=request.input_text,
            user={"userId": request.envelope.principal_id},
            run_id=request.envelope.run_id,
            activity_id=request.activity_id,
            deadline_at_ms=request.envelope.deadline_at,
            idempotency_key=request.envelope.idempotency_key,
        ))
        try:
            timeout_seconds = max(0.001, io.remaining_ms() / 1000)
            async with asyncio.timeout(timeout_seconds):
                async for event in engine.run_stream(rc):
                    # Tool Broker has already committed the authoritative call/result
                    # events for dispatched tools.  Native unknown-tool and argument-
                    # parse failures never enter the Broker, so their explicit
                    # ``authority=engine`` projections must still become durable facts.
                    # ADK projections have no hint and default to Broker authority.
                    if _broker_owns_tool_projection(event, self.tool_broker):
                        # Preserve model-stream order: a sub-2KiB text buffer must
                        # become durable before execution can commit the following
                        # ToolCall/ToolResult fact.
                        await io.force_flush()
                        if await io.is_cancelled():
                            return EngineOutcome(kind=EngineOutcomeKind.CANCELLED)
                        continue
                    await io.emit(event.event, event.data)
                    if await io.is_cancelled():
                        return EngineOutcome(kind=EngineOutcomeKind.CANCELLED)
            # The iterator carries event drafts only.  Exhaustion cannot prove
            # success: every real engine must record its explicit control result
            # in the attempt-local RunContext.  This also makes a future engine
            # regression that simply returns/EOFs fail closed.
            if rc.engine_outcome is None:
                return EngineOutcome(
                    kind=EngineOutcomeKind.TERMINAL_FAILURE,
                    error_code="ENGINE_OUTCOME_MISSING",
                    message="engine event stream ended without an explicit outcome",
                )
            return rc.engine_outcome
        except TimeoutError:
            return EngineOutcome(
                kind=EngineOutcomeKind.TERMINAL_FAILURE,
                error_code="DEADLINE_EXCEEDED",
                message="engine attempt exceeded the absolute Run deadline",
            )
        finally:
            reset_request_context(token)

    async def _read_all(self, artifact_id: str, *, total_size: int) -> bytes:
        """Materialize a verified image in bounded CAS reads for one attempt.

        ``read_range(start=..., end=None)`` deliberately rejects blobs larger
        than the public HTTP Range cap.  Supplying explicit bounded ranges keeps
        the same integrity check while allowing an uploaded image (up to the
        upload limit) to be reconstructed for the attempt-local ADK input.
        """
        if total_size < 0:
            raise ValueError("artifact metadata size cannot be negative")
        chunks: list[bytes] = []
        offset = 0
        while offset < total_size:
            part = await self.artifact_store.read_range(
                artifact_id,
                start=offset,
                end_exclusive=min(total_size, offset + MAX_HTTP_RANGE_BYTES),
            )
            chunks.append(part.data)
            offset = part.end_exclusive
        if offset != total_size:
            raise RuntimeError("artifact metadata size changed during materialization")
        return b"".join(chunks)
