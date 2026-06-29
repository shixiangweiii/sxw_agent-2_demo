"""内置通用工具（演示「通用技能调用」，区别于知识检索）。

ADK 会按函数的类型注解 + docstring 自动生成工具 schema，故签名与文档需规范。
"""
from __future__ import annotations

import ast
import operator
from typing import Any, Callable

_OPS: dict[type, Callable[..., float]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}


def _safe_eval(node: ast.AST) -> float:
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return float(node.value)
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_safe_eval(node.left), _safe_eval(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_safe_eval(node.operand))
    raise ValueError("unsupported expression")


def calculator(expression: str) -> dict[str, Any]:
    """计算一个数学算术表达式并返回结果。

    Args:
        expression: 形如 "2*(3+4)" 的纯算术表达式（仅支持 + - * / // % ** 与括号）。
    """
    try:
        tree = ast.parse(expression, mode="eval")
        return {"expression": expression, "result": _safe_eval(tree.body)}
    except Exception as exc:  # noqa: BLE001 - 工具异常以结构化结果返回，由引擎喂回模型
        return {"expression": expression, "error": str(exc)}


def get_weather(city: str) -> dict[str, Any]:
    """查询指定城市的天气（demo：返回固定示例数据）。

    Args:
        city: 城市名，如 "杭州"。
    """
    return {"city": city, "weather": "晴", "temperature_c": 26, "humidity": "40%"}


def simulate_unstable_operation(should_fail: bool = False) -> dict[str, Any]:
    """演示用工具：验证"工具框架级异常被喂回、不中断 turn"（ToolErrorFeedback）。

    与 calculator/knowledge_search 的"业务可预期失败=结构化返回"不同，本工具在 should_fail=True 时
    直接抛出未捕获异常，触发 agent-loop 的 on_tool_error_callback 将异常封装成 function_response 喂回模型。
    仅在用户显式要求"模拟/触发工具失败"时才应以 should_fail=True 调用，常规流程不会误触。

    Args:
        should_fail: 为 True 时抛出异常（模拟框架级工具失败）；为 False 时正常返回。
    """
    if should_fail:
        raise RuntimeError("simulated tool failure")
    return {"status": "ok", "note": "operation succeeded"}


def build_builtin_tools() -> list[Callable[..., Any]]:
    return [calculator, get_weather]
