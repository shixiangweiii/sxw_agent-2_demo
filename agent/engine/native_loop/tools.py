"""ToolSpec：native_loop 的统一工具契约 + 两个适配器。

设计目标是**一行现有工具代码都不改**就能被自研循环调用。项目里的工具有两种形态：
- 普通 Python 函数（calculator / knowledge_search / update_task_plan …）
  —— 原来由 ADK 从类型注解 + docstring 自动生成 schema，这里自己生成；
- ADK ``BaseTool`` 子类（SelectedSkillTool / ClaudeSkillTool / A2A AgentTool）
  —— 它们已经有 ``_get_declaration()`` + ``run_async(args, tool_context)``，
  几乎就是我们要的接口，套一层适配即可。

``NativeToolContext`` 是一个鸭子类型 shim，同时满足现有代码的三处 getattr 需求：
``function_call_id`` / ``invocation_id``（技能调用身份）与 ``state``（计划工具写状态）。
"""
from __future__ import annotations

import asyncio
import inspect
import re
import typing
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from jsonschema import Draft202012Validator

# ADK 用类型注解把 tool_context 排除在 schema 之外；我们按**参数名**排除。
# 漏掉它会把内部上下文当成模型可填参数暴露出去。
_CONTEXT_PARAM_NAMES = frozenset({"tool_context"})

# 只读工具白名单：决定 executor 能否把它们放进并发批次（对应 CC 的 isConcurrencySafe）。
# 保守原则——不在名单里的一律串行，宁可慢也不要并发副作用。
_READ_ONLY_TOOLS = frozenset({
    "calculator",
    "get_weather",
    "knowledge_search",
    "tool_search",
    "translate",
    "text_stats",
    "read_artifact",
})


