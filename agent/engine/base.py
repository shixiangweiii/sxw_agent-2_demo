"""Narrow ADK reasoning-engine surface behind ``AdkEngineAdapter``.

两套 ADK 实现由每个 Run 的 ``engine`` 字段选择：
- plan_execute（Gen1）：先规划再执行；
- agent_loop（Gen2）：Tool-Use 循环，但 while 在 ADK BaseLlmFlow 内部；
Native 直接实现公开 EngineAdapter，不再进入本 ADK 内部面。Canonical event 和
terminal 的裁决属于 Runtime，本端口只是 ADK 引擎的 attempt-local 适配面。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, AsyncIterator, Literal

from google.genai import types

from agent.config import AgentSettings
from agent.runtime.domain.models import EngineOutcome
from agent.stream.event_converters import StreamEvent

if TYPE_CHECKING:
    from agent.context import AgentContext


APP_NAME = "sxw-agent"


# 单次请求的运行参数（区别于 AgentContext：那是进程级共享装配，这是每请求一份）。
@dataclass
class RunContext:
    run_id: str
    activity_id: str
    engine: Literal["plan_execute", "agent_loop"]
    agent_uuid: str
    user_id: str
    session_id: str
    user_message: types.Content     # 已构造好的 ADK 消息（文本 + 可选图片 Part）
    settings: AgentSettings         # 引擎从中取 max_loop_iters 等熔断参数
    canonical_history: tuple[types.Content, ...] = field(default_factory=tuple)
    # ADK Session / Artifact 仅是单次 attempt 的投影，由 Runtime adapter 创建并销毁。
    session_service: Any = None
    artifact_service: Any = None
    deadline_at_ms: int | None = None
    tool_broker: Any = None
    fencing_token: int = 0
    release_fingerprint: str = ""
    runtime_io: Any = None
    engine_checkpoint: Any = None
    # ADK's request-local tool state is only an execution mirror.  This cursor
    # lets brokered state tools persist their authoritative WorkingState via
    # RuntimeIO without racing concurrent callbacks.
    runtime_checkpoint_revision: int = 0
    runtime_working_state: Any = None
    runtime_checkpoint_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Attempt-local explicit control result.  The ADK async iterator is only
    # an event-draft transport: exhausting it is not evidence of success.  Each
    # real engine must set this field on its own completed/failure control path;
    # the Runtime adapter fails closed when the iterator ends without one.
    engine_outcome: EngineOutcome | None = None


class ReasoningEngine(ABC):
    # 该窄端口是引擎内部兼容层；对外契约见 EngineAdapter/RuntimeIO。
    @abstractmethod
    def run_stream(self, ctx: RunContext) -> AsyncIterator[StreamEvent]:
        """产出统一 SSE 流事件序列。"""
        ...


def extract_text(content: types.Content) -> str:
    parts = content.parts or []
    return " ".join(p.text for p in parts if getattr(p, "text", None)).strip()


def build_engine(
    ctx: "AgentContext",
    engine: Literal["plan_execute", "agent_loop"],
) -> ReasoningEngine:
    # 仅构建两套 ADK 引擎；native_loop 由 NativeLoopAdapter 直接驱动。
    if engine == "plan_execute":
        from agent.engine.plan_execute.plan_execute_engine import PlanExecuteEngine
        return PlanExecuteEngine(ctx)
    if engine == "agent_loop":
        from agent.engine.agent_loop.agent_loop_engine import AgentLoopEngine
        return AgentLoopEngine(ctx)
    raise ValueError(f"unknown run engine={engine}")
