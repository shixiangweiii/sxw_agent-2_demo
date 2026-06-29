"""ReasoningEngine 统一端口 + 选型工厂。

两代实现：plan_execute（Gen1 先规划再执行）/ agent_loop（Gen2 ReAct 单循环，M3）。
经 ENGINE 配置切换（替代原项目灰度门 gray_gate）。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, AsyncIterator

from google.genai import types

from agent.config import AgentSettings
from agent.stream.event_converters import StreamEvent

if TYPE_CHECKING:
    from agent.context import AgentContext


@dataclass
class RunContext:
    agent_uuid: str
    user_id: str
    session_id: str
    user_message: types.Content
    settings: AgentSettings


class ReasoningEngine(ABC):
    @abstractmethod
    def run_stream(self, ctx: RunContext) -> AsyncIterator[StreamEvent]:
        """产出统一 SSE 流事件序列。"""
        ...


def extract_text(content: types.Content) -> str:
    parts = content.parts or []
    return " ".join(p.text for p in parts if getattr(p, "text", None)).strip()


def build_engine(ctx: "AgentContext") -> ReasoningEngine:
    engine = ctx.settings.engine
    if engine == "plan_execute":
        from agent.engine.plan_execute.plan_execute_engine import PlanExecuteEngine
        return PlanExecuteEngine(ctx)
    if engine == "agent_loop":
        from agent.engine.agent_loop.agent_loop_engine import AgentLoopEngine
        return AgentLoopEngine(ctx)
    raise ValueError(f"unknown ENGINE={engine}")
