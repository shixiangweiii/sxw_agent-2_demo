"""ReasoningEngine 统一端口 + 选型工厂。

三代实现，经 ENGINE 配置切换（替代原项目灰度门 gray_gate）：
- plan_execute（Gen1）：先规划再执行；
- agent_loop（Gen2）：Tool-Use 循环，但 while 在 ADK BaseLlmFlow 内部；
- native_loop（Gen3）：自研循环，while 在 agent/engine/native_loop/loop.py 里。
对比轴是「循环归谁驱动」——三者共享同一套工具面、系统指令与 SSE 契约。
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


# 单次请求的运行参数（区别于 AgentContext：那是进程级共享装配，这是每请求一份）。
@dataclass
class RunContext:
    agent_uuid: str
    user_id: str
    session_id: str
    user_message: types.Content     # 已构造好的 ADK 消息（文本 + 可选图片 Part）
    settings: AgentSettings         # 引擎从中取 max_loop_iters 等熔断参数


class ReasoningEngine(ABC):
    # 三代引擎唯一的公共契约：喂进一个 RunContext，吐出统一的 StreamEvent 序列。
    # 正因为端口收得这么窄，上游（chat.py / citation / SSE 格式化）完全不知道
    # 背后是"先规划再执行"还是"ReAct 单循环"——换引擎不需要改上游任何一行。
    @abstractmethod
    def run_stream(self, ctx: RunContext) -> AsyncIterator[StreamEvent]:
        """产出统一 SSE 流事件序列。"""
        ...


def extract_text(content: types.Content) -> str:
    parts = content.parts or []
    return " ".join(p.text for p in parts if getattr(p, "text", None)).strip()


def build_engine(ctx: "AgentContext") -> ReasoningEngine:
    # 每次请求都新建引擎实例（引擎本身无状态，真正的共享状态都在 ctx 里）。
    # 延迟 import 是为了避免三代引擎的模块在启动时互相牵连，只加载实际选中的那一代。
    # 注意：ENGINE 是启动期配置，不能按请求切换——评测因此需要按引擎各起一个 agent 实例
    # （8000=agent_loop / 8001=plan_execute / 8002=native_loop）。
    engine = ctx.settings.engine
    if engine == "plan_execute":
        from agent.engine.plan_execute.plan_execute_engine import PlanExecuteEngine
        return PlanExecuteEngine(ctx)
    if engine == "agent_loop":
        from agent.engine.agent_loop.agent_loop_engine import AgentLoopEngine
        return AgentLoopEngine(ctx)
    if engine == "native_loop":
        # Gen3：自研 Tool-Use 循环。与 agent_loop 的工具面、系统指令、SSE 契约完全一致，
        # 区别只在"循环归谁驱动"——这里的 while 在我们自己手里，不经任何 Agent 框架。
        from agent.engine.native_loop.engine import NativeLoopEngine
        return NativeLoopEngine(ctx)
    raise ValueError(f"unknown ENGINE={engine}")
