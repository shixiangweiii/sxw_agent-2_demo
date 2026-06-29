"""内置演示技能（MCP 风格：tools + inputSchema）。

精简模拟真实技能中心托管的 MCP 技能；同步执行返回 MCP `content:[{type,text}]`，
流式执行（S1）产出 SkillResultDTO 富信封。
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncGenerator

from common.skill_contract import (
    SkillDef,
    SkillResultDataType,
    SkillResultDTO,
    SkillToolDef,
    SkillToolExecuteResultDTO,
)

# 哨兵：translate 传该文本时模拟算粒不足（演示配额错误链路）
QUOTA_SENTINEL = "__quota__"

SKILL_DEFS: list[SkillDef] = [
    SkillDef(
        skillId="skill-deep-translate",
        name="深度翻译",
        description="把文本翻译成目标语言（流式：先思考再逐字输出）。",
        tools=[SkillToolDef(
            name="deep_translate",
            description="把给定文本翻译成目标语言。",
            inputSchema={
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "待翻译文本"},
                    "target_lang": {"type": "string", "description": "目标语言，如 en/ja/fr"},
                },
                "required": ["text"],
            },
        )],
    ),
    SkillDef(
        skillId="skill-weather-card",
        name="天气卡片",
        description="查询城市天气并以卡片形式展示。",
        tools=[SkillToolDef(
            name="query_weather",
            description="查询指定城市的天气，返回天气卡片。",
            inputSchema={
                "type": "object",
                "properties": {"city": {"type": "string", "description": "城市名"}},
                "required": ["city"],
            },
        )],
    ),
]


def get_skill_defs(snapshot_tag: str = "PUBLISHED") -> list[SkillDef]:
    # demo：PUBLISHED 运行态返回全部；DRAFT 仅占位（返回相同集合）。
    return list(SKILL_DEFS)


def _mcp_text(text: str) -> list[dict[str, Any]]:
    return [{"type": "text", "text": text}]


def translate_text(arguments: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    text = str(arguments.get("text", ""))
    target = str(arguments.get("target_lang") or "en")
    translation = f"[{target}] {text}"
    return translation, {"sourceLang": "auto", "targetLang": target}


def weather_card(arguments: dict[str, Any]) -> dict[str, Any]:
    city = str(arguments.get("city", ""))
    return {"type": "weather_card", "city": city, "weather": "晴", "temperatureC": 26, "humidity": "40%"}


def execute_sync(skill_id: str, tool_name: str, arguments: dict[str, Any]) -> SkillToolExecuteResultDTO:
    arguments = arguments or {}
    if skill_id == "skill-deep-translate" and tool_name == "deep_translate":
        translation, _meta = translate_text(arguments)
        return SkillToolExecuteResultDTO(content=_mcp_text(translation))
    if skill_id == "skill-weather-card" and tool_name == "query_weather":
        card = weather_card(arguments)
        return SkillToolExecuteResultDTO(
            content=_mcp_text(json.dumps(card, ensure_ascii=False)), structuredContent=card,
        )
    return SkillToolExecuteResultDTO(isError=True, errorMessage=f"未知技能/工具: {skill_id}/{tool_name}")


def _split_chunks(text: str, size: int) -> list[str]:
    return [text[i:i + size] for i in range(0, len(text), size)] or [""]


async def _stream_translate(arguments: dict[str, Any]) -> AsyncGenerator[SkillResultDTO, None]:
    text = str(arguments.get("text", ""))
    target = str(arguments.get("target_lang") or "en")
    seq = 0
    # 1) 技能思考帧
    yield SkillResultDTO(seq=seq, dataType=SkillResultDataType.TEXT, isThinking=True,
                         isDisplay=True, isPartial=True, data="正在分析源语言与语境…")
    seq += 1
    await asyncio.sleep(0.05)
    # 2) 算粒哨兵 → 配额错误结束帧
    if QUOTA_SENTINEL in text:
        yield SkillResultDTO(success=False, errorCode="BENEFIT_NOT_ENOUGH",
                             errorMsg="AI通用算粒余额不足，请联系管理员", eof=True, isPartial=False)
        return
    # 3) 翻译结果分块流式输出
    translation, meta = translate_text(arguments)
    for chunk in _split_chunks(translation, 6):
        yield SkillResultDTO(seq=seq, dataType=SkillResultDataType.TEXT, isDisplay=True,
                             isPartial=True, data=chunk)
        seq += 1
        await asyncio.sleep(0.03)
    # 4) 结束帧（携带 customMetadata）
    yield SkillResultDTO(seq=seq, dataType=SkillResultDataType.TEXT, eof=True, isPartial=False,
                         isDisplay=False, customMetadata={"skill": "deep_translate", "targetLang": target})


async def _stream_weather(arguments: dict[str, Any]) -> AsyncGenerator[SkillResultDTO, None]:
    card = weather_card(arguments)
    # CARD 帧：直呈不总结
    yield SkillResultDTO(seq=0, dataType=SkillResultDataType.CARD, isDisplay=True,
                         skipSummarization=True, isPartial=False, data=card)
    yield SkillResultDTO(seq=1, eof=True, isPartial=False, isDisplay=False)


async def execute_streaming(
    skill_id: str, tool_name: str, arguments: dict[str, Any],
) -> AsyncGenerator[SkillResultDTO, None]:
    arguments = arguments or {}
    if skill_id == "skill-deep-translate" and tool_name == "deep_translate":
        async for result in _stream_translate(arguments):
            yield result
        return
    if skill_id == "skill-weather-card" and tool_name == "query_weather":
        async for result in _stream_weather(arguments):
            yield result
        return
    yield SkillResultDTO(success=False, errorMsg=f"未知技能/工具: {skill_id}/{tool_name}",
                         eof=True, isPartial=False)
