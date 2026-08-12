"""每请求技能执行上下文（contextvar）：供 SelectedSkillTool 在工具调用时构造 SkillToolExecuteContext。

对应原项目 `BaseSelectedTool.build_skill_execute_context` 的入参来源（request/user）。
"""
from __future__ import annotations

import contextvars
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class SkillRequestContext:
    agent_uuid: str
    user_id: str
    session_id: str
    text: str = ""
    user: dict[str, Any] = field(default_factory=dict)
    run_id: str = ""
    activity_id: str = ""
    deadline_at_ms: int | None = None
    idempotency_key: str = ""
    tool_execution_id: str = ""


_var: contextvars.ContextVar[Optional[SkillRequestContext]] = contextvars.ContextVar(
    "skill_req_ctx", default=None,
)


def set_request_context(ctx: SkillRequestContext) -> "contextvars.Token[Optional[SkillRequestContext]]":
    return _var.set(ctx)


def reset_request_context(token: "contextvars.Token[Optional[SkillRequestContext]]") -> None:
    _var.reset(token)


def get_request_context() -> Optional[SkillRequestContext]:
    return _var.get()
