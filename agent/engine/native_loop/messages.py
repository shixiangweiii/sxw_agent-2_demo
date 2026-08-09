"""native_loop 的消息模型：整个循环的**全部状态**就是一个 Msg 扁平列表。

对应 CC `query.ts` 里的 `State.messages`——没有 agent 树、没有图、没有状态机，
续推就是 `state = next; continue`，历史就是数组追加。

这里同时承担三件事：
1. 内部 Msg ↔ OpenAI 线格式（`to_wire`）的转换；
2. genai ``types.Content``（接入层给的多模态消息）→ Msg；
3. **原子单元**划分——OpenAI 协议要求 role=tool 的消息必须紧跟在带 tool_calls 的
   assistant 消息之后，任何裁剪/压缩都不允许从中间切开，否则上游直接 400。
"""
from __future__ import annotations

import base64
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

# Msg.kind 取值：普通消息 / 压缩摘要（同时充当 compact boundary）/ 系统临时提醒。
KIND_NORMAL = "normal"
KIND_COMPACT_SUMMARY = "compact_summary"
KIND_META = "meta"


@dataclass
class ToolCall:
    """模型发起的一次工具调用。

    ``arguments`` 保留**原始 JSON 字符串**而不是解析后的 dict：
    解析失败本身是需要喂回模型的信息（CC 的 `is_error` tool_result 路径），
    在这一层提前解析会把错误变成异常，丢掉"喂回去让模型自己改"的机会。
    """

    id: str
    name: str
    arguments: str = ""
    logical_key: str = ""


@dataclass
class Msg:
    role: str                                          # system | user | assistant | tool
    content: Any = None                                # str | list[block] | None
    tool_calls: Optional[list[ToolCall]] = None
    tool_call_id: Optional[str] = None                 # role=tool 时必填
    name: Optional[str] = None                         # role=tool 时记工具名，便于日志与体积治理
    is_error: bool = False                             # 本地标记，不进线格式
    kind: str = KIND_NORMAL                            # 本地标记，不进线格式

    def to_wire(self) -> dict[str, Any]:
        """转成 OpenAI 兼容线格式。本地标记（is_error / kind / name）按角色裁剪。"""
        if self.role == "tool":
            return {
                "role": "tool",
                "tool_call_id": self.tool_call_id or "",
                "content": _as_text(self.content),
            }
        wire: dict[str, Any] = {"role": self.role}
        # 带 tool_calls 的 assistant 消息，content 可以为 None（模型只调工具不说话）。
        # 必须显式给 None 而不是 ""，部分上游对空串校验更严。
        wire["content"] = self.content if self.content not in ("", None) else None
        if self.tool_calls:
            wire["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.name, "arguments": tc.arguments or "{}"},
                }
                for tc in self.tool_calls
            ]
        return wire


def _as_text(content: Any) -> str:
    """role=tool 的 content 必须是字符串。"""
    if isinstance(content, str):
        return content
    if content is None:
        return ""
    return str(content)


def to_wire(messages: Iterable[Msg]) -> list[dict[str, Any]]:
    return [m.to_wire() for m in messages]


# ── genai Content → Msg ────────────────────────────────────────────────────
# Runtime adapter 构造的是 ADK/GenAI 的 types.Content。原来由 LiteLlm 负责把
# inline_data 转成 base64 image_url；自研循环必须自己做这一步。

def content_to_msg(content: Any) -> Msg:
    """把 genai ``types.Content``（文本 +（可选）图片 Part）转成一条 user Msg。"""
    parts = getattr(content, "parts", None) or []
    texts: list[str] = []
    blocks: list[dict[str, Any]] = []
    for part in parts:
        text = getattr(part, "text", None)
        if text:
            texts.append(text)
            blocks.append({"type": "text", "text": text})
            continue
        image_url = _inline_data_to_image_url(part)
        if image_url is not None:
            blocks.append({"type": "image_url", "image_url": {"url": image_url}})

    role = getattr(content, "role", None) or "user"
    has_image = any(b["type"] == "image_url" for b in blocks)
    # 纯文本走字符串形态（更省、更兼容）；有图片才升级成 block 数组。
    return Msg(role=role, content=blocks if has_image else "\n".join(texts))


def _inline_data_to_image_url(part: Any) -> Optional[str]:
    """genai inline_data Part → data URL；非图片 Part 返回 None。"""
    inline = getattr(part, "inline_data", None)
    if inline is None:
        return None
    data = getattr(inline, "data", None)
    if not data:
        return None
    mime = getattr(inline, "mime_type", None) or "image/jpeg"
    # genai 在不同路径下可能已是 bytes 或已是 base64 字符串，两种都兜住。
    if isinstance(data, bytes):
        encoded = base64.b64encode(data).decode("ascii")
    else:
        encoded = str(data)
    return f"data:{mime};base64,{encoded}"


def extract_text(content: Any) -> str:
    """从 genai Content 取纯文本（供日志 / 摘要 / 子代理 query 使用）。"""
    parts = getattr(content, "parts", None) or []
    return " ".join(p.text for p in parts if getattr(p, "text", None)).strip()


