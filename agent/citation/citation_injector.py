"""CitationInjector：把 knowledge_search 命中映射为 id→文档，扫描正文 [n]，末尾补 citation 事件。

对应原项目 ID 协议 CitationInjector 的精简版：
- LLM 只在正文输出 [n] marker（不接触文件名/URL，避免幻觉）；
- 程序按检索命中维护 n→文档映射，仅对正文真实出现过的 [n] 生成引用块；
- 检索无命中 → 映射为空 → 不生成引用（guard：无资料不编造引用）。
"""
from __future__ import annotations

import re
from typing import Any, AsyncIterator, Optional

from agent.stream.event_converters import StreamEvent

_MARKER = re.compile(r"\[(\d+)\]")
KNOWLEDGE_TOOL = "knowledge_search"


class CitationInjector:
    def __init__(self) -> None:
        self._id_to_doc: dict[int, dict[str, str]] = {}
        self._used: set[int] = set()

    def register_hits(self, hits: list[dict[str, Any]]) -> None:
        for h in hits:
            try:
                n = int(h.get("n"))
            except (TypeError, ValueError):
                continue
            self._id_to_doc[n] = {"title": str(h.get("title", "")), "doc_id": str(h.get("doc_id", ""))}

    def scan_text(self, text: str) -> None:
        for m in _MARKER.finditer(text or ""):
            n = int(m.group(1))
            if n in self._id_to_doc:
                self._used.add(n)

    def build_event(self) -> Optional[StreamEvent]:
        if not self._used:
            return None
        refs = [
            {"n": n, "title": self._id_to_doc[n]["title"], "doc_id": self._id_to_doc[n]["doc_id"]}
            for n in sorted(self._used)
        ]
        return StreamEvent("citation", {"refs": refs})


async def with_citations(events: AsyncIterator[StreamEvent]) -> AsyncIterator[StreamEvent]:
    """包裹引擎事件流：记录检索命中、扫描正文 [n]，在 done 前补 citation 事件。

    对两代引擎通用（只要工具里有 knowledge_search）。
    """
    # 这是一个"透明中间件"：所有事件原样放行，只额外做两件事——
    #   1) 侦听 knowledge_search 的结果，建立 序号 n → 文档 的映射；
    #   2) 累积正文，在 done 之前补一个 citation 事件。
    # 之所以要程序端维护映射：LLM 只被允许输出 [n] 标记，不接触文件名/URL，
    # 这样"引用哪篇文档"由检索结果决定而不是由模型生成，从根上避免引用幻觉。
    injector = CitationInjector()
    answer_parts: list[str] = []
    async for ev in events:
        if ev.event == "tool_result" and ev.data.get("name") == KNOWLEDGE_TOOL:
            resp = ev.data.get("response")
            if isinstance(resp, dict) and isinstance(resp.get("hits"), list):
                injector.register_hits(resp["hits"])
            yield ev
        elif ev.event == "text":
            # 累积全文、done 时一次性扫描 [n]：避免 marker 被流式拆到多个 delta（如 "[" + "1]"）而漏识别。
            answer_parts.append(ev.data.get("delta", ""))
            yield ev
        elif ev.event == "done":
            # 在 done 之前才扫描：此时正文已完整，能正确识别被流式拆开的 marker。
            injector.scan_text("".join(answer_parts))
            citation = injector.build_event()
            # 只有正文里真的出现过、且能对上检索命中的 [n] 才会生成引用块；
            # 检索无命中 → 映射为空 → 这里返回 None → 不发 citation（不编造引用）。
            if citation is not None:
                yield citation
            yield ev
        else:
            yield ev
