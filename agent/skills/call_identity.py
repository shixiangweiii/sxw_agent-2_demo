"""从 ADK ToolContext 提取稳定的技能调用身份。"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Optional

from google.adk.tools import ToolContext

from common.obs import log_kv


@dataclass(frozen=True)
class SkillCallIdentity:
    parent_invocation_id: str
    skill_call_id: str


def resolve_skill_call_identity(
    tool_context: Optional[ToolContext],
    *,
    logger: logging.Logger,
    tag: str,
    skill: str,
) -> SkillCallIdentity:
    """标准 ADK 路径复用已有 ID；直调缺失时 UUID 降级并告警。"""
    skill_call_id = getattr(tool_context, "function_call_id", None)
    parent_invocation_id = getattr(tool_context, "invocation_id", None)
    fallback_fields: list[str] = []
    if not skill_call_id:
        skill_call_id = f"call_{uuid.uuid4().hex}"
        fallback_fields.append("skill_call_id")
    if not parent_invocation_id:
        parent_invocation_id = f"invocation_{uuid.uuid4().hex}"
        fallback_fields.append("parent_invocation_id")
    if fallback_fields:
        log_kv(
            logger,
            logging.WARNING,
            tag,
            "missing ADK call identity; generated fallback UUID",
            skill=skill,
            fallback_fields=fallback_fields,
            skill_call_id=skill_call_id,
            parent_invocation_id=parent_invocation_id,
        )
    return SkillCallIdentity(
        parent_invocation_id=parent_invocation_id,
        skill_call_id=skill_call_id,
    )
