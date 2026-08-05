"""Claude Skill Agent-as-Tool 的调用级契约与稳定错误码。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Optional


SKILL_TIMEOUT = "SKILL_TIMEOUT"
SKILL_SANDBOX_UNAVAILABLE = "SKILL_SANDBOX_UNAVAILABLE"
SKILL_PACKAGE_INVALID = "SKILL_PACKAGE_INVALID"
SKILL_INSTRUCTION_NOT_READ = "SKILL_INSTRUCTION_NOT_READ"
SKILL_EXECUTION_FAILED = "SKILL_EXECUTION_FAILED"


def _truncate_summary(summary: str, max_chars: int) -> tuple[str, bool]:
    limit = max(1, max_chars)
    return summary[:limit], len(summary) > limit


class SkillPackageInvalidError(ValueError):
    """技能包无法安全装载或不满足运行契约。"""


class SkillInstructionNotReadError(RuntimeError):
    """Skill Agent 连续两次未成功读取指定 SKILL.md。"""

    def __init__(self, message: str, *, attempts: int = 2) -> None:
        super().__init__(message)
        self.attempts = attempts


class SkillExecutionFailedError(RuntimeError):
    """子 Runner 未能形成有效终局结果。"""

    def __init__(self, message: str, *, attempts: int) -> None:
        super().__init__(message)
        self.attempts = attempts


@dataclass(frozen=True)
class SkillRuntimeConfig:
    """每次 Skill Agent 调用共享的有界运行参数。"""

    call_timeout_seconds: float
    max_llm_calls: int
    result_max_chars: int


@dataclass(frozen=True)
class SkillCallContext:
    """主 Agent tool_use 与子 Skill Agent 调用之间的关联标识。"""

    parent_invocation_id: str
    skill_call_id: str


@dataclass(frozen=True)
class SkillRunResult:
    """子 Runner 的内部终局结果；由 Tool 边界转换成 SkillCallResult。"""

    summary: str
    attempts: int


@dataclass
class SkillRunProgress:
    """让 Tool 在超时/取消边界仍能记录已启动的子会话次数。"""

    attempts: int = 0


@dataclass(frozen=True)
class SkillCallError:
    code: str
    message: str
    retryable: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
        }


@dataclass(frozen=True)
class SkillCallResult:
    """返回父 LLM 的高信号 tool_result envelope。"""

    skill_call_id: str
    skill_id: str
    status: Literal["success", "error"]
    summary: str
    is_error: bool
    error: Optional[SkillCallError]
    attempts: int
    truncated: bool = False

    @classmethod
    def success(
        cls,
        *,
        skill_call_id: str,
        skill_id: str,
        summary: str,
        attempts: int,
        max_chars: int,
    ) -> "SkillCallResult":
        safe_summary, truncated = _truncate_summary(summary, max_chars)
        return cls(
            skill_call_id=skill_call_id,
            skill_id=skill_id,
            status="success",
            summary=safe_summary,
            is_error=False,
            error=None,
            attempts=attempts,
            truncated=truncated,
        )

    @classmethod
    def failure(
        cls,
        *,
        skill_call_id: str,
        skill_id: str,
        summary: str,
        code: str,
        retryable: bool,
        attempts: int,
        max_chars: int,
    ) -> "SkillCallResult":
        safe_summary, truncated = _truncate_summary(summary, max_chars)
        return cls(
            skill_call_id=skill_call_id,
            skill_id=skill_id,
            status="error",
            summary=safe_summary,
            is_error=True,
            error=SkillCallError(code=code, message=safe_summary, retryable=retryable),
            attempts=attempts,
            truncated=truncated,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "skillCallId": self.skill_call_id,
            "skillId": self.skill_id,
            "status": self.status,
            "summary": self.summary,
            "isError": self.is_error,
            "error": self.error.to_dict() if self.error else None,
            "meta": {
                "attempts": self.attempts,
                "truncated": self.truncated,
            },
        }
