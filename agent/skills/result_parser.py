"""SkillResultDTO 解析：单帧 → 对外展示 skill_event + 可聚合给 LLM 的文本。

精简自 `base_select_tool._parse_result`（去掉 DQL/browser-use/real/agui 等技能特化分支）。
"""
from __future__ import annotations

from typing import Optional

from agent.stream.event_converters import StreamEvent
from common.skill_contract import SkillResultDataType, SkillResultDTO


def to_skill_event(skill_name: str, result: SkillResultDTO) -> Optional[StreamEvent]:
    """展示帧 → skill_event；结束帧/非展示帧返回 None。"""
    if result.eof or not result.is_display:
        return None
    if not result.data and not result.is_thinking:
        return None
    return StreamEvent("skill_event", {
        "skill": skill_name,
        "dataType": result.data_type.value if result.data_type else None,
        "isThinking": bool(result.is_thinking),
        "isPartial": bool(result.is_partial),
        "data": result.data,
    })


def extract_text(result: SkillResultDTO) -> str:
    """从单帧提取可聚合给 LLM 的正文（思考帧/结束帧不计入）。"""
    if result.is_thinking or result.eof:
        return ""
    if result.data_type == SkillResultDataType.TEXT and isinstance(result.data, str):
        return result.data
    return ""
