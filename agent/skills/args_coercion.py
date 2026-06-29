"""LLM 工具参数修复：按 input_schema 把声明为 array/object 的参数从 JSON 字符串反序列化回结构化值。

精简复刻 albert-agent-2 `app/lumi/tools/args_coercion.py` 的核心取舍：
LLM 常把数组/对象参数输出成 JSON 字符串（如 `{"items": "[1,2,3]"}`），
若直接透传给下游技能中心，会收到字符串而非数组/对象。这里按 schema 的 type 把它们 parse 回来。
"""
from __future__ import annotations

import json
from typing import Any, Optional


def coerce_args_by_schema(
    args: dict[str, Any], input_schema: Optional[dict[str, Any]],
) -> dict[str, Any]:
    """按 input_schema.properties 的 type 修复 args 中被错输成 JSON 字符串的 array/object 参数。"""
    if not args or not input_schema:
        return args
    properties = input_schema.get("properties")
    if not isinstance(properties, dict):
        return args
    coerced = dict(args)
    for name, spec in properties.items():
        if name not in coerced or not isinstance(spec, dict):
            continue
        coerced[name] = _coerce_one(coerced[name], spec.get("type"))
    return coerced


def _coerce_one(value: Any, expected_type: Optional[str]) -> Any:
    if expected_type not in ("array", "object") or not isinstance(value, str):
        return value
    text = value.strip()
    if not text:
        return value
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return value
    if expected_type == "array" and isinstance(parsed, list):
        return parsed
    if expected_type == "object" and isinstance(parsed, dict):
        return parsed
    return value