@dataclass
class NativeToolContext:
    """每次工具调用一份的上下文 shim。

    字段名刻意与 ADK ``ToolContext`` 对齐，这样 ``resolve_skill_call_identity``
    （全部走 getattr）和 ``update_task_plan``（只用 ``.state``）无需任何改动。
    """

    function_call_id: str
    invocation_id: str
    state: dict[str, Any] = field(default_factory=dict)
    logical_key: str = ""


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]                       # JSON Schema（object）
    run: Callable[[dict[str, Any], NativeToolContext], Awaitable[Any]]
    concurrency_safe: bool = False                   # → executor 分批依据
    exclusive_resources: tuple[str, ...] = ()        # 同名独占资源跨调用互斥
    result_protocol: str = "plain"
    implementation: str = ""

    def to_wire(self) -> dict[str, Any]:
        """转成 OpenAI ``tools`` 声明。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolRegistry:
    """按名字查工具。查不到返回 None —— 调用方据此合成"工具不存在"的 tool_result。"""

    def __init__(self, specs: list[ToolSpec]) -> None:
        self._by_name: dict[str, ToolSpec] = {}
        for spec in specs:
            if not spec.name or not spec.description.strip():
                raise ValueError("tool name and description must be non-empty")
            if spec.parameters.get("type") != "object" or not isinstance(
                spec.parameters.get("properties"), dict,
            ):
                raise ValueError(f"{spec.name}: parameters must be an object JSON Schema")
            Draft202012Validator.check_schema(spec.parameters)
            if spec.name in self._by_name:
                raise ValueError(f"duplicate tool name: {spec.name}")
            if not spec.implementation:
                spec = ToolSpec(
                    name=spec.name,
                    description=spec.description,
                    parameters=spec.parameters,
                    run=spec.run,
                    concurrency_safe=spec.concurrency_safe,
                    exclusive_resources=spec.exclusive_resources,
                    result_protocol=spec.result_protocol,
                    implementation=(
                        f"{getattr(spec.run, '__module__', type(spec.run).__module__)}."
                        f"{getattr(spec.run, '__qualname__', type(spec.run).__qualname__)}"
                    ),
                )
            self._by_name[spec.name] = spec

    def get(self, name: str) -> Optional[ToolSpec]:
        return self._by_name.get(name)

    def all(self) -> list[ToolSpec]:
        return list(self._by_name.values())

    def wire_declarations(self) -> list[dict[str, Any]]:
        return [spec.to_wire() for spec in self._by_name.values()]

    def __len__(self) -> int:
        return len(self._by_name)


# ── 适配器 1：普通 Python 函数 ─────────────────────────────────────────────

def from_function(fn: Callable[..., Any]) -> ToolSpec:
    """从带类型注解 + Google 风格 docstring 的函数生成 ToolSpec。"""
    name = getattr(fn, "__name__", "unknown_tool")
    doc = inspect.getdoc(fn) or ""
    summary, arg_docs = _parse_docstring(doc)
    signature = inspect.signature(fn)
    hints = typing.get_type_hints(fn)

    properties: dict[str, Any] = {}
    required: list[str] = []
    wants_context = False
    for param_name, param in signature.parameters.items():
        if param_name in _CONTEXT_PARAM_NAMES:
            wants_context = True
            continue
        if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
            continue
        schema = _annotation_to_schema(hints.get(param_name, param.annotation))
        if param_name in arg_docs:
            schema["description"] = arg_docs[param_name]
        properties[param_name] = schema
        if param.default is inspect.Parameter.empty:
            required.append(param_name)

    parameters: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        parameters["required"] = required

    is_async = inspect.iscoroutinefunction(fn)

    async def run(args: dict[str, Any], ctx: NativeToolContext) -> Any:
        call_args = dict(args)
        if wants_context:
            call_args["tool_context"] = ctx
        if is_async:
            return await fn(**call_args)
        # 同步工具直接调用：本项目的同步工具都是纯计算（calculator / text_stats 等），
        # 不阻塞事件循环；若将来加入阻塞 IO 的同步工具，应改走 asyncio.to_thread。
        return fn(**call_args)

    return ToolSpec(
        name=name,
        description=summary,
        parameters=parameters,
        run=run,
        concurrency_safe=name in _READ_ONLY_TOOLS,
        implementation=(
            f"{getattr(fn, '__module__', type(fn).__module__)}."
            f"{getattr(fn, '__qualname__', type(fn).__qualname__)}"
        ),
    )


_TYPE_MAP: dict[Any, str] = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    dict: "object",
}


def _annotation_to_schema(annotation: Any) -> dict[str, Any]:
    """Python 类型注解 → JSON Schema 片段；不明确的声明启动即失败。"""
    if annotation is inspect.Parameter.empty or annotation is Any:
        raise TypeError("tool parameters require explicit supported type annotations")

    origin = typing.get_origin(annotation)
    args = typing.get_args(annotation)

    # Optional[X] / Union[X, None] → 取 X（可选性由 required 列表表达）
    if origin is typing.Union or str(origin) == "types.UnionType":
        non_none = [a for a in args if a is not type(None)]
        if len(non_none) == 1:
            return _annotation_to_schema(non_none[0])
        raise TypeError(f"unsupported tool parameter union: {annotation!r}")

    if origin in (list, set, tuple):
        if not args:
            raise TypeError(f"tool collection annotation requires an item type: {annotation!r}")
        item_schema = _annotation_to_schema(args[0])
        return {"type": "array", "items": item_schema}

    if origin is dict:
        if len(args) != 2 or args[0] is not str:
            raise TypeError(f"tool mapping annotation must be dict[str, T]: {annotation!r}")
        return {
            "type": "object",
            "additionalProperties": _annotation_to_schema(args[1]),
        }

    mapped = _TYPE_MAP.get(annotation)
    if mapped:
        return {"type": mapped}
    raise TypeError(f"unsupported tool parameter annotation: {annotation!r}")


_ARGS_HEADER = re.compile(r"^\s*(Args|Arguments|参数)\s*[:：]\s*$")
_ARG_LINE = re.compile(r"^\s{2,}(\*{0,2}\w+)\s*(?:\([^)]*\))?\s*[:：]\s*(.+)$")
_SECTION_HEADER = re.compile(r"^\s*(Returns|Raises|Yields|Examples?|Notes?)\s*[:：]\s*$")


def _parse_docstring(doc: str) -> tuple[str, dict[str, str]]:
    """拆出摘要与 Google 风格 Args 段。项目现有工具都是这个写法。"""
    if not doc:
        return "", {}
    lines = doc.splitlines()
    summary_lines: list[str] = []
    arg_docs: dict[str, str] = {}
    in_args = False
    current: Optional[str] = None

    for line in lines:
        if _ARGS_HEADER.match(line):
            in_args = True
            current = None
            continue
        if _SECTION_HEADER.match(line):
            in_args = False
            current = None
            continue
        if in_args:
            matched = _ARG_LINE.match(line)
            if matched:
                current = matched.group(1).lstrip("*")
                arg_docs[current] = matched.group(2).strip()
            elif current and line.strip():
                # 续行：拼进上一个参数的描述
                arg_docs[current] = f"{arg_docs[current]} {line.strip()}"
            continue
        if not arg_docs:
            summary_lines.append(line)

    summary = " ".join(part.strip() for part in summary_lines if part.strip())
    return summary.strip(), arg_docs


# ── 适配器 2：ADK BaseTool 子类 ────────────────────────────────────────────

def from_adk_tool(tool: Any) -> ToolSpec:
    """包装 ADK ``BaseTool``（skill-center 技能 / Claude SKILL / A2A 子代理）。

    只读取它已有的声明并转调 ``run_async``——不复制任何执行逻辑，
    超时、并发治理、错误码分类全部留在原工具里。
    """
    name = getattr(tool, "name", None) or type(tool).__name__
    description = getattr(tool, "description", "") or ""
    parameters = _adk_declaration_to_schema(tool, name)

    async def run(args: dict[str, Any], ctx: NativeToolContext) -> Any:
        return await tool.run_async(args=args, tool_context=ctx)

    return ToolSpec(
        name=name,
        description=description,
        parameters=parameters,
        run=run,
        # Claude SKILL 在技能包 frontmatter 里已声明并发语义；其余远程/副作用工具保守串行。
        concurrency_safe=bool(getattr(tool, "concurrency_safe", False)),
        exclusive_resources=tuple(getattr(tool, "exclusive_resources", ()) or ()),
        result_protocol=_result_protocol(tool),
        implementation=f"{type(tool).__module__}.{type(tool).__qualname__}",
    )


def _adk_declaration_to_schema(tool: Any, name: str) -> dict[str, Any]:
    """从 ``_get_declaration()`` 取 JSON Schema。

    ADK 2.6.2 的 FunctionDeclaration 有两种承载方式：``parameters_json_schema``
    （原始 JSON Schema，skill-center / Claude SKILL 走这条）与 ``parameters``
    （genai ``Schema`` 对象，AgentTool 走这条）。两种都要能吃。
    """
    declaration = tool._get_declaration()  # noqa: SLF001 - ADK 公版工具的既定取声明方式

    if declaration is None:
        raise ValueError(f"{name}: ADK tool declaration is missing")

    json_schema = getattr(declaration, "parameters_json_schema", None)
    if isinstance(json_schema, dict) and json_schema:
        return _normalize_schema(json_schema)

    parameters = getattr(declaration, "parameters", None)
    if parameters is None:
        raise ValueError(f"{name}: ADK tool parameters schema is missing")
    dumped = _dump_genai_schema(parameters)
    if not dumped:
        raise ValueError(f"{name}: ADK tool parameters schema is empty")
    return _normalize_schema(dumped)


def _dump_genai_schema(schema: Any) -> Optional[dict[str, Any]]:
    """genai ``Schema``（pydantic 模型）→ dict。"""
    dump = getattr(schema, "model_dump", None)
    if callable(dump):
        return dump(exclude_none=True, mode="json")
    if isinstance(schema, dict):
        return dict(schema)
    return None


def _normalize_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """genai 用大写枚举表示类型（OBJECT/STRING…），OpenAI 端要小写。"""
    normalized = _normalize_schema_node(schema)
    if normalized.get("type") != "object" or not isinstance(
        normalized.get("properties"), dict,
    ):
        raise ValueError("tool parameters schema must declare object properties")
    return normalized


def _normalize_schema_node(schema: dict[str, Any]) -> dict[str, Any]:
    """Normalize every nested JSON-Schema node without treating it as a root."""

    normalized: dict[str, Any] = {}
    for key, value in schema.items():
        if key == "type" and isinstance(value, str):
            normalized[key] = value.lower()
        elif key == "properties" and isinstance(value, dict):
            normalized[key] = {k: _normalize_schema_node(v) if isinstance(v, dict) else v
                               for k, v in value.items()}
        elif key == "items" and isinstance(value, dict):
            normalized[key] = _normalize_schema_node(value)
        elif isinstance(value, dict):
            normalized[key] = _normalize_schema_node(value)
        elif isinstance(value, list):
            normalized[key] = [
                _normalize_schema_node(item) if isinstance(item, dict) else item
                for item in value
            ]
        else:
            normalized[key] = value
    return normalized


# ── 组装 ───────────────────────────────────────────────────────────────────

def build_registry(tools: list[Any]) -> ToolRegistry:
    """把混合形态的工具列表（函数 + ADK BaseTool）统一成 ToolRegistry。"""
    # 延迟 import 避免与 adk_bridge 形成模块级循环依赖。
    from agent.engine.native_loop.adk_bridge import from_agent_tool, is_agent_tool  # noqa: PLC0415

    specs: list[ToolSpec] = []
    for tool in tools:
        if callable(tool) and not hasattr(tool, "run_async"):
            specs.append(from_function(tool))
        elif is_agent_tool(tool):
            # ADK AgentTool（本地 researcher 或 A2A 远程子代理）依赖真实 InvocationContext，
            # 鸭子类型 shim 满足不了 → 必须走桥接自建子 Runner，否则调用必失败。
            specs.append(from_agent_tool(tool))
        else:
            specs.append(from_adk_tool(tool))
    return ToolRegistry(specs)


def _result_protocol(tool: Any) -> str:
    """Classify only the transport vocabulary; Broker owns its adapter."""

    from agent.claude_skill.claude_skill_tool import ClaudeSkillTool  # noqa: PLC0415
    from agent.skills.selected_skill_tool import SelectedSkillTool  # noqa: PLC0415

    if isinstance(tool, SelectedSkillTool):
        return "skill-center"
    if isinstance(tool, ClaudeSkillTool):
        return "claude-skill"
    return "plain"


async def call_tool(spec: ToolSpec, args: dict[str, Any], ctx: NativeToolContext) -> Any:
    """执行工具。异常原样抛出，由 executor 统一转成 is_error 的 tool_result。"""
    result = spec.run(args, ctx)
    if inspect.isawaitable(result):
        return await result
    return result


__all__ = [
    "NativeToolContext",
    "ToolRegistry",
    "ToolSpec",
    "build_registry",
    "call_tool",
    "from_adk_tool",
    "from_function",
]
