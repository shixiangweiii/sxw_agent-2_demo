"""文档切分：markdown 段落感知 + 字符预算打包。

为离线/可移植，采用字符预算而非 tiktoken（避免首用下载 BPE 词表的网络依赖）。
"""
from __future__ import annotations

import re

from arag.store.base import Chunk, Document

_PARA_SPLIT = re.compile(r"\n\s*\n")
_SENT_SPLIT = re.compile(r"(?<=[。！？!?\.])\s+|\n")


def _split_sentences(text: str) -> list[str]:
    return [s for s in _SENT_SPLIT.split(text) if s and s.strip()]


class Chunker:
    def __init__(self, max_chars: int = 500, overlap: int = 80) -> None:
        self._max_chars = max_chars
        self._overlap = overlap

    def split(self, doc: Document) -> list[Chunk]:
        paragraphs = [p.strip() for p in _PARA_SPLIT.split(doc.content) if p.strip()]
        chunks: list[Chunk] = []
        buf = ""
        idx = 0

        def flush() -> None:
            nonlocal buf, idx
            content = buf.strip()
            if content:
                chunks.append(Chunk(
                    chunk_id=f"{doc.doc_id}#{idx}",
                    doc_id=doc.doc_id,
                    title=doc.title,
                    content=content,
                    metadata=dict(doc.metadata),
                ))
                idx += 1
            # 字符级 overlap：保留尾部一小段作为下一块前缀，提升跨块召回
            buf = content[-self._overlap:] if self._overlap and content else ""

        for para in paragraphs:
            if len(para) > self._max_chars:
                for sent in _split_sentences(para):
                    if buf and len(buf) + len(sent) + 1 > self._max_chars:
                        flush()
                    buf += ("\n" + sent if buf else sent)
                flush()
            else:
                if buf and len(buf) + len(para) + 1 > self._max_chars:
                    flush()
                buf += ("\n" + para if buf else para)
        flush()
        return chunks
