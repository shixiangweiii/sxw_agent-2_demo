"""唯一 current Native kernel checkpoint codec。

本模块刻意不提供版本 union、默认值猜测或 upgrader。只要数据库里存在 native
checkpoint，它就必须完整满足当前 codec；否则恢复失败关闭，不能退回 canonical
history 重跑一个已经 accepted 的 durable Run。
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from agent.engine.native_loop.loop import LoopState
from agent.engine.native_loop.messages import Msg, ToolCall, Usage
from agent.runtime.domain.errors import RuntimeFault
from agent.runtime.domain.models import canonical_json


NATIVE_CHECKPOINT_CONTRACT = "native-kernel-current"
NativeCheckpointPhase = Literal[
    "MODEL_REQUEST",
    "MODEL_RESPONSE_COMMITTED",
    "TOOL_BATCH_COMMITTED",
    "TOOL_RESULT_COMMITTED",
    "NEXT_TURN",
    "COMPLETED",
]
GenerationReason = Literal[
    "initial",
    "next_turn",
    "retry",
    "recovery",
    "reactive_compact",
]
ContentSource = Literal["INLINE", "CURRENT_INPUT", "LEDGER_RESULT"]


class NativeToolCallState(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: str = Field(min_length=1, max_length=512)
    name: str = Field(min_length=1, max_length=256)
    arguments: str
    logical_key: str = Field(min_length=1, max_length=512)


class NativeMessageState(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    role: Literal["user", "assistant", "tool"]
    content: Any
    content_source: ContentSource
    tool_calls: list[NativeToolCallState]
    tool_call_id: str | None
    name: str | None
    is_error: bool
    kind: Literal["normal", "compact_summary", "meta"]
    tool_execution_id: str | None

    @model_validator(mode="after")
    def validate_role_contract(self) -> "NativeMessageState":
        if self.role == "assistant":
            if self.tool_call_id is not None or self.name is not None:
                raise ValueError("assistant message cannot carry tool result identity")
            if self.content_source != "INLINE" or self.tool_execution_id is not None:
                raise ValueError("assistant content must be inline")
            if self.content is not None and not isinstance(self.content, str):
                raise ValueError("assistant content must be text or null")
        elif self.role == "tool":
            if not self.tool_call_id or not self.name:
                raise ValueError("tool message requires tool_call_id and name")
            if self.tool_calls:
                raise ValueError("tool message cannot carry tool_calls")
            if self.content_source == "CURRENT_INPUT":
                raise ValueError("tool message cannot reference current input")
            if self.content_source == "LEDGER_RESULT":
                if not self.tool_execution_id:
                    raise ValueError("ledger result requires tool_execution_id")
                if self.content is not None:
                    raise ValueError("ledger result content must not be copied into checkpoint")
            elif self.tool_execution_id is not None and self.content_source != "INLINE":
                raise ValueError("tool_execution_id has an invalid content source")
            if self.content is not None and not isinstance(self.content, str):
                raise ValueError("tool content must be text or a ledger reference")
        else:
            if self.tool_calls or self.tool_call_id is not None or self.name is not None:
                raise ValueError(f"{self.role} message cannot carry tool call identity")
            if self.tool_execution_id is not None:
                raise ValueError(f"{self.role} message cannot carry tool execution identity")
            if self.content_source == "LEDGER_RESULT":
                raise ValueError("only tool messages may reference ledger results")
            if self.content_source == "CURRENT_INPUT" and self.role != "user":
                raise ValueError("only a user message may reference current input")
            if self.content_source == "INLINE" and not isinstance(self.content, str):
                raise ValueError("user content must be text unless it references current input")

        if self.content_source == "CURRENT_INPUT" and self.content is not None:
            raise ValueError("current input bytes must not be copied into checkpoint")
        try:
            canonical_json(self.content)
        except (TypeError, ValueError) as exc:
            raise ValueError("message content must be deterministic JSON") from exc
        return self


class NativeUsageState(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    prompt_tokens: int | None = Field(ge=0)
    completion_tokens: int | None = Field(ge=0)
    total_tokens: int | None = Field(ge=0)
    extra: dict[str, Any]


class NativeCheckpointState(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    contract: Literal["native-kernel-current"]
    phase: NativeCheckpointPhase
    iters: int = Field(ge=0)
    model_call_count: int = Field(ge=0)
    tool_call_count: int = Field(ge=0)
    transition: str | None
    attempted_reactive_compact: int = Field(ge=0)
    compact_failures: int = Field(ge=0)
    compact_cooldown: int = Field(ge=0)
    last_usage: NativeUsageState | None
    tool_state: dict[str, Any]
    messages: list[NativeMessageState]
    generation_counter: int = Field(ge=0)
    current_message_id: str = Field(min_length=1, max_length=512)
    current_generation_id: str = Field(min_length=1, max_length=512)
    supersedes_generation_id: str | None
    generation_reason: GenerationReason
    final_text: str | None
    final_message_id: str | None
    final_generation_id: str | None

    @model_validator(mode="after")
    def validate_kernel_invariants(self) -> "NativeCheckpointState":
        if not self.messages:
            raise ValueError("native checkpoint requires at least one message")
        if self.iters < 1 or self.model_call_count < 1 or self.generation_counter < 1:
            raise ValueError("persisted phases require a reserved model generation")
        if self.model_call_count != self.generation_counter:
            raise ValueError("model call and generation counters must advance together")
        if (
            self.current_message_id == "uninitialized"
            or self.current_generation_id == "uninitialized"
        ):
            raise ValueError("persisted phases require current generation identity")
        visible_tool_calls = sum(
            len(message.tool_calls) for message in self.messages if message.role == "assistant"
        )
        if self.tool_call_count < visible_tool_calls:
            raise ValueError("cumulative ToolCall count is smaller than visible history")
        logical_keys = [
            call.logical_key
            for message in self.messages
            for call in message.tool_calls
        ]
        if len(logical_keys) != len(set(logical_keys)):
            raise ValueError("ToolCall logical keys must be unique")
        try:
            canonical_json(self.tool_state)
            if self.last_usage is not None:
                canonical_json(self.last_usage.extra)
        except (TypeError, ValueError) as exc:
            raise ValueError("kernel state must be deterministic JSON") from exc
        _validate_message_sequence(self.messages, self.phase)
        if self.phase == "COMPLETED":
            if not self.final_text or not self.final_text.strip():
                raise ValueError("COMPLETED requires non-empty final_text")
            if not self.final_message_id or not self.final_generation_id:
                raise ValueError("COMPLETED requires final generation identity")
            if not self.messages:
                raise ValueError("COMPLETED requires a final assistant message")
            final = self.messages[-1]
            if (
                final.role != "assistant"
                or final.tool_calls
                or not isinstance(final.content, str)
                or final.content != self.final_text
                or self.final_message_id != self.current_message_id
                or self.final_generation_id != self.current_generation_id
            ):
                raise ValueError("COMPLETED final assistant does not match generation state")
            if self.transition != "completed":
                raise ValueError("COMPLETED requires transition=completed")
        elif any(
            value is not None
            for value in (self.final_text, self.final_message_id, self.final_generation_id)
        ):
            raise ValueError("only COMPLETED may persist final assistant fields")
        return self


def _validate_message_sequence(
    messages: list[NativeMessageState],
    phase: NativeCheckpointPhase,
) -> None:
    seen_calls: set[str] = set()
    pending: list[NativeToolCallState] = []
    answered = 0
    batch_assistant_index: int | None = None

    for index, message in enumerate(messages):
        if pending:
            if message.role != "tool":
                raise ValueError("an incomplete ToolCall batch cannot be followed by another message")
            expected = pending[answered]
            if message.tool_call_id != expected.id or message.name != expected.name:
                raise ValueError("ToolResult order/name does not match the complete ToolCall batch")
            answered += 1
            if answered == len(pending):
                pending = []
                answered = 0
                batch_assistant_index = None
            continue

        if message.role == "tool":
            raise ValueError("orphan ToolResult without an assistant ToolCall batch")
        if message.role == "assistant" and message.tool_calls:
            ids = [call.id for call in message.tool_calls]
            if len(ids) != len(set(ids)) or any(call_id in seen_calls for call_id in ids):
                raise ValueError("duplicate ToolCall id")
            seen_calls.update(ids)
            pending = list(message.tool_calls)
            answered = 0
            batch_assistant_index = index

    incomplete = bool(pending)
    if incomplete:
        allowed = {
            "MODEL_RESPONSE_COMMITTED",
            "TOOL_BATCH_COMMITTED",
            "TOOL_RESULT_COMMITTED",
        }
        if phase not in allowed:
            raise ValueError(f"{phase} cannot contain an incomplete ToolCall batch")
        assert batch_assistant_index is not None
        result_count = len(messages) - batch_assistant_index - 1
        if phase == "MODEL_RESPONSE_COMMITTED" and result_count != 0:
            raise ValueError("MODEL_RESPONSE_COMMITTED cannot contain ToolResults")
        if phase == "TOOL_BATCH_COMMITTED" and result_count != 0:
            raise ValueError("TOOL_BATCH_COMMITTED cannot contain ToolResults")
        if phase == "TOOL_RESULT_COMMITTED" and result_count <= 0:
            raise ValueError("TOOL_RESULT_COMMITTED requires at least one ToolResult")

    tail = messages[-1]
    if phase == "MODEL_REQUEST":
        # The response for this reserved provider slot has not been accepted yet.
        # A persisted assistant tail would silently turn a half-committed response
        # into a second model request on recovery.
        if tail.role not in {"user", "tool"}:
            raise ValueError("MODEL_REQUEST cannot contain the current assistant response")
    elif phase == "MODEL_RESPONSE_COMMITTED":
        if tail.role != "assistant":
            raise ValueError("MODEL_RESPONSE_COMMITTED requires the current assistant response")
        if not tail.tool_calls and (
            not isinstance(tail.content, str) or not tail.content.strip()
        ):
            raise ValueError(
                "MODEL_RESPONSE_COMMITTED requires non-empty text or a ToolCall batch"
            )
    elif phase == "TOOL_BATCH_COMMITTED":
        if tail.role != "assistant" or not tail.tool_calls:
            raise ValueError("TOOL_BATCH_COMMITTED requires a ToolCall batch at the tail")
    elif phase == "TOOL_RESULT_COMMITTED":
        if tail.role != "tool":
            raise ValueError("TOOL_RESULT_COMMITTED requires a ToolResult at the tail")
    elif phase == "NEXT_TURN":
        if tail.role != "tool":
            raise ValueError("NEXT_TURN requires a fully paired ToolResult batch")


def _message_to_state(message: Msg, *, large_result_bytes: int) -> NativeMessageState:
    content = message.content
    source: ContentSource = "INLINE"
    if isinstance(content, list) and any(
        isinstance(item, dict) and item.get("type") == "image_url" for item in content
    ):
        source = "CURRENT_INPUT"
        content = None
    elif message.role == "tool":
        content_bytes = len(canonical_json(content).encode("utf-8"))
        if content_bytes > large_result_bytes:
            if not message.tool_execution_id:
                raise RuntimeFault(
                    "NATIVE_CHECKPOINT_INVALID",
                    "large ToolResult has no durable ToolExecution reference",
                    409,
                )
            source = "LEDGER_RESULT"
            content = None
    return NativeMessageState(
        role=message.role,
        content=content,
        content_source=source,
        tool_calls=[
            NativeToolCallState(
                id=call.id,
                name=call.name,
                arguments=call.arguments,
                logical_key=call.logical_key,
            )
            for call in message.tool_calls or []
        ],
        tool_call_id=message.tool_call_id,
        name=message.name,
        is_error=message.is_error,
        kind=message.kind,
        tool_execution_id=message.tool_execution_id,
    )


def encode_native_checkpoint(
    state: LoopState,
    phase: NativeCheckpointPhase,
    *,
    large_result_bytes: int = 8 * 1024,
) -> dict[str, Any]:
    """Validate and serialize the one current codec."""
    try:
        payload = NativeCheckpointState(
            contract=NATIVE_CHECKPOINT_CONTRACT,
            phase=phase,
            iters=state.iters,
            model_call_count=state.model_call_count,
            tool_call_count=state.tool_call_count,
            transition=state.transition,
            attempted_reactive_compact=state.attempted_reactive_compact,
            compact_failures=state.compact_failures,
            compact_cooldown=state.compact_cooldown,
            last_usage=(
                NativeUsageState(
                    prompt_tokens=state.last_usage.prompt_tokens,
                    completion_tokens=state.last_usage.completion_tokens,
                    total_tokens=state.last_usage.total_tokens,
                    extra=state.last_usage.extra,
                )
                if state.last_usage is not None
                else None
            ),
            tool_state=state.tool_state,
            messages=[
                _message_to_state(message, large_result_bytes=large_result_bytes)
                for message in state.messages
            ],
            generation_counter=state.generation_counter,
            current_message_id=state.current_message_id,
            current_generation_id=state.current_generation_id,
            supersedes_generation_id=state.supersedes_generation_id,
            generation_reason=state.generation_reason,
            final_text=state.final_text,
            final_message_id=state.final_message_id,
            final_generation_id=state.final_generation_id,
        )
    except (ValidationError, TypeError, ValueError) as exc:
        raise RuntimeFault(
            "NATIVE_CHECKPOINT_INVALID",
            "native kernel attempted to persist a contradictory checkpoint",
            409,
            {"validation_error": str(exc)[:2000]},
        ) from exc
    return payload.model_dump(mode="json")


def decode_native_checkpoint(
    raw: Any,
    *,
    current_input: Any,
) -> tuple[LoopState, NativeCheckpointPhase]:
    """Strictly decode current state; any discrepancy is terminal corruption."""
    try:
        checkpoint = NativeCheckpointState.model_validate(raw, strict=True)
    except (ValidationError, TypeError, ValueError) as exc:
        raise RuntimeFault(
            "NATIVE_CHECKPOINT_INVALID",
            "native checkpoint does not satisfy the current codec",
            409,
            {"validation_error": str(exc)[:2000]},
        ) from exc

    messages: list[Msg] = []
    for item in checkpoint.messages:
        content = item.content
        if item.content_source == "CURRENT_INPUT":
            content = current_input
        messages.append(Msg(
            role=item.role,
            content=content,
            tool_calls=[
                ToolCall(
                    id=call.id,
                    name=call.name,
                    arguments=call.arguments,
                    logical_key=call.logical_key,
                )
                for call in item.tool_calls
            ] or None,
            tool_call_id=item.tool_call_id,
            name=item.name,
            is_error=item.is_error,
            kind=item.kind,
            tool_execution_id=item.tool_execution_id,
        ))

    usage = (
        Usage(
            prompt_tokens=checkpoint.last_usage.prompt_tokens,
            completion_tokens=checkpoint.last_usage.completion_tokens,
            total_tokens=checkpoint.last_usage.total_tokens,
            extra=checkpoint.last_usage.extra,
        )
        if checkpoint.last_usage is not None
        else None
    )
    state = LoopState(
        messages=messages,
        iters=checkpoint.iters,
        model_call_count=checkpoint.model_call_count,
        tool_call_count=checkpoint.tool_call_count,
        transition=checkpoint.transition,
        attempted_reactive_compact=checkpoint.attempted_reactive_compact,
        compact_failures=checkpoint.compact_failures,
        compact_cooldown=checkpoint.compact_cooldown,
        last_usage=usage,
        tool_state=checkpoint.tool_state,
        generation_counter=checkpoint.generation_counter,
        current_message_id=checkpoint.current_message_id,
        current_generation_id=checkpoint.current_generation_id,
        supersedes_generation_id=checkpoint.supersedes_generation_id,
        generation_reason=checkpoint.generation_reason,
        final_text=checkpoint.final_text,
        final_message_id=checkpoint.final_message_id,
        final_generation_id=checkpoint.final_generation_id,
        restored_phase=checkpoint.phase,
    )
    if checkpoint.phase == "MODEL_REQUEST":
        # The interrupted provider slot is retried with the same message id but
        # a new generation. The already-reserved model-call budget is not refunded.
        state.iters = max(0, state.iters - 1)
        state.resume_from_model_request = True
    return state, checkpoint.phase


__all__ = [
    "NATIVE_CHECKPOINT_CONTRACT",
    "NativeCheckpointPhase",
    "decode_native_checkpoint",
    "encode_native_checkpoint",
]