# ── 原子单元 ───────────────────────────────────────────────────────────────
# 为什么必须有：OpenAI 兼容协议要求 role=tool 紧跟在带 tool_calls 的 assistant 之后。
# 裁剪或压缩时若把 assistant 丢了只留 tool（或反之），请求会被直接判 400。
# 所以"可丢弃的最小单位"不是一条消息，而是一次完整的【调用—响应】区间。
#
# 扁平列表上这件事比 ADK 的 Content 结构简单得多：一条带 tool_calls 的 assistant，
# 加上紧随其后、引用了它任一 call id 的所有 tool 消息，构成一个不可拆分单元。

# 字符 → token 的粗略换算比。按中文取值（中文约 1.5 字符/token，英文约 4）：
# 偏保守 = 倾向高估上下文规模 = 更早触发压缩，这个方向的错更安全。
# 上游返回真实 usage 时不会用到它（见 compact.estimate_tokens）。
CHARS_PER_TOKEN = 1.5


@dataclass
class Unit:
    start: int          # 闭区间起点
    end: int            # 闭区间终点


def atomic_units(messages: list[Msg]) -> list[Unit]:
    """把消息列表切成一串连续的不可拆分单元。"""
    units: list[Unit] = []
    index = 0
    total = len(messages)
    while index < total:
        msg = messages[index]
        if msg.role == "assistant" and msg.tool_calls:
            pending = {tc.id for tc in msg.tool_calls}
            end = index
            probe = index + 1
            # 只吞紧随其后的 tool 消息；遇到非 tool 消息立即停止，
            # 避免把下一轮的内容误并进本单元。
            while probe < total and messages[probe].role == "tool":
                if messages[probe].tool_call_id in pending:
                    pending.discard(messages[probe].tool_call_id)
                end = probe
                probe += 1
            units.append(Unit(index, end))
            index = end + 1
        else:
            units.append(Unit(index, index))
            index += 1
    return units


def unit_index_for(units: list[Unit], message_index: int) -> int:
    for i, unit in enumerate(units):
        if unit.start <= message_index <= unit.end:
            return i
    return 0


def unit_aligned_start(messages: list[Msg], desired_start: int) -> int:
    """把「从哪里开始保留」对齐到原子单元边界。

    任何按条数计算出来的切分点都必须先过这里。从 call/response 区间中间切开，
    会留下没有前置 ``tool_calls`` 的孤立 tool 消息，请求被上游直接判 400——
    而且历史一旦写回存储，该会话之后每一轮都会失败。

    向后取第一个 ``start >= desired_start`` 的单元（宁可多丢一点也不切坏）；
    若切分点落在最后一个单元内部，则退回该单元起点——**保留一个完整单元，
    哪怕略微超出条数上限**，因为结构正确性优先于容量精确。
    """
    if desired_start <= 0:
        return 0
    units = atomic_units(messages)
    if not units:
        return 0
    for unit in units:
        if unit.start >= desired_start:
            return unit.start
    return units[-1].start


# ── compact boundary ──────────────────────────────────────────────────────
# 等价 CC 的 `getMessagesAfterCompactBoundary`：压缩摘要本身即边界。
# 注意：`compact.compact()` 返回的是 `[摘要, *保留尾部]` —— 边界之前的原始历史
# 会被**整体替换掉**（同 CC），不再可回溯。本函数因此在正常链路上总是返回全量；
# 它的存在是为了让"边界"这个语义显式化，并兜住任何未来引入的追加式压缩实现。

def messages_after_boundary(messages: list[Msg]) -> list[Msg]:
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].kind == KIND_COMPACT_SUMMARY:
            return messages[i:]
    return list(messages)


# ── tool_result 体积治理 ───────────────────────────────────────────────────
# 对应 CC 的 `applyToolResultBudget`：只替换**超大 tool 消息**的正文，不丢消息。
# 与"整段丢弃"分工明确——整段丢弃交给 compact 摘要，这里只压单条巨型结果，
# 这样原子单元结构完全不受影响（消息条数不变，call/response 配对天然保持）。

_TRUNCATION_NOTE = "\n\n[本条工具结果因超出单条体积上限已截断；如需完整内容请缩小查询范围后重试]"
_ARTIFACT_READ_MAX_CHARS = 70 * 1024


def apply_tool_result_budget(messages: list[Msg], max_chars: int) -> int:
    """就地截断超长 tool 消息正文，返回被截断的条数。"""
    truncated = 0
    for msg in messages:
        if msg.role != "tool":
            continue
        text = _as_text(msg.content)
        message_limit = (
            max(max_chars, _ARTIFACT_READ_MAX_CHARS)
            if msg.name == "read_artifact"
            else max_chars
        )
        if len(text) <= message_limit:
            continue
        keep = max(0, message_limit - len(_TRUNCATION_NOTE))
        msg.content = text[:keep] + _TRUNCATION_NOTE
        truncated += 1
    return truncated


def clone(messages: Iterable[Msg]) -> list[Msg]:
    """浅拷贝消息列表（Msg 本身按值复制，避免体积治理污染原始历史）。"""
    out: list[Msg] = []
    for m in messages:
        out.append(
            Msg(
                role=m.role,
                content=m.content,
                tool_calls=list(m.tool_calls) if m.tool_calls else None,
                tool_call_id=m.tool_call_id,
                name=m.name,
                is_error=m.is_error,
                kind=m.kind,
            )
        )
    return out


@dataclass
class Usage:
    """一次模型调用的 token 用量（上游不返回时保持 None，由 compact 回落到字符估算）。"""

    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    extra: dict[str, Any] = field(default_factory=dict)
